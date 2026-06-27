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
    │   ├── device.py          # /library/delete, /library/sync, /library/download, /operations
    │   ├── walkman.py         # /walkman/scan, /walkman/scan_status
    │   └── sources.py         # /sources/* — CRUD, scan, browse, library, audio, artwork
    ├── services/
    │   ├── devices.py         # DeviceService: lsblk polling, mount/unmount, library cache, eject
    │   ├── ipod.py            # iPod detection: IPOD_SENTINEL, is_ipod(), log_mount_contents()
    │   ├── ipod_db.py         # Persistent iPod device records, per-device settings, sync rules, auto-sync path computation
    │   ├── gpod.py            # Runs gpod-ls, parses JSON → nested artist/album/track dicts; _classify_mediatype bitmask
    │   ├── walkman.py         # WALKMAN detection, SQLite scan, library build, delete/copy ops
    │   ├── artwork.py         # mutagen-based artwork extractor (M4A/MP3/FLAC)
    │   ├── fs_utils.py        # os.statvfs capacity + /proc/mounts FS-type label
    │   ├── db.py              # SQLite schema + migrations (sources, source_tracks, walkman_devices, walkman_tracks, ipod_devices, ipod_track_ratings, ipod_device_settings, ipod_sync_rules, walkman_device_settings, walkman_sync_rules, settings)
    │   ├── scanner.py         # Async file scanner: walks dirs, reads tags via mutagen; _remap_podcast for podcast sources
    │   ├── track_key.py       # Python port of JS _normStr/_trackKey; shared by ratings + operations
    │   ├── ratings.py         # persist_ratings(): upserts iPod ratings into ipod_track_ratings (max wins)
    │   └── operations.py      # OperationService: gpod-rm / gpod-cp / WALKMAN shutil; smart encoder selection; progress tracking
    ├── templates/
    │   └── index.html         # iTunes-like 3-pane dark UI + bottom player bar
    └── static/
        ├── style.css          # All CSS; CSS var token system, light/dark theme
        ├── utils.js           # Format helpers, gradients, _normStr/_trackKey, source format/quality, theme state
        ├── devices.js         # Device list, SSE, library fetch/refresh, eject; _connectedDeviceName
        ├── device.js          # Device 3-pane browser, artUrl, _buildDeviceMap, isOnDevice; devLabels getter
        ├── player.js          # Audio queue, play/pause/skip, iPod + source playback
        ├── sources.js         # Source CRUD, scan polling, folder browser, _buildCopyPaths; media type selector; srcLabels
        ├── selection.js       # Checkboxes, select-all, delete/sync/download ops, storage bar
        ├── settings.js        # Global settings + device settings modal; auto-sync trigger; openDeviceSettings / openDeviceSettingsForKnown
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
| `BUILD_VERSION` | `dev` | Version string shown in the UI header. Set automatically by the Docker build via `ARG BUILD_VERSION`; the CI workflow computes it with `git describe --tags --always --dirty=-dirty`. |

---

## HTTP API

### Core (app/main.py)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Renders the main HTML UI |
| `GET` | `/devices` | All known devices + currently selected devnode; includes `known_devices` with iPod and WALKMAN records |
| `POST` | `/devices/select` | `{ devnode }` — select an iPod; triggers library load if not cached |
| `POST` | `/devices/mount` | `{ devnode }` — mount an unmounted device |
| `POST` | `/devices/eject` | `{ devnode }` — sync + umount + remove; returns 409 if device is busy |
| `GET` | `/devices/events` | SSE stream; pushes device state on connect and on any change |
| `GET` | `/library` | Cached library for the selected device (loads if needed) |
| `POST` | `/library/refresh` | Clears cache and re-runs `gpod-ls` for the selected device |
| `GET` | `/artwork?path=&devnode=` | Extracts embedded album art via mutagen; cached 24 h |
| `GET` | `/audio?path=&devnode=` | Serves audio from iPod mount; ALAC M4A is transcoded to FLAC on-the-fly |
| `GET` | `/device-settings?devnode=` | Per-device settings (force_aac override, sync rules); resolves via devnode or db_id/type |
| `POST` | `/device-settings` | `{ devnode?, db_id?, device_type?, force_aac, sync_rules[] }` — save settings |
| `POST` | `/auto-sync` | `{ devnode }` — run sync using per-device sync rules; returns 409 if busy or no rules |

### iPod/WALKMAN operations (app/routers/device.py)

