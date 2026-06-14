import asyncio
import logging
import os

log = logging.getLogger(__name__)
BATCH_SIZE = 5
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

    async def _gpod_rm_batch(self, track_ids: list, mount: str) -> str | None:
        ids_str = [str(t) for t in track_ids]
        log.info("exec: IPOD_MOUNT_POINT=%s gpod-rm %s", mount, " ".join(ids_str))
        if GPOD_DRY_RUN:
            log.info("[dry-run] skipping gpod-rm (%d tracks)", len(track_ids))
            return None
        env = {**os.environ, "IPOD_MOUNT_POINT": mount}
        proc = await asyncio.create_subprocess_exec(
            "gpod-rm", *ids_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await proc.communicate()
        output = out.decode().strip()
        if output:
            log.debug("gpod-rm output:\n%s", output)
        if proc.returncode != 0:
            return output or f"gpod-rm exited {proc.returncode}"
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
        output = out.decode().strip()
        if output:
            log.debug("gpod-cp output:\n%s", output)
        if proc.returncode != 0:
            return output or f"gpod-cp exited {proc.returncode}"
        return None

    async def _do_delete(self, op: _Op, track_ids: list, mount: str) -> None:
        async with self._lock:
            i = 0
            while i < len(track_ids):
                batch = track_ids[i:i + BATCH_SIZE]
                op.current = str(batch[0]) if len(batch) == 1 else f"{batch[0]}…{batch[-1]}"
                err = await self._gpod_rm_batch(batch, mount)
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
            # Delete first, in batches
            i = 0
            while i < len(delete_ids):
                batch = delete_ids[i:i + BATCH_SIZE]
                op.current = str(batch[0]) if len(batch) == 1 else f"{batch[0]}…{batch[-1]}"
                err = await self._gpod_rm_batch(batch, mount)
                op.processed += len(batch)
                if err:
                    op.status = "error"
                    op.error = err
                    log.error("gpod-rm batch failed: %s", err)
                    return
                i += BATCH_SIZE

            # Copy in batches
            i = 0
            while i < len(copy_paths):
                batch = copy_paths[i:i + BATCH_SIZE]
                op.current = os.path.basename(batch[0])
                err = await self._gpod_cp_batch(batch, mount)
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
