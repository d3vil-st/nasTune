function playlistsModule() {
  return {
    // ── device playlists (Playlists media type, library tab) ─────────────
    selectedDevPlaylistIdx: null,   // index into devPlaylists — names can repeat on real devices
    publishBusy: false,
    publishResult: null,
    _trackById: null,               // track id -> { t, artist, album }
    _trackByIdLib: null,            // library reference _trackById was built from

    get devPlaylists() {
      return this.library?.playlists || [];
    },

    get selectedDevPlaylist() {
      const i = this.selectedDevPlaylistIdx;
      return (i != null && this.devPlaylists[i]) || null;
    },

    pickDevPlaylist(i) {
      this.selectedDevPlaylistIdx = i;
      this.publishResult = null;
    },

    // Built from the UNFILTERED library (playlists may mix media types).
    // Cache keyed by library reference — rebuilt automatically on library reload.
    _deviceTrackById() {
      if (this._trackById && this._trackByIdLib === this.library) return this._trackById;
      const map = new Map();
      for (const artist of (this.library?.artists || [])) {
        for (const album of artist.albums) {
          for (const t of album.tracks) {
            map.set(t.id, { t, artist: artist.name, album: album.name });
          }
        }
      }
      this._trackById = map;
      this._trackByIdLib = this.library;
      return map;
    },

    get devPlaylistTracks() {
      const pl = this.selectedDevPlaylist;
      if (!pl) return [];
      const byId = this._deviceTrackById();
      const out = [];
      for (const id of pl.track_ids) {
        const hit = byId.get(id);
        if (hit) out.push({ ...hit.t, _artist: hit.t.artist || hit.artist, _album: hit.album });
        else out.push({ id, title: '(unknown track)', _artist: '', _album: '', _unresolved: true });
      }
      return out;
    },

    get devPlaylistDurationMs() {
      return this.devPlaylistTracks.reduce((s, t) => s + (t.duration_ms || 0), 0);
    },

    // ── smart playlists ───────────────────────────────────────────────────
    smartPlaylists: [],
    showSmartModal: false,
    smartSpec: null,          // editing copy: { id, name, match_all, rules[], … }
    smartLimitOn: false,
    smartPreview: null,       // { count, duration_ms, size_bytes } | null
    smartBusy: false,
    smartError: null,
    _smartPreviewTimer: null,

    // Mirrors the gpod-playlist SPL vocabulary — these rules are written to
    // the iTunesDB as a real smart playlist, not evaluated by nasTune.
    smartFields: [
      { key: 'title',       label: 'Title',              kind: 'text' },
      { key: 'artist',      label: 'Artist',             kind: 'text' },
      { key: 'album',       label: 'Album',              kind: 'text' },
      { key: 'albumartist', label: 'Album Artist',       kind: 'text' },
      { key: 'genre',       label: 'Genre',              kind: 'text' },
      { key: 'composer',    label: 'Composer',           kind: 'text' },
      { key: 'rating',      label: 'Rating (stars)',     kind: 'number' },
      { key: 'playcount',   label: 'Plays',              kind: 'number' },
      { key: 'year',        label: 'Year',               kind: 'number' },
      { key: 'bitrate',     label: 'Bit Rate (kbps)',    kind: 'number' },
      { key: 'samplerate',  label: 'Sample Rate (Hz)',   kind: 'number' },
      { key: 'duration',    label: 'Duration (seconds)', kind: 'number' },
      { key: 'size',        label: 'Size (MB)',          kind: 'number' },
      { key: 'track_nr',    label: 'Track #',            kind: 'number' },
      { key: 'disc_nr',     label: 'Disc #',             kind: 'number' },
      { key: 'time_added',  label: 'Date Added',         kind: 'date' },
      { key: 'time_played', label: 'Last Played',        kind: 'date' },
    ],
    smartOps: {
      text:   [['contains', 'contains'], ['not_contains', 'does not contain'], ['is', 'is'], ['is_not', 'is not'], ['starts_with', 'starts with'], ['ends_with', 'ends with']],
      number: [['is', 'is'], ['is_not', 'is not'], ['gt', 'is greater than'], ['gte', 'is at least'], ['lt', 'is less than'], ['lte', 'is at most'], ['in_range', 'is in range']],
      date:   [['in_last', 'in the last (days)'], ['not_in_last', 'not in the last (days)']],
    },
    smartLimitUnits: ['items', 'minutes', 'hours', 'MB', 'GB'],
    smartLimitBy: [
      ['random', 'random'],
      ['most_recently_added', 'most recently added'], ['least_recently_added', 'least recently added'],
      ['most_played', 'most often played'], ['least_played', 'least often played'],
      ['most_recently_played', 'most recently played'], ['least_recently_played', 'least recently played'],
      ['highest_rating', 'highest rating'], ['lowest_rating', 'lowest rating'],
      ['name', 'name'], ['artist', 'artist'], ['album', 'album'], ['genre', 'genre'],
    ],

    smartFieldDef(key) {
      return this.smartFields.find(f => f.key === key) || this.smartFields[0];
    },
    smartOpsFor(key) {
      return this.smartOps[this.smartFieldDef(key).kind];
    },

    _newSmartRule() {
      return { field: 'artist', op: 'contains', value: '', value2: '' };
    },

    async loadSmartPlaylists() {
      try {
        const r = await this.apiFetch('/smart-playlists');
        if (r.ok) this.smartPlaylists = (await r.json()).smart_playlists;
      } catch (e) { console.error('loadSmartPlaylists:', e); }
    },

    openSmartModal(spec = null) {
      this.smartError = null;
      this.smartPreview = null;
      if (spec) {
        this.smartSpec = JSON.parse(JSON.stringify(spec));
        if (!this.smartSpec.rules.length) this.smartSpec.rules = [this._newSmartRule()];
        this.smartLimitOn = this.smartSpec.limit_n != null;
        if (this.smartSpec.limit_n == null) this.smartSpec.limit_n = 25;
      } else {
        this.smartSpec = {
          id: null, name: '', match_all: true,
          limit_n: 25, limit_unit: 'items', limit_by: 'random',
          live: false, rules: [this._newSmartRule()],
        };
        this.smartLimitOn = false;
      }
      this.showSmartModal = true;
      this.scheduleSmartPreview();
    },

    onSmartFieldChange(rule) {
      rule.op = this.smartOpsFor(rule.field)[0][0];
      rule.value = '';
      rule.value2 = '';
      this.scheduleSmartPreview();
    },

    addSmartRule(idx) {
      this.smartSpec.rules.splice(idx + 1, 0, this._newSmartRule());
      this.scheduleSmartPreview();
    },
    removeSmartRule(idx) {
      this.smartSpec.rules.splice(idx, 1);
      if (!this.smartSpec.rules.length) this.smartSpec.rules = [this._newSmartRule()];
      this.scheduleSmartPreview();
    },

    _smartPayload() {
      return {
        ...this.smartSpec,
        limit_n: this.smartLimitOn ? (parseInt(this.smartSpec.limit_n, 10) || null) : null,
      };
    },

    scheduleSmartPreview() {
      clearTimeout(this._smartPreviewTimer);
      this._smartPreviewTimer = setTimeout(() => this._runSmartPreview(), 400);
    },

    async _runSmartPreview() {
      if (!this.showSmartModal) return;
      if (!this.selectedDevnode || !this.library || this.library.walkman) {
        this.smartPreview = null;
        return;
      }
      try {
        const r = await this.apiFetch('/smart-playlists/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode, spec: { ...this._smartPayload(), name: this.smartSpec.name || '_' } }),
        });
        this.smartPreview = r.ok ? await r.json() : null;
      } catch (e) { this.smartPreview = null; }
    },

    async saveSmartPlaylist(apply = false) {
      if (!this.smartSpec.name.trim()) { this.smartError = 'Give the playlist a name'; return; }
      this.smartBusy = true;
      this.smartError = null;
      try {
        const r = await this.apiFetch('/smart-playlists', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this._smartPayload()),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { this.smartError = data.detail || 'Save failed'; return; }
        await this.loadSmartPlaylists();
        if (apply) {
          const ar = await this.apiFetch('/smart-playlists/' + data.id + '/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ devnode: this.selectedDevnode }),
          });
          const adata = await ar.json().catch(() => ({}));
          if (!ar.ok) { this.smartError = adata.detail || 'Apply failed'; return; }
        }
        this.showSmartModal = false;
      } catch (e) {
        if (e.message !== 'auth_expired') this.smartError = 'Save failed: ' + e.message;
      } finally { this.smartBusy = false; }
    },

    async deleteSmartPlaylist() {
      if (!this.smartSpec?.id) { this.showSmartModal = false; return; }
      if (!confirm('Delete this smart playlist definition? The playlist already on the device is kept.')) return;
      try {
        const r = await this.apiFetch('/smart-playlists/' + this.smartSpec.id, { method: 'DELETE' });
        if (!r.ok) { this.smartError = 'Delete failed'; return; }
        this.showSmartModal = false;
        await this.loadSmartPlaylists();
      } catch (e) { this.smartError = 'Delete failed: ' + e.message; }
    },

    smartDeviceIdx(spec) {
      return this.devPlaylists.findIndex(p => p.name === spec.name);
    },

    async publishPlaylistToPlex() {
      const pl = this.selectedDevPlaylist;
      if (!pl || !this.selectedDevnode || this.publishBusy) return;
      this.publishBusy = true;
      this.publishResult = null;
      try {
        const r = await this.apiFetch('/plex/publish-playlist', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            devnode: this.selectedDevnode,
            name: pl.name,
            track_ids: pl.track_ids,
          }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          this.publishResult = { ok: false, msg: data.detail || 'Publish failed' };
          return;
        }
        this.publishResult = {
          ok: true,
          msg: 'Published ' + data.matched + ' / ' + data.total + ' tracks to Plex'
               + (data.skipped ? ' (' + data.skipped + ' not in Plex)' : ''),
        };
      } catch (e) {
        if (e.message !== 'auth_expired')
          this.publishResult = { ok: false, msg: 'Publish failed: ' + e.message };
      } finally { this.publishBusy = false; }
    },
  };
}
