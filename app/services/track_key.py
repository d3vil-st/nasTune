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


def track_key(artist: str, album: str, track_nr: int | None, disc_nr: int | None, title: str) -> str:
    disc_prefix = f"{disc_nr}." if (disc_nr or 0) > 1 else ""
    track_part = str(track_nr) if (track_nr or 0) > 0 else _norm_str(title)
    return f"{_norm_str(artist)}|||{_norm_str(album)}|||{disc_prefix}{track_part}"
