# Changelog

All notable changes to nasTune are documented here, newest first.

---

## [v0.10.2] — 2026-11-02

### Added
- **Skip FLAC transcoding** setting (global + per-iPod override) for firmware with native FLAC playback — FLAC is copied as-is via `gpod-cp --native-flac`; takes precedence over Force AAC; FLAC above 48 kHz or surround is still transcoded

### Fixed
- **Force AAC now actually re-encodes ALAC** — gpod-cp only transcodes formats the iPod can't play, so ALAC sources were copied byte-for-byte regardless of `--encoder`. gpod-utils bumped to 1.6.1 (`--transcode-lossless`); Force AAC now re-encodes all lossless sources (ALAC, FLAC, WAV…) to AAC 256 kbps, while MP3/AAC sources are still copied as-is (no lossy-to-lossy re-encode). The size estimate follows the same rules

---

## [v0.10.1] — 2026-11-02

### Added
- Missing `iPod_Control/Device/SysInfoExtended` is read off the device with libgpod's `ipod-read-sysinfo-extended` (image now installs `libgpod-common`) before the library loads and before initializing a blank iPod — libgpod needs it to identify the model and hash the iTunesDB, and iPods restored or never synced with iTunes lack it

### Fixed
- Sync size estimate ignored a per-device Force AAC override: the device's `ipod_db_id` reaches the browser only on a later devices push, and the USB-serial fallback can be missing, so the lookup fell back to the global setting. It now also matches the loaded library's UUID
- Static JS/CSS are cache-busted with the build version (`?v=`), so browsers don't keep running a previous release's scripts after an upgrade

---

## [v0.10.0] — 2026-11-02

### Changed
- **AAC transcoding uses ffmpeg 9.1's native encoder** — gpod-utils bumped to 1.6.0 in both Dockerfiles; its packages statically link ffmpeg 9.1, whose rewritten AAC encoder (NMR coder) is on par with fdk-aac. Lossy and Force-AAC transcodes now run `gpod-cp --encoder aac-ffmpeg --encoder-quality cbr256`
- Sync copy size is estimated from track duration at 256 kbps for tracks that will be transcoded to AAC (Force AAC on, or OGG/WMA/Opus sources) instead of the source file size; the storage bar, out-of-space warning, and sync dialog (shown as `≈`) use it. Per-device Force AAC overrides are honoured

### Fixed
- Force AAC produced MP3 files: the image's ffmpeg has no libfdk_aac, and gpod-cp ≤ 1.5.6 silently fell back to MP3 even with `--disable-encoder-fallback`
- `--encoder-quality 9` was ignored by gpod-cp (it only parsed `vbrN`/`cbrN`); now replaced by an explicit `cbr256`

---

## [v0.9.2] — 2026-07-04

### Added
- In-app changelog viewer — click the build version in the status bar

---

## [v0.9.1] — 2026-07-04

### Fixed
- gpod-utils bumped to 1.5.4 in both Dockerfiles
- Offline/disconnected device library load never recomputed the Sources pane's "already on device" checkboxes — `browseOfflineLibrary()` was missing the `_initSrcChecked()` call that the live-device select path already had

---

## [v0.9.0] — 2026-07-03

### Added
- **Automatic iTunesDB backups** — every write op (sync, delete, playlist edit/delete, smart playlist, verify) gzips `iTunesDB` to `/data/itunesdb_backups/{device}/` before touching it, keeping the last 10; device settings lists snapshots with a Restore button (itself undoable — restore backs up the current DB first)
- **Incremental pre-sync rescan** — both auto-sync paths rescan the sources referenced by a device's enabled sync rules before computing what to copy, so files added since the last manual scan are no longer silently missed
- **Blank iPod initialization** — a freshly formatted/restored device with no iTunesDB is detected and offered an "Initialize iPod" action (`gpod-cp -I`); picker shows a "Not initialized" badge
- **Rename iPod** — a "Device name" field in device settings renames the master playlist (the name shown on the iPod itself)
- **Album tile/grid view** — list/grid toggle in both device and source panes; grid mode is a two-column layout where picking an album replaces the cover wall with the track list, with a back row to return

