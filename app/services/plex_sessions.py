"""Session store, user approval list, and the optional Plex auth gate middleware."""
import logging
import os
import secrets
import time

import aiosqlite

from app.services.db import DB_PATH

log = logging.getLogger(__name__)

COOKIE_NAME = "nastune_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days

# In-memory caches: auth flag is read on every request, sessions on most.
_auth_enabled: bool = False
_session_cache: dict[str, dict] = {}  # token -> {expires_at, plex_user_id, role}


def auth_enabled() -> bool:
    if os.getenv("NASTUNE_DISABLE_AUTH") == "1":
        return False
    return _auth_enabled


async def load_auth_flag() -> None:
    global _auth_enabled
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key='plex_auth_enabled'"
        ) as cur:
            row = await cur.fetchone()
    _auth_enabled = bool(row and row[0] == "true")
    if os.getenv("NASTUNE_DISABLE_AUTH") == "1" and _auth_enabled:
        log.warning("NASTUNE_DISABLE_AUTH=1 — Plex auth gate is bypassed")


async def set_auth_enabled(enabled: bool) -> None:
    global _auth_enabled
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('plex_auth_enabled', ?)",
            ("true" if enabled else "false",),
        )
        await db.commit()
    _auth_enabled = enabled


# ── users (admin approval list) ──────────────────────────────────────────────

async def upsert_user(user: dict, role: str | None = None,
                      enabled: bool | None = None) -> dict:
    """Insert or refresh a plex_users row; returns the stored row as a dict."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO plex_users(plex_user_id, username, email, thumb, role, enabled, created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(plex_user_id) DO UPDATE SET
                 username=excluded.username, email=excluded.email, thumb=excluded.thumb""",
            (user["id"], user["username"], user.get("email", ""), user.get("thumb", ""),
             role or "user", 1 if enabled else 0, now),
        )
        if role is not None:
            await db.execute(
                "UPDATE plex_users SET role=?, enabled=? WHERE plex_user_id=?",
                (role, 1 if enabled else 0, user["id"]),
            )
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM plex_users WHERE plex_user_id=?", (user["id"],)
        ) as cur:
            row = await cur.fetchone()
    return dict(row)


async def get_user_row(plex_user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM plex_users WHERE plex_user_id=?", (plex_user_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM plex_users ORDER BY role='admin' DESC, created_at"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_user_enabled(plex_user_id: int, enabled: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE plex_users SET enabled=? WHERE plex_user_id=? AND role != 'admin'",
            (1 if enabled else 0, plex_user_id),
        )
        if not enabled:
            await db.execute(
                "DELETE FROM plex_sessions WHERE plex_user_id=?", (plex_user_id,)
            )
        await db.commit()
        changed = cur.rowcount > 0
    if not enabled:
        for token in [t for t, s in _session_cache.items()
                      if s["plex_user_id"] == plex_user_id]:
            _session_cache.pop(token, None)
    return changed


# ── sessions ─────────────────────────────────────────────────────────────────

async def create_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + SESSION_TTL
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO plex_sessions(token, plex_user_id, username, thumb, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (token, user["id"], user["username"], user.get("thumb", ""), now, expires),
        )
        await db.execute("DELETE FROM plex_sessions WHERE expires_at < ?", (now,))
        await db.execute(
            "UPDATE plex_users SET last_login_at=? WHERE plex_user_id=?", (now, user["id"]),
        )
        await db.commit()
    row = await get_user_row(user["id"])
    _session_cache[token] = {
        "expires_at": expires,
        "plex_user_id": user["id"],
        "role": (row or {}).get("role", "user"),
    }
    return token


async def get_session(token: str) -> dict | None:
    """Return {plex_user_id, role, expires_at} for a valid session, else None."""
    if not token:
        return None
    cached = _session_cache.get(token)
    now = int(time.time())
    if cached:
        if cached["expires_at"] < now:
            _session_cache.pop(token, None)
            return None
        return cached
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT s.plex_user_id, s.expires_at, COALESCE(u.role, 'user'), COALESCE(u.enabled, 0) "
            "FROM plex_sessions s LEFT JOIN plex_users u ON u.plex_user_id = s.plex_user_id "
            "WHERE s.token=?",
            (token,),
        ) as cur:
            row = await cur.fetchone()
    if not row or row[1] < now or not row[3]:
        return None
    session = {"plex_user_id": row[0], "expires_at": row[1], "role": row[2]}
    _session_cache[token] = session
    return session


async def get_session_user(token: str) -> dict | None:
    session = await get_session(token)
    if not session:
        return None
    row = await get_user_row(session["plex_user_id"])
    if not row:
        return None
    return {"username": row["username"], "thumb": row["thumb"], "role": row["role"]}


async def delete_session(token: str) -> None:
    _session_cache.pop(token, None)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM plex_sessions WHERE token=?", (token,))
        await db.commit()


# ── ASGI middleware ──────────────────────────────────────────────────────────

# Paths reachable without a session (login flow + static assets for the login page)
_EXEMPT_PREFIXES = ("/auth/", "/static/")
_EXEMPT_PATHS = ("/login",)

# Admin-only endpoints while the gate is enabled
_ADMIN_PATHS = {
    ("POST", "/settings"),
    ("POST", "/artwork/cache/drop"),
}
_ADMIN_PREFIXES = ("/plex/link", "/plex/unlink", "/plex/auth", "/plex/import", "/plex/users")


def _cookie_from_scope(scope: dict, name: str) -> str:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == name:
                    return val
    return ""


class PlexAuthMiddleware:
    """Pure-ASGI auth gate.

    Not BaseHTTPMiddleware: that wraps responses in a way that breaks
    `request.is_disconnected()` inside the SSE StreamingResponse generators.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not auth_enabled():
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            return await self.app(scope, receive, send)

        token = _cookie_from_scope(scope, COOKIE_NAME)
        session = await get_session(token)
        if session is None:
            return await self._reject(scope, send)

        if session["role"] != "admin" and self._is_admin_route(scope["method"], path):
            return await self._plain(send, 403, b'{"detail":"Admin access required"}')

        scope.setdefault("state", {})["plex_session"] = session
        return await self.app(scope, receive, send)

    @staticmethod
    def _is_admin_route(method: str, path: str) -> bool:
        if (method, path) in _ADMIN_PATHS:
            return True
        return path.startswith(_ADMIN_PREFIXES)

    async def _reject(self, scope, send):
        # Browsers navigating get the login page; fetch/EventSource get the
        # redirect too — the HTML content-type trips the frontend's apiFetch.
        headers = [(b"location", b"/login"), (b"content-length", b"0")]
        await send({"type": "http.response.start", "status": 302, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    async def _plain(self, send, status: int, body: bytes):
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode())]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
