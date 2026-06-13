import os
from pathlib import Path

import aiosqlite

DB_PATH = Path(os.environ.get("DB_PATH", "/data/nastune.db"))

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

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
    track_nr    INTEGER,
    duration_ms INTEGER,
    bitrate     INTEGER,
    samplerate  INTEGER,
    year        INTEGER,
    size        INTEGER,
    file_mtime  INTEGER,
    codec       TEXT,
    scanned_at  INTEGER NOT NULL,
    UNIQUE(source_id, path)
);

CREATE INDEX IF NOT EXISTS idx_st_source   ON source_tracks(source_id);
CREATE INDEX IF NOT EXISTS idx_st_artist   ON source_tracks(source_id, albumartist, artist);
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
            ("codec", "ALTER TABLE source_tracks ADD COLUMN codec TEXT"),
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
        await db.commit()
