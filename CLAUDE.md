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
| Client-side state | **Alpine.js** (vendored, `app/static/alpine.min.js`) | 3-pane browser state, player, modals, detail panel — no build step |
| iPod CLI backend | **gpod-utils** (`gpod-ls`, `gpod-cp`, `gpod-rm`) | Wraps libgpod, supports iPod Classic and iPod 5 |
| WALKMAN backend | **shutil** + **SQLite** | Direct file copy/delete; library indexed once and updated incrementally |
| Album art extraction | **mutagen** + **ffmpeg** | mutagen for embedded tags; ffmpeg for cover extraction; cached persistently in SQLite |
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
├── requirements.txt
└── app/
    ├── main.py                # FastAPI app factory; mounts /static; core iPod endpoints
    ├── routers/
    │   ├── device.py          # /library/delete, /library/sync, /library/download, /operations
    │   ├── walkman.py         # /walkman/scan, /walkman/scan_status
    │   └── sources.py         # /sources/* — CRUD, scan, browse, library, audio, artwork
    ├── services/
    │   ├── devices.py         # DeviceService: lsblk polling, probe-mount, mount/unmount, library cache, eject
    │   ├── ipod.py            # iPod detection: IPOD_SENTINEL, is_ipod(), log_mount_contents()
    │   ├── ipod_db.py         # Persistent iPod device records, per-device settings, sync rules, auto-sync path computation
    │   ├── gpod.py            # Runs gpod-ls, parses JSON → nested artist/album/track dicts; _classify_mediatype bitmask
    │   ├── walkman.py         # WALKMAN detection, SQLite scan, library build, delete/copy ops, fetch_library_offline
    │   ├── artwork.py         # mutagen-based artwork extractor (M4A/MP3/FLAC)
    │   ├── artwork_cache.py   # Persistent artwork cache: SQLite index + file store; junction-table ref-counting per owner
    │   ├── fs_utils.py        # os.statvfs capacity + /proc/mounts FS-type label
    │   ├── db.py              # SQLite schema + migrations
    │   ├── scanner.py         # Async file scanner: walks dirs, reads tags via mutagen; _remap_podcast for podcast sources; stores pub_date raw string
    │   ├── track_key.py       # Python port of JS _normStr/_trackKey; shared by ratings + operations
    │   ├── ratings.py         # persist_ratings(): upserts iPod ratings into ipod_track_ratings (max wins); persist_playcounts()
    │   └── operations.py      # OperationService: gpod-rm / gpod-cp / gpod-verify / WALKMAN shutil; smart encoder selection; progress tracking
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

```bash
docker compose up --build   # build + start on port 127.0.0.1:8080
```

Key volumes: `/dev:/dev`, `./ipod:/mnt/ipod`, `/mnt/music:/music:ro`, `./data:/data`. `privileged: true` required for mount syscalls.

Both Dockerfiles install `hfsprogs` (for `fsck.hfsplus`) and `dosfstools` (for `fsck.vfat`) to support the filesystem check operation. gpod-utils 1.4.20.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `IPOD_MOUNT_POINT` | _(unset)_ | Register a pre-mounted iPod directory as a manual device. The poll loop still runs alongside it. |
| `IPOD_MOUNT_BASE` | `/mnt/ipods` | Base directory where auto-discovered devices are mounted (subdirs per devname). |
| `IPOD_AUTOMOUNT` | `0` | Set to `1`/`true`/`yes` to enable automatic mounting of USB block devices found by lsblk. |
| `DB_PATH` | `/data/nastune.db` | Path to the SQLite database file for source library index. |
| `GPOD_DRY_RUN` | `0` | Set to `1`/`true`/`yes` to log all `gpod-rm` and `gpod-cp` commands without executing them. |
| `BUILD_VERSION` | `dev` | Version string shown in the UI header. Set by Docker build via `ARG BUILD_VERSION`; CI uses `git describe --tags --always --dirty=-dirty`. |

---

## HTTP API

