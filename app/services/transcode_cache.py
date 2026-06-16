import asyncio
import errno
import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_DIR = Path(os.getenv("TRANSCODE_CACHE_DIR", "/tmp/nastune-cache"))
_LOW_SPACE_MB = 80  # evict unused files below this free threshold


class TranscodeCache:
    def __init__(self):
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._refs: dict[str, int] = {}  # abs path str → active reader count

    def _dst(self, src: str) -> Path:
        return _CACHE_DIR / (hashlib.sha1(src.encode()).hexdigest() + ".flac")

    async def get(self, src: str) -> Path:
        """Return path to cached FLAC file, transcoding from src if needed."""
        dst = self._dst(src)
        async with self._lock:
            if not dst.exists():
                await self._transcode(src, dst)
        return dst

    async def _transcode(self, src: str, dst: Path) -> None:
        self._evict_if_needed()
        tmp = dst.with_suffix(".part")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", src, "-c:a", "flac", str(tmp),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg exited {proc.returncode} for {src}")
            tmp.rename(dst)
            log.info("Transcoded → cache %s", dst.name)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                log.warning("tmpfs full even after eviction, cannot cache %s", src)
            raise
        finally:
            tmp.unlink(missing_ok=True)

    def acquire(self, src: str) -> None:
        k = str(self._dst(src))
        self._refs[k] = self._refs.get(k, 0) + 1

    def release(self, src: str) -> None:
        k = str(self._dst(src))
        n = self._refs.get(k, 1) - 1
        if n <= 0:
            self._refs.pop(k, None)
        else:
            self._refs[k] = n

    def _evict_if_needed(self) -> None:
        try:
            st = os.statvfs(_CACHE_DIR)
            free_mb = st.f_bavail * st.f_bsize / 1_048_576
            if free_mb >= _LOW_SPACE_MB:
                return
        except OSError:
            return
        active = set(self._refs)
        for f in sorted(
            (f for f in _CACHE_DIR.glob("*.flac") if str(f) not in active),
            key=lambda f: f.stat().st_atime,
        ):
            f.unlink(missing_ok=True)
            log.info("Evicted transcode cache %s", f.name)


transcode_cache = TranscodeCache()
