import logging
import time

import aiosqlite

from app.services.db import DB_PATH
from app.services.track_key import lib_track_key as _lib_track_key

log = logging.getLogger(__name__)


def ipod_rating_to_stars(rating_100: int) -> int:
    """Convert iPod 0–100 rating to 0–5 stars (0 = unrated)."""
    if not rating_100:
        return 0
    return round(rating_100 / 20)


async def persist_ratings(lib: dict) -> None:
    """Upsert track ratings from a freshly parsed gpod-ls library. Highest rating wins on conflict."""
    updates: list[tuple[str, int, int]] = []
    now = int(time.time())

    for artist in lib.get('artists', []):
        artist_name = artist['name']
        for album in artist.get('albums', []):
            album_name = album['name']
            for track in album.get('tracks', []):
                stars = ipod_rating_to_stars(track.get('rating') or 0)
                if stars <= 0:
                    continue
                key = _lib_track_key(track, artist_name, album_name)
                updates.append((key, stars, now))

    if not updates:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO ipod_track_ratings(track_key, rating, updated_at)
               VALUES(?, ?, ?)
               ON CONFLICT(track_key) DO UPDATE SET
                 rating     = MAX(rating, excluded.rating),
                 updated_at = excluded.updated_at""",
            updates,
        )
        await db.commit()
    log.info("persist_ratings: upserted %d rated track(s)", len(updates))


async def persist_playcounts(lib: dict) -> None:
    """Upsert track playcounts from a freshly parsed gpod-ls library. Highest count wins on conflict."""
    updates: list[tuple[str, int, int]] = []
    now = int(time.time())

    for artist in lib.get('artists', []):
        artist_name = artist['name']
        for album in artist.get('albums', []):
            album_name = album['name']
            for track in album.get('tracks', []):
                pc = track.get('playcount') or 0
                key = _lib_track_key(track, artist_name, album_name)
                updates.append((key, pc, now))

    if not updates:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO ipod_track_playcounts(track_key, playcount, updated_at)
               VALUES(?, ?, ?)
               ON CONFLICT(track_key) DO UPDATE SET
                 playcount  = MAX(playcount, excluded.playcount),
                 updated_at = excluded.updated_at""",
            updates,
        )
        await db.commit()
    log.info("persist_playcounts: upserted %d track(s)", len(updates))