### Core (app/main.py)

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Renders the main HTML UI |
| `GET` | `/devices` | All known devices + currently selected devnode; includes `known_devices` with iPod and WALKMAN records |
| `POST` | `/devices/select` | `{ devnode }` — select a device; triggers library load if not cached |
| `POST` | `/devices/mount` | `{ devnode }` — mount an unmounted device |
| `POST` | `/devices/eject` | `{ devnode }` — sync + umount + remove; returns 409 if device is busy |
| `GET` | `/devices/events` | SSE stream; pushes device state on connect and on any change |
| `GET` | `/library` | Cached library for the selected device (loads if needed) |
| `POST` | `/library/refresh` | Clears cache and re-runs `gpod-ls` for the selected device |
| `GET` | `/artwork/album?artist=&album=` | Looks up cached artwork by normalized artist+album keys; 404 if not cached |
| `GET` | `/artwork?path=&devnode=` | Extracts embedded album art via mutagen; cached 24 h |
| `GET` | `/audio?path=&devnode=` | Serves audio from iPod mount; ALAC M4A is transcoded to FLAC on-the-fly |
| `GET` | `/device-settings?devnode=` | Per-device settings (force_aac override, sync rules) |
| `POST` | `/device-settings` | `{ devnode?, db_id?, device_type?, force_aac, sync_rules[] }` — save settings |
| `POST` | `/auto-sync` | `{ devnode }` — run sync using per-device sync rules; returns 409 if busy or no rules |
| `POST` | `/artwork/cache/drop` | Delete all cached artwork files and clear the DB tables |

### iPod/WALKMAN operations (app/routers/device.py)

| Method | Path | Description |
|---|---|---|
| `POST` | `/library/check-fs` | `{ devnode }` — unmount, run `fsck` (hfsplus or vfat), remount; tracked as `check_fs` op |
| `POST` | `/library/verify` | `{ devnode, mode }` — runs `gpod-verify`; `mode`: `'check'`, `'add'`, `'delete'`; iPod-only |
| `POST` | `/library/delete` | `{ devnode, track_ids[] }` — enqueues gpod-rm for each track ID |
| `POST` | `/library/sync` | `{ devnode, copy_paths[], delete_ids[], copy_track_count }` — delete then copy then rating sync |
| `POST` | `/library/rate` | `{ devnode, track_id, rating }` — set track rating (0–5); updates cache + ipod_track_ratings; gpod-tag applied on next sync |
| `POST` | `/library/download` | `{ devnode, tracks[] }` — streams selected tracks as a `.tar` archive |
| `GET` | `/operations` | Current operation status: kind, status, processed, total, current, error, started_at |
| `GET` | `/operations/events` | SSE stream; pushes full op state on any change (250 ms server-side diff-poll) |
| `GET` | `/operations/log?from=N` | Lines `[N:]` of the current op log + total line count; used by frontend poll |
| `GET` | `/operations/history?devnode=` | Last 10 finished ops for the device, newest-first, full log included |
| `GET` | `/devices/offline-library?device_id=&device_type=` | Cached library for a disconnected device; `device_type=ipod` returns `library_json`; `device_type=walkman` queries `walkman_tracks` via `fetch_library_offline` |

### WALKMAN operations (app/routers/walkman.py — prefix `/walkman`)

| Method | Path | Description |
|---|---|---|
| `POST` | `/walkman/scan?devnode=&full=` | Trigger a background library scan; `full=true` clears all tracks first |
| `GET` | `/walkman/scan_status?devnode=` | Live scan progress: status, processed, total, current_file, error |

WALKMAN delete and sync go through the same `/library/delete` and `/library/sync` endpoints as iPod — the router dispatches to `walkman.py` based on `device_info.is_walkman`.

#### Download track object schema
```json
{ "ipod_path": ":iPod_Control:Music:F02:TGWN.mp3", "artist": "...", "albumartist": "...",
  "album": "...", "year": 1980, "track_nr": 3, "title": "..." }
```
Archive restores: `{albumartist}/{[year] - album}/{NN - title.ext}`

### Sources (app/routers/sources.py — prefix `/sources`)

| Method | Path | Description |
|---|---|---|
| `GET` | `/sources` | List all registered sources |
| `POST` | `/sources` | `{ name, path, type }` — add source and start scan |
| `DELETE` | `/sources/{id}` | Remove source and all its track data |
| `POST` | `/sources/{id}/scan?full=` | Trigger a rescan; `full=true` clears all tracks first |
| `GET` | `/sources/browse?path=` | Directory browser for adding sources |
| `GET` | `/sources/{id}/library` | Source library as artist → album → track hierarchy |
| `GET` | `/sources/audio?path=` | Serve audio file from source; ALAC → FLAC transcode |
| `GET` | `/sources/artwork?path=` | Extract artwork from source file via mutagen |
| `POST` | `/sources/rate` | `{ path, rating }` — set rating (0–5) for a source track by file path |

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

Navigation state is encoded in the URL hash so the browser preserves it across reloads:

- Library tab: `#tab=library&artist=Joy+Division&album=Closer`
- Sources tab: `#tab=sources&src=1&srca=Joy+Division&sral=Closer`

