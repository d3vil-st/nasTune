import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)


class _NoOperationsPoll(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("GET /operations " in msg and '" 200' in msg)

logging.getLogger("uvicorn.access").addFilter(_NoOperationsPoll())

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from app.services.artwork import extract_artwork
from app.services.db import init_db
from app.services.devices import device_service
from app.services.transcode_cache import transcode_cache
from app.routers.sources import router as sources_router
from app.routers.device import router as device_router
from app.routers.walkman import router as walkman_router

log = logging.getLogger(__name__)

BUILD_VERSION = os.getenv("BUILD_VERSION", "dev")


async def _cache_cleanup_loop():
    while True:
        await asyncio.sleep(300)  # run every 5 minutes
        try:
            transcode_cache.cleanup_expired(max_age_seconds=3600)
        except Exception:
            log.exception("Error in transcode cache cleanup")


@asynccontextmanager
async def lifespan(_app):
    await init_db()
    await device_service.start()
    asyncio.create_task(_cache_cleanup_loop())
    yield
    from app.services.operations import op_service
    if op_service.is_busy():
        log.warning("SIGTERM received — operation in progress, delaying shutdown…")
        while op_service.is_busy():
            await asyncio.sleep(1)
        log.info("Operation finished, shutting down.")


app = FastAPI(lifespan=lifespan)
app.include_router(sources_router)
app.include_router(device_router)
app.include_router(walkman_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["tojson"] = lambda v: Markup(json.dumps(v, ensure_ascii=False))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"build_version": BUILD_VERSION})


@app.get("/devices")
async def get_devices():
    return JSONResponse(device_service.snapshot())


class SelectBody(BaseModel):
    devnode: str


@app.post("/devices/select")
async def select_device(body: SelectBody):
    try:
        await device_service.select_device(body.devnode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@app.get("/library")
async def get_library():
    lib = await device_service.get_library()
    if lib is None:
        raise HTTPException(status_code=404, detail="No iPod selected or library not available")
    return JSONResponse(lib)


@app.post("/library/refresh")
async def refresh_library():
    lib = await device_service.refresh_library()
    if lib is None:
        raise HTTPException(status_code=404, detail="No iPod selected or library not available")
    return JSONResponse(lib)


@app.get("/devices/events")
async def device_events(request: Request):
    async def stream():
        q = device_service.subscribe()
        try:
            yield f"data: {json.dumps(device_service.snapshot())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            device_service.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/artwork")
async def get_artwork(path: str, devnode: str = ""):
    target = devnode or device_service.selected
    if not target:
        raise HTTPException(status_code=400, detail="No device selected")

    info = device_service.get_device_info(target)
    if not info:
        raise HTTPException(status_code=404, detail="Device not found")

    try:
        result = await asyncio.to_thread(extract_artwork, path, info.mount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if result is None:
        raise HTTPException(status_code=404, detail="No artwork found")

    data, mime = result
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=86400"},
    )


_AUDIO_MIMES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/mp4",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
    "wav": "audio/wav",
    "flac": "audio/flac",
}

def _is_alac(path: Path) -> bool:
    try:
        from mutagen.mp4 import MP4
        info = MP4(str(path)).info
        return getattr(info, "codec", "").lower() == "alac"
    except Exception:
        return False


@app.get("/audio")
async def get_audio(path: str, devnode: str = "", background_tasks: BackgroundTasks = None):
    target = devnode or device_service.selected
    if not target:
        raise HTTPException(status_code=400, detail="No device selected")

    info = device_service.get_device_info(target)
    if not info:
        raise HTTPException(status_code=404, detail="Device not found")

    mount = Path(info.mount).resolve()
    full = (mount / path.lstrip("/")).resolve()
    if not str(full).startswith(str(mount)):
        raise HTTPException(status_code=400, detail="Path outside mount point")
    if not full.exists():
        raise HTTPException(status_code=404, detail="File not found")

    ext = full.suffix.lower().lstrip(".")

    # ALAC in M4A is not supported by Firefox on Linux — transcode to FLAC
    if ext in ("m4a", "aac") and await asyncio.to_thread(_is_alac, full):
        try:
            cached = await transcode_cache.get(str(full))
            transcode_cache.acquire(str(full))
            background_tasks.add_task(transcode_cache.release, str(full))
            return FileResponse(str(cached), media_type="audio/flac")
        except Exception:
            log.exception("Transcode failed for %s, falling back to raw file", full)

    mime = _AUDIO_MIMES.get(ext, "application/octet-stream")
    return FileResponse(str(full), media_type=mime)


@app.post("/audio/cache/evict")
async def evict_audio_cache(path: str, devnode: str = ""):
    target = devnode or device_service.selected
    if not target:
        return {"evicted": False}
    info = device_service.get_device_info(target)
    if not info:
        return {"evicted": False}
    mount = Path(info.mount).resolve()
    full = (mount / path.lstrip("/")).resolve()
    if not str(full).startswith(str(mount)):
        return {"evicted": False}
    return {"evicted": transcode_cache.evict(str(full))}


@app.post("/devices/mount")
async def mount_device(body: SelectBody):
    try:
        await device_service.mount_device(body.devnode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@app.post("/devices/eject")
async def eject_device(body: SelectBody):
    try:
        await device_service.eject(body.devnode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}
