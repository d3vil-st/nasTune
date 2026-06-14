function utilsModule() {
  return {
    fmtDur(ms) {
      if (!ms) return '—';
      const s = Math.round(ms / 1000);
      return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    },

    fmtTime(s) {
      if (!s || isNaN(s) || !isFinite(s)) return '—';
      s = Math.floor(s);
      return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    },

    fmtTotalDur(tracks) {
      const ms = (tracks || []).reduce((s, t) => s + t.duration_ms, 0);
      const s = Math.round(ms / 1000);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      return h > 0 ? `${h} hr ${m} min` : `${m} min`;
    },

    fmtShort(ft) {
      if (!ft) return '?';
      if (ft.includes('Apple Lossless') || ft.includes('ALAC')) return 'ALAC';
      if (ft.includes('AAC'))  return 'AAC';
      if (ft.includes('MP3') || ft.includes('MPEG')) return 'MP3';
      if (ft.includes('WAV'))  return 'WAV';
      if (ft.includes('AIFF')) return 'AIFF';
      return ft.split(' ')[0].slice(0, 5).toUpperCase();
    },

    fmtClass(ft) {
      const s = this.fmtShort(ft);
      return { ALAC: 'fmt-alac', AAC: 'fmt-aac', MP3: 'fmt-mp3', WAV: 'fmt-wav' }[s] || 'fmt-other';
    },

    fmtRating(r) {
      if (!r) return '—';
      const stars = Math.round(r / 20);
      return '★'.repeat(stars) + '☆'.repeat(5 - stars);
    },

    fmtQuality(t) {
      const fmt = this.fmtShort(t.filetype);
      if (['ALAC', 'WAV', 'AIFF', 'FLAC'].includes(fmt))
        return t.samplerate ? (t.samplerate / 1000).toFixed(1) + ' kHz' : '—';
      return t.bitrate ? t.bitrate + ' kbps' : '—';
    },

    fmtDate(ts) {
      if (!ts) return '—';
      return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    },

    fmtSize(bytes) {
      if (!bytes) return '0 B';
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
      if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
      return (bytes / 1073741824).toFixed(2) + ' GB';
    },

    artStyle(t) {
      return this.gradientFor(t ? t.title + t.id : '');
    },

    gradientFor(seed) {
      const palettes = [
        '#1a2a4a,#0c1220', '#2a1a40,#160c28', '#1a3828,#0c2018',
        '#3a281a,#28180c', '#1a1a3a,#0c0c20', '#381a1c,#200c0e',
        '#1a3838,#0c2020', '#2a2818,#1c1c0c',
      ];
      let h = 0;
      for (const c of String(seed)) h = ((h << 5) - h + c.charCodeAt(0)) | 0;
      const [c1, c2] = palettes[Math.abs(h) % palettes.length].split(',');
      return `background:linear-gradient(145deg,${c1} 0%,${c2} 100%)`;
    },

    _normStr(s) {
      // NFD-decompose so accented chars shed their diacritic (ö→o, é→e),
      // then keep only ASCII alphanumerics. Everything else (punctuation,
      // Unicode hyphens/apostrophes/quotes, remaining non-ASCII) collapses
      // to a space, so no variant list is needed.
      return (s || '')
        .normalize('NFD')
        .replace(/̀-ͯ/g, '')    // strip combining diacritical marks
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, ' ')      // non-alphanumeric → space
        .trim();
    },

    _trackKey(artist, album, track_nr, title, disc_nr) {
      const a  = this._normStr(artist);
      const al = this._normStr(album);
      const nr = track_nr != null && track_nr > 0 ? String(track_nr) : '';
      const t  = this._normStr(title);
      const d  = disc_nr != null && disc_nr > 1 ? disc_nr + '.' : '';
      return a + '|||' + al + '|||' + d + (nr || t);
    },

    srcFmtShort(t) {
      if (t.codec) {
        if (t.codec === 'alac') return 'ALAC';
        if (t.codec.startsWith('mp4a') || t.codec === 'aac') return 'AAC';
      }
      const ext = (t.path || '').split('.').pop().toLowerCase();
      const map = { mp3: 'MP3', m4a: 'AAC', aac: 'AAC', flac: 'FLAC', aiff: 'AIFF', aif: 'AIFF', wav: 'WAV', ogg: 'OGG' };
      return map[ext] || ext.toUpperCase().slice(0, 5) || '?';
    },

    srcFmtClass(t) {
      const s = this.srcFmtShort(t);
      return { ALAC: 'fmt-alac', FLAC: 'fmt-alac', AAC: 'fmt-aac', MP3: 'fmt-mp3', WAV: 'fmt-wav' }[s] || 'fmt-other';
    },

    srcQuality(t) {
      const fmt = this.srcFmtShort(t);
      if (['ALAC', 'FLAC', 'WAV', 'AIFF'].includes(fmt)) {
        const khz = t.samplerate ? (t.samplerate / 1000).toFixed(1) + ' kHz' : '';
        const bit = t.bits_per_sample ? t.bits_per_sample + '-bit' : '';
        return [bit, khz].filter(Boolean).join(' / ') || '—';
      }
      return t.bitrate ? t.bitrate + ' kbps' : '—';
    },

    sourceDotClass(s) {
      if (!s) return 'src-dot-pending';
      return { done: 'src-dot-done', scanning: 'src-dot-scanning', error: 'src-dot-error' }[s.scan_status] || 'src-dot-pending';
    },

    themeMode: localStorage.getItem('nastune-theme') || 'auto',

    setTheme(mode) {
      this.themeMode = mode;
      localStorage.setItem('nastune-theme', mode);
      const isLight = mode === 'light' ||
        (mode === 'auto' && matchMedia('(prefers-color-scheme: light)').matches);
      document.documentElement.classList.toggle('light', isLight);
    },

    initTheme() {
      matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
        if (this.themeMode === 'auto') this.setTheme('auto');
      });
    },
  };
}
