import asyncio
import json
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.services.artwork import extract_artwork
from app.services.gpod import fetch_library

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["tojson"] = lambda v: Markup(json.dumps(v, ensure_ascii=False))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    data = None
    error = None
    try:
        data = await fetch_library()
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(request, "index.html", {
        "data": data,
        "error": error,
        "mount_point": os.environ.get("IPOD_MOUNT_POINT", "(not set)"),
    })


@app.get("/artwork")
async def get_artwork(path: str):
    try:
        result = await asyncio.to_thread(extract_artwork, path)
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
