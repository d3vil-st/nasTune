import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path

import aiosqlite

from app.services.db import DB_PATH

log = logging.getLogger(__name__)
AUDIO_EXT = {'.mp3', '.m4a', '.aac', '.flac', '.aiff', '.aif', '.wav', '.ogg'}


def _ffprobe_date(file_path: Path) -> str | None:
    """Run ffprobe to extract the date tag. ffprobe reads XMP private frames and
    other sources that mutagen misses, so it returns the correct publication date
    for podcasts where TDRC holds the download timestamp instead."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(file_path)],
            capture_output=True, text=True, timeout=15,
        )
        tags = json.loads(r.stdout).get('format', {}).get('tags', {})
        # ffprobe may return keys in either case depending on the container
        return tags.get('date') or tags.get('DATE') or None
    except Exception:
        return None


def _read_track(file_path: Path, source_type: str = 'music') -> dict | None:
    try:
        from mutagen import File as MFile
        audio = MFile(str(file_path), easy=True)
        if audio is None:
            return None

        def g(k):
            v = audio.get(k)
            return str(v[0]).strip() if v else None

        def gi(k):
            v = g(k)
            try:
                return int(str(v).split('/')[0]) if v else None
            except (ValueError, TypeError):
                return None

        info = audio.info
        stat = file_path.stat()

        # codec and bit depth: easy=True gives us EasyMP4 whose info is MP4Info,
        # but older mutagen may not expose .codec. Re-open as MP4 for M4A to be sure.
        codec = getattr(info, 'codec', None)
        bits_per_sample = getattr(info, 'bits_per_sample', None)
        if file_path.suffix.lower() in ('.m4a', '.aac'):
            try:
                from mutagen.mp4 import MP4 as _MP4
                _mp4info = _MP4(str(file_path)).info
                if not codec:
                    codec = getattr(_mp4info, 'codec', None)
                if not bits_per_sample:
                    bits_per_sample = getattr(_mp4info, 'bits_per_sample', None)
            except Exception:
                pass
        if codec:
            codec = codec.lower()

        date_str = g('date') or ''
        if source_type == 'podcast':
            date_str = _ffprobe_date(file_path) or date_str
        return {
            'path': str(file_path),
            'artist': g('artist'),
            'albumartist': g('albumartist') or g('artist'),
            'album': g('album'),
            'title': g('title') or file_path.stem,
            'disc_nr': gi('discnumber'),
            'track_nr': gi('tracknumber'),
            'duration_ms': int(info.length * 1000) if hasattr(info, 'length') else None,
            'bitrate': int(getattr(info, 'bitrate', 0) / 1000) or None,
            'samplerate': getattr(info, 'sample_rate', None),
            'bits_per_sample': bits_per_sample,
            'year': int(date_str.split('-')[0].split('/')[0]) if date_str else None,
            'pub_date': date_str or None,
            'size': stat.st_size,
            'file_mtime': int(stat.st_mtime),
            'codec': codec,
        }
    except Exception as exc:
        log.debug("Failed to read %s: %s", file_path, exc)
        return None


async def _flush_batch(batch: list[dict]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO source_tracks
                   (source_id, path, artist, albumartist, album, title, disc_nr, track_nr,
                    duration_ms, bitrate, samplerate, year, pub_date, size, file_mtime, codec, bits_per_sample, scanned_at)
               VALUES
                   (:source_id, :path, :artist, :albumartist, :album, :title, :disc_nr, :track_nr,
                    :duration_ms, :bitrate, :samplerate, :year, :pub_date, :size, :file_mtime, :codec, :bits_per_sample, :scanned_at)
               ON CONFLICT(source_id, path) DO UPDATE SET
                   artist=excluded.artist, albumartist=excluded.albumartist,
                   album=excluded.album, title=excluded.title,
                   disc_nr=excluded.disc_nr, track_nr=excluded.track_nr,
                   duration_ms=excluded.duration_ms, bitrate=excluded.bitrate,
                   samplerate=excluded.samplerate, year=excluded.year,
                   pub_date=excluded.pub_date,
                   size=excluded.size, file_mtime=excluded.file_mtime,
                   codec=excluded.codec, bits_per_sample=excluded.bits_per_sample,
                   scanned_at=excluded.scanned_at""",
            batch,
        )
        await db.commit()


def _find_files(root: Path) -> list[tuple[Path, int, int]]:
    """Audio files under root as (path, mtime, size) — stat once, in the thread."""
    out = []
    for p in root.rglob('*'):
        if p.is_file() and p.suffix.lower() in AUDIO_EXT:
            st = p.stat()
            out.append((p, int(st.st_mtime), st.st_size))
    return out


async def _write_progress(source_id: int, processed: int, total: int, current_file: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sources SET scan_processed=?, scan_total=?, scan_current_file=? WHERE id=?",
            (processed, total, current_file, source_id),
        )
        await db.commit()


def _remap_podcast(track: dict) -> dict:
    """Remap podcast tags: album tag = show name (artist/albumartist for key matching).
    album is intentionally kept as the show name so the track key matches the iPod's
    album field. Season/year grouping is done at library-build time, not in the DB."""
    # iPod groups podcasts purely by album tag (Main Menu → Podcasts → Shows).
    # Artist is irrelevant for grouping — use album as the canonical show name.
    show = track.get('album') or track.get('albumartist') or track.get('artist') or 'Unknown Show'
    track['artist'] = show
    track['albumartist'] = show
    # album stays as the original tag value (show name) — do NOT overwrite with year
    return track


