"""Plex integration: plex.tv PIN auth, server API, track matching, rating/playcount import."""
import asyncio
import logging
import time
import uuid

import aiosqlite
import httpx

from app.services.db import DB_PATH
from app.services.track_key import track_key as _track_key

log = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
PRODUCT = "nasTune"

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=30.0), follow_redirects=True)
    return _client


# ── settings helpers ─────────────────────────────────────────────────────────

_CONFIG_KEYS = (
    "plex_server_id", "plex_server_name", "plex_server_url",
    "plex_server_token", "plex_owner_id", "plex_owner_username",
)


async def _get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def _set_settings(pairs: dict[str, str]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            list(pairs.items()),
        )
        await db.commit()


async def get_client_id() -> str:
    cid = await _get_setting("plex_client_id")
    if not cid:
        cid = str(uuid.uuid4())
        await _set_settings({"plex_client_id": cid})
    return cid


async def get_config() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'plex_%'"
        ) as cur:
            stored = {k: v for k, v in await cur.fetchall()}
    return stored


async def is_linked() -> bool:
    cfg = await get_config()
    return bool(cfg.get("plex_server_url") and cfg.get("plex_server_token"))


async def save_link(server_id: str, server_name: str, server_url: str,
                    server_token: str, owner_id: int, owner_username: str) -> None:
    await _set_settings({
        "plex_server_id": server_id,
        "plex_server_name": server_name,
        "plex_server_url": server_url,
        "plex_server_token": server_token,
        "plex_owner_id": str(owner_id),
        "plex_owner_username": owner_username,
    })


async def clear_link() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "DELETE FROM settings WHERE key=?", [(k,) for k in _CONFIG_KEYS]
        )
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('plex_auth_enabled', 'false')"
        )
        await db.execute("DELETE FROM plex_sessions")
        await db.execute("DELETE FROM plex_users")
        await db.execute("DELETE FROM plex_tracks")
        await db.commit()


# ── plex.tv API ──────────────────────────────────────────────────────────────

async def _headers(token: str | None = None) -> dict[str, str]:
    h = {
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": await get_client_id(),
        "Accept": "application/json",
    }
    if token:
        h["X-Plex-Token"] = token
    return h


async def create_pin() -> dict:
    r = await _http().post(f"{PLEX_TV}/api/v2/pins?strong=true", headers=await _headers())
    r.raise_for_status()
    data = r.json()
    return {"pin_id": data["id"], "code": data["code"]}


async def build_auth_url(code: str) -> str:
    cid = await get_client_id()
    from urllib.parse import quote
    return (
        "https://app.plex.tv/auth#?"
        f"clientID={quote(cid)}&code={quote(code)}"
        f"&context%5Bdevice%5D%5Bproduct%5D={quote(PRODUCT)}"
    )


async def check_pin(pin_id: int, code: str) -> str | None:
    """Return authToken once the PIN has been claimed, else None."""
    r = await _http().get(
        f"{PLEX_TV}/api/v2/pins/{pin_id}", params={"code": code}, headers=await _headers()
    )
    r.raise_for_status()
    return r.json().get("authToken") or None


async def get_user(token: str) -> dict:
    r = await _http().get(f"{PLEX_TV}/api/v2/user", headers=await _headers(token))
    r.raise_for_status()
    u = r.json()
    return {
        "id": u.get("id"),
        "username": u.get("username") or u.get("title") or "",
        "email": u.get("email") or "",
        "thumb": u.get("thumb") or "",
    }


async def get_resources(token: str) -> list[dict]:
    r = await _http().get(
        f"{PLEX_TV}/api/v2/resources",
        params={"includeHttps": "1", "includeRelay": "1"},
        headers=await _headers(token),
    )
    r.raise_for_status()
    return [d for d in r.json() if "server" in (d.get("provides") or "")]


