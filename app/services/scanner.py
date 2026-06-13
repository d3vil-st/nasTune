import asyncio
import logging
import time
from pathlib import Path

import aiosqlite

from app.services.db import DB_PATH

log = logging.getLogger(__name__)
AUDIO_EXT = {'.mp3', '.m4a', '.aac', '.flac', '.aiff', '.aif', '.wav', '.ogg'}


def _read_track(file_path: Path) -> dict | None:
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

        return {
            'path': str(file_path),
            'artist': g('artist'),
            'albumartist': g('albumartist') or g('artist'),
            'album': g('album'),
            'title': g('title') or file_path.stem,
            'track_nr': gi('tracknumber'),
            'duration_ms': int(info.length * 1000) if hasattr(info, 'length') else None,
            'bitrate': int(getattr(info, 'bitrate', 0) / 1000) or None,
            'samplerate': getattr(info, 'sample_rate', None),
            'bits_per_sample': bits_per_sample,
            'year': gi('date'),
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
                   (source_id, path, artist, albumartist, album, title, track_nr,
                    duration_ms, bitrate, samplerate, year, size, file_mtime, codec, bits_per_sample, scanned_at)
               VALUES
                   (:source_id, :path, :artist, :albumartist, :album, :title, :track_nr,
                    :duration_ms, :bitrate, :samplerate, :year, :size, :file_mtime, :codec, :bits_per_sample, :scanned_at)
               ON CONFLICT(source_id, path) DO UPDATE SET
                   artist=excluded.artist, albumartist=excluded.albumartist,
                   album=excluded.album, title=excluded.title, track_nr=excluded.track_nr,
                   duration_ms=excluded.duration_ms, bitrate=excluded.bitrate,
                   samplerate=excluded.samplerate, year=excluded.year,
                   size=excluded.size, file_mtime=excluded.file_mtime,
                   codec=excluded.codec, bits_per_sample=excluded.bits_per_sample,
                   scanned_at=excluded.scanned_at""",
            batch,
        )
        await db.commit()


def _find_files(root: Path) -> list[Path]:
    return [p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in AUDIO_EXT]


async def _write_progress(source_id: int, processed: int, total: int, current_file: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sources SET scan_processed=?, scan_total=?, scan_current_file=? WHERE id=?",
            (processed, total, current_file, source_id),
        )
        await db.commit()


async def scan_source(source_id: int, root: str) -> None:
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
        # Run rglob in a thread — it blocks and would stall the event loop for large trees
        files = await asyncio.to_thread(_find_files, root_path)
        total = len(files)
        log.info("Source %d: found %d audio files", source_id, total)

        await _write_progress(source_id, 0, total, "")

        batch: list[dict] = []
        processed = 0

        for fpath in files:
            track = await asyncio.to_thread(_read_track, fpath)
            if track:
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
