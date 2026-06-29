function selectionModule() {
  // Non-reactive closure caches — writing here never triggers Alpine reactive effects.
  // Both Sets are always replaced (never mutated in-place), so === is a valid signal.
  let _selCache  = null;  // { lib, sel, tracks }  for deviceSelectedTracks
  let _syncCache = null;  // { ini, chk, del, cpy, delTracks, cpyTracks }

  return {
    // Selection & operations state
    deviceSelection: new Set(),
    srcChecked: new Set(),
    srcInitialOnIpod: new Set(),
    showDeleteConfirm: false,
    showSyncConfirm: false,
    showOpLog: false,
    currentOp: null,
    opHistory: [],
    historyViewOp: null,
    _opDeleteCount: 0,
    _opCopyCount: 0,

    get lastOp() {
      // Running op: currentOp has live progress; use it.
      if (this.currentOp?.status === 'running') return this.currentOp;
      // Finished op: prefer the history entry — it has the full log.
      // currentOp from SSE no longer carries the log array.
      return this.opHistory[0] || this.currentOp || null;
    },

    get opRunning() { return this.currentOp?.status === 'running'; },

    // ── Storage bar ──────────────────────────────────────────────────

    get deviceSelectedTracks() {
      const lib = this.library;
      const sel = this.deviceSelection;
      if (_selCache && _selCache.lib === lib && _selCache.sel === sel) return _selCache.tracks;
      const out = [];
      for (const artist of (lib?.artists || []))
        for (const album of artist.albums)
          for (const t of album.tracks)
            if (sel.has(t.id)) out.push(t);
      _selCache = { lib, sel, tracks: out };
      return out;
    },

    get deviceSelectedBytes() {
      return this.deviceSelectedTracks.reduce((s, t) => s + (t.size || 0), 0);
    },

    get storageRemoveBytes() {
      if (this.viewMode === 'library') return this.deviceSelectedBytes;
      if (this.viewMode === 'sources') return this.syncDeleteBytes;
      return 0;
    },

    get storageAddBytes() {
      if (this.viewMode === 'sources') return this.syncCopyBytes;
      return 0;
    },

    get storageBasePct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
      const usedAfter = (this.library?.fs_used_gb || 0) - this.storageRemoveBytes / 1073741824;
      let pct = Math.max(0, Math.min(100, (usedAfter / total) * 100));
      // Show projected final state once copy starts; N/M counter provides per-track progress.
      // Avoids reading currentOp.processed every SSE tick (would cascade to O(n) syncCopyTracks).
      if (this.opRunning && this._opCopyCount) {
        pct = Math.min(100, pct + (this.storageAddBytes / 1073741824 / total) * 100);
      }
      return pct;
    },

    get storageRemovePct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
      return Math.min(100, (this.storageRemoveBytes / 1073741824 / total) * 100);
    },

    get storageAddPct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
      // Projected add space is absorbed into storageBasePct while the op is running
      if (this.opRunning && this._opCopyCount) return 0;
      return Math.min(100, (this.storageAddBytes / 1073741824 / total) * 100);
    },

    get storageFreePct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
      const freeGb = total - (this.library?.fs_used_gb || 0)
        + this.storageRemoveBytes / 1073741824
        - this.storageAddBytes / 1073741824;
      return Math.max(0, freeGb);
    },

    // ── iPod selection ───────────────────────────────────────────────

    isTrackSelected(t) { return this.deviceSelection.has(t.id); },

    isAlbumSelected(al) {
      if (!al) return false;
      return al.tracks.length > 0 && al.tracks.every(t => this.deviceSelection.has(t.id));
    },

    isAlbumIndeterminate(al) {
      if (!al) return false;
      const n = al.tracks.filter(t => this.deviceSelection.has(t.id)).length;
      return n > 0 && n < al.tracks.length;
    },

    isArtistSelected(name) {
      const artist = this.library?.artists.find(a => a.name === name);
      if (!artist) return false;
      const all = artist.albums.flatMap(al => al.tracks);
      return all.length > 0 && all.every(t => this.deviceSelection.has(t.id));
    },

    isArtistIndeterminate(name) {
      const artist = this.library?.artists.find(a => a.name === name);
      if (!artist) return false;
      const all = artist.albums.flatMap(al => al.tracks);
      const n = all.filter(t => this.deviceSelection.has(t.id)).length;
      return n > 0 && n < all.length;
    },

    toggleTrack(t, checked) {
      const s = new Set(this.deviceSelection);
      checked ? s.add(t.id) : s.delete(t.id);
      this.deviceSelection = s;
    },

    toggleAlbum(al, checked) {
      if (!al) return;
      const s = new Set(this.deviceSelection);
      al.tracks.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.deviceSelection = s;
    },

    toggleArtist(name, checked) {
      const artist = this.library?.artists.find(a => a.name === name);
      if (!artist) return;
      const s = new Set(this.deviceSelection);
      artist.albums.flatMap(al => al.tracks).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.deviceSelection = s;
    },

    allCurrentTracksSelected() {
      return this.currentTracks.length > 0 && this.currentTracks.every(t => this.deviceSelection.has(t.id));
    },
    someCurrentTracksSelected() {
      return this.currentTracks.some(t => this.deviceSelection.has(t.id));
    },
    toggleAllCurrentTracks(checked) {
      const s = new Set(this.deviceSelection);
      this.currentTracks.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.deviceSelection = s;
    },

    allCurrentAlbumsSelected() {
      return this.currentAlbums.length > 0 &&
        this.currentAlbums.every(al => al.tracks.length > 0 && al.tracks.every(t => this.deviceSelection.has(t.id)));
    },
    someCurrentAlbumsSelected() {
      return this.currentAlbums.some(al => al.tracks.some(t => this.deviceSelection.has(t.id)));
    },
    toggleAllCurrentAlbums(checked) {
      const s = new Set(this.deviceSelection);
      this.currentAlbums.flatMap(al => al.tracks).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.deviceSelection = s;
    },

    allFilteredArtistsSelected() {
      const tracks = this.filteredArtists.flatMap(a => a.albums.flatMap(al => al.tracks));
      return tracks.length > 0 && tracks.every(t => this.deviceSelection.has(t.id));
    },
    someFilteredArtistsSelected() {
      return this.filteredArtists.some(a => a.albums.some(al => al.tracks.some(t => this.deviceSelection.has(t.id))));
    },
    toggleAllFilteredArtists(checked) {
      const s = new Set(this.deviceSelection);
      this.filteredArtists.flatMap(a => a.albums.flatMap(al => al.tracks)).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.deviceSelection = s;
    },

    async downloadSelectedTracks() {
      const tracks = [];
      for (const artist of (this.library?.artists || []))
        for (const album of artist.albums)
          for (const t of album.tracks)
            if (this.deviceSelection.has(t.id))
              tracks.push({
                ipod_path:   t.ipod_path,
                artist:      t.artist || artist.name,
                albumartist: artist.name,
                album:       album.name,
                year:        album.year || t.year || null,
                track_nr:    t.track_nr || null,
                title:       t.title || '',
              });
      if (!tracks.length || !this.selectedDevnode) return;
      try {
        const r = await this.apiFetch('/library/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode, tracks }),
        });
        if (!r.ok) { alert('Download failed'); return; }
        const blob = await r.blob();
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement('a');
        a.href     = url;
        a.download = 'device_export.tar';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (e) { if (e.message !== 'auth_expired') alert('Download failed: ' + e.message); }
    },

    async deleteSelectedTracks() {
      this.showDeleteConfirm = false;
      const ids = this.deviceSelectedTracks.map(t => t.id);
      if (!ids.length || !this.selectedDevnode) return;
      this._opDeleteCount = ids.length;
      this._opCopyCount = 0;
      try {
        const r = await this.apiFetch('/library/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode, track_ids: ids }),
        });
        if (!r.ok) { const d = await r.json().catch(()=>({})); alert(d.detail || 'Delete failed'); return; }
        this.deviceSelection = new Set();
      } catch (e) { if (e.message !== 'auth_expired') alert('Delete failed: ' + e.message); }
    },

    // ── Sources selection ────────────────────────────────────────────

    _initSrcChecked() {
      this._buildDeviceMap();  // always rebuild — library may have changed since last cache fill
      const checked = new Set();
      for (const artist of (this.sourceLibrary?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            if (this._deviceMap.has(this._trackKey(t.artist || artist.name, t.album || album.name, t.track_nr, t.title, t.disc_nr))) {
              checked.add(t.id);
            }
          }
        }
      }
      this.srcChecked = checked;
      this.srcInitialOnIpod = new Set(checked);
    },

    isSrcTrackChecked(t) { return this.srcChecked.has(t.id); },

    isSrcAlbumChecked(al) {
      if (!al) return false;
      return al.tracks.length > 0 && al.tracks.every(t => this.srcChecked.has(t.id));
    },

    isSrcAlbumIndeterminate(al) {
      if (!al) return false;
      const n = al.tracks.filter(t => this.srcChecked.has(t.id)).length;
      return n > 0 && n < al.tracks.length;
    },

    isSrcArtistChecked(name) {
      const all = this._srcArtistTracks(name);
      return all.length > 0 && all.every(t => this.srcChecked.has(t.id));
    },

    isSrcArtistIndeterminate(name) {
      const all = this._srcArtistTracks(name);
      const n = all.filter(t => this.srcChecked.has(t.id)).length;
      return n > 0 && n < all.length;
    },

    toggleSrcTrack(t, checked) {
      const s = new Set(this.srcChecked);
      checked ? s.add(t.id) : s.delete(t.id);
      this.srcChecked = s;
    },

    toggleSrcAlbum(al, checked) {
      if (!al) return;
      const s = new Set(this.srcChecked);
      al.tracks.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.srcChecked = s;
    },

    toggleSrcArtist(name, checked) {
      const all = this._srcArtistTracks(name);
      const s = new Set(this.srcChecked);
      all.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.srcChecked = s;
    },

    allSrcCurrentTracksSelected() {
      return this.srcCurrentTracks.length > 0 && this.srcCurrentTracks.every(t => this.srcChecked.has(t.id));
    },
    someSrcCurrentTracksSelected() {
      return this.srcCurrentTracks.some(t => this.srcChecked.has(t.id));
    },
    toggleAllSrcCurrentTracks(checked) {
      const s = new Set(this.srcChecked);
      this.srcCurrentTracks.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.srcChecked = s;
    },

    allSrcCurrentAlbumsSelected() {
      return this.srcCurrentAlbums.length > 0 &&
        this.srcCurrentAlbums.every(al => al.tracks.length > 0 && al.tracks.every(t => this.srcChecked.has(t.id)));
    },
    someSrcCurrentAlbumsSelected() {
      return this.srcCurrentAlbums.some(al => al.tracks.some(t => this.srcChecked.has(t.id)));
    },
    toggleAllSrcCurrentAlbums(checked) {
      const s = new Set(this.srcChecked);
      this.srcCurrentAlbums.flatMap(al => al.tracks).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.srcChecked = s;
    },

    allSrcArtistsSelected() {
      const tracks = (this.sourceLibrary?.artists || []).flatMap(a => a.albums.flatMap(al => al.tracks));
      return tracks.length > 0 && tracks.every(t => this.srcChecked.has(t.id));
    },
    someSrcArtistsSelected() {
      return (this.sourceLibrary?.artists || []).some(a => a.albums.some(al => al.tracks.some(t => this.srcChecked.has(t.id))));
    },
    toggleAllSrcArtists(checked) {
      const s = new Set(this.srcChecked);
      (this.sourceLibrary?.artists || []).flatMap(a => a.albums.flatMap(al => al.tracks)).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.srcChecked = s;
    },

    // ── Sync ─────────────────────────────────────────────────────────

    get syncToDelete() {
      const ini = this.srcInitialOnIpod, chk = this.srcChecked;
      if (!_syncCache || _syncCache.ini !== ini || _syncCache.chk !== chk) {
        const del = [...ini].filter(id => !chk.has(id));
        const cpy = [...chk].filter(id => !ini.has(id));
        _syncCache = {
          ini, chk, del, cpy,
          delTracks: del.map(id => this._srcTrackById(id)).filter(Boolean),
          cpyTracks: cpy.map(id => this._srcTrackById(id)).filter(Boolean),
        };
      }
      return _syncCache.del;
    },

    get syncToCopy() {
      const ini = this.srcInitialOnIpod, chk = this.srcChecked;
      if (!_syncCache || _syncCache.ini !== ini || _syncCache.chk !== chk) {
        const del = [...ini].filter(id => !chk.has(id));
        const cpy = [...chk].filter(id => !ini.has(id));
        _syncCache = {
          ini, chk, del, cpy,
          delTracks: del.map(id => this._srcTrackById(id)).filter(Boolean),
          cpyTracks: cpy.map(id => this._srcTrackById(id)).filter(Boolean),
        };
      }
      return _syncCache.cpy;
    },

    get syncDeleteTracks() { this.syncToDelete; return _syncCache?.delTracks || []; },
    get syncCopyTracks()   { this.syncToCopy;   return _syncCache?.cpyTracks || []; },
    get syncDeleteBytes()  { return this.syncDeleteTracks.reduce((s, t) => s + (t.size || 0), 0); },
    get syncCopyBytes()    { return this.syncCopyTracks.reduce((s, t) => s + (t.size || 0), 0); },
    get hasSyncChanges()   { return this.syncToDelete.length > 0 || this.syncToCopy.length > 0; },
    get syncSpaceWarning() {
      if (!this.library || !this.syncCopyTracks.length) return false;
      const freeBytes = (this.library.fs_total_gb - this.library.fs_used_gb) * 1024 ** 3;
      return this.syncCopyBytes > freeBytes + this.syncDeleteBytes;
    },
    get syncNeedsConfirm() {
      return this.syncToDelete.length > 0 || this.syncSpaceWarning;
    },

    async confirmSync() {
      this.showSyncConfirm = false;
      if (!this.selectedDevnode) return;
      if (!this._deviceMap) this._buildDeviceMap();
      const deleteIds = this.syncDeleteTracks
        .map(t => { const k = this._trackKey(t.artist || t.albumartist, t.album, t.track_nr, t.title, t.disc_nr); const ipodT = this._deviceMap.get(k); return ipodT?.id; })
        .filter(id => id !== undefined);
      const copyPaths = this._buildCopyPaths(this.syncCopyTracks);
      this._opDeleteCount = deleteIds.length;
      this._opCopyCount = this.syncCopyTracks.length;
      try {
        const r = await this.apiFetch('/library/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode, copy_paths: copyPaths, delete_ids: deleteIds, copy_track_count: this.syncCopyTracks.length, media_type: this.selectedSourceObj?.type || 'music' }),
        });
        if (!r.ok) { const d = await r.json().catch(()=>({})); alert(d.detail || 'Sync failed'); return; }
      } catch (e) { if (e.message !== 'auth_expired') alert('Sync failed: ' + e.message); }
    },

    async loadOpHistory(devnode) {
      if (!devnode) { this.opHistory = []; return; }
      try {
        const r = await this.apiFetch(`/operations/history?devnode=${encodeURIComponent(devnode)}`);
        this.opHistory = r.ok ? await r.json() : [];
      } catch { this.opHistory = []; }
    },

    // Append an array of line strings to opLogEl. Pure DOM — no Alpine reactivity.
    // Returns true if the element was ready; false if it wasn't (caller should retry).
    _appendLogLines(lines) {
      const el = this.$refs?.opLogEl ?? document.querySelector('pre.op-log-body');
      if (!el) return false;
      if (!lines.length) return true;
      const frag = document.createDocumentFragment();
      for (const line of lines) {
        const span = document.createElement('span');
        if (line.startsWith('$')) span.className = 'op-log-cmd';
        span.textContent = line + '\n';
        frag.appendChild(span);
      }
      // Check before appending: if the user scrolled up, don't hijack their position.
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      el.appendChild(frag);
      // Trim oldest lines so the DOM stays bounded and layout cost doesn't grow unbounded.
      const maxLines = 500;
      while (el.childNodes.length > maxLines) el.removeChild(el.firstChild);
      if (atBottom) el.scrollTop = 9999999;
      return true;
    },

    async _pollLiveLog(session) {
      while (this.showOpLog && !this.historyViewOp && this._logSession === session) {
        try {
          const r = await fetch(`/operations/log?from=${this._logRenderedCount || 0}`);
          if (r.ok && this._logSession === session) {
            const { lines, total } = await r.json();
            if (lines.length) {
              this._appendLogLines(lines);
              this._logRenderedCount = total;
            }
          }
        } catch { /* network hiccup — keep polling */ }
        if (!this.opRunning) break;
        await new Promise(res => setTimeout(res, 2000));
      }
    },

    openLiveLog() {
      this.historyViewOp = null;
      this.showOpLog = true;
      this._logRenderedCount = 0;
      this._logSession = (this._logSession || 0) + 1;
      const session = this._logSession;
      // setTimeout gives WebKit one extra event-loop turn to register x-ref
      // after Alpine evaluates the x-if template (needed on iOS Safari).
      setTimeout(() => {
        // Clear stale content — _logRenderedCount reset to 0 means the poll
        // re-fetches from the beginning, so any existing DOM lines would duplicate.
        const el = this.$refs?.opLogEl ?? document.querySelector('pre.op-log-body');
        if (el) el.textContent = '';
        this._pollLiveLog(session);
      }, 0);
    },

    async openLastOpLog() {
      this.historyViewOp = this.lastOp;
      this.showOpLog = true;
      // Wait two ticks: one for Alpine to evaluate x-if, one for WebKit to register x-ref.
      await this.$nextTick();
      await new Promise(res => setTimeout(res, 0));
      if (this.historyViewOp?.log?.length) {
        this._appendLogLines(this.historyViewOp.log);
      } else {
        // History not yet loaded (op just finished) — fetch log from server
        try {
          const r = await fetch('/operations/log?from=0');
          if (r.ok) { const { lines } = await r.json(); this._appendLogLines(lines); }
        } catch { /* ignore */ }
      }
      const el = this.$refs?.opLogEl ?? document.querySelector('pre.op-log-body');
      if (el) el.scrollTop = 9999999;
    },

    _connectOpEvents() {
      // Closure-scoped so Alpine never proxies the EventSource object
      let es = null;
      this._connectOpEvents = () => {
        if (es) return;
        const connectedAt = Date.now() / 1000;
        es = new EventSource('/operations/events');
        es.onmessage = async (evt) => {
          const op = JSON.parse(evt.data);
          const prevStatus = this.currentOp?.status;
          const prevStartedAt = this.currentOp?.started_at;
          const existing = this.currentOp;
          if (existing && existing.started_at === op.started_at &&
              existing.status === 'running' && op.status === 'running') {
            // Mutate in-place: only effects tracking .processed / .current re-run.
            // Replacing the whole object would fire every effect that reads any currentOp property.
            existing.processed = op.processed;
            existing.current = op.current;
          } else {
            this.currentOp = op;
          }
          // Trigger on running→done transition, OR on a new op that completed before
          // the first SSE poll (fast ops like WALKMAN delete). connectedAt guards
          // against spuriously refreshing on pre-existing done ops seen on connect.
          const justFinished = op?.status !== 'running' && (
            prevStatus === 'running' ||
            (op?.started_at != null && op.started_at !== prevStartedAt && op.started_at >= connectedAt)
          );
          if (justFinished) {
            this._opDeleteCount = 0;
            this._opCopyCount = 0;
            this._deviceMap = null;
            this.selectedTrack = null;
            this.srcSelectedTrack = null;
            this.deviceSelection = new Set();
            await this._fetchLibrary(true);
            if (this.selectedSourceId) this._initSrcChecked();
            await this.loadOpHistory(this.selectedDevnode);
          }
        };
        es.onerror = () => { this._probeAuth(); };
      };
      this._connectOpEvents();
    },
  };
}
