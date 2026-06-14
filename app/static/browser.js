function browserModule() {
  return {
    // Browser state
    search: '',
    selectedArtist: null,
    selectedAlbum: null,
    selectedTrack: null,
    albumArtUrl: null,
    coverPopupUrl: null,
    _ipodIndex: null,
    _ipodMap: null,

    get filteredArtists() {
      const artists = this.library?.artists || [];
      if (!this.search) return artists;
      const q = this.search.toLowerCase();
      return artists.filter(a =>
        a.name.toLowerCase().includes(q) ||
        a.albums.some(al =>
          al.name.toLowerCase().includes(q) ||
          al.tracks.some(t => t.title.toLowerCase().includes(q))
        )
      );
    },

    get currentAlbums() {
      if (!this.selectedArtist) return [];
      const artists = this.library?.artists || [];
      const a = artists.find(a => a.name === this.selectedArtist);
      if (!a) return [];
      if (!this.search) return a.albums;
      const q = this.search.toLowerCase();
      return a.albums.filter(al =>
        al.name.toLowerCase().includes(q) ||
        al.tracks.some(t => t.title.toLowerCase().includes(q))
      );
    },

    get currentTracks() {
      if (!this.selectedAlbum) return [];
      const al = this.currentAlbums.find(al => al.name === this.selectedAlbum);
      if (!al) return [];
      if (!this.search) return al.tracks;
      const q = this.search.toLowerCase();
      return al.tracks.filter(t =>
        t.title.toLowerCase().includes(q) ||
        al.name.toLowerCase().includes(q)
      );
    },

    onSearch() {
      this.selectedArtist = null;
      this.selectedAlbum = null;
      this.selectedTrack = null;
      this.albumArtUrl = null;
    },

    pickArtist(name) {
      this.selectedArtist = name;
      this.selectedAlbum = null;
      this.selectedTrack = null;
      this.albumArtUrl = null;
    },

    pickAlbum(name) {
      this.selectedAlbum = name;
      this.selectedTrack = null;
      this.albumArtUrl = null;
      const al = this.currentAlbums.find(a => a.name === name);
      const url = this.artUrl(al);
      if (url) this.albumArtUrl = url;
    },

    openDetail(t) {
      this.selectedTrack = this.selectedTrack?.id === t.id ? null : t;
    },

    artUrl(album) {
      if (!album) return null;
      const t = album.tracks.find(t => t.artwork && t.ipod_path);
      if (!t) return null;
      return '/artwork?devnode=' + encodeURIComponent(this.selectedDevnode || '') +
             '&path=' + encodeURIComponent(t.ipod_path);
    },

    _buildIpodMap() {
      this._ipodMap = new Map();
      for (const artist of (this.library?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            this._ipodMap.set(this._trackKey(t.artist || artist.name, t.album || album.name, t.track_nr, t.title), t);
          }
        }
      }
      this._ipodIndex = new Set(this._ipodMap.keys());
    },

    isOnIpod(track) {
      if (!this.library) return false;
      if (!this._ipodMap) this._buildIpodMap();
      return this._ipodMap.has(this._trackKey(track.artist || track.albumartist, track.album, track.track_nr, track.title));
    },
  };
}