### Changed
- Device picker rows show a device-type icon and mounted-status dot; WALKMAN storage type is promoted into the name instead of trailing a serial number
- Destructive actions (Remove device, Full rescan, Drop artwork cache) visually separated from routine ones; Rescan is a single button running the incremental scan directly, with Full rescan in its dropdown
- Settings modal footers pinned outside the scrollable body so they no longer clip on short viewports; Maintenance section collapses by default
- Plex "Import ratings" button moved from the settings modal to the sources bar (Playlists tab)
- Header stats condensed back to one line; storage bar redesigned as a gauge with quarter-capacity tick marks and a colored delta chip
- Symbol-only artist names ("#####") now sort after real names in all three library builders (iPod, WALKMAN, sources), Unicode-aware
- **Auto-mount setting removed** — mount-on-demand (select, library load, auto-sync-on-connect) already covers every flow; the setting's only distinct effect was mounting unrelated USB sticks on plug-in

---

## [v0.8.0] — 2026-07-03

### Added
- Device playlists are now fully editable regardless of who created them — add/remove tracks on regular playlists, delete a playlist, or adopt an iTunes-created smart playlist by reading its rules off the device
- Podcast played state syncs across devices and Plex via a unified track-key builder; episodes played elsewhere get `gpod-tag --playcount` on next sync, clearing the on-screen unplayed bullet
- Per-iPod "auto-sync on connect" toggle — fires headlessly from the poll loop via USB serial → known UUID, before mounting

### Fixed
- Sync rules carry their media type through auto-sync (`--tracks-media-type`) so podcasts/audiobooks no longer land as music
- Backend-initiated ops now invalidate the device library cache in every op's `finally` block, so synced items no longer show as not-on-device until a manual refresh
- Plex connections self-heal: a stale `plex.direct` URL is re-resolved and retried once on connect error
- Freshly connected identifiable devices become active in the UI automatically without stealing focus mid-op or auto-selecting random USB sticks

---

## [v0.7.1] — 2026-07-02

### Changed
- Smart playlists are now written natively into the iTunesDB as real SPL rules (`gpod-playlist -S`, gpod-utils 1.5.1) instead of static Python-evaluated snapshots — visible and editable in iTunes; every sync ends with a full smart-playlist member refresh

---

## [v0.7.0] — 2026-07-02

### Added
- **Plex integration** — PIN OAuth server link, admin-gated sign-in gate with pending-user approval, ratings/play-count import, two-way rating push, path-first track matching
- **Playlists as a fourth media type** — browse device playlists, sync a Plex playlist to the iPod, publish a device playlist (including On-The-Go) back to Plex
- **Smart playlists** with an iTunes-style rule editor (17 fields, live match preview)
- Runtime "Auto-mount devices" setting, superseding the `IPOD_AUTOMOUNT` env var (later removed in v0.9.0)

---

## [v0.6.1] — 2026-06-30

### Added
- Keyboard navigation (arrow keys + space) across artists/albums/tracks in both device and source panes
- Format labels (MP3/FLAC/ALAC/AAC/WAV/AIFF/OGG) get a distinct color per theme
- Artist name shown above album name in the middle column when "All Artists" is selected

### Fixed
- Podcast/audiobook grouping uses the album tag (show name) consistently across `gpod.py`, the scanner, `sources.py`, and both device/source JS maps — the iPod itself navigates podcasts by album only, not artist
- Offline library preserved when an unrelated USB device is removed; an offline-browsed device auto-switches to live view when it reconnects
- gpod-utils bumped to 1.4.21

---

## [v0.6.0] — 2026-06-29

### Added
- **Sony WALKMAN support** — on-demand mount/unmount with ref-counting, probe-mount to identify Internal vs SD-card LUN via capability XML, dual-LUN devices tracked independently, offline browsing from SQLite without mounting
- **Persistent artwork cache** with ffmpeg extraction and ref-counted ownership across devices/sources; "Drop artwork cache" button in settings
- **Filesystem check** (`fsck.hfsplus` / `fsck.vfat`) for iPod volumes

### Fixed
- Podcast publication dates read via `ffprobe` (XMP) instead of mutagen's TDRC, which often holds the download timestamp rather than the real pub date
- Op log `from=` query param handling, duplicate log lines on modal re-open, disconnected-WALKMAN offline browsing
- gpod-utils bumped to 1.4.18, then 1.4.20

