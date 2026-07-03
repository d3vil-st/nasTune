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
        } else {
          // Nothing selected server-side (e.g. known iPod connected but unmounted
          // with auto-mount off) — activate the first identifiable device.
          await this._loadKnownDevices();
          const sel = this.devices.find(d => this._isSelectableDevice(d));
          if (sel) await this.selectDevice(sel.devnode);
        }
      } catch (e) {
        this.libraryError = e.message;
      }
    },

    async selectDevice(devnode) {
      if (this.selectedDevnode === devnode) { this.showDevicePicker = false; return; }
      this.showDevicePicker = false;
      this._browsingOfflineDeviceId = null;
      // On-demand mounting: server mounts automatically during select/library load.
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
        if (this.library?.walkman && this.mediaType !== 'music') this.setMediaType('music');
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
      let _seenSnapshot = false;
      es.onmessage = (e) => {
        const data = JSON.parse(e.data);
        // The first SSE message is the startup baseline, regardless of content.
        // Do NOT infer this from _prevDevnodes === '' — unplugging the only
        // device also empties the list, which would mask the next connect.
        const isFirstSnapshot = !_seenSnapshot;
        _seenSnapshot = true;
        this.devices = data.devices;
        if (this.selectedDevnode && !data.devices.find(d => d.devnode === this.selectedDevnode)) {
          this.selectedDevnode = null;
          if (!this._browsingOfflineDeviceId) {
            this.library = null;
            this.selectedArtist = null;
            this.selectedAlbum = null;
            this.selectedTrack = null;
          }
        }
        if (!this.selectedDevnode && data.selected && !this._browsingOfflineDeviceId) {
          this.selectDevice(data.selected);
        }
        // Refresh the known-devices list whenever the connected set changes so the
        // picker's "Disconnected" section stays in sync without a page reload.
        const devnodes = data.devices.map(d => d.devnode).sort().join(',');
        if (devnodes !== _prevDevnodes) {
          const prevSet = new Set(_prevDevnodes.split(',').filter(Boolean));
          _prevDevnodes = devnodes;
          this._loadKnownDevices();
          // If browsing an offline device and it just connected, switch to live.
          if (this._browsingOfflineDeviceId != null) {
            const match = data.devices.find(d =>
              d.ipod_db_id === this._browsingOfflineDeviceId ||
              d.walkman_db_id === this._browsingOfflineDeviceId
            );
            if (match) {
              this._browsingOfflineDeviceId = null;
              this.selectDevice(match.devnode);
            }
          } else if (!isFirstSnapshot && this.currentOp?.status !== 'running') {
            // Freshly connected identifiable device becomes active. Skip the first
            // SSE snapshot (startup baseline) and never steal focus mid-operation.
            const fresh = data.devices.find(d =>
              !prevSet.has(d.devnode) && this._isSelectableDevice(d));
            if (fresh && fresh.devnode !== this.selectedDevnode) {
              this.selectDevice(fresh.devnode);
            }
          }
        }
      };
      es.onerror = () => { this._probeAuth(); };
    },

    // A device we can identify without mounting it. Random USB sticks also match
    // the fstype filter and register unmounted — never auto-select those.
    _isSelectableDevice(d) {
      if (d.mounted) return d.is_ipod || d.is_walkman;
      if (d.is_walkman) return true;  // probe-mount identified it
      return !!(d.usb_serial && this.knownDevices?.ipods?.some(k => k.uuid === d.usb_serial));
    },

    async initIpod() {
      if (!this.selectedDevnode) return;
      if (!confirm('Create an empty iPod database on this device?\n\nThe device becomes manageable by nasTune (and iTunes). Existing files are not touched.')) return;
      try {
        const r = await this.apiFetch('/devices/init-ipod', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode }),
        });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          alert(d.detail || 'Initialize failed');
          return;
        }
        this.libraryError = null;
        // Library loads via the op-done refresh once gpod-cp -I finishes.
      } catch (e) { alert('Initialize failed: ' + e.message); }
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
      } else if (d.usb_serial) {
        if (this.knownDevices?.ipods) {
          const k = this.knownDevices.ipods.find(i => i.uuid === d.usb_serial);
          if (k?.ipod_name) return k.ipod_name;
        }
        if (this.knownDevices?.walkmans) {
          const k = this.knownDevices.walkmans.find(i => i.serial === d.usb_serial);
          if (k) return k.marketing_name || k.model || null;
        }
      }
      return null;
    },
  };
}
