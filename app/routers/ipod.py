from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services.devices import device_service
from app.services.operations import op_service

router = APIRouter(tags=["ipod"])


class DeleteBody(BaseModel):
    devnode: str
    track_ids: list[int | str]


class SyncBody(BaseModel):
    devnode: str
    copy_paths: list[str] = []
    delete_ids: list[int | str] = []
    copy_track_count: int | None = None


def _get_mount(devnode: str) -> str:
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_ipod:
        raise HTTPException(400, "Not an iPod")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")
    return info.mount


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


@router.get("/operations")
async def get_operations():
    return JSONResponse(op_service.current())
