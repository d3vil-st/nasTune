# CLAUDE.md — iPod NAS Web UI

## Project overview

A self-hosted web UI for managing iPod Classic and iPod 5th generation devices from a headless NAS (Ubuntu-based, no display). Designed as a Docker container accessible via browser over the local network, superseding iTunes for NAS environments including Synology and similar boxes.

The backend is built around [gpod-utils](https://github.com/d3vil-st/gpod-utils), a CLI toolkit that wraps libgpod for reading and writing the iPod's proprietary iTunesDB format.

---

## Goals

- Allow a user with only SSH or web browser access to a NAS to fully manage their iPod library
- Discover connected iPods automatically via USB/udev without any manual configuration
- Browse, add, and remove music on the iPod through a clean browser UI
- Compare iPod contents against the NAS music library (via Beets or Plex) to identify what is missing or duplicated
- Keep the stack light, fast, and dependency-minimal — no Node.js, no frontend build toolchains

---

## Technology stack

| Layer | Choice | Rationale |
|---|---|---|
| HTTP framework | **FastAPI** (Python 3.12+) | Async, lightweight, ideal for subprocess streaming |
| ASGI server | **Uvicorn** | Minimal, production-ready |
| Templating | **Jinja2** | Server-side HTML, no JS build step |
| Frontend reactivity | **HTMX** | Attribute-driven partial page updates, SSE support |
| Minor client interactivity | **Alpine.js** (single CDN script tag) | Checkboxes, toggles, modals — no build step |
| iPod CLI backend | **gpod-utils** (`gpod-ls`, `gpod-cp`, `gpod-rm`) | Wraps libgpod, supports iPod Classic and iPod 5 |
| Device discovery | **udev + lsblk** via `asyncio.subprocess` | Detect and identify connected iPods |
| Music library index | **Beets** (`beet ls --format json`) or **Plex API** | NAS library metadata without re-indexing |
| Container runtime | **Docker** with `/dev` mounted and `SYS_ADMIN` cap | Required for USB access and mount syscalls |

Do **not** introduce Node.js, npm, webpack, or any frontend build pipeline. All JS must be delivered via CDN script tags or be inline.

---

## Repository structure

```
ipod-nas-webui/
├── CLAUDE.md                  # This file
├── Dockerfile
├── docker-compose.yml
├── app/
│   ├── main.py                # FastAPI app entrypoint
│   ├── routers/
│   │   ├── devices.py         # iPod discovery and mount endpoints
│   │   ├── library.py         # NAS music library browsing
│   │   ├── ipod.py            # iPod content read/write (gpod-ls, gpod-cp, gpod-rm)
│   │   └── sync.py            # Diff + sync orchestration
│   ├── services/
│   │   ├── gpod.py            # Async wrappers around gpod-utils CLI
│   │   ├── udev.py            # USB device detection
│   │   ├── mounter.py         # Mount/unmount iPod block devices
│   │   └── beets.py           # Beets/Plex library indexing
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html         # Dashboard: connected devices
│   │   ├── ipod.html          # iPod content browser
│   │   ├── library.html       # NAS library browser
│   │   └── sync.html          # Diff view + sync controls
│   └── static/
│       └── style.css          # Minimal custom CSS only
├── tests/
└── requirements.txt
```

---

## Docker setup

```yaml
# docker-compose.yml
services:
  ipodweb:
    build: .
    privileged: true            # required for mount syscalls
    volumes:
      - /dev:/dev
      - /run/udev:/run/udev:ro
      - /mnt/music:/music:ro   # NAS music share, read-only
      - /tmp/ipod:/mnt/ipod    # iPod mount point inside container
    ports:
      - "8080:8080"
    devices:
      - /dev/bus/usb
    restart: unless-stopped
```

The container image must include:
- `gpod-utils` (built from source or pre-packaged)
- `libgpod` and its dependencies
- `udev` tools (`udevadm`, `lsblk`, `blkid`)
- `mount` / `umount`
- `ffmpeg` (optional, for transcoding to AAC/ALAC if needed)
- Python 3.12+ with dependencies from `requirements.txt`

---

## Feature requirements

### 1. Device discovery

- Poll for connected block devices on a background asyncio task (every 3–5 seconds)
- Identify iPod devices by checking for the presence of `/iPod_Control/iTunes/iTunesDB` on the mounted filesystem
- Show device name, model (parsed from gpod-ls output), storage used/total
- Support multiple simultaneously connected iPods
- Expose a "Mount" / "Unmount" button per device
- Push device state changes to the browser via **Server-Sent Events (SSE)**

### 2. iPod content browser

- Invoke `gpod-ls --json` and parse the output
- Display a hierarchy: Artist → Album → Tracks
- For each track show: title, artist, album, duration, format (MP3/AAC/ALAC), bitrate, sample rate
- Display album art thumbnails where available
- Support sorting and filtering by artist / album / format
- Selection via checkboxes (individual tracks, whole albums, whole artists)

### 3. NAS library browser

- Index the bound `/music` directory using Beets (`beet ls --format json`) or by walking the filesystem with `mutagen` for tag reading
- Display the same Artist → Album → Track hierarchy
- Highlight tracks/albums that are **already on the iPod** vs **not yet synced**
- Support browsing by folder path as a fallback if Beets is not available

### 4. Diff / sync view

- Side-by-side or unified view: NAS library vs iPod contents
- Match tracks by filename, tags, or acoustic fingerprint (if Chromaprint/fpcalc available)
- Show three states per track: ✓ on iPod, + missing from iPod, − only on iPod (not on NAS)
- User selects what to copy (`gpod-cp`) or delete (`gpod-rm`)
- Confirm before destructive operations

### 5. Copy to iPod (`gpod-cp`)

- Run `gpod-cp` as an async subprocess
- Stream stdout/stderr back to the browser via SSE
- Show per-file progress: filename, bytes transferred, estimated time
- Abort button that sends SIGTERM to the subprocess

### 6. Delete from iPod (`gpod-rm`)

- Run `gpod-rm` for selected tracks
- Require explicit confirmation dialog before execution
- Show result per track (success / error)

### 7. Safe unmount

- Flush writes (`sync`) before unmounting
- Run `umount` on the iPod mount point
- Notify the user when safe to disconnect

---

## gpod-utils CLI reference

All interaction with the iPod goes through these three commands:

```bash
# List all tracks on iPod as JSON
gpod-ls --json /mnt/ipod

# Copy files to iPod
gpod-cp /mnt/ipod /music/Artist/Album/track.mp3

# Remove a track from iPod (by persistent ID or path)
gpod-rm /mnt/ipod <track-id>
```

Always check gpod-utils documentation for exact flags — the `--json` flag and argument order should be verified against the installed version.

---

## Async subprocess pattern

Long-running gpod operations must stream output back to the client. Use this pattern:

```python
async def run_streaming(cmd: list[str]):
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    async for line in process.stdout:
        yield line.decode()
    await process.wait()
    return process.returncode
```

Expose streaming endpoints as SSE using FastAPI's `StreamingResponse` with `media_type="text/event-stream"`.

---

## Frontend conventions

- Use **HTMX** for all dynamic updates: `hx-get`, `hx-post`, `hx-swap`, `hx-trigger`
- Use `hx-ext="sse"` for progress streaming (gpod-cp output)
- Use **Alpine.js** (`x-data`, `x-show`, `x-on`) only for purely client-side UI state (modal open/close, checkbox select-all)
- All pages are full server-rendered HTML — no JSON API needed for the UI layer (JSON endpoints are acceptable for internal use)
- Keep CSS minimal; use a single `style.css`. A small utility CSS framework (e.g. PicoCSS or Milligram, delivered via CDN) is acceptable if it keeps custom CSS near zero
- No TypeScript, no React, no Vue, no bundler

---

## Non-goals

- No support for iPhone, iPad, or iPod Touch (different protocol entirely)
- No cloud sync or remote access (LAN only)
- No music playback in the browser
- No transcoding pipeline (users should pre-transcode; ffmpeg is a stretch goal)
- No user authentication (assume trusted LAN; can add basic auth later)
- No Rockbox support (Rockbox iPods are plain USB mass storage and can use rsync directly — a separate simpler tool)

---

## Known constraints and gotchas

- **libgpod hash requirement**: Writing to iPod Classic (6th gen+) and Nano 3G+ requires computing a device-specific cryptographic hash. Verify `gpod-utils` handles this — it should via libgpod's `itdb_device_write_sysinfo` support.
- **Mount permissions**: The container must run privileged or with `CAP_SYS_ADMIN` for mount syscalls. Document this prominently.
- **iTunesDB corruption**: Always take a backup of `/mnt/ipod/iPod_Control/iTunes/iTunesDB` before any write operation.
- **HFS+ vs FAT32**: iPod 5 may be formatted as HFS+. Ensure `hfsplus` kernel module or `hfsprogs` is available in the container if needed.
- **udev inside Docker**: `/run/udev` must be bind-mounted read-only from the host for `udevadm` to work. Alternatively, poll `lsblk -J` on a timer.
- **gpod-ls JSON output**: Verify the exact JSON schema produced by `gpod-ls --json` before building the parser. Field names may differ from documentation.

---

## Development setup (local, no Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run dev server with auto-reload
uvicorn app.main:app --reload --port 8080
```

For local development without a real iPod, create a mock directory structure at `/tmp/ipod-mock/iPod_Control/iTunes/` with a sample iTunesDB, and point gpod-ls at it.

---

## Open questions / future work

- **Beets vs filesystem walk**: If Beets is not installed, fall back to scanning `/music` with `mutagen`. Should this be auto-detected?
- **Acoustic fingerprinting**: fpcalc/Chromaprint for matching re-encoded tracks — worthwhile but optional for v1.
- **Playlist management**: Creating and editing playlists is a v2 feature.
- **Multi-user / auth**: Basic HTTP auth via nginx reverse proxy is the simplest path if needed.
- **Synology package**: Packaging as a Synology SPK would broaden the audience significantly.