`app.js` reads the hash during `init()` and restores state after the library/source loads, with validation that the artist and album still exist. `$watch` on `viewMode`, `selectedArtist`, `selectedAlbum`, `selectedSourceId`, `srcArtist`, `srcAlbum` calls `_syncUrl()` on every navigation change. `history.replaceState` is used (no new history entries).

### Operation history and SSE

- **SSE**: `_connectOpEvents()` opens `EventSource` to `/operations/events` (closure-scoped). On `running → done/error`, library is refreshed and `loadOpHistory` called.
- **In-place mutation for running ops**: SSE `running` updates mutate `existing.processed`/`existing.current` in-place rather than replacing `this.currentOp` — replacing re-evaluates every getter reading `currentOp.*`.
- **Fast-op detection**: stamps `connectedAt = Date.now() / 1000` on EventSource open; triggers refresh if `op.started_at >= connectedAt && op.status !== 'running'`. Guards against spurious refresh on pre-existing done ops.
- **History files**: each finished op written to `/data/op_history/{device_id}/{timestamp}.json`; directory pruned to last 10 files.
- **`lastOp` getter**: in-session `currentOp` (done/error) takes priority over `opHistory[0]`.
- **Op log modal**: live ops poll `GET /operations/log?from=N` every 2 s. Backend uses `Query(0, alias="from")` (`from` is a Python keyword). `_appendLogLines` caps at 500 DOM nodes; auto-scrolls only when within 40 px of bottom. `openLiveLog()` clears `<pre>` before polling to prevent duplicate lines on re-open.

### Media type system

`mediaType` (stored in `localStorage` under key `nastune-media-type`) is a shared state value that gates both the device pane and source pane simultaneously. Valid values: `'music'`, `'podcast'`, `'audiobook'`.

- **Device pane** (`_typeFilteredLibrary`): filters tracks by `t.mediatype` — `'music'` accepts `null` or `'music'`; `'podcast'` and `'audiobook'` match exactly. Track `mediatype` field is set by `_classify_mediatype` in `gpod.py` (bitmask: AUDIO=1, VIDEO=2, PODCAST=4, AUDIOBOOK=8; value 5 = PODCAST|AUDIO which classifies as podcast).
- **Source pane** (`filteredSources`): filters the sources list to `s.type === mediaType`. Each source has a `type` column in the DB set at creation time.
- **WALKMAN**: Podcasts and Audiobooks tabs are hidden when a WALKMAN is selected (`x-show="!selectedDevice?.is_walkman"`); if the active `mediaType` is either when a WALKMAN library loads, it is reset to `'music'`.
- **Labels**: `devLabels` (in `device.js`) and `srcLabels` (in `sources.js`) return media-type-specific terminology — "Artists/Albums/Tracks" for music, "Shows/Seasons/Episodes" for podcasts, "Authors/Books/Chapters" for audiobooks.
- **Per-type source memory**: `setMediaType()` saves the current `selectedSourceId` to `localStorage` under key `nastune-src-{type}` before switching, and restores it on switch-back.

### Device settings and auto-sync

`settings.js` contains both the global settings modal and the per-device settings modal. They are independent — the device settings modal uses `_deviceSettingsDevnode` (for connected devices) or `_deviceSettingsDbId + _deviceSettingsType` (for offline/known devices via the known-devices list).

- `openDeviceSettings(devnode)`: for unmounted iPods, redirects to `openDeviceSettingsForKnown` via USB serial → UUID match. For unmounted WALKMANs, redirects via `walkman_db_id`. Mounted devices proceed with `_deviceSettingsDevnode`.
- `_loadDeviceSettings()`: calls `GET /devices/device-settings?devnode=` (connected) or `GET /devices/known/{id}/settings?device_type=` (offline). Note: devnode is a **query parameter** (not path) because devnode values like `/dev/sdb3` contain slashes that Starlette would decode and misroute if used as path segments.
- **Auto-sync** (`runAutoSync()`): sends `POST /auto-sync?devnode=` which calls `compute_auto_sync_paths` in `ipod_db.py` — walks per-device sync rules, collects source paths for tracks not already on the device, and passes them to the normal sync endpoint.
- **Per-device `force_aac`** override: takes precedence over the global `force_aac` setting from the `settings` table; stored in `ipod_device_settings` / `walkman_device_settings`.
- **Library verify** (`runVerify(mode)`): posts `POST /library/verify { devnode, mode }` and closes the modal; only shown when the device is connected and mounted; three modes: `check`, `add`, `delete`. Tracked as an operation so progress appears in the status bar.
- **Filesystem check** (`runCheckFs()`): available only for connected mounted devices. Posts `POST /library/check-fs { devnode }`. Uses a `confirm()` dialog before proceeding. The `check_fs` op has `total=0` — the status bar hides the N/M counter when `total === 0`.
- **Eject button**: shown only when `selectedDevice?.mounted` is true; applies to both iPod and WALKMAN.

