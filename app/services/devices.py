import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.services.fs_utils import FS_LABELS, fs_type, fs_usage

log = logging.getLogger(__name__)

MOUNT_BASE = Path(os.environ.get("IPOD_MOUNT_BASE", "/mnt/ipods"))
AUTOMOUNT = os.environ.get("IPOD_AUTOMOUNT", "0").strip().lower() in ("1", "true", "yes")
_IPOD_SENTINEL = "iPod_Control/iTunes/iTunesDB"
_IPOD_FSTYPES = {"vfat", "hfsplus", "hfs", "exfat", "msdos"}


def _log_mount_contents(mount: Path) -> None:
    """Log the top two levels of a mount point to diagnose missing sentinel."""
    try:
        top = sorted(mount.iterdir(), key=lambda p: p.name.lower())
    except Exception as exc:
        log.warning("  Cannot list %s: %s", mount, exc)
        return

    if not top:
        log.warning("  Mount point is empty — iPod may not be mounted")
        return

    log.warning("  Contents of %s: %s", mount, [p.name for p in top])

    ipod_ctrl = next((p for p in top if p.name.lower() == "ipod_control"), None)
    if ipod_ctrl:
        log.warning("  Found control dir as '%s' (expected 'iPod_Control')", ipod_ctrl.name)
        try:
            sub = sorted(ipod_ctrl.iterdir(), key=lambda p: p.name.lower())
            log.warning("  Contents of %s: %s", ipod_ctrl, [p.name for p in sub])
            itunes_dir = next((p for p in sub if p.name.lower() == "itunes"), None)
            if itunes_dir:
                db = sorted(itunes_dir.iterdir(), key=lambda p: p.name.lower())
                log.warning("  Contents of %s: %s", itunes_dir, [p.name for p in db])
        except Exception as exc:
            log.warning("  Cannot list %s: %s", ipod_ctrl, exc)
    else:
        log.warning("  No 'iPod_Control' directory found (case-insensitive search also failed)")


@dataclass
class DeviceInfo:
    devnode: str    # "/dev/sdb1" or "manual"
    devname: str    # "sdb1" or "manual"
    mount: str      # absolute mount path; "" if not yet mounted
    fstype: str     # display label e.g. "FAT32"
    size_bytes: int
    is_ipod: bool
    manual: bool = False
    mounted: bool = True


