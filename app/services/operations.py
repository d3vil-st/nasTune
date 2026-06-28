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

_LOSSLESS_EXTS    = frozenset({'.flac', '.wav', '.aiff', '.aif', '.ape', '.wv'})
_PASSTHROUGH_EXTS = frozenset({'.mp3'})


async def _load_sync_settings(ipod_db_id: int | None = None) -> tuple[bool, int]:
    """Returns (force_aac, max_threads). Per-device override wins over global when set."""
    try:
        import aiosqlite
        async with aiosqlite.connect(_DB_PATH) as db:
            async with db.execute("SELECT key, value FROM settings") as cur:
                stored = {k: v for k, v in await cur.fetchall()}
        global_force_aac = stored.get("force_aac", "false") == "true"
        max_threads = int(stored.get("max_threads", "0") or "0")
        if ipod_db_id is not None:
            from app.services.ipod_db import get_effective_force_aac
            override = await get_effective_force_aac(ipod_db_id, "ipod")
            force_aac = global_force_aac if override is None else override
        else:
            force_aac = global_force_aac
        return force_aac, max_threads
    except Exception:
        return False, 0


def _classify_audio_path(path: str) -> str:
    """Return 'lossless', 'passthrough', or 'lossy' for a file or directory path."""
    p = Path(path.rstrip('/'))
    if p.is_dir():
        for f in sorted(p.rglob('*')):
            if f.is_file() and f.suffix.lower() in _LOSSLESS_EXTS | _PASSTHROUGH_EXTS | {'.m4a', '.aac', '.ogg', '.wma', '.opus'}:
                return _classify_audio_path(str(f))
        return 'lossy'
    ext = p.suffix.lower()
    if ext in _LOSSLESS_EXTS:
        return 'lossless'
    if ext in _PASSTHROUGH_EXTS:
        return 'passthrough'
    if ext == '.m4a':
        try:
            from mutagen.mp4 import MP4
            codec = (MP4(str(p)).info.codec or '').lower()
            if 'alac' in codec:
                return 'lossless'
            if 'aac' in codec or 'mp4a' in codec:
                return 'passthrough'
        except Exception:
            pass
    return 'lossy'


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
            "started_at": self.started_at,
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

    async def run_sync(self, copy_paths: list[str], delete_ids: list, mount: str, device_id: str = "", copy_track_count: int | None = None, media_type: str = "music", ipod_db_id: int | None = None) -> None:
        total = (copy_track_count if copy_track_count is not None else len(copy_paths)) + len(delete_ids)
        op = _Op("sync", total)
        self._op = op
        asyncio.create_task(self._do_sync(op, copy_paths, delete_ids, mount, device_id, media_type, ipod_db_id))

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

    _LOSSLESS_ARGS    = ['--encoder', 'alac', '--disable-encoder-fallback']
    _PASSTHROUGH_ARGS: list[str] = []
    _LOSSY_ARGS       = ['--encoder', 'fdk-aac', '--encoder-quality', '9', '--disable-encoder-fallback']

    async def _gpod_cp_batch(self, paths: list[str], mount: str, op: _Op, proc_offset: int = 0,
                              *, force_aac: bool = False, threads: int = 0,
                              media_type_args: list[str] = ()) -> tuple[str | None, int]:
        """Split paths by format and dispatch with the appropriate encoder.

        lossless (FLAC/WAV/…) → ALAC
        passthrough (MP3)      → copied as-is (no encoder args)
        lossy (AAC/OGG/…)     → fdk-aac
        force_aac=True         → everything goes through fdk-aac (passthrough ignored)
        media_type_args        → e.g. ['--tracks-media-type', 'audiobook']
        """
        paths = list(dict.fromkeys(paths))
        if force_aac:
            lossless, passthrough, lossy = [], [], paths
        else:
            lossless, passthrough, lossy = [], [], []
            for p in paths:
                cls = _classify_audio_path(p)
                if cls == 'lossless':
                    lossless.append(p)
                elif cls == 'passthrough':
                    passthrough.append(p)
                else:
                    lossy.append(p)

        total = 0
        for group, extra_args in (
            (lossless,    self._LOSSLESS_ARGS),
            (passthrough, self._PASSTHROUGH_ARGS),
            (lossy,       self._LOSSY_ARGS),
        ):
            if not group:
                continue
            err, n = await self._gpod_cp_exec(
                group, extra_args, mount, op, proc_offset + total,
                threads=threads, media_type_args=media_type_args,
            )
            total += n
            if err:
                return err, total
        return None, total

    async def _gpod_cp_exec(self, paths: list[str], extra_args: list[str], mount: str, op: _Op,
                             proc_offset: int = 0, *, threads: int = 0,
                             media_type_args: list[str] = ()) -> tuple[str | None, int]:
        """Execute gpod-cp with given extra args. Returns (error, track_count)."""
        thread_args = ['--threads', str(threads)] if threads > 0 else []
        all_flags = extra_args + thread_args + media_type_args
        cmd_str = f"gpod-cp {' '.join(all_flags)} {' '.join(paths)}"
        log.info("exec: IPOD_MOUNT_POINT=%s %s", mount, cmd_str)
        op.log.append(f"$ {cmd_str}")
        if GPOD_DRY_RUN:
            log.info("[dry-run] skipping gpod-cp (%d path(s))", len(paths))
            op.log.append("[dry-run] skipped")
            return None, 0
        env = {**os.environ, "IPOD_MOUNT_POINT": mount}
        proc = await asyncio.create_subprocess_exec(
            "gpod-cp", *extra_args, *thread_args, *media_type_args, *paths,
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

    async def _gpod_rating_sync(self, mount: str, op: _Op) -> None:
        """Run gpod-ls then apply stored DB ratings to iPod tracks that need updating."""
        import aiosqlite
        from app.services.db import DB_PATH
        from app.services.gpod import fetch_library
        from app.services.ratings import ipod_rating_to_stars
        from app.services.track_key import track_key as _tk

        # Skip the expensive gpod-ls entirely when no ratings are stored
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT COUNT(*) FROM ipod_track_ratings') as cur:
                if (await cur.fetchone())[0] == 0:
                    return

        op.current = "Rating sync: reading library…"
        op.log.append("$ gpod-ls  # rating sync")
        try:
            lib = await fetch_library(mount)
        except Exception as exc:
            log.warning("rating sync: gpod-ls failed, skipping: %s", exc)
            op.log.append(f"  WARN: rating sync skipped ({exc})")
            return

        # Build {track_key -> (id, current_stars)} from fresh library
        track_map: dict[str, tuple[int, int]] = {}
        for artist in lib.get('artists', []):
            artist_name = artist['name']
            for album in artist.get('albums', []):
                album_name = album['name']
                for track in album.get('tracks', []):
                    artist_tag = track.get('artist') or artist_name
                    key = _tk(
                        artist_tag, album_name,
                        track.get('track_nr'), track.get('disc_nr'),
                        track.get('title', ''),
                    )
                    track_map[key] = (track['id'], ipod_rating_to_stars(track.get('rating') or 0))

        if not track_map:
            return

        keys = list(track_map.keys())
        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ','.join('?' * len(keys))
            async with db.execute(
                f'SELECT track_key, rating FROM ipod_track_ratings WHERE track_key IN ({placeholders})',
                keys,
            ) as cur:
                stored = {row[0]: row[1] for row in await cur.fetchall()}

        # Group by rating value; only update tracks where stored rating > current iPod rating
        by_rating: dict[int, list[int]] = {}
        for key, stored_stars in stored.items():
            if key not in track_map:
                continue
            track_id, current_stars = track_map[key]
            if stored_stars > 0 and stored_stars > current_stars:
                by_rating.setdefault(stored_stars, []).append(track_id)

        if not by_rating:
            log.info("rating sync: no tracks need rating update")
            op.log.append("  rating sync: nothing to update")
            return

        total = sum(len(ids) for ids in by_rating.values())
        log.info("rating sync: applying ratings to %d track(s)", total)

        for stars, track_ids in sorted(by_rating.items()):
            ids_str = [str(i) for i in track_ids]
            op.current = f"Rating sync: {stars}★ × {len(track_ids)}"
            op.log.append(f"$ gpod-tag --rating {stars} {' '.join(ids_str)}")
            if GPOD_DRY_RUN:
                op.log.append("[dry-run] skipped")
                continue
            env = {**os.environ, "IPOD_MOUNT_POINT": mount}
            proc = await asyncio.create_subprocess_exec(
                "gpod-tag", "--rating", str(stars), *ids_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            async for raw in proc.stdout:
                op.log.append(raw.decode().rstrip())
            await proc.wait()
            if proc.returncode != 0:
                log.warning("gpod-tag --rating %d exited %d", stars, proc.returncode)
            else:
                log.info("gpod-tag --rating %d applied to %d track(s)", stars, len(track_ids))

    async def _do_sync(self, op: _Op, copy_paths: list[str], delete_ids: list, mount: str, device_id: str = "", media_type: str = "music", ipod_db_id: int | None = None) -> None:
        force_aac, threads = await _load_sync_settings(ipod_db_id=ipod_db_id)
        media_type_args = ['--tracks-media-type', media_type] if media_type in ('audiobook', 'podcast') else []
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
                err, batch_tracks = await self._gpod_cp_batch(
                    batch, mount, op, proc_offset=len(delete_ids) + copy_offset,
                    force_aac=force_aac, threads=threads, media_type_args=media_type_args,
                )
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

            await self._gpod_rating_sync(mount, op)

            op.status = "done"
            op.current = ""
            log.info("Sync done: deleted %d, copied %d", len(delete_ids), len(copy_paths))
            if device_id:
                _save_op_history(op, device_id)


    # ── gpod-verify ──────────────────────────────────────────────────

    async def run_verify(self, mode: str, mount: str, device_id: str = "") -> None:
        op = _Op("verify", 0)
        self._op = op
        asyncio.create_task(self._do_verify(op, mode, mount, device_id))

    async def _do_verify(self, op: _Op, mode: str, mount: str, device_id: str) -> None:
        flag = {"add": "--add", "delete": "--delete"}.get(mode)
        cmd = ["gpod-verify", "-M", mount] + ([flag] if flag else [])
        log.info("exec: %s", " ".join(cmd))
        op.log.append(f"$ {' '.join(cmd)}")
        async with self._lock:
            if GPOD_DRY_RUN:
                op.log.append("[dry-run] skipped")
                op.status = "done"
                if device_id:
                    _save_op_history(op, device_id)
                return
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                async for raw in proc.stdout:
                    line = raw.decode().rstrip()
                    op.log.append(line)
                    op.current = line
                await proc.wait()
                if proc.returncode != 0:
                    op.status = "error"
                    op.error = f"gpod-verify exited {proc.returncode}"
                else:
                    op.status = "done"
                    op.current = ""
            except Exception as exc:
                op.status = "error"
                op.error = str(exc)
            finally:
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
