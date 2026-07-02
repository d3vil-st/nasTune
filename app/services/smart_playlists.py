"""Smart playlists: rule storage + evaluation against an iPod library.

The iPod firmware never evaluates smart playlist rules — iTunes computes the
member list at sync time and stores a static playlist. nasTune does the same:
rules live in the smart_playlists table, evaluate() runs them against the
device library dict (gpod.py _parse output), and the result is written to the
device as a regular playlist via the normal playlist-sync machinery.
"""
import json
import logging
import random
import time

import aiosqlite

from app.services.db import DB_PATH
from app.services.track_key import track_key as _track_key

log = logging.getLogger(__name__)

# field -> kind. Values are matched against the flattened track dict built in
# _flatten(); text matching is case-insensitive substring/equality.
FIELDS = {
    "title":       "text",
    "artist":      "text",
    "album":       "text",
    "albumartist": "text",
    "genre":       "text",
    "composer":    "text",
    "filetype":    "text",
    "mediatype":   "enum",     # music | podcast | audiobook
    "rating":      "number",   # 0-5 stars
    "playcount":   "number",
    "year":        "number",
    "bitrate":     "number",   # kbps
    "duration":    "number",   # seconds
    "size":        "number",   # MB
    "track_nr":    "number",
    "time_added":  "date",
    "time_played": "date",
}

TEXT_OPS   = {"contains", "not_contains", "is", "is_not", "starts_with", "ends_with"}
NUMBER_OPS = {"is", "is_not", "gt", "lt", "in_range"}
DATE_OPS   = {"in_last", "not_in_last", "before", "after"}
ENUM_OPS   = {"is", "is_not"}

LIMIT_UNITS = {"items", "minutes", "hours", "MB", "GB"}
LIMIT_BY = {
    "random", "name", "artist", "album", "genre", "highest_rating",
    "most_played", "least_played", "most_recently_played", "most_recently_added",
}


def _flatten(lib: dict) -> list[dict]:
    """Flatten the nested library into per-track dicts with rule-ready values."""
    out = []
    for artist in lib.get("artists", []):
        for album in artist.get("albums", []):
            for t in album.get("tracks", []):
                out.append({
                    "id": t["id"],
                    "title": t.get("title") or "",
                    "artist": t.get("artist") or artist["name"],
                    "album": album["name"],
                    "albumartist": artist["name"],
                    "genre": t.get("genre") or "",
                    "composer": t.get("composer") or "",
                    "filetype": t.get("filetype") or "",
                    "mediatype": t.get("mediatype") or "music",
                    "rating": round((t.get("rating") or 0) / 20),
                    "playcount": t.get("playcount") or 0,
                    "year": t.get("year") or 0,
                    "bitrate": t.get("bitrate") or 0,
                    "duration": (t.get("duration_ms") or 0) / 1000,
                    "size": (t.get("size") or 0) / 1048576,
                    "track_nr": t.get("track_nr") or 0,
                    "time_added": t.get("time_added") or 0,
                    "time_played": t.get("time_played") or 0,
                    "duration_ms": t.get("duration_ms") or 0,
                    "size_bytes": t.get("size") or 0,
                    "disc_nr": t.get("disc_nr") or 0,
                })
    return out


def _match_rule(track: dict, rule: dict) -> bool:
    field = rule.get("field")
    kind = FIELDS.get(field)
    if kind is None:
        return True  # unknown field — ignore the rule rather than match nothing
    op = rule.get("op")
    val = rule.get("value")
    v = track.get(field)

    if kind in ("text", "enum"):
        a = str(v or "").lower()
        b = str(val or "").lower()
        if op == "contains":     return b in a
        if op == "not_contains": return b not in a
        if op == "is":           return a == b
        if op == "is_not":       return a != b
        if op == "starts_with":  return a.startswith(b)
        if op == "ends_with":    return a.endswith(b)
        return True

    if kind == "number":
        try:
            n = float(v or 0)
            x = float(val if val not in (None, "") else 0)
        except (TypeError, ValueError):
            return True
        if op == "is":     return n == x
        if op == "is_not": return n != x
        if op == "gt":     return n > x
        if op == "lt":     return n < x
        if op == "in_range":
            try:
                y = float(rule.get("value2") if rule.get("value2") not in (None, "") else x)
            except (TypeError, ValueError):
                y = x
            lo, hi = min(x, y), max(x, y)
            return lo <= n <= hi
        return True

    if kind == "date":
        ts = int(v or 0)
        if op in ("in_last", "not_in_last"):
            try:
                days = float(val if val not in (None, "") else 0)
            except (TypeError, ValueError):
                return True
            cutoff = time.time() - days * 86400
            hit = ts >= cutoff and ts > 0
            return hit if op == "in_last" else not hit
        # before / after take a YYYY-MM-DD string
        try:
            ref = time.mktime(time.strptime(str(val), "%Y-%m-%d"))
        except (TypeError, ValueError):
            return True
        if op == "before": return 0 < ts < ref
        if op == "after":  return ts > ref
        return True

    return True


