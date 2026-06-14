function app() {
  const mods = [
    utilsModule(),
    devicesModule(),
    browserModule(),
    playerModule(),
    sourcesModule(),
    selectionModule(),
  ];

  // Use defineProperties to preserve getters across module boundaries
  const state = {};
  for (const mod of mods) {
    Object.defineProperties(state, Object.getOwnPropertyDescriptors(mod));
  }

  state._syncUrl = function () {
    const p = new URLSearchParams();
    p.set('tab', this.viewMode);
    if (this.viewMode === 'library') {
      if (this.selectedArtist) p.set('artist', this.selectedArtist);
      if (this.selectedAlbum)  p.set('album',  this.selectedAlbum);
    } else {
      if (this.selectedSourceId != null) p.set('src',  this.selectedSourceId);
      if (this.srcArtist)                p.set('srca', this.srcArtist);
      if (this.srcAlbum)                 p.set('sral', this.srcAlbum);
    }
    history.replaceState(null, '', '#' + p.toString());
  };

  state.init = async function () {
    this.initTheme();

    const hash = location.hash.slice(1);
    const url = hash ? new URLSearchParams(hash) : null;
    if (url?.get('tab')) this.viewMode = url.get('tab');

    await this.loadDevices();
    this.startSSE();
    this._connectOpEvents();

    // Restore iPod artist/album after library loads
    if (url && this.library) {
      const artist = url.get('artist');
      if (artist === '__ALL__') {
        this.pickArtist('__ALL__');
      } else if (artist && this.library.artists?.find(a => a.name === artist)) {
        this.pickArtist(artist);
        const album = url.get('album');
        if (album && this.currentAlbums.find(a => a.name === album))
          this.pickAlbum(album);
      }
    }

    await this.loadSources();

    // Restore source from URL; fall back to localStorage
    const urlSrcId = url ? parseInt(url.get('src') || '', 10) : NaN;
    const lsSrcId  = parseInt(localStorage.getItem('nasTune_selectedSourceId') || '', 10);
    const srcId    = !isNaN(urlSrcId) ? urlSrcId : lsSrcId;
    if (!isNaN(srcId) && this.sources.find(s => s.id === srcId)) {
      await this.pickSource(srcId);
      if (url) {
        const srca = url.get('srca');
        if (srca === '__ALL__') {
          this.pickSrcArtist('__ALL__');
        } else if (srca && this.sourceLibrary?.artists?.find(a => a.name === srca)) {
          this.pickSrcArtist(srca);
          const sral = url.get('sral');
          if (sral && this.srcCurrentAlbums.find(a => a.name === sral))
            this.pickSrcAlbum(sral);
        }
      }
    }

    // Persist unsynced filter toggle
    this.$watch('srcShowUnsynced', v => localStorage.setItem('nastune-src-unsynced', v ? '1' : '0'));

    // Keep URL in sync with navigation state
    this.$watch('viewMode',        () => this._syncUrl());
    this.$watch('selectedArtist',  () => this._syncUrl());
    this.$watch('selectedAlbum',   () => this._syncUrl());
    this.$watch('selectedSourceId',() => this._syncUrl());
    this.$watch('srcArtist',       () => this._syncUrl());
    this.$watch('srcAlbum',        () => this._syncUrl());
  };

  return state;
}