async def check_membership(token: str) -> dict | None:
    """Return the user dict if this Plex account may access the linked server, else None."""
    cfg = await get_config()
    server_id = cfg.get("plex_server_id")
    if not server_id:
        return None
    user = await get_user(token)
    if str(user["id"]) == cfg.get("plex_owner_id"):
        return user
    try:
        resources = await get_resources(token)
    except httpx.HTTPError:
        return None
    if any(d.get("clientIdentifier") == server_id for d in resources):
        return user
    return None


async def pick_connection(resource: dict) -> str | None:
    """Probe a server resource's connections; prefer local direct > remote direct > relay."""
    conns = resource.get("connections") or []
    token = resource.get("accessToken") or ""

    def rank(c: dict) -> int:
        if c.get("relay"):
            return 2
        return 0 if c.get("local") else 1

    for conn in sorted(conns, key=rank):
        uri = conn.get("uri")
        if not uri:
            continue
        try:
            r = await _http().get(
                f"{uri}/identity", headers=await _headers(token), timeout=5.0
            )
            if r.status_code == 200:
                return uri
        except httpx.HTTPError:
            continue
    return None


# ── Plex server API ──────────────────────────────────────────────────────────

_rediscover_lock = asyncio.Lock()


async def rediscover_server_url() -> str | None:
    """Re-resolve the server connection via plex.tv when the stored URL dies.

    plex.direct URLs embed the server's WAN IP — a new IP (or a LAN/WAN route
    change) invalidates the URL saved at link time. Plex clients re-resolve
    through the resources API on every failure; do the same.
    """
    cfg = await get_config()
    token = cfg.get("plex_server_token")
    machine_id = cfg.get("plex_server_id")
    old_url = cfg.get("plex_server_url")
    if not token or not machine_id:
        return None
    async with _rediscover_lock:
        # Another task may have already fixed it while we waited
        current = await _get_setting("plex_server_url")
        if current and current != old_url:
            return current
        try:
            resources = await get_resources(token)
        except httpx.HTTPError as exc:
            log.warning("Plex rediscovery failed: plex.tv unreachable: %s", exc)
            return None
        resource = next((d for d in resources if d.get("clientIdentifier") == machine_id), None)
        if resource is None:
            log.warning("Plex rediscovery failed: server %s not visible to the stored token", machine_id)
            return None
        uri = await pick_connection(resource)
        if not uri:
            log.warning("Plex rediscovery failed: no reachable connection for %s", machine_id)
            return None
        new_token = resource.get("accessToken") or token
        await _set_settings({"plex_server_url": uri, "plex_server_token": new_token})
        log.info("Plex server connection re-discovered: %s → %s", old_url, uri)
        return uri


_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


async def _server_once(method: str, path: str, params: dict | None = None,
                       extra_headers: dict | None = None) -> httpx.Response:
    cfg = await get_config()
    url, token = cfg.get("plex_server_url"), cfg.get("plex_server_token")
    if not url or not token:
        raise RuntimeError("No Plex server linked")
    headers = await _headers(token)
    if extra_headers:
        headers.update(extra_headers)
    r = await _http().request(method, f"{url}{path}", params=params or {}, headers=headers)
    r.raise_for_status()
    return r


async def _server_call(method: str, path: str, params: dict | None = None,
                       extra_headers: dict | None = None) -> httpx.Response:
    """Server request with one retry after re-discovering the connection."""
    try:
        return await _server_once(method, path, params, extra_headers)
    except _CONNECT_ERRORS as exc:
        log.warning("Plex server unreachable at stored URL (%s) — re-discovering via plex.tv", exc)
        if await rediscover_server_url() is None:
            raise
        return await _server_once(method, path, params, extra_headers)


async def _server(path: str, params: dict | None = None,
                  extra_headers: dict | None = None) -> dict:
    return (await _server_call("GET", path, params, extra_headers)).json()


async def _server_req(method: str, path: str, params: dict | None = None) -> dict | None:
    """Write request (POST/PUT/DELETE) to the linked Plex server."""
    r = await _server_call(method, path, params)
    try:
        return r.json()
    except ValueError:
        return None