_SORT_KEYS = {
    "name":                 (lambda t: t["title"].lower(), False),
    "artist":               (lambda t: t["artist"].lower(), False),
    "album":                (lambda t: t["album"].lower(), False),
    "genre":                (lambda t: t["genre"].lower(), False),
    "highest_rating":       (lambda t: t["rating"], True),
    "most_played":          (lambda t: t["playcount"], True),
    "least_played":         (lambda t: t["playcount"], False),
    "most_recently_played": (lambda t: t["time_played"], True),
    "most_recently_added":  (lambda t: t["time_added"], True),
}


def evaluate(lib: dict, spec: dict) -> list[dict]:
    """Return the flattened tracks matching a smart playlist spec, limited + ordered."""
    rules = [r for r in (spec.get("rules") or []) if r.get("field")]
    match_all = bool(spec.get("match_all", True))
    tracks = _flatten(lib)

    if rules:
        pred = all if match_all else any
        tracks = [t for t in tracks if pred(_match_rule(t, r) for r in rules)]

    limit_by = spec.get("limit_by") or "random"
    if limit_by == "random":
        rng = random.Random(spec.get("id") or 0)  # stable order per playlist between edits
        rng.shuffle(tracks)
    elif limit_by in _SORT_KEYS:
        key, rev = _SORT_KEYS[limit_by]
        tracks.sort(key=key, reverse=rev)

    limit_n = spec.get("limit_n")
    if limit_n:
        unit = spec.get("limit_unit") or "items"
        if unit == "items":
            tracks = tracks[: int(limit_n)]
        else:
            budget = float(limit_n) * {
                "minutes": 60_000, "hours": 3_600_000,
                "MB": 1048576, "GB": 1073741824,
            }[unit] if unit in ("minutes", "hours", "MB", "GB") else 0
            metric = "duration_ms" if unit in ("minutes", "hours") else "size_bytes"
            picked, used = [], 0
            for t in tracks:
                cost = t[metric] or 0
                if used + cost > budget and picked:
                    break
                picked.append(t)
                used += cost
            tracks = picked

    # Playlists read nicer sorted by artist/album/track when not explicitly ordered
    if limit_by == "random" and not spec.get("limit_n"):
        tracks.sort(key=lambda t: (t["artist"].lower(), t["album"].lower(), t["track_nr"]))
    return tracks


def to_playlist_payload(name: str, tracks: list[dict]) -> dict:
    """Build the {name, track_keys} payload consumed by _gpod_playlist_sync."""
    track_keys = []
    for t in tracks:
        keys = [_track_key(t["artist"], t["album"], t["track_nr"], t["disc_nr"], t["title"])]
        if t["albumartist"] != t["artist"]:
            keys.append(_track_key(t["albumartist"], t["album"], t["track_nr"], t["disc_nr"], t["title"]))
        track_keys.append(keys)
    return {"name": name, "track_keys": track_keys}


# ── CRUD ─────────────────────────────────────────────────────────────────────

def _row_to_spec(row) -> dict:
    return {
        "id": row[0], "name": row[1], "match_all": bool(row[2]),
        "limit_n": row[3], "limit_unit": row[4], "limit_by": row[5],
        "live": bool(row[6]), "rules": json.loads(row[7] or "[]"),
        "created_at": row[8], "updated_at": row[9],
    }

_COLS = "id, name, match_all, limit_n, limit_unit, limit_by, live, rules, created_at, updated_at"


async def list_smart_playlists() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT {_COLS} FROM smart_playlists ORDER BY name") as cur:
            return [_row_to_spec(r) for r in await cur.fetchall()]


async def get_smart_playlist(spl_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT {_COLS} FROM smart_playlists WHERE id=?", (spl_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_spec(row) if row else None


async def live_smart_playlists() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT {_COLS} FROM smart_playlists WHERE live=1 ORDER BY name") as cur:
            return [_row_to_spec(r) for r in await cur.fetchall()]


async def save_smart_playlist(spec: dict) -> dict:
    now = int(time.time())
    rules = json.dumps(spec.get("rules") or [])
    async with aiosqlite.connect(DB_PATH) as db:
        if spec.get("id"):
            await db.execute(
                """UPDATE smart_playlists SET name=?, match_all=?, limit_n=?, limit_unit=?,
                   limit_by=?, live=?, rules=?, updated_at=? WHERE id=?""",
                (spec["name"], int(bool(spec.get("match_all", True))), spec.get("limit_n"),
                 spec.get("limit_unit") or "items", spec.get("limit_by") or "random",
                 int(bool(spec.get("live"))), rules, now, spec["id"]),
            )
            spl_id = spec["id"]
        else:
            cur = await db.execute(
                """INSERT INTO smart_playlists(name, match_all, limit_n, limit_unit, limit_by,
                   live, rules, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (spec["name"], int(bool(spec.get("match_all", True))), spec.get("limit_n"),
                 spec.get("limit_unit") or "items", spec.get("limit_by") or "random",
                 int(bool(spec.get("live"))), rules, now, now),
            )
            spl_id = cur.lastrowid
        await db.commit()
    return await get_smart_playlist(spl_id)


async def delete_smart_playlist(spl_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM smart_playlists WHERE id=?", (spl_id,))
        await db.commit()
