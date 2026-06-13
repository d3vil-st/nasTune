import asyncio
import json
import logging
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from app.services.artwork import extract_artwork
from app.services.devices import device_service


@asynccontextmanager
async def lifespan(_app):
    await device_service.start()
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["tojson"] = lambda v: Markup(json.dumps(v, ensure_ascii=False))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


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
