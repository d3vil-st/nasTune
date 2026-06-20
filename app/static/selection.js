function selectionModule() {
  return {
    // Selection & operations state
    ipodSelection: new Set(),
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
      // In-session finished op takes priority; fall back to persisted history
      if (this.currentOp && this.currentOp.status !== 'running') return this.currentOp;
      return this.opHistory[0] || null;
    },

    get opRunning() { return this.currentOp?.status === 'running'; },

    // ── Storage bar ──────────────────────────────────────────────────

    get ipodSelectedTracks() {
      const out = [];
      for (const artist of (this.library?.artists || []))
        for (const album of artist.albums)
          for (const t of album.tracks)
            if (this.ipodSelection.has(t.id)) out.push(t);
      return out;
    },

    get ipodSelectedBytes() {
      return this.ipodSelectedTracks.reduce((s, t) => s + (t.size || 0), 0);
    },

    get storageRemoveBytes() {
      if (this.viewMode === 'library') return this.ipodSelectedBytes;
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
      // During copy phase, blue grows as files land on device
      if (this.opRunning && this._opCopyCount) {
        const copyDone = Math.max(0, (this.currentOp.processed || 0) - this._opDeleteCount);
        const copyFrac = Math.min(1, copyDone / this._opCopyCount);
        pct = Math.min(100, pct + (this.storageAddBytes / 1073741824 / total) * 100 * copyFrac);
      }
      return pct;
    },

    get storageRemovePct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
      const base = Math.min(100, (this.storageRemoveBytes / 1073741824 / total) * 100);
      if (this.opRunning && this._opDeleteCount) {
        const delFrac = Math.min(1, (this.currentOp.processed || 0) / this._opDeleteCount);
        return base * (1 - delFrac);
      }
      return base;
    },

    get storageAddPct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
      const base = Math.min(100, (this.storageAddBytes / 1073741824 / total) * 100);
      if (this.opRunning && this._opCopyCount) {
        const copyDone = Math.max(0, (this.currentOp.processed || 0) - this._opDeleteCount);
        const copyFrac = Math.min(1, copyDone / this._opCopyCount);
        return base * (1 - copyFrac);
      }
      return base;
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

    isTrackSelected(t) { return this.ipodSelection.has(t.id); },

    isAlbumSelected(al) {
      if (!al) return false;
      return al.tracks.length > 0 && al.tracks.every(t => this.ipodSelection.has(t.id));
    },

    isAlbumIndeterminate(al) {
      if (!al) return false;
      const n = al.tracks.filter(t => this.ipodSelection.has(t.id)).length;
      return n > 0 && n < al.tracks.length;
    },

    isArtistSelected(name) {
      const artist = this.library?.artists.find(a => a.name === name);
      if (!artist) return false;
      const all = artist.albums.flatMap(al => al.tracks);
      return all.length > 0 && all.every(t => this.ipodSelection.has(t.id));
    },

    isArtistIndeterminate(name) {
      const artist = this.library?.artists.find(a => a.name === name);
      if (!artist) return false;
      const all = artist.albums.flatMap(al => al.tracks);
      const n = all.filter(t => this.ipodSelection.has(t.id)).length;
      return n > 0 && n < all.length;
    },

    toggleTrack(t, checked) {
      const s = new Set(this.ipodSelection);
      checked ? s.add(t.id) : s.delete(t.id);
      this.ipodSelection = s;
    },

    toggleAlbum(al, checked) {
      if (!al) return;
      const s = new Set(this.ipodSelection);
      al.tracks.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.ipodSelection = s;
    },

    toggleArtist(name, checked) {
      const artist = this.library?.artists.find(a => a.name === name);
      if (!artist) return;
      const s = new Set(this.ipodSelection);
      artist.albums.flatMap(al => al.tracks).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.ipodSelection = s;
    },

    allCurrentTracksSelected() {
      return this.currentTracks.length > 0 && this.currentTracks.every(t => this.ipodSelection.has(t.id));
    },
    someCurrentTracksSelected() {
      return this.currentTracks.some(t => this.ipodSelection.has(t.id));
    },
    toggleAllCurrentTracks(checked) {
      const s = new Set(this.ipodSelection);
      this.currentTracks.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.ipodSelection = s;
    },

    allCurrentAlbumsSelected() {
      return this.currentAlbums.length > 0 &&
        this.currentAlbums.every(al => al.tracks.length > 0 && al.tracks.every(t => this.ipodSelection.has(t.id)));
    },
    someCurrentAlbumsSelected() {
      return this.currentAlbums.some(al => al.tracks.some(t => this.ipodSelection.has(t.id)));
    },
    toggleAllCurrentAlbums(checked) {
      const s = new Set(this.ipodSelection);
      this.currentAlbums.flatMap(al => al.tracks).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.ipodSelection = s;
    },

    allFilteredArtistsSelected() {
      const tracks = this.filteredArtists.flatMap(a => a.albums.flatMap(al => al.tracks));
      return tracks.length > 0 && tracks.every(t => this.ipodSelection.has(t.id));
    },
    someFilteredArtistsSelected() {
      return this.filteredArtists.some(a => a.albums.some(al => al.tracks.some(t => this.ipodSelection.has(t.id))));
    },
    toggleAllFilteredArtists(checked) {
      const s = new Set(this.ipodSelection);
      this.filteredArtists.flatMap(a => a.albums.flatMap(al => al.tracks)).forEach(t => checked ? s.add(t.id) : s.delete(t.id));
      this.ipodSelection = s;
    },

    async downloadSelectedTracks() {
      const tracks = [];
      for (const artist of (this.library?.artists || []))
        for (const album of artist.albums)
          for (const t of album.tracks)
            if (this.ipodSelection.has(t.id))
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
        a.download = 'ipod_export.tar';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (e) { if (e.message !== 'auth_expired') alert('Download failed: ' + e.message); }
    },

    async deleteSelectedTracks() {
      this.showDeleteConfirm = false;
      const ids = this.ipodSelectedTracks.map(t => t.id);
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
        this.ipodSelection = new Set();
      } catch (e) { if (e.message !== 'auth_expired') alert('Delete failed: ' + e.message); }
    },

    // ── Sources selection ────────────────────────────────────────────

    _initSrcChecked() {
      if (!this._ipodMap) this._buildIpodMap();
      const checked = new Set();
      for (const artist of (this.sourceLibrary?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            if (this._ipodMap.has(this._trackKey(t.artist || artist.name, t.album || album.name, t.track_nr, t.title, t.disc_nr))) {
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
      return [...this.srcInitialOnIpod].filter(id => !this.srcChecked.has(id));
    },

    get syncToCopy() {
      return [...this.srcChecked].filter(id => !this.srcInitialOnIpod.has(id));
    },

    get syncDeleteTracks() { return this.syncToDelete.map(id => this._srcTrackById(id)).filter(Boolean); },
    get syncCopyTracks()   { return this.syncToCopy.map(id => this._srcTrackById(id)).filter(Boolean); },
    get syncDeleteBytes()  { return this.syncDeleteTracks.reduce((s, t) => s + (t.size || 0), 0); },
    get syncCopyBytes()    { return this.syncCopyTracks.reduce((s, t) => s + (t.size || 0), 0); },
    get hasSyncChanges()   { return this.syncToDelete.length > 0 || this.syncToCopy.length > 0; },

    async confirmSync() {
      this.showSyncConfirm = false;
      if (!this.selectedDevnode) return;
      if (!this._ipodMap) this._buildIpodMap();
      const deleteIds = this.syncDeleteTracks
        .map(t => { const k = this._trackKey(t.artist || t.albumartist, t.album, t.track_nr, t.title, t.disc_nr); const ipodT = this._ipodMap.get(k); return ipodT?.id; })
        .filter(id => id !== undefined);
      const copyPaths = this._buildCopyPaths(this.syncCopyTracks);
      this._opDeleteCount = deleteIds.length;
      this._opCopyCount = this.syncCopyTracks.length;
      try {
        const r = await this.apiFetch('/library/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode, copy_paths: copyPaths, delete_ids: deleteIds, copy_track_count: this.syncCopyTracks.length }),
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

    openLiveLog() {
      this.historyViewOp = null;
      this.showOpLog = true;
      this.$nextTick(() => { const el = this.$refs?.opLogEl; if (el) el.scrollTop = el.scrollHeight; });
    },

    openLastOpLog() {
      this.historyViewOp = this.lastOp;
      this.showOpLog = true;
      this.$nextTick(() => { const el = this.$refs?.opLogEl; if (el) el.scrollTop = el.scrollHeight; });
    },

    _connectOpEvents() {
      // Closure-scoped so Alpine never proxies the EventSource object
      let es = null;
      this._connectOpEvents = () => {
        if (es) return;
        es = new EventSource('/operations/events');
        es.onmessage = async (evt) => {
          const op = JSON.parse(evt.data);
          const prevStatus = this.currentOp?.status;
          const prevStartedAt = this.currentOp?.started_at;
          this.currentOp = op;
          if (this.showOpLog && !this.historyViewOp) {
            this.$nextTick(() => {
              const el = this.$refs?.opLogEl;
              if (el) el.scrollTop = el.scrollHeight;
            });
          }
          // Trigger on running→done transition, OR on a new op that completed so fast
          // we never saw it as 'running' (started_at differs from any previously seen op).
          const justFinished = op?.status !== 'running' && (
            prevStatus === 'running' ||
            (op?.started_at != null && op.started_at !== prevStartedAt && prevStartedAt != null)
          );
          if (justFinished) {
            this._opDeleteCount = 0;
            this._opCopyCount = 0;
            this._ipodMap = null;
            this.selectedTrack = null;
            this.srcSelectedTrack = null;
            this.ipodSelection = new Set();
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
