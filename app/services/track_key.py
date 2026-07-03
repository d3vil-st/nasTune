import re
import unicodedata


def _norm_str(s: str) -> str:
    raw = str(s or '')
    norm = unicodedata.normalize('NFD', raw)
    norm = ''.join(c for c in norm if unicodedata.category(c) != 'Mn')
    norm = norm.lower()
    norm = re.sub(r'[^a-z0-9]', ' ', norm)
    norm = re.sub(r' +', ' ', norm).strip()
    return norm or raw  # symbol-only strings mustn't all collapse to the same empty key


norm_str = _norm_str  # public alias used by artwork_cache


def track_key(artist: str, album: str, track_nr: int | None, disc_nr: int | None, title: str) -> str:
    disc_prefix = f"{disc_nr}." if (disc_nr or 0) > 1 else ""
    if disc_prefix:
        # Multi-disc: use title because iPod/iTunes stores global track numbers
        # while file tags store per-disc numbers — they never agree.
        track_part = _norm_str(title)
    else:
        track_part = str(track_nr) if (track_nr or 0) > 0 else _norm_str(title)
    return f"{_norm_str(artist)}|||{_norm_str(album)}|||{disc_prefix}{track_part}"


def lib_track_key(track: dict, group_artist: str, album_name: str) -> str:
    """Key for a track object from a parsed device library (gpod.py _parse output).

    Podcasts key by show name (the album) as the artist component — the track's
    own artist tag holds the episode author and differs from the show grouping.
    Must stay in step with JS _buildDeviceMap / _buildSrcKeyMap in device.js.
    """
    if track.get('mediatype') == 'podcast':
        artist = album_name
    else:
        artist = track.get('artist') or group_artist
    return track_key(artist, album_name, track.get('track_nr'), track.get('disc_nr'), track.get('title', ''))
