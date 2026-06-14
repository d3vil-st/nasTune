function selectionModule() {
  return {
    // Selection & operations state
    ipodSelection: new Set(),
    srcChecked: new Set(),
    srcInitialOnIpod: new Set(),
    showDeleteConfirm: false,
    showSyncConfirm: false,
    currentOp: null,
    opPollTimer: null,

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
      return Math.max(0, Math.min(100, (usedAfter / total) * 100));
    },

    get storageRemovePct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
      return Math.min(100, (this.storageRemoveBytes / 1073741824 / total) * 100);
    },

    get storageAddPct() {
      const total = this.library?.fs_total_gb || 0;
      if (!total) return 0;
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

    isTrackSelected(t) { return this.ipodSelection.has(t.id); },

    isAlbumSelected(artistName, albumName) {
      const artist = this.library?.artists.find(a => a.name === artistName);
      const album = artist?.albums.find(al => al.name === albumName);
      if (!album) return false;
      return album.tracks.length > 0 && album.tracks.every(t => this.ipodSelection.has(t.id));
    },

    isAlbumIndeterminate(artistName, albumName) {
      const artist = this.library?.artists.find(a => a.name === artistName);
      const album = artist?.albums.find(al => al.name === albumName);
      if (!album) return false;
      const n = album.tracks.filter(t => this.ipodSelection.has(t.id)).length;
      return n > 0 && n < album.tracks.length;
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

    toggleAlbum(artistName, albumName, checked) {
      const artist = this.library?.artists.find(a => a.name === artistName);
      const album = artist?.albums.find(al => al.name === albumName);
      if (!album) return;
      const s = new Set(this.ipodSelection);
      album.tracks.forEach(t => checked ? s.add(t.id) : s.delete(t.id));
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

    async deleteSelectedTracks() {
      this.showDeleteConfirm = false;
      const ids = this.ipodSelectedTracks.map(t => t.id);
      if (!ids.length || !this.selectedDevnode) return;
      const r = await fetch('/library/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ devnode: this.selectedDevnode, track_ids: ids }),
      });
      if (!r.ok) { const d = await r.json().catch(()=>({})); alert(d.detail || 'Delete failed'); return; }
      this.ipodSelection = new Set();
      this._startOpPoll();
    },

    // ── Sources selection ────────────────────────────────────────────

    _initSrcChecked() {
      if (!this._ipodMap) this._buildIpodMap();
      const checked = new Set();
      for (const artist of (this.sourceLibrary?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            if (this._ipodMap.has(this._trackKey(t.artist || artist.name, t.album || album.name, t.track_nr, t.title))) {
              checked.add(t.id);
            }
          }
        }
      }
      this.srcChecked = checked;
      this.srcInitialOnIpod = new Set(checked);
    },

    isSrcTrackChecked(t) { return this.srcChecked.has(t.id); },

    isSrcAlbumChecked(artistName, albumName) {
      const al = this._srcAlbum(artistName, albumName);
      if (!al) return false;
      return al.tracks.length > 0 && al.tracks.every(t => this.srcChecked.has(t.id));
    },

    isSrcAlbumIndeterminate(artistName, albumName) {
      const al = this._srcAlbum(artistName, albumName);
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

    toggleSrcAlbum(artistName, albumName, checked) {
      const al = this._srcAlbum(artistName, albumName);
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
        .map(t => { const k = this._trackKey(t.artist, t.album, t.track_nr, t.title); const ipodT = this._ipodMap.get(k); return ipodT?.id; })
        .filter(id => id !== undefined);
      const copyPaths = this.syncCopyTracks.map(t => t.path);
      const r = await fetch('/library/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ devnode: this.selectedDevnode, copy_paths: copyPaths, delete_ids: deleteIds }),
      });
      if (!r.ok) { const d = await r.json().catch(()=>({})); alert(d.detail || 'Sync failed'); return; }
      this._startOpPoll();
    },

    _startOpPoll() {
      if (this.opPollTimer) return;
      this.opPollTimer = setInterval(async () => {
        try {
          const r = await fetch('/operations');
          this.currentOp = r.ok ? await r.json() : null;
          if (!this.currentOp || this.currentOp.status !== 'running') {
            clearInterval(this.opPollTimer);
            this.opPollTimer = null;
            if (this.currentOp?.status === 'done') {
              this._ipodMap = null;
              await this._fetchLibrary();
              if (this.selectedSourceId) this._initSrcChecked();
            }
          }
        } catch (e) { /* ignore */ }
      }, 1000);
    },
  };
}