### All Artists mode

`selectedArtist === '__ALL__'` selects all artists at once. `currentAlbums` returns a flat list via `flatMap`. `pickArtist('__ALL__')` skips first-album auto-select. Album checkboxes pass the **album object** to `isAlbumSelected`/`toggleAlbum` (not `(artistName, albumName)`) — string lookup breaks when `selectedArtist === '__ALL__'`. URL encodes `__ALL__` literally.

### Search navigation

Search filters all three panes simultaneously; `onSearch()` clears artist/album selection on each keystroke. Clicking an artist sets both `search` and `selectedArtist` — the album getter shows all albums when the artist name matched, or only matching albums/tracks when an album/track matched. A `×` button (`.search-clear`) appears when the field is non-empty.

### Sources bar filters

- **Unsynced only** (`srcShowUnsynced`) — filters all three source panes to artists/albums/tracks not yet on the device, using `isOnDevice()`. Only visible when both device library and source library are loaded. Persisted in `localStorage` under key `nastune-src-unsynced`. Active state indicated by blue border (`.manage-btn.active`).
- **Full rescan** — available for WALKMAN devices and NAS sources. Calls `triggerWalkmanScan(true)` / `rescanSource(id, true)` which send `?full=true` to the backend. A `confirm()` dialog is shown before starting.

### Source presence highlighting (iPod/WALKMAN pane)

When a source is selected, items in the device pane that are **absent from the source** are highlighted in blue text via the `.not-in-src` CSS class, applied to `.artist-name`, `.album-name`, and `.t-title`.

`device.js` maintains `_srcKeyMap` — a `Map<trackKey, true>` built from the source library using the same `_trackKey` formula as `_deviceMap`. It is built lazily on first use and cleared whenever `sourceLibrary` changes. Three derived helpers: `isTrackInSrc`, `isAlbumInSrc` (all tracks must match), `isArtistInSrc` (all albums must match).

### Sync confirmation dialog

`syncNeedsConfirm`: true when deletes are queued or `syncCopyBytes > freeBytes + syncDeleteBytes`. When false, Sync calls `confirmSync()` directly — do not add `showSyncConfirm = true` unconditionally.

### Storage bar

Shows `used / ± net diff / free`. Percentages computed from byte totals at op start; a `.storage-bar-delta` overlay shows live `−N`/`+N` track counts during ops. `syncToDelete`/`syncToCopy` cached in `_syncCache` keyed by reference equality of `srcInitialOnIpod` and `srcChecked` Sets.

### Status bar right section

`.statusbar-right` has `width: min(440px, 54%)` — fixed, not content-driven. This prevents layout reflow when the N/M counter or track name changes. Text elements use `max-width` + `text-overflow: ellipsis`. The build version span is the rightmost child, always visible.

### Track matching (sync / isOnDevice)

Key: `_normStr(artist) + '|||' + _normStr(album) + '|||' + (disc_nr+'.' if disc_nr>1 else '') + (track_nr || _normStr(title))`

`_normStr` NFD-decomposes, strips diacritics, lowercases, collapses non-alphanumeric to spaces. `disc_nr` prefix only when `> 1`; discs 0, 1, absent are treated identically. `_deviceMap` built lazily and **always rebuilt** in `_initSrcChecked()`. `_srcTrackMap` built on source library load for O(1) ID → track resolution.

### gpod-cp path collapsing (`_buildCopyPaths`)

`sources.js` exposes `_buildCopyPaths(tracks)` which compresses individual file paths into directory paths before sending to `gpod-cp`. At up to 3 ancestor levels (CD dir → album dir → artist dir), if every library track under a directory is in the selection, the directory path is used instead of individual files. The sync body includes `copy_track_count` (actual track count, not collapsed path count) so `op.total` reflects real track counts.

### Sync progress

`_gpod_cp_batch` in `operations.py` parses `[N/M]` streaming lines to update `op.processed` with per-track granularity and `op.current` with the track name. A `proc_offset` parameter accumulates progress across multi-batch syncs. `BATCH_SIZE = 50`.

### Light / dark theming

Auto / Light / Dark modes via a 3-segment pill switcher in the header. Preference stored in `localStorage` under key `nastune-theme`.

