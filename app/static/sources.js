function sourcesModule() {
  return {
    // Sources state
    viewMode: 'library',
    sources: [],
    selectedSourceId: null,
    sourceLibrary: null,
    sourceLibraryLoading: false,
    srcArtist: null,
    srcAlbum: null,
    srcAlbumArtUrl: null,
    srcSelectedTrack: null,
    showManageSources: false,
    showAddSource: false,
    showSourcePicker: false,
    addSourceName: '',
    browsePath: '/',
    browseDirs: [],
    browseParent: null,
    _srcTrackMap: null,
    _srcPollTimer: null,

    get selectedSourceObj() {
      return this.sources.find(s => s.id === this.selectedSourceId) || null;
    },

    get scanningSource() {
      const sel = this.selectedSourceObj;
      if (sel && sel.scan_status === 'scanning') return sel;
      return this.sources.find(s => s.scan_status === 'scanning') || null;
    },

    get srcCurrentAlbums() {
      if (!this.srcArtist || !this.sourceLibrary) return [];
      const a = this.sourceLibrary.artists.find(a => a.name === this.srcArtist);
      return a ? a.albums : [];
    },

    get srcCurrentTracks() {
      if (!this.srcAlbum) return [];
      const al = this.srcCurrentAlbums.find(a => a.name === this.srcAlbum);
      return al ? al.tracks : [];
    },

    async loadSources() {
      try {
        const r = await fetch('/sources');
        this.sources = await r.json();
        if (this.selectedSourceId) {
          const still = this.sources.find(s => s.id === this.selectedSourceId);
          if (!still) { this.selectedSourceId = null; this.sourceLibrary = null; }
        }
      } catch (e) { console.error('loadSources:', e); }
    },

    async pickSource(id) {
      this.showSourcePicker = false;
      if (this.selectedSourceId === id) return;
      this.selectedSourceId = id;
      localStorage.setItem('nasTune_selectedSourceId', String(id));
      this.srcArtist = null;
      this.srcAlbum = null;
      this.srcAlbumArtUrl = null;
      this.sourceLibrary = null;
      this._ipodIndex = null;
      this._ipodMap = null;
      await this._loadSourceLibrary(id);
      this._startSrcPoll();
    },

    async _loadSourceLibrary(id) {
      this.sourceLibraryLoading = true;
      try {
        const r = await fetch('/sources/' + id + '/library');
        if (r.ok) {
          this.sourceLibrary = await r.json();
          this._buildSrcTrackMap();
          this._initSrcChecked();
        }
      } catch (e) { console.error('loadSourceLibrary:', e); }
      finally { this.sourceLibraryLoading = false; }
    },

    _startSrcPoll() {
      if (this._srcPollTimer) return;
      this._srcPollTimer = setInterval(async () => {
        await this.loadSources();
        const scanning = this.sources.some(s => s.scan_status === 'scanning');
        if (!scanning) {
          clearInterval(this._srcPollTimer);
          this._srcPollTimer = null;
          if (this.selectedSourceId) await this._loadSourceLibrary(this.selectedSourceId);
        }
      }, 2000);
    },

    async addSource() {
      const name = this.addSourceName.trim();
      if (!name) return;
      try {
        const r = await fetch('/sources', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, path: this.browsePath }),
        });
        if (!r.ok) { const d = await r.json().catch(()=>({})); alert(d.detail || 'Failed to add source'); return; }
        const data = await r.json();
        this.showAddSource = false;
        this.addSourceName = '';
        await this.loadSources();
        this._startSrcPoll();
        this.pickSource(data.id);
      } catch (e) { alert('Add source failed: ' + e.message); }
    },

    async deleteSource(id) {
      if (!confirm('Remove this source? Track data will be deleted.')) return;
      try {
        await fetch('/sources/' + id, { method: 'DELETE' });
        if (this.selectedSourceId === id) {
          this.selectedSourceId = null;
          this.sourceLibrary = null;
          this.srcArtist = null;
          this.srcAlbum = null;
        }
        await this.loadSources();
      } catch (e) { alert('Delete failed: ' + e.message); }
    },

    async rescanSource(id) {
      try {
        await fetch('/sources/' + id + '/scan', { method: 'POST' });
        await this.loadSources();
        this._startSrcPoll();
      } catch (e) { alert('Rescan failed: ' + e.message); }
    },

    async browseDir(path) {
      try {
        const r = await fetch('/sources/browse?path=' + encodeURIComponent(path));
        if (!r.ok) { alert('Cannot browse: ' + path); return; }
        const data = await r.json();
        this.browsePath = data.path;
        this.browseParent = data.parent;
        this.browseDirs = data.dirs;
        if (!this.addSourceName) this.addSourceName = data.path.split('/').filter(Boolean).pop() || data.path;
      } catch (e) { console.error('browseDir:', e); }
    },

    pickSrcArtist(name) {
      this.srcArtist = name;
      this.srcAlbum = null;
      this.srcAlbumArtUrl = null;
    },

    pickSrcAlbum(name) {
      this.srcAlbum = name;
      this.srcAlbumArtUrl = null;
      const al = this.srcCurrentAlbums.find(a => a.name === name);
      const url = this.sourceArtUrl(al);
      if (url) this.srcAlbumArtUrl = url;
    },

    openSrcDetail(t) {
      this.srcSelectedTrack = this.srcSelectedTrack?.id === t.id ? null : t;
    },

    sourceArtUrl(album) {
      if (!album) return null;
      const t = album.tracks && album.tracks[0];
      if (!t) return null;
      return '/sources/artwork?path=' + encodeURIComponent(t.path);
    },

    _buildSrcTrackMap() {
      this._srcTrackMap = new Map();
      for (const artist of (this.sourceLibrary?.artists || []))
        for (const album of artist.albums)
          for (const t of album.tracks)
            this._srcTrackMap.set(t.id, t);
    },

    _srcTrackById(id) {
      if (!this._srcTrackMap) this._buildSrcTrackMap();
      return this._srcTrackMap.get(id) || null;
    },

    _srcAlbum(artistName, albumName) {
      const artist = this.sourceLibrary?.artists.find(a => a.name === artistName);
      return artist?.albums.find(al => al.name === albumName) || null;
    },

    _srcArtistTracks(name) {
      const artist = this.sourceLibrary?.artists.find(a => a.name === name);
      return artist ? artist.albums.flatMap(al => al.tracks) : [];
    },
  };
}
