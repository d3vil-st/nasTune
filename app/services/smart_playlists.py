"""Smart playlists: rule storage, gpod-playlist rule generation, and preview.

Rules live in the smart_playlists table and are written to the iPod as REAL
smart playlists via `gpod-playlist -c <name> -S -e '<field> <op> <value>' …`
(gpod-utils ≥ 1.5.1) — rules are stored in the iTunesDB and visible in iTunes.
The iPod firmware never evaluates rules itself, so gpod-playlist populates the
member list at creation (itdb_spl_update) and `-U` refreshes all smart
playlists after every sync.

evaluate() is a Python approximation of the same semantics used only for the
live match preview in the rule editor modal.
"""
import asyncio
import json
import logging
import random
import re
import time

import aiosqlite

from app.services.db import DB_PATH

log = logging.getLogger(__name__)

# field -> kind. Mirrors the full gpod-playlist SPL vocabulary so rules read
# back from iTunes-created smart playlists survive a round-trip. Fields not in
# gpod-ls output (comment, grouping, skipcount, bpm, compilation, purchased)
# evaluate as empty/0/unset in the preview — the device result is what counts.
FIELDS = {
    "title":       "text",
    "artist":      "text",
    "album":       "text",
    "albumartist": "text",
    "genre":       "text",
    "composer":    "text",
    "comment":     "text",
    "grouping":    "text",
    "rating":      "number",   # 0-5 stars
    "playcount":   "number",
    "skipcount":   "number",
    "year":        "number",
    "bitrate":     "number",   # kbps
    "samplerate":  "number",   # Hz
    "bpm":         "number",
    "duration":    "number",   # seconds
    "size":        "number",   # MB
    "track_nr":    "number",
    "disc_nr":     "number",
    "compilation": "bool",     # value 'set' | 'unset'
    "purchased":   "bool",
    "time_added":  "date",
    "time_played": "date",
    "time_modified": "date",
    "time_skipped":  "date",
}

TEXT_OPS   = {"contains", "not_contains", "is", "is_not", "starts_with", "ends_with"}
NUMBER_OPS = {"is", "is_not", "gt", "lt", "gte", "lte", "in_range"}
DATE_OPS   = {"in_last", "not_in_last"}
DATE_UNITS = {"h": 3600, "d": 86400, "w": 604800, "m": 2629746, "y": 31536000}

LIMIT_UNITS = {"items", "minutes", "hours", "MB", "GB"}
LIMIT_BY = {
    "random", "name", "artist", "album", "genre",
    "highest_rating", "lowest_rating",
    "most_played", "least_played",
    "most_recently_played", "least_recently_played",
    "most_recently_added", "least_recently_added",
}

# ── gpod-playlist rule generation ────────────────────────────────────────────