def _plex_track_meta(md: dict) -> dict:
    """Normalize a Plex track Metadata dict to nasTune fields."""
    artist = md.get("originalTitle") or md.get("grandparentTitle") or ""
    user_rating = md.get("userRating")
    part_file = None
    media = md.get("Media") or []
    if media:
        parts = media[0].get("Part") or []
        if parts:
            part_file = parts[0].get("file")
    return {
        "rating_key": str(md.get("ratingKey")),
        "title": md.get("title") or "",
        "artist": artist,
        "grandparent_artist": md.get("grandparentTitle") or "",
        "album": md.get("parentTitle") or "",
        "track_nr": md.get("index"),
        "disc_nr": md.get("parentIndex"),
        "duration_ms": md.get("duration"),
        "rating": min(5, round(user_rating / 2)) if user_rating else None,
        "playcount": md.get("viewCount") or 0,
        "file": part_file,
    }


def _keys_for(t: dict) -> list[str]:
    """Primary key uses track artist; fallback uses album artist when they differ."""
    keys = [_track_key(t["artist"], t["album"], t["track_nr"], t["disc_nr"], t["title"])]
    ga = t.get("grandparent_artist")
    if ga and ga != t["artist"]:
        keys.append(_track_key(ga, t["album"], t["track_nr"], t["disc_nr"], t["title"]))
    return keys


async def get_playlists() -> list[dict]:
    data = await _server("/playlists", {"playlistType": "audio"})
    out = []
    for md in data.get("MediaContainer", {}).get("Metadata", []) or []:
        out.append({
            "rating_key": str(md.get("ratingKey")),
            "title": md.get("title") or "",
            "smart": bool(md.get("smart")),
            "leaf_count": md.get("leafCount") or 0,
            "duration_ms": md.get("duration") or 0,
        })
    return out


async def get_playlist_items(rating_key: str) -> list[dict]:
    data = await _server(f"/playlists/{rating_key}/items")
    return [_plex_track_meta(md)
            for md in data.get("MediaContainer", {}).get("Metadata", []) or []]


PLAYLIST_CHUNK = 200


async def replace_playlist(title: str, rating_keys: list[str]) -> dict:
    """Create (or replace) a non-smart audio playlist on the Plex server.

    Deletes any existing non-smart playlist with the same title first.
    Raises ValueError if the title collides with a smart playlist.
    """
    cfg = await get_config()
    machine_id = cfg.get("plex_server_id")
    if not machine_id:
        raise RuntimeError("No Plex server linked")

    for pl in await get_playlists():
        if pl["title"] == title:
            if pl["smart"]:
                raise ValueError(f"'{title}' is a smart playlist in Plex — rename one of them")
            await _server_req("DELETE", f"/playlists/{pl['rating_key']}")

    def _uri(keys: list[str]) -> str:
        return (f"server://{machine_id}/com.plexapp.plugins.library"
                f"/library/metadata/{','.join(keys)}")

    data = await _server_req("POST", "/playlists", {
        "type": "audio", "smart": "0", "title": title,
        "uri": _uri(rating_keys[:PLAYLIST_CHUNK]),
    })
    new_key = str(data["MediaContainer"]["Metadata"][0]["ratingKey"])
    rest = rating_keys[PLAYLIST_CHUNK:]
    for i in range(0, len(rest), PLAYLIST_CHUNK):
        await _server_req("PUT", f"/playlists/{new_key}/items",
                          {"uri": _uri(rest[i:i + PLAYLIST_CHUNK])})
    log.info("Published playlist '%s' to Plex (%d tracks)", title, len(rating_keys))
    return {"rating_key": new_key}


async def rating_keys_for(track_keys: list[str]) -> dict[str, str]:
    """Map track keys -> Plex ratingKey via the plex_tracks cache (import fills it)."""
    keys = list(track_keys)
    out: dict[str, str] = {}
    if not keys:
        return out
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT track_key, rating_key FROM plex_tracks WHERE track_key IN ({placeholders})",
                chunk,
            ) as cur:
                for k, rk in await cur.fetchall():
                    out.setdefault(k, rk)
    return out


