import os

import aiosqlite
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services.db import DB_PATH

router = APIRouter(tags=["settings"])


class SettingsBody(BaseModel):
    force_aac: bool = False
    max_threads: int = 0


@router.get("/settings")
async def get_settings():
    cpu_count = os.cpu_count() or 1
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM settings") as cur:
            stored = {k: v for k, v in await cur.fetchall()}
    max_threads = int(stored.get("max_threads", "0") or "0")
    if max_threads <= 0:
        max_threads = cpu_count
    return JSONResponse({
        "force_aac": stored.get("force_aac", "false") == "true",
        "max_threads": max_threads,
        "cpu_count": cpu_count,
    })


@router.post("/settings")
async def save_settings(body: SettingsBody):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("force_aac", "true" if body.force_aac else "false"),
        )
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("max_threads", str(max(0, body.max_threads))),
        )
        await db.commit()
    return {"ok": True}
