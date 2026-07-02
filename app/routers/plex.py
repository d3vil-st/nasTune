import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services import plex as plex_svc
from app.services import plex_sessions as sessions

log = logging.getLogger(__name__)

router = APIRouter(prefix="/plex", tags=["plex"])

# Account tokens acquired during the link flow, held server-side only until
# the admin picks a server. pin_id -> (token, expires_at)
_pending_links: dict[int, tuple[str, float]] = {}
_PENDING_TTL = 600


def _stash_link_token(pin_id: int, token: str) -> None:
    now = time.time()
    for pid in [p for p, (_, exp) in _pending_links.items() if exp < now]:
        _pending_links.pop(pid, None)
    _pending_links[pin_id] = (token, now + _PENDING_TTL)


def _pop_link_token(pin_id: int) -> str | None:
    entry = _pending_links.get(pin_id)
    if not entry or entry[1] < time.time():
        _pending_links.pop(pin_id, None)
        return None
    return entry[0]


async def _status_payload() -> dict:
    cfg = await plex_svc.get_config()
    return {
        "linked": bool(cfg.get("plex_server_url") and cfg.get("plex_server_token")),
        "server_name": cfg.get("plex_server_name"),
        "server_url": cfg.get("plex_server_url"),
        "owner_username": cfg.get("plex_owner_username"),
        "auth_enabled": sessions.auth_enabled(),
    }


@router.get("/status")
async def plex_status():
    return await _status_payload()


@router.post("/link/pin")
async def link_pin():
    try:
        pin = await plex_svc.create_pin()
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"plex.tv unreachable: {exc}"}, status_code=502)
    pin["auth_url"] = await plex_svc.build_auth_url(pin["code"])
    return pin


class PinCheckBody(BaseModel):
    pin_id: int
    code: str


@router.post("/link/pin/check")
async def link_pin_check(body: PinCheckBody):
    try:
        token = await plex_svc.check_pin(body.pin_id, body.code)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"plex.tv unreachable: {exc}"}, status_code=502)
    if not token:
        return {"pending": True}

    _stash_link_token(body.pin_id, token)
    try:
        resources = await plex_svc.get_resources(token)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"plex.tv unreachable: {exc}"}, status_code=502)

    servers = [{
        "machine_id": d.get("clientIdentifier"),
        "name": d.get("name"),
        "owned": bool(d.get("owned")),
        "product_version": d.get("productVersion"),
        "connections": [
            {"uri": c.get("uri"), "local": bool(c.get("local")), "relay": bool(c.get("relay"))}
            for c in (d.get("connections") or [])
        ],
    } for d in resources]
    return {"ok": True, "servers": servers}


class LinkBody(BaseModel):
    pin_id: int
    machine_id: str
    uri: str | None = None


@router.post("/link")
async def link_server(body: LinkBody):
    token = _pop_link_token(body.pin_id)
    if not token:
        raise HTTPException(400, "Link flow expired — start over")

    try:
        resources = await plex_svc.get_resources(token)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"plex.tv unreachable: {exc}"}, status_code=502)

    resource = next(
        (d for d in resources if d.get("clientIdentifier") == body.machine_id), None
    )
    if resource is None:
        raise HTTPException(404, "Server not found in this Plex account")

    server_token = resource.get("accessToken") or token
    uri = body.uri or await plex_svc.pick_connection(resource)
    if not uri:
        raise HTTPException(502, "Could not reach the Plex server on any connection")

    owner = await plex_svc.get_user(token)
    await plex_svc.save_link(
        server_id=body.machine_id,
        server_name=resource.get("name") or "Plex",
        server_url=uri,
        server_token=server_token,
        owner_id=owner["id"],
        owner_username=owner["username"],
    )
    # The linking account is the admin, always enabled.
    await sessions.upsert_user(owner, role="admin", enabled=True)
    _pending_links.pop(body.pin_id, None)
    log.info("Linked Plex server '%s' at %s (owner %s)",
             resource.get("name"), uri, owner["username"])
    return await _status_payload()


@router.post("/unlink")
async def unlink_server():
    await plex_svc.clear_link()
    await sessions.set_auth_enabled(False)
    return await _status_payload()


class AuthToggleBody(BaseModel):
    enabled: bool