async def _music_section_keys() -> list[str]:
    data = await _server("/library/sections")
    return [str(d.get("key"))
            for d in data.get("MediaContainer", {}).get("Directory", []) or []
            if d.get("type") == "artist"]


PAGE_SIZE = 1000


async def iter_all_tracks():
    """Yield pages (lists) of normalized track dicts across all music sections."""
    for section in await _music_section_keys():
        start = 0
        while True:
            data = await _server(
                f"/library/sections/{section}/all",
                {"type": "10"},
                extra_headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(PAGE_SIZE),
                },
            )
            mc = data.get("MediaContainer", {})
            page = mc.get("Metadata", []) or []
            if not page:
                break
            yield [_plex_track_meta(md) for md in page], mc.get("totalSize") or 0
            start += len(page)
            if start >= (mc.get("totalSize") or 0):
                break


# ── track matching against NAS sources ───────────────────────────────────────

def _path_parts(p: str) -> list[str]:
    return [seg for seg in (p or "").replace("\\", "/").split("/") if seg]


async def load_source_index() -> tuple[dict, dict]:
    """(tag_map, path_index) over all scanned music sources.

    tag_map:    track_key -> info
    path_index: filename -> [(path components, info)] for suffix matching
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT st.path, st.source_id, st.artist, st.albumartist, st.album,
                      st.track_nr, st.disc_nr, st.title
               FROM source_tracks st JOIN sources s ON s.id = st.source_id
               WHERE s.type = 'music'"""
        ) as cur:
            rows = await cur.fetchall()
    tag_map: dict[str, dict] = {}
    path_index: dict[str, list] = {}
    for path, source_id, artist, albumartist, album, track_nr, disc_nr, title in rows:
        # Same artist precedence as get_source_library / rate_source_track
        artist_tag = artist or albumartist or ""
        key = _track_key(artist_tag, album or "", track_nr, disc_nr, title or "")
        info = {"path": path, "source_id": source_id, "key": key}
        tag_map.setdefault(key, info)
        if albumartist and albumartist != artist_tag:
            alt = _track_key(albumartist, album or "", track_nr, disc_nr, title or "")
            tag_map.setdefault(alt, info)
        parts = _path_parts(path)
        if parts:
            path_index.setdefault(parts[-1], []).append((parts, info))
    return tag_map, path_index


def _match_by_path(plex_file: str | None, path_index: dict) -> dict | None:
    """Suffix-match a Plex file path against source paths.

    Plex and nasTune see the same files under different mount prefixes, so we
    compare trailing path components. Filename alone is too collision-prone
    ("01 - Intro.mp3"): require the parent directory to agree as well.
    """
    parts = _path_parts(plex_file or "")
    if not parts:
        return None
    best, best_overlap = None, 0
    for cand_parts, info in path_index.get(parts[-1], []):
        n = 0
        for a, b in zip(reversed(parts), reversed(cand_parts)):
            if a != b:
                break
            n += 1
        if n > best_overlap:
            best, best_overlap = info, n
    return best if best_overlap >= 2 else None


async def match_to_sources(tracks: list[dict], index: tuple[dict, dict] | None = None) -> None:
    """Annotate Plex track dicts with matched/path/source_id/source_key in place.

    Path match wins over tags: Plex metadata agents often rewrite artist/album,
    while the underlying file is by definition the same one the scanner indexed.
    """
    tag_map, path_index = index if index is not None else await load_source_index()
    for t in tracks:
        hit = _match_by_path(t.get("file"), path_index)
        if hit is None:
            for key in _keys_for(t):
                hit = tag_map.get(key)
                if hit:
                    break
        t["matched"] = hit is not None
        t["path"] = hit["path"] if hit else None
        t["source_id"] = hit["source_id"] if hit else None
        t["source_key"] = hit["key"] if hit else None