---

## [v0.5.4] — 2026-06-28

### Fixed
- CPU spikes from SSE polling and op log rendering — cached selection/sync computations, in-place mutation of running ops instead of object replacement, removed a CSS animation that forced 60fps VSync, replaced `x-for` log rendering with direct DOM append
- Multi-disc track matching now uses title instead of track number when `disc_nr > 1`
- Op history loads on init and device switch so a completed log survives a page reload

---

## [v0.5.3] — 2026-06-28

### Fixed
- Eject now unmounts before removing the device from tracked state, preventing a remount flash when the device reports busy

---

## [v0.5.2] — 2026-06-28

### Fixed
- Verify operation shows "Verifying…/Verify" in the status bar instead of "Syncing…/Sync"

---

## [v0.5.1] — 2026-06-28

### Added
- `gpod-verify` exposed as a tracked operation (`check`/`add`/`delete` modes) in device settings
- Podcast publication date column (`pub_date`) with locale-aware, UTC-safe formatting
- Unplayed-episode indicator backed by a new `ipod_track_playcounts` table mirroring the ratings table
- Podcasts sort newest-first by publication date / track number

### Fixed
- Podcast/audiobook artwork now displays on device — `gpod-ls` reports `artwork=false` for podcasts even with an embedded cover, so the flag is now only trusted for music
- gpod-utils bumped to 1.4.12

---

## [v0.5.0] — 2026-06-27

### Added
- **Media type system** — Music / Podcasts / Audiobooks tabs in both device and source panes, with per-type terminology and per-type source memory
- Podcast and audiobook tag remapping so shows group correctly by album (show name) instead of episode-author artist tags
- Per-device settings: `force_aac` override and per-media-type sync rules, with an auto-sync button that computes unsynced paths
- **Smart audio encoder selection** — lossless sources go to ALAC, lossy sources to fdk-aac, MP3/AAC M4A pass through unchanged; `Dockerfile.non-free` rebuilds ffmpeg with `--enable-libfdk-aac` for personal use

### Fixed
- Directory-path collapsing no longer sends non-audio files (cover.jpg etc.) to `gpod-cp` — a directory is only collapsed when its audio track count matches its actual file count

---

## [v0.4.0] — 2026-06-23

### Added
- **Track rating persistence** — iPod track ratings (1–5 stars) are read from `gpod-ls` output on every library load and upserted into a new `ipod_track_ratings` SQLite table; highest rating seen across multiple reads wins on conflict
- **Rating sync during sync** — after copy/delete, `gpod-ls` is run to get fresh track IDs, then `gpod-tag --rating <stars> <id…>` is called for every iPod track whose stored rating is higher than its current iPod rating; skipped entirely (no `gpod-ls`) when no ratings are stored
- **Inline star picker** — both the iPod and NAS source track lists now show a clickable 1–5 star picker directly in the track row; hover previews which stars will be set; clicking the active star clears the rating; `@click.stop` prevents the row click from opening the detail panel
- **Source tab ratings** — the source library (`GET /sources/{id}/library`) now includes a `rating` field (0–5) for each track, populated from `ipod_track_ratings` by normalized track key; the rating column is shown in the source track list and detail panel
- **`POST /library/rate`** — sets an iPod track rating in `ipod_track_ratings` and updates the in-memory library cache; `gpod-tag` is deferred to the next sync (no immediate iTunesDB write)
- **`POST /sources/rate`** — sets a rating for a NAS source track by file path; computes the normalized key from `source_tracks` metadata and upserts `ipod_track_ratings`; the same table is shared with the iPod tab so a rating set in one view carries over to the other
- **`services/track_key.py`** — Python port of the JS `_normStr` / `_trackKey` normalization used by sync matching; shared by rating persistence, rating sync, and both rate endpoints
- **`services/ratings.py`** — `persist_ratings(lib)` and `ipod_rating_to_stars(r)` helpers called after every successful `gpod-ls` run

---

## [v0.3.2] — 2026-06-21

### Fixed
- Build version now appears in the right end of the status bar where it is always visible (previous placement used `position: fixed` with a lower z-index than the header, so it was permanently hidden behind it)
- Status bar right section redesigned to eliminate large gaps between op text and timestamps: op indicators are now naturally sized (no `flex-grow`) with `justify-content: flex-end` stacking them against the right edge
- Last-op summary text capped at `max-width: 200px` with ellipsis; running op track name capped at `max-width: 160px` — prevents layout jumps when long track names appear during a sync

