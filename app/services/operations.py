import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)
BATCH_SIZE = 50
GPOD_DRY_RUN = os.environ.get("GPOD_DRY_RUN", "").lower() in ("1", "true", "yes")

_DB_PATH = Path(os.environ.get("DB_PATH", "/data/nastune.db"))
_OP_HISTORY_DIR = _DB_PATH.parent / "op_history"


def _save_op_history(op: "_Op", device_id: str) -> None:
    # device_id is the iPod UUID (preferred) or sanitized devnode as fallback
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", device_id)
    dir_path = _OP_HISTORY_DIR / safe
    dir_path.mkdir(parents=True, exist_ok=True)
    finished_at = time.time()
    ts = time.strftime("%Y%m%dT%H%M%S", time.localtime(op.started_at))
    record = {
        "id": ts,
        "device_id": device_id,
        "kind": op.kind,
        "status": op.status,
        "total": op.total,
        "processed": op.processed,
        "started_at": op.started_at,
        "finished_at": finished_at,
        "duration_s": round(finished_at - op.started_at, 1),
        "error": op.error,
        "log": op.log,
    }
    (dir_path / f"{ts}.json").write_text(json.dumps(record, ensure_ascii=False))
    for old in sorted(dir_path.glob("*.json"))[:-10]:
        old.unlink(missing_ok=True)

_PROGRESS_RE = re.compile(r'^\[\s*\d+/\d+\]')
_PROGRESS_NM_RE = re.compile(r'^\[\s*(\d+)/(\d+)\]')
_TITLE_ARTIST_RE = re.compile(r"title='(.*?)'\s+artist='(.*?)'")


class _Op:
    def __init__(self, kind: str, total: int):
        self.kind = kind
        self.status = "running"
        self.total = total
        self.processed = 0
        self.current = ""
        self.error: str | None = None
        self.log: list[str] = []
        self.started_at = time.time()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "current": self.current,
            "error": self.error,
            "log": self.log,
        }


