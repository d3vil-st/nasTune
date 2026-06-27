import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import aiosqlite

from app.services.db import DB_PATH
from app.services.fs_utils import fs_usage, fs_type

log = logging.getLogger(__name__)

SENTINEL = "default-capability.xml"

AUDIO_EXT = {
    '.mp3', '.m4a', '.aac', '.flac', '.aiff', '.aif', '.wav',
    '.dsf', '.dff', '.ape', '.wma', '.mp4', '.3gp',
}

# devnode -> asyncio.Task, to prevent concurrent scans per device
_scan_tasks: dict[int, asyncio.Task] = {}


def parse_capability(mount: Path) -> dict | None:
    """Parse default-capability.xml. Returns None if not a WALKMAN mount."""
    xml_path = mount / SENTINEL
    if not xml_path.exists():
        return None
    try:
        tree = ElementTree.parse(str(xml_path))
        root = tree.getroot()
        device = root.find('device')
        if device is None:
            return None

        def txt(parent, tag, default=''):
            if parent is None:
                return default
            child = parent.find(tag)
            return (child.text or '').strip() if child is not None else default

        ident   = device.find('identification')
        storage = device.find('storage')
        fs_path = device.find('filesystem/path')

        music_raw = txt(fs_path, 'sound', '\\MUSIC\\')
        music_rel = music_raw.replace('\\', '/').strip('/')
        if not music_rel:
            music_rel = 'MUSIC'

        return {
            'model':          txt(ident,   'model')           or 'WALKMAN',
            'marketing_name': txt(ident,   'marketingname'),
            'vendor':         txt(ident,   'vendor'),
            'firmware':       txt(ident,   'firmwareversion'),
            'storage_type':   txt(storage, 'type', 'INTERNAL').upper(),
            'music_path':     music_rel,
        }
    except Exception as exc:
        log.warning("Failed to parse %s: %s", xml_path, exc)
        return None


