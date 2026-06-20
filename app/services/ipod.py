import logging
from pathlib import Path

log = logging.getLogger(__name__)

IPOD_SENTINEL = "iPod_Control/iTunes/iTunesDB"


def is_ipod(mount: Path) -> bool:
    return (mount / IPOD_SENTINEL).exists()


def log_mount_contents(mount: Path) -> None:
    """Log the top two levels of a mount point to diagnose a missing iPod sentinel."""
    try:
        top = sorted(mount.iterdir(), key=lambda p: p.name.lower())
    except Exception as exc:
        log.warning("  Cannot list %s: %s", mount, exc)
        return

    if not top:
        log.warning("  Mount point is empty — iPod may not be mounted")
        return

    log.warning("  Contents of %s: %s", mount, [p.name for p in top])

    ipod_ctrl = next((p for p in top if p.name.lower() == "ipod_control"), None)
    if ipod_ctrl:
        log.warning("  Found control dir as '%s' (expected 'iPod_Control')", ipod_ctrl.name)
        try:
            sub = sorted(ipod_ctrl.iterdir(), key=lambda p: p.name.lower())
            log.warning("  Contents of %s: %s", ipod_ctrl, [p.name for p in sub])
            itunes_dir = next((p for p in sub if p.name.lower() == "itunes"), None)
            if itunes_dir:
                db = sorted(itunes_dir.iterdir(), key=lambda p: p.name.lower())
                log.warning("  Contents of %s: %s", itunes_dir, [p.name for p in db])
        except Exception as exc:
            log.warning("  Cannot list %s: %s", ipod_ctrl, exc)
    else:
        log.warning("  No 'iPod_Control' directory found (case-insensitive search also failed)")
