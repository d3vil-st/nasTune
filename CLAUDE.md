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
| Frontend reactivity | **HTMX** | Reserved for future SSE streaming features (gpod-cp progress) |
| Client-side state | **Alpine.js** (single CDN script tag) | 3-pane browser state, player, modals, detail panel — no build step |
| iPod CLI backend | **gpod-utils** (`gpod-ls`, `gpod-cp`, `gpod-rm`) | Wraps libgpod, supports iPod Classic and iPod 5 |
| Album art extraction | **mutagen** | Pure-Python; reads ALAC/M4A `covr`, MP3 `APIC`, FLAC picture blocks |
| Audio codec detection | **mutagen** (`MP4.info.codec`) | Detects ALAC vs AAC before deciding whether to transcode |
| ALAC transcoding | **ffmpeg** (streaming, on-the-fly) | ALAC in M4A is unsupported by Firefox on Linux; transcoded to FLAC |
| Device discovery | **lsblk** via `asyncio.subprocess` | Poll every 3 s; detects mounted iPods; auto-mounts if `IPOD_AUTOMOUNT=1` |
| Filesystem info | **os.statvfs + /proc/mounts** | Real capacity (flash-mod safe) and FS type label |
| Music library index | **Beets** (`beet ls --format json`) or **Plex API** | NAS library metadata without re-indexing |
| Container runtime | **Docker** with `/dev` mounted and `SYS_ADMIN` cap | Required for USB access and mount syscalls |

Do **not** introduce Node.js, npm, webpack, or any frontend build pipeline. All JS must be delivered via CDN script tags or be inline.

---

## Repository structure

```
nasTune/
├── CLAUDE.md
├── Dockerfile                 # Ubuntu 26.04 base; installs gpod-utils deb + ffmpeg + Python venv
├── docker-compose.yml
├── requirements.txt           # fastapi, uvicorn[standard], jinja2, python-multipart, mutagen
└── app/
    ├── main.py                # FastAPI app + all HTTP endpoints
    ├── services/
    │   ├── devices.py         # DeviceService: lsblk polling, mount/unmount, library cache, eject
    │   ├── gpod.py            # Runs gpod-ls, parses JSON → nested artist/album/track dicts
    │   ├── artwork.py         # mutagen-based artwork extractor (M4A/MP3/FLAC)
    │   └── fs_utils.py        # os.statvfs capacity + /proc/mounts FS-type label
    └── templates/
        └── index.html         # iTunes-like 3-pane dark UI + bottom player bar (Alpine.js)
```

---

## Docker setup

The Dockerfile uses `ubuntu:26.04` (matches the gpod-utils `.deb` target). It installs the pre-built `gpod-utils_1.4.4.ubuntu26.04_amd64.deb` from GitHub releases plus `ffmpeg` for ALAC transcoding. A Python venv is created at `/opt/venv`.

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

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `IPOD_MOUNT_POINT` | _(unset)_ | Register a pre-mounted iPod directory as a manual device. The poll loop still runs alongside it. |
| `IPOD_MOUNT_BASE` | `/mnt/ipods` | Base directory where auto-discovered devices are mounted (subdirs per devname). |
| `IPOD_AUTOMOUNT` | `0` | Set to `1`/`true`/`yes` to enable automatic mounting of USB block devices found by lsblk. Disabled by default — devices already mounted by the host are always detected regardless. |

---

## HTTP API

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Renders the main HTML UI (Jinja2, no data embedded) |
| `GET` | `/devices` | Returns all known devices and the currently selected devnode |
| `POST` | `/devices/select` | `{ devnode }` — select an iPod; triggers library load if not cached |
| `POST` | `/devices/eject` | `{ devnode }` — sync + umount + remove; returns 409 if device is busy |
| `GET` | `/devices/events` | SSE stream; pushes device state on connect and on any change |
| `GET` | `/library` | Returns cached library for the selected device (loads if needed) |
| `POST` | `/library/refresh` | Clears cache and re-runs `gpod-ls` for the selected device |
| `GET` | `/artwork?path=&devnode=` | Extracts embedded album art via mutagen; cached 24 h |
| `GET` | `/audio?path=&devnode=` | Serves an audio file from the iPod mount; ALAC M4A is transcoded to FLAC on-the-fly via ffmpeg |

---

## Feature requirements

### 1. Device discovery

- Poll `lsblk -J -b` every 3 s in a background asyncio task
- Identify iPod devices by checking for the sentinel `iPod_Control/iTunes/iTunesDB`
- Show device name, model (from gpod-ls), storage used/total, filesystem type
- Support multiple simultaneously connected iPods
- Auto-selects the first iPod found; supports manual selection in the UI
- Eject button: checks for active streams/library-load before unmounting
- Push device state changes to the browser via **Server-Sent Events (SSE)**

### 2. iPod content browser

- Invoke `gpod-ls` (sets `IPOD_MOUNT_POINT` env) and parse JSON output
- Display a hierarchy: Artist → Album → Tracks
- For each track show: title, artist, album, duration, format (MP3/AAC/ALAC), bitrate, sample rate
- Display album art thumbnails where available (via `/artwork`)
- Support filtering by artist / album / format via search bar
- Track rows where the actual file is missing from the iPod filesystem are marked MISSING

### 3. Audio playback

