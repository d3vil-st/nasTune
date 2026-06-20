import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.services.fs_utils import FS_LABELS, fs_type, fs_usage
from app.services.ipod import is_ipod as detect_ipod, log_mount_contents as log_ipod_mount_contents

log = logging.getLogger(__name__)

MOUNT_BASE = Path(os.environ.get("IPOD_MOUNT_BASE", "/mnt/ipods"))
AUTOMOUNT = os.environ.get("IPOD_AUTOMOUNT", "0").strip().lower() in ("1", "true", "yes")
# Expanded to include NTFS (Sony WALKMAN internal storage) and common SD card formats
_IPOD_FSTYPES = {"vfat", "hfsplus", "hfs", "exfat", "msdos", "ntfs"}


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
    is_walkman: bool = False
    walkman_db_id: int | None = None


class DeviceService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.devices: dict[str, DeviceInfo] = {}  # devnode -> info
        self.selected: str | None = None
        self._cache: dict[str, dict] = {}          # devnode -> parsed library
        self._subscribers: set[asyncio.Queue] = set()
        self._active_streams: dict[str, int] = {}  # devnode -> open stream count
        self._loading: set[str] = set()            # devnodes currently running gpod-ls / walkman fetch
        self._ejected: set[str] = set()            # devnodes explicitly ejected by user; skip auto-mount until physical disconnect
        self._walkman_meta: dict[str, dict] = {}   # devnode -> {serial, cap} for walkman devices

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
        from app.services.walkman import parse_capability, get_serial, get_or_create_db_device
        mount = Path(mount_str)
        total, _ = await asyncio.to_thread(fs_usage, mount)
        fst = fs_type(mount)

        cap = await asyncio.to_thread(parse_capability, mount)
        is_walkman = cap is not None
        walkman_db_id: int | None = None

        if is_walkman:
            serial = await get_serial("manual")
            if not serial:
                serial = f"manual_{cap['storage_type']}"
            walkman_db_id = await get_or_create_db_device(serial, cap)
            self._walkman_meta["manual"] = {"serial": serial, "cap": cap}
            log.info("Debug device: WALKMAN detected (%s %s, db_id=%d)",
                     cap['model'], cap['storage_type'], walkman_db_id)
        else:
            if detect_ipod(mount):
                log.info("Debug device: iPod sentinel found at %s", mount)
            else:
                log.warning("Debug device: iPod sentinel NOT found at %s — treating as non-iPod", mount)
                log_ipod_mount_contents(mount)

        is_ipod = not is_walkman and detect_ipod(mount)
        log.info("Debug device registered: mount=%s fstype=%s size=%.1f GB is_ipod=%s is_walkman=%s",
                 mount_str, fst, total / 1024 ** 3, is_ipod, is_walkman)

        info = DeviceInfo(
            devnode="manual",
            devname="manual",
            mount=mount_str,
            fstype=fst,
            size_bytes=total,
            is_ipod=is_ipod,
            manual=True,
            is_walkman=is_walkman,
            walkman_db_id=walkman_db_id,
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
        total, _ = await asyncio.to_thread(fs_usage, mount)

        # Detect device type: WALKMAN takes priority (has default-capability.xml)
        from app.services.walkman import parse_capability, get_serial, get_or_create_db_device
        cap = await asyncio.to_thread(parse_capability, mount)
        is_walkman = cap is not None
        walkman_db_id: int | None = None

        if is_walkman:
            serial = await get_serial(devnode)
            if not serial:
                serial = f"{devname}_{cap['storage_type']}"
            walkman_db_id = await get_or_create_db_device(serial, cap)
            self._walkman_meta[devnode] = {"serial": serial, "cap": cap}
            log.info("  WALKMAN confirmed: %s %s (db_id=%d, serial=%s)",
                     cap['model'], cap['storage_type'], walkman_db_id, serial or "(none)")
            is_ipod = False
        else:
            is_ipod = detect_ipod(mount)
            if is_ipod:
                log.info("  iPod confirmed: sentinel found at %s", mount)
            else:
                log.info("  Not an iPod or WALKMAN: no sentinels found at %s", mount)

        info = DeviceInfo(
            devnode=devnode,
            devname=devname,
            mount=mount_str,
            fstype=FS_LABELS.get(fstype_raw, fstype_raw.upper()),
            size_bytes=total,
            is_ipod=is_ipod,
            is_walkman=is_walkman,
            walkman_db_id=walkman_db_id,
        )

        async with self._lock:
            self.devices[devnode] = info
            if (is_ipod or is_walkman) and self.selected is None:
                self.selected = devnode
                log.info("  Auto-selected %s as active device", devnode)

        log.info("Device registered: %s (is_ipod=%s, %.1f GB %s)",
                 devnode, is_ipod, total / 1024 ** 3, info.fstype)
        return True

    async def _do_mount(self, devnode: str, mountpoint: str) -> bool:
        # Probe fstype to add UTF-8 options — WALKMAN uses UTF-8 filenames
        fstype_raw = ""
        try:
            p = await asyncio.create_subprocess_exec(
                "lsblk", "-no", "FSTYPE", devnode,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await p.communicate()
            fstype_raw = out.decode().strip().lower()
        except Exception:
            pass

        if fstype_raw in ("vfat", "msdos"):
            # utf8=1 makes the kernel present filenames as UTF-8 regardless of codepage
            opts = "sync,utf8=1"
        elif fstype_raw == "ntfs":
            # ntfs-3g defaults to UTF-8; nls=utf8 for kernel ntfs3 driver
            opts = "sync,nls=utf8"
        else:
            opts = "sync"

        proc = await asyncio.create_subprocess_exec(
            "mount", "-o", opts, devnode, mountpoint,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            # Retry without charset hint if the driver rejected the option
            if fstype_raw and ("invalid option" in err.lower() or "unknown" in err.lower()):
                log.warning("  mount with opts=%r failed (%s), retrying with sync only", opts, err)
                proc = await asyncio.create_subprocess_exec(
                    "mount", "-o", "sync", devnode, mountpoint,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    log.error("  mount %s → %s failed (rc=%d): %s",
                              devnode, mountpoint, proc.returncode, stderr.decode().strip())
                    return False
            else:
                log.error("  mount %s → %s failed (rc=%d): %s",
                          devnode, mountpoint, proc.returncode, err)
                return False
        log.info("  mount %s → %s OK (opts=%s)", devnode, mountpoint, opts)
        return True

    async def _remove(self, devnode: str) -> None:
        async with self._lock:
            info = self.devices.pop(devnode, None)
            self._cache.pop(devnode, None)
            self._walkman_meta.pop(devnode, None)
            if self.selected == devnode:
                known = [d for d in self.devices.values() if d.is_ipod or d.is_walkman]
                self.selected = known[0].devnode if known else None

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

    def get_walkman_meta(self, devnode: str) -> dict | None:
        """Returns {serial, cap} for a WALKMAN device, or None."""
        return self._walkman_meta.get(devnode)

    def invalidate_walkman_cache(self, walkman_db_id: int) -> None:
        """Drop the cached library for the WALKMAN device with this DB id."""
        for devnode, info in self.devices.items():
            if info.walkman_db_id == walkman_db_id:
                self._cache.pop(devnode, None)
                break

    def get_device_uuid(self, devnode: str) -> str | None:
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
        if not info.is_ipod and not info.is_walkman:
            log.warning("_load_library: %s is neither iPod nor WALKMAN, skipping", devnode)
            return
        log.info("Loading library for %s from %s", devnode, info.mount)
        self._loading.add(devnode)
        try:
            if info.is_walkman:
                from app.services.walkman import fetch_library as wm_fetch
                meta = self._walkman_meta.get(devnode, {})
                lib = await wm_fetch(
                    info.mount,
                    info.walkman_db_id,
                    meta.get("serial", ""),
                    meta.get("cap", {}),
                )
            else:
                from app.services.gpod import fetch_library
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
        total, _ = await asyncio.to_thread(fs_usage, mount)

        from app.services.walkman import parse_capability, get_serial, get_or_create_db_device
        cap = await asyncio.to_thread(parse_capability, mount)
        is_walkman = cap is not None
        walkman_db_id: int | None = None

        if is_walkman:
            serial = await get_serial(devnode)
            if not serial:
                serial = f"{info.devname}_{cap['storage_type']}"
            walkman_db_id = await get_or_create_db_device(serial, cap)
            self._walkman_meta[devnode] = {"serial": serial, "cap": cap}
            is_ipod = False
        else:
            is_ipod = detect_ipod(mount)

        log.info("Mounted %s at %s (is_ipod=%s, is_walkman=%s)", devnode, mount_path, is_ipod, is_walkman)
        updated = DeviceInfo(
            devnode=devnode,
            devname=info.devname,
            mount=str(mount_path),
            fstype=info.fstype,
            size_bytes=total,
            is_ipod=is_ipod,
            mounted=True,
            is_walkman=is_walkman,
            walkman_db_id=walkman_db_id,
        )
        async with self._lock:
            self.devices[devnode] = updated
            if (is_ipod or is_walkman) and self.selected is None:
                self.selected = devnode
        if is_ipod or is_walkman:
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
