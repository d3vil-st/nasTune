# CLAUDE.md — iPod NAS Web UI

## Project overview

A self-hosted web UI for managing iPod Classic and iPod 5th generation devices from a headless NAS (Ubuntu-based, no display). Designed as a Docker container accessible via browser over the local network, superseding iTunes for NAS environments including Synology and similar boxes.

The backend is built around [gpod-utils](https://github.com/d3vil-st/gpod-utils), a CLI toolkit that wraps libgpod for reading and writing the iPod's proprietary iTunesDB format.

---

## Goals

- Allow a user with only SSH or web browser access to a NAS to fully manage their iPod library
- Discover connected iPods automatically via USB/udev without any manual configuration
- Browse, add, and remove music on the iPod through a clean browser UI
- Compare iPod contents against the NAS music library to identify what is missing or duplicated
- Keep the stack light, fast, and dependency-minimal — no Node.js, no frontend build toolchains

---

## Technology stack

| Layer | Choice | Rationale |
|---|---|---|
| HTTP framework | **FastAPI** (Python 3.12+) | Async, lightweight, ideal for subprocess streaming |
| ASGI server | **Uvicorn** | Minimal, production-ready |
| Templating | **Jinja2** | Server-side HTML, no JS build step |
| Frontend reactivity | **HTMX** | Reserved for future SSE streaming features |
| Client-side state | **Alpine.js** (CDN script tag) | 3-pane browser state, player, modals, detail panel — no build step |
| iPod CLI backend | **gpod-utils** (`gpod-ls`, `gpod-cp`, `gpod-rm`) | Wraps libgpod, supports iPod Classic and iPod 5 |
| Album art extraction | **mutagen** | Pure-Python; reads ALAC/M4A `covr`, MP3 `APIC`, FLAC picture blocks |
| Audio codec detection | **mutagen** (`MP4.info.codec`) | Detects ALAC vs AAC before deciding whether to transcode |
| ALAC transcoding | **ffmpeg** (streaming, on-the-fly) | ALAC in M4A is unsupported by Firefox on Linux; transcoded to FLAC |
| Device discovery | **lsblk** via `asyncio.subprocess` | Poll every 3 s; detects mounted iPods; auto-mounts if `IPOD_AUTOMOUNT=1` |
| Filesystem info | **os.statvfs + /proc/mounts** | Real capacity (flash-mod safe) and FS type label |
| NAS library index | **mutagen** (async scanner) | Walks `/music`, reads tags, stores in SQLite; codec + bit-depth aware |
| Persistent storage | **SQLite + aiosqlite** | Source library index at `DB_PATH` (default `/data/nastune.db`); WAL mode |
| Container runtime | **Docker** with `/dev` mounted and `SYS_ADMIN` cap | Required for USB access and mount syscalls |

Do **not** introduce Node.js, npm, webpack, or any frontend build pipeline. All JS must be delivered via CDN script tags or served as plain static files.

---

## Repository structure

```
nasTune/
├── CLAUDE.md
├── Dockerfile                 # Ubuntu 26.04 base; installs gpod-utils deb + ffmpeg + Python venv
├── docker-compose.yml
├── requirements.txt           # fastapi, uvicorn[standard], jinja2, python-multipart, mutagen, aiosqlite
└── app/
    ├── main.py                # FastAPI app factory; mounts /static; core iPod endpoints
    ├── routers/
    │   ├── ipod.py            # /library/delete, /library/sync, /operations
    │   └── sources.py         # /sources/* — CRUD, scan, browse, library, audio, artwork
    ├── services/
    │   ├── devices.py         # DeviceService: lsblk polling, mount/unmount, library cache, eject
    │   ├── gpod.py            # Runs gpod-ls, parses JSON → nested artist/album/track dicts
    │   ├── artwork.py         # mutagen-based artwork extractor (M4A/MP3/FLAC)
    │   ├── fs_utils.py        # os.statvfs capacity + /proc/mounts FS-type label
    │   ├── db.py              # SQLite schema + migrations (sources + source_tracks tables)
    │   ├── scanner.py         # Async file scanner: walks dirs, reads tags via mutagen
    │   └── operations.py      # OperationService: gpod-rm / gpod-cp with progress tracking
    ├── templates/
    │   └── index.html         # iTunes-like 3-pane dark UI + bottom player bar (845 lines, HTML only)
    └── static/
        ├── style.css          # All CSS (~980 lines)
        ├── utils.js           # Format helpers, gradients, _normStr/_trackKey, source format/quality
        ├── devices.js         # Device list, SSE, library fetch/refresh, eject
        ├── browser.js         # iPod 3-pane browser, artUrl, _buildIpodMap, isOnIpod
        ├── player.js          # Audio queue, play/pause/skip, iPod + source playback
        ├── sources.js         # Source CRUD, scan polling, folder browser, _srcTrackById
        ├── selection.js       # Checkboxes, select-all, delete/sync ops, storage bar
        └── app.js             # Assembles all modules via Object.defineProperties + init()
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
- `./data:/data` — persistent SQLite database

`privileged: true` is required for mount syscalls inside the container.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `IPOD_MOUNT_POINT` | _(unset)_ | Register a pre-mounted iPod directory as a manual device. The poll loop still runs alongside it. |
| `IPOD_MOUNT_BASE` | `/mnt/ipods` | Base directory where auto-discovered devices are mounted (subdirs per devname). |
| `IPOD_AUTOMOUNT` | `0` | Set to `1`/`true`/`yes` to enable automatic mounting of USB block devices found by lsblk. Disabled by default — devices already mounted by the host are always detected regardless. |
| `DB_PATH` | `/data/nastune.db` | Path to the SQLite database file for source library index. |
| `GPOD_DRY_RUN` | `0` | Set to `1`/`true`/`yes` to log all `gpod-rm` and `gpod-cp` commands without executing them. `gpod-ls` always runs (read-only). |

---

## HTTP API

### Core (app/main.py)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Renders the main HTML UI |
| `GET` | `/devices` | All known devices + currently selected devnode |
| `POST` | `/devices/select` | `{ devnode }` — select an iPod; triggers library load if not cached |
| `POST` | `/devices/mount` | `{ devnode }` — mount an unmounted device |
| `POST` | `/devices/eject` | `{ devnode }` — sync + umount + remove; returns 409 if device is busy |
| `GET` | `/devices/events` | SSE stream; pushes device state on connect and on any change |
| `GET` | `/library` | Cached library for the selected device (loads if needed) |
| `POST` | `/library/refresh` | Clears cache and re-runs `gpod-ls` for the selected device |
| `GET` | `/artwork?path=&devnode=` | Extracts embedded album art via mutagen; cached 24 h |
| `GET` | `/audio?path=&devnode=` | Serves audio from iPod mount; ALAC M4A is transcoded to FLAC on-the-fly |

### iPod operations (app/routers/ipod.py)

| Method | Path | Description |
|---|---|---|
| `POST` | `/library/delete` | `{ devnode, track_ids[] }` — enqueues gpod-rm for each track ID |
| `POST` | `/library/sync` | `{ devnode, copy_paths[], delete_ids[] }` — delete then copy in CPU-count batches |
| `GET` | `/operations` | Current operation status: kind, status, processed, total, current, error |

### Sources (app/routers/sources.py — prefix `/sources`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/sources` | List all registered sources |
| `POST` | `/sources` | `{ name, path, type }` — add source and start scan |
| `DELETE` | `/sources/{id}` | Remove source and all its track data |
| `POST` | `/sources/{id}/scan` | Trigger a rescan |
| `GET` | `/sources/browse?path=` | Directory browser for adding sources |
| `GET` | `/sources/{id}/library` | Source library as artist → album → track hierarchy |
| `GET` | `/sources/audio?path=` | Serve audio file from source; ALAC → FLAC transcode |
| `GET` | `/sources/artwork?path=` | Extract artwork from source file via mutagen |

---

## Frontend architecture

The UI is a single-page app built from server-rendered HTML (Jinja2) with Alpine.js managing all client state. JS and CSS are served as plain static files from `app/static/`.

### Module assembly

`app.js` assembles all modules via `Object.defineProperties` (which preserves getter descriptors unlike `Object.assign`):

```js
function app() {
  const mods = [utilsModule(), devicesModule(), browserModule(),
                playerModule(), sourcesModule(), selectionModule()];
  const state = {};
  for (const mod of mods) {
    Object.defineProperties(state, Object.getOwnPropertyDescriptors(mod));
  }
  state.init = async function() { ... };
  return state;
}
```

Scripts load synchronously (no `defer`) before Alpine's deferred CDN tag, so `window.app` is defined in time.

### Track matching (sync / isOnIpod)

Tracks are matched across iPod and source library by a normalized key:

```
_normStr(artist) + '|||' + _normStr(album) + '|||' + (track_nr || _normStr(title))
```

`_normStr` collapses Unicode dashes (U+2010 etc.) → `-` and curly quotes → `'` before lowercasing. This handles common tag discrepancies (e.g. `Guns N' Roses` vs `Guns N' Roses`, `Static-X` vs `Static‐X`).

The `_ipodMap` (`Map<key, track>`) is built lazily on first use and invalidated on library refresh. `_srcTrackMap` (`Map<id, track>`) is built on source library load for O(1) ID → track resolution.

---

## gpod-utils CLI reference

All interaction with the iPod goes through these three commands. The `IPOD_MOUNT_POINT` environment variable is read natively by gpod-utils — no explicit path argument needed.

```bash
# List all tracks on iPod as JSON (IPOD_MOUNT_POINT must be set)
gpod-ls

# Copy files to iPod
gpod-cp /music/Artist/Album/track.mp3

# Remove a track from iPod (by persistent ID)
gpod-rm <track-id>
```

Every invocation is logged at INFO level as `exec: IPOD_MOUNT_POINT=<mount> <cmd> <args>`. Set `GPOD_DRY_RUN=1` to skip execution while preserving logs.

`gpod-ls` JSON output schema: `ipod_data.device` (model, capacity, uuid) + `ipod_data.playlists.items[]` where `type == "master"` contains all tracks. Track fields include `id`, `ipod_path`, `title`, `artist`, `album`, `albumartist`, `filetype`, `bitrate`, `samplerate`, `tracklen` (ms), `track_nr`, `year`, `size`, `artwork` (bool), `rating` (0–100), `playcount`.

---

## Source library scanner

`scanner.py` walks directories with `asyncio.to_thread(_find_files)` + `asyncio.to_thread(_read_track)` (per file). Tags are read via `mutagen` with `easy=True`; M4A/AAC files are re-opened with `mutagen.mp4.MP4` to reliably extract `codec` and `bits_per_sample` (older mutagen may not expose these via the Easy API).

Progress is written to `sources.scan_processed / scan_total / scan_current_file` and polled by the frontend every 2 s. Files removed from disk since the last scan are deleted from the DB (tracked by `scanned_at` timestamp).

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

## Known constraints and gotchas

- **libgpod hash requirement**: Writing to iPod Classic (6th gen+) and Nano 3G+ requires computing a device-specific cryptographic hash. Handled via libgpod's `itdb_device_write_sysinfo` support inside gpod-utils.
- **Mount permissions**: The container must run privileged or with `CAP_SYS_ADMIN` for mount syscalls.
- **iTunesDB corruption**: Always take a backup of `/mnt/ipod/iPod_Control/iTunes/iTunesDB` before any write operation. Use `GPOD_DRY_RUN=1` when testing sync logic.
- **HFS+ vs FAT32**: iPod 5 may be formatted as HFS+. Ensure `hfsplus` kernel module or `hfsprogs` is available in the container if needed.
- **udev inside Docker**: `/run/udev` must be bind-mounted read-only from the host. Alternatively, poll `lsblk -J` on a timer (current approach).
- **ALAC playback**: Firefox on Linux cannot decode ALAC in M4A containers. The `/audio` and `/sources/audio` endpoints detect ALAC via `mutagen.MP4.info.codec` and pipe through `ffmpeg -f flac -` on the fly. Seeking is disabled for streamed ALAC; duration falls back to the DB value.
- **IPOD_AUTOMOUNT disabled by default**: The poll loop detects already-mounted iPods but will not call `mount` itself unless `IPOD_AUTOMOUNT=1`.
- **Alpine.js x-show + inline flex**: Alpine's `x-show` sets `el.style.display = ''` when restoring visibility, which wipes any inline `display:flex`. Always use a CSS class for flex containers that are toggled with `x-show`.
- **Object.defineProperties for getters**: Alpine.js getters in module objects must be merged with `Object.defineProperties`, not `Object.assign` — the latter evaluates getters immediately and stores the result as a plain value.
- **Unicode tag normalization**: Source file tags often use Unicode hyphens (U+2010) or curly apostrophes (U+2019) where the iPod DB stores ASCII. `_normStr()` in `utils.js` normalizes both before key comparison.

---

## Development setup (local, no Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run dev server with auto-reload
IPOD_MOUNT_POINT=/mnt/ipod uvicorn app.main:app --reload --port 8080
```

`gpod-ls` and `ffmpeg` must be on `PATH`. Install gpod-utils from the pre-built deb or build from source. Without a real iPod, the app shows an empty device list — that's expected. The source library scanner works without an iPod.

---

## Open questions / future work

- **ALAC seeking**: Cache the ffmpeg-transcoded output to a temp file so Range requests work, enabling seek for ALAC tracks.
- **Playlist management**: Creating and editing playlists is a v2 feature.
- **Multi-user / auth**: Basic HTTP auth via nginx reverse proxy is the simplest path if needed.
- **Synology package**: Packaging as a Synology SPK would broaden the audience significantly.
- **Acoustic fingerprinting**: fpcalc/Chromaprint for matching re-encoded tracks — optional, useful when track numbers are absent.
