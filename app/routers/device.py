import asyncio
import json
import logging
import os
import re
import tarfile
import threading

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.services.devices import device_service
from app.services.operations import op_service, _OP_HISTORY_DIR

log = logging.getLogger(__name__)
router = APIRouter(tags=["device"])

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DeleteBody(BaseModel):
    devnode: str
    track_ids: list[int | str]


class SyncBody(BaseModel):
    devnode: str
    copy_paths: list[str] = []
    delete_ids: list[int | str] = []
    copy_track_count: int | None = None
    media_type: str = 'music'


class DownloadTrack(BaseModel):
    ipod_path: str
    artist: str = ''
    albumartist: str = ''
    album: str = ''
    year: int | None = None
    track_nr: int | None = None
    title: str = ''


class DownloadBody(BaseModel):
    devnode: str
    tracks: list[DownloadTrack]


class RateBody(BaseModel):
    devnode: str
    track_id: int
    rating: int  # 0-5 stars


class VerifyBody(BaseModel):
    devnode: str
    mode: str  # 'add' | 'delete'


class CheckFsBody(BaseModel):
    devnode: str


async def _get_mount(devnode: str) -> str:
    """For iPod-only operations (gpod-rm/gpod-cp). Mounts on demand. Raises for WALKMAN."""
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod:
        raise HTTPException(400, "Not an iPod")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    try:
        return await device_service.ensure_mounted(devnode)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(500, f"Mount failed: {exc}")


async def _get_device_mount(devnode: str):
    """For operations that support both iPod and WALKMAN. Mounts on demand. Returns (info, mount)."""
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod and not info.is_walkman:
        raise HTTPException(400, "Device does not support library operations")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    try:
        mount = await device_service.ensure_mounted(devnode)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(500, f"Mount failed: {exc}")
    # Re-read info after mount (type may have been detected during first mount)
    info = device_service.get_device_info(devnode) or info
    return info, mount


async def _get_mount_ro(devnode: str) -> str:
    """Mount for read-only operations (download). No busy check."""
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod and not info.is_walkman:
        raise HTTPException(400, "Device does not support downloads")
    try:
        return await device_service.ensure_mounted(devnode)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(500, f"Mount failed: {exc}")


def _safe(s: str) -> str:
    s = _UNSAFE.sub('_', (s or '').strip())
    return s.strip('. ') or '_'


def _arcname(t: DownloadTrack) -> str:
    artist = _safe(t.albumartist or t.artist or 'Unknown Artist')
    album  = _safe(t.album or 'Unknown Album')
    title  = _safe(t.title or 'Unknown')
    ext    = os.path.splitext(t.ipod_path)[1].lower()
    nr     = t.track_nr or 0
    year   = t.year or 0

    album_dir  = f"[{year}] - {album}" if year else album
    track_file = f"{nr:02d} - {title}{ext}" if nr else f"{title}{ext}"
    return f"{artist}/{album_dir}/{track_file}"


def _track_disk_path(mount: str, ipod_path: str) -> str:
    """Resolve a track's disk path from mount + ipod_path.
    iPod uses colon-separated paths (:iPod_Control:…); WALKMAN uses POSIX paths (MUSIC/…).
    """
    if ':' in ipod_path:
        return mount + ipod_path.replace(':', '/')
    return os.path.join(mount, ipod_path.lstrip('/'))


async def _tar_stream(tracks: list[DownloadTrack], mount: str):
    r_fd, w_fd = os.pipe()

    def write_tar():
        try:
            with os.fdopen(w_fd, 'wb') as wf:
                with tarfile.open(fileobj=wf, mode='w|') as tar:
                    for t in tracks:
                        disk_path = _track_disk_path(mount, t.ipod_path)
                        arcname  = _arcname(t)
                        try:
                            tar.add(disk_path, arcname=arcname)
                        except OSError as e:
                            log.warning("download: skipping %s: %s", disk_path, e)
        except Exception:
            log.exception("download: tar stream failed")
            try:
                os.close(w_fd)
            except OSError:
                pass

    thread = threading.Thread(target=write_tar, daemon=True)
    thread.start()

    with os.fdopen(r_fd, 'rb') as rf:
        while True:
            chunk = await asyncio.to_thread(rf.read, 65536)
            if not chunk:
                break
            yield chunk

    thread.join()


