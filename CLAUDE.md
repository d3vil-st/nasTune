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
| Client-side state | **Alpine.js** (single CDN script tag) | 3-pane browser state, modals, detail panel — no build step |
| iPod CLI backend | **gpod-utils** (`gpod-ls`, `gpod-cp`, `gpod-rm`) | Wraps libgpod, supports iPod Classic and iPod 5 |
| Album art extraction | **mutagen** | Pure-Python; reads ALAC/M4A `covr`, MP3 `APIC`, FLAC picture blocks |
| Device discovery | **udev + lsblk** via `asyncio.subprocess` | Detect and identify connected iPods |
| Music library index | **Beets** (`beet ls --format json`) or **Plex API** | NAS library metadata without re-indexing |
| Container runtime | **Docker** with `/dev` mounted and `SYS_ADMIN` cap | Required for USB access and mount syscalls |

Do **not** introduce Node.js, npm, webpack, or any frontend build pipeline. All JS must be delivered via CDN script tags or be inline.

---

## Repository structure

```
nasTune/
├── CLAUDE.md
├── Dockerfile                 # Ubuntu 26.04 base; installs gpod-utils deb + Python venv
├── docker-compose.yml
├── requirements.txt           # fastapi, uvicorn[standard], jinja2, python-multipart, mutagen
├── app/
│   ├── main.py                # FastAPI app; GET / (browser), GET /artwork (art extraction)
│   └── services/
│       ├── gpod.py            # Runs gpod-ls, parses JSON → nested artist/album/track dicts
│       └── artwork.py         # mutagen-based artwork extractor (M4A/MP3/FLAC)
└── app/templates/
    └── index.html             # Full iTunes-like 3-pane dark UI (Alpine.js, inline CSS/JS)
```

---

## Docker setup

The Dockerfile uses `ubuntu:26.04` (matches the gpod-utils `.deb` target). It installs the pre-built `gpod-utils_1.4.4.ubuntu26.04_amd64.deb` from GitHub releases — no compilation needed. A Python venv is created at `/opt/venv`.

```bash
docker compose up --build   # build + start on port 127.0.0.1:8080
```

Key docker-compose volumes:
- `/dev:/dev` + `/dev/bus/usb` — USB device access
- `/run/udev:/run/udev:ro` — udev event detection
- `./ipod:/mnt/ipod` — iPod mount point (host directory shared with container)
- `/mnt/music:/music:ro` — NAS music share (read-only)

`privileged: true` is required for mount syscalls inside the container.

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

All interaction with the iPod goes through these three commands. The `IPOD_MOUNT_POINT` environment variable is read natively by gpod-utils — no explicit path argument needed.

```bash
# List all tracks on iPod as JSON (IPOD_MOUNT_POINT must be set)
gpod-ls

# Copy files to iPod
gpod-cp /music/Artist/Album/track.mp3

# Remove a track from iPod (by persistent ID or path)
gpod-rm <track-id>
```

`gpod-ls` JSON output schema: `ipod_data.device` (model, capacity, uuid) + `ipod_data.playlists.items[]` where `type == "master"` contains all tracks. Track fields include `id`, `ipod_path`, `title`, `artist`, `album`, `albumartist`, `filetype`, `bitrate`, `samplerate`, `tracklen` (ms), `track_nr`, `year`, `size`, `artwork` (bool), `rating` (0–100), `playcount`.

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

- The current PoC uses **Alpine.js** as the primary interactivity layer — the full library JSON is embedded in the page on first load and all filtering/navigation is client-side. HTMX is reserved for future streaming features (gpod-cp progress via SSE).
- Use **HTMX** for partial page updates and SSE streaming when implementing write operations
- All pages are server-rendered HTML (Jinja2); the `tojson` filter must be registered manually on `templates.env.filters` — Starlette 1.3+ does not include it by default
- **Starlette 1.3+ API change**: `TemplateResponse` signature is `(request, name, context)` — the `request` is the first positional arg, not inside the context dict
- Keep CSS in `<style>` blocks inside the template (PoC); extract to `app/static/style.css` when the file grows unwieldy
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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run dev server with auto-reload
IPOD_MOUNT_POINT=/mnt/ipod uvicorn app.main:app --reload --port 8080
```

`gpod-ls` must be on `PATH`. Install from the pre-built deb or build from source. Without a real iPod, the app renders an error banner showing the mount point — that's expected.

---

## Open questions / future work

- **Beets vs filesystem walk**: If Beets is not installed, fall back to scanning `/music` with `mutagen`. Should this be auto-detected?
- **Acoustic fingerprinting**: fpcalc/Chromaprint for matching re-encoded tracks — worthwhile but optional for v1.
- **Playlist management**: Creating and editing playlists is a v2 feature.
- **Multi-user / auth**: Basic HTTP auth via nginx reverse proxy is the simplest path if needed.
- **Synology package**: Packaging as a Synology SPK would broaden the audience significantly.
