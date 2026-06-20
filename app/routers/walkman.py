import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.services.devices import device_service
import app.services.walkman as wm_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/walkman", tags=["walkman"])


def _get_walkman(devnode: str):
    info = device_service.get_device_info(devnode)
    if not info:
        raise HTTPException(404, "Device not found")
    if not info.is_walkman:
        raise HTTPException(400, "Not a WALKMAN device")
    return info


@router.post("/scan")
async def trigger_scan(devnode: str = Query(...)):
    info = _get_walkman(devnode)
    if not info.mounted or not info.mount:
        raise HTTPException(400, "Device is not mounted")
    meta = device_service.get_walkman_meta(devnode)
    if not meta:
        raise HTTPException(500, "WALKMAN metadata not available")
    cap = meta["cap"]
    wm_svc.start_scan(info.walkman_db_id, Path(info.mount), cap["music_path"])
    return {"ok": True, "db_id": info.walkman_db_id}


@router.get("/scan_status")
async def get_scan_status(devnode: str = Query(...)):
    info = _get_walkman(devnode)
    status = await wm_svc.scan_status(info.walkman_db_id)
    return JSONResponse(status)
