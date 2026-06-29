import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.services.fs_utils import FS_LABELS, fs_type, fs_usage
from app.services.ipod import is_ipod as detect_ipod, log_mount_contents as log_ipod_mount_contents
from app.services.walkman import get_serial as _get_usb_serial

log = logging.getLogger(__name__)

MOUNT_BASE = Path(os.environ.get("IPOD_MOUNT_BASE", "/mnt/ipods"))
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
    mounted: bool = False
    is_walkman: bool = False
    walkman_db_id: int | None = None
    walkman_storage_type: str | None = None  # 'INTERNAL' | 'CARD', set on mount
    ipod_db_id: int | None = None
    usb_serial: str | None = None   # USB iSerial read at connect time (= iPod UUID)


class DeviceService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.devices: dict[str, DeviceInfo] = {}  # devnode -> info
        self.selected: str | None = None
        self._cache: dict[str, dict] = {}          # devnode -> parsed library
        self._subscribers: set[asyncio.Queue] = set()
        self._active_streams: dict[str, int] = {}  # devnode -> open stream count
        self._loading: set[str] = set()            # devnodes currently running gpod-ls / walkman fetch
        self._ejected: set[str] = set()            # devnodes explicitly ejected; skip auto-detect until physical disconnect
        self._walkman_meta: dict[str, dict] = {}   # devnode -> {serial, cap} for walkman devices
        self._mount_refs: dict[str, int] = {}      # devnode -> active mount ref count
        self._mount_locks: dict[str, asyncio.Lock] = {}  # per-device lock for mount/unmount ops

    def _device_mount_lock(self, devnode: str) -> asyncio.Lock:
        if devnode not in self._mount_locks:
            self._mount_locks[devnode] = asyncio.Lock()
        return self._mount_locks[devnode]

    # ── On-demand mount ref-counting ─────────────────────────────────

    async def ensure_mounted(self, devnode: str) -> str:
        """Increment mount ref; mount and detect device type on first ref. Returns mount path."""
        async with self._device_mount_lock(devnode):
            info = self.devices.get(devnode)
            if not info:
                raise KeyError(f"Unknown device: {devnode}")

            refs = self._mount_refs.get(devnode, 0)
            if refs > 0:
                self._mount_refs[devnode] = refs + 1
                return info.mount

            # Device already mounted externally (pre-mounted or manual) — just track ref
            if info.mounted and info.mount:
                self._mount_refs[devnode] = 1
                return info.mount

            # Need to actually mount
            mount_path = MOUNT_BASE / info.devname
            await asyncio.to_thread(mount_path.mkdir, parents=True, exist_ok=True)
            ok = await self._do_mount(devnode, str(mount_path))
            if not ok:
                raise RuntimeError(f"Failed to mount {devnode}")

            mount = mount_path
            total, _ = await asyncio.to_thread(fs_usage, mount)

            # Detect device type if not already known
            is_ipod = info.is_ipod
            is_walkman = info.is_walkman
            walkman_db_id = info.walkman_db_id
            walkman_storage_type = info.walkman_storage_type
            usb_serial = info.usb_serial

            # Detect device type when meta isn't populated yet (no probe was done,
            # e.g. device was pre-mounted by the host before nasTune started).
            if not is_ipod and (not is_walkman or devnode not in self._walkman_meta):
                from app.services.walkman import parse_capability, get_serial, get_or_create_db_device
                cap = await asyncio.to_thread(parse_capability, mount)
                is_walkman = cap is not None

                if is_walkman:
                    serial = await get_serial(devnode)
                    if not serial:
                        serial = f"{info.devname}_{cap['storage_type']}"
                    walkman_db_id = await get_or_create_db_device(serial, cap)
                    walkman_storage_type = cap.get('storage_type')
                    self._walkman_meta[devnode] = {"serial": serial, "cap": cap}
                    usb_serial = usb_serial or serial  # expose serial on DeviceInfo for UI
                    log.info("  WALKMAN confirmed: %s %s (db_id=%d)", cap['model'], cap['storage_type'], walkman_db_id)
                else:
                    is_ipod = detect_ipod(mount)
                    if is_ipod:
                        log.info("  iPod confirmed: sentinel found at %s", mount)
                        usb_serial = usb_serial or await _get_usb_serial(devnode) or None
                    else:
                        log.info("  Not an iPod or WALKMAN at %s", mount)

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
                walkman_storage_type=walkman_storage_type,
                usb_serial=usb_serial,
                ipod_db_id=info.ipod_db_id,
            )
            async with self._lock:
                self.devices[devnode] = updated
                if (is_ipod or is_walkman) and self.selected is None:
                    self.selected = devnode
                    log.info("  Auto-selected %s as active device", devnode)
            self._broadcast()

            self._mount_refs[devnode] = 1
            return str(mount_path)

    async def release_mount(self, devnode: str) -> None:
        """Decrement mount ref. Unmount when ref reaches 0 (unless manual device)."""
        async with self._device_mount_lock(devnode):
            refs = self._mount_refs.get(devnode, 0)
            if refs <= 0:
                return  # Nothing to release

            if refs > 1:
                self._mount_refs[devnode] = refs - 1
                return

            # Last ref — unmount
            self._mount_refs.pop(devnode, None)
            info = self.devices.get(devnode)
            if not info or info.manual or not info.mounted or not info.mount:
                return

            log.info("Unmounting %s (last ref released)", info.mount)
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

            if devnode in self.devices:
                updated = DeviceInfo(
                    devnode=info.devnode, devname=info.devname,
                    mount=info.mount,   # keep path for display/reference
                    fstype=info.fstype, size_bytes=info.size_bytes,
                    is_ipod=info.is_ipod, manual=info.manual,
                    mounted=False, is_walkman=info.is_walkman,
                    walkman_db_id=info.walkman_db_id, ipod_db_id=info.ipod_db_id,
                    walkman_storage_type=info.walkman_storage_type,
                    usb_serial=info.usb_serial,
                )
                async with self._lock:
                    if devnode in self.devices:
                        self.devices[devnode] = updated
                self._broadcast()

    @asynccontextmanager
    async def mounted(self, devnode: str):
        """Async context manager that holds a mount ref for the duration of the block."""
        mount = await self.ensure_mounted(devnode)
        try:
            yield mount
        finally:
            await self.release_mount(devnode)

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
        log.info("Device service: starting auto-discovery poll loop (mount base: %s)", MOUNT_BASE)
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
            walkman_storage_type = cap.get('storage_type')
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
            mounted=True,
            is_walkman=is_walkman,
            walkman_db_id=walkman_db_id,
            walkman_storage_type=walkman_storage_type if is_walkman else None,
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

        # Clear ejected state for devices that physically disconnected
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

        existing_mount = bd.get("mountpoint")

        if not existing_mount or devnode in self._ejected:
            # Register as unmounted — will mount on demand when needed
            size_bytes = bd.get("size") or 0
            usb_serial_early = await _get_usb_serial(devnode) or None
            reason = "ejected" if devnode in self._ejected else "will mount on demand"
            log.info("USB device visible (unmounted, %s): %s  fstype=%s  size=%.1f GB  serial=%s",
                     reason, devnode, fstype_raw, size_bytes / 1024 ** 3, usb_serial_early or "(none)")
            # Probe-mount to identify WALKMAN type and storage (Internal/Card).
            # Only skip for ejected devices; probing is the only reliable way to
            # differentiate dual-LUN devices that share the same USB serial.
            is_walkman_pre = False
            walkman_db_id_pre: int | None = None
            walkman_storage_type_pre: str | None = None
            if devnode not in self._ejected:
                cap_probe = await self._probe_walkman_cap(devnode, fstype_raw)
                if cap_probe is not None:
                    from app.services.walkman import get_or_create_db_device
                    serial_probe = usb_serial_early
                    if not serial_probe:
                        serial_probe = f"{devname}_{cap_probe['storage_type']}"
                    walkman_db_id_pre = await get_or_create_db_device(serial_probe, cap_probe)
                    walkman_storage_type_pre = cap_probe.get('storage_type')
                    is_walkman_pre = True
                    usb_serial_early = usb_serial_early or serial_probe
                    self._walkman_meta[devnode] = {"serial": serial_probe, "cap": cap_probe}
                    log.info("  Probe: WALKMAN %s %s at %s (db_id=%d)",
                             cap_probe['model'], cap_probe['storage_type'], devnode, walkman_db_id_pre)
            info = DeviceInfo(
                devnode=devnode,
                devname=devname,
                mount="",
                fstype=FS_LABELS.get(fstype_raw, fstype_raw.upper()),
                size_bytes=size_bytes,
                is_ipod=False,
                is_walkman=is_walkman_pre,
                walkman_db_id=walkman_db_id_pre,
                walkman_storage_type=walkman_storage_type_pre,
                mounted=False,
                usb_serial=usb_serial_early,
            )
            async with self._lock:
                self.devices[devnode] = info
            self._broadcast()
            return True

        # Device is already mounted (pre-mounted by host/previous session) — detect type and register
        mount_str = existing_mount
        log.info("New USB storage device (pre-mounted): %s  fstype=%s  size=%.1f GB  mount=%s",
                 devnode, fstype_raw, (bd.get("size") or 0) / 1024 ** 3, mount_str)

        mount = Path(mount_str)
        total, _ = await asyncio.to_thread(fs_usage, mount)

        from app.services.walkman import parse_capability, get_serial, get_or_create_db_device
        cap = await asyncio.to_thread(parse_capability, mount)
        is_walkman = cap is not None
        walkman_db_id: int | None = None

        walkman_storage_type: str | None = None
        if is_walkman:
            serial = await get_serial(devnode)
            if not serial:
                serial = f"{devname}_{cap['storage_type']}"
            walkman_db_id = await get_or_create_db_device(serial, cap)
            walkman_storage_type = cap.get('storage_type')
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

        usb_serial: str | None = None
        if is_ipod:
            usb_serial = await _get_usb_serial(devnode) or None
            if usb_serial:
                log.info("  iPod USB serial (early UUID): %s", usb_serial)
        elif is_walkman:
            usb_serial = serial or None

        info = DeviceInfo(
            devnode=devnode,
            devname=devname,
            mount=mount_str,
            fstype=FS_LABELS.get(fstype_raw, fstype_raw.upper()),
            size_bytes=total,
            is_ipod=is_ipod,
            is_walkman=is_walkman,
            walkman_db_id=walkman_db_id,
            walkman_storage_type=walkman_storage_type,
            usb_serial=usb_serial,
            mounted=True,
        )

        async with self._lock:
            self.devices[devnode] = info
            if (is_ipod or is_walkman) and self.selected is None:
                self.selected = devnode
                log.info("  Auto-selected %s as active device", devnode)

        log.info("Device registered: %s (is_ipod=%s, %.1f GB %s, pre-mounted at %s)",
                 devnode, is_ipod, total / 1024 ** 3, info.fstype, mount_str)
        return True

    async def _probe_walkman_cap(self, devnode: str, fstype_raw: str) -> "dict | None":
        """Mount devnode read-only to a temp dir, parse WALKMAN capability XML, unmount."""
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="nastune_probe_")
        try:
            if fstype_raw in ("vfat", "msdos"):
                opts = "ro,utf8=1"
            elif fstype_raw == "ntfs":
                opts = "ro,nls=utf8"
            else:
                opts = "ro"
            proc = await asyncio.create_subprocess_exec(
                "mount", "-o", opts, devnode, tmpdir,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.debug("Probe mount %s failed: %s", devnode, stderr.decode().strip())
                return None
            from app.services.walkman import parse_capability
            return await asyncio.to_thread(parse_capability, Path(tmpdir))
        except Exception as e:
            log.debug("Probe %s error: %s", devnode, e)
            return None
        finally:
            proc = await asyncio.create_subprocess_exec(
                "umount", tmpdir, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            try:
                os.rmdir(tmpdir)
            except Exception:
                pass

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
            opts = "sync,utf8=1"
        elif fstype_raw == "ntfs":
            opts = "sync,nls=utf8"
        else:
            opts = "sync"

        log.info("Mounting %s → %s (opts=%s)", devnode, mountpoint, opts)
        proc = await asyncio.create_subprocess_exec(
            "mount", "-o", opts, devnode, mountpoint,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
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

    async def _remove(self, devnode: str, *, skip_umount: bool = False) -> None:
        async with self._lock:
            info = self.devices.pop(devnode, None)
            self._cache.pop(devnode, None)
            self._walkman_meta.pop(devnode, None)
            self._mount_refs.pop(devnode, None)
            if self.selected == devnode:
                known = [d for d in self.devices.values() if d.is_ipod or d.is_walkman]
                self.selected = known[0].devnode if known else None

        if not info:
            return

        log.info("Device disconnected: %s (mount=%s)", devnode, info.mount)
        if not skip_umount and not info.manual and info.mounted and info.mount:
            log.info("Unmounting %s", info.mount)
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
        return self._walkman_meta.get(devnode)

    def invalidate_walkman_cache(self, walkman_db_id: int) -> None:
        for devnode, info in self.devices.items():
            if info.walkman_db_id == walkman_db_id:
                self._cache.pop(devnode, None)
                break

    def update_cached_track_rating(self, devnode: str, track_id: int, rating_100: int) -> None:
        lib = self._cache.get(devnode)
        if not lib:
            return
        for artist in lib.get('artists', []):
            for album in artist.get('albums', []):
                for track in album.get('tracks', []):
                    if track.get('id') == track_id:
                        track['rating'] = rating_100
                        return

    def get_device_uuid(self, devnode: str) -> str | None:
        lib = self._cache.get(devnode)
        if lib:
            return lib.get("device", {}).get("uuid") or None
        info = self.devices.get(devnode)
        return info.usb_serial if info else None

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

        self._loading.add(devnode)
        owns_ref = False
        try:
            mount_str = await self.ensure_mounted(devnode)
            owns_ref = True
            info = self.devices[devnode]  # re-read: ensure_mounted may have updated type

            if not info.is_ipod and not info.is_walkman:
                log.warning("_load_library: %s is not iPod or WALKMAN after mount, skipping", devnode)
                return

            log.info("Loading library for %s from %s", devnode, mount_str)

            if info.is_walkman:
                from app.services.walkman import fetch_library as wm_fetch
                meta = self._walkman_meta.get(devnode, {})
                lib = await wm_fetch(
                    mount_str,
                    info.walkman_db_id,
                    meta.get("serial", ""),
                    meta.get("cap", {}),
                )
            else:
                from app.services.gpod import fetch_library
                from app.services.ratings import persist_ratings, persist_playcounts
                from app.services.ipod_db import upsert_ipod
                from app.services.track_key import track_key as _tk
                from app.services.db import DB_PATH as _DB_PATH
                import aiosqlite as _aiosqlite
                lib = await fetch_library(mount_str)
                asyncio.create_task(persist_ratings(lib))
                asyncio.create_task(persist_playcounts(lib))
                # Merge stored ratings into cache before serving
                _key_to_track: dict[str, dict] = {}
                for _a in lib.get("artists", []):
                    for _al in _a["albums"]:
                        for _t in _al["tracks"]:
                            _k = _tk(_t.get("artist") or _a["name"], _al["name"],
                                     _t.get("track_nr"), _t.get("disc_nr"), _t.get("title", ""))
                            _key_to_track[_k] = _t
                if _key_to_track:
                    _ph = ",".join("?" * len(_key_to_track))
                    async with _aiosqlite.connect(_DB_PATH) as _db:
                        async with _db.execute(
                            f"SELECT track_key, rating FROM ipod_track_ratings WHERE track_key IN ({_ph})",
                            list(_key_to_track),
                        ) as _cur:
                            for _row in await _cur.fetchall():
                                _t2 = _key_to_track.get(_row[0])
                                if _t2:
                                    _t2["rating"] = max(_t2.get("rating", 0), _row[1] * 20)
                device_meta = lib.get("device", {})
                uuid = device_meta.get("uuid") or ""
                if uuid:
                    lib_json = json.dumps(lib, default=str)
                    ipod_db_id = await upsert_ipod(
                        uuid,
                        device_meta.get("model_name") or device_meta.get("model"),
                        device_meta.get("capacity"),
                        lib_json,
                        lib.get("ipod_name"),
                    )
                    info.ipod_db_id = ipod_db_id
                    log.info("iPod registered in DB: uuid=%s db_id=%d", uuid, ipod_db_id)

            self._cache[devnode] = lib
            log.info("Library loaded for %s: %d tracks, %d artists",
                     devnode, lib.get("total_tracks", 0), len(lib.get("artists", [])))

            # Launch artwork task — it inherits the mount ref and releases when done
            from app.services.artwork_cache import cache_library_artwork
            if info.is_walkman:
                meta = self._walkman_meta.get(devnode, {})
                cap = meta.get("cap", {})
                owner_id = f"{meta.get('serial', devnode)}_{cap.get('storage_type', 'INTERNAL')}"
                asyncio.create_task(self._artwork_task(devnode, lib, "walkman", owner_id, mount_str, is_walkman=True))
            else:
                uuid2 = lib.get("device", {}).get("uuid") or devnode
                asyncio.create_task(self._artwork_task(devnode, lib, "ipod", uuid2, mount_str, is_walkman=False))
            owns_ref = False  # Ownership transferred to artwork task

        except Exception as exc:
            log.error("Failed to load library for %s: %s", devnode, exc)
        finally:
            self._loading.discard(devnode)
            if owns_ref:
                await self.release_mount(devnode)

    async def _artwork_task(self, devnode: str, lib: dict, owner_type: str, owner_id: str,
                            mount_str: str, *, is_walkman: bool) -> None:
        """Caches artwork for all albums, then releases the mount ref."""
        try:
            from app.services.artwork_cache import cache_library_artwork
            await cache_library_artwork(
                lib, owner_type, owner_id,
                Path(mount_str) if mount_str else None,
                is_walkman=is_walkman,
            )
        except Exception as exc:
            log.error("Artwork task failed for %s: %s", devnode, exc)
        finally:
            await self.release_mount(devnode)

    async def mount_device(self, devnode: str) -> None:
        """Trigger on-demand mount + library load for a device."""
        async with self._lock:
            info = self.devices.get(devnode)
        if not info:
            raise KeyError(f"Unknown device: {devnode}")
        # Library load handles mount internally via ensure_mounted
        await self._load_library(devnode)
        info = self.devices.get(devnode)
        if info and (info.is_ipod or info.is_walkman):
            async with self._lock:
                if self.selected is None:
                    self.selected = devnode
        self._broadcast()

    async def eject(self, devnode: str) -> None:
        async with self._lock:
            info = self.devices.get(devnode)
        if not info:
            raise KeyError(f"Unknown device: {devnode}")

        if self._mount_refs.get(devnode, 0) > 0 or self.is_busy(devnode):
            raise RuntimeError("Device has active operations — stop playback or wait for operations to complete")

        log.info("Ejecting %s (mounted=%s, manual=%s)", devnode, info.mounted, info.manual)

        if not info.manual and info.mounted and info.mount:
            log.info("Syncing and unmounting %s", info.mount)
            proc = await asyncio.create_subprocess_exec("sync", stderr=asyncio.subprocess.PIPE)
            await proc.communicate()

            log.info("Unmounting %s", info.mount)
            proc = await asyncio.create_subprocess_exec(
                "umount", info.mount,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode().strip()
                log.warning("umount %s failed (rc=%d): %s", info.mount, proc.returncode, err)
                raise RuntimeError(f"Cannot unmount {info.mount}: {err or 'device or resource busy'}")
            log.info("Unmounted %s", info.mount)

        self._ejected.add(devnode)
        log.info("Marked %s as ejected; auto-mount suppressed until physical disconnect", devnode)
        await self._remove(devnode, skip_umount=True)

    async def detach_for_fsck(self, devnode: str) -> dict:
        """Remove device from tracking and mark ejected so the poll loop cannot remount it.
        Returns saved device info needed for reattach_after_fsck."""
        async with self._lock:
            info = self.devices.pop(devnode, None)
            self._cache.pop(devnode, None)
            self._walkman_meta.pop(devnode, None)
            self._mount_refs.pop(devnode, None)
            if self.selected == devnode:
                known = [d for d in self.devices.values() if d.is_ipod or d.is_walkman]
                self.selected = known[0].devnode if known else None
        if not info:
            raise KeyError(f"Unknown device: {devnode}")
        self._ejected.add(devnode)
        self._broadcast()
        return {
            "devname": info.devname,
            "mount":   info.mount,
            "fstype":  info.fstype,
            "is_ipod": info.is_ipod,
            "is_walkman": info.is_walkman,
            "walkman_db_id": info.walkman_db_id,
            "size_bytes": info.size_bytes,
            "usb_serial": info.usb_serial,
        }

    async def reattach_after_fsck(self, devnode: str, saved: dict) -> bool:
        """Remount device after fsck and fully re-register it (mirrors the mount_device flow)."""
        self._ejected.discard(devnode)
        mountpoint = saved["mount"]
        ok = await self._do_mount(devnode, mountpoint)
        if not ok:
            log.error("reattach_after_fsck: remount %s → %s failed", devnode, mountpoint)
            return False

        mount = Path(mountpoint)
        total, _ = await asyncio.to_thread(fs_usage, mount)

        from app.services.walkman import parse_capability, get_serial, get_or_create_db_device
        cap = await asyncio.to_thread(parse_capability, mount)
        is_walkman = cap is not None
        walkman_db_id: int | None = None

        walkman_storage_type_fsck: str | None = None
        if is_walkman:
            serial = await get_serial(devnode)
            if not serial:
                serial = f"{saved['devname']}_{cap['storage_type']}"
            walkman_db_id = await get_or_create_db_device(serial, cap)
            walkman_storage_type_fsck = cap.get('storage_type')
            self._walkman_meta[devnode] = {"serial": serial, "cap": cap}
            is_ipod = False
        else:
            is_ipod = detect_ipod(mount)

        info = DeviceInfo(
            devnode=devnode,
            devname=saved["devname"],
            mount=mountpoint,
            fstype=saved["fstype"],
            size_bytes=total,
            is_ipod=is_ipod,
            mounted=True,
            is_walkman=is_walkman,
            walkman_db_id=walkman_db_id,
            walkman_storage_type=walkman_storage_type_fsck,
            usb_serial=saved.get("usb_serial"),
        )
        async with self._lock:
            self.devices[devnode] = info
            if (is_ipod or is_walkman) and self.selected is None:
                self.selected = devnode
        if is_ipod or is_walkman:
            await self._load_library(devnode)
        self._broadcast()
        log.info("reattach_after_fsck: %s re-registered (is_ipod=%s is_walkman=%s)", devnode, is_ipod, is_walkman)
        return True

    async def reattach_without_mount(self, devnode: str, saved: dict) -> None:
        """Re-register device as unmounted after fsck without remounting."""
        self._ejected.discard(devnode)
        info = DeviceInfo(
            devnode=devnode,
            devname=saved["devname"],
            mount=saved.get("mount", ""),
            fstype=saved["fstype"],
            size_bytes=saved.get("size_bytes", 0),
            is_ipod=saved.get("is_ipod", False),
            mounted=False,
            is_walkman=saved.get("is_walkman", False),
            walkman_db_id=saved.get("walkman_db_id"),
            usb_serial=saved.get("usb_serial"),
        )
        async with self._lock:
            self.devices[devnode] = info
            if (info.is_ipod or info.is_walkman) and self.selected is None:
                self.selected = devnode
        self._broadcast()
        log.info("reattach_without_mount: %s re-registered as unmounted", devnode)

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
