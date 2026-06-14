import asyncio
import logging
import os
import re

log = logging.getLogger(__name__)
BATCH_SIZE = 5
GPOD_DRY_RUN = os.environ.get("GPOD_DRY_RUN", "").lower() in ("1", "true", "yes")

_PROGRESS_RE = re.compile(r'^\[\s*\d+/\d+\]')
_TITLE_ARTIST_RE = re.compile(r"title='(.*?)'\s+artist='(.*?)'")


class _Op:
    def __init__(self, kind: str, total: int):
        self.kind = kind
        self.status = "running"
        self.total = total
        self.processed = 0
        self.current = ""
        self.error: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "current": self.current,
            "error": self.error,
        }


class OperationService:
    def __init__(self) -> None:
        self._op: _Op | None = None
        self._lock = asyncio.Lock()

    def current(self) -> dict | None:
        return self._op.to_dict() if self._op else None

    def is_busy(self) -> bool:
        return self._op is not None and self._op.status == "running"

    async def run_delete(self, track_ids: list, mount: str) -> None:
        op = _Op("delete", len(track_ids))
        self._op = op
        asyncio.create_task(self._do_delete(op, track_ids, mount))

    async def run_sync(self, copy_paths: list[str], delete_ids: list, mount: str) -> None:
        op = _Op("sync", len(copy_paths) + len(delete_ids))
        self._op = op
        asyncio.create_task(self._do_sync(op, copy_paths, delete_ids, mount))

    async def _gpod_rm_batch(self, track_ids: list, mount: str, op: _Op) -> str | None:
        ids_str = list(dict.fromkeys(str(t) for t in track_ids))
        log.info("exec: IPOD_MOUNT_POINT=%s gpod-rm %s", mount, " ".join(ids_str))
        if GPOD_DRY_RUN:
            log.info("[dry-run] skipping gpod-rm (%d tracks)", len(ids_str))
            return None
        env = {**os.environ, "IPOD_MOUNT_POINT": mount}
        proc = await asyncio.create_subprocess_exec(
            "gpod-rm", *ids_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        lines = []
        async for raw in proc.stdout:
            line = raw.decode().rstrip()
            lines.append(line)
            if _PROGRESS_RE.match(line):
                m = _TITLE_ARTIST_RE.search(line)
                op.current = f"{m.group(2)} – {m.group(1)}" if m else line.split("->")[0].strip()
        await proc.wait()
        output = "\n".join(lines)
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

    async def _gpod_cp_batch(self, paths: list[str], mount: str, op: _Op) -> str | None:
        paths = list(dict.fromkeys(paths))
        log.info("exec: IPOD_MOUNT_POINT=%s gpod-cp %s", mount, " ".join(paths))
        if GPOD_DRY_RUN:
            log.info("[dry-run] skipping gpod-cp (%d file(s))", len(paths))
            return None
        env = {**os.environ, "IPOD_MOUNT_POINT": mount}
        proc = await asyncio.create_subprocess_exec(
            "gpod-cp", *paths,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        lines = []
        async for raw in proc.stdout:
            line = raw.decode().rstrip()
            lines.append(line)
            if _PROGRESS_RE.match(line):
                m = _TITLE_ARTIST_RE.search(line)
                if m:
                    op.current = f"{m.group(2)} – {m.group(1)}"
                else:
                    # fall back to filename from path before " ->"
                    path_part = line.split("->")[0].strip().split()[-1] if "->" in line else ""
                    op.current = os.path.basename(path_part) if path_part else line
        await proc.wait()
        output = "\n".join(lines)
        if output:
            log.debug("gpod-cp output:\n%s", output)
        if proc.returncode != 0:
            return output or f"gpod-cp exited {proc.returncode}"
        # "N/M items  dupl=D" — duplicates count as success
        m = re.search(r'(\d+)/(\d+)\s+items\s+dupl=(\d+)', output)
        if m:
            added, total, dupl = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if added + dupl < total:
                msg = f"gpod-cp partial: copied {added + dupl}/{total} items ({dupl} duplicate(s))"
                log.warning("%s\n%s", msg, output)
                return msg
        return None

    async def _do_delete(self, op: _Op, track_ids: list, mount: str) -> None:
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
                    return
                i += BATCH_SIZE
            op.status = "done"
            op.current = ""
            log.info("Delete done: %d tracks", op.total)

    async def _do_sync(self, op: _Op, copy_paths: list[str], delete_ids: list, mount: str) -> None:
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

            i = 0
            while i < len(copy_paths):
                batch = copy_paths[i:i + BATCH_SIZE]
                err = await self._gpod_cp_batch(batch, mount, op)
                op.processed += len(batch)
                if err:
                    op.status = "error"
                    op.error = err
                    log.error("gpod-cp batch failed: %s", err)
                    return
                i += BATCH_SIZE

            op.status = "done"
            op.current = ""
            log.info("Sync done: deleted %d, copied %d", len(delete_ids), len(copy_paths))


op_service = OperationService()
