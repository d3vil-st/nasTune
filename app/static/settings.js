function settingsModule() {
  return {
    // Global settings modal
    showSettings: false,
    settingsCpuCount: 1,
    settings: { force_aac: false, max_threads: 1 },
    settingsDraft: { force_aac: false, max_threads: 1 },

    // Device settings modal (independent from global)
    showDeviceSettings: false,
    deviceSettings: null,
    deviceSettingsDraft: null,
    _deviceSettingsDevnode: null,  // devnode of the specific connected device
    _deviceSettingsDbId: null,     // DB id for offline/known device
    _deviceSettingsType: 'ipod',   // 'ipod' | 'walkman'

    // Known devices (for picker)
    knownDevices: null,
    _browsingOfflineDeviceId: null,  // id of the offline iPod whose library is shown

    get hasOfflineDevices() {
      return !!(this.knownDevices &&
        (this.knownDevices.ipods.some(d => !d.connected) ||
         this.knownDevices.walkmans.some(d => !d.connected)));
    },
    get offlineIpods() {
      return this.knownDevices ? this.knownDevices.ipods.filter(d => !d.connected) : [];
    },
    get offlineWalkmans() {
      return this.knownDevices ? this.knownDevices.walkmans.filter(d => !d.connected) : [];
    },

    get _deviceLabel() {
      if (this._deviceSettingsDevnode) {
        // Prefer the human name from the library (loaded for this device)
        if (this.library?.ipod_name && this.selectedDevnode === this._deviceSettingsDevnode)
          return this.library.ipod_name;
        // Fall back: look up by ipod_db_id stored on the DeviceInfo
        const dev = this.devices.find(d => d.devnode === this._deviceSettingsDevnode);
        const dbId = dev?.ipod_db_id ?? dev?.walkman_db_id;
        if (dbId != null) {
          const list = dev?.is_walkman ? (this.knownDevices?.walkmans || []) : (this.knownDevices?.ipods || []);
          const known = list.find(d => d.id === dbId);
          if (known) return known.ipod_name || known.marketing_name || known.model || 'iPod';
        }
        return this._deviceSettingsDevnode;
      }
      if (this._deviceSettingsDbId != null) {
        const list = this._deviceSettingsType === 'ipod'
          ? (this.knownDevices?.ipods || [])
          : (this.knownDevices?.walkmans || []);
        const dev = list.find(d => d.id === this._deviceSettingsDbId);
        return (this._deviceSettingsType === 'ipod'
          ? (dev?.ipod_name || dev?.model)
          : (dev?.marketing_name || dev?.model))
          || (this._deviceSettingsType === 'ipod' ? 'iPod' : 'WALKMAN');
      }
      return 'Device';
    },

    async loadSettings() {
      try {
        const r = await fetch('/settings');
        if (!r.ok) return;
        const data = await r.json();
        this.settingsCpuCount = data.cpu_count;
        this.settings = { force_aac: data.force_aac, max_threads: data.max_threads };
        this.settingsDraft = { ...this.settings };
      } catch (e) { console.error('loadSettings:', e); }
    },

    async _loadKnownDevices() {
      try {
        const r = await fetch('/devices/known');
        if (!r.ok) return;
        this.knownDevices = await r.json();
      } catch (e) { console.error('_loadKnownDevices:', e); }
    },

    // Open global settings modal (from header gear)
    openSettings() {
      this.settingsDraft = { ...this.settings };
      this.showSettings = true;
    },

    // Open device settings modal from picker gear icon (connected device)
    async openDeviceSettings(devnode) {
      this.showDevicePicker = false;
      const dev = this.devices.find(d => d.devnode === devnode);
      // Unmounted device: is_ipod=False on backend so devnode endpoint can't resolve it.
      // Redirect to the known-device path via USB serial → iPod UUID match.
      if (dev && !dev.mounted && dev.usb_serial && this.knownDevices?.ipods) {
        const known = this.knownDevices.ipods.find(k => k.uuid === dev.usb_serial);
        if (known) { await this.openDeviceSettingsForKnown(known.id, 'ipod'); return; }
      }
      this._deviceSettingsDevnode = devnode;
      this._deviceSettingsDbId = null;
      this._deviceSettingsType = dev?.is_walkman ? 'walkman' : 'ipod';
      this.deviceSettings = null;
      this.deviceSettingsDraft = null;
      this.showDeviceSettings = true;
      await this._loadDeviceSettings();
    },

    // Open device settings modal for offline/known device
    async openDeviceSettingsForKnown(dbId, deviceType) {
      this.showDevicePicker = false;
      this._deviceSettingsDevnode = null;
      this._deviceSettingsDbId = dbId;
      this._deviceSettingsType = deviceType;
      this.deviceSettings = null;
      this.deviceSettingsDraft = null;
      this.showDeviceSettings = true;
      await this._loadDeviceSettings();
    },

    async _loadDeviceSettings() {
      let url;
      if (this._deviceSettingsDbId != null) {
        url = '/devices/known/' + this._deviceSettingsDbId + '/settings?device_type=' + this._deviceSettingsType;
      } else if (this._deviceSettingsDevnode) {
        url = '/devices/device-settings?devnode=' + encodeURIComponent(this._deviceSettingsDevnode);
      } else {
        return;
      }
      try {
        const r = await fetch(url);
        if (!r.ok) return;
        this.deviceSettings = await r.json();
        this.deviceSettingsDraft = JSON.parse(JSON.stringify(this.deviceSettings));
      } catch (e) { console.error('_loadDeviceSettings:', e); }
    },

    async saveSettings() {
      try {
        const r = await fetch('/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.settingsDraft),
        });
        if (!r.ok) { alert('Failed to save settings'); return; }
        this.settings = { ...this.settingsDraft };
        this.showSettings = false;
      } catch (e) { alert('Save settings failed: ' + e.message); }
    },

    async saveDeviceSettings() {
      if (!this.deviceSettingsDraft) return;
      let url;
      if (this._deviceSettingsDbId != null) {
        url = '/devices/known/' + this._deviceSettingsDbId + '/settings?device_type=' + this._deviceSettingsType;
      } else if (this._deviceSettingsDevnode) {
        url = '/devices/device-settings?devnode=' + encodeURIComponent(this._deviceSettingsDevnode);
      } else {
        return;
      }
      try {
        const r = await fetch(url, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.deviceSettingsDraft),
        });
        if (!r.ok) { alert('Failed to save device settings'); return; }
        this.deviceSettings = JSON.parse(JSON.stringify(this.deviceSettingsDraft));
        this.showDeviceSettings = false;
      } catch (e) { alert('Save device settings failed: ' + e.message); }
    },

    async deleteKnownDevice(id, type) {
      const label = type === 'ipod' ? 'iPod' : 'WALKMAN';
      if (!confirm(`Remove this ${label} from the known devices list? This will delete its cached library, sync rules, and associated data.`)) return;
      try {
        const r = await fetch('/devices/known/' + id + '?device_type=' + type, { method: 'DELETE' });
        if (!r.ok) { alert('Failed to remove device'); return; }
        this.showDeviceSettings = false;
        if (this._browsingOfflineDeviceId === id) {
          this.library = null;
          this._browsingOfflineDeviceId = null;
          this._deviceIndex = null;
          this._deviceMap = null;
          this.deviceSelection = new Set();
          this.selectedArtist = null;
          this.selectedAlbum = null;
        }
        await this._loadKnownDevices();
      } catch (e) { alert('Remove failed: ' + e.message); }
    },

    async browseOfflineLibrary(deviceId) {
      try {
        const r = await fetch('/devices/offline-library?device_id=' + deviceId + '&device_type=ipod');
        if (!r.ok) { alert('No cached library for this device'); return; }
        const lib = await r.json();
        this.library = lib;
        this._browsingOfflineDeviceId = deviceId;
        this._deviceIndex = null;
        this._deviceMap = null;
        this.showDevicePicker = false;
      } catch (e) { alert('Failed to load offline library: ' + e.message); }
    },

    async runAutoSync() {
      const devnode = this._deviceSettingsDevnode;
      if (!devnode) return;
      try {
        const r = await fetch('/devices/' + encodeURIComponent(devnode) + '/auto-sync', { method: 'POST' });
        const data = await r.json();
        if (!r.ok) { alert(data.detail || 'Auto-sync failed'); return; }
        if (data.queued === 0) { alert(data.message || 'Nothing to sync — all content is already on the device.'); return; }
        this.showDeviceSettings = false;
      } catch (e) { alert('Auto-sync failed: ' + e.message); }
    },

    cycleDeviceForceAac() {
      if (!this.deviceSettingsDraft) return;
      const cur = this.deviceSettingsDraft.force_aac;
      this.deviceSettingsDraft.force_aac = cur === null ? 1 : cur === 1 ? 0 : null;
    },

    deviceForceAacLabel() {
      if (!this.deviceSettingsDraft) return '';
      const v = this.deviceSettingsDraft.force_aac;
      if (v === null) return 'Global default';
      return v ? 'Always on' : 'Always off';
    },

    syncRuleSources(mediaType) {
      return (this.sources || []).filter(s => s.type === mediaType);
    },
  };
}
