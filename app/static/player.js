function playerModule() {
  return {
    // Player state
    queue: [],
    queueIndex: -1,
    playing: false,
    shuffle: false,
    repeat: 'none',
    currentTime: 0,
    duration: 0,
    seeking: false,
    playerVisible: false,
    playerArtUrl: null,
    playerContext: { artist: '', album: '' },
    playerSource: 'ipod',
    _cacheEvictUrl: null,

    get playerTrack() {
      return this.queue[this.queueIndex] || null;
    },

    get effectiveDuration() {
      if (isFinite(this.duration) && this.duration > 0) return this.duration;
      return this.playerTrack ? this.playerTrack.duration_ms / 1000 : 0;
    },

    playTrack(track) {
      const album = this.currentAlbums.find(a => a.name === this.selectedAlbum);
      const playable = album ? album.tracks.filter(t => !t.missing) : (!track.missing ? [track] : []);
      if (!playable.length) return;
      this.queue = playable;
      this.queueIndex = this.queue.findIndex(t => t.id === track.id);
      if (this.queueIndex < 0) this.queueIndex = 0;
      this.playerSource = 'ipod';
      this.playerContext = { artist: this.selectedArtist === '__ALL__' ? (track.artist || '') : (this.selectedArtist || ''), album: this.selectedAlbum || '' };
      const artAlbum = album || (this.currentAlbums[0] || null);
      this.playerArtUrl = artAlbum ? this.artUrl(artAlbum) : null;
      this._loadAndPlay();
    },

    _evictCurrentCache() {
      const url = this._cacheEvictUrl;
      if (!url) return;
      this._cacheEvictUrl = null;
      fetch(url, { method: 'POST' }).catch(() => {});
    },

    _loadAndPlay() {
      const track = this.queue[this.queueIndex];
      if (!track) return;
      this._evictCurrentCache();
      const audio = this.$refs.audio;
      if (this.playerSource === 'sources') {
        audio.src = '/sources/audio?path=' + encodeURIComponent(track.path);
        this._cacheEvictUrl = '/sources/audio/cache/evict?path=' + encodeURIComponent(track.path);
      } else {
        const devnode = encodeURIComponent(this.selectedDevnode || '');
        audio.src = '/audio?devnode=' + devnode + '&path=' + encodeURIComponent(track.ipod_path);
        this._cacheEvictUrl = '/audio/cache/evict?devnode=' + devnode + '&path=' + encodeURIComponent(track.ipod_path);
      }
      audio.play().catch(() => {});
      this.playerVisible = true;
    },

    togglePlay() {
      const a = this.$refs.audio;
      if (a.paused) a.play().catch(() => {}); else a.pause();
    },

    prevTrack() {
      if (this.$refs.audio.currentTime > 3) {
        this.$refs.audio.currentTime = 0;
        return;
      }
      let idx = this.shuffle
        ? Math.floor(Math.random() * this.queue.length)
        : this.queueIndex - 1;
      if (idx < 0) idx = this.repeat !== 'none' ? this.queue.length - 1 : 0;
      this.queueIndex = idx;
      this._loadAndPlay();
    },

    nextTrack() {
      if (this.repeat === 'one') {
        this.$refs.audio.currentTime = 0;
        this.$refs.audio.play().catch(() => {});
        return;
      }
      let idx = this.shuffle
        ? Math.floor(Math.random() * this.queue.length)
        : this.queueIndex + 1;
      if (idx >= this.queue.length) {
        if (this.repeat === 'all') idx = 0;
        else { this.playing = false; return; }
      }
      this.queueIndex = idx;
      this._loadAndPlay();
    },

    onTrackEnded() { this.nextTrack(); },

    seek(val) { this.$refs.audio.currentTime = parseFloat(val); },

    toggleShuffle() { this.shuffle = !this.shuffle; },

    toggleRepeat() {
      const modes = ['none', 'all', 'one'];
      this.repeat = modes[(modes.indexOf(this.repeat) + 1) % modes.length];
    },

    playAlbum(al) {
      const playable = al.tracks.filter(t => !t.missing);
      if (!playable.length) return;
      this.queue = playable;
      this.queueIndex = 0;
      this.playerSource = 'ipod';
      this.playerContext = { artist: this.selectedArtist === '__ALL__' ? (al.tracks[0]?.artist || '') : (this.selectedArtist || ''), album: al.name };
      this.playerArtUrl = this.artUrl(al);
      this._loadAndPlay();
    },

    stopPlayback() {
      this._evictCurrentCache();
      const a = this.$refs.audio;
      a.pause();
      a.src = '';
      this.playing = false;
      this.queue = [];
      this.queueIndex = -1;
      this.currentTime = 0;
      this.duration = 0;
      this.playerVisible = false;
    },

    isCurrentTrack(t) {
      return this.playerSource === 'ipod' && this.queue.length > 0 && this.queue[this.queueIndex]?.id === t.id;
    },

    isSrcCurrentTrack(t) {
      return this.playerSource === 'sources' && this.queue.length > 0 && this.queue[this.queueIndex]?.id === t.id;
    },

    playSrcTrack(track) {
      const al = this.srcCurrentAlbums.find(a => a.name === this.srcAlbum);
      const playable = al ? al.tracks : [track];
      this.queue = playable;
      this.queueIndex = this.queue.findIndex(t => t.id === track.id);
      if (this.queueIndex < 0) this.queueIndex = 0;
      this.playerSource = 'sources';
      this.playerContext = { artist: this.srcArtist === '__ALL__' ? (track.artist || '') : (this.srcArtist || ''), album: this.srcAlbum || '' };
      this.playerArtUrl = this.srcAlbumArtUrl;
      this._loadAndPlay();
    },

    playSrcAlbum(al) {
      if (!al || !al.tracks.length) return;
      this.queue = al.tracks;
      this.queueIndex = 0;
      this.playerSource = 'sources';
      this.playerContext = { artist: this.srcArtist === '__ALL__' ? (al.tracks[0]?.artist || '') : (this.srcArtist || ''), album: al.name };
      this.playerArtUrl = this.sourceArtUrl(al);
      this._loadAndPlay();
    },
  };
}