_GPOD_FIELDS = {
    "title": "title", "artist": "artist", "album": "album",
    "albumartist": "albumartist", "genre": "genre", "composer": "composer",
    "comment": "comment", "grouping": "grouping",
    "rating": "rating", "playcount": "playcount", "skipcount": "skipcount",
    "year": "year", "bitrate": "bitrate", "samplerate": "samplerate",
    "bpm": "bpm", "duration": "time", "size": "size",
    "track_nr": "track", "disc_nr": "disc",
    "compilation": "compilation", "purchased": "purchased",
    "time_added": "added", "time_played": "played",
    "time_modified": "modified", "time_skipped": "skipped",
}
_GPOD_TEXT_OPS = {
    "is": "=", "is_not": "!=", "contains": "~", "not_contains": "!~",
    "starts_with": "^", "ends_with": "$",
}
_GPOD_INT_OPS = {"is": "=", "is_not": "!=", "gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
_GPOD_LIMIT_UNITS = {"items": "songs", "minutes": "minutes", "hours": "hours", "MB": "mb", "GB": "gb"}
_GPOD_LIMIT_SORTS = {
    "random": "random", "name": "name", "artist": "artist", "album": "album",
    "genre": "genre", "highest_rating": "highest-rated", "lowest_rating": "lowest-rated",
    "most_played": "most-played", "least_played": "least-played",
    "most_recently_played": "recently-played", "least_recently_played": "least-recently-played",
    "most_recently_added": "recent", "least_recently_added": "least-recent",
}


def _gpod_num(field: str, val) -> int:
    """Rule value in gpod's user-facing units (size rules are raw bytes)."""
    n = float(val if val not in (None, "") else 0)
    if field == "size":
        n *= 1048576
    return int(round(n))


def rule_expr(rule: dict) -> str | None:
    """Render one stored rule as a gpod-playlist -e expression, or None if it
    cannot be represented (unknown field, degenerate rule)."""
    field = rule.get("field")
    gf = _GPOD_FIELDS.get(field)
    kind = FIELDS.get(field)
    if gf is None or kind is None:
        return None
    op, val = rule.get("op"), rule.get("value")

    if kind == "text":
        gop = _GPOD_TEXT_OPS.get(op)
        if gop is None or val in (None, ""):
            return None
        return f"{gf} {gop} {val}"

    if kind == "number":
        try:
            n = _gpod_num(field, val)
        except (TypeError, ValueError):
            return None
        if op == "in_range":
            try:
                n2 = _gpod_num(field, rule.get("value2"))
            except (TypeError, ValueError):
                return None
            return f"{gf} = {min(n, n2)}..{max(n, n2)}"
        gop = _GPOD_INT_OPS.get(op)
        if gop is None:
            return None
        if gop == ">=" and n <= 0:
            return None  # gpod rejects '>= 0' (always true)
        return f"{gf} {gop} {n}"

    if kind == "bool":
        if op != "is" or val not in ("set", "unset"):
            return None
        return f"{gf} = {val}"

    if kind == "date":
        if op not in ("in_last", "not_in_last"):
            return None
        try:
            n = int(round(float(val if val not in (None, "") else 0)))
        except (TypeError, ValueError):
            return None
        if n <= 0:
            return None
        unit = rule.get("unit") if rule.get("unit") in DATE_UNITS else "d"
        return f"{gf} {'<' if op == 'in_last' else '>'} {n}{unit}"

    return None


def to_gpod_args(spec: dict) -> tuple[list[str], list[dict]]:
    """Build the gpod-playlist create args for a spec.

    Returns (args, dropped_rules) — args is everything after `-M <mount>`;
    dropped_rules are stored rules that cannot be represented on the device.
    """
    args = ["-c", spec["name"], "-S"]
    if not spec.get("match_all", True):
        args.append("-A")
    dropped = []
    for rule in spec.get("rules") or []:
        if not rule.get("field"):
            continue
        expr = rule_expr(rule)
        if expr is None:
            dropped.append(rule)
        else:
            args += ["-e", expr]
    if spec.get("limit_n"):
        unit = _GPOD_LIMIT_UNITS.get(spec.get("limit_unit") or "items", "songs")
        sort = _GPOD_LIMIT_SORTS.get(spec.get("limit_by") or "random", "random")
        args += ["-L", f"{int(spec['limit_n'])}:{unit}:{sort}"]
    return args, dropped


# ── reading rules back from the device (adopt iTunes-created playlists) ──────

_REV_FIELDS      = {v: k for k, v in _GPOD_FIELDS.items()}
_REV_TEXT_OPS    = {v: k for k, v in _GPOD_TEXT_OPS.items()}
_REV_INT_OPS     = {v: k for k, v in _GPOD_INT_OPS.items()}
_REV_LIMIT_UNITS = {v: k for k, v in _GPOD_LIMIT_UNITS.items()}
_REV_LIMIT_SORTS = {v: k for k, v in _GPOD_LIMIT_SORTS.items()}

_SMART_HEADER_RE = re.compile(
    r"smart \{ match=(all|any) liveupdate=\w+(?: limit=(\d+):([\w-]+):([\w-]+))? \}"
)
_RULE_LINE_RE = re.compile(r"^\s*rule: (.+)$")
_DATE_EXPR_RE = re.compile(r"^(\d+)([hdwmy])$")


def _from_gpod_num(field: str, n: float) -> float | int:
    if field == "size":
        mb = n / 1048576
        return round(mb, 1) if mb != int(mb) else int(mb)
    return int(n)


def parse_rule_expr(expr: str) -> dict | None:
    """Reverse of rule_expr: parse a gpod-playlist rule dump line into a stored rule."""
    parts = expr.split(None, 2)
    if len(parts) != 3:
        return None
    gf, gop, val = parts
    field = _REV_FIELDS.get(gf)
    kind = FIELDS.get(field)
    if kind is None:
        return None

    if kind == "text":
        op = _REV_TEXT_OPS.get(gop)
        return {"field": field, "op": op, "value": val} if op else None

    if kind == "number":
        try:
            if gop == "=" and ".." in val:
                a, b = val.split("..", 1)
                return {"field": field, "op": "in_range",
                        "value": _from_gpod_num(field, float(a)),
                        "value2": _from_gpod_num(field, float(b))}
            op = _REV_INT_OPS.get(gop)
            if op is None:
                return None
            return {"field": field, "op": op, "value": _from_gpod_num(field, float(val))}
        except ValueError:
            return None

    if kind == "bool":
        if gop == "=" and val in ("set", "unset"):
            return {"field": field, "op": "is", "value": val}
        return None

    if kind == "date":
        m = _DATE_EXPR_RE.match(val)
        if not m or gop not in ("<", ">"):
            return None
        return {"field": field, "op": "in_last" if gop == "<" else "not_in_last",
                "value": int(m.group(1)), "unit": m.group(2)}

    return None


def parse_gpod_dump(text: str) -> dict | None:
    """Parse `gpod-playlist -l -p <name>` output into a spec dict.

    Returns None when the output has no smart-playlist header (regular playlist).
    Unreadable rule lines are collected under 'unparsed'.
    """
    header = _SMART_HEADER_RE.search(text)
    if not header:
        return None
    spec: dict = {
        "id": None, "name": "", "match_all": header.group(1) == "all",
        "limit_n": None, "limit_unit": "items", "limit_by": "random",
        "live": False, "rules": [], "unparsed": [],
    }
    if header.group(2):
        spec["limit_n"] = int(header.group(2))
        spec["limit_unit"] = _REV_LIMIT_UNITS.get(header.group(3), "items")
        spec["limit_by"] = _REV_LIMIT_SORTS.get(header.group(4), "random")
    for line in text.splitlines():
        m = _RULE_LINE_RE.match(line)
        if not m:
            continue
        rule = parse_rule_expr(m.group(1))
        if rule:
            spec["rules"].append(rule)
        else:
            spec["unparsed"].append(m.group(1))
    return spec


async def read_device_spec(mount: str, name: str) -> dict | None:
    """Read a smart playlist's rules off the device. None = not smart / unreadable."""
    proc = await asyncio.create_subprocess_exec(
        "gpod-playlist", "-M", mount, "-l", "-p", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        log.warning("read_device_spec '%s' failed (rc=%d): %s", name, proc.returncode, text.strip()[:200])
        return None
    spec = parse_gpod_dump(text)
    if spec is not None:
        spec["name"] = name
    return spec


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
                    "samplerate": t.get("samplerate") or 0,
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
        if op == "gte":    return n >= x
        if op == "lte":    return n <= x
        if op == "in_range":
            try:
                y = float(rule.get("value2") if rule.get("value2") not in (None, "") else x)
            except (TypeError, ValueError):
                y = x
            lo, hi = min(x, y), max(x, y)
            return lo <= n <= hi
        return True

    if kind == "bool":
        if str(val) == "set":   return bool(v)
        if str(val) == "unset": return not v
        return True

    if kind == "date":
        ts = int(v or 0)
        if op in ("in_last", "not_in_last"):
            try:
                n = float(val if val not in (None, "") else 0)
            except (TypeError, ValueError):
                return True
            cutoff = time.time() - n * DATE_UNITS.get(rule.get("unit") or "d", 86400)
            hit = ts >= cutoff and ts > 0
            return hit if op == "in_last" else not hit
        return True

    return True


_SORT_KEYS = {
    "name":                  (lambda t: t["title"].lower(), False),
    "artist":                (lambda t: t["artist"].lower(), False),
    "album":                 (lambda t: t["album"].lower(), False),
    "genre":                 (lambda t: t["genre"].lower(), False),
    "highest_rating":        (lambda t: t["rating"], True),
    "lowest_rating":         (lambda t: t["rating"], False),
    "most_played":           (lambda t: t["playcount"], True),
    "least_played":          (lambda t: t["playcount"], False),
    "most_recently_played":  (lambda t: t["time_played"], True),
    "least_recently_played": (lambda t: t["time_played"], False),
    "most_recently_added":   (lambda t: t["time_added"], True),
    "least_recently_added":  (lambda t: t["time_added"], False),
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
