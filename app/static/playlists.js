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
      { key: 'comment',     label: 'Comment',            kind: 'text' },
      { key: 'grouping',    label: 'Grouping',           kind: 'text' },
      { key: 'skipcount',   label: 'Skips',              kind: 'number' },
      { key: 'bpm',         label: 'BPM',                kind: 'number' },
      { key: 'compilation', label: 'Compilation',        kind: 'bool' },
      { key: 'purchased',   label: 'Purchased',          kind: 'bool' },
      { key: 'time_added',  label: 'Date Added',         kind: 'date' },
      { key: 'time_played', label: 'Last Played',        kind: 'date' },
      { key: 'time_modified', label: 'Date Modified',    kind: 'date' },
      { key: 'time_skipped',  label: 'Last Skipped',     kind: 'date' },
    ],
    smartOps: {
      text:   [['contains', 'contains'], ['not_contains', 'does not contain'], ['is', 'is'], ['is_not', 'is not'], ['starts_with', 'starts with'], ['ends_with', 'ends with']],
      number: [['is', 'is'], ['is_not', 'is not'], ['gt', 'is greater than'], ['gte', 'is at least'], ['lt', 'is less than'], ['lte', 'is at most'], ['in_range', 'is in range']],
      date:   [['in_last', 'in the last'], ['not_in_last', 'not in the last']],
      bool:   [['is', 'is']],
    },
    smartDateUnits: [['h', 'hours'], ['d', 'days'], ['w', 'weeks'], ['m', 'months'], ['y', 'years']],
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
        for (const r of this.smartSpec.rules) {
          if (this.smartFieldDef(r.field).kind === 'date' && !r.unit) r.unit = 'd';
        }
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
      const kind = this.smartFieldDef(rule.field).kind;
      rule.value = kind === 'bool' ? 'set' : '';
      rule.value2 = '';
      rule.unit = kind === 'date' ? 'd' : undefined;
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
      const name = this.smartSpec.name;
      // Remove from the iPod too when it's connected and carries this playlist
      const onDevice = this.selectedDevnode && this.library && !this.library.walkman &&
                       this.devPlaylists.some(p => p.name === name);
      const msg = onDevice
        ? 'Delete smart playlist "' + name + '"?\n\nIt will also be removed from the iPod.'
        : 'Delete smart playlist "' + name + '"?';
      if (!confirm(msg)) return;
      try {
        const url = '/smart-playlists/' + this.smartSpec.id +
                    (onDevice ? '?devnode=' + encodeURIComponent(this.selectedDevnode) : '');
        const r = await this.apiFetch(url, { method: 'DELETE' });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          this.smartError = data.detail || 'Delete failed';
          return;
        }
        this.showSmartModal = false;
        this.selectedDevPlaylistIdx = null;
        await this.loadSmartPlaylists();
      } catch (e) { this.smartError = 'Delete failed: ' + e.message; }
    },

    smartDeviceIdx(spec) {
      return this.devPlaylists.findIndex(p => p.name === spec.name);
    },

    async _editPlaylistTracks(name, body) {
      try {
        const r = await this.apiFetch('/library/playlist-tracks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode, name, ...body }),
        });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          alert(data.detail || 'Playlist update failed');
          return false;
        }
        return true;
      } catch (e) { alert('Playlist update failed: ' + e.message); return false; }
    },

    async removeFromDevPlaylist(t) {
      const pl = this.selectedDevPlaylist;
      if (!pl || pl.smartpl || !t.ipod_path || !this.selectedDevnode) return;
      await this._editPlaylistTracks(pl.name, { remove_ipod_paths: [t.ipod_path] });
    },

    // Add the music browser's checked tracks to a device playlist (or a new one)
    async addSelectedToPlaylist(name, create = false) {
      if (!this.selectedDevnode) return;
      const paths = [...new Set(this.deviceSelectedTracks.map(t => t.ipod_path).filter(Boolean))];
      if (!paths.length || !name) return;
      if (await this._editPlaylistTracks(name, { add_ipod_paths: paths, create })) {
        this.deviceSelection = new Set();
      }
    },

    addSelectedToNewPlaylist() {
      const name = (prompt('New playlist name:') || '').trim();
      if (!name) return;
      if (this.devPlaylists.some(p => p.name === name)) {
        alert('A playlist with this name already exists — pick it from the list instead.');
        return;
      }
      this.addSelectedToPlaylist(name, true);
    },

    // Read an iTunes-created smart playlist's rules off the iPod into the editor
    async adoptSmartPlaylist(p) {
      if (!this.selectedDevnode) return;
      try {
        const r = await this.apiFetch('/smart-playlists/device-rules?devnode=' +
          encodeURIComponent(this.selectedDevnode) + '&name=' + encodeURIComponent(p.name));
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { alert(data.detail || 'Could not read the playlist rules'); return; }
        const unparsed = data.unparsed || [];
        delete data.unparsed;
        this.openSmartModal(data);
        if (unparsed.length) {
          this.smartError = 'Unreadable rules will be lost on Save & Apply: ' + unparsed.join(' · ');
        }
      } catch (e) { alert('Could not read the playlist rules: ' + e.message); }
    },

    async deleteDevPlaylist() {
      const pl = this.selectedDevPlaylist;
      if (!pl || !this.selectedDevnode) return;
      if (!confirm('Delete playlist "' + pl.name + '" from the iPod?\n\nTracks stay on the device.')) return;
      try {
        const r = await this.apiFetch('/library/playlist-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ devnode: this.selectedDevnode, name: pl.name }),
        });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          alert(data.detail || 'Delete failed');
          return;
        }
        this.selectedDevPlaylistIdx = null;
      } catch (e) { alert('Delete failed: ' + e.message); }
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
