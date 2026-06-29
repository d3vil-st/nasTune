function sourcesModule() {
  return {
    // Sources state
    viewMode: 'library',
    mediaType: localStorage.getItem('nastune-media-type') || 'music',
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
    addSourceNameEdited: false,
    addSourceType: 'music',
    browsePath: '/',
    browseDirs: [],
    browseParent: null,
    _srcTrackMap: null,
    _srcPollTimer: null,
    srcShowUnsynced: localStorage.getItem('nastune-src-unsynced') === '1',

    get filteredSources() {
      return this.sources.filter(s => s.type === this.mediaType);
    },

    get srcLabels() {
      if (this.mediaType === 'audiobook') return { artist: 'Authors', allArtists: 'All Authors', album: 'Books',   albumPl: 'books',   trackSg: 'chapter', trackPl: 'chapters', trackLabel: 'Chapters', selectArtist: 'Select an author' };
      if (this.mediaType === 'podcast')   return { artist: 'Shows',   allArtists: 'All Shows',   album: 'Seasons', albumPl: 'seasons', trackSg: 'episode', trackPl: 'episodes', trackLabel: 'Episodes', selectArtist: 'Select a show' };
      return                                     { artist: 'Artists', allArtists: 'All Artists', album: 'Albums',  albumPl: 'albums',  trackSg: 'track',   trackPl: 'tracks',   trackLabel: 'Tracks',   selectArtist: 'Select an artist' };
    },

    get selectedSourceObj() {
      return this.filteredSources.find(s => s.id === this.selectedSourceId) || null;
    },

    async setMediaType(type) {
      if (this.mediaType === type) return;
      // Remember the current source for the type we're leaving
      if (this.selectedSourceId != null) {
        localStorage.setItem('nastune-src-' + this.mediaType, String(this.selectedSourceId));
      }
      this.mediaType = type;
      localStorage.setItem('nastune-media-type', type);
      this.selectedArtist = null;
      this.selectedAlbum = null;
      this.srcArtist = null;
      this.srcAlbum = null;
      // Restore the previously selected source for this type (if it still exists)
      const savedId = parseInt(localStorage.getItem('nastune-src-' + type) || '', 10);
      const match = !isNaN(savedId) && this.filteredSources.find(s => s.id === savedId);
      if (match) {
        await this.pickSource(savedId);
      } else {
        this.selectedSourceId = null;
        this.sourceLibrary = null;
        this._srcKeyMap = null;
      }
    },

    get scanningSource() {
      const sel = this.selectedSourceObj;
      if (sel && sel.scan_status === 'scanning') return sel;
      return this.sources.find(s => s.scan_status === 'scanning') || null;
    },

    get filteredSrcArtists() {
      if (!this.selectedSourceObj) return [];
      let artists = this.sourceLibrary?.artists || [];
      if (this.search) {
        const q = this.search.toLowerCase();
        artists = artists.filter(a =>
          a.name.toLowerCase().includes(q) ||
          a.albums.some(al =>
            al.name.toLowerCase().includes(q) ||
            al.tracks.some(t => t.title.toLowerCase().includes(q))
          )
        );
      }
      if (this.srcShowUnsynced && this.library) {
        artists = artists.filter(a =>
          a.albums.some(al => al.tracks.some(t => !this.isOnDevice(t)))
        );
      }
      return artists;
    },

    get srcCurrentAlbums() {
      if (!this.sourceLibrary) return [];
      const artists = this.sourceLibrary.artists;
      let albums;
      if (this.srcArtist === '__ALL__') {
        albums = artists.flatMap(a => a.albums);
        if (this.search) {
          const q = this.search.toLowerCase();
          albums = albums.filter(al =>
            al.name.toLowerCase().includes(q) ||
            al.tracks.some(t => t.title.toLowerCase().includes(q))
          );
        }
      } else {
        if (!this.srcArtist) return [];
        const a = artists.find(a => a.name === this.srcArtist);
        if (!a) return [];
        albums = a.albums;
        if (this.search) {
          const q = this.search.toLowerCase();
          if (!a.name.toLowerCase().includes(q)) {
            albums = albums.filter(al =>
              al.name.toLowerCase().includes(q) ||
              al.tracks.some(t => t.title.toLowerCase().includes(q))
            );
          }
        }
      }
      if (this.srcShowUnsynced && this.library) {
        albums = albums.filter(al => al.tracks.some(t => !this.isOnDevice(t)));
      }
      return albums;
    },

    get srcCurrentTracks() {
      if (!this.srcAlbum) return [];
      const al = this.srcCurrentAlbums.find(a => a.name === this.srcAlbum);
      if (!al) return [];
      let tracks = al.tracks;
      if (this.search) {
        const q = this.search.toLowerCase();
        const artistMatches = this.srcArtist && this.srcArtist.toLowerCase().includes(q);
        if (!artistMatches && !al.name.toLowerCase().includes(q)) {
          tracks = tracks.filter(t => t.title.toLowerCase().includes(q));
        }
      }
      if (this.srcShowUnsynced && this.library) {
        tracks = tracks.filter(t => !this.isOnDevice(t));
      }
      return tracks;
    },

    async loadSources() {
      try {
        const r = await this.apiFetch('/sources');
        this.sources = await r.json();
        if (this.selectedSourceId) {
          const still = this.sources.find(s => s.id === this.selectedSourceId);
          if (!still) { this.selectedSourceId = null; this.sourceLibrary = null; this._srcKeyMap = null; }
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
      // Keep sourceLibrary visible while the new one loads — avoids the blank flash.
      // _loadSourceLibrary replaces it atomically when the fetch completes.
      this._srcKeyMap = null;
      this._deviceIndex = null;
      this._deviceMap = null;
      await this._loadSourceLibrary(id);
      // Only poll if the source is actually scanning; polling an already-done source
      // unconditionally caused a spurious _loadSourceLibrary call 2 s later.
      if (this.sources.some(s => s.scan_status === 'scanning')) this._startSrcPoll();
    },

    async _loadSourceLibrary(id) {
      this.sourceLibraryLoading = true;
      try {
        const r = await this.apiFetch('/sources/' + id + '/library');
        if (!r.ok) return;
        const data = await r.json();
        // Guard: user may have switched away while this fetch was in flight.
        if (this.selectedSourceId !== id) return;
        this.sourceLibrary = data;
        this._srcKeyMap = null;
        this._buildSrcTrackMap();
        this._initSrcChecked();
      } catch (e) { console.error('loadSourceLibrary:', e); }
      finally { this.sourceLibraryLoading = false; }
    },

    _startSrcPoll() {
      if (this._srcPollTimer) return;
      this._srcPollTimer = setInterval(async () => {
        const wasScanning = this.sources.some(s => s.scan_status === 'scanning');
        await this.loadSources();
        const scanning = this.sources.some(s => s.scan_status === 'scanning');
        if (!scanning) {
          clearInterval(this._srcPollTimer);
          this._srcPollTimer = null;
          // Only reload the library if a scan actually finished this tick.
          if (wasScanning && this.selectedSourceId) await this._loadSourceLibrary(this.selectedSourceId);
        }
      }, 2000);
    },

    async addSource() {
      const name = this.addSourceName.trim();
      if (!name) return;
      try {
        const r = await this.apiFetch('/sources', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, path: this.browsePath, type: this.addSourceType }),
        });
        if (!r.ok) { const d = await r.json().catch(()=>({})); alert(d.detail || 'Failed to add source'); return; }
        const data = await r.json();
        this.showAddSource = false;
        this.addSourceName = '';
        this.addSourceNameEdited = false;
        this.addSourceType = this.mediaType;
        await this.loadSources();
        this._startSrcPoll();
        this.pickSource(data.id);
      } catch (e) { alert('Add source failed: ' + e.message); }
    },

    async deleteSource(id) {
      if (!confirm('Remove this source? Track data will be deleted.')) return;
      try {
        await this.apiFetch('/sources/' + id, { method: 'DELETE' });
        if (this.selectedSourceId === id) {
          this.selectedSourceId = null;
          this.sourceLibrary = null;
          this._srcKeyMap = null;
          this.srcArtist = null;
          this.srcAlbum = null;
        }
        await this.loadSources();
      } catch (e) { alert('Delete failed: ' + e.message); }
    },

    async rescanSource(id, full = false) {
      if (full && !confirm('Re-read all tags in this source? Existing track data will be cleared and the full scan may take several minutes.')) return;
      try {
        await this.apiFetch('/sources/' + id + '/scan' + (full ? '?full=true' : ''), { method: 'POST' });
        await this.loadSources();
        this._startSrcPoll();
      } catch (e) { alert('Rescan failed: ' + e.message); }
    },

    async browseDir(path) {
      try {
        const r = await this.apiFetch('/sources/browse?path=' + encodeURIComponent(path));
        if (!r.ok) { alert('Cannot browse: ' + path); return; }
        const data = await r.json();
        this.browsePath = data.path;
        this.browseParent = data.parent;
        this.browseDirs = data.dirs;
        if (!this.addSourceNameEdited) this.addSourceName = data.path.split('/').filter(Boolean).pop() || data.path;
      } catch (e) { console.error('browseDir:', e); }
    },

    pickSrcArtist(name) {
      this.srcArtist = name;
      this.srcAlbum = null;
      this.srcAlbumArtUrl = null;
      this.srcSelectedTrack = null;
      if (name !== '__ALL__' && window.innerWidth > 768) {
        const first = this.srcCurrentAlbums[0];
        if (first) this.pickSrcAlbum(first.name);
      }
    },

    pickSrcAlbum(name) {
      this.srcAlbum = name;
      this.srcAlbumArtUrl = null;
      this.srcSelectedTrack = null;
      const al = this.srcCurrentAlbums.find(a => a.name === name);
      const url = this.sourceArtUrl(al);
      if (url) this.srcAlbumArtUrl = url;
    },

    openSrcDetail(t) {
      this.srcSelectedTrack = this.srcSelectedTrack?.id === t.id ? null : t;
    },

    async setSrcRating(track, stars) {
      if (!track) return;
      const r = await fetch('/sources/rate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ path: track.path, rating: stars }),
      });
      if (r.ok) track.rating = stars;
    },

    sourceArtUrl(album) {
      if (!album) return '';
      const albumartist = album.albumartist || album.tracks?.[0]?.albumartist || '';
      // For podcasts/audiobooks _build_library sets album.name to the year/display key,
      // but the actual album tag (show name) is on the track — use that for cache lookup.
      const albumName = album.tracks?.[0]?.album || album.name || '';
      if (!albumartist && !albumName) return '';
      return '/artwork/album?artist=' + encodeURIComponent(albumartist) +
             '&album=' + encodeURIComponent(albumName);
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

    // Return the minimal set of paths (files or dirs) covering exactly `tracks`.
    // Checks up to 3 ancestor levels: leaf dir (CD), album dir, artist dir.
    // A dir is used when every library track under it is in the wanted set.
    _buildCopyPaths(tracks) {
      if (!tracks.length) return [];

      const wanted = new Set(tracks.map(t => t.path));
      const dirname = p => p.substring(0, p.lastIndexOf('/'));

      // Collect all library paths and per-directory actual file counts
      const allPaths = [];
      const dirFileCounts = new Map(); // level-1 dir -> total files on disk (incl. non-audio)
      for (const artist of (this.sourceLibrary?.artists || []))
        for (const album of artist.albums)
          for (const t of album.tracks) {
            allPaths.push(t.path);
            if (t.dir_file_count) {
              const d = dirname(t.path);
              if (!dirFileCounts.has(d)) dirFileCounts.set(d, t.dir_file_count);
            }
          }

      if (!allPaths.length) return [...wanted];

      // Build coverage map: dir -> { total, wanted } at up to 3 ancestor levels
      const coverage = new Map();
      for (const path of allPaths) {
        let p = path;
        for (let level = 0; level < 3; level++) {
          p = dirname(p);
          if (!p) break;
          if (!coverage.has(p)) coverage.set(p, { total: 0, wanted: 0 });
          const c = coverage.get(p);
          c.total++;
          if (wanted.has(path)) c.wanted++;
        }
      }

      // Sum actual file counts for all known level-1 dirs under a given directory
      const subtreeFileCount = dir =>
        [...dirFileCounts.entries()]
          .filter(([d]) => d === dir || d.startsWith(dir + '/'))
          .reduce((sum, [, n]) => sum + n, 0);

      // Dirs where every track is selected and no extra non-audio files exist in subtree
      const completeDirs = [...coverage.entries()]
        .filter(([dir, c]) => {
          if (c.wanted === 0 || c.wanted !== c.total) return false;
          if (dirFileCounts.size > 0) {
            const actual = subtreeFileCount(dir);
            if (actual > 0 && actual !== c.total) return false;
          }
          return true;
        })
        .map(([dir]) => dir)
        .sort((a, b) => a.split('/').length - b.split('/').length);

      // Greedy: pick highest-level dir, skip descendants already covered
      const selectedDirs = [];
      const covered = new Set();
      for (const dir of completeDirs) {
        if (selectedDirs.some(d => dir === d || dir.startsWith(d + '/'))) continue;
        selectedDirs.push(dir);
        for (const path of wanted)
          if (path.startsWith(dir + '/')) covered.add(path);
      }

      const result = selectedDirs.map(d => d + '/');
      for (const path of wanted)
        if (!covered.has(path)) result.push(path);
      return result;
    },
  };
}