async def scan_source(source_id: int, root: str, source_type: str = 'music') -> None:
    log.info("Scanning source %d: %s", source_id, root)
    root_path = Path(root)
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE sources SET scan_status='scanning', scan_error=NULL,
               scan_processed=0, scan_total=0, scan_current_file='Finding files…',
               last_scanned_at=? WHERE id=?""",
            (now, source_id),
        )
        await db.commit()

    try:
        # Known files: skip tag-reading when mtime+size are unchanged. A full
        # rescan clears source_tracks first, so everything re-reads there.
        existing: dict[str, tuple[int | None, int | None]] = {}
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT path, file_mtime, size FROM source_tracks WHERE source_id=?",
                (source_id,),
            ) as cur:
                async for path, mtime, size in cur:
                    existing[path] = (mtime, size)

        # Run rglob in a thread — it blocks and would stall the event loop for large trees
        files = await asyncio.to_thread(_find_files, root_path)
        total = len(files)
        log.info("Source %d: found %d audio files", source_id, total)

        await _write_progress(source_id, 0, total, "")

        batch: list[dict] = []
        unchanged: list[str] = []
        processed = 0

        for fpath, mtime, size in files:
            spath = str(fpath)
            if existing.get(spath) == (mtime, size):
                unchanged.append(spath)
                processed += 1
                continue
            track = await asyncio.to_thread(_read_track, fpath, source_type)
            if track:
                if source_type == 'podcast':
                    track = _remap_podcast(track)
                track['source_id'] = source_id
                track['scanned_at'] = now
                batch.append(track)
            processed += 1

            if processed % 10 == 0 or processed == 1:
                await _write_progress(source_id, processed, total, fpath.name)

            if len(batch) >= 100:
                await _flush_batch(batch)
                batch.clear()
                log.debug("Source %d: processed %d/%d", source_id, processed, total)

        if batch:
            await _flush_batch(batch)

        # Keep unchanged rows alive past the stale-row cleanup below
        if unchanged:
            log.info("Source %d: %d file(s) unchanged, tags not re-read", source_id, len(unchanged))
            async with aiosqlite.connect(DB_PATH) as db:
                for i in range(0, len(unchanged), 500):
                    chunk = unchanged[i:i + 500]
                    await db.execute(
                        f"UPDATE source_tracks SET scanned_at=? WHERE source_id=? "
                        f"AND path IN ({','.join('?' * len(chunk))})",
                        (now, source_id, *chunk),
                    )
                await db.commit()

        # Remove DB rows for files that no longer exist on disk
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM source_tracks WHERE source_id=? AND scanned_at < ?",
                (source_id, now),
            )
            cur = await db.execute(
                "SELECT COUNT(*) FROM source_tracks WHERE source_id=?", (source_id,)
            )
            count = (await cur.fetchone())[0]
            await db.execute(
                """UPDATE sources SET scan_status='done', track_count=?, last_scanned_at=?,
                   scan_processed=?, scan_total=?, scan_current_file=NULL WHERE id=?""",
                (count, now, total, total, source_id),
            )
            await db.commit()

        log.info("Source %d scan complete: %d tracks", source_id, count)

    except Exception as exc:
        log.error("Source %d scan failed: %s", source_id, exc)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE sources SET scan_status='error', scan_error=?, scan_current_file=NULL WHERE id=?",
                (str(exc), source_id),
            )
            await db.commit()


async def rescan_rule_sources(device_id: int, device_type: str = "ipod") -> list[str]:
    """Incrementally rescan the sources referenced by a device's enabled sync
    rules, so auto-sync sees files added since the last manual scan. Returns
    the names of the sources rescanned. Sources already scanning are skipped
    rather than awaited — whatever that scan has flushed so far is still
    picked up by compute_auto_sync_paths, and the next run gets the rest.
    """
    rules_table = "ipod_sync_rules" if device_type == "ipod" else "walkman_sync_rules"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT media_type, source_id FROM {rules_table} WHERE device_id=? AND enabled=1",
            (device_id,),
        ) as cur:
            rules = await cur.fetchall()
        if not rules:
            return []
        async with db.execute("SELECT id, name, path, type, scan_status FROM sources") as cur:
            sources = await cur.fetchall()

    wanted: dict[int, tuple] = {}
    for media_type, rule_source_id in rules:
        for sid, name, path, stype, status in sources:
            if stype != media_type:
                continue
            if rule_source_id is not None and sid != rule_source_id:
                continue
            wanted[sid] = (sid, name, path, stype, status)

    rescanned: list[str] = []
    for sid, name, path, stype, status in wanted.values():
        if status == "scanning":
            log.info("pre-sync rescan: source %d (%s) already scanning, skipped", sid, name)
            continue
        try:
            await scan_source(sid, path, stype)
            rescanned.append(name)
        except Exception as exc:
            log.warning("pre-sync rescan of source %d (%s) failed: %s", sid, name, exc)
    return rescanned
