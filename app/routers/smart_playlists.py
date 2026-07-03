import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import smart_playlists as spl
from app.services.devices import device_service
from app.services.operations import op_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/smart-playlists", tags=["smart-playlists"])


class SmartPlaylistBody(BaseModel):
    id: int | None = None
    name: str
    match_all: bool = True
    limit_n: int | None = None
    limit_unit: str = "items"
    limit_by: str = "random"
    live: bool = False
    rules: list[dict] = []


class PreviewBody(BaseModel):
    devnode: str
    spec: SmartPlaylistBody


class ApplyBody(BaseModel):
    devnode: str


def _validated(spec: SmartPlaylistBody) -> dict:
    if not spec.name.strip():
        raise HTTPException(422, "Playlist name is required")
    if spec.limit_unit not in spl.LIMIT_UNITS:
        raise HTTPException(422, f"Invalid limit unit: {spec.limit_unit}")
    if spec.limit_by not in spl.LIMIT_BY:
        raise HTTPException(422, f"Invalid limit selector: {spec.limit_by}")
    for r in spec.rules:
        if r.get("field") and r["field"] not in spl.FIELDS:
            raise HTTPException(422, f"Unknown rule field: {r['field']}")
    d = spec.model_dump()
    d["name"] = spec.name.strip()
    return d


def _cached_lib(devnode: str) -> dict:
    info = device_service.get_device_info(devnode)
    if not info or not info.is_ipod:
        raise HTTPException(400, "Smart playlists require a connected iPod")
    lib = device_service._cache.get(devnode)
    if not lib:
        raise HTTPException(400, "Device library not loaded — select the device first")
    return lib


@router.get("")
async def list_playlists():
    return {"smart_playlists": await spl.list_smart_playlists()}


@router.post("")
async def save_playlist(body: SmartPlaylistBody):
    return await spl.save_smart_playlist(_validated(body))


@router.delete("/{spl_id}")
async def delete_playlist(spl_id: int, devnode: str | None = None):
    """Delete a definition; with ?devnode= also remove the playlist from the iPod."""
    spec = await spl.get_smart_playlist(spl_id)
    if spec is None:
        raise HTTPException(404, "Smart playlist not found")

    mount = None
    if devnode:
        info = device_service.get_device_info(devnode)
        if not info or not info.is_ipod:
            raise HTTPException(400, "Device removal requires a connected iPod")
        if op_service.is_busy():
            raise HTTPException(409, "Another operation is already running")
        try:
            mount = await device_service.ensure_mounted(devnode)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(500, f"Mount failed: {exc}")

    await spl.delete_smart_playlist(spl_id)

    if mount is not None:
        from app.routers.device import _resolve_device_id
        await op_service.run_playlist_delete(
            mount, spec["name"],
            device_id=_resolve_device_id(devnode),
            devnode=devnode,
        )
    return {"ok": True, "device_delete": mount is not None}


@router.get("/device-rules")
async def device_rules(devnode: str, name: str):
    """Read an iTunes-created smart playlist's rules off the iPod for editing."""
    info = device_service.get_device_info(devnode)
    if not info or not info.is_ipod:
        raise HTTPException(400, "Requires a connected iPod")
    try:
        mount = await device_service.ensure_mounted(devnode)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(500, f"Mount failed: {exc}")
    try:
        spec = await spl.read_device_spec(mount, name)
    finally:
        await device_service.release_mount(devnode)
    if spec is None:
        raise HTTPException(422, "Not a smart playlist, or its rules could not be read")
    return spec


@router.post("/preview")
async def preview(body: PreviewBody):
    lib = _cached_lib(body.devnode)
    tracks = spl.evaluate(lib, _validated(body.spec))
    return {
        "count": len(tracks),
        "duration_ms": sum(t["duration_ms"] for t in tracks),
        "size_bytes": sum(t["size_bytes"] for t in tracks),
    }


@router.post("/{spl_id}/apply")
async def apply_playlist(spl_id: int, body: ApplyBody):
    spec = await spl.get_smart_playlist(spl_id)
    if spec is None:
        raise HTTPException(404, "Smart playlist not found")
    info = device_service.get_device_info(body.devnode)
    if not info or not info.is_ipod:
        raise HTTPException(400, "Smart playlists require a connected iPod")
    if op_service.is_busy():
        raise HTTPException(409, "Another operation is already running")

    try:
        mount = await device_service.ensure_mounted(body.devnode)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(500, f"Mount failed: {exc}")

    from app.routers.device import _resolve_device_id
    await op_service.run_smart_playlist_apply(
        mount, spec,
        device_id=_resolve_device_id(body.devnode),
        devnode=body.devnode,
    )
    return {"ok": True}