@router.post("/auth")
async def toggle_auth(body: AuthToggleBody):
    if body.enabled and not await plex_svc.is_linked():
        raise HTTPException(400, "Link a Plex server before enabling sign-in")
    await sessions.set_auth_enabled(body.enabled)
    return {"ok": True, "auth_enabled": sessions.auth_enabled()}


# ── users (admin approval list) ──────────────────────────────────────────────

@router.get("/users")
async def plex_users():
    return {"users": await sessions.list_users()}


class UserToggleBody(BaseModel):
    plex_user_id: int
    enabled: bool


@router.post("/users")
async def set_user(body: UserToggleBody):
    changed = await sessions.set_user_enabled(body.plex_user_id, body.enabled)
    if not changed:
        raise HTTPException(404, "User not found (or is the admin)")
    return {"ok": True}


# ── playlists ────────────────────────────────────────────────────────────────

@router.get("/playlists")
async def playlists():
    if not await plex_svc.is_linked():
        raise HTTPException(400, "No Plex server linked")
    try:
        return {"playlists": await plex_svc.get_playlists()}
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"Plex server unreachable: {exc}"}, status_code=502)


@router.get("/playlists/{rating_key}")
async def playlist_detail(rating_key: str):
    if not await plex_svc.is_linked():
        raise HTTPException(400, "No Plex server linked")
    try:
        tracks = await plex_svc.get_playlist_items(rating_key)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"Plex server unreachable: {exc}"}, status_code=502)

    await plex_svc.match_to_sources(tracks)
    # Cache ratingKey -> track_key so ratings set in nasTune can push back.
    asyncio.create_task(plex_svc.cache_plex_tracks(tracks))
    return {
        "total": len(tracks),
        "matched_count": sum(1 for t in tracks if t["matched"]),
        "tracks": tracks,
    }


class PublishPlaylistBody(BaseModel):
    devnode: str
    name: str
    track_ids: list[int] = []  # device track ids, in playlist order


@router.post("/publish-playlist")
async def publish_playlist(body: PublishPlaylistBody):
    """Publish an iPod playlist to Plex, replacing any same-name non-smart playlist."""
    if not await plex_svc.is_linked():
        raise HTTPException(400, "No Plex server linked")
    if not body.name.strip() or not body.track_ids:
        raise HTTPException(422, "Playlist name and tracks required")

    from app.services.devices import device_service
    from app.services.track_key import track_key as _tk
    lib = device_service._cache.get(body.devnode)
    if not lib:
        raise HTTPException(400, "Device library not loaded — select the device first")

    # device track id -> candidate track keys (track artist + album artist variants)
    keys_by_id: dict[int, list[str]] = {}
    for artist in lib.get("artists", []):
        for album in artist.get("albums", []):
            for t in album.get("tracks", []):
                artist_tag = t.get("artist") or artist["name"]
                nr, dnr, title = t.get("track_nr"), t.get("disc_nr"), t.get("title", "")
                keys = [_tk(artist_tag, album["name"], nr, dnr, title)]
                if artist_tag != artist["name"]:
                    keys.append(_tk(artist["name"], album["name"], nr, dnr, title))
                keys_by_id[t["id"]] = keys

    all_keys = [k for tid in body.track_ids for k in keys_by_id.get(tid, [])]
    key_to_rk = await plex_svc.rating_keys_for(all_keys)

    rating_keys: list[str] = []
    skipped = 0
    for tid in body.track_ids:
        rk = next((key_to_rk[k] for k in keys_by_id.get(tid, []) if k in key_to_rk), None)
        if rk is None:
            skipped += 1
        else:
            rating_keys.append(rk)

    if not rating_keys:
        raise HTTPException(422, "No tracks matched in Plex — run the Plex import first (Settings → Plex)")

    try:
        await plex_svc.replace_playlist(body.name.strip(), rating_keys)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"Plex server unreachable: {exc}"}, status_code=502)

    return {"ok": True, "total": len(body.track_ids),
            "matched": len(rating_keys), "skipped": skipped}


# ── ratings / playcounts import ──────────────────────────────────────────────

@router.post("/import")
async def start_import():
    if not await plex_svc.is_linked():
        raise HTTPException(400, "No Plex server linked")
    if not plex_svc.start_import():
        raise HTTPException(409, "Import already running")
    return {"ok": True}


@router.get("/import/status")
async def import_status():
    return plex_svc.import_status()