class DeviceService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.devices: dict[str, DeviceInfo] = {}  # devnode -> info
        self.selected: str | None = None
        self._cache: dict[str, dict] = {}          # devnode -> parsed library
        self._subscribers: set[asyncio.Queue] = set()
        self._active_streams: dict[str, int] = {}  # devnode -> open stream count
        self._loading: set[str] = set()            # devnodes currently running gpod-ls
        self._ejected: set[str] = set()            # devnodes explicitly ejected by user; skip auto-mount until physical disconnect

    # ── Stream tracking ──────────────────────────────────────────────

    def stream_start(self, devnode: str) -> None:
        self._active_streams[devnode] = self._active_streams.get(devnode, 0) + 1

    def stream_end(self, devnode: str) -> None:
        n = self._active_streams.get(devnode, 0)
        if n > 1:
            self._active_streams[devnode] = n - 1
        else:
            self._active_streams.pop(devnode, None)

    def is_busy(self, devnode: str) -> bool:
        return self._active_streams.get(devnode, 0) > 0 or devnode in self._loading

    async def start(self) -> None:
        debug = os.environ.get("IPOD_MOUNT_POINT")
        if debug:
            log.info("Device service: registering IPOD_MOUNT_POINT=%s as manual device", debug)
            await self._init_debug(debug)
        log.info("Device service: starting auto-discovery poll loop (mount base: %s, automount: %s)",
                 MOUNT_BASE, "enabled" if AUTOMOUNT else "disabled")
        asyncio.create_task(self._poll_loop())

    # ── Debug / manual mode ──────────────────────────────────────────

    async def _init_debug(self, mount_str: str) -> None:
        mount = Path(mount_str)
        sentinel = mount / _IPOD_SENTINEL
        is_ipod = sentinel.exists()
        total, _ = await asyncio.to_thread(fs_usage, mount)
        fst = fs_type(mount)

        if is_ipod:
            log.info("Debug device: iPod sentinel found at %s", sentinel)
        else:
            log.warning("Debug device: iPod sentinel NOT found at %s — treating as non-iPod", sentinel)
            _log_mount_contents(mount)

        log.info("Debug device registered: mount=%s fstype=%s size=%.1f GB is_ipod=%s",
                 mount_str, fst, total / 1024 ** 3, is_ipod)

        info = DeviceInfo(
            devnode="manual",
            devname="manual",
            mount=mount_str,
            fstype=fst,
            size_bytes=total,
            is_ipod=is_ipod,
            manual=True,
        )
        async with self._lock:
            self.devices["manual"] = info
            self.selected = "manual"
        self._broadcast()

    # ── Auto-discovery polling ────────────────────────────────────────

    async def _poll_loop(self) -> None:
        log.debug("Poll loop started")
        while True:
            try:
                await self._scan()
            except Exception as exc:
                log.exception("Unexpected error in scan loop: %s", exc)
            await asyncio.sleep(3)

    async def _scan(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "lsblk", "-J", "-b", "-o", "NAME,FSTYPE,SIZE,MOUNTPOINT,HOTPLUG",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("lsblk failed (rc=%d): %s", proc.returncode, stderr.decode().strip())
            return

        raw = stdout.decode()
        log.debug("lsblk output: %s", raw.strip())
        data = json.loads(raw)
        blockdevices = data.get("blockdevices") or []
        log.debug("lsblk: %d top-level block device(s) found", len(blockdevices))

        visible: set[str] = set()
        changed = False

        for bd in blockdevices:
            c = await self._process_bd(bd, visible)
            changed = changed or c

        # Clear ejected state for devices that physically disconnected (no longer in lsblk)
        self._ejected -= self._ejected - visible

        async with self._lock:
            gone = {dn for dn, d in self.devices.items() if not d.manual} - visible

        for devnode in gone:
            await self._remove(devnode)
            changed = True

        if changed:
            self._broadcast()

    async def _process_bd(self, bd: dict, visible: set[str]) -> bool:
        changed = False
        for child in bd.get("children") or []:
            c = await self._process_bd(child, visible)
            changed = changed or c

        devname = bd.get("name", "?")
        hotplug = bd.get("hotplug")
        fstype_raw = (bd.get("fstype") or "").lower()
        size_gb = (bd.get("size") or 0) / 1024 ** 3
        mountpoint = bd.get("mountpoint") or ""
        log.debug("  examine /dev/%s: hotplug=%s fstype=%r size=%.1f GB mountpoint=%r",
                  devname, hotplug, fstype_raw or "(none)", size_gb, mountpoint)

        if hotplug not in (True, "1", 1):
            log.debug("    → skip: not hotplug")
            return changed

        if fstype_raw not in _IPOD_FSTYPES:
            log.debug("    → skip: fstype not in iPod-compatible set %s", _IPOD_FSTYPES)
            return changed

        devnode = f"/dev/{devname}"
        visible.add(devnode)

        async with self._lock:
            known = devnode in self.devices
        if known:
            return changed

        # Decide on mount path before logging — if we can't use this device, skip silently
        existing_mount = bd.get("mountpoint")
        if existing_mount:
            mount_str = existing_mount
        elif AUTOMOUNT and devnode not in self._ejected:
            mount_path = MOUNT_BASE / devname
            await asyncio.to_thread(mount_path.mkdir, parents=True, exist_ok=True)
            ok = await self._do_mount(devnode, str(mount_path))
            if not ok:
                return changed
            mount_str = str(mount_path)
        else:
            # Not mounted (automount off, or device was explicitly ejected) — register as
            # unmounted so it remains visible in the UI with a manual Mount button
            size_bytes = bd.get("size") or 0
            info = DeviceInfo(
                devnode=devnode,
                devname=devname,
                mount="",
                fstype=FS_LABELS.get(fstype_raw, fstype_raw.upper()),
                size_bytes=size_bytes,
                is_ipod=False,
                mounted=False,
            )
            reason = "ejected" if devnode in self._ejected else "automount disabled"
            log.info("USB device visible (not mounted, %s): %s  fstype=%s  size=%.1f GB",
                     reason, devnode, fstype_raw, size_bytes / 1024 ** 3)
            async with self._lock:
                self.devices[devnode] = info
            self._broadcast()
            return True

        log.info("New USB storage device: %s  fstype=%s  size=%.1f GB  mount=%s",
                 devnode, fstype_raw, (bd.get("size") or 0) / 1024 ** 3, mount_str)
        if existing_mount:
            log.info("  %s was already mounted at %s", devnode, existing_mount)
        else:
            log.info("  Mounted %s → %s", devnode, mount_str)

        mount = Path(mount_str)
        sentinel = mount / _IPOD_SENTINEL
        is_ipod = sentinel.exists()
        total, _ = await asyncio.to_thread(fs_usage, mount)

        if is_ipod:
            log.info("  iPod confirmed: sentinel found at %s", sentinel)
        else:
            log.info("  Not an iPod: sentinel missing at %s", sentinel)

        info = DeviceInfo(
            devnode=devnode,
            devname=devname,
            mount=mount_str,
            fstype=FS_LABELS.get(fstype_raw, fstype_raw.upper()),
            size_bytes=total,
            is_ipod=is_ipod,
        )

        async with self._lock:
            self.devices[devnode] = info
            if is_ipod and self.selected is None:
                self.selected = devnode
                log.info("  Auto-selected %s as active device", devnode)

        log.info("Device registered: %s (is_ipod=%s, %.1f GB %s)",
                 devnode, is_ipod, total / 1024 ** 3, info.fstype)
        return True

    async def _do_mount(self, devnode: str, mountpoint: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "mount", "-o", "sync", devnode, mountpoint,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("  mount %s → %s failed (rc=%d): %s",
                      devnode, mountpoint, proc.returncode, stderr.decode().strip())
            return False
        log.info("  mount %s → %s OK", devnode, mountpoint)
        return True

    async def _remove(self, devnode: str) -> None:
        async with self._lock:
            info = self.devices.pop(devnode, None)
            self._cache.pop(devnode, None)
            if self.selected == devnode:
                ipods = [d for d in self.devices.values() if d.is_ipod]
                self.selected = ipods[0].devnode if ipods else None

        if not info:
            return

        log.info("Device disconnected: %s (mount=%s)", devnode, info.mount)
        if not info.manual and info.mounted and info.mount:
            proc = await asyncio.create_subprocess_exec(
                "umount", info.mount,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.warning("umount %s failed (rc=%d): %s",
                            info.mount, proc.returncode, stderr.decode().strip())
            else:
                log.info("Unmounted %s", info.mount)

    # ── Public API ────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "devices": [asdict(d) for d in self.devices.values()],
            "selected": self.selected,
        }

    def get_device_info(self, devnode: str) -> DeviceInfo | None:
        return self.devices.get(devnode)

    def get_ipod_uuid(self, devnode: str) -> str | None:
        lib = self._cache.get(devnode)
        if lib:
            return lib.get("device", {}).get("uuid") or None
        return None

    async def select_device(self, devnode: str) -> None:
        async with self._lock:
            if devnode not in self.devices:
                raise KeyError(f"Unknown device: {devnode}")
            prev = self.selected
            self.selected = devnode

        if devnode not in self._cache:
            await self._load_library(devnode)

        if self.selected != prev:
            self._broadcast()

    async def get_library(self) -> dict | None:
        devnode = self.selected
        if not devnode:
            return None
        if devnode not in self._cache:
            await self._load_library(devnode)
        return self._cache.get(devnode)

    async def refresh_library(self) -> dict | None:
        devnode = self.selected
        if not devnode:
            return None
        self._cache.pop(devnode, None)
        await self._load_library(devnode)
        return self._cache.get(devnode)

    async def _load_library(self, devnode: str) -> None:
        info = self.devices.get(devnode)
        if not info:
            log.warning("_load_library: unknown devnode %s", devnode)
            return
        if not info.is_ipod:
            log.warning("_load_library: %s is not an iPod, skipping", devnode)
            return
        log.info("Loading library for %s from %s", devnode, info.mount)
        from app.services.gpod import fetch_library
        self._loading.add(devnode)
        try:
            lib = await fetch_library(info.mount)
            self._cache[devnode] = lib
            log.info("Library loaded for %s: %d tracks, %d artists",
                     devnode, lib.get("total_tracks", 0), len(lib.get("artists", [])))
        except Exception as exc:
            log.error("Failed to load library for %s: %s", devnode, exc)
        finally:
            self._loading.discard(devnode)

    async def mount_device(self, devnode: str) -> None:
        async with self._lock:
            info = self.devices.get(devnode)
        if not info:
            raise KeyError(f"Unknown device: {devnode}")
        if info.mounted:
            return
        mount_path = MOUNT_BASE / info.devname
        await asyncio.to_thread(mount_path.mkdir, parents=True, exist_ok=True)
        ok = await self._do_mount(devnode, str(mount_path))
        if not ok:
            raise RuntimeError(f"Failed to mount {devnode}")
        mount = mount_path
        sentinel = mount / _IPOD_SENTINEL
        is_ipod = sentinel.exists()
        total, _ = await asyncio.to_thread(fs_usage, mount)
        updated = DeviceInfo(
            devnode=devnode,
            devname=info.devname,
            mount=str(mount_path),
            fstype=info.fstype,
            size_bytes=total,
            is_ipod=is_ipod,
            mounted=True,
        )
        log.info("Mounted %s at %s (is_ipod=%s)", devnode, mount_path, is_ipod)
        async with self._lock:
            self.devices[devnode] = updated
            if is_ipod and self.selected is None:
                self.selected = devnode
        if is_ipod:
            await self._load_library(devnode)
        self._broadcast()

    async def eject(self, devnode: str) -> None:
        async with self._lock:
            info = self.devices.get(devnode)
        if not info:
            raise KeyError(f"Unknown device: {devnode}")
        if not info.mounted:
            raise RuntimeError("Device is not mounted")
        if self.is_busy(devnode):
            raise RuntimeError("Device has active operations — stop playback or wait for library load")
        log.info("Ejecting %s (manual=%s)", devnode, info.manual)
        if not info.manual:
            proc = await asyncio.create_subprocess_exec(
                "sync", stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            log.info("sync complete for %s", devnode)
            self._ejected.add(devnode)
            log.info("Marked %s as ejected; auto-mount suppressed until physical disconnect", devnode)
        await self._remove(devnode)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self) -> None:
        snapshot = self.snapshot()
        for q in list(self._subscribers):
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass


device_service = DeviceService()
