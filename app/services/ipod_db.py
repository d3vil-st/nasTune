"""Persistent storage for known iPod devices, per-device settings, and sync rules."""
import json
import time

import aiosqlite

from app.services.db import DB_PATH

_MEDIA_TYPES = ("music", "audiobook", "podcast")


async def upsert_ipod(uuid: str, model: str | None, capacity: int | None,
                      library_json: str | None = None, ipod_name: str | None = None) -> int:
    """Insert or update ipod_devices row; returns the device id."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        if library_json is not None:
            await db.execute(
                """INSERT INTO ipod_devices(uuid, model, ipod_name, capacity, first_seen, last_seen, library_json)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(uuid) DO UPDATE SET
                     model=excluded.model, ipod_name=excluded.ipod_name,
                     capacity=excluded.capacity,
                     last_seen=excluded.last_seen, library_json=excluded.library_json""",
                (uuid, model, ipod_name, capacity, now, now, library_json),
            )
        else:
            await db.execute(
                """INSERT INTO ipod_devices(uuid, model, ipod_name, capacity, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(uuid) DO UPDATE SET
                     model=excluded.model, ipod_name=excluded.ipod_name,
                     capacity=excluded.capacity, last_seen=excluded.last_seen""",
                (uuid, model, ipod_name, capacity, now, now),
            )
        await db.commit()
        async with db.execute("SELECT id FROM ipod_devices WHERE uuid=?", (uuid,)) as cur:
            row = await cur.fetchone()
    return row[0]


async def get_ipod_db_id(uuid: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM ipod_devices WHERE uuid=?", (uuid,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def get_ipod_cached_library(device_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT library_json FROM ipod_devices WHERE id=?", (device_id,)
        ) as cur:
            row = await cur.fetchone()
    if row and row[0]:
        try:
            lib = json.loads(row[0])
            # Ensure albumartist is set on album objects (heals stale cached libraries)
            for artist in lib.get("artists", []):
                for album in artist.get("albums", []):
                    if not album.get("albumartist"):
                        album["albumartist"] = artist["name"]
            return lib
        except Exception:
            return None
    return None


async def get_known_ipods(connected_uuids: set[str] | None = None,
                          connected_db_ids: set[int] | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT d.id, d.uuid, d.model, d.ipod_name, d.capacity, d.first_seen, d.last_seen, "
            "d.library_json IS NOT NULL, s.force_aac, s.native_flac FROM ipod_devices d "
            "LEFT JOIN ipod_device_settings s ON s.device_id = d.id ORDER BY d.last_seen DESC"
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        device_id, uuid, model, ipod_name, capacity, first_seen, last_seen, has_cache, force_aac, native_flac = row
        connected = (
            (connected_uuids is not None and uuid in connected_uuids) or
            (connected_db_ids is not None and device_id in connected_db_ids)
        )
        result.append({
            "id": device_id,
            "uuid": uuid,
            "model": model or "iPod",
            "ipod_name": ipod_name or model or "iPod",
            "capacity": capacity,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "has_cache": bool(has_cache),
            "connected": connected,
            "force_aac": force_aac,  # None = use global, 0/1 = per-device override
            "native_flac": native_flac,  # same tri-state
        })
    return result


async def delete_ipod(device_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ipod_devices WHERE id=?", (device_id,))
        await db.commit()


async def get_device_settings(device_id: int, device_type: str = "ipod") -> dict:
    """Return {force_aac: None|0|1, native_flac: None|0|1, auto_sync, sync_rules: [{media_type, enabled, source_id}]}."""
    if device_type == "ipod":
        settings_table = "ipod_device_settings"
        rules_table = "ipod_sync_rules"
    else:
        settings_table = "walkman_device_settings"
        rules_table = "walkman_sync_rules"

    async with aiosqlite.connect(DB_PATH) as db:
        if device_type == "ipod":
            async with db.execute(
                f"SELECT force_aac, auto_sync, native_flac FROM {settings_table} WHERE device_id=?", (device_id,)
            ) as cur:
                srow = await cur.fetchone()
            force_aac = srow[0] if srow else None
            auto_sync = bool(srow[1]) if srow else False
            native_flac = srow[2] if srow else None
        else:
            async with db.execute(
                f"SELECT force_aac FROM {settings_table} WHERE device_id=?", (device_id,)
            ) as cur:
                srow = await cur.fetchone()
            force_aac = srow[0] if srow else None
            auto_sync = False
            native_flac = None

        async with db.execute(
            f"""SELECT r.media_type, r.enabled, r.source_id, s.name
                FROM {rules_table} r
                LEFT JOIN sources s ON s.id = r.source_id
                WHERE r.device_id=?
                ORDER BY r.media_type""",
            (device_id,),
        ) as cur:
            rule_rows = await cur.fetchall()

    existing = {r[0]: {"media_type": r[0], "enabled": bool(r[1]),
                        "source_id": r[2], "source_name": r[3]}
                for r in rule_rows}
    sync_rules = [existing.get(mt, {"media_type": mt, "enabled": False,
                                     "source_id": None, "source_name": None})
                  for mt in _MEDIA_TYPES]
    return {"force_aac": force_aac, "native_flac": native_flac, "auto_sync": auto_sync, "sync_rules": sync_rules}


async def save_device_settings(device_id: int, force_aac: int | None,
                                sync_rules: list[dict], device_type: str = "ipod",
                                auto_sync: bool = False, native_flac: int | None = None) -> None:
    if device_type == "ipod":
        settings_table = "ipod_device_settings"
        rules_table = "ipod_sync_rules"
    else:
        settings_table = "walkman_device_settings"
        rules_table = "walkman_sync_rules"

    async with aiosqlite.connect(DB_PATH) as db:
        if device_type == "ipod":
            await db.execute(
                "INSERT INTO ipod_device_settings(device_id, force_aac, auto_sync, native_flac) VALUES(?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET force_aac=excluded.force_aac, auto_sync=excluded.auto_sync, "
                "native_flac=excluded.native_flac",
                (device_id, None if force_aac is None else int(force_aac), int(bool(auto_sync)),
                 None if native_flac is None else int(native_flac)),
            )
        elif force_aac is None:
            await db.execute(f"DELETE FROM {settings_table} WHERE device_id=?", (device_id,))
        else:
            await db.execute(
                f"INSERT INTO {settings_table}(device_id, force_aac) VALUES(?,?) "
                f"ON CONFLICT(device_id) DO UPDATE SET force_aac=excluded.force_aac",
                (device_id, int(force_aac)),
            )
        for rule in sync_rules:
            mt = rule.get("media_type")
            if mt not in _MEDIA_TYPES:
                continue
            enabled = 1 if rule.get("enabled") else 0
            raw_sid = rule.get("source_id")
            source_id = int(raw_sid) if raw_sid not in (None, "", 0) else None
            await db.execute(
                f"""INSERT INTO {rules_table}(device_id, media_type, enabled, source_id)
                    VALUES(?,?,?,?)
                    ON CONFLICT(device_id, media_type) DO UPDATE SET
                      enabled=excluded.enabled, source_id=excluded.source_id""",
                (device_id, mt, enabled, source_id),
            )
        await db.commit()


async def get_autosync_ipod_by_uuid(uuid: str) -> int | None:
    """Return the iPod's db id when auto-sync-on-connect is enabled for it, else None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT d.id FROM ipod_devices d
               JOIN ipod_device_settings s ON s.device_id = d.id
               WHERE d.uuid = ? AND s.auto_sync = 1""",
            (uuid,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def get_effective_force_aac(device_id: int, device_type: str = "ipod") -> bool | None:
    """Return per-device override as bool, or None if 'use global'."""
    table = "ipod_device_settings" if device_type == "ipod" else "walkman_device_settings"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT force_aac FROM {table} WHERE device_id=?", (device_id,)
        ) as cur:
            row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return bool(row[0])


async def get_effective_native_flac(device_id: int) -> bool | None:
    """Return the iPod's 'Skip FLAC transcoding' override as bool, or None if 'use global'."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT native_flac FROM ipod_device_settings WHERE device_id=?", (device_id,)
        ) as cur:
            row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return bool(row[0])


