import asyncio
import logging
import os

log = logging.getLogger(__name__)
CPU_COUNT = max(1, os.cpu_count() or 1)
GPOD_DRY_RUN = os.environ.get("GPOD_DRY_RUN", "").lower() in ("1", "true", "yes")


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

    async def _gpod_rm(self, track_id, mount: str) -> str | None:
        log.info("exec: IPOD_MOUNT_POINT=%s gpod-rm %s", mount, track_id)
        if GPOD_DRY_RUN:
            log.info("[dry-run] skipping gpod-rm %s", track_id)
            return None
        env = {**os.environ, "IPOD_MOUNT_POINT": mount}
        proc = await asyncio.create_subprocess_exec(
            "gpod-rm", str(track_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return out.decode().strip() or f"gpod-rm exited {proc.returncode}"
        return None

    async def _gpod_cp_batch(self, paths: list[str], mount: str) -> str | None:
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
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return out.decode().strip() or f"gpod-cp exited {proc.returncode}"
        return None

    async def _do_delete(self, op: _Op, track_ids: list, mount: str) -> None:
        for tid in track_ids:
            op.current = str(tid)
            err = await self._gpod_rm(tid, mount)
            op.processed += 1
            if err:
                op.status = "error"
                op.error = err
                log.error("gpod-rm %s failed: %s", tid, err)
                return
        op.status = "done"
        op.current = ""
        log.info("Delete done: %d tracks", op.total)

    async def _do_sync(self, op: _Op, copy_paths: list[str], delete_ids: list, mount: str) -> None:
        # Delete first, then copy
        for tid in delete_ids:
            op.current = str(tid)
            err = await self._gpod_rm(tid, mount)
            op.processed += 1
            if err:
                op.status = "error"
                op.error = err
                log.error("gpod-rm %s failed: %s", tid, err)
                return

        # Copy in batches of CPU_COUNT paths per gpod-cp invocation
        i = 0
        while i < len(copy_paths):
            batch = copy_paths[i:i + CPU_COUNT]
            op.current = os.path.basename(batch[0])
            err = await self._gpod_cp_batch(batch, mount)
            op.processed += len(batch)
            if err:
                op.status = "error"
                op.error = err
                log.error("gpod-cp batch failed: %s", err)
                return
            i += CPU_COUNT

        op.status = "done"
        op.current = ""
        log.info("Sync done: deleted %d, copied %d", len(delete_ids), len(copy_paths))


op_service = OperationService()