- Visual switching toggles the `html.light` class — all components use CSS custom properties (`--surface-bg`, `--card-bg`, `--player-bg`, `--detail-bg`, `--fill-bg`, `--hover-surface`, `--chip-active`, `--badge-bg`, `--overlay-bg`, `--card-border`, `--surface-border`, `--icon-grad-from/to`).
- `html.light` is set immediately via an inline `<script>` in `<head>` (before the CSS `<link>`) to prevent FOUC. Do not move it after the stylesheet link.
- `initTheme()` attaches a `prefers-color-scheme` media-query listener so Auto mode reacts to OS-level theme changes without a page reload.

---

## Track rating system

Ratings (1–5 stars) stored in `ipod_track_ratings`:

- **iPod → DB** (`persist_ratings`): background task after `gpod-ls`; converts 0–100 → 0–5 (`round(r/20)`); upserts with `MAX(stored, new)`.
- **DB → iPod** (sync step): runs `gpod-ls` for fresh IDs, then `gpod-tag --rating` grouped by value where `stored > current`; skipped when table empty; non-fatal.
- **UI → DB** (`POST /library/rate`, `POST /sources/rate`): writes DB + mutates cache immediately; `gpod-tag` deferred to sync; setting 0 deletes the row.

---

## Artwork cache

`artwork_cache.py` stores album artwork keyed by `(artist_key, album_key)` (same `norm_str` as track keys).

- **`cache_library_artwork`**: background task after library load; tries source files first, then device file; semaphore-limited by `max_threads`.
- **`lookup_artwork(albumartist, album)`**: DB-only lookup; returns `None` if not cached. Used by `GET /artwork/album` for device pane and offline browsing.
- **`artwork_refs` junction table**: tracks `(owner_type, owner_id)` per artwork entry; file deleted when ref count drops to 0.
- **`drop_all_artwork()`**: deletes all files and clears tables; exposed via `POST /artwork/cache/drop`.
- **Stale cache heal**: `get_ipod_cached_library` backfills `albumartist` on stale cached album objects so `artUrl()` always sends a non-empty artist key.

---

## Sync encoder selection

`_classify_audio_path` in `operations.py` classifies each sync path:

| Class | Extensions | gpod-cp args |
|---|---|---|
| `lossless` | `.flac` `.wav` `.aiff` `.aif` `.ape` `.wv`, ALAC `.m4a` | `--encoder alac --disable-encoder-fallback` |
| `passthrough` | `.mp3`, AAC `.m4a` | _(no encoder args — copied as-is)_ |
| `lossy` | `.aac` `.ogg` `.wma` `.opus`, other `.m4a` | `--encoder fdk-aac --encoder-quality 9 --disable-encoder-fallback` |

For directory paths, classification is based on the first audio file found in sorted order. For `.m4a` files, `mutagen.mp4.MP4.info.codec` is read to distinguish ALAC from AAC.

**`force_aac` setting**: routes everything through fdk-aac regardless of classification. **`max_threads` setting**: forwarded as `--threads N` to each `gpod-cp` invocation. Both loaded at sync start via `_load_sync_settings()`.

---

## gpod-utils CLI reference

`IPOD_MOUNT_POINT` env var is read natively by gpod-utils (except `gpod-verify`).

```bash
gpod-ls                              # list all tracks as JSON
gpod-cp /music/Artist/Album/         # copy files or directories to iPod
gpod-rm <id1> <id2> ...              # remove by persistent ID
gpod-tag --rating <0-5> <id1> ...   # set star rating
gpod-verify -M <mount>               # check only
gpod-verify -M <mount> --add        # add entries for orphan files
gpod-verify -M <mount> --delete     # remove entries with no file
```

Every invocation is logged at INFO level. Set `GPOD_DRY_RUN=1` to skip execution while preserving logs.

`gpod-ls` JSON schema: `ipod_data.device` (model, capacity, uuid) + `ipod_data.playlists.items[]` where `type == "master"` contains all tracks. Track fields: `id`, `ipod_path`, `title`, `artist`, `album`, `albumartist`, `filetype`, `bitrate`, `samplerate`, `tracklen` (ms), `track_nr`, `cd_nr`, `year`, `size`, `artwork` (bool), `rating` (0–100), `playcount`.

`gpod-rm` output: `[N/M]  :iPod_Control:... -> { id=X }` per track. `gpod-cp` output: `[N/M]  /source/path -> { title='...' ... }` per track, summary `X/M items (size)  dupl=D`.

---

## Source library scanner