async def compute_auto_sync_paths(device_lib: dict, device_id: int,
                                   device_type: str = "ipod") -> dict[str, list[str]]:
    """Source file paths for tracks not yet on the device, per enabled sync rule.

    Returns {media_type: [paths]} — grouping matters because gpod-cp needs
    --tracks-media-type for podcasts/audiobooks or they land as music.
    """
    from app.services.track_key import track_key as _tk

    rules_table = "ipod_sync_rules" if device_type == "ipod" else "walkman_sync_rules"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT media_type, enabled, source_id FROM {rules_table} WHERE device_id=?",
            (device_id,),
        ) as cur:
            rules = await cur.fetchall()

    enabled_rules = [(mt, sid) for mt, en, sid in rules if en]
    if not enabled_rules:
        return {}

    device_keys: set[str] = set()
    for artist in device_lib.get("artists", []):
        for album in artist["albums"]:
            for t in album["tracks"]:
                # Podcasts key by album (show name) only — artist tag is irrelevant.
                if t.get("mediatype") == "podcast":
                    artist_key = album["name"]
                else:
                    artist_key = t.get("artist") or artist["name"]
                key = _tk(artist_key, album["name"], t.get("track_nr"), t.get("disc_nr"), t.get("title", ""))
                device_keys.add(key)

    groups: dict[str, list[str]] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        for media_type, source_id in enabled_rules:
            paths = groups.setdefault(media_type, [])
            if source_id:
                q = ("SELECT st.path, st.artist, st.albumartist, st.album, st.track_nr, st.disc_nr, st.title "
                     "FROM source_tracks st JOIN sources s ON s.id=st.source_id "
                     "WHERE s.id=? AND s.type=? AND s.scan_status='done'")
                params: tuple = (source_id, media_type)
            else:
                q = ("SELECT st.path, st.artist, st.albumartist, st.album, st.track_nr, st.disc_nr, st.title "
                     "FROM source_tracks st JOIN sources s ON s.id=st.source_id "
                     "WHERE s.type=? AND s.scan_status='done'")
                params = (media_type,)
            async with db.execute(q, params) as cur:
                for path, artist, albumartist, album, track_nr, disc_nr, title in await cur.fetchall():
                    if media_type == "podcast":
                        key = _tk(album or "", album or "", track_nr, disc_nr, title or "")
                    else:
                        key = _tk(artist or albumartist or "", album or "", track_nr, disc_nr, title or "")
                    if key not in device_keys:
                        paths.append(path)
    return {mt: p for mt, p in groups.items() if p}
