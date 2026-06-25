function settingsModule() {
  return {
    showSettings: false,
    settingsCpuCount: 1,
    settings: { force_aac: false, max_threads: 1 },
    settingsDraft: { force_aac: false, max_threads: 1 },

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

    openSettings() {
      this.settingsDraft = { ...this.settings };
      this.showSettings = true;
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
  };
}
