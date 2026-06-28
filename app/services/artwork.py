import base64
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def extract_artwork(ipod_path: str, mount: str) -> tuple[bytes, str] | None:
    """
    Extract embedded album art from an audio file on the iPod.
    Returns (image_bytes, mime_type) or None.
    Raises ValueError on path-traversal attempts.
    """
    mount_path = Path(mount).resolve()
    full = (mount_path / ipod_path.lstrip("/")).resolve()

    if not str(full).startswith(str(mount_path)):
        raise ValueError("Path outside mount point")

    if not full.exists():
        log.warning("artwork: file not found on device: %s", full)
        return None

    try:
        from mutagen import File
        audio = File(str(full), easy=False)
    except Exception as exc:
        log.warning("artwork: failed to open %s: %s", full, exc)
        return None

    if audio is None:
        log.warning("artwork: mutagen could not identify format: %s", full)
        return None

    if audio.tags is None:
        log.warning("artwork: no tags in %s", full)
        return None

    result = _flac(audio) or _mp4(audio.tags) or _id3(audio.tags) or _vorbis(audio.tags)
    if result is None:
        log.warning("artwork: no embedded art in %s", full)
    return result


def _flac(audio) -> tuple[bytes, str] | None:
    """Extract from native FLAC picture blocks (audio.pictures, not tags)."""
    pics = getattr(audio, "pictures", None)
    if not pics:
        return None
    pic = pics[0]
    return pic.data, pic.mime or "image/jpeg"


def _mp4(tags) -> tuple[bytes, str] | None:
    covers = tags.get("covr")
    if not covers:
        return None
    cover = covers[0]
    mime = "image/png" if getattr(cover, "imageformat", None) == 14 else "image/jpeg"
    return bytes(cover), mime


def _id3(tags) -> tuple[bytes, str] | None:
    for tag in tags.values():
        if getattr(tag, "FrameID", None) == "APIC":
            return tag.data, tag.mime or "image/jpeg"
    return None


def _vorbis(tags) -> tuple[bytes, str] | None:
    raw = tags.get("metadata_block_picture") or tags.get("METADATA_BLOCK_PICTURE")
    if not raw:
        return None
    try:
        from mutagen.flac import Picture
        pic = Picture(base64.b64decode(raw[0]))
        return pic.data, pic.mime or "image/jpeg"
    except Exception:
        return None
