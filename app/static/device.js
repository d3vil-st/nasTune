function deviceModule() {
  return {
    // Browser state
    search: '',
    selectedArtist: null,
    selectedAlbum: null,
    selectedTrack: null,
    albumArtUrl: null,
    coverPopupUrl: null,
    _deviceIndex: null,
    _deviceMap: null,
    _srcKeyMap: null,

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
      const artists = this.library?.artists || [];
      if (this.selectedArtist === '__ALL__') {
        if (!this.search) return artists.flatMap(a => a.albums);
        const q = this.search.toLowerCase();
        return artists.flatMap(a => a.albums.filter(al =>
          al.name.toLowerCase().includes(q) ||
          al.tracks.some(t => t.title.toLowerCase().includes(q))
        ));
      }
      if (!this.selectedArtist) return [];
      const a = artists.find(a => a.name === this.selectedArtist);
      if (!a) return [];
      if (!this.search) return a.albums;
      const q = this.search.toLowerCase();
      // Artist name itself matched — show all albums unfiltered
      if (a.name.toLowerCase().includes(q)) return a.albums;
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
      // Artist or album name matched — show all tracks unfiltered
      const artistMatches = this.selectedArtist && this.selectedArtist.toLowerCase().includes(q);
      if (artistMatches || al.name.toLowerCase().includes(q)) return al.tracks;
      return al.tracks.filter(t => t.title.toLowerCase().includes(q));
    },

    onSearch() {
      this.selectedArtist = null;
      this.selectedAlbum = null;
      this.selectedTrack = null;
      this.albumArtUrl = null;
      this.srcArtist = null;
      this.srcAlbum = null;
      this.srcSelectedTrack = null;
      this.srcAlbumArtUrl = null;
    },

    pickArtist(name) {
      this.selectedArtist = name;
      this.selectedAlbum = null;
      this.selectedTrack = null;
      this.albumArtUrl = null;
      if (name !== '__ALL__' && window.innerWidth > 768) {
        const first = this.currentAlbums[0];
        if (first) this.pickAlbum(first.name);
      }
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

    _buildDeviceMap() {
      this._deviceMap = new Map();
      for (const artist of (this.library?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            this._deviceMap.set(this._trackKey(t.artist || artist.name, t.album || album.name, t.track_nr, t.title, t.disc_nr), t);
          }
        }
      }
      this._deviceIndex = new Set(this._deviceMap.keys());
    },

    isOnDevice(track) {
      if (!this.library) return false;
      if (!this._deviceMap) this._buildDeviceMap();
      return this._deviceMap.has(this._trackKey(track.artist || track.albumartist, track.album, track.track_nr, track.title, track.disc_nr));
    },

    _buildSrcKeyMap() {
      this._srcKeyMap = new Map();
      for (const artist of (this.sourceLibrary?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            this._srcKeyMap.set(
              this._trackKey(t.artist || artist.name, t.album || album.name, t.track_nr, t.title, t.disc_nr),
              true
            );
          }
        }
      }
    },

    isTrackInSrc(track, artistName, albumName) {
      if (!this.selectedSourceId || !this.sourceLibrary) return true;
      if (!this._srcKeyMap) this._buildSrcKeyMap();
      return this._srcKeyMap.has(
        this._trackKey(track.artist || artistName, albumName, track.track_nr, track.title, track.disc_nr)
      );
    },

    isAlbumInSrc(al, artistName) {
      if (!this.selectedSourceId || !this.sourceLibrary) return true;
      return al.tracks.length > 0 && al.tracks.every(t => this.isTrackInSrc(t, artistName, al.name));
    },

    isArtistInSrc(artist) {
      if (!this.selectedSourceId || !this.sourceLibrary) return true;
      return artist.albums.length > 0 && artist.albums.every(al => this.isAlbumInSrc(al, artist.name));
    },
  };
}
