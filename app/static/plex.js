function plexModule() {
  return {
    // ── status / identity ────────────────────────────────────────────────
    plexStatus: null,    // GET /plex/status: { linked, server_name, owner_username, auth_enabled }
    plexUser: null,      // GET /auth/me user: { username, thumb, role } | null

    // ── playlists view (mediaType === 'playlist', sources tab) ───────────
    plexPlaylists: [],
    plexPlaylistsLoading: false,
    plexPlaylist: null,           // { total, matched_count, tracks: [] }
    selectedPlexPlaylistKey: null,
    plexPlaylistLoading: false,
    plexError: null,

    // ── link flow (settings modal) ───────────────────────────────────────
    plexLinkPinId: null,
    plexLinkCode: null,
    plexLinkServers: null,        // list after PIN approved
    plexLinkServerId: null,       // machine_id chosen in the select
    plexLinkStatus: null,
    plexLinkBusy: false,
    _plexLinkTimer: null,

    // ── users / import (settings modal) ──────────────────────────────────
    plexUsers: [],
    plexImport: null,
    _plexImportTimer: null,

    get plexPendingUsers() {
      return this.plexUsers.filter(u => u.role !== 'admin' && !u.enabled);
    },

    async loadPlexStatus() {
      try {
        const r = await fetch('/plex/status');
        if (r.ok) this.plexStatus = await r.json();
      } catch (e) { console.error('loadPlexStatus:', e); }
    },

    async loadPlexMe() {
      try {
        const r = await fetch('/auth/me');
        if (r.ok) this.plexUser = (await r.json()).user;
      } catch (e) { console.error('loadPlexMe:', e); }
    },

    get plexIsAdmin() {
      // No auth gate (or not signed in) → full access, admin included.
      return !this.plexUser || this.plexUser.role === 'admin';
    },

    async logoutPlex() {
      try { await fetch('/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
      window.location = '/login';
    },

    // ── playlists ────────────────────────────────────────────────────────

    async loadPlexPlaylists() {
      if (!this.plexStatus?.linked) return;
      this.plexError = null;
      this.plexPlaylistsLoading = true;
      try {
        const r = await this.apiFetch('/plex/playlists');
        const data = await r.json();
        if (!r.ok) { this.plexError = data.detail || 'Failed to load playlists'; return; }
        this.plexPlaylists = data.playlists;
        if (this.selectedPlexPlaylistKey &&
            !this.plexPlaylists.find(p => p.rating_key === this.selectedPlexPlaylistKey)) {
          this.selectedPlexPlaylistKey = null;
          this.plexPlaylist = null;
        }
      } catch (e) {
        if (e.message !== 'auth_expired') this.plexError = 'Failed to load playlists';
      } finally { this.plexPlaylistsLoading = false; }
    },

    async pickPlexPlaylist(key) {
      this.selectedPlexPlaylistKey = key;
      this.plexPlaylistLoading = true;
      this.plexError = null;
      try {
        const r = await this.apiFetch('/plex/playlists/' + encodeURIComponent(key));
        const data = await r.json();
        if (this.selectedPlexPlaylistKey !== key) return;  // switched away mid-fetch
        if (!r.ok) { this.plexError = data.detail || 'Failed to load playlist'; return; }
        this.plexPlaylist = data;
      } catch (e) {
        if (e.message !== 'auth_expired') this.plexError = 'Failed to load playlist';
      } finally { this.plexPlaylistLoading = false; }
    },

    get selectedPlexPlaylistObj() {
      return this.plexPlaylists.find(p => p.rating_key === this.selectedPlexPlaylistKey) || null;
    },

    _plexTrackOnDevice(t) {
      if (!this.library) return false;
      if (!this._deviceMap) this._buildDeviceMap();
      return this._deviceMap.has(this._trackKey(t.artist, t.album, t.track_nr, t.title, t.disc_nr));
    },

    get plexSyncablePaths() {
      if (!this.plexPlaylist) return [];
      const paths = [];
      const seen = new Set();
      for (const t of this.plexPlaylist.tracks) {
        if (!t.matched || !t.path || seen.has(t.path)) continue;
        if (this._plexTrackOnDevice(t)) continue;
        seen.add(t.path);
        paths.push(t.path);
      }
      return paths;
    },

    // One candidate-key list per playlist member, in order. The source file's
    // tag key goes first — after gpod-cp the iPod carries the file's tags, not
    // Plex's (agents often rewrite artist/album). Plex-tag keys are fallbacks.
    get _plexPlaylistTrackKeys() {
      if (!this.plexPlaylist) return [];
      return this.plexPlaylist.tracks.map(t => {
        const keys = [];
        if (t.source_key) keys.push(t.source_key);
        keys.push(this._trackKey(t.artist, t.album, t.track_nr, t.title, t.disc_nr));
        if (t.grandparent_artist && t.grandparent_artist !== t.artist)
          keys.push(this._trackKey(t.grandparent_artist, t.album, t.track_nr, t.title, t.disc_nr));
        return [...new Set(keys)];
      });
    },

    async syncPlexPlaylist() {
      if (!this.plexPlaylist || !this.selectedDevnode) return;
      const paths = this.plexSyncablePaths;
      const name = this.selectedPlexPlaylistObj?.title || '';
      if (!name || (!paths.length && !this.plexPlaylist.matched_count)) return;
      try {
        const r = await this.apiFetch('/library/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            devnode: this.selectedDevnode,
            copy_paths: paths,
            delete_ids: [],
            copy_track_count: paths.length,
            media_type: 'music',
            playlist: { name, track_keys: this._plexPlaylistTrackKeys },
          }),
        });
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          alert(data.detail || 'Sync failed to start');
        }
      } catch (e) { alert('Sync failed: ' + e.message); }
    },

    // ── link flow ────────────────────────────────────────────────────────

    async startPlexLink() {
      // window.open must run synchronously in the click handler — popup blockers.
      const popup = window.open('about:blank', 'plexlink', 'width=600,height=700');
      this.plexLinkBusy = true;
      this.plexLinkStatus = 'Contacting plex.tv…';
      this.plexLinkServers = null;
      try {
        const r = await fetch('/plex/link/pin', { method: 'POST' });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || 'Failed to start link');
        if (popup) popup.location = data.auth_url;
        else this.plexLinkStatus = 'Popup blocked — open ' + data.auth_url + ' manually';
        this.plexLinkPinId = data.pin_id;
        this.plexLinkCode = data.code;
        if (popup) this.plexLinkStatus = 'Waiting for approval in Plex…';
        this._startPlexLinkPoll();
      } catch (e) {
        if (popup) popup.close();
        this.plexLinkBusy = false;
        this.plexLinkStatus = e.message;
      }
    },

    _startPlexLinkPoll() {
      const startedAt = Date.now();
      this._plexLinkTimer = setInterval(async () => {
        if (Date.now() - startedAt > 5 * 60 * 1000) {
          this._stopPlexLinkPoll('Link timed out. Try again.');
          return;
        }
        try {
          const r = await fetch('/plex/link/pin/check', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pin_id: this.plexLinkPinId, code: this.plexLinkCode }),
          });
          const data = await r.json();
          if (data.pending) return;
          if (!r.ok) { this._stopPlexLinkPoll(data.detail || 'Link failed'); return; }
          clearInterval(this._plexLinkTimer);
          this._plexLinkTimer = null;
          this.plexLinkServers = data.servers;
          this.plexLinkStatus = data.servers.length
            ? 'Choose the server to link'
            : 'No Plex servers found on this account';
          this.plexLinkBusy = false;
          // Preselect after the <select> options render — setting the model in the
          // same tick the x-for options are created leaves the select out of sync.
          this.$nextTick(() => {
            this.plexLinkServerId = data.servers[0]?.machine_id ?? null;
          });
        } catch (e) { /* transient — keep polling */ }
      }, 2000);
    },

    _stopPlexLinkPoll(msg) {
      if (this._plexLinkTimer) { clearInterval(this._plexLinkTimer); this._plexLinkTimer = null; }
      this.plexLinkBusy = false;
      this.plexLinkStatus = msg || null;
    },

    async finishPlexLink() {
      if (!this.plexLinkServerId) return;
      this.plexLinkBusy = true;
      this.plexLinkStatus = 'Connecting to server…';
      try {
        const r = await fetch('/plex/link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pin_id: this.plexLinkPinId, machine_id: this.plexLinkServerId }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || 'Link failed');
        this.plexStatus = data;
        this.plexLinkServers = null;
        this.plexLinkStatus = null;
        await this.loadPlexUsers();
      } catch (e) {
        this.plexLinkStatus = e.message;
      } finally { this.plexLinkBusy = false; }
    },

    async unlinkPlex() {
      if (!confirm('Unlink the Plex server? This disables Plex sign-in and removes all Plex sessions, users, and track mappings.')) return;
      try {
        const r = await fetch('/plex/unlink', { method: 'POST' });
        if (!r.ok) { alert('Unlink failed'); return; }
        this.plexStatus = await r.json();
        this.plexUsers = [];
        this.plexPlaylists = [];
        this.plexPlaylist = null;
        this.selectedPlexPlaylistKey = null;
        if (this.mediaType === 'playlist') this.setMediaType('music');
      } catch (e) { alert('Unlink failed: ' + e.message); }
    },

    async togglePlexAuth() {
      const target = !this.plexStatus?.auth_enabled;
      if (target && !confirm('Require Plex sign-in for everyone using nasTune?\n\nOnly your Plex account and approved users with access to the linked server will be able to log in.')) return;
      try {
        const r = await fetch('/plex/auth', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: target }),
        });
        const data = await r.json();
        if (!r.ok) { alert(data.detail || 'Failed to change sign-in setting'); return; }
        this.plexStatus.auth_enabled = data.auth_enabled;
        if (data.auth_enabled) {
          // Gate is now up but this browser has no session yet — sign in.
          window.location = '/login';
        }
      } catch (e) { alert('Failed: ' + e.message); }
    },

    // ── users ────────────────────────────────────────────────────────────

    async loadPlexUsers() {
      if (!this.plexStatus?.linked) return;
      try {
        const r = await fetch('/plex/users');
        if (r.ok) this.plexUsers = (await r.json()).users;
      } catch (e) { console.error('loadPlexUsers:', e); }
    },

    async setPlexUserEnabled(user, enabled) {
      try {
        const r = await fetch('/plex/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plex_user_id: user.plex_user_id, enabled }),
        });
        if (!r.ok) { alert('Failed to update user'); return; }
        user.enabled = enabled ? 1 : 0;
      } catch (e) { alert('Failed: ' + e.message); }
    },

    // ── import ───────────────────────────────────────────────────────────

    async startPlexImport() {
      try {
        const r = await fetch('/plex/import', { method: 'POST' });
        const data = await r.json().catch(() => ({}));
        if (!r.ok && r.status !== 409) { alert(data.detail || 'Import failed to start'); return; }
        this.plexImport = { status: 'running', processed: 0, total: 0 };
        this._pollPlexImport();
      } catch (e) { alert('Import failed: ' + e.message); }
    },

    _pollPlexImport() {
      if (this._plexImportTimer) return;
      this._plexImportTimer = setInterval(async () => {
        try {
          const r = await fetch('/plex/import/status');
          if (!r.ok) return;
          this.plexImport = await r.json();
          if (this.plexImport.status !== 'running') {
            clearInterval(this._plexImportTimer);
            this._plexImportTimer = null;
            if (this.plexImport.status === 'done' && this.selectedSourceId)
              await this._loadSourceLibrary(this.selectedSourceId);
          }
        } catch (e) { /* transient */ }
      }, 2000);
    },

    plexImportLabel() {
      const s = this.plexImport;
      if (!s) return '';
      if (s.status === 'running') return 'Importing… ' + s.processed + (s.total ? ' / ' + s.total : '');
      if (s.status === 'done') return 'Done: ' + s.ratings + ' ratings, ' + s.playcounts + ' play counts';
      if (s.status === 'error') return 'Error: ' + (s.error || 'unknown');
      return '';
    },
  };
}
