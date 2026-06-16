import asyncio
import logging
import time
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

log = logging.getLogger(__name__)

_AUDIO_MIMES = {
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/mp4",
    "flac": "audio/flac", "aiff": "audio/aiff", "aif": "audio/aiff",
    "wav": "audio/wav", "ogg": "audio/ogg",
}


async def _validated_source_path(path: str) -> Path:
    full = Path(path).resolve()
    if not full.is_file():
        raise HTTPException(404, "File not found")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT path FROM sources") as cur:
            roots = [row[0] for row in await cur.fetchall()]
    if not any(str(full).startswith(r) for r in roots):
        raise HTTPException(403, "Path is not in any registered source")
    return full


from app.services.db import DB_PATH
from app.services.transcode_cache import transcode_cache
from app.services.scanner import scan_source

router = APIRouter(prefix="/sources", tags=["sources"])

_scan_tasks: dict[int, asyncio.Task] = {}


class AddSourceBody(BaseModel):
    name: str
    path: str
    type: str = "folder"


@router.get("")
async def list_sources():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, type, name, path, added_at, last_scanned_at, scan_status, scan_error, track_count "
            "FROM sources ORDER BY added_at"
        ) as cur:
            rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.post("")
async def add_source(body: AddSourceBody):
    p = Path(body.path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"Path does not exist or is not a directory: {body.path}")

    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO sources (type, name, path, added_at, scan_status) VALUES (?,?,?,?,'pending') RETURNING id",
            (body.type, body.name, str(p.resolve()), now),
        )
        row = await cur.fetchone()
        source_id = row[0]
        await db.commit()

    _start_scan(source_id, str(p.resolve()))
    return {"id": source_id, "ok": True}


@router.delete("/{source_id}")
async def delete_source(source_id: int):
    task = _scan_tasks.pop(source_id, None)
    if task and not task.done():
        task.cancel()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sources WHERE id=?", (source_id,))
        await db.commit()
    return {"ok": True}


@router.post("/{source_id}/scan")
async def trigger_scan(source_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT path, scan_status FROM sources WHERE id=?", (source_id,)) as cur:
            row = await cur.fetchone()

    if not row:
        raise HTTPException(404, "Source not found")
    if row["scan_status"] == "scanning":
        raise HTTPException(409, "Scan already in progress")

    _start_scan(source_id, row["path"])
    return {"ok": True}


@router.get("/browse")
async def browse_fs(path: str = "/"):
    p = Path(path).resolve()
    if not p.is_dir():
        p = p.parent

    try:
        entries = sorted(
            [d for d in p.iterdir() if d.is_dir() and not d.name.startswith('.')],
            key=lambda x: x.name.lower(),
        )
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    return {
        "path": str(p),
        "parent": str(p.parent) if str(p) != str(p.parent) else None,
        "dirs": [{"name": d.name, "path": str(d)} for d in entries],
    }


@router.get("/{source_id}/library")
async def get_source_library(source_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT last_scanned_at FROM sources WHERE id=?", (source_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "Source not found")
            last_scanned_at = row[0] or 0

        async with db.execute(
            """SELECT id, path, artist, albumartist, album, title, disc_nr, track_nr,
                      duration_ms, bitrate, samplerate, year, size, codec, bits_per_sample
               FROM source_tracks WHERE source_id=?
               ORDER BY albumartist, artist, album, disc_nr, track_nr, title""",
            (source_id,),
        ) as cur:
            tracks = [dict(t) for t in await cur.fetchall()]

    result = _build_library(tracks)
    result["last_scanned_at"] = last_scanned_at
    return JSONResponse(result)


@router.get("/audio")
async def source_audio(path: str, background_tasks: BackgroundTasks = None):
    full = await _validated_source_path(path)
    ext = full.suffix.lower().lstrip(".")

    if ext in ("m4a", "aac"):
        from mutagen.mp4 import MP4
        try:
            codec = getattr(MP4(str(full)).info, "codec", "").lower()
        except Exception:
            codec = ""
        if codec == "alac":
            cached = await transcode_cache.get(str(full))
            transcode_cache.acquire(str(full))
            background_tasks.add_task(transcode_cache.release, str(full))
            return FileResponse(str(cached), media_type="audio/flac")

    mime = _AUDIO_MIMES.get(ext, "application/octet-stream")
    return FileResponse(str(full), media_type=mime)


@router.get("/artwork")
async def source_artwork(path: str):
    full = await _validated_source_path(path)

    try:
        from mutagen import File as MFile
        from app.services.artwork import _mp4, _id3, _vorbis

        audio = MFile(str(full), easy=False)
        if not audio or not audio.tags:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})

        result = _mp4(audio.tags) or _id3(audio.tags) or _vorbis(audio.tags)
        if not result:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})

        data, mime = result
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to read artwork: {exc}")


def _build_library(tracks: list[dict]) -> dict:
    library: dict[str, dict] = {}

    for t in tracks:
        artist_key = (t.get("albumartist") or t.get("artist") or "Unknown Artist").strip()
        album_key = (t.get("album") or "Unknown Album").strip()
        year = t.get("year") or 0

        if artist_key not in library:
            library[artist_key] = {}
        if album_key not in library[artist_key]:
            library[artist_key][album_key] = {"name": album_key, "year": year, "tracks": []}

        library[artist_key][album_key]["tracks"].append({
            "id": t["id"],
            "path": t["path"],
            "artist": t.get("artist") or "",
            "albumartist": t.get("albumartist") or "",
            "album": album_key,
            "title": t.get("title") or "Unknown",
            "disc_nr": t.get("disc_nr") or 0,
            "track_nr": t.get("track_nr") or 0,
            "duration_ms": t.get("duration_ms") or 0,
            "bitrate": t.get("bitrate") or 0,
            "samplerate": t.get("samplerate") or 0,
            "year": t.get("year") or 0,
            "size": t.get("size") or 0,
            "codec": t.get("codec") or "",
            "bits_per_sample": t.get("bits_per_sample") or 0,
        })

    result_artists = []
    for artist in _sorted_names(library):
        albums = sorted(
            library[artist].values(),
            key=lambda a: (a["year"] if a["year"] > 0 else 9999, a["name"].lower()),
        )
        for album in albums:
            album["tracks"].sort(key=lambda t: (t["disc_nr"] or 0, t["track_nr"] or 999, t["title"].lower()))
        track_count = sum(len(a["tracks"]) for a in albums)
        result_artists.append({"name": artist, "albums": albums, "track_count": track_count})

    return {"artists": result_artists, "total_tracks": len(tracks)}


def _sorted_names(d: dict) -> list[str]:
    def key(name: str) -> str:
        lower = name.lower()
        for prefix in ("the ", "a ", "an "):
            if lower.startswith(prefix):
                return lower[len(prefix):]
        return lower

    return sorted(d, key=key)


def _start_scan(source_id: int, path: str) -> None:
    existing = _scan_tasks.get(source_id)
    if existing and not existing.done():
        return
    _scan_tasks[source_id] = asyncio.create_task(scan_source(source_id, path))
