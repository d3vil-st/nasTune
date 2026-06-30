function deviceModule() {
  return {
    // Browser state
    search: '',
    selectedArtist: null,
    selectedAlbum: null,
    selectedTrack: null,
    albumArtUrl: null,
    coverPopupUrl: null,
    _focusedCol: null,
    _selectedAlbumArtist: null,
    _deviceIndex: null,
    _deviceMap: null,
    _srcKeyMap: null,

    // Library filtered to tracks matching the current media type.
    // Spreads album/artist objects so mutations don't affect the original cache.
    get _typeFilteredLibrary() {
      const all = this.library?.artists || [];
      const mt = this.mediaType || 'music';
      return all.map(a => {
        const albums = a.albums.map(al => {
          const tracks = al.tracks.filter(t =>
            mt === 'music' ? (!t.mediatype || t.mediatype === 'music') : t.mediatype === mt
          );
          return tracks.length ? { ...al, tracks } : null;
        }).filter(Boolean);
        return albums.length ? { ...a, albums } : null;
      }).filter(Boolean);
    },

    get devLabels() {
      if (this.mediaType === 'audiobook') return { artist: 'Authors', allArtists: 'All Authors', album: 'Books', trackLabel: 'Chapters', trackSg: 'chapter', trackPl: 'chapters', selectArtist: 'Select an author', selectAlbum: 'Select a book' };
      if (this.mediaType === 'podcast')   return { artist: 'Shows',   allArtists: 'All Shows',   album: 'Seasons', trackLabel: 'Episodes', trackSg: 'episode', trackPl: 'episodes', selectArtist: 'Select a show',   selectAlbum: 'Select a season' };
      return                                     { artist: 'Artists', allArtists: 'All Artists', album: 'Albums',  trackLabel: 'Tracks',   trackSg: 'track',   trackPl: 'tracks',   selectArtist: 'Select an artist', selectAlbum: 'Select an album' };
    },

    get filteredArtists() {
      const artists = this._typeFilteredLibrary;
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
      const artists = this._typeFilteredLibrary;
      if (this.selectedArtist === '__ALL__') {
        if (!this.search) return artists.flatMap(a => a.albums.map(al => ({ ...al, _artist: a.name })));
        const q = this.search.toLowerCase();
        return artists.flatMap(a => a.albums.filter(al =>
          al.name.toLowerCase().includes(q) ||
          al.tracks.some(t => t.title.toLowerCase().includes(q))
        ).map(al => ({ ...al, _artist: a.name })));
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

    _findSelectedAlbum(albums, name) {
      return albums.find(a =>
        a.name === name &&
        (!this._selectedAlbumArtist || a._artist === this._selectedAlbumArtist)
      );
    },

    get currentTracks() {
      if (!this.selectedAlbum) return [];
      const al = this._findSelectedAlbum(this.currentAlbums, this.selectedAlbum);
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
      this._selectedAlbumArtist = null;
      if (name !== '__ALL__' && (this.mediaType === 'podcast' || window.innerWidth > 768)) {
        const first = this.currentAlbums[0];
        if (first) this.pickAlbum(first.name);
      }
    },

    pickAlbum(name) {
      this.selectedAlbum = name;
      this.selectedTrack = null;
      this.albumArtUrl = null;
      const al = this._findSelectedAlbum(this.currentAlbums, name);
      const url = this.artUrl(al);
      if (url) this.albumArtUrl = url;
    },

    openDetail(t) {
      this.selectedTrack = this.selectedTrack?.id === t.id ? null : t;
    },

    _navArrow(dir) {
      const isSrc = this.viewMode === 'sources';
      const paneIdx = isSrc ? 1 : 0;
      const scrollInto = (cls, rowCls) => this.$nextTick(() => {
        document.querySelectorAll(cls)[paneIdx]?.querySelector(rowCls)?.scrollIntoView({ block: 'nearest' });
      });

      if (this._focusedCol === 'artist') {
        if (isSrc) {
          const names = [];
          if (this.sourceLibrary && this.mediaType !== 'podcast') names.push('__ALL__');
          for (const a of this.filteredSrcArtists) names.push(a.name);
          const idx = names.indexOf(this.srcArtist);
          const next = idx + dir;
          if (next >= 0 && next < names.length) this.pickSrcArtist(names[next]);
        } else {
          const names = [];
          if (this.library && this.mediaType !== 'podcast') names.push('__ALL__');
          for (const a of this.filteredArtists) names.push(a.name);
          const idx = names.indexOf(this.selectedArtist);
          const next = idx + dir;
          if (next >= 0 && next < names.length) this.pickArtist(names[next]);
        }
        this._focusedCol = 'artist';
        scrollInto('.col-artists', '.artist-row.selected');

      } else if (this._focusedCol === 'album') {
        if (isSrc) {
          const albums = this.srcCurrentAlbums;
          const idx = albums.findIndex(a =>
            a.name === this.srcAlbum &&
            (!this._selectedAlbumArtist || a._artist === this._selectedAlbumArtist)
          );
          const next = idx + dir;
          if (next >= 0 && next < albums.length) {
            this._selectedAlbumArtist = albums[next]._artist || null;
            this.pickSrcAlbum(albums[next].name);
          }
        } else {
          const albums = this.currentAlbums;
          const idx = albums.findIndex(a =>
            a.name === this.selectedAlbum &&
            (!this._selectedAlbumArtist || a._artist === this._selectedAlbumArtist)
          );
          const next = idx + dir;
          if (next >= 0 && next < albums.length) {
            this._selectedAlbumArtist = albums[next]._artist || null;
            this.pickAlbum(albums[next].name);
          }
        }
        this._focusedCol = 'album';
        scrollInto('.col-albums', '.album-row.selected');

      } else if (this._focusedCol === 'track') {
        if (isSrc) {
          const tracks = this.srcCurrentTracks;
          const idx = tracks.findIndex(t => t.id === this.srcSelectedTrack?.id);
          const next = idx + dir;
          if (next >= 0 && next < tracks.length) this.srcSelectedTrack = tracks[next];
        } else {
          const tracks = this.currentTracks;
          const idx = tracks.findIndex(t => t.id === this.selectedTrack?.id);
          const next = idx + dir;
          if (next >= 0 && next < tracks.length) this.selectedTrack = tracks[next];
        }
        scrollInto('.col-tracks', '.track-row.active');
      }
    },

    _navSpace() {
      const isSrc = this.viewMode === 'sources';
      if (this._focusedCol === 'artist') {
        if (isSrc && this.srcArtist)
          this.toggleSrcArtist(this.srcArtist, !this.isSrcArtistChecked(this.srcArtist));
        else if (!isSrc && this.selectedArtist)
          this.toggleArtist(this.selectedArtist, !this.isArtistSelected(this.selectedArtist));
      } else if (this._focusedCol === 'album') {
        if (isSrc && this.srcAlbum) {
          const al = this.srcCurrentAlbums.find(a => a.name === this.srcAlbum);
          if (al) this.toggleSrcAlbum(al, !this.isSrcAlbumChecked(al));
        } else if (!isSrc && this.selectedAlbum) {
          const al = this.currentAlbums.find(a => a.name === this.selectedAlbum);
          if (al) this.toggleAlbum(al, !this.isAlbumSelected(al));
        }
      } else if (this._focusedCol === 'track') {
        if (isSrc && this.srcSelectedTrack)
          this.toggleSrcTrack(this.srcSelectedTrack, !this.isSrcTrackChecked(this.srcSelectedTrack));
        else if (!isSrc && this.selectedTrack)
          this.toggleTrack(this.selectedTrack, !this.deviceSelection.has(this.selectedTrack.id));
      }
    },

    async setIpodRating(track, stars) {
      if (!track || !this.selectedDevnode) return;
      const r = await fetch('/library/rate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ devnode: this.selectedDevnode, track_id: track.id, rating: stars }),
      });
      if (r.ok) track.rating = stars * 20;
    },

    artUrl(album) {
      if (!album) return null;
      const albumartist = album.albumartist || '';
      if (!albumartist && !album.name) return null;
      return '/artwork/album?artist=' + encodeURIComponent(albumartist) +
             '&album=' + encodeURIComponent(album.name || '');
    },

    _buildDeviceMap() {
      this._deviceMap = new Map();
      for (const artist of (this.library?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            // Podcasts group by album (show name) — artist tag is not used for navigation.
            const ak = t.mediatype === 'podcast' ? (t.album || album.name) : (t.artist || artist.name);
            this._deviceMap.set(this._trackKey(ak, t.album || album.name, t.track_nr, t.title, t.disc_nr), t);
          }
        }
      }
      this._deviceIndex = new Set(this._deviceMap.keys());
    },

    isOnDevice(track) {
      if (!this.library) return false;
      if (!this._deviceMap) this._buildDeviceMap();
      // For podcast source tracks, artist = show name (from _remap_podcast using album tag first).
      return this._deviceMap.has(this._trackKey(track.artist || track.albumartist, track.album, track.track_nr, track.title, track.disc_nr));
    },

    _buildSrcKeyMap() {
      this._srcKeyMap = new Map();
      for (const artist of (this.sourceLibrary?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            const ak = this.mediaType === 'podcast' ? (t.album || album.name) : (t.artist || artist.name);
            this._srcKeyMap.set(
              this._trackKey(ak, t.album || album.name, t.track_nr, t.title, t.disc_nr),
              true
            );
          }
        }
      }
    },

    isTrackInSrc(track, artistName, albumName) {
      if (!this.selectedSourceId || !this.sourceLibrary) return true;
      if (!this._srcKeyMap) this._buildSrcKeyMap();
      // For podcast device tracks, use albumName (show name) as the grouping key.
      const ak = track.mediatype === 'podcast' ? albumName : (track.artist || artistName);
      return this._srcKeyMap.has(
        this._trackKey(ak, albumName, track.track_nr, track.title, track.disc_nr)
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