- Play button on track row hover (`▶` replaces track number)
- Play button overlay on album artwork hover (also opens the album)
- Bottom player bar appears when playback starts; dismissed with stop (`■`) button
- Player controls: shuffle (`⇄`), previous (`⏮`), play/pause, next (`⏭`), repeat (off/all/one), stop
- Queue = all non-missing tracks in the current album; navigation in the 3-pane browser does not interrupt playback
- Playback via HTML5 `<audio>` element; ALAC files are served transcoded to FLAC
- Duration falls back to the iPod DB value (`tracklen`) for streaming/ALAC tracks where the browser reports `Infinity`

### 4. NAS library browser _(planned)_

- Index the bound `/music` directory using Beets (`beet ls --format json`) or by walking the filesystem with `mutagen` for tag reading
- Display the same Artist → Album → Track hierarchy
- Highlight tracks/albums that are **already on the iPod** vs **not yet synced**
- Support browsing by folder path as a fallback if Beets is not available

### 5. Diff / sync view _(planned)_

- Side-by-side or unified view: NAS library vs iPod contents
- Match tracks by filename, tags, or acoustic fingerprint (if Chromaprint/fpcalc available)
- Show three states per track: ✓ on iPod, + missing from iPod, − only on iPod (not on NAS)
- User selects what to copy (`gpod-cp`) or delete (`gpod-rm`)
- Confirm before destructive operations

### 6. Copy to iPod (`gpod-cp`) _(planned)_

- Run `gpod-cp` as an async subprocess
- Stream stdout/stderr back to the browser via SSE
- Show per-file progress: filename, bytes transferred, estimated time
- Abort button that sends SIGTERM to the subprocess

### 7. Delete from iPod (`gpod-rm`) _(planned)_

- Run `gpod-rm` for selected tracks
- Require explicit confirmation dialog before execution
- Show result per track (success / error)

### 8. Safe unmount / eject

- Eject button visible in header whenever a device is selected
- Backend checks `is_busy()`: refuses with 409 if any ffmpeg stream or gpod-ls is active
- Runs `sync` before `umount` for auto-discovered (non-manual) devices
- Frontend stops playback automatically before sending the eject request
- SSE update notifies all clients when the device is removed

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

- All library data is fetched via `fetch('/library')` on page load / device select — **not embedded in the Jinja2 context** (saves RAM for large libraries)
- Alpine.js manages all UI state: device picker, 3-pane browser, player, modals
- HTMX is reserved for future streaming features (gpod-cp progress via SSE)
- All pages are server-rendered HTML (Jinja2); the `tojson` filter must be registered manually on `templates.env.filters` — Starlette 1.3+ does not include it by default
- **Starlette 1.3+ API change**: `TemplateResponse` signature is `(request, name, context)` — the `request` is the first positional arg, not inside the context dict
- Keep CSS in `<style>` blocks inside the template; extract to `app/static/style.css` when the file grows unwieldy
- No TypeScript, no React, no Vue, no bundler

---

## Known constraints and gotchas

- **libgpod hash requirement**: Writing to iPod Classic (6th gen+) and Nano 3G+ requires computing a device-specific cryptographic hash. Verify `gpod-utils` handles this — it should via libgpod's `itdb_device_write_sysinfo` support.
- **Mount permissions**: The container must run privileged or with `CAP_SYS_ADMIN` for mount syscalls. Document this prominently.
- **iTunesDB corruption**: Always take a backup of `/mnt/ipod/iPod_Control/iTunes/iTunesDB` before any write operation.
- **HFS+ vs FAT32**: iPod 5 may be formatted as HFS+. Ensure `hfsplus` kernel module or `hfsprogs` is available in the container if needed.
- **udev inside Docker**: `/run/udev` must be bind-mounted read-only from the host for `udevadm` to work. Alternatively, poll `lsblk -J` on a timer (current approach).
- **gpod-ls JSON output**: Verify the exact JSON schema produced by `gpod-ls --json` before building the parser. Field names may differ from documentation.
- **ALAC playback**: Firefox on Linux cannot decode ALAC in M4A containers. The `/audio` endpoint detects ALAC via `mutagen.MP4.info.codec` and pipes through `ffmpeg -f flac -` on the fly. Seeking is disabled for streamed ALAC; duration is taken from the iPod DB as a fallback.
- **IPOD_AUTOMOUNT disabled by default**: The poll loop detects already-mounted iPods but will not call `mount` itself unless `IPOD_AUTOMOUNT=1`. This avoids unexpected mounts on systems where the host OS handles mounting.

---

## Development setup (local, no Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run dev server with auto-reload
IPOD_MOUNT_POINT=/mnt/ipod uvicorn app.main:app --reload --port 8080
```

`gpod-ls` and `ffmpeg` must be on `PATH`. Install gpod-utils from the pre-built deb or build from source. Without a real iPod, the app shows an empty device list — that's expected.

---

## Open questions / future work

- **Beets vs filesystem walk**: If Beets is not installed, fall back to scanning `/music` with `mutagen`. Should this be auto-detected?
- **Acoustic fingerprinting**: fpcalc/Chromaprint for matching re-encoded tracks — worthwhile but optional for v1.
- **ALAC seeking**: Cache the ffmpeg-transcoded output to a temp file so Range requests work, enabling seek for ALAC tracks.
- **Playlist management**: Creating and editing playlists is a v2 feature.
- **Multi-user / auth**: Basic HTTP auth via nginx reverse proxy is the simplest path if needed.
- **Synology package**: Packaging as a Synology SPK would broaden the audience significantly.