| Method | Path | Description |
|---|---|---|
| `POST` | `/library/delete` | `{ devnode, track_ids[] }` — enqueues gpod-rm for each track ID |
| `POST` | `/library/sync` | `{ devnode, copy_paths[], delete_ids[], copy_track_count }` — delete then copy then rating sync |
| `POST` | `/library/rate` | `{ devnode, track_id, rating }` — set track rating (0–5); updates cache + ipod_track_ratings; gpod-tag applied on next sync |
| `POST` | `/library/download` | `{ devnode, tracks[] }` — streams selected tracks as a `.tar` archive |
| `GET` | `/operations` | Current operation status: kind, status, processed, total, current, error, started_at |
| `GET` | `/operations/events` | SSE stream; pushes full op state on any change (250 ms server-side diff-poll) |
| `GET` | `/operations/history?devnode=` | Last 10 finished ops for the device, newest-first, full log included |

### WALKMAN operations (app/routers/walkman.py — prefix `/walkman`)

| Method | Path | Description |
|---|---|---|
| `POST` | `/walkman/scan?devnode=&full=` | Trigger a background library scan; `full=true` clears all tracks first (full rescan) |
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
| `POST` | `/sources/{id}/scan?full=` | Trigger a rescan; `full=true` clears all tracks first (full rescan) |
| `GET` | `/sources/browse?path=` | Directory browser for adding sources |
| `GET` | `/sources/{id}/library` | Source library as artist → album → track hierarchy |
| `GET` | `/sources/audio?path=` | Serve audio file from source; ALAC → FLAC transcode |
| `GET` | `/sources/artwork?path=` | Extract artwork from source file via mutagen |
| `POST` | `/sources/rate` | `{ path, rating }` — set rating (0–5) for a source track by file path; updates ipod_track_ratings |

---

## Frontend architecture

The UI is a single-page app built from server-rendered HTML (Jinja2) with Alpine.js managing all client state. JS and CSS are served as plain static files from `app/static/`.

### Module assembly

`app.js` assembles all modules via `Object.defineProperties` (which preserves getter descriptors unlike `Object.assign`):

```js
function app() {
  const mods = [utilsModule(), devicesModule(), deviceModule(),
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

### Media type system

`mediaType` (stored in `localStorage` under key `nastune-media-type`) is a shared state value that gates both the device pane and source pane simultaneously. Valid values: `'music'`, `'podcast'`, `'audiobook'`.

- **Device pane** (`_typeFilteredLibrary`): filters tracks by `t.mediatype` — `'music'` accepts `null` or `'music'`; `'podcast'` and `'audiobook'` match exactly. Track `mediatype` field is set by `_classify_mediatype` in `gpod.py` (bitmask: AUDIO=1, VIDEO=2, PODCAST=4, AUDIOBOOK=8; value 5 = PODCAST|AUDIO which classifies as podcast).
- **Source pane** (`filteredSources`): filters the sources list to `s.type === mediaType`. Each source has a `type` column in the DB set at creation time.
- **Labels**: `devLabels` (in `device.js`) and `srcLabels` (in `sources.js`) return media-type-specific terminology — "Artists/Albums/Tracks" for music, "Shows/Seasons/Episodes" for podcasts, "Authors/Books/Chapters" for audiobooks.
- **Per-type source memory**: `setMediaType()` saves the current `selectedSourceId` to `localStorage` under key `nastune-src-{type}` before switching, and restores it on switch-back.
- **`filteredSrcArtists` guard**: returns `[]` when `selectedSourceObj` is null (no source of the current type selected), preventing stale content from a previous type's source from bleeding through.

### Device settings and auto-sync

`settings.js` contains both the global settings modal and the per-device settings modal. They are independent — the device settings modal uses `_deviceSettingsDevnode` (for connected devices) or `_deviceSettingsDbId + _deviceSettingsType` (for offline/known devices via the known-devices list).

- `openDeviceSettings(devnode)`: for unmounted devices, redirects to `openDeviceSettingsForKnown` using USB serial → iPod UUID match via `knownDevices.ipods`.
- `_loadDeviceSettings()`: calls `GET /devices/device-settings?devnode=` (connected) or `GET /devices/known/{id}/settings?device_type=` (offline). Note: devnode is a **query parameter** (not path) because devnode values like `/dev/sdb3` contain slashes that Starlette would decode and misroute if used as path segments.
- **Auto-sync** (`runAutoSync()`): sends `POST /auto-sync?devnode=` which calls `compute_auto_sync_paths` in `ipod_db.py` — walks per-device sync rules, collects source paths for tracks not already on the device, and passes them to the normal sync endpoint.
- **Per-device `force_aac`** override: takes precedence over the global `force_aac` setting from the `settings` table; stored in `ipod_device_settings` / `walkman_device_settings`.

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

- **Unsynced only** (`srcShowUnsynced`) — filters all three source panes to artists/albums/tracks not yet on the device, using `isOnDevice()`. Only visible when both device library and source library are loaded. Persisted in `localStorage` under key `nastune-src-unsynced`. Active state indicated by blue border (`.manage-btn.active`).
- **Full rescan** — available for WALKMAN devices (magnifying-glass icon in the device header) and for NAS sources (in the Manage Sources modal and the sources bar). Calls `triggerWalkmanScan(true)` / `rescanSource(id, true)` which send `?full=true` to the backend, clearing all existing DB rows before re-reading every file. A `confirm()` dialog is shown before starting since full rescans can take minutes.

### Source presence highlighting (iPod/WALKMAN pane)

When a source is selected, items in the device pane that are **absent from the source** are highlighted in blue text via the `.not-in-src` CSS class (color `#4a9eff`), applied to `.artist-name`, `.album-name`, and `.t-title`.