---

## [v0.3.1] — 2026-06-19

### Changed
- Build version repositioned to a fixed top-right overlay (superseded by v0.3.2)

### Fixed
- Docker build cache bust: ARG `BUILD_VERSION` placement corrected so layer cache is not invalidated unnecessarily on every build

---

## [v0.3.0] — 2026-06-18

### Added
- **Build version display** — the version string is shown in the UI header and injected at Docker build time via `ARG BUILD_VERSION`. The CI workflow computes it with `git describe --tags --always --dirty=-dirty` so tagged releases show the bare tag, post-tag commits show `vX.Y.Z-N-gSHA`, and dirty trees append `-dirty`. Local builds without the arg default to `dev`.

---

## [v0.2.9] — 2026-06-17

### Added
- **Full rescan** — WALKMAN devices and NAS sources now have a dedicated full-rescan action that clears all existing library data and re-reads every file from scratch; a confirmation dialog is shown before starting since rescans can take minutes

### Fixed
- Source presence highlighting (`.not-in-src` blue text) no longer flashes blank during a source switch — `pickSource()` no longer nulls out `sourceLibrary` before the fetch; the old library stays visible until replaced atomically
- `_srcKeyMap` is properly invalidated on source switch, source removal, and library reload so stale source-vs-device comparisons are not shown
- iPod detection (`is_ipod`, `IPOD_SENTINEL`, `log_mount_contents`) extracted into `app/services/ipod.py`, mirroring the WALKMAN service module structure

---

## [v0.2.8] — 2026-06-16

### Added
- **Sync confirmation dialog** — shown automatically when the sync includes deletes or when the files to copy exceed free space (accounting for space freed by the deletes that run first); displays a space warning in orange with a "Sync anyway" button for the insufficient-space case; skipped entirely when there are no deletes and enough free space
- **Source presence highlighting** — tracks in the device pane that are absent from the selected NAS source appear in blue (`.not-in-src`); parent album and artist rows turn blue as soon as even one track is missing; uses the same `_trackKey` normalization as sync matching

### Fixed
- WALKMAN fast ops (delete/sync completing in under 250 ms) now reliably trigger a library refresh and op history reload — the SSE handler uses a `connectedAt` timestamp instead of checking for a prior op state, which was `undefined` on first connect
- Storage bar net-change label simplified to a single signed number (green for net add, red for net reduction)

---

## [v0.2.7] — 2026-06-14

### Fixed
- WALKMAN library and op history now refresh correctly after a delete or sync completes
- Various UI/UX polish: spacing, alignment, and state transitions across device and source panes

---

## [v0.2.6] — 2026-06-13

### Added
- **Sony WALKMAN support** — devices are detected by `default-capability.xml` at the mount root; model, serial, and storage type (INTERNAL / CARD) are parsed from the XML. Dual-LUN devices (internal + SD card as separate USB mass-storage units) are each tracked independently by `serial + storage_type`
- WALKMAN library scanning via mutagen — indexes all audio files into SQLite (`walkman_devices`, `walkman_tracks` tables); incremental by default (only new files read), full rescan available
- WALKMAN delete and sync via `shutil.remove` / `shutil.copy2` — SQLite library updated immediately on completion, no rescan required
- WALKMAN scan progress shown in the status bar with live file counter
- VFAT WALKMAN mounts use `utf8` / `iocharset=utf8` mount options to handle non-ASCII filenames

---

## [v0.2.5] — 2026-06-11

### Added
- **Seekable ALAC transcoding** — `ffmpeg` output is now cached to a tmpfs file so the browser can seek within ALAC tracks; cache is evicted on track switch/stop and expires after 60 minutes
- **Storage bar animation** — during delete/sync operations a live overlay shows `−N` (red) and `+N` (green) track counts incremented by the SSE handler; resets to normal on completion
- **Auth-expiry detection** — when a reverse proxy (e.g. Authentik) redirects to a login page mid-session, nasTune detects the HTML response and shows a "Session expired" overlay instead of silently breaking