async def cache_plex_tracks(tracks: list[dict]) -> None:
    """Upsert ratingKey -> track_key mapping used for rating push-back.

    Prefer the matched source track's key (set by match_to_sources) — that is
    the key the UI computes when rating a source track, even when Plex's
    metadata agent rewrote the tags.
    """
    now = int(time.time())
    rows = [(t["rating_key"], t.get("source_key") or _keys_for(t)[0],
             t["title"], t["artist"], t["album"], now)
            for t in tracks if t.get("rating_key")]
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO plex_tracks(rating_key, track_key, title, artist, album, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(rating_key) DO UPDATE SET
                 track_key=excluded.track_key, title=excluded.title,
                 artist=excluded.artist, album=excluded.album,
                 updated_at=excluded.updated_at""",
            rows,
        )
        await db.commit()


# ── ratings / playcounts import task ─────────────────────────────────────────

_import_status: dict = {"status": "idle", "processed": 0, "total": 0,
                        "ratings": 0, "playcounts": 0, "error": None}
_import_task: asyncio.Task | None = None


def import_status() -> dict:
    return dict(_import_status)


def start_import() -> bool:
    global _import_task
    if _import_task and not _import_task.done():
        return False
    _import_status.update(status="running", processed=0, total=0,
                          ratings=0, playcounts=0, error=None)
    _import_task = asyncio.create_task(_run_import())
    return True


async def _run_import() -> None:
    try:
        now = int(time.time())
        source_index = await load_source_index()
        async for page, total in iter_all_tracks():
            _import_status["total"] = max(_import_status["total"], total)
            await match_to_sources(page, index=source_index)
            await cache_plex_tracks(page)

            ratings, playcounts = [], []
            for t in page:
                keys = set(_keys_for(t))
                if t.get("source_key"):
                    keys.add(t["source_key"])
                for key in keys:
                    if t["rating"]:
                        ratings.append((key, t["rating"], now))
                    if t["playcount"] > 0:
                        playcounts.append((key, t["playcount"], now))
            async with aiosqlite.connect(DB_PATH) as db:
                if ratings:
                    await db.executemany(
                        """INSERT INTO ipod_track_ratings(track_key, rating, updated_at)
                           VALUES(?,?,?)
                           ON CONFLICT(track_key) DO UPDATE SET
                             rating = MAX(rating, excluded.rating),
                             updated_at = excluded.updated_at""",
                        ratings,
                    )
                if playcounts:
                    await db.executemany(
                        """INSERT INTO ipod_track_playcounts(track_key, playcount, updated_at)
                           VALUES(?,?,?)
                           ON CONFLICT(track_key) DO UPDATE SET
                             playcount = MAX(playcount, excluded.playcount),
                             updated_at = excluded.updated_at""",
                        playcounts,
                    )
                await db.commit()

            _import_status["processed"] += len(page)
            _import_status["ratings"] += sum(1 for t in page if t["rating"])
            _import_status["playcounts"] += sum(1 for t in page if t["playcount"] > 0)

        _import_status["status"] = "done"
        log.info("Plex import done: %d tracks, %d rated, %d played",
                 _import_status["processed"], _import_status["ratings"],
                 _import_status["playcounts"])
    except Exception as exc:
        log.exception("Plex import failed")
        _import_status.update(status="error", error=str(exc))


# ── rating push-back ─────────────────────────────────────────────────────────

async def push_rating(track_key: str, stars: int) -> None:
    """Push a 0-5 star rating to Plex for every ratingKey mapped to this track key.

    Fire-and-forget: never raises, no-op when unlinked or unmapped.
    """
    try:
        if not await is_linked():
            return
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT rating_key FROM plex_tracks WHERE track_key=?", (track_key,)
            ) as cur:
                rating_keys = [row[0] for row in await cur.fetchall()]
        if not rating_keys:
            return
        rating = stars * 2 if stars > 0 else -1
        for rk in rating_keys:
            await _server_req("PUT", "/:/rate", {
                "key": rk, "identifier": "com.plexapp.plugins.library",
                "rating": str(rating),
            })
        log.info("Pushed rating %d stars to Plex for %d item(s)", stars, len(rating_keys))
    except Exception:
        log.exception("Plex rating push-back failed (non-fatal)")
