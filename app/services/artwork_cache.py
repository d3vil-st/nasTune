import asyncio
import hashlib
import logging
import os
import subprocess
from pathlib import Path

import aiosqlite

from app.services.db import DB_PATH
from app.services.track_key import norm_str as _ns

log = logging.getLogger(__name__)

ARTWORK_DIR = DB_PATH.parent / "artwork"


def _out_path(artist_key: str, album_key: str) -> Path:
    h = hashlib.sha1(f"{artist_key}|||{album_key}".encode()).hexdigest()
    return ARTWORK_DIR / f"{h}.jpg"


def _device_file_path(mount: Path, ipod_path: str, is_walkman: bool) -> Path:
    """Resolve a track's filesystem path from mount + ipod_path.
    Mirrors _track_disk_path in device.py router: colon paths use replace+lstrip,
    slash paths use lstrip only. Both avoid pathlib's absolute-path override.
    """
    if is_walkman:
        return mount / ipod_path.lstrip("/")
    if ":" in ipod_path:
        # ':iPod_Control:Music:F02:track.mp3' → 'iPod_Control/Music/F02/track.mp3'
        return mount / ipod_path.replace(":", "/").lstrip("/")
    # '/iPod_Control/Music/F02/track.mp3' — lstrip to avoid pathlib dropping mount
    return mount / ipod_path.lstrip("/")


async def _get_max_threads() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key='max_threads'") as cur:
            row = await cur.fetchone()
    n = int(row[0]) if row and row[0] else 0
    return max(1, n) if n > 0 else (os.cpu_count() or 4)


def _ffmpeg_extract(src: Path, dst: Path) -> bool:
    """Extract and downsample embedded artwork with ffmpeg. Returns True on success."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    log.info("artwork: extract %s", src)
    r = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(src),
            "-map", "0:v:0",
            "-vf", "scale='min(500,iw)':'min(500,ih)':force_original_aspect_ratio=decrease",
            "-vframes", "1",
            str(dst),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    ok = r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    if not ok:
        dst.unlink(missing_ok=True)
        stderr = r.stderr.decode(errors="replace").strip()
        last = stderr.splitlines()[-1] if stderr else "(no output)"
        log.warning("artwork: ffmpeg rc=%d for %s — %s", r.returncode, src.name, last)
    else:
        size_kb = dst.stat().st_size / 1024
        log.info("artwork: saved %s (%.1f KB)", dst.name, size_kb)
    return ok


async def _try_candidates(candidates: list[Path | None], dst: Path) -> bool:
    """Try each candidate with ffmpeg; return True on first success."""
    valid = [p for p in candidates if p and p.exists()]
    if not valid:
        return False
    for p in valid:
        try:
            if await asyncio.to_thread(_ffmpeg_extract, p, dst):
                return True
        except Exception as exc:
            log.error("artwork: unexpected error extracting from %s: %s", p, exc)
    return False


async def _add_ref(artwork_id: int, owner_type: str, owner_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO artwork_refs (artwork_id, owner_type, owner_id) VALUES (?,?,?)",
            (artwork_id, owner_type, owner_id),
        )
        await db.commit()


async def ensure_album_artwork(
    artist_key: str,
    album_key: str,
    owner_type: str,
    owner_id: str,
    source_files: list[Path | None],
    device_files: list[Path | None],
) -> bool:
    """
    Ensure artwork is cached for (artist_key, album_key).
    Tries source_files first (NAS always accessible), then device_files as fallback.
    Adds a ref for (owner_type, owner_id).
    Returns True if artwork is available (cached now or previously).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, path FROM artwork_cache WHERE artist_key=? AND album_key=?",
            (artist_key, album_key),
        ) as cur:
            row = await cur.fetchone()

    if row:
        cache_id, cached_path = row
        if Path(cached_path).exists():
            await _add_ref(cache_id, owner_type, owner_id)
            return True
        # File gone — fall through to re-extract

    dst = _out_path(artist_key, album_key)
    if not await _try_candidates(source_files + device_files, dst):
        return False

    h = hashlib.sha1(dst.read_bytes()).hexdigest()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO artwork_cache (artist_key, album_key, path, content_hash)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(artist_key, album_key) DO UPDATE SET
                   path=excluded.path, content_hash=excluded.content_hash""",
            (artist_key, album_key, str(dst), h),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM artwork_cache WHERE artist_key=? AND album_key=?",
            (artist_key, album_key),
        ) as cur:
            cache_id = (await cur.fetchone())[0]
        await db.execute(
            "INSERT OR IGNORE INTO artwork_refs (artwork_id, owner_type, owner_id) VALUES (?,?,?)",
            (cache_id, owner_type, owner_id),
        )
        await db.commit()

    return True


async def prune_refs(owner_type: str, owner_id: str, current_keys: set[tuple[str, str]]) -> None:
    """
    Remove refs for (owner_type, owner_id) no longer in current_keys.
    Delete artwork_cache rows and files that have no remaining refs.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT ar.artwork_id, ac.artist_key, ac.album_key, ac.path
               FROM artwork_refs ar JOIN artwork_cache ac ON ac.id=ar.artwork_id
               WHERE ar.owner_type=? AND ar.owner_id=?""",
            (owner_type, owner_id),
        ) as cur:
            existing = await cur.fetchall()

        stale = [(aid, path) for aid, ak, lk, path in existing if (ak, lk) not in current_keys]
        for artwork_id, _ in stale:
            await db.execute(
                "DELETE FROM artwork_refs WHERE artwork_id=? AND owner_type=? AND owner_id=?",
                (artwork_id, owner_type, owner_id),
            )

        for artwork_id, path in stale:
            async with db.execute(
                "SELECT COUNT(*) FROM artwork_refs WHERE artwork_id=?", (artwork_id,)
            ) as cur:
                if (await cur.fetchone())[0] == 0:
                    await db.execute("DELETE FROM artwork_cache WHERE id=?", (artwork_id,))
                    try:
                        Path(path).unlink(missing_ok=True)
                        log.debug("artwork: deleted orphan %s", path)
                    except Exception as exc:
                        log.warning("artwork: failed to delete %s: %s", path, exc)

        await db.commit()

    if stale:
        log.info("artwork: pruned %d stale ref(s) for %s/%s", len(stale), owner_type, owner_id)


async def _source_file_for(albumartist: str, album: str) -> Path | None:
    """Find the first source track file matching (albumartist, album)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT path FROM source_tracks WHERE albumartist=? AND album=? "
            "ORDER BY track_nr, title LIMIT 1",
            (albumartist, album),
        ) as cur:
            row = await cur.fetchone()
    if row:
        p = Path(row[0])
        return p if p.exists() else None
    return None


