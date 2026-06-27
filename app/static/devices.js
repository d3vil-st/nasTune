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

    // WALKMAN scan state
    walkmanScanning: false,
    walkmanScanProgress: {},
    _walkmanPollTimer: null,

    async loadDevices() {
      try {
        const r = await this.apiFetch('/devices');
        const data = await r.json();
        this.devices = data.devices;
        if (data.selected) {
          this.selectedDevnode = data.selected;
          await this._fetchLibrary(true);
          await this.loadOpHistory(data.selected);
        }
      } catch (e) {
        this.libraryError = e.message;
      }
    },

    async selectDevice(devnode) {
      if (this.selectedDevnode === devnode) { this.showDevicePicker = false; return; }
      this.showDevicePicker = false;
      this._browsingOfflineDeviceId = null;
      const device = this.devices.find(d => d.devnode === devnode);
      if (device && !device.mounted) {
        this.libraryLoading = true;
        this.libraryError = null;
        const mr = await this.apiFetch('/devices/mount', {
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
      this._stopWalkmanPoll();
      this.walkmanScanning = false;
      this.walkmanScanProgress = {};
      this.selectedDevnode = devnode;
      this.library = null;
      this._deviceMap = null;
      this.deviceSelection = new Set();
      this.selectedArtist = null;
      this.selectedAlbum = null;
      this.selectedTrack = null;
      this.albumArtUrl = null;
      this.libraryError = null;
      this.libraryLoading = true;
      try {
        const r = await this.apiFetch('/devices/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode }),
        });
        if (!r.ok) { this.libraryError = await r.text(); return; }
        await this._fetchLibrary();
        await this.loadOpHistory(devnode);
        if (this.selectedSourceId) this._initSrcChecked();
      } catch (e) {
        this.libraryError = e.message;
      } finally {
        this.libraryLoading = false;
      }
    },

    _validateIpodSelection() {
      if (this.selectedArtist && this.selectedArtist !== '__ALL__' &&
          !this.library.artists?.find(a => a.name === this.selectedArtist)) {
        this.selectedArtist = null;
        this.selectedAlbum = null;
        this.albumArtUrl = null;
      } else if (this.selectedAlbum && !this.currentAlbums.find(a => a.name === this.selectedAlbum)) {
        this.selectedAlbum = null;
        this.albumArtUrl = null;
      } else if (this.selectedAlbum) {
        const al = this.currentAlbums.find(a => a.name === this.selectedAlbum);
        this.albumArtUrl = this.artUrl(al) || null;
      }
    },

    async _fetchLibrary(refresh = false) {
      this.libraryLoading = true;
      try {
        const r = refresh
          ? await this.apiFetch('/library/refresh', { method: 'POST' })
          : await this.apiFetch('/library');
        if (!r.ok) { this.libraryError = await r.text(); return; }
        this.library = await r.json();
        if (refresh) this._validateIpodSelection();
        // Backend registers the iPod UUID in DB during library load; refresh the
        // known-devices list so a connected iPod no longer appears as "Disconnected".
        this._loadKnownDevices();
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
        const r = await this.apiFetch('/library/refresh', { method: 'POST' });
        if (!r.ok) { this.libraryError = await r.text(); return; }
        this.library = await r.json();
        this._deviceMap = null;
        this.deviceSelection = new Set();
        this.selectedTrack = null;
        this._validateIpodSelection();
      } catch (e) {
        this.libraryError = e.message;
      } finally {
        this.libraryRefreshing = false;
      }
    },

    startSSE() {
      const es = new EventSource('/devices/events');
      let _prevDevnodes = '';
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
        // Refresh the known-devices list whenever the connected set changes so the
        // picker's "Disconnected" section stays in sync without a page reload.
        const devnodes = data.devices.map(d => d.devnode).sort().join(',');
        if (devnodes !== _prevDevnodes) {
          _prevDevnodes = devnodes;
          this._loadKnownDevices();
        }
      };
      es.onerror = () => { this._probeAuth(); };
    },

    async ejectDevice() {
      if (!this.selectedDevnode) return;
      if (this.playerVisible) this.stopPlayback();
      try {
        const r = await this.apiFetch('/devices/eject', {
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

    async triggerWalkmanScan(full = false) {
      if (!this.selectedDevnode || !this.selectedDevice?.is_walkman) return;
      if (full && !confirm('Re-read all tags on this device? Existing library data will be cleared and the full scan may take several minutes.')) return;
      try {
        const url = `/walkman/scan?devnode=${encodeURIComponent(this.selectedDevnode)}${full ? '&full=true' : ''}`;
        const r = await this.apiFetch(url, { method: 'POST' });
        if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.detail || 'Scan failed'); return; }
        this.walkmanScanning = true;
        this.walkmanScanProgress = {};
        this._startWalkmanPoll();
      } catch (e) { alert('Scan failed: ' + e.message); }
    },

    _startWalkmanPoll() {
      if (this._walkmanPollTimer) return;
      this._walkmanPollTimer = setInterval(async () => {
        if (!this.selectedDevnode || !this.selectedDevice?.is_walkman) {
          this._stopWalkmanPoll(); return;
        }
        try {
          const r = await this.apiFetch(`/walkman/scan_status?devnode=${encodeURIComponent(this.selectedDevnode)}`);
          if (!r.ok) return;
          const s = await r.json();
          this.walkmanScanProgress = s;
          if (s.status !== 'scanning') {
            this.walkmanScanning = false;
            this._stopWalkmanPoll();
            if (s.status === 'done') {
              this._deviceMap = null;
              await this.refreshLibrary();
              if (this.selectedSourceId) this._initSrcChecked();
            }
          }
        } catch (_) {}
      }, 2000);
    },

    _stopWalkmanPoll() {
      if (this._walkmanPollTimer) { clearInterval(this._walkmanPollTimer); this._walkmanPollTimer = null; }
    },

    get ipodDevices() {
      return this.devices.filter(d => d.is_ipod);
    },

    get selectedDevice() {
      return this.devices.find(d => d.devnode === this.selectedDevnode) || null;
    },

    _connectedDeviceName(d) {
      if (d.mounted) {
        if (d.devnode === this.selectedDevnode && this.library?.ipod_name)
          return this.library.ipod_name;
        if (d.ipod_db_id && this.knownDevices?.ipods) {
          const k = this.knownDevices.ipods.find(i => i.id === d.ipod_db_id);
          if (k?.ipod_name) return k.ipod_name;
        }
        if (d.walkman_db_id && this.knownDevices?.walkmans) {
          const k = this.knownDevices.walkmans.find(i => i.id === d.walkman_db_id);
          if (k) return k.marketing_name || k.model || null;
        }
      } else if (d.usb_serial && this.knownDevices?.ipods) {
        const k = this.knownDevices.ipods.find(i => i.uuid === d.usb_serial);
        if (k?.ipod_name) return k.ipod_name;
      }
      return null;
    },
  };
}
