import os
from pathlib import Path

import aiosqlite

DB_PATH = Path(os.environ.get("DB_PATH", "/data/nastune.db"))

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS walkman_devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    serial           TEXT    NOT NULL,
    storage_type     TEXT    NOT NULL,
    model            TEXT,
    marketing_name   TEXT,
    vendor           TEXT,
    firmware         TEXT,
    music_path       TEXT    NOT NULL DEFAULT 'MUSIC',
    scan_status      TEXT    NOT NULL DEFAULT 'idle',
    scan_processed   INTEGER NOT NULL DEFAULT 0,
    scan_total       INTEGER NOT NULL DEFAULT 0,
    scan_current_file TEXT,
    scan_error       TEXT,
    last_scanned_at  INTEGER,
    track_count      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(serial, storage_type)
);

CREATE TABLE IF NOT EXISTS walkman_tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES walkman_devices(id) ON DELETE CASCADE,
    path            TEXT    NOT NULL,
    title           TEXT,
    artist          TEXT,
    albumartist     TEXT,
    album           TEXT,
    disc_nr         INTEGER,
    track_nr        INTEGER,
    duration_ms     INTEGER,
    bitrate         INTEGER,
    samplerate      INTEGER,
    bits_per_sample INTEGER,
    year            INTEGER,
    genre           TEXT,
    composer        TEXT,
    size            INTEGER,
    filetype        TEXT,
    has_artwork     INTEGER NOT NULL DEFAULT 0,
    scanned_at      INTEGER NOT NULL,
    UNIQUE(device_id, path)
);

CREATE INDEX IF NOT EXISTS idx_wt_device  ON walkman_tracks(device_id);
CREATE INDEX IF NOT EXISTS idx_wt_artist  ON walkman_tracks(device_id, albumartist, artist);

CREATE TABLE IF NOT EXISTS sources (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    type             TEXT    NOT NULL DEFAULT 'folder',
    name             TEXT    NOT NULL,
    path             TEXT    NOT NULL,
    added_at         INTEGER NOT NULL,
    last_scanned_at  INTEGER,
    scan_status      TEXT    NOT NULL DEFAULT 'pending',
    scan_error       TEXT,
    track_count      INTEGER NOT NULL DEFAULT 0,
    scan_processed   INTEGER NOT NULL DEFAULT 0,
    scan_total       INTEGER NOT NULL DEFAULT 0,
    scan_current_file TEXT
);

CREATE TABLE IF NOT EXISTS source_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    artist      TEXT,
    albumartist TEXT,
    album       TEXT,
    title       TEXT,
    disc_nr     INTEGER,
    track_nr    INTEGER,
    duration_ms INTEGER,
    bitrate     INTEGER,
    samplerate  INTEGER,
    year        INTEGER,
    size            INTEGER,
    file_mtime      INTEGER,
    codec           TEXT,
    bits_per_sample INTEGER,
    scanned_at      INTEGER NOT NULL,
    UNIQUE(source_id, path)
);

CREATE INDEX IF NOT EXISTS idx_st_source   ON source_tracks(source_id);
CREATE INDEX IF NOT EXISTS idx_st_artist   ON source_tracks(source_id, albumartist, artist);

CREATE TABLE IF NOT EXISTS ipod_track_ratings (
    track_key   TEXT PRIMARY KEY,
    rating      INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ipod_devices (
    id           INTEGER PRIMARY KEY,
    uuid         TEXT UNIQUE NOT NULL,
    model        TEXT,
    ipod_name    TEXT,
    capacity     INTEGER,
    first_seen   INTEGER NOT NULL DEFAULT (unixepoch()),
    last_seen    INTEGER NOT NULL DEFAULT (unixepoch()),
    library_json TEXT
);

CREATE TABLE IF NOT EXISTS ipod_device_settings (
    device_id  INTEGER PRIMARY KEY REFERENCES ipod_devices(id) ON DELETE CASCADE,
    force_aac  INTEGER
);

CREATE TABLE IF NOT EXISTS ipod_sync_rules (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES ipod_devices(id) ON DELETE CASCADE,
    media_type  TEXT NOT NULL DEFAULT 'music',
    enabled     INTEGER NOT NULL DEFAULT 0,
    source_id   INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    UNIQUE(device_id, media_type)
);

CREATE TABLE IF NOT EXISTS walkman_device_settings (
    device_id  INTEGER PRIMARY KEY REFERENCES walkman_devices(id) ON DELETE CASCADE,
    force_aac  INTEGER
);

CREATE TABLE IF NOT EXISTS walkman_sync_rules (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES walkman_devices(id) ON DELETE CASCADE,
    media_type  TEXT NOT NULL DEFAULT 'music',
    enabled     INTEGER NOT NULL DEFAULT 0,
    source_id   INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    UNIQUE(device_id, media_type)
);
"""


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Migrate existing databases that lack the progress columns
        async with db.execute("PRAGMA table_info(sources)") as cur:
            existing_cols = {row[1] for row in await cur.fetchall()}
        async with db.execute("PRAGMA table_info(source_tracks)") as cur:
            st_cols = {row[1] for row in await cur.fetchall()}
        for col, ddl in [
            ("codec",           "ALTER TABLE source_tracks ADD COLUMN codec           TEXT"),
            ("bits_per_sample", "ALTER TABLE source_tracks ADD COLUMN bits_per_sample INTEGER"),
            ("disc_nr",         "ALTER TABLE source_tracks ADD COLUMN disc_nr         INTEGER"),
        ]:
            if col not in st_cols:
                await db.execute(ddl)

        for col, ddl in [
            ("scan_processed",    "ALTER TABLE sources ADD COLUMN scan_processed    INTEGER NOT NULL DEFAULT 0"),
            ("scan_total",        "ALTER TABLE sources ADD COLUMN scan_total        INTEGER NOT NULL DEFAULT 0"),
            ("scan_current_file", "ALTER TABLE sources ADD COLUMN scan_current_file TEXT"),
        ]:
            if col not in existing_cols:
                await db.execute(ddl)

        async with db.execute("PRAGMA table_info(walkman_tracks)") as cur:
            wt_cols = {row[1] for row in await cur.fetchall()}
        if "has_artwork" not in wt_cols:
            await db.execute("ALTER TABLE walkman_tracks ADD COLUMN has_artwork INTEGER NOT NULL DEFAULT 0")

        async with db.execute("PRAGMA table_info(ipod_devices)") as cur:
            ipod_cols = {row[1] for row in await cur.fetchall()}
        if "ipod_name" not in ipod_cols:
            await db.execute("ALTER TABLE ipod_devices ADD COLUMN ipod_name TEXT")

        # Rename legacy type='folder' to 'music' for all existing sources
        await db.execute("UPDATE sources SET type='music' WHERE type='folder'")

        await db.commit()