### Changed
- UI color palette muted across storage bar, action buttons, checkboxes, and accent tokens for a less saturated look

### Fixed
- ffmpeg ALAC transcode: added `-f flac` output format flag; stderr is now captured; fallback to direct serve on transcode failure

---

## [v0.2.4] — 2026-06-09

### Changed
- CI: Docker build now pulls the published registry image as a cache layer before building, significantly reducing rebuild times for unchanged layers

---

## [v0.2.3] — 2026-06-08

### Fixed
- After deleting tracks, if the selected artist or album no longer exists in the refreshed library the device pane resets to the top level instead of showing a blank column

---

## [v0.2.2] — 2026-06-07

### Fixed
- On mobile (≤768px), selecting an artist no longer auto-opens the first album — on a single-pane slide layout this caused the album pane to appear immediately without the user navigating there

---

## [v0.2.1] — 2026-06-06

### Added
- nasTune logo (`docs/logo.svg`) and favicon

---

## [v0.2.0] — 2026-06-05

### Added
- **Operation log popup** — click the status bar progress indicator to open a terminal-style modal with live-streaming stdout from the current gpod or WALKMAN operation; historical logs persist across page reloads
- **SSE for operation progress** — replaced polling with a persistent `EventSource` at `/operations/events`; op state is pushed to all connected tabs in real time; device list changes also use SSE
- **Op history** — last 10 finished operations per device are stored as JSON files under `/data/op_history/` and shown in the status bar; persist across page reloads and container restarts
- **All Artists mode** — select "All Artists" at the top of the artist column to browse all albums across every artist in a single flat view; checkboxes, sync, and search all work in this mode
- **Unsynced-only filter** — button in the sources bar to show only tracks not yet on the device; state saved in `localStorage`
- **Search clear button** — `×` button inside the search field resets the query and navigation state
- Screenshots added to README

### Fixed
- Search navigation: clicking an artist from filtered results now correctly shows all their albums when the artist name matched, or only relevant albums when an album/track matched
- All Artists checkboxes correctly pass album objects (not artist name strings) to avoid lookup failures when `selectedArtist === '__ALL__'`

---

## [v0.1.0] — 2026-05-28

Initial release.

### Features
- **iTunes-style 3-pane browser** — artist → album → track columns for the connected iPod library
- **iPod auto-discovery** — polls `lsblk` every 3 s; detects mounted iPods automatically; optional auto-mount (`IPOD_AUTOMOUNT=1`)
- **Audio playback** — play tracks directly in the browser from the iPod or NAS source; ALAC in M4A is transcoded to FLAC on the fly for Firefox compatibility
- **NAS source library** — add one or more directories from your NAS; mutagen scans all audio files into SQLite; tracks already on the iPod are pre-checked
- **Sync** — copy missing tracks from NAS to iPod using `gpod-cp`; directory-path collapsing sends one arg per complete album instead of per-file; progress tracked per-track from streaming output
- **Delete** — remove selected tracks from the iPod via `gpod-rm`; IDs processed in descending order to avoid index shifts
- **Download** — receive selected tracks as a streaming `.tar` archive with restored directory structure (`Artist/[Year] - Album/NN - Title.ext`); no temp files on server
- **Multi-disc album** support — CD separators in the track list; `disc_nr` prefix in track matching key
- **Track matching** — normalized `artist + album + track_nr` key with NFD decomposition, diacritic stripping, and non-alphanumeric collapsing; handles Unicode hyphens, curly quotes, and accented characters
- **Storage bar** — real capacity via `os.statvfs` (flash-mod safe); shows used / net change / free
- **Eject** — sync iTunesDB and unmount safely from the UI
- **Dry-run mode** — `GPOD_DRY_RUN=1` logs all write commands without executing them
- **Responsive layout** — 3-pane browser collapses to single-pane slide view on ≤768px screens; back navigation via column heading tap
- **Light / Dark / Auto theme** — FOUC-prevention inline script; OS preference detection; preference saved in `localStorage`
- **URL state persistence** — tab, artist, album, and device encoded in the hash; reloads and back button restore the view
- **Multi-arch Docker image** — amd64 and arm64 builds via GitHub Actions native runners
- Alpine.js 3.15.12 vendored locally (no CDN dependency)