def _resolve_device_id(devnode: str) -> str:
    uuid = device_service.get_device_uuid(devnode)
    if uuid:
        return uuid
    return devnode.lstrip("/").replace("/", "_")


@router.post("/library/verify")
async def verify_library(body: VerifyBody):
    if body.mode not in ("add", "delete", "check"):
        raise HTTPException(422, "mode must be 'add', 'delete', or 'check'")
    mount = await _get_mount(body.devnode)  # iPod-only; mounts on demand; raises for WALKMAN
    device_id = _resolve_device_id(body.devnode)
    await op_service.run_verify(body.mode, mount, device_id, devnode=body.devnode)
    return {"ok": True}


_FSCK_SUPPORTED = {"hfsplus", "hfs", "vfat", "msdos"}


@router.post("/library/check-fs")
async def check_fs(body: CheckFsBody):
    info = device_service.get_device_info(body.devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod and not info.is_walkman:
        raise HTTPException(400, "Device does not support filesystem check")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    if device_service._mount_refs.get(body.devnode, 0) > 0:
        raise HTTPException(409, "Device is currently in use — wait for active operations to complete")

    # Probe raw fstype directly from the block device (works unmounted)
    fstype_raw = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "lsblk", "-no", "FSTYPE", body.devnode,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        fstype_raw = out.decode().strip().lower()
    except Exception:
        pass

    if not fstype_raw:
        raise HTTPException(422, "Could not detect filesystem type")
    if fstype_raw not in _FSCK_SUPPORTED:
        raise HTTPException(422, f"fsck not supported for filesystem: {fstype_raw}")

    device_id = _resolve_device_id(body.devnode)
    await op_service.run_check_fs(body.devnode, fstype_raw, device_id)
    return {"ok": True}


@router.post("/library/delete")
async def delete_tracks(body: DeleteBody):
    info, mount = await _get_device_mount(body.devnode)
    device_id = _resolve_device_id(body.devnode)
    if info.is_walkman:
        ids = [int(i) for i in body.track_ids]
        await op_service.run_walkman_delete(ids, mount, info.walkman_db_id, device_id, devnode=body.devnode)
    else:
        await op_service.run_delete(body.track_ids, mount, device_id=device_id, devnode=body.devnode)
    return {"ok": True}


@router.post("/library/sync")
async def sync_tracks(body: SyncBody):
    info, mount = await _get_device_mount(body.devnode)
    device_id = _resolve_device_id(body.devnode)
    if info.is_walkman:
        meta = device_service.get_walkman_meta(body.devnode)
        music_path = meta["cap"]["music_path"] if meta else "MUSIC"
        delete_ids = [int(i) for i in body.delete_ids]
        await op_service.run_walkman_sync(
            body.copy_paths, delete_ids, mount, music_path,
            info.walkman_db_id, device_id, body.copy_track_count,
            devnode=body.devnode,
        )
    else:
        await op_service.run_sync(
            body.copy_paths, body.delete_ids, mount,
            device_id=device_id, copy_track_count=body.copy_track_count,
            media_type=body.media_type, ipod_db_id=info.ipod_db_id,
            devnode=body.devnode,
        )
    return {"ok": True}


@router.post("/library/rate")
async def rate_track(body: RateBody):
    if not 0 <= body.rating <= 5:
        raise HTTPException(400, "Rating must be 0-5")
    info = device_service.get_device_info(body.devnode)
    if not info or not info.is_ipod:
        raise HTTPException(404, "iPod not found")

    # Update in-memory cache so the track row reflects the change immediately
    device_service.update_cached_track_rating(body.devnode, body.track_id, body.rating * 20)

    # Persist to ipod_track_ratings — gpod-tag is applied on the next sync via _gpod_rating_sync
    import aiosqlite, time as _time
    from app.services.db import DB_PATH
    from app.services.track_key import track_key as _tk
    lib = device_service._cache.get(body.devnode)
    if lib:
        for artist in lib.get('artists', []):
            for album in artist.get('albums', []):
                for track in album.get('tracks', []):
                    if track.get('id') == body.track_id:
                        artist_tag = track.get('artist') or artist['name']
                        key = _tk(artist_tag, album['name'], track.get('track_nr'), track.get('disc_nr'), track.get('title', ''))
                        async with aiosqlite.connect(DB_PATH) as db:
                            if body.rating > 0:
                                await db.execute(
                                    "INSERT INTO ipod_track_ratings(track_key, rating, updated_at) VALUES(?,?,?) "
                                    "ON CONFLICT(track_key) DO UPDATE SET rating=excluded.rating, updated_at=excluded.updated_at",
                                    (key, body.rating, int(_time.time())),
                                )
                            else:
                                await db.execute("DELETE FROM ipod_track_ratings WHERE track_key=?", (key,))
                            await db.commit()
                        break

    return {"ok": True}


@router.post("/library/download")
async def download_tracks(body: DownloadBody):
    if not body.tracks:
        raise HTTPException(400, "No tracks specified")
    mount = await _get_mount_ro(body.devnode)

    async def _stream_and_release():
        try:
            async for chunk in _tar_stream(body.tracks, mount):
                yield chunk
        finally:
            await device_service.release_mount(body.devnode)

    log.info("download: %d tracks from %s", len(body.tracks), mount)
    return StreamingResponse(
        _stream_and_release(),
        media_type="application/x-tar",
        headers={"Content-Disposition": 'attachment; filename="device_export.tar"'},
    )


@router.get("/operations")
async def get_operations():
    return JSONResponse(op_service.current())


@router.get("/operations/history")
async def get_op_history(devnode: str):
    device_id = _resolve_device_id(devnode)
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", device_id)
    dir_path = _OP_HISTORY_DIR / safe
    if not dir_path.exists():
        return JSONResponse([])
    files = sorted(dir_path.glob("*.json"), reverse=True)[:10]
    ops = []
    for f in files:
        try:
            ops.append(json.loads(f.read_text()))
        except Exception:
            pass
    return JSONResponse(ops)


class DeviceSettingsBody(BaseModel):
    force_aac: int | None = None  # None=global, 0=off, 1=on
    sync_rules: list[dict] = []


def _resolve_db_id(devnode: str) -> tuple[int | None, str]:
    """Return (db_id, device_type) for an active device, or (None, '') if not registered."""
    info = device_service.get_device_info(devnode)
    if not info:
        return None, ""
    if info.is_ipod:
        return info.ipod_db_id, "ipod"
    if info.is_walkman:
        return info.walkman_db_id, "walkman"
    return None, ""


@router.get("/devices/known")
async def list_known_devices():
    from app.services.ipod_db import get_known_ipods
    from app.services.walkman import get_known_walkmans
    connected_uuids: set[str] = set()
    connected_db_ids: set[int] = set()
    for info in device_service.devices.values():
        if info.is_ipod:
            # ipod_db_id is set after library load; uuid is in _cache after library load
            if info.ipod_db_id is not None:
                connected_db_ids.add(info.ipod_db_id)
            uuid = device_service.get_device_uuid(info.devnode)
            if uuid:
                connected_uuids.add(uuid)
        elif not info.mounted and info.usb_serial:
            # Unmounted device: is_ipod=False (can't confirm without mounting) but USB serial
            # matches iPod UUID — include it so the known entry isn't shown as disconnected.
            connected_uuids.add(info.usb_serial)
    ipods = await get_known_ipods(connected_uuids, connected_db_ids)
    walkmans = await get_known_walkmans()
    connected_wm_serials: set[str] = {
        info.usb_serial for info in device_service.devices.values()
        if info.usb_serial and not info.is_ipod
    }
    for wm in walkmans:
        wm["connected"] = (
            any(d.walkman_db_id == wm["id"] for d in device_service.devices.values())
            or wm["serial"] in connected_wm_serials
        )
    return {"ipods": ipods, "walkmans": walkmans}


@router.delete("/devices/known/{device_id}")
async def delete_known_device(device_id: int, device_type: str = "ipod"):
    if device_type == "ipod":
        from app.services.ipod_db import delete_ipod
        await delete_ipod(device_id)
    else:
        from app.services.walkman import delete_walkman_device
        await delete_walkman_device(device_id)
    return {"ok": True}


@router.get("/devices/device-settings")
async def get_device_settings_endpoint(devnode: str = Query(...)):
    db_id, device_type = _resolve_db_id(devnode)
    if db_id is None:
        return {"force_aac": None, "sync_rules": []}
    from app.services.ipod_db import get_device_settings
    return await get_device_settings(db_id, device_type)


@router.put("/devices/device-settings")
async def save_device_settings_endpoint(devnode: str = Query(...), body: DeviceSettingsBody = None):
    db_id, device_type = _resolve_db_id(devnode)
    if db_id is None:
        info = device_service.get_device_info(devnode)
        if info and info.is_ipod:
            uuid = device_service.get_device_uuid(devnode)
            if uuid:
                from app.services.ipod_db import upsert_ipod
                db_id = await upsert_ipod(uuid, None, None)
                device_type = "ipod"
    if db_id is None:
        raise HTTPException(404, "Device not yet registered — load its library first")
    from app.services.ipod_db import save_device_settings
    await save_device_settings(db_id, body.force_aac, body.sync_rules, device_type)
    return {"ok": True}


@router.get("/devices/known/{device_id}/settings")
async def get_known_device_settings(device_id: int, device_type: str = "ipod"):
    from app.services.ipod_db import get_device_settings
    return await get_device_settings(device_id, device_type)


@router.put("/devices/known/{device_id}/settings")
async def save_known_device_settings(device_id: int, body: DeviceSettingsBody, device_type: str = "ipod"):
    from app.services.ipod_db import save_device_settings
    await save_device_settings(device_id, body.force_aac, body.sync_rules, device_type)
    return {"ok": True}


@router.get("/devices/offline-library")
async def offline_library(device_id: int, device_type: str = "ipod"):
    if device_type == "ipod":
        from app.services.ipod_db import get_ipod_cached_library
        lib = await get_ipod_cached_library(device_id)
    elif device_type == "walkman":
        from app.services.walkman import fetch_library_offline
        lib = await fetch_library_offline(device_id)
    else:
        raise HTTPException(400, f"Unknown device type: {device_type}")
    if lib is None:
        raise HTTPException(404, "No cached library for this device")
    return lib


@router.post("/devices/{devnode}/auto-sync")
async def run_auto_sync(devnode: str):
    info = device_service.get_device_info(devnode)
    if not info or not info.is_ipod:
        raise HTTPException(404, "iPod not found or not connected")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    lib = device_service._cache.get(devnode)
    if not lib:
        raise HTTPException(400, "Library not loaded — select the device first")
    db_id = info.ipod_db_id
    if db_id is None:
        raise HTTPException(400, "Device not yet registered in DB")
    from app.services.ipod_db import compute_auto_sync_paths
    paths = await compute_auto_sync_paths(lib, db_id, "ipod")
    if not paths:
        return {"ok": True, "queued": 0, "message": "Nothing to sync"}
    device_id = _resolve_device_id(devnode)
    try:
        mount = await device_service.ensure_mounted(devnode)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(500, f"Mount failed: {exc}")
    await op_service.run_sync(
        paths, [], mount,
        device_id=device_id, copy_track_count=len(paths),
        ipod_db_id=db_id, devnode=devnode,
    )
    return {"ok": True, "queued": len(paths)}


@router.get("/operations/log")
async def get_op_log(from_line: int = Query(0, alias="from")):
    return JSONResponse(op_service.current_log(from_line))


@router.get("/operations/events")
async def operations_events(request: Request):
    async def stream():
        last_json = None
        while True:
            if await request.is_disconnected():
                break
            current_json = json.dumps(op_service.current_status())
            if current_json != last_json:
                last_json = current_json
                yield f"data: {current_json}\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
