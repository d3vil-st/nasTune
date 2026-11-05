"""Source files gpod-cp could not read or transcode (corrupt, no audio stream…).

Auto-sync leaves a recorded file out until it changes on disk (mtime/size), so
one bad file isn't re-attempted — and reported as failed — on every connect.
Manual syncs always retry; a successful copy (or duplicate) clears the record.
"""
import os
import time

import aiosqlite

from app.services.db import DB_PATH


async def record(failures: list[tuple[str, str]]) -> None:
    """Upsert (path, reason) pairs with the file's current mtime/size."""
    now = int(time.time())
    rows = []
    for path, reason in dict(failures).items():
        try:
            st = os.stat(path)
        except OSError:
            continue
        rows.append((path, st.st_mtime, st.st_size, reason, now))
    if not rows:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO sync_failures (path, file_mtime, size, reason, failed_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET file_mtime=excluded.file_mtime, size=excluded.size, "
            "reason=excluded.reason, failed_at=excluded.failed_at",
            rows,
        )
        await db.commit()


async def clear(paths: list[str]) -> None:
    if not paths:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany("DELETE FROM sync_failures WHERE path=?", [(p,) for p in set(paths)])
        await db.commit()


async def filter_known_bad(groups: dict[str, list[str]]) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Drop paths recorded in sync_failures whose file is unchanged since the failure.

    Returns (remaining groups, [(path, reason)] skipped)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT path, file_mtime, size, reason FROM sync_failures") as cur:
            known = {p: (mtime, size, reason) for p, mtime, size, reason in await cur.fetchall()}
    if not known:
        return groups, []
    skipped: list[tuple[str, str]] = []
    out: dict[str, list[str]] = {}
    for media_type, paths in groups.items():
        keep = []
        for p in paths:
            rec = known.get(p)
            if rec:
                try:
                    st = os.stat(p)
                    if st.st_mtime == rec[0] and st.st_size == rec[1]:
                        skipped.append((p, rec[2]))
                        continue
                except OSError:
                    pass
            keep.append(p)
        if keep:
            out[media_type] = keep
    return out, skipped