async def cache_library_artwork(
    lib: dict,
    owner_type: str,
    owner_id: str,
    mount: Path | None = None,
    is_walkman: bool = False,
) -> None:
    """
    Background task: cache artwork for every album in lib.
    Pool size = max_threads setting.
    Source files are tried first; device file is the fallback.
    """
    max_threads = await _get_max_threads()
    sem = asyncio.Semaphore(max_threads)
    current_keys: set[tuple[str, str]] = set()
    jobs: list[tuple] = []

    for artist in lib.get("artists", []):
        albumartist = artist["name"]
        for album in artist["albums"]:
            ak = _ns(albumartist)
            lk = _ns(album["name"])
            current_keys.add((ak, lk))

            device_file: Path | None = None
            if mount:
                for t in album.get("tracks", []):
                    ip = t.get("ipod_path", "")
                    if ip:
                        device_file = _device_file_path(mount, ip, is_walkman)
                        break

            jobs.append((ak, lk, albumartist, album["name"], device_file))

    await prune_refs(owner_type, owner_id, current_keys)

    log.info("artwork: starting cache for %s/%s — %d albums, pool=%d",
             owner_type, owner_id, len(jobs), max_threads)

    cached = no_art = errors = 0

    async def _one(ak, lk, albumartist_raw, album_raw, device_file):
        nonlocal cached, no_art, errors
        async with sem:
            src = await _source_file_for(albumartist_raw, album_raw)
            src_files = [src] if src else []
            dev_files = [device_file] if device_file else []

            if not src_files and not dev_files:
                log.warning("artwork: no candidates for '%s' / '%s'", albumartist_raw, album_raw)
                no_art += 1
                return

            ok = await ensure_album_artwork(ak, lk, owner_type, owner_id, src_files, dev_files)
            if ok:
                cached += 1
            else:
                log.warning("artwork: no embedded artwork found for '%s' / '%s' (tried %d file(s))",
                            albumartist_raw, album_raw, len(src_files) + len(dev_files))
                no_art += 1

    results = await asyncio.gather(*[_one(*j) for j in jobs], return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, Exception))

    log.info("artwork: %s/%s done — %d cached, %d without art, %d errors",
             owner_type, owner_id, cached, no_art, errors)


async def cache_source_artwork(source_id: int) -> None:
    """Cache artwork for all albums in a source after scan completes."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(NULLIF(albumartist,''), artist, 'Unknown Artist'), album, path "
            "FROM source_tracks WHERE source_id=? ORDER BY track_nr, title",
            (source_id,),
        ) as cur:
            rows = await cur.fetchall()

    # Build album → first track path map (first track per album)
    seen: dict[tuple[str, str], tuple[str, str, Path]] = {}
    for albumartist, album, path in rows:
        ak = _ns(albumartist or "")
        lk = _ns(album or "")
        if (ak, lk) not in seen:
            seen[(ak, lk)] = (albumartist, album, Path(path))

    current_keys = set(seen.keys())
    owner_id = str(source_id)
    await prune_refs("source", owner_id, current_keys)

    max_threads = await _get_max_threads()
    sem = asyncio.Semaphore(max_threads)
    log.info("artwork: starting cache for source/%s — %d albums, pool=%d",
             owner_id, len(seen), max_threads)

    cached = no_art = 0

    async def _one(ak, lk, albumartist_raw, album_raw, file_path):
        nonlocal cached, no_art
        async with sem:
            ok = await ensure_album_artwork(ak, lk, "source", owner_id, [file_path], [])
            if ok:
                cached += 1
            else:
                log.warning("artwork: no embedded artwork in source '%s' / '%s'",
                            albumartist_raw, album_raw)
                no_art += 1

    results = await asyncio.gather(
        *[_one(ak, lk, aa, al, fp) for (ak, lk), (aa, al, fp) in seen.items()],
        return_exceptions=True,
    )
    errors = sum(1 for r in results if isinstance(r, Exception))
    log.info("artwork: source/%s done — %d cached, %d without art, %d errors",
             owner_id, cached, no_art, errors)


async def drop_all_artwork() -> int:
    """Delete every cached artwork file and clear the DB tables. Returns number of files removed."""
    removed = 0
    if ARTWORK_DIR.exists():
        for f in ARTWORK_DIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM artwork_refs")
        await db.execute("DELETE FROM artwork_cache")
        await db.commit()
    log.info("artwork cache dropped: %d files removed", removed)
    return removed


async def lookup_artwork(albumartist: str, album: str) -> Path | None:
    """Return the cached artwork Path, or None if not in cache."""
    ak = _ns(albumartist)
    lk = _ns(album)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT path FROM artwork_cache WHERE artist_key=? AND album_key=?",
            (ak, lk),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    p = Path(row[0])
    return p if p.exists() else None