`device.js` maintains `_srcKeyMap` — a `Map<trackKey, true>` built from the source library using the same `_trackKey` formula as `_deviceMap`. It is built lazily on first use and cleared whenever `sourceLibrary` changes (source switch, source removed, source rescan completes). Three derived helpers:

- `isTrackInSrc(track, artistName, albumName)` — returns `true` if the track's key exists in `_srcKeyMap`; returns `true` when no source is selected
- `isAlbumInSrc(al, artistName)` — `true` only if **all** tracks in the album are in the source (uses `.every()` with a `length > 0` guard — a single missing track turns the album blue)
- `isArtistInSrc(artist)` — `true` only if **all** albums of the artist satisfy `isAlbumInSrc` (uses `.every()` — a single partially-missing album turns the artist blue)

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

The status bar uses `display:flex`. The right section is wrapped in `.statusbar-right` which has a CSS-declared `width: min(440px, 54%)` — fixed, not content-driven. This prevents layout reflow when the N/M counter or track name changes, which would otherwise cause the left-side text to jump. `justify-content: flex-end` keeps all items stacked against the right edge so only active indicators appear; inactive ones simply aren't rendered, and the remaining items shift right without artificial gaps.

Inside `.statusbar-right`, each `.statusbar-op` span is naturally sized (no `flex-grow`) with `flex-shrink: 1` and `overflow: hidden`. Text elements use `max-width` + `text-overflow: ellipsis` instead of `flex: 1 1 0` — this eliminates the dead space that used to appear between short op text and the timestamp. `.statusbar-op-current` (the track name during a running op) is capped at `max-width: 160px`. The N/M counter uses `font-variant-numeric: tabular-nums` for stable digit widths.

The **build version** (`<span class="build-ver">`) is the last (rightmost) child of `.statusbar-right`, always visible.

### Track matching (sync / isOnDevice)

Tracks are matched across iPod and source library by a normalized key:

```
_normStr(artist) + '|||' + _normStr(album) + '|||' + (disc_nr+'.' if disc_nr>1 else '') + (track_nr || _normStr(title))
```

`_normStr` NFD-decomposes, strips diacritics, lowercases, and collapses all non-alphanumeric characters to spaces. This handles common tag discrepancies (Unicode hyphens, curly quotes, accented characters).

`disc_nr` prefix (e.g. `2.`) is only added when `disc_nr > 1`. Tracks with `disc_nr` of 0, 1, or absent are treated identically, so single-disc albums match regardless of whether disc number is tagged.

The `_deviceMap` (`Map<key, track>`) is built lazily on first use and **always rebuilt** in `_initSrcChecked()` (not skipped when already cached) to avoid stale data after Alpine reactive re-renders during async library fetches. `_srcTrackMap` (`Map<id, track>`) is built on source library load for O(1) ID → track resolution.

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

### Build version display

