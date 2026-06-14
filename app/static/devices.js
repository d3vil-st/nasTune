function devicesModule() {
  return {
    // Device state
    devices: [],
    selectedDevnode: null,
    showDevicePicker: false,

    // Library state
    library: null,
    libraryLoading: false,
    libraryRefreshing: false,
    libraryError: null,

    async loadDevices() {
      try {
        const r = await fetch('/devices');
        const data = await r.json();
        this.devices = data.devices;
        if (data.selected) {
          this.selectedDevnode = data.selected;
          await this._fetchLibrary(true);
        }
      } catch (e) {
        this.libraryError = e.message;
      }
    },

    async selectDevice(devnode) {
      if (this.selectedDevnode === devnode) { this.showDevicePicker = false; return; }
      this.showDevicePicker = false;
      const device = this.devices.find(d => d.devnode === devnode);
      if (device && !device.mounted) {
        this.libraryLoading = true;
        this.libraryError = null;
        const mr = await fetch('/devices/mount', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode }),
        });
        if (!mr.ok) {
          const d = await mr.json().catch(() => ({}));
          this.libraryError = d.detail || 'Mount failed';
          this.libraryLoading = false;
          return;
        }
        await new Promise(r => setTimeout(r, 500));
      }
      this.selectedDevnode = devnode;
      this.library = null;
      this._ipodMap = null;
      this.ipodSelection = new Set();
      this.selectedArtist = null;
      this.selectedAlbum = null;
      this.selectedTrack = null;
      this.albumArtUrl = null;
      this.libraryError = null;
      this.libraryLoading = true;
      try {
        const r = await fetch('/devices/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode }),
        });
        if (!r.ok) { this.libraryError = await r.text(); return; }
        await this._fetchLibrary();
      } catch (e) {
        this.libraryError = e.message;
      } finally {
        this.libraryLoading = false;
      }
    },

    async _fetchLibrary(refresh = false) {
      this.libraryLoading = true;
      try {
        const r = refresh
          ? await fetch('/library/refresh', { method: 'POST' })
          : await fetch('/library');
        if (!r.ok) { this.libraryError = await r.text(); return; }
        this.library = await r.json();
      } catch (e) {
        this.libraryError = e.message;
      } finally {
        this.libraryLoading = false;
      }
    },

    async refreshLibrary() {
      if (this.libraryRefreshing) return;
      this.libraryRefreshing = true;
      try {
        const r = await fetch('/library/refresh', { method: 'POST' });
        if (!r.ok) { this.libraryError = await r.text(); return; }
        this.library = await r.json();
        this._ipodMap = null;
        this.ipodSelection = new Set();
        this.selectedArtist = null;
        this.selectedAlbum = null;
        this.selectedTrack = null;
        this.albumArtUrl = null;
      } catch (e) {
        this.libraryError = e.message;
      } finally {
        this.libraryRefreshing = false;
      }
    },

    startSSE() {
      const es = new EventSource('/devices/events');
      es.onmessage = (e) => {
        const data = JSON.parse(e.data);
        this.devices = data.devices;
        if (this.selectedDevnode && !data.devices.find(d => d.devnode === this.selectedDevnode)) {
          this.library = null;
          this.selectedDevnode = null;
          this.selectedArtist = null;
          this.selectedAlbum = null;
          this.selectedTrack = null;
        }
        if (!this.selectedDevnode && data.selected) {
          this.selectDevice(data.selected);
        }
      };
    },

    async ejectDevice() {
      if (!this.selectedDevnode) return;
      if (this.playerVisible) this.stopPlayback();
      try {
        const r = await fetch('/devices/eject', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode }),
        });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          alert(data.detail || 'Eject failed');
          return;
        }
        this.library = null;
        this.selectedArtist = null;
        this.selectedAlbum = null;
        this.selectedTrack = null;
        this.albumArtUrl = null;
        this.selectedDevnode = null;
        await this.loadDevices();
      } catch (e) {
        alert('Eject failed: ' + e.message);
      }
    },

    get ipodDevices() {
      return this.devices.filter(d => d.is_ipod);
    },

    get selectedDevice() {
      return this.devices.find(d => d.devnode === this.selectedDevnode) || null;
    },
  };
}