class OperationService:
    def __init__(self) -> None:
        self._op: _Op | None = None
        self._lock = asyncio.Lock()

    def current(self) -> dict | None:
        return self._op.to_dict() if self._op else None

    def is_busy(self) -> bool:
        return self._op is not None and self._op.status == "running"

    async def run_delete(self, track_ids: list, mount: str, device_id: str = "") -> None:
        op = _Op("delete", len(track_ids))
        self._op = op
        asyncio.create_task(self._do_delete(op, track_ids, mount, device_id))

    async def run_sync(self, copy_paths: list[str], delete_ids: list, mount: str, device_id: str = "", copy_track_count: int | None = None) -> None:
        total = (copy_track_count if copy_track_count is not None else len(copy_paths)) + len(delete_ids)
        op = _Op("sync", total)
        self._op = op
        asyncio.create_task(self._do_sync(op, copy_paths, delete_ids, mount, device_id))

    async def _gpod_rm_batch(self, track_ids: list, mount: str, op: _Op) -> str | None:
        ids_str = list(dict.fromkeys(str(t) for t in track_ids))
        log.info("exec: IPOD_MOUNT_POINT=%s gpod-rm %s", mount, " ".join(ids_str))
        op.log.append(f"$ gpod-rm {' '.join(ids_str)}")
        if GPOD_DRY_RUN:
            log.info("[dry-run] skipping gpod-rm (%d tracks)", len(ids_str))
            op.log.append("[dry-run] skipped")
            return None
        env = {**os.environ, "IPOD_MOUNT_POINT": mount}
        proc = await asyncio.create_subprocess_exec(
            "gpod-rm", *ids_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        batch_lines = []
        async for raw in proc.stdout:
            line = raw.decode().rstrip()
            batch_lines.append(line)
            op.log.append(line)
            if _PROGRESS_RE.match(line):
                m = _TITLE_ARTIST_RE.search(line)
                op.current = f"{m.group(2)} – {m.group(1)}" if m else line.split("->")[0].strip()
        await proc.wait()
        output = "\n".join(batch_lines)
        if output:
            log.debug("gpod-rm output:\n%s", output)
        if proc.returncode != 0:
            return output or f"gpod-rm exited {proc.returncode}"
        m = re.search(r'removed\s+(\d+)/(\d+)\s+items', output)
        if m:
            removed, total = int(m.group(1)), int(m.group(2))
            if removed != total:
                msg = f"gpod-rm partial: removed {removed}/{total} items"
                log.warning("%s\n%s", msg, output)
                return msg
        return None

    async def _gpod_cp_batch(self, paths: list[str], mount: str, op: _Op, proc_offset: int = 0) -> tuple[str | None, int]:
        """Returns (error, batch_track_count). Updates op.processed live via [N/M] lines."""
        paths = list(dict.fromkeys(paths))
        log.info("exec: IPOD_MOUNT_POINT=%s gpod-cp %s", mount, " ".join(paths))
        op.log.append(f"$ gpod-cp {' '.join(paths)}")
        if GPOD_DRY_RUN:
            log.info("[dry-run] skipping gpod-cp (%d path(s))", len(paths))
            op.log.append("[dry-run] skipped")
            return None, 0
        env = {**os.environ, "IPOD_MOUNT_POINT": mount}
        proc = await asyncio.create_subprocess_exec(
            "gpod-cp", *paths,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        batch_lines = []
        batch_total = 0
        async for raw in proc.stdout:
            line = raw.decode().rstrip()
            batch_lines.append(line)
            op.log.append(line)
            nm = _PROGRESS_NM_RE.match(line)
            if nm:
                n, batch_total = int(nm.group(1)), int(nm.group(2))
                op.processed = proc_offset + n
                m = _TITLE_ARTIST_RE.search(line)
                if m:
                    op.current = f"{m.group(2)} – {m.group(1)}"
                else:
                    path_part = line.split("->")[0].strip().split()[-1] if "->" in line else ""
                    op.current = os.path.basename(path_part) if path_part else line
        await proc.wait()
        output = "\n".join(batch_lines)
        if output:
            log.debug("gpod-cp output:\n%s", output)
        if proc.returncode != 0:
            return output or f"gpod-cp exited {proc.returncode}", batch_total
        # "N/M items (size)  dupl=D" — duplicates count as success
        m = re.search(r'(\d+)/(\d+)\s+items.*?dupl=(\d+)', output)
        if m:
            added, total, dupl = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if batch_total == 0:
                batch_total = total
            if added + dupl < total:
                msg = f"gpod-cp partial: copied {added + dupl}/{total} items ({dupl} duplicate(s))"
                log.warning("%s\n%s", msg, output)
                return msg, batch_total
        return None, batch_total

    async def _do_delete(self, op: _Op, track_ids: list, mount: str, device_id: str = "") -> None:
        async with self._lock:
            track_ids = sorted(track_ids, key=lambda x: int(x), reverse=True)
            i = 0
            while i < len(track_ids):
                batch = track_ids[i:i + BATCH_SIZE]
                err = await self._gpod_rm_batch(batch, mount, op)
                op.processed += len(batch)
                if err:
                    op.status = "error"
                    op.error = err
                    log.error("gpod-rm batch failed: %s", err)
                    if device_id:
                        _save_op_history(op, device_id)
                    return
                i += BATCH_SIZE
            op.status = "done"
            op.current = ""
            log.info("Delete done: %d tracks", op.total)
            if device_id:
                _save_op_history(op, device_id)

    async def _do_sync(self, op: _Op, copy_paths: list[str], delete_ids: list, mount: str, device_id: str = "") -> None:
        async with self._lock:
            # Delete highest IDs first so lower IDs don't shift after each batch commit
            delete_ids = sorted(delete_ids, key=lambda x: int(x), reverse=True)
            i = 0
            while i < len(delete_ids):
                batch = delete_ids[i:i + BATCH_SIZE]
                err = await self._gpod_rm_batch(batch, mount, op)
                op.processed += len(batch)
                if err:
                    op.status = "error"
                    op.error = err
                    log.error("gpod-rm batch failed: %s", err)
                    return
                i += BATCH_SIZE

            copy_offset = 0
            i = 0
            while i < len(copy_paths):
                batch = copy_paths[i:i + BATCH_SIZE]
                err, batch_tracks = await self._gpod_cp_batch(batch, mount, op, proc_offset=len(delete_ids) + copy_offset)
                copy_offset += batch_tracks
                op.processed = len(delete_ids) + copy_offset
                if err:
                    op.status = "error"
                    op.error = err
                    log.error("gpod-cp batch failed: %s", err)
                    if device_id:
                        _save_op_history(op, device_id)
                    return
                i += BATCH_SIZE

            op.status = "done"
            op.current = ""
            log.info("Sync done: deleted %d, copied %d", len(delete_ids), len(copy_paths))
            if device_id:
                _save_op_history(op, device_id)


    # ── WALKMAN operations (cp/rm via shutil) ────────────────────────

    async def run_walkman_delete(self, track_db_ids: list[int], mount: str,
                                  walkman_db_id: int, device_id: str = "") -> None:
        op = _Op("delete", len(track_db_ids))
        self._op = op
        asyncio.create_task(self._do_walkman_delete(op, track_db_ids, mount, walkman_db_id, device_id))

    async def run_walkman_sync(self, copy_paths: list[str], delete_db_ids: list[int],
                                mount: str, music_path: str, walkman_db_id: int,
                                device_id: str = "", copy_track_count: int | None = None) -> None:
        total = (copy_track_count if copy_track_count is not None else len(copy_paths)) + len(delete_db_ids)
        op = _Op("sync", total)
        self._op = op
        asyncio.create_task(self._do_walkman_sync(
            op, copy_paths, delete_db_ids, mount, music_path, walkman_db_id, device_id))

    async def _do_walkman_delete(self, op: _Op, track_db_ids: list[int], mount: str,
                                   walkman_db_id: int, device_id: str) -> None:
        import aiosqlite
        async with self._lock:
            mount_path = Path(mount)
            async with aiosqlite.connect(_DB_PATH) as db:
                placeholders = ','.join('?' * len(track_db_ids))
                async with db.execute(
                    f'SELECT id, path FROM walkman_tracks WHERE id IN ({placeholders}) AND device_id=?',
                    (*track_db_ids, walkman_db_id),
                ) as cur:
                    rows = await cur.fetchall()

            for row_id, rel_path in rows:
                full = mount_path / rel_path
                op.log.append(f"$ rm {full}")
                try:
                    await asyncio.to_thread(os.remove, str(full))
                    # Remove empty parent dirs up to music root (best-effort)
                    try:
                        parent = full.parent
                        while parent != mount_path and not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent
                    except OSError:
                        pass
                except FileNotFoundError:
                    op.log.append(f"  WARN: already gone: {full}")
                except Exception as exc:
                    op.status = "error"
                    op.error = str(exc)
                    log.error("rm failed for %s: %s", full, exc)
                    if device_id:
                        _save_op_history(op, device_id)
                    return
                op.processed += 1
                op.current = full.name

            async with aiosqlite.connect(_DB_PATH) as db:
                placeholders = ','.join('?' * len(track_db_ids))
                await db.execute(
                    f'DELETE FROM walkman_tracks WHERE id IN ({placeholders}) AND device_id=?',
                    (*track_db_ids, walkman_db_id),
                )
                async with db.execute(
                    'SELECT COUNT(*) FROM walkman_tracks WHERE device_id=?', (walkman_db_id,)
                ) as cur:
                    count = (await cur.fetchone())[0]
                await db.execute(
                    'UPDATE walkman_devices SET track_count=? WHERE id=?', (count, walkman_db_id)
                )
                await db.commit()

            from app.services.devices import device_service as _ds
            _ds.invalidate_walkman_cache(walkman_db_id)

            op.status = "done"
            op.current = ""
            log.info("WALKMAN delete done: %d tracks", op.total)
            if device_id:
                _save_op_history(op, device_id)

    async def _do_walkman_sync(self, op: _Op, copy_paths: list[str], delete_db_ids: list[int],
                                 mount: str, music_path: str, walkman_db_id: int, device_id: str) -> None:
        import aiosqlite
        async with self._lock:
            mount_path = Path(mount)
            music_dir = mount_path / music_path

            # Fetch source roots for computing dest paths
            async with aiosqlite.connect(_DB_PATH) as db:
                async with db.execute('SELECT path FROM sources') as cur:
                    source_roots = [row[0] for row in await cur.fetchall()]

            # 1. Delete
            if delete_db_ids:
                async with aiosqlite.connect(_DB_PATH) as db:
                    placeholders = ','.join('?' * len(delete_db_ids))
                    async with db.execute(
                        f'SELECT id, path FROM walkman_tracks WHERE id IN ({placeholders}) AND device_id=?',
                        (*delete_db_ids, walkman_db_id),
                    ) as cur:
                        del_rows = await cur.fetchall()

                for row_id, rel_path in del_rows:
                    full = mount_path / rel_path
                    op.log.append(f"$ rm {full}")
                    try:
                        await asyncio.to_thread(os.remove, str(full))
                        try:
                            parent = full.parent
                            while parent != mount_path and not any(parent.iterdir()):
                                parent.rmdir()
                                parent = parent.parent
                        except OSError:
                            pass
                    except FileNotFoundError:
                        op.log.append(f"  WARN: already gone: {full}")
                    except Exception as exc:
                        op.status = "error"
                        op.error = str(exc)
                        log.error("rm failed for %s: %s", full, exc)
                        if device_id:
                            _save_op_history(op, device_id)
                        return
                    op.processed += 1
                    op.current = full.name

                async with aiosqlite.connect(_DB_PATH) as db:
                    placeholders = ','.join('?' * len(delete_db_ids))
                    await db.execute(
                        f'DELETE FROM walkman_tracks WHERE id IN ({placeholders}) AND device_id=?',
                        (*delete_db_ids, walkman_db_id),
                    )
                    await db.commit()

            # 2. Copy — track destination files so we can index them in the DB immediately
            newly_copied: list[Path] = []
            for src_str in copy_paths:
                src = Path(src_str)
                rel = _find_rel(src, source_roots)
                if rel is None:
                    op.log.append(f"WARN: {src} not under any source root, skipping")
                    continue
                dest = music_dir / rel
                try:
                    if src.is_file():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        op.log.append(f"$ cp {src} {dest}")
                        await asyncio.to_thread(shutil.copy2, str(src), str(dest))
                        newly_copied.append(dest)
                        op.processed += 1
                        op.current = src.name
                    else:
                        # Walk directory, copy file by file for per-track progress
                        for fpath in sorted(src.rglob('*')):
                            if not fpath.is_file():
                                continue
                            fdest = dest / fpath.relative_to(src)
                            fdest.parent.mkdir(parents=True, exist_ok=True)
                            op.log.append(f"$ cp {fpath} {fdest}")
                            await asyncio.to_thread(shutil.copy2, str(fpath), str(fdest))
                            newly_copied.append(fdest)
                            op.processed += 1
                            op.current = fpath.name
                except Exception as exc:
                    op.status = "error"
                    op.error = str(exc)
                    log.error("cp failed for %s: %s", src, exc)
                    if device_id:
                        _save_op_history(op, device_id)
                    return

            # 3. Index newly copied files directly into the DB (no full rescan needed)
            if newly_copied:
                from app.services.walkman import _read_track as wm_read, _flush_batch as wm_flush
                now = int(time.time())
                batch: list[dict] = []
                for dest_file in newly_copied:
                    track = await asyncio.to_thread(wm_read, dest_file, mount_path)
                    if track:
                        track['device_id'] = walkman_db_id
                        track['scanned_at'] = now
                        batch.append(track)
                if batch:
                    await wm_flush(batch)

            # 4. Refresh track_count
            async with aiosqlite.connect(_DB_PATH) as db:
                async with db.execute(
                    'SELECT COUNT(*) FROM walkman_tracks WHERE device_id=?', (walkman_db_id,)
                ) as cur:
                    count = (await cur.fetchone())[0]
                await db.execute(
                    'UPDATE walkman_devices SET track_count=? WHERE id=?', (count, walkman_db_id)
                )
                await db.commit()

            from app.services.devices import device_service as _ds
            _ds.invalidate_walkman_cache(walkman_db_id)

            op.status = "done"
            op.current = ""
            log.info("WALKMAN sync done: deleted %d, copied %d files", len(delete_db_ids), len(newly_copied))
            if device_id:
                _save_op_history(op, device_id)


def _find_rel(src: Path, source_roots: list[str]):
    """Return src relative to the first matching source root, or None."""
    for root in source_roots:
        root_p = Path(root)
        try:
            return src.relative_to(root_p)
        except ValueError:
            continue
    return None


op_service = OperationService()