`scanner.py` walks directories with `asyncio.to_thread`. Tags are read via mutagen with `easy=True`; M4A/AAC files are re-opened with `mutagen.mp4.MP4` to extract `codec` and `bits_per_sample`.

The DB schema includes `disc_nr INTEGER` and `pub_date TEXT` on `source_tracks`. `pub_date` stores the raw `date` mutagen tag string (e.g. `2026-05-19T12:12:40` or `2024-03-15`). `fmtPubDate(d)` in `utils.js` uses `s.match(/^(\d{4})-(\d{2})-(\d{2})/)` to extract date components and constructs `new Date(+y, +m-1, +d)` (local time, no UTC shift). `year` is extracted via `date_str.split('-')[0]`.

The `ipod_track_ratings` table stores 0–5 star ratings keyed by normalized track key, shared between iPod and source tabs. The `ipod_track_playcounts` table mirrors the same pattern. The source library response attaches `played: bool | None` to each track — `null` means no DB record, `false` means explicitly 0 plays, `true` means played.

### Podcast source remapping (`_remap_podcast`)

Called per-track when `source_type == 'podcast'`: sets `artist` and `albumartist` to the show name using priority `albumartist` → `artist` → `album` → `'Unknown Show'`. This mirrors `gpod.py`'s grouping logic exactly, so track keys match between the source pane and the device pane. `album` is **not** overwritten — it stays as the original show name so `isOnDevice` keys match the iPod.

**Season grouping** is done at library-build time in `_build_library` (sources.py): `album_key = str(year) if year else artist_key`. Track objects retain `"album": show_name` for key matching.

---

## WALKMAN scanner

`walkman.py` implements a self-contained async scanner:

- **Detection**: `parse_capability(mount)` reads `default-capability.xml` to extract model, serial, storage type (INTERNAL / CARD), and music path.
- **Probe-mount**: when an unmounted device first appears in the poll loop (`_process_bd`), `_probe_walkman_cap` briefly mounts it read-only to a temp dir, parses the capability XML, then unmounts. This identifies each LUN of a dual-LUN device (both share the same USB serial but each has its own XML reporting INTERNAL or CARD) before the user selects anything. `_walkman_meta[devnode]` is populated at probe time so `ensure_mounted` skips re-detection. Ejected devices skip the probe.
- **Dual-LUN devices**: some WALKMAN models expose INTERNAL storage and SD card as two separate USB mass-storage LUNs. Each is keyed by `serial + storage_type` in `walkman_devices` (`UNIQUE(serial, storage_type)`). `DeviceInfo.walkman_storage_type` carries the capitalized type string through all code paths.
- **DB tables**: `walkman_devices` (keyed by `serial + storage_type`) and `walkman_tracks` (indexed by `device_id + path`).
- **Incremental scan**: existing DB rows are fetched first; only new files have tags read with mutagen. Stale rows are bulk-deleted.
- **Library format**: `fetch_library()` builds the same `artists[].albums[].tracks[]` nested dict as `gpod.py`, plus `walkman: True` and `walkman_db_id` flags.
- **Offline browsing**: `fetch_library_offline(db_device_id)` reads `walkman_tracks` from SQLite without needing a mount. Called by `GET /devices/offline-library?device_type=walkman` so disconnected WALKMANs can be browsed from the picker's Disconnected section.
- **Operations**: `os.remove()` / `shutil.copy2()` update `walkman_tracks` immediately after each file.
- **Full rescan**: `POST /walkman/scan?devnode=&full=true` clears all rows and resets `track_count=0`.

---

## Known constraints and gotchas

