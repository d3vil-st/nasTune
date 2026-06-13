import os
from pathlib import Path

FS_LABELS: dict[str, str] = {
    "vfat": "FAT32",
    "msdos": "FAT16",
    "exfat": "exFAT",
    "hfsplus": "HFS+",
    "hfs": "HFS",
    "ntfs": "NTFS",
    "ext4": "ext4",
    "ext3": "ext3",
}


def fs_usage(mount: Path) -> tuple[int, int]:
    """Returns (total_bytes, used_bytes) from the actual filesystem."""
    if not mount.parts:
        return 0, 0
    try:
        st = os.statvfs(mount)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        return total, total - free
    except OSError:
        return 0, 0


def fs_type(mount: Path) -> str:
    """Reads /proc/mounts to find the filesystem type for the given mount point."""
    if not mount.parts:
        return ""
    mount_str = str(mount).rstrip("/")
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1].rstrip("/") == mount_str:
                    raw = parts[2].lower()
                    return FS_LABELS.get(raw, raw.upper())
    except OSError:
        pass
    return ""