A `<span class="build-ver">` at the far right of `.statusbar-right` (the status bar's right section) shows the `BUILD_VERSION` env var injected by the server into the Jinja2 template. The full string is also in the element's `title` tooltip for long SHA-suffixed versions.

- `BUILD_VERSION` is read in `app/main.py` at startup (`os.getenv("BUILD_VERSION", "dev")`) and passed to every `/` template render.
- The Dockerfile declares `ARG BUILD_VERSION=dev` → `ENV BUILD_VERSION=${BUILD_VERSION}`, so local `docker build` without the arg defaults to `dev`.
- The CI workflow (`release.yml`) computes `git describe --tags --always --dirty=-dirty` in the "Set build env" step and forwards it as `--build-arg BUILD_VERSION=...`. Tagged release builds show the bare tag (e.g. `v0.2.9`); builds from commits after a tag show `v0.2.9-3-ga8f200d`; dirty trees append `-dirty`.

---

## Track rating system

Ratings (1–5 stars) are stored in `ipod_track_ratings` and flow in two directions:

**iPod → DB** (`persist_ratings` in `services/ratings.py`):
- Called as a background task after every successful `gpod-ls` run (library load or refresh)
- Converts iPod's 0–100 rating to 0–5 stars (`round(r / 20)`); unrated tracks (0) are skipped
- Upserts with `MAX(stored, new)` — highest rating seen across multiple reads wins on conflict
- Track identity uses the same normalized key as sync: `_norm_str(artist) + '|||' + _norm_str(album) + '|||' + disc_prefix + track_nr_or_title`; implemented in `services/track_key.py`, mirroring `_normStr` / `_trackKey` in JS

**DB → iPod** (rating sync step in `_do_sync`, `services/operations.py`):
- Runs on every sync (delete-only and copy); skipped entirely via a `COUNT(*)` pre-check when `ipod_track_ratings` is empty
- Runs `gpod-ls` to get fresh track IDs, builds key→(id, current_stars) map, queries stored ratings
- Calls `gpod-tag --rating <stars> <id…>` grouped by rating value for tracks where `stored_stars > current_ipod_stars`; failure is non-fatal (logged as warning, sync still completes)

**UI → DB** (`POST /library/rate` and `POST /sources/rate`):
- Writing a rating from the track-row star picker updates `ipod_track_ratings` immediately and mutates the in-memory library cache so the UI reflects the change without a page reload
- For iPod tracks, `gpod-tag` is **not** called at this point — it is deferred to the next sync
- Explicit UI writes overwrite the DB value (no max-wins); setting 0 deletes the row
- Source track ratings use the file `path` to look up metadata in `source_tracks`, compute the key, and upsert `ipod_track_ratings` — the same table used for iPod tracks

---

## Sync encoder selection

`_classify_audio_path` in `operations.py` classifies each sync path into one of three buckets, which determine the `gpod-cp` encoder flags:

| Class | Extensions | gpod-cp args |
|---|---|---|
| `lossless` | `.flac` `.wav` `.aiff` `.aif` `.ape` `.wv`, ALAC `.m4a` | `--encoder alac --disable-encoder-fallback` |
| `passthrough` | `.mp3`, AAC `.m4a` | _(no encoder args — copied as-is)_ |
| `lossy` | `.aac` `.ogg` `.wma` `.opus`, other `.m4a` | `--encoder fdk-aac --encoder-quality 9 --disable-encoder-fallback` |

For directory paths (collapsed album/artist args), classification is based on the first audio file found in sorted order. For `.m4a` files, `mutagen.mp4.MP4.info.codec` is read to distinguish ALAC (→ lossless) from AAC (→ passthrough).

**`force_aac` setting** (`settings` table, key `"force_aac"`, value `"true"`): routes everything through fdk-aac regardless of classification — including MP3 and AAC M4A. Useful when the target iPod is limited in storage and you want a uniform AAC library.

**`max_threads` setting** (`settings` table, key `"max_threads"`, value `"N"`): forwarded as `--threads N` to each `gpod-cp` invocation. `0` means let gpod-cp use its default.

Settings are loaded at the start of each sync via `_load_sync_settings()`, which falls back to `(False, 0)` if the `settings` table does not exist yet.

---

## gpod-utils CLI reference

All interaction with the iPod goes through these commands. The `IPOD_MOUNT_POINT` environment variable is read natively by gpod-utils — no explicit path argument needed.

```bash
# List all tracks on iPod as JSON (IPOD_MOUNT_POINT must be set)
gpod-ls

# Copy files to iPod (accepts files or directories)
gpod-cp /music/Artist/Album/track.mp3
gpod-cp /music/Artist/Album/

# Remove tracks from iPod by persistent ID (accepts multiple IDs)
gpod-rm <id1> <id2> ...

# Set track rating (0 = unrated, 1–5 = stars); accepts multiple IDs
gpod-tag --rating <0-5> <id1> <id2> ...
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

The `ipod_track_ratings` table (`track_key TEXT PRIMARY KEY, rating INTEGER, updated_at INTEGER`) stores 0–5 star ratings keyed by normalized track key. It is shared between the iPod and source tabs — the same key formula is used in both so a rating set in the source view applies to the matching iPod track and vice versa.

### Podcast source remapping (`_remap_podcast`)

Podcast files use `album` tag = show name, `artist` tag = empty or per-episode host name. `_remap_podcast` is called per-track when `source_type == 'podcast'`:

- Sets `artist` and `albumartist` to the show name (from `album` tag, falling back to `albumartist`, `artist`, then `'Unknown Show'`)
- Does **not** overwrite `album` — it stays as the original show name in the DB

**Why album must not be overwritten**: the key formula uses `artist + album + track_nr/title`. On the iPod, the `album` field retains the original file tag (show name). If the source DB stored `album = year_string` instead, `isOnDevice` keys would mismatch. The year is used purely for display grouping.

**Season grouping** is done at library-build time in `_build_library` (sources.py) when `source_type == 'podcast'`: `album_key = str(year) if year else artist_key`. This makes column 2 show years/seasons. Individual track objects receive `"album": original_show_name_from_db` so their key matches the iPod. A Full Rescan is needed after the first deploy of this fix to clear DB rows that had `album = year` stored from a previous version.

---

## WALKMAN scanner

`walkman.py` implements a self-contained async scanner triggered manually via the UI magnifying-glass button:

- **Detection**: `parse_capability(mount)` reads `default-capability.xml` at the mount root to extract model, serial, storage type (INTERNAL / CARD), and music path. Returns `None` for non-WALKMAN mounts.
- **DB tables**: `walkman_devices` (keyed by `serial + storage_type` to handle dual-LUN USB) and `walkman_tracks` (indexed by `device_id + path`).
- **Incremental scan**: existing DB rows are fetched first; only files not yet in the DB have tags read with mutagen. Stale DB rows (files removed from disk) are bulk-deleted. Progress is written per-file to `walkman_devices.scan_processed / scan_total / scan_current_file`.
- **Library format**: `fetch_library()` builds the same `artists[].albums[].tracks[]` nested dict as `gpod.py`, plus `walkman: True` and `walkman_db_id` flags used by the router to dispatch operations.
- **Operations**: delete enqueues `os.remove()` for each file; sync enqueues `shutil.copy2()` for each source file into the WALKMAN music directory. Both update the `walkman_tracks` table immediately after each file so the library is consistent without a rescan.
- **Full rescan**: `POST /walkman/scan?devnode=&full=true` clears all `walkman_tracks` rows for the device and resets `track_count=0` before starting the scan, forcing every file to be re-read. Used to pick up tag corrections or filesystem changes that the incremental scan would miss.

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
- **iPod service module**: `app/services/ipod.py` owns `IPOD_SENTINEL`, `is_ipod(mount)`, and `log_mount_contents(mount)` — parallel to `app/services/walkman.py` for WALKMAN. `devices.py` imports these as `detect_ipod` / `log_ipod_mount_contents` to avoid shadowing the local `is_ipod` variable that holds the boolean result.
- **Source switch flash prevention**: `pickSource()` does NOT null out `sourceLibrary` before fetching the new library. The old library stays visible while the fetch is in flight; `_loadSourceLibrary` replaces it atomically on success. Setting `sourceLibrary = null` before the fetch causes a blank flash (two separate Alpine render batches across `await` boundaries). The stale-response guard `if (this.selectedSourceId !== id) return` inside `_loadSourceLibrary` handles rapid source switching.
- **`_startSrcPoll` wasScanning guard**: `_startSrcPoll` records `wasScanning` before calling `loadSources`. On the tick where scanning transitions to done, the library is reloaded only when `wasScanning && !scanning`. Without this guard, `_loadSourceLibrary` would fire spuriously on the first tick after `pickSource()` even if the source was never scanning.
- **Sync confirmation bypass**: `syncNeedsConfirm` being `false` means the Sync button calls `confirmSync()` directly without opening the modal. The modal is still rendered in the DOM but only shown when `showSyncConfirm` is set to `true`. Do not add `showSyncConfirm = true` to the Sync button click handler unconditionally — it will break the fast-path.
- **Track library structure**: The nested library response (`artists[].albums[].tracks[]`) does not embed `album` or `albumartist` on individual track objects — those fields live at the parent level. Code that needs full track context (e.g. download, display) must walk the nested structure to obtain them.
- **`__ALL__` sentinel in album checkboxes**: `isAlbumSelected`, `isAlbumIndeterminate`, `toggleAlbum` (and source equivalents) accept an album object, not `(artistName, albumName)`. Do not change to string-based lookup — it breaks when `selectedArtist === '__ALL__'` because no library artist has that name.
- **`_normStr` empty fallback**: `_normStr` returns `norm || raw` so that symbol-only strings like `#####` (which normalize to `''`) don't collide with each other or with null/empty artist names.
- **Status bar `.statusbar-right` width**: declared as `width: min(440px, 54%)` (CSS value, not content-driven). Never change to `width: auto` or remove the declaration — the right section will grow/shrink with text and cause the left content to jump during N/M counter updates. Do not add `flex-grow` to `.statusbar-op` children — that re-introduces artificial gaps between op text and timestamps.
- **Rating sync runs `gpod-ls`**: `_gpod_rating_sync` re-reads the full iPod library after copy/delete to get fresh track IDs. This adds a few seconds to sync time on large libraries. It is skipped via a `COUNT(*)` pre-check when `ipod_track_ratings` is empty, so new installs with no rated tracks pay no cost.
- **Star picker uses nested `x-data` in `x-for`**: each track row's `.t-rating` span carries `x-data="{ hov: 0 }"` to manage per-row hover state. Alpine v3 correctly scopes nested `x-data` inside `x-for` iterations. `@click.stop` on the container prevents the click from bubbling to the row's `openDetail` handler.
- **iPod rating deferred to sync**: `POST /library/rate` writes to `ipod_track_ratings` and mutates the in-memory cache, but does **not** call `gpod-tag` immediately. The actual iTunesDB write happens during the next sync's rating-sync step. This means ratings set via UI are not written to the iPod until the next sync operation completes.
- **`ipod_track_ratings` shared across tabs**: the same normalized key is used by both the iPod library (via `persist_ratings` and `POST /library/rate`) and the source library (via `POST /sources/rate` and `GET /sources/{id}/library`). A rating set in one view is visible in the other after the next library load.
- **Rating scale**: `ipod_track_ratings` stores 0–5 stars. The iPod library cache stores the iPod-native 0–100 scale (`rating * 20`). `fmtRating(r)` in `utils.js` takes 0–100; `fmtRatingStars(s)` takes 0–5. Use the correct formatter for each tab.
- **Device settings devnode in query param**: `GET/PUT /devices/device-settings` takes `devnode` as a query parameter, not a path segment. DevNode values like `/dev/sdb3` contain slashes — Starlette decodes `%2F` back to `/` before routing, so a path-segment route `/devices/{devnode}/device-settings` would never match. Always use `?devnode=` and `encodeURIComponent` on the JS side.
- **iPod mediatype bitmask**: `gpod-ls` returns `mediatype` as a raw integer bitmask (AUDIO=1, VIDEO=2, PODCAST=4, AUDIOBOOK=8). Value `5` (PODCAST|AUDIO) is a valid podcast — use `raw & 4` to test for podcast, not a dict lookup. `_classify_mediatype` in `gpod.py` implements this.
- **Podcast artist/album fallback in gpod.py**: for podcast and audiobook tracks where both `albumartist` and `artist` are null (common for podcasts), `_parse` uses `t.get("album")` (the show name) as the artist key. This matches how the source scanner sets `artist = show_name` via `_remap_podcast`.
- **Podcast key matching**: `track.album` in source library track objects must equal the iPod's `album` field (the show name) for `isOnDevice` to match. Do not overwrite `album` with year/season in `_remap_podcast` — season grouping is done at library-build time by `_build_library` using `year` as `album_key`, while the track object retains `"album": show_name`.
- **`filteredSrcArtists` null guard**: returns `[]` when `selectedSourceObj` is null. Without this, switching to a media type with no source still shows the library from the previous type's source (the `sourceLibrary` reference is not nulled on type switch when no matching source exists).

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
