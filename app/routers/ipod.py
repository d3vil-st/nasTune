import asyncio
import logging
import os
import re
import tarfile
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.services.devices import device_service
from app.services.operations import op_service

log = logging.getLogger(__name__)
router = APIRouter(tags=["ipod"])

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
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod:
        raise HTTPException(400, "Not an iPod")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    return info.mount


def _get_mount_ro(devnode: str) -> str:
    """Mount lookup without busy check — downloads are read-only."""
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod:
        raise HTTPException(400, "Not an iPod")
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


async def _tar_stream(tracks: list[DownloadTrack], mount: str):
    r_fd, w_fd = os.pipe()

    def write_tar():
        try:
            with os.fdopen(w_fd, 'wb') as wf:
                with tarfile.open(fileobj=wf, mode='w|') as tar:
                    for t in tracks:
                        # iPod stores paths with colon separators: ":iPod_Control:Music:..."
                        rel      = t.ipod_path.replace(':', '/')
                        disk_path = mount + rel
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


@router.post("/library/delete")
async def delete_tracks(body: DeleteBody):
    mount = _get_mount(body.devnode)
    await op_service.run_delete(body.track_ids, mount)
    return {"ok": True}


@router.post("/library/sync")
async def sync_tracks(body: SyncBody):
    mount = _get_mount(body.devnode)
    await op_service.run_sync(body.copy_paths, body.delete_ids, mount, copy_track_count=body.copy_track_count)
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
        headers={"Content-Disposition": 'attachment; filename="ipod_export.tar"'},
    )


@router.get("/operations")
async def get_operations():
    return JSONResponse(op_service.current())