async def get_serial(devnode: str) -> str:
    """Return USB serial via udevadm; empty string on failure."""
    disk = re.sub(r'p?\d+$', '', devnode)  # /dev/sdb1 → /dev/sdb
    try:
        proc = await asyncio.create_subprocess_exec(
            'udevadm', 'info', '--query=property', f'--name={disk}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        serial = ''
        for line in stdout.decode().splitlines():
            if line.startswith('ID_SERIAL_SHORT='):
                return line.split('=', 1)[1].strip()
            if line.startswith('ID_SERIAL='):
                serial = line.split('=', 1)[1].strip()
        return serial
    except Exception as exc:
        log.debug("udevadm serial lookup failed for %s: %s", devnode, exc)
        return ''


async def get_or_create_db_device(serial: str, cap: dict) -> int:
    """Upsert walkman_devices row; return its id."""
    storage_type = cap['storage_type']
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO walkman_devices
                   (serial, storage_type, model, marketing_name, vendor, firmware, music_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(serial, storage_type) DO UPDATE SET
                   model=excluded.model, marketing_name=excluded.marketing_name,
                   vendor=excluded.vendor, firmware=excluded.firmware,
                   music_path=excluded.music_path""",
            (serial, storage_type, cap['model'], cap.get('marketing_name'),
             cap.get('vendor'), cap.get('firmware'), cap['music_path']),
        )
        await db.commit()
        async with db.execute(
            'SELECT id FROM walkman_devices WHERE serial=? AND storage_type=?',
            (serial, storage_type),
        ) as cur:
            row = await cur.fetchone()
    return row[0]


async def fetch_library(mount: str, db_device_id: int, serial: str, cap: dict) -> dict[str, Any]:
    """Build the library dict from walkman_tracks (same shape as gpod._parse output)."""
    mount_path = Path(mount)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM walkman_tracks WHERE device_id=? ORDER BY albumartist, album, disc_nr, track_nr, title',
            (db_device_id,),
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            'SELECT scan_status, scan_processed, scan_total, scan_current_file, scan_error, last_scanned_at, track_count '
            'FROM walkman_devices WHERE id=?',
            (db_device_id,),
        ) as cur:
            dev = await cur.fetchone()

    library: dict[str, dict] = {}
    total_bytes = 0

    for t in rows:
        artist    = t['albumartist'] or t['artist'] or 'Unknown Artist'
        album_name = t['album'] or 'Unknown Album'
        year      = t['year'] or 0
        total_bytes += t['size'] or 0

        if artist not in library:
            library[artist] = {}
        if album_name not in library[artist]:
            library[artist][album_name] = {'name': album_name, 'year': year, 'tracks': []}

        library[artist][album_name]['tracks'].append({
            'id':          t['id'],
            'artist':      t['artist'] or '',
            'title':       t['title'] or 'Unknown',
            'disc_nr':     t['disc_nr'] or 0,
            'track_nr':    t['track_nr'] or 0,
            'duration_ms': t['duration_ms'] or 0,
            'filetype':    t['filetype'] or '',
            'bitrate':     t['bitrate'] or 0,
            'samplerate':  t['samplerate'] or 0,
            'size':        t['size'] or 0,
            'playcount':   0,
            'rating':      0,
            'artwork':     bool(t['has_artwork']),
            'ipod_path':   t['path'],  # mount-relative, e.g. MUSIC/Artist/Album/track.flac
            'genre':       t['genre'] or '',
            'composer':    t['composer'] or '',
            'year':        t['year'] or 0,
            'time_added':  0,
            'time_played': 0,
            'missing':     not (mount_path / t['path']).exists(),
        })

    for al_dict in library.values():
        for al in al_dict.values():
            al['tracks'].sort(key=lambda t: (t['disc_nr'], t['track_nr']))

    artists_sorted = sorted(library.keys(), key=_sort_key)
    result_artists = []
    for artist in artists_sorted:
        albums = sorted(
            library[artist].values(),
            key=lambda a: (a['year'] if a['year'] > 0 else 9999, a['name'].lower()),
        )
        track_count = sum(len(a['tracks']) for a in albums)
        result_artists.append({'name': artist, 'albums': albums, 'track_count': track_count})

    fs_total_bytes, fs_used_bytes = fs_usage(mount_path)
    used_pct = round(min(fs_used_bytes / fs_total_bytes * 100, 100), 1) if fs_total_bytes else 0

    return {
        'device': {
            'model_name':    cap['model'],
            'uuid':          '',
            'vendor':        cap.get('vendor', ''),
            'firmware':      cap.get('firmware', ''),
            'storage_type':  cap['storage_type'],
            'serial':        serial,
        },
        'ipod_name':        cap.get('marketing_name') or cap['model'],
        'walkman':          True,
        'walkman_db_id':    db_device_id,
        'scan_status':      dev['scan_status']       if dev else 'idle',
        'scan_processed':   dev['scan_processed']    if dev else 0,
        'scan_total':       dev['scan_total']        if dev else 0,
        'scan_current_file': dev['scan_current_file'] if dev else None,
        'scan_error':       dev['scan_error']        if dev else None,
        'last_scanned_at':  dev['last_scanned_at']   if dev else None,
        'total_tracks':     sum(a['track_count'] for a in result_artists),
        'total_albums':     sum(len(a['albums'])  for a in result_artists),
        'total_bytes':      total_bytes,
        'total_size_gb':    round(total_bytes / 1024 ** 3, 2),
        'fs_total_gb':      round(fs_total_bytes / 1024 ** 3, 2) if fs_total_bytes else 0,
        'fs_used_gb':       round(fs_used_bytes  / 1024 ** 3, 2) if fs_total_bytes else 0,
        'fs_type':          fs_type(mount_path),
        'used_pct':         used_pct,
        'artists':          result_artists,
    }


async def scan_status(db_device_id: int) -> dict:
    """Return live scan progress from walkman_devices row."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT scan_status, scan_processed, scan_total, scan_current_file, scan_error '
            'FROM walkman_devices WHERE id=?',
            (db_device_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {'status': 'idle', 'processed': 0, 'total': 0, 'current_file': None, 'error': None}
    return {
        'status':       row['scan_status'],
        'processed':    row['scan_processed'],
        'total':        row['scan_total'],
        'current_file': row['scan_current_file'],
        'error':        row['scan_error'],
    }


def start_scan(db_device_id: int, mount: Path, music_path: str) -> None:
    """Start or restart a background scan task for the given walkman device."""
    existing = _scan_tasks.get(db_device_id)
    if existing and not existing.done():
        log.info("WALKMAN %d: scan already running, skipping", db_device_id)
        return
    task = asyncio.create_task(_scan(db_device_id, mount, music_path))
    _scan_tasks[db_device_id] = task


async def _scan(db_device_id: int, mount: Path, music_path: str) -> None:
    music_root = mount / music_path
    now = int(time.time())
    log.info("WALKMAN %d: scanning %s", db_device_id, music_root)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE walkman_devices SET scan_status='scanning', scan_error=NULL,
               scan_processed=0, scan_total=0, scan_current_file='Finding files…',
               last_scanned_at=? WHERE id=?""",
            (now, db_device_id),
        )
        await db.commit()

    try:
        if not music_root.exists():
            raise FileNotFoundError(f"Music directory not found: {music_root}")

        # Paths already indexed in the DB (relative to mount)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                'SELECT id, path FROM walkman_tracks WHERE device_id=?', (db_device_id,)
            ) as cur:
                db_rows = {row[1]: row[0] for row in await cur.fetchall()}  # path -> id

        # Files currently on disk (relative path -> absolute path)
        files = await asyncio.to_thread(_find_files, music_root)
        disk_files: dict[str, Path] = {str(f.relative_to(mount)): f for f in files}
        total = len(disk_files)
        log.info("WALKMAN %d: %d on disk, %d in DB", db_device_id, total, len(db_rows))
        await _write_progress(db_device_id, 0, total, '')

        # Remove DB rows whose files are gone
        removed = set(db_rows) - set(disk_files)
        if removed:
            removed_ids = [db_rows[p] for p in removed]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    f'DELETE FROM walkman_tracks WHERE id IN ({",".join("?" * len(removed_ids))})',
                    removed_ids,
                )
                await db.commit()
            log.info("WALKMAN %d: removed %d stale entries", db_device_id, len(removed_ids))

        # Read tags only for files not yet in the DB
        new_paths = set(disk_files) - set(db_rows)
        batch: list[dict] = []
        processed = 0

        for rel_path, fpath in disk_files.items():
            processed += 1
            if processed % 10 == 0 or processed == 1:
                await _write_progress(db_device_id, processed, total, fpath.name)

            if rel_path not in new_paths:
                continue  # already indexed — existence confirmed above

            track = await asyncio.to_thread(_read_track, fpath, mount)
            if track:
                track['device_id'] = db_device_id
                track['scanned_at'] = now
                batch.append(track)

            if len(batch) >= 100:
                await _flush_batch(batch)
                batch.clear()

        if batch:
            await _flush_batch(batch)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                'SELECT COUNT(*) FROM walkman_tracks WHERE device_id=?', (db_device_id,)
            ) as cur:
                count = (await cur.fetchone())[0]
            await db.execute(
                """UPDATE walkman_devices SET scan_status='done', track_count=?,
                   scan_processed=?, scan_total=?, scan_current_file=NULL WHERE id=?""",
                (count, total, total, db_device_id),
            )
            await db.commit()

        log.info("WALKMAN %d scan done: %d tracks (%d new, %d removed)",
                 db_device_id, count, len(new_paths), len(removed))

    except Exception as exc:
        log.error("WALKMAN %d scan failed: %s", db_device_id, exc)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE walkman_devices SET scan_status='error', scan_error=?, scan_current_file=NULL WHERE id=?",
                (str(exc), db_device_id),
            )
            await db.commit()


def _find_files(root: Path) -> list[Path]:
    return [p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in AUDIO_EXT]


def _read_track(file_path: Path, mount: Path) -> dict | None:
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

        codec = getattr(info, 'codec', None)
        bits_per_sample = getattr(info, 'bits_per_sample', None)
        if file_path.suffix.lower() in ('.m4a', '.aac', '.mp4'):
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

        # Lightweight artwork presence check — re-open without easy=True to access raw tags
        has_artwork = False
        try:
            from mutagen import File as MFileRaw
            raw = MFileRaw(str(file_path), easy=False)
            if raw is not None:
                # FLAC: pictures are in audio.pictures, not in tags
                if getattr(raw, 'pictures', None):
                    has_artwork = True
                elif raw.tags is not None:
                    tags = raw.tags
                    apic = hasattr(tags, 'values') and any(
                        getattr(v, 'FrameID', None) == 'APIC' for v in tags.values()
                    )
                    has_artwork = bool(
                        tags.get('covr') or apic or
                        tags.get('metadata_block_picture') or
                        tags.get('METADATA_BLOCK_PICTURE')
                    )
        except Exception:
            pass

        return {
            'path':          str(file_path.relative_to(mount)),
            'title':         g('title') or file_path.stem,
            'artist':        g('artist'),
            'albumartist':   g('albumartist') or g('artist'),
            'album':         g('album'),
            'disc_nr':       gi('discnumber'),
            'track_nr':      gi('tracknumber'),
            'duration_ms':   int(info.length * 1000) if hasattr(info, 'length') else None,
            'bitrate':       int(getattr(info, 'bitrate', 0) / 1000) or None,
            'samplerate':    getattr(info, 'sample_rate', None),
            'bits_per_sample': bits_per_sample,
            'year':          (lambda v: int(v.split('-')[0].split('/')[0]) if v else None)(g('date')),
            'size':          stat.st_size,
            'filetype':      file_path.suffix.lstrip('.').upper(),
            'genre':         g('genre'),
            'composer':      g('composer'),
            'has_artwork':   1 if has_artwork else 0,
        }
    except Exception as exc:
        log.debug("Failed to read %s: %s", file_path, exc)
        return None


async def _write_progress(db_device_id: int, processed: int, total: int, current_file: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'UPDATE walkman_devices SET scan_processed=?, scan_total=?, scan_current_file=? WHERE id=?',
            (processed, total, current_file, db_device_id),
        )
        await db.commit()


async def _flush_batch(batch: list[dict]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO walkman_tracks
                   (device_id, path, title, artist, albumartist, album, disc_nr, track_nr,
                    duration_ms, bitrate, samplerate, bits_per_sample, year, size, filetype,
                    genre, composer, has_artwork, scanned_at)
               VALUES
                   (:device_id, :path, :title, :artist, :albumartist, :album, :disc_nr, :track_nr,
                    :duration_ms, :bitrate, :samplerate, :bits_per_sample, :year, :size, :filetype,
                    :genre, :composer, :has_artwork, :scanned_at)
               ON CONFLICT(device_id, path) DO UPDATE SET
                   title=excluded.title, artist=excluded.artist, albumartist=excluded.albumartist,
                   album=excluded.album, disc_nr=excluded.disc_nr, track_nr=excluded.track_nr,
                   duration_ms=excluded.duration_ms, bitrate=excluded.bitrate,
                   samplerate=excluded.samplerate, bits_per_sample=excluded.bits_per_sample,
                   year=excluded.year, size=excluded.size, filetype=excluded.filetype,
                   genre=excluded.genre, composer=excluded.composer,
                   has_artwork=excluded.has_artwork, scanned_at=excluded.scanned_at""",
            batch,
        )
        await db.commit()


def _sort_key(name: str) -> str:
    lower = name.lower()
    for prefix in ('the ', 'a ', 'an '):
        if lower.startswith(prefix):
            return lower[len(prefix):]
    return lower


async def get_known_walkmans() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, serial, storage_type, model, marketing_name, last_scanned_at, track_count FROM walkman_devices ORDER BY last_scanned_at DESC NULLS LAST"
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "id": r[0], "serial": r[1], "storage_type": r[2],
            "model": r[3] or "WALKMAN", "marketing_name": r[4],
            "last_scanned_at": r[5], "track_count": r[6],
            "connected": False,  # caller sets this
        }
        for r in rows
    ]


async def delete_walkman_device(device_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM walkman_devices WHERE id=?", (device_id,))
        await db.commit()
