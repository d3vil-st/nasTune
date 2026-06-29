import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.services.fs_utils import fs_usage, fs_type

log = logging.getLogger(__name__)

def _classify_mediatype(raw: int | None) -> str:
    """Classify iPod mediatype bitmask (libgpod: AUDIO=1, VIDEO=2, PODCAST=4, AUDIOBOOK=8)."""
    if not raw:
        return 'music'
    if raw & 8:
        return 'audiobook'
    if raw & 4:
        return 'podcast'
    return 'music'



async def fetch_library(mount: str) -> dict[str, Any]:
    env = {**os.environ, "IPOD_MOUNT_POINT": mount}
    log.info("exec: IPOD_MOUNT_POINT=%s gpod-ls", mount)
    process = await asyncio.create_subprocess_exec(
        "gpod-ls",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or "gpod-ls exited with code " + str(process.returncode))

    return _parse(json.loads(stdout.decode()), Path(mount))


def _parse(raw: dict, mount: Path) -> dict[str, Any]:
    ipod = raw["ipod_data"]
    device = ipod["device"]

    master = next(p for p in ipod["playlists"]["items"] if p["type"] == "master")

    library: dict[str, dict] = {}
    total_bytes = 0

    for t in master["tracks"]:
        mt = _classify_mediatype(t.get("mediatype"))
        if mt in ('podcast', 'audiobook') and not (t.get("albumartist") or t.get("artist")):
            artist = t.get("album") or "Unknown Artist"
        else:
            artist = t.get("albumartist") or t.get("artist") or "Unknown Artist"
        album_name = t.get("album") or "Unknown Album"
        year = t.get("year") or 0
        total_bytes += t.get("size") or 0

        if artist not in library:
            library[artist] = {}

        if album_name not in library[artist]:
            library[artist][album_name] = {"name": album_name, "albumartist": artist, "year": year, "tracks": []}

        library[artist][album_name]["tracks"].append({
            "id": t["id"],
            "artist": t.get("artist") or "",
            "title": t.get("title") or "Unknown",
            "disc_nr": t.get("cd_nr") or 0,
            "track_nr": t.get("track_nr") or 0,
            "duration_ms": t.get("tracklen") or 0,
            "filetype": t.get("filetype") or "",
            "bitrate": t.get("bitrate") or 0,
            "samplerate": t.get("samplerate") or 0,
            "size": t.get("size") or 0,
            "playcount": t.get("playcount") or 0,
            "rating": t.get("rating") or 0,
            "artwork": bool(t.get("artwork")),
            "ipod_path": t.get("ipod_path") or "",
            "genre": t.get("genre") or "",
            "composer": t.get("composer") or "",
            "year": t.get("year") or 0,
            "time_added": t.get("time_added") or 0,
            "time_played": t.get("time_played") or 0,
            "mediatype": _classify_mediatype(t.get("mediatype")),
            "missing": _is_missing(mount, t.get("ipod_path") or ""),
        })

    for artist_albums in library.values():
        for album in artist_albums.values():
            is_podcast = bool(album["tracks"]) and album["tracks"][0].get("mediatype") == "podcast"
            if is_podcast:
                # Newest episodes first; 0/missing track_nr falls to the end
                album["tracks"].sort(
                    key=lambda t: (-t["track_nr"] if t["track_nr"] else 99999)
                )
            else:
                album["tracks"].sort(key=lambda t: (t["disc_nr"], t["track_nr"]))

    artists_sorted = sorted(library.keys(), key=lambda a: _sort_key(a))
    result_artists = []
    for artist in artists_sorted:
        first_track = next((t for a in library[artist].values() for t in a["tracks"]), {})
        is_podcast = first_track.get("mediatype") == "podcast"
        if is_podcast:
            # Newest season first; year=0 (unknown) falls to the end
            albums = sorted(
                library[artist].values(),
                key=lambda a: (1 if a["year"] <= 0 else 0, -(a["year"] or 0), a["name"].lower()),
            )
        else:
            albums = sorted(
                library[artist].values(),
                key=lambda a: (a["year"] if a["year"] > 0 else 9999, a["name"].lower()),
            )
        track_count = sum(len(a["tracks"]) for a in albums)
        result_artists.append({"name": artist, "albums": albums, "track_count": track_count})

    fs_total_bytes, fs_used_bytes = fs_usage(mount)
    used_pct = round(min(fs_used_bytes / fs_total_bytes * 100, 100), 1) if fs_total_bytes else 0

    return {
        "device": device,
        "ipod_name": master.get("name") or device.get("model_name") or "iPod",
        "total_tracks": sum(a["track_count"] for a in result_artists),
        "total_albums": sum(len(a["albums"]) for a in result_artists),
        "total_bytes": total_bytes,
        "total_size_gb": round(total_bytes / 1024 ** 3, 2),
        "fs_total_gb": round(fs_total_bytes / 1024 ** 3, 2) if fs_total_bytes else 0,
        "fs_used_gb": round(fs_used_bytes / 1024 ** 3, 2) if fs_total_bytes else 0,
        "fs_type": fs_type(mount),
        "used_pct": used_pct,
        "artists": result_artists,
    }


def _is_missing(mount: Path, ipod_path: str) -> bool:
    if not mount.parts or not ipod_path:
        return False
    return not (mount / ipod_path.lstrip("/")).exists()


def _sort_key(name: str) -> str:
    lower = name.lower()
    for prefix in ("the ", "a ", "an "):
        if lower.startswith(prefix):
            return lower[len(prefix):]
    return lower
