import asyncio
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

IPOD_SENTINEL = "iPod_Control/iTunes/iTunesDB"
IPOD_CONTROL_DEVICE = "iPod_Control/Device"
SYSINFO_EXTENDED = "iPod_Control/Device/SysInfoExtended"


def is_ipod(mount: Path) -> bool:
    return (mount / IPOD_SENTINEL).exists()


def is_uninitialized_ipod(mount: Path) -> bool:
    """iPod directory skeleton present but no iTunesDB — a freshly formatted or
    disk-restored iPod. Never true for random USB sticks (no iPod_Control)."""
    return (Path(mount) / IPOD_CONTROL_DEVICE).is_dir() and not (Path(mount) / IPOD_SENTINEL).exists()


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


async def ensure_sysinfo_extended(devnode: str, mount: str | Path) -> str | None:
    """Write SysInfoExtended if missing, read off the device with libgpod's
    ipod-read-sysinfo-extended (SCSI inquiry on the whole disk).

    libgpod needs it to identify the model and compute the iTunesDB hash on
    Classic/Nano 3G+; iTunes writes it on connect, so restored or never-iTunes'd
    iPods lack it and gpod-* tools misbehave. Non-fatal: returns a status line
    when it acted, None when the file was already there or there is no disk.
    """
    path = Path(mount) / SYSINFO_EXTENDED
    if path.exists() or not devnode.startswith("/dev/"):
        return None
    disk = re.sub(r"p?\d+$", "", devnode)  # /dev/sdb2 → /dev/sdb
    cmd = ["ipod-read-sysinfo-extended", disk, str(mount)]
    log.info("SysInfoExtended missing at %s — exec: %s", mount, " ".join(cmd))
    try:
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except FileNotFoundError:
        msg = "ipod-read-sysinfo-extended not found (install libgpod-common)"
        log.warning(msg)
        return msg
    except Exception as exc:
        msg = f"ipod-read-sysinfo-extended failed: {exc}"
        log.warning(msg)
        return msg
    output = out.decode(errors="replace").strip()
    if proc.returncode != 0 or not path.exists():
        msg = f"ipod-read-sysinfo-extended {disk} exited {proc.returncode}: {output or 'no output'}"
        log.warning(msg)
        return msg
    msg = f"SysInfoExtended written from {disk}"
    log.info(msg)
    return msg
