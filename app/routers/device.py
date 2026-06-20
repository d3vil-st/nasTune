import asyncio
import json
import logging
import os
import re
import tarfile
import threading

from fastapi import APIRouter, HTTPException, Request
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


def _get_mount(devnode: str) -> str:
    """For iPod-only operations (gpod-rm/gpod-cp). Raises for WALKMAN."""
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod:
        raise HTTPException(400, "Not an iPod")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    return info.mount


def _get_device(devnode: str):
    """For operations that support both iPod and WALKMAN."""
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod and not info.is_walkman:
        raise HTTPException(400, "Device does not support library operations")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    return info


def _get_mount_ro(devnode: str) -> str:
    """Mount lookup without busy check — downloads are read-only."""
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod and not info.is_walkman:
        raise HTTPException(400, "Device does not support downloads")
    return info.mount


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


@router.post("/library/delete")
async def delete_tracks(body: DeleteBody):
    info = _get_device(body.devnode)
    device_id = _resolve_device_id(body.devnode)
    if info.is_walkman:
        ids = [int(i) for i in body.track_ids]
        await op_service.run_walkman_delete(ids, info.mount, info.walkman_db_id, device_id)
    else:
        await op_service.run_delete(body.track_ids, info.mount, device_id=device_id)
    return {"ok": True}


@router.post("/library/sync")
async def sync_tracks(body: SyncBody):
    info = _get_device(body.devnode)
    device_id = _resolve_device_id(body.devnode)
    if info.is_walkman:
        meta = device_service.get_walkman_meta(body.devnode)
        music_path = meta["cap"]["music_path"] if meta else "MUSIC"
        delete_ids = [int(i) for i in body.delete_ids]
        await op_service.run_walkman_sync(
            body.copy_paths, delete_ids, info.mount, music_path,
            info.walkman_db_id, device_id, body.copy_track_count,
        )
    else:
        await op_service.run_sync(
            body.copy_paths, body.delete_ids, info.mount,
            device_id=device_id, copy_track_count=body.copy_track_count,
        )
    return {"ok": True}


@router.post("/library/download")
async def download_tracks(body: DownloadBody):
    mount = _get_mount_ro(body.devnode)
    if not body.tracks:
        raise HTTPException(400, "No tracks specified")
    log.info("download: %d tracks from %s", len(body.tracks), mount)
    return StreamingResponse(
        _tar_stream(body.tracks, mount),
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


@router.get("/operations/events")
async def operations_events(request: Request):
    async def stream():
        last_json = None
        while True:
            if await request.is_disconnected():
                break
            current_json = json.dumps(op_service.current())
            if current_json != last_json:
                last_json = current_json
                yield f"data: {current_json}\n\n"
            await asyncio.sleep(0.25)
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
