# CLAUDE.md — nasTune

## Project overview

A self-hosted web UI for managing iPod Classic, iPod 5th generation, and Sony WALKMAN devices from a headless NAS (Ubuntu-based, no display). Designed as a Docker container accessible via browser over the local network, superseding iTunes for NAS environments including Synology and similar boxes.

iPod support is built around [gpod-utils](https://github.com/d3vil-st/gpod-utils), a CLI toolkit that wraps libgpod for reading and writing the iPod's proprietary iTunesDB format. WALKMAN support detects devices by `default-capability.xml`, scans tags with mutagen into SQLite, and manages files with direct `shutil` operations.

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
| Client-side state | **Alpine.js** (vendored, `app/static/alpine.min.js`) | 3-pane browser state, player, modals, detail panel — no build step |
| iPod CLI backend | **gpod-utils** (`gpod-ls`, `gpod-cp`, `gpod-rm`) | Wraps libgpod, supports iPod Classic and iPod 5 |
| WALKMAN backend | **shutil** + **SQLite** | Direct file copy/delete; library indexed once and updated incrementally |
| Album art extraction | **mutagen** | Pure-Python; reads ALAC/M4A `covr`, MP3 `APIC`, FLAC picture blocks |
| Audio codec detection | **mutagen** (`MP4.info.codec`) | Detects ALAC vs AAC before deciding whether to transcode |
| ALAC transcoding | **ffmpeg** (streaming + tmpfs cache) | ALAC in M4A is unsupported by Firefox on Linux; transcoded to FLAC and cached on tmpfs for seekable playback |
| Device discovery | **lsblk** via `asyncio.subprocess` | Poll every 3 s; detects mounted iPods and WALKMANs; auto-mounts if `IPOD_AUTOMOUNT=1` |
| Filesystem info | **os.statvfs + /proc/mounts** | Real capacity (flash-mod safe) and FS type label |
| NAS library index | **mutagen** (async scanner) | Walks `/music`, reads tags, stores in SQLite; codec + bit-depth aware |
| Persistent storage | **SQLite + aiosqlite** | Source library + WALKMAN library at `DB_PATH` (default `/data/nastune.db`); WAL mode |
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
    │   ├── ipod.py            # /library/delete, /library/sync, /library/download, /operations
    │   ├── walkman.py         # /walkman/scan, /walkman/scan_status
    │   └── sources.py         # /sources/* — CRUD, scan, browse, library, audio, artwork
    ├── services/
    │   ├── devices.py         # DeviceService: lsblk polling, mount/unmount, library cache, eject
    │   ├── gpod.py            # Runs gpod-ls, parses JSON → nested artist/album/track dicts
    │   ├── walkman.py         # WALKMAN detection, SQLite scan, library build, delete/copy ops
    │   ├── artwork.py         # mutagen-based artwork extractor (M4A/MP3/FLAC)
    │   ├── fs_utils.py        # os.statvfs capacity + /proc/mounts FS-type label
    │   ├── db.py              # SQLite schema + migrations (sources, source_tracks, walkman_devices, walkman_tracks)
    │   ├── scanner.py         # Async file scanner: walks dirs, reads tags via mutagen
    │   └── operations.py      # OperationService: gpod-rm / gpod-cp / WALKMAN shutil with progress tracking
    ├── templates/
    │   └── index.html         # iTunes-like 3-pane dark UI + bottom player bar
    └── static/
        ├── style.css          # All CSS; CSS var token system, light/dark theme
        ├── utils.js           # Format helpers, gradients, _normStr/_trackKey, source format/quality, theme state
        ├── devices.js         # Device list, SSE, library fetch/refresh, eject
        ├── browser.js         # iPod 3-pane browser, artUrl, _buildIpodMap, isOnIpod
        ├── player.js          # Audio queue, play/pause/skip, iPod + source playback
        ├── sources.js         # Source CRUD, scan polling, folder browser, _buildCopyPaths
        ├── selection.js       # Checkboxes, select-all, delete/sync/download ops, storage bar
        └── app.js             # Assembles all modules via Object.defineProperties + init(); URL state
```

---

## Docker setup

The Dockerfile uses `ubuntu:26.04` (matches the gpod-utils `.deb` target). It installs the pre-built `gpod-utils_1.4.6.ubuntu26.04_amd64.deb` from GitHub releases plus `ffmpeg` for ALAC transcoding. A Python venv is created at `/opt/venv`.

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
| `POST` | `/library/sync` | `{ devnode, copy_paths[], delete_ids[], copy_track_count }` — delete then copy |
| `POST` | `/library/download` | `{ devnode, tracks[] }` — streams selected tracks as a `.tar` archive |
| `GET` | `/operations` | Current operation status: kind, status, processed, total, current, error, started_at |
| `GET` | `/operations/events` | SSE stream; pushes full op state on any change (250 ms server-side diff-poll) |
| `GET` | `/operations/history?devnode=` | Last 10 finished ops for the device, newest-first, full log included |

### WALKMAN operations (app/routers/walkman.py — prefix `/walkman`)

| Method | Path | Description |
|---|---|---|
| `POST` | `/walkman/scan?devnode=` | Trigger a background library scan (mutagen tag read into SQLite) |
| `GET` | `/walkman/scan_status?devnode=` | Live scan progress: status, processed, total, current_file, error |

WALKMAN delete and sync go through the same `/library/delete` and `/library/sync` endpoints as iPod — the router dispatches to `walkman.py` based on `device_info.is_walkman`. Operations use `shutil.copy2` / `os.remove` and update the SQLite library immediately on completion without a rescan.

#### Download track object schema
```json
{
  "ipod_path": ":iPod_Control:Music:F02:TGWN.mp3",
  "artist": "...", "albumartist": "...", "album": "...",
  "year": 1980, "track_nr": 3, "title": "..."
}
```
The archive restores the original directory structure:
`{albumartist}/{[year] - album}/{NN - title.ext}`

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

Scripts load synchronously (no `defer`) before Alpine's deferred `<script defer>` tag, so `window.app` is defined in time. Alpine.js 3.15.12 is vendored at `app/static/alpine.min.js` rather than loaded from unpkg.

### URL state persistence

Navigation state is encoded in the URL hash so the browser preserves it across reloads and the back button works:

- Library tab: `#tab=library&artist=Joy+Division&album=Closer`
- Sources tab: `#tab=sources&src=1&srca=Joy+Division&sral=Closer`

`app.js` reads the hash during `init()` and restores state after the library/source loads, with validation that the artist and album still exist. `$watch` on `viewMode`, `selectedArtist`, `selectedAlbum`, `selectedSourceId`, `srcArtist`, `srcAlbum` calls `_syncUrl()` on every navigation change. `history.replaceState` is used (no new history entries).

### Operation history and SSE

Operations are tracked in real time via SSE and persisted to disk so the status bar survives page reloads.

- **SSE**: `_connectOpEvents()` in `selection.js` opens an `EventSource` to `/operations/events`. The variable is closure-scoped (not stored in Alpine state) to avoid proxy issues with non-plain objects. On `running → done/error` transition, the library is refreshed and `loadOpHistory` is called.
- **Fast-op detection**: WALKMAN delete/sync completes in milliseconds — faster than the 250 ms SSE poll interval, so the frontend may never see `status: 'running'`. The handler stamps `connectedAt = Date.now() / 1000` when the `EventSource` opens and triggers refresh if `op.started_at >= connectedAt && op.status !== 'running'` (even when the previous op state was null). This guards against spuriously refreshing on pre-existing done ops seen at connect time.
- **History files**: each finished op is written to `/data/op_history/{device_id}/{timestamp}.json` and the directory is pruned to the last 10 files. `device_id` is the iPod UUID from the library cache; falls back to sanitized devnode if the library is not yet loaded.
- **`lastOp` getter**: in-session `currentOp` (when status is `done` or `error`) takes priority over `opHistory[0]` so the status bar shows the freshest result.
- **Op log modal**: clicking the running indicator or last-op entry in the status bar opens a terminal-style modal (`op-log-modal`). Live ops auto-scroll; historical ops are shown via `historyViewOp`.

### All Artists mode

`selectedArtist === '__ALL__'` (and `srcArtist === '__ALL__'`) is a sentinel that selects all artists at once.

- `currentAlbums` / `srcCurrentAlbums` return a flat list via `flatMap(a => a.albums)` when `__ALL__` is active. Search still filters this flat list by album name or track title.
- `pickArtist('__ALL__')` / `pickSrcArtist('__ALL__')` skip the first-album auto-select that happens for normal artist selection.
- Album-column checkboxes pass the **album object** directly to `isAlbumSelected(al)` / `toggleAlbum(al, checked)` (and source equivalents) instead of `(artistName, albumName)`. Passing the object is required because artist-name lookup fails when `artistName === '__ALL__'` — no library entry has that name.
- URL state encodes `__ALL__` literally and the restoration logic handles it before the normal `.find()` check.

### Search navigation

Search filters all three panes simultaneously. `onSearch()` clears the artist and album selection on each keystroke, so a search and a navigation selection cannot coexist — typing always resets navigation.

When a user then clicks an artist from the filtered list, both `search` and `selectedArtist` are set. The album getter resolves what to show based on *why* the artist appeared:

- **Artist name matched** — `a.name.toLowerCase().includes(q)` → show all albums unfiltered.
- **Album or track matched** — filter albums to those matching the query; filter tracks to matching titles.
- Track list is similarly unfiltered when the artist or album name matched the query, and filtered to matching titles otherwise.

A clear (×) button appears inside the search input (`.search-clear`) when the field is non-empty. Clicking calls `onSearch()` to reset navigation state.

### Sources bar filters

The sources bar (`.sources-bar`) contains quick-action buttons that are only shown when relevant:

- **Unsynced only** (`srcShowUnsynced`) — filters all three source panes to artists/albums/tracks not yet on the iPod, using `isOnIpod()`. Only visible when both iPod library and source library are loaded. Persisted in `localStorage` under key `nastune-src-unsynced`. Active state indicated by blue border (`.manage-btn.active`).

### Source presence highlighting (iPod/WALKMAN pane)

When a source is selected, items in the device pane that are **absent from the source** are highlighted in blue text via the `.not-in-src` CSS class (color `#4a9eff`), applied to `.artist-name`, `.album-name`, and `.t-title`.

`browser.js` maintains `_srcKeyMap` — a `Map<trackKey, true>` built from the source library using the same `_trackKey` formula as `_ipodMap`. It is built lazily on first use and cleared whenever `sourceLibrary` changes (source switch, source removed, source rescan completes). Three derived helpers:

- `isTrackInSrc(track, artistName, albumName)` — returns `true` if the track's key exists in `_srcKeyMap`; returns `true` when no source is selected
- `isAlbumInSrc(al, artistName)` — `true` if any track in the album is in the source
- `isArtistInSrc(artist)` — `true` if any album of the artist has at least one track in the source

The `:class` binding on artist/album/track rows adds `not-in-src` when the corresponding `isXxxInSrc` returns `false`.

### Sync confirmation dialog

The **Sync** button skips the confirmation modal when there are no deletes and enough free space:

- `syncNeedsConfirm` getter: `true` when `syncToDelete.length > 0 || syncSpaceWarning`
- `syncSpaceWarning` getter: `true` when `syncCopyBytes > freeBytes + syncDeleteBytes` (free space after accounting for the deletes that run first)
- When `syncNeedsConfirm` is false, the Sync button calls `confirmSync()` directly
- When `syncSpaceWarning` is true the dialog shows an orange warning and the confirm button becomes `.btn-danger` with label "Sync anyway"

### Storage bar

The storage bar shows three labels: `used` / `± net diff` / `free`. The net diff is a single signed number (green for net add, red for net reduction) replacing the previous four separate remove/add spans.

During an operation (`opRunning && (_opDeleteCount > 0 || _opCopyCount > 0)`), a `.storage-bar-delta` overlay (`position: absolute; inset: 0`) is shown on top of the bar displaying live `−N` (red) and `+N` (green) track counts. `_opDeleteCount` and `_opCopyCount` are incremented by the SSE op handler as tracks are processed and reset to 0 on op completion.

### Status bar right section

The status bar uses `display:flex`. The right section (scan progress, operation progress, last-op summary) is wrapped in `.statusbar-op` which has `width: min(480px, 55%)` — a CSS-declared width, not content-driven. This prevents layout reflow when the N/M counter or track name changes, which would otherwise cause the left-side text to jump. `overflow: hidden` clips content that doesn't fit. The N/M counter uses `font-variant-numeric: tabular-nums` for stable digit widths within the fixed box.

### Track matching (sync / isOnIpod)

Tracks are matched across iPod and source library by a normalized key:

```
_normStr(artist) + '|||' + _normStr(album) + '|||' + (disc_nr+'.' if disc_nr>1 else '') + (track_nr || _normStr(title))
```

`_normStr` NFD-decomposes, strips diacritics, lowercases, and collapses all non-alphanumeric characters to spaces. This handles common tag discrepancies (Unicode hyphens, curly quotes, accented characters).

`disc_nr` prefix (e.g. `2.`) is only added when `disc_nr > 1`. Tracks with `disc_nr` of 0, 1, or absent are treated identically, so single-disc albums match regardless of whether disc number is tagged.

The `_ipodMap` (`Map<key, track>`) is built lazily on first use and **always rebuilt** in `_initSrcChecked()` (not skipped when already cached) to avoid stale data after Alpine reactive re-renders during async library fetches. `_srcTrackMap` (`Map<id, track>`) is built on source library load for O(1) ID → track resolution.

### gpod-cp path collapsing (`_buildCopyPaths`)

`sources.js` exposes `_buildCopyPaths(tracks)` which compresses individual file paths into directory paths before sending to `gpod-cp`. At up to 3 ancestor levels (CD dir → album dir → artist dir), if every library track under a directory is in the selection, the directory path is used instead of individual files. `gpod-cp` accepts both file and directory arguments natively.

This means syncing a complete album sends one directory arg instead of N file args. Behavior is correct for flat albums (no CD subdirs), CD-organized albums, and mixed selections — partial albums fall back to individual file paths.

### Sync progress

The sync body includes `copy_track_count` (actual number of tracks to copy, not collapsed path count) so `op.total` reflects real track counts even when directories are collapsed. During `gpod-cp` execution, `_gpod_cp_batch` parses `[N/M]` streaming lines to update `op.processed` with per-track granularity and `op.current` with the track currently being copied (`Artist – Title`). A `proc_offset` parameter accumulates progress across multi-batch syncs.

### Light / dark theming

The app supports Auto / Light / Dark modes via a 3-segment pill switcher in the header. The selected preference is stored in `localStorage` under key `nastune-theme`.

- Theme state is managed by `themeMode`, `setTheme(mode)`, and `initTheme()` in `utils.js`.
- Visual switching is done by toggling the `html.light` class — no duplicate CSS, all components use CSS custom properties (`--surface-bg`, `--card-bg`, `--player-bg`, `--detail-bg`, `--fill-bg`, `--hover-surface`, `--chip-active`, `--badge-bg`, `--overlay-bg`, `--card-border`, `--surface-border`, `--icon-grad-from/to`).
- `html.light` is set immediately via an inline `<script>` in `<head>` (before the CSS `<link>`) to prevent flash of wrong theme (FOUC).
- `initTheme()` (called from `app.js`'s `init()`) attaches a `prefers-color-scheme` media-query listener so Auto mode reacts to OS-level theme changes without a page reload.
- Placeholder album art gradients (set as inline styles via Alpine's `:style` binding) are overridden in light mode via `html.light .album-thumb / .abar-art / .player-art / .detail-art { background: ... !important; }` CSS rules.

---

## gpod-utils CLI reference

All interaction with the iPod goes through these three commands. The `IPOD_MOUNT_POINT` environment variable is read natively by gpod-utils — no explicit path argument needed.

```bash
# List all tracks on iPod as JSON (IPOD_MOUNT_POINT must be set)
gpod-ls

# Copy files to iPod (accepts files or directories)
gpod-cp /music/Artist/Album/track.mp3
gpod-cp /music/Artist/Album/

# Remove tracks from iPod by persistent ID (accepts multiple IDs)
gpod-rm <id1> <id2> ...
```

Every invocation is logged at INFO level as `exec: IPOD_MOUNT_POINT=<mount> <cmd> <args>`. Set `GPOD_DRY_RUN=1` to skip execution while preserving logs.

`gpod-ls` JSON output schema: `ipod_data.device` (model, capacity, uuid) + `ipod_data.playlists.items[]` where `type == "master"` contains all tracks. Track fields include `id`, `ipod_path`, `title`, `artist`, `album`, `albumartist`, `filetype`, `bitrate`, `samplerate`, `tracklen` (ms), `track_nr`, `cd_nr`, `year`, `size`, `artwork` (bool), `rating` (0–100), `playcount`.

`gpod-rm` output: `[N/M]  :iPod_Control:...-path -> { id=X ... }` per track, summary `removed X/Y items`. IDs are positional — always delete in descending ID order across batches to avoid index shifts.

`gpod-cp` output: `[N/M]  /source/path -> { title='...' artist='...' ... }` per track, summary `X/M items (size)  dupl=D`. Duplicates (same audio content by checksum) count as success.

---

## Source library scanner

`scanner.py` walks directories with `asyncio.to_thread(_find_files)` + `asyncio.to_thread(_read_track)` (per file). Tags are read via `mutagen` with `easy=True`; M4A/AAC files are re-opened with `mutagen.mp4.MP4` to reliably extract `codec` and `bits_per_sample` (older mutagen may not expose these via the Easy API).

Progress is written to `sources.scan_processed / scan_total / scan_current_file` and polled by the frontend every 2 s. Files removed from disk since the last scan are deleted from the DB (tracked by `scanned_at` timestamp).

The DB schema includes `disc_nr INTEGER` on `source_tracks`. The library response includes `last_scanned_at` (Unix timestamp) which is appended as `?_v=` to artwork URLs to bust the browser cache after each rescan.

---

## WALKMAN scanner

`walkman.py` implements a self-contained async scanner triggered manually via the UI magnifying-glass button:

- **Detection**: `parse_capability(mount)` reads `default-capability.xml` at the mount root to extract model, serial, storage type (INTERNAL / CARD), and music path. Returns `None` for non-WALKMAN mounts.
- **DB tables**: `walkman_devices` (keyed by `serial + storage_type` to handle dual-LUN USB) and `walkman_tracks` (indexed by `device_id + path`).
- **Incremental scan**: existing DB rows are fetched first; only files not yet in the DB have tags read with mutagen. Stale DB rows (files removed from disk) are bulk-deleted. Progress is written per-file to `walkman_devices.scan_processed / scan_total / scan_current_file`.
- **Library format**: `fetch_library()` builds the same `artists[].albums[].tracks[]` nested dict as `gpod.py`, plus `walkman: True` and `walkman_db_id` flags used by the router to dispatch operations.
- **Operations**: delete enqueues `os.remove()` for each file; sync enqueues `shutil.copy2()` for each source file into the WALKMAN music directory. Both update the `walkman_tracks` table immediately after each file so the library is consistent without a rescan.

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

### Tar download streaming

`/library/download` uses an OS pipe + thread to stream tar content with no temp files:

```python
r_fd, w_fd = os.pipe()

def write_tar():
    with os.fdopen(w_fd, 'wb') as wf:
        with tarfile.open(fileobj=wf, mode='w|') as tar:
            for t in tracks:
                tar.add(disk_path, arcname=arcname)

thread = threading.Thread(target=write_tar, daemon=True)
thread.start()

with os.fdopen(r_fd, 'rb') as rf:
    while chunk := await asyncio.to_thread(rf.read, 65536):
        yield chunk
```

The `mode='w|'` flag enables streaming (no seeking). The OS pipe provides natural backpressure so the writer thread blocks when the client is slow. Downloads do not check `op_service.is_busy()` since they are read-only.

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
- **Mobile single-pane navigation**: On `≤768px` screens the three-column browser collapses to a single-pane slide view driven entirely by CSS `:has()` — no JS required. Selecting an artist/album slides in the next pane. Tapping the column heading navigates back (clears `selectedAlbum` / `selectedArtist`).
- **Light theme + FOUC**: The FOUC-prevention inline `<script>` runs before the CSS `<link>` in `<head>`. It reads `localStorage` and conditionally adds `html.light` before any paint. Do not move it after the stylesheet link.
- **Theme CSS variable tokens**: All surface/card/border colors are CSS variables. Adding a new component that needs theme awareness: use `var(--surface-bg)`, `var(--card-bg)`, `var(--fill-bg)`, `var(--surface-border)`, `var(--card-border)`, `var(--hover-surface)`, `var(--chip-active)`, `var(--overlay-bg)`, `var(--detail-bg)`, `var(--player-bg)` instead of hardcoded `rgba()` values. Override these in `html.light { }` if the dark default is wrong for light mode.
- **gpod-rm ID positional shift**: After each batch deletion, remaining track IDs shift down. Always sort delete IDs in descending order and process them highest-first so earlier deletions don't invalidate later IDs in the same operation.
- **gpod-cp directory args**: `gpod-cp` accepts both file paths and directory paths. Passing a directory copies all audio files under it recursively. `_buildCopyPaths` exploits this to collapse complete album/artist selections into a single directory argument.
- **WALKMAN dual-LUN**: some WALKMAN models expose INTERNAL storage and SD card as two separate USB mass-storage LUNs (two block devices). Each is treated as a separate device, keyed by `serial + storage_type` in `walkman_devices`.
- **WALKMAN NTFS/exFAT**: `devices.py` accepts `vfat`, `ntfs`, `exfat`, and `fuseblk` fstypes when detecting WALKMAN mounts. VFAT mounts get `utf8` / `iocharset=utf8` mount options to handle non-ASCII filenames correctly.
- **SSE connectedAt guard**: `_connectOpEvents()` stamps `connectedAt = Date.now() / 1000` when the EventSource opens. The `justFinished` condition uses `op.started_at >= connectedAt` instead of `prevStartedAt != null` — the latter fails (JS `undefined == null` is true) when no prior op exists in the session, causing WALKMAN fast ops to never trigger a library refresh.
- **`_srcKeyMap` invalidation**: `_srcKeyMap` must be set to `null` wherever `sourceLibrary` is reassigned (source switch, source removal, library reload). Missing an invalidation point causes stale source-presence highlighting in the device pane.
- **Sync confirmation bypass**: `syncNeedsConfirm` being `false` means the Sync button calls `confirmSync()` directly without opening the modal. The modal is still rendered in the DOM but only shown when `showSyncConfirm` is set to `true`. Do not add `showSyncConfirm = true` to the Sync button click handler unconditionally — it will break the fast-path.
- **Track library structure**: The nested library response (`artists[].albums[].tracks[]`) does not embed `album` or `albumartist` on individual track objects — those fields live at the parent level. Code that needs full track context (e.g. download, display) must walk the nested structure to obtain them.
- **`__ALL__` sentinel in album checkboxes**: `isAlbumSelected`, `isAlbumIndeterminate`, `toggleAlbum` (and source equivalents) accept an album object, not `(artistName, albumName)`. Do not change to string-based lookup — it breaks when `selectedArtist === '__ALL__'` because no library artist has that name.
- **`_normStr` empty fallback**: `_normStr` returns `norm || raw` so that symbol-only strings like `#####` (which normalize to `''`) don't collide with each other or with null/empty artist names.
- **Status bar `.statusbar-op` width**: declared as `width: min(480px, 55%)` (CSS value, not content-driven). Never change to `width: auto` or remove the declaration — the right section will grow/shrink with text and cause the left content to jump during N/M counter updates.

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
