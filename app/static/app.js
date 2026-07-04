function app() {
  const mods = [
    utilsModule(),
    devicesModule(),
    deviceModule(),
    playerModule(),
    sourcesModule(),
    selectionModule(),
    settingsModule(),
    plexModule(),
    playlistsModule(),
  ];

  // Use defineProperties to preserve getters across module boundaries
  const state = {};
  for (const mod of mods) {
    Object.defineProperties(state, Object.getOwnPropertyDescriptors(mod));
  }

  state._syncUrl = function () {
    const p = new URLSearchParams();
    p.set('tab', this.viewMode);
    if (this.mediaType && this.mediaType !== 'music') p.set('mt', this.mediaType);
    if (this.selectedDevnode) p.set('dev', this.selectedDevnode);
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
    await this.loadSettings();
    this._loadKnownDevices();  // fire-and-forget; populates picker before first open
    this.loadPlexMe();         // fire-and-forget; header chip + admin gating
    this.loadPlexStatus();     // fire-and-forget; Plex playlists entry in source picker

    const hash = location.hash.slice(1);
    const url = hash ? new URLSearchParams(hash) : null;
    if (url?.get('tab')) this.viewMode = url.get('tab');
    if (url?.get('mt')) this.mediaType = url.get('mt');

    const urlDev = url?.get('dev') || null;

    await this.loadDevices();

    // Restore device from URL if it differs from what the server auto-selected
    if (urlDev && urlDev !== this.selectedDevnode) {
      const found = this.devices.find(d => d.devnode === urlDev);
      if (found) await this.selectDevice(urlDev);
    }

    this.startSSE();
    this._connectOpEvents();
    if (this.selectedDevnode) this.loadOpHistory(this.selectedDevnode);

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

    // Restore Plex playlists pane when the URL landed on mt=playlist
    if (this.mediaType === 'playlist') {
      await this.loadPlexStatus();
      this.loadPlexPlaylists();
      this.loadSmartPlaylists();
    }

    // Keyboard column navigation
    window.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      const anyModal = this.showSettings || this.showManageSources || this.showAddSource ||
                       this.showDeleteConfirm || this.showSyncConfirm || this.showDeviceSettings ||
                       this.showOpLog || this.coverPopupUrl || this.showDevicePicker || this.showSourcePicker ||
                       this.showSmartModal || this.showChangelog;
      if (anyModal) return;
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        if (!this._focusedCol) return;
        e.preventDefault();
        this._navArrow(e.key === 'ArrowDown' ? 1 : -1);
      } else if (e.key === ' ') {
        if (!this._focusedCol) return;
        e.preventDefault();
        this._navSpace();
      }
    });

    // Persist unsynced filter toggle
    this.$watch('srcShowUnsynced', v => localStorage.setItem('nastune-src-unsynced', v ? '1' : '0'));

    // Keep URL in sync with navigation state
    this.$watch('mediaType',       () => this._syncUrl());
    this.$watch('selectedDevnode', (v) => { this._syncUrl(); if (v) this.loadOpHistory(v); });
    this.$watch('viewMode',        () => this._syncUrl());
    this.$watch('selectedArtist',  () => this._syncUrl());
    this.$watch('selectedAlbum',   () => this._syncUrl());
    this.$watch('selectedSourceId',() => this._syncUrl());
    this.$watch('srcArtist',       () => this._syncUrl());
    this.$watch('srcAlbum',        () => this._syncUrl());
  };

  return state;
}