- **libgpod hash requirement**: Writing to iPod Classic (6th gen+) and Nano 3G+ requires a device-specific cryptographic hash. Handled inside gpod-utils.
- **Mount permissions**: The container must run privileged or with `CAP_SYS_ADMIN` for mount syscalls.
- **iTubesDB corruption**: Always take a backup of `/mnt/ipod/iPod_Control/iTunes/iTunesDB` before any write operation. Use `GPOD_DRY_RUN=1` when testing.
- **HFS+ vs FAT32**: iPod 5 may be formatted as HFS+. Ensure `hfsplus` kernel module or `hfsprogs` is available if needed.
- **ALAC playback**: Firefox on Linux cannot decode ALAC in M4A containers. `/audio` and `/sources/audio` detect ALAC via `mutagen.MP4.info.codec` and pipe through `ffmpeg -f flac -` on the fly. Seeking is disabled for streamed ALAC.
- **IPOD_AUTOMOUNT disabled by default**: The poll loop detects already-mounted iPods but will not call `mount` itself unless `IPOD_AUTOMOUNT=1`.
- **Alpine.js x-show + inline flex**: Alpine's `x-show` sets `el.style.display = ''` when restoring visibility, which wipes any inline `display:flex`. Always use a CSS class for flex containers toggled with `x-show`.
- **Object.defineProperties for getters**: Alpine.js getters in module objects must be merged with `Object.defineProperties`, not `Object.assign` — the latter evaluates getters immediately and stores the result as a plain value.
- **Alpine SSE in-place mutation**: When the SSE handler receives a `running` update for the same op, mutate `existing.processed`/`existing.current` in-place rather than replacing `this.currentOp`. Replacing the whole object causes Alpine to re-evaluate every getter that reads any `currentOp.*` field.
- **CSS Grid reflow from display toggle**: Never use `display:none ↔ display:inline` on elements inside a CSS Grid. Use `position:absolute + opacity` instead — `.t-nr-play` is absolutely positioned over `.t-nr-text` so grid column width never changes.
- **CSS animation forces 60fps VSync**: Any active CSS animation forces Firefox's RefreshDriver to 60fps. The storage bar stripe animation was removed for this reason — a static hatched `::before` overlay is used instead when an op is running.
- **Op log duplication**: `openLiveLog()` clears the `<pre>` content before starting the poll. Without this, re-opening the modal mid-operation duplicates already-rendered lines.
- **Unicode tag normalization**: Source file tags often use Unicode hyphens (U+2010) or curly apostrophes (U+2019) where the iPod DB stores ASCII. `_normStr()` in `utils.js` normalizes both before key comparison.
- **Mobile single-pane navigation**: On `≤768px` screens the three-column browser collapses to a single-pane slide view driven entirely by CSS `:has()` — no JS required.
- **Light theme + FOUC**: The FOUC-prevention inline `<script>` runs before the CSS `<link>` in `<head>`. Do not move it after the stylesheet link.
- **Theme CSS variable tokens**: Use `var(--surface-bg)`, `var(--card-bg)`, `var(--fill-bg)`, `var(--surface-border)`, `var(--card-border)`, `var(--hover-surface)`, `var(--chip-active)`, `var(--overlay-bg)`, `var(--detail-bg)`, `var(--player-bg)` — never hardcoded `rgba()` values.
- **gpod-rm ID positional shift**: After each batch deletion, remaining track IDs shift down. Always sort delete IDs in descending order and process them highest-first.
- **gpod-cp directory args**: `gpod-cp` accepts both file paths and directory paths. `_buildCopyPaths` exploits this to collapse complete album/artist selections into a single directory argument.
- **WALKMAN NTFS/exFAT**: `devices.py` accepts `vfat`, `ntfs`, `exfat`, and `fuseblk` fstypes. VFAT mounts get `utf8` / `iocharset=utf8` mount options.
- **SSE connectedAt guard**: `_connectOpEvents()` stamps `connectedAt = Date.now() / 1000` when the EventSource opens. The `justFinished` condition uses `op.started_at >= connectedAt` instead of `prevStartedAt != null` — the latter fails (JS `undefined == null` is true) when no prior op exists in the session.
- **`_srcKeyMap` invalidation**: `_srcKeyMap` must be set to `null` wherever `sourceLibrary` is reassigned. Missing an invalidation point causes stale source-presence highlighting in the device pane.
- **iPod service module**: `app/services/ipod.py` owns `IPOD_SENTINEL`, `is_ipod(mount)`, and `log_mount_contents(mount)`. `devices.py` imports these as `detect_ipod` / `log_ipod_mount_contents` to avoid shadowing the local `is_ipod` variable.
- **Source switch flash prevention**: `pickSource()` does NOT null out `sourceLibrary` before fetching. Setting `sourceLibrary = null` before the fetch causes a blank flash. The stale-response guard `if (this.selectedSourceId !== id) return` handles rapid source switching.
- **`_startSrcPoll` wasScanning guard**: `wasScanning` recorded before `loadSources`. Library reloads only when `wasScanning && !scanning` — prevents spurious `_loadSourceLibrary` on first tick after `pickSource()`.
- **Track library structure**: `artists[].albums[].tracks[]` does not embed `album` or `albumartist` on individual track objects — those fields live at the parent level.
- **`__ALL__` sentinel in album checkboxes**: `isAlbumSelected`, `isAlbumIndeterminate`, `toggleAlbum` (and source equivalents) accept an album object, not `(artistName, albumName)`. String-based lookup breaks when `selectedArtist === '__ALL__'`.
- **`_normStr` empty fallback**: returns `norm || raw` so symbol-only strings like `#####` (which normalize to `''`) don't collide with each other.
- **Status bar `.statusbar-right` width**: declared as `width: min(440px, 54%)`. Never change to `width: auto` — the right section will grow/shrink and cause the left content to jump.
- **Rating sync runs `gpod-ls`**: `_gpod_rating_sync` re-reads the full iPod library after copy/delete. Skipped via `COUNT(*)` pre-check when `ipod_track_ratings` is empty.
- **Star picker uses nested `x-data` in `x-for`**: each track row's `.t-rating` span carries `x-data="{ hov: 0 }"` for per-row hover state. `@click.stop` prevents click from bubbling to `openDetail`.
- **iPod rating deferred to sync**: `POST /library/rate` writes to `ipod_track_ratings` and mutates the in-memory cache, but `gpod-tag` is not called until next sync.
- **Rating scale**: `ipod_track_ratings` stores 0–5 stars. iPod library cache stores 0–100 (`rating * 20`). `fmtRating(r)` takes 0–100; `fmtRatingStars(s)` takes 0–5.
- **Device settings devnode in query param**: `devnode` is a query param, not a path segment — Starlette decodes `%2F` before routing, so path-segment routes never match for `/dev/sdX` values.
- **iPod mediatype bitmask**: `gpod-ls` returns `mediatype` as a raw integer bitmask (AUDIO=1, VIDEO=2, PODCAST=4, AUDIOBOOK=8). Use `raw & 4` to test for podcast.
- **Podcast artist/album fallback in gpod.py**: for podcast/audiobook tracks where both `albumartist` and `artist` are null, `_parse` uses `t.get("album")` (the show name) as the artist key.
- **Podcast key matching**: `track.album` in source library track objects must equal the iPod's `album` field (the show name). Do not overwrite `album` with year/season in `_remap_podcast`.
- **`filteredSrcArtists` null guard**: returns `[]` when `selectedSourceObj` is null.
- **Podcast artwork — `t.artwork` flag bypass**: `gpod-ls` reports `artwork: false` for podcast tracks even when an APIC frame is embedded. `artUrl()` only gates on `t.artwork` for `mediaType === 'music'`; for podcasts/audiobooks it always attempts the request.
- **`ipod_track_playcounts` shared across tabs**: `persist_playcounts` stores all tracks (including 0 plays). Source tab: `played: bool | None` — `null` means no record, `false` means 0 plays, `true` means played.
- **Podcast sort newest-first**: season/year column key: `(1 if year<=0 else 0, -year, name)` — unknown years fall to end. Episode sort for source: `pub_date` descending then `track_nr` descending. Device track sort: `track_nr` descending, 0/missing at end.
- **`gpod-verify` uses `-M`, not env var**: construct command as `["gpod-verify", "-M", mount, flag]`. Do not pass the mount via `IPOD_MOUNT_POINT`.
- **Tar download streaming**: OS pipe + daemon thread writing `tarfile.open(mode='w|')`; async handler reads from read end. No temp files; OS pipe provides backpressure. Does not check `is_busy()` — read-only.
- **Filesystem check (`check_fs`) flow**: `POST /library/check-fs` → `op_service.run_check_fs` → `_do_check_fs`. Steps: `sync`, `umount`, `device_service.detach_for_fsck` (removes from tracking + adds to `_ejected` to block poll-loop remount), run `fsck.hfsplus -f` or `fsck.vfat -a`, `device_service.reattach_after_fsck` (clears `_ejected`, calls `_do_mount`, re-registers `DeviceInfo`, reloads library). Supported fstypes: `hfsplus`, `hfs`, `vfat`, `msdos`.
- **SVG icons**: all UI icons are inline SVG (Material Design paths), not Unicode characters or emoji. The `.ctrl-btn` uses `display: inline-flex; align-items: center; justify-content: center` so SVG children are vertically centred.
- **Play-cell click toggle**: `.t-play-cell` click handler checks `isCurrentTrack(t)` — if the track is already playing, clicking calls `togglePlay()` rather than re-queuing the track.
- **Artwork cache key**: `GET /artwork/album` looks up by `_ns(albumartist)` + `_ns(album)`. `artUrl()` sends `album.albumartist` as the artist param; `get_ipod_cached_library` backfills missing `albumartist` on stale cached JSON to prevent empty-artist lookups.
- **WALKMAN probe-mount**: `_probe_walkman_cap` mounts to a `tempfile.mkdtemp` dir with `ro` option, reads XML, always unmounts in `finally`. Skipped for ejected devices. Sets `_walkman_meta[devnode]` so `ensure_mounted` skips re-detection.
