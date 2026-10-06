/**
 * app.js — Trellis Music Library SPA: router, state, and view renderers.
 *
 * Hash-based routing:
 *   #/                  → catalog (artist selected or empty state)
 *   #/artist/:id        → artist recordings list
 *   #/recording/:id     → recording detail (tracks + info file)
 *   #/ingest            → ingest wizard (stub for MVP)
 */

const App = (() => {

  // ── State ──────────────────────────────────────────────────────────────────
  const state = {
    user:            null,
    artists:         [],
    selectedArtist:  null,   // { id, name, ... }
    currentRecId:    null,   // recording id currently in detail view
    playingTrackId:  null,   // track id currently in player
    skipNonMusic:    false,  // filter announcements/banter/tuning from queue
    // Generic "where did I come from" navigation tracking (2026-07-23),
    // replacing three earlier ad hoc mechanisms (a selectedArtist-based
    // back-link that only worked one hop, a one-shot recFrom that only
    // covered Recording→Artist/Venue, and several hardcoded '#/'
    // fallbacks) — see route() for how these are kept in sync, and the
    // 2026-07-23 project memory entry for the bug this fixed (Recently
    // Added → Recording → Back landed on Library instead of Recently Added).
    //   navCurrent — { hash, label } for the page ON SCREEN right now, set
    //     by that page's own render function once its label is known
    //     (setNavCurrent()). A same-page reload (direct render*View() call,
    //     not a hash change) just re-sets this to the same value — harmless.
    //   navBack — { hash, label } | null, the page that was on screen
    //     immediately before the CURRENT one. This is what every "← Back"
    //     link in the app should point to. Snapshotted from navCurrent by
    //     route() itself, and ONLY on a genuine hash change — never on the
    //     first-ever dispatch (nothing preceded it) or a same-hash
    //     re-dispatch (a reload must never overwrite the real back target).
    navCurrent:      null,
    navBack:         null,
    // Bulk Ingest (spec 1.9/4, chunk 7c) — just enough of /api/bulk-ingest/
    // current's own shape to decide the nav item and home routing without
    // every reader re-fetching it. null until the boot fetch (or a later
    // refresh from the bulkIngest page itself) resolves; {run: null} once
    // resolved with nothing active. See refreshBulkIngestStatus().
    bulkIngest:        null,
  }

  // ── Track flag registry — single source of truth ─────────────────────────
  // Every flag list/label/skip-set is derived from this one array. `nonMusic`
  // marks flags whose tracks are skipped by the "Skip non-music" filter.
  //
  // ALPHABETICAL BY LABEL (Ryan, 2026-09-01). The previous order was the order
  // the flags were thought of in, which is not an order anyone can predict —
  // twelve pills in an arbitrary sequence means reading all twelve every time
  // to find one. Alphabetical is the only ordering a reader can navigate
  // without learning it first.
  //
  // ⚠ Nothing may depend on this ORDER. `NON_MUSIC_FLAGS` and `FLAG_LABELS`
  // below are both order-independent, and `trackChipsArray` iterates the
  // TRACK'S own `flags` array rather than this one, so chip order on a row is
  // unaffected. Keep it that way: this list is a vocabulary, not a sequence.
  const TRACK_FLAGS = [
    { key: 'announcement',    label: 'Announcement',    nonMusic: true  },
    { key: 'audience',        label: 'Audience',        nonMusic: true  },
    { key: 'band_intros',     label: 'Band Intros',     nonMusic: true  },
    { key: 'banter',          label: 'Banter',          nonMusic: true  },
    { key: 'end_truncated',   label: 'End Truncated',   nonMusic: false },
    { key: 'incomplete',      label: 'Incomplete',      nonMusic: false },
    { key: 'interview',       label: 'Interview',       nonMusic: true  },
    { key: 'introduction',    label: 'Introduction',    nonMusic: true  },
    { key: 'medley',          label: 'Medley',          nonMusic: false },
    { key: 'start_truncated', label: 'Start Truncated', nonMusic: false },
    { key: 'tuning',          label: 'Tuning',          nonMusic: true  },
    { key: 'unknown_title',   label: 'Unknown Title',   nonMusic: false },
  ]
  const NON_MUSIC_FLAGS = TRACK_FLAGS.filter(f => f.nonMusic).map(f => f.key)
  const FLAG_LABELS     = Object.fromEntries(TRACK_FLAGS.map(f => [f.key, f.label]))

  // ── Placeholder venue names ("Unknown Venue", "TBD", ...) ──────────────────
  // These aren't real, canonical physical places — they're a stand-in every
  // show without a known venue reuses. Must mirror app/utils/venues.py's
  // PLACEHOLDER_VENUE_NAMES exactly (Ryan, 2026-07-15 — see that module's
  // docstring for the full contamination story and the confirmed audit).
  const PLACEHOLDER_VENUE_NAMES = new Set(['unknown venue', 'unknown', 'tbd', 'n/a', 'various'])
  function isPlaceholderVenue(name) {
    return !!name && PLACEHOLDER_VENUE_NAMES.has(String(name).trim().toLowerCase())
  }

  /** Official badge + flag chips ("bubble tags") for a track, as an ordered
   *  array of individual chip HTML strings — official badge first, then each
   *  flag. Add Recording's track table (renderIngestReview) uses the array
   *  directly: first chip stays under the title, any rest go in a dedicated
   *  full-width row (Ryan, 2026-07-15 — stacking multiples under the title
   *  in that narrow input-constrained cell was pushing the title text up). */
  function trackChipsArray(t, opts) {
    const chips = []
    // Add Recording passes { hideOfficial: true } (Ryan, 2026-08-08): the
    // form already has its own "Official release" checkbox right below the
    // track table, so a © badge on every row it cascades to is redundant —
    // View Recording (the only other caller) keeps showing it, since that's
    // a read-only page with no checkbox in view.
    if (t.is_official && !(opts && opts.hideOfficial)) {
      chips.push(`<span class="track-official-badge" title="Officially released">©</span>`)
    }
    ;(t.flags || []).forEach(f => chips.push(`<span class="track-flag-chip">${FLAG_LABELS[f] || f}</span>`))
    // The non-music audio signal is deliberately NOT surfaced here (Ryan,
    // 2026-08-28). It exists to inform the ingestion and metadata engines,
    // not to put a second, hedged opinion next to a real flag in the UI.
    return chips
  }

  /** Official badge + flag chips joined into one string — View Recording's
   *  track title shares this one line inline (no width constraint there, so
   *  no need to split first-chip/rest like Add Recording does). */
  function trackBadgesHtml(t) {
    return trackChipsArray(t).join('')
  }

  /** Apply/remove the skip-filter visual state to all track rows in the current view. */
  function applySkipFilter() {
    document.querySelectorAll('.track-row[data-flags]').forEach(row => {
      const flags = (row.dataset.flags || '').split(',').filter(Boolean)
      const isNonMusic = flags.some(f => NON_MUSIC_FLAGS.includes(f))
      row.classList.toggle('track-row--skipped', state.skipNonMusic && isNonMusic)
    })
  }

  /** Single source of truth for toggling the filter — syncs all UIs. */
  function setSkipFilter(v) {
    state.skipNonMusic = v
    document.querySelectorAll('.skip-filter-cb').forEach(cb => { cb.checked = v })
    applySkipFilter()
  }

  // ── Waveform (wavesurfer.js) ──────────────────────────────────────────────
  // Officially adopted 2026-07-15 (was a spike prototype) — replaces the old
  // hand-rolled canvas RAF-loop renderer. Ryan: "fully wired into the
  // persistent player. It should not be separate." Deliberately does NOT use
  // wavesurfer's own `media`/`url` binding, though — that mechanism fetches
  // the whole file as a blob to decode it, which (a) defeats the browser's
  // native HTTP range-request streaming we rely on for large lossless files
  // and (b) replaces the shared #audio-el's src with a blob: URL that gets
  // revoked on destroy(), risking a playback interruption just from
  // navigating away. Instead: wavesurfer renders purely from our own
  // precomputed peaks (`_waveformMap`, already computed server-side — no
  // network fetch at all) and its OWN internal silent audio element, which
  // we never play. All REAL playback stays owned by Player/#audio-el, the
  // one true audio channel:
  //   - click/drag on the waveform → 'interaction' event → we set
  //     #audio-el's currentTime directly (loading this recording's queue
  //     first, paused, if it wasn't already the active one)
  //   - #audio-el's real timeupdate → wsInstance.setTime(...), which only
  //     moves wavesurfer's own silent cursor/renders progress, never plays
  //     anything — see the one-time listener below.
  let _waveformMap      = {}   // trackId → waveform data (also the "has analysis" check)
  let _trackDurationMap = {}   // trackId → duration, needed alongside peaks when (re)loading wavesurfer
  let _wsInstance       = null
  let _wsTrackId        = null

  function _cancelWaveform() {
    if (_wsInstance) { try { _wsInstance.destroy() } catch (_) {} }
    _wsInstance = null
    _wsTrackId  = null
  }

  /** wavesurfer's `peaks` option wants a flat array of -1..1 values per
   * channel. Our precomputed data is either v2 {min:[...], max:[...]} (real
   * peak envelope) or v1 a flat mirrored-magnitude array (pre-bump tracks) —
   * `.max` alone reads fine as a single-channel peaks array either way. */
  function _peaksForTrack(trackId) {
    const wf = _waveformMap[trackId]
    if (!wf) return null
    const arr = Array.isArray(wf) ? wf : wf.max
    return (arr && arr.length) ? [arr] : null
  }

  // One-time sync: whenever the REAL shared audio element advances, mirror
  // its position onto wavesurfer's own (silent, unplayed) cursor so the
  // waveform's progress indicator always matches actual playback — without
  // wavesurfer ever touching the real audio itself.
  ;(function () {
    const audio = document.getElementById('audio-el')
    if (!audio) return
    audio.addEventListener('timeupdate', () => {
      if (_wsInstance && _wsTrackId != null && Player.currentId() === _wsTrackId) {
        _wsInstance.setTime(audio.currentTime)
      }
    })
  })()

  // Ingest wizard state — persists across step renders
  const ingest = {
    step:       'source',  // 'source' | 'review' | 'success'
    folderPath: null,
    scan:       null,      // full scan API response
    form: {},              // resolved metadata (populated on review step)
    tracks:     [],        // array of { track_number, title, set, duration, filename }
    // Set to a run's import page hash ('#/bulk-ingest/<id>') when this review
    // was opened from that page's per-item Review. Back and Add & Return land
    // there instead of the picker or the new recording's page.
    returnTo:   null,
  }

  // ── DOM refs ───────────────────────────────────────────────────────────────
  const loginScreen = document.getElementById('login-screen')
  const appShell    = document.getElementById('app-shell')
  const mainContent = document.getElementById('main-content')
  const userAvatar  = document.getElementById('user-avatar')
  const userName    = document.getElementById('user-name')

  // ── Kill native spellcheck/autocorrect on text inputs ─────────────────────
  // This app runs inside PyWebView's underlying WKWebView, which applies
  // macOS's own spellcheck/text-replacement to any unmarked text input — pops
  // an unwanted correction bubble while typing artist/venue/person names
  // (proper nouns trip it constantly; Ryan, 2026-07-23, typing "Ricky
  // Simpkins" got auto-"corrected" toward "Simpkin's"). Delegated on focusin
  // at the document level rather than patched into every input's template —
  // most of these inputs (add-picker rows, inline edits) are created well
  // after their page's own setMainHTML() call, so a one-time sweep wouldn't
  // reach them; this catches every text input, present and future.
  document.addEventListener('focusin', e => {
    const el = e.target
    if (el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'search')) {
      el.spellcheck = false
      el.setAttribute('autocorrect', 'off')
      el.setAttribute('autocapitalize', 'off')
    }
  })

  // ── Palette ────────────────────────────────────────────────────────────────
  // Seven named schemes, v1, chosen 2026-08-30. This registry carries ids,
  // labels and one line of description — NO colours. Every hex lives in the
  // palette blocks in main.css, and the Settings picker paints its swatches
  // straight from those blocks via [data-pal-preview], so the JS and the CSS
  // cannot drift apart about what a scheme looks like. Adding a scheme means a
  // block there and an entry here, with matching ids.
  //
  // The id sits on <html>, not on <body> the way the old theme class did. That
  // was a real bug and not a stylistic choice: buildWaveform() reads --t2 /
  // --accent / --accent-lit off document.documentElement, so a palette
  // declared on <body> never reached it and light mode drew a dark-theme
  // waveform. Anything reading a token off :root now gets the live scheme.
  //
  // Still localStorage rather than the server, unchanged from the old theme
  // toggle: it is per-machine, and it must apply before the first paint — see
  // the inline script in index.html, which stamps the saved id and knows
  // nothing else about palettes.
  // `mode` is text polarity ('light' = light ground, dark text); main.css keys
  // its light/dark rules on it. `group` is only where the picker shows it.
  const PALETTES = [
    { id: 'steely',  label: 'Steely',  mode: 'light', group: 'light', note: 'Cool paper, petrol accent' },
    { id: 'krauss',  label: 'Krauss',  mode: 'light', group: 'light', note: 'Warm ivory, sage accent' },
    { id: 'monk',    label: 'Monk',    mode: 'light', group: 'light', note: 'Near-white, ink, bold blue' },
    { id: 'joni',    label: 'Joni',    mode: 'light', group: 'mid',   note: 'Powder blue-grey, deep indigo' },
    { id: 'gillian', label: 'Gillian', mode: 'light', group: 'mid',   note: 'Dust-bowl khaki, oxblood' },
    { id: 'hazel',   label: 'Hazel',   mode: 'light', group: 'mid',   note: 'Grey-green, dark plum' },
    { id: 'chet',    label: 'Chet',    mode: 'dark',  group: 'mid',   note: 'Smoky slate, muted brass' },
    { id: 'townes',  label: 'Townes',  mode: 'dark',  group: 'mid',   note: 'Olive drab, faded denim' },
    { id: 'gram',    label: 'Gram',    mode: 'dark',  group: 'mid',   note: 'Spruce green, faded rose' },
    { id: 'cale',    label: 'Cale',    mode: 'dark',  group: 'dark',  note: 'Warm ash, amber-tan accent' },
    { id: 'miles',   label: 'Miles',   mode: 'dark',  group: 'dark',  note: 'Midnight slate, icy blue' },
    { id: 'alice',   label: 'Alice',   mode: 'dark',  group: 'dark',  note: 'Deep aubergine, gold accent' },
    { id: 'eno',     label: 'Eno',     mode: 'dark',  group: 'dark',  note: 'Neutral graphite, chrome blue' },
  ]
  // Cale is the app default because it is the palette every existing install is
  // already running — shipping a new set should not silently relight anyone's
  // app. Moving the default is this one string.
  const DEFAULT_PALETTE_ID = 'cale'
  const PALETTE_KEY = 'trellisPalette'

  const paletteById = id => PALETTES.find(p => p.id === id) || null

  function currentPalette() {
    return paletteById(document.documentElement.getAttribute('data-palette'))
        || paletteById(DEFAULT_PALETTE_ID)
  }

  /** Apply and persist. Returns the palette, or null for an id we do not have,
   *  so a caller can tell a no-op from a switch. */
  function setPalette(id) {
    const p = paletteById(id)
    if (!p) return null
    document.documentElement.setAttribute('data-palette', p.id)
    localStorage.setItem(PALETTE_KEY, p.id)
    return p
  }

  // The inline script stamps whatever string it found without validating it,
  // because it has no registry to validate against. Correct it here, once, on
  // load: an id left behind by a build that had a scheme this one does not
  // would otherwise sit in localStorage forever, rendering as the :root
  // fallback while Settings showed nothing selected.
  setPalette(currentPalette().id)

  // ── File handling: shared naming constants, cached reader, reiteration
  //    strip (spec sections 4 and 6.3) ─────────────────────────────────────
  const NAMING_SCHEME_LABELS = [
    ['original',      'Keep original'],
    ['number_title',  'Number and title'],
    ['etree',         'etree'],
    ['etree_sets',    'etree, sets'],
    ['custom',        'Custom'],
  ]
  // Mirrors app/utils/file_naming.py's PRESETS — display only, so the
  // Settings template field shows something real for a preset scheme
  // instead of sitting blank.
  const NAMING_PRESET_TEMPLATES = {
    original:     '{original}',
    number_title: '{track} - {title}',
    etree:        '{artist_abbr}{date}[.{source:lower}][.{source_tag}][.{shnid}].[d{disc}]t{track_in_disc}',
    etree_sets:   '{artist_abbr}{date}[.{source:lower}][.{source_tag}][.{shnid}][s{set}]t{track_in_set}',
  }
  const NAMING_TOKENS = [
    'artist', 'artist_abbr', 'date', 'year', 'venue', 'location', 'source',
    'source_tag', 'shnid', 'disc', 'track_in_disc', 'set', 'track_in_set',
    'track', 'title', 'original',
  ]

  let _fileHandlingCache = null
  // Cached for the life of the page load; Settings clears it (passing
  // force=true) whenever it saves a change, so the reiteration strip on the
  // ingest screens never shows a value the user just overwrote.
  async function fileHandling(force) {
    if (_fileHandlingCache && !force) return _fileHandlingCache
    _fileHandlingCache = await API.system.getFileHandling()
    return _fileHandlingCache
  }

  /** Fills a container (by id) with the reiteration strip once file handling
   *  has loaded. The three ingest screens render the container empty (its
   *  content depends on an async fetch) and call this right after. */
  async function _wireFhStrip(containerId) {
    const el = document.getElementById(containerId)
    if (!el) return
    try {
      el.innerHTML = fileHandlingStripHtml(await fileHandling())
    } catch (e) { /* leave it empty rather than showing a broken strip */ }
  }

  function fileHandlingStripHtml(fh) {
    // Reduced to the mode label + Change (2026-09-27, unified ingest queue
    // table): the rename/tag detail this used to spell out in full every
    // time now lives only in Settings, which keeps its own labels.
    const modeLabel = fh.file_handling_mode === 'organize' ? 'Organize my files' : 'Keep my files as-is'
    return `<span class="fh-strip">
      <b>${esc(modeLabel)}</b><span class="sep">·</span>
      <a href="#/settings/files" class="change">Change</a></span>`
  }

  // ── Resizable sidebar ──────────────────────────────────────────────────────
  ;(function () {
    const MIN = 200, MAX = 460
    const setW = w => document.documentElement.style.setProperty('--sidebar-w', Math.round(w) + 'px')
    const saved = parseInt(localStorage.getItem('trellisSidebarW'), 10)
    if (saved && saved >= MIN && saved <= MAX) setW(saved)

    const handle = document.getElementById('sidebar-resizer')
    if (!handle) return
    let dragging = false
    handle.addEventListener('mousedown', e => {
      dragging = true
      handle.classList.add('dragging')
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      e.preventDefault()
    })
    window.addEventListener('mousemove', e => {
      if (!dragging) return
      setW(Math.max(MIN, Math.min(e.clientX, MAX)))
    })
    window.addEventListener('mouseup', () => {
      if (!dragging) return
      dragging = false
      handle.classList.remove('dragging')
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      const cur = getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w').trim()
      localStorage.setItem('trellisSidebarW', parseInt(cur, 10))
    })
  })()

  // ── Utilities ──────────────────────────────────────────────────────────────

  function fmtDate(year, month, day) {
    if (!year) return 'Unknown date'
    if (month && day) return `${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`
    if (month) return `${year}-${String(month).padStart(2,'0')}`
    return String(year)
  }

  function fmtDateLong(year, month, day) {
    if (!year) return 'Unknown date'
    const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    if (month && day) return `${MONTHS[month-1]} ${day}, ${year}`
    if (month) return `${MONTHS[month-1]} ${year}`
    return String(year)
  }

  // Start date, extended with an end date when the performance spans more
  // than one day (2026-07-23 — e.g. the Danny Gatton Cellar Door stand,
  // start/end a day apart). Same month+year → compact "Jan 25–26, 1979";
  // otherwise a full "Start – End" range.
  function fmtDateRangeLong(perf) {
    const start = fmtDateLong(perf.start_year, perf.start_month, perf.start_day)
    if (!perf.end_year && !perf.end_month && !perf.end_day) return start
    if (perf.end_year === perf.start_year && perf.end_month === perf.start_month && perf.end_day) {
      const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
      return `${MONTHS[perf.start_month-1]} ${perf.start_day}–${perf.end_day}, ${perf.start_year}`
    }
    const end = fmtDateLong(perf.end_year || perf.start_year, perf.end_month, perf.end_day)
    return `${start} – ${end}`
  }

  function fmtLocation(city, state, country) {
    if (city && state)   return `${city}, ${state}`
    if (city && country) return `${city}, ${country}`
    return city || state || country || ''
  }

  function fmtDuration(secs) {
    if (!secs) return '—'
    const m = Math.floor(secs / 60)
    const s = Math.floor(secs % 60)
    return `${m}:${s.toString().padStart(2,'0')}`
  }

  // Show-length runtime, e.g. "1h 42m" or "47m" — for the catalog length column.
  function fmtRuntime(secs) {
    if (!secs) return ''
    const totalMin = Math.round(secs / 60)
    const h = Math.floor(totalMin / 60)
    const m = totalMin % 60
    return h ? `${h}h ${m}m` : `${m}m`
  }

  // Compact "date added" (ingest timestamp) for the catalog column — ISO date only.
  function fmtDateAdded(iso) {
    return iso ? iso.slice(0, 10) : ''
  }

  // Studio vs live naming layer (Studio Records spec v1, chunk 5 — Ryan
  // approved). Every render helper that decides how to label a recording row
  // routes through this instead of assuming a live show's date/venue/source.
  // Live (kind !== 'studio', including missing kind) returns exactly what
  // every call site already computed before this helper existed, so nothing
  // about live rendering changes. Studio never carries a venue, location or
  // source -- `lead`/`sub`/`dateText` are the only three fields a studio row
  // ever needs.
  function recIdentity(r) {
    const isStudio = !!(r && r.kind === 'studio')
    if (!isStudio) {
      return {
        lead:     (r && r.artist) || '',
        sub:      (r && r.venue) || '',
        dateText: r ? fmtDate(r.start_year, r.start_month, r.start_day) : '',
        isStudio: false,
      }
    }
    const hasTitle = !!(r.title)
    return {
      lead:     r.title || r.artist || '',
      sub:      hasTitle ? (r.artist || '') : '',
      dateText: r.start_year ? String(r.start_year) : '',
      isStudio: true,
    }
  }

  // Date comparator shared by every list that sorts recordings/performances
  // chronologically. A dateless studio row (no start_year at all) sorts by
  // year like everything else, and lands LAST whichever direction is asked
  // for -- `desc` reverses real dates, never where the nulls go.
  function compareByDate(a, b, desc) {
    const ay = a && a.start_year, by = b && b.start_year
    if (ay == null && by == null) return 0
    if (ay == null) return 1
    if (by == null) return -1
    // A year-only row (no month at all -- every studio row) compares by year
    // alone: falling to month/day with a missing one defaulted to 0 would
    // sort it as January against a real date in the same year.
    let cmp = ay - by
    if (cmp === 0 && a.start_month != null && b.start_month != null) {
      cmp = (a.start_month - b.start_month) || ((a.start_day || 0) - (b.start_day || 0))
    }
    return desc ? -cmp : cmp
  }

  function sourceBadge(source) {
    if (!source) return ''
    const cls = ['SBD','AUD','MTX','FM'].includes(source) ? `badge-${source}` : 'badge-src'
    return `<span class="badge ${cls}">${escHtml(source)}</span>`
  }

  function qualityClass(q) {
    if (!q) return ''
    const first = q[0].toUpperCase()
    if (first === 'A') return q.includes('+') ? 'quality-Ap' : q.includes('-') ? 'quality-Am' : 'quality-A'
    if (first === 'B') return q.includes('+') ? 'quality-Bp' : 'quality-B'
    if (first === 'C') return 'quality-C'
    return ''
  }

  function escHtml(s) {
    if (s == null) return ''
    return String(s)
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;')
  }

  function esc(s) { return escHtml(s) }

  // Shared byte formatter — used by the Add Recordings folder navigator's
  // size column (2026-08-22). One decimal above 1 GB, none below, because a
  // show folder in single-digit GB and a multi-disc box set in double digits
  // both want to be scannable at a glance.
  function fmtBytes(n) {
    if (!n) return '—'
    if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB'
    if (n >= 1e6) return (n / 1e6).toFixed(0) + ' MB'
    if (n >= 1e3) return (n / 1e3).toFixed(0) + ' KB'
    return n + ' B'
  }

  // Shared expand/contract chevron (Ryan, 2026-08-23 — "the tiny caret just
  // isn't big enough... do this replacement for all uses throughout the
  // site"). Replaces the bare ▸/▾ unicode triangle everywhere it was used as
  // a real expand/collapse control. A unicode triangle's visual weight varies
  // wildly by font/OS — which is why the 2026-08-02 pass that bumped these to
  // a bigger font-size and an 18px hit target (see .nav-caret) never actually
  // fixed the complaint. One crisp inline SVG instead, coloured with
  // currentColor so it inherits whatever the container already sets.
  //
  // Drawn pointing right (closed); every call site already rotates its
  // container 90° on an `.open`/`.expanded` class to mean "expanded", so that
  // CSS keeps working untouched — only the glyph itself changed. Deliberately
  // scoped to the tree/list expand-collapse family (.nav-caret, .rq-caret,
  // .lq-dir-caret, batch import's row expander, the ingest review panel
  // toggle). NOT applied to .lib-select-caret or "Actions ▾" — those open a
  // dropdown MENU, a different control with different semantics, and weren't
  // what "the expand/contract toggle" was describing. Also left alone: the
  // quality report's lq-tab chevrons and the ingest review "parsed tracks"
  // toggle, both ▴/▾ swap-based rather than rotate-based.
  //
  // ⚠ THE MENU EXCEPTION IS OVER (Ryan, 2026-08-28). Dropdown openers used a
  // DIFFERENT chevron on the theory that a menu is not an expander — true of
  // the semantics, invisible to the eye, and indefensible once the triage
  // row put "Move ⌄" forty pixels from an expand caret: two chevrons of
  // different stroke weight and different proportions, side by side. Lucide's
  // chevron in a 24-unit box renders a ~0.8px stroke at this size; this one in
  // an 8-unit box renders ~2.1px — and the THIN one is the keeper. chevronIcon()
  // now emits that Lucide glyph and is the ONLY chevron in the app.
  // ── Icons ─────────────────────────────────────────────────────────────────
  // Lucide v1.33.0, ISC — app/static/js/LUCIDE-LICENSE.txt.
  //
  // Paths are vendored VERBATIM. Do not hand-edit them and do not draw new
  // ones: copy the <path> elements out of the lucide-static package so the set
  // stays internally consistent. An icon someone drew by eye is exactly the
  // kind of tell this replaces.
  //
  // Inline SVG rather than an icon font or Unicode characters. The nav used to
  // use ◎ ✦ ♪ ♫ ＋ ↻ borrowed from the text face, which rendered at a different
  // size and weight from their own labels — that is what made one sidebar in
  // one typeface look like several.
  //
  // Naming follows the data model, and getting it backwards would be a lie
  // told in pictures: an Artist is the ACT that took the stage (users), an
  // Musician is a PERSON (user).
  //
  // Only icons actually in use belong here. A grab-bag of unused glyphs is how
  // icons end up sprinkled on everything.
  const ICONS = {
    // Research (2026-10-04). Lucide 'brain' replaced 'sparkles' on every Research
    // button; the colour is what ties those buttons together, the glyph may vary
    // with the surface. Details panel toggle: Lucide 'panel-right'.
    'brain':        '<path d="M12 18V5"/><path d="M15 13a4.17 4.17 0 0 1-3-4 4.17 4.17 0 0 1-3 4"/><path d="M17.598 6.5A3 3 0 1 0 12 5a3 3 0 1 0-5.598 1.5"/><path d="M17.997 5.125a4 4 0 0 1 2.526 5.77"/><path d="M18 18a4 4 0 0 0 2-7.464"/><path d="M19.967 17.483A4 4 0 1 1 12 18a4 4 0 1 1-7.967-.517"/><path d="M6 18a4 4 0 0 1-2-7.464"/><path d="M6.003 5.125a4 4 0 0 0-2.526 5.77"/>',
    'panel-right':  '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18"/>',
    // Lomax (2026-10-05). The tape-reel mark: two reels on a baseline. 'lomax' is the
    // 14px form beside the tab name; 'lomax-full' adds hubs and feet for the chat
    // avatar. Never on a button. 'info' is Lucide 'info', the (i) beside level
    // toggles and Resolver values.
    'info':         '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    'lomax':        '<circle cx="7" cy="11" r="4"/><circle cx="17" cy="11" r="4"/><path d="M3 19h18"/>',
    'lomax-full':   '<circle cx="7" cy="11" r="4"/><circle cx="17" cy="11" r="4"/><circle cx="7" cy="11" r="1"/><circle cx="17" cy="11" r="1"/><path d="M7 15l2 4"/><path d="M17 15l-2 4"/><path d="M3 19h18"/>',
    // Preview transport on Add Recording (2026-08-28). Lucide 'skip-back' /
    // 'skip-forward' — the SAME two glyphs the player bar draws inline in
    // index.html, so the two transports cannot drift apart. Kept here as well
    // because that bar predates this registry and never went through it.
    'skip-back':    '<path d="M17.971 4.285A2 2 0 0 1 21 6v12a2 2 0 0 1-3.029 1.715l-9.997-5.998a2 2 0 0 1-.003-3.432z"/><path d="M3 20V4"/>',
    'skip-forward': '<path d="M21 4v16"/><path d="M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z"/>',
    // Failed-ingest marker on a triage row (2026-08-28). Lucide 'circle-alert'.
    'alert':        '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    'folder-open':  '<path d=\"m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2\"/>',
    // Fingerprint verdict on the compact row (2026-08-28). Lucide 'fingerprint'.
    'fingerprint':  '<path d="M2 12C2 6.5 6.5 2 12 2a10 10 0 0 1 8 4"/><path d="M5 19.5C5.5 18 6 15 6 12c0-.7.12-1.37.34-2"/><path d="M17.29 21.02c.12-.6.43-2.3.5-3.02"/><path d="M12 10a2 2 0 0 0-2 2c0 1.02-.1 2.51-.26 4"/><path d="M8.65 22c.21-.66.45-1.32.57-2"/><path d="M14 13.12c0 2.38 0 6.38-1 8.88"/><path d="M2 16h.01"/><path d="M21.8 16c.2-2 .131-5.354 0-6"/><path d="M9 6.8a6 6 0 0 1 9 5.2c0 .47 0 1.17-.02 2"/>',
    'map-pin':      '<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>',
    'users':        '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><path d="M16 3.128a4 4 0 0 1 0 7.744"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><circle cx="9" cy="7" r="4"/>',
    'user':         '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    'tag':          '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/>',
    // Lucide 'calendar-days', for the Events dimension (2026-09-01). Copied out
    // of the package verbatim, per CONTEXT.md — never draw one by hand.
    'calendar':     '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/><path d="M8 14h.01"/><path d="M12 14h.01"/><path d="M16 14h.01"/><path d="M8 18h.01"/><path d="M12 18h.01"/><path d="M16 18h.01"/>',
    'plus':         '<path d="M5 12h14"/><path d="M12 5v14"/>',
    'minus':        '<path d="M5 12h14"/>',
    'library':      '<path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/>',
    'search':       '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>',
    'clock':        '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    // Left nav Albums entry (Studio Records spec v1, chunk 7). No 'album'
    // glyph exists in the set actually vendored into this file; Lucide
    // 'disc' is the closest match (a record) and reads at the same weight
    // as the library/search/clock icons beside it -- copied verbatim.
    'disc':         '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="2"/>',
    'arrow-left-right': '<path d="M8 3 4 7l4 4"/><path d="M4 7h16"/><path d="m16 21 4-4-4-4"/><path d="M20 17H4"/>',
    'rotate-cw':    '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
    'play':         '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
    'pause':        '<rect x="14" y="3" width="5" height="18" rx="1"/><rect x="5" y="3" width="5" height="18" rx="1"/>',
    'square':       '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    'star':         '<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/>',
    'x':            '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    'check':        '<path d="M20 6 9 17l-5-5"/>',
    'arrow-left':   '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    'chevron-left':  '<path d="m15 18-6-6 6-6"/>',
    'chevron-right': '<path d="m9 18 6-6-6-6"/>',
    'chevron-down':  '<path d="m6 9 6 6 6-6"/>',
    // Archive Downloads (2026-10-01): the sidebar's Downloads
    // link, and the drag handle on the queue's Up next rows. Lucide 'download'
    // and 'grip-vertical', copied verbatim from lucide-static. 'ticket' is
    // Live Recordings' icon (a concert).
    'ticket':       '<path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M13 5v2"/><path d="M13 17v2"/><path d="M13 11v2"/>',
    'landmark':     '<path d=\"M10 18v-7\"/><path d=\"M11.119 2.205a2 2 0 0 1 1.762 0l7.84 3.846A.5.5 0 0 1 20.5 7h-17a.5.5 0 0 1-.22-.949z\"/><path d=\"M14 18v-7\"/><path d=\"M18 18v-7\"/><path d=\"M3 22h18\"/><path d=\"M6 18v-7\"/>',
    // Reserved for the Bluegrass Archive entry (Ryan, 2026-10-01).
    'guitar':       '<path d=\"m11.9 12.1 4.514-4.514\"/><path d=\"M20.1 2.3a1 1 0 0 0-1.4 0l-1.114 1.114A2 2 0 0 0 17 4.828v1.344a2 2 0 0 1-.586 1.414A2 2 0 0 1 17.828 7h1.344a2 2 0 0 0 1.414-.586L21.7 5.3a1 1 0 0 0 0-1.4z\"/><path d=\"m6 16 2 2\"/><path d=\"M8.23 9.85A3 3 0 0 1 11 8a5 5 0 0 1 5 5 3 3 0 0 1-1.85 2.77l-.92.38A2 2 0 0 0 12 18a4 4 0 0 1-4 4 6 6 0 0 1-6-6 4 4 0 0 1 4-4 2 2 0 0 0 1.85-1.23z\"/>',
    'wrench':       '<path d=\"M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z\"/>',
    'archive':      '<rect width=\"20\" height=\"5\" x=\"2\" y=\"3\" rx=\"1\"/><path d=\"M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8\"/><path d=\"M10 12h4\"/>',
    'download':     '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    'grip-vertical': '<circle cx="9" cy="12" r="1"/><circle cx="9" cy="5" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="19" r="1"/>',
  }

  // `fill` is for the one genuine filled/outline pair we have: a favourited
  // star reads as filled, an unfavourited one as an outline. Nothing else
  // should use it — Lucide is a stroke set and filling arbitrary icons breaks
  // the family's consistency.
  function icon(name, cls, fill) {
    const d = ICONS[name]
    if (!d) return ''
    return `<svg class="tic${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" ` +
           `fill="${fill ? 'currentColor' : 'none'}" stroke="currentColor" ` +
           `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
           `aria-hidden="true">${d}</svg>`
  }

  // The Details panel's tabs, in ONE order for View Recording and Add Recording
  // (Ryan, 2026-10-04). `attr` is the data attribute each page's wiring reads
  // (data-pane / data-ipane) and `prefix` the pane-id prefix ('' / 'isp-').
  // Resolver exists only when the recording has resolver data; Lomax only
  // where the user can run it.
  const DETAILS_TABS = [
    ['info', 'Info File'], ['quality', 'Quality'], ['resolver', 'Resolver'],
    ['filetags', 'Tags'], ['checksums', 'Checksums'], ['mb', 'MusicBrainz'], ['ai', 'Lomax'],
  ]
  // An album has no Info File tab (info: false) and no Resolver; Add Recording's album layout
  // adds the MusicBrainz tab (mb: true).
  function detailsTabsHtml(attr, prefix, { resolver, research, staged, info = true, mb = false }) {
    return DETAILS_TABS
      .filter(([k]) => (k !== 'resolver' || resolver) && (k !== 'ai' || research) &&
                       (k !== 'info' || info) && (k !== 'mb' || mb))
      .map(([k, label]) => `<button class="slide-tab${k === 'ai' ? ' slide-tab--ai' : ''}` +
        `${k === 'filetags' && staged ? ' slide-tab--staged' : ''}" ${attr}="${prefix}${k}">${k === 'ai' ? icon('lomax') + ' ' : ''}${label}</button>`)
      .join('')
  }
  // Show/hide the Details panel. Sits above the panel at the right, and stays
  // put in both states because it lives outside the panel that slides.
  function panelToggleHtml(id) {
    return `<button class="panel-toggle" id="${id}" title="Show/hide details" ` +
           `aria-label="Show/hide details" aria-expanded="false">${icon('panel-right')}</button>`
  }

  // The app's ONE chevron. Lucide 'chevron-right', rotated by
  // .caret-ic--up/--down/--open — the same family as everything in ICONS.
  //
  // ⚠ Was a hand-drawn path in an 8-unit viewBox at stroke-width 1.7, which
  // renders a ~2.1px stroke at this size against Lucide's ~0.9px. On
  // 2026-08-28 the app briefly standardised on THAT one, purely because it had
  // more call sites — and it is the heavy, clumsy glyph. Ryan: "the current one
  // looks ridiculous, it is too large." Standardising on the thin one is also
  // what [[icon-system]] already said to do: everything goes through Lucide.
  //
  // 12px rather than the old 10px because a 0.9px stroke reads smaller than a
  // 2.1px one at the same box size; this lands on the weight the Move button
  // had, which is the one that was liked.
  //
  // `size` (Ryan, 2026-08-31): the Review & Ingest row-expand caret — the
  // main, farthest-right control on every row — asked to be bigger and more
  // prominent than a tab-strip or menu chevron earns. Same glyph, same
  // stroke-width in SVG units, just rendered larger; scaling the whole icon
  // up scales its visual stroke weight with it; the box is what to size, not
  // stroke-width from svg. Still the ONE chevron shape in the app — every
  // call site but that one still gets the default 12px.
  function chevronIcon(cls, size = 12) {
    return `<svg class="caret-ic${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>`
  }

  // Canonical form for comparing filesystem paths client-side. macOS hands out
  // decomposed (NFD) filenames; the quality API's staging rows are always
  // NFC-normalised server-side (app/utils/quality_store.py), but batch-scan's
  // item.path is a raw, un-normalised os.scandir() result — so a straight ===
  // between the two can silently miss a match on any accented folder name
  // (the "Guitar Trio" bug, 2026-07-28). Normalise both sides before comparing.
  const nfc = s => (s || '').normalize('NFC')

  // ── Who may edit, and are they asking to? ────────────────────────────────
  //
  // Two separate questions, kept separate (Ryan, 2026-08-21):
  //
  //   hasEditRole()      Does this account have the authority at all?
  //   getViewMode()      Is this person currently asking to use it?
  //
  // canEditLibrary() is their conjunction, and it stayed the name every caller
  // already used — which is why adding a whole Playback mode touched almost
  // nothing. The recording view, the personnel widget, the quick-edit cells and
  // the track rows were all already built around this one flag, so switching
  // modes turns the entire editing surface off through a single choke point
  // instead of a dozen scattered checks that could drift apart.
  //
  // This is a UI mode, NOT a security boundary. Every endpoint still enforces
  // its own role check server-side; Playback mode hides controls, it does not
  // protect anything. A listener gets Playback because they have no edit role,
  // not because the toggle put them there.

  // True for roles that may edit library metadata (admin/archivist).
  // Listener is read-only. Doesn't yet distinguish an archivist's specific
  // artist permissions (all_artists / user_artist_permission) — the frontend
  // has no per-artist gating anywhere else either, so this matches the
  // existing (coarser) enforcement level rather than building that out here.
  function hasEditRole() {
    const role = state.user?.role
    return role === 'admin' || role === 'archivist'
  }

  // 'admin' | 'playback'. Anyone without an edit role is ALWAYS 'playback' and
  // never sees the toggle, so a stale localStorage value from a previous
  // session on a shared machine cannot hand a listener an editing UI.
  function getViewMode() {
    // A remote library is someone else's -- Playback is not a preference
    // there, it is the only possible state. We deliberately do NOT write
    // this to localStorage, so switching back to your own library restores
    // whatever mode you had it in before.
    if (libraryState.activeId != null) return 'playback'
    if (!hasEditRole()) return 'playback'
    return localStorage.getItem('trellisViewMode') === 'playback' ? 'playback' : 'admin'
  }

  function setViewMode(mode) {
    const next = mode === 'playback' ? 'playback' : 'admin'
    localStorage.setItem('trellisViewMode', next)
    document.documentElement.classList.toggle('playback-mode', next === 'playback')
    paintViewModeToggle()
    // Re-render both halves of the chrome: the sidebar drops its admin entries
    // and the current view rebuilds with the new gating. route() re-dispatches
    // the hash we are already on, which is the whole re-render — and it is also
    // what bounces us off an admin-only page if that is where we were standing.
    renderSidebar()
    route()
  }

  function paintViewModeToggle() {
    const wrap = document.getElementById('view-mode-toggle')
    if (!wrap) return
    // Offering a choice the user cannot have is a lie -- a remote library is
    // always Playback, so there is nothing to toggle between.
    const show = hasEditRole() && libraryState.activeId == null
    wrap.classList.toggle('hidden', !show)
    const mode = getViewMode()
    wrap.querySelectorAll('.vm-opt').forEach(b => {
      const on = b.dataset.mode === mode
      b.classList.toggle('active', on)
      b.setAttribute('aria-pressed', on ? 'true' : 'false')
    })
  }

  function initViewMode() {
    document.documentElement.classList.toggle('playback-mode', getViewMode() === 'playback')
    paintViewModeToggle()
    const wrap = document.getElementById('view-mode-toggle')
    if (wrap && !wrap._wired) {
      wrap._wired = true
      wrap.addEventListener('click', e => {
        const btn = e.target.closest('.vm-opt')
        if (btn && btn.dataset.mode !== getViewMode()) setViewMode(btn.dataset.mode)
      })
    }
  }

  function canEditLibrary() {
    // Editing a peer's library is impossible regardless of role or mode --
    // the peer door has no editing endpoints at all, so an affordance here
    // could only ever produce an error.
    return hasEditRole() && getViewMode() === 'admin' && libraryState.activeId == null
  }

  // Where Home goes -- #/bulk-ingest while a Bulk Ingest run is running or
  // paused, #/ otherwise. Shared by the sidebar shelf-head link
  // (renderSidebar) and the wordmark click (wireHeaderNav) so the two never
  // drift apart.
  function homeHash() {
    const active = state.bulkIngest
                   && (state.bulkIngest.status === 'running' || state.bulkIngest.status === 'paused')
    return active ? '#/bulk-ingest' : '#/'
  }

  // True only for the admin role -- narrower than hasEditRole() (which also
  // covers archivist).
  function isAdmin() {
    return state.user?.role === 'admin'
  }

  // ── The viewer's star ─────────────────────────────────────────────────────
  //
  // A star is the ONE mark a listener may make while browsing someone else's
  // library, and it is NOT an edit: it writes a row on THIS node about THEIR
  // recording and never touches their library at all. That is why it survives
  // the read-only gate above when every other affordance does not.
  //
  // Two stores, one question. In my own library the answer is the column on
  // the recording; in a joined library it is `remote_favorite` here, keyed by
  // (node, remote recording id). Both are asked through these helpers so no
  // call site has to know which world it is in — the same reason
  // canEditLibrary() exists at all.
  //
  // ⚠ `rec.is_favorite` from a share payload is ALWAYS false: the sharer's own
  // star deliberately does not travel (see _peer_row in api/share.py). Reading
  // it directly in a remote library paints every star empty. Ask
  // viewerHasFavorited() instead.

  function viewerHasFavorited(rec) {
    if (!rec) return false
    return libraryState.activeId != null
      ? libraryState.favIds.has(rec.id)
      : !!rec.is_favorite
  }

  async function setViewerFavorite(recId, on) {
    const nodeId = libraryState.activeId
    if (nodeId == null) {
      await API.recordings.update(recId, { is_favorite: on })
      return
    }
    if (on) await API.remoteFavorites.add(nodeId, recId)
    else    await API.remoteFavorites.remove(nodeId, recId)
    if (on) libraryState.favIds.add(recId)
    else    libraryState.favIds.delete(recId)
  }

  // Ids only, and local — so stars paint correctly even when the remote is
  // unreachable. Whether I starred something is a fact about MY node.
  async function loadRemoteFavorites() {
    const nodeId = libraryState.activeId
    if (nodeId == null) { libraryState.favIds = new Set(); return }
    try {
      libraryState.favIds = new Set(await API.remoteFavorites.ids(nodeId))
    } catch (_) {
      libraryState.favIds = new Set()
    }
  }

  // Venue autocomplete — searches venues, shows location, offers a create row.
  // onPick receives {id|null, name}.
  function wireVenuePickerDropdown(inputEl, dropEl, onPick) {
    if (!inputEl || !dropEl) return
    let debounce = null
    const close = () => { dropEl.style.display = 'none'; dropEl.innerHTML = '' }
    async function run() {
      const q = inputEl.value.trim()
      if (q.length < 2) { close(); return }
      let results = []
      try { results = await API.venues.list(q) } catch (_) {}
      const rows = results.slice(0, 10).map(v => {
        const loc = [v.city, v.state, v.country].filter(Boolean).join(', ')
        return `<div class="venue-result" data-id="${v.id}" data-name="${esc(v.name)}">${esc(v.name)}${loc ? ` <span class="venue-result-loc">${esc(loc)}</span>` : ''}</div>`
      }).join('')
      const exact = results.some(v => v.name.toLowerCase() === q.toLowerCase())
      const createRow = (!exact && q)
        ? `<div class="venue-result venue-result-new" data-id="" data-name="${esc(q)}">+ Create venue: "${esc(q)}"</div>` : ''
      dropEl.innerHTML = rows + createRow
      dropEl.style.display = (rows || createRow) ? 'block' : 'none'
      dropEl.querySelectorAll('.venue-result').forEach(el => {
        el.addEventListener('mousedown', e => {
          e.preventDefault()
          onPick({ id: el.dataset.id ? parseInt(el.dataset.id) : null, name: el.dataset.name })
          close()
        })
      })
    }
    inputEl.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(run, 220) })
    inputEl.addEventListener('focus', () => { if (inputEl.value.trim().length >= 2) run() })
  }

  // Generic autocomplete over {id,name} results with an optional "create" row.
  // onPick receives {id|null, name}. Used for the Artist and Member pickers.
  // Omitting createLabel suppresses the create row entirely — for a picker
  // over a fixed vocabulary (e.g. Genre) where nothing may be created as a
  // side effect of typing.
  function wirePickerDropdown(inputEl, dropEl, searchFn, onPick, createLabel) {
    if (!inputEl || !dropEl) return
    let debounce = null
    const close = () => { dropEl.style.display = 'none'; dropEl.innerHTML = '' }
    async function run() {
      const q = inputEl.value.trim()
      if (q.length < 2) { close(); return }
      let results = []
      try { results = await searchFn(q) } catch (_) {}
      const rows = results.map(r =>
        `<div class="artist-result" data-id="${r.id}" data-name="${esc(r.name)}">${esc(r.name)}</div>`).join('')
      const exact = results.some(r => r.name.toLowerCase() === q.toLowerCase())
      const createRow = (!exact && q && createLabel)
        ? `<div class="artist-result artist-result-new" data-id="" data-name="${esc(q)}">+ ${esc(createLabel)}: "${esc(q)}"</div>` : ''
      dropEl.innerHTML = rows + createRow
      dropEl.style.display = (rows || createRow) ? 'block' : 'none'
      dropEl.querySelectorAll('.artist-result').forEach(el => {
        el.addEventListener('mousedown', e => {
          e.preventDefault()
          onPick({ id: el.dataset.id ? parseInt(el.dataset.id) : null, name: el.dataset.name })
          close()
        })
      })
    }
    inputEl.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(run, 220) })
    inputEl.addEventListener('blur',  () => setTimeout(close, 200))
    inputEl.addEventListener('focus', () => { if (inputEl.value.trim().length >= 2) run() })
  }

  // The first non-"create" result currently showing in a wirePickerDropdown
  // dropdown, if any — lets a fixed-vocabulary picker (Genre: existing values
  // only, never a free-text create) treat Enter as "commit the top visible
  // match" instead of the venue/event pickers' "create whatever was typed".
  function firstPickerResult(dropEl) {
    const el = dropEl?.querySelector('.artist-result:not(.artist-result-new)')
    return el ? { id: parseInt(el.dataset.id), name: el.dataset.name } : null
  }

  // ── Reusable Artist + Members/Guests widget ───────────────────────────────
  // Bound to a `store` object holding `.members`, `.guests` (+ .artist_name/
  // .artist_id). `ids.field` is a mount point div — renderChips() rebuilds
  // its full innerHTML each call (both rows + pills + add controls) and
  // rewires events, the same rebuild-and-rewire pattern already used for
  // buildAiResultsHtml, rather than DOM-patching individual chips.
  //
  // Members/Guests two-row redesign (2026-07-22), replacing one flat Musicians
  // pill row + descriptive subtext: a small (+) button per row reveals an
  // inline add-picker input on click. Removing a pill is a plain splice —
  // this is still draft form state until Confirm, no server round-trip.
  function createMembersWidget(store, ids) {
    // Which role's input had focus when renderChips() was called, so the
    // rebuild can hand it back. Module-level to the widget, not the DOM: the
    // node it refers to is destroyed by the very render that needs to read it.
    let _focusRole = null
    const pill = (p, i, role) => `
      <span class="member-chip ${role === 'guest' ? 'member-chip--guest' : ''}">
        ${esc(p.name)} <span class="member-chip-x" data-role="${role}" data-idx="${i}" title="Remove">${icon('x')}</span>
      </span>`
    // The add control is ALWAYS an input — there is no show/hide any more
    // (Ryan, 2026-09-01: "when clicking to add, the height of the element
    // jumps").
    //
    // The jump was structural, not a stray margin. `.mg-row` is a wrapping
    // flex row of chips; revealing a 150px input at the end of it pushes the
    // row to a second line whenever the chips already come close to the edge,
    // and the row's height doubles under the cursor. Sizing the input
    // differently would only move the threshold, not remove it.
    //
    // So the input is a permanent member of the row, shaped and sized exactly
    // like a chip: same height, same radius, same border box, dashed to read
    // as "add one" rather than as a filled value. The row's height is then the
    // height of one chip line whatever happens, because clicking changes
    // nothing about what is in the row. It also removes a click — the old
    // "+" was a button whose entire job was revealing a text box.
    //
    // Matching the CHIPS rather than the form's other inputs is deliberate:
    // this control lives inside a row of chips, and local consistency is what
    // a reader actually sees. A 32px form input wedged between 24px chips is
    // the mismatch that reads as wrong.
    const row = (role, label, items) => `
      <div class="mg-row">
        <span class="mg-row-label">${label}</span>
        ${items.map((p, i) => pill(p, i, role)).join('')}
        <span class="artist-picker-wrap mg-add-picker" data-role="${role}">
          <input type="text" class="mg-role-input" data-role="${role}" autocomplete="off"
                 aria-label="Add ${label === 'Members' ? 'a member' : 'a guest'}"
                 placeholder="+ Add ${label === 'Members' ? 'Member' : 'Guest'}" />
          <div class="artist-dropdown mg-role-dd" data-role="${role}" style="display:none"></div>
        </span>
      </div>`

    function renderChips() {
      const field = document.getElementById(ids.field)
      if (!field) return
      store.members = store.members || []
      store.guests  = store.guests  || []
      field.innerHTML = row('member', 'Members', store.members) + row('guest', 'Guests', store.guests)

      field.querySelectorAll('.member-chip-x').forEach(x =>
        x.addEventListener('click', () => {
          const list = x.dataset.role === 'guest' ? store.guests : store.members
          list.splice(parseInt(x.dataset.idx), 1)
          renderChips()
        }))

      field.querySelectorAll('.mg-role-input').forEach(input => {
        const role = input.dataset.role
        const dd   = field.querySelector(`.mg-role-dd[data-role="${role}"]`)
        wirePickerDropdown(input, dd, API.musicians.search,
          ({ id, name }) => { _focusRole = role; addMember(name, id, role) }, 'Add new musician')
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') {
            e.preventDefault()
            if (!input.value.trim()) return
            _focusRole = role
            addMember(input.value, null, role)
          }
        })
      })

      // renderChips() rebuilds this whole block on every add and remove, which
      // would drop focus in the middle of adding three people. The focused
      // role is remembered across the rebuild and handed back here — LAST,
      // after wirePickerDropdown has attached its own focus handler, so the
      // restored focus behaves like any other.
      if (_focusRole) {
        const back = field.querySelector(`.mg-role-input[data-role="${_focusRole}"]`)
        _focusRole = null
        back?.focus()
      }
    }

    function addMember(name, id, role = 'member') {
      name = (name || '').trim()
      const list = role === 'guest' ? (store.guests = store.guests || []) : (store.members = store.members || [])
      const dupe = !!name && list.some(m => m.name.toLowerCase() === name.toLowerCase())
      if (name && !dupe) list.push(id ? { id, name } : { name })
      // Re-render even when nothing was added. The rebuild is what CLEARS the
      // input, and the two no-op cases are exactly where a stale value is
      // worst: picking someone already in the row otherwise leaves their name
      // sitting in the box looking like it failed. It also releases
      // `_focusRole`, which would otherwise stay armed and steal focus on
      // whatever render happened next.
      renderChips()
    }

    // Artist picked (existing act → load its current roster into Members;
    // new act → no members by default, Musicians are optional and only added
    // for special collaborations). Guests always reset — a freshly (re)picked
    // act has no per-show guests carried over from whatever was typed before.
    async function onArtistPick({ id, name }) {
      const el = document.getElementById(ids.artistInput)
      if (el) el.value = name
      store.artist_name = name
      store.artist_id   = id || null
      store.guests = []
      if (id) {
        try {
          const p = await API.artists.get(id)
          store.members = (p.members || []).map(m => ({ id: m.id, name: m.name }))
          // The act's existing genre comes with it (Ryan, 2026-09-01). Only
          // ids.genreInput surfaces it — this widget is shared with surfaces
          // that have no genre field, and setting store.genre_* on those would
          // put a value into a payload nothing on screen ever showed.
          if (ids.genreInput) setGenreFromArtist(p.genre || null)
        }
        catch (_) { store.members = [] }
      } else {
        store.members = []
        // A NEW act has no genre to inherit. Deliberately does NOT clear a
        // genre the user already picked by hand: they may be typing the act
        // name last, and silently discarding a deliberate choice because a
        // different field changed is the kind of quiet loss this app avoids.
      }
      renderChips()
    }

    // Reflects an act's genre into the ingest form's Genre field. Writes both
    // the store and the DOM because the field is plain markup rather than part
    // of renderChips()' rebuild.
    function setGenreFromArtist(genre) {
      const input = document.getElementById(ids.genreInput)
      const idEl  = ids.genreIdInput ? document.getElementById(ids.genreIdInput) : null
      store.genre_id   = genre ? genre.id : null
      store.genre_name = genre ? genre.name : ''
      store.genre_source = null          // the act's own genre, not a pick
      if (input) {
        input.value = store.genre_name
        input.classList.remove('is-new-genre')
      }
      if (idEl) idEl.value = store.genre_id || ''
    }
    function mount() {
      wirePickerDropdown(document.getElementById(ids.artistInput), document.getElementById(ids.artistDropdown),
        API.artists.search, onArtistPick, 'Create new artist')
      renderChips()
    }
    return { renderChips, addMember, onArtistPick, setGenreFromArtist, mount }
  }

  // Splits a billed-act name into candidate individual-person names, for
  // matching against existing Musicians when the Artist itself doesn't
  // exist yet (2026-07-22) — e.g. "Bela Fleck & Edgar Meyer" ->
  // ["Bela Fleck", "Edgar Meyer"]. Conservative separators only; a missed
  // split is harmless (that name just stays unmatched), which is why exact
  // matching below matters more than aggressive splitting here.
  const _NAME_SPLIT_RE = /\s*(?:&|,|\/|\+|\bwith\b|\bfeat\.?\b|\bfeaturing\b|\band\b)\s*/i
  function splitArtistNameCandidates(raw) {
    return (raw || '').split(_NAME_SPLIT_RE).map(s => s.trim()).filter(Boolean)
  }

  // A field the person edited by hand on the Add Recording form (kept on the form, so a new form starts clean).
  function _ingestMarkDirty(field) {
    ingest.form._dirty = ingest.form._dirty || {}
    ingest.form._dirty[field] = true
  }

  // An act billing compared the way the library compares names: case, accents, "&" vs "and",
  // punctuation and a leading "The" do not make a different act.
  function _normBilling(text) {
    return String(text || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
      .replace(/&/g, ' and ').replace(/[^\w\s]/g, ' ').replace(/\s+/g, ' ').trim().replace(/^the /, '')
  }

  // Add flow: preload Members if the scanned Artist (act) already exists
  // in the DB — pulls its current roster. If the act itself is new (e.g. a
  // one-off duo billing), fall back to splitting the act name into candidate
  // person names and matching each against existing Musicians — EXACT
  // (case-insensitive) name match only, never a fuzzy/substring hit, since a
  // wrong auto-attached person is worse than an unmatched name Ryan fills in
  // by hand. Ryan chose auto-fill over a click-to-confirm suggestion step
  // for this (2026-07-22), weighing it against the AI-Assist auto-apply bug
  // fixed earlier the same session.
  async function initAddArtistMembers(widget) {
    const f = ingest.form
    const name = (f.artist_name || '').trim()
    if (f._membersInit) { widget.renderChips(); return }
    f._membersInit = true
    if (!name) { f.members = f.members || []; widget.renderChips(); return }
    try {
      const matches = await API.artists.search(name)
      const exact = matches.find(m => m.name.toLowerCase() === name.toLowerCase())
      if (exact) {
        f.artist_id = exact.id
        const p = await API.artists.get(exact.id)
        f.members = (p.members || []).map(m => ({ id: m.id, name: m.name }))
        // Same inheritance as picking the act by hand — the scanned name
        // matching an existing act is the commonest way into this form, and a
        // genre that only appears when you re-pick a name already in the box
        // would look like the field was broken.
        if (p.genre) { f.genre_id = p.genre.id; f.genre_name = p.genre.name }
      } else {
        const found = []
        for (const cand of splitArtistNameCandidates(name)) {
          try {
            const results = await API.musicians.search(cand)
            const hit = results.find(r => r.name.toLowerCase() === cand.toLowerCase())
            if (hit) found.push({ id: hit.id, name: hit.name })
          } catch (_) { /* best-effort — a failed lookup just leaves that name unmatched */ }
        }
        // The people an "X and Y" billing names (the resolver splits only that, never a
        // "with" list or a group name): a brand-new act's Members row starts with them
        // (saved with the recording, which seeds the new act's roster; an existing act never
        // gets here). An existing Musician of that exact name is linked, others are new; a
        // single-word name is kept only when it matches an existing Musician exactly.
        //
        // Only while the billing the resolver read is still the act name in the form (an artist
        // the person changed, or accepted from Lomax, is not that billing), and only when no act
        // of a similar name is already in the library: "Bela Fleck and Edgar Meyer" must not seed
        // a new roster when "Bela Fleck & Edgar Meyer" exists.
        const billing = ingest.scan?.resolved?.artist?.value
        const sameBilling = _normBilling(billing) === _normBilling(name)
        let similar = true                       // unknown counts as similar: no pre-fill is the safe side
        if (sameBilling) {
          try { similar = ((await API.ingest.similarActs(name)).acts || []).length > 0 } catch (_) { /* stays true */ }
        }
        for (const nm of (sameBilling && !similar ? (ingest.scan?.resolved?.members || []) : [])) {
          if (found.some(m => m.name.toLowerCase() === String(nm).toLowerCase())) continue
          let hit = null
          try { hit = (await API.musicians.search(nm)).find(r => r.name.toLowerCase() === String(nm).toLowerCase()) } catch (_) { /* best-effort */ }
          if (hit) found.push({ id: hit.id, name: hit.name })
          else if (/\s/.test(String(nm).trim())) found.push({ name: nm })
        }
        f.members = found
      }
    } catch (_) { f.members = f.members || [] }
    widget.renderChips()
    // The genre field is not part of renderChips()' markup, so it needs its own
    // paint once the lookup above has resolved.
    if (f.genre_name) widget.setGenreFromArtist({ id: f.genre_id, name: f.genre_name })
  }

  function setMainHTML(html) {
    _cancelWaveform()        // destroy any wavesurfer instance from the page we're leaving
    Player.setFallbackPlay(null)   // only the recording page currently shown gets to set this
    mainContent.innerHTML = html
  }

  function setLoading() {
    mainContent.innerHTML = `
      <div class="empty-state">
        <div class="loading-spinner"></div>
      </div>`
  }

  // ── Resizable split panel ──────────────────────────────────────────────────

  let _resizeCleanup = null

  /**
   * Make `sizedEl` draggable against `handleEl` inside `shellEl`.
   *
   * `side` says which side of the handle the sized element is on. It used to be
   * hardwired to 'left'; Add Recording now sizes the RIGHT-hand details panel
   * instead (2026-08-28), because that panel also has to animate open and shut,
   * and only the element that owns an explicit width can be transitioned. With
   * the panel sized and the form flexible, the form simply takes back whatever
   * the panel gives up, frame by frame, and the drawer slides.
   *
   * While dragging, the sized element carries `.resizing` so its CSS can drop
   * the transition — otherwise every mousemove would animate towards the
   * cursor over 220ms and the divider would feel like it was on elastic.
   */
  function wireResizablePanel(shellEl, sizedEl, handleEl, minSized = 200, minOther = 200, opts) {
    // Remove any previous listeners to avoid stacking on re-renders
    if (_resizeCleanup) { _resizeCleanup(); _resizeCleanup = null }
    if (!shellEl || !sizedEl || !handleEl) return
    const side = (opts && opts.side) || 'left'

    let dragging = false, startX = 0, startWidth = 0

    const onDown = e => {
      dragging   = true
      startX     = e.clientX
      startWidth = sizedEl.offsetWidth
      sizedEl.classList.add('resizing')
      document.body.style.cursor    = 'col-resize'
      document.body.style.userSelect = 'none'
      e.preventDefault()
    }

    const onMove = e => {
      if (!dragging) return
      // Dragging right grows a left-hand element and shrinks a right-hand one.
      const delta = side === 'left' ? (e.clientX - startX) : (startX - e.clientX)
      const max   = shellEl.offsetWidth - minOther - handleEl.offsetWidth
      const newW  = Math.max(minSized, Math.min(startWidth + delta, max))
      sizedEl.style.width     = newW + 'px'
      sizedEl.style.flexBasis = newW + 'px'
    }

    const onUp = () => {
      if (!dragging) return
      dragging = false
      sizedEl.classList.remove('resizing')
      document.body.style.cursor    = ''
      document.body.style.userSelect = ''
    }

    handleEl.addEventListener('mousedown', onDown)
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup',   onUp)

    _resizeCleanup = () => {
      handleEl.removeEventListener('mousedown', onDown)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup',   onUp)
    }
  }

  // ── Nav helpers ────────────────────────────────────────────────────────────

  // Every render*View() function calls this once it knows its own display
  // label (immediately for a static-label page like "Library"; after its
  // data fetch succeeds for a dynamic one like an artist/venue/recording
  // name) — see state.navCurrent/navBack above for how "← Back" links use
  // it. A page whose data fetch FAILS (e.g. "Recording not found") simply
  // never calls this, which is deliberate: a subsequent page's Back link
  // then skips the dead page and points at the last one that actually
  // loaded, rather than back to a dead end.
  function setNavCurrent(label) {
    state.navCurrent = { hash: window.location.hash, label }
  }

  function setActiveNav(active) {
    state._activeNav = active
    const nav = document.getElementById('sidebar-nav')
    // The shelf heading shares data-nav="library" with Live Recordings but is
    // a heading, not a page: it never lights up (Ryan, 2026-10-01).
    if (nav) nav.querySelectorAll('[data-nav]:not(.nav-shelf-head)').forEach(el =>
      el.classList.toggle('active', el.dataset.nav === active))
  }

  function setActiveArtist(id) {
    document.querySelectorAll('#sidebar-nav .nav-record[data-dim="artists"]').forEach(el =>
      el.classList.toggle('active', parseInt(el.dataset.id) === id))
  }

  // ── Sidebar nav (all top-level in small caps; dimensions expandable) ──────────

  // Collections is expanded on arrival (Ryan, 2026-08-23). It is the shelf's
  // point — a collapsed list of things you curated is a door with the light
  // off. The four dimension indexes below stay shut; those are for re-finding
  // something you already know exists.
  // Collections is no longer an expandable dimension at all (Ryan,
  // 2026-08-23) — its list is always shown and the disclosure lives on each
  // individual collection instead. The four indexes below still expand.
  state.expandedDims = state.expandedDims || new Set()
  const _dimCache = {}

  function _dimSection(dim, iconHtml, label, sub) {
    const open = state.expandedDims.has(dim)
    const singular = label.replace(/s$/, '')
    return `
      <div class="nav-section">
        <div class="nav-item ${sub ? 'nav-sub' : 'nav-top'} nav-expand nav-dim" data-dim="${dim}">
          ${iconHtml ? `<span class="nav-icon">${iconHtml}</span>` : ''}
          <span class="nav-dim-label truncate">${label}</span>
          <span class="nav-dim-actions">
            ${canEditLibrary() ? `<span class="nav-action" data-act="new" data-admin
                     title="Create new ${esc(singular)}">${icon('plus')}</span>` : ''}
            <span class="nav-action" data-act="refresh" title="Refresh list">${icon('rotate-cw')}</span>
          </span>
          <span class="nav-caret nav-caret--sm ${open ? 'open' : ''}">${chevronIcon()}</span>
        </div>
        <div class="nav-records ${sub ? 'nav-records--sub' : ''}" id="nav-records-${dim}" style="display:${open ? '' : 'none'}"></div>
      </div>`
  }

  // A system collection (Full Library) is a SHARING PRIMITIVE, not a curated
  // set: its membership is a live query, it cannot be edited by hand, and its
  // contents are the library you are already looking at. So it appears in
  // exactly one place — the peer grant UI, where it is the thing you tick — and
  // nowhere that lists collections as curation. One predicate, used by every
  // such list, so the rule cannot drift between surfaces.
  const isCuratedCollection = c => !c.is_system

  async function _loadDim(dim) {
    if (_dimCache[dim]) return _dimCache[dim]
    let rows = []
    try {
      if (dim === 'venues')            rows = await API.venues.list()
      else if (dim === 'artists')   rows = await API.artists.list()
      else if (dim === 'musicians')      rows = await API.musicians.list()
      else if (dim === 'collections')  rows = await API.collections.list()
      else if (dim === 'genres')       rows = await API.genres.list()
      else if (dim === 'events')       rows = await API.events.list()
    } catch (_) {}
    _dimCache[dim] = rows
    return rows
  }

  // Per-collection expand state (2026-08-23) — separate from state.expandedDims,
  // which only tracks whether the COLLECTIONS list itself is open. This tracks
  // which individual collections, inside that list, are themselves expanded to
  // show their recordings — a second, independent level of disclosure.
  const _colOpenIds = new Set()
  const _colRecCache = {}

  async function _renderDimRecords(dim) {
    const box = document.getElementById(`nav-records-${dim}`)
    if (!box) return
    let rows = await _loadDim(dim)
    // Full Library must never render here: it is not curation, and expanding it
    // in place would pull the entire library into the sidebar (580 card rows
    // through GET /api/collections/<id>).
    if (dim === 'collections') rows = rows.filter(isCuratedCollection)
    const target = { venues: 'venue', artists: 'artist', musicians: 'musician',
                     collections: 'collection', genres: 'genre', events: 'event' }[dim]
    // The index page for this dimension — the "view all" door. A dimension
    // section used to be a dead end: expanding it listed every record and there
    // was no way from the sidebar to the index at all, so #/venues and #/genres
    // were reachable only by typing them or by deleting a record (which
    // redirects there). That is why nobody had noticed those two pages were
    // still the pre-entity-shell admin screens.
    const indexHash = { venues: '#/venues', artists: '#/artists',
                        musicians: '#/musicians', genres: '#/genres',
                        events: '#/events' }[dim]
    if (!rows.length) {
      // Still offer the index: it is where the create form lives, and a bare
      // "None yet" with nothing to click is a dead end on the one dimension
      // that most needs a way to add its first record.
      const emptyIndex = { venues: '#/venues', artists: '#/artists',
                           musicians: '#/musicians', genres: '#/genres',
                           events: '#/events' }[dim]
      box.innerHTML = `<div class="nav-record nav-record--empty">None yet</div>` +
        (emptyIndex ? `<div class="nav-record nav-record--all" data-all="${emptyIndex}">Open ${dim} \u2192</div>` : '')
      box.querySelector('.nav-record--all')?.addEventListener('click', () =>
        { window.location.hash = emptyIndex })
      return
    }
    // Collections got its own row shape and click behaviour 2026-08-23: a
    // collection name is now click-to-expand-in-place (showing the recordings
    // it holds) rather than a navigation link, unindented to sit at the same
    // level as the COLLECTIONS header itself, and without the recording-count
    // badge every other dimension row carries ("out of context" — Ryan). Every
    // other dimension (Venues, Artists, Musicians, Genres) is untouched.
    box.innerHTML = dim === 'collections'
      ? rows.map(c => {
          const open = _colOpenIds.has(c.id)
          return `
            <div class="nav-col-item">
              <!-- TWO targets, two meanings (Ryan, 2026-09-03). The +/- box
                   keeps the 2026-08-23 behaviour — expand the recordings in
                   place, without leaving whatever you were looking at — and
                   the NAME is now a link to the collection's own page, which
                   is where you add, remove and delete.
                   Splitting them rather than choosing: the in-place list is
                   for a glance, the page is for work, and one row cannot mean
                   both from a single click target. Same split the dimension
                   sections already use for their caret. -->
              <div class="nav-record nav-record--flush" data-col-row="${c.id}">
                <span class="nav-caret nav-caret--pm nav-pm-box ${open ? 'open' : ''}"
                      data-col-toggle="${c.id}" title="${open ? 'Hide' : 'Show'} this collection's recordings"
                      >${icon('plus', 'pm-plus')}${icon('minus', 'pm-minus')}</span>
                <span class="truncate nav-col-name" data-col-open="${c.id}"
                      title="Open ${esc(c.name)}">${esc(c.name)}</span>
              </div>
              <div class="nav-col-recs" id="nav-col-recs-${c.id}" style="display:${open ? '' : 'none'}"></div>
            </div>`
        }).join('')
      : rows.map(r => `<div class="nav-record" data-dim="${dim}" data-id="${r.id}">
           <span class="truncate">${esc(r.name)}</span>${r.recording_count ? `<span class="nav-record-count">${r.recording_count}</span>` : ''}
         </div>`).join('')
    if (dim === 'collections') {
      box.querySelectorAll('[data-col-toggle]').forEach(el =>
        el.addEventListener('click', e => {
          e.stopPropagation()   // the row is a link now; the box is not
          _toggleCollectionRow(parseInt(el.dataset.colToggle, 10))
        }))
      box.querySelectorAll('[data-col-open]').forEach(el =>
        el.addEventListener('click', () => {
          window.location.hash = `#/collection/${el.dataset.colOpen}`
        }))
      _colOpenIds.forEach(id => { if (document.getElementById(`nav-col-recs-${id}`)) _renderCollectionRecs(id) })
    } else {
      // "View all" sits at the FOOT of the expanded list, not on the section
      // header. The header is the expand/collapse control and has been since
      // the sidebar was built; making it navigate as well would mean one click
      // target with two meanings, and the caret would stop being reachable.
      if (indexHash) {
        box.insertAdjacentHTML('beforeend',
          `<div class="nav-record nav-record--all" data-all="${indexHash}">View all ${rows.length} \u2192</div>`)
      }
      box.querySelectorAll('.nav-record[data-id]').forEach(el =>
        el.addEventListener('click', () => { window.location.hash = `#/${target}/${el.dataset.id}` }))
      box.querySelector('.nav-record--all')?.addEventListener('click', () =>
        { window.location.hash = indexHash })
    }
  }

  // Expands one collection in place to show the recordings it holds — the
  // same information the collection's own page shows, just close enough to
  // reach without leaving the shelf (Ryan, 2026-08-23). Reuses
  // GET /api/collections/<id>, which already returns a `recordings` array
  // (card=True rows) for the collection detail page — no new endpoint needed
  // (checked API.collections and app/api/collections.py before building this;
  // see get_collection()).
  async function _toggleCollectionRow(id) {
    const row   = document.querySelector(`.nav-record--flush[data-col-row="${id}"]`)
    const box   = document.getElementById(`nav-col-recs-${id}`)
    const caret = row?.querySelector('.nav-caret')
    if (_colOpenIds.has(id)) {
      _colOpenIds.delete(id); if (box) box.style.display = 'none'; caret?.classList.remove('open')
      return
    }
    _colOpenIds.add(id); if (box) box.style.display = ''; caret?.classList.add('open')
    _renderCollectionRecs(id)
  }

  async function _renderCollectionRecs(id) {
    const box = document.getElementById(`nav-col-recs-${id}`)
    if (!box) return
    if (!_colRecCache[id]) {
      box.innerHTML = `<div class="nav-record nav-record--empty nav-col-rec">Loading…</div>`
      try {
        const full = await API.collections.get(id)
        _colRecCache[id] = full.recordings || []
      } catch (_) {
        _colRecCache[id] = []
      }
    }
    const recs = _colRecCache[id]
    box.innerHTML = recs.length
      ? recs.map(r => navRecRowHtml(r, 'nav-col-rec')).join('')
      : `<div class="nav-record nav-record--empty nav-col-rec">No recordings yet</div>`
    box.querySelectorAll('[data-id]').forEach(el =>
      el.addEventListener('click', e => { e.stopPropagation(); window.location.hash = `#/recording/${el.dataset.id}` }))
  }

  function _toggleDim(dim, forceOpen) {
    const row  = document.querySelector(`.nav-dim[data-dim="${dim}"]`)
    const box  = document.getElementById(`nav-records-${dim}`)
    const caret = row?.querySelector('.nav-caret')
    const open = state.expandedDims.has(dim)
    if (open && !forceOpen) {
      state.expandedDims.delete(dim); if (box) box.style.display = 'none'; caret?.classList.remove('open')
    } else {
      state.expandedDims.add(dim); if (box) box.style.display = ''; caret?.classList.add('open')
      _renderDimRecords(dim)
    }
  }

  // Collections' own refresh. NOT _refreshDim: that one calls _toggleDim to
  // ensure the section is open, and Collections stopped being a collapsible
  // section on 2026-08-23 — it is always shown. Routing it through there would
  // quietly add 'collections' to state.expandedDims, a flag for a control that
  // no longer exists.
  //
  // Drops the per-collection recording caches as well as the list itself: a
  // refresh that re-read the names but served a stale expanded list from
  // memory would be the more confusing half-answer.
  function _refreshCollections() {
    _dimCache.collections = null
    Object.keys(_colRecCache).forEach(k => delete _colRecCache[k])
    _renderDimRecords('collections')
  }

  function _refreshDim(dim) {
    _dimCache[dim] = null
    if (dim === 'collections') { _colRecCache && Object.keys(_colRecCache).forEach(k => delete _colRecCache[k]) }
    _toggleDim(dim, true)   // ensure open, then re-render from DB
    _renderDimRecords(dim)
  }

  // Favorites (2026-08-23): no longer a collapsible dim-section — Ryan wants
  // starred shows shown flush in the sidebar, at MY LIBRARY's own indentation,
  // with no "Favorites" heading/caret at all ("we will simply display the
  // favorited recordings"). So this renders straight into a plain mount div
  // renderSidebar() leaves for it, always open, no expand/collapse state and
  // no _dimCache entry (that cache exists to survive a section being
  // collapsed and reopened — irrelevant here since there's nothing to
  // collapse; renderSidebar() re-fetches on every render like the rest of the
  // sidebar already does).
  //
  // card=True on GET /api/recordings/favorites (added alongside this) is what
  // supplies `image_id` for the small artist thumbnail Ryan asked for.
  // The FAVORITES header lives INSIDE the rendered block, not in the sidebar
  // markup, so it disappears with the list. A standing header over nothing
  // advertises an empty shelf; this way the section is absent until it has
  // something to hold (which is why the flattened version had no header at
  // all — Ryan asked for one back on 2026-08-23, with Collections' styling
  // minus the chevron, since there is nothing here to expand or collapse).
  // Single entry point for "the favourites shelf is stale". Called from every
  // place a recording can be starred or unstarred — the Browse card button and
  // the Recording view's toggle — so the two can never drift apart again.
  // Deliberately fire-and-forget: a failed sidebar refresh must never surface
  // as a failed favourite, because the favourite itself already saved.
  function refreshFavoritesNav() {
    try { _renderFavoritesFlat() } catch (_) {}
  }

  // Shared by FAVORITES and by an expanded collection (Ryan, 2026-08-23:
  // "the style of each recording in the collection should be the same style as
  // the recording in Favorites"). Both payloads come back with card=True, so
  // both carry image_id and the photo path works in both places.
  function navRecRowHtml(r, extraCls) {
    // Studio: lead with the title (falling back to the artist), artist
    // second, year last -- never the venue this row would otherwise show.
    const id = recIdentity(r)
    const full = esc((id.isStudio ? [id.lead, id.sub, id.dateText] : [r.artist, r.date, r.venue])
      .filter(Boolean).join(' · '))
    const initials = String(r.artist || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    const avatar = r.image_id
      ? `<img class="nav-fav-avatar" src="${API.artists.imageUrl(r.image_id)}" alt="">`
      : `<div class="nav-fav-avatar nav-fav-avatar--blank">${esc(initials)}</div>`
    return `
      <div class="nav-fav-row${extraCls ? ' ' + extraCls : ''}" data-id="${r.id}" title="${full}">
        ${avatar}
        <span class="nav-fav-title truncate">${full}</span>
      </div>`
  }

  async function _renderFavoritesFlat() {
    const box = document.getElementById('nav-favorites-flat')
    if (!box) return
    let rows = []
    try {
      rows = libraryState.activeId != null
        ? await API.remoteFavorites.list(libraryState.activeId, true)
        : await API.recordings.favorites()
    } catch (_) {}
    if (!rows.length) { box.innerHTML = ''; return }   // nothing to announce — see comment above
    const head = '<div class="nav-item nav-top nav-shelf-head nav-shelf-head--static nav-shelf-head--spaced">Favorites</div>'
    box.innerHTML = head + rows.map(r => navRecRowHtml(r)).join('')
    box.querySelectorAll('.nav-fav-row[data-id]').forEach(el =>
      el.addEventListener('click', () => { window.location.hash = `#/recording/${el.dataset.id}` }))
  }

  // Invalidate one or more dimension caches and silently re-render any open ones.
  // Call after edits that can prune/create artists, venues, or musicians.
  function invalidateDims(...dims) {
    dims.forEach(d => {
      _dimCache[d] = null
      if (state.expandedDims.has(d)) _renderDimRecords(d)
    })
  }

  // Header "+ Create new" action per dimension.
  function createInDim(dim) {
    // Every dimension now goes to a real create FORM (2026-08-07). Venues used
    // to land on the admin list — a view-and-edit screen, not a create flow —
    // and artists/musicians used a window.prompt().
    // Genres sent you to the INDEX rather than a create form, because until
    // 2026-09-01 there wasn't one — the only way to make a genre was the admin
    // list's inline form. There is a real form now, so it goes where the other
    // four go.
    if (dim === 'collections')     window.location.hash = '#/collection/new'
    else if (dim === 'venues')     window.location.hash = '#/venue/new'
    else if (dim === 'genres')     window.location.hash = '#/genre/new'
    else if (dim === 'artists') window.location.hash = '#/artist/new'
    else if (dim === 'musicians')    window.location.hash = '#/musician/new'
    else if (dim === 'events')     window.location.hash = '#/event/new'
  }
  // _promptCreate() removed 2026-08-07 — every dimension now opens a real
  // create form (renderCreateForm) instead of a window.prompt().
  // ══ Library selector ═══════════════════════════════════════════════════════
  //
  // Sits above "Add Recordings" (Ryan, 2026-08-08). Your own library is the
  // default and always first; libraries shared WITH you appear beneath it once
  // the outbound side exists.
  //
  // Built now, before there is anything to select, on purpose: it is the frame
  // the peer theme hangs off, and it makes "which library am I in?" a question
  // the UI answers at all times rather than only when the answer is unusual.
  // With one entry it renders as a plain, non-interactive label — a dropdown
  // arrow that opens a menu of one is a lie about what the app can do.
  //
  // `remotes` stays empty until `remote_node` lands (milestone 2); the selector
  // reads it rather than checking a feature flag, so it starts working the day
  // remotes exist with no change here.
  const libraryState = {
    remotes: [],        // [{id, display_name, last_connected_at}]
    activeId: null,     // null = my own library
    favIds: new Set(),  // MY starred ids inside the ACTIVE remote library
  }

  function activeLibrary() {
    if (libraryState.activeId == null) return null
    return libraryState.remotes.find(r => r.id === libraryState.activeId) || null
  }

  function librarySelectorHtml() {
    const active = activeLibrary()
    const label = active ? active.display_name : 'My Library'
    const solo = libraryState.remotes.length === 0
    return `
      <div class="lib-select${solo ? ' is-solo' : ''}${active ? ' is-remote' : ''}"
           id="lib-select" ${solo ? '' : 'role="button" tabindex="0"'}>
        <span class="lib-select-icon">${active ? icon('arrow-left-right') : icon('library')}</span>
        <span class="lib-select-name truncate">${esc(label)}</span>
        ${solo ? '' : `<span class="lib-select-caret">${chevronIcon('caret-ic--down')}</span>`}
      </div>
      <div class="lib-select-menu" id="lib-select-menu" style="display:none"></div>`
  }

  // Renders the selector into its App Header host and wires it. Called from
  // renderSidebar(), which already fires at every moment this can change.
  //
  // With no remote libraries the host is left EMPTY rather than showing a
  // one-option control: a caret that opens a menu of one promises something the
  // app cannot yet do, and in the header — where space is now contested by the
  // mode toggle and the user chip — a label that only ever says "My Library"
  // beside a sidebar heading that says the same thing is pure duplication.
  // The moment a remote is joined it appears.
  function renderLibrarySelector() {
    const host = document.getElementById('lib-select-host')
    if (!host) return

    // With no libraries joined this host used to render EMPTY, which made the
    // only possible entry point invisible until after you had already joined
    // something. A listener handed an invite had nowhere to paste it. So the
    // empty state is now the invitation itself.
    if (libraryState.remotes.length === 0) {
      host.innerHTML = `
        <button class="btn btn-ghost btn-sm lib-join-btn" id="lib-join-empty">
          ${icon('plus', 'lib-join-ic')}Join a Library
        </button>`
      host.querySelector('#lib-join-empty')
          .addEventListener('click', openJoinLibraryModal)
      return
    }

    host.innerHTML = librarySelectorHtml()
    wireLibrarySelector(host)
  }

  function wireLibrarySelector(nav) {
    const el = nav.querySelector('#lib-select')
    const menu = nav.querySelector('#lib-select-menu')
    if (!el || !menu || libraryState.remotes.length === 0) return

    const close = () => { menu.style.display = 'none' }
    const open = () => {
      menu.innerHTML = [
        { id: null, display_name: 'My Library' },
        ...libraryState.remotes,
      ].map(l => `
        <div class="lib-select-opt${l.id === libraryState.activeId ? ' active' : ''}"
             data-lib-id="${l.id == null ? '' : l.id}">
          <span class="lib-select-icon">${l.id == null ? icon('library') : icon('arrow-left-right')}</span>
          <span class="truncate">${esc(l.display_name)}</span>
          ${l.id == null ? '' :
            `<span class="lib-select-leave" data-leave-id="${l.id}"
                   title="Leave ${esc(l.display_name)}">${icon('x')}</span>`}
        </div>`).join('')
        + `<div class="lib-select-join" id="lib-select-join">
             <span class="lib-select-icon">${icon('plus')}</span>
             <span>Join a Library…</span>
           </div>`
      menu.style.display = 'block'

      menu.querySelectorAll('.lib-select-opt').forEach(opt =>
        opt.addEventListener('click', () => {
          const raw = opt.dataset.libId
          switchLibrary(raw === '' ? null : Number(raw))
          close()
        }))

      // Leave sits INSIDE a row whose own click switches library, so it has to
      // stop propagation or leaving would also navigate into the thing you are
      // leaving.
      menu.querySelectorAll('.lib-select-leave').forEach(x =>
        x.addEventListener('click', e => {
          e.stopPropagation()
          close()
          leaveLibrary(Number(x.dataset.leaveId))
        }))

      menu.querySelector('#lib-select-join')
          .addEventListener('click', () => { close(); openJoinLibraryModal() })
    }
    el.addEventListener('click', () => {
      menu.style.display === 'block' ? close() : open()
    })
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.click() }
    })
    document.addEventListener('click', e => {
      if (!e.target.closest('#lib-select, #lib-select-menu')) close()
    })
  }

  // Switching library is a whole-app context change, not a navigation: the
  // theme flips, the sidebar reloads, and the current view is meaningless in
  // the new context. So it resets to the library root rather than trying to
  // map, say, /artist/12 onto a different database's ids.
  async function switchLibrary(id) {
    if (libraryState.activeId === id) return
    libraryState.activeId = id
    // Tell api.js first — everything rendered after this line must resolve
    // against the new library, and route() below re-renders immediately.
    API.setLibraryContext(id)
    applyPeerTheme()
    // Before anything renders: stars paint from this, and a nav that drew
    // first would show every one of them empty.
    await loadRemoteFavorites()
    // initViewMode() is idempotent (its event wiring is guarded by _wired),
    // so calling it again here just re-derives playback-mode/the toggle for
    // whichever library is now active, rather than leaving stale chrome from
    // the library we just left.
    initViewMode()
    window.location.hash = '#/'
    renderSidebar()
    route()
  }

  // ── Joining and leaving libraries ─────────────────────────────────────────
  //
  // The front door for the entire consumer side — and it did not exist until
  // 2026-08-24. `API.remotes.enroll` and `API.remotes.leave` had been in
  // api.js since the August milestone with NOTHING in the frontend calling
  // either one: the dev rig enrolled by curl, so the gap was invisible during
  // development and total for a real user.

  async function openJoinLibraryModal() {
    const wrap = document.createElement('div')
    wrap.className = 'modal-overlay'
    wrap.innerHTML = `
      <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="join-title">
        <div class="modal-header"><h3 id="join-title">Join a Library</h3></div>
        <div class="modal-body">
          <p class="join-note">Paste the invite you were sent. It is an address
            and a code joined by a <span class="join-hash">#</span>.</p>
          <input type="text" class="join-input" id="join-invite" autocomplete="off"
                 spellcheck="false" placeholder="https://their-library#CODE" />
          <p class="join-error" id="join-error" hidden></p>
        </div>
        <div class="modal-footer">
          <button class="btn btn-sm btn-ghost" id="join-cancel">Cancel</button>
          <button class="btn btn-sm btn-primary" id="join-go">Join</button>
        </div>
      </div>`
    document.body.appendChild(wrap)

    const input = wrap.querySelector('#join-invite')
    const err   = wrap.querySelector('#join-error')
    const go    = wrap.querySelector('#join-go')
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey) }
    const onKey = e => {
      if (e.key === 'Escape') close()
      if (e.key === 'Enter' && document.activeElement === input) go.click()
    }
    document.addEventListener('keydown', onKey)
    wrap.querySelector('#join-cancel').addEventListener('click', close)
    wrap.addEventListener('click', e => { if (e.target === wrap) close() })
    input.focus()

    const fail = msg => { err.textContent = msg; err.hidden = false; go.disabled = false; go.textContent = 'Join' }

    go.addEventListener('click', async () => {
      const invite = input.value.trim()
      err.hidden = true
      if (!invite) return fail('Paste an invite first.')
      // Checked here rather than letting the server say it, because this is
      // the one mistake a human actually makes: pasting the bare code without
      // the address it came with. The server cannot guess the address, so the
      // error would otherwise be a confusing "not a usable address".
      if (!invite.includes('#')) {
        return fail('That looks like just the code. The invite needs the address too: "https://their-library#CODE".')
      }

      go.disabled = true; go.textContent = 'Joining…'
      let node
      try {
        node = await API.remotes.enroll(invite)
      } catch (e) {
        return fail(e.message || 'Could not join that library.')
      }
      // A node that enrolled without a retrievable credential is broken, not
      // empty — say so here rather than letting it present as a library with
      // nothing in it.
      if (node && node.has_token === false) {
        return fail('Joined, but the access token could not be saved to your keychain. Try again.')
      }
      close()
      await loadRemotes()
      renderLibrarySelector()
      if (node && node.id != null) switchLibrary(node.id)
    })
  }

  async function leaveLibrary(id) {
    const lib = libraryState.remotes.find(r => r.id === id)
    const name = lib ? lib.display_name : 'this library'
    if (!confirm(`Leave ${name}?\n\nYou will lose access until they invite you again. Nothing of yours is deleted.`)) return
    try {
      await API.remotes.leave(id)
    } catch (e) {
      alert('Could not leave: ' + e.message)
      return
    }
    // Leaving the library you are standing in has to move you somewhere real,
    // or every subsequent request proxies to a remote that no longer exists.
    if (libraryState.activeId === id) switchLibrary(null)
    await loadRemotes()
    renderLibrarySelector()
    renderSidebar()
  }

  // Joined remote libraries. Failure is deliberately silent: a remote list that
  // can't be fetched leaves `remotes` empty, the selector renders as a plain
  // label, and the app behaves exactly as it did before remotes existed —
  // rather than blocking startup over a feature the user may not be using.
  async function loadRemotes() {
    try {
      libraryState.remotes = await API.remotes.list()
    } catch (_) {
      libraryState.remotes = []
    }
  }

  // No longer a "theme" function despite the name (kept for call-site
  // stability — renaming ~1 call site wasn't worth the churn). The Cool
  // Slate retint this used to drive is gone (2026-08-24, Ryan: "get rid of
  // the third theme... have the host library show up the same as the user's
  // preferences define") — a peer's library now just renders in whichever
  // of the two real themes the viewer has chosen. `.peer-mode` on <html>
  // still exists and still matters: it's the flag the non-colour peer rules
  // in main.css key off (library-selector centre cluster hidden, the
  // drive-offline banner hidden, the `[data-admin]` backstop).
  function applyPeerTheme() {
    document.documentElement.classList.toggle('peer-mode', libraryState.activeId != null)
  }

  // Bumped at the top of every renderSidebar() call so the async
  // kindCounts().then below can tell whether it is still the newest one in
  // flight -- a slow response landing after a later renderSidebar (or after
  // the active library changed) must not touch a sidebar it no longer owns.
  let _sidebarRenderSeq = 0

  // OTHER ARCHIVES: the Live Music Archive (admin only) and one entry per
  // library joined through an invite, which any role may browse. A peer entry
  // switches library exactly as the header selector does (switchLibrary).
  // All entries carry the icon Live Recordings had before it became a ticket.
  function _otherArchivesHtml() {
    const lma = _dlAllowed()
      ? `<a class="nav-item" data-nav="archive-lma" href="#/archive/lma">${icon('landmark', 'nav-ic')}Live Music Archive</a>`
      : ''
    const peers = (libraryState.remotes || []).map(r =>
      `<a class="nav-item${libraryState.activeId === r.id ? ' active' : ''}" href="#/" data-lib-switch="${r.id}">${icon('library', 'nav-ic')}<span class="truncate">${esc(r.display_name)}</span></a>`).join('')
    // Two sections (Ryan, 2026-10-01): public archives, then libraries this
    // user is a peer of. Each header shows only when it has entries.
    const head = t => `<div class="nav-item nav-top nav-shelf-head nav-shelf-head--static nav-shelf-head--spaced">${t}</div>`
    return (lma ? `
        ${head('Archives')}
        ${lma}` : '') + (peers ? `
        ${head('Other Libraries')}
        ${peers}` : '')
  }

  async function renderSidebar() {
    const nav = document.getElementById('sidebar-nav')
    if (!nav) return
    // Workshop/Backlog entries depend on which working folders are set.
    if (!appPrefs) await getPrefs()
    const _mySidebarSeq = ++_sidebarRenderSeq
    const _mySidebarLib = libraryState.activeId
    _dimCache.venues = _dimCache.artists = _dimCache.musicians =
      _dimCache.collections = _dimCache.genres = _dimCache.events = null

    // A shared library offers a deliberately narrower sidebar. This is not
    // squeamishness about peer mode — it is that api/share.py has no LIST
    // endpoint for venues, artists or musicians, by design: a peer reaches
    // those pages FROM a recording they were granted, never by browsing an
    // index of everything the owner holds. Rendering sections that could only
    // ever be empty would advertise a door that isn't there.
    //
    // Collections and Genres do have peer-facing lists (both scoped to the
    // visible set), so both stay. Add Recordings and Sharing are local
    // operations on my own library and are meaningless here.
    // A shared library gets Library + Collections and nothing else (Ryan,
    // 2026-08-08). The dimension indexes are dropped entirely rather than
    // moved: a peer reaches an artist, venue or genre page FROM a recording
    // they were granted, and an index listing three genres is noise pretending
    // to be navigation. The pages themselves still exist and still work.
    // Reworked 2026-08-22 (Ryan) along the lines of Spotify's left column: this
    // is a shelf of things you chose to keep — starred shows, collections, and
    // playlists once those exist — not a menu of the app's pages.
    //
    // What left, and where it went:
    //   Recently Added    → it was already a module on the Library view. A nav
    //                       link to a page that duplicates a module you scroll
    //                       past on arrival is a second front door to one room.
    //   Sharing           → Settings. Peer management is account configuration,
    //                       not a place in the library.
    //   Library selector  → the App Header, beside the mode toggle. Switching
    //                       library is a whole-app context change; the sidebar
    //                       is for moving around inside one.
    //   Add Recordings    → was the bottom, moved back to the TOP 2026-08-22
    //                       (Ryan) — it is the entry point into ingest, and he
    //                       wants it as the first thing under the App Header,
    //                       not below a shelf you have to scroll past first.
    //
    // Left Nav Refinement (Ryan, 2026-08-23) — the App Header's Home button is
    // gone; "My Library" IS the link back to Library Home now, one control
    // instead of two that did the same thing. Collections moved up to sit
    // directly under it, unindented, at My Library's own font size — no longer
    // a generic "sub" dimension. Favorites stopped being a dimension section
    // at all: no heading, no caret, no collapse — the starred shows themselves
    // just appear, flush, right under Collections (see _renderFavoritesFlat).
    //
    // The whole upper shelf (Add Recordings / My Library / Collections /
    // Favorites) now lives in its own `.nav-scroll` wrapper so it can scroll
    // independently, while `.nav-dims-foot` (Venues/Artists/Musicians/Genres
    // — explicitly out of scope for this rework, Ryan's own words) sits
    // OUTSIDE that wrapper as a sibling, so it stays pinned to the sidebar's
    // bottom no matter how many favorites someone piles up. `.nav-spacer` is
    // gone — it existed only to push the footer down when the scroll region
    // was short, which `.nav-scroll{flex:1}` now does on its own even with
    // nothing in it to scroll.
    const remote = libraryState.activeId != null
    const active = activeLibrary()

    // ONE sidebar for both contexts (Ryan, 2026-08-24). A listener browsing a
    // shared library gets exactly what the owner sees in Playback mode —
    // Browse / Search / Recently Added, the curator's Collections, the
    // curator's Favorites, and the dimension foot — because "see the full
    // information system on the left" is the point of handing someone a
    // library at all.
    //
    // This reverses the narrow peer sidebar of 2026-08-09, which existed
    // because share.py had no LIST endpoints and rendering sections that could
    // only ever be empty would advertise doors that were not there. Those
    // endpoints now exist (venues, musicians, favorites, search, collections),
    // so the reasoning has expired rather than been overruled.
    //
    // Nothing here branches on `remote` except the header LABEL. It does not
    // need to: `canEditLibrary()` is false in a remote library, so Add
    // Recordings and every "+" drop out on their own. A second template is how
    // the two drift apart — which is exactly what happened last time.
    const shelfTitle = remote
      ? (active ? active.display_name : 'Shared Library')
      : 'My Library'
    // Bulk Ingest (spec 1.9/4, chunk 7c) — local only, never in a shared
    // library (a peer's own machine has whatever bulkIngest status it has;
    // nothing here is about MY library when I'm looking at theirs).
    // The entry shows while ANY listed run exists (runs() lists only unfinished
    // runs and Review First runs whose Queue still has ready/review items) and
    // links to the most recent one. Add Recordings follows the same target.
    const biHash = remote ? null : _biOpenRunHash()
    // Rows waiting across the listed runs (ready + needs review); the Add
    // Recordings button carries the count.
    const biWaiting = remote ? 0 : _biWaitingCount()
    const homeHashUrl = remote ? '#/' : homeHash()
    nav.innerHTML = `
      <div class="nav-scroll">
        ${canEditLibrary() ? `<a class="nav-add-btn" data-nav="ingest" href="${biHash || '#/ingest'}"><span class="nav-add-plus">${icon('plus')}</span> Add Recordings${biWaiting ? `<span class="nav-add-count">${biWaiting}</span>` : ''}</a>` : ''}
        <a class="nav-item nav-top nav-shelf-head nav-shelf-head--static truncate" data-nav="library" href="${homeHashUrl}">${esc(shelfTitle)}</a>
        <a class="nav-item" data-nav="library" href="${homeHashUrl}">${icon('library', 'nav-ic')}Live Recordings</a>
        <a class="nav-item" data-nav="search" href="#/search">${icon('search', 'nav-ic')}Search</a>
        <a class="nav-item" data-nav="recent" href="#/recent">${icon('clock', 'nav-ic')}Recently Added</a>
        ${_dlAllowed() ? `<a class="nav-item" data-nav="downloads" href="#/downloads">${icon('download', 'nav-ic')}Downloads<span class="nav-record-count nav-dl-count" data-dl-count>${DL.folderCount || ''}</span></a>` : ''}
        ${_dlAllowed() && triageDests().includes('workshop') ? `<a class="nav-item" data-nav="workshop" href="#/workshop">${icon('wrench', 'nav-ic')}Workshop</a>` : ''}
        ${_dlAllowed() && triageDests().includes('backlog') ? `<a class="nav-item" data-nav="backlog" href="#/backlog">${icon('archive', 'nav-ic')}Backlog</a>` : ''}
        <!-- Collections was the ONE section with no actions on its header
             (Ryan, 2026-09-03) — every dimension in .nav-dims-foot has had a
             "+" and a refresh since the sidebar was built, and this one was
             skipped because it is a static header rather than a _dimSection.
             Same two controls, same order, same icons; the header itself
             stays static (it does not expand, so it has no caret). -->
        <div class="nav-item nav-top nav-shelf-head nav-shelf-head--static nav-shelf-head--spaced nav-shelf-head--acts">
          <span class="nav-dim-label truncate">My Collections</span>
          <span class="nav-dim-actions">
            ${canEditLibrary() ? `<span class="nav-action" data-col-new data-admin
                     title="Create new collection">${icon('plus')}</span>` : ''}
            <span class="nav-action" data-col-refresh title="Refresh collections">${icon('rotate-cw')}</span>
          </span>
        </div>
        <div class="nav-records" id="nav-records-collections"></div>
        <div class="nav-favorites" id="nav-favorites-flat"></div>
        ${_otherArchivesHtml()}
      </div>
      <div class="nav-dims-foot">
        ${_dimSection('venues', icon('map-pin'), 'Venues')}
        ${_dimSection('artists', icon('users'), 'Artists')}
        ${_dimSection('musicians', icon('user'), 'Musicians')}
        ${_dimSection('events', icon('calendar'), 'Events')}
        ${_dimSection('genres', icon('tag'), 'Genres')}
      </div>`

    // The library selector lives in the App Header now, but it is rendered from
    // here: this function already runs at every moment the selector could need
    // to change (boot, after loadRemotes, on a mode switch, after a library
    // switch), and one render path is worth more than tidy ownership.
    renderLibrarySelector()
    nav.querySelectorAll('.nav-expand').forEach(el => {
      el.addEventListener('click', e => {
        if (e.target.closest('.nav-action')) return
        _toggleDim(el.dataset.dim)
      })
    })
    nav.querySelectorAll('.nav-action').forEach(el => {
      el.addEventListener('click', e => {
        e.stopPropagation()
        // ⚠ Collections' header carries .nav-action too now, but it is a
        // static shelf head, not a .nav-dim — `closest` returns null there and
        // reading .dataset off it is a TypeError that kills every listener
        // this loop had not yet attached. Its two actions are wired
        // separately, just below.
        const dimEl = el.closest('.nav-dim')
        if (!dimEl) return
        if (el.dataset.act === 'refresh') _refreshDim(dimEl.dataset.dim)
        else createInDim(dimEl.dataset.dim)
      })
    })
    nav.querySelectorAll('[data-lib-switch]').forEach(el =>
      el.addEventListener('click', e => {
        e.preventDefault()
        switchLibrary(Number(el.dataset.libSwitch))
      }))
    nav.querySelector('[data-col-new]')?.addEventListener('click', e => {
      e.stopPropagation(); createInDim('collections')
    })
    nav.querySelector('[data-col-refresh]')?.addEventListener('click', e => {
      e.stopPropagation(); _refreshCollections()
    })
    state.expandedDims.forEach(dim => _renderDimRecords(dim))
    _renderDimRecords('collections')   // always rendered — no longer a toggle
    // Favorites is the VIEWER'S, in both worlds (Ryan, 2026-08-24).
    //
    // The OWNER'S stars never travel. The star is deliberately not a quality
    // scale — "this one is special", one click, no deliberation — and it is
    // only free to mean that while it stays private; publish it and you start
    // starring for an audience, which costs you the tool. Same argument that
    // keeps play_log home. Curation travels through COLLECTIONS, the surface
    // built to be read by someone else.
    //
    // So this section means what it says everywhere else in software: MINE.
    // In my own library that is Recording.is_favorite; in a joined library it
    // is the remote_favorite rows on this node. One section, one meaning, two
    // stores — resolved in _renderFavoritesFlat, not here.
    _renderFavoritesFlat()
    setActiveNav(state._activeNav)
    _dlSidebarSync()

    // Albums nav entry (Studio Records spec v1, chunk 7) — present only
    // when the visible set holds at least one studio record (a peer's own
    // visible set too: 'recordings' is REMOTE_CAPABLE). Fetched after the
    // rest of the sidebar has already painted so a slow call never blocks
    // it; inserted in place once it resolves, and re-fetched every time the
    // sidebar itself refreshes since this whole function is that refresh.
    API.recordings.kindCounts().then(counts => {
      if (_mySidebarSeq !== _sidebarRenderSeq || libraryState.activeId !== _mySidebarLib) return
      if (!nav.isConnected || !counts || !counts.studio) return
      if (nav.querySelector('[data-nav="albums"]')) return
      // Directly under Live Recordings (Ryan, 2026-10-01): Live Recordings,
      // Albums, Search, Recently Added.
      const liveLink = nav.querySelector('a.nav-item[data-nav="library"]:not(.nav-shelf-head)')
      if (!liveLink) return
      liveLink.insertAdjacentHTML('afterend',
        `<a class="nav-item" data-nav="albums" href="#/albums">${icon('disc', 'nav-ic')}Albums</a>`)
      setActiveNav(state._activeNav)
    }).catch(() => {})
  }

  // Back-compat alias — call sites still say loadArtistList().
  const loadArtistList = renderSidebar

  // ── Recording image: one rule everywhere (Ryan, 2026-10-04) ──────────────────
  // The server resolves the chain (the recording's own image, else its
  // artist's -- see serialize._primary_recording_image_url) into `image_url`.
  // What is left for the client is the last link: no url means the artist's
  // initials. Every surface that draws a recording's image takes its initials
  // from here, so the three-step rule has one implementation on each side.
  function recInitials(r) {
    return String(r?.artist || '?').split(/\s+/).filter(Boolean)
      .slice(0, 2).map(w => w[0]).join('').toUpperCase()
  }

  // ── Shared compact recording row (one line, all show info) ───────────────────
  function flatRowHtml(r, showArtist) {
    const id      = recIdentity(r)
    // Studio: date cell is the year (or blank), the venue cell carries the
    // title instead (falling back to the artist when there is no title),
    // and location/source/quality stay empty -- a studio release has none
    // of those (Studio Records spec v1, chunk 5).
    const date    = id.isStudio ? id.dateText : fmtDate(r.start_year, r.start_month, r.start_day)
    const loc     = id.isStudio ? '' : fmtLocation(r.city, r.state, r.country)
    const quality = id.isStudio ? '' : (r.quality || '')
    const venueText = id.isStudio ? id.lead : (r.venue || '(unknown venue)')
    const runtime = fmtRuntime(r.duration_sec)
    const inc     = r.is_complete ? '' : '<span class="rec-inc" title="Incomplete recording">inc</span>'
    // Every row carries the image column: the recording's image, else the
    // artist's, else the artist's initials (Ryan, 2026-10-04).
    const thumb   = r.image_url
      ? `<img class="rec-thumb-sm" src="${esc(r.image_url)}" alt="" loading="lazy">`
      : `<span class="rec-thumb-sm rec-thumb-sm--initials">${esc(recInitials(r))}</span>`
    return `
      <div class="rec-row rec-row--flat ${showArtist ? 'with-artist' : ''}" data-rec-id="${r.id}">
        ${thumb}
        ${showArtist ? `<span class="rec-artist-cell truncate">${esc(r.artist || '')}</span>` : ''}
        <span class="rec-date truncate">${esc(date)}</span>
        <span class="rec-venue truncate">${esc(venueText)}</span>
        <span class="rec-location truncate">${esc(loc)}</span>
        <span>${id.isStudio ? '' : sourceBadge(r.source)}</span>
        <span class="quality ${id.isStudio ? '' : qualityClass(quality)}">${esc(quality)}</span>
        <span class="rec-runtime">${runtime}</span>
        <span class="rec-tracks">${r.track_count}t${inc ? ' ' + inc : ''}</span>
        <span class="rec-date-added">${esc(fmtDateAdded(r.created_at))}</span>
        <button class="rec-fav-star rec-fav-star--sm${viewerHasFavorited(r) ? ' is-fav' : ''}" data-rec-id="${r.id}"
                aria-pressed="${viewerHasFavorited(r) ? 'true' : 'false'}"
                title="${viewerHasFavorited(r) ? 'Remove from favorites' : 'Mark as favorite'}">${icon('star', null, viewerHasFavorited(r))}</button>
        <button class="rec-play-btn" data-rec-id="${r.id}" title="Play">${icon('play')}</button>
      </div>`
  }

  // Minimal header row paired with flatRowHtml's grid — every cell is blank
  // except "Added", which doubles as a click-to-sort toggle (default: unsorted,
  // i.e. whatever order the page already puts rows in).
  function recTableHeadHtml(showArtist) {
    return `
      <div class="rec-table-head ${showArtist ? 'with-artist' : ''}">
        <span></span>
        ${showArtist ? '<span></span>' : ''}
        <!-- One blank cell per data column before "Added": date, venue, location,
             source, quality, runtime, tracks. The rating column was removed
             2026-08-18 — keep this count in step with flatRowHtml() and with the
             grid-template-columns pair in main.css or the header shears off the
             row it labels. -->
        <span></span><span></span><span></span><span></span><span></span><span></span><span></span>
        <button class="rec-th-added" type="button" title="Sort by date added">Added <span class="rec-th-arrow"></span></button>
        <span></span><span></span>
      </div>`
  }

  // Wires the "Added" header's sort toggle for a rendered rec-table. `rows` is the
  // page's row-data array (left in its original/default order); sorting is purely
  // a display-time re-render, it doesn't touch how the page loads next time.
  function wireDateAddedSort(mountEl, rows, showArtist) {
    const head = mountEl?.previousElementSibling
    const btn  = head?.querySelector('.rec-th-added')
    const arrow = head?.querySelector('.rec-th-arrow')
    if (!mountEl || !btn) return
    let dir = null   // null = default order; 'asc' | 'desc' once clicked
    btn.addEventListener('click', () => {
      dir = dir === 'desc' ? 'asc' : 'desc'
      const sorted = rows.slice().sort((a, b) => {
        const av = a.created_at || '', bv = b.created_at || ''
        return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av)
      })
      mountEl.innerHTML = sorted.map(r => flatRowHtml(r, showArtist)).join('')
      wireRecordingRows(mountEl)
      arrow.textContent = dir === 'asc' ? '▲' : '▼'
    })
  }

  function wireRecordingRows(container) {
    container.querySelectorAll('.rec-row').forEach(el => {
      el.addEventListener('click', e => {
        if (e.target.closest('.rec-play-btn') || e.target.closest('.rec-fav-star')) return
        window.location.hash = `#/recording/${el.dataset.recId}`
      })
      el.addEventListener('contextmenu', e => {
        e.preventDefault()
        openAddToCollectionMenu(parseInt(el.dataset.recId), e.clientX, e.clientY)
      })
    })
    // Card surfaces get the SAME right-click menu (2026-08-07). Handbill and
    // row cards are real anchors, so navigation already works without a click
    // handler — but the add-to-collection menu was table-only, and cards are
    // now the primary surface on Browse, Recently Added and Collections. That
    // would have quietly removed the only way to file a recording.
    container.querySelectorAll('.rec-card[data-rec-id], .rec-rowcard[data-rec-id]').forEach(el => {
      el.addEventListener('contextmenu', e => {
        e.preventDefault()
        openAddToCollectionMenu(parseInt(el.dataset.recId), e.clientX, e.clientY)
      })
    })
    container.querySelectorAll('.rec-play-btn').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation()
        playRecording(parseInt(btn.dataset.recId), 0, null)
      })
    })
    // Favorite star, table row (2026-08-09) — same optimistic-toggle pattern
    // as the recording page's own button (see renderRecordingView), just
    // scoped per-row and reusing the icon-only .rec-fav-star instead of the
    // text button (no room for text in a compact grid column).
    container.querySelectorAll('.rec-fav-star--sm').forEach(btn => {
      btn.addEventListener('click', async e => {
        e.stopPropagation()
        const id = parseInt(btn.dataset.recId)
        const on = btn.getAttribute('aria-pressed') !== 'true'
        const paint = (isFav) => {
          btn.classList.toggle('is-fav', isFav)
          btn.innerHTML = icon('star', null, isFav)
          btn.setAttribute('aria-pressed', isFav ? 'true' : 'false')
          btn.title = isFav ? 'Remove from favorites' : 'Mark as favorite'
        }
        paint(on)
        btn.disabled = true
        try {
          await setViewerFavorite(id, on)
          refreshFavoritesNav()   // the card's star and the shelf are one control
        } catch (err) {
          paint(!on)
          alert('Could not save favorite: ' + err.message)
        } finally { btn.disabled = false }
      })
    })
  }

  // Add a recording to a collection (or create one). onAdded({id, name}) fires on success.
  async function openAddToCollectionMenu(recId, x, y, onAdded) {
    document.getElementById('collection-menu')?.remove()
    let cols = []
    // System collections are excluded: their membership is a query, and the API
    // refuses a hand-add with 409. Offering one here would be a menu item whose
    // only possible outcome is an error.
    try { cols = (await API.collections.list()).filter(isCuratedCollection) } catch (_) {}
    const menu = document.createElement('div')
    menu.className = 'track-qmenu'; menu.id = 'collection-menu'
    menu.innerHTML = `
      <div class="track-qmenu-label">Add to collection</div>
      ${cols.map(c => `<div class="col-menu-item" data-id="${c.id}" data-name="${esc(c.name)}">${esc(c.name)}</div>`).join('')
        || '<div class="col-menu-empty">No collections yet</div>'}
      <div class="col-menu-item col-menu-new">+ Create collection…</div>`
    document.body.appendChild(menu)
    const r = menu.getBoundingClientRect()
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth  - r.width  - 8)) + 'px'
    menu.style.top  = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + 'px'
    const close = () => menu.remove()
    async function addTo(colId, name) {
      try { await API.collections.addRecording(colId, recId); onAdded && onAdded({ id: colId, name }) }
      catch (e) { alert('Failed: ' + e.message) }
      close()
    }
    menu.querySelectorAll('.col-menu-item[data-id]').forEach(el =>
      el.addEventListener('click', () => addTo(parseInt(el.dataset.id), el.dataset.name)))
    menu.querySelector('.col-menu-new').addEventListener('click', async () => {
      const name = prompt('New collection name:')
      if (!name || !name.trim()) { close(); return }
      try { const c = await API.collections.create({ name: name.trim() }); await addTo(c.id, name.trim()) }
      catch (e) { alert('Failed: ' + e.message); close() }
    })
    setTimeout(() => document.addEventListener('mousedown', function h(e) {
      if (!menu.contains(e.target)) { close(); document.removeEventListener('mousedown', h) }
    }), 0)
  }

  // Collection tags on the recording detail (styled like flag pills).
  function collectionTagHtml(c) {
    return `<span class="collection-tag" data-id="${c.id}">${esc(c.name)}<span class="collection-tag-x" title="Remove from collection">${icon('x')}</span></span>`
  }
  function wireCollectionTag(tagEl, recId) {
    tagEl.querySelector('.collection-tag-x')?.addEventListener('click', async () => {
      try { await API.collections.removeRecording(parseInt(tagEl.dataset.id), recId); tagEl.remove() }
      catch (e) { alert('Failed: ' + e.message) }
    })
  }
  function wireRecCollectionArea(recId) {
    const box = document.getElementById('rec-collections')
    if (!box) return
    box.querySelectorAll('.collection-tag').forEach(t => wireCollectionTag(t, recId))
    document.getElementById('btn-add-collection')?.addEventListener('click', e => {
      openAddToCollectionMenu(recId, e.clientX, e.clientY, ({ id, name }) => {
        if (box.querySelector(`.collection-tag[data-id="${id}"]`)) return
        const span = document.createElement('span')
        span.className = 'collection-tag'; span.dataset.id = id
        span.innerHTML = `${esc(name)}<span class="collection-tag-x" title="Remove from collection">${icon('x')}</span>`
        box.insertBefore(span, document.getElementById('btn-add-collection'))
        wireCollectionTag(span, recId)
      })
    })
  }

  // ── Collections views ────────────────────────────────────────────────────────
  // ══ Shared entity-page shell ═══════════════════════════════════════════════
  //
  // Hero + tab strip + panes, used by Artist, Venue, Musician (person), Genre
  // and Collection (Ryan, 2026-08-07). Extracted rather than copied five times:
  // four copies is exactly the situation that produced today's three-copy
  // analysis refactor and the .rec-row class collision, and a spacing or
  // navigation fix should not need applying five times.
  //
  // NAMING: the CSS keeps its `.pp-*` prefix. It reads as "artist page" and
  // now means "entity page" — renaming sixty-odd selectors and their JS
  // references overnight is a large diff with real regression risk for zero
  // behavioural gain. Treat `pp-` as the entity-page namespace.
  //
  // opts:
  //   navBack   {label, hash} | null   — breadcrumb
  //   portrait  html | ''              — left-hand hero visual (id it yourself)
  //   title     html                   — already-escaped heading content
  //   titleId   string                 — so callers can wire inline editing
  //   chips     html | ''              — genre pill, facts line, etc.
  //   stats     [[value, label], …]    — hero stat blocks
  //   actions   html | ''              — top-right buttons (Delete, toggles)
  //   pageClass string | ''             — extra class for per-entity tweaks
  //                                       (Venue uses it for square portraits)
  //   tabs      [{id, label, count, html, active}]
  //
  // Tabs are show/hide over already-rendered panes, never a re-fetch: that is
  // what keeps a half-typed description or a running AI Assist job alive across
  // a tab switch, and it is why tab state is deliberately NOT in the hash.
  // Wire a control that entityShellHtml() only renders for an editor.
  //
  // `actions` is dropped in Playback mode inside the shell — correctly, and
  // deliberately, so a new entity page cannot forget. But every caller then
  // wired its button with a bare getElementById(...).addEventListener, which
  // is null for a listener. All six entity pages threw on load in Playback
  // mode (found 2026-08-23 via the debug drawer, on the Musician page:
  // "null is not an object … 'pn-delete'"). Inside an async render the throw
  // surfaced as an unhandled rejection and nothing showed it, so it had been
  // silently breaking every one of those pages.
  //
  // CONTEXT.md's rule is that gating belongs in the shared helper rather than
  // at ~25 call sites. This is the wiring half of that same rule.
  //
  // Use this ONLY where absence is expected. Elsewhere a missing element is a
  // real bug and should keep throwing.
  function onAdminClick(id, handler) {
    const el = document.getElementById(id)
    if (el) el.addEventListener('click', handler)
  }

  function entityShellHtml(opts) {
    const tabs = (opts.tabs || []).filter(Boolean)
    const activeId = (tabs.find(t => t.active) || tabs[0] || {}).id
    const stats = opts.stats || []
    // Playback mode: no editable title, and no hero actions — every page that
    // passes `actions` passes an admin verb there (Delete artist / venue /
    // genre / collection / musician, + Add peer). Enforced in the shell rather
    // than at each caller so a new entity page cannot forget.
    // `actionsPlayback` renders in BOTH modes, for a verb that is content
    // rather than editing. "+ New collection" moved here 2026-08-22 (Ryan) —
    // making a collection is something a listener does too; deleting one
    // stays admin-only via `actions`.
    const shellEditable = canEditLibrary()
    const titleEditable = opts.titleEditable && shellEditable
    const heroActions   = (shellEditable ? (opts.actions || '') : '') + (opts.actionsPlayback || '')
    return `
      <div class="artist-page${opts.pageClass ? ' ' + opts.pageClass : ''}">

        <div class="pp-hero">
          ${opts.portrait ? `<div class="pp-hero-portrait">${opts.portrait}</div>` : ''}
          <div class="pp-hero-main">
            <h1 class="pp-name${titleEditable ? ' pp-editable' : ''}"
                ${opts.titleId ? `id="${opts.titleId}"` : ''}
                ${titleEditable ? 'title="Click to edit"' : ''}>${opts.title}</h1>
            ${opts.chips ? `<div class="pp-hero-chips">${opts.chips}</div>` : ''}
            ${stats.length ? `<div class="pp-hero-stats">${stats.map(([n, l]) => `
              <div class="pp-stat"><div class="pp-stat-n">${esc(String(n))}</div><div class="pp-stat-l">${esc(l)}</div></div>`).join('')}</div>` : ''}
          </div>
          ${heroActions ? `<div class="pp-hero-actions">${heroActions}</div>` : ''}
        </div>

        ${tabs.length > 1 ? `
          <div class="pp-tabs" role="tablist">
            ${tabs.map(t => `
              <button class="pp-tab${t.id === activeId ? ' active' : ''}" data-pane="${t.id}" role="tab">${esc(t.label)}${
                t.count != null ? `<span class="pp-tab-n">${esc(String(t.count))}</span>` : ''}</button>`).join('')}
          </div>` : ''}

        <div class="pp-panes">
          ${tabs.map(t => `
            <div class="pp-pane${t.id === activeId ? ' active' : ''}" data-pane="${t.id}">${t.html || ''}</div>`).join('')}
        </div>
      </div>`
  }

  // ══ Shared photo gallery ═══════════════════════════════════════════════════
  //
  // The Photos tab for any entity with images — Artist and Venue today
  // (Ryan, 2026-08-07). Parameterised by an API namespace rather than
  // duplicated, so make-primary, delete-promotes-a-survivor, drag-and-drop and
  // partial-upload reporting behave identically wherever photos appear.
  //
  // opts:
  //   mountId    string             — element the gallery renders into
  //   api        object             — must expose listImages / uploadImages /
  //                                   setPrimaryImage / removeImage / imageUrl
  //   entityId   number
  //   images     array              — initial list, avoids a first round-trip
  //   fetchTile  {label, sub, run, disabledNote} | null
  //                                  — optional AUTOMATIC fetch tile. Only the
  //                                    Artist has one (Wikimedia Commons via
  //                                    its MusicBrainz → Wikidata → P18 match);
  //                                    no other dimension has that bridge.
  //   linkTiles  [{label, sub, href, glyph, title}]
  //                                  — tiles that just OPEN A SEARCH in a new
  //                                    tab. Nothing is fetched, nothing lands
  //                                    in the gallery, no licence is inspected
  //                                    — the human looks, saves, and drops the
  //                                    file on the drop zone beside them.
  //                                    Ryan, 2026-09-01: an integrated lookup
  //                                    for four dimensions would need four
  //                                    licence bridges, and three of them do
  //                                    not exist. A link-out is honest about
  //                                    being a search box. See photoSearchTiles.
  //   suggest    {url, tag, action, run} | array of them | null | fn(images) -> same
  //                                  — images that are NOT in the gallery yet,
  //                                    each drawn as a tile with a single action.
  //                                    The recording image modal offers the
  //                                    artist's and the venue's picture this way; `run` copies
  //                                    it in and the gallery redraws.
  //   hideNote   boolean            — omit the line of help text under the grid
  //   onChange   fn(images)         — called after any mutation, so a hero
  //                                   portrait or tab badge can follow along
  //
  // Returns { refresh() } so callers can force a reload after external changes.
  function createPhotoGallery(opts) {
    let images = opts.images || []

    function render() {
      const box = document.getElementById(opts.mountId)
      if (!box) return
      // Playback mode gets the pictures and nothing else — no per-photo
      // actions, no drop zone, no Commons fetch tile.
      const galEditable = canEditLibrary()
      const ft = galEditable
        ? (typeof opts.fetchTile === 'function' ? opts.fetchTile() : opts.fetchTile)
        : null
      // Link-outs are an editing affordance too — a listener has nowhere to put
      // what they'd find, so they follow the same gate as the drop zone.
      const links = galEditable ? (opts.linkTiles || []) : []
      const sgRaw = galEditable ? (typeof opts.suggest === 'function' ? opts.suggest(images) : opts.suggest) : null
      const sgList = [].concat(sgRaw || []).filter(Boolean)
      const sg = sgList.length ? sgList : null
      box.innerHTML = `
        <div class="pp-gal" data-gal="1">
          ${images.map(img => `
            <div class="pp-ph${img.is_primary ? ' is-primary' : ''}" data-img-id="${img.id}"
                 title="${img.credit ? esc(img.credit) : ''}">
              <img src="${opts.api.imageUrl(img.id)}" alt="" loading="lazy">
              ${img.is_primary ? '<span class="pp-ph-tag">Primary</span>' : ''}
              ${img.credit ? `<span class="pp-ph-credit">${esc(img.credit)}</span>` : ''}
              ${galEditable ? `<div class="pp-ph-acts">
                ${img.is_primary ? '' : `<button type="button" class="pp-ph-btn" data-act="primary">Make primary</button>`}
                <button type="button" class="pp-ph-btn" data-act="delete">Delete</button>
              </div>` : ''}
            </div>`).join('')}
          ${sgList.map((g, n) => `
            <div class="pp-ph pp-ph--suggest" data-suggest="${n}">
              <img src="${esc(g.url)}" alt="" loading="lazy">
              <span class="pp-ph-tag">${esc(g.tag)}</span>
              <div class="pp-ph-acts">
                <button type="button" class="pp-ph-btn" data-act="suggest">${esc(g.action)}</button>
              </div>
            </div>`).join('')}
          ${galEditable ? `<div class="pp-drop" data-drop="1">
            <span class="pp-drop-plus">${icon('plus')}</span>
            <span>Drop photos here<br>or click to browse</span>
            <div class="pp-drop-veil">Drop to upload</div>
          </div>` : ''}
          ${ft ? `
            <div class="pp-drop pp-fetch-tile${ft.run ? '' : ' is-disabled'}" data-fetch="1"
                 ${ft.run ? '' : 'aria-disabled="true"'}>
              <span class="pp-drop-plus">☁</span>
              ${ft.run
                ? `<span>${esc(ft.label)}<br><span class="pp-drop-sub">${esc(ft.sub || '')}</span></span>`
                : `<span class="pp-drop-sub">${esc(ft.disabledNote || '')}</span>`}
            </div>` : ''}
          ${links.map(l => `
            <a class="pp-drop pp-link-tile" href="${esc(l.href)}"
               target="_blank" rel="noopener noreferrer" title="${esc(l.title || '')}">
              <span class="pp-drop-plus">${l.glyph || '🔍'}</span>
              <span>${esc(l.label)}<br><span class="pp-drop-sub">${esc(l.sub || '')}</span></span>
            </a>`).join('')}
        </div>
        <input type="file" data-input="1" multiple
               accept="image/png,image/jpeg,image/webp" style="display:none" />
        <div class="pp-fetch-msg" data-msg="1"></div>
        ${opts.hideNote ? '' : `<div class="pp-gal-note">${
          !galEditable
            ? (images.length ? '' : 'No photos yet.')
            : images.length
              ? 'The primary photo is the one shown on this page and on cards.'
              : sg ? '' : 'No photos yet. The primary photo appears on this page and on cards.'
        }</div>`}`

      const input = box.querySelector('[data-input]')
      const msg   = box.querySelector('[data-msg]')
      input?.addEventListener('change', e => { upload(e.target.files); input.value = '' })

      // Delegated, and always re-fetching after a mutation rather than patching
      // the local array: the SERVER owns the one-primary rule and the
      // promote-on-delete rule, so mirroring them here would be a second
      // implementation waiting to disagree with the first.
      box.querySelector('[data-gal]')?.addEventListener('click', async e => {
        const btn = e.target.closest('.pp-ph-btn')
        if (!btn) return
        e.preventDefault()
        if (btn.dataset.act === 'suggest') {
          try { await sgList[Number(btn.closest('.pp-ph').dataset.suggest)].run(); await refresh() } catch (err) { alert('Failed: ' + err.message) }
          return
        }
        const id = Number(btn.closest('.pp-ph').dataset.imgId)
        try {
          if (btn.dataset.act === 'primary') await opts.api.setPrimaryImage(id)
          else {
            if (!confirm('Delete this photo?')) return
            await opts.api.removeImage(id)
          }
          await refresh()
        } catch (err) { alert('Failed: ' + err.message) }
      })

      // ⚠ The drop zone is NOT rendered in Playback mode — a listener has
      // nowhere to put a file — so this element is legitimately absent and the
      // wiring has to be conditional (found 2026-09-01). Unguarded, `drop` was
      // null and this threw inside an async render, which surfaces as an
      // unhandled rejection with nothing on screen: the Venue page's Photos tab
      // has been silently dead in Playback mode since 2026-08-07, and putting
      // this component on three more pages would have quadrupled that.
      //
      // Exactly the failure mode entityShellHtml's `actions` gate and
      // onAdminClick were introduced for — gate the RENDER in the shared
      // helper, and the WIRING has to follow it there too.
      const drop = box.querySelector('[data-drop]')
      if (drop) {
        drop.addEventListener('click', () => input.click())
        // Counter, not a boolean — dragenter/dragleave fire for every child
        // element crossed, so a flag flickers off halfway across the tile.
        let depth = 0
        drop.addEventListener('dragover', e => e.preventDefault())
        drop.addEventListener('dragenter', e => { e.preventDefault(); depth++; drop.classList.add('is-dropping') })
        drop.addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; drop.classList.remove('is-dropping') } })
        drop.addEventListener('drop', e => {
          e.preventDefault(); depth = 0; drop.classList.remove('is-dropping')
          const files = Array.from(e.dataTransfer.files || []).filter(f => f.type.startsWith('image/'))
          if (files.length) upload(files)
        })
      }

      const fetchEl = box.querySelector('[data-fetch]')
      if (fetchEl && ft && ft.run) {
        fetchEl.addEventListener('click', async () => {
          if (fetchEl.classList.contains('is-busy')) return
          fetchEl.classList.add('is-busy')
          msg.className = 'pp-fetch-msg'
          msg.textContent = ft.busyNote || 'Searching…'
          try {
            const res = await ft.run()
            if (!res.ok) {
              msg.textContent = res.note || 'Nothing found.'
              fetchEl.classList.remove('is-busy')
              return
            }
            await refresh()
            // Written AFTER refresh: that redraw would otherwise wipe it.
            const m = document.getElementById(opts.mountId).querySelector('[data-msg]')
            m.className = 'pp-fetch-msg is-ok'
            m.textContent = res.note || 'Added.'
          } catch (err) {
            msg.className = 'pp-fetch-msg is-err'
            msg.textContent = err.message
            fetchEl.classList.remove('is-busy')
          }
        })
      }
    }

    async function upload(files) {
      if (!files || !files.length) return
      try {
        const res = await opts.api.uploadImages(opts.entityId, files)
        await refresh()
        // Partial success is a 200 with an `errors` list — four of five photos
        // landing must not read as failure, but the rejected one has to say why.
        if (res.errors && res.errors.length) alert('Some files were skipped:\n' + res.errors.join('\n'))
      } catch (err) { alert('Upload failed: ' + err.message) }
    }

    async function refresh() {
      images = await opts.api.listImages(opts.entityId)
      render()
      if (opts.onChange) opts.onChange(images)
    }

    render()
    if (opts.onChange) opts.onChange(images)
    return { refresh, get images() { return images } }
  }

  // ══ The two photo-search link-outs every photographed entity gets ══════════
  //
  // Standardised across Artist, Musician, Venue and Event (Ryan, 2026-09-01).
  // Both open a search and stop there — no fetch, no automatic import.
  //
  // COMMONS, not "google it plus the words creative commons". Wikimedia Commons
  // accepts freely-licensed files ONLY, so every result is safe to redistribute
  // once peer sharing exposes the library; a web search for the phrase "creative
  // commons license" returns pages that merely CONTAIN those words, which is not
  // the same claim and is exactly the trap CONTEXT.md records for English
  // Wikipedia's local fair-use uploads. Special:MediaSearch is Commons' own
  // image search, so the licence guarantee comes from the destination rather
  // than from a query string.
  //
  // Google Images has no licence guarantee at all and is not pretending to —
  // it is the long tail Commons does not cover, for the acts and halls nobody
  // has photographed freely. `qualifier` narrows a bare name that would
  // otherwise be ambiguous ("Fillmore" alone is a district).
  function photoSearchTiles(name, qualifier) {
    const q = [name, qualifier].filter(Boolean).join(' ')
    return [
      {
        label: 'Find a free photo', sub: 'Wikimedia Commons', glyph: '☁',
        href: 'https://commons.wikimedia.org/w/index.php?search=' +
              encodeURIComponent(q) + '&title=Special:MediaSearch&type=image',
        title: 'Search Wikimedia Commons. Freely licensed images only. ' +
               'Opens in a new tab; save one and drop it here.',
      },
      {
        label: 'Search the web', sub: 'Google Images', glyph: '🔍',
        href: 'https://www.google.com/search?tbm=isch&q=' + encodeURIComponent(q),
        title: 'Open a Google Images search in a new tab. No licence is ' +
               'checked. Mind what you keep.',
      },
    ]
  }

  // Round entity portrait for a hero. Falls back to INITIALS rather than a
  // silhouette icon: with photos on a small minority of entities, the no-photo
  // state is the normal appearance and should look intentional.
  function heroPortraitHtml(name, imageUrl, ringColor) {
    const ring = esc(ringColor || 'var(--bd-1)')
    if (imageUrl) {
      return `<img class="pp-portrait-img" style="--ring:${ring}" src="${imageUrl}" alt="${esc(name)}">`
    }
    const initials = String(name || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    return `<div class="pp-portrait-blank" style="--ring:${ring}">${esc(initials)}</div>`
  }

  // ══ Recording image modal ═════════════════════════════════════════════════
  //
  // Opened from the square in the View Recording header (Ryan, 2026-10-04).
  // Top: the current image, large, for a close look; click it to toggle
  // between fit and actual size. Below, for an editor: the same photo gallery
  // the Artist and Venue pages use (upload, make primary, delete, the Commons
  // and Google link-outs), plus the artist's picture as a one-click choice --
  // choosing it COPIES the file into the recording, so the recording keeps its
  // image if the artist's is later replaced. A listener, or a peer, gets the
  // zoomed image and nothing else.
  //
  // opts: { recordingId, rec, artistImageId, venueImageId, artistName, linkQualifier,
  //         onChange }   -- `rec` is the page's own object and is updated here.
  function openRecordingImageModal(opts) {
    const { recordingId, rec } = opts
    // Peers receive no `images` array (the share door sends image_url only), so
    // its absence is what marks a context with nothing to edit.
    const editable = canEditLibrary() && Array.isArray(rec.images)
    const wrap = document.createElement('div')
    wrap.className = 'modal-overlay'
    wrap.innerHTML = `
      <div class="modal-card img-modal" role="dialog" aria-modal="true">
        <div class="img-modal-zoom" id="img-modal-zoom"></div>
        ${editable ? '<div class="img-modal-gal"><div id="img-modal-gal"></div></div>' : ''}
        <div class="modal-footer">
          <button class="btn btn-sm btn-ghost" id="img-modal-close">Close</button>
        </div>
      </div>`
    document.body.appendChild(wrap)

    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey) }
    const onKey = e => { if (e.key === 'Escape') close() }
    document.addEventListener('keydown', onKey)
    wrap.querySelector('#img-modal-close').addEventListener('click', close)
    wrap.addEventListener('click', e => { if (e.target === wrap) close() })

    const zoomBox = wrap.querySelector('#img-modal-zoom')
    function renderZoom() {
      zoomBox.hidden = !rec.image_url
      zoomBox.innerHTML = rec.image_url
        ? `<img src="${esc(rec.image_url)}" alt="">` : ''
      zoomBox.querySelector('img')?.addEventListener('click', e => e.target.classList.toggle('is-actual'))
    }
    renderZoom()
    if (!editable) return

    // The recording's image after any change is the SERVER's answer (own
    // image, else the artist's), not a second copy of that rule in the client.
    let firstCall = true
    async function sync(imgs) {
      if (firstCall) { firstCall = false; return }   // the gallery reports its initial state
      rec.images = imgs
      try { rec.image_url = (await API.recordings.get(recordingId)).image_url } catch (_) {}
      renderZoom()
      if (opts.onChange) opts.onChange()
    }

    createPhotoGallery({
      mountId: 'img-modal-gal', api: API.recordings, entityId: recordingId,
      images: rec.images, hideNote: true,
      linkTiles: photoSearchTiles(opts.artistName, opts.linkQualifier),
      // Offered until the recording's primary already IS that picture.
      suggest: imgs => {
        const primary = (imgs || []).find(i => i.is_primary)
        const out = []
        if (opts.artistImageId && primary?.origin !== 'artist') out.push({
          url: API.artists.imageUrl(opts.artistImageId),
          tag: 'Artist', action: 'Use as image',
          run: () => API.recordings.useArtistImage(recordingId),
        })
        if (opts.venueImageId && primary?.origin !== 'venue') out.push({
          url: API.venues.imageUrl(opts.venueImageId),
          tag: 'Venue', action: 'Use as image',
          run: () => API.recordings.useVenueImage(recordingId),
        })
        return out
      },
      onChange: sync,
    })
  }

  // ══ Shared "create entity" form ════════════════════════════════════════════
  //
  // One simple form per dimension (Ryan, 2026-08-07). The + buttons previously
  // did two different wrong things: Venues navigated to the ADMIN LIST — a
  // view-and-edit screen inconsistent with everything else and not a create
  // flow at all — while Artists and Musicians used a bare window.prompt().
  //
  // Built on the entity shell so a create form looks like the page it will
  // become, and deliberately minimal: name plus whatever else is genuinely
  // required. Everything else is editable in place afterwards, which is the
  // established pattern for every object in this app.
  //
  // Built out 2026-09-01 (Ryan, "build out the Add New pages a bit more"), but
  // still deliberately short of the record's own page. The line is: a field
  // belongs here if you would otherwise have to type it TWICE (a venue's city,
  // an event's dates — things you already know while creating and would
  // immediately go back in to add), or if leaving it blank creates a record
  // that is hard to find again. Everything that is genuinely a later decision
  // — an artist's members and genre, an event's linked venue — stays on the
  // record, edited in place, which is this app's established pattern for every
  // object. A create form that mirrors the whole page is a second edit surface,
  // and the two-edit-surfaces mistake is exactly what the old Venues admin
  // screen was.
  //
  // opts: { title, backHash, fields: [...], intro, note,
  //         onSave(values) -> hash to navigate to, invalidate: dimName }
  //
  // field: { id, label, placeholder, required, multiline, hint, type }
  //   placeholder — normally ABSENT. Every field here carries a visible label,
  //           so a placeholder can only repeat it or show an example, and an
  //           example value in a City box is one more thing to read and one
  //           more thing to mistake for a filled-in value. All five forms
  //           carried one per field until 2026-09-18 (Ryan); they are gone,
  //           and a new one needs a reason the label cannot cover. A field
  //           with none gets no attribute at all, not an empty one.
  //   hint  — a line under the input. Use it for a FORMAT the user cannot
  //           guess (partial dates) or a consequence they cannot see, never to
  //           restate the label.
  //   type  — 'color' pairs a native swatch with the text box, so a hex can be
  //           typed or picked. Genre is the only user of it; genre colour is
  //           the most complete visual signal the library owns (CONTEXT.md),
  //           so it is worth setting at creation rather than later.
  function renderCreateForm(opts) {
    setNavCurrent(opts.title)
    const fields = opts.fields
    // No placeholder means no attribute. `placeholder=""` renders the same but
    // leaves every input in the markup looking like it lost its text.
    const ph = (f, fallback = '') => {
      const text = f.placeholder || fallback
      return text ? ` placeholder="${esc(text)}"` : ''
    }
    setMainHTML(entityShellHtml({
      navBack: opts.backHash ? { label: opts.backLabel || 'Back', hash: opts.backHash } : null,
      title: esc(opts.title),
      tabs: [{
        id: 'form', label: 'New',
        html: `
          <div class="create-form">
            ${opts.intro ? `<p class="create-form-intro">${esc(opts.intro)}</p>` : ''}
            ${fields.map(f => `
              <div class="ingest-field">
                <label for="cf-${f.id}">${esc(f.label)}${f.required ? '' : ' <span class="cf-opt">(optional)</span>'}</label>
                ${f.multiline
                  ? `<textarea id="cf-${f.id}"${ph(f)}></textarea>`
                  : f.type === 'color'
                    ? `<span class="cf-color">
                         <input type="color" id="cf-${f.id}-swatch" value="${esc(f.value || '#7a8b99')}" />
                         <input type="text" id="cf-${f.id}"${ph(f, '#rrggbb')}
                                value="${esc(f.value || '')}" autocomplete="off" spellcheck="false" />
                       </span>`
                    : `<input type="text" id="cf-${f.id}"${ph(f)} autocomplete="off" />`}
                ${f.hint ? `<div class="cf-hint">${esc(f.hint)}</div>` : ''}
              </div>`).join('')}
            ${opts.note ? `<p class="create-form-note">${esc(opts.note)}</p>` : ''}
            <div class="create-form-actions">
              <button class="btn btn-primary btn-sm" id="cf-save">Create</button>
              <button class="btn btn-ghost btn-sm" id="cf-cancel">Cancel</button>
              <span class="pp-sec-msg" id="cf-msg"></span>
            </div>
          </div>`,
      }],
    }))
    wireEntityShell(mainContent, opts.backHash ? { hash: opts.backHash } : null)

    const val = id => document.getElementById('cf-' + id).value.trim()
    const msgEl = document.getElementById('cf-msg')
    const first = document.getElementById('cf-' + fields[0].id)
    first?.focus()

    async function save() {
      const values = {}
      for (const f of fields) {
        values[f.id] = val(f.id) || null
        if (f.required && !values[f.id]) {
          msgEl.className = 'pp-sec-msg is-err'
          msgEl.textContent = `${f.label} is required`
          document.getElementById('cf-' + f.id).focus()
          return
        }
      }
      const btn = document.getElementById('cf-save')
      btn.disabled = true; btn.textContent = 'Creating…'
      msgEl.className = 'pp-sec-msg'; msgEl.textContent = ''
      try {
        const hash = await opts.onSave(values)
        if (opts.invalidate) invalidateDims(opts.invalidate)
        window.location.hash = hash
      } catch (e) {
        msgEl.className = 'pp-sec-msg is-err'
        msgEl.textContent = e.message
        btn.disabled = false; btn.textContent = 'Create'
      }
    }

    // Colour fields: the swatch and the hex box are two views of one value, so
    // each writes the other. The TEXT box stays the source of truth for save()
    // — a native colour input has no empty state, so reading it would make
    // "no colour" unrepresentable, and NULL colour is a supported state that
    // renders neutral grey.
    fields.filter(f => f.type === 'color').forEach(f => {
      const box = document.getElementById('cf-' + f.id)
      const sw  = document.getElementById('cf-' + f.id + '-swatch')
      if (!box || !sw) return
      sw.addEventListener('input', () => { box.value = sw.value })
      box.addEventListener('input', () => {
        if (/^#[0-9a-fA-F]{6}$/.test(box.value.trim())) sw.value = box.value.trim()
      })
    })

    document.getElementById('cf-save').addEventListener('click', save)
    document.getElementById('cf-cancel').addEventListener('click', () => {
      window.location.hash = opts.backHash || '#/'
    })
    // Enter submits from any single-line field — a two-field form should not
    // require reaching for the mouse.
    mainContent.querySelectorAll('.create-form input').forEach(el =>
      el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); save() } }))
  }

  // ══ Peers — inbound sharing management ═════════════════════════════════════
  //
  // A frontend for api/peers.py, which has existed since 2026-07-16 and was
  // curl-only until now. NOTHING here is new server surface — every call maps
  // to an endpoint that already works and is already tested.
  //
  // Sharing is COLLECTION-ONLY (Ryan, 2026-08-08). Artist-level, whole-library
  // and per-recording grants were considered and dropped: collections are
  // already the arbitrary-set-of-recordings primitive, and bulk tools for
  // filling a collection make "share everything" a collection you build once
  // rather than a second grant model to maintain. That decision is why this
  // page needs no migration at all.
  // Existing invites for one peer.
  //
  // Added 2026-08-25 because the ONLY control on this page was "Revoke access",
  // and that means something else entirely (Ryan): revoking kills the person —
  // every device they hold stops working and their library goes dark.
  // Cancelling an invite stops ONE unused code from working and touches nobody
  // who has already joined. Two very different blast radii, so they get
  // different words and sit in different blocks.
  //
  // Three states, three treatments:
  //   Unused  — a live key to this library. The only one worth cancelling, and
  //             the only one that asks for confirmation.
  //   Expired — already dead. "Clear" is tidying, so no confirm.
  //   Used    — history, and NOT deletable: the device it produced still works,
  //             and removing the row would erase the record of a live access.
  //             Killing that access is a device revocation, a third thing again.
  function peerInvitesHtml(invites) {
    if (!invites || !invites.length) return ''
    const rows = invites.map(i => {
      const made = `Created ${esc(fmtDateAdded(i.created_at))}`
      if (i.status === 'used') {
        return `<div class="peer-inv">
          <span class="peer-inv-state peer-inv-state--used">Used</span>
          <span class="peer-inv-when truncate">${made}${
            i.consumed_at ? ` · joined ${esc(fmtDateAdded(i.consumed_at))}` : ''}</span>
        </div>`
      }
      const live = i.status === 'pending'
      return `<div class="peer-inv">
        <span class="peer-inv-state${live ? ' peer-inv-state--live' : ''}">${
          live ? 'Unused' : 'Expired'}</span>
        <span class="peer-inv-when truncate">${made} · ${
          live ? 'expires' : 'expired'} ${esc(fmtDateAdded(i.expires_at))}</span>
        <button class="btn btn-ghost btn-xs peer-inv-del" data-invite-id="${i.id}"${
          live ? ' data-live="1"' : ''}>${live ? 'Cancel' : 'Clear'}</button>
      </div>`
    }).join('')
    return `<div class="peer-inv-list"><div class="peer-inv-head">Previous invites</div>${rows}</div>`
  }

  async function renderPeersPage(preSelectId = null) {
    setActiveNav('peers')
    setNavCurrent('Sharing')
    setLoading()

    let peers = [], collections = []
    try {
      [peers, collections] = await Promise.all([
        API.peers.list(), API.collections.list(),
      ])
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Could not load peers</div><div class="empty-sub">${esc(e.message)}</div></div>`)
      return
    }

    let activeId = preSelectId || (peers[0] && peers[0].id) || null

    setMainHTML(entityShellHtml({
      title: 'Sharing',
      stats: [
        [peers.filter(p => p.is_active).length, 'Peers'],
        [collections.length, collections.length === 1 ? 'Collection' : 'Collections'],
      ],
      actions: `<button class="btn btn-ghost btn-sm" id="peer-new">+ Add peer</button>`,
      tabs: [{
        id: 'peers', label: 'Peers',
        html: `
          <div class="peer-share-address">
            <label class="set-label" for="peer-share-url">Public address</label>
            <div class="set-actions">
              <input class="set-input" type="text" id="peer-share-url"
                     placeholder="https://share.example.com" autocomplete="off">
              <button class="btn btn-primary btn-sm" id="peer-share-url-save">Save</button>
              <span class="set-flash" id="peer-share-url-flash"></span>
            </div>
            <p class="set-hint" id="peer-share-url-hint">The address peer invites point at (your Cloudflare Tunnel hostname, or whatever's fronting this instance). Without this, invites show a bare code instead of a paste-able link.</p>
          </div>
          <div class="peer-layout">
            <div class="peer-list" id="peer-list"></div>
            <div class="peer-detail" id="peer-detail"></div>
          </div>`,
      }],
    }))
    wireEntityShell(mainContent, null)

    // ── Public address (SHARE_BASE_URL) — commit-on-blur, same shape as Settings ──
    ;(async () => {
      const input = document.getElementById('peer-share-url')
      const hint = document.getElementById('peer-share-url-hint')
      const saveBtn = document.getElementById('peer-share-url-save')
      if (!input) return
      let current = ''
      try {
        const s = await API.peers.getShareAddress()
        current = s.share_base_url || ''
        input.value = current
        if (s.from_env) {
          input.disabled = true
          saveBtn.disabled = true
          hint.textContent = 'Set via the SHARE_BASE_URL environment variable, which always wins. Unset it there to edit this from here.'
        }
      } catch (e) { /* non-fatal — field just stays empty */ }
      const commit = async () => {
        const v = input.value.trim()
        if (v === current) return
        try {
          const s = await API.peers.setShareAddress(v)
          current = s.share_base_url || ''
          input.value = current
          _settingsSaved(document.getElementById('peer-share-url-flash'))
        } catch (e) {
          input.value = current
          _settingsSaved(document.getElementById('peer-share-url-flash'), e.message)
        }
      }
      input.addEventListener('blur', commit)
      input.addEventListener('keydown', e => { if (e.key === 'Enter') input.blur() })
      saveBtn?.addEventListener('click', commit)
    })()

    function renderList() {
      const el = document.getElementById('peer-list')
      if (!peers.length) {
        el.innerHTML = `<div class="peer-empty">No peers yet.<br>Add one to start sharing.</div>`
        return
      }
      el.innerHTML = peers.map(p => `
        <div class="peer-row${p.id === activeId ? ' active' : ''}${p.is_active ? '' : ' is-revoked'}" data-id="${p.id}">
          <div class="peer-row-name truncate">${esc(p.name)}</div>
          <div class="peer-row-meta">${
            !p.is_active ? 'Revoked'
            : p.has_joined ? `${p.grant_count} collection${p.grant_count === 1 ? '' : 's'}`
            : p.pending_invites ? 'Invited, not joined'
            : 'Not invited'
          }</div>
        </div>`).join('')
      el.querySelectorAll('.peer-row').forEach(row =>
        row.addEventListener('click', () => {
          activeId = Number(row.dataset.id)
          renderList(); renderDetail()
        }))
    }

    async function renderDetail() {
      const el = document.getElementById('peer-detail')
      if (activeId == null) {
        el.innerHTML = `<div class="peer-empty">Select a peer, or add one.</div>`
        return
      }
      el.innerHTML = `<div class="peer-empty">Loading…</div>`
      let p
      try { p = await API.peers.get(activeId) }
      catch (e) { el.innerHTML = `<div class="peer-empty">Failed to load: ${esc(e.message)}</div>`; return }

      const granted = new Set(p.grants.map(g => g.collection_id))
      el.innerHTML = `
        <div class="pp-sec-row">
          <h2 class="pp-block-title" id="peer-name" title="Click to edit">${esc(p.name)}</h2>
          ${p.is_active
            ? `<button class="btn btn-ghost btn-xs" id="peer-revoke" style="margin-left:auto; color:var(--red)">Revoke access</button>`
            : `<span style="margin-left:auto; display:flex; align-items:center; gap:8px">
                 <span class="peer-badge peer-badge--revoked">Revoked</span>
                 <button class="btn btn-ghost btn-xs" id="peer-unrevoke">Restore access</button>
               </span>`}
        </div>
        <div class="pp-desc pp-editable ${p.contact_note ? '' : 'pp-empty'}" id="peer-note" title="Click to edit">${
          p.contact_note ? esc(p.contact_note) : 'Add a note: who is this?'}</div>

        <div class="pp-block">
          <h2 class="pp-block-title">Access</h2>
          <div class="pp-block-hint">Sharing gives this person your whole library, read-only. Anything you add later appears for them automatically; anything you move out to Workshop or Backlog disappears.</div>
          ${(() => {
            // MVP is share-everything (Ryan, 2026-08-24). Per-collection
            // checkboxes are DELIBERATELY not rendered: offering them invites
            // exactly the partial grants that were deferred, and every partial
            // grant needs every filtered endpoint to be exactly right.
            //
            // Underneath this is still an ordinary CollectionGrant against the
            // Full Library system collection, so nothing about the grant model
            // changed and selective sharing can return as an advanced option
            // without a migration. The existing change handler is reused as-is
            // — it keys on data-col-id and does not care that there is now one
            // box instead of six.
            const full = collections.find(c => c.is_system)
            if (!full) {
              return `<div class="peer-empty">No Full Library collection in this database. Run <span class="join-hash">scripts/migrate_add_system_collections.py</span>.</div>`
            }
            const on = granted.has(full.id)
            return `
            <div class="peer-grants">
              <label class="peer-grant peer-grant--system${on ? ' is-on' : ''}">
                <input type="checkbox" data-col-id="${full.id}" ${on ? 'checked' : ''} ${p.is_active ? '' : 'disabled'}>
                <span class="peer-grant-name truncate">Share my library</span>
                <span class="peer-grant-count">${full.recording_count}</span>
                <span class="peer-grant-note">They see everything on the shelf (Browse, Search and your collections) but cannot change anything.</span>
              </label>
            </div>`
          })()}
        </div>

        <div class="pp-block">
          <h2 class="pp-block-title">Invite</h2>
          <div class="pp-block-hint">Generates a one-time code. It is shown once and stored only as a hash. If it's lost, mint a new one.</div>
          <div class="ai-assist-cta">
            <button class="btn btn-primary btn-sm" id="peer-invite" ${p.is_active ? '' : 'disabled'}>
              ${p.has_joined ? 'New invite' : 'Create invite'}</button>
            <div class="ai-assist-hint">${
              p.has_joined ? `Joined · ${p.devices.length} device${p.devices.length === 1 ? '' : 's'}`
              : p.pending_invites ? `${p.pending_invites} invite pending`
              : 'Not yet invited'}</div>
          </div>
          <div id="peer-invite-out"></div>
          ${peerInvitesHtml(p.invites)}
        </div>

        <div class="pp-block">
          <h2 class="pp-block-title">Activity</h2>
          <div class="pp-block-hint">${p.last_seen_at ? 'Last seen ' + esc(fmtDateAdded(p.last_seen_at)) : 'Never connected.'}</div>
          <div id="peer-activity"></div>
        </div>`

      makeInlineEditable(document.getElementById('peer-name'), {
        get: () => p.name,
        onSave: async v => {
          v = v.trim(); if (!v || v === p.name) return
          await API.peers.update(p.id, { name: v })
          p.name = v
          const row = peers.find(x => x.id === p.id); if (row) row.name = v
          renderList()
        },
      })
      makeInlineEditable(document.getElementById('peer-note'), {
        multiline: true, placeholder: 'Add a note: who is this?',
        get: () => p.contact_note || '',
        onSave: async v => { v = v.trim(); p.contact_note = v; await API.peers.update(p.id, { contact_note: v || null }) },
      })

      // Grants toggle immediately — a checkbox that needs a Save button is a
      // checkbox that will be left unsaved.
      el.querySelectorAll('.peer-grants input').forEach(cb =>
        cb.addEventListener('change', async () => {
          const cid = Number(cb.dataset.colId)
          cb.disabled = true
          try {
            if (cb.checked) await API.peers.addGrants(p.id, [cid])
            else await API.peers.revokeGrant(p.id, cid)
            cb.closest('.peer-grant').classList.toggle('is-on', cb.checked)
            const row = peers.find(x => x.id === p.id)
            if (row) { row.grant_count += cb.checked ? 1 : -1; renderList() }
          } catch (e) {
            cb.checked = !cb.checked
            alert('Failed: ' + e.message)
          } finally { cb.disabled = false }
        }))

      document.getElementById('peer-invite')?.addEventListener('click', async () => {
        const out = document.getElementById('peer-invite-out')
        out.innerHTML = `<div class="peer-empty">Creating…</div>`
        try {
          const inv = await API.peers.mintInvite(p.id)
          // Shown ONCE. The server stores only a SHA-256 hash, so there is no
          // "show it again" — say so plainly rather than letting someone
          // navigate away assuming they can come back for it.
          out.innerHTML = `
            <div class="peer-invite-box">
              <div class="peer-invite-label">${inv.invite ? 'Send this to your peer' : 'Invite code'}</div>
              <code class="peer-invite-code" id="peer-invite-code">${esc(inv.invite || inv.code)}</code>
              <div class="peer-invite-actions">
                <button class="btn btn-ghost btn-xs" id="peer-invite-copy">Copy</button>
                <span class="peer-invite-note">Shown once · expires ${esc(fmtDateAdded(inv.expires_at))}</span>
              </div>
              ${inv.base_url_set ? '' : `
                <div class="peer-invite-warn">No public address set. Fill in the field above and mint again to get a single paste-able link instead of a bare code.</div>`}
            </div>`
          document.getElementById('peer-invite-copy').addEventListener('click', () => {
            navigator.clipboard?.writeText(inv.invite || inv.code)
            document.getElementById('peer-invite-copy').textContent = 'Copied'
          })
          const row = peers.find(x => x.id === p.id)
          if (row) { row.pending_invites += 1; renderList() }
        } catch (e) { out.innerHTML = `<div class="peer-empty" style="color:var(--red)">${esc(e.message)}</div>` }
      })

      el.querySelectorAll('.peer-inv-del').forEach(btn =>
        btn.addEventListener('click', async () => {
          const live = btn.dataset.live === '1'
          if (live && !confirm(
                'Cancel this unused invite?\n\n' +
                'The code stops working immediately. Anyone who has already ' +
                'joined your library is unaffected. This is not the same as ' +
                'revoking access.')) return
          btn.disabled = true
          try {
            await API.peers.deleteInvite(p.id, Number(btn.dataset.inviteId))
            peers = await API.peers.list()
            renderList(); renderDetail()
          } catch (e) { btn.disabled = false; alert('Failed: ' + e.message) }
        }))

      document.getElementById('peer-revoke')?.addEventListener('click', async () => {
        if (!confirm(`Revoke all access for "${p.name}"?\n\nThis kills every grant and every device token at once. You can restore it from this page afterward if you change your mind.`)) return
        try {
          await API.peers.revoke(p.id)
          peers = await API.peers.list()
          renderList(); renderDetail()
        } catch (e) { alert('Failed: ' + e.message) }
      })

      document.getElementById('peer-unrevoke')?.addEventListener('click', async () => {
        try {
          await API.peers.unrevoke(p.id)
          peers = await API.peers.list()
          renderList(); renderDetail()
        } catch (e) { alert('Failed: ' + e.message) }
      })

      try {
        const acts = await API.peers.activity(p.id)
        const box = document.getElementById('peer-activity')
        if (box) {
          // A studio row has no venue/date, so it needs recIdentity's own
          // title/artist/year shape rather than the live [artist, date] line.
          const activityLine = a => {
            if (a.kind === 'studio') {
              const id = recIdentity({ kind: 'studio', title: a.title, artist: a.artist,
                                        start_year: a.date ? parseInt(a.date, 10) : null })
              return [id.lead, id.sub, id.dateText].filter(Boolean).join(' · ')
            }
            return [a.artist, a.date].filter(Boolean).join(' · ')
          }
          box.innerHTML = acts.length
            ? `<div>${acts.slice(0, 12).map(a => `
                <div class="peer-act">
                  <span class="truncate">${esc(activityLine(a) || a.track_title || 'track')}</span>
                  <span class="peer-act-when">${esc(fmtDateAdded(a.occurred_at))}</span>
                </div>`).join('')}</div>`
            : `<div class="peer-empty">Nothing streamed yet.</div>`
        }
      } catch (_) { /* activity is nice-to-have, never blocks the page */ }
    }

    onAdminClick('peer-new', async () => {
      const name = prompt('Peer name (your own label for this person):')
      if (!name || !name.trim()) return
      try {
        const created = await API.peers.create({ name: name.trim() })
        peers = await API.peers.list()
        activeId = created.id
        renderList(); renderDetail()
      } catch (e) { alert('Failed: ' + e.message) }
    })

    renderList()
    renderDetail()
  }

  const renderVenueForm = () => renderCreateForm({
    title: 'New venue', backHash: '#/venues', backLabel: 'Venues', invalidate: 'venues',
    intro: 'A hall, club or field where shows happened. Photos and anything '
         + 'else go on the venue’s own page once it exists.',
    fields: [
      { id: 'name',    label: 'Venue name', required: true },
      { id: 'city',    label: 'City' },
      { id: 'state',   label: 'State / Region' },
      { id: 'country', label: 'Country' },
      // City/state/country earn their place here rather than on the page alone:
      // half a dozen halls in this library share a name, and a venue created
      // bare is one you cannot tell apart from the other Fillmore next week.
      { id: 'bio', label: 'Notes', multiline: true },
    ],
    onSave: async v => `#/venue/${(await API.venues.create(v)).id}`,
  })

  const renderArtistForm = () => renderCreateForm({
    title: 'New artist', backHash: '#/artists', backLabel: 'Artists',
    invalidate: 'artists',
    intro: 'The act that took the stage. The billing on the poster, not an '
         + 'individual musician. Add people to it as Members afterwards.',
    fields: [
      { id: 'name', label: 'Artist name', required: true },
      { id: 'bio',  label: 'Description', multiline: true },
    ],
    // Genre and members stay off this form deliberately: genre is a picker over
    // a fixed vocabulary and members are a roster with tenure dates, and both
    // are already better on the page than they could be in a text box. A
    // MusicBrainz lookup also runs on create, so several of the facts a longer
    // form would ask for arrive on their own.
    note: 'Members, genre and photos are edited on the artist’s page. A '
        + 'MusicBrainz lookup runs automatically when the artist is created.',
    onSave: async v => `#/artist/${(await API.artists.create(v)).id}`,
  })

  const renderMusicianForm = () => renderCreateForm({
    title: 'New musician', backHash: '#/musicians', backLabel: 'Musicians', invalidate: 'musicians',
    intro: 'An individual musician. Link them to the acts they play in from '
         + 'their page, or from the act’s Members list.',
    fields: [
      { id: 'name', label: 'Musician name', required: true },
      // Sort name removed 2026-09-01 (Ryan). It asked, at the moment of
      // creation, for a clerical restatement of the name that had just been
      // typed — and the field is NULL for all 179 existing rows anyway, so
      // nothing was consistent with it. Ordering already reads
      // COALESCE(sort_name, name) on both the server and the client, so a blank
      // one costs nothing; the column stays, and scripts/backfill_sort_names.py
      // is still the way to populate it in bulk if that day comes.
      { id: 'bio', label: 'Bio', multiline: true },
    ],
    onSave: async v => `#/musician/${(await API.musicians.create(v)).id}`,
  })

  const renderGenreForm = () => renderCreateForm({
    title: 'New genre', backHash: '#/genres', backLabel: 'Genres', invalidate: 'genres',
    intro: 'Genres are a fixed vocabulary. Nothing in the app creates one '
         + 'implicitly, so this form is the only door in.',
    fields: [
      { id: 'name',  label: 'Genre name', required: true },
      // Colour is set here rather than later because it is not decoration: it
      // tints every card, row and tile belonging to this genre's artists,
      // and CONTEXT.md records it as the most complete visual signal the
      // library owns — 566 of 580 recordings carry one, far more than photos.
      { id: 'color', label: 'Colour', type: 'color', value: '#7a8b99',
        hint: 'Tints every recording card and browse row for artists in this genre.' },
      { id: 'description', label: 'Description', multiline: true },
    ],
    onSave: async v => `#/genre/${(await API.genres.create(v)).id}`,
  })

  const renderEventForm = () => renderCreateForm({
    title: 'New event', backHash: '#/events', backLabel: 'Events', invalidate: 'events',
    intro: 'A named container for several shows (a festival, or a tour run). '
         + 'Attach performances to it from the recordings themselves.',
    fields: [
      { id: 'name',  label: 'Event name', required: true },
      // ONE box per end, not three. A partial date is the norm for this corpus
      // — a tour run often has only a year — and six number inputs would make
      // the common case six times the work. See _parsePartialDate: anything it
      // cannot read is REJECTED rather than guessed.
      { id: 'start_date', label: 'Start date',
        hint: 'Year alone is fine: 2009, 2009-06 or 2009-06-11.' },
      { id: 'end_date',   label: 'End date' },
      { id: 'city',    label: 'City' },
      { id: 'state',   label: 'State / Region' },
      { id: 'country', label: 'Country' },
      { id: 'notes',   label: 'Notes', multiline: true },
    ],
    note: 'An anchor venue can be linked on the event’s page. Shows inside an '
        + 'event can override its location with their own.',
    onSave: async v => {
      const start = _parsePartialDate(v.start_date)
      const end   = _parsePartialDate(v.end_date)
      // renderCreateForm surfaces a thrown message beside the buttons, which is
      // where the eye already is — better than an alert over a form.
      if (start === undefined) throw new Error('Start date: use 2009, 2009-06 or 2009-06-11')
      if (end   === undefined) throw new Error('End date: use 2009, 2009-06 or 2009-06-11')
      const created = await API.events.create({
        name: v.name, city: v.city, state: v.state, country: v.country, notes: v.notes,
        start_year: start[0], start_month: start[1], start_day: start[2],
        end_year:   end[0],   end_month:   end[1],   end_day:   end[2],
      })
      return `#/event/${created.id}`
    },
  })

  // Wires the shell's tab strip and back link. `root` is normally mainContent.
  function wireEntityShell(root, navBack) {
    root.querySelectorAll('.pp-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        root.querySelectorAll('.pp-tab').forEach(t => t.classList.remove('active'))
        root.querySelectorAll('.pp-pane').forEach(p => p.classList.remove('active'))
        btn.classList.add('active')
        root.querySelector(`.pp-pane[data-pane="${btn.dataset.pane}"]`)?.classList.add('active')
      })
    })
    // The #pp-back-row link was removed 2026-08-22 — the App Header's back
    // arrow covers every view. `navBack` is still threaded through because the
    // new-entity forms use it as their post-save/cancel destination.
  }

  // A recordings pane using the flat catalog table — the shape Venue, Musician,
  // Genre and Artist all want. `showArtist` differs per page: an Artist
  // page needn't repeat the act's name on every row, a Venue page must.
  function recordingsPaneHtml(rows, { showArtist = false, mountId = 'rec-table-entity', empty = 'No recordings yet' } = {}) {
    if (!rows.length) {
      return `<div class="empty-state" style="min-height:180px"><div class="empty-title">${esc(empty)}</div></div>`
    }
    return recTableHeadHtml(showArtist)
         + `<div class="rec-table" id="${mountId}">${rows.map(r => flatRowHtml(r, showArtist)).join('')}</div>`
  }

  async function renderCollectionsIndex() {
    setActiveNav('collections'); setActiveArtist(null); setLoading()
    setNavCurrent('Collections')
    let cols = []
    try { cols = (await API.collections.list()).filter(isCuratedCollection) } catch (_) {}

    // Same tile component the Browse dashboard uses, so a collection looks
    // like itself wherever you meet it (Ryan, 2026-08-07 — "playbill style
    // across the board"). A collection has no date or venue, so a literal
    // handbill would be mostly empty; the tile is the card language minus the
    // fields collections don't have.
    setMainHTML(entityShellHtml({
      title: 'Collections',
      stats: [[cols.length, cols.length === 1 ? 'Collection' : 'Collections']],
      actionsPlayback: `<button class="btn btn-ghost btn-sm" id="btn-new-collection">+ New collection</button>`,
      tabs: [{
        id: 'collections', label: 'Collections',
        html: cols.length
          ? `<div class="lib-module-grid lib-module-grid--tiles">${cols.map(colTileHtml).join('')}</div>`
          : `<div class="empty-state" style="min-height:160px"><div class="empty-title">No collections yet</div><div class="empty-sub">Right-click any recording to start one.</div></div>`,
      }],
    }))
    wireEntityShell(mainContent, null)

    document.getElementById('btn-new-collection').addEventListener('click', async () => {
      const name = prompt('New collection name:')
      if (!name || !name.trim()) return
      try {
        const c = await API.collections.create({ name: name.trim() })
        _dimCache.collections = null
        if (state.expandedDims.has('collections')) _renderDimRecords('collections')
        window.location.hash = `#/collection/${c.id}`
      } catch (e) { alert('Failed: ' + e.message) }
    })
  }

  // New collection (create only — editing happens in place on the collection page).
  async function renderCollectionForm() {
    setActiveNav('collections'); setActiveArtist(null)
    setNavCurrent('New Collection')
    setMainHTML(`
      <div class="artist-header"><h1>New collection</h1></div>
      <div style="max-width:480px; padding:0 20px">
        <div class="ingest-field" style="margin-bottom:12px">
          <label>Name</label>
          <input type="text" id="col-name" placeholder="Collection name" />
        </div>
        <div class="ingest-field" style="margin-bottom:16px">
          <label>Description <span style="color:var(--t3); font-weight:400">(optional)</span></label>
          <textarea id="col-desc" style="min-height:70px"></textarea>
        </div>
        <div style="display:flex; gap:8px; align-items:center">
          <button class="btn btn-primary btn-sm" id="col-save">Create</button>
          <button class="btn btn-ghost btn-sm" id="col-cancel">Cancel</button>
        </div>
      </div>`)
    document.getElementById('col-name').focus()
    document.getElementById('col-cancel').addEventListener('click', () => {
      window.location.hash = state.navBack ? state.navBack.hash : '#/'
    })
    document.getElementById('col-save').addEventListener('click', async () => {
      const name = document.getElementById('col-name').value.trim()
      if (!name) { alert('Name is required'); return }
      try {
        const c = await API.collections.create({
          name, description: document.getElementById('col-desc').value.trim() || null,
        })
        _dimCache.collections = null
        if (state.expandedDims.has('collections')) _renderDimRecords('collections')
        window.location.hash = `#/collection/${c.id}`
      } catch (e) { alert('Failed: ' + e.message) }
    })
  }

  // Collection page — editable name/description in place + recording catalog.
  async function renderCollectionView(id) {
    setActiveNav('collections'); setActiveArtist(null); setLoading()
    let c
    try { c = await API.collections.get(id) }
    catch (e) { setMainHTML(`<div class="empty-state"><div class="empty-title">Collection not found</div></div>`); return }
    setNavCurrent(c.name)
    const navBack = state.navBack

    // ⚠ A SYSTEM collection (Full Library) is a sharing primitive, not
    // curation: its membership is a live query and the server refuses both
    // membership edits and deletion with a 409. So the page must not offer
    // them — a Delete button that always fails is worse than no button, and
    // this page had been rendering one since it was built.
    const editable = canEditLibrary() && !c.is_system

    // ── Render, from a local list, so add and remove repaint without a round
    //    trip to re-read the whole collection ──────────────────────────────
    let rows = c.recordings || []

    const cardsHtml = () => rows.length
      ? `<div class="lib-module-grid">${rows.map(r => editable
          ? `<div class="col-card-wrap">${recCardHtml(r)}
               <button type="button" class="col-card-x" data-remove="${r.id}"
                       title="Remove from this collection">${icon('x')}</button>
             </div>`
          : recCardHtml(r)).join('')}</div>`
      : `<div class="empty-state" style="min-height:160px">
           <div class="empty-title">Empty collection</div>
           <div class="empty-sub">${editable
             ? 'Search above to add recordings, or right-click a recording anywhere in the app.'
             : 'Nothing here yet.'}</div>
         </div>`

    const descText = c.description && c.description.trim()

    // The add bar. A collection is the one surface whose whole job is putting
    // recordings INTO it, and until now the only way was to right-click a
    // recording somewhere else and pick this collection off a menu — which
    // means knowing what you want before you arrive (Ryan, 2026-09-03).
    const addBarHtml = !editable ? '' : `
      <div class="col-add">
        <div class="col-add-wrap">
          <input type="text" id="col-add-q" class="col-add-input" autocomplete="off"
                 placeholder="Search recordings to add…">
          <div class="col-add-drop" id="col-add-drop" style="display:none"></div>
        </div>
        <span class="col-add-hint" id="col-add-hint"></span>
      </div>`

    setMainHTML(entityShellHtml({
      navBack,
      title: esc(c.name),
      titleId: 'col-name',
      titleEditable: editable,
      stats: [[rows.length, rows.length === 1 ? 'Recording' : 'Recordings']],
      // Refresh sits with Delete rather than in the nav: this one re-reads
      // THIS collection, where the sidebar's refresh re-reads the list of them.
      actions: `
        <button class="btn btn-ghost btn-sm" id="col-refresh" title="Re-read this collection from the library">
          ${icon('rotate-cw', 'lq-browse-ic')} Refresh</button>
        ${editable ? `<button class="btn btn-ghost btn-sm pp-delete" id="col-delete" title="Delete collection">Delete</button>` : ''}`,
      tabs: [{
        id: 'recordings', label: 'Recordings',
        html: `
          <div class="pp-sec">Description</div>
          <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="col-desc" title="Click to edit">${descText ? esc(c.description) : 'Add a description…'}</div>
          <div class="pp-sec" style="margin-top:24px">Recordings</div>
          ${addBarHtml}
          <div id="col-cards">${cardsHtml()}</div>`,
      }],
    }))
    wireEntityShell(mainContent, navBack)

    // Repaint just the cards and the count. Re-rendering the whole shell would
    // tear down the search box the user is typing in — the same lesson the
    // triage page learned the hard way (see _lqCaptureFocus).
    function repaintCards() {
      const box = document.getElementById('col-cards')
      if (box) box.innerHTML = cardsHtml()
      // The hero stat, patched in place — entityShellHtml renders it as
      // .pp-stat-n / .pp-stat-l (checked, not guessed: inventing class names
      // here would fail silently and leave the count wrong after every add).
      const stat = mainContent.querySelector('.pp-stat-n')
      if (stat) stat.textContent = String(rows.length)
      const statLabel = mainContent.querySelector('.pp-stat-l')
      if (statLabel) statLabel.textContent = rows.length === 1 ? 'Recording' : 'Recordings'
      wireRemove()
    }

    function wireRemove() {
      mainContent.querySelectorAll('[data-remove]').forEach(btn => {
        btn.addEventListener('click', async e => {
          // The card is an <a>. Without both of these the click navigates to
          // the recording and the removal is never seen.
          e.preventDefault(); e.stopPropagation()
          const rid = parseInt(btn.dataset.remove, 10)
          btn.disabled = true
          try {
            await API.collections.removeRecording(id, rid)
            rows = rows.filter(r => r.id !== rid)
            _colRecCache[id] = rows
            repaintCards()
            refreshSidebar()
          } catch (err) {
            btn.disabled = false
            alert('Could not remove: ' + err.message)
          }
        })
      })
    }

    const refreshSidebar = () => {
      _dimCache.collections = null
      delete _colRecCache[id]
      _renderDimRecords('collections')
    }
    async function saveField(patch) {
      try { await API.collections.update(id, patch); refreshSidebar() }
      catch (e) { alert('Save failed: ' + e.message) }
    }
    if (editable) {
      makeInlineEditable(document.getElementById('col-name'), {
        get: () => c.name,
        onSave: async v => { v = v.trim(); if (!v || v === c.name) return; c.name = v; await saveField({ name: v }) },
      })
      makeInlineEditable(document.getElementById('col-desc'), {
        multiline: true, placeholder: 'Add a description…',
        get: () => c.description || '',
        onSave: async v => { v = v.trim(); c.description = v; await saveField({ description: v || null }) },
      })
    }
    wireRemove()

    document.getElementById('col-refresh')?.addEventListener('click', async () => {
      const btn = document.getElementById('col-refresh')
      btn.disabled = true
      try {
        const fresh = await API.collections.get(id)
        c = fresh
        rows = fresh.recordings || []
        _colRecCache[id] = rows
        repaintCards()
        refreshSidebar()
      } catch (e) {
        alert('Refresh failed: ' + e.message)
      } finally { btn.disabled = false }
    })

    onAdminClick('col-delete', async () => {
      if (!confirm(`Delete collection "${c.name}"? Recordings are not affected.`)) return
      try { await API.collections.remove(id); refreshSidebar(); window.location.hash = '#/collections' }
      catch (e) { alert(e.message) }
    })

    // ── The add search ────────────────────────────────────────────────────
    //
    // Reuses the global search's `recordings` group rather than inventing an
    // endpoint: it already matches act, person, venue and date, which is
    // exactly how a collector looks for a show, and it is already paged.
    //
    // Results the collection ALREADY holds are shown and marked, not hidden.
    // Hiding them makes a show you know you added look missing, and the user
    // then adds it again from somewhere else; saying "Added" answers the
    // question the search was really asking.
    ;(function wireAdd() {
      const q    = document.getElementById('col-add-q')
      const drop = document.getElementById('col-add-drop')
      const hint = document.getElementById('col-add-hint')
      if (!q) return
      let debounce = null
      let lastQuery = ''

      const close = () => { drop.style.display = 'none'; drop.innerHTML = '' }

      function paint(items, total) {
        if (!items.length) {
          drop.innerHTML = `<div class="col-add-none">No recordings match that.</div>`
          drop.style.display = 'block'
          return
        }
        const have = new Set(rows.map(r => r.id))
        drop.innerHTML = items.map(it => {
          const inSet = have.has(it.id)
          let mainHtml
          if (it.type === 'album') {
            // Albums have no date/venue — lead with title (falling back to
            // artist), sub-line is artist · year (year alone with no title).
            const id = recIdentity({ kind: 'studio', title: it.title, artist: it.artist, start_year: it.year })
            const sub2 = [id.sub, id.dateText].filter(Boolean).join(' · ')
            mainHtml = `<span class="col-add-perf">${esc(id.lead)}</span>
              ${sub2 ? `<span class="col-add-sub">${esc(sub2)}</span>` : ''}`
          } else {
            const line2 = [it.date, it.venue, [it.city, it.state].filter(Boolean).join(', ')]
              .filter(Boolean).join(' · ')
            mainHtml = `<span class="col-add-perf">${esc(it.artist || '(unknown)')}</span>
              ${line2 ? `<span class="col-add-sub">${esc(line2)}</span>` : ''}`
          }
          return `<div class="col-add-row${inSet ? ' is-in' : ''}" data-add="${it.id}" ${inSet ? 'data-in="1"' : ''}>
            <span class="col-add-main">${mainHtml}</span>
            <span class="col-add-act">${inSet ? 'Added' : 'Add'}</span>
          </div>`
        }).join('')
          + (total > items.length
              ? `<div class="col-add-none">${total - items.length} more match. Narrow the search.</div>`
              : '')
        drop.style.display = 'block'

        drop.querySelectorAll('[data-add]').forEach(el => {
          // mousedown, not click: the input's blur would close the dropdown
          // out from under a click before it landed.
          el.addEventListener('mousedown', async ev => {
            ev.preventDefault()
            if (el.dataset.in) return
            const rid = parseInt(el.dataset.add, 10)
            el.classList.add('is-in'); el.dataset.in = '1'
            el.querySelector('.col-add-act').textContent = 'Added'
            try {
              await API.collections.addRecording(id, rid)
              // Re-read only to get the CARD shape — the search item carries a
              // different, thinner set of fields than recCardHtml needs, and
              // building a half-populated card would render a handbill with no
              // genre colour and no photo.
              const fresh = await API.collections.get(id)
              c = fresh
              rows = fresh.recordings || []
              _colRecCache[id] = rows
              repaintCards()
              refreshSidebar()
              if (hint) hint.textContent = `${rows.length} in this collection`
            } catch (err) {
              el.classList.remove('is-in'); delete el.dataset.in
              el.querySelector('.col-add-act').textContent = 'Add'
              if (hint) hint.textContent = 'Could not add: ' + err.message
            }
          })
        })
      }

      q.addEventListener('input', () => {
        const text = q.value.trim()
        clearTimeout(debounce)
        if (text.length < 2) { close(); return }
        debounce = setTimeout(async () => {
          lastQuery = text
          try {
            const [recRes, albRes] = await Promise.all([
              API.search.group(text, 'recordings', 12, 0),
              API.search.group(text, 'albums', 12, 0),
            ])
            if (lastQuery !== text) return      // a later keystroke already won
            paint([...(recRes.items || []), ...(albRes.items || [])],
                  (recRes.total || 0) + (albRes.total || 0))
          } catch (_) { close() }
        }, 220)
      })
      q.addEventListener('blur', () => setTimeout(close, 180))
      q.addEventListener('focus', () => { if (drop.innerHTML) drop.style.display = 'block' })
      q.addEventListener('keydown', e => { if (e.key === 'Escape') { close(); q.blur() } })
    })()
  }

  // Musician (person) page — editable info + Artist associations + appearances,
  // grouped by Artist alphabetically. Mirrors the Artist page.
  async function renderPersonView(id) {
    setActiveNav('musicians'); setActiveArtist(null); setLoading()
    let a
    try { a = await API.musicians.get(id) }
    catch (e) {
      invalidateDims('musicians')   // heal the sidebar if this person was removed
      setMainHTML(`<div class="empty-state"><div class="empty-title">This musician no longer exists</div></div>`)
      return
    }
    setNavCurrent(a.name)
    // Artists the person is a member of (already sorted by the API).
    let artists = (a.artists || []).map(p => ({ id: p.id, name: p.name }))

    // Fetch each act's recordings so we can group appearances by artist.
    let perfRecs = []
    try {
      perfRecs = await Promise.all(artists.map(p =>
        API.artists.recordings(p.id).then(rs => ({ artist: p, performances: rs.filter(x => (x.recordings || []).length) }))))
    } catch (_) {}

    const totalRecordings = perfRecs.reduce((n, g) => n + g.performances.reduce((m, p) => m + p.recordings.length, 0), 0)

    // One <section> per artist (alpha), each with a header + flat recording rows.
    const groupsHtml = perfRecs.map(g => {
      const ordered = g.performances.slice().sort((x, y) => compareByDate(x, y, false))
      const rowObjs = ordered.flatMap(p =>
        p.recordings.map(r => ({
          id: r.id, artist: p.artist_name,
          start_year: p.start_year, start_month: p.start_month, start_day: p.start_day,
          venue: p.venue_name, city: p.city, state: p.state, country: p.country,
          source: r.source, quality: r.quality,
          is_complete: r.is_complete,
          track_count: r.track_count, duration_sec: r.duration_sec, image_url: r.image_url,
          kind: r.kind, title: r.title,
        })))
      const rows = rowObjs.map(r => flatRowHtml(r, false)).join('')
      if (!rows) return ''
      return `<div class="pp-group">
        <div class="pp-group-head"><a href="#/artist/${g.artist.id}">${esc(g.artist.name)}</a></div>
        <div class="rec-table">${rows}</div>
      </div>`
    }).join('')

    // Guest / sit-in appearances — performance_personnel rows on acts this
    // person isn't formally a Membership of (2026-07-18 Per-Show Personnel,
    // ripple item 3: "Béla's page would finally surface his All-Stars
    // sit-ins"). Grouped by artist like the section above, but kept
    // visually separate and tagged "guest" since it's not the same thing as
    // full membership — this is a different act's recording that happens to
    // include this person for one show.
    const guestAppearances = a.guest_appearances || []
    const guestByArtist = {}
    guestAppearances.forEach(g => {
      const key = g.artist_id
      if (!guestByArtist[key]) guestByArtist[key] = { artist_id: g.artist_id, artist_name: g.artist_name, appearances: [] }
      guestByArtist[key].appearances.push(g)
    })
    const totalGuestRecordings = guestAppearances.reduce((n, g) => n + (g.recordings || []).length, 0)

    const guestGroupsHtml = Object.values(guestByArtist).map(g => {
      const ordered = g.appearances.slice().sort((x, y) => compareByDate(x, y, false))
      const rowObjs = ordered.flatMap(ap =>
        (ap.recordings || []).map(r => ({
          id: r.id, artist: g.artist_name,
          start_year: ap.start_year, start_month: ap.start_month, start_day: ap.start_day,
          venue: ap.venue_name, city: ap.city, state: ap.state, country: ap.country,
          source: r.source, quality: r.quality,
          is_complete: r.is_complete,
          track_count: r.track_count, duration_sec: r.duration_sec, image_url: r.image_url,
          kind: r.kind, title: r.title,
        })))
      const rows = rowObjs.map(r => flatRowHtml(r, false)).join('')
      if (!rows) return ''
      // "Guest" tag only when every appearance under this act name is
      // actually is_guest=True (2026-07-23 fix — this section is really "not
      // a formal roster member of this act," which the API named
      // guest_appearances back when that always meant a sit-in. The
      // Members/Guests two-row redesign (2026-07-22) added a real non-guest
      // case here too: a full billed appearance under a one-off act name
      // (e.g. a duo billing) that this person isn't formally on the roster
      // of. Tagging that "guest" was Ryan's bug report — Bela Fleck & Bryan
      // Sutton is a real Member appearance, not a sit-in. Each appearance
      // carries its own is_guest; only tag the group when ALL of them agree.)
      const allGuest = ordered.every(ap => ap.is_guest)
      return `<div class="pp-group">
        <div class="pp-group-head"><a href="#/artist/${g.artist_id}">${esc(g.artist_name)}</a>${allGuest ? ' <span class="pp-guest-tag">guest</span>' : ''}</div>
        <div class="rec-table">${rows}</div>
      </div>`
    }).join('')

    const descText = a.bio && a.bio.trim()
    const navBack = state.navBack

    const photoCount = (a.images || []).length

    setMainHTML(entityShellHtml({
      navBack,
      portrait: '<div id="pn-portrait"></div>',
      title: esc(a.name),
      titleId: 'pn-name',
      titleEditable: true,
      chips: `<span class="pp-hero-fact">${artists.length} artist${artists.length !== 1 ? 's' : ''}</span>`,
      stats: [
        [totalRecordings, totalRecordings === 1 ? 'Recording' : 'Recordings'],
        ...(totalGuestRecordings ? [[totalGuestRecordings, 'Guest']] : []),
      ],
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="pn-delete" title="Delete musician">Delete</button>`,
      // Photos arrived here 2026-09-01, reversing the 2026-08-07 "photos are
      // artist-level only" call — for this PAGE. The original reasoning
      // stands where it was aimed: cards key off the act, so putting a person's
      // likeness on them would still give a wall of identical tiles. What it
      // was not aimed at is a person's own page, which is the one surface in
      // this app where a portrait is obviously the right thing.
      tabs: [
        { id: 'recordings', label: 'Recordings', active: true,
          count: totalRecordings + totalGuestRecordings, html: `
            ${groupsHtml || (guestGroupsHtml ? '' : '<div class="empty-state" style="min-height:160px"><div class="empty-title">No appearances yet</div></div>')}
            ${guestGroupsHtml ? `<div class="pp-sec" style="margin-top:24px">Guest appearances</div>${guestGroupsHtml}` : ''}` },
        { id: 'about', label: 'About', html: `
            <div class="pp-sec">Artists</div>
            <div class="pp-musicians" id="pn-artists"></div>

            <div class="pp-sec">Bio</div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="pn-desc" title="Click to edit">${descText ? esc(a.bio) : 'Add a bio\u2026'}</div>` },
        { id: 'photos', label: 'Photos', count: photoCount || null,
          html: '<div id="pn-photos"></div>' },
      ],
    }))
    wireEntityShell(mainContent, navBack)

    wireRecordingRows(mainContent)

    // ── Photos ──────────────────────────────────────────────────────────────
    // Shared gallery, no automatic fetch tile: the Commons bridge runs through
    // an ACT's MusicBrainz match (MusicBrainz → Wikidata → P18) and a person
    // reached through this page has no equivalent hook. The two search tiles
    // cover it — they open a search and import nothing, so they need no
    // licence bridge to be honest about what they are.
    const pnPortrait = () => {
      const el = document.getElementById('pn-portrait')
      if (!el) return
      const primary = (a.images || [])[0]      // server orders primary-first
      el.innerHTML = heroPortraitHtml(a.name, primary ? API.musicians.imageUrl(primary.id) : null)
    }
    createPhotoGallery({
      mountId: 'pn-photos', api: API.musicians, entityId: id, images: a.images || [],
      // "musician" disambiguates a person's name from the hundred other people
      // who share it — the single most useful qualifier for this dimension.
      linkTiles: photoSearchTiles(a.name, 'musician'),
      onChange: imgs => {
        a.images = imgs
        pnPortrait()
        const tab = mainContent.querySelector('.pp-tab[data-pane="photos"]')
        if (tab) tab.innerHTML = 'Photos' + (imgs.length ? `<span class="pp-tab-n">${imgs.length}</span>` : '')
      },
    })

    const refreshSidebar = () => invalidateDims('musicians', 'artists')
    async function saveField(patch) {
      try { await API.musicians.update(id, patch); refreshSidebar() }
      catch (e) { alert('Save failed: ' + e.message) }
    }
    makeInlineEditable(document.getElementById('pn-name'), {
      get: () => a.name,
      onSave: async v => { v = v.trim(); if (!v || v === a.name) return; a.name = v; await saveField({ name: v }) },
    })
    makeInlineEditable(document.getElementById('pn-desc'), {
      multiline: true, placeholder: 'Add a bio…',
      get: () => a.bio || '',
      onSave: async v => { v = v.trim(); a.bio = v; await saveField({ bio: v || null }) },
    })

    // ── Editable Artist associations ─────────────────────────────────────
    function renderArtists() {
      const box = document.getElementById('pn-artists')
      box.innerHTML =
        artists.map((p, i) => `<span class="member-chip">${esc(p.name)} <span class="member-chip-x" data-i="${i}" title="Remove from this act">${icon('x')}</span></span>`).join('') +
        `<span class="artist-picker-wrap pp-add-wrap">
           <input type="text" class="member-input pp-add-input" autocomplete="off" placeholder="Add to an artist…" />
           <div class="artist-dropdown" id="pn-add-dd" style="display:none"></div>
         </span>`
      box.querySelectorAll('.member-chip-x').forEach(x =>
        x.addEventListener('click', async () => {
          const p = artists[parseInt(x.dataset.i)]
          try { await API.musicians.removeArtist(id, p.id); invalidateDims('artists') } catch (e) { alert(e.message); return }
          renderPersonView(id)   // reload so the grouped appearances update
        }))
      const input = box.querySelector('.pp-add-input')
      wirePickerDropdown(input, document.getElementById('pn-add-dd'), API.artists.search,
        async ({ id: pid, name }) => {
          try { await API.musicians.addArtist(id, pid ? { artist_id: pid } : { artist_name: name }); invalidateDims('artists') }
          catch (e) { alert(e.message); return }
          renderPersonView(id)
        }, 'Create new artist')
    }
    renderArtists()

    onAdminClick('pn-delete', async () => {
      if (!confirm(`Delete musician "${a.name}"? This can't be undone.`)) return
      try { await API.musicians.remove(id); refreshSidebar(); window.location.hash = '#/' }
      catch (e) { alert(e.message) }
    })
  }

  // ── Library Browse view (2026-08-02 design spec) ────────────────────────────
  // Browse is now the ONLY presentation for Library and Recently Added — the
  // Browse/List toggle and the flat-table "List" mode it switched to are gone
  // (Ryan, 2026-08-24: "we don't need list anymore"). getLibraryViewMode /
  // setLibraryViewMode / libToggleHtml / wireLibToggle and the
  // fluxLibraryView localStorage key retired with it; a stale value left over
  // from an old visit is simply never read again.

  // Source → CSS color token. Kept separate rather than parsing sourceBadge's
  // HTML back apart. Drives the card's accent rules and source chip.
  function _sourceColorVar(source) {
    return { SBD: '--sbd-fg', AUD: '--aud-fg', MTX: '--mtx-fg', FM: '--fm-fg' }[source] || '--other-fg'
  }

  // MusicBrainz's public page for an artist. The MBID is a stable permalink,
  // so this needs no lookup — and it's the only way to actually VERIFY a match
  // on a vague name like "Acoustic All-Stars", where our one-line summary
  // can't settle it but the real entry's releases and relationships can.
  function mbArtistUrl(mbid) {
    return mbid ? `https://musicbrainz.org/artist/${encodeURIComponent(mbid)}` : '#'
  }

  // Release lookup (Studio Records spec v1, chunk 3) -- same musicbrainz.org
  // link-out pattern as mbArtistUrl, one path segment down.
  function mbReleaseUrl(mbid) {
    return mbid ? `https://musicbrainz.org/release/${encodeURIComponent(mbid)}` : '#'
  }

  // Defensive strip of citation markup in stored AI text. The real fix is
  // server-side in artist_research.py (_clean_prose), so what gets SAVED is
  // clean — this only covers dossiers written before that landed, which would
  // otherwise show "<cite index=…>" on the page forever.
  function stripCitations(text) {
    return String(text || '')
      .replace(/<\/?cite[^>]*>/gi, '')
      .replace(/\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]/g, '')
      .replace(/\s+([.,;:!?])/g, '$1')
      .replace(/[ \t]{2,}/g, ' ')
      .trim()
  }

  // Title case since 2026-08-23 (Ryan): the card date is a standard date
  // line, "Apr 22, 1974", not a poster's shouted caps.
  const _BILL_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

  // Handbill date line — the card's most prominent element after the act.
  //
  // Deliberately NOT fmtDate(): this is display typography, uppercase and
  // spaced for a poster, while fmtDate() is the app's neutral everywhere-else
  // format. Degrades through three precisions because 50 of 544 recordings
  // have a partial date — a card must never render "NOV undefined · 2000".
  //
  //   full     → "NOV 3 · 2000"
  //   no day   → "SEP 1989"
  //   year only→ "1965"
  function handbillDate(y, m, d) {
    if (!y) return ''
    const mon = (m >= 1 && m <= 12) ? _BILL_MONTHS[m - 1] : null
    if (mon && d) return `${mon} ${d}, ${y}`
    if (mon)      return `${mon} ${y}`
    return String(y)
  }

  // Genre colour with the agreed fallback. 70 of 164 artists have no genre,
  // so NULL is the common case, not an error case — those cards get a neutral
  // warm grey and read as quiet rather than broken (Ryan, 2026-08-07). The
  // fallback lives HERE and nowhere else: the serializer deliberately sends
  // null rather than substituting a default, so there is exactly one place
  // that decides what "no genre" looks like.
  function genreColor(r) {
    return (r && r.genre_color) ? r.genre_color : 'var(--t2)'
  }

  // Artist avatar for the cards — photo when there is one, INITIALS when
  // there isn't (Ryan, 2026-08-07). It previously rendered nothing without a
  // photo, which made cards for photographed and un-photographed acts two
  // different shapes. Since most acts have no photo, the initials disc IS the
  // normal appearance, and using the same one the Artist page hero uses
  // makes every card and page agree.
  function perfPhotoHtml(r, cls) {
    if (!r) return ''
    if (r.image_id) {
      return `<img class="${cls}" src="${API.artists.imageUrl(r.image_id)}"
                   alt="" loading="lazy">`
    }
    const initials = String(r.artist || '?').split(/\s+/).filter(Boolean)
      .slice(0, 2).map(w => w[0]).join('').toUpperCase()
    return `<span class="${cls} ${cls}--initials">${esc(initials)}</span>`
  }

  // Footer bits shared by both card layouts. Composes from whatever exists —
  // grade is present on ~22% of recordings, so its absence must read as normal
  // rather than as something missing.
  function recCardFootParts(r) {
    const foot = []
    if (r.source) foot.push(`<span class="rec-card-src">${esc(r.source)}</span>`)
    if (r.track_count) {
      foot.push(`<span>${r.track_count} track${r.track_count === 1 ? '' : 's'}</span>`)
    }
    const runtime = fmtRuntime(r.duration_sec)
    if (runtime) foot.push(`<span>${esc(runtime)}</span>`)
    if (r.quality) foot.push(`<span class="${qualityClass(r.quality)}">${esc(r.quality)}</span>`)
    return foot
  }

  // Studio footer variant: track count and runtime only -- a studio record
  // has no source or quality grade (product rule), so recCardFootParts'
  // source/quality bits would either read blank or, worse, leak a value the
  // model still carries from before it was marked studio.
  function recCardFootPartsStudio(r) {
    const foot = []
    if (r.track_count) {
      foot.push(`<span>${r.track_count} track${r.track_count === 1 ? '' : 's'}</span>`)
    }
    const runtime = fmtRuntime(r.duration_sec)
    if (runtime) foot.push(`<span>${esc(runtime)}</span>`)
    return foot
  }

  // ── Handbill card — RECOMMENDED MODULE ONLY (Ryan, 2026-08-07) ─────────────
  //
  // Replaced the waveform strip: waveform-led cards are what the Internet
  // Archive's Live Music Archive already does, so they read as derivative.
  // This renders the METADATA as the artwork instead — also the better-covered
  // asset, since every performance has a year and 542 of 544 have a city,
  // against 530 with waveform data and only 118 with a letter grade.
  //
  // Scoped to Recommended deliberately. The handbill is tall, centred and
  // deliberately showy; that works for three hero picks and would be
  // exhausting repeated down a twelve-item Recently Added list, which is why
  // that module now has its own row layout below.
  //
  // Colour comes from the artist's GENRE, not the source — source is a
  // technical attribute and makes a poor identity, whereas genre groups the
  // library the way a listener actually browses it.
  // Studio variant: cover-led (Ryan, Studio Records spec v1 chunk 5 --
  // "for an album the cover is what the date is for a show"). The cover
  // photo takes the date line's spot when there is one; the title always
  // takes the date line's strong typographic slot, artist below it, year
  // where the venue line was. Never a venue, location or source line.
  function _studioRecCardHtml(r, id) {
    const cover = r.image_url
      ? `<img class="rec-card-cover" src="${esc(r.image_url)}" alt="" loading="lazy">`
      : ''
    const foot = recCardFootPartsStudio(r)
    return `
      <a class="rec-card rec-card--studio" href="#/recording/${r.id}" data-rec-id="${r.id}"
         style="--genre-fg:${esc(genreColor(r))}">
        <div class="rec-card-spine"></div>
        ${cover}
        <div class="rec-card-date">${esc(id.lead)}</div>
        <div class="rec-card-rule"></div>
        ${id.sub ? `<div class="rec-card-artist">${esc(id.sub)}</div>
        <div class="rec-card-rule"></div>` : ''}
        <div class="rec-card-venue">${esc(id.dateText)}</div>
        <div class="rec-card-foot">${foot.join('<span class="rec-card-dot">·</span>')}</div>
      </a>`
  }

  function recCardHtml(r) {
    const id = recIdentity(r)
    if (id.isStudio) return _studioRecCardHtml(r, id)
    const date = handbillDate(r.start_year, r.start_month, r.start_day)
    const loc  = fmtLocation(r.city, r.state, r.country)
    const foot = recCardFootParts(r)
    const photo = perfPhotoHtml(r, 'rec-card-photo')
    return `
      <a class="rec-card" href="#/recording/${r.id}" data-rec-id="${r.id}"
         style="--genre-fg:${esc(genreColor(r))}">
        <div class="rec-card-spine"></div>
        ${photo}
        ${date ? `<div class="rec-card-date">${esc(date)}</div>` : ''}
        <div class="rec-card-rule"></div>
        <div class="rec-card-artist">${esc(r.artist || '')}</div>
        <div class="rec-card-rule"></div>
        <div class="rec-card-venue">${esc(r.venue || '(unknown venue)')}</div>
        ${loc ? `<div class="rec-card-loc">${esc(loc)}</div>` : ''}
        ${r.genre ? `<div class="rec-card-genre">${esc(r.genre)}</div>` : ''}
        <div class="rec-card-foot">${foot.join('<span class="rec-card-dot">·</span>')}</div>
      </a>`
  }

  // ── Row card — RECENTLY ADDED MODULE ─────────────────────────────────────
  //
  // Full-width row with list-like density but card styling (Ryan, 2026-08-07):
  // a browsable middle ground between the handbill and the flat table. Carries
  // more per-recording detail than the handbill precisely because a row has
  // horizontal space a 3-up card doesn't.
  //
  // The ingest date is the point of this module — "what's new" is the question
  // being answered, and the show date (1969) says nothing about that. The
  // server orders by created_at DESC; this does not re-sort.
  // Recently Added row (2026-10-01): the Live Recordings / Albums row grid,
  // one layout for both kinds. Image, Artist, Date, then venue + place or
  // the album title, then source or release, tracks/length, date added.
  function _recentRowHtml(r) {
    const id = recIdentity(r)
    const initials = String(r.artist || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    const av = r.image_url
      ? `<img class="brow-av brow-av--img" src="${esc(r.image_url)}" alt="" loading="lazy">`
      : `<span class="brow-av">${esc(initials)}</span>`
    const dateText = id.isStudio ? (id.dateText || '—') : (handbillDate(r.start_year, r.start_month, r.start_day) || '—')
    const main = id.isStudio
      ? `<span class="brow-title">${esc(r.title || '')}</span>`
      : `<span class="brow-venue">${esc([r.venue || '(unknown venue)', fmtLocation(r.city, r.state, r.country)].filter(Boolean).join(', '))}</span>`
    const info = id.isStudio
      ? `<span class="brow-release">${esc([r.mb_label, r.mb_catalog_number].filter(Boolean).join(' · '))}</span>`
      : `<span class="brow-srccell">${r.source ? `<span class="brow-src">${esc(r.source)}</span>` : ''}</span>`
    return `
      <a class="brow brow--recent" href="#/recording/${r.id}" data-rec-id="${r.id}" style="--genre-fg:${esc(genreColor(r))}">
        ${av}
        <span class="brow-perf">${esc(r.artist || '')}</span>
        <span class="brow-date">${esc(dateText)}</span>
        ${main}
        ${info}
        <span class="brow-size">${esc(_rowSizeText(r))}</span>
        <span class="brow-date brow-added">${esc(fmtDateAdded(r.created_at) || '—')}</span>
      </a>`
  }

  function recentRowCardHtml(r) {
    const id = recIdentity(r)
    // Studio: the row already leads with an "artist" line, so it leads with
    // the title instead and puts the artist second (falling back to the
    // artist alone when there is no title) -- never a venue line.
    const date = id.isStudio ? (id.dateText || '') : handbillDate(r.start_year, r.start_month, r.start_day)
    const loc  = fmtLocation(r.city, r.state, r.country)
    const foot = id.isStudio ? recCardFootPartsStudio(r) : recCardFootParts(r)
    const photo = perfPhotoHtml(r, 'rec-rowcard-photo')
    const venue = [r.venue || '(unknown venue)', loc].filter(Boolean).join(' · ')
    const mainLead = id.isStudio ? id.lead : (r.artist || '')
    const mainSub  = id.isStudio ? id.sub  : venue
    return `
      <a class="rec-rowcard" href="#/recording/${r.id}" data-rec-id="${r.id}"
         style="--genre-fg:${esc(genreColor(r))}">
        <div class="rec-rowcard-spine"></div>
        <div class="rec-rowcard-avatar">${photo}</div>
        <div class="rec-rowcard-date">${esc(date || '—')}</div>
        <div class="rec-rowcard-main">
          <div class="rec-rowcard-artist truncate">${esc(mainLead)}</div>
          <div class="rec-rowcard-venue truncate">${esc(mainSub)}</div>
        </div>
        <div class="rec-rowcard-meta">${foot.join('<span class="rec-card-dot">·</span>')}</div>
        <div class="rec-rowcard-added">
          <span class="rec-rowcard-added-lbl">Added</span>
          <span class="rec-rowcard-added-val">${esc(fmtDateAdded(r.created_at) || '—')}</span>
        </div>
      </a>`
  }

  function colTileHtml(c) {
    const count = c.recording_count || 0
    return `
      <a class="col-tile" href="#/collection/${c.id}">
        <div class="col-tile-name truncate">${esc(c.name)}</div>
        ${c.description ? `<div class="col-tile-desc truncate">${esc(c.description)}</div>` : ''}
        <div class="col-tile-count">${count} recording${count === 1 ? '' : 's'}</div>
      </a>`
  }

  // Reroll counter for Recommended's "Show me three more" — deliberately kept
  // in memory only (module scope), not persisted: the default draw is stable
  // for the day (server-seeded by date), and a page reload should return to
  // that same stable default, not wherever the reroll was left.
  let _libRecommendedReroll = 0

  // ── The Top Shelf as a record bin (Ryan, 2026-09-02) ─────────────────────
  //
  // It was six tiles and a scrollbar. It is now a bin you flip through: every
  // click of the right chevron pulls ONE more recording off the ordered
  // sequence, appends it to the right, and slides the leftmost tile out of
  // view. Click until something catches your eye; go back with the left
  // chevron. When the pool is spent, the right chevron GOES — the strip
  // refusing to move with no explanation was the thing to avoid, and a
  // disappearing control says "that's all of them" without a word of copy.
  //
  // `_topsShown` is how far into the sequence we have drawn, and doubles as
  // the server-side offset — see /api/recordings/recommended's `offset`.
  // `_topsNext` is one tile fetched ahead, so a click paints immediately
  // instead of waiting on a round trip; its emptiness is also how exhaustion
  // is discovered, one click before the user would have hit it.
  let _topsShown = 0
  let _topsNext  = null
  let _topsBusy  = false

  // ── How many records the shelf holds is a function of the WINDOW ──────────
  //
  // It was six, always, and six 168px tiles leave a hand's width of dead space
  // on an ordinary laptop and a chasm on anything wider (Ryan, 2026-09-03).
  // A shelf that does not reach the end of its own shelf reads as broken
  // furniture.
  //
  // So the count is measured, not chosen, and the tiles are then sized to fill
  // the track EXACTLY. Both halves are needed: picking a count alone still
  // leaves up to one tile-width of remainder, and stretching six tiles alone
  // makes them bigger rather than showing more records, which is not what a
  // bin is for.
  //
  // MIN is the smallest a tile may be before another one stops fitting; the
  // real width lands between MIN and just under 2×MIN. Height tracks width so
  // the art stays square — ⚠ it must be set explicitly and not by
  // `aspect-ratio`, which collapsed these tiles to a 2px sliver in 2026-08-24
  // because their only children are absolutely positioned.
  const TOPS_TILE_MIN = 168
  const TOPS_GAP      = 10

  function _topsFit(width) {
    if (!width || width < TOPS_TILE_MIN) return 1
    return Math.max(1, Math.floor((width + TOPS_GAP) / (TOPS_TILE_MIN + TOPS_GAP)))
  }

  // The usable track, with the strip's own 28px gutters taken off.
  function _topsTrackWidth() {
    const grid = document.getElementById('tops-grid')
    if (grid && grid.clientWidth) {
      const cs = getComputedStyle(grid)
      return grid.clientWidth - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0)
    }
    // The FIRST fetch happens before the strip is painted, so fall back to the
    // content column.
    //
    // ⚠ NOT #lib-topshelf, the shelf's own mount. It carries
    // `#lib-topshelf:empty { display: none }` so an absent shelf leaves no gap
    // — and it is empty at exactly this moment, so it measures ZERO and the
    // first fetch asked for the six-tile floor on every window however wide.
    // Confirmed live: a 1920px load painted six tiles and a 394px gap, then
    // corrected itself a beat later when the observer fired. Measuring the
    // column the shelf will occupy gets it right on the first paint instead.
    const host = mainContent
    return Math.max(0, (host ? host.clientWidth : 0) - 56)
  }

  // Size the tiles to fill the track exactly, and report how many fit.
  function _topsLayout() {
    const grid = document.getElementById('tops-grid')
    if (!grid) return 0
    const w = _topsTrackWidth()
    const n = _topsFit(w)
    // Floor, not round: a fractional pixel per tile accumulated across nine of
    // them is enough to push the last one into overflow and put a scrollbar on
    // a row that fits.
    const tile = Math.floor((w - TOPS_GAP * (n - 1)) / n)
    if (tile > 0) grid.style.setProperty('--tops-tile', tile + 'px')
    return n
  }

  // Widening deals more records rather than stretching the ones already out.
  // Narrowing deals none: the extra tiles stay loaded and simply scroll off,
  // which is what the left chevron is for — throwing away records the user has
  // already been shown would be a worse answer than letting them slide.
  async function _topsFillTo(want) {
    const grid = document.getElementById('tops-grid')
    if (!grid) return
    let have = grid.querySelectorAll('.top-tile').length
    if (want <= have) return

    // The look-ahead tile is already paid for; spend it before asking for more,
    // or it would be requested a second time at the same offset and appear
    // twice.
    if (_topsNext) {
      grid.insertAdjacentHTML('beforeend', _topTileHtml(_topsNext))
      _topsShown += 1; _topsNext = null; have += 1
    }
    if (want > have) {
      try {
        const more = await API.recordings.recommended(want - have, _libRecommendedReroll, _topsShown)
        ;(more || []).forEach(r => {
          grid.insertAdjacentHTML('beforeend', _topTileHtml(r))
          _topsShown += 1
        })
      } catch (_) { /* a shelf that did not grow is not worth a banner */ }
    }
    _updateTopsNav()
    _topsPrefetch()
  }

  // ⚠ A ResizeObserver, not window.onresize. The content column also changes
  // width when the sidebar is collapsed from the header, which fires no window
  // resize at all — the shelf would have stayed six-wide in exactly the case
  // that gains the most room.
  //
  // One observer for the app, re-pointed at each new strip: renderBrowseModules
  // runs on every visit to Browse, and attaching a fresh observer each time
  // would leave the old ones alive against detached nodes.
  let _topsRO = null
  let _topsResizeT = null

  // One debounced re-layout, shared by both triggers. Debounced because a drag
  // of the window edge fires continuously and each widening step would
  // otherwise start its own fetch.
  function _topsRelayoutSoon() {
    clearTimeout(_topsResizeT)
    _topsResizeT = setTimeout(() => {
      const want = _topsLayout()
      if (want) _topsFillTo(want)
    }, 160)
  }

  // ⚠ TWO triggers, and each covers what the other misses.
  //
  // ResizeObserver is the one that matters — it catches the content column
  // changing width when the SIDEBAR is collapsed, which fires no window resize
  // at all, and that is the case that gains the most room.
  //
  // But RO callbacks are delivered as part of the rendering lifecycle, so a
  // window that is not painting frames never receives them. Measured
  // directly: in a hidden pane a freshly-created observer did not even fire
  // its initial observation, and fired the instant the pane painted. That is
  // the same starvation that makes a smooth scroll a no-op here (see
  // _topsScrollTo). `resize` is a plain event and is not gated that way, so it
  // is the belt to RO's braces for the ordinary window-drag case.
  //
  // Registered ONCE for the app. renderBrowseModules runs on every visit to
  // Browse, and a listener per visit would pile up against detached shelves.
  let _topsResizeBound = false
  function _observeTops() {
    const shelf = document.getElementById('tops-grid')
    if (_topsRO) _topsRO.disconnect()
    if (!_topsResizeBound) {
      window.addEventListener('resize', () => {
        if (document.getElementById('tops-grid')) _topsRelayoutSoon()
      })
      _topsResizeBound = true
    }
    if (shelf && typeof ResizeObserver !== 'undefined') {
      _topsRO = new ResizeObserver(_topsRelayoutSoon)
      _topsRO.observe(shelf)
    }
    // Synchronously, so the first paint is already the right shape rather than
    // reflowing a beat later.
    const want = _topsLayout()
    if (want) _topsFillTo(want)
  }

  // Look one past the end. Returns nothing and leaves _topsNext null when the
  // sequence is spent, which _updateTopsNav() reads as "hide the right nav".
  async function _topsPrefetch() {
    if (_topsNext || _topsBusy) return
    _topsBusy = true
    try {
      const more = await API.recordings.recommended(1, _libRecommendedReroll, _topsShown)
      _topsNext = more && more.length ? more[0] : null
    } catch (_) {
      _topsNext = null
    } finally {
      _topsBusy = false
      _updateTopsNav()
    }
  }

  // Shuffle. Replaces only the TILES, not the whole section — re-rendering the
  // heading and its button meant re-wiring the button every time, which is how
  // the old version worked and one listener leak away from not working.
  //
  // A shuffle is a NEW BIN: the reroll counter advances, so the sequence is a
  // different draw, and everything flipped past is forgotten. Resetting
  // `_topsShown` is what makes that true — leaving it would have the fresh
  // bin start six deep into its own sequence.
  async function _refreshRecommendedModule() {
    const grid = document.getElementById('tops-grid')
    if (!grid) return
    _libRecommendedReroll++
    let recs = []
    // The same measured count, or a shuffle on a wide window would refill the
    // shelf to six and reintroduce the gap it was just widened out of.
    const want = Math.max(BROWSE_TOP_N, _topsFit(_topsTrackWidth()))
    try { recs = await API.recordings.recommended(want, _libRecommendedReroll, 0) } catch (_) {}
    if (!recs.length) { document.getElementById('lib-mod-tops')?.remove(); return }
    grid.innerHTML = recs.map(_topTileHtml).join('')
    grid.scrollLeft = 0
    _topsShown = recs.length
    _topsNext  = null
    _topsLayout()
    _updateTopsNav()
    _topsPrefetch()
  }

  // Left nav appears once there is anything to go back to; right nav lives
  // exactly as long as the bin has another record in it.
  //
  // ⚠ Right used to be UNCONDITIONAL, and deliberately so (2026-08-24): the
  // strip scrolled natively, all six tiles fit on a wide window, and a button
  // that only appeared on overflow never appeared at all. That reasoning does
  // not survive the rebuild — the button no longer scrolls, it DEALS, so
  // "nothing left to deal" is a real state and hiding it is the honest answer
  // rather than a no-op click.
  function _updateTopsNav() {
    const grid = document.getElementById('tops-grid')
    const navL = document.getElementById('tops-nav-l')
    const navR = document.getElementById('tops-nav-r')
    if (!grid || !navL) return
    navL.classList.toggle('hidden', grid.scrollLeft <= 2)
    // Hidden only once a completed prefetch has come back empty. While one is
    // in flight the button stays put: flickering it away and back on every
    // click would read as a bug, and a click during that window is queued by
    // _topsAdvance rather than lost.
    if (navR) navR.classList.toggle('hidden', !_topsNext && !_topsBusy)
  }

  // Move the strip, and MAKE SURE IT MOVED.
  //
  // ⚠ Found live, 2026-09-02: a smooth scroll is a silent no-op in some
  // webviews. `scrollTo({behavior:'smooth'})` schedules an animation driven by
  // animation frames, and in a pane that is not ticking them the animation
  // never starts, never finishes, and never reports anything — scrollLeft
  // simply stays where it was. `scroll-behavior: smooth` in CSS is worse,
  // because it hijacks a plain `el.scrollLeft = n` assignment into the same
  // dead animation, so even the obvious fallback fails. Verified all three
  // combinations against the running app: CSS-smooth + JS-smooth, CSS-auto +
  // JS-smooth, and CSS-auto + JS-instant. Only the last one moved.
  //
  // So: ask for the animation, then check. If nothing has moved a quarter of a
  // second later, put it where it belongs outright. The strip arriving without
  // a glide is a cosmetic loss; a chevron that does nothing is a broken
  // control, and this is a DEALING button now — the tile has already been
  // appended by the time we get here, so a failed scroll would leave the shelf
  // silently growing off the right-hand edge.
  //
  // The CSS `scroll-behavior: smooth` came off .tops-grid in the same pass:
  // one place decides whether a movement animates, and it is here.
  async function _topsScrollTo(grid, left) {
    const from = grid.scrollLeft
    grid.scrollTo({ left, behavior: 'smooth' })
    await new Promise(r => setTimeout(r, 250))
    if (Math.abs(grid.scrollLeft - from) < 1) grid.scrollLeft = left
  }

  function _topsStep(grid) {
    const tile = grid.querySelector('.top-tile')
    if (!tile) return grid.clientWidth
    const gap = parseFloat(getComputedStyle(grid).columnGap) || 0
    return tile.getBoundingClientRect().width + gap
  }

  // One flip right: append the prefetched tile, slide one tile-width along,
  // and look ahead again.
  async function _topsAdvance() {
    const grid = document.getElementById('tops-grid')
    if (!grid) return
    // A click that lands while the look-ahead is still out waits for it rather
    // than doing nothing — on a slow first click that is the difference
    // between "the button is broken" and "the button is thinking".
    if (!_topsNext && _topsBusy) {
      while (_topsBusy) await new Promise(r => setTimeout(r, 40))
    }
    if (!_topsNext) { _updateTopsNav(); return }

    grid.insertAdjacentHTML('beforeend', _topTileHtml(_topsNext))
    _topsShown += 1
    _topsNext = null
    await _topsScrollTo(grid, grid.scrollLeft + _topsStep(grid))
    _updateTopsNav()
    _topsPrefetch()
  }

  function _wireTopsNav() {
    const grid = document.getElementById('tops-grid')
    const navL = document.getElementById('tops-nav-l')
    const navR = document.getElementById('tops-nav-r')
    if (!grid || !navL || !navR) return
    navL.addEventListener('click', () =>
      _topsScrollTo(grid, Math.max(0, grid.scrollLeft - _topsStep(grid))).then(_updateTopsNav))
    navR.addEventListener('click', _topsAdvance)
    grid.addEventListener('scroll', _updateTopsNav)
    _updateTopsNav()
    _topsPrefetch()
  }

  // Builds and mounts all five Browse modules into `mountEl`. Each module is
  // fetched independently and simply omitted if empty or its request fails —
  // "every module hides entirely when it has nothing to show" is what makes a
  // fixed, un-configurable module set work on a sparse library (design spec).
  // ── Browse (rebuilt 2026-08-23, "Direction A — The Shelf") ─────────────────
  //
  // The linear list IS the page. Everything above it is a thin band that has to
  // earn its height, and Collections moved BELOW the list (Ryan) — it is four
  // collections, and putting it up top gave the sparsest section the best real
  // estate.
  //
  // What this replaced and why:
  //   Recommended        → "Random Top Shows". "Recommended" implied a
  //                        recommender; nothing here models taste. These are a
  //                        random draw from the graded shows, and the name now
  //                        says so. 6 tiles at roughly 40% of the old handbill
  //                        height instead of 3 large ones.
  //   "Show me three more" → a Shuffle control. Ryan: the literal phrasing was
  //                        the problem; a refresh should read as a refresh.
  //   Artists grid    → gone entirely ("unusable"). Collectors expect a
  //                        linear list of recordings, so that is what the page
  //                        is now, sortable and filterable.
  //   Genre pills        → folded into a real filter bar alongside quality and
  //                        source (Ryan), so the three filters compose.
  //
  // 2026-08-24: "Random Top Shows" renamed again → "The Top Shelf" (Ryan). On
  // This Day removed for now (Ryan: may come back later) — otdHtml and its
  // fetch are gone, not just hidden; CSS for it is left in main.css since it
  // costs nothing unused. The shelf itself no longer wraps to a second row at
  // narrow widths — .tops-grid is a horizontal-scroll strip now, with
  // explicit left/right nav buttons (_wireTopsNav / _updateTopsNav below).
  //
  // `rows` is the same flattened array the List view builds — no extra request.
  const BROWSE_TOP_N = 6

  // The flat list's own endless scroll (2026-08-24, Ryan — was rendering all
  // ~580 rows to the DOM on every Library load, which is what made it slow).
  // This pages CLIENT-SIDE over the already-fetched, already-filtered/sorted
  // `_browseRows` — no extra network round trip on scroll, unlike Recently
  // Added's server-paged endless scroll (RECENT_INITIAL/RECENT_PAGE above).
  // That's deliberate here: Browse's filters/sort need the FULL dataset in
  // memory to compute correct counts and orderings (the "132 of 580
  // recordings" subtitle, A–Z across the whole library, etc.) — paginating
  // the fetch itself would mean re-deriving those from a partial set. The
  // actual fetch was the slow part (see the N+1 fix on `all_recordings()`,
  // api/artists.py — was ~2000+ per-request DB round trips, now ~7); this
  // just keeps the DOM small on top of that.
  const BROWSE_LIST_INITIAL = 16   // first paint
  const BROWSE_LIST_PAGE    = 16   // each subsequent reveal, scrolled into view

  let _browseRows = []
  let _browseFilters = { quality: 'any', source: 'any', genre: 'any' }
  // Recently added, not A–Z (Ryan, 2026-09-02). What a collector wants on
  // opening a library they add to constantly is what arrived since last time;
  // alphabetical is a lookup order, and lookup is what Search is for.
  let _browseSort = 'added'
  let _browseVisibleCount = 0
  let _browseListIO = null

  function _browseFilterOptions(rows) {
    // Options are derived from the DATA, not hardcoded: the source column has
    // 22 distinct values, 14 of which appear exactly once ("MR", "AM", "OSM"…).
    // A select listing all of them is unusable, so anything rare collapses into
    // Other. Deriving it also means the list maintains itself as the library
    // grows — and it surfaces the near-duplicates worth cleaning up
    // ("DVB-S" and "DVBS" are both present today).
    const RARE = 5
    const count = (get) => {
      const m = new Map()
      rows.forEach(r => { const v = get(r); if (v) m.set(v, (m.get(v) || 0) + 1) })
      return m
    }
    const sources = [...count(r => r.source).entries()]
      .sort((a, b) => b[1] - a[1])
    const common = sources.filter(([, n]) => n >= RARE)
    const rare   = sources.filter(([, n]) => n < RARE)
    const genres = [...count(r => r.genre).entries()].sort((a, b) => b[1] - a[1])
    return { common, rare, genres }
  }

  function _browseApply(rows) {
    const f = _browseFilters
    const rare = _browseRareSources || new Set()
    let out = rows.filter(r => {
      if (f.quality === 'top'   && !['A', 'A+'].includes(r.quality)) return false
      if (f.quality === 'graded' && !r.quality) return false
      if (f.genre !== 'any' && r.genre !== f.genre) return false
      if (f.source === 'any') return true
      if (f.source === '__none')  return !r.source
      if (f.source === '__other') return !!r.source && rare.has(r.source)
      return r.source === f.source
    })
    // Nulls-last regardless of direction (compareByDate) — a dateless
    // studio row never jumps to the front of "Newest".
    if (_browseSort === 'az')      out = out.slice().sort((a, b) =>
      (a.artist || '').localeCompare(b.artist || '') || compareByDate(a, b, false))
    if (_browseSort === 'newest')  out = out.slice().sort((a, b) => compareByDate(a, b, true))
    if (_browseSort === 'oldest')  out = out.slice().sort((a, b) => compareByDate(a, b, false))
    if (_browseSort === 'added')   out = out.slice().sort((a, b) =>
      String(b.created_at || '').localeCompare(String(a.created_at || '')))
    return out
  }

  // "20 tracks · 72 min" -- one wording for every row list (2026-10-01).
  function _rowSizeText(r) {
    const n = r.track_count || 0
    const mins = r.duration_sec ? Math.round(r.duration_sec / 60) : 0
    return [n ? `${n} track${n === 1 ? '' : 's'}` : '', mins ? `${mins} min` : ''].filter(Boolean).join(' · ')
  }

  function _browseRowHtml(r) {
    const id = recIdentity(r)
    const initials = String(r.artist || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    const loc = fmtLocation(r.city, r.state, r.country)
    const c = r.genre_color || 'var(--t2)'
    // The photo, where there is one (Ryan, 2026-09-02 — it had always drawn
    // initials). 103 of 184 artists have one; the initials square is the
    // NORMAL case for the rest, so it stays exactly as it was rather than
    // becoming a broken-image placeholder. `image_id` rides on the artist
    // in /api/artists/all-recordings, added the same day.
    // Recording image first, then the artist photo, then initials
    // (Ryan, 2026-10-01: the square is the recording's image).
    const av = r.image_url
      ? `<img class="brow-av brow-av--img" src="${esc(r.image_url)}" alt="" loading="lazy">`
      : `<span class="brow-av">${esc(initials)}</span>`
    // Column order (Ryan, 2026-10-01): Image, Artist, Date, Venue, Location
    // left; Rating, Source right. Genre spine removed. Source and rating
    // are reserved cells, always emitted even when empty, so a graded row
    // is never wider than an ungraded one (2026-09-02).
    // Studio: the venue cell carries the title, location stays empty.
    const dateText  = id.isStudio ? (id.dateText || '—') : (handbillDate(r.start_year, r.start_month, r.start_day) || '—')
    const venueText = id.isStudio ? id.lead : (r.venue || '(unknown venue)')
    const locText   = id.isStudio ? '' : loc
    return `
      <a class="brow" href="#/recording/${r.id}" data-rec-id="${r.id}" style="--genre-fg:${esc(c)}">
        ${av}
        <span class="brow-perf">${esc(r.artist || '')}</span>
        <span class="brow-date">${esc(dateText)}</span>
        <span class="brow-venue">${esc(venueText)}</span>
        <span class="brow-loc">${esc(locText)}</span>
        <span class="brow-size">${esc(_rowSizeText(r))}</span>
        <span class="brow-grade">${id.isStudio ? '' : (r.quality ? esc(r.quality) : '')}</span>
        <span class="brow-srccell">${id.isStudio ? '' : (r.source ? `<span class="brow-src">${esc(r.source)}</span>` : '')}</span>
      </a>`
  }

  // Renders the first BROWSE_LIST_INITIAL rows of the current filter/sort
  // result and resets the endless-scroll furniture. Called on every filter,
  // sort, or Clear change — a changed result set always restarts at the top
  // of its own list, same as scrolling a fresh page back to the start.
  function _browseDrawList() {
    const listEl = document.getElementById('browse-rows')
    const cntEl  = document.getElementById('browse-count')
    if (!listEl) return
    _browseListIO?.disconnect()
    const rows = _browseApply(_browseRows)
    cntEl.textContent = `${rows.length} of ${_browseRows.length} recordings`
    if (!rows.length) {
      listEl.innerHTML = `<div class="empty-state" style="min-height:120px">
           <div class="empty-title">Nothing matches these filters</div>
           <div class="empty-sub">Widen one of them. Quality is the narrowest, since only
             ${_browseRows.filter(r => r.quality).length} of ${_browseRows.length} recordings are graded.</div>
         </div>`
      _browseSetMoreFurniture(false)
      return
    }
    _browseVisibleCount = Math.min(BROWSE_LIST_INITIAL, rows.length)
    listEl.innerHTML = rows.slice(0, _browseVisibleCount).map(_browseRowHtml).join('')
    wireRecordingRows(listEl)
    _browseSetMoreFurniture(_browseVisibleCount < rows.length)
  }

  // Appends the next BROWSE_LIST_PAGE rows from the CURRENT filter/sort
  // result (recomputed fresh, not cached — a filter/sort change always goes
  // through _browseDrawList first, which is the only place `rows` here can
  // legitimately be stale against, and that path resets scroll to the top
  // anyway). New rows are wired in a detached wrapper before insertion so a
  // repeat scroll never double-wires the rows already in the DOM.
  function _browseLoadMoreRows() {
    const listEl = document.getElementById('browse-rows')
    if (!listEl) return
    const rows = _browseApply(_browseRows)
    const next = rows.slice(_browseVisibleCount, _browseVisibleCount + BROWSE_LIST_PAGE)
    if (!next.length) { _browseSetMoreFurniture(false); return }
    const wrap = document.createElement('div')
    wrap.innerHTML = next.map(_browseRowHtml).join('')
    wireRecordingRows(wrap)
    while (wrap.firstChild) listEl.appendChild(wrap.firstChild)
    _browseVisibleCount += next.length
    _browseSetMoreFurniture(_browseVisibleCount < rows.length)
  }

  // Sentinel + IntersectionObserver, same idiom as Recently Added's endless
  // scroll (see renderRecentView) — root is the scrolling main content pane,
  // a generous rootMargin so the next batch is ready before the reader
  // actually reaches the bottom.
  function _browseSetMoreFurniture(hasMore) {
    const moreEl = document.getElementById('browse-more')
    if (!moreEl) return
    _browseListIO?.disconnect()
    if (!hasMore) { moreEl.innerHTML = ''; return }
    moreEl.innerHTML = '<div class="browse-sentinel" id="browse-sentinel"></div>'
    const sentinel = document.getElementById('browse-sentinel')
    _browseListIO = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) _browseLoadMoreRows()
    }, { root: mainContent, rootMargin: '400px' })
    _browseListIO.observe(sentinel)
  }

  let _browseRareSources = new Set()

  async function renderBrowseModules(mountEl, rows) {
    _libRecommendedReroll = 0
    _browseRows = rows || []
    _browseFilters = { quality: 'any', source: 'any', genre: 'any' }
    _browseSort = 'added'

    // How many fit RIGHT NOW, measured off the mount point before the strip
    // is painted. BROWSE_TOP_N is only the floor for a window so narrow that
    // the measurement is not worth trusting.
    const wantTops = Math.max(BROWSE_TOP_N, _topsFit(_topsTrackWidth()))
    const [tops, collections] = await Promise.all([
      API.recordings.recommended(wantTops, 0).catch(() => []),
      API.collections.list().catch(() => []),
    ])

    const { common, rare, genres } = _browseFilterOptions(_browseRows)
    _browseRareSources = new Set(rare.map(([v]) => v))
    const graded = _browseRows.filter(r => r.quality).length

    // Left nav starts hidden — .hidden comes off in _updateTopsNav() once
    // scrolled. Right nav is unconditional (see _updateTopsNav's comment).
    // Both use real Lucide glyphs from ICONS, not chevronIcon() — that's the
    // separate small-caret helper used for dropdown/tree carets elsewhere in
    // the app, and reads as a mismatched icon family next to the rest of
    // this Lucide-built page (Ryan, 2026-08-24: "we do not need to use
    // inconsistent chevrons that are not from our icon package").
    // The bin starts here, not at zero — the first paint has already dealt
    // `tops.length` records off the sequence. Length of the RESULT, not of the
    // request: a pool with fewer records than the window has room for returns
    // short, and counting the ask would leave the cursor past records never
    // shown.
    _topsShown = tops.length
    _topsNext  = null

    const topsHtml = tops.length ? `
      <section class="lib-module lib-module--tops" id="lib-mod-tops">
        <div class="lib-module-head lib-module-head--quiet">
          <h2>The Top Shelf</h2>
          <button type="button" class="lib-reroll-btn" id="browse-shuffle">
            ${icon('rotate-cw')} Shuffle
          </button>
        </div>
        <div class="tops-shelf">
          <button type="button" class="tops-nav tops-nav-l hidden" id="tops-nav-l" aria-label="Scroll left">${icon('chevron-left', 'tops-nav-ic')}</button>
          <div class="tops-grid" id="tops-grid">${tops.map(_topTileHtml).join('')}</div>
          <button type="button" class="tops-nav tops-nav-r" id="tops-nav-r" aria-label="Scroll right">${icon('chevron-right', 'tops-nav-ic')}</button>
        </div>
      </section>` : ''

    const opt = (v, label, n) =>
      `<option value="${esc(v)}">${esc(label)}${n != null ? ` (${n})` : ''}</option>`

    // The shelf goes ABOVE the "Browse My Library" header (Ryan, 2026-09-02).
    // It is the one part of this page you look AT rather than read past, so it
    // gets the top of the window; the H1 becomes the label on the list below
    // it, which is what it actually names. Its own mount point, because it now
    // sits outside the module column entirely — renderLibraryView() lays out
    // #lib-topshelf, then the header, then this.
    const topsMount = document.getElementById('lib-topshelf')
    if (topsMount) topsMount.innerHTML = topsHtml

    mountEl.innerHTML = `
      ${topsMount ? '' : topsHtml}

      <section class="lib-module" id="lib-mod-all">
        <div class="browse-bar">
          <div class="browse-sorts" id="browse-sorts">
            <button class="sortb" data-sort="az">A–Z</button>
            <button class="sortb" data-sort="newest">Newest</button>
            <button class="sortb" data-sort="oldest">Oldest</button>
            <button class="sortb on" data-sort="added">Recently added</button>
          </div>
          <div class="browse-filters">
            <label class="bfilter">Quality
              <select id="bf-quality">
                ${opt('any', 'Any')}
                ${opt('top', 'A and A+', _browseRows.filter(r => ['A','A+'].includes(r.quality)).length)}
                ${opt('graded', 'Graded only', graded)}
              </select>
            </label>
            <label class="bfilter">Source
              <select id="bf-source">
                ${opt('any', 'Any')}
                ${common.map(([v, n]) => opt(v, v, n)).join('')}
                ${rare.length ? opt('__other', 'Other', rare.reduce((n, x) => n + x[1], 0)) : ''}
                ${opt('__none', 'Unknown', _browseRows.filter(r => !r.source).length)}
              </select>
            </label>
            <label class="bfilter">Genre
              <select id="bf-genre">
                ${opt('any', 'Any')}
                ${genres.map(([v, n]) => opt(v, v, n)).join('')}
              </select>
            </label>
            <button type="button" class="bfilter-clear" id="bf-clear">Clear</button>
          </div>
          <span class="browse-count" id="browse-count"></span>
        </div>
        <div class="brows" id="browse-rows"></div>
        <div class="browse-more" id="browse-more"></div>
      </section>

      ${collections.length ? `
      <section class="lib-module" id="lib-mod-collections">
        <div class="lib-module-head"><h2>Collections</h2></div>
        <div class="col-strip">${collections.map(_colStripHtml).join('')}</div>
      </section>` : ''}
    `

    _browseDrawList()
    _wireTopsNav()
    _observeTops()

    document.getElementById('browse-shuffle')?.addEventListener('click', _refreshRecommendedModule)
    mountEl.querySelectorAll('#browse-sorts .sortb').forEach(b =>
      b.addEventListener('click', () => {
        _browseSort = b.dataset.sort
        mountEl.querySelectorAll('#browse-sorts .sortb').forEach(x => x.classList.toggle('on', x === b))
        _browseDrawList()
      }))
    const onFilter = (id, key) =>
      mountEl.querySelector(id)?.addEventListener('change', e => {
        _browseFilters[key] = e.target.value
        _browseDrawList()
      })
    onFilter('#bf-quality', 'quality')
    onFilter('#bf-source', 'source')
    onFilter('#bf-genre', 'genre')
    mountEl.querySelector('#bf-clear')?.addEventListener('click', () => {
      _browseFilters = { quality: 'any', source: 'any', genre: 'any' }
      ;['#bf-quality', '#bf-source', '#bf-genre'].forEach(sel => {
        const el = mountEl.querySelector(sel); if (el) el.value = 'any'
      })
      _browseDrawList()
    })
  }

  // Top Shelf tile (art squared up + overlay text, 2026-08-24, Ryan). The art
  // area is a square (aspect-ratio 1:1 on .top-art) rather than the old 76px
  // strip — tall enough to actually show traditional album cover art once
  // that's supported, not just an artist headshot crop. Artist photo
  // when there is one (312 of 580 shows have one), a genre-colour field with
  // initials when there is not — the no-photo case is the NORMAL case for
  // 46% of the library, so it has to look deliberate rather than like a
  // failed image, hence the initials rather than a blank/broken square.
  //
  // Text (artist, venue, date · grade) now lives in .top-overlay, a
  // gradient scrim over the BOTTOM of the art rather than a separate white
  // panel below it — the image is the whole tile now, and the scrim exists
  // purely for legibility (dark gradient works over both a photo and a
  // genre-colour field, light or saturated).
  // The Top Shelf draws from live recordings only (server-side filter), so
  // this stays live-shaped -- routed through recIdentity anyway so a stray
  // studio row (a bad fetch, a future relaxation of that filter) never shows
  // a venue or "undefined" rather than just quietly showing less.
  function _topTileHtml(r) {
    const id = recIdentity(r)
    const initials = String(r.artist || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    const c = r.genre_color || 'var(--bg-4)'
    const art = r.image_id
      ? `<img class="top-img" src="${API.artists.imageUrl(r.image_id)}" alt="" loading="lazy">`
      : `<span class="top-initials">${esc(initials)}</span>`
    return `
      <a class="top-tile" href="#/recording/${r.id}" style="--genre-fg:${esc(c)}">
        <span class="top-art">${art}</span>
        <span class="top-overlay">
          <span class="top-perf">${esc(id.isStudio ? id.lead : (r.artist || ''))}</span>
          ${(!id.isStudio && r.venue) ? `<span class="top-venue">${esc(r.venue)}</span>` : ''}
          <span class="top-meta">${[
            id.isStudio ? id.dateText : (handbillDate(r.start_year, r.start_month, r.start_day) || ''),
            // Source between the date and the grade (Ryan, 2026-09-02). On a
            // shelf of A/A+ shows the grade barely separates them; SBD vs AUD
            // is the thing that actually decides whether you pull the record
            // out of the bin.
            id.isStudio ? '' : (r.source || ''),
            id.isStudio ? '' : (r.quality || ''),
          ].filter(Boolean).map(esc).join(' · ')}</span>
        </span>
      </a>`
  }

  // Album tile (Studio Records spec v1, section 7) -- the Top Shelf's own
  // art/overlay treatment reused for the Albums page grid and the Artist
  // page's Albums strip (`.album-tile` in main.css sizes it for each of
  // those two layouts; `.top-tile`/`.top-art`/`.top-overlay` do everything
  // else, unchanged). Initials come from the TITLE, not the artist -- an
  // album is named by its title everywhere (spec, principle 2) -- falling
  // back to the artist's initials only when a studio record has no title.
  function _albumTileHtml(r) {
    const id = recIdentity(r)
    const initials = recInitials(r)
    const c = r.genre_color || 'var(--bg-4)'
    const art = r.image_url
      ? `<img class="top-img" src="${esc(r.image_url)}" alt="" loading="lazy">`
      : `<span class="top-initials">${esc(initials)}</span>`
    return `
      <a class="top-tile album-tile" href="#/recording/${r.id}" style="--genre-fg:${esc(c)}">
        <span class="top-art">${art}</span>
        <span class="top-overlay">
          <span class="top-perf">${esc(id.lead)}</span>
          ${id.sub ? `<span class="top-venue">${esc(id.sub)}</span>` : ''}
          <span class="top-meta">${esc(id.dateText)}</span>
        </span>
      </a>`
  }

  // Collections carry the genre colours of what they hold, so a four-item
  // section still reads as deliberate rather than sparse.
  function _colStripHtml(c) {
    return `
      <a class="col-card" href="#/collection/${c.id}">
        <span class="col-card-n">${esc(c.name)}</span>
        <span class="col-card-c">${c.recording_count || 0} recording${c.recording_count === 1 ? '' : 's'}</span>
      </a>`
  }

  // Flatten /api/artists/all-recordings to one row per recording — shared by
  // Browse (live-only) and the Albums page (studio-only), so both read the
  // exact same shape off the exact same fetch (Studio Records spec v1,
  // section 9: "no new endpoint"). Already ordered by artist (backend) then
  // chronologically old→new; genre/genre_color ride on the ARTIST (one genre
  // per act) and colour the spine as well as driving the genre filters.
  function _flattenLibraryRows(allArtists) {
    return allArtists.flatMap(artist =>
      artist.performances.flatMap(p =>
        p.recordings.map(r => ({
          id: r.id, artist: artist.artist_name, artist_id: artist.artist_id,
          genre: artist.genre, genre_color: artist.genre_color,
          image_id: artist.image_id,
          start_year: p.start_year, start_month: p.start_month, start_day: p.start_day,
          venue: p.venue_name, city: p.city, state: p.state, country: p.country,
          source: r.source, quality: r.quality,
          is_complete: r.is_complete,
          track_count: r.track_count, duration_sec: r.duration_sec, created_at: r.created_at,
          kind: r.kind, title: r.title, image_url: r.image_url,
          mb_label: r.mb_label, mb_catalog_number: r.mb_catalog_number,
        }))
      )
    )
  }

  async function renderLibraryView() {
    setActiveNav('library')
    setActiveArtist(null)
    state.selectedArtist = null
    setNavCurrent('Library')
    setLoading()

    let allArtists
    try {
      allArtists = await API.artists.allRecordings()
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Failed to load library</div></div>`)
      return
    }

    // Browse is live-only (Studio Records spec v1, section 7): its filters,
    // sorts and grade logic only make sense for a show. Studio rows still
    // ride along in the fetch — they just never reach renderBrowseModules.
    const allRows   = _flattenLibraryRows(allArtists)
    const rows      = allRows.filter(r => r.kind !== 'studio')
    const hasStudio = allRows.some(r => r.kind === 'studio')

    // Zero recordings: two lines and nothing else (Ryan, testing feedback).
    // Hidden the instant a recording exists, and never shown while a Bulk
    // Ingest run is in flight -- that has its own page at #/bulk-ingest, and
    // showing this underneath it would read as two contradictory states.
    // A library with albums but no live recordings routes to Albums instead
    // (Studio Records spec v1, section 7) — Browse's own message is about
    // importing music, which would be wrong for a collector whose ingest
    // already worked.
    const bulkIngestInFlight = state.bulkIngest
      && (state.bulkIngest.status === 'running' || state.bulkIngest.status === 'paused')
    if (!rows.length && !bulkIngestInFlight) {
      if (hasStudio) {
        // Same reasoning as route()'s admin bounce: this redirect is not a
        // real navigation the user asked for, so it must not push a
        // history/nav-stack entry, or Back would loop between the two.
        _navMoving  = false
        _navReplace = true
        window.location.hash = '#/albums'
        return
      }
      // Welcome copy is Ryan's (2026-10-02). Top-aligned, not centred: it is
      // the first thing a new library shows, so it reads as a greeting.
      setMainHTML(`
        <div class="welcome-empty">
          <h1 class="welcome-empty-title">Welcome to Trellis!</h1>
          <p class="welcome-empty-body">Add some <a href="#/ingest">Recordings</a> to get started,
            or check out the <a href="#/archive/lma">${icon('landmark', 'welcome-empty-ic')}Live Music Archive</a>
            to find stuff to download.</p>
          <p class="welcome-empty-body">Got an invite to join a friend's library? Choose "Join a Library" up top.</p>
        </div>`)
      return
    }

    // The "Library" H1 and the "580 recordings · 184 artists" subtitle were
    // gone for a while (Ryan, 2026-08-23: "it's not important enough") because
    // the header was just the Browse/List toggle. The toggle is retired
    // (2026-08-24), so the page gets an H1 back — "Browse My Library".
    const headerHtml = `
      <div class="artist-header">
        <div class="artist-header-row">
          <h1>Browse Live Recordings</h1>
        </div>
      </div>`

    setMainHTML(`<div id="lib-topshelf"></div>${headerHtml}`
              + `<div class="lib-modules" id="lib-modules-mount"></div>`)
    await renderBrowseModules(document.getElementById('lib-modules-mount'), rows)
  }

  /** Recently Added — virtual view, the N most recently ingested recordings.
   *  Not a collection; just a live query, always exactly correct. */
  // Recently Added page size. 20 is about a screenful of row cards; the full
  // 50 is one click away and already in memory.
  const RECENT_INITIAL = 25   // first page (Ryan, 2026-08-23)
  const RECENT_PAGE    = 25   // each subsequent page, fetched on scroll

  async function renderRecentView() {
    setActiveNav('recent')
    setActiveArtist(null)
    state.selectedArtist = null
    setNavCurrent('Recently Added')
    setLoading()

    let rows
    try {
      rows = await API.recordings.recent(RECENT_INITIAL, { card: true })
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Failed to load recent recordings</div></div>`)
      return
    }

    // No count in the subtitle any more: it used to say "50 most recently
    // added", which was only ever the size of the fetch, not a fact about the
    // library. With paging it would be a number that changes as you scroll.
    const headerHtml = `
      <div class="artist-header">
        <div class="artist-header-row">
          <h1>Recently Added</h1>
        </div>
      </div>`

    // This is a full version of the Browse page's Recently Added module
    // (Ryan, 2026-08-07) — NOT a call to renderBrowseModules(), which is the
    // entire global dashboard and would make "Recently Added" show everything
    // except a longer list of recently added recordings. Same class of bug as
    // the Collection view's.
    setMainHTML(`${headerHtml}
      <div class="brows" id="recent-rowcards"></div>
      <div class="recent-more" id="recent-more"></div>`)

    // Endless scroll (Ryan, 2026-08-23) — was a hardcoded 50 with a
    // "Show all" button. A sentinel below the list fetches the next page
    // when it comes into view; the observer disconnects when the server
    // returns a short page, which is how we know we have reached the end.
    const listEl = document.getElementById('recent-rowcards')
    const moreEl = document.getElementById('recent-more')
    listEl.innerHTML = rows.map(_recentRowHtml).join('')
    // Right-click to file into a collection, as the old row cards had.
    listEl.addEventListener('contextmenu', e => {
      const el = e.target.closest('[data-rec-id]')
      if (!el) return
      e.preventDefault()
      openAddToCollectionMenu(parseInt(el.dataset.recId), e.clientX, e.clientY)
    })

    let loading = false, done = rows.length < RECENT_INITIAL
    moreEl.innerHTML = done ? '' : '<div class="recent-sentinel" id="recent-sentinel"></div>'

    const loadMore = async () => {
      if (loading || done) return
      loading = true
      moreEl.innerHTML = '<div class="recent-loading">Loading more…</div>'
      let next = []
      try {
        next = await API.recordings.recent(RECENT_PAGE, { card: true, offset: listEl.children.length })
      } catch (_) {
        moreEl.innerHTML = '<div class="recent-loading">Could not load more.</div>'
        loading = false
        return
      }
      listEl.insertAdjacentHTML('beforeend', next.map(_recentRowHtml).join(''))
      // A short page means the end. Checking the RETURNED count rather than
      // a total avoids a second endpoint and cannot disagree with it.
      done = next.length < RECENT_PAGE
      moreEl.innerHTML = done ? '' : '<div class="recent-sentinel" id="recent-sentinel"></div>'
      loading = false
      if (!done) observe()
    }

    let io = null
    const observe = () => {
      const sentinel = document.getElementById('recent-sentinel')
      if (!sentinel) return
      io?.disconnect()
      // rootMargin starts the fetch before the sentinel is actually visible,
      // so the next rows are usually there by the time the reader arrives.
      io = new IntersectionObserver(entries => {
        if (entries.some(e => e.isIntersecting)) loadMore()
      }, { root: mainContent, rootMargin: '400px' })
      io.observe(sentinel)
    }
    observe()
  }

  /** Albums page (Studio Records spec v1, section 7) — a grid of studio
   *  recordings, reached from the nav only when at least one exists. Built
   *  client-side off the same /api/artists/all-recordings fetch Browse uses
   *  (no new endpoint) — _flattenLibraryRows() is the shared shape. */
  async function renderAlbumsView() {
    setActiveNav('albums')
    setActiveArtist(null)
    state.selectedArtist = null
    setNavCurrent('Albums')
    setLoading()

    let allArtists
    try {
      allArtists = await API.artists.allRecordings()
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Failed to load library</div></div>`)
      return
    }

    const rows = _flattenLibraryRows(allArtists).filter(r => r.kind === 'studio')

    const headerHtml = `
      <div class="artist-header">
        <div class="artist-header-row">
          <h1>Albums</h1>
        </div>
      </div>`

    const { genres } = _browseFilterOptions(rows)
    const opt = (v, label) => `<option value="${esc(v)}">${esc(label)}</option>`

    // Same bar and row grid as Live Recordings, minus Quality and Source
    // (Ryan, 2026-10-01). A-Z is artist then year: the discography order the
    // backend already returns, so it needs no re-sort.
    setMainHTML(`${headerHtml}
      <div class="browse-bar">
        <div class="browse-sorts" id="albums-sorts">
          <button class="sortb on" data-sort="az">A–Z</button>
          <button class="sortb" data-sort="newest">Newest</button>
          <button class="sortb" data-sort="oldest">Oldest</button>
          <button class="sortb" data-sort="added">Recently added</button>
        </div>
        <div class="browse-filters">
          <label class="bfilter">Genre
            <select id="albums-genre">
              ${opt('any', 'Any')}
              ${genres.map(([v]) => opt(v, v)).join('')}
            </select>
          </label>
          <button type="button" class="bfilter-clear" id="albums-clear">Clear</button>
        </div>
        <span class="browse-count" id="albums-count"></span>
      </div>
      <div class="brows" id="albums-rows"></div>`)

    let sort = 'az', genreFilter = 'any'
    function draw() {
      let out = rows.filter(r => genreFilter === 'any' || r.genre === genreFilter)
      if (sort === 'newest') out = out.slice().sort((a, b) => compareByDate(a, b, true))
      if (sort === 'oldest') out = out.slice().sort((a, b) => compareByDate(a, b, false))
      if (sort === 'added')  out = out.slice().sort((a, b) =>
        String(b.created_at || '').localeCompare(String(a.created_at || '')))
      document.getElementById('albums-count').textContent = `${out.length} of ${rows.length} albums`
      const list = document.getElementById('albums-rows')
      list.innerHTML = out.length
        ? out.map(_albumRowHtml).join('')
        : `<div class="empty-state" style="min-height:120px"><div class="empty-title">Nothing matches these filters</div></div>`
    }
    draw()

    document.querySelectorAll('#albums-sorts .sortb').forEach(b =>
      b.addEventListener('click', () => {
        sort = b.dataset.sort
        document.querySelectorAll('#albums-sorts .sortb').forEach(x => x.classList.toggle('on', x === b))
        draw()
      }))
    const genreSel = document.getElementById('albums-genre')
    genreSel?.addEventListener('change', e => { genreFilter = e.target.value; draw() })
    document.getElementById('albums-clear')?.addEventListener('click', () => {
      genreFilter = 'any'
      if (genreSel) genreSel.value = 'any'
      draw()
    })
  }

  // Album row (2026-10-01): the Live Recordings grid with album facts in the
  // live slots. Image, Artist, Year, Title, then "N tracks · N min", then the
  // release (label · catalog number) when MusicBrainz knows it.
  function _albumRowHtml(r) {
    const id = recIdentity(r)
    const initials = recInitials(r)
    const c = r.genre_color || 'var(--t2)'
    const av = r.image_url
      ? `<img class="brow-av brow-av--img" src="${esc(r.image_url)}" alt="" loading="lazy">`
      : `<span class="brow-av">${esc(initials)}</span>`
    const size = _rowSizeText(r)
    const release = [r.mb_label, r.mb_catalog_number].filter(Boolean).join(' · ')
    return `
      <a class="brow brow--album" href="#/recording/${r.id}" data-rec-id="${r.id}" style="--genre-fg:${esc(c)}">
        ${av}
        <span class="brow-perf">${esc(r.artist || '')}</span>
        <span class="brow-date">${esc(id.dateText || '—')}</span>
        <span class="brow-title">${esc(r.title || '')}</span>
        <span class="brow-size">${esc(size)}</span>
        <span class="brow-release">${esc(release)}</span>
      </a>`
  }

  /** Artist page — editable info + member Musicians + recording catalog. */
  async function renderArtistView(artistId) {
    setActiveNav('library')
    setActiveArtist(artistId)
    setLoading()

    let artist, performances
    try {
      [artist, performances] = await Promise.all([
        API.artists.get(artistId),
        API.artists.recordings(artistId),
      ])
    } catch (e) {
      // Likely an artist that was pruned after reassignment — heal the stale
      // sidebar so the phantom entry disappears.
      invalidateDims('artists')
      setMainHTML(`<div class="empty-state"><div class="empty-title">This artist no longer exists</div><div class="empty-sub">It may have been removed after its recordings were reassigned.</div></div>`)
      return
    }
    setNavCurrent(artist.name)
    // Whatever page brought us here (2026-07-23 generic mechanism — see
    // state.navBack) — shown as a "← Back" breadcrumb below, replacing the
    // old one-shot recFrom that only covered arriving via a Recording's ↗
    // nav-link icon. Read directly, not consumed/cleared: route() already
    // refreshes it on every real navigation, and a same-page reload (e.g.
    // after an inline edit) never touches it.
    const navBack = state.navBack

    state.selectedArtist = artist
    // Local, mutable copy of the roster — edited in place, persisted on each change.
    // Each member also carries `.stints` (date-bounded tenures; usually one
    // unbounded row = "always a member") — see the stint editor below.
    let members = (artist.members || []).map(m => ({ id: m.id, name: m.name, stints: m.stints || [] }))
    let defaultPersonnelMode = artist.default_personnel_mode || 'inherit'
    let expandedMemberId = null   // which member's stint editor drawer is open, if any

    const withRecs = performances.filter(p => (p.recordings || []).length > 0)

    // Flat one row per recording, oldest→newest. No year headers (one artist).
    // Nulls-last (compareByDate): a dateless studio row goes at the end
    // rather than sorting to the front alongside "no date at all" = 0.
    const ordered = withRecs.slice().sort((a, b) => compareByDate(a, b, false))
    const perfRows = ordered.flatMap(p =>
      p.recordings.map(r => ({
        id: r.id, artist: p.artist_name,
        start_year: p.start_year, start_month: p.start_month, start_day: p.start_day,
        venue: p.venue_name, city: p.city, state: p.state, country: p.country,
        source: r.source, quality: r.quality,
        is_complete: r.is_complete,
        track_count: r.track_count, duration_sec: r.duration_sec, created_at: r.created_at,
        kind: r.kind, title: r.title, image_url: r.image_url,
        genre: artist.genre ? artist.genre.name : null,
        genre_color: artist.genre ? artist.genre.color : null,
      }))
    )
    const rowsHtml = perfRows.map(r => flatRowHtml(r, false)).join('')

    // Live vs studio (Studio Records spec v1, section 7): the Recordings tab
    // stays live-only; the hero stat and the Albums strip read the split.
    const liveRows        = perfRows.filter(r => r.kind !== 'studio')
    const studioRows      = perfRows.filter(r => r.kind === 'studio')
    const totalRecordings = liveRows.length
    const studioCount     = studioRows.length

    const descText = artist.bio && artist.bio.trim()

    const mbf = artist.musicbrainz || {}

    // ── Hero stat: recordings only (Ryan, 2026-08-07) ────────────────────────
    // Venue count and total runtime were cut as uninteresting. Span was cut as
    // potentially INCONSISTENT with our own data — it was derived from whatever
    // dates happen to be filled in, so an act with one undated show would
    // advertise a range that contradicts the recordings listed right below it.
    // The derivations for all three are gone rather than left computed-unused.
    // Album count joins it (Studio Records spec v1, section 7) only when the
    // act has any — most acts don't, and a stat that always read "0 Albums"
    // would be worse than not being there.
    // An albums-only act (no live recordings at all) shows only the Albums
    // stat -- a "0 Recordings" reading next to it would misdescribe a
    // library that in fact has music, just none of it live.
    const stats = (totalRecordings === 0 && studioCount)
      ? []
      : [[totalRecordings, totalRecordings === 1 ? 'Recording' : 'Recordings']]
    if (studioCount) stats.push([studioCount, studioCount === 1 ? 'Album' : 'Albums'])

    // MusicBrainz one-liner: type · origin · active years, whichever exist.
    const mbBits = [
      mbf.type ? `<b>${esc(mbf.type)}</b>` : '',
      mbf.area ? esc(mbf.area) : '',
      mbf.begin ? `active <b>${esc(mbf.begin)}${mbf.end ? '–' + esc(mbf.end) : '–present'}</b>` : '',
    ].filter(Boolean).join(' · ')

    const photoCount = (artist.images || []).length

    setMainHTML(entityShellHtml({
      navBack,
      portrait: '<div id="pp-hero-portrait"></div>',
      title: esc(artist.name),
      titleId: 'pp-name',
      titleEditable: true,
      // ONE genre element, and it is the editable one. A static colour pill
      // alongside it (as first built) showed the same value twice and only one
      // of them responded to a click.
      chips: `<span class="pp-editable pp-genre-field" id="pp-genre"
                    style="--genre-fg:${esc((artist.genre && artist.genre.color) || 'var(--t2)')}"
                    title="Click to edit"></span>`
           + (mbBits ? `<span class="pp-hero-fact">${mbBits}</span>` : ''),
      stats,
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="pp-delete" title="Delete artist">Delete</button>`,
      // Recordings first and default across all five dimension pages (Ryan,
      // 2026-09-01). On an artist that is emphatically what you came for;
      // Overview led with Members and a Description that is empty on most acts.
      tabs: [
        { id: 'recordings', label: 'Recordings', count: totalRecordings, active: true,
          // Albums strip above the live list (Studio Records spec v1,
          // section 7) — release order (year ascending, nulls last), only
          // when the act has any. When an act has ONLY albums the live list
          // is skipped entirely rather than printing its own empty state
          // (spec, section 7: "no empty-state text for the live list").
          html: (studioCount ? `
            <div class="pp-sec">Albums</div>
            <div class="albums-strip">${studioRows.slice().sort((a, b) => compareByDate(a, b, false)).map(_albumTileHtml).join('')}</div>` : '')
            + (liveRows.length || !studioCount
                ? recordingsPaneHtml(liveRows, { mountId: 'rec-table-artist' })
                : '') },
        { id: 'about',      label: 'About', html: `
            <!-- Members first (Ryan, 2026-08-07): who the act IS comes before
                 prose about it, and Description is empty on most artists so
                 leading with it opened the page on a placeholder. -->
            <div class="pp-sec">Members</div>
            <div class="pp-musicians" id="pp-musicians"></div>
            <div class="pp-stint-editor" id="pp-stint-editor" style="display:none"></div>
            <div class="lx-slot" id="pp-lx-members"></div>

            <!-- Abbreviation feeds the {artist_abbr} naming token (falls back
                 to lowercase initials when empty) — see app/utils/ingest.py -->
            <div class="pp-sec-row">
              <div class="pp-sec">Abbreviation</div>
            </div>
            <div class="pp-editable ${artist.abbreviation ? '' : 'pp-empty'}" id="pp-abbr" title="Click to edit">${artist.abbreviation ? esc(artist.abbreviation) : 'Add an abbreviation…'}</div>

            <!-- Biography. Lomax writes it (and Restore previous undoes that); it is
                 still click-to-edit. The Restore link is painted by the Lomax view. -->
            <div class="pp-sec-row">
              <div class="pp-sec">Biography</div>
              <span id="pp-lx-bio"></span>
            </div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="pp-desc" title="Click to edit">${descText ? esc(artist.bio) : 'Add a description\u2026'}</div>

            <div class="pp-block">
              <h2 class="pp-block-title">MusicBrainz</h2>
              <div class="pp-block-hint">Links this act to its MusicBrainz entry, so future imports know where to look for information about it.</div>
              <div id="pp-mb"></div>
            </div>

            <!-- Reframed from "Resources" to sources the ENRICHMENT JOBS should
                 trust. Trellis already knows to consult Wikipedia and setlist.fm;
                 what it can't know is the act-specific archive a collector
                 knows about. Ordered last because it's what you fill in AFTER
                 seeing what the automated passes missed. -->
            <div class="pp-block">
              <h2 class="pp-block-title">Trusted sources</h2>
              <div class="pp-block-hint">Sites worth trusting for this act specifically: a fan-maintained show database, an archivist's site. We already check the obvious ones, so add what we wouldn't know to look for. These are treated as sources of truth in future research and import jobs.</div>
              <div class="pp-resources" id="pp-resources"></div>
              <div class="lx-slot" id="pp-lx-resources"></div>
            </div>

            <!-- Questions and the ask bar close the page (not pinned). -->
            <div class="lx-page" id="pp-lx"></div>` },
        { id: 'photos',     label: 'Photos', count: photoCount || null,
          html: '<div id="pp-photos"></div>' },
      ],
    }))
    wireEntityShell(mainContent, navBack)

    wireRecordingRows(mainContent)
    if (liveRows.length) wireDateAddedSort(document.getElementById('rec-table-artist'), liveRows, false)

    const refreshSidebar = () => { _dimCache.artists = null; if (state.expandedDims.has('artists')) _renderDimRecords('artists') }

    // ── Inline-editable name / description ──────────────────────────────────
    async function saveField(patch) {
      try { await API.artists.update(artistId, patch); refreshSidebar() }
      catch (e) { alert('Save failed: ' + e.message) }
    }
    makeInlineEditable(document.getElementById('pp-name'), {
      get: () => artist.name,
      onSave: async v => {
        v = v.trim(); if (!v || v === artist.name) return
        artist.name = v; state.selectedArtist.name = v
        await saveField({ name: v })
      },
    })
    makeInlineEditable(document.getElementById('pp-desc'), {
      multiline: true, placeholder: 'Add a description…',
      get: () => artist.bio || '',
      onSave: async v => {
        v = v.trim(); artist.bio = v
        await saveField({ bio: v || null })
      },
    })
    makeInlineEditable(document.getElementById('pp-abbr'), {
      placeholder: 'Add an abbreviation…',
      get: () => artist.abbreviation || '',
      onSave: async v => {
        v = v.trim(); artist.abbreviation = v
        await saveField({ abbreviation: v || null })
      },
    })

    // ── Genre → picker (existing genres only) ─────────────────────────────────
    // Click-to-edit inline, exactly like the venue picker on the Recording
    // page (a custom click handler + wirePickerDropdown, not makeInlineEditable
    // — the field needs a dropdown, not a plain text input). Unlike venue/event,
    // there is no "+ Create" row: creating a genre is an explicit admin action
    // on #/genres, never a side effect of typing here (Genre design spec,
    // 2026-08-02 — the FK is the whole point, nothing may write to it implicitly).
    const genreEl = document.getElementById('pp-genre')
    function showGenre() {
      const hasGenre = !!artist.genre
      genreEl.innerHTML = hasGenre
        ? `<span class="genre-pill">${esc(artist.genre.name)}</span>`
        : `<span class="pp-empty">Add genre…</span>`
    }
    showGenre()
    genreEl?.addEventListener('click', () => {
      if (genreEl.querySelector('input')) return
      genreEl.innerHTML = `<span class="artist-picker-wrap" style="display:inline-block; min-width:160px">
        <input type="text" class="pp-inline-input" id="pp-genre-input" value="${esc(artist.genre?.name || '')}" autocomplete="off" />
        <div class="artist-dropdown" id="pp-genre-dd" style="display:none"></div></span>`
      const input = document.getElementById('pp-genre-input')
      const dd    = document.getElementById('pp-genre-dd')
      input.focus(); input.select()
      let committed = false
      const commitGenre = async ({ id, name }) => {
        if (committed) return; committed = true
        try {
          await API.artists.update(artistId, { genre_id: id || null })
          artist.genre = id ? { id, name } : null
        } catch (e) { alert('Failed: ' + e.message) }
        showGenre()
      }
      wirePickerDropdown(input, dd, API.genres.list, commitGenre)   // no createLabel → no create row
      input.addEventListener('keydown', e => {
        e.stopPropagation()
        if (e.key === 'Enter') { e.preventDefault(); const m = firstPickerResult(dd); if (m) commitGenre(m) }
        else if (e.key === 'Escape') { committed = true; showGenre() }
      })
    })

    // ── Editable Musicians (members) + per-person stint dates ──────────────────
    // A member usually has one unbounded stint ("always a member" — zero UI
    // tax, matches every pre-2026-07-18 row). Click a chip's name to expand
    // an inline drawer for real tenure dates (era lineups, second stints like
    // Mickey Hart) — see Per-Show Personnel design doc §7.6.
    async function persistMembers() { await saveField({ members: members.map(m => m.name) }) }

    async function refreshRoster() {
      // Stint mutations happen against Membership rows directly (not via the
      // plain-name-list sync), so re-fetch rather than hand-patch local state.
      const fresh = await API.artists.get(artistId)
      members = (fresh.members || []).map(m => ({ id: m.id, name: m.name, stints: m.stints || [] }))
      defaultPersonnelMode = fresh.default_personnel_mode || 'inherit'
    }

    function isUnbounded(s) {
      return !s.start_year && !s.start_month && !s.start_day && !s.end_year && !s.end_month && !s.end_day
    }

    // Members row uses the same (+) button + inline picker style as the
    // recording page's Members/Guests rows (2026-07-22) — no Guests row here,
    // guests are a per-show concept and don't apply to the act itself.
    // One member per ROW, not a run of chips (Ryan, 2026-08-07).
    //
    // The old chip row hid the entire stint feature: tenure dates only appeared
    // if you happened to click a name, and nothing suggested a name was
    // clickable. A row gives the dates somewhere to live permanently, and
    // members with no stint recorded say so explicitly — "Tenure not set" is a
    // prompt, whereas blank space is invisible.
    function memberTenure(m) {
      const stints = m.stints || []
      if (!stints.length) return null
      const fmt = s => {
        const a = [s.start_year, s.start_month, s.start_day].filter(Boolean).join('-')
        const b = [s.end_year, s.end_month, s.end_day].filter(Boolean).join('-')
        if (!a && !b) return null           // unbounded = "always a member"
        return `${a || '?'} – ${b || 'present'}`
      }
      const parts = stints.map(fmt).filter(Boolean)
      return parts.length ? parts.join(', ') : null
    }

    function renderMusicians() {
      const box = document.getElementById('pp-musicians')
      box.innerHTML =
        members.map((m, i) => {
          const tenure = memberTenure(m)
          const open = expandedMemberId === m.id
          return `
          <div class="pp-member-row${open ? ' is-open' : ''}">
            <button type="button" class="pp-member-name member-chip-name" data-id="${m.id}"
                    title="Edit tenure dates">${esc(m.name)}</button>
            <span class="pp-member-tenure${tenure ? '' : ' is-unset'}" data-id="${m.id}"
                  title="Edit tenure dates">${tenure ? esc(tenure) : 'Tenure not set'}</span>
            <span class="pp-member-edit" data-id="${m.id}" title="Edit tenure dates">${open ? 'Close' : 'Edit'}</span>
            <span class="member-chip-x pp-member-x" data-i="${i}" title="Remove member">${icon('x')}</span>
          </div>`
        }).join('') +
        // No "no members recorded" message — an empty list is self-evident
        // (Ryan, 2026-08-07), and the Add control below already says what to do.
        `<div class="pp-member-add">
           <button type="button" class="mg-add-btn" id="pp-add-btn" title="Add Member Name">+</button>
           <span class="pp-member-add-lbl">Add member</span>
           <span class="artist-picker-wrap mg-add-picker" id="pp-add-picker" style="display:none">
             <input type="text" class="member-input mg-role-input" id="pp-add-input" autocomplete="off" placeholder="Add Member Name…" />
             <div class="artist-dropdown" id="pp-add-dd" style="display:none"></div>
           </span>
         </div>`

      // Whole row opens the editor — name, tenure text and the Edit affordance
      // all point at the same action, so the target isn't a single small word.
      box.querySelectorAll('.pp-member-name, .pp-member-tenure, .pp-member-edit').forEach(el =>
        el.addEventListener('click', () => {
          const id = parseInt(el.dataset.id)
          expandedMemberId = (expandedMemberId === id) ? null : id
          renderMusicians(); renderStintEditor()
        }))

      box.querySelectorAll('.member-chip-x').forEach(x =>
        x.addEventListener('click', async () => {
          const removedId = members[parseInt(x.dataset.i)]?.id
          members.splice(parseInt(x.dataset.i), 1)
          if (expandedMemberId === removedId) expandedMemberId = null
          await persistMembers(); renderMusicians(); renderStintEditor()
        }))
      // (The old `.member-chip-name` handler lived here. Removed 2026-08-07:
      // the row markup keeps that class for styling, so it was binding a
      // SECOND toggle to the same element — two toggles per click cancel out
      // and the editor never opened.)
      const openPicker = () => {
        const picker = document.getElementById('pp-add-picker')
        const showing = picker.style.display !== 'none'
        picker.style.display = showing ? 'none' : 'inline-flex'
        if (!showing) document.getElementById('pp-add-input').focus()
      }
      document.getElementById('pp-add-btn').addEventListener('click', openPicker)
      box.querySelector('.pp-member-add-lbl')?.addEventListener('click', openPicker)
      const input = box.querySelector('#pp-add-input')
      wirePickerDropdown(input, document.getElementById('pp-add-dd'), API.musicians.search,
        async ({ name }) => {
          name = (name || '').trim()
          if (name && !members.some(m => m.name.toLowerCase() === name.toLowerCase())) {
            members.push({ name }); await persistMembers()   // set_artist_members creates new people as needed
            await refreshRoster()
          }
          renderMusicians()
        }, 'Create new musician')
    }

    function renderStintEditor() {
      const box = document.getElementById('pp-stint-editor')
      const member = members.find(m => m.id === expandedMemberId)
      if (!member) { box.style.display = 'none'; box.innerHTML = ''; return }
      box.style.display = ''
      const single = member.stints.length <= 1
      box.innerHTML = `
        <div class="pp-stint-editor-head">
          <span class="pp-stint-editor-title">Stint dates: <b>${esc(member.name)}</b></span>
          <span class="pp-stint-editor-close" title="Close">${icon('x')}</span>
        </div>
        <div class="pp-stint-rows">
          ${member.stints.map(s => `
            <div class="pp-stint-row" data-stint-id="${s.id}">
              ${isUnbounded(s) ? '<span class="pp-stint-always">Always a member. Leave blank, or set dates for a specific tenure</span>' : ''}
              <input type="number" class="pp-stint-input pp-s-y1" placeholder="Start yr" value="${s.start_year ?? ''}" style="width:64px" />
              <input type="number" class="pp-stint-input pp-s-m1" placeholder="mo" value="${s.start_month ?? ''}" min="1" max="12" style="width:38px" />
              <input type="number" class="pp-stint-input pp-s-d1" placeholder="day" value="${s.start_day ?? ''}" min="1" max="31" style="width:38px" />
              <span class="pp-stint-dash">–</span>
              <input type="number" class="pp-stint-input pp-s-y2" placeholder="End yr" value="${s.end_year ?? ''}" style="width:64px" />
              <input type="number" class="pp-stint-input pp-s-m2" placeholder="mo" value="${s.end_month ?? ''}" min="1" max="12" style="width:38px" />
              <input type="number" class="pp-stint-input pp-s-d2" placeholder="day" value="${s.end_day ?? ''}" min="1" max="31" style="width:38px" />
              <span class="pp-stint-del" title="Remove this stint" ${single ? 'style="display:none"' : ''}>${icon('x')}</span>
            </div>`).join('')}
        </div>
        <button class="btn btn-ghost btn-xs pp-stint-add-btn" type="button">+ Add another stint (e.g. a second tenure)</button>`

      box.querySelector('.pp-stint-editor-close').addEventListener('click', () => {
        expandedMemberId = null; renderMusicians(); renderStintEditor()
      })

      box.querySelectorAll('.pp-stint-row').forEach(row => {
        const stintId = parseInt(row.dataset.stintId)
        const read = () => ({
          start_year:  parseInt(row.querySelector('.pp-s-y1').value) || null,
          start_month: parseInt(row.querySelector('.pp-s-m1').value) || null,
          start_day:   parseInt(row.querySelector('.pp-s-d1').value) || null,
          end_year:    parseInt(row.querySelector('.pp-s-y2').value) || null,
          end_month:   parseInt(row.querySelector('.pp-s-m2').value) || null,
          end_day:     parseInt(row.querySelector('.pp-s-d2').value) || null,
        })
        row.querySelectorAll('.pp-stint-input').forEach(inp =>
          inp.addEventListener('blur', async () => {
            try { await API.artists.updateStint(stintId, read()); await refreshRoster(); renderMusicians(); renderStintEditor() }
            catch (e) { alert('Failed to save stint: ' + e.message) }
          }))
        const del = row.querySelector('.pp-stint-del')
        if (del) del.addEventListener('click', async () => {
          try { await API.artists.removeStint(stintId); await refreshRoster(); renderMusicians(); renderStintEditor() }
          catch (e) { alert('Failed to remove stint: ' + e.message) }
        })
      })

      box.querySelector('.pp-stint-add-btn').addEventListener('click', async () => {
        try {
          await API.artists.addStint(artistId, member.id, {})   // unbounded until edited
          await refreshRoster(); renderMusicians(); renderStintEditor()
        } catch (e) { alert('Failed to add stint: ' + e.message) }
      })
    }

    renderMusicians()
    renderStintEditor()

    // Artist.default_personnel_mode is still a real field (new
    // performances of this act still start in whatever mode it's set to,
    // and the case-5 auto-flip still fires per-show) — it just has no
    // manual UI control on this page anymore, per the 2026-07-22 Members/
    // Guests redesign. `defaultPersonnelMode` is kept around unused here
    // only because refreshRoster() still reads it off a fresh fetch; nothing
    // reads the local variable itself now.

    // ── Editable reference Resources (external DBs / discographies) ──────────
    let resources = (artist.resources || []).map(r => ({ label: r.label, url: r.url }))
    const persistResources = () => saveField({ resources })
    function renderResources() {
      const box = document.getElementById('pp-resources')
      if (!box) return
      // Styled to match the Members list (Ryan, 2026-08-07): rows, then a
      // single "+ Add source" control. Two always-visible input boxes made an
      // empty list look like an unfilled form — most acts have no extra
      // sources, so the resting state should be quiet.
      box.innerHTML =
        resources.map((r, i) => `
          <div class="pp-resource-row" data-i="${i}">
            <a class="pp-resource-link" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.label || r.url)}</a>
            <span class="pp-resource-url">${esc(r.url)}</span>
            <span class="pp-resource-x" data-i="${i}" title="Remove">${icon('x')}</span>
          </div>`).join('') +
        `<div class="pp-member-add">
           <button type="button" class="mg-add-btn" id="pp-res-add-btn" title="Add source">+</button>
           <span class="pp-member-add-lbl" id="pp-res-add-lbl">Add source</span>
           <span class="pp-res-entry" id="pp-res-entry" style="display:none">
             <input type="text" class="pp-res-input" id="pp-res-input" autocomplete="off" spellcheck="false" />
             <span class="pp-res-hint" id="pp-res-hint"></span>
           </span>
         </div>`

      box.querySelectorAll('.pp-resource-x').forEach(x =>
        x.addEventListener('click', async () => {
          resources.splice(parseInt(x.dataset.i), 1)
          await persistResources(); renderResources()
        }))

      // Two-step entry: URL first, then an optional label. Sequential rather
      // than side-by-side because the URL is the only required part — asking
      // for both at once implies both matter, and the label is usually left
      // blank.
      const entry = document.getElementById('pp-res-entry')
      const input = document.getElementById('pp-res-input')
      const hint  = document.getElementById('pp-res-hint')
      let pendingUrl = null

      const reset = () => {
        pendingUrl = null
        input.value = ''
        input.placeholder = 'https://…'
        hint.textContent = 'Enter to continue · Esc to cancel'
        entry.style.display = 'none'
      }
      reset()

      const open = () => {
        entry.style.display = 'inline-flex'
        input.focus()
      }
      document.getElementById('pp-res-add-btn').addEventListener('click', open)
      document.getElementById('pp-res-add-lbl').addEventListener('click', open)

      input.addEventListener('keydown', async e => {
        if (e.key === 'Escape') { e.preventDefault(); reset(); return }
        if (e.key !== 'Enter') return
        e.preventDefault()
        const val = input.value.trim()

        if (pendingUrl === null) {
          if (!val) return
          pendingUrl = /^https?:\/\//i.test(val) ? val : 'https://' + val
          input.value = ''
          input.placeholder = 'Label (optional). Enter to save'
          hint.textContent = pendingUrl
          return
        }
        // Second Enter saves, with or without a label.
        resources.push({ label: val || null, url: pendingUrl })
        await persistResources()
        renderResources()
      })
    }
    renderResources()

    // ── Profile pictures (2026-07-22; MULTI-IMAGE 2026-08-07) ───────────────
    //
    // Now the SHARED gallery (2026-09-01). This page carried its own ~180-line
    // copy of a component Venue was already using — same grid, same drop zone,
    // same delegated click handler, same dragenter depth counter — and the two
    // had already drifted: only this one had the Google Images tile, only that
    // one honoured Playback mode. CONTEXT.md's rule for exactly this situation
    // is that when two surfaces drift the fix is to DELETE the divergence, not
    // to re-style one of them. So the copy is gone and the differences that
    // were worth keeping became parameters: `fetchTile` for the Commons lookup
    // (still artist-only — the Wikidata bridge runs through MusicBrainz and
    // no other dimension has one), `linkTiles` for the two search link-outs
    // that every photographed entity now gets.
    //
    // fetchTile is passed as a FUNCTION on purpose: `artist` is REASSIGNED
    // when a MusicBrainz match lands, and the tile's enabled state reads
    // artist.musicbrainz.mbid. Evaluated per render, it follows along; read
    // once at construction it would keep saying "match this act first" after a
    // successful match, which is the bug this page already had in 2026-08.
    let ppImages = artist.images || []

    // Small round portrait in the hero. Read-only — all management lives in the
    // Photos tab, so the hero never grows buttons and stays a header.
    function renderHeroPortrait() {
      const box = document.getElementById('pp-hero-portrait')
      if (!box) return
      const primary = ppImages[0] || null    // server orders primary-first
      const ring = artist.genre && artist.genre.color
        ? artist.genre.color : 'var(--bd-1)'
      box.innerHTML = heroPortraitHtml(
        artist.name, primary ? API.artists.imageUrl(primary.id) : null, ring)
    }

    const ppGallery = createPhotoGallery({
      mountId: 'pp-photos', api: API.artists, entityId: artistId,
      images: ppImages,
      fetchTile: () => {
        const hasMbid = !!(artist.musicbrainz && artist.musicbrainz.mbid)
        return {
          label: 'Import a free photo', sub: 'Wikimedia Commons',
          busyNote: 'Searching Wikimedia Commons\u2026',
          // Disabled rather than hidden with no match: the Wikidata link comes
          // from MusicBrainz, and a tile that explains why it cannot run
          // teaches the dependency where hiding it would not.
          disabledNote: 'Match on MusicBrainz first (About tab)',
          run: hasMbid ? (async () => {
            const res = await API.artists.fetchImage(artistId)
            // Not an error. Most acts genuinely have no freely-licensed photo,
            // and the long tail of this library especially so — saying "failed"
            // would misrepresent an ordinary outcome.
            return res.found
              ? { ok: true,  note: 'Added: ' + (res.image.credit || 'Wikimedia Commons') }
              : { ok: false, note: 'No freely-licensed photo found for this act.' }
          }) : null,
        }
      },
      linkTiles: photoSearchTiles(artist.name, 'band'),
      onChange: imgs => {
        ppImages = imgs
        artist.images = imgs
        renderHeroPortrait()
        // Keep the tab's count badge honest after an add or delete.
        const tab = mainContent.querySelector('.pp-tab[data-pane="photos"]')
        if (tab) tab.innerHTML = 'Photos' + (imgs.length ? `<span class="pp-tab-n">${imgs.length}</span>` : '')
      },
    })

    // ── MusicBrainz block (About tab) ───────────────────────────────────────
    // Four states, and they must look different: matched (facts + links),
    // ambiguous (needs you to pick), none (looked, found nothing), and null
    // (never looked up — pre-existing rows, or created while offline).
    function renderMusicBrainz() {
      const box = document.getElementById('pp-mb')
      if (!box) return
      const mb = artist.musicbrainz || {}
      // 'matched' = the confidence gate chose it, no human involved.
      // 'linked'  = a human picked it from the candidate list.
      // Saying "Matched automatically" for the second is a small lie that makes
      // every other automatic claim in the app less believable (Ryan,
      // 2026-08-07).
      if (mb.status === 'matched' || mb.status === 'linked') {
        const how = mb.status === 'matched' ? 'Matched automatically' : 'Linked by you'
        // WHAT we linked to, and just enough to confirm it's the right act
        // (Ryan, 2026-08-07). This panel is a CONNECTION, not a data display:
        // the link list it used to show is still fetched and stored — future
        // ingest and enrichment jobs need to know where to look — but nobody
        // needs to read it, so it isn't rendered.
        // Active years sit right beside the type — the single most useful
        // check that a match is the right act, since two same-named bands
        // almost always differ by era (Ryan, 2026-08-07).
        //
        // When MusicBrainz has no dates, SAY SO rather than omitting the
        // field: silence reads as "we didn't bother", where "no dates" is
        // itself a fact about the entry — and a common one for ad-hoc
        // billings like Acoustic All-Stars.
        const years = mb.begin
          ? `${mb.begin}${mb.end ? '–' + mb.end : '–present'}`
          : 'no dates on record'
        const facts = [mb.type, years, mb.area, mb.disambiguation]
          .filter(Boolean).join(' · ')
        box.innerHTML = `
          <div class="pp-mb-linked">
            <a class="pp-mb-name" href="${esc(mbArtistUrl(mb.mbid))}"
               target="_blank" rel="noopener"
               title="View this entry on musicbrainz.org">${esc(mb.name || artist.name)} ↗</a>
            ${facts ? `<span class="pp-mb-facts">${esc(facts)}</span>` : ''}
          </div>
          <div class="pp-mb-foot">
            <span class="pp-mb-dot"></span>${how}
            <button type="button" class="btn btn-ghost btn-xs" id="pp-mb-change">Change match</button>
          </div>`
      } else {
        const msg = mb.status === 'ambiguous'
          ? 'More than one act goes by this name, please pick the right one.'
          : mb.status === 'none'
            ? 'Nothing found for this name.'
            : 'Not looked up yet.'
        // Editable search term (Ryan, 2026-08-08): a billing variant like
        // "Aaron Parks Trio" is the act's real name but often not what
        // MusicBrainz indexed it under, so the string actually sent to the API
        // needs to be adjustable without renaming the Artist. Pre-filled
        // with the Artist's name; edits here are one-shot — nothing saved.
        box.innerHTML = `
          <div class="pp-mb-empty">${msg}</div>
          <div class="pp-mb-searchrow">
            <input type="text" class="pp-mb-input" id="pp-mb-term"
                   value="${esc(artist.name)}" placeholder="Search term"
                   title="Sent to MusicBrainz as the artist name. Edit if a billing variant (e.g. “Trio”, “Quartet”) is causing a miss">
            <button type="button" class="btn btn-primary btn-xs" id="pp-mb-lookup">
              ${mb.status === 'ambiguous' ? 'Choose a match' : 'Look up'}</button>
          </div>`
        document.getElementById('pp-mb-term').addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); runMbLookup() }
        })
      }
      document.getElementById('pp-mb-change')?.addEventListener('click', openMbPicker)
      document.getElementById('pp-mb-lookup')?.addEventListener('click', runMbLookup)
    }

    // Reads the editable search-term box when present, else falls back to the
    // Artist's own name (the "Change match" entry point has no box — it
    // starts from an already-matched state). Must be called BEFORE the panel
    // is overwritten with "Searching…", since that swap removes the input.
    function currentMbTerm() {
      const el = document.getElementById('pp-mb-term')
      const v = el && el.value.trim()
      return v || artist.name
    }

    // Look up, and LINK IT IF THE ANSWER IS OBVIOUS (Ryan, 2026-08-07).
    // Clicking through a candidate list to confirm a single 100-scoring result
    // is busywork. The server applies the same confidence gate the creation-time
    // pass uses; only a genuinely unclear result comes back as candidates.
    async function runMbLookup() {
      const box = document.getElementById('pp-mb')
      const term = currentMbTerm()
      box.innerHTML = `<div class="pp-mb-empty">Searching MusicBrainz…</div>`
      try {
        const res = await API.artists.mbLookup(artistId, term)
        if (res.status === 'matched') {
          artist = await API.artists.get(artistId)
          renderMusicBrainz()
          // The Photos tab gates its Wikimedia lookup on artist.musicbrainz
          // .mbid. Its fetchTile is a function so it re-reads that on every
          // render — but something still has to ASK for a render, or the tile
          // keeps saying "match this act first" after the match succeeded.
          ppGallery.refresh()
          return
        }
        renderMbCandidates(res.query, res.candidates || [])
      } catch (e) {
        box.innerHTML = `<div class="pp-mb-empty">Lookup failed: ${esc(e.message)}
          <button type="button" class="btn btn-ghost btn-xs" id="pp-mb-lookup">Try again</button></div>`
        document.getElementById('pp-mb-lookup').addEventListener('click', runMbLookup)
      }
    }

    // Candidate picker. Deliberately a manual step: automatic matching either
    // wins outright or defers to a human — it never guesses (Ryan, 2026-08-07),
    // because a wrong entity attaches wrong facts to a page nobody re-checks.
    async function openMbPicker() {
      const box = document.getElementById('pp-mb')
      const term = currentMbTerm()
      box.innerHTML = `<div class="pp-mb-empty">Searching MusicBrainz…</div>`
      let res
      try { res = await API.artists.mbCandidates(artistId, term) }
      catch (e) {
        box.innerHTML = `<div class="pp-mb-empty">Lookup failed: ${esc(e.message)}
          <button type="button" class="btn btn-ghost btn-xs" id="pp-mb-change">Try again</button></div>`
        document.getElementById('pp-mb-change').addEventListener('click', openMbPicker)
        return
      }
      renderMbCandidates(res.query, res.candidates || [])
    }

    // Shared candidate list — used by both the explicit "Look up" (when the
    // result wasn't clear-cut) and "Change match".
    function renderMbCandidates(query, cands) {
      const box = document.getElementById('pp-mb')
      if (!cands.length) {
        box.innerHTML = `
          <div class="pp-mb-empty">Nothing found for “${esc(query || artist.name)}”.</div>
          <div class="pp-mb-searchrow">
            <input type="text" class="pp-mb-input" id="pp-mb-term"
                   value="${esc(query || artist.name)}" placeholder="Search term"
                   title="Sent to MusicBrainz as the artist name. Edit if a billing variant (e.g. “Trio”, “Quartet”) is causing a miss">
            <button type="button" class="btn btn-ghost btn-xs" id="pp-mb-lookup">Try again</button>
          </div>`
        document.getElementById('pp-mb-lookup').addEventListener('click', runMbLookup)
        document.getElementById('pp-mb-term').addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); runMbLookup() }
        })
        return
      }
      box.innerHTML = `
        <!-- Explicit instruction (Ryan, 2026-08-07): the list looked like
             results to read, not a choice to make, so people didn't realise a
             click was required to actually link the act. -->
        <div class="pp-mb-prompt">Click the right act to link it${
          cands.length === 1 ? '' : ' (more than one goes by this name)'}.</div>
        <!-- Same editable term as the empty state (Ryan, 2026-08-08) — if none
             of these candidates are right, revise and re-search without
             leaving the panel. "Search" only re-lists candidates (no
             auto-link); "Look up" is the gated path that can commit outright. -->
        <div class="pp-mb-searchrow pp-mb-searchrow-sm">
          <input type="text" class="pp-mb-input" id="pp-mb-term"
                 value="${esc(query || artist.name)}" placeholder="Search term"
                 title="Sent to MusicBrainz as the artist name. Edit and search again if none of these are right">
          <button type="button" class="btn btn-ghost btn-xs" id="pp-mb-research">Search</button>
        </div>
        <div class="pp-mb-cands">
          ${cands.map(c => `
            <div class="pp-mb-cand" data-mbid="${esc(c.mbid)}" role="button" tabindex="0">
              <span class="pp-mb-cand-name">${esc(c.name)}</span>
              <span class="pp-mb-cand-meta">${[
                c.type, c.area,
                c.begin ? `${c.begin}${c.end ? '–' + c.end : ''}` : '',
                c.disambiguation,
              ].filter(Boolean).map(esc).join(' · ')}</span>
              <span class="pp-mb-cand-score" title="MusicBrainz match score">${c.score ?? ''}</span>
              <!-- Verify BEFORE committing. For a vague name like "Acoustic
                   All-Stars" the summary line rarely settles it, and the real
                   entry (with its releases and relationships) usually does. -->
              <a class="pp-mb-cand-view" href="${esc(mbArtistUrl(c.mbid))}"
                 target="_blank" rel="noopener"
                 title="Open on musicbrainz.org">View ↗</a>
            </div>`).join('')}
        </div>
        <div class="pp-mb-foot">
          <button type="button" class="btn btn-ghost btn-xs" id="pp-mb-cancel">Cancel</button>
          ${artist.musicbrainz?.mbid
            ? `<button type="button" class="btn btn-ghost btn-xs" id="pp-mb-clear">Clear match</button>` : ''}
        </div>`

      box.querySelectorAll('.pp-mb-cand').forEach(btn => {
        btn.addEventListener('click', async e => {
          // The View link lives inside the clickable row — following it must
          // not also select the candidate.
          if (e.target.closest('.pp-mb-cand-view')) return
          box.innerHTML = `<div class="pp-mb-empty">Fetching…</div>`
          try {
            await API.artists.mbResolve(artistId, btn.dataset.mbid)
            artist = await API.artists.get(artistId)
            renderMusicBrainz()
            ppGallery.refresh()      // see note in runMbLookup
          } catch (e) {
            alert('Failed: ' + e.message); renderMusicBrainz()
          }
        })
        // The row is a div (a <button> can't legally contain the View link),
        // so keyboard activation has to be wired by hand.
        btn.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); btn.click() }
        })
      })
      document.getElementById('pp-mb-research').addEventListener('click', openMbPicker)
      document.getElementById('pp-mb-term').addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); openMbPicker() }
      })
      document.getElementById('pp-mb-cancel').addEventListener('click', renderMusicBrainz)
      document.getElementById('pp-mb-clear')?.addEventListener('click', async () => {
        try {
          await API.artists.mbResolve(artistId, null)
          artist = await API.artists.get(artistId)
          renderMusicBrainz()
          ppGallery.refresh()        // clearing a match disables the lookup again
        } catch (e) { alert('Failed: ' + e.message) }
      })
    }
    renderMusicBrainz()

    // ── Lomax: biography, members, resources, questions ───────────────────────
    // The biography is auto-applied by the server (with Restore previous);
    // members and resources are suggestions to accept. Questions and the ask bar
    // close the About tab. Every control is edit-only (lomaxActions / lomaxAskBar).
    {
      const lxBio = document.getElementById('pp-lx-bio')
      const lxMem = document.getElementById('pp-lx-members')
      const lxRes = document.getElementById('pp-lx-resources')
      const lxAsk = document.getElementById('pp-lx')
      const alive = () => document.body.contains(lxAsk)
      const dateParts = str => {
        const p = String(str || '').trim().split('-')
        const n = i => (p[i] && /^\d+$/.test(p[i]) ? parseInt(p[i], 10) : null)
        return [n(0), n(1), n(2)]
      }
      const onRoster = m => {
        const member = members.find(x => x.name.toLowerCase() === String(m.name || '').toLowerCase())
        if (!member) return false
        const [sy, sm, sd] = dateParts(m.start), [ey, em, ed] = dateParts(m.end)
        return (member.stints || []).some(st => st.start_year === sy && st.start_month === sm && st.start_day === sd &&
          st.end_year === ey && st.end_month === em && st.end_day === ed)
      }
      const parsed = p => { try { return JSON.parse(p.proposed) || {} } catch (_) { return {} } }
      const paintBio = () => {
        const el = document.getElementById('pp-desc')
        if (!el || el.querySelector('textarea, input')) return
        el.textContent = artist.bio || (canEditLibrary() ? 'Add a description…' : '')
        el.classList.toggle('pp-empty', !artist.bio)
      }

      const c = lomaxController({
        skill: 'artist', subjectType: 'artist', subjectId: artistId, alive,
        // The server has already written the accepted value; bring this page's copy in line.
        afterAccept: async props => {
          if (props.some(p => p.field === 'member')) { await refreshRoster(); renderMusicians(); renderStintEditor() }
          if (props.some(p => p.field === 'resource')) {
            artist = await API.artists.get(artistId)
            resources = (artist.resources || []).map(r => ({ label: r.label, url: r.url }))
            renderResources()
          }
        },
        afterRestore: run => {
          artist.bio = (run.result && run.result.replaced && run.result.replaced.text) || ''
          paintBio()
        },
        // A finished run has already replaced the biography on the server.
        onDone: run => {
          const rep = run.result && run.result.replaced
          if (rep && rep.written) { artist.bio = rep.written; paintBio() }
        },
      })
      c.addView(ctl => {
        if (!alive()) return false
        // Restore previous, on the newest run that replaced the biography.
        const rep = ctl.runs.slice().reverse().find(r => r.status === 'done' && r.result && r.result.replaced)
        lxRepaint(lxBio, rep ? lomaxRestoreLink(ctl, rep, artist.bio) : '')

        const run = ctl.latest()
        const members_ = canEditLibrary() ? lxProps(run).filter(p => p.field === 'member' && lxShown(p)) : []
        const open = members_.filter(p => lxOpen(p) && !onRoster(parsed(p)))
        lxRepaint(lxMem, members_.length ? `<table class="lx-tbl lx-sugg"><thead><tr><th>Member</th><th>Dates</th>` +
          `<th class="lx-act-td">${open.length ? `<button type="button" class="lx-link lx-edit" data-lx-act="accept-all" data-lx-ids="${open.map(p => p.id).join(',')}">Add all</button>` : ''}</th></tr></thead><tbody>${
            members_.map(p => {
              const m = parsed(p), roster = p.decision === 'accepted' || onRoster(m)
              const dates = (m.start || m.end) ? `${m.start || '?'} – ${m.end || 'present'}` : ''
              return `<tr><td><span class="${roster ? 'lx-same' : 'lx-val'}">${esc(m.name || '')}</span>` +
                `${m.instrument ? ` <span class="${roster ? 'lx-same' : 'lx-val'} lx-inst">${esc(m.instrument)}</span>` : ''}</td>` +
                `<td><span class="${roster ? 'lx-same' : 'lx-val'}">${esc(dates)}</span></td>` +
                `<td class="lx-act-td">${roster ? '<span class="lx-roster">On the roster</span>' : lomaxActions(p, { accept: 'Add' })}</td></tr>`
            }).join('')}</tbody></table>` : '')

        const have = new Set(resources.map(r => r.url))
        const res_ = canEditLibrary() ? lxProps(run).filter(p => p.field === 'resource' && lxShown(p) && (p.decision === 'accepted' || !have.has(parsed(p).url))) : []
        lxRepaint(lxRes, res_.length ? `<table class="lx-tbl lx-sugg"><tbody>${res_.map(p => {
          const r = parsed(p)
          return `<tr><td><a class="lx-val" href="${esc(r.url || '#')}" target="_blank" rel="noopener">${esc(r.label || r.url || '')}</a></td>` +
            `<td><span class="lx-val lx-inst">${esc(r.url || '')}</span></td><td class="lx-act-td">${lomaxActions(p, { accept: 'Add' })}</td></tr>`
        }).join('')}</tbody></table>` : '')

        // Every question asked of this artist, oldest first, with its answer.
        const asked = ctl.runs.filter(r => r.question && r.status === 'done' && r.result && String(r.result.answer || '').trim())
        const err = ctl.lastError()
        const bar = lomaxAskBar({ placeholder: 'Anything else you want to learn? (optional)',
          note: "Lomax researches this artist's biography, members over the years, and useful links. Add anything else below.",
          last: run, busy: !!ctl.pending })
        lxRepaint(lxAsk, !(asked.length || ctl.pending || err || bar) ? '' : `<div class="lx-ask-wrap">
          ${asked.length ? `<div class="pp-sec">Questions</div><div class="lx-qs">${asked.map(r =>
            `<div class="lx-qa"><div class="lx-qa-q">${esc(r.question)}</div><div class="lx-answer">${esc(stripCitations(r.result.answer))}</div>` +
            `<div class="lx-qa-d">${esc(lxDay(r.created_at))}</div></div>`).join('')}</div>` : ''}
          ${ctl.pending ? lomaxRunState(ctl.pending) : (err ? `<div class="lx-error" role="alert">${esc(err)}</div>` : '')}
          ${bar}
        </div>`)
        return true
      })
      for (const el of [lxBio, lxMem, lxRes, lxAsk]) if (el) el.setAttribute('data-lx-ctl', c.id)
      c.refresh()
      c.load()
    }

    onAdminClick('pp-delete', async () => {
      if (!confirm(`Delete artist "${artist.name}"? This can't be undone.`)) return
      try { await API.artists.remove(artistId); refreshSidebar(); window.location.hash = '#/' }
      catch (e) { alert(e.message) }
    })
  }

  // Turn an element into a click-to-edit field. opts: {get, onSave, multiline, placeholder}.
  // Every click-to-edit field in the app goes through here — recording notes,
  // artist and venue and genre and collection and musician names and
  // descriptions, venue City/State/Country. That makes it the one place
  // Playback mode has to be honoured, rather than gating ~25 call sites and
  // missing one (which is exactly how the Venue page's City/State/Country
  // stayed editable in Playback — Ryan, 2026-08-22).
  //
  // It strips the affordance as well as the behaviour: a span that still looks
  // clickable and highlights on hover but does nothing is worse than a plain
  // one, because the user tries it twice before believing it.
  function makeInlineEditable(el, opts) {
    if (!el) return
    if (!canEditLibrary()) {
      el.classList.remove('pp-editable')
      el.removeAttribute('title')
      // An empty field's text is a call to action here by convention — "Add a
      // description…", "Add notes…". With no way to act on it, it is a lie, so
      // it goes and the space closes up. A real "—" (unknown value) is left
      // alone: that still means something to a reader.
      if (el.classList.contains('pp-empty') && /^Add\b/.test(el.textContent.trim())) {
        el.textContent = ''
      }
      return
    }
    el.addEventListener('click', () => {
      if (el.querySelector('input, textarea, select')) return   // already editing
      const cur = opts.get()
      // `options`: [[value, label], ...] -- a fixed-choice field (spec 7e's
      // Studio/Live kind toggle) instead of free text. Same commit/Escape
      // contract as the text path below, minus Tab-to-next (nothing in this
      // codebase chains a select into another field yet).
      if (opts.options) {
        const field = document.createElement('select')
        field.className = 'pp-inline-input'
        field.innerHTML = opts.options.map(([v, label]) =>
          `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(label)}</option>`).join('')
        el.replaceChildren(field)
        field.focus()
        let doneSel = false
        const commitSelect = async (save) => {
          if (doneSel) return; doneSel = true
          if (save) await opts.onSave(field.value)
          else {
            const shown = (opts.get() || '').trim()
            el.textContent = shown || (opts.placeholder || '')
            el.classList.toggle('pp-empty', !shown)
          }
        }
        field.addEventListener('change', () => commitSelect(true))
        field.addEventListener('blur', () => commitSelect(false))
        field.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); commitSelect(false) } })
        return
      }
      const field = document.createElement(opts.multiline ? 'textarea' : 'input')
      field.className = 'pp-inline-input'
      field.value = cur
      if (opts.multiline) field.rows = 3
      el.replaceChildren(field)
      field.focus(); field.select?.()
      let done = false
      const commit = async (save) => {
        if (done) return; done = true
        const val = field.value
        if (save) await opts.onSave(val)
        const shown = (opts.get() || '').trim()
        el.textContent = shown || (opts.placeholder || '')
        el.classList.toggle('pp-empty', !shown)
      }
      field.addEventListener('blur', () => commit(true))
      field.addEventListener('keydown', async e => {
        if (e.key === 'Escape') { e.preventDefault(); commit(false) }
        else if (e.key === 'Enter' && !opts.multiline) { e.preventDefault(); field.blur() }
        else if (e.key === 'Enter' && e.metaKey) { e.preventDefault(); field.blur() }
        else if (e.key === 'Tab' && opts.tabTo) {
          // TAB ADVANCES TO THE NEXT FIELD (Ryan, 2026-08-07). Without this,
          // Tab left the browser to pick a focus target — usually nothing
          // useful, since the neighbouring "fields" are spans that only become
          // inputs on click. Editing a venue's City/State/Country meant three
          // separate mouse trips.
          //
          // `tabTo` names the next element's id; committing first, then
          // clicking it, reuses the exact same open-editor path a real click
          // takes, so there is only one way an editor is ever opened.
          e.preventDefault()
          await commit(true)
          const nextId = typeof opts.tabTo === 'function' ? opts.tabTo(e.shiftKey) : opts.tabTo
          const next = nextId && document.getElementById(nextId)
          if (next) next.click()
        }
      })
    })
  }

  // ── Checksums pane — .ffp/.md5/.st5 fingerprint verification (View Recording) ──
  // Track.checksum is {type, expected, status, verified_at} or null (no
  // fingerprint file could be matched to that track). "status" is one of
  // match / mismatch / unverified, set by app/utils/checksums.py.
  const CKSUM_STATUS_LABEL = { match: 'Match', mismatch: 'Mismatch', unverified: 'Unverified' }

  function buildChecksumsPaneHtml(tracks) {
    tracks = tracks || []
    const withData = tracks.filter(t => t.checksum)
    if (!withData.length) {
      return `<div class="info-panel-empty">No checksums on file for this recording yet. Click Re-validate to check the library folder for a fingerprint file.</div>`
    }
    const mismatches = withData.filter(t => t.checksum.status === 'mismatch').length
    const summary = mismatches
      ? `<div class="cksum-summary cksum-summary--warn">${mismatches} track${mismatches === 1 ? '' : 's'} did not match ${mismatches === 1 ? 'its' : 'their'} recorded checksum.</div>`
      : `<div class="cksum-summary cksum-summary--ok">All checked tracks match their recorded checksum.</div>`
    const rows = tracks.map(t => {
      const c = t.checksum
      const num = esc(String(t.track_number).padStart(2, '0'))
      const title = esc(t.title)
      if (!c) {
        return `<div class="cksum-row"><span class="cksum-num">${num}</span><span class="cksum-title">${title}</span><span class="cksum-type">—</span><span class="cksum-status">no fingerprint</span></div>`
      }
      return `
        <div class="cksum-row">
          <span class="cksum-num">${num}</span>
          <span class="cksum-title">${title}</span>
          <span class="cksum-type">${esc((c.type || '').toUpperCase())}</span>
          <span class="cksum-status cksum-status--${esc(c.status || '')}">${CKSUM_STATUS_LABEL[c.status] || esc(c.status || '')}</span>
        </div>
        ${c.status === 'mismatch' ? `<div class="cksum-detail">expected ${esc(c.expected || '')}</div>` : ''}`
    }).join('')
    const md5Note = withData.some(t => t.checksum.type === 'md5')
      ? `<p class="cksum-hint">MD5 checks the whole file, tags included. Any tag edit (including Write Tags to Files) will flip a match to a mismatch. Expected, not corruption.</p>` : ''
    const st5Note = withData.some(t => t.checksum.type === 'st5')
      ? `<p class="cksum-hint">ST5 verification is best-effort. Treat a mismatch as worth a second look, not a hard failure.</p>` : ''
    return `${summary}<div class="cksum-rows">${rows}</div>${md5Note}${st5Note}`
  }

  // Add Recording's Checksums pane is detection-only — the files haven't been
  // copied yet at review time, so there's nothing to verify against; real
  // verification happens automatically on Confirm (see api/ingest.py
  // _do_confirm) once the copy exists at a stable library path.
  function buildChecksumsPreviewHtml(fingerprints) {
    fingerprints = fingerprints || []
    if (!fingerprints.length) {
      return `<div class="info-panel-empty">No checksum/fingerprint files (.ffp / .md5 / .st5) found in this folder.</div>`
    }
    const rows = fingerprints.map(fp => `
      <div class="cksum-row">
        <span class="cksum-type">${esc((fp.type || '').toUpperCase())}</span>
        <span class="cksum-title">${esc(fp.filename)}</span>
      </div>`).join('')
    return `<div class="cksum-summary">Found ${fingerprints.length} fingerprint file${fingerprints.length === 1 ? '' : 's'}. Verified automatically against the copied files when you confirm.</div>
      <div class="cksum-rows">${rows}</div>`
  }

  // ── Lomax ─────────────────────────────────────────────────────────────────
  // Every Lomax surface is built from the helpers in this section, so a new
  // screen mounts Lomax with one controller and a few calls:
  //   lomaxController(cfg)      a run list, the one poller, accept/dismiss/restore
  //   lomaxAskBar(o)            question box, level toggle with (i), Ask Lomax
  //   lomaxSuggestionCell(p)    the blue value (quiet when Lomax agrees)
  //   lomaxActions(p)           the check / X column, or "Accepted"
  //   lomaxSourcesPopover(s)    (i) beside a Trellis value: where it came from
  //   lomaxRunState(run)        Working with elapsed time, or the error
  //   lomaxUsageLine(run)       "4 sources · Research · 52k tokens · 4 searches"
  // Screen roots carry data-lx-ctl="<controller id>"; ONE document-level click
  // handler (lxOnClick, bottom of this section) serves them all. Every control
  // is edit-only: canEditLibrary() false renders none of them, and Playback
  // mode also hides .lx-edit in CSS.
  const LX_LEVEL_LABEL = { off: 'Off', study: 'Study', research: 'Research' }
  // Study was removed (Ryan, 2026-10-05): every run is Research. Add Recordings is Off / On, the ask
  // bars have no level control. LX_LEVEL_LABEL keeps 'study' so older runs still label correctly.
  const LX_IMPORT_KEY = 'trellisLomaxLevel'      // Off / On on Add Recordings ('research' means On)
  const LX_PAUSED_KEY = 'trellisLomaxPausedRun'  // the import run whose Lomax queue the person paused
  const LX_IMPORT_TIP = 'Lomax, your AI research assistant, will peruse recording files, query your Trellis database, and search web resources to find missing info for recordings.'
  const LX_FIELDS = [
    ['artist', 'Artist'], ['date', 'Date'], ['venue', 'Venue'], ['city', 'City'], ['state', 'State'],
    ['country', 'Country'], ['event', 'Event'], ['stage', 'Stage'], ['source', 'Source'], ['lineage', 'Lineage'],
  ]
  // An album run proposes these (skill 'album'); they show as field cards in the Lomax pane.
  const LX_ALBUM_FIELDS = [['title', 'Title'], ['year', 'Year'], ['notes', 'Notes']]
  const _lxCtls = new Map()
  let _lxSeq = 0
  let _lxTickTimer = null

  function lxGet(k) { try { return localStorage.getItem(k) } catch (_) { return null } }
  function lxSet(k, v) { try { localStorage.setItem(k, v) } catch (_) { /* storage blocked: the level just is not remembered */ } }
  // Off when there is no key, whatever was remembered. appPrefs is null until
  // first read, which is treated as "has a key" so a cold page is not wrongly locked.
  function lxHasKey() { return appPrefs ? !!appPrefs.has_api_key : true }
  function lxImportLevel() {
    if (!lxHasKey()) return 'off'
    const v = lxGet(LX_IMPORT_KEY)
    return v === 'study' || v === 'research' ? 'research' : 'off'   // an old Study pick reads as On
  }
  function lxPlural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + 's')}` }
  function lxTokens(n) { n = Number(n) || 0; return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n) }
  // The server writes some timestamps without a zone; those are UTC.
  function lxTs(s) {
    if (!s) return NaN
    return Date.parse(/(Z|[+-]\d\d:?\d\d)$/.test(s) ? s : s + 'Z')
  }
  function lxDay(s) {
    const t = lxTs(s)
    if (isNaN(t)) return ''
    const d = new Date(t), now = new Date()
    const same = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
    return same ? 'Today' : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  function lxClock(sec) {
    sec = Math.max(0, Math.floor(sec))
    return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
  }
  function lxErrText(e) {
    const m = String((e && e.message) || e || '')
    if (/no_api_key/.test(m)) return 'Lomax requires your Anthropic key to be set.'
    return m || 'The run failed.'
  }

  // ── Run data ────────────────────────────────────────────────────────────────
  const lxProps = run => (run && run.result && Array.isArray(run.result.proposals)) ? run.result.proposals : []
  // {field: proposal} for the non-track fields.
  function lxFieldProps(run) {
    const out = {}
    for (const p of lxProps(run)) if (p && !String(p.field).startsWith('track.')) out[p.field] = p
    return out
  }
  // {number: {title, songwriter, note}} (each a proposal) from "track.N.piece".
  function lxTrackProps(run) {
    const out = {}
    for (const p of lxProps(run)) {
      const m = /^track\.(\d+)\.(title|songwriter|note)$/.exec(String(p && p.field))
      if (!m) continue
      ;(out[m[1]] = out[m[1]] || {})[m[2]] = p
    }
    return out
  }
  // What a person can still act on: has an id (a migrated legacy row has none),
  // is not Lomax agreeing with Trellis, and nobody has decided it.
  const lxOpen = p => !!(p && p.id && !p.agrees && !p.decision)
  const lxSuggestions = run => lxProps(run).filter(p => !p.agrees)
  // The stored decision is the truth. Every accepted proposal (oldest run first, the newest one per
  // field winning) whose value differs from what the form holds now; the caller writes them back
  // into the form as human-set. read(field) is the form's current value for a field.
  // The identity of a scanned show: audio file count, a hash of the file names, a hash of the info
  // file. Stored on a folder run when it is made; a run for another show at the same path differs.
  function lxFingerprint(scan) {
    if (!scan) return ''
    const h = t => { let x = 5381; const q = String(t || ''); for (let i = 0; i < q.length; i++) x = ((x * 33) ^ q.charCodeAt(i)) >>> 0; return x.toString(36) }
    return [scan.audio_file_count || 0, h((scan.audio_files || []).map(f => f.rel_path || f.filename).join('\n')), h(scan.info_file_content)].join('.')
  }
  // fingerprint: the current show's. A run made for a different show, or one that carries no identity,
  // is left alone. skip(p) vetoes one proposal (already applied here, or its field typed in by hand).
  function lxAcceptedToReapply(runs, read, skip, fingerprint) {
    const byField = new Map()
    for (const run of runs || []) {
      if (!run || run.status !== 'done') continue
      if (fingerprint && run.fingerprint !== fingerprint) continue
      for (const p of lxProps(run)) if (p && p.decision === 'accepted' && !p.agrees && p.id) byField.set(p.field, p)
    }
    const same = (a, b) => String(a == null ? '' : a).trim() === String(b == null ? '' : b).trim()
    return Array.from(byField.values()).filter(p => !(skip && skip(p)) && !same(read(p.field), p.proposed))
  }

  // ── Pieces ──────────────────────────────────────────────────────────────────
  // Add Recordings: Lomax Off / On. On stores 'research', the only level runs use.
  function lomaxImportSwitch({ on, disabled }) {
    const b = (v, label) => `<button type="button" class="${on === v ? 'on' : ''}" data-lx-level="${v ? 'research' : 'off'}" ` +
      `data-lx-key="import"${disabled ? ' disabled' : ''}>${label}</button>`
    return `<span class="lx-level"><span class="seg lx-seg" role="group" aria-label="Lomax">${b(false, 'Off')}${b(true, 'On')}</span>` +
      `<span class="lx-tipwrap"><button type="button" class="lx-info" data-lx-tip aria-expanded="false" aria-label="What Lomax does">${icon('info')}</button>` +
      `<span class="lx-pop lx-pop--tip" role="tooltip" hidden>${esc(LX_IMPORT_TIP)}</span></span></span>`
  }

  // o: {level, placeholder, note, last (run), rows, pin}. The controller is found
  // from the screen root, so the bar needs no id of its own.
  function lomaxAskBar(o = {}) {
    if (!canEditLibrary()) return ''
    const last = o.last && o.last.finished_at
      ? `<div class="lx-ask-last">Last run: ${esc(lxDay(o.last.finished_at))} · ${esc(LX_LEVEL_LABEL[o.last.level] || '')}</div>` : ''
    return `<div class="lx-ask lx-edit${o.pin ? ' lx-ask--pin' : ''}" data-lx-ask>
      ${o.note ? `<div class="lx-ask-note">${esc(o.note)}</div>` : ''}
      ${last}
      <textarea class="lx-q" rows="${o.rows || 2}" placeholder="${esc(o.placeholder || '')}" aria-label="${esc(o.placeholder || 'Question')}"></textarea>
      <div class="lx-ask-row">
        <button type="button" class="btn btn-sm lx-go" data-lx-act="ask"${o.busy ? ' disabled' : ''}>Ask Lomax</button>
      </div>
    </div>`
  }

  // The blue value. Lomax agreeing, or an accepted value, is quiet blue; a
  // rejected one is not shown. Never any confidence or source note.
  function lomaxSuggestionCell(prop, opts = {}) {
    if (!prop || prop.decision === 'rejected') return ''
    const text = opts.text != null ? opts.text : prop.proposed
    if (text == null || text === '') return ''
    const quiet = prop.agrees || prop.decision === 'accepted'
    return `<span class="${quiet ? 'lx-same' : 'lx-val'}">${esc(text)}</span>`
  }

  // The check / X column. props may be several (a track row: title, songwriter,
  // note), acted on together by id list.
  function lomaxActions(props, opts = {}) {
    if (!canEditLibrary()) return ''
    const list = (Array.isArray(props) ? props : [props]).filter(Boolean)
    if (!list.length) return ''
    const open = list.filter(lxOpen)
    if (open.length) {
      const ids = open.map(p => p.id).join(',')
      return `<span class="lx-acts lx-edit">` +
        `<button type="button" class="lx-ic lx-ok" data-lx-act="accept" data-lx-ids="${ids}" aria-label="${opts.accept || 'Accept'}" title="${opts.accept || 'Accept'}">${icon('check')}</button>` +
        `<button type="button" class="lx-ic lx-no" data-lx-act="dismiss" data-lx-ids="${ids}" aria-label="Dismiss" title="Dismiss">${icon('x')}</button></span>`
    }
    return list.some(p => p.decision === 'accepted') ? '<span class="lx-done">Accepted</span>' : ''
  }

  // (i) beside a Trellis value. src is resolved.sources_plain[field]:
  // {metadata, info_files, folder, reference_match}, each the quoted text a
  // source offered or null.
  function lomaxSourcesPopover(src, field) {
    if (!src) return ''
    const row = (label, v) => `<div class="lx-src-row"><span class="lx-src-k">${label}</span>` +
      `<span class="lx-src-v${v ? '' : ' lx-src-none'}">${v ? '“' + esc(v) + '”' : 'None'}</span></div>`
    return `<span class="lx-tipwrap"><button type="button" class="lx-info" data-lx-tip aria-expanded="false" aria-label="Sources">${icon('info')}</button>` +
      `<span class="lx-pop" hidden><div class="lx-pop-h">Sources</div>` +
      row('Metadata', src.metadata) + row('Info Files', src.info_files) + row('Folder', src.folder) +
      (field === 'venue' && src.reference_match ? row('Matches a known venue', src.reference_match) : '') +
      `</span></span>`
  }

  function lomaxAvatar() { return `<span class="lx-av" aria-hidden="true">${icon('lomax-full')}</span>` }

  // Working (with the time it has taken, counted from the server's own start
  // time so it survives a reload) or the error. A finished run returns ''.
  // o: {leave: show the leave-the-page line, avatar}
  function lomaxRunState(run, o = {}) {
    if (!run) return ''
    if (run.status === 'queued' || run.status === 'running') {
      const since = lxTs(run.created_at)
      return `<div class="lx-state">${o.avatar ? lomaxAvatar() : ''}<div>
        <div class="lx-working"><b>Working</b><span class="lx-elapsed" data-lx-since="${isNaN(since) ? Date.now() : since}" data-lx-lvl="${esc(LX_LEVEL_LABEL[run.level] || '')}">${lxClock((Date.now() - (isNaN(since) ? Date.now() : since)) / 1000)} · ${esc(LX_LEVEL_LABEL[run.level] || '')}</span></div>
        ${o.leave ? '<div class="lx-leave">You can leave this page. The result is saved to this recording.</div>' : ''}
      </div></div>`
    }
    if (run.status === 'error') {
      return `<div class="lx-error" role="alert">${esc(lxErrText({ message: run.error }))}</div>`
    }
    return ''
  }

  function lomaxUsageLine(run) {
    if (!run) return ''
    const r = run.result || {}, u = run.usage || r.usage || null
    const srcs = (r.sources || []).filter(s => s && s.url)
    const parts = []
    if (srcs.length) parts.push(`<a href="#" class="lx-srclink" data-lx-act="sources">${lxPlural(srcs.length, 'source')}</a>`)
    if (LX_LEVEL_LABEL[run.level]) parts.push(esc(LX_LEVEL_LABEL[run.level]))
    if (u) {
      const total = u.total_tokens ?? ((u.input_tokens || 0) + (u.output_tokens || 0))
      parts.push(`${lxTokens(total)} tokens`)
      if (u.web_search_requests) parts.push(lxPlural(u.web_search_requests, 'search', 'searches'))
    }
    if (!parts.length) return ''
    return `<div class="lx-usage">${parts.join(' · ')}</div>` +
      (srcs.length ? `<ul class="lx-srclist" hidden>${srcs.map(s =>
        `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a></li>`).join('')}</ul>` : '')
  }

  // Re-render a Lomax screen without losing what is being typed or where the
  // reader had scrolled. opts.stick keeps a chat pinned to its newest message.
  function lxRepaint(el, html, opts = {}) {
    if (!el) return
    const q = el.querySelector('.lx-q')
    const keep = q ? { v: q.value, focus: document.activeElement === q, s: q.selectionStart } : null
    const scroller = el.classList.contains('lx-scroll') ? el : (el.querySelector('.lx-scroll') || el.closest('.slide-pane-scroll'))
    const top = scroller ? scroller.scrollTop : 0
    const atEnd = scroller ? (!el.dataset.lxPainted || scroller.scrollHeight - top - scroller.clientHeight < 40) : false
    el.innerHTML = html
    el.dataset.lxPainted = '1'
    if (scroller) scroller.scrollTop = opts.stick && atEnd ? scroller.scrollHeight : top
    const q2 = el.querySelector('.lx-q')
    if (keep && q2 && keep.v) {
      q2.value = keep.v
      if (keep.focus) { q2.focus(); try { q2.setSelectionRange(keep.s, keep.s) } catch (_) { /* not focusable */ } }
    }
  }

  // ── The one poller ──────────────────────────────────────────────────────────
  // Resolves with the finished run, or null when the screen has gone away (or
  // the run vanished). A dropped request is retried a few times before giving up.
  async function lomaxWaitRun(id, { alive } = {}) {
    let failures = 0
    for (;;) {
      await new Promise(r => setTimeout(r, 2000))
      if (alive && !alive()) return null
      let run
      try { run = await API.lomax.run(id); failures = 0 }
      catch (e) {
        if (/not found|404/i.test(String(e && e.message)) || ++failures >= 5) return null
        continue
      }
      if (run && (run.status === 'done' || run.status === 'error')) return run
    }
  }

  function lxTick() {
    const els = document.querySelectorAll('[data-lx-since]')
    if (!els.length) { clearInterval(_lxTickTimer); _lxTickTimer = null; return }
    const now = Date.now()
    els.forEach(el => { el.textContent = `${lxClock((now - Number(el.dataset.lxSince)) / 1000)} · ${el.dataset.lxLvl}` })
  }
  function lxTickStart() { if (!_lxTickTimer) _lxTickTimer = setInterval(lxTick, 1000) }

  // cfg: {skill, subjectType, subjectId | subjectKey, current() (folder only),
  //   alive() (is the screen still mounted), onChange(c), afterAccept(props, c),
  //   afterRestore(run, c), onDone(run, c), onLoaded(c) (the stored runs have just been read)}
  function lomaxController(cfg) {
    const c = { id: ++_lxSeq, cfg, runs: [], pending: null, error: null, loaded: false, blocked: {}, notesAdded: {}, views: new Set() }
    _lxCtls.set(c.id, c)
    const alive = () => !cfg.alive || cfg.alive()
    // Several views can share one controller (Add Recording paints the Resolver
    // tab and the chat from the same runs). A view returns false once its
    // element has left the page.
    const changed = () => {
      if (!alive()) return
      if (cfg.onChange) cfg.onChange(c)
      for (const v of Array.from(c.views)) {
        try { if (v(c) === false) c.views.delete(v) } catch (e) { console.error(e) }
      }
    }
    c.addView = fn => { c.views.add(fn); return c }
    c.refresh = changed
    const query = () => cfg.subjectKey
      ? { skill: cfg.skill, subject_type: cfg.subjectType, subject_key: cfg.subjectKey }
      : { skill: cfg.skill, subject_type: cfg.subjectType, subject_id: cfg.subjectId }
    const put = run => {
      const i = c.runs.findIndex(r => r.id === run.id)
      if (i >= 0) c.runs[i] = run; else c.runs.push(run)
    }

    // The newest finished run is the one with something to act on.
    c.latest = () => { for (let i = c.runs.length - 1; i >= 0; i--) if (c.runs[i].status === 'done') return c.runs[i]; return null }
    // The error to show: the newest run's own, else a failed start.
    c.lastError = () => {
      const last = c.runs[c.runs.length - 1]
      if (last && last.status === 'error') return lxErrText({ message: last.error })
      return c.error
    }
    c.wait = async id => {
      lxTickStart()
      const run = await lomaxWaitRun(id, { alive })
      if (!run) { if (!alive()) _lxCtls.delete(c.id); return }
      c.pending = null
      put(run)
      changed()
      if (run.status === 'done' && cfg.onDone) cfg.onDone(run, c)
    }
    c.load = async () => {
      try { c.runs = ((await API.lomax.runs(query())) || {}).runs || [] } catch (_) { c.runs = [] }
      c.loaded = true
      if (cfg.onLoaded) { try { await cfg.onLoaded(c) } catch (e) { console.error(e) } }
      const p = c.runs.slice().reverse().find(r => r.status === 'queued' || r.status === 'running')
      if (p) { c.pending = p; c.wait(p.id) }
      changed()
    }
    c.ask = async ({ question, level }) => {
      if (c.pending) return
      c.error = null
      const body = { skill: cfg.skill, subject_type: cfg.subjectType, level: level || 'research' }
      if (question) body.question = question
      if (cfg.subjectKey) { body.subject_key = cfg.subjectKey; body.current = cfg.current ? cfg.current() : {} }
      else body.subject_id = cfg.subjectId
      let run
      try { run = await API.lomax.start(body) }
      catch (e) { c.error = lxErrText(e); changed(); return }
      c.pending = run
      put(run)
      changed()
      c.wait(run.id)
    }
    // Record the decision; the server applies an accepted value for a saved
    // subject. A folder subject is applied by the page (afterAccept), because
    // the database has nothing yet.
    c.decide = async (ids, decision) => {
      const done = []
      c.error = null
      for (const id of ids.map(Number)) {
        try {
          const p = await API.lomax.decide(id, decision)
          for (const r of c.runs) {
            const props = r.result && r.result.proposals
            const i = props ? props.findIndex(x => x.id === p.id) : -1
            if (i >= 0) props[i] = p
          }
          done.push(p)
        } catch (e) { c.error = lxErrText(e); break }
      }
      if (decision === 'accepted' && done.length && cfg.afterAccept) {
        try { await cfg.afterAccept(done, c) } catch (e) { c.error = lxErrText(e) }
      }
      changed()
    }
    c.restore = async runId => {
      try {
        const run = await API.lomax.restore(runId)
        put(run)
        if (cfg.afterRestore) cfg.afterRestore(run, c)
      } catch (e) {
        if (/edited_since/.test(String(e && e.message))) c.blocked[runId] = true
        else c.error = lxErrText(e)
      }
      changed()
    }
    return c
  }
  const lxAttr = c => `data-lx-ctl="${c.id}"`

  // A Restore previous link: only while the run's auto-applied text is still
  // what is on the page (the server refuses once it has been edited).
  function lomaxRestoreLink(c, run, currentText) {
    const rep = run && run.result && run.result.replaced
    if (!rep || run.result.restored_at || c.blocked[run.id] || !canEditLibrary()) return ''
    if (currentText != null && String(currentText) !== String(rep.written)) return ''
    return `<button type="button" class="lx-link lx-edit" data-lx-act="restore" data-lx-run="${run.id}">Restore previous</button>`
  }

  // ── The one click handler ───────────────────────────────────────────────────
  function lxClosePops(except) {
    document.querySelectorAll('.lx-pop:not([hidden])').forEach(p => {
      if (p === except) return
      p.hidden = true
      p.parentElement?.querySelector('[data-lx-tip]')?.setAttribute('aria-expanded', 'false')
    })
  }
  // Popovers are position:fixed so a scrolling pane or a table cell cannot clip
  // them: below the (i) when there is room, above it otherwise, and always
  // inside the window.
  function lxPlacePop(pop, tip) {
    const r = tip.getBoundingClientRect()
    const w = pop.offsetWidth, h = pop.offsetHeight, m = 8
    let top = r.bottom + 6
    if (top + h > window.innerHeight - m) top = Math.max(m, r.top - h - 6)
    const left = Math.min(Math.max(m, r.left - 8), Math.max(m, window.innerWidth - w - m))
    pop.style.top = `${top}px`
    pop.style.left = `${left}px`
  }
  function lxOnClick(ev) {
    const t = ev.target
    if (!t || !t.closest) return
    const tip = t.closest('[data-lx-tip]')
    if (tip) {
      const pop = tip.parentElement.querySelector('.lx-pop')
      lxClosePops(pop)
      if (pop) {
        pop.hidden = !pop.hidden
        tip.setAttribute('aria-expanded', String(!pop.hidden))
        if (!pop.hidden) lxPlacePop(pop, tip)
      }
      ev.preventDefault()
      return
    }
    if (!t.closest('.lx-pop')) lxClosePops()
    const lv = t.closest('[data-lx-level]')
    if (lv) {
      if (lv.disabled) return
      const level = lv.dataset.lxLevel
      const key = lv.dataset.lxKey
      if (key !== 'import') return
      lxSet(LX_IMPORT_KEY, level)
      document.querySelectorAll(`[data-lx-key="${key}"]`).forEach(b => b.classList.toggle('on', b.dataset.lxLevel === level))
      _lxImportLevelChanged(level)
      return
    }
    const btn = t.closest('[data-lx-act]')
    if (!btn) return
    const act = btn.dataset.lxAct
    if (act === 'sources') {
      ev.preventDefault()
      const list = btn.closest('.lx-usage')?.nextElementSibling
      if (list && list.classList.contains('lx-srclist')) list.hidden = !list.hidden
      return
    }
    if (act === 'show') {
      const card = btn.closest('.lx-card')
      if (card) { card.classList.add('open'); btn.hidden = true }
      return
    }
    const root = btn.closest('[data-lx-ctl]')
    const c = root && _lxCtls.get(Number(root.dataset.lxCtl))
    if (!c) return
    if (act === 'ask') {
      if (c.pending) return
      const bar = btn.closest('[data-lx-ask]')
      const q = bar && bar.querySelector('.lx-q')
      const question = q ? q.value.trim() : ''
      if (q) q.value = ''
      c.ask({ question, level: 'research' })
    } else if (act === 'accept' || act === 'dismiss' || act === 'accept-all') {
      btn.disabled = true
      c.decide(String(btn.dataset.lxIds || '').split(',').filter(Boolean), act === 'dismiss' ? 'rejected' : 'accepted')
    } else if (act === 'restore') {
      btn.disabled = true
      c.restore(Number(btn.dataset.lxRun))
    } else if (c.cfg.onAct) {
      c.cfg.onAct(act, btn, c)
    }
  }
  document.addEventListener('click', lxOnClick)
  document.addEventListener('scroll', ev => {
    if (!(ev.target && ev.target.closest && ev.target.closest('.lx-pop'))) lxClosePops()
  }, true)
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') lxClosePops()
    else if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey) && ev.target && ev.target.matches && ev.target.matches('.lx-q')) {
      ev.target.closest('[data-lx-ask]')?.querySelector('.lx-go')?.click()
    }
  })

  // ── Lomax: the recording chat ───────────────────────────────────────────────
  // View Recording's and Add Recording's Lomax tab. Each message is a fresh
  // run (the back end hands it a recap of earlier ones), so this is a list of
  // runs shown as a conversation: the question as a right-aligned bubble, the
  // answer as a Lomax message. Only the newest finished run can be acted on.
  const lxPad2 = n => String(n).padStart(2, '0')
  const lxShown = p => !!p && p.decision !== 'rejected'

  function lxTrackSummary(props) {
    const n = re => props.filter(p => re.test(p.field)).length
    return [[n(/\.title$/), 'title'], [n(/\.songwriter$/), 'songwriter'], [n(/\.note$/), 'note']]
      .filter(([k]) => k).map(([k, w]) => lxPlural(k, w)).join(', ')
  }

  // The Lomax cell of a track row: title, then "Songwriter: X" and "Note: Y",
  // all blue. opts.onlyNew drops the pieces Lomax merely agrees with.
  function lxTrackPiecesHtml(tp, opts = {}) {
    const ok = p => lxShown(p) && !(opts.onlyNew && p.agrees && p !== tp.title)
    const out = []
    if (ok(tp.title)) out.push(`<div>${lomaxSuggestionCell(tp.title)}</div>`)
    if (ok(tp.songwriter)) out.push(`<div class="lx-sub">${lomaxSuggestionCell(tp.songwriter, { text: 'Songwriter: ' + tp.songwriter.proposed })}</div>`)
    if (ok(tp.note)) out.push(`<div class="lx-sub">${lomaxSuggestionCell(tp.note, { text: 'Note: ' + tp.note.proposed })}</div>`)
    return out.join('')
  }
  const lxTrackPieces = tp => ['title', 'songwriter', 'note'].map(k => tp[k]).filter(Boolean)

  function lxFieldCardHtml(run, o) {
    const fp = lxFieldProps(run)
    const rows = LX_FIELDS.concat(LX_ALBUM_FIELDS, [['genre', 'Genre']]).filter(([k]) => fp[k] && !fp[k].agrees && lxShown(fp[k]))
    if (!rows.length) return ''
    return `<div class="lx-card"><table class="lx-tbl"><tbody>${rows.map(([k, label]) => {
      const p = fp[k]
      return `<tr><th scope="row">${label}</th><td>${p.current ? `<div class="lx-was">${esc(p.current)}</div>` : ''}${lomaxSuggestionCell(p)}</td>` +
        `<td class="lx-act-td">${o.readonly ? '' : lomaxActions(p)}</td></tr>`
    }).join('')}</tbody></table></div>`
  }

  function lxTrackCardHtml(run, o) {
    const tp = lxTrackProps(run)
    const rows = Object.keys(tp).map(Number).sort((a, b) => a - b)
      .map(n => ({ n, pieces: lxTrackPieces(tp[n]).filter(p => !p.agrees && lxShown(p)), tp: tp[n] }))
      .filter(r => r.pieces.length)
    if (!rows.length) return ''
    const all = rows.flatMap(r => r.pieces)
    const open = all.filter(lxOpen)
    const SHOW = 3
    return `<div class="lx-card"><div class="lx-card-h"><span class="lx-card-t">Tracks</span>` +
      `<span class="lx-card-s">${esc(lxTrackSummary(all))}</span>` +
      (!o.readonly && open.length && canEditLibrary()
        ? `<button type="button" class="lx-link lx-edit" data-lx-act="accept-all" data-lx-ids="${open.map(p => p.id).join(',')}">Accept all</button>` : '') +
      `</div><table class="lx-tbl"><tbody>${rows.map((r, i) =>
        `<tr${i >= SHOW ? ' class="lx-more-row"' : ''}><td class="lx-n">${lxPad2(r.n)}</td><td>${lxTrackPiecesHtml(r.tp, { onlyNew: true })}</td>` +
        `<td class="lx-act-td">${o.readonly ? '' : lomaxActions(r.pieces)}</td></tr>`).join('')}</tbody></table>` +
      (rows.length > SHOW ? `<button type="button" class="lx-link lx-showall" data-lx-act="show">Show all ${rows.length}</button>` : '') +
      `</div>`
  }

  const lxList = items => `<ul class="lx-list">${items.map(v => `<li>${esc(stripCitations(v))}</li>`).join('')}</ul>`
  function lxEarHtml(run) {
    const v = (run.result && run.result.verify_items) || []
    return v.length ? `<div class="lx-sec"><div class="lx-h">Check by ear</div>${lxList(v)}</div>` : ''
  }
  function lxKeepHtml(run, c, o) {
    const v = (run.result && run.result.provenance_notes) || []
    if (!v.length) return ''
    const add = o.addNotes && !c.notesAdded[run.id] && canEditLibrary()
      ? `<button type="button" class="btn btn-ghost btn-sm lx-edit lx-notes" data-lx-act="notes" data-lx-run="${run.id}">Add to Notes</button>` : ''
    return `<div class="lx-sec"><div class="lx-h">Worth keeping</div>${lxList(v)}${add}</div>`
  }
  const lxAnswerHtml = run => {
    const a = run && run.result && run.result.answer
    return a && String(a).trim() ? `<div class="lx-answer">${esc(stripCitations(a))}</div>` : ''
  }

  function lxResultParts(run, c, o) {
    return [lxAnswerHtml(run), lxFieldCardHtml(run, o), lxTrackCardHtml(run, o), lxEarHtml(run), lxKeepHtml(run, c, o)].filter(Boolean)
  }

  function lxOldMessage(run, c, o) {
    const sug = lxSuggestions(run), acc = sug.filter(p => p.decision === 'accepted')
    const titles = acc.filter(p => /\.title$/.test(p.field)).length
    const parts = lxResultParts(run, c, Object.assign({}, o, { readonly: true }))
    const line = sug.length
      ? `${lxPlural(sug.length, 'suggestion')}, ${acc.length} accepted` + (titles ? ` · ${lxPlural(titles, 'title')} used` : '')
      : 'Nothing to add'
    return `<div class="lx-msg">${lomaxAvatar()}<div class="lx-bub lx-card lx-old"><div class="lx-old-h"><span>${esc(line)}</span>` +
      (parts.length ? `<button type="button" class="lx-link" data-lx-act="show">Show</button>` : '') + `</div>` +
      lomaxUsageLine(run) + `<div class="lx-old-body">${parts.join('')}</div></div></div>`
  }

  function lxResultMessage(run, c, o) {
    const parts = lxResultParts(run, c, Object.assign({}, o, { readonly: false }))
    return `<div class="lx-msg">${lomaxAvatar()}<div class="lx-bub">${parts.join('') || '<div class="lx-none">Nothing to add</div>'}${lomaxUsageLine(run)}</div></div>`
  }

  // o: {leave (show the leave-the-page line), addNotes (async fn(text), or null)}
  function lomaxChatHtml(c, o = {}) {
    if (!c.loaded) return '<div class="lx-empty">Loading…</div>'
    const latest = c.latest()
    let html = '', day = null
    for (const run of c.runs) {
      const d = lxDay(run.created_at)
      if (d && d !== day) { day = d; html += `<div class="lx-day">${esc(d)}</div>` }
      if (run.question) html += `<div class="lx-me"><div class="lx-me-b">${esc(run.question)}</div></div>`
      if (run.status === 'done') html += run === latest ? lxResultMessage(run, c, o) : lxOldMessage(run, c, o)
      else if (run.status === 'error') html += `<div class="lx-msg">${lomaxAvatar()}<div class="lx-bub">${lomaxRunState(run)}</div></div>`
      else html += `<div class="lx-msg">${lomaxRunState(run, { avatar: true, leave: o.leave })}</div>`
    }
    if (c.error) html += `<div class="lx-msg">${lomaxAvatar()}<div class="lx-bub"><div class="lx-error" role="alert">${esc(c.error)}</div></div></div>`
    if (!c.runs.length && !c.error) {
      html = `<div class="lx-empty">${lomaxAvatar()}<div>Lomax checks this recording's details, track titles and songwriters.</div></div>`
    }
    return html
  }

  // Mount the chat into a .slide-pane that holds
  //   <div class="slide-pane-scroll lx-scroll"></div><div class="lx-bar-slot"></div>
  // c is an existing controller (Add Recording shares one with its Resolver
  // tab) or null to make one from ctlCfg. Returns the controller.
  function lomaxMountChat(pane, ctlCfg, o = {}, c = null) {
    if (!pane) return null
    const body = pane.querySelector('.lx-scroll'), bar = pane.querySelector('.lx-bar-slot')
    const view = ctl => {
      if (!document.body.contains(pane)) return false
      lxRepaint(body, lomaxChatHtml(ctl, o), { stick: true })
      lxRepaint(bar, lomaxAskBar({ placeholder: 'Anything you want checked (optional)', busy: !!ctl.pending }))
      return true
    }
    const own = !c
    if (own) {
      c = lomaxController(Object.assign({ alive: () => document.body.contains(pane) }, ctlCfg))
    }
    pane.setAttribute('data-lx-ctl', c.id)
    if (o.addNotes) {
      const prev = c.cfg.onAct
      c.cfg.onAct = async (act, btn, ctl) => {
        if (act !== 'notes') { if (prev) prev(act, btn, ctl); return }
        const run = ctl.runs.find(r => r.id === Number(btn.dataset.lxRun))
        if (!run) return
        btn.disabled = true
        try {
          await o.addNotes(((run.result && run.result.provenance_notes) || []).map(stripCitations).join('\n'))
          ctl.notesAdded[run.id] = true
        } catch (e) { ctl.error = lxErrText(e) }
        ctl.refresh()
      }
    }
    c.addView(view)
    if (own) c.load(); else view(c)
    return c
  }

  // Bring the Trellis column up to date with the form, in place. Not a repaint:
  // a field losing focus fires this just before the click that moved focus, and
  // replacing the buttons under the pointer would lose that click.
  function lxSyncResolver(root, o) {
    if (!root) return
    root.querySelectorAll('[data-lx-tv]').forEach(el => { el.textContent = o.value(el.dataset.lxTv) || '' })
    root.querySelectorAll('[data-lx-tp]').forEach(el => {
      const [n, f] = el.dataset.lxTp.split(':')
      const t = (o.tracks || []).find(x => String(x.track_number) === n)
      const v = t ? (t[f] || '') : ''
      el.textContent = v
      if (f === 'title') el.classList.toggle('lx-dim', !v || /^Track \d+$/i.test(v.trim()))
    })
    // A genre in the form is never replaced: its accept and dismiss go once the field is filled.
    const filled = !!(o.value('genre') || '')
    root.querySelectorAll('[data-lx-genre-acts] .lx-acts').forEach(el => { el.style.display = filled ? 'none' : '' })
  }

  // ── Lomax: the Resolver tab (Add Recording and import review) ───────────────
  // One compact table, Field | Trellis | Lomax | actions, for all ten fields,
  // then the tracks on a lighter surface, then the Answer and Check by ear. The
  // ask bar is pinned to the bottom of the panel (sticky, solid ground) and
  // looks the same before and after a run. The Trellis column is read live from
  // the form (o.value), so an edit shows without a new run.
  // o: {resolved, value(field), tracks, leave, genreRow, mbGenre}
  // Tracks: each of Title, Songwriter and Notes is its own row with its own Trellis cell, Lomax cell
  // and accept / dismiss, so Title and Songwriter can be accepted and Notes dismissed. Accept all
  // takes titles and songwriters only; a note is always decided by itself.
  const LX_TRACK_ROWS = [['title', 'Title', 'title'], ['songwriter', 'Songwriter', 'songwriter'], ['note', 'Notes', 'notes']]
  const lxIconBtn = (act, label, ico, cls) =>
    `<button type="button" class="lx-ic ${cls}" data-lx-act="${act}" aria-label="${label}" title="${label}">${icon(ico)}</button>`

  // Genre: Lomax's proposal, else what MusicBrainz offers for the act (neutral ink, it is not Lomax's).
  function lxGenreRowHtml(fp, o) {
    const p = fp.genre, mb = o.mbGenre
    let cell = '', acts = ''
    if (lxShown(p)) { cell = lomaxSuggestionCell(p); acts = lomaxActions(p) }
    else if (mb && mb.name && mb.state !== 'dismissed') {
      const done = mb.state === 'accepted'
      cell = `<span class="${done ? 'lx-same' : 'lx-mb'}">${esc(mb.name)}</span>`
      acts = done ? '<span class="lx-done">Accepted</span>'
        : (canEditLibrary() ? `<span class="lx-acts lx-edit">${lxIconBtn('mb-genre-accept', 'Accept', 'check', 'lx-ok')}${lxIconBtn('mb-genre-dismiss', 'Dismiss', 'x', 'lx-no')}</span>` : '')
    }
    return `<tr><th scope="row">Genre</th><td><span class="lx-tv" data-lx-tv="genre">${esc(o.value('genre') || '')}</span></td>` +
      `<td>${cell}</td><td class="lx-act-td" data-lx-genre-acts>${acts}</td></tr>`
  }

  function lxTrackRowsHtml(tracks, tp) {
    return tracks.map(t => {
      const n = t.track_number, props = tp[n] || {}
      const out = []
      for (const [k, label, tk] of LX_TRACK_ROWS) {
        const p = props[k], now = t[tk] || ''
        if (k !== 'title' && !now && !lxShown(p)) continue      // nothing to show on either side
        const first = out.length === 0
        const dim = k === 'title' && (!now || /^Track \d+$/i.test(String(now).trim()))
        out.push(`<tr${first ? ' class="lx-trk"' : ''}>${first ? `<td class="lx-n" rowspan="@@">${lxPad2(n)}</td>` : ''}` +
          `<th scope="row" class="lx-pk">${label}</th>` +
          `<td data-lx-tp="${n}:${tk}"${dim ? ' class="lx-dim"' : ''}>${esc(now)}</td>` +
          `<td>${lomaxSuggestionCell(p)}</td><td class="lx-act-td">${lomaxActions(p)}</td></tr>`)
      }
      return out.join('').replace('rowspan="@@"', `rowspan="${out.length}"`)
    }).join('')
  }

  function lomaxResolverHtml(c, o) {
    const run = c.latest()
    const fp = lxFieldProps(run), tp = lxTrackProps(run)
    const plain = (o.resolved && o.resolved.sources_plain) || {}
    let rows = LX_FIELDS.map(([k, label]) => {
      const f = (o.resolved && o.resolved[k]) || {}
      const tentative = f.confidence === 'tentative' ? ' <span class="lx-tent">Tentative</span>' : ''
      const p = fp[k]
      return `<tr><th scope="row">${label}</th>` +
        `<td><span class="lx-tv" data-lx-tv="${k}">${esc(o.value(k) || '')}</span>\u2060${tentative}${lomaxSourcesPopover(plain[k], k)}</td>` +
        `<td>${lomaxSuggestionCell(p)}</td><td class="lx-act-td">${lomaxActions(p)}</td></tr>`
    }).join('')
    if (o.genreRow || lxShown(fp.genre)) rows += lxGenreRowHtml(fp, o)

    const tracks = o.tracks || []
    const bulk = []
    for (const n of Object.keys(tp)) for (const k of ['title', 'songwriter']) if (lxOpen(tp[n][k])) bulk.push(tp[n][k].id)
    const acceptAll = bulk.length && canEditLibrary()
      ? `<button type="button" class="lx-link lx-edit" data-lx-act="accept-all" data-lx-ids="${bulk.join(',')}">Accept all</button>` : ''

    const err = c.lastError()
    const q = run && run.question ? `<div class="lx-h">You asked: ${esc(run.question)}</div>` : ''
    const answer = lxAnswerHtml(run)
    const state = c.pending ? lomaxRunState(c.pending, { leave: o.leave }) : (err ? `<div class="lx-error" role="alert">${esc(err)}</div>` : '')
    return `<div class="lx-res-body">
      ${state}
      <table class="lx-tbl lx-fields"><thead><tr><th>Field</th><th>Trellis</th><th>Lomax</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      ${tracks.length ? `<div class="lx-sec"><div class="lx-h">Tracks</div><table class="lx-tbl lx-tracks"><thead><tr><th>#</th><th>Track</th><th>Trellis</th><th>Lomax</th><th class="lx-act-td">${acceptAll}</th></tr></thead><tbody>${lxTrackRowsHtml(tracks, tp)}</tbody></table></div>` : ''}
      ${answer ? `<div class="lx-sec">${q}${answer}</div>` : ''}
      ${lxEarHtml(run || {})}
    </div>${lomaxAskBar({ pin: true, placeholder: 'Anything you want checked (optional)', last: run, busy: !!c.pending })}`
  }

  // The same Resolver table for a saved recording (View Recording). The Trellis column is what is
  // saved now; the stored resolver_json gives the Sources popovers and the Tentative marks. Genre
  // shows when the act has none (or Lomax proposed one).
  function lxRecordingResolverOpts(rec, perf) {
    const p = perf || {}
    const pad = n => String(n).padStart(2, '0')
    const date = p.start_year
      ? `${p.start_year}${p.start_month ? '-' + pad(p.start_month) : ''}${(p.start_month && p.start_day) ? '-' + pad(p.start_day) : ''}` : ''
    const vals = { artist: p.artist, date, venue: p.venue_name, city: p.city, state: p.state, country: p.country,
      event: p.event_name, stage: p.stage, source: rec.source, lineage: rec.lineage, genre: p.artist_genre }
    return {
      resolved: rec.resolver_json || {},
      value: k => (vals[k] == null ? '' : String(vals[k])),
      tracks: rec.tracks || [],
      genreRow: !p.artist_genre,
      leave: true,
    }
  }

  /** Recording detail — split panel: tracks + info file */
  async function renderRecordingView(recordingId) {
    setLoading()
    state.currentRecId      = recordingId
    state._lastTrackCount   = null   // reset until rec loads

    let rec
    try {
      rec = await API.recordings.get(recordingId)
      state._lastTrackCount = rec.tracks?.length ?? null
      // A studio record lives under Albums, not Live Recordings (2026-10-01).
      // Set after the fetch so the wrong item never flashes while loading.
      setActiveNav(rec.kind === 'studio' ? 'albums' : 'library')
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Recording not found</div></div>`)
      return
    }

    // "← Back" points at whatever page immediately preceded this one — the
    // generic navBack mechanism (route()), not the old state.selectedArtist
    // hack that only worked if you'd arrived via an Artist page (Ryan's
    // 2026-07-23 bug report: Recently Added → Recording → Back landed on
    // Library, since selectedArtist was never set by Recently Added).
    // The visible "← Library" link is gone (Ryan, 2026-08-22) — the App
    // Header's back arrow does this job for every view at once. state.navBack
    // survives because it is still the right destination after a delete or a
    // move: those actions destroy the page you are on, so "go back" is more
    // useful than "go to the previous entry in the history stack", which would
    // be this same dead recording.
    const backHash  = state.navBack ? state.navBack.hash : '#/'

    // We need performance info to show the date/venue
    let perf = null
    try { perf = await API.performances.get(rec.performance_id) } catch (_) {}

    const dateStr    = perf ? fmtDateRangeLong(perf) : ''
    const venueStr   = perf?.venue_name || ''
    const venueId    = perf?.venue_id   || null
    const locStr     = perf ? fmtLocation(perf.city, perf.state, perf.country) : ''
    const perfName   = perf?.artist || ''
    const perfId     = perf?.artist_id || null
    const eventStr   = perf?.event_name || ''
    const stageStr   = perf?.stage || ''
    setNavCurrent(dateStr || perfName || 'Recording')

    // Small "go to its own page" nav icons (2026-07-23) — same treatment for
    // Artist and Venue, shown regardless of edit permission since it's
    // navigation, not editing. Plain hash links — the generic navBack
    // mechanism (route()) picks up the "came from a recording" reference
    // automatically, no per-link wiring needed.
    const perfNavLink = perfId
      ? `<a class="rec-nav-link" href="#/artist/${perfId}" title="Go to ${esc(perfName)}'s page">↗</a>` : ''
    const venueNavLink = venueId
      ? `<a class="rec-nav-link" href="#/venue/${venueId}" title="Go to ${esc(venueStr)}'s page">↗</a>` : ''

    // Date line — venue is a clickable link if we have a venue_id
    const venueHtml  = venueId
      ? `<span class="venue-link" data-venue-id="${venueId}">${esc(venueStr)}</span>${venueNavLink}`
      : (venueStr ? esc(venueStr) : '')
    const dateLineParts = [dateStr ? esc(dateStr) : '', venueHtml, locStr ? esc(locStr) : ''].filter(Boolean)
    const dateLineHtml  = dateLineParts.join(' · ')

    // Staged changes: metadata_updated events after the last tags_written
    // events array is ascending by created_at (oldest first)
    const events = rec.events || []
    let lastWritePos = -1
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].event_type === 'tags_written') { lastWritePos = i; break }
    }
    const stagedCount = events
      .slice(lastWritePos + 1)   // everything after the last write (or all if never written)
      .filter(e => e.event_type === 'metadata_updated')
      .length

    // Rename Files staging (spec section 6.4): the GET payload already says
    // how many tracks the active global scheme would rename, no disk access
    // needed here to decide the button's amber state.
    const filesStaged = rec.files_staged || 0

    // Inner HTML of a track's title cell: title + official badge + flag chips
    // + inline note. Factored so the right-click quick-edit menu can refresh a
    // single row in place after changing flags or notes.
    function trackTitleInnerHtml(t) {
      const badges = trackBadgesHtml(t)
      return `<span class="track-title-text">${esc(t.title)}</span>${badges ? ' ' + badges : ''}`
    }

    // Track list. Grouped under set headers when any track carries a
    // set_number (spec section 6.4, mockup panel 4e); flat otherwise --
    // same row markup either way. Note/Songwriter/Set are click-to-edit
    // directly in the row; right-click is Flags (+ Official) only --
    // matches Add Recording's track table treatment (Ryan, 2026-07-15).
    // Grouping is markup only: it never touches Player or playback order,
    // which follows the continuous track_number as it always has.
    const canEdit  = canEditLibrary()
    // The Resolver tab is the Lomax table. Lomax calls are not proxied to a peer, so a joined
    // library (which also sends no resolver_json) has none.
    const showResolver = libraryState.activeId == null
    const editHint = canEdit ? ' title="Click title to rename · right-click for flags"' : ''
    // Set no longer has a cell: it is edited from the track's right-click menu.
    // The set group headers in the list still show it.
    // No disc-track cell (Ryan, 2026-10-04), even when the metadata has discs.
    function trackRowHtml(t) {
      const isPlaying  = t.id === state.playingTrackId
      const playingCls = isPlaying ? ' playing' : ''
      const playIcon   = icon(isPlaying ? 'pause' : 'play')
      return `
        <div class="track-row${playingCls}" data-track-id="${t.id}" data-flags="${(t.flags||[]).join(',')}"${editHint}>
          <span class="track-play">${playIcon}</span>
          <span class="track-num">${String(t.track_number || '').padStart(2,'0')}</span>
          <span class="track-title-wrap">
            <span class="track-title truncate${canEdit ? ' track-title--editable' : ''}">${trackTitleInnerHtml(t)}</span>
          </span>
          <span class="track-note-col truncate${canEdit ? ' pp-editable' : ''}${t.notes ? '' : ' pp-empty'}" id="t-note-${t.id}" title="${esc(t.notes || (canEdit ? 'Click to add a note' : ''))}">${esc(t.notes || (canEdit ? '—' : ''))}</span>
          <span class="track-sw-col truncate${canEdit ? ' pp-editable' : ''}${t.songwriter ? '' : ' pp-empty'}" id="t-sw-${t.id}" title="${esc(t.songwriter || (canEdit ? 'Click to add a songwriter' : ''))}">${esc(t.songwriter || (canEdit ? '—' : ''))}</span>
          <span class="track-dur">${fmtDuration(t.duration)}</span>
        </div>`
    }
    // Column headers for everything after the title (Ryan, 2026-10-04): same
    // grid as .track-row so they sit over their cells. Play, number and title
    // need none. The disc cell is blank on a single-disc recording.
    function trackHeadHtml() {
      if (!(rec.tracks || []).length) return ''
      return `
        <div class="track-row track-head" aria-hidden="true">
          <span></span><span></span>
          <span class="track-head-title">Title</span>
          <span class="track-head-note">Notes</span>
          <span class="track-head-sw">Songwriter</span>
          <span class="track-head-dur">Time</span>
        </div>`
    }
    // Groups form by set_number label in first-appearance order -- the same
    // rule the derived set_track_number/track_in_set use (spec section 5),
    // so the header order and the per-set numbering always agree.
    function buildTrackListHtml() {
      const tracks = rec.tracks || []
      if (!tracks.some(t => t.set_number)) return tracks.map(trackRowHtml).join('')
      const groups = []
      tracks.forEach(t => {
        const label = t.set_number || null
        let g = groups.find(g => g.label === label)
        if (!g) { g = { label, items: [] }; groups.push(g) }
        g.items.push(t)
      })
      return groups.map(g =>
        (g.label ? `<div class="track-set-header">${esc(g.label)}</div>` : '') +
        g.items.map(trackRowHtml).join('')
      ).join('')
    }
    let trackRows = buildTrackListHtml()

    // Info File is READ-ONLY until asked otherwise (Ryan, 2026-08-21). It used
    // to be a live textarea that autosaved on blur, matching Add Recording —
    // but Add Recording is a form you came to in order to type, and a recording
    // page is a page you came to in order to listen. An always-hot textarea on
    // the listening surface means a stray click plus a keystroke silently
    // rewrites the taper's own words. Admins get an explicit "Edit File" link;
    // everyone else never sees an editable control at all.
    //
    // Still a <textarea> rather than a <pre>-swapped-for-a-textarea, so that
    // unlocking preserves scroll position and needs no re-render: the lock is
    // the `readonly` attribute plus a class that strips the input chrome.
    const infoContent = canEdit
      ? `<textarea class="rev-info-text rev-info-edit rev-info-text--locked" id="rec-info-edit"
          readonly placeholder="No info file found.">${esc(rec.info_file_content || '')}</textarea>`
      : (rec.info_file_content
          ? `<pre class="info-file-content">${esc(rec.info_file_content)}</pre>`
          : `<div class="info-panel-empty">No info file attached</div>`)

    // Per-track "has analysis" map (gates the waveform banner + Fidelity tab)
    // and duration lookup (needed alongside peaks whenever wavesurfer (re)loads).
    _waveformMap      = {}
    _trackDurationMap = {}
    ;(rec.tracks || []).forEach(t => {
      const wf = t.analysis?.waveform
      const hasWf = Array.isArray(wf) ? wf.length > 0 : !!(wf && wf.max && wf.max.length)
      if (hasWf) _waveformMap[t.id] = wf
      _trackDurationMap[t.id] = t.duration || 0
    })
    const hasAnalysis = Object.keys(_waveformMap).length > 0

    // Fidelity metrics used to be computed here and rendered as a second tab of
    // the top-right box — raw librosa readings (RMS, Dyn Range, cutoff) in one
    // vocabulary, while the Listening Quality engine described the same property
    // in another vocabulary elsewhere on the same page. Two systems, one
    // property. Unified 2026-08-18 (IO-61): the engine is now the single quality
    // surface and it lives in the Side Panel, which has the room for its three
    // meters and the metrics underneath them. Everything the old Fidelity tab
    // showed is still on the page — format, cutoff, dynamics and the
    // spectrogram all come out of the same feature dict the engine scores from,
    // so nothing was dropped, it was de-duplicated.
    //
    // The top-right box is therefore no longer a tabbed element at all. It is a
    // plain borderless block of the three things a human typed.
    const firstAnalysed = rec.tracks?.find(t => t.analysis) ?? null

    // Quick-edit in place is unchanged — click a value, type, Enter to save.
    const trunc          = (s, n) => s && s.length > n ? s.slice(0, n) + '\u2026' : s
    const lineageDisplay = rec.lineage ? trunc(rec.lineage, 220) : null

    const qEditable = canEditLibrary()
    const qc  = qEditable ? ' hm-val--editable' : ''
    const qa  = f => qEditable ? ` data-qedit="${f}" title="Click to edit"` : ''
    // Source and Quality render with the SAME chips the Library rows, cards and
    // search results use (Ryan, 2026-08-21) — a coloured source badge and the
    // graded quality colour — rather than the flat grey text they used to carry.
    // They are the two fastest reads on a recording, and a listener scanning a
    // list then opening a show should not have to re-learn what "SBD" looks
    // like on the way in. Editability is unchanged wherever they land: the chip
    // sits INSIDE the .hm-val cell rather than replacing it, so the quick-edit
    // handler still finds .hm-val--editable[data-qedit] and swaps its innerHTML.
    //
    // Rating (the Quality letter grade) was promoted to the action row alone
    // 2026-08-21, then moved back down 2026-08-27 (Ryan) to sit left of Source
    // — Rating, Source and Lineage read as one row of "the tape's paperwork
    // plus the verdict on it" rather than Rating living apart from the two
    // facts it's a judgement about.
    //
    // Aligned with the Members/Guests rows above it — same .mg-row-label type
    // treatment and the same left edge, because it is the same kind of content.
    //
    // Studio kind (Ryan, 2026-09-27): a studio release carries none of the
    // tape paperwork above (no Source/Source tag/shnid/Lineage — those
    // describe a taper's transfer chain, which a studio release doesn't
    // have). It gets Rating plus a Release item instead, holding the same
    // #rec-release mount renderReleaseBlock() already targets — moved up
    // from the standalone pp-block below the header into this row so
    // Release reads as one more fact in the row rather than its own box.
    const isStudioKind = rec.kind === 'studio'
    const releaseIsLinked = rec.mb_release_status === 'matched' || rec.mb_release_status === 'linked'
    // Admin-in-Admin-mode gets the interactive block (renderReleaseBlock, wired
    // inside the canEdit section below); anyone else sees a linked release as
    // static facts rendered here, since that wiring never runs for them.
    const releaseAdmin = isAdmin() && canEdit
    const showReleaseBlock = isStudioKind && (releaseAdmin || releaseIsLinked)
    const releaseStaticHtml = (!releaseAdmin && releaseIsLinked) ? `
            <div class="pp-mb-linked">
              <a class="pp-mb-name" href="${esc(mbReleaseUrl(rec.mb_release_id))}" target="_blank" rel="noopener">${esc(
                [rec.mb_label, rec.mb_catalog_number, rec.mb_release_country].filter(Boolean).join(' · ') || rec.title || 'Release')} ↗</a>
            </div>` : ''
    const releaseItemHtml = showReleaseBlock ? `
        <div class="rec-sl-item rec-sl-item--release">
          <span class="mg-row-label">Release</span>
          <div id="rec-release">${releaseStaticHtml}</div>
        </div>` : ''
    const discsItemHtml = rec.disc_count > 1 ? `
        <div class="rec-sl-item">
          <span class="mg-row-label">Discs</span>
          <span class="hm-val">${esc(String(rec.disc_count))}</span>
        </div>` : ''
    // Rating (the letter-grade chip) is a live-show judgement — Ryan does not
    // grade studio releases, so it never appears in a studio Provenance Row.
    const rowIsEmpty = isStudioKind
      ? (!showReleaseBlock)
      : (!qEditable && !rec.source && !rec.lineage && !rec.quality
         && !rec.source_tag && !rec.etree_shnid)
    const sourceLineageRow = rowIsEmpty ? '' : `
      <div class="rec-sl-row">
        ${!isStudioKind ? `<div class="rec-sl-item rec-sl-item--quality">
          <span class="mg-row-label">Rating</span>
          <span class="hm-val hm-val--chip${qc}"${qa('quality')}><span class="quality ${qualityClass(rec.quality)}">${esc(rec.quality || '\u2014')}</span></span>
        </div>` : ''}
        ${!isStudioKind ? `<div class="rec-sl-item">
          <span class="mg-row-label">Source</span>
          <span class="hm-val hm-val--chip${qc}"${qa('source')}>${sourceBadge(rec.source) || '\u2014'}</span>
        </div>
        <div class="rec-sl-item">
          <span class="mg-row-label">Source Tag</span>
          <span class="hm-val${qc}"${qa('source_tag')}>${esc(rec.source_tag || '\u2014')}</span>
        </div>
        <div class="rec-sl-item">
          <span class="mg-row-label">SHNID</span>
          <span class="hm-val mono${qc}"${qa('etree_shnid')}>${esc(rec.etree_shnid != null ? String(rec.etree_shnid) : '\u2014')}</span>
        </div>` : ''}
        ${discsItemHtml}
        ${!isStudioKind ? `<div class="rec-sl-item rec-sl-item--lineage">
          <span class="mg-row-label">Lineage</span>
          <span class="hm-val${qc}"${qa('lineage')}>${esc(lineageDisplay || rec.lineage || '\u2014')}</span>
        </div>` : ''}
        ${releaseItemHtml}
      </div>`

    // Analyze Audio lives in the Side Panel's tab strip with every other pane
    // action (2026-08-21) — see the .pane-acts block below. POST /reprocess runs
    // the whole audio pass (score, signals, librosa) for any recording, so the
    // button and the quality verdict beside it can no longer disagree.

    // Which track to show by default: currently playing (if in this rec) else first track
    const firstTrack    = rec.tracks?.[0] ?? null
    const defaultTrackId = (state.playingTrackId && _waveformMap[state.playingTrackId])
      ? state.playingTrackId
      : (firstTrack?.id ?? null)

    // Collections moved out of the box, up to the top row alongside the back
    // link (Ryan, 2026-07-15).
    const collectionArea = `
      <div class="rec-collections" id="rec-collections">
        ${(rec.collections || []).map(collectionTagHtml).join('')}
        ${libraryState.activeId == null ? `
        <button class="collection-add-btn" id="btn-add-collection">+ Add to Collection</button>` : ''}
        <!-- Favorite toggle (moved here 2026-08-09 — was a star icon beside
             the title). Sits next to Add to Collection since both are "mark
             this recording" actions: one files it into a set, the other is a
             single personal flag. Text-led rather than icon-only, per Ryan —
             visible to everyone including listeners, since a highlight is a
             personal reaction, not a library edit. -->
        <button class="fav-toggle-btn${viewerHasFavorited(rec) ? ' is-fav' : ''}" id="btn-favorite"
                aria-pressed="${viewerHasFavorited(rec) ? 'true' : 'false'}">${
          viewerHasFavorited(rec) ? 'Favorited' : 'Mark as Favorite'}</button>
        <!-- Actions menu (Ryan, 2026-08-21). Replaces the .rec-bottom-actions
             row that used to sit under the track list: four admin buttons at
             the far end of a page whose main content scrolls, so reaching
             Delete meant scrolling past every track. These are per-recording
             admin verbs, they belong with the other per-recording controls,
             and a menu keeps them from competing with Play All for attention
             on a listening surface. Write Tags is NOT here — it moved into
             the File Tags pane, beside the tags it writes. -->
        ${canEdit ? `
        <div class="rec-actions-wrap">
          <button class="actions-btn" id="btn-rec-actions" aria-expanded="false"
                  aria-haspopup="true">Actions ${chevronIcon('caret-ic--down')}</button>
          <div class="actions-menu" id="rec-actions-menu" hidden role="menu">
            <button class="actions-item" role="menuitem" data-act="reveal">Open in Containing Folder</button>
            <button class="actions-item" role="menuitem" data-act="official">${
              rec.is_official ? 'Official Release' : 'Mark as Official Release'}</button>
            <button class="actions-item" role="menuitem" data-act="kind">${
              rec.kind === 'studio' ? 'Classify as Live Recording' : 'Classify as Album'}</button>
            <!-- Move to — same two destinations as the triage queue's Move,
                 deliberately: Workshop and Backlog are the two real folders a
                 show goes back to, and having a different vocabulary before
                 and after ingest would be the kind of small inconsistency
                 that makes people hesitate. -->
            ${rec.is_published === false ? `
            <div class="actions-note">Out of the library, in Workshop or Backlog</div>` : `
            ${triageDests().length ? `
            <button class="actions-item" role="menuitem" data-act="move-toggle" aria-expanded="false">Move to ${chevronIcon()}</button>
            <div class="actions-submenu" id="rec-move-sub" hidden>
              ${triageDestButtons('actions')}
            </div>` : ''}`}
            <div class="actions-sep"></div>
            <button class="actions-item actions-item--danger" role="menuitem" data-act="delete">Delete Recording</button>
          </div>
        </div>` : ''}
      </div>`

    // The recording's ONE square (Ryan, 2026-10-04). It used to be two -- the
    // artist's avatar and a separate cover slot -- and is now the recording's
    // image by the chain the server resolves into rec.image_url: its own image,
    // else the artist's, else the artist's initials. Clicking it opens the image
    // modal (zoom, and for an editor the gallery). Genre-colour ring, as the
    // artist avatar it replaces had. Not a button when there is nothing to open:
    // initials only, and no edit rights.
    const imageClickable = !!(rec.image_url || canEdit)
    const coverBlockHtml = `
      <div class="rec-header-avatar${imageClickable ? ' rec-header-avatar--btn' : ''}" id="rec-cover"
           style="--genre-fg:${esc(perf?.artist_genre_color || 'var(--bd-1)')}"${imageClickable ? ' role="button" tabindex="0"' : ''}></div>`

    // Studio heading + sub-line (Ryan, 2026-09-27 — owner-approved design).
    // The album title takes the h2 that a live recording gives the artist;
    // the artist name drops down into the sub-line that a live recording
    // spends on date/venue/location/event, none of which a studio release
    // has. If the ALBUM tag never gave us a title, the heading falls back to
    // exactly what a live recording shows (artist name, same id/wiring) —
    // the sub-line still carries artist/year/track-count/length below it,
    // since that information does not depend on having a title to put above it.
    const studioTitleHtml = rec.title ? `
          <div class="rec-name-row">
            <h2 class="rec-perf-name rec-perf-name--album${canEdit ? ' pp-editable' : ''}" id="rec-title"${canEdit ? ' title="Click to edit"' : ''}>${esc(rec.title)}</h2>
          </div>` : `
          <div class="rec-name-row">
            <h2 class="rec-perf-name${canEdit ? ' pp-editable' : ''}" id="rec-perf-name"${canEdit ? ' title="Click to reassign artist"' : ''}>${esc(perfName) || (canEdit ? '<span class="pp-empty">Set artist</span>' : '')}</h2>
            ${perfNavLink}
          </div>`
    const studioTrackCount = rec.tracks?.length || 0
    const studioTrackCountStr = studioTrackCount
      ? `${studioTrackCount} track${studioTrackCount === 1 ? '' : 's'}` : null
    const studioTotalMinutes = Math.round((rec.tracks || []).reduce((sum, t) => sum + (t.duration || 0), 0) / 60)
    const studioLengthStr = studioTotalMinutes > 0 ? `${studioTotalMinutes} min` : null
    const studioYearStr = perf?.start_year ? String(perf.start_year) : null
    const studioArtistHtml = rec.title
      ? `<span class="rec-f rec-f-artist${canEdit ? ' pp-editable' : ''}" id="rec-perf-name"${canEdit ? ' title="Click to reassign artist"' : ''}>${esc(perfName) || (canEdit ? '<span class="pp-empty">Set artist</span>' : '')}</span>${perfNavLink}`
      : null
    const studioSubParts = [studioArtistHtml, studioYearStr ? esc(studioYearStr) : null,
      studioTrackCountStr ? esc(studioTrackCountStr) : null, studioLengthStr ? esc(studioLengthStr) : null].filter(Boolean)
    const studioSubLineHtml = `
          <div class="rec-date-line" id="rec-date-line">
            ${studioSubParts.join('<span class="rec-dot">·</span>')}
          </div>`

    setMainHTML(`
      <div class="rec-view-shell">
      ${hasAnalysis ? `
      <!-- Waveform banner — spans full width above everything, incl. the back
           link (Ryan, 2026-07-15: "placed at the top of the screen, above
           everything"). Hidden entirely until analysis exists. Rendered with
           wavesurfer.js (vendored locally under /js/vendor/ — no CDN, this
           app runs offline) — adopted officially 2026-07-15 after a spike;
           replaces the old hand-rolled canvas renderer. -->
      <div class="rec-waveform-wrap" id="rec-waveform-wrap">
        <div id="rec-waveform-ws" class="rec-waveform-ws"></div>
      </div>` : ''}
      <div class="rec-detail-header">
        <!-- Two rows since 2026-08-21: the identity row (avatar + lines +
             then Notes spanning the full width beneath it. Notes used to sit
             inside .rec-header-lines, which capped it at the left column's
             width and at 480px on top of that — a paragraph of taper notes
             wrapped into a narrow ribbon with half the header empty beside it.
             The right-hand meta block that emptiness belonged to is gone as of
             2026-08-21; see the sourceLineageRow comment (Rating rejoined
             it 2026-08-27). -->
        <div class="rec-header-main">
        <div class="rec-header-left">
          <!-- Recording image (see coverBlockHtml) — to the left of the name
               and date lines, spanning both. -->
          ${coverBlockHtml}
          <div class="rec-header-lines">
          ${isStudioKind ? studioTitleHtml : `
          <div class="rec-name-row">
            <h2 class="rec-perf-name${canEdit ? ' pp-editable' : ''}" id="rec-perf-name"${canEdit ? ' title="Click to reassign artist"' : ''}>${esc(perfName) || (canEdit ? '<span class="pp-empty">Set artist</span>' : '')}</h2>
            ${perfNavLink}
          </div>`}
          ${isStudioKind ? studioSubLineHtml : `
          <div class="rec-date-line" id="rec-date-line">
            <span class="rec-f rec-f-date${canEdit ? ' pp-editable' : ''}" id="rec-f-date">${dateStr ? esc(dateStr) : (canEdit ? '<span class="pp-empty">Add date</span>' : '')}</span>
            <span class="rec-dot">·</span>
            ${canEdit
              ? `<span class="rec-f rec-f-venue pp-editable" id="rec-f-venue">${venueStr ? esc(venueStr) : '<span class="pp-empty">Add venue</span>'}</span>${venueNavLink}`
              : (venueHtml || '')}
            ${locStr ? `<span class="rec-dot">·</span><span class="rec-f-loc">${esc(locStr)}</span>` : ''}
            ${canEdit
              ? `<span class="rec-dot">·</span><span class="rec-f rec-f-event pp-editable${eventStr ? '' : ' pp-empty'}" id="rec-f-event" title="Click to set the festival/event this show is part of">${eventStr ? esc(eventStr) : 'Add event'}</span>`
              : (eventStr ? `<span class="rec-dot">·</span><span class="rec-f-loc">${esc(eventStr)}</span>` : '')}
            ${canEdit
              ? `<span class="rec-dot">·</span><span class="rec-f rec-f-stage pp-editable${stageStr ? '' : ' pp-empty'}" id="rec-f-stage">${stageStr ? esc(stageStr) : 'Add stage'}</span>`
              : (stageStr ? `<span class="rec-dot">·</span><span class="rec-f-loc">${esc(stageStr)}</span>` : '')}
          </div>`}
          <div class="rec-musicians-row" id="rec-musicians"></div>
          ${sourceLineageRow}
          ${(rec.is_official || rec.is_published === false) ? `<div class="badge-row">
            ${rec.is_published === false ? `<span class="badge-unpublished" title="This recording's folder was moved out of the library to Workshop or Backlog. The library record is intact; playback will not work until it comes back.">Out of Library</span>` : ''}
            ${rec.is_official ? `<span class="badge-official" title="Contains officially released material">© Official</span>` : ''}
          </div>` : ''}
          <div class="rec-header-notes${canEdit ? ' pp-editable' : ''}${rec.notes ? '' : ' pp-empty'}" id="rec-notes"${canEdit ? ' title="Click to edit notes"' : ''}>${rec.notes ? esc(rec.notes) : (canEdit ? 'Add notes…' : '')}</div>
          </div>
        </div>
        <!-- Action cluster (collections, favorite, Actions menu) — sibling of
             .rec-header-left inside .rec-header-main as of 2026-08-27, not its
             own row above the header. It used to be a full row of its own
             (originally aligned with a back link removed 2026-08-22), which
             left it floating above the Artist Name with a big dead gap
             between them. Pulled level with the top of the avatar/name block
             instead (Ryan). -->
        <div class="rec-header-actions">
          ${collectionArea}
        </div>
        </div>
      </div>
      <div class="action-bar">
        <!-- Playback actions only — editing/admin actions live at the bottom -->
        <button class="btn btn-ghost btn-sm" id="btn-play-all">Play All</button>
        ${!isStudioKind ? `<label class="skip-toggle skip-toggle--action" title="Skip announcements, banter &amp; tuning from queue">
          <input type="checkbox" class="skip-filter-cb" id="skip-filter-action" ${state.skipNonMusic ? 'checked' : ''} />
          <span class="skip-toggle-track"></span>
          <span class="skip-toggle-label">Skip Non-Music</span>
        </label>` : ''}
        ${canEdit ? panelToggleHtml('slide-rail') : ''}
      </div>
      <div class="detail-panels" id="detail-panels">
        <div class="track-panel" id="track-panel">
          ${trackHeadHtml()}${trackRows || '<div class="info-panel-empty">No tracks</div>'}
        </div>

        <!-- The Details pane is an ADMIN surface and is not rendered at all in
             Playback mode (Ryan, 2026-08-21) — not hidden with CSS, absent.
             Info file, quality metrics, Vorbis comments and checksums are an
             archivist's working set; a listener opening a show wants the tracks
             and the transport. Removing it also gives the track list the full
             width, which is the point of the mode. -->
        ${canEdit ? `
        <!-- Slide-in right panel: the Details pane. Tabs are an index column at
             its right edge (detailsTabsHtml, shared with Add Recording); the
             show/hide toggle is in the action bar above, outside the panel, so
             it stays put while the panel slides (Ryan, 2026-10-04). -->
        <div class="slide-panel slide-panel--htabs slide-panel--index" id="slide-panel">
          <div class="slide-panel-main">

          <!-- Actions for the active pane (and the staged-edits note), one pane's
               controls at a time. Tabs live in the index column at the right
               (Ryan, 2026-10-04), so this row has the panel's full width and the
               longest cluster (the note plus Write Tags and Rename) fits on one
               line. Every action for every pane is rendered once here and shown
               by data-for as the pane changes, so ids stay stable and existing
               wiring (markStaged's #btn-write-tags, wireReanalyze) keeps
               working with no lookup churn. The row hides itself when the active
               pane has nothing to offer. -->
          <div class="pane-acts" id="pane-acts">
              <span class="pane-title" id="pane-title"></span>
              ${canEdit ? `
              <span class="pane-act-status" id="rec-info-save-status" data-for="info"></span>
              <button class="pane-act act-suppressed" id="btn-rec-save-info" data-for="info" hidden disabled>Save to File</button>
              <button class="pane-act" id="btn-info-edit" data-for="info">Edit File</button>` : ''}
              ${canEdit ? `
              <button class="pane-act" id="btn-analyze-audio" data-for="quality">Analyze Audio</button>` : ''}
              ${canEdit ? `
              <span class="pane-act-note${stagedCount > 0 ? '' : ' act-suppressed'}" id="tags-staged-note" data-for="filetags"
                    ${stagedCount > 0 ? '' : 'hidden'}>Edits not yet written to the files</span>
              <button class="pane-act${stagedCount > 0 ? ' pane-act--staged' : ''}" id="btn-write-tags" data-for="filetags"
                      title="Write the database's metadata into the FLAC files' Vorbis comments">Write Tags to Files</button>
              <button class="pane-act${filesStaged > 0 ? ' pane-act--staged' : ''}" id="btn-rename-files" data-for="filetags"
                      title="Rename the files on disk to match a naming scheme">Rename Files</button>` : ''}
              <button class="pane-act" id="btn-cksum-revalidate" data-for="checksums"
                      title="Re-check against the files on disk">Re-validate</button>
          </div>

          <div class="slide-panel-body" id="slide-panel-body">

            <!-- Info File pane. Locked until "Edit File" is clicked; see the
                 infoContent comment above for why. Save stays disabled until
                 the text actually changes — "changes have been staged" is a
                 real precondition here, not decoration: the button writes to
                 the collector's disk. -->
            <div class="slide-pane" id="sp-info">
              <div class="slide-pane-scroll"><div class="rev-raw-section">${infoContent}</div></div>
            </div>

            <!-- Listening Quality pane — the single quality surface (IO-61,
                 2026-08-18). Verdict band + the three group meters up top,
                 each group's advanced metrics folded underneath it behind a
                 caret. Loaded lazily on first open: it is a second request and
                 most visits to a recording are to play it, not to audit it. -->
            <div class="slide-pane" id="sp-quality">
              <div class="slide-pane-scroll" id="sp-quality-body">
                <div class="info-panel-empty">Loading…</div>
              </div>
            </div>

            <!-- File Tags pane — actual on-disk Vorbis comments -->
            <div class="slide-pane" id="sp-filetags">
              <div class="slide-pane-scroll" id="sp-filetags-body">
                <div class="info-panel-empty">Loading…</div>
              </div>
            </div>

            <!-- Checksums pane — .ffp/.md5/.st5 fingerprint verification -->
            <div class="slide-pane" id="sp-checksums">
              <div class="slide-pane-scroll" id="sp-checksums-body">${buildChecksumsPaneHtml(rec.tracks)}</div>
            </div>

            ${showResolver && !isStudioKind ? `
            <!-- Resolver pane: the same Lomax table as Add Recording -->
            <div class="slide-pane" id="sp-resolver">
              <div class="slide-pane-scroll lx-col"><div class="lx-res" id="lx-res-root"></div></div>
            </div>` : ''}

            ${canEdit ? `
            <!-- Lomax pane: a chat. Messages scroll; the ask bar is its own
                 flex row at the bottom (lomaxMountChat fills both). -->
            <div class="slide-pane" id="sp-ai">
              <div class="slide-pane-scroll lx-scroll"></div>
              <div class="lx-bar-slot"></div>
            </div>` : ''}

          </div>
          </div>
          <nav class="slide-index" aria-label="Details">
            ${detailsTabsHtml('data-pane', '', { resolver: showResolver && !isStudioKind, research: canEdit, staged: stagedCount > 0, info: !isStudioKind })}
          </nav>
        </div>
        ` : ''}

      </div>
      </div>
    `)

    // Venue name → venue page
    document.querySelector('.venue-link')?.addEventListener('click', () => {
      if (venueId) window.location.hash = `#/venue/${venueId}`
    })

    // Info-panel section toggle (Info file)
    ;['btn-info-toggle'].forEach(id => {
      document.getElementById(id)?.addEventListener('click', function () {
        const panel = document.getElementById(this.dataset.panel)
        if (!panel) return
        const collapsed = panel.style.display === 'none'
        panel.style.display = collapsed ? '' : 'none'
        this.innerHTML = chevronIcon(collapsed ? 'caret-ic--down' : '')
      })
    })

    // Play all
    document.getElementById('btn-play-all')?.addEventListener('click', () => {
      playRecording(recordingId, 0, rec.tracks)
    })

    // Track row clicks — skip grayed-out rows; use track ID to find correct queue index.
    // Clicking the row for the track that's already loaded toggles play/pause
    // in place; clicking any other row starts that track fresh (Ryan,
    // 2026-08-27 — previously every click restarted playback from 0, so the
    // "pause" icon could never actually get back to "play"). Named so a Set
    // edit's re-render (below) can re-wire the rebuilt rows the same way —
    // grouping is markup only and must never touch how playback finds a row.
    function wirePlaybackTrackRowClicks() {
      mainContent.querySelectorAll('.track-row[data-track-id]').forEach(row => {
        row.addEventListener('click', e => {
          // Only the play button, the number and the title start or pause a
          // track (Ryan, 2026-10-04). Notes, Songwriter, Set and the gaps
          // between cells do nothing, so editing a cell never plays the row.
          if (!e.target.closest('.track-play, .track-num, .track-title-wrap')) return
          if (row.classList.contains('track-row--skipped')) return
          const tid = parseInt(row.dataset.trackId)
          if (tid === Player.currentId()) {
            Player.togglePlay()
            return
          }
          const idx = rec.tracks.findIndex(t => t.id === tid)
          if (idx >= 0) playRecording(recordingId, idx, rec.tracks)
        })
      })
    }
    wirePlaybackTrackRowClicks()

    // ── Quick edit: recording metadata (Source/Lineage/Quality) ──────────────
    // Click an editable value → inline input → Enter saves, Esc cancels.
    // Rating dropped 2026-08-18 — see app/models/recording.py.
    // Redisplay after a quick edit must rebuild the SAME chip markup the
    // initial render emitted (2026-08-21) — otherwise editing Source once
    // silently downgrades it from a coloured badge to grey text until reload.
    function metaCellDisplay(field) {
      if (field === 'source')  return sourceBadge(rec.source) || '—'
      if (field === 'source_tag') return esc(rec.source_tag || '—')
      if (field === 'etree_shnid') return esc(rec.etree_shnid != null ? String(rec.etree_shnid) : '—')
      if (field === 'lineage') { const l = rec.lineage; return esc(l ? (l.length > 220 ? l.slice(0, 220) + '…' : l) : '—') }
      return `<span class="quality ${qualityClass(rec.quality)}">${esc(rec.quality || '—')}</span>`  // quality
    }
    function startMetaQuickEdit(cell) {
      const field = cell.dataset.qedit
      const raw = field === 'source'      ? (rec.source      || '')
                : field === 'source_tag'  ? (rec.source_tag  || '')
                : field === 'etree_shnid' ? (rec.etree_shnid != null ? String(rec.etree_shnid) : '')
                : field === 'lineage'     ? (rec.lineage     || '')
                :                           (rec.quality     || '')
      cell.innerHTML = `<input class="hm-qedit-input" type="text" value="${esc(String(raw))}" />`
      const input = cell.querySelector('input')
      input.focus(); input.select()
      let done = false
      const finish = async (save) => {
        if (done) return; done = true
        if (save) {
          const v = input.value.trim()
          const payload = { [field]: v || null }
          try {
            await API.recordings.update(recordingId, { ...payload, change_note: 'Quick edit' })
            Object.assign(rec, payload)
            markStaged()   // now has unwritten changes
          } catch (e) { console.error('Quick edit failed:', e) }
        }
        // The colour now lives on the inner .quality chip, so the cell's own
        // className is stable and no longer needs rewriting here.
        cell.innerHTML = metaCellDisplay(field)
      }
      input.addEventListener('keydown', e => {
        e.stopPropagation()
        if (e.key === 'Enter')  { e.preventDefault(); finish(true) }
        else if (e.key === 'Escape') { finish(false) }
      })
      input.addEventListener('blur', () => finish(true))
    }
    // ── Members / Guests pills ──────────────────────────────────────────────
    // Who played the show is CONTENT, not an editing surface — a listener wants
    // it as much as an archivist does. It went missing in Playback mode because
    // the whole personnel block sat inside `if (canEdit && perf)`, wiring and
    // rendering together (Ryan, 2026-08-22).
    //
    // The markup lives here, once, and both paths call it: renderRecMusicians()
    // inside the editing block, and the read-only render below. Duplicating it
    // would be the classic two-implementations-of-one-thing drift.
    function recPersonnelHtml(personnel, editable) {
      const members = (personnel || []).filter(p => !p.is_guest)
      const guests  = (personnel || []).filter(p =>  p.is_guest)
      // The name is a link to that person's Musician page, in BOTH modes
      // (Ryan, 2026-08-22). It used to open an inline instrument/note editor —
      // see renderPersonnelDetail, removed with it. Every other name in the app
      // navigates when clicked; this one alone opened a form, which is exactly
      // the kind of inconsistency that makes people stop clicking things.
      //
      // musician_id is on every resolved entry (inherited ones have no
      // PerformancePersonnel row, so `id` can be null — `musician_id` cannot).
      // Guard anyway: a name with nowhere to go renders as plain text rather
      // than a link to #/musician/undefined.
      const pill = (p, i, role) => `
        <span class="member-chip ${role === 'guest' ? 'member-chip--guest' : ''}">
          ${p.musician_id
            ? `<a class="member-chip-name rec-pill-name" href="#/musician/${p.musician_id}" title="Open ${esc(p.name)}">${esc(p.name)}</a>`
            : `<span class="member-chip-name">${esc(p.name)}</span>`}
          ${editable ? `<span class="member-chip-x" data-role="${role}" data-i="${i}" title="Remove">${icon('x')}</span>` : ''}
        </span>`
      const row = (role, label, items) => {
        // Read-only: an empty row is a prompt to an editor that isn't there, so
        // it is omitted rather than shown as a dash.
        if (!editable && !items.length) return ''
        return `
        <div class="mg-row">
          <span class="mg-row-label">${label}</span>
          ${items.map((p, i) => pill(p, i, role)).join('')}
          ${editable ? `
            <span class="artist-picker-wrap mg-add-picker" data-role="${role}">
              <input type="text" class="mg-role-input" data-role="${role}" autocomplete="off"
                     aria-label="Add ${label === 'Members' ? 'a member' : 'a guest'}"
                     placeholder="+ Add ${label === 'Members' ? 'member' : 'guest'}" />
              <div class="artist-dropdown mg-role-dd" data-role="${role}" style="display:none"></div>
            </span>` : ''}
        </div>`
      }
      return row('member', 'Members', members) + row('guest', 'Guests', guests)
    }

    // Read-only render for Playback mode and for listeners. The editable path
    // renders from inside the `if (canEdit && perf)` block below.
    if (!canEdit && perf) {
      const box = document.getElementById('rec-musicians')
      if (box) box.innerHTML = recPersonnelHtml(perf.personnel || [], false)
    }

    // ── Recording image (Ryan, 2026-10-04) ───────────────────────────────
    // Outside the canEdit block on purpose: Playback mode and peers must see the
    // image too (it was absent there). The modal itself gates editing.
    // Draws rec.image_url -- already the resolved chain (own image, else the
    // artist's) -- or the artist's initials. Peers get the same field from
    // the share door with peer URLs, so one branch serves both contexts.
    function renderCoverSlot() {
      const el = document.getElementById('rec-cover')
      if (!el) return
      el.innerHTML = rec.image_url
        ? `<img class="rec-header-photo" src="${esc(rec.image_url)}" alt="Cover">`
        : `<span class="rec-header-photo rec-header-photo--initials">${esc(recInitials({ artist: perfName }))}</span>`
    }
    renderCoverSlot()
    const coverEl = document.getElementById('rec-cover')
    if (coverEl?.getAttribute('role') === 'button') {
      coverEl.addEventListener('click', () => openRecordingImageModal({
        recordingId, rec, artistImageId: perf?.artist_image_id || null,
        venueImageId: perf?.venue_image_id || null,
        artistName: perfName,
        // Narrows the link-out searches: an album by its title, a live show
        // by its venue and year.
        linkQualifier: rec.kind === 'studio'
          ? [rec.title, 'album cover'].filter(Boolean).join(' ')
          : [venueStr, perf?.start_year].filter(Boolean).join(' '),
        onChange: renderCoverSlot,
      }))
      coverEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); coverEl.click() }
      })
    }

    // Delegated from the view shell rather than one container: Quality now sits
    // in the top action row and Source/Lineage down beside Members, so there is
    // no single ancestor of all three but the view itself.
    //
    // .rec-view-shell, NOT mainContent — mainContent survives every navigation,
    // so binding there would stack one more handler per recording opened.
    if (canEditLibrary()) {
      mainContent.querySelector('.rec-view-shell')?.addEventListener('click', ev => {
        const cell = ev.target.closest('.hm-val--editable[data-qedit]')
        if (cell && !cell.querySelector('input')) startMetaQuickEdit(cell)
      })
    }

    // ── Quick edit: right-click a track → flags + note popup ──────────────────
    // Three surfaces, one call: the Write Tags button, the File Tags tab, and
    // the note that explains what the amber means. They must never disagree —
    // an amber tab with no explanation is a puzzle, and an explanation with no
    // amber tab is noise.
    function setTagsStaged(on) {
      document.getElementById('btn-write-tags')?.classList.toggle('pane-act--staged', on)
      document.querySelector('.slide-tab[data-pane="filetags"]')?.classList.toggle('slide-tab--staged', on)
      document.getElementById('tags-staged-note')?.classList.toggle('act-suppressed', !on)
      syncPaneActs()
      // The Current/After table is built from the database as of the last load,
      // so a new edit made while that pane is on screen has to refresh it.
      if (on && document.getElementById('sp-filetags')?.classList.contains('active')) {
        loadFileTags(recordingId)
      }
    }
    function markStaged() { setTagsStaged(true) }
    function refreshTrackRow(t) {
      const row = mainContent.querySelector(`.track-row[data-track-id="${t.id}"]`)
      if (!row) return
      const titleEl = row.querySelector('.track-title')
      if (titleEl) titleEl.innerHTML = trackTitleInnerHtml(t)
      const noteEl = row.querySelector('.track-note-col')
      if (noteEl) {
        noteEl.textContent = t.notes || '—'; noteEl.title = t.notes || 'Click to add a note'
        noteEl.classList.toggle('pp-empty', !t.notes)
      }
      const swEl = row.querySelector('.track-sw-col')
      if (swEl) {
        swEl.textContent = t.songwriter || '—'; swEl.title = t.songwriter || 'Click to add a songwriter'
        swEl.classList.toggle('pp-empty', !t.songwriter)
      }
      row.dataset.flags = (t.flags || []).join(',')
      applySkipFilter()
    }
    // Click a track title → inline rename (auto-saves on Enter/blur).
    function startTrackTitleEdit(titleEl, t) {
      if (titleEl.querySelector('input')) return
      titleEl.innerHTML = `<input class="track-title-input" type="text" value="${esc(t.title || '')}" />`
      const input = titleEl.querySelector('input')
      input.focus(); input.select()
      let done = false
      const finish = async (save) => {
        if (done) return; done = true
        if (save) {
          const v = input.value.trim()
          if (v && v !== t.title) {
            t.title = v
            try { await API.tracks.update(t.id, { title: v }); markStaged() }
            catch (e) { console.error('Track rename failed:', e) }
          }
        }
        titleEl.innerHTML = trackTitleInnerHtml(t)
      }
      input.addEventListener('click', e => e.stopPropagation())
      input.addEventListener('keydown', e => {
        e.stopPropagation()
        if (e.key === 'Enter') { e.preventDefault(); finish(true) }
        else if (e.key === 'Escape') { finish(false) }
      })
      input.addEventListener('blur', () => finish(true))
    }
    // Named so a Set edit can re-wire the rebuilt rows (grouping/derived
    // numbering changes on every Set save — spec section 6.4).
    function wireEditableTrackRows() {
      if (!canEditLibrary()) return
      mainContent.querySelectorAll('.track-row[data-track-id]').forEach(row => {
        const track = rec.tracks.find(t => t.id === parseInt(row.dataset.trackId))
        if (!track) return
        // Click the title → rename (don't start playback)
        row.querySelector('.track-title--editable')?.addEventListener('click', ev => {
          ev.stopPropagation()
          startTrackTitleEdit(row.querySelector('.track-title'), track)
        })
        // Right-click anywhere on the row → flags (+ Official) popup. Note
        // and Songwriter used to live here too; they're click-to-edit cells
        // directly in the row now, matching Add Recording (Ryan, 2026-07-15).
        row.addEventListener('contextmenu', ev => {
          ev.preventDefault()
          openTrackMenu(track, ev.clientX, ev.clientY, {
            flagsOnly: true,
            showOfficial: true,
            showSet: true,
            onChange: async (t) => {
              try { await API.tracks.update(t.id, { flags: t.flags, is_official: t.is_official }); markStaged() }
              catch (e) { console.error(e) }
              refreshTrackRow(t)
            },
            // Saving a Set never touches disk, tags or track_number (spec
            // section 6.4) and re-renders the WHOLE list, since grouping and
            // every set's derived position can change from one edit. No
            // markStaged(): write_flac_tags writes no set tag (N6, 2026-09-25).
            onSet: async (t) => {
              try { await API.tracks.update(t.id, { set_number: t.set_number }) }
              catch (e) { alert('Failed: ' + e.message) }
              renderTrackList()
            },
          })
        })

        // Note / Songwriter — click-to-edit directly in the row, same
        // treatment as Add Recording's track table.
        makeInlineEditable(document.getElementById(`t-note-${track.id}`), {
          placeholder: '—',
          get: () => track.notes || '',
          onSave: async v => {
            v = v.trim() || null
            track.notes = v
            try { await API.tracks.update(track.id, { notes: v }); markStaged() }
            catch (e) { alert('Failed: ' + e.message) }
            refreshTrackRow(track)
          },
        })
        makeInlineEditable(document.getElementById(`t-sw-${track.id}`), {
          placeholder: '—',
          get: () => track.songwriter || '',
          onSave: async v => {
            v = v.trim() || null
            track.songwriter = v
            try { await API.tracks.update(track.id, { songwriter: v }); markStaged() }
            catch (e) { alert('Failed: ' + e.message) }
            refreshTrackRow(track)
          },
        })
      })
    }
    wireEditableTrackRows()

    // Rebuilds the track panel from the current rec.tracks (grouped by set
    // when any track has one) and re-wires every row the same way the
    // initial render did. Player itself is untouched — only markup changes.
    function renderTrackList() {
      trackRows = buildTrackListHtml()
      const panel = document.getElementById('track-panel')
      if (panel) panel.innerHTML = trackHeadHtml() + (trackRows || '<div class="info-panel-empty">No tracks</div>')
      wirePlaybackTrackRowClicks()
      wireEditableTrackRows()
      applySkipFilter()
    }

    // Collection tags (add / remove)
    wireRecCollectionArea(recordingId)

    // ── Inline header editing (artist / date / venue / musicians / notes) ─────
    if (canEdit && perf) {
      const reload = () => renderRecordingView(recordingId)

      // Artist name → reassign (autocomplete; Enter commits typed name).
      const nameEl = document.getElementById('rec-perf-name')
      nameEl?.addEventListener('click', () => {
        if (nameEl.querySelector('input')) return
        nameEl.innerHTML = `<span class="artist-picker-wrap" style="display:inline-block; min-width:220px">
          <input type="text" class="pp-inline-input" id="rec-perf-input" value="${esc(perf.artist || '')}" autocomplete="off" />
          <div class="artist-dropdown" id="rec-perf-dd" style="display:none"></div></span>`
        const input = document.getElementById('rec-perf-input')
        input.focus(); input.select()
        let committed = false
        const commit = async name => {
          if (committed) return; committed = true
          name = (name || '').trim()
          if (name && name.toLowerCase() !== (perf.artist || '').toLowerCase()) {
            try { await API.performances.update(perf.id, { artist_name: name }); invalidateDims('artists', 'musicians') }
            catch (e) { alert('Failed: ' + e.message) }
          }
          reload()
        }
        wirePickerDropdown(input, document.getElementById('rec-perf-dd'), API.artists.search,
          ({ name }) => commit(name), 'Create new artist')
        input.addEventListener('keydown', e => {
          e.stopPropagation()
          if (e.key === 'Enter') { e.preventDefault(); commit(input.value) }
          else if (e.key === 'Escape') { committed = true; reload() }
        })
      })

      // Date → inline Year / Month / Day, with an optional End Date (same
      // +/- toggle pattern as the ingest form's "+ End date", 2026-07-23 —
      // for multi-day stands, e.g. the Danny Gatton Cellar Door shows).
      // Clearing the end fields and committing removes the end date.
      const dateEl = document.getElementById('rec-f-date')
      dateEl?.addEventListener('click', () => {
        if (dateEl.querySelector('input')) return
        const hasEnd = !!(perf.end_year || perf.end_month || perf.end_day)
        dateEl.innerHTML = `
          <input type="number" class="rec-date-input" id="rec-d-y" placeholder="YYYY" value="${perf.start_year || ''}" min="1900" max="2099" style="width:52px" />
          <input type="number" class="rec-date-input" id="rec-d-m" placeholder="MM" value="${perf.start_month || ''}" min="1" max="12" style="width:38px" />
          <input type="number" class="rec-date-input" id="rec-d-d" placeholder="DD" value="${perf.start_day || ''}" min="1" max="31" style="width:38px" />
          <a class="field-toggle-link" id="rec-toggle-end-date" href="#">${hasEnd ? '− End Date' : '+ End Date'}</a>
          <span id="rec-end-date-fields" style="display:${hasEnd ? 'inline-flex' : 'none'}; gap:3px; margin-left:4px">
            <input type="number" class="rec-date-input" id="rec-d-y2" placeholder="YYYY" value="${perf.end_year || ''}" min="1900" max="2099" style="width:52px" />
            <input type="number" class="rec-date-input" id="rec-d-m2" placeholder="MM" value="${perf.end_month || ''}" min="1" max="12" style="width:38px" />
            <input type="number" class="rec-date-input" id="rec-d-d2" placeholder="DD" value="${perf.end_day || ''}" min="1" max="31" style="width:38px" />
          </span>`
        document.getElementById('rec-d-y').focus()
        let done = false
        const commit = async () => {
          if (done) return; done = true
          const y = parseInt(document.getElementById('rec-d-y').value) || null
          const m = parseInt(document.getElementById('rec-d-m').value) || null
          const d = parseInt(document.getElementById('rec-d-d').value) || null
          const endShown = document.getElementById('rec-end-date-fields').style.display !== 'none'
          const ey = endShown ? (parseInt(document.getElementById('rec-d-y2').value) || null) : null
          const em = endShown ? (parseInt(document.getElementById('rec-d-m2').value) || null) : null
          const ed = endShown ? (parseInt(document.getElementById('rec-d-d2').value) || null) : null
          try {
            await API.performances.update(perf.id, {
              start_year: y, start_month: m, start_day: d,
              end_year: ey, end_month: em, end_day: ed,
            })
          } catch (e) { alert('Failed: ' + e.message) }
          reload()
        }
        document.getElementById('rec-toggle-end-date').addEventListener('click', e => {
          e.preventDefault()
          const box = document.getElementById('rec-end-date-fields')
          const visible = box.style.display !== 'none'
          if (visible) {
            // Hide and clear — committing after this removes the end date.
            box.style.display = 'none'
            e.currentTarget.textContent = '+ End Date'
            document.getElementById('rec-d-y2').value = ''
            document.getElementById('rec-d-m2').value = ''
            document.getElementById('rec-d-d2').value = ''
          } else {
            box.style.display = 'inline-flex'
            e.currentTarget.textContent = '− End Date'
            // Pre-fill from the start date on first reveal, same as ingest.
            if (!document.getElementById('rec-d-y2').value) document.getElementById('rec-d-y2').value = document.getElementById('rec-d-y').value
            if (!document.getElementById('rec-d-m2').value) document.getElementById('rec-d-m2').value = document.getElementById('rec-d-m').value
            if (!document.getElementById('rec-d-d2').value) document.getElementById('rec-d-d2').value = document.getElementById('rec-d-d').value
            document.getElementById('rec-d-y2').focus()
          }
        })
        dateEl.querySelectorAll('input').forEach(inp => {
          inp.addEventListener('keydown', e => {
            e.stopPropagation()
            if (e.key === 'Enter') { e.preventDefault(); commit() }
            else if (e.key === 'Escape') { done = true; reload() }
          })
        })
        // commit when focus leaves the whole date group
        dateEl.addEventListener('focusout', () => setTimeout(() => {
          if (!dateEl.contains(document.activeElement)) commit()
        }, 0))
      })

      // Venue → picker (search existing / create new)
      const venueEl = document.getElementById('rec-f-venue')
      venueEl?.addEventListener('click', () => {
        if (venueEl.querySelector('input')) return
        venueEl.innerHTML = `<span class="venue-picker-wrap" style="display:inline-block; min-width:200px">
          <input type="text" class="pp-inline-input" id="rec-venue-input" value="${esc(perf.venue_name || '')}" autocomplete="off" />
          <div class="venue-dropdown" id="rec-venue-dd" style="display:none"></div></span>`
        const input = document.getElementById('rec-venue-input')
        input.focus(); input.select()
        let committed = false
        const commitVenue = async ({ id, name }) => {
          if (committed) return; committed = true
          try {
            let venueId = id
            if (!venueId && name) { const c = await API.venues.create({ name }); venueId = c.id; invalidateDims('venues') }
            if (venueId) await API.performances.update(perf.id, { venue_id: venueId })
          } catch (e) { alert('Failed: ' + e.message) }
          reload()
        }
        wireVenuePickerDropdown(input, document.getElementById('rec-venue-dd'), commitVenue)
        input.addEventListener('keydown', e => {
          e.stopPropagation()
          if (e.key === 'Enter') { e.preventDefault(); commitVenue({ id: null, name: input.value.trim() }) }
          else if (e.key === 'Escape') { committed = true; reload() }
        })
      })

      // Festival / Event → picker (search existing / create new / clear)
      const eventEl = document.getElementById('rec-f-event')
      eventEl?.addEventListener('click', () => {
        if (eventEl.querySelector('input')) return
        eventEl.innerHTML = `<span class="event-picker-wrap" style="display:inline-block; min-width:160px">
          <input type="text" class="pp-inline-input" id="rec-event-input" value="${esc(perf.event_name || '')}" autocomplete="off" />
          <div class="event-dropdown" id="rec-event-dd" style="display:none"></div></span>`
        const input = document.getElementById('rec-event-input')
        input.focus(); input.select()
        let committed = false
        const commitEvent = async ({ id, name }) => {
          if (committed) return; committed = true
          try {
            let eventId = id
            if (!eventId && name) { const c = await API.events.create({ name }); eventId = c.id }
            await API.performances.update(perf.id, { event_id: eventId || null })
          } catch (e) { alert('Failed: ' + e.message) }
          reload()
        }
        wirePickerDropdown(input, document.getElementById('rec-event-dd'), API.events.search,
          ({ id, name }) => commitEvent({ id, name }), 'Create new event')
        input.addEventListener('keydown', e => {
          e.stopPropagation()
          if (e.key === 'Enter') { e.preventDefault(); commitEvent({ id: null, name: input.value.trim() }) }
          else if (e.key === 'Escape') { committed = true; reload() }
        })
      })

      // Stage → inline text (optional, Performance.stage)
      makeInlineEditable(document.getElementById('rec-f-stage'), {
        placeholder: 'Add stage',
        get: () => perf.stage || '',
        onSave: async v => {
          v = v.trim()
          try { await API.performances.update(perf.id, { stage: v || null }); perf.stage = v || null }
          catch (e) { alert('Failed: ' + e.message) }
        },
      })

      // Notes → inline multiline (recording-level)
      makeInlineEditable(document.getElementById('rec-notes'), {
        multiline: true, placeholder: 'Add notes…',
        get: () => rec.notes || '',
        onSave: async v => {
          v = v.trim(); rec.notes = v
          try { await API.recordings.update(recordingId, { notes: v || null, change_note: 'Quick edit' }); markStaged() }
          catch (e) { alert('Failed: ' + e.message) }
        },
      })

      // Album title (studio kind, Ryan, 2026-09-27) — no-ops when #rec-title
      // isn't on the page (live, or a studio recording with no title yet).
      // Reloads on save rather than patching in place: clearing the title
      // out swaps the whole heading/sub-line back to the live-style fallback,
      // which is a different element (id and all), not a text swap.
      makeInlineEditable(document.getElementById('rec-title'), {
        get: () => rec.title || '',
        onSave: async v => {
          v = v.trim()
          try { await API.recordings.update(recordingId, { title: v, change_note: 'Quick edit' }) }
          catch (e) { alert('Failed: ' + e.message) }
          rec.title = v
          renderRecordingView(recordingId)
        },
      })

      // ── Release block (Studio Records spec v1, chunk 3) ─────────────────
      // Same .pp-mb-* styling and state machine as the Artist page's
      // MusicBrainz block: matched (confidence gate chose it, no human
      // involved), linked (a human picked it), ambiguous/none/null (nothing
      // to show yet). "Matched automatically" / "Linked by you" are the
      // exact strings that block already renders — reused verbatim (S2/S3).
      function releaseFactsLine() {
        // Label, catalog number, country only (Ryan, 2026-09-27) — title and
        // year are the header's job now, one row up, so this stopped
        // repeating them. Falls back to the title only when none of the
        // three release-specific facts are known.
        const facts = [rec.mb_label, rec.mb_catalog_number, rec.mb_release_country].filter(Boolean)
        return facts.length ? facts.join(' · ') : (rec.title || '')
      }
      function renderReleaseBlock() {
        const box = document.getElementById('rec-release')
        if (!box) return
        // canEditLibrary() on every mutating control (S7) -- an admin in
        // Playback mode, or on a remote library, must not see Unlink/Find
        // release any more than any other edit control on this page.
        const admin = isAdmin() && canEdit
        if (rec.mb_release_status === 'matched' || rec.mb_release_status === 'linked') {
          const how = rec.mb_release_status === 'matched' ? 'Matched automatically' : 'Linked by you'
          box.innerHTML = `
            <div class="pp-mb-linked">
              <a class="pp-mb-name" href="${esc(mbReleaseUrl(rec.mb_release_id))}"
                 target="_blank" rel="noopener"
                 title="View this release on musicbrainz.org">${esc(releaseFactsLine() || rec.title || 'Release')} ↗</a>
            </div>
            <div class="pp-mb-foot">
              <span class="pp-mb-dot"></span>${how}
              ${admin ? `<button type="button" class="btn btn-ghost btn-xs" id="rec-release-unlink">Unlink</button>` : ''}
            </div>`
        } else if (admin) {
          // 'unlinked' (a human explicitly removed a link) is not "never
          // looked up" -- rendering it that way claims something that never
          // happened (N1). No status text for it at all, just the action.
          const msg = rec.mb_release_status === 'ambiguous'
            ? 'More than one release matches this recording, please pick the right one.'
            : rec.mb_release_status === 'none'
              ? 'Nothing found for this recording.'
              : (rec.mb_release_status === 'unlinked' || rec.mb_release_status === 'error')
                ? ''
                : 'Not looked up yet.'
          box.innerHTML = `
            ${msg ? `<div class="pp-mb-empty">${msg}</div>` : ''}
            <button type="button" class="btn btn-primary btn-xs" id="rec-release-find">Find release</button>`
        } else {
          box.innerHTML = ''
        }
        document.getElementById('rec-release-unlink')?.addEventListener('click', async () => {
          try {
            await API.recordings.releaseUnlink(recordingId)
            rec.mb_release_status    = 'unlinked'
            rec.mb_release_id        = null
            rec.mb_release_group_id  = null
            rec.mb_release_type      = null
            rec.mb_label             = null
            rec.mb_catalog_number    = null
            rec.mb_release_country   = null
            renderReleaseBlock()
          } catch (e) { alert('Failed: ' + e.message) }
        })
        document.getElementById('rec-release-find')?.addEventListener('click', openReleasePicker)
      }

      async function openReleasePicker() {
        const box = document.getElementById('rec-release')
        if (!box) return
        box.innerHTML = `<div class="pp-mb-empty">Searching MusicBrainz…</div>`
        let res
        try { res = await API.recordings.releaseCandidates(recordingId) }
        catch (e) {
          box.innerHTML = `<div class="pp-mb-empty">Lookup failed: ${esc(e.message)}
            <button type="button" class="btn btn-ghost btn-xs" id="rec-release-find">Try again</button></div>`
          document.getElementById('rec-release-find')?.addEventListener('click', openReleasePicker)
          return
        }
        renderReleaseCandidates(res.candidates || [])
      }

      function renderReleaseCandidates(cands) {
        const box = document.getElementById('rec-release')
        if (!box) return
        if (!cands.length) {
          box.innerHTML = `
            <div class="pp-mb-empty">Nothing found for “${esc(rec.title || '')}”.</div>
            <button type="button" class="btn btn-ghost btn-xs" id="rec-release-find">Try again</button>`
          document.getElementById('rec-release-find')?.addEventListener('click', openReleasePicker)
          return
        }
        box.innerHTML = `
          <div class="pp-mb-prompt">Click the right release to link it${
            cands.length === 1 ? '' : ' (more than one matches)'}.</div>
          <div class="pp-mb-cands">
            ${cands.map(c => `
              <div class="pp-mb-cand" data-mbid="${esc(c.mbid)}" role="button" tabindex="0">
                <span class="pp-mb-cand-name">${esc(c.title || '')}</span>
                <span class="pp-mb-cand-meta">${[
                  c.label, c.catalog_number, c.country, c.date,
                ].filter(Boolean).map(esc).join(' · ')}</span>
                <span class="pp-mb-cand-score" title="MusicBrainz match score">${c.score ?? ''}</span>
                <a class="pp-mb-cand-view" href="${esc(mbReleaseUrl(c.mbid))}"
                   target="_blank" rel="noopener" title="Open on musicbrainz.org">View ↗</a>
              </div>`).join('')}
          </div>
          <div class="pp-mb-foot">
            <button type="button" class="btn btn-ghost btn-xs" id="rec-release-cancel">Cancel</button>
          </div>`
        box.querySelectorAll('.pp-mb-cand').forEach(btn => {
          btn.addEventListener('click', async e => {
            if (e.target.closest('.pp-mb-cand-view')) return
            box.innerHTML = `<div class="pp-mb-empty">Fetching…</div>`
            try {
              const res = await API.recordings.releaseLink(recordingId, btn.dataset.mbid)
              rec.mb_release_status   = res.status
              rec.mb_release_id       = res.mb_release_id
              rec.mb_release_group_id = res.mb_release_group_id
              rec.mb_release_type     = res.mb_release_type
              rec.mb_label            = res.mb_label
              rec.mb_catalog_number   = res.mb_catalog_number
              rec.mb_release_country  = res.mb_release_country
              renderReleaseBlock()
            } catch (e) { alert('Failed: ' + e.message); renderReleaseBlock() }
          })
          btn.addEventListener('keydown', e => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); btn.click() }
          })
        })
        document.getElementById('rec-release-cancel').addEventListener('click', renderReleaseBlock)
      }

      if (showReleaseBlock) renderReleaseBlock()

      // ── Info File: locked → Edit File → Save to File ────────────────────
      // Replaces the old always-hot textarea that autosaved on blur (see the
      // infoContent comment for why). Three states:
      //   locked            readonly + .rev-info-text--locked, Save suppressed
      //   editing, clean    editable, Save visible but disabled
      //   editing, dirty    Save enabled
      // Save writes the DB row AND the .txt on disk when the library folder
      // already has one — API.recordings.saveInfoFile, not update(), because
      // it can touch the filesystem. It never creates a file in a folder that
      // never had one; the endpoint reports that back and the status line
      // says so rather than pretending the disk was written.
      //
      // Both controls live in the tab strip now, so hiding Save is done with
      // the .act-suppressed class rather than the hidden attribute: the pane
      // switcher owns `hidden` on every .pane-acts child, and two owners of
      // one attribute is a bug waiting to happen.
      const infoEditEl  = document.getElementById('rec-info-edit')
      const infoEditBtn = document.getElementById('btn-info-edit')
      const infoSaveBtn = document.getElementById('btn-rec-save-info')
      const infoStatus  = document.getElementById('rec-info-save-status')

      if (infoEditEl && infoEditBtn) {
        const isDirty = () => infoEditEl.value !== (rec.info_file_content || '')

        function setLocked(locked) {
          infoEditEl.readOnly = locked
          infoEditEl.classList.toggle('rev-info-text--locked', locked)
          infoSaveBtn.classList.toggle('act-suppressed', locked)
          infoSaveBtn.hidden = locked
          syncPaneActs('info')   // Save appearing/leaving can empty the row
          // "Cancel", not "Done" — clicking it while editing DISCARDS whatever
          // is unsaved, so the label has to say so. "Done" reads like a save.
          infoEditBtn.textContent = locked ? 'Edit File' : 'Cancel'
          if (!locked) infoEditEl.focus()
        }
        const refreshSaveBtn = () => { infoSaveBtn.disabled = !isDirty() }

        infoEditBtn.addEventListener('click', () => {
          if (!infoEditEl.readOnly && isDirty() &&
              !confirm('Discard unsaved changes to the info file?')) return
          if (!infoEditEl.readOnly) infoEditEl.value = rec.info_file_content || ''
          infoStatus.textContent = ''
          infoStatus.title = ''
          setLocked(!infoEditEl.readOnly)
          refreshSaveBtn()
        })

        infoEditEl.addEventListener('input', refreshSaveBtn)

        infoSaveBtn.addEventListener('click', async () => {
          const v = infoEditEl.value
          infoSaveBtn.disabled = true
          infoStatus.textContent = 'Saving…'
          try {
            const res = await API.recordings.saveInfoFile(recordingId, v)
            rec.info_file_content = v
            // Stays in edit mode on purpose. The status line lives inside the
            // save row, so relocking here would hide the very confirmation the
            // save produced — and after saving to disk, "did that land, and
            // where?" is exactly what you want to read. "Done" relocks.
            refreshSaveBtn()
            // title as well as text: the status shares the tab row now and is
            // ellipsized at 200px, so a long message is only readable on hover.
            infoStatus.textContent = res?.wrote_file
              ? `Saved to ${res.filename}`
              : `Saved: ${res?.reason || 'database only'}`
            infoStatus.title = infoStatus.textContent
          } catch (e) {
            infoStatus.textContent = 'Save failed: ' + e.message
            infoStatus.title = infoStatus.textContent
            infoSaveBtn.disabled = false
          }
        })
      }

      // Members/Guests two-row personnel widget (2026-07-22, replacing the
      // single Musicians pill row + Inherit/Explicit mode selector). Pills
      // split purely on perf.personnel[].is_guest — Members = roster/explicit
      // non-guest rows, Guests = is_guest rows — same split used by the Add
      // Recording form's createMembersWidget, matched visually here (mg-row/
      // mg-add-picker markup) so both surfaces look identical. ⚠ They share
      // markup AND CSS, so a change to one is a change to both — the
      // 2026-09-01 always-present-input rework had to be applied here in the
      // same pass for exactly that reason.
      // The Inherit/Explicit mode is still a real field on Performance (case
      // 5 — dropping a roster member for this one show — still auto-flips it
      // under the hood), it just no longer has a manual UI control; nothing
      // in this Phase needed one, since editing the rows already covers
      // every case the toggle used to require picking by hand.
      const persistPersonnelLists = async (memberNames, guestNames) => {
        try {
          await API.performances.update(perf.id, { members: memberNames, guests: guestNames })
          invalidateDims('musicians')
        } catch (e) { alert('Failed: ' + e.message) }
        reload()
      }

      function renderRecMusicians() {
        const box = document.getElementById('rec-musicians')
        if (!box) return
        const personnel = perf.personnel || []
        const members = personnel.filter(p => !p.is_guest)
        const guests  = personnel.filter(p =>  p.is_guest)
        const listFor = role => role === 'guest' ? guests : members

        box.innerHTML = recPersonnelHtml(personnel, true)

        if (!canEdit) return

        box.querySelectorAll('.member-chip-x').forEach(x =>
          x.addEventListener('click', async () => {
            const role = x.dataset.role, idx = parseInt(x.dataset.i)
            const newMembers = (role === 'member' ? members.filter((_, i) => i !== idx) : members).map(p => p.name)
            const newGuests  = (role === 'guest'  ? guests.filter((_, i) => i !== idx)  : guests).map(p => p.name)
            await persistPersonnelLists(newMembers, newGuests)
          }))

        // The "+" button is gone (2026-09-01) — the input is permanent, so
        // there is nothing left to reveal. See createMembersWidget's comment
        // for why the reveal itself was the height jump; this surface shares
        // the markup and the CSS, so it has to share the fix or the two drift.
        box.querySelectorAll('.mg-role-input').forEach(input => {
          const role = input.dataset.role
          const dd   = box.querySelector(`.mg-role-dd[data-role="${role}"]`)
          wirePickerDropdown(input, dd, API.musicians.search,
            async ({ name }) => {
              name = (name || '').trim()
              if (!name || listFor(role).some(p => p.name.toLowerCase() === name.toLowerCase())) return
              const newMembers = members.map(p => p.name).concat(role === 'member' ? [name] : [])
              const newGuests  = guests.map(p => p.name).concat(role === 'guest'  ? [name] : [])
              await persistPersonnelLists(newMembers, newGuests)
            }, 'Create new musician')
        })
      }

      // renderPersonnelDetail() REMOVED 2026-08-22 (Ryan) — the inline
      // instrument/note editor is out of V1. Clicking a name now opens that
      // person's Musician page instead, which is what every other name in the app
      // does.
      //
      // The DATA and its API survive untouched: PerformancePersonnel still
      // carries `instrument` and `note`, resolve_performance_personnel still
      // returns them, and PATCH /api/performances/<id>/personnel/<row_id>
      // (API.performances.updatePersonnelRow) still writes them. Only the UI
      // went. Rebuilding it is a render function, not a migration.

      renderRecMusicians()

      // Lomax: Resolver tab and chat. Accepting a suggestion is applied by the server through the
      // field's normal save path, so the page only has to redraw. Non-editors get the table
      // without actions and never get the chat pane. One controller drives the Resolver tab and the chat. Without a Resolver tab (a joined
      // library) there is no controller and no Lomax call at all.
      const resRoot = (showResolver && !isStudioKind) ? document.getElementById('lx-res-root') : null
      // An album has no Resolver table: its Lomax pane (skill 'album') is the chat alone.
      const albumLx = isStudioKind && canEdit && showResolver
      const lxc = (resRoot || albumLx) ? lomaxController({
        skill: isStudioKind ? 'album' : 'recording', subjectType: 'recording', subjectId: recordingId,
        alive: () => document.body.contains(resRoot || document.getElementById('sp-ai')),
        afterAccept: () => renderRecordingView(recordingId),
      }) : null
      if (lxc) {
        if (resRoot) {
          resRoot.closest('.slide-pane').setAttribute('data-lx-ctl', lxc.id)
          lxc.addView(ctl => {
            if (!document.body.contains(resRoot)) return false
            lxRepaint(resRoot, lomaxResolverHtml(ctl, lxRecordingResolverOpts(rec, perf)))
            return true
          })
        }
        if (canEdit) {
          lomaxMountChat(document.getElementById('sp-ai'), null, {
            leave: true,
            addNotes: async text => {
              const cur = String(rec.notes || '').trim()
              const next = cur ? `${cur}\n${text}` : text
              await API.recordings.update(recordingId, { notes: next, change_note: 'Lomax' })
              rec.notes = next
              const el = document.getElementById('rec-notes')
              if (el) { el.textContent = next; el.classList.remove('pp-empty') }
            },
          }, lxc)
        } else lxc.refresh()
        lxc.load()
      }
    }

    // Analyze Audio — run Librosa analysis on all tracks. The button lives in
    // the tab strip and is present from page build, so it is bound once below
    // (see wireReanalyze) rather than re-attached on every pane render.
    async function onAnalyzeAudio() {
      // The tab-strip button and the Quality tab's empty-state button run the
      // same action and show the same progress.
      const btns = ['btn-analyze-audio', 'btn-analyze-audio-empty']
        .map(id => document.getElementById(id)).filter(Boolean)
      if (!btns.length) return
      const setAll = (text, disabled) => btns.forEach(b => { b.disabled = disabled; b.textContent = text })
      setAll('Analyzing…', true)
      try {
        const result = await API.recordings.reprocess(recordingId)
        setAll(`Done (${result.analysed} track${result.analysed === 1 ? '' : 's'})`, true)
        setTimeout(() => {
          setAll('Analyze Audio', false)
          // Full reload: the Quality pane, score displays, waveform and
          // spectrogram all rebuild from the fresh rows.
          renderRecordingView(recordingId)
        }, 1500)
        if (result.errors?.length) {
          console.warn('Analysis errors:', result.errors)
        }
      } catch (e) {
        setAll('Analyze Audio', false)
        alert('Analysis failed: ' + e.message)
      }
    }

    // Re-validate checksums — re-checks against the files on disk now, and
    // opportunistically picks up any fingerprint file that was never parsed
    // (e.g. this recording predates the checksum feature). Not gated on
    // canEdit — re-checking integrity is a read-only action.
    document.getElementById('btn-cksum-revalidate')?.addEventListener('click', async (e) => {
      const btn = e.currentTarget
      btn.disabled = true
      btn.textContent = '…'
      try { await API.recordings.verifyChecksums(recordingId) }
      catch (err) { alert('Re-validate failed: ' + err.message) }
      renderRecordingView(recordingId)
    })

    // Skip non-music toggle (action bar instance)
    document.getElementById('skip-filter-action')?.addEventListener('change', function () {
      setSkipFilter(this.checked)
    })
    // Apply filter to current view immediately (in case state was already on)
    applySkipFilter()

    // Open in Containing Folder (2026-08-09). File paths never reach the
    // frontend (see the module's File obfuscation note) — the path is
    // resolved server-side and Finder is opened from there, since this is a
    // single-machine PyWebView desktop app and Flask is already running on
    // Ryan's own Mac. Fails soft: no folder on disk (a Move-ingested show
    // whose staging row outlived it, same story as the Triage list) just
    // shows an alert rather than anything blocking.
    async function actRevealFolder() {
      try {
        await API.recordings.revealFolder(recordingId)
      } catch (e) {
        alert('Could not open folder: ' + e.message)
      }
    }

    // Write FLAC tags. A real dialog (panel 4d) rather than confirm(), skipped
    // entirely when write_tags_default is on (the setting exists precisely so
    // this stops asking every time).
    async function _doWriteTags() {
      const btn = document.getElementById('btn-write-tags')
      try {
        btn.disabled = true
        btn.textContent = 'Writing…'
        const result = await API.recordings.writeTags(recordingId)
        if (result.errors?.length) {
          alert(`Tags written to ${result.written} file(s).\n\nWarnings:\n${result.errors.map(([f, e]) => `${f}: ${e}`).join('\n')}`)
        }
        // Update in place (no full reload) so the side panel stays open and the
        // user sees the result instantly. Clear the staged indicator on the
        // button, then refresh the File Tags pane if it's open.
        btn.disabled = false
        btn.textContent = 'Write Tags to Files'
        setTagsStaged(false)
        const ftPane = document.getElementById('sp-filetags')
        if (ftPane && ftPane.classList.contains('active')) {
          loadFileTags(recordingId)
        }
      } catch (e) {
        alert('Error writing tags: ' + e.message)
        if (btn) { btn.disabled = false; btn.textContent = 'Write Tags to Files' }
      }
    }
    document.getElementById('btn-write-tags')?.addEventListener('click', async () => {
      let fh = null
      try { fh = await fileHandling() } catch (e) { /* fall through to the dialog */ }
      if (fh && fh.write_tags_default) { await _doWriteTags(); return }

      const n = (rec.tracks || []).length
      const wrap = document.createElement('div')
      wrap.className = 'modal-overlay'
      wrap.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="wrt-title">
          <div class="modal-header"><h3 id="wrt-title">Write tags to ${n} file${n === 1 ? '' : 's'}</h3></div>
          <div class="modal-body">
            <p>Existing tags in the files are replaced. FFP and ST5 checksums still verify. MD5 will not.</p>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-ghost" id="wrt-cancel">Cancel</button>
            <button class="btn btn-sm btn-primary" id="wrt-confirm">Write Tags</button>
          </div>
        </div>`
      document.body.appendChild(wrap)
      const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey) }
      const onKey = e => { if (e.key === 'Escape') close() }
      document.addEventListener('keydown', onKey)
      wrap.querySelector('#wrt-cancel').addEventListener('click', close)
      wrap.addEventListener('click', e => { if (e.target === wrap) close() })
      wrap.querySelector('#wrt-confirm').addEventListener('click', async () => {
        close()
        await _doWriteTags()
      })
    })

    // Rename Files (spec section 6.4/panels 4b/4c) — a real dialog, not
    // confirm(), because there is a scheme to choose inside it. Defaults to
    // the active global scheme; a per-click override in the select applies
    // to this one confirm only and is never saved.
    const NAMING_SCHEME_LABELS = [
      ['original',      'Keep original'],
      ['number_title',  'Number and title'],
      ['etree',         'etree'],
      ['etree_sets',    'etree, sets'],
    ]
    function renamePreviewRowsHtml(plan) {
      const rows = plan.slice(0, 3).map(p => `
        <tr><td class="fh-prev-old">${esc(p.current)}</td><td class="fh-prev-arrow">→</td><td>${esc(p.proposed)}</td></tr>`).join('')
      const more = plan.length > 3
        ? `<tr><td class="fh-prev-more" colspan="3">${plan.length - 3} more</td></tr>` : ''
      return rows + more
    }
    async function actRenameFiles() {
      let fh
      try { fh = await API.system.getFileHandling() }
      catch (e) { alert('Could not load file handling settings: ' + e.message); return }

      const wrap = document.createElement('div')
      wrap.className = 'modal-overlay'
      wrap.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="rnf-title">
          <div class="modal-header"><h3 id="rnf-title">Rename files</h3></div>
          <div class="modal-body">
            ${fh.file_handling_mode === 'keep'
              ? `<p>File handling is set to Keep my files as-is. These files will be renamed anyway.</p>` : ''}
            <div class="set-field" style="margin-bottom:8px">
              <label class="set-label">Naming scheme</label>
              <select class="set-input" id="rnf-scheme">
                ${NAMING_SCHEME_LABELS.map(([v, label]) =>
                  `<option value="${v}"${v === fh.naming_scheme ? ' selected' : ''}>${esc(label)}</option>`).join('')}
                <option value="custom"${fh.naming_scheme === 'custom' ? ' selected' : ''}${fh.naming_template ? '' : ' disabled'}>Custom</option>
              </select>
            </div>
            <table class="fh-prev" id="rnf-preview">
              <tr><th>Now</th><th></th><th>After</th></tr>
            </table>
            <p class="note" style="color:var(--t2);font-size:12px;margin-top:6px">Checksum files list the current names. Matching runs again after the rename.</p>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-ghost" id="rnf-cancel">Cancel</button>
            <button class="btn btn-sm btn-primary" id="rnf-confirm" disabled>Rename</button>
          </div>
        </div>`
      document.body.appendChild(wrap)

      const select     = wrap.querySelector('#rnf-scheme')
      const previewTbl = wrap.querySelector('#rnf-preview')
      const confirmBtn = wrap.querySelector('#rnf-confirm')
      const title       = wrap.querySelector('#rnf-title')
      const close      = () => { wrap.remove(); document.removeEventListener('keydown', onKey) }
      const onKey      = e => { if (e.key === 'Escape') close() }
      document.addEventListener('keydown', onKey)
      wrap.querySelector('#rnf-cancel').addEventListener('click', close)
      wrap.addEventListener('click', e => { if (e.target === wrap) close() })

      let currentPlan = []
      let previewSeq  = 0
      async function refreshPreview() {
        const seq = ++previewSeq
        confirmBtn.disabled = true
        try {
          const result = await API.naming.preview({
            scheme: select.value, recording_id: recordingId,
            template: select.value === 'custom' ? fh.naming_template : undefined,
          })
          if (seq !== previewSeq) return   // a later change already superseded this
          currentPlan = (result.plan || []).filter(p => p.current !== p.proposed)
          previewTbl.innerHTML = `<tr><th>Now</th><th></th><th>After</th></tr>` + renamePreviewRowsHtml(currentPlan)
          title.textContent = `Rename ${currentPlan.length} file${currentPlan.length === 1 ? '' : 's'}`
          confirmBtn.disabled = currentPlan.length === 0
        } catch (e) {
          previewTbl.innerHTML = `<tr><td colspan="3">${esc(e.message)}</td></tr>`
        }
      }
      select.addEventListener('change', refreshPreview)
      refreshPreview()

      confirmBtn.addEventListener('click', async () => {
        confirmBtn.disabled = true
        confirmBtn.textContent = 'Renaming…'
        try {
          const result = await API.recordings.renameFiles(recordingId, {
            scheme: select.value,
            template: select.value === 'custom' ? fh.naming_template : undefined,
          })
          close()
          if (result.errors?.length) {
            alert(`Renamed ${result.renamed} file(s).\n\nWarnings:\n${result.errors.map(([f, e]) => `${f}: ${e}`).join('\n')}`)
          }
          renderRecordingView(recordingId)
        } catch (e) {
          alert('Rename failed: ' + e.message)
          confirmBtn.disabled = false
          confirmBtn.textContent = 'Rename'
        }
      })
    }
    document.getElementById('btn-rename-files')?.addEventListener('click', actRenameFiles)

    // Mark / unmark as official release (cascades to tracks server-side).
    async function actToggleOfficial(item) {
      const next = !rec.is_official
      item.disabled = true
      try {
        await API.recordings.update(recordingId, { is_official: next, change_note: 'Official flag' })
        rec.is_official = next
        item.textContent = next ? 'Official Release' : 'Mark as Official Release'
        markStaged()
      } catch (e) { alert('Failed: ' + e.message) }
      finally { item.disabled = false }
    }

    // Favorite toggle. Optimistic: the button flips immediately and reverts if
    // the request fails. A highlight is a low-stakes personal mark and should
    // feel instant — unlike the official/delete actions above, nothing
    // downstream depends on it, so there is no markStaged() and no change_note.
    document.getElementById('btn-favorite')?.addEventListener('click', async () => {
      const btn = document.getElementById('btn-favorite')
      const next = !viewerHasFavorited(rec)
      const paint = (on) => {
        btn.classList.toggle('is-fav', on)
        btn.textContent = on ? 'Favorited' : 'Mark as Favorite'
        btn.setAttribute('aria-pressed', on ? 'true' : 'false')
      }
      // Optimistic on BOTH stores: setViewerFavorite updates favIds itself on
      // success, so the local mirror here is only for my own library.
      if (libraryState.activeId == null) rec.is_favorite = next
      paint(next)
      btn.disabled = true
      try {
        await setViewerFavorite(recordingId, next)
        // The sidebar's Favorites section is this star's other face — starring a
        // show and not seeing it appear on the shelf makes the star feel like it
        // did nothing. Cache dropped either way; re-rendered only if the section
        // is open, so a collapsed one just reloads next time it is expanded.
        refreshFavoritesNav()
      } catch (e) {
        if (libraryState.activeId == null) rec.is_favorite = !next
        paint(!next)
        alert('Could not save favorite: ' + e.message)
      } finally { btn.disabled = false }
    })

    // Delete — a real dialog rather than confirm(), because there is a choice
    // to make inside it and confirm() cannot carry one (Ryan, 2026-08-21).
    //
    // The files checkbox is UNCHECKED by default and stays that way: for a ROIO
    // collector the tape is the irreplaceable thing and the database row is
    // not. Checking it turns a reversible mistake into an unrecoverable one, so
    // it is opt-in, it is spelled out in red, and the confirm button changes
    // its own label to say which of the two things is about to happen.
    function actDeleteRecording() {
      const shown = [perfName, dateStr, venueStr].filter(Boolean).join(' · ')
      const wrap = document.createElement('div')
      wrap.className = 'modal-overlay'
      wrap.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="del-title">
          <div class="modal-header"><h3 id="del-title">Delete recording</h3></div>
          <div class="modal-body">
            <p class="del-subject">${esc(shown || 'This recording')}</p>
            <p class="del-note">Removes the library record, its tracks, checksums and history.
              Any artist or venue left with nothing attached is pruned too.</p>
            <label class="del-files-row">
              <input type="checkbox" id="del-files-cb" />
              <span>Also delete the audio files from disk</span>
            </label>
            <p class="del-warn" id="del-warn" hidden>The folder and everything in it is removed permanently. This cannot be undone.</p>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-ghost" id="del-cancel">Cancel</button>
            <button class="btn btn-sm btn-danger" id="del-confirm">Delete record</button>
          </div>
        </div>`
      document.body.appendChild(wrap)

      const cb      = wrap.querySelector('#del-files-cb')
      const warn    = wrap.querySelector('#del-warn')
      const confirmBtn = wrap.querySelector('#del-confirm')
      const close   = () => { wrap.remove(); document.removeEventListener('keydown', onKey) }
      const onKey   = e => { if (e.key === 'Escape') close() }
      document.addEventListener('keydown', onKey)

      cb.addEventListener('change', () => {
        warn.hidden = !cb.checked
        confirmBtn.textContent = cb.checked ? 'Delete record and files' : 'Delete record'
      })
      wrap.querySelector('#del-cancel').addEventListener('click', close)
      wrap.addEventListener('click', e => { if (e.target === wrap) close() })

      confirmBtn.addEventListener('click', async () => {
        confirmBtn.disabled = true
        confirmBtn.textContent = 'Deleting…'
        try {
          const res = await API.recordings.delete(recordingId, cb.checked)
          // The row goes even when the folder could not be removed — the server
          // says so rather than failing the whole delete, so surface that here
          // instead of letting the user assume the disk is clean.
          if (cb.checked && !res?.files_deleted) {
            alert('The library record was deleted, but the files were not: ' +
                  (res?.files_error || 'unknown reason'))
          }
          // Deleting a recording can prune its artist / venue / musicians.
          invalidateDims('artists', 'venues', 'musicians')
          close()
          // Navigate back to wherever the user came from (falls back to
          // Library if this recording was reached with nothing preceding it).
          window.location.hash = state.navBack ? state.navBack.hash : '#/'
        } catch (err) {
          confirmBtn.disabled = false
          confirmBtn.textContent = cb.checked ? 'Delete record and files' : 'Delete record'
          alert('Delete failed: ' + err.message)
        }
      })
    }

    // Move the folder out of the library. Confirmed inline rather than with a
    // dialog: unlike Delete this is fully reversible — the files are all still
    // there under a name the response reports — so the weight of a modal would
    // overstate it. The page reloads so the header picks up its Out of Library
    // badge and the menu collapses to a note.
    async function actMoveOut(item) {
      const dest = item.dataset.dest
      const label = dest === 'workshop' ? 'Workshop' : 'Backlog'
      if (!confirm(`Move this recording's folder out of the library into ${label}?\n\n` +
                   'The library record, its metadata and its history are kept. ' +
                   'The show stops being playable until it comes back.')) return
      item.disabled = true
      const original = item.textContent
      item.textContent = 'Moving…'
      try {
        const res = await API.recordings.moveOut(recordingId, dest)
        rec.is_published = false
        // A move can empty an artist or venue of everything visible.
        invalidateDims('artists', 'venues', 'musicians')
        alert(`Moved to ${label} as "${res.moved_to_name}".`)
        renderRecordingView(recordingId)
      } catch (e) {
        item.disabled = false
        item.textContent = original
        alert('Move failed: ' + e.message)
      }
    }

    // One action row, five panes: show the controls tagged for the pane being
    // opened and hide the rest, then collapse the row entirely if that leaves
    // nothing. .act-suppressed is a second, separate reason a control can stay
    // hidden (today: Save to File while the Info File pane is locked) and
    // always wins.
    //
    // Declared at this level rather than inside the slide-panel IIFE below
    // because the Info File wiring calls it too, from a deeper scope.
    function syncPaneActs(pane) {
      // Called with no argument from setTagsStaged, which can fire while any
      // pane is showing — read the active tab rather than guessing.
      if (pane == null) pane = document.querySelector('.slide-tab.active')?.dataset.pane || null
      const row = document.getElementById('pane-acts')
      let any = false
      document.querySelectorAll('#pane-acts [data-for]').forEach(el => {
        el.hidden = el.dataset.for !== pane || el.classList.contains('act-suppressed')
        // Status text and the staged note are not actions — neither should hold
        // the row open on a pane whose only real control is suppressed.
        const isAction = !el.classList.contains('pane-act-status') &&
                         !el.classList.contains('pane-act-note')
        if (!el.hidden && isAction) any = true
      })
      // The row always shows: it carries the pane's title even with no action.
      const tab = document.querySelector(`.slide-tab[data-pane="${pane}"]`)
      const title = document.getElementById('pane-title')
      if (title && tab) title.textContent = tab.textContent
      if (row) row.hidden = false
    }

    // ── Actions menu ────────────────────────────────────────────────────────
    ;(function () {
      const btn  = document.getElementById('btn-rec-actions')
      const menu = document.getElementById('rec-actions-menu')
      if (!btn || !menu) return
      const setOpen = open => {
        menu.hidden = !open
        btn.setAttribute('aria-expanded', open ? 'true' : 'false')
        btn.classList.toggle('is-open', open)
      }
      btn.addEventListener('click', e => { e.stopPropagation(); setOpen(menu.hidden) })
      document.addEventListener('click', e => {
        if (!menu.hidden && !menu.contains(e.target)) setOpen(false)
      })
      document.addEventListener('keydown', e => { if (e.key === 'Escape') setOpen(false) })

      menu.addEventListener('click', async e => {
        const item = e.target.closest('.actions-item')
        if (!item) return
        const act = item.dataset.act
        // Official and Move-to keep the menu open — one shows its result in
        // place, the other is a step towards a second choice. The rest either
        // navigate or open a dialog, so there is nothing left to look at.
        if (act === 'official') return actToggleOfficial(item)
        if (act === 'kind') {
          setOpen(false)
          const nextKind = rec.kind === 'studio' ? 'live' : 'studio'
          try { await API.recordings.update(recordingId, { kind: nextKind, change_note: 'Quick edit' }) }
          catch (e) { alert('Failed: ' + e.message) }
          // The Albums nav entry and its kind-counts depend on this -- refresh
          // the sidebar the same way other kind-affecting actions do.
          loadArtistList()
          return renderRecordingView(recordingId)
        }
        if (act === 'move-toggle') {
          const sub = document.getElementById('rec-move-sub')
          if (!sub) return
          sub.hidden = !sub.hidden
          item.setAttribute('aria-expanded', sub.hidden ? 'false' : 'true')
          return
        }
        if (act === 'move') return actMoveOut(item)
        setOpen(false)
        if (act === 'reveal') actRevealFolder()
        if (act === 'delete') actDeleteRecording()
      })
    })()

    // ── Slide panel tab wiring ───────────────────────────────────────────────
    // Declared HERE, before the IIFE below: its last line calls openPane(), which
    // calls loadQualityPane() when Quality was the last pane open, and that reads
    // this flag. Declared further down it was still in its temporal dead zone,
    // so the pane sat on "Loading..." with a ReferenceError (Ryan, 2026-10-04).
    let _qualityLoaded = false
    ;(function () {
      const panel = document.getElementById('slide-panel')
      if (!panel) return
      let activePane = null

      const rail = document.getElementById('slide-rail')

      // A remembered pane whose tab is gone (an album has no Info File tab) falls back to the
      // first tab that remains.
      function pickPane(p) {
        if (p && p !== 'spectrogram' && panel.querySelector(`.slide-tab[data-pane="${p}"]`)) return p
        return panel.querySelector('.slide-tab')?.dataset.pane || 'info'
      }

      function openPane(pane) {
        state.recPanelOpen = true
        panel.classList.add('open')
        document.querySelectorAll('.slide-pane').forEach(p => p.classList.remove('active'))
        document.querySelectorAll('.slide-tab').forEach(t => t.classList.remove('active'))
        document.getElementById(`sp-${pane}`)?.classList.add('active')
        document.querySelector(`.slide-tab[data-pane="${pane}"]`)?.classList.add('active')
        syncPaneActs(pane)
        activePane = pane
        state.recLastPane = pane   // survives the reload an Apply/edit triggers
        rail?.setAttribute('aria-expanded', 'true')
        if (pane === 'filetags') loadFileTags(recordingId)
        if (pane === 'quality')  loadQualityPane()
      }

      // Collapse keeps `activePane` in `state.recLastPane` so reopening lands
      // where you left off. It is NOT cleared any more: clearing it meant the
      // rail always reopened on the fallback pane, which read as the panel
      // forgetting what you had been looking at.
      function closePanel() {
        state.recPanelOpen = false
        panel.classList.remove('open')
        // The pane and tab keep their .active class through the close (Ryan,
        // 2026-08-28). Stripping it emptied the panel on the first frame, so
        // the last 220ms of the slide was a blank rectangle narrowing — the
        // content has to still be there for there to be anything to slide out.
        // The collapsed panel is clipped to 28px and goes visibility:hidden
        // once the transition ends, so nothing selected is left on screen, and
        // openPane re-asserts both classes on the way back in anyway.
        activePane = null
        rail?.setAttribute('aria-expanded', 'false')
      }

      // The rail is always on screen now (2026-08-21) and is the panel's
      // show/hide control in both directions — the single obvious way to get
      // the track list back to full width.
      rail?.addEventListener('click', () => {
        if (panel.classList.contains('open')) closePanel()
        else openPane(pickPane(state.recLastPane))
      })

      document.querySelectorAll('.slide-tab').forEach(tab => {
        tab.addEventListener('click', () => {
          const pane = tab.dataset.pane
          // Same tab clicked again → collapse (kept alongside the rail; it is
          // the gesture existing muscle memory expects).
          if (panel.classList.contains('open') && activePane === pane) closePanel()
          else openPane(pane)
        })
      })

      // Default: whichever pane was open before the last reload (e.g. an AI
      // Assist Apply), falling back to Info File on a fresh visit. 'spectrogram'
      // is stale from before the spectrogram moved (2026-07-15, then again
      // 2026-08-18 into the Quality pane) — treat it as no saved pane.
      // recPanelOpen persists a deliberate collapse across recordings: someone
      // who put Details away is listening, not auditing, and should not have to
      // dismiss it again on every show. Undefined (first visit) means open.
      const startPane = pickPane(state.recLastPane)
      if (state.recPanelOpen === false) closePanel()
      else openPane(startPane)
    })()

    // ── Listening Quality pane ──────────────────────────────────────────────
    // Renders the SAME report the triage card renders, from the same endpoint
    // and the same interpret_full() output — see app/api/quality.py. Anything
    // that changes in the engine's vocabulary changes in both places at once,
    // which is the whole point of unifying them (IO-61).
    //
    // Differences from the triage card, all deliberate: no sampled-track
    // players (the real player is right there), no triage actions (the
    // recording is already ingested), and the per-group metrics are COLLAPSED
    // behind a caret. On the triage card the metrics are the point — you are
    // deciding whether to ingest. Here you are usually deciding whether to
    // press play, and the verdict answers that on its own.
    async function loadQualityPane() {
      if (_qualityLoaded) return
      _qualityLoaded = true
      const body = document.getElementById('sp-quality-body')
      if (!body) return
      let q
      try {
        q = await API.quality.forRecording(recordingId, true)
      } catch (e) {
        // 404 is the normal "never analysed" case, not a failure worth shouting
        // about — offer the button that fixes it.
        body.innerHTML = `<div class="rq-empty">Run Analyze Audio</div>
          ${canEdit ? '<button class="pane-act" id="btn-analyze-audio-empty">Analyze Audio</button>' : ''}`
        document.getElementById('btn-analyze-audio-empty')?.addEventListener('click', onAnalyzeAudio)
        return
      }
      body.innerHTML = buildQualityPaneHtml(q)
      wireReanalyze()

      // Caret sections, closed by default.
      body.querySelectorAll('.rq-adv-toggle').forEach(btn => {
        btn.addEventListener('click', () => {
          const wrap = btn.closest('.rq-grp')?.querySelector('.rq-adv')
          if (!wrap) return
          const open = wrap.classList.toggle('open')
          btn.classList.toggle('open', open)
          btn.setAttribute('aria-expanded', open ? 'true' : 'false')
        })
      })

      // Spectrogram is the one heavy asset here, so it loads only once the
      // pane it lives in is actually on screen.
      if (defaultTrackId) {
        const defaultTrack = rec.tracks.find(t => t.id === defaultTrackId)
        loadSpectrogram(defaultTrackId, defaultTrack?.title)
      }
    }

    // Bound once — the button is in the tab strip now, not inside the pane it
    // acts on, so it survives every re-render of that pane. The _wired guard
    // stays because loadQualityPane() still calls this after a successful load
    // and it must not double-bind.
    function wireReanalyze() {
      const btn = document.getElementById('btn-analyze-audio')
      if (btn && !btn._wired) { btn._wired = true; btn.addEventListener('click', onAnalyzeAudio) }
    }

    // ── Waveform (wavesurfer.js) — official renderer, fully wired to the
    // persistent player, adopted 2026-07-15 ─────────────────────────────────
    // Was a spike, then briefly its own separate audio channel; Ryan: "We
    // definitely want the thing fully wired into the persistent player. It
    // should not be separate." Renders from precomputed peaks (no network
    // fetch), and its OWN internal audio element is never played — see the
    // big comment above `_waveformMap` for why. All real playback control
    // routes through the shared #audio-el via Player.
    ;(function () {
      const wrap  = document.getElementById('rec-waveform-wrap')
      if (!wrap || !defaultTrackId) return
      const wsBox = document.getElementById('rec-waveform-ws')
      const peaks = _peaksForTrack(defaultTrackId)
      const duration = _trackDurationMap[defaultTrackId]
      if (!peaks || !duration) return

      const cs = getComputedStyle(document.documentElement)
      _wsInstance = window.WaveSurfer.create({
        container: wsBox,
        peaks,
        duration,
        waveColor: (cs.getPropertyValue('--t2') || '#7a6e64').trim(),
        progressColor: (cs.getPropertyValue('--accent') || '#c4956a').trim(),
        cursorColor: (cs.getPropertyValue('--accent-lit') || '#d4aa82').trim(),
        height: 100,
        normalize: true,
        cursorWidth: 1,
      })
      _wsTrackId = defaultTrackId
      // Zoom plugin removed 2026-08-27 (Ryan) — wheel-zoom, pinch-zoom and the
      // top-right slider all came from this one registerPlugin call.

      // Click/drag → seek the REAL shared player, not wavesurfer's own
      // silent internal audio. If this recording isn't already the one
      // loaded in the player, ready it first (paused — visiting a page
      // shouldn't start blaring audio) so the seek has somewhere to land.
      _wsInstance.on('interaction', async (time) => {
        const audio = document.getElementById('audio-el')
        if (!audio) return
        if (Player.currentId() === _wsTrackId) {
          audio.currentTime = time
          return
        }
        const idx = rec.tracks.findIndex(t => t.id === defaultTrackId)
        await playRecording(recordingId, idx < 0 ? 0 : idx, rec.tracks, { autoplay: false })
        const applySeek = () => { audio.currentTime = time }
        if (audio.readyState >= 1) applySeek()
        else audio.addEventListener('loadedmetadata', applySeek, { once: true })
      })
    })()

    // If nothing is currently loaded in the player, pressing the persistent
    // bar's play button while viewing this page should start this
    // recording's first track (Ryan, 2026-07-15) instead of no-op'ing.
    if ((rec.tracks || []).length) {
      Player.setFallbackPlay(() => playRecording(recordingId, 0, rec.tracks))
    }

    // ── Spectrogram — load for default track, reload when track changes ───────
    function loadSpectrogram(trackId, trackTitle) {
      const wrap    = document.getElementById('spectrogram-wrap')
      const imgEl   = document.getElementById('spectrogram-img')
      const loading = document.getElementById('spectrogram-loading')
      const label   = document.getElementById('spectrogram-track-name')
      if (!wrap || !imgEl) return

      if (label) label.textContent = trackTitle ? `: ${trackTitle}` : ''
      imgEl.style.display = 'none'
      if (loading) { loading.style.display = ''; loading.textContent = 'Generating…' }

      const url = `/api/tracks/${trackId}/spectrogram?t=${Date.now()}`
      imgEl.onload  = () => { imgEl.style.display = 'block'; if (loading) loading.style.display = 'none' }
      imgEl.onerror = async () => {
        // Fetch the URL as text to get the actual error from the server
        try {
          const r = await fetch(url)
          const body = await r.json()
          if (loading) loading.textContent = `Error: ${body.error || r.status}`
        } catch (_) {
          if (loading) loading.textContent = 'Spectrogram failed'
        }
      }
      imgEl.src = url
    }

    // Spectrogram loads lazily when the tab is opened (see slide tab wiring above)

    // ── File Tags pane ────────────────────────────────────────────────────────
    // Fetch the actual on-disk Vorbis comments and render them as a well-formed
    // JSON object keyed by "NN · Title", so the effect of "Write Tags to Files"
    // is visible and verifiable.
    async function loadFileTags(recId) {
      const body = document.getElementById('sp-filetags-body')
      if (!body) return
      body.innerHTML = '<div class="info-panel-empty">Loading…</div>'
      try {
        const data = await API.recordings.fileTags(recId)
        const obj = {}
        ;(data.tracks || []).forEach(t => {
          const key = `${String(t.track_number || '').padStart(2, '0')} · ${t.title || ''}`
          obj[key] = t.error ? { error: t.error } : (t.tags || {})
        })
        // Staged edits: Current/After. Nothing staged: the plain on-disk view.
        const diff = fileTagsDiffHtml(data.tracks || [])
        body.innerHTML = diff
          ? `<div class="ft-wrap">${diff}</div>`
          : `<pre class="filetags-json">${esc(JSON.stringify(obj, null, 2))}</pre>`
      } catch (e) {
        body.innerHTML = `<div class="info-panel-empty">Failed to read tags: ${esc(e.message || '')}</div>`
      }
    }

    // Reload spectrogram when a new track is clicked (only if the pane is open)
    mainContent.querySelectorAll('.track-row[data-track-id]').forEach(row => {
      row.addEventListener('click', () => {
        const tid   = parseInt(row.dataset.trackId)
        const title = row.querySelector('.track-title')?.textContent || ''
        // Only when the Quality pane is on screen — the spectrogram lives
        // inside it now (2026-08-18), so redrawing it while another pane is
        // showing costs a render for a picture nobody can see.
        const qualityPaneEl = document.getElementById('sp-quality')
        if (tid && _waveformMap[tid] && qualityPaneEl?.classList.contains('active')) {
          loadSpectrogram(tid, title)
        }
      })
    })
  }

  // Tags pane, Current/After (Ryan, 2026-10-04). `tracks` is the tags endpoint's
  // list: per track, `tags` (on disk) and `staged` (what Write Tags to Files would
  // write). A full outer join per track, so every tag on disk and every tag that
  // would be written appears. A tag that would be removed keeps its current value
  // (struck through) with an empty After; a new tag has an empty Current. Rows
  // identical on every track form one "All tracks" block instead of repeating.
  // Returns '' when nothing differs, so the caller keeps the plain on-disk view.
  function fileTagsDiffHtml(tracks) {
    const show = v => v == null ? '' : (Array.isArray(v) ? v.join('; ') : String(v))
    const label = t => `${String(t.track_number || '').padStart(2, '0')} · ${t.title || ''}`
    const joined = tracks.filter(t => t.tags && t.staged).map(t => {
      const keys = [...new Set([...Object.keys(t.tags), ...Object.keys(t.staged)])].sort()
      return { t, rows: keys.map(k => {
        const hasC = k in t.tags, hasA = k in t.staged
        const c = show(t.tags[k]), a = show(t.staged[k])
        const state = !hasC ? 'added' : !hasA ? 'removed' : c === a ? 'same' : 'changed'
        return { k, c, a, state, sig: [k, c, a, state].join('\u0000') }
      }) }
    })
    if (!joined.some(j => j.rows.some(r => r.state !== 'same'))) return ''

    // Rows every track shares, in identical form.
    let common = null
    if (joined.length > 1) {
      for (const j of joined) {
        const sigs = new Set(j.rows.map(r => r.sig))
        common = common ? new Set([...common].filter(x => sigs.has(x))) : sigs
      }
    }
    const isCommon = r => !!common && common.has(r.sig)
    const row = r => `<tr class="ft-row ft-row--${r.state}"><td class="ft-k">${esc(r.k)}</td>` +
      `<td class="ft-c">${esc(r.c)}</td><td class="ft-a">${esc(r.a)}</td></tr>`
    const group = (title, rows, open) => !rows.length ? '' : `
      <details class="ft-grp"${open ? ' open' : ''}>
        <summary>${esc(title)}</summary>
        <table class="ft-table"><tbody>${rows.map(row).join('')}</tbody></table>
      </details>`

    const shared = common ? joined[0].rows.filter(isCommon) : []
    const perTrack = joined.map(j => {
      const rows = j.rows.filter(r => !isCommon(r))
      return group(label(j.t), rows, rows.some(r => r.state !== 'same'))
    }).join('')
    const unreadable = tracks.filter(t => !t.tags || !t.staged).map(t =>
      `<div class="ft-err">${esc(label(t))}: ${esc(t.error || '')}</div>`).join('')
    return `
      <div class="ft-head"><span>Tag</span><span>Current</span><span>After</span></div>
      ${group('All tracks', shared, true)}${perTrack}${unreadable}`
  }

  // ── Ingest wizard ─────────────────────────────────────────────────────────

  // Step indicators — pass optional steps array; defaults to 3-step wizard
  function stepDots(current, steps) {
    steps = steps || ['folder', 'review']  // Confirm step removed 2026-07-15
    const idx = steps.indexOf(current)
    return `<div class="step-indicator">
      ${steps.map((s, i) => {
        const cls = i < idx ? 'done' : i === idx ? 'active' : ''
        return `<div class="step-dot ${cls}" title="Step ${i + 1}"></div>`
      }).join('')}
    </div>`
  }

  // ══════════════════════════════════════════════════════════════════════════
  // Add Recordings
  //
  //   Source picker  →  Bulk Ingest run (#/bulk-ingest/<id>)
  //   Per-item Review (the Add Recording form) opens from a run's import page.
  // ══════════════════════════════════════════════════════════════════════════

  // Server-side preferences snapshot (config.IMPORT_DIR…).
  // Module-scoped and cached: several views need `import_dir`, it doesn't change
  // within a session, and there is deliberately NO bare `prefs` global — the
  // only other `prefs` in this file is a local inside renderSettingsPage().
  let appPrefs = null

  // ── Triage destinations: offer only what this install HAS (2026-09-17) ────
  //
  // A library Trellis laid out itself has Backlog and Workshop. A library the
  // user imported may have neither: those folders are optional and Trellis
  // does not create them inside someone's existing collection. Both server
  // readers validate against TRIAGE_DIRS, so an unconfigured destination is
  // already a clean 400 -- but a BUTTON that 400s is a failure reported as a
  // different failure. Offer what exists, and nothing when nothing does.
  //
  // Falling back to both when the key is absent keeps a frontend newer than
  // its server behaving as it always did, rather than hiding Move entirely.
  const TRIAGE_LABELS = { backlog: 'Backlog', workshop: 'Workshop' }

  function triageDests() {
    const d = appPrefs?.triage_destinations
    return Array.isArray(d) ? d : ['backlog', 'workshop']
  }

  /** Destination buttons for one of the two Move menu shapes.
   *  One builder, three call sites — the three had drifted into two different
   *  markups already, and a fourth copy is how the next one drifts. */
  function triageDestButtons(shape, path) {
    return triageDests().map(dest => {
      const label = TRIAGE_LABELS[dest] || dest
      return shape === 'actions'
        ? `<button class="actions-item actions-item--indent" role="menuitem"
                   data-act="move" data-dest="${esc(dest)}">${esc(label)}</button>`
        : `<button class="lq-move-opt" data-dest="${esc(dest)}"
                   data-path="${esc(path || '')}">${esc(label)}</button>`
    }).join('')
  }

  async function getPrefs() {
    if (appPrefs) return appPrefs
    try { appPrefs = await API.preferences.get() } catch (_) { appPrefs = {} }
    return appPrefs
  }

  function renderIngestView() {
    setActiveNav('ingest')
    setActiveArtist(null)
    setNavCurrent('Add Recording')
    // Fresh navigation always starts at the source picker. The exception is
    // the import page's per-item Review opening a pre-scanned folder, which
    // sets a one-shot _resume flag so the in-progress review isn't wiped.
    if (ingest._resume) {
      ingest._resume = false
    } else {
      resetIngestState()
    }
    renderIngestStep()
  }

  function renderIngestStep() {
    // All three ingest steps share one hash, so the history stack cannot carry
    // them — the header Back button goes through this handler instead. Set on
    // every step render; route() clears it on the way to any other view.
    setInPageBack(ingestStepBack)
    // renderIngestSource is async (it needs the preferences snapshot for the
    // default folder). Without an explicit catch a failure in there becomes a
    // silent unhandled rejection and the page just stays blank — which is
    // exactly how the `prefs` ReferenceError presented.
    const show = fn => {
      try {
        const r = fn()
        if (r && typeof r.catch === 'function') r.catch(_ingestRenderFailed)
      } catch (e) { _ingestRenderFailed(e) }
    }
    switch (ingest.step) {
      case 'source':  show(renderIngestSource);  break
      case 'folder':  show(renderIngestSource);  break  // legacy alias
      // A review step with no scan cannot render anything meaningful — that
      // happens when the page is re-entered after its source folder was moved
      // or removed. Fall back to the picker rather than throwing into the
      // recovery screen.
      case 'review':
      case 'tracks':                                    // merged into review step
        if (!ingest.scan) { resetIngestState(); show(renderIngestSource); break }
        show(renderIngestReview); break
      case 'success': show(renderIngestSuccess); break
    }
    // AFTER the switch, deliberately. route() paints before dispatching to a
    // view, so this is the repaint that accounts for the handler registered
    // above — and the 'review' case can reset the step to 'source' on its way
    // through, which changes whether there is an in-page Back at all.
    paintNavButtons()
  }

  // ALWAYS OFFER A WAY OUT (2026-08-07). This used to render a bare red
  // message with no action, which is how Ryan got stranded: he ingested the
  // last recording of an act, the source folder was removed by the Move +
  // empty-parent cleanup, and returning to Add Recording tried to re-open that
  // now-missing folder. The error was correct; having no button was the bug.
  function _ingestRenderFailed(e) {
    console.error('[ingest] render failed', e)
    setMainHTML(`
      <div class="empty-state">
        <div class="empty-title">Could not open this step</div>
        <div class="empty-sub" style="color:var(--red)">${esc(e && e.message || String(e))}</div>
        <div style="margin-top:14px; display:flex; gap:8px; justify-content:center">
          <button class="btn btn-primary btn-sm" id="ingest-recover">Start over</button>
          <button class="btn btn-ghost btn-sm" id="ingest-recover-home">Go to Library</button>
        </div>
      </div>`)
    document.getElementById('ingest-recover')?.addEventListener('click', resetIngestToSource)
    document.getElementById('ingest-recover-home')?.addEventListener('click', () => {
      resetIngestState(); window.location.hash = '#/'
    })
  }

  // Drop every remembered scan/folder and go back to the picker. The stale
  // `folderPath` is the thing that re-breaks the page on each retry, so
  // clearing it IS the recovery — not just re-rendering.
  function resetIngestState() {
    ingest.step       = 'source'
    ingest.scan       = null
    ingest.folderPath = null
    ingest.form       = {}
    ingest.tracks     = []
    ingest.lxc        = null
    ingest.lxSyncRes  = null
    ingest.aiApplied  = {}
    ingest.returnTo   = null
    ingest.kindFolder = null
  }

  /** Header Back, while standing inside the Add Recordings wizard.
   *
   *  Source picker, triage queue and metadata review all live at '#/ingest',
   *  so browser history holds ONE entry for the whole wizard: Back used to
   *  jump clean out of it to whatever preceded it, normally the library
   *  (Ryan, 2026-08-28 — "it should have gone back to the add recording
   *  queue"). This steps backwards THROUGH the wizard first.
   *
   *  Only the steps that have a step behind them are claimed. From the queue
   *  or the picker, Back still leaves the wizard, which is what it should do
   *  at the front of a flow — and it keeps Back from stranding a run in
   *  flight behind a picker screen.
   *
   *  `probe` asks "would you handle a Back press?" without performing it, so
   *  paintNavButtons can light the button without duplicating these rules. */
  function ingestStepBack(probe) {
    if ((window.location.hash || '').split('?')[0] !== '#/ingest') return false
    switch (ingest.step) {
      case 'review':
      case 'tracks':
        if (!probe) ingestBackFromReview()
        return true
      case 'success':
        if (!probe) {
          ingest.step = 'source'
          renderIngestStep()
        }
        return true
      default:
        return false
    }
  }

  function resetIngestToSource() {
    resetIngestState()
    renderIngestStep()
  }

  // ── Stage 1: Source ────────────────────────────────────────────────────────
  // The empty Add Recordings page: the import page's own header, tab strip,
  // button row and table header with nothing in it (Ryan, 2026-10-02). The
  // folder navigator, Up button and listing table are gone; Browse opens the
  // native folder dialog and the two mode buttons start a run on the path.
  async function renderIngestSource() {
    setActiveNav('ingest')
    setNavCurrent('Add Recordings')
    await getPrefs()   // the Lomax toggle needs has_api_key before the first paint
    // Only a fallback: getPrefs().import_dir wins whenever the backend has one.
    // After Reset Queue the picker reopens on the folder that was reset.
    const defaultDir = _biPickerPath
                       || (await getPrefs()).import_dir
                       || '/Volumes/music/Trellis/Downloads'
    _biPickerPath = null
    let here = defaultDir
    // Picks the remembered mode for a source inside vs outside the library.
    let inLibrary = false
    let mode = _biMode(inLibrary)

    // The strip below always shows Queue; a tab left over from an earlier run
    // must not leave it unselected.
    _biTab = 'queue'
    setMainHTML(`
      <div class="batch-shell lq-shell">
        <div class="lq-header">
          <div style="min-width:0">${_biSrcHeaderHtml(here)}</div>
        </div>
        <div id="bi-scan" class="bi-scan">${_biScanControlsHtml({ run: null, mode })}</div>
        <div id="lq-msg"></div>
        <!-- No Queue tab or table header until a scan exists (Ryan, 2026-10-03). -->
      </div>`)

    const msgEl  = document.getElementById('lq-msg')
    const wrapEl = document.getElementById('bi-scan')
    // `say('')` clears. Every path that can SUCCEED must call it, or the
    // message outlives the condition that produced it.
    const say = t => { msgEl.innerHTML = t ? `<div class="lq-err">${esc(t)}</div>` : '' }
    const paintButtons = () => { wrapEl.innerHTML = _biScanControlsHtml({ run: null, mode }) }
    const paintPath = () => {
      const el = document.querySelector('.bi-src-path')
      if (!el) return
      el.textContent = _lqShortPath(here)
      el.title = here
    }
    _wireBiFileHandling()

    // Validates a chosen path with the server: it climbs to the nearest
    // surviving ancestor, rejects folders outside the permitted import roots,
    // and says whether the folder is inside the library.
    async function choose(path) {
      let j
      try {
        j = await Promise.race([
          API.quality.browse(path),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error('That folder is taking too long to read. '
                                 + 'It may be very large, or the drive may be asleep.')),
            15000)),
        ])
      } catch (e) { say(e.message); return }
      if (j.error) { say(j.error); return }
      say('')
      here = j.path
      inLibrary = !!j.in_library
      mode = _biMode(inLibrary)
      paintPath()
      paintButtons()
    }

    // Browse opens PyWebView's native folder dialog. In headless/server mode
    // there is no `window.pywebview`, so it falls back to a path prompt rather
    // than a button that does nothing.
    async function browseNative() {
      const api = window.pywebview && window.pywebview.api
      if (!api || !api.pick_folder) {
        const v = (window.prompt('Source Folder', here) || '').trim()
        if (v) choose(v)
        return
      }
      let picked
      try { picked = await api.pick_folder() }
      catch (e) { say('Could not open the folder dialog: ' + e.message); return }
      if (picked) choose(picked)
    }

    document.getElementById('bi-browse')?.addEventListener('click', browseNative)
    wrapEl.addEventListener('click', e => {
      const m = e.target.closest('[data-bi-mode]')
      if (m) {
        mode = m.dataset.biMode
        _biRememberMode(inLibrary, mode)
        paintButtons()
        return
      }
      if (e.target.closest('[data-scan="start"]')) {
        _biStartAndOpen(here, mode).catch(err => say(err.message))
      }
    })

    paintButtons()
    // Resolve the default folder too, so inLibrary is right before a click.
    choose(here)
  }

  // ── Listening Quality colours ──────────────────────────────────────────────

  // Same bands as the interpretation text, ported from the standalone app so a
  // score is never a different colour in the two places it can be read.
  function _lqColour(s) {
    if (s == null) return 'var(--t2)'
    if (s >= 90) return 'var(--green)'
    if (s >= 80) return 'var(--accent-lit)'
    if (s >= 60) return 'var(--amber)'
    return 'var(--red)'
  }

  // Advanced-metric and quick-glance states run on their own scale — these are
  // verdicts on a raw measurement, not 0-100 scores.
  const _LQ_STATE = { good: 'var(--green)', ok: 'var(--accent-lit)',
                      poor: 'var(--amber)', bad: 'var(--red)' }
  const _stateColour = s => _LQ_STATE[s] || 'var(--t2)'

  const _fmt1 = v => (v == null ? 'n/a' : Number(v).toFixed(1))
  const _fmtN = (v, unit, dp) =>
    v == null ? 'n/a' : `${Number(v).toFixed(dp == null ? 1 : dp)}${unit || ''}`

  // Group weights, shown per meter as "35% of score". Mirrors GROUP_WEIGHTS in
  // app/utils/quality/quality_scoring.py — update both together.
  // Three-band verdict replaces the 1-decimal 0-100 headline (2026-07-31).
  //
  // Validated against 113 graded recordings the engine reaches r = 0.55 with a
  // mean absolute error near 7 grade points. A decimal on a number routinely 7
  // points out is false precision — 75.7 vs 75.0 is noise, not a B against a C.
  // The decision this card drives is triage, which was always a 3-way call.
  //
  // The engine still computes and returns the number; the standalone harness at
  // tools/quality/ is where the quantitative score continues to be developed.
  // This is a presentation restriction in the app, not a capability removed.
  //
  // Labels are deliberately NEUTRAL (Ryan, 2026-08-02). "Worth ingesting" /
  // "Probably skip" asserted a decision the engine has no standing to make —
  // every recording in this queue is worth ingesting to the right person, and
  // the band is a DETECTION of measured audio character, not a recommendation.
  // Mirrors BAND_LABEL in app/utils/quality/quality_scoring.py.
  const _LQ_BAND_TEXT = { green: 'High', yellow: 'Medium', red: 'Low' }

  /* The Listening Quality report, rendered from one builder for every surface
     that shows it (2026-08-28). Lives at the top level rather than inside
     renderRecordingView because Add Recording's Quality tab renders the same
     report from the triage pass's numbers — see loadIngestQualityPane. Takes
     the payload shape both /api/quality/recording/<id> and
     /api/quality/staging/features return: { verdict_band, interpretation }. */
  function buildQualityPaneHtml(q, opts) {
    const it = q.interpretation || {}
    const band = q.verdict_band || 'unknown'

    // Headline: the three-band verdict, and ONLY the verdict.
    //
    // The raw composite used to sit beside it, on the argument that an
    // archivist ranking two shows needs to break a tie the band cannot.
    // Removed 2026-08-21 (Ryan — a second time; it had been taken out once
    // before and came back with the IO-61 unification, because this pane now
    // renders from the same builder as the triage card). Validated fit is
    // r 0.55 / MAE ~7 grade points: the number reads as precision the model
    // does not have, and this is the listener's surface, not the harness.
    // The dev surface in tools/ still shows the full decimal.
    const head = `
      <div class="rq-head">
        <span class="lq-verdict lq-verdict--${esc(band)}">${_LQ_BAND_TEXT[band] || ''}</span>
      </div>`

    // Quick facts line — format, bitrate, cutoff. Same strip the triage card
    // leads with, and the last surviving content of the old Fidelity tab.
    const qk = it.quick || {}, cut = it.cutoff || {}
    const bits = [
      [qk.format, qk.bit_depth ? `${qk.bit_depth}-bit` : null,
       qk.sample_rate_hz ? `${(qk.sample_rate_hz / 1000).toFixed(1)} kHz` : null]
        .filter(Boolean).join(' ') || null,
      qk.bitrate_kbps ? `${qk.bitrate_kbps} kbps` : null,
      cut.khz != null ? `${cut.khz} kHz cutoff` : null,
    ].filter(Boolean).map(esc)
    const quick = bits.length
      ? `<div class="rq-qline">${bits.map(b => `<span>${b}</span>`)
          .join('<span class="sep">|</span>')}</div>` : ''

    // Metrics filed under the group whose score they actually move, with the
    // scored ones first — same ordering rule as the triage card, for the same
    // reason: the first reading under a meter should be one that moves it.
    const byGroup = {}
    for (const m of (it.metrics || [])) (byGroup[m.group] ||= []).push(m)
    for (const k of Object.keys(byGroup)) {
      byGroup[k] = [...byGroup[k].filter(m => m.scored),
                    ...byGroup[k].filter(m => !m.scored)]
    }

    const metricRow = m => {
      const hasScale = m.scale && m.scale.length
      const dp  = m.dp != null ? m.dp : (m.unit === ' Hz' ? 0 : 1)
      const shown = m.abs ? Math.abs(m.value) : m.value
      return `
        <div class="rq-mrow" title="${esc(m.about || '')}">
          <span class="rq-mlabel">${esc(m.label)}</span>
          <span class="rq-mval">${_fmtN(shown, m.unit, dp)}</span>
          <span class="rq-mverdict">${esc(m.verdict || '')}</span>
        </div>`
    }

    const groups = (it.groups || []).map(g => {
      const rows = byGroup[g.key] || []
      return `
      <div class="rq-grp">
        <div class="rq-grp-head" title="${esc(g.blurb || '')}">
          <span class="rq-grp-name">${esc(g.label)}</span>
          <span class="rq-grp-score" style="color:${_lqColour(g.score)}">${_fmt1(g.score)}</span>
        </div>
        <div class="lq-meter"><div class="lq-meter-fill"
             style="width:${g.score || 0}%;background:${_lqColour(g.score)}"></div></div>
        ${rows.length ? `
          <button class="rq-adv-toggle" aria-expanded="false">
            <span class="rq-caret">${chevronIcon()}</span>${rows.length} metric${rows.length === 1 ? '' : 's'}
          </button>
          <div class="rq-adv">${rows.map(metricRow).join('')}</div>` : ''}
      </div>`
    }).join('')

    // Ungrouped catch-all: every metric should map to a group, but a new
    // METRICS entry without a METRIC_GROUP entry would otherwise vanish
    // silently, which is the worst failure mode for a panel like this.
    const other = (byGroup.other || []).length ? `
      <div class="rq-grp">
        <div class="rq-grp-head"><span class="rq-grp-name">Ungrouped</span></div>
        <div class="rq-adv open">${byGroup.other.map(metricRow).join('')}</div>
      </div>` : ''

    const issues = (it.issues || []).length
      ? `<div class="rq-issues"><h4>Technical Issues</h4>
          ${it.issues.map(i => `<div class="rq-issue"><b>${esc(i.issue)}</b>: ${esc(i.detail)}
            (−${i.deduction}) <span>${esc(i.text || '')}</span></div>`).join('')}
         </div>`
      : `<div class="rq-clean">No technical issues detected.</div>`

    // Add Recording passes { spectrogram: false }: a spectrogram is drawn from
    // a track that has been analysed and given an id, and nothing on that page
    // has been ingested yet. An empty image frame there would read as a broken
    // spectrogram rather than an absent one.
    const spectro = (opts && opts.spectrogram === false) ? '' : `
      <div class="rq-spectrogram">
        <div class="rq-section-label">Spectrogram <span class="spectrogram-track-name" id="spectrogram-track-name"></span></div>
        <div id="spectrogram-wrap">
          <div class="spectrogram-img-wrap" id="spectrogram-img-wrap">
            <div class="spectrogram-loading" id="spectrogram-loading">Generating…</div>
            <img id="spectrogram-img" class="spectrogram-img" style="display:none" />
          </div>
        </div>
      </div>`

    return `<div class="rq-wrap">${head}${quick}${groups}${other}${issues}${spectro}</div>`
  }
  // Metadata Completeness reads as a WORD now, not 0-100 (Ryan, 2026-08-28:
  // "I don't like the numerical score for metadata"). The number was a count
  // of populated fields over expected fields, so 79 vs 82 never meant anything
  // anyone acted on — the three bands were the whole signal. Server-side
  // `health.rating` is the source of truth (utils/health.py RATING); the band
  // fallback covers a payload from an older build.
  const _metaRating = h =>
    (h && (h.rating || _LQ_BAND_TEXT[h.band])) || '—'

  // The per-group "15% of score" caption was removed 2026-08-28 (Ryan). It
  // invited exactly the arithmetic nobody should be doing by eye — and did it
  // badly, because the group meters combine GEOMETRICALLY (_geo in
  // quality_scoring.py), so the three percentages never did add up the way the
  // caption implied. The weights live in GROUP_WEIGHTS and nowhere else now.

  function _mmss(sec) {
    const s = Math.round(sec || 0)
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
  }

  // ── Compact row — Direction A, "The Belt" (Ryan, 2026-08-28) ──────────────
  //
  // One grid row per recording: coloured spine, name, a mono facts line, the
  // band pill, the score, and the SAME action buttons as the card. Reusing
  // `_lqActions()` verbatim is the point — the compact row is a different
  // presentation of a recording, not a second feature with its own subset of
  // things you can do to it, and every handler in _wireTriage() binds by class
  // so nothing needs re-wiring.
  //
  // The caret does NOT open a panel inside the row. It swaps that one row back
  // to its full card, because the card is already the thing that answers "why
  // this score" and a second, thinner version of it would be two places to fix
  // every future change to the metrics panel.
  // Last two path segments — the rail is a column, not a header, and a NAS
  // path is longer than any column. The full path stays in the title attribute.
  function _lqShortPath(p) {
    if (!p) return '—'
    const parts = p.replace(/\/+$/, '').split('/').filter(Boolean)
    return parts.length <= 2 ? (p || '—') : '…/' + parts.slice(-2).join('/')
  }

  // ════════════════════════════════════════════════════════════════════════
  // ── Shared ingest queue table ("The Belt", unified 2026-09-27) ───────────
  // ONE table component for every batch-style ingest surface (Bulk Ingest). Each caller normalizes its
  // own rows into the shape below and calls ingestQueueTable(); the caret,
  // the column layout, the base expand panel and the default action group
  // are shared, so a change to any of them cannot drift between the three
  // surfaces the way the pre-2026-09-27 forks did.
  //
  // Row shape: { id, name, meta, format, kind, sound_band, meta_band,
  //   needs_review, review_reason, status ('pending'|'ingesting'|'ingested'|
  //   'review'|'skipped'|'failed'), recording_id, status_text, detail: {
  //     artist, date, venue, location, source, lineage, tracksText,
  //     trackListing, issuesHtml, path } }
  // `status` other than the five named terminal/in-flight ones (i.e.
  // 'review') is the default "awaiting a decision" row -- it gets the full
  // Ingest / Review / Move action group whether or not `needs_review` is
  // set; `needs_review` only changes the meta line.
  // ════════════════════════════════════════════════════════════════════════

  // ── Resolver reason codes -> short review labels ───────────────────────────
  // Ingest Field Resolver spec v1, chunk 6: verdict() (app/utils/resolve.py)
  // can return several reason codes for one folder at once, so every surface
  // that shows them -- Batch Import's extracted.reasons, Bulk Ingest's
  // BulkIngestItem.reason (a single comma-joined column, split back apart
  // the same way app/api/quality.py's _bulk_ingest_review_reasons does) --
  // reads this one map rather than keeping its own copy of these strings.
  const _INGEST_FIELD_LABEL = {
    date: 'date', artist: 'artist', venue: 'venue', city: 'city',
    state: 'state', country: 'country', source: 'source', lineage: 'lineage',
    source_tag: 'source tag', shnid: 'shnid', album: 'album', stage: 'stage',
  }
  const _INGEST_REASON_LABEL = {
    needs_artist:        'No artist found',
    needs_date:          'No date found',
    needs_month:         'Only the year is known',
    needs_day:           'Year and month known, day missing',
    unsupported_format:  'Unsupported format',
    unreadable:          'Could not be read',
    no_audio:            'No audio files',
    // Shortest plain phrasing (Ryan should confirm wording): every track's
    // fingerprint already belongs to one recording in the library -- this
    // folder was never ingested a second time (spec section 9).
    duplicate_content:   'Duplicate recording',
  }
  function _ingestReasonLabel(code) {
    if (!code) return null
    if (code.startsWith('conflict:')) {
      const field = code.slice('conflict:'.length)
      return `Sources disagree on ${_INGEST_FIELD_LABEL[field] || field}`
    }
    if (code.startsWith('tentative:')) {
      const field = code.slice('tentative:'.length)
      return `Check ${_INGEST_FIELD_LABEL[field] || field}`
    }
    return _INGEST_REASON_LABEL[code] || null
  }
  // `codes` is an array (Batch Import's extracted.reasons) or a comma-joined
  // string (BulkIngestItem.reason) -- either way, the labels that actually
  // have text for their code, in order, nothing invented for an unknown one.
  function _ingestReasonLabels(codes) {
    const list = Array.isArray(codes) ? codes : String(codes || '').split(',')
    return list.map(_ingestReasonLabel).filter(Boolean)
  }

  function _iqBandPill(band, concern) {
    if (!band) return ''
    // `concern` (2026-09-27) adds the same amber treatment the triage
    // page's concern chips use (.lq-concern--warn) on top of the normal
    // band colour, WITHOUT touching the High/Medium/Low text -- a concern
    // (possible duplicate, failed checksum, ...) is a flag on the reading,
    // not a different reading.
    return `<span class="lq-verdict lq-verdict--${band}${concern ? ' lq-concern--warn' : ''}">${esc(_LQ_BAND_TEXT[band] || band)}</span>`
  }

  // The default action group: Ingest / Review / Move(caret) / chevron. A
  // caller with richer per-row states (Review & Ingest's convert-to-FLAC
  // offer, retry-on-error, a live copy in progress) passes its own
  // `actionsHtml(row)` instead -- the column, the layout and everything else
  // in the row stays shared either way.
  function _iqDefaultActions(row, opts) {
    if (row.status === 'ingested') {
      return `<button type="button" class="lq-act lq-act--ingest" disabled>Imported</button>
        <a class="lq-act lq-act--view" href="#/recording/${row.recording_id}">View</a>`
    }
    if (row.status === 'pending' || row.status === 'ingesting') return ''
    if (row.status === 'skipped') {
      return `<span class="iq-act-status">${esc(row.status_text || 'Skipped')}</span>`
    }
    if (row.status === 'failed') {
      return `<span class="iq-act-status iq-act-status--bad">${esc(row.status_text || 'Could not be read')}</span>`
    }
    if (!canEditLibrary()) return ''
    const btns = []
    if (opts.canIngest) {
      btns.push(`<button type="button" class="lq-act lq-act--ingest" data-path="${esc(row.id)}">Import</button>`)
    }
    btns.push(`<button type="button" class="lq-act lq-act--review" data-path="${esc(row.id)}">Review</button>`)
    if (opts.moveTargets && opts.moveTargets.length) {
      btns.push(`<div class="lq-move-wrap">
        <button type="button" class="lq-act lq-act--move" data-path="${esc(row.id)}">Move ${chevronIcon('caret-ic--down lq-act-chev')}</button>
        <div class="lq-move-menu" hidden>${opts.moveTargets.map(t =>
          `<button type="button" class="lq-move-opt" data-path="${esc(row.id)}" data-dest="${esc(t.key)}">${esc(t.label)}</button>`).join('')}</div>
      </div>`)
    }
    return btns.join('')
  }

  // Base expand panel: Artist / Date / Venue / Location / Source / Lineage /
  // Format / Type / Tracks / Issues / Path, the same fields whichever
  // caller built the row from. When `soundQuality`, the triage sound-quality
  // pane (track preview, groups, technical issues) renders BENEATH it --
  // never instead of it, and never a fingerprint row or tab anywhere here.
  function _iqExpandHtml(row, opts) {
    const d = row.detail || {}
    const baseRows = [
      ['Artist', d.artist], ['Date', d.date], ['Venue', d.venue],
      ['Location', d.location], ['Source', d.source], ['Lineage', d.lineage],
      ['Format', row.format], ['Type', row.kind === 'studio' ? 'Album' : (row.kind ? 'Live' : null)],
      ['Tracks', d.tracksText],
    ]
    const listingRow = d.trackListing ? `
      <div class="iq-expand-row iq-expand-row--wide">
        <span class="iq-expand-label">Listing</span>
        <div class="iq-tracklist">${d.trackListing}</div>
      </div>` : ''
    const issuesRow = d.issuesHtml ? `
      <div class="iq-expand-row"><span class="iq-expand-label">Issues</span>
        <span class="iq-expand-val">${d.issuesHtml}</span></div>` : ''
    const base = `
      <div class="iq-expand-panel">
        <div class="iq-expand-grid">
          ${baseRows.map(([label, val]) => `
          <div class="iq-expand-row"><span class="iq-expand-label">${esc(label)}</span>
            <span class="iq-expand-val">${esc(val || '—')}</span></div>`).join('')}
          ${listingRow}
          ${issuesRow}
          <div class="iq-expand-row"><span class="iq-expand-label">Path</span>
            <span class="iq-expand-val iq-expand-mono">${esc(d.path || '—')}</span></div>
        </div>
      </div>`
    const sq = opts.soundQuality && row._triageRow ? _lqSoundQualityPane(row._triageRow) : ''
    return base + sq
  }

  // Row status (Ryan, 2026-10-02): "Ready" or the review issues get their own
  // column instead of riding in the grey title line, where they read as part
  // of the recording's name. The alert glyph stays because the Queue's note
  // line tells the person to check it.
  function _iqStatusCell(row) {
    if (row.needs_review) {
      const issues = (row.review_issues && row.review_issues.length)
        ? row.review_issues : (row.review_reason ? [row.review_reason] : [])
      return `<span class="iq-status iq-status--issue">${icon('alert', 'iq-status-ic')}<span>${issues.map(esc).join(', ')}</span></span>`
    }
    if (row.status === 'ready') return '<span class="iq-status iq-status--ready">Ready</span>'
    return ''
  }

  function _iqRow(row, opts) {
    const open = !!(opts.isOpen && opts.isOpen(row))
    const cls = (opts.soundQuality ? 'iq-brow' : 'iq-brow iq-brow--nosq') + (opts.lomaxCell ? ' iq-brow--lx' : '')
    let metaLine
    if (row.status === 'pending') metaLine = 'Queued'
    else if (row.status === 'ingesting') metaLine = `<span class="lq-spin"></span><span>Importing</span>`
    else if (row.status === 'failed') metaLine = esc(row.status_text || 'Could not be read')
    else metaLine = row.meta === '' ? '' : (row.meta || '—')

    const actions = opts.actionsHtml ? opts.actionsHtml(row) : _iqDefaultActions(row, opts)
    const canExpand = row.status !== 'pending' && row.status !== 'ingesting'

    return `<div class="lq-row iq-row${open ? ' is-open' : ''}" data-id="${esc(row.id)}">
      <div class="lq-brow ${cls}${row.status === 'pending' ? ' lq-brow--pending' : ''}${
           row.status === 'ingesting' ? ' lq-brow--running iq-now-row' : ''}${
           row.needs_review ? ' iq-brow--issue' : ''}">
        <div class="lq-brow-main${row.thumb ? ' iq-main--thumb' : ''}">
          ${row.thumb ? (row.thumb.url
            ? `<img class="brow-av brow-av--img iq-thumb" src="${esc(row.thumb.url)}" alt="" loading="lazy">`
            : `<span class="brow-av iq-thumb">${esc(row.thumb.initials)}</span>`) : ''}
          <div class="iq-main-text">
            <div class="iq-name-row">
              <div class="lq-brow-name" title="${esc((row.detail && row.detail.path) || row.name)}">${esc(row.name)}</div>
            </div>
            ${metaLine ? `<div class="lq-brow-sub">${metaLine}</div>` : ''}
          </div>
        </div>
        <span class="iq-col-format">${row.format ? `<span class="iq-pill">${esc(row.format)}</span>` : ''}</span>
        <span class="iq-col-type">${row.kind ? `<span class="iq-pill">${row.kind === 'studio' ? 'Album' : 'Live'}</span>` : ''}</span>
        ${opts.soundQuality ? `<span class="lq-brow-band">${_iqBandPill(row.sound_band)}</span>` : ''}
        <span class="lq-brow-meta">${_iqBandPill(row.meta_band, !!(row.concerns && row.concerns.length))}</span>
        <span class="iq-col-status">${_iqStatusCell(row)}</span>
        ${opts.lomaxCell ? `<span class="iq-col-lx">${opts.lomaxCell(row)}</span>` : ''}
        <span class="lq-actions iq-actions">${actions}</span>
        ${canExpand
          ? `<button type="button" class="lq-brow-caret" data-expand="${esc(row.id)}"
                title="${open ? 'Hide the detail' : 'Show the detail for this recording'}"
                aria-expanded="${!!open}">${chevronIcon(open ? 'caret-ic--up' : 'caret-ic--down', 20)}</button>`
          : '<span class="lq-brow-caret lq-brow-caret--spacer"></span>'}
      </div>
      ${open ? _iqExpandHtml(row, opts) : ''}
    </div>`
  }

  // One triage row = the CARD (a faithful port of the standalone app's card)
  // plus an ACTION COLUMN sitting outside it. Keeping the actions outside the
  // card boundary is deliberate: the card is a report on the recording, the
  // column is what you do about it, and blurring the two is what made the
  // first attempt read as a list row instead of a report.
  // The drill-in that drops BELOW a row when its caret is clicked.
  //
  // Was `_lqCard` — a whole alternative rendering of the recording, head and
  // action buttons included. Expanding therefore swapped one component for
  // another, and the Ingest / Review / Move buttons and the caret itself
  // JUMPED to different positions (Ryan, 2026-08-28). The row now never
  // changes: this is only what appears underneath it, so nothing above moves.
  //
  // `open` is which pane is showing: 'lq' | 'meta' | 'fp'.
  function _lqSoundQualityPane(row) {

    const it  = row.interp || {}
    const lqs = row.listening_quality
    const health = row.health

    // Metrics now sit UNDER the group they belong to (Ryan, 2026-08-02) rather
    // than in one flat "Advanced Metrics" list of eleven readings with no
    // stated relationship to the three meters above them. Grouping comes from
    // METRIC_GROUP in quality_interpret.py, which is derived from what
    // score_tone/score_noise/score_dynamics actually consume — so the panel
    // cannot drift from the scoring.
    //
    // Each row is also marked scored vs measured-only. Five of the eleven carry
    // ZERO weight (presence balance, midrange scoop, hum, nonstationarity,
    // clarity) — showing them at equal visual standing implied they all move
    // the number, which is how the 07-30 Gatton confusion started.
    // Scored metrics first within each group, then the measured-only ones
    // (Ryan, 2026-08-02). METRICS order is authoring order, which interleaved
    // them — so the first thing under a meter could be a reading that does not
    // move it. Stable sort keeps the authored order inside each half.
    // No scored/unscored partition any more: the triage endpoint sends
    // `scored_only` rows (api/quality.py), so everything here moves its meter
    // by construction. View Recording still receives the full set and still
    // marks the zero-weight ones.
    const byGroup = {}
    for (const m of (it.metrics || [])) (byGroup[m.group] ||= []).push(m)

    const q = it.quick || {}, cut = it.cutoff || {}
    // Track count leads the strip (2026-08-02). It is the plainest fact about
    // the recording and the one most likely to be checked against the source
    // info file, so it sits before the technical readings rather than after.
    // Counts AUDIO files only — `extracted.track_count` is len(audio_files)
    // from the same scan payload the completeness score is computed from, so
    // art and text files never inflate it.
    const nTracks = row.extracted?.track_count
    // Collapsed from a four-cell boxed strip to ONE pipe-separated line on the
    // card's own background, sitting directly under the title (Ryan,
    // 2026-08-02). These are plain facts about the file, not readings that
    // need a meter — the box gave them more visual weight than they earn, and
    // it was occupying the top-right corner where the actions belong.
    // Cutoff folded in as a plain fact (2026-08-27, Ryan) — it used to be
    // singled out with its own colour + hover tooltip (.lq-qcut), which read
    // as more alarming than the reading warrants for a strip of otherwise
    // plain facts. Matches the read-only Fidelity pane's version above,
    // which never had the highlight to begin with.
    const bits = [
      nTracks != null ? `${nTracks} track${nTracks === 1 ? '' : 's'}` : null,
      [q.format, q.bit_depth ? `${q.bit_depth}-bit` : null,
       q.sample_rate_hz ? `${(q.sample_rate_hz / 1000).toFixed(1)} kHz` : null]
        .filter(Boolean).join(' ') || null,
      q.bitrate_kbps ? `${q.bitrate_kbps} kbps` : null,
      cut.khz != null ? `${cut.khz} kHz cutoff` : null,
    ].filter(Boolean).map(esc)

    const quick = `<div class="lq-qline">${
      bits.map(b => `<span>${b}</span>`).join('<span class="sep">|</span>')}</div>`

    const metricRow = m => {
      // No ladder (e.g. mains frequency) → no colour and no range: a range is
      // meaningless for a categorical value.
      const hasScale = m.scale && m.scale.length
      const col = hasScale ? _stateColour(m.state) : 'var(--t1)'
      const dp  = m.dp != null ? m.dp : (m.unit === ' Hz' ? 0 : 1)
      const shown = m.abs ? Math.abs(m.value) : m.value
      const ladder = hasScale ? m.scale.map((s, i) => {
        const prev = i ? m.scale[i - 1].upto : null
        const range = prev === null ? `< ${s.upto}`
          : (s.upto >= 9 && i === m.scale.length - 1 && m.unit !== ' Hz') ? `> ${prev}`
          : `${prev} to ${s.upto}`
        return `<div class="rg${s.text === m.verdict ? ' on' : ''}">
          <span>${esc(s.text)}</span><span class="b">${esc(range)}</span></div>`
      }).join('') : ''
      // The (i) leads the row and carries the state colour the dot used to
      // (Ryan, 2026-08-02) — one glyph doing both jobs rather than a dot that
      // only coloured and a button that only informed, at opposite ends.
      return `
      <div class="lq-mrow${m.scored ? '' : ' lq-mrow--unscored'}">
        <span class="lq-minfo lq-tip" style="${hasScale
          ? `color:${col};border-color:${col}` : ''}">i</span>
        <span class="lq-mlabel">${esc(m.label)}</span>
        <span class="lq-mval" style="color:${col}">${_fmtN(shown, m.unit, dp)}</span>
        <span class="lq-mverdict">${esc(m.verdict || '')}</span>
        <span class="lq-tipbox">
          <div class="tt">${esc(m.label)}: ${esc(m.verdict || '')}</div>
          <div class="ab">${esc(m.about || '')}</div>
          ${hasScale ? `<div class="th">Ranges</div>${ladder}` : ''}
        </span>
      </div>`
    }

    // One block per group: meter, verdict sentence, then that group's readings.
    const groups = (it.groups || []).map(g => `
      <div class="lq-grp">
        <div class="lq-grp-head">
          <span class="lq-grp-name lq-tip">${esc(g.label)}
            <span class="lq-tipbox">${esc(g.blurb || '')}</span></span>
          <span class="lq-grp-score" style="color:${_lqColour(g.score)}">${_fmt1(g.score)}</span>
        </div>
        <div class="lq-meter"><div class="lq-meter-fill"
             style="width:${g.score || 0}%;background:${_lqColour(g.score)}"></div></div>
        ${(byGroup[g.key] || []).length
          ? `<div class="lq-adv">${byGroup[g.key].map(metricRow).join('')}</div>` : ''}
      </div>`).join('')

    // Every metric now belongs to one of the three groups (Ryan, 2026-08-02),
    // so "other" should be empty. Kept as a catch-all rather than dropped: a
    // metric added to METRICS without a METRIC_GROUP entry would otherwise
    // vanish from the UI silently. A test pins the mapping, but a silent
    // disappearance is the worst failure mode for a panel like this.
    const otherMetrics = (byGroup.other || []).length ? `
      <div class="lq-grp lq-grp--other">
        <div class="lq-grp-head"><span class="lq-grp-name">Ungrouped</span></div>
        <div class="lq-adv">${byGroup.other.map(metricRow).join('')}</div>
      </div>` : ''

    const issues = (it.issues || []).length ? `
      <div class="lq-issues"><h4>Technical Issues</h4>
        ${it.issues.map(i => `<div class="lq-issue"><b>${esc(i.issue)}</b>: ${esc(i.detail)}
          (−${i.deduction}) <span>${esc(i.text || '')}</span></div>`).join('')}
      </div>` : `<div class="lq-clean"><span class="lq-dot"></span>No technical issues detected.</div>`

    // Each sampled track gets its own row with an inline player slot directly
    // beneath it (2026-08-02). Playback used to hand off to the global player
    // bar at the bottom of the window — visually miles from the timestamp that
    // was clicked, so it read as "nothing happened, and something unrelated
    // started". The audio element now appears in place, under the track it
    // belongs to.
    // One player per CARD, living in the block header (Ryan, 2026-08-02) — not
    // one slot per track. A slot under each track pushed the row it belonged to
    // apart from its neighbours every time it opened, so the list jumped around
    // as you sampled it. A fixed position in the header holds still; the
    // playing row is highlighted instead.
    const slot = row.folder_path
    const tracks = (row.sampled || []).map(t => {
      const file = t.rel || t.track
      return `
      <div class="lq-trk">
        <button class="lq-trk-play" title="Play from the start"
                data-folder="${esc(row.folder_path)}" data-file="${esc(file)}"
                data-slot="${esc(slot)}" data-seek="0">${icon('play')}</button>
        <span class="lq-trk-name">${esc(t.track)}</span>
        <span class="lq-trk-win">
          ${(t.offsets || []).map(o =>
            `<button class="lq-win" data-folder="${esc(row.folder_path)}"
                   data-file="${esc(file)}" data-slot="${esc(slot)}" data-seek="${o}"
                   title="Jump to the analyzed window, ${_mmss(o)} into the track"
                   >${_mmss(o)}</button>`).join('')}
        </span>
      </div>`
    }).join('')

    const flags = (row.flags || []).length
      ? `<div class="lq-flags">${row.flags.map(x => `<span>${esc(x)}</span>`).join('')}</div>` : ''

    return `<div class="lq-detailwrap lq-detailwrap--sq" data-path="${esc(row.folder_path)}">
      <div class="lq-pane">
        <div class="lq-samp lq-samp--lead">
          <div class="lq-samp-head">
            <h4>Track Preview</h4>
            <!-- Two hints said the same thing (Ryan, 2026-09-02). This one
                 ("Timestamps mark where each sample was taken") is gone; the
                 surviving line is .lq-trk-player's own ::before placeholder,
                 "Play a track, or click a timestamp to hear the analyzed
                 moment", which says it AND names the two ways to start. -->
            <div class="lq-trk-player" data-slot-for="${esc(row.folder_path)}"></div>
          </div>
          ${tracks}
        </div>
        ${issues}
        ${groups}
        ${otherMetrics}
        ${flags}
      </div>
    </div>`
  }

  // "Converting 4 of 17". The filename was dropped (Ryan, 2026-10-02): it made
  // the row far too wide.
  function _lqConvertText(cv) {
    return `Converting${cv.total ? ` ${Math.min(cv.done + 1, cv.total)} of ${cv.total}` : ''}`
  }

  // A failure marker that costs one column, not four lines.
  //
  // The message used to be printed in full inside the actions area: it wrapped
  // to three lines, pushed the row's height around, and left room for Retry
  // ONLY — so the message's own advice ("use Review to fill it in") pointed at
  // a button it had just removed (Ryan, 2026-08-28). The text moves into a
  // hover; the buttons come back.
  function _lqErrorChip(message, heading) {
    return `<span class="lq-errchip lq-tip" role="img"
                  aria-label="${esc(heading)}: ${esc(message)}">
      ${icon('alert', 'lq-errchip-ic')}
      <span class="lq-tipbox lq-tipbox--right">
        <div class="tt">${esc(heading)}</div>
        <div class="ab">${esc(message)}</div>
      </span></span>`
  }

  // The "Apply values to every recording below" block, shared by Review & Ingest and
  // the import page: `cx` carries what differs (the typed values `aa`, open,
  // busy, the staged values `applied`, and the derived `typed`/`state`).
  function _applyAllHtml(cx) {
    const aa = cx.aa
    const anyApplied = !!(aa.event.trim() || aa.stage.trim() || aa.artist.trim() || aa.venue.name.trim()
      || aa.city.trim() || aa.state.trim() || aa.country.trim()
      || aa.source.trim() || aa.source_tag.trim() || aa.lineage.trim() || aa.notes.trim())
    // Locked exactly like the Add Recording form's own venue picker: an id
    // means an existing Venue row was picked, so City/State/Country are that
    // venue's own stored values and editing them here would do nothing — the
    // server ignores them once venue_id is set (_do_confirm step 3).
    const aaVenueLocked = !!aa.venue.id
    // How many queue-level values are actually set. Only shown when the block
    // is COLLAPSED and holds something: the old rule was that the block could
    // not be closed once it held a value, so a set value could never be
    // invisible. The +/- toggle (Ryan, 2026-09-02) makes that unenforceable —
    // the user can now close it whenever they like — so the invariant is kept
    // by saying so on the header instead of by refusing the click.
    const aaTyped = cx.typed
    const aaCount = _aaCount(aaTyped)
    const aaState = cx.state
    // cx.tabbed (import page, 2026-10-05): the form is its own tab, so it is always open, has no
    // +/- or title, opens with one line, and its Apply / Clear row sits under the fields (CSS order).
    const tabbed = !!cx.tabbed
    const aaOpen = tabbed || cx.open
    return `<div class="lq-applyall${aaOpen ? '' : ' is-closed'}${tabbed ? ' lq-applyall--tab' : ''}">
           ${tabbed ? '<p class="lq-applyall-intro">Enter information here to apply the values to all recordings in the Queue.</p>' : ''}
           <div class="lq-applyall-head">
             ${tabbed ? '' : `<button type="button" class="lq-applyall-tog" id="lq-applyall-toggle"
                     aria-expanded="${aaOpen}"
                     title="${aaOpen ? 'Collapse' : 'Expand'}"
                     >${icon(aaOpen ? 'minus' : 'plus', 'lq-applyall-ic')}</button>
             <span class="lq-applyall-h">Apply values to every recording below</span>`}
             ${!aaOpen && aaCount ? `<span class="lq-applyall-n">${aaCount} value${
                 aaCount === 1 ? '' : 's'} set</span>` : ''}
             <!-- Apply Values (Ryan, 2026-09-03). The blanket values do
                  NOTHING until this is pressed — not on the Review form, not
                  on auto-Ingest — and once pressed they OVERWRITE whatever the
                  scan inferred. Both halves are deliberate: overwriting is
                  what the box promises, and a deliberate press is what earns
                  the right to overwrite.
                  Three states, because "typed" and "staged" are different
                  facts and the difference is the whole feature.
                  ⚠ No number in this label. It said "Applied to all 4",
                  meaning four VALUES, and it read as four RECORDINGS (Ryan,
                  2026-09-03) — the one number a reader expects beside "applied
                  to all" is how many recordings it hit. The value count is
                  already on the collapsed header, where "4 values set" says
                  what it counts. -->
             ${aaState === 'empty' ? '' : aaState === 'applied'
               ? `<span class="lq-applyall-ok" title="These values overwrite the inferred metadata on every recording in this queue.">
                    ${icon('check', 'lq-applyall-ic')} Applied to all recordings</span>`
               : `<button type="button" class="lq-applyall-go" id="lq-applyall-apply"
                          ${cx.busy ? 'disabled' : ''}
                          title="Stage these values for every recording below. They will replace whatever the scan inferred for those fields.">
                    Apply Values</button>`}
             ${anyApplied || cx.applied ? `<button type="button" class="lq-applyall-x" id="lq-applyall-clear"
                               ${cx.busy ? 'disabled' : ''}>Clear All</button>` : ''}
           </div>
           ${!aaOpen ? '' : `<div class="lq-applyall-grid">
             <!-- Same rows and field classes as the Add Recording form
                  (ingest-row-act / ingest-row-src), so the inputs come out at
                  the same widths. -->
             <div class="ingest-field-grid ingest-row-act">
               <div class="ingest-field">
                 <label>Artist</label>
                 <div class="artist-picker-wrap">
                   <input type="text" id="lq-apply-artist" autocomplete="off"
                          value="${esc(aa.artist)}" ${cx.busy ? 'disabled' : ''}>
                   <div class="artist-dropdown" id="lq-apply-artist-dropdown" style="display:none"></div>
                 </div>
               </div>
               <div class="ingest-field">
                 <label>Event / Festival</label>
                 <input type="text" id="lq-apply-event"
                        value="${esc(aa.event)}" ${cx.busy ? 'disabled' : ''}>
               </div>
               <div class="ingest-field">
                 <label>Stage</label>
                 <input type="text" id="lq-apply-stage"
                        value="${esc(aa.stage)}" ${cx.busy ? 'disabled' : ''}>
               </div>
             </div>
             <div class="ingest-field">
               <label>Venue</label>
               <div class="venue-picker-wrap">
                 <input type="text" id="lq-apply-venue-name" autocomplete="off"
                        value="${esc(aa.venue.name)}" ${cx.busy ? 'disabled' : ''}>
                 <input type="hidden" id="lq-apply-venue-id" value="${esc(String(aa.venue.id || ''))}">
                 <div class="venue-dropdown" id="lq-apply-venue-dropdown" style="display:none"></div>
               </div>
             </div>
             <div class="ingest-field-grid ingest-row-loc">
               <div class="ingest-field">
                 <label>City</label>
                 <input type="text" id="lq-apply-city" value="${esc(aa.city)}"
                        ${cx.busy || aaVenueLocked ? 'disabled' : ''}
                        title="${aaVenueLocked ? 'Filled from the selected venue' : ''}">
               </div>
               <div class="ingest-field">
                 <label>State</label>
                 <input type="text" id="lq-apply-state" maxlength="6" value="${esc(aa.state)}"
                        ${cx.busy || aaVenueLocked ? 'disabled' : ''}
                        title="${aaVenueLocked ? 'Filled from the selected venue' : ''}">
               </div>
               <div class="ingest-field">
                 <label>Country</label>
                 <input type="text" id="lq-apply-country" value="${esc(aa.country)}"
                        ${cx.busy || aaVenueLocked ? 'disabled' : ''}
                        title="${aaVenueLocked ? 'Filled from the selected venue' : ''}">
               </div>
             </div>
             <div class="ingest-field-grid ingest-row-src">
               <div class="ingest-field">
                 <label>Source</label>
                 <input type="text" id="lq-apply-source"
                        value="${esc(aa.source)}" ${cx.busy ? 'disabled' : ''}>
               </div>
               <div class="ingest-field">
                 <label>Source Tag</label>
                 <input type="text" id="lq-apply-source-tag"
                        value="${esc(aa.source_tag)}" ${cx.busy ? 'disabled' : ''}>
               </div>
               <div class="ingest-field">
                 <label>Lineage</label>
                 <input type="text" id="lq-apply-lineage" value="${esc(aa.lineage)}"
                        ${cx.busy ? 'disabled' : ''}>
               </div>
             </div>
             <div class="ingest-field lq-applyall-notes">
               <label>Notes</label>
               <textarea id="lq-apply-notes" ${cx.busy ? 'disabled' : ''}>${esc(aa.notes)}</textarea>
             </div>
           </div>`}
         </div>`
  }

  // Field wiring for the block above: plain inputs, the Artist picker and the
  // Venue picker. `cx` = { aa, rerender(), repaintSoon(delay) }.
  function _wireApplyAll(cx) {
    // Plain text/textarea fields — same input/blur pattern as the Event field
    // always used.
    ;[['lq-apply-event', 'event'], ['lq-apply-stage', 'stage'], ['lq-apply-source', 'source'], ['lq-apply-source-tag', 'source_tag'],
      ['lq-apply-lineage', 'lineage'], ['lq-apply-notes', 'notes'],
      ['lq-apply-city', 'city'], ['lq-apply-state', 'state'], ['lq-apply-country', 'country'],
    ].forEach(([id, key]) => {
      const el = document.getElementById(id)
      if (!el) return
      el.addEventListener('input', e => { cx.aa[key] = e.target.value })
      el.addEventListener('blur', () => cx.repaintSoon())
    })

    // Artist — the same wirePickerDropdown() autocomplete the Members/
    // Guests widget uses, minus the members machinery: this only ever needs
    // one name, resolved/created server-side by name exactly like the Add
    // Recording form's own Artist field.
    ;(function () {
      const el = document.getElementById('lq-apply-artist')
      const dd = document.getElementById('lq-apply-artist-dropdown')
      if (!el) return
      el.addEventListener('input', e => { cx.aa.artist = e.target.value })
      el.addEventListener('blur', () => cx.repaintSoon())
      wirePickerDropdown(el, dd, API.artists.search, ({ name }) => {
        cx.aa.artist = name
        cx.rerender()
      }, 'Use as typed')
    })()

    // Venue — autocomplete with the same lock/unlock of City/State/Country as
    // the Add Recording form's venue picker (see the f-venue-name wiring
    // below): picking an existing row locks the location fields to ITS
    // stored values; typing or picking "+ Use as typed" unlocks them.
    ;(function () {
      const nameEl = document.getElementById('lq-apply-venue-name')
      const idEl   = document.getElementById('lq-apply-venue-id')
      const dropEl = document.getElementById('lq-apply-venue-dropdown')
      if (!nameEl) return
      let debounce = null
      function closeDropdown() { dropEl.style.display = 'none'; dropEl.innerHTML = '' }
      function showResults(venues, q) {
        const rows = venues.map(v => {
          const loc = [v.city, v.state, v.country].filter(Boolean).join(', ')
          return `<div class="venue-result" data-id="${v.id}" data-name="${esc(v.name)}">
            <span class="venue-result-name">${esc(v.name)}</span>
            ${loc ? `<span class="venue-result-loc">${esc(loc)}</span>` : ''}
          </div>`
        }).join('')
        const exact = venues.some(v => v.name.toLowerCase() === q.toLowerCase())
        const createRow = (q && !exact)
          ? `<div class="venue-result venue-result-create" data-id="" data-name="${esc(q)}">+ Use "${esc(q)}" as typed</div>`
          : ''
        dropEl.innerHTML = rows + createRow
        dropEl.style.display = (rows || createRow) ? 'block' : 'none'
        dropEl.querySelectorAll('.venue-result').forEach(el => {
          el.addEventListener('mousedown', async e => {
            e.preventDefault()
            if (el.dataset.id) {
              const id = parseInt(el.dataset.id)
              cx.aa.venue = { id, name: el.dataset.name }
              try {
                const v = await API.venues.get(id)
                if (!isPlaceholderVenue(v.name)) {
                  cx.aa.city = v.city || ''
                  cx.aa.state = v.state || ''
                  cx.aa.country = v.country || ''
                }
              } catch (_) {}
            } else {
              cx.aa.venue = { id: null, name: q }
            }
            cx.rerender()
          })
        })
      }
      nameEl.addEventListener('input', () => {
        idEl.value = ''
        cx.aa.venue = { id: null, name: nameEl.value }
        const q = nameEl.value.trim()
        clearTimeout(debounce)
        if (q.length < 2) { closeDropdown(); return }
        debounce = setTimeout(async () => {
          try { showResults(await API.venues.list(q), q) } catch (_) { closeDropdown() }
        }, 220)
      })
      nameEl.addEventListener('blur', () => cx.repaintSoon(200))
      nameEl.addEventListener('focus', () => {
        if (nameEl.value.trim().length >= 2) nameEl.dispatchEvent(new Event('input'))
      })
    })()
  }

  function _closeMoveMenus() {
    mainContent.querySelectorAll('.lq-move-menu').forEach(m => { m.hidden = true })
  }

  async function runScan(folderPath) {
    ingest.folderPath = folderPath  // re-set in case the render-reset cleared it
    const statusEl = document.getElementById('scan-status')
    // No status element means we are not on the picker — previously this
    // returned silently and the caller was left waiting forever. Fail loudly
    // into the recovery screen instead.
    if (!statusEl) {
      try { await API.recordings.scan(folderPath) }
      catch (e) { _ingestRenderFailed(e); return }
      return
    }
    statusEl.innerHTML = `
      <div class="empty-state" style="min-height:100px">
        <div class="loading-spinner"></div>
        <div style="margin-top:8px; color:var(--t2); font-size:12px">Scanning ${esc(folderPath.split('/').pop())}...</div>
      </div>`
    try {
      const scan = await API.recordings.scan(folderPath)
      ingest.scan = scan
      ingest.step = 'review'
      renderIngestStep()
      window.trellisDebug?.refresh()   // update the debug panel's Paula section if it's already open
    } catch (e) {
      // Clear the remembered folder: if it has gone away, keeping it means the
      // next visit to this page fails the same way.
      ingest.folderPath = null
      ingest.scan = null
      statusEl.innerHTML = `
        <div style="color:var(--red); font-size:13px; margin-top:12px; padding:12px 16px; background:rgba(224,85,85,0.08); border-radius:var(--r-sm);">
          Scan failed: ${esc(e.message)}
          <div style="color:var(--t2); margin-top:6px">If this show was just imported, its source folder was moved into the library and is no longer here.</div>
        </div>`
    }
  }

  // ── Step 2: Combined metadata + track review ──────────────────────────────

  function hintChips(fieldId, tagVal, infoVal) {
    const chips = []
    if (tagVal)  chips.push({ label: `Tags: ${tagVal}`, val: tagVal })
    if (infoVal && infoVal !== tagVal) chips.push({ label: `Info: ${infoVal}`, val: infoVal })
    if (!chips.length) return ''
    return `<div class="field-hints">
      ${chips.map((c, i) => `
        <span class="hint-chip ${i === 0 ? 'active' : ''}"
              data-field="${fieldId}" data-val="${esc(c.val)}">${esc(c.label)}</span>
      `).join('')}
    </div>`
  }

  // Paula's purple-border threshold — a starting point, meant to be tuned
  // once this has run against more real folders (Ryan, 2026-07-16: "let's
  // give it a try and see how it plays out"). The raw per-field subscore is
  // always visible in the debug panel regardless of where this line sits.
  const PAULA_THRESHOLD = 0.70
  function paulaCls(attrName) {
    const sub = ingest.scan?.paula?.attributes?.[attrName]?.subscore
    return (typeof sub === 'number' && sub >= PAULA_THRESHOLD) ? 'paula-recommend' : ''
  }

  // AI Assist tab body: the health score folded in (current + band message),
  // a Run button, and a container that fills with clean results after a run.
  // File Tags JSON (raw Vorbis per track) for the scan — same shape/formatting as
  // the recording view's File Tags pane.
  function scanFileTagsJson() {
    const tracks = ingest.scan?.suggestions?.from_tags?.tracks || []
    const obj = {}
    tracks.forEach(t => {
      const key = `${String(t.track_number || '').padStart(2, '0')} · ${t.title || ''}`
      obj[key] = t.raw || {}
    })
    return JSON.stringify(obj, null, 2)
  }

  // ── Lomax: the form, read and written ─────────────────────────────────────
  // Read the form's current metadata to send to Lomax.
  function collectCurrentMeta() {
    const g = id => (document.getElementById(id)?.value || '').trim()
    const y = g('f-year'), m = g('f-month'), d = g('f-day')
    const date = y
      ? `${y}${m ? '-' + String(m).padStart(2, '0') : ''}${(m && d) ? '-' + String(d).padStart(2, '0') : ''}`
      : ''
    return {
      artist:  g('f-artist'), date, venue: g('f-venue-name'),
      city:    g('f-city'),   state: g('f-state'), country: g('f-country'),
      source:  g('f-source'), lineage: g('f-lineage'), event: g('f-event-name'),
      stage:   g('f-stage'),   genre: g('f-genre'),   fingerprint: lxFingerprint(ingest.scan),
      tracks:  (ingest.tracks || []).map(t => ({
        number: t.track_number, title: t.title, duration: t.duration,
        songwriter: t.songwriter || '', notes: t.notes || '',
      })),
      info_file_content: ingest.scan.info_file_content || '',
      resolved: ingest.scan.resolved || null,
    }
  }

  // The album form as the Lomax album skill reads it.
  function collectAlbumMeta() {
    const g = id => (document.getElementById(id)?.value || '').trim()
    return {
      artist: g('f-artist'), title: g('f-album-title'), year: g('f-year'),
      tracks: (ingest.tracks || []).map(t => ({
        number: t.track_number, title: t.title, duration: t.duration, songwriter: t.songwriter || '',
      })),
      info_file_content: ingest.scan.info_file_content || '',
      notes: g('f-notes'),
      fingerprint: lxFingerprint(ingest.scan),
    }
  }

  // Release facts of the MusicBrainz release picked on the Add Recording album form, under the
  // album title (label, catalog number, country; the same line View Recording shows).
  function ingestMbFactsHtml() {
    const p = ingest.mb && ingest.mb.picked
    if (!p) return ''
    const facts = [p.label, p.catalog_number, p.country].filter(Boolean).join(' · ') || p.title || 'Release'
    return `<a class="pp-mb-name" href="${esc(mbReleaseUrl(p.mbid))}" target="_blank" rel="noopener">${esc(facts)} ↗</a>`
  }

  // Read the current value of a proposal's target field (for revert).
  function getFormField(field) {
    const g = id => document.getElementById(id)?.value || ''
    switch (field) {
      case 'artist':  return g('f-artist')
      case 'venue':   return g('f-venue-name')
      case 'city':    return g('f-city')
      case 'state':   return g('f-state')
      case 'country': return g('f-country')
      case 'event':   return g('f-event-name')
      case 'source':  return g('f-source')
      case 'stage':   return g('f-stage')
      case 'lineage': return g('f-lineage')
      case 'genre':   return g('f-genre')
      case 'date': {
        const y = g('f-year'), m = g('f-month'), d = g('f-day')
        return y ? `${y}${m ? '-' + String(m).padStart(2, '0') : ''}${(m && d) ? '-' + String(d).padStart(2, '0') : ''}` : ''
      }
    }
    return ''
  }

  // Write a value into the form field(s) for a proposal, highlighting the input.
  function setFormField(field, value) {
    const set = (id, v) => {
      const el = document.getElementById(id)
      if (el) { el.value = v; el.classList.toggle('ai-applied', v !== '' && v != null) }
    }
    switch (field) {
      case 'artist':  set('f-artist', value);      ingest.form.artist_name = value; break
      case 'venue':   set('f-venue-name', value);  ingest.form.venue_name  = value; break
      case 'city':    set('f-city', value);        ingest.form.city        = value; break
      case 'state':   set('f-state', value);       ingest.form.state       = value; break
      case 'country': set('f-country', value);     ingest.form.country     = value; break
      case 'event':   set('f-event-name', value);  ingest.form.event_name  = value; break
      // Source is free text now (2026-08-08 — was a fixed SBD/AUD/MTX/FM/
      // DVB-S/Other <select>, same as every other field it lives beside), so
      // it no longer needs the old "only accept a value that's one of the
      // <select>'s options" guard.
      case 'source':  set('f-source', value);      ingest.form.source      = value; break
      case 'stage':   set('f-stage', value);       ingest.form.stage       = value; break
      case 'lineage': set('f-lineage', value);     ingest.form.lineage     = value; break
      case 'date': {
        const p = String(value).split('-')
        set('f-year', p[0] || '')
        set('f-month', p[1] ? parseInt(p[1]) : '')
        set('f-day',   p[2] ? parseInt(p[2]) : '')
        ingest.form.start_year  = p[0] || ''
        ingest.form.start_month = p[1] ? parseInt(p[1]) : ''
        ingest.form.start_day   = p[2] ? parseInt(p[2]) : ''
        break
      }
    }
  }

  // Render a standardized LCR info-file text from the live form + tracks + AI
  // provenance notes — the "Proposed" side of the compare and the confirm regen.
  function buildInfoFileText() {
    const g = id => (document.getElementById(id)?.value || '').trim()
    const y = g('f-year'), m = g('f-month'), d = g('f-day')
    const date = y ? `${y}${m ? '-' + String(m).padStart(2, '0') : ''}${(m && d) ? '-' + String(d).padStart(2, '0') : ''}` : ''
    const loc  = [g('f-city'), g('f-state'), g('f-country')].filter(Boolean).join(', ')
    const L = []
    if (g('f-artist'))     L.push(g('f-artist'))
    if (date)              L.push(date)
    if (g('f-venue-name')) L.push(g('f-venue-name'))
    if (loc)               L.push(loc)
    if (g('f-source'))     L.push(g('f-source'))
    if (g('f-lineage') || g('f-event-name')) L.push('')
    if (g('f-lineage'))    L.push('Lineage: ' + g('f-lineage'))
    if (g('f-event-name')) L.push('Event: ' + g('f-event-name'))
    L.push('', 'Setlist:', '')
    let lastSet = null
    ;(ingest.tracks || []).forEach((t, i) => {
      if (t.set_number && t.set_number !== lastSet) { L.push(t.set_number); lastSet = t.set_number }
      L.push(`${String(t.track_number || i + 1).padStart(2, '0')}. ${t.title || ''}`.trimEnd())
    })
    const prov = ingest.lxc?.latest()?.result?.provenance_notes || []
    if (prov.length) { L.push('', 'Notes:'); prov.forEach(n => L.push(n)) }
    return L.join('\n')
  }

  // ── Reusable track context menu (right-click): flags + songwriter + note ──────
  // Shared by the recording view, Edit, and Add. opts.onChange(track) fires after any
  // change; the caller persists (API for saved recordings, local state for ingest)
  // and refreshes the row. The menu mutates track.flags/songwriter/notes in place.
  function _closeTrackMenu() {
    const m = document.getElementById('track-qmenu')
    if (m) { try { m._commit?.() } catch (_) {} m.remove() }
    document.removeEventListener('mousedown', _trackMenuOutside)
    document.removeEventListener('keydown', _trackMenuEsc)
  }
  function _trackMenuOutside(e) {
    const m = document.getElementById('track-qmenu')
    if (m && !m.contains(e.target)) _closeTrackMenu()
  }
  function _trackMenuEsc(e) { if (e.key === 'Escape') _closeTrackMenu() }

  function openTrackMenu(track, clientX, clientY, opts = {}) {
    _closeTrackMenu()
    const onChange = opts.onChange || (() => {})
    // flagsOnly: Add Recording's table now has Note/Songwriter as click-to-edit
    // cells directly (Ryan, 2026-07-15), so its right-click popup is Flags
    // (+ Official, if showOfficial) only. View Recording still gets the full
    // Note/Songwriter/Flags/Official grid — it doesn't pass this option.
    const flagsOnly = !!opts.flagsOnly
    const menu = document.createElement('div')
    menu.className = 'track-qmenu'
    menu.id = 'track-qmenu'
    // Alphabetical, because TRACK_FLAGS itself is (2026-09-01) — no sort here,
    // so the popup and every other consumer read one ordering from one place.
    const flagPills = TRACK_FLAGS.map(f => {
      const active = (track.flags || []).includes(f.key)
      return `<button class="flag-pill ${active ? 'active' : ''}" data-flag="${f.key}" type="button"
                      aria-pressed="${active}">${f.label}</button>`
    }).join('')
    // Official-release toggle — opt-in (opts.showOfficial) since View Recording
    // manages that per-track flag elsewhere; Add Recording has no other place
    // for it once the expand row goes away, so it lives here for that caller.
    //
    // Its own footer row below a rule, not another `.et-detail-field`
    // (Ryan, 2026-09-01: "colocated next to its checkbox and no line break").
    // The old markup borrowed the expand-row's field wrapper, whose
    // `label { display: block }` puts a label ABOVE its control — the exact
    // opposite of what a checkbox wants — and only a second, more specific
    // `.check-inline` rule pulled it back onto one line. Two rules fighting
    // over one element is how the break got in; a purpose-built class with
    // `white-space: nowrap` on the text cannot break at all, whatever the
    // menu width.
    //
    // It is also NOT a flag. Flags describe what is on the tape; official
    // release is a rights fact about the recording. Same popup, separated by a
    // rule, so the two never read as thirteen pills.
    const officialRow = opts.showOfficial
      ? `<label class="track-qmenu-official-row" title="Mark this track as an official release">
           <input type="checkbox" class="track-qmenu-official" ${track.is_official ? 'checked' : ''} />
           <span>Official release</span>
         </label>`
      : ''
    // Stacked full width, not the old two-up grid. In a 300px popup, side by
    // side gave each field ~140px — narrower than most song titles and far
    // narrower than a note. Vertical costs one scroll of nothing and makes
    // both fields usable.
    const detailGrid = flagsOnly ? '' : `
      <div class="track-qmenu-field">
        <span class="track-qmenu-label">Note</span>
        <textarea class="track-qmenu-note" placeholder="Add a note…">${esc(track.notes || '')}</textarea>
      </div>
      <div class="track-qmenu-field">
        <span class="track-qmenu-label">Songwriter</span>
        <input class="track-qmenu-songwriter" type="text" placeholder="Songwriter…" value="${esc(track.songwriter || '')}" />
      </div>`
    const setRow = opts.showSet ? `
      <div class="track-qmenu-field">
        <span class="track-qmenu-label">Set</span>
        <input class="track-qmenu-set" type="text" value="${esc(track.set_number || '')}" />
      </div>` : ''
    menu.innerHTML = `
      <div class="track-qmenu-title">${esc(String(track.track_number || '').padStart(2, '0'))} · ${esc(track.title || '')}</div>
      ${detailGrid}
      ${setRow}
      <div class="track-qmenu-field">
        <span class="track-qmenu-label">Flags</span>
        <div class="flag-pill-row track-qmenu-flags">${flagPills}</div>
      </div>
      ${officialRow}`
    document.body.appendChild(menu)

    menu.querySelector('.track-qmenu-official')?.addEventListener('change', function () {
      track.is_official = this.checked
      onChange(track)
    })

    // Position at cursor, clamped to the viewport
    const r = menu.getBoundingClientRect()
    menu.style.left = Math.max(8, Math.min(clientX, window.innerWidth  - r.width  - 8)) + 'px'
    menu.style.top  = Math.max(8, Math.min(clientY, window.innerHeight - r.height - 8)) + 'px'

    // Flags — toggle notifies immediately
    menu.querySelectorAll('.flag-pill').forEach(btn => {
      btn.addEventListener('click', () => {
        btn.classList.toggle('active')
        btn.setAttribute('aria-pressed', btn.classList.contains('active'))
        track.flags = [...menu.querySelectorAll('.flag-pill.active')].map(b => b.dataset.flag)
        onChange(track)
      })
    })

    // Songwriter + Note — commit on Enter / on close. Only present when
    // !flagsOnly (Add Recording's flagsOnly popup has neither field).
    const swEl   = menu.querySelector('.track-qmenu-songwriter')
    const noteEl = menu.querySelector('.track-qmenu-note')
    if (swEl && noteEl) {
      const commit = () => {
        const sw   = swEl.value.trim() || null
        const note = noteEl.value.trim() || null
        let changed = false
        if (sw !== (track.songwriter || null)) { track.songwriter = sw; changed = true }
        if (note !== (track.notes || null))     { track.notes = note;    changed = true }
        if (changed) onChange(track)
      }
      menu._commit = commit
      // Auto-save on complete: commit when the field loses focus, and on Enter.
      swEl.addEventListener('blur', commit)
      noteEl.addEventListener('blur', commit)
      swEl.addEventListener('keydown', e => {
        e.stopPropagation()
        if (e.key === 'Enter') { e.preventDefault(); commit() }
      })
      noteEl.addEventListener('keydown', e => {
        e.stopPropagation()
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commit(); _closeTrackMenu() }
      })
    }

    // Set -- commit on Enter / blur, empty clears it. Chained onto _commit so
    // closing the menu with the field still focused saves it too.
    const setEl = menu.querySelector('.track-qmenu-set')
    if (setEl) {
      const commitSet = () => {
        const v = setEl.value.trim() || null
        if (v === (track.set_number || null)) return
        track.set_number = v
        opts.onSet?.(track)
      }
      const prior = menu._commit
      menu._commit = () => { prior?.(); commitSet() }
      setEl.addEventListener('keydown', e => {
        e.stopPropagation()
        if (e.key === 'Enter') { e.preventDefault(); commitSet(); _closeTrackMenu() }
      })
    }

    setTimeout(() => {
      document.addEventListener('mousedown', _trackMenuOutside)
      document.addEventListener('keydown', _trackMenuEsc)
    }, 0)
  }

  // Re-score the current form state and update the AI tab's score header.
  // The fields Rescan would overwrite, as a comparable string. Deliberately
  // NOT every key on the form: `members`/`guests` are objects, `_filled` and
  // friends are bookkeeping, and none of them should make an untouched form
  // look edited.
  const _INGEST_SNAPSHOT_KEYS = [
    'artist_name', 'start_year', 'start_month', 'start_day',
    'end_year', 'end_month', 'end_day', 'venue_name', 'city', 'state',
    'country', 'source', 'quality', 'lineage', 'notes', 'event_name', 'stage',
  ]
  // Queue-level values → this form (Ryan, 2026-09-02).
  //
  // "Apply values to every recording below" already reached the server on BOTH
  // ingest paths — the confirm payload falls back to it field by field — but
  // on the Review path it did so INVISIBLY: you set a Venue for the batch,
  // clicked Review on one show to check it, and the Venue box was empty. The
  // form is what the reviewer reads to decide whether the record is right, so
  // a value that will be written and is not on the form is the form lying.
  //
  // Only ever fills a field the form left EMPTY. The form is the more specific
  // statement: an inferred artist, or one the reviewer typed, outranks the
  // batch default, and that is the same precedence the confirm payload has
  // always used — this just makes it visible before the click rather than
  // after.
  //
  // Returns how many fields it filled, so the caller can say so.
  // ── Blanket values: staged, and they WIN ──────────────────────────────────
  //
  // Reversed 2026-09-03 (Ryan), and the reversal is the whole point, so read
  // this before touching any of it.
  //
  // These used to fill only fields the inference had left EMPTY, on the
  // reasoning that the form is the more specific statement. That is wrong for
  // what the control is actually for: a queue of twenty shows from one festival
  // where the taper's own info files each guessed a slightly different venue
  // string. The human typing one venue into "applies to every recording below"
  // knows more than twenty parsers do, and having their value quietly lose to
  // whatever a text file said is the opposite of what the box promises.
  //
  // So a staged value OVERWRITES. And because overwriting is destructive, it
  // does not happen on its own: nothing propagates until **Apply Values** is
  // pressed. `lq.applyAll` is what is typed; `lq.applied` is what was pressed.
  // Only `lq.applied` is ever read by the ingest paths — typing alone changes
  // nothing, anywhere, including auto-Ingest (Ryan's call: "Apply is the only
  // path"). That is a deliberate trade of convenience for the guarantee that
  // nothing overwrites a parsed value without a deliberate press.
  //
  // Empty blanket fields are NOT staged. "Apply" means "use the values I
  // typed", never "blank out the eight fields I left alone".

  // The nine fields, in one place, so the snapshot, the dirty check and both
  // writers cannot drift. `form`/`live` are the ids each value lands on.
  const _AA_FIELDS = [
    { key: 'artist', form: 'artist_name', live: 'f-artist' },
    { key: 'venue',     form: 'venue_name',  live: 'f-venue-name' },   // + id
    { key: 'city',      form: 'city',        live: 'f-city' },
    { key: 'state',     form: 'state',       live: 'f-state' },
    { key: 'country',   form: 'country',     live: 'f-country' },
    { key: 'event',     form: 'event_name',  live: 'f-event-name' },   // + id
    { key: 'stage',     form: 'stage',       live: 'f-stage' },
    { key: 'source',    form: 'source',      live: 'f-source' },
    { key: 'source_tag', form: 'source_tag', live: 'f-source-tag' },
    { key: 'lineage',   form: 'lineage',     live: 'f-lineage' },
    { key: 'notes',     form: 'notes',       live: 'f-notes' },
  ]

  // What is currently TYPED, normalised: {key: value} for non-empty fields
  // only, plus venue_id when a real Venue row was picked.
  function _aaTyped(aa) {
    if (!aa) return {}
    const out = {}
    for (const f of _AA_FIELDS) {
      const v = String((f.key === 'venue' ? aa.venue.name : aa[f.key]) || '').trim()
      if (v) out[f.key] = v
    }
    if (out.venue && aa.venue.id) out.venue_id = aa.venue.id
    return out
  }

  // Compared as a string, not field by field, so a venue id changing under an
  // unchanged venue NAME still counts as a change worth re-applying.
  function _aaFingerprint(o) {
    return JSON.stringify(Object.keys(o || {}).sort().map(k => [k, o[k]]))
  }

  function _aaCount(o) {
    return Object.keys(o || {}).filter(k => k !== 'venue_id').length
  }

  // Write the STAGED values over a form object, at prefill, before the DOM
  // exists. Returns how many fields it set.
  //
  // ⚠ Overwrites unconditionally — that is the change. The inferred value is
  // not lost forever: Rescan re-derives the whole form from the info file, and
  // re-applies whatever is staged afterwards.
  function _applyQueueValuesToForm(f) {
    const a = _biApplied
    if (!a) return 0
    let n = 0
    for (const fld of _AA_FIELDS) {
      const v = a[fld.key]
      if (!v) continue
      f[fld.form] = v
      n += 1
    }
    // An id and a name must move together, or the server resolves a SECOND row
    // by name for the very venue the batch picked. Clearing the id when the
    // blanket venue was typed rather than picked is equally load-bearing:
    // leaving the inferred row's id behind would file the show against a venue
    // whose name is no longer on the form.
    if (a.venue) f.venue_id = a.venue_id || null
    if (a.event) f.event_id = null
    return n
  }

  // The same write, against the LIVE INPUTS.
  //
  // Two variants exist because the two moments are genuinely different, and
  // getting that wrong loses data. At PREFILL there is no DOM yet, so the
  // object is the only thing to write to. Once the page is up, `ingest.form`
  // is stale by design — it is written back only on submit (see
  // _ingestFormEdited, which reads the inputs for exactly this reason) — so
  // writing to the object and repainting would throw away everything typed
  // since the page loaded.
  //
  // `.ai-applied` is borrowed from AI Assist's own apply: it is already the
  // app's way of saying "this box was filled by something other than you",
  // which matters more now that the fill can replace something.
  function _applyQueueValuesToLiveForm() {
    const a = _biApplied
    if (!a) return 0
    let n = 0
    for (const fld of _AA_FIELDS) {
      const v = a[fld.key]
      if (!v) continue
      const el = document.getElementById(fld.live)
      if (!el) continue
      el.value = v
      el.classList.add('ai-applied')
      n += 1
    }
    const vId = document.getElementById('f-venue-id')
    if (a.venue && vId) vId.value = a.venue_id ? String(a.venue_id) : ''
    const eId = document.getElementById('f-event-id')
    if (a.event && eId) eId.value = ''
    return n
  }

  // How many blanket values are STAGED. Drives whether the Review form offers
  // its re-apply button at all — unstaged values have no effect anywhere, so
  // offering to apply them would be offering a no-op.
  function _queueValuesCount() {
    return _aaCount(_biApplied)
  }

  function _ingestFormSnapshot(f) {
    return _INGEST_SNAPSHOT_KEYS.map(k => String(f[k] ?? '')).join('\u0000')
  }

  // True when the reviewer has changed anything the inference filled in.
  // Reads the LIVE inputs rather than ingest.form, because the form object is
  // only written back on submit — comparing it to the snapshot would say
  // "unedited" no matter how much had been typed.
  function _ingestFormEdited() {
    const f = ingest.form
    if (!f || !f._inferred) return false
    const g = id => (document.getElementById(id)?.value ?? '').trim()
    const live = {
      artist_name: g('f-artist'), start_year: g('f-year'),
      start_month: g('f-month'), start_day: g('f-day'),
      end_year: g('f-end-year'), end_month: g('f-end-month'), end_day: g('f-end-day'),
      venue_name: g('f-venue-name'), city: g('f-city'), state: g('f-state'),
      country: g('f-country'), source: g('f-source'), quality: g('f-quality'),
      lineage: g('f-lineage'), notes: g('f-notes'), event_name: g('f-event-name'),
      stage: g('f-stage'),
    }
    return _ingestFormSnapshot(live) !== f._inferred
  }

  async function reScore() {
    if (!ingest.scan) return
    const g = id => (document.getElementById(id)?.value || '').trim()
    if (ingest.kind === 'studio') {
      const titles = [...document.querySelectorAll('.t-title')].map(el => ({ title: el.value }))
      const h = _studioCompleteness(g('f-artist'), g('f-year'), titles.length ? titles : (ingest.tracks || []))
      const valEl = document.querySelector('#iq-score .meta-readout-value')
      if (valEl) {
        valEl.textContent = h.rating
        valEl.className = 'meta-readout-value meta-readout-value--' + h.band
      }
      return
    }
    const y = g('f-year'), m = g('f-month'), d = g('f-day')
    const date = y ? `${y}${m ? '-' + String(m).padStart(2, '0') : ''}${(m && d) ? '-' + String(d).padStart(2, '0') : ''}` : ''
    const clone = JSON.parse(JSON.stringify(ingest.scan))
    const t = clone.suggestions.from_tags, inf = clone.suggestions.from_info_file
    const both = (k, v) => { t[k] = v; inf[k] = v }
    both('artist', g('f-artist')); both('venue', g('f-venue-name'))
    both('city', g('f-city')); both('state', g('f-state')); both('country', g('f-country'))
    both('source', g('f-source')); both('lineage', g('f-lineage'))
    t.concert_date = date
    inf.year = parseInt(y) || null; inf.month = parseInt(m) || null; inf.day = parseInt(d) || null
    inf.tracks = (ingest.tracks || []).map((tk, i) => ({ number: tk.track_number || i + 1, title: tk.title || '' }))
    try {
      const h = await API.ingest.health(clone)
      ingest.scan.health = h
      // Only the VALUE is rewritten now (2026-09-01) — the label is static
      // markup. Rewriting the whole element's innerHTML, as this used to, is
      // what made it easy to lose the label wording on an edit; there is now
      // one place the words "Metadata Completeness" appear at all.
      const valEl = document.querySelector('#iq-score .meta-readout-value')
      if (valEl) {
        // The RATING word, not the raw number (Ryan, 2026-08-13 — Low/Medium/
        // High replaced the numeric score everywhere). This once wrote
        // `h.score`, so it rendered "High" on first paint and silently became
        // "82" on the first field edit.
        valEl.textContent = _metaRating(h)
        valEl.className = 'meta-readout-value meta-readout-value--' + h.band
      }
    } catch (_) {}
  }

  // Poll a background /api/ingest/confirm job until it finishes. `onProgress`
  // (optional) is called on each running tick with (copied, total) bytes — the
  // copy step can take a while for big folders. Used by both the Add Recording
  // confirm step and batch import, so neither one can silently move on before
  // the ingest is actually done.
  async function pollConfirmJob(jobId, onProgress) {
    const sleep = ms => new Promise(r => setTimeout(r, ms))
    while (true) {
      await sleep(600)
      const s = await API.ingest.confirmStatus(jobId)
      if (s.status === 'running') {
        // The full status object, not just the two copy counters. Those are
        // meaningful during exactly one of the job's phases; `phase_label` is
        // what the other three have to say for themselves.
        if (onProgress) onProgress(s.copied || 0, s.total || 0, s)
      } else if (s.status === 'done') {
        return s.result
      } else if (s.status === 'cancelled') {
        // Null, not an exception: a cancel is something the user asked for, and
        // the worker has already undone its own work. Callers distinguish this
        // from success by the null.
        return null
      } else if (s.status === 'error') {
        throw new Error(s.error)
      }
    }
  }

  // Switch which right-column pane is visible in the ingest review.
  /** Show one pane of the Add Recording details panel.
   *
   *  Also opens the panel if it was collapsed, syncs the shared action row,
   *  and kicks the lazy loads. `tabEl` is optional: callers that know only the
   *  pane id can leave it out and the tab is found by
   *  data-ipane. */
  function switchIngestPane(paneId, tabEl) {
    const root = document.getElementById('ingest-slide-panel')
    if (!root) return
    if (!tabEl) tabEl = root.querySelector(`.slide-tab[data-ipane="${paneId}"]`)
    _ingestPanelOpen(true)
    root.querySelectorAll('.slide-tab').forEach(t => t.classList.toggle('active', t === tabEl))
    root.querySelectorAll('.slide-pane').forEach(el => el.classList.toggle('active', el.id === paneId))
    state.ingestLastPane = paneId
    syncIngestPaneActs(paneId)
    if (paneId === 'isp-quality') loadIngestQualityPane()
  }

  /** The shared action row, shown by data-for — the same rule View Recording
   *  adopted on 2026-08-21. The row hides itself entirely when the active pane
   *  has no action to offer, rather than sitting there as an empty bar. */
  function syncIngestPaneActs(paneId) {
    const row = document.getElementById('ingest-pane-acts')
    if (!row) return
    if (paneId == null) {
      paneId = document.querySelector('#ingest-slide-panel .slide-tab.active')?.dataset.ipane || null
    }
    let any = false
    row.querySelectorAll('[data-for]').forEach(el => {
      // .act-suppressed is a SECOND, separate reason a control stays hidden —
      // Save to File is suppressed while the info file is locked. Without this
      // term the pane switcher un-hid it again on every tab change, so a
      // locked file showed a Save button (2026-08-28).
      el.hidden = el.dataset.for !== paneId || el.classList.contains('act-suppressed')
      // Status text and notes are not actions and must not hold the row open.
      const isAction = !el.classList.contains('pane-act-status') &&
                       !el.classList.contains('pane-act-note')
      if (!el.hidden && isAction) any = true
    })
    const tab = document.querySelector(`#ingest-slide-panel .slide-tab[data-ipane="${paneId}"]`)
    const title = document.getElementById('ingest-pane-title')
    if (title && tab) title.textContent = tab.textContent
    row.hidden = false
  }

  /** Open or collapse the details panel, as a slide.
   *
   *  The panel owns its width, so the collapse is a width transition and the
   *  form takes back the space frame by frame. The one wrinkle is the drag
   *  handle, which sets that width INLINE and would therefore beat the
   *  collapsed rule: the dragged width is stashed and the inline value cleared
   *  on the way in, then put back on the way out.
   *
   *  Stashed on `ingest`, not as an expando on the element: setMainHTML
   *  destroys the node on every re-render, so a collapse that survived a
   *  Back-and-return would have reopened at the CSS default rather than the
   *  width the reviewer had dragged to. */
  function _ingestPanelOpen(open) {
    const panel = document.getElementById('ingest-slide-panel')
    const grip  = document.getElementById('rev-divider')
    if (!panel) return
    const was = panel.classList.contains('open')
    state.ingestPanelOpen = !!open
    document.getElementById('ingest-slide-rail')?.setAttribute('aria-expanded', open ? 'true' : 'false')
    if (grip) grip.hidden = !open          // nothing to drag against when closed
    if (was === !!open) { panel.classList.toggle('open', !!open); return }
    if (!open) {
      if (panel.style.width) ingest._panelWidth = panel.style.width
      panel.style.width = ''
      panel.style.flexBasis = ''
    } else if (ingest._panelWidth) {
      panel.style.width     = ingest._panelWidth
      panel.style.flexBasis = ingest._panelWidth
    }
    panel.classList.toggle('open', !!open)
  }

  /** The Quality tab: the TRIAGE pass's numbers, because nothing on this page
   *  has been ingested yet and so has no permanent score (Ryan, 2026-08-28:
   *  "show the triage pass's partial numbers if that's all that exists").
   *
   *  /api/quality/staging/features is keyed by folder path, not by how the
   *  reviewer got here, so it works the same from the triage queue, from bulk
   *  import and from a single add. 404 is the ordinary "this folder was never
   *  analyzed" answer, not a failure worth a red box: Quick Add skips the
   *  analysis pass by design, so most folders reaching this page legitimately
   *  have nothing to show. Rendered by the same builder View Recording uses,
   *  minus the spectrogram. */
  let _ingestQualityLoaded = false
  async function loadIngestQualityPane() {
    if (_ingestQualityLoaded) return
    _ingestQualityLoaded = true
    const body = document.getElementById('isp-quality-body')
    if (!body) return
    // Which folder this fetch is FOR. The pane is addressed by a fixed id, so
    // without this a slow response for folder A could paint itself under
    // folder B after a Back and a second Review — and the one-shot flag meant
    // nothing would ever correct it.
    const forFolder = ingest.folderPath
    try {
      const q = await API.quality.stagingFeatures(forFolder)
      if (ingest.folderPath !== forFolder) return
      // A row can exist with the analysis never having produced a score (an
      // errored or interrupted pass). Treat that as "nothing to show" too.
      if (!q || q.listening_quality == null) throw new Error('no analysis')
      body.innerHTML = buildQualityPaneHtml(
        { verdict_band: q.verdict_band, interpretation: q.interpretation },
        { spectrogram: false })
      body.querySelectorAll('.rq-adv-toggle').forEach(btn => {
        btn.addEventListener('click', () => {
          const wrap = btn.closest('.rq-grp')?.querySelector('.rq-adv')
          if (!wrap) return
          const open = wrap.classList.toggle('open')
          btn.classList.toggle('open', open)
          btn.setAttribute('aria-expanded', open ? 'true' : 'false')
        })
      })
    } catch (_) {
      if (ingest.folderPath !== forFolder) return
      // Same empty state and button as View Recording's Quality pane. The old line here promised a
      // measurement during import that does not happen for review rows, albums or in-library runs.
      body.innerHTML = `<div class="rq-empty">Run Analyze Audio</div>
        ${canEditLibrary() ? '<button class="pane-act" id="btn-ingest-analyze">Analyze Audio</button>' : ''}`
      document.getElementById('btn-ingest-analyze')?.addEventListener('click', e => _ingestAnalyze(e.currentTarget, forFolder))
    }
  }

  // Score this folder now: the staging row the analyzer writes is the one import promotes, so the
  // score carries into the library. Polls the job, then repaints the pane from the fresh row.
  async function _ingestAnalyze(btn, forFolder) {
    btn.disabled = true
    btn.textContent = 'Analyzing…'
    try {
      const { job_id } = await API.quality.analyze(forFolder)
      for (;;) {
        await new Promise(r => setTimeout(r, 1500))
        if (ingest.folderPath !== forFolder) return
        const st = await API.quality.analyzeStatus(job_id)
        if (st.status !== 'running') {
          if (st.status === 'error') throw new Error(st.error || 'Analysis failed')
          break
        }
      }
      _ingestQualityLoaded = false
      await loadIngestQualityPane()
    } catch (e) {
      if (ingest.folderPath !== forFolder) return
      btn.disabled = false
      btn.textContent = 'Analyze Audio'
      alert('Analysis failed: ' + e.message)
    }
  }

  /** Leave the metadata review step the way it was entered: a review opened
   *  from a run's import page goes back to that page, otherwise to the picker.
   *
   *  Shared by the in-page back link and the header Back button. */
  function ingestBackFromReview() {
    if (ingest.returnTo) {
      _ingestReturnToRun()
      return
    }
    ingest.step = 'folder'
    renderIngestStep()
    paintNavButtons()
  }

  // Back to the import page that opened this review. The page paints through
  // route(), but window.location.hash is still '#/ingest' from the way in, so
  // the recorded hash and the nav stack are corrected first (replaceState
  // fires no hashchange) and route() is dispatched directly.
  function _ingestReturnToRun() {
    const hash = ingest.returnTo
    resetIngestState()
    history.replaceState(null, '', hash)
    _navRewrite(hash)
    setInPageBack(null)
    route()
    paintNavButtons()
  }

  // Album (studio) form: Metadata Completeness counts only artist, year and tracks, the same
  // rule as the queue's band (app/utils/completeness.py).
  const _PLACEHOLDER_TITLE_RE = /^(?:(?:cd|disc|disk|side)\s*\w*\s*)?(?:track|trk|t|d\d+\s*t)\s*[-_. ]?\s*\d+$|^\d+$/i
  const _PLACEHOLDER_TITLES = ['untitled', 'unknown', 'unknown title', 'audio track', 'track']
  function _isRealTrackTitle(title) {
    const t = String(title || '').trim().toLowerCase().split(/\s+/).join(' ')
    return !!t && !_PLACEHOLDER_TITLES.includes(t) && !_PLACEHOLDER_TITLE_RE.test(t)
  }
  function _studioCompleteness(artist, year, tracks) {
    const miss = []
    if (!String(artist || '').trim()) miss.push('artist')
    if (!String(year || '').trim()) miss.push('date')
    if (!tracks.length || !tracks.every(t => _isRealTrackTitle(t.title))) miss.push('tracks')
    const band = (miss.length >= 2 || miss.includes('artist') || miss.includes('date')) ? 'red'
               : miss.length ? 'yellow' : 'green'
    return { band, rating: { green: 'High', yellow: 'Medium', red: 'Low' }[band] }
  }

  // Write the live inputs of the Add Recording form back onto ingest.form (and the track titles
  // onto ingest.tracks). Used by both exits and by the Album / Live Recording switch, so a
  // re-render keeps everything typed. The live-only inputs stay in the page, hidden, on an album.
  function _syncIngestFormFromDom() {
    const f = ingest.form
      f.artist_name     = document.getElementById('f-artist').value.trim()
      f.start_year      = parseInt(document.getElementById('f-year').value)      || null
      f.start_month     = parseInt(document.getElementById('f-month').value)     || null
      f.start_day       = parseInt(document.getElementById('f-day').value)       || null
      f.end_year        = parseInt(document.getElementById('f-end-year').value)  || null
      f.end_month       = parseInt(document.getElementById('f-end-month').value) || null
      f.end_day         = parseInt(document.getElementById('f-end-day').value)   || null
      f.venue_name      = document.getElementById('f-venue-name').value.trim()
      f.venue_id        = parseInt(document.getElementById('f-venue-id').value) || null
      f.city            = document.getElementById('f-city').value.trim()
      f.state           = document.getElementById('f-state').value.trim()
      f.country         = document.getElementById('f-country').value.trim()
      f.event_name      = document.getElementById('f-event-name').value.trim()
      f.event_id        = parseInt(document.getElementById('f-event-id').value) || null
      f.stage           = document.getElementById('f-stage').value.trim()
      // Genre: an id for an existing genre, or a name for one the user
      // explicitly chose to create. A NAME WITH NO ID that was merely typed and
      // never confirmed through the create row is discarded here rather than
      // minting a vocabulary entry from a half-finished keystroke — see the
      // picker's wiring for the full argument.
      f.genre_id        = parseInt(document.getElementById('f-genre-id').value) || null
      f.genre_name      = document.getElementById('f-genre').classList.contains('is-new-genre')
                          ? document.getElementById('f-genre').value.trim()
                          : ''
      f.is_official     = document.getElementById('f-is-official').checked
      f.source          = document.getElementById('f-source').value
      f.quality         = document.getElementById('f-quality').value.trim()
      f.lineage         = document.getElementById('f-lineage').value.trim()
      f.source_tag      = document.getElementById('f-source-tag').value.trim()
      f.etree_shnid     = document.getElementById('f-shnid').value.trim()
      f.notes           = document.getElementById('f-notes').value.trim()
    f.album_title     = (document.getElementById('f-album-title')?.value ?? f.album_title ?? '').trim()
    f._genreText      = document.getElementById('f-genre')?.value || ''
    mainContent.querySelectorAll('.t-title').forEach(el => {
      const t = ingest.tracks[parseInt(el.dataset.idx)]; if (t) t.title = el.value.trim()
    })
  }

  function renderIngestReview() {
    // The form's kind: from the resolver's reading of this folder, then the reviewer's switch.
    // Held on ingest (not ingest.form) so Rescan, which rebuilds the form, keeps it.
    if (ingest.kindFolder !== ingest.folderPath) {
      ingest.mb = null   // a MusicBrainz pick belongs to one folder
      ingest.kind = ingest.scan.resolved?.kind === 'studio' ? 'studio' : 'live'   // the scan carries kind on resolved, not at top level
      ingest.kindFolder = ingest.folderPath
    }
    const studio = ingest.kind === 'studio'
    const hid = studio ? ' style="display:none"' : ''
    const tags = ingest.scan.suggestions.from_tags
    const info = ingest.scan.suggestions.from_info_file

    // Build the track list on first load; edits survive a back-nav. Same
    // resolve_tracks() (app/utils/resolve.py) the auto-confirm and bulk
    // paths ingest from, so the wizard shows exactly what an unattended
    // ingest would have produced.
    if (!ingest.tracks.length) {
      // Resolver-built (spec chunk 6) -- title-cased, flagged, scan-index
      // numbered. The human can still edit this list before it posts.
      ingest.tracks = ingest.scan?.resolved?.tracks || []
    }

    // Pre-fill metadata form on first load
    const f = ingest.form
    if (!f._filled) {
      // Resolver-built (Ingest Field Resolver spec v1, chunk 6): every field
      // below reads scan.resolved.<field>.value -- tags-vs-info precedence,
      // date precision, and the tag/info-file merge (including its own
      // ad hoc concert_date parsing) all used to be re-derived here in JS;
      // that logic is deleted and this just reads the resolver's answer,
      // same one the wizard's prefill, batch_scan and auto_confirm all use.
      const r = ingest.scan.resolved || {}
      const rv = key => r[key]?.value ?? null
      const date = r.date?.value || {}
      f.artist_name     = rv('artist') || ''
      f.start_year      = date.year  || ''
      f.start_month     = date.month || ''
      f.start_day       = date.day   || ''
      f.venue_name      = rv('venue') || ''
      f.venue_id        = null
      f.city            = rv('city')    || ''
      f.state           = rv('state')   || ''
      f.country         = rv('country') || ''
      f.source          = rv('source') || ''
      // Folder-name only (spec section 5) -- resolved.source_tag/shnid carry
      // these even though neither tags nor the info file's text itself
      // produces them; never written without the form.
      f.source_tag      = rv('source_tag') || ''
      f.etree_shnid     = r.shnid?.value != null ? String(r.shnid.value) : ''
      f.quality         = ''
      f.lineage         = rv('lineage') || ''
      f.notes           = ''
      f.end_year        = ''
      f.end_month       = ''
      f.end_day         = ''
      f.event_name      = ''
      f.event_id        = null
      f.stage           = rv('stage') || ''
      // Genre is never inferred from tags or the info file. It is a controlled
      // vocabulary keyed to the ACT, so the only honest sources are the act's
      // existing row (filled in by initAddArtistMembers) or a human pick.
      f.genre_id        = null
      f.genre_name      = ''
      f.genre_source    = null
      f.is_official     = false
      f.album_title     = ingest.scan.resolved?.album?.value || ''
      f._filled         = true
      // Queue-level values land BEFORE the snapshot below, deliberately.
      // They are not something the reviewer typed on this form, so a Rescan
      // must not treat them as unsaved work and raise a confirm dialog over
      // them — and it does not need to, because Rescan clears `ingest.form`
      // entirely and comes straight back through here, so they are re-applied
      // on the way out. Fix the info file, rescan, and the batch's Venue is
      // still in the box.
      _applyQueueValuesToForm(f)
      // What the inference produced, frozen. Rescan diffs the live form
      // against this to decide whether it has anything to warn about — the
      // common case (fix the info file, rescan immediately) has nothing typed
      // and should not cost a dialog.
      f._inferred = _ingestFormSnapshot(f)
    }

    const studioHealth = studio ? _studioCompleteness(f.artist_name, f.start_year, ingest.tracks) : null

    // Right panel: FLAC Tags — container fields + per-track sub-section
    const tagKeys = ['artist', 'concert_date', 'venue', 'location', 'source', 'lineage']
    const rawTagRows = tagKeys.map(k => `
      <div class="rev-raw-row">
        <span class="rev-raw-key">${k}</span>
        <span class="rev-raw-val ${tags[k] ? '' : 'rev-raw-empty'}">${tags[k] ? esc(tags[k]) : '—'}</span>
      </div>`).join('')

    const tagTracks = tags.tracks || []
    const rawTrackRows = tagTracks.length ? tagTracks.map(t => `
      <div class="rev-raw-row rev-raw-track-row">
        <span class="rev-raw-key">${String(t.track_number || t.index).padStart(2,'0')}</span>
        <span class="rev-raw-val ${t.title ? '' : 'rev-raw-empty'}">${t.title ? esc(t.title) : '—'}</span>
      </div>`).join('') : ''

    const rawTracksSection = rawTrackRows ? `
      <div class="rev-raw-tracks-header">
        <span>Tracks (${tagTracks.length})</span>
        <button class="rev-panel-toggle" data-panel="panel-flac-tracks">${chevronIcon()}</button>
      </div>
      <div id="panel-flac-tracks" style="display:none">${rawTrackRows}</div>` : ''

    // Right panel: parsed info file — arrows on LEFT of label
    //
    // Reads the resolver's own candidates (Ingest Field Resolver spec v1,
    // chunk 6) rather than the raw info-file suggestions: resolved.<field>
    // .candidates.info is the SAME value build_scan_payload's resolve() call
    // already filtered (an implausible venue line -- a clock time, "Two
    // Shows: Show 1..." -- is rejected there and never becomes a candidate
    // at all), so this panel cannot offer an apply arrow onto a value the
    // resolver itself would not have trusted.
    //
    // resolved.<field>.conflict (true when tags and the info file disagree)
    // is available here but not rendered -- no existing style in this file
    // marks a field as uncertain/conflicting (grepped for "uncertain": only
    // hits are unrelated doc comments), and this task does not add new UI.
    const resolvedFields = ingest.scan.resolved || {}
    const infoCand = key => resolvedFields[key]?.candidates?.info ?? null
    const infoDateCand = resolvedFields.date?.candidates?.info || null
    const infoDate = infoDateCand
      ? (infoDateCand.month
          ? `${infoDateCand.year}-${String(infoDateCand.month).padStart(2,'0')}` +
            (infoDateCand.day ? `-${String(infoDateCand.day).padStart(2,'0')}` : '')
          : String(infoDateCand.year))
      : null

    const parsedFields = [
      { label: 'Artist', val: infoCand('artist'),   action: 'apply-artist' },
      { label: 'Date',   val: infoDate,                       action: 'apply-date'   },
      { label: 'Venue',  val: infoCand('venue'),    action: 'apply-venue'  },
      { label: 'City',   val: infoCand('city'),     action: 'apply-city'   },
      { label: 'State',  val: infoCand('state'),              action: 'apply-state'  },
      { label: 'Country',val: infoCand('country'), action: 'apply-country'},
    ].filter(f => f.val)

    const parsedTrackCount = info.tracks?.length || 0

    // Arrow button is now LEFT of the label
    const parsedRows = parsedFields.map(f => `
      <div class="rev-parsed-row">
        <button class="btn-parsed-apply" data-action="${f.action}" data-val="${esc(f.val)}"
                data-year="${infoDateCand?.year||''}" data-month="${infoDateCand?.month||''}" data-day="${infoDateCand?.day||''}">${icon('arrow-left')}</button>
        <span class="rev-parsed-key">${f.label}</span>
        <span class="rev-parsed-val">${esc(f.val)}</span>
      </div>`).join('')

    // Tracks row: apply button + expandable track list
    const parsedTrackItems = (info.tracks || []).map(t =>
      `<div class="rev-parsed-track-item">${String(t.number).padStart(2,'0')}. ${esc(t.title)}</div>`
    ).join('')

    const parsedTracksRow = parsedTrackCount ? `
      <div class="rev-parsed-row">
        <button class="btn-parsed-apply" data-action="apply-tracks">${icon('arrow-left')}</button>
        <span class="rev-parsed-key">Tracks</span>
        <span class="rev-parsed-val">
          ${parsedTrackCount} found
          <button class="btn-parsed-tracks-toggle" id="btn-parsed-tracks-toggle">${chevronIcon('caret-ic--up')}</button>
        </span>
      </div>
      <div class="rev-parsed-tracklist" id="rev-parsed-tracklist">
        ${parsedTrackItems}
      </div>` : ''

    const parsedPanelBody = (parsedRows || parsedTracksRow)
      ? `<div class="rev-parsed-section">${parsedRows}${parsedTracksRow}</div>`
      : `<div class="rev-raw-empty" style="padding:8px 16px 12px">No data parsed</div>`

    // Right panel: info file text (selectable) + switcher when multiple candidates
    const textCandidates = ingest.scan.text_file_candidates || []
    const textSwitcher = textCandidates.length > 1
      ? `<div class="info-file-switcher">
          <span class="info-file-switcher-label">Multiple:</span>
          ${textCandidates.map((tf, i) => `
            <button class="info-file-btn ${i === (ingest._activeTextIdx || 0) ? 'active' : ''}"
                    data-idx="${i}">${esc(tf.filename)}</button>`).join('')}
         </div>`
      : ''
    // Editable — the archivist can fix up the parsed text, or type one in from
    // scratch when the folder had no info file. Edits flow straight into
    // ingest.scan.info_file_content (the value sent on Confirm); no re-parse.
    // "Save to file" writes it to disk independent of Confirm, so a re-run of
    // AI Assist picks up the correction — Confirm still sends whatever's in
    // memory either way, saving to disk is just for round-tripping with AI.
    // Save to File lives in the panel's shared action row now (2026-08-28),
    // not at the bottom of this pane. Same rule View Recording adopted on
    // 08-21: every pane's action in one place, shown by data-for.
    // READ-ONLY until asked otherwise, matching View Recording (Ryan,
    // 2026-08-28). It was a live textarea here and a locked one there, which
    // is the divergence: a stray click plus a keystroke silently rewrote the
    // taper's own words, and on THIS page that is worse than on View
    // Recording, because the text goes straight into what Confirm sends
    // rather than waiting for a save.
    //
    // The one exception is an empty folder. With no info file there is nothing
    // to protect and typing one in from scratch is the documented purpose of
    // the box, so it opens unlocked and the Edit button already says Cancel.
    const infoLocked = !!(ingest.scan.info_file_content || '').trim()
    const hasResolver = !!ingest.scan.resolved   // the Resolver tab is the Lomax table
    const infoText = `<textarea class="rev-info-text rev-info-edit${infoLocked ? ' rev-info-text--locked' : ''}" id="rev-info-edit"
      ${infoLocked ? 'readonly' : ''}
      placeholder="No info file found. Paste or type one in.">${esc(ingest.scan.info_file_content || '')}</textarea>`

    // Track count mismatch detection
    const audioCount     = ingest.scan.audio_file_count
    const infoTrackCount = info.tracks?.length || 0
    const hasMismatch    = infoTrackCount > 0 && audioCount !== infoTrackCount
    const mismatchBanner = hasMismatch ? `
      <div class="track-mismatch-warn">
        ${audioCount} audio file${audioCount !== 1 ? 's' : ''} on disk · ${infoTrackCount} track${infoTrackCount !== 1 ? 's' : ''} in info file. Use playback to verify
      </div>` : ''

    // Track table rows — play preview, title, and the same flag-chip layout
    // as View Recording. Note/Songwriter are click-to-edit cells right in the
    // table (staged into ingest.tracks in memory — no API call; Confirm sends
    // it all at once). Right-click a row for Flags only (openTrackMenu with
    // flagsOnly — Ryan, 2026-07-15: Note/Songwriter moved out of that popup
    // now that they're editable inline).
    // A track's chip row: the FIRST chip (official badge, then flags in
    // order) stays under the title as before; if there's more than one, the
    // rest get their own full-width row right underneath, laid out
    // horizontally — they used to all stack vertically inside the narrow
    // title-cell and push the title text up (Ryan, 2026-07-15).
    function _trackChipExpandRowHtml(i, chips) {
      // chips[0] is already rendered separately under the title (see
      // trackRows below / refreshIngestTrackRow) — this row is only for the
      // REST. Bug fixed 2026-07-23 (Ryan: "Banter" showing twice on tracks
      // titled e.g. "Banter & Tuning"): this used to join the FULL chips
      // array here, so the first chip was shown once under the title AND
      // again in this row every time a track had 2+ chips. Not a data bug —
      // t.flags itself was always clean (detect_track_flags in
      // app/utils/ingest.py builds off a Set, which can't hold a duplicate
      // key) — purely a rendering double-count.
      return `<tr class="track-review-chiprow" data-idx="${i}">
          <td colspan="7"><div class="track-chip-expand-row">${chips.slice(1).join('')}</div></td>
        </tr>`
    }

    // Official-release mark for the dedicated column (Ryan, 2026-08-09) —
    // separate from trackChipsArray's badge, which Add Recording hides
    // (hideOfficial) to keep it out of the title cell/chip row entirely.
    function _officialBadgeHtml(t) {
      return t.is_official
        ? `<span class="track-official-badge" title="Officially released">©</span>` : ''
    }

    const trackRows = ingest.tracks.map((t, i) => {
      const chips = trackChipsArray(t, { hideOfficial: true })
      const expandRow = chips.length > 1 ? _trackChipExpandRowHtml(i, chips) : ''
      return `
        <tr class="track-review-row" data-idx="${i}" title="Right-click for flags">
          <td class="num">${t.track_number}</td>
          <td class="play-cell">
            <button class="btn-preview-track" data-filename="${esc(t.filename || '')}" title="${esc(t.filename || 'no file')}">${icon('play')}</button>
          </td>
          <td class="title-cell">
            <input type="text" class="t-title" data-idx="${i}" value="${esc(t.title)}" />
            <div class="track-chip-row" id="t-chips-${i}">${chips[0] || ''}</div>
          </td>
          <td class="note-cell truncate pp-editable${t.notes ? '' : ' pp-empty'}" id="t-note-${i}" title="${esc(t.notes || 'Click to add a note')}">${esc(t.notes || '—')}</td>
          <td class="sw-cell truncate pp-editable${t.songwriter ? '' : ' pp-empty'}" id="t-sw-${i}" title="${esc(t.songwriter || 'Click to add a songwriter')}">${esc(t.songwriter || '—')}</td>
          <td class="dur">${fmtDur(t.duration)}</td>
          <td class="official-cell" id="t-off-${i}">${_officialBadgeHtml(t)}</td>
        </tr>${expandRow}`
    }).join('')

    setMainHTML(`
      <div class="ingest-review-outer">
      <div class="ingest-review-topbar">
        <a href="#" id="ingest-back-link" class="ingest-back-link">${
          ingest.returnTo ? 'Back to Add Recordings' : 'Back'}</a>
        <div class="ingest-topbar-line">
          <h2 class="ingest-topbar-title">Add Recording: <span class="rev-header-folder">${esc(ingest.folderPath?.split('/').pop() || '')}</span></h2>
          <!-- Metadata Completeness — a labelled readout, not a pill (Ryan,
               2026-09-01). A pill is a badge: a small, coloured, rounded thing
               the eye reads as a STATUS on the object beside it, which is why
               this one kept reading as a property of the folder name it sat
               next to. This is a measurement of the form below, so it is
               written as one — its name spelled out in full, the value beside
               it, and the colour carried by the value alone rather than by a
               tinted capsule. Same three bands, same id reScore() writes to. -->
          <div class="meta-readout" id="iq-score"
               title="How much of the metadata this form has filled in">
            <span class="meta-readout-label">Metadata Completeness</span>
            <span class="meta-readout-value meta-readout-value--${studioHealth ? studioHealth.band : (ingest.scan.health?.band || 'yellow')}"
                  >${esc(studioHealth ? studioHealth.rating : _metaRating(ingest.scan.health))}</span>
          </div>
          <!-- Rescan (Ryan, 2026-09-01). The Details panel lets a reviewer fix
               the info file in place; until now nothing re-read it, so a
               corrected tracklist or date sat there doing nothing and the only
               way to act on it was to leave the page and come back. -->
          <button class="btn btn-ghost btn-sm ingest-rescan-btn" id="btn-rescan"
                  title="Re-run the inference over the info file, including any edits you have made to it">
            ${icon('rotate-cw', 'lq-browse-ic')} Rescan</button>
          <button class="btn btn-ghost btn-sm ingest-rescan-btn" id="btn-classify-kind">${
            studio ? 'Classify as Live Recording' : 'Classify as Album'}</button>
          <!-- Re-apply the queue's applied values (2026-09-02, precedence
               reversed 2026-09-03). They are already written over the
               inference when this form opens, so in the ordinary case this has
               nothing to do. It is here for the cases where it does: you
               pressed Apply Values AFTER opening this show, or you Rescanned
               and want the blanket values back over the freshly parsed ones.
               It overwrites, like the prefill — the point of a blanket value
               is that it wins.
               Shown only when values are actually STAGED. Typed-but-unapplied
               values do nothing anywhere, so offering to apply them here would
               be offering a no-op. -->
          ${_queueValuesCount() ? `<button class="btn btn-ghost btn-sm ingest-rescan-btn" id="btn-apply-queue"
                  title="Overwrite this form's Artist, Venue, Event and the rest with the values applied to the whole queue">
            ${icon('plus', 'lq-browse-ic')} Apply Queue Values</button>` : ''}
          ${panelToggleHtml('ingest-slide-rail')}
        </div>
      </div>
      <div class="ingest-review-shell">

        <!-- Left: metadata form + track list -->
        <div class="ingest-review-form">
          <div class="ingest-review-form-body">

            <!-- Artist + Genre on one row (Ryan, 2026-09-01).
                 The parenthetical "(the act, from the FLAC ARTIST tag)" is
                 gone: it put a second font treatment inside a 10px label to
                 explain a word the app uses everywhere, and where the value
                 came from is what the Details panel's FLAC Tags pane is for.
                 Genre belongs beside the act because it IS a property of the
                 act — see the genre picker's wiring below. -->
            <div class="ingest-field-grid ingest-row-act">
              <div class="ingest-field">
                <label for="f-artist">Artist</label>
                <div class="artist-picker-wrap">
                  <input type="text" id="f-artist" class="${paulaCls('artist')}" value="${esc(f.artist_name)}" autocomplete="off" placeholder="Search or type the act…" />
                  <div class="artist-dropdown" id="f-artist-dropdown" style="display:none"></div>
                </div>
              </div>
              <div class="ingest-field">
                <label for="f-genre">Genre</label>
                <div class="genre-picker-wrap">
                  <input type="text" id="f-genre" value="${esc(f.genre_name || f._genreText || '')}" autocomplete="off" placeholder="Search or add a genre…" />
                  <input type="hidden" id="f-genre-id" value="${esc(String(f.genre_id || ''))}" />
                  <div class="artist-dropdown" id="f-genre-dropdown" style="display:none"></div>
                </div>
              </div>
            </div>

            <!-- Members/Guests two-row personnel widget — filled in by
                 createMembersWidget().renderChips(), see app.js. -->
            <div class="ingest-field" style="margin-top:6px">
              <div class="members-field" id="f-members-field"></div>
            </div>

            <!-- Date, Venue and Event on ONE row (Ryan, 2026-08-28, from the
                 redesign sheet). They were three separate rows of part-width
                 controls, which is what made the form read as a column of
                 boxes rather than a record. End date keeps its own disclosure:
                 a multi-day show is the rare case and should not cost three
                 permanent inputs. -->
            <div class="ingest-field-grid ingest-row-ident${studio ? ' ingest-row-album' : ''}" style="margin-top:6px">
              ${studio ? `<div class="ingest-field"><label for="f-album-title">Album Title</label><input type="text" id="f-album-title" value="${esc(f.album_title || '')}" autocomplete="off" /></div>` : ''}
              <div class="ingest-field"><label>Year</label><input type="number" id="f-year" class="${paulaCls('date')}" value="${esc(f.start_year)}" min="1900" max="2099" /></div>
              <div class="ingest-field"${hid}><label>Month</label><input type="number" id="f-month" class="${paulaCls('date')}" value="${esc(f.start_month)}" min="1" max="12" /></div>
              <div class="ingest-field"${hid}><label>Day</label><input type="number" id="f-day" class="${paulaCls('date')}" value="${esc(f.start_day)}" min="1" max="31" /></div>
              <div class="ingest-field"${hid}>
                <label>Venue</label>
                <div class="venue-picker-wrap">
                  <input type="text" id="f-venue-name" class="${paulaCls('venue_name')}" value="${esc(f.venue_name)}" autocomplete="off" placeholder="Search or type venue name…" />
                  <input type="hidden" id="f-venue-id" value="${esc(String(f.venue_id || ''))}" />
                  <div class="venue-dropdown" id="f-venue-dropdown" style="display:none"></div>
                </div>
              </div>
              <div class="ingest-field"${hid}>
                <label>Festival / Event</label>
                <div class="event-picker-wrap">
                  <input type="text" id="f-event-name" value="${esc(f.event_name || '')}" autocomplete="off" />
                  <input type="hidden" id="f-event-id" value="${esc(String(f.event_id || ''))}" />
                  <div class="event-dropdown" id="f-event-dropdown" style="display:none"></div>
                </div>
              </div>
              <div class="ingest-field"${hid}>
                <label>Stage</label>
                <input type="text" id="f-stage" value="${esc(f.stage || '')}" autocomplete="off" />
              </div>
            </div>
            <div id="end-date-toggle-row" style="margin-top:3px${studio ? '; display:none' : ''}">
              <a class="field-toggle-link" id="btn-toggle-end-date" href="#">+ End Date</a>
            </div>
            <div class="ingest-field-grid date-grid" id="end-date-row" style="margin-top:5px; display:none">
              <div class="ingest-field"><label>End Year</label><input type="number" id="f-end-year" value="${esc(f.end_year)}" min="1900" max="2099" /></div>
              <div class="ingest-field"><label>Month</label><input type="number" id="f-end-month" value="${esc(f.end_month)}" min="1" max="12" /></div>
              <div class="ingest-field"><label>Day</label><input type="number" id="f-end-day" value="${esc(f.end_day)}" min="1" max="31" /></div>
            </div>

            <!-- Non-blocking: already-in-library warning for this artist+date
                 (checked once both are known — see wireDupCheck). Multiple
                 recordings per show are legitimate, so this never blocks Confirm. -->
            <div class="dup-warn" id="dup-warn" style="display:none">
              <div class="dup-warn-title">Already in your library</div>
              <div class="dup-warn-body" id="dup-warn-body"></div>
            </div>

            <!-- City / State / Country — state is narrow -->
            <div class="ingest-field-grid" style="grid-template-columns:minmax(0,1fr) 64px minmax(0,1fr); gap:10px; margin-top:6px${studio ? '; display:none' : ''}" id="f-location-row">
              <div class="ingest-field"><label>City</label><input type="text" id="f-city" class="${paulaCls('city')}" value="${esc(f.city)}" /></div>
              <div class="ingest-field"><label>State</label><input type="text" id="f-state" class="${paulaCls('state')}" value="${esc(f.state)}" maxlength="6" /></div>
              <div class="ingest-field"><label>Country</label><input type="text" id="f-country" class="${paulaCls('country')}" value="${esc(f.country)}" /></div>
            </div>

            <!-- Quality, Source, Lineage — that order (Ryan, 2026-08-28).
                 Source carries no placeholder: "SBD, AUD, MTX…" read as a
                 value at a glance in a form whose other boxes are pre-filled,
                 and the field is not free text anyway. -->
            <div class="ingest-field-grid ingest-row-src" style="margin-top:6px${studio ? '; display:none' : ''}">
              <div class="ingest-field">
                <label>Quality</label>
                <input type="text" id="f-quality" value="${esc(f.quality)}" />
              </div>
              <div class="ingest-field">
                <label>Source</label>
                <input type="text" id="f-source" value="${esc(f.source)}" />
              </div>
              <div class="ingest-field">
                <label>Lineage</label>
                <input type="text" id="f-lineage" value="${esc(f.lineage)}" />
              </div>
            </div>

            <!-- Source tag, shnid -- spec section 5: folder-name detection
                 only, never written without this form. Same labels as the
                 View Recording Source block. -->
            <div class="ingest-field-grid ingest-row-src" style="margin-top:6px${studio ? '; display:none' : ''}">
              <div class="ingest-field">
                <label>Source Tag</label>
                <input type="text" id="f-source-tag" value="${esc(f.source_tag)}" />
              </div>
              <div class="ingest-field">
                <label>SHNID</label>
                <input type="text" id="f-shnid" class="mono" value="${esc(f.etree_shnid)}" />
              </div>
            </div>

            <!-- Track table -->
            <!-- Preview player moved into this header row (Ryan, 2026-08-08),
                 right-aligned opposite the "Tracks (N)" title — same idea as
                 the Bulk Update preview layout, rather than pinned to the
                 bottom action bar where it competed with Add & Return/View. -->
            <div class="rev-tracks-header" style="margin-top:16px; padding-top:12px; border-top:1px solid var(--bd-1)">
              <div class="rev-section-title" style="margin-bottom:0">
                Tracks <span style="font-weight:400; text-transform:none; letter-spacing:0; color:var(--t2)">(${ingest.tracks.length})</span>
                <span style="font-weight:400; text-transform:none; letter-spacing:0; color:var(--t3); font-size:10px">right-click a track to add flags</span>
              </div>
              <!-- Preview transport (Ryan, 2026-08-28: option P1). Was
                   <audio controls>, the one control in the app the OS drew
                   itself, at its own weight, radius and palette — and on the
                   packaged WKWebView build it read as a piece of Safari
                   sitting inside Trellis. This is the player bar's own
                   vocabulary at two thirds scale: same accent circle, same
                   Lucide transport glyphs, same input[type=range].progress-bar
                   with its accent gradient fill, same tabular-nums times.
                   Prev/Next step through the track table, which the native
                   widget could not do at all. The <audio> element stays, now
                   with no controls attribute: it is the engine, not the UI. -->
              <div id="ingest-audio-bar" class="ingest-xport">
                <button class="ingest-xport-btn" id="ixp-prev" type="button" title="Previous track">${icon('skip-back')}</button>
                <button class="ingest-xport-btn ingest-xport-play" id="ixp-play" type="button" title="Play / pause">${icon('play')}</button>
                <button class="ingest-xport-btn" id="ixp-next" type="button" title="Next track">${icon('skip-forward')}</button>
                <span class="ingest-xport-name" id="ixp-name">—</span>
                <span class="ingest-xport-time" id="ixp-cur">0:00</span>
                <input type="range" class="progress-bar" id="ixp-seek" min="0" max="100" value="0" step="0.1"
                       aria-label="Seek within the preview track" />
                <span class="ingest-xport-time" id="ixp-dur">0:00</span>
                <audio id="ingest-preview-audio" preload="metadata"></audio>
              </div>
            </div>
            ${studio ? `<div class="pp-mb-linked" id="ingest-mb-facts">${ingestMbFactsHtml()}</div>` : ''}
            <div style="overflow:auto; margin-bottom:4px">
              <table class="track-review-table">
                <thead>
                  <tr>
                    <th style="width:20px; text-align:center">#</th>
                    <th style="width:28px"></th>
                    <th style="width:36%">Title</th>
                    <th style="width:22%">Notes</th>
                    <th style="width:18%">Songwriter</th>
                    <th style="width:44px">Time</th>
                    <!-- Official-release mark — otherwise-blank column, far
                         right (Ryan, 2026-08-09). Reinstated the © badge but
                         out of the title cell, where it risked wrapping the
                         row; a dedicated fixed-width column can't. -->
                    <th style="width:20px"></th>
                  </tr>
                </thead>
                <tbody>${trackRows || '<tr><td colspan="7" style="color:var(--t2);padding:12px">No tracks found</td></tr>'}</tbody>
              </table>
            </div>

            <div class="ingest-field" style="margin-top:12px">
              <label>Notes</label>
              <textarea id="f-notes" style="min-height:80px">${esc(f.notes)}</textarea>
            </div>

            <label style="display:flex; align-items:center; gap:8px; color:var(--t3); font-size:11px; margin-top:8px; cursor:pointer">
              <input type="checkbox" id="f-is-official" ${f.is_official ? 'checked' : ''} />
              <span>Official Release</span>
              <span style="color:var(--t3); font-style:italic">marks the recording and all tracks as officially released</span>
            </label>

          </div>
          <div class="ingest-actions">
            <!-- Audio preview player moved up into the Tracks header row
                 (Ryan, 2026-08-08) — this bar is action-only now: both exits,
                 right-aligned. Two exits because a reviewer working a queue
                 and a reviewer adding one show want opposite things.
                 "Add & Return" is primary: mid-queue is the common case, and
                 it goes straight back to the ingest list with this row marked
                 done. "Add & View" (farthest right) opens the finished record
                 instead — a lighter fill than the primary button so the pair
                 doesn't read as primary+disabled-looking-ghost. -->
            <div class="ingest-actions-left" id="ingest-fh-strip"></div>
            <div class="ingest-actions-right">
              <button class="btn btn-ingest-secondary" id="btn-confirm"
                      data-after="return" title="Add to library and return to the list">Add &amp; Return ↵</button>
              <button class="btn btn-ghost" id="btn-confirm-view"
                      data-after="view" title="Add to library and open the finished record">Add &amp; View →</button>
            </div>
          </div>
          <!-- Reserved before it is needed (Ryan, 2026-10-02): the bar used to be
               inserted on click and pushed the page up. Hidden, not absent. -->
          <div id="confirm-progress" class="confirm-progress" style="visibility:hidden">
            <div class="confirm-progress-bar"><div class="confirm-progress-fill" id="confirm-progress-fill"></div></div>
            <div class="confirm-progress-label" id="confirm-progress-label">Preparing…</div>
          </div>
          <div id="review-submit-error" class="review-submit-error" style="display:none"></div>
        </div>

        <!-- Resize handle -->
        <div class="rev-resize-handle" id="rev-divider"></div>

        <!-- Right: the Details panel — the SAME component View Recording uses
             (Ryan, 2026-08-28: "ship A"). Horizontal tab strip, no per-pane
             headers repeating the tab above them, one .pane-acts row shown by
             data-for, and a permanent rail that toggles the whole panel.

             The rail sits AFTER .slide-panel-main, against the window's right
             edge, and View Recording was moved to match. Leading, it rode the
             panel's inner edge and travelled the panel's full width on every
             click — a toggle that jumps out from under the cursor.

             The old quality bar is gone: it was 34px of chrome for one rating
             and one button. The rating is a chip in the topbar, the button is
             a pane action. -->
        <div class="ingest-review-raw slide-panel--htabs slide-panel--index open" id="ingest-slide-panel">
          <div class="slide-panel-main">
            <!-- Same three info-file controls, in the same order, as View
                 Recording (Ryan, 2026-08-28). Save leads and is suppressed
                 while the file is locked; the status sits between them. -->
            <div class="pane-acts" id="ingest-pane-acts">
              <span class="pane-title" id="ingest-pane-title"></span>
              <span class="pane-act-status" id="info-file-save-status" data-for="isp-info"></span>
              <button class="pane-act act-suppressed" id="btn-save-info-file" data-for="isp-info" hidden disabled>Save to File</button>
              <button class="pane-act" id="btn-ingest-info-edit" data-for="isp-info">Edit File</button>
            </div>
            <div class="slide-panel-body" id="ingest-panes">
              <div class="slide-pane active" id="isp-info">
                ${textSwitcher}
                <div class="slide-pane-scroll"><div class="rev-raw-section">${infoText}</div></div>
              </div>
              <!-- Quality: the triage pass's numbers, fetched lazily. Nothing
                   here has been ingested, so there is no permanent score yet —
                   see loadIngestQualityPane for what happens when the folder
                   was never analyzed either. -->
              <div class="slide-pane" id="isp-quality">
                <div class="slide-pane-scroll" id="isp-quality-body">
                  <div class="info-panel-empty">Loading…</div>
                </div>
              </div>
              <div class="slide-pane" id="isp-filetags">
                <div class="slide-pane-scroll"><pre class="filetags-json">${esc(scanFileTagsJson())}</pre></div>
              </div>
              <div class="slide-pane" id="isp-checksums">
                <div class="slide-pane-scroll">${buildChecksumsPreviewHtml(ingest.scan.fingerprints)}</div>
              </div>
              ${hasResolver && !studio ? `<div class="slide-pane" id="isp-resolver">
                <div class="slide-pane-scroll lx-col"><div class="lx-res" id="lx-res-root"></div></div>
              </div>` : ''}
              ${studio ? `<div class="slide-pane" id="isp-mb">
                <div class="slide-pane-scroll"><div class="rev-raw-section" id="ingest-mb-root"></div></div>
              </div>` : ''}
              <!-- Permanent, so the tab always advertises Lomax. The chat and
                   the Resolver tab share one controller (see lomaxController). -->
              <div class="slide-pane" id="isp-ai">
                <div class="slide-pane-scroll lx-scroll"></div>
                <div class="lx-bar-slot"></div>
              </div>
            </div>
          </div>
          <nav class="slide-index" id="ingest-tab-rail" aria-label="Details">
            ${detailsTabsHtml('data-ipane', 'isp-', { resolver: hasResolver && !studio, research: true, staged: false, info: !studio, mb: studio })}
          </nav>
        </div>

      </div>
      </div>`)

    // Health score — recompute on any committed field change, not just AI
    // Assist actions (Ryan, 2026-07-16: the badge must never sit stale
    // relative to what's actually on screen — this is what let a scan
    // showing "9 of 23 tracks have a title" still show a 100/"Looks
    // complete" badge). Delegated on the review container itself, which is
    // torn down by the next setMainHTML() call, so this doesn't accumulate.
    // `focusout` (unlike `blur`) bubbles, so one listener covers every field.
    mainContent.querySelector('.ingest-review-outer')?.addEventListener('focusout', e => {
      if (e.target.matches('input, textarea, select')) {
        reScore()
        if (ingest.lxSyncRes) ingest.lxSyncRes()
      }
    })
    reScore()   // also recompute right away, against whatever track list just rendered

    // Parsed info file — apply buttons
    ;(function () {
      mainContent.querySelectorAll('.btn-parsed-apply').forEach(btn => {
        btn.addEventListener('click', e => {
          e.preventDefault()
          const action = btn.dataset.action
          const val    = btn.dataset.val || ''

          if (action === 'apply-artist') {
            document.getElementById('f-artist').value = val

          } else if (action === 'apply-date') {
            document.getElementById('f-year').value  = btn.dataset.year  || ''
            document.getElementById('f-month').value = btn.dataset.month || ''
            document.getElementById('f-day').value   = btn.dataset.day   || ''

          } else if (action === 'apply-venue') {
            document.getElementById('f-venue-name').value = val
            document.getElementById('f-venue-id').value   = ''  // clear any locked venue

          } else if (action === 'apply-city') {
            document.getElementById('f-city').value = val

          } else if (action === 'apply-state') {
            document.getElementById('f-state').value = val

          } else if (action === 'apply-country') {
            document.getElementById('f-country').value = val

          } else if (action === 'apply-tracks') {
            const titles  = (ingest.scan.resolved?.tracks || []).map(t => t.info_title)
            const inputs  = [...mainContent.querySelectorAll('.t-title')]
            inputs.forEach((inp, i) => { if (titles[i] != null) inp.value = titles[i] })
            inputs.forEach((inp, i) => { if (titles[i] != null) ingest.tracks[i].title = titles[i] })
          }

          // Quick flash to confirm
          btn.innerHTML = icon('check')
          setTimeout(() => { btn.innerHTML = icon('arrow-left') }, 800)

          // These buttons set field values programmatically (no real focus
          // change), so the usual focusout-triggered reScore() below never
          // fires for them — recompute explicitly (Ryan, 2026-07-16: the
          // health score must never sit stale against what's on screen).
          reScore()
        })
      })
    })()

    // Ingest track preview — play/pause individual audio files. Shown by
    // default (previewing the first track, paused) rather than only
    // appearing after a play click (Ryan, 2026-07-15).
    ;(function () {
      const audioEl  = document.getElementById('ingest-preview-audio')
      if (!audioEl) return

      let activeBtn = null
      const previewBtns = [...mainContent.querySelectorAll('.btn-preview-track')]
      const elPlay = document.getElementById('ixp-play')
      const elName = document.getElementById('ixp-name')
      const elCur  = document.getElementById('ixp-cur')
      const elDur  = document.getElementById('ixp-dur')
      const elSeek = document.getElementById('ixp-seek')

      const mmss = v => (!isFinite(v) || v < 0) ? '0:00'
        : `${Math.floor(v / 60)}:${String(Math.floor(v % 60)).padStart(2, '0')}`

      /** One place decides what every control looks like, from the <audio>
       *  element's actual state — so the row button, the big play button and
       *  the scrubber can never disagree about whether sound is coming out.
       *
       *  timeupdate fires about four times a second, so the ICON writes are
       *  guarded on an actual state change. Repainting every row button on
       *  every tick meant 120 inline SVGs re-parsed per second on a 30-track
       *  folder: the buttons flickered, their hover transitions never
       *  completed, and the table janked while a preview played. The times and
       *  the scrubber are cheap text/attribute writes and repaint every tick,
       *  which is the point of them. */
      let _pBtn = null, _pPlaying = null
      function paintXport() {
        const playing = !audioEl.paused && !audioEl.ended
        if (_pBtn !== activeBtn || _pPlaying !== playing) {
          if (elPlay) elPlay.innerHTML = icon(playing ? 'pause' : 'play')
          // Only the button that just stopped being active, and the one that
          // is: every other row is already showing a plain play triangle.
          if (_pBtn && _pBtn !== activeBtn) _pBtn.innerHTML = icon('play')
          if (activeBtn) activeBtn.innerHTML = icon(playing ? 'square' : 'play')
          _pBtn = activeBtn
          _pPlaying = playing
        }
        const dur = audioEl.duration
        const pct = (isFinite(dur) && dur > 0) ? (audioEl.currentTime / dur) * 100 : 0
        if (elSeek) {
          elSeek.value = String(pct)
          // The fill is a CSS variable on the range, same mechanism the player
          // bar uses — see input[type=range].progress-bar in main.css.
          elSeek.style.setProperty('--pct', pct + '%')
        }
        if (elCur) elCur.textContent = mmss(audioEl.currentTime)
        if (elDur) elDur.textContent = isFinite(dur) ? mmss(dur) : '0:00'
      }

      function loadTrack(btn, filename, autoplay) {
        const url = `/api/stream/ingest-preview?folder=${encodeURIComponent(ingest.folderPath)}&file=${encodeURIComponent(filename)}`
        audioEl.src = url
        activeBtn = btn
        const row = btn.closest('tr')
        const title = row?.querySelector('.t-title')?.value || filename
        const num = row?.querySelector('.num')?.textContent?.trim()
        if (elName) {
          elName.textContent = num ? `${String(num).padStart(2, '0')} ${title}` : title
          elName.title = filename
        }
        mainContent.querySelectorAll('.track-review-row').forEach(r =>
          r.classList.toggle('track-review-row--playing', r === row))
        if (autoplay) audioEl.play().catch(() => {})
        paintXport()
      }

      /** The rows that actually have a file behind them.
       *
       *  resolve_tracks() (app/utils/resolve.py) always emits one track per
       *  audio file with a real filename now, but this guard stays: a track
       *  row missing one used to stop stepping dead at the first such row,
       *  and if it happened to be row 1 the transport never loaded anything
       *  at all and every control was inert. Skipping them is the only
       *  behaviour that makes sense -- there is nothing to play. */
      const playable = () => previewBtns.filter(b => b.dataset.filename)

      /** Step to the track `delta` playable rows away. Stops at both ends
       *  rather than wrapping: this is a checking tool, and running out of
       *  tracks is information. */
      function step(delta, autoplay) {
        const list = playable()
        if (!list.length) return
        const i = activeBtn ? list.indexOf(activeBtn) : -1
        const next = i < 0 ? list[0] : list[i + delta]
        if (!next) return
        loadTrack(next, next.dataset.filename, autoplay)
      }

      elPlay?.addEventListener('click', () => {
        if (!audioEl.src) { step(1, true); return }
        if (audioEl.paused) {
          if (typeof Player !== 'undefined' && Player.isPlaying()) Player.pause()
          audioEl.play().catch(() => {})
        } else {
          audioEl.pause()
        }
      })
      document.getElementById('ixp-prev')?.addEventListener('click', () => step(-1, !audioEl.paused))
      document.getElementById('ixp-next')?.addEventListener('click', () => step(1, !audioEl.paused))
      elSeek?.addEventListener('input', () => {
        const dur = audioEl.duration
        if (isFinite(dur) && dur > 0) audioEl.currentTime = (Number(elSeek.value) / 100) * dur
      })
      ;['play', 'pause', 'timeupdate', 'loadedmetadata', 'durationchange', 'emptied']
        .forEach(ev => audioEl.addEventListener(ev, paintXport))

      previewBtns.forEach(btn => {
        btn.addEventListener('click', e => {
          e.preventDefault()
          const filename = btn.dataset.filename
          if (!filename) return

          // The row already holding the transport toggles it, in BOTH
          // directions. Guarding on `!audioEl.paused` meant clicking the row
          // you had just paused reassigned audioEl.src and restarted it from
          // 0:00, while the big play button resumed correctly — the two
          // controls disagreeing about the same track, which is exactly what
          // paintXport's single-owner rule exists to prevent.
          // No icon bookkeeping here: paintXport reads the audio element.
          if (activeBtn === btn) {
            if (audioEl.paused) {
              if (typeof Player !== 'undefined' && Player.isPlaying()) Player.pause()
              audioEl.play().catch(() => {})
            } else {
              audioEl.pause()
            }
            return
          }

          // Pausing the main player bar so the two don't talk over each other
          // (Ryan, 2026-07-15). `window.Player` is always undefined — a
          // top-level const doesn't attach to window — so this guard was
          // dead and the two players could run at once (Ryan, 2026-08-27).
          if (typeof Player !== 'undefined' && Player.isPlaying()) Player.pause()

          loadTrack(btn, filename, true)
        })
      })

      // Default preview: first playable track, loaded but paused, so the
      // transport has something ready to go the moment the page opens.
      const firstBtn = playable()[0]
      if (firstBtn) loadTrack(firstBtn, firstBtn.dataset.filename, false)
      else paintXport()   // nothing to play: still paint the empty state

      audioEl.addEventListener('ended', paintXport)
    })()

    // Right-click a track row → same note/songwriter/flags/official popup as
    // View Recording (openTrackMenu), but staged: onChange just updates the
    // in-memory ingest.tracks entry (already mutated by openTrackMenu itself)
    // and repaints this row's chips/note/songwriter cells. Nothing is sent to
    // the server until Confirm.
    function refreshIngestTrackRow(i) {
      const t = ingest.tracks[i]
      if (!t) return
      const chips = trackChipsArray(t, { hideOfficial: true })
      const chipsEl = document.getElementById(`t-chips-${i}`)
      if (chipsEl) chipsEl.innerHTML = chips[0] || ''

      // The overflow row (2nd+ chips) doesn't have a stable id — it's a
      // sibling <tr> right after the main row. Add/update/remove it in place
      // rather than re-rendering the whole table on every flag toggle.
      const mainRow = mainContent.querySelector(`.track-review-row[data-idx="${i}"]`)
      const existingExpand = mainRow?.nextElementSibling?.classList.contains('track-review-chiprow')
        ? mainRow.nextElementSibling : null
      if (chips.length > 1) {
        if (existingExpand) {
          existingExpand.querySelector('.track-chip-expand-row').innerHTML = chips.slice(1).join('')
        } else if (mainRow) {
          mainRow.insertAdjacentHTML('afterend', _trackChipExpandRowHtml(i, chips))
        }
      } else if (existingExpand) {
        existingExpand.remove()
      }

      const noteEl = document.getElementById(`t-note-${i}`)
      if (noteEl) {
        noteEl.textContent = t.notes || '—'; noteEl.title = t.notes || 'Click to add a note'
        noteEl.classList.toggle('pp-empty', !t.notes)
      }
      const swEl = document.getElementById(`t-sw-${i}`)
      if (swEl) {
        swEl.textContent = t.songwriter || '—'; swEl.title = t.songwriter || 'Click to add a songwriter'
        swEl.classList.toggle('pp-empty', !t.songwriter)
      }
      // Official mark — its own column (Ryan, 2026-08-09), covers both the
      // master checkbox's cascade and a per-track right-click toggle, since
      // both funnel through this one function.
      const offEl = document.getElementById(`t-off-${i}`)
      if (offEl) offEl.innerHTML = _officialBadgeHtml(t)
    }
    mainContent.querySelectorAll('.track-review-row[data-idx]').forEach(row => {
      const idx = parseInt(row.dataset.idx)
      row.addEventListener('contextmenu', ev => {
        ev.preventDefault()
        const t = ingest.tracks[idx]
        if (!t) return
        openTrackMenu(t, ev.clientX, ev.clientY, {
          showOfficial: true,
          flagsOnly: true,
          onChange: () => refreshIngestTrackRow(idx),
        })
      })
    })

    // Note/Songwriter — click-to-edit directly in the table (Ryan, 2026-07-15:
    // moved out of the right-click menu, which is Flags-only here now). Staged
    // into ingest.tracks in memory, same as every other field on this form —
    // nothing hits the API until Confirm.
    ingest.tracks.forEach((t, i) => {
      const noteEl = document.getElementById(`t-note-${i}`)
      makeInlineEditable(noteEl, {
        placeholder: '—',
        get: () => ingest.tracks[i].notes || '',
        onSave: v => {
          v = v.trim() || null
          ingest.tracks[i].notes = v
          _ingestMarkDirty(`track.${ingest.tracks[i].track_number}.note`)
          if (noteEl) noteEl.title = v || 'Click to add a note'
        },
      })
      const swEl = document.getElementById(`t-sw-${i}`)
      makeInlineEditable(swEl, {
        placeholder: '—',
        get: () => ingest.tracks[i].songwriter || '',
        onSave: v => {
          v = v.trim() || null
          ingest.tracks[i].songwriter = v
          _ingestMarkDirty(`track.${ingest.tracks[i].track_number}.songwriter`)
          if (swEl) swEl.title = v || 'Click to add a songwriter'
        },
      })
    })

    // Right panel — collapsible panels
    ;(function () {
      mainContent.querySelectorAll('.rev-panel-toggle').forEach(btn => {
        btn.addEventListener('click', () => {
          const panel = document.getElementById(btn.dataset.panel)
          if (!panel) return
          const collapsed = panel.style.display === 'none'
          panel.style.display = collapsed ? '' : 'none'
          btn.querySelector('.caret-ic')?.classList.toggle('caret-ic--open', collapsed)
        })
      })
    })()

    // Text file switcher — swap which info file drives the parsed panel + raw text
    ;(function () {
      const candidates = ingest.scan.text_file_candidates || []
      if (candidates.length <= 1) return

      mainContent.querySelectorAll('.info-file-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const idx = parseInt(btn.dataset.idx)
          if (isNaN(idx) || !candidates[idx]) return

          // Switching candidates replaces the text and re-renders, so an
          // unsaved edit would vanish silently — while Cancel, two pixels
          // away, prompts. Same question, same words (2026-08-28).
          const editing = document.getElementById('rev-info-edit')
          if (editing && !editing.readOnly &&
              editing.value !== (ingest._infoBaseline || '') &&
              !confirm('Discard unsaved changes to the info file?')) return
          ingest._activeTextIdx = idx
          ingest._keepPane = 'isp-info'   // the re-render must not bounce to Resolver
          const chosen = candidates[idx]

          // Swap active scan data so re-renders pick it up
          ingest.scan.info_file_content = chosen.content
          ingest.scan.suggestions.from_info_file = chosen.suggestions

          // Re-render the whole review step to update parsed panel + raw text
          renderIngestReview()
        })
      })
    })()

    // ── Info File: locked → Edit File → Save to File ──────────────────────
    // The same three states as View Recording, in the same order, with the
    // same labels — see the block in renderRecordingView for the reasoning.
    //   locked            readonly + .rev-info-text--locked, Save suppressed
    //   editing, clean    editable, Save visible but disabled
    //   editing, dirty    Save enabled
    //
    // What differs is only what an edit MEANS. Here it goes straight into
    // ingest.scan.info_file_content, which is what Confirm sends, so an edit
    // takes effect whether or not it is ever written to disk. Save to File is
    // the separate act of putting it back on the collector's disk, so a re-run
    // of AI Assist reads the correction. `_infoBaseline` is therefore what
    // Cancel restores: the text as it stood when the pane was last locked or
    // last saved, not the last thing typed.
    ;(function () {
      const el      = document.getElementById('rev-info-edit')
      const editBtn = document.getElementById('btn-ingest-info-edit')
      const saveBtn = document.getElementById('btn-save-info-file')
      const status  = document.getElementById('info-file-save-status')
      if (!el || !editBtn || !saveBtn) return

      ingest._infoBaseline = ingest.scan.info_file_content || ''
      const isDirty = () => el.value !== (ingest._infoBaseline || '')
      const refreshSave = () => { saveBtn.disabled = !isDirty() }

      // `focus` is opt-in. setLocked is called once at render as well as from
      // the button, and focusing on render put the caret in this textarea
      // every time a folder with no info file was opened — so the first thing
      // typed went into the Confirm payload instead of the form field the
      // reviewer was aiming at (and it fired even with the panel collapsed).
      function setLocked(locked, focus) {
        el.readOnly = locked
        el.classList.toggle('rev-info-text--locked', locked)
        // .act-suppressed, not the hidden attribute: syncIngestPaneActs owns
        // `hidden` on every child of the row, and two owners of one attribute
        // is a bug waiting to happen (same rule as View Recording).
        saveBtn.classList.toggle('act-suppressed', locked)
        saveBtn.hidden = locked
        syncIngestPaneActs('isp-info')   // Save leaving can empty the row
        // "Cancel", not "Done": clicking it while editing DISCARDS whatever is
        // unsaved, so the label has to say so.
        editBtn.textContent = locked ? 'Edit File' : 'Cancel'
        if (!locked && focus) el.focus()
      }

      editBtn.addEventListener('click', () => {
        if (!el.readOnly && isDirty() &&
            !confirm('Discard unsaved changes to the info file?')) return
        if (!el.readOnly) {
          el.value = ingest._infoBaseline || ''
          ingest.scan.info_file_content = ingest._infoBaseline || ''
        }
        if (status) { status.textContent = ''; status.title = '' }
        setLocked(!el.readOnly, true)
        refreshSave()
      })

      // Straight into the payload Confirm sends — no re-parse and no
      // re-render, so typing does not lose focus or cursor position.
      el.addEventListener('input', () => {
        ingest.scan.info_file_content = el.value
        refreshSave()
      })

      setLocked(!!(ingest.scan.info_file_content || '').trim())
      refreshSave()
    })()

    // "Save to file" — write the (possibly edited) info file back to disk,
    // independent of Confirm, so a re-run of AI Assist sees the fix. Confirm
    // itself still always sends whatever's in memory, saved or not.
    document.getElementById('btn-save-info-file')?.addEventListener('click', async () => {
      const btn      = document.getElementById('btn-save-info-file')
      const status   = document.getElementById('info-file-save-status')
      const candList = ingest.scan.text_file_candidates || []
      const idx      = ingest._activeTextIdx || 0
      const filename = candList[idx]?.filename || 'info.txt'
      // Snapshot what we are SENDING. Reading ingest.scan.info_file_content
      // again after the await would read whatever has been typed since, so a
      // keystroke landing mid-request left the UI claiming text was saved that
      // never reached the disk, with Cancel then reverting to it.
      const sent = ingest.scan.info_file_content || ''
      btn.disabled = true
      status.textContent = 'Saving…'
      try {
        const res = await API.ingest.saveInfoFile({
          folder_path: ingest.folderPath,
          filename,
          content: sent,
        })
        // A from-scratch file gets a filename back — track it so the next
        // save (and a future Confirm-time re-scan) target the same file.
        if (res?.filename && !candList.length) {
          ingest.scan.text_file_candidates = [{ filename: res.filename, content: sent }]
          ingest._activeTextIdx = 0
        } else if (candList[idx]) {
          // Keep the candidate in step with the disk. Without this, saving and
          // then switching candidates and back reloaded the pre-save text and
          // Confirm sent something that disagreed with the file on disk.
          candList[idx].content = sent
        }
        // The saved text becomes the new baseline, so Save goes disabled and
        // Cancel now reverts to what is actually on disk. Stays in edit mode
        // on purpose, same as View Recording: the status line shares a row
        // with this button, and relocking would hide the very confirmation
        // the save just produced.
        ingest._infoBaseline = sent
        const saved = res?.filename || filename
        status.textContent = `Saved to ${saved}`
        status.title = `Saved to ${saved}`
        // Not a hard disable: anything typed while the request was in flight
        // is a real unsaved change and Save has to come back for it.
        btn.disabled = (ingest.scan.info_file_content || '') === sent
      } catch (e) {
        status.textContent = 'Save failed: ' + e.message
        status.title = 'Save failed: ' + e.message
        btn.disabled = false
      }
    })

    // Re-apply the queue-level values to whatever is still empty. Writes to
    // ingest.form and repaints through renderIngestStep, so the boxes on
    // screen agree with what Confirm will send — the entire point of the
    // change (see _applyQueueValuesToForm).
    document.getElementById('btn-apply-queue')?.addEventListener('click', () => {
      _applyQueueValuesToLiveForm()
      const errEl = document.getElementById('review-submit-error')
      if (!errEl) return
      // Ryan cut the "no values applied" message (2026-10-02); the button only
      // shows when values are staged, so an empty apply is near-unreachable.
      errEl.style.display = 'none'
    })

    // ── Rescan (Ryan, 2026-09-01) ──────────────────────────────────────────
    //
    // Re-runs the same inference the folder was first scanned with, over the
    // info file AS IT NOW STANDS in the Details panel — edits included, saved
    // or not. The edited text rides on the request; nothing is written to the
    // reviewer's disk. "Try again" must not silently modify a collector's
    // source folder, and the info file is the taper's own words.
    //
    // It is DESTRUCTIVE to the form, which is the point: it re-derives every
    // field and the whole track list from the corrected text. So it asks
    // first — but only when there is something to lose. `_inferred` is the
    // snapshot taken at prefill; if the form still matches it, nothing typed
    // is at risk and a confirm dialog would just be a speed bump on the
    // common case (fix the tracklist, rescan, carry on).
    // Album <-> Live Recording. Keeps everything typed: the inputs are written back to the form
    // first, then the page is drawn again in the other layout.
    document.getElementById('btn-classify-kind')?.addEventListener('click', () => {
      _syncIngestFormFromDom()
      ingest.kind = ingest.kind === 'studio' ? 'live' : 'studio'
      ingest._keepPane = state.ingestLastPane
      renderIngestStep()
    })

    document.getElementById('btn-rescan')?.addEventListener('click', async () => {
      const btn = document.getElementById('btn-rescan')
      if (!btn || btn.classList.contains('is-busy')) return

      if (_ingestFormEdited() &&
          !confirm('Rescan re-reads the info file and rebuilds every field and '
                 + 'the track list from it.\n\nAnything you have typed on this '
                 + 'form will be replaced. Continue?')) return

      const candList = ingest.scan.text_file_candidates || []
      const filename = candList[ingest._activeTextIdx || 0]?.filename || null

      btn.classList.add('is-busy')
      btn.disabled = true
      try {
        const fresh = await API.recordings.rescan(
          ingest.folderPath, filename, ingest.scan.info_file_content || '')
        // Carry the reviewer's in-memory info-file text across. The server
        // echoes back what it parsed, but `_infoBaseline` (what Cancel
        // restores, and what the Save button diffs against) is about the DISK,
        // and a rescan wrote nothing to disk — so it must NOT move.
        const baseline = ingest._infoBaseline
        ingest.scan = fresh
        ingest._infoBaseline = baseline
        // Everything derived is stale by definition: the form's prefill guard,
        // the built track list, and the members prefill that keys off the
        // artist name the scan just re-derived.
        ingest.form = { members: [], guests: [] }
        ingest.tracks = []
        ingest.lxc = null
        ingest.lxSyncRes = null
        ingest.aiApplied = {}
        // Through renderIngestStep, not renderIngestReview directly — the step
        // renderer is what reinstalls the in-page Back handler and repaints the
        // header's nav buttons. Calling the view straight would leave Back
        // pointing at whatever the previous step registered.
        renderIngestStep()
      } catch (e) {
        btn.classList.remove('is-busy')
        btn.disabled = false
        const errEl = document.getElementById('review-submit-error')
        if (errEl) {
          errEl.textContent = `Rescan failed: ${e.message}`
          errEl.style.display = 'block'
        }
      }
    })

    // is_official checkbox on recording form — cascade to every track (flags/
    // note/songwriter/official all live on ingest.tracks now; right-click a
    // row — via openTrackMenu — to edit an individual track).
    document.getElementById('f-is-official')?.addEventListener('change', function () {
      // Cascades both ways (Ryan, 2026-08-09) — it used to only ever set
      // tracks TO official; unchecking left every track stuck official with
      // no way back short of clearing each one individually by hand.
      const official = this.checked
      ingest.tracks.forEach((t, i) => { t.is_official = official; refreshIngestTrackRow(i) })
    })

    // Parsed tracks toggle — expand/collapse the track list
    ;(function () {
      const toggleBtn = document.getElementById('btn-parsed-tracks-toggle')
      const trackList = document.getElementById('rev-parsed-tracklist')
      if (!toggleBtn || !trackList) return
      toggleBtn.addEventListener('click', e => {
        e.stopPropagation()  // don't bubble to panel toggle
        const visible = trackList.style.display !== 'none'
        trackList.style.display = visible ? 'none' : ''
        toggleBtn.innerHTML = chevronIcon(visible ? 'caret-ic--down' : 'caret-ic--up')  // ▴=visible, ▾=collapsed
      })
    })()

    // Artist autocomplete
    // Artist + Members/Guests widget.
    const addMembersWidget = createMembersWidget(ingest.form, {
      artistInput: 'f-artist', artistDropdown: 'f-artist-dropdown',
      field: 'f-members-field',
      // Optional, and only Add Recording passes them — View Recording reuses
      // this widget and has no genre field.
      genreInput: 'f-genre', genreIdInput: 'f-genre-id',
    })
    addMembersWidget.mount()
    const membersReady = initAddArtistMembers(addMembersWidget)

    // ── Genre (Ryan, 2026-09-01) ────────────────────────────────────────────
    //
    // Genre is a property of the ARTIST, not of the recording — there is no
    // genre column on Recording and there should not be, since an act's genre
    // is the same on every night it played. So this field says what the act's
    // genre is, and Confirm writes it to the Artist row.
    //
    // ⚠ This softens a standing rule, deliberately and on Ryan's instruction.
    // The Genre design spec (2026-08-02) says nothing may create a genre
    // implicitly and that creating one is an explicit action on the Genres
    // page. What that rule was protecting against is a typo in a hurried
    // ingest quietly minting "Blugrass" alongside "Bluegrass" — the placeholder
    // -venue contamination story in another costume.
    //
    // The protection is kept where it matters: creation here is still EXPLICIT.
    // Typing a name that does not exist creates nothing. The dropdown offers a
    // distinct "+ Create genre: …" row that has to be clicked, and Enter
    // commits the top MATCH rather than whatever was typed (firstPickerResult,
    // the same rule the Artist page's genre field uses). A half-typed name
    // left in the box on submit is discarded, not created — see the Confirm
    // payload below.
    ;(function () {
      const input = document.getElementById('f-genre')
      const idEl  = document.getElementById('f-genre-id')
      const dd    = document.getElementById('f-genre-dropdown')
      if (!input || !dd) return

      // `source` says who set the genre: 'hand' (the default: a person's pick or edit), 'suggestion'
      // (MusicBrainz or Lomax accepted into the field), or null (carried over from the act itself).
      // Confirm sends it so only a hand pick may replace an act's existing genre.
      const setGenre = ({ id, name }, source) => {
        ingest.setFormGenre = setGenre         // the Resolver table fills the field through this
        ingest.form.genre_source = source === undefined ? 'hand' : source
        ingest.form.genre_id   = id || null
        ingest.form.genre_name = name || ''
        input.value = name || ''
        idEl.value  = id || ''
        // A NEW genre has no id yet — mark it so the field reads as a pending
        // creation rather than as an ordinary pick, and so a glance tells you
        // a row is about to be added to a controlled vocabulary.
        input.classList.toggle('is-new-genre', !!name && !id)
      }

      // The picker searches the full list rather than a /search endpoint:
      // genres are a small controlled vocabulary (dozens, not thousands) and
      // GET /api/genres/ is already fetched for the sidebar, so a substring
      // filter here costs one cached round trip and no new server surface.
      wirePickerDropdown(input, dd,
        async q => {
          const all = await API.genres.list()
          const ql = q.toLowerCase()
          return all.filter(g => g.name.toLowerCase().includes(ql))
        },
        setGenre, 'Create genre')

      input.addEventListener('keydown', e => {
        if (e.key !== 'Enter') return
        e.preventDefault()
        // Enter commits the top EXISTING match, never the raw text — creating
        // a vocabulary entry has to be a deliberate click on the create row.
        const m = firstPickerResult(dd)
        if (m) setGenre(m)
      })
      // Clearing the box clears the link. Anything else typed and left
      // unmatched is dropped on submit; `blur` is where that becomes visible
      // rather than at Confirm, so the field never lies about what it holds.
      input.addEventListener('blur', () => setTimeout(() => {
        const typed = input.value.trim()
        if (!typed) { setGenre({ id: null, name: '' }); return }
        if (typed.toLowerCase() !== (ingest.form.genre_name || '').toLowerCase()) {
          setGenre({ id: ingest.form.genre_id || null, name: ingest.form.genre_name || '' }, ingest.form.genre_source || null)
        }
      }, 220))   // after wirePickerDropdown's own 200ms close, or a click on a
                 // dropdown row is undone before it lands

      ingest.setFormGenre = setGenre
      if (ingest.form.genre_name) setGenre({ id: ingest.form.genre_id, name: ingest.form.genre_name }, ingest.form.genre_source || null)
    })()

    // End date toggle — show/hide the row; pre-fill from start date on first reveal
    ;(function () {
      const toggleBtn = document.getElementById('btn-toggle-end-date')
      const endRow    = document.getElementById('end-date-row')
      if (!toggleBtn || !endRow) return

      // If end date was already set (back-nav), show immediately
      if (ingest.form.end_year) {
        endRow.style.display = ''
        toggleBtn.textContent = '− End Date'
      }

      toggleBtn.addEventListener('click', e => {
        e.preventDefault()
        const visible = endRow.style.display !== 'none'
        if (visible) {
          // Hide and clear
          endRow.style.display = 'none'
          toggleBtn.textContent = '+ End Date'
          document.getElementById('f-end-year').value  = ''
          document.getElementById('f-end-month').value = ''
          document.getElementById('f-end-day').value   = ''
        } else {
          // Show and pre-fill from start date
          endRow.style.display = ''
          toggleBtn.textContent = '− End Date'
          const yr = document.getElementById('f-year').value
          const mo = document.getElementById('f-month').value
          const dy = document.getElementById('f-day').value
          document.getElementById('f-end-year').value  = yr
          document.getElementById('f-end-month').value = mo
          document.getElementById('f-end-day').value   = dy
          document.getElementById('f-end-year').focus()
        }
      })
    })()

    // Duplicate-in-library check — fires once artist + year are both
    // known. Non-blocking: a second source for the same show (SBD + AUD) is
    // legitimate, so this only informs, never prevents Confirm. Debounced so
    // it doesn't hammer the API on every keystroke. (Ryan, 2026-07-14.)
    ;(function () {
      const artistEl = document.getElementById('f-artist')
      const yearEl   = document.getElementById('f-year')
      const monthEl  = document.getElementById('f-month')
      const dayEl    = document.getElementById('f-day')
      const warnEl   = document.getElementById('dup-warn')
      const bodyEl   = document.getElementById('dup-warn-body')
      if (!artistEl || !yearEl || !warnEl) return

      let debounce = null
      async function runCheck() {
        const artist_name = artistEl.value.trim()
        const year  = parseInt(yearEl.value)  || null
        const month = parseInt(monthEl.value) || null
        const day   = parseInt(dayEl.value)   || null
        if (!artist_name || !year) { warnEl.style.display = 'none'; return }
        try {
          const res   = await API.ingest.checkExisting({ artist_name, year, month, day })
          const perfs = res.performances || []
          if (!perfs.length) { warnEl.style.display = 'none'; return }
          bodyEl.innerHTML = perfs.map(p => `
            <div class="dup-warn-perf">
              <span class="dup-warn-perf-head">${esc(p.date)}${p.venue ? ' · ' + esc(p.venue) : ''}</span>
              ${p.recordings.map(r => `
                <div class="dup-warn-rec">${esc(r.source || 'Unknown source')}${r.quality ? ' · ' + esc(r.quality) : ''} \
· ${r.track_count} track${r.track_count !== 1 ? 's' : ''}${r.created_at ? ' · added ' + esc(r.created_at.slice(0, 10)) : ''}</div>`).join('')}
            </div>`).join('')
          warnEl.style.display = ''
        } catch (_) { /* best-effort — a failed check should never block ingest */ }
      }

      ;[artistEl, yearEl, monthEl, dayEl].forEach(el => {
        el.addEventListener('input', () => {
          clearTimeout(debounce)
          debounce = setTimeout(runCheck, 500)
        })
      })
      runCheck()   // also on load — covers AI Assist auto-fill / back-nav restore
    })()

    // Paula's purple border means "I pre-filled this with confidence" — the
    // moment a human edits that specific field it's their entry, not hers,
    // so the border clears immediately (no re-scoring involved, just a
    // one-time visual cue that's done its job).
    ;(function () {
      ['f-artist', 'f-year', 'f-month', 'f-day',
       'f-venue-name', 'f-city', 'f-state', 'f-country'].forEach(id => {
        const el = document.getElementById(id)
        if (el) el.addEventListener('input', () => el.classList.remove('paula-recommend'), { once: true })
      })
    })()

    // Venue picker — autocomplete with lock/unlock of city/state/country
    ;(function () {
      const nameEl  = document.getElementById('f-venue-name')
      const idEl    = document.getElementById('f-venue-id')
      const dropEl  = document.getElementById('f-venue-dropdown')
      const cityEl  = document.getElementById('f-city')
      const stateEl = document.getElementById('f-state')
      const cntryEl = document.getElementById('f-country')
      let debounce  = null

      function lockLocation(venue) {
        // Placeholder venues ("Unknown Venue", "TBD", ...) aren't one real
        // canonical place — their stored city/state/country is leftover from
        // whichever other show wrote there last, not this show's location.
        // Don't lock/prefill from it; leave the tag/info guess editable.
        // (Ryan, 2026-07-15 — see app/utils/venues.py for the full story.)
        if (isPlaceholderVenue(venue?.name)) return
        cityEl.value  = venue.city    || ''
        stateEl.value = venue.state   || ''
        cntryEl.value = venue.country || ''
        cityEl.disabled  = true
        stateEl.disabled = true
        cntryEl.disabled = true
      }

      function unlockLocation() {
        cityEl.disabled  = false
        stateEl.disabled = false
        cntryEl.disabled = false
      }

      // Restore lock on back-nav if a venue was previously selected. On a
      // fresh scan (no venue_id yet — just a tag/info-derived name), check
      // whether that name already matches an existing venue: if so, treat it
      // like a manual pick and lock city/state/country to the venue's own
      // stored values rather than the tag/info guess, which may be stale or
      // just less precise (e.g. "Ottawa, ON" in tags vs. the venue's actual
      // "Gatineau, QC"). A genuinely new venue name is left as the tag/info
      // prefill, editable. (Ryan, 2026-07-14.)
      if (ingest.form.venue_id) {
        API.venues.get(ingest.form.venue_id).then(v => lockLocation(v)).catch(() => {})
      } else if (nameEl.value.trim().length >= 2) {
        const typed = nameEl.value.trim()
        API.venues.list(typed).then(venues => {
          if (idEl.value) return   // user already picked something while this was in flight
          const exact = venues.find(v => v.name.toLowerCase() === typed.toLowerCase())
          if (exact) {
            idEl.value = exact.id
            ingest.form.venue_id = exact.id
            lockLocation(exact)
          }
        }).catch(() => {})
      }

      function closeDropdown() { dropEl.style.display = 'none'; dropEl.innerHTML = '' }

      function showResults(venues, q) {
        dropEl.innerHTML = ''
        const rows = venues.map(v => {
          const loc = [v.city, v.state, v.country].filter(Boolean).join(', ')
          return `<div class="venue-result" data-id="${v.id}" data-name="${esc(v.name)}">
            <span class="venue-result-name">${esc(v.name)}</span>
            ${loc ? `<span class="venue-result-loc">${esc(loc)}</span>` : ''}
          </div>`
        }).join('')
        // Only offer "+ Create" when the typed name doesn't already exist —
        // no point suggesting creation of a venue that's right there in the list.
        const exactMatch = venues.some(v => v.name.toLowerCase() === q.toLowerCase())
        const createRow = (q && !exactMatch)
          ? `<div class="venue-result venue-result-create" data-id="" data-name="${esc(q)}">+ Create "${esc(q)}"</div>`
          : ''
        dropEl.innerHTML = rows + createRow
        dropEl.style.display = (rows || createRow) ? 'block' : 'none'

        dropEl.querySelectorAll('.venue-result').forEach(el => {
          el.addEventListener('mousedown', async e => {
            e.preventDefault()
            if (el.dataset.id) {
              // Existing venue — lock location fields to venue's stored values
              idEl.value   = el.dataset.id
              nameEl.value = el.dataset.name
              try {
                const v = await API.venues.get(parseInt(el.dataset.id))
                lockLocation(v)
              } catch (_) {}
            } else {
              // New venue — just set the name, leave ID empty so confirm endpoint
              // creates it with city/state/country from the form fields
              nameEl.value = q
              idEl.value   = ''
              unlockLocation()
            }
            closeDropdown()
          })
        })
      }

      nameEl.addEventListener('input', () => {
        idEl.value = ''     // clear selection when user edits
        unlockLocation()    // re-enable location fields when typing
        const q = nameEl.value.trim()
        clearTimeout(debounce)
        if (q.length < 2) { closeDropdown(); return }
        debounce = setTimeout(async () => {
          try { showResults(await API.venues.list(q), q) }
          catch (_) { closeDropdown() }
        }, 220)
      })

      nameEl.addEventListener('blur',  () => setTimeout(closeDropdown, 200))
      nameEl.addEventListener('focus', () => {
        if (nameEl.value.trim().length >= 2) nameEl.dispatchEvent(new Event('input'))
      })
    })()

    // Event picker — simple autocomplete (no location lock, just name+id)
    ;(function () {
      const nameEl = document.getElementById('f-event-name')
      const idEl   = document.getElementById('f-event-id')
      const dropEl = document.getElementById('f-event-dropdown')
      let debounce = null

      function closeDropdown() { dropEl.style.display = 'none'; dropEl.innerHTML = '' }

      function showResults(events, q) {
        dropEl.innerHTML = ''
        const rows = events.map(ev => `
          <div class="event-result" data-id="${ev.id}" data-name="${esc(ev.name)}">
            ${esc(ev.name)}
          </div>`).join('')
        const createRow = q
          ? `<div class="event-result event-result-create" data-id="" data-name="${esc(q)}">+ Create "${esc(q)}"</div>`
          : ''
        dropEl.innerHTML = rows + createRow
        dropEl.style.display = (rows || createRow) ? 'block' : 'none'

        dropEl.querySelectorAll('.event-result').forEach(el => {
          el.addEventListener('mousedown', async e => {
            e.preventDefault()
            if (el.dataset.id) {
              idEl.value   = el.dataset.id
              nameEl.value = el.dataset.name
            } else {
              // Create new event record on the fly
              try {
                const created = await API.events.create({ name: q })
                idEl.value   = created.id
                nameEl.value = created.name
              } catch (err) { console.error('Failed to create event:', err) }
            }
            closeDropdown()
          })
        })
      }

      nameEl.addEventListener('input', () => {
        idEl.value = ''
        const q = nameEl.value.trim()
        clearTimeout(debounce)
        if (q.length < 2) { closeDropdown(); return }
        debounce = setTimeout(async () => {
          try { showResults(await API.events.search(q), q) }
          catch (_) { closeDropdown() }
        }, 220)
      })

      nameEl.addEventListener('blur',  () => setTimeout(closeDropdown, 200))
      nameEl.addEventListener('focus', () => {
        if (nameEl.value.trim().length >= 2) nameEl.dispatchEvent(new Event('input'))
      })
    })()

    // Enter key on track title → select next track's title
    const titleInputs = [...mainContent.querySelectorAll('.t-title')]
    titleInputs.forEach((el, i) => {
      el.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          e.preventDefault()
          const next = titleInputs[i + 1]
          if (next) { next.focus(); next.select() }
        }
      })
    })

    // Standardized back link (top of page). Both this and the header Back
    // button call the one function below, so they cannot disagree about
    // where "back" is (Ryan, 2026-08-28).
    document.getElementById('ingest-back-link').addEventListener('click', e => {
      e.preventDefault()
      ingestBackFromReview()
    })

    // Lomax. One controller on this folder drives the Resolver tab and the
    // chat. A run is filed against the folder, and moves to the recording when
    // it is added. Accepting applies to the form here, because the database has
    // nothing yet (the server only records the decision).
    {
      const resRoot = document.getElementById('lx-res-root')
      const chatPane = document.getElementById('isp-ai')
      const alive = () => !!document.getElementById('ingest-panes')
      const resOpts = () => ({ resolved: ingest.scan.resolved, value: getFormField, tracks: ingest.tracks,
                               genreRow: !!ingest.form._genreRow, mbGenre: ingest.form._mbGenre })
      // Fill the Genre field from a proposal or the MusicBrainz suggestion. An act has one genre and
      // a genre already in the form (one a person set) is never replaced.
      const applyGenre = async (name, id) => {
        const f = ingest.form
        if (f.genre_id || f.genre_name || !ingest.setFormGenre || !name) return false
        let g = null
        try {
          g = (await API.genres.list()).find(x => (id && x.id === id) || x.name.toLowerCase() === String(name).toLowerCase())
        } catch (_) { /* the vocabulary could not be read: the name is offered as a new genre */ }
        ingest.setFormGenre(g ? { id: g.id, name: g.name } : { id: null, name }, 'suggestion')
        return true
      }
      const applyLocal = async props => {
        ingest.aiApplied = ingest.aiApplied || {}
        const done = ingest.form._lxApplied = ingest.form._lxApplied || {}
        for (const p of props) {
          const m = /^track\.(\d+)\.(title|songwriter|note)$/.exec(p.field)
          if (m) {
            const i = ingest.tracks.findIndex(t => String(t.track_number) === m[1])
            if (i < 0) continue
            const t = ingest.tracks[i]
            if (m[2] === 'title') {
              t.title = p.proposed
              const inp = mainContent.querySelector(`.t-title[data-idx="${i}"]`)
              if (inp) inp.value = p.proposed
            } else t[m[2] === 'note' ? 'notes' : 'songwriter'] = p.proposed
            refreshIngestTrackRow(i)
          } else if (p.field === 'genre') {
            await applyGenre(p.proposed)
          } else if (ingest.kind === 'studio' && LX_ALBUM_FIELDS.some(([k]) => k === p.field)) {
            // An album's own fields: title -> the album title input, year -> Year, notes -> Notes.
            const id = { title: 'f-album-title', year: 'f-year', notes: 'f-notes' }[p.field]
            const key = { title: 'album_title', year: 'start_year', notes: 'notes' }[p.field]
            if (!(p.field in ingest.aiApplied)) ingest.aiApplied[p.field] = document.getElementById(id)?.value || ''
            const el = document.getElementById(id)
            if (el) { el.value = p.proposed; el.classList.add('ai-applied') }
            ingest.form[key] = p.proposed
          } else if (LX_FIELDS.some(([k]) => k === p.field)) {
            if (!(p.field in ingest.aiApplied)) ingest.aiApplied[p.field] = getFormField(p.field)
            setFormField(p.field, p.proposed)
          }
          if (p.id) done[p.id] = true
        }
        reScore()
      }
      // The stored decision is the truth. A show reopened gets a fresh form from the scan, but its
      // accepted proposals are still accepted: put each back, as a human-set value, unless this
      // form already took it. A value that cannot be written (a genre over one already chosen) stays
      // as it is.
      const formValue = field => {
        const m = /^track\.(\d+)\.(title|songwriter|note)$/.exec(field)
        if (ingest.kind === 'studio' && field === 'title') return document.getElementById('f-album-title')?.value || ''
        if (ingest.kind === 'studio' && field === 'year') return document.getElementById('f-year')?.value || ''
        if (ingest.kind === 'studio' && field === 'notes') return document.getElementById('f-notes')?.value || ''
        if (!m) return getFormField(field)
        const t = ingest.tracks.find(x => String(x.track_number) === m[1])
        return t ? (t[m[2] === 'note' ? 'notes' : m[2]] || '') : ''
      }
      const reapplyAccepted = async ctl => {
        const done = ingest.form._lxApplied || {}, dirty = ingest.form._dirty || {}
        const todo = lxAcceptedToReapply(ctl.runs, formValue, p => done[p.id] || dirty[p.field], lxFingerprint(ingest.scan))
        if (todo.length) await applyLocal(todo)
      }
      // A field the person types into is theirs: it is never re-applied from a stored decision.
      const FIELD_OF = { 'f-artist': 'artist', 'f-venue-name': 'venue', 'f-city': 'city', 'f-state': 'state',
        'f-country': 'country', 'f-event-name': 'event', 'f-source': 'source', 'f-stage': 'stage',
        'f-lineage': 'lineage', 'f-year': 'date', 'f-month': 'date', 'f-day': 'date', 'f-genre': 'genre',
        'f-album-title': 'title', 'f-notes': 'notes' }
      mainContent.oninput = ev => {        // one handler, replaced on each render
        const t = ev.target
        if (!t || !alive()) return
        let field = FIELD_OF[t.id]
        if (!field && t.classList && t.classList.contains('t-title')) {
          const tr = ingest.tracks[parseInt(t.dataset.idx)]
          if (tr) field = `track.${tr.track_number}.title`
        }
        if (field) _ingestMarkDirty(field)
        if (t.id === 'f-year') _ingestMarkDirty('year')   // an album's Year is the proposal field 'year'
      }
      const c = lomaxController({
        skill: ingest.kind === 'studio' ? 'album' : 'recording', subjectType: 'folder', subjectKey: ingest.folderPath,
        current: ingest.kind === 'studio' ? collectAlbumMeta : collectCurrentMeta, alive, onLoaded: reapplyAccepted,
        afterAccept: async props => { await applyLocal(props); scheduleGenre() },
        onAct: async (act, btn, ctl) => {
          const mb = ingest.form._mbGenre
          if (!mb) return
          if (act === 'mb-genre-accept') { if (await applyGenre(mb.name, mb.genre_id)) mb.state = 'accepted' }
          else if (act === 'mb-genre-dismiss') mb.state = 'dismissed'
          ctl.refresh()
        },
      })
      // The Genre row: shown when the act is new or has no genre, with what MusicBrainz offers for it
      // (an online lookup outside the resolver; nothing is shown when it cannot be reached).
      // The lookup is for the act the form will save: the Artist field as it stands now (typed or
      // accepted from Lomax), not the reading the scan made. Answers are cached per name on the form.
      const offerGenre = async () => {
        const f = ingest.form, name = (getFormField('artist') || f.artist_name || '').trim()
        if (!alive()) return
        const key = name.toLowerCase()
        if (f.genre_id || f.genre_name || !name) {
          if (f._mbGenre && f._mbGenre.key !== key) { f._mbGenre = null; c.refresh() }
          return
        }
        f._genreRow = true
        f._mbCache = f._mbCache || {}
        if (!f._mbGenre || f._mbGenre.key !== key) {
          const hit = f._mbCache[key]
          f._mbGenre = hit ? { ...hit } : { key, name: '' }
          if (!hit) {
            try {
              const g = ((await API.ingest.genreSuggestion(name)) || {}).genre
              const got = g && g.name ? { key, name: g.name, genre_id: g.genre_id || null } : { key, name: '' }
              f._mbCache[key] = got
              if (ingest.form === f && (getFormField('artist') || '').trim().toLowerCase() === key) f._mbGenre = { ...got }
            } catch (_) { /* offline or unreachable: no suggestion, and nothing cached */ }
          }
        }
        if (alive()) c.refresh()
      }
      let genreTimer = null
      const scheduleGenre = () => {
        clearTimeout(genreTimer)
        genreTimer = setTimeout(offerGenre, 600)
      }
      membersReady.then(offerGenre)
      document.getElementById('f-artist')?.addEventListener('input', scheduleGenre)
      document.getElementById('f-artist')?.addEventListener('change', scheduleGenre)
      ingest.lxc = c
      if (resRoot) {
        resRoot.closest('.slide-pane').setAttribute('data-lx-ctl', c.id)
        c.addView(ctl => {
          if (!document.body.contains(resRoot)) return false
          lxRepaint(resRoot, lomaxResolverHtml(ctl, resOpts()))
          return true
        })
        ingest.lxSyncRes = () => lxSyncResolver(resRoot, resOpts())
      }
      lomaxMountChat(chatPane, null, {
        addNotes: async text => {
          const el = document.getElementById('f-notes')
          if (!el) return
          el.value = el.value.trim() ? `${el.value.trim()}\n${text}` : text
          ingest.form.notes = el.value
        },
      }, c)
      c.refresh()
      c.load()
    }

    // MusicBrainz tab (albums only). Searches by the form's Artist and album title when asked,
    // never on its own, and never picks: a click on a candidate does. A pick fills only what is
    // empty (album title, Year, track titles that are empty or "Track N"); the release's label,
    // catalog number and country show under the album title. The id rides along on save.
    ;(function () {
      const root = document.getElementById('ingest-mb-root')
      if (!root) return
      const m = () => (ingest.mb = ingest.mb || { state: 'idle', cands: [], picked: null, error: '' })
      const val = id => (document.getElementById(id)?.value || '').trim()
      const paintFacts = () => {
        const box = document.getElementById('ingest-mb-facts')
        if (box) box.innerHTML = ingestMbFactsHtml()
      }
      const findBtn = label => `<button type="button" class="btn ${label === 'Find release' ? 'btn-primary' : 'btn-ghost'} btn-xs" id="ingest-mb-find">${label}</button>`
      function paint() {
        if (!document.body.contains(root)) return
        const st = m()
        if (st.picked) {
          root.innerHTML = `
            <div class="pp-mb-linked">${ingestMbFactsHtml()}</div>
            <div class="pp-mb-foot">
              <span class="pp-mb-dot"></span>Linked by you
              <button type="button" class="btn btn-ghost btn-xs" id="ingest-mb-unlink">Unlink</button>
            </div>`
        } else if (st.state === 'loading') {
          root.innerHTML = `<div class="pp-mb-empty">Searching MusicBrainz…</div>`
        } else if (st.state === 'fetching') {
          root.innerHTML = `<div class="pp-mb-empty">Fetching…</div>`
        } else if (st.state === 'error') {
          root.innerHTML = `<div class="pp-mb-empty">Lookup failed: ${esc(st.error)} ${findBtn('Try again')}</div>`
        } else if (st.state === 'list' && !st.cands.length) {
          root.innerHTML = `<div class="pp-mb-empty">Nothing found for “${esc(st.title || '')}”.</div>${findBtn('Try again')}`
        } else if (st.state === 'list') {
          root.innerHTML = `
            <div class="pp-mb-prompt">Click the right release to link it${st.cands.length === 1 ? '' : ' (more than one matches)'}.</div>
            <div class="pp-mb-cands">
              ${st.cands.map(c => `
                <div class="pp-mb-cand pp-mb-cand--noscore" data-mbid="${esc(c.mbid)}" role="button" tabindex="0">
                  <span class="pp-mb-cand-name">${esc(c.title || '')}</span>
                  <span class="pp-mb-cand-meta">${[c.label, c.catalog_number, c.country, c.date].filter(Boolean).map(esc).join(' · ')}</span>
                  <a class="pp-mb-cand-view" href="${esc(mbReleaseUrl(c.mbid))}" target="_blank" rel="noopener" title="Open on musicbrainz.org">View ↗</a>
                </div>`).join('')}
            </div>
            <div class="pp-mb-foot"><button type="button" class="btn btn-ghost btn-xs" id="ingest-mb-cancel">Cancel</button></div>`
        } else {
          root.innerHTML = findBtn('Find release')
        }
        document.getElementById('ingest-mb-find')?.addEventListener('click', search)
        document.getElementById('ingest-mb-cancel')?.addEventListener('click', () => { m().state = 'idle'; paint() })
        document.getElementById('ingest-mb-unlink')?.addEventListener('click', () => {
          const st2 = m(); st2.picked = null; st2.state = 'idle'; st2.cands = []
          paintFacts(); paint()
        })
        root.querySelectorAll('.pp-mb-cand').forEach(el => {
          el.addEventListener('click', e => { if (!e.target.closest('.pp-mb-cand-view')) pick(el.dataset.mbid) })
          el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.click() } })
        })
      }
      async function search() {
        const st = m()
        if (!val('f-album-title')) { document.getElementById('f-album-title')?.focus(); return }
        st.state = 'loading'; st.title = val('f-album-title'); paint()
        try {
          const res = await API.ingest.releaseCandidates(val('f-artist'), st.title, (ingest.tracks || []).length)
          st.cands = (res && res.candidates) || []
          st.state = 'list'
        } catch (e) { st.state = 'error'; st.error = e.message || String(e) }
        paint()
      }
      async function pick(mbid) {
        const st = m()
        st.state = 'fetching'; paint()
        let d
        try { d = ((await API.ingest.releaseDetail(mbid)) || {}).release } catch (e) { d = null }
        const cand = st.cands.find(c => c.mbid === mbid) || { mbid }
        if (!d) { st.state = 'error'; st.error = 'release not found'; paint(); return }
        d = Object.assign({}, cand, d, { mbid })
        // Fill only what is empty.
        const titleEl = document.getElementById('f-album-title')
        if (titleEl && !titleEl.value.trim() && d.title) { titleEl.value = d.title; ingest.form.album_title = d.title }
        const yearEl = document.getElementById('f-year')
        const yr = /^(\d{4})/.exec(d.date || '')
        if (yearEl && !yearEl.value.trim() && yr) { yearEl.value = yr[1]; ingest.form.start_year = yr[1] }
        const rt = d.tracks || []
        if (rt.length && rt.length === (ingest.tracks || []).length) {
          const order = ingest.tracks.map((t, i) => [t, i]).sort((a, b) => (a[0].track_number || 0) - (b[0].track_number || 0))
          order.forEach(([t, i], k) => {
            const inp = mainContent.querySelector(`.t-title[data-idx="${i}"]`)
            const cur = (inp ? inp.value : t.title || '').trim()
            const next = rt[k] && rt[k].title
            if (next && (!cur || /^track\s*\d+$/i.test(cur))) {
              t.title = next
              if (inp) inp.value = next
              refreshIngestTrackRow(i)
            }
          })
        }
        st.picked = { mbid, title: d.title, label: d.label, catalog_number: d.catalog_number, country: d.country, date: d.date }
        st.state = 'idle'
        paintFacts(); paint(); reScore()
      }
      paint()
    })()

    // Details panel: horizontal tabs + the permanent rail, same gestures as
    // View Recording. Clicking the ACTIVE tab collapses the panel, which is the
    // gesture existing muscle memory expects; the rail toggles it both ways and
    // is the one control that advertises itself.
    ;(function () {
      const panel = document.getElementById('ingest-slide-panel')
      if (!panel) return
      panel.querySelectorAll('.slide-tab').forEach(tab => {
        tab.addEventListener('click', () => {
          const pane = tab.dataset.ipane
          if (panel.classList.contains('open') && tab.classList.contains('active')) {
            _ingestPanelOpen(false)
          } else {
            switchIngestPane(pane, tab)
          }
        })
      })
      document.getElementById('ingest-slide-rail')?.addEventListener('click', () => {
        if (panel.classList.contains('open')) _ingestPanelOpen(false)
        else switchIngestPane(panel.querySelector(`.slide-tab[data-ipane="${state.ingestLastPane}"]`) ? state.ingestLastPane
          : (panel.querySelector('.slide-tab')?.dataset.ipane || 'isp-info'))
      })
      // Default open on the Resolver when adding a recording (Ryan, 2026-10-03:
      // it is the first tab here), else Info File. A deliberate collapse
      // survives moving between recordings, same rule as recPanelOpen on
      // View Recording. switchIngestPane sets the active tab and pane.
      _ingestQualityLoaded = false
      // An album has no Info File or Resolver tab, so a remembered pane that has no tab falls back.
      const _hasTab = id => !!panel.querySelector(`.slide-tab[data-ipane="${id}"]`)
      const _firstPane = ingest._keepPane && _hasTab(ingest._keepPane)
        ? ingest._keepPane
        : _hasTab('isp-resolver') ? 'isp-resolver'
        : _hasTab('isp-info') ? 'isp-info' : (panel.querySelector('.slide-tab')?.dataset.ipane || 'isp-info')
      delete ingest._keepPane
      if (state.ingestPanelOpen === false) _ingestPanelOpen(false)
      else switchIngestPane(_firstPane)
    })()

    const _submitReview = async (ev) => {
      // Which exit the user chose: 'return' (back to the ingest queue) or
      // 'view' (open the finished record).
      const after = ev.currentTarget.dataset.after || 'view'
      // Collect metadata
      const f = ingest.form
      _syncIngestFormFromDom()

      if (!f.artist_name) { alert('Artist name is required.'); return }

      // Track titles were collected by _syncIngestFormFromDom. Notes,
      // songwriter, flags, and official are already staged directly on
      // ingest.tracks by openTrackMenu's onChange (right-click popup).

      // Submit directly — the old "Confirm & Add to Library" review screen
      // is gone (Ryan, 2026-07-15: "never very useful, nothing is ever
      // something i need to change"). File-behavior (copy/move) is no longer
      // a per-add choice either — it's a standing preference (Settings ⚙,
      // right next to the Anthropic key), read silently here.
      const btn = ev.currentTarget
      const otherBtn = document.getElementById(
        btn.id === 'btn-confirm' ? 'btn-confirm-view' : 'btn-confirm')
      const errEl = document.getElementById('review-submit-error')
      btn.disabled = true
      if (otherBtn) otherBtn.disabled = true
      // The LABEL DOES NOT CHANGE (Ryan, 2026-09-01). It used to become
      // "Adding to library…", a wider string, so the button grew at the exact
      // moment it was clicked and pushed the pair sideways — and with the
      // Files select unable to shrink, the row wrapped and the buttons
      // dropped onto a second line. Busy is now a CSS spinner in front of an
      // unchanged label, which cannot change the button's width, and the
      // detail ("Preparing…", "Copying files… 41%") goes to the progress bar
      // below, which has room for a sentence and moves nothing when it
      // changes.
      btn.classList.add('is-busy')
      errEl.style.display = 'none'

      const payload = {
        source_folder_path: ingest.folderPath,
        ...f,
        tracks: ingest.tracks,
        fingerprints: ingest.scan.fingerprints || [],
        info_file_content: ingest.scan.info_file_content || null,
        members: (f.members || []).map(m => m.name),
        guests:  (f.guests  || []).map(m => m.name),
        // Written to the ARTIST, not the recording — see api/ingest.py.
        // Both are sent: an id links an existing genre, a name creates one.
        genre_id:   f.genre_id   || null,
        genre_name: f.genre_name || null,
        // 'hand' when a person picked the genre (may replace the act's genre); anything else fills only an empty one.
        genre_source: f.genre_source || null,
        // Lomax may already have run on this draft (a run on the folder moves to
        // the recording on confirm; the result rides along as the fallback).
        ai_result: ingest.lxc?.latest()?.result || null,
        // Fields whose value the person applied from an AI proposal (they may learn aliases).
        ai_accepted: Object.keys(ingest.aiApplied || {}),
        resolver_result: ingest.scan?.resolved || null,
        kind: ingest.kind === 'studio' ? 'studio' : 'live',
      }
      // An album sends its title and none of the live-only fields; a live recording sends no title.
      delete payload.album_title
      payload.mb_release_id = (payload.kind === 'studio' && ingest.mb && ingest.mb.picked) ? ingest.mb.picked.mbid : null
      delete payload._genreText
      if (payload.kind === 'studio') {
        payload.title = f.album_title || null
        Object.assign(payload, {
          start_month: null, start_day: null, end_year: null, end_month: null, end_day: null,
          venue_name: '', venue_id: null, event_name: '', event_id: null, stage: '',
          city: '', state: '', country: '', quality: '', source: '', lineage: '',
          source_tag: '', etree_shnid: '',
        })
      }
      // ⚠ NO blanket-value fallback here any more (2026-09-03).
      //
      // This used to re-apply `lq.applyAll` field by field to anything the
      // payload had left empty — a safety net from when the values were
      // invisible on this form. They are not invisible now: applied values are
      // written into the form at prefill, over the top of the inference, and
      // the form shows them highlighted. So what is on screen IS what gets
      // sent, and a net underneath it would only reintroduce the divergence it
      // was built to paper over — worse, it would silently undo a deliberate
      // edit the reviewer made to a blanket value for this one show, which is
      // precisely why they opened Review.
      //
      // The re-apply button on this form (btn-apply-queue) is the deliberate
      // way to pull the staged values back in after a Rescan or an edit.

      // Progress UI under the button (copy can take a while for big folders)
      // The progress area is already in the markup, holding its space.
      const prog  = document.getElementById('confirm-progress')
      const fill  = document.getElementById('confirm-progress-fill')
      const label = document.getElementById('confirm-progress-label')
      if (fill)  fill.style.width = '0'
      if (label) label.textContent = 'Preparing…'
      if (prog)  prog.style.visibility = 'visible'
      const fmtMB = b => b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(1) + ' MB'

      try {
        const { job_id } = await API.ingest.confirm(payload)
        const result = await pollConfirmJob(job_id, (copied, total) => {
          const pct = total ? Math.min(100, Math.round(100 * copied / total)) : 0
          if (fill)  fill.style.width = pct + '%'
          if (label) label.textContent = total ? `Copying files… ${pct}% (${fmtMB(copied)} / ${fmtMB(total)})` : 'Copying files…'
        })
        if (fill) fill.style.width = '100%'
        ingest._lastResult = result
        if (result.recording_id) {
          if (result.checksum_mismatches > 0) {
            alert(`${result.checksum_mismatches} track checksum${result.checksum_mismatches === 1 ? '' : 's'} did not match the fingerprint file for this show. Check the Checksums pane before trusting this copy.`)
          }
          await loadArtistList()   // new artist/venue/musician may exist

          if (after === 'view') {
            resetIngestState()
            window.location.hash = `#/recording/${result.recording_id}`
          } else if (ingest.returnTo) {
            // "Add & Return": back to the run's import page.
            _ingestReturnToRun()
          } else {
            resetIngestState()
            window.location.hash = `#/recording/${result.recording_id}`
          }
        } else {
          // Fallback, shouldn't normally happen — no recording_id to jump to.
          ingest.step = 'success'
          renderIngestStep()
        }
      } catch (e) {
        errEl.textContent = `Error: ${e.message}`
        errEl.style.display = 'block'
        btn.disabled = false
        if (otherBtn) otherBtn.disabled = false
        btn.classList.remove('is-busy')
        if (prog) prog.style.visibility = 'hidden'
      }
    }

    document.getElementById('btn-confirm').addEventListener('click', _submitReview)
    document.getElementById('btn-confirm-view')?.addEventListener('click', _submitReview)
    _wireFhStrip('ingest-fh-strip')

    // Resize handle
    // The DETAILS panel is the sized side now, so it can be animated open and
    // shut; the form is flexible and absorbs whatever the panel is not using.
    wireResizablePanel(
      mainContent.querySelector('.ingest-review-shell'),
      document.getElementById('ingest-slide-panel'),
      document.getElementById('rev-divider'),
      304, 300, { side: 'right' }
    )
  }

  // fmtDur is shared by the review-step track table and the confirm summary.
  function fmtDur(s) {
    if (!s) return '—'
    const m = Math.floor(s / 60), sec = Math.floor(s % 60)
    return `${m}:${String(sec).padStart(2,'0')}`
  }

  // Step 4 ("Confirm & Add to Library") removed 2026-07-15 — Ryan: "never
  // very useful, nothing is ever something i need to change... doesn't look
  // great." The review step's "Add Recording →" button now submits directly
  // (see its click handler above) instead of navigating to a separate
  // summary-then-confirm screen. File behavior (copy/move) moved from a
  // per-add choice to a standing preference (Settings ⚙).

  // ── Step 5: Success ────────────────────────────────────────────────────────

  function renderIngestSuccess() {
    const result = ingest._lastResult || {}
    setMainHTML(`
      <div class="ingest-view">
        <div class="success-state">
          <div class="success-icon">${icon('check')}</div>
          <div class="success-title">Recording added to library</div>
          <div class="success-sub">${esc(ingest.form.artist_name)} · ${fmtDate(ingest.form.start_year, ingest.form.start_month, ingest.form.start_day)}</div>
          <div style="display:flex; gap:10px; margin-top:20px">
            <button class="btn btn-primary" id="btn-view-recording">View recording</button>
            <button class="btn btn-ghost" id="btn-add-another">Add another</button>
          </div>
        </div>
      </div>`)

    document.getElementById('btn-view-recording').addEventListener('click', () => {
      if (!result.recording_id) return
      // Refresh the sidebar (new artist/venue/musician may exist) then navigate.
      loadArtistList().then(() => {
        window.location.hash = `#/recording/${result.recording_id}`
      })
    })

    document.getElementById('btn-add-another').addEventListener('click', () => {
      // Reset wizard
      ingest.step = 'folder'
      ingest.scan = null
      ingest.folderPath = null
      ingest.form = {}
      ingest.tracks = []
      renderIngestStep()
      loadArtistList()  // refresh sidebar counts
    })
  }

  // ── Player integration ─────────────────────────────────────────────────────

  async function playRecording(recId, startIdx, preloadedTracks, opts) {
    let tracks  = preloadedTracks
    let recData = null
    try {
      recData = await API.recordings.get(recId)
      if (!tracks) tracks = recData.tracks
    } catch (e) { return }

    // Build meta string: Artist · Date · Venue (studio: Title · Year)
    const artist = state.selectedArtist?.name || ''
    const perfId = recData?.performance_id
    const isStudio = recData?.kind === 'studio'
    let dateStr = '', venueStr = '', sourceStr = '', artistName = '', studioMeta = ''
    if (perfId) {
      try {
        const perf = await API.performances.get(perfId)
        dateStr       = perf ? fmtDateLong(perf.start_year, perf.start_month, perf.start_day) : ''
        venueStr      = perf?.venue_name || ''
        artistName = perf?.artist  || ''
        if (isStudio) {
          const id = recIdentity({ kind: 'studio', title: recData?.title, artist: artistName, start_year: perf?.start_year })
          // No title: year alone, never the artist again -- it's already
          // shown on line 3 as recLabel below.
          studioMeta = recData?.title ? [id.lead, id.dateText].filter(Boolean).join(' · ') : (id.dateText || '')
        }
      } catch (_) {}
    }
    if (recData) {
      sourceStr = recData.source || ''
    }
    // Player bar line 2: Date · Venue (artist name is redundant here — it's
    // shown on line 3). Line 3: the artist/band name. Studio: Title · Year,
    // never a venue (studio releases don't have one) and never source --
    // a studio record has none, so falling back to it would be reading a
    // stale value off a recording that used to be a live show.
    const metaParts = [dateStr, venueStr].filter(Boolean)
    const meta      = isStudio ? (studioMeta || '—') : (metaParts.join(' · ') || sourceStr || '—')
    const recLabel  = artistName || artist || ''

    // Filter out non-music tracks when the skip toggle is on
    const startTrack   = tracks[startIdx]
    const queueTracks  = state.skipNonMusic
      ? tracks.filter(t => !(t.flags || []).some(f => NON_MUSIC_FLAGS.includes(f)))
      : tracks
    // Find equivalent start position in (possibly filtered) queue
    let queueStart = 0
    if (startTrack) {
      const pos = queueTracks.findIndex(t => t.id === startTrack.id)
      queueStart = pos >= 0 ? pos : 0
    }

    const queue = queueTracks.map(t => ({
      id:          t.id,
      title:       t.title,
      duration:    t.duration,
      streamUrl:   t.stream_url,
      recordingId: recId,
      meta,
      recLabel,
    }))

    Player.loadQueue(queue, queueStart, opts)
  }

  /** Sync every play/pause icon to REAL Player state — both "is this the
   * loaded track" AND "is it actually playing", not just the former. Called
   * on track load (via onTrackChange) and on every play/pause of a track
   * that was already loaded (via Player's audio listeners calling this
   * directly) — a row that only asked "is this the loaded track" stayed
   * stuck on the pause icon forever once paused (Ryan, 2026-08-27). */
  function syncPlayButtons() {
    const activeId = Player.currentId()
    const playing  = activeId != null && Player.isPlaying()

    document.querySelectorAll('.track-row[data-track-id]').forEach(el => {
      const isActive = parseInt(el.dataset.trackId) === activeId && playing
      el.classList.toggle('playing', isActive)
      el.querySelector('.track-play').innerHTML = icon(isActive ? 'pause' : 'play')
    })

    // Same, but for the track table in the Edit Recording view
    document.querySelectorAll('.et-row').forEach(el => {
      const isActive = parseInt(el.dataset.id) === activeId && playing
      el.classList.toggle('playing', isActive)
      const playBtn = el.querySelector('.et-play')
      if (playBtn) playBtn.innerHTML = icon(isActive ? 'pause' : 'play')
    })
  }

  /** Called by Player when the track changes (for highlighting in the track list) */
  function onTrackChange(trackId) {
    state.playingTrackId = trackId
    syncPlayButtons()

    // Switch the wavesurfer waveform to the new track's peaks if we have
    // analysis data for it (mirrors the old canvas's track-follow
    // behaviour). No network fetch — same precomputed peaks used to render
    // the banner in the first place.
    if (_wsInstance && trackId !== _wsTrackId) {
      const peaks = _peaksForTrack(trackId)
      const duration = _trackDurationMap[trackId]
      if (peaks && duration) {
        _wsInstance.load('', peaks, duration)
        _wsTrackId = trackId
      }
    }
  }

  // Venue page — editable name / location / bio in place + performances.
  async function renderVenueView(id) {
    setActiveNav('venues'); setActiveArtist(null); setLoading()
    let v
    try { v = await API.venues.get(id) }
    catch (e) {
      invalidateDims('venues')
      setMainHTML(`<div class="empty-state"><div class="empty-title">This venue no longer exists</div></div>`)
      return
    }
    setNavCurrent(v.name)
    const navBack = state.navBack   // see the Artist page's identical comment
    const descText = v.bio && v.bio.trim()
    // One row per Recording at this venue (showing the artist, since a venue
    // hosts many different acts). Already ordered chronologically by the API.
    const venueRows = v.recordings || []
    const rowsHtml = venueRows.map(r => flatRowHtml(r, true)).join('')

    const photoCount = (v.images || []).length
    const loc = fmtLocation(v.city, v.state, v.country)

    setMainHTML(entityShellHtml({
      navBack,
      pageClass: 'venue-page',      // square portrait + square gallery tiles
      portrait: '<div id="vn-portrait"></div>',
      title: esc(v.name),
      titleId: 'vn-name',
      titleEditable: true,
      chips: loc ? `<span class="pp-hero-fact">${esc(loc)}</span>` : '',
      stats: [
        [venueRows.length, venueRows.length === 1 ? 'Recording' : 'Recordings'],
        [v.performance_count || 0, (v.performance_count === 1) ? 'Show' : 'Shows'],
      ],
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="vn-delete" title="Delete venue">Delete</button>`,
      // TAB ORDER, all five dimension pages (Ryan, 2026-09-01): Recordings
      // first and default, then About, then Photos. What you came for is the
      // shows; "Overview" opened every page on a form and, on most records,
      // on an empty one — the Venue page's Notes field is blank for nearly
      // every hall in the library. Renamed About because the pane is prose
      // and facts ABOUT the record, not an overview of it.
      tabs: [
        { id: 'recordings', label: 'Recordings', count: venueRows.length, active: true,
          // showArtist: true — a venue hosts many different acts, so the row
          // must name who played. The Artist page omits it for the reverse
          // reason.
          html: recordingsPaneHtml(venueRows, { showArtist: true, mountId: 'rec-table-venue',
                                                empty: 'No recordings from this venue yet' }) },
        { id: 'about', label: 'About', html: `
            <div class="pp-sec-row">
              <div class="pp-sec">History</div>
              <span id="vn-lx-hist"></span>
            </div>
            <div class="pp-desc pp-editable ${v.history && v.history.trim() ? '' : 'pp-empty'}" id="vn-history" title="Click to edit">${v.history && v.history.trim() ? esc(v.history) : 'Add history\u2026'}</div>

            <div class="pp-sec">Location</div>
            <div class="vn-loc">
              <span class="vn-field"><label>City</label><span class="pp-editable vn-val ${v.city ? '' : 'pp-empty'}" id="vn-city">${v.city ? esc(v.city) : '\u2014'}</span></span>
              <span class="vn-field"><label>State / Region</label><span class="pp-editable vn-val ${v.state ? '' : 'pp-empty'}" id="vn-state">${v.state ? esc(v.state) : '\u2014'}</span></span>
              <span class="vn-field"><label>Country</label><span class="pp-editable vn-val ${v.country ? '' : 'pp-empty'}" id="vn-country">${v.country ? esc(v.country) : '\u2014'}</span></span>
            </div>

            <div class="lx-slot" id="vn-lx-loc"></div>
            <div class="lx-slot" id="vn-lx-names"></div>

            <div class="pp-sec">Notes</div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="vn-bio" title="Click to edit">${descText ? esc(v.bio) : 'Add notes\u2026'}</div>

            <!-- Questions and the ask bar close the page (not pinned). -->
            <div class="lx-page" id="vn-lx"></div>` },
        { id: 'photos', label: 'Photos', count: photoCount || null,
          html: '<div id="vn-photos"></div>' },
      ],
    }))
    wireEntityShell(mainContent, navBack)
    wireRecordingRows(mainContent)
    if (venueRows.length) wireDateAddedSort(document.getElementById('rec-table-venue'), venueRows, true)

    // Photos — the shared gallery. No automatic fetch tile: the Wikidata bridge
    // runs through the Artist's MusicBrainz match and a venue has no
    // equivalent. It does get the two SEARCH tiles every photographed entity
    // now carries (2026-09-01) — they open a search rather than importing
    // anything, so they need no licence bridge to be honest.
    const vnPortrait = () => {
      const el = document.getElementById('vn-portrait')
      if (!el) return
      const primary = (v.images || [])[0]
      el.innerHTML = heroPortraitHtml(v.name, primary ? API.venues.imageUrl(primary.id) : null)
    }
    createPhotoGallery({
      mountId: 'vn-photos', api: API.venues, entityId: id, images: v.images || [],
      // Location as the qualifier: "Fillmore" alone is a San Francisco
      // district before it is a hall.
      linkTiles: photoSearchTiles(v.name, fmtLocation(v.city, v.state, v.country)),
      onChange: imgs => {
        v.images = imgs
        vnPortrait()
        const tab = mainContent.querySelector('.pp-tab[data-pane="photos"]')
        if (tab) tab.innerHTML = 'Photos' + (imgs.length ? `<span class="pp-tab-n">${imgs.length}</span>` : '')
      },
    })

    const refreshSidebar = () => invalidateDims('venues')
    async function saveField(patch) {
      try { await API.venues.update(id, patch); refreshSidebar() }
      catch (e) { alert('Save failed: ' + e.message) }
    }
    makeInlineEditable(document.getElementById('vn-name'), {
      tabTo: shift => shift ? null : 'vn-city',
      get: () => v.name,
      onSave: async val => { val = val.trim(); if (!val || val === v.name) return; v.name = val; await saveField({ name: val }) },
    })
    ;['city', 'state', 'country'].forEach((f, i, arr) => {
      makeInlineEditable(document.getElementById('vn-' + f), {
        placeholder: '\u2014',
        get: () => v[f] || '',
        onSave: async val => { val = val.trim(); v[f] = val; await saveField({ [f]: val || null }) },
        // Forward: City → State → Country → Notes. Shift-Tab walks back up.
        tabTo: shift => shift
          ? (i > 0 ? 'vn-' + arr[i - 1] : 'vn-name')
          : (i < arr.length - 1 ? 'vn-' + arr[i + 1] : 'vn-bio'),
      })
    })
    makeInlineEditable(document.getElementById('vn-bio'), {
      multiline: true, placeholder: 'Add notes…',
      get: () => v.bio || '',
      onSave: async val => { val = val.trim(); v.bio = val; await saveField({ bio: val || null }) },
    })

    // ── Lomax: history, location, former names, questions ─────────────────────
    // History is auto-applied by the server (Restore previous undoes it);
    // location corrections and former names are suggestions to accept.
    {
      const lxHist = document.getElementById('vn-lx-hist')
      const lxLoc = document.getElementById('vn-lx-loc')
      const lxNames = document.getElementById('vn-lx-names')
      const lxAsk = document.getElementById('vn-lx')
      const alive = () => document.body.contains(lxAsk)
      const LOC = [['city', 'City'], ['state', 'State / Region'], ['country', 'Country']]
      const paintLoc = () => {
        for (const [f] of LOC) {
          const el = document.getElementById('vn-' + f)
          if (!el || el.querySelector('input')) continue
          el.textContent = v[f] || '—'
          el.classList.toggle('pp-empty', !v[f])
        }
      }
      const paintHist = () => {
        const el = document.getElementById('vn-history')
        if (!el || el.querySelector('textarea, input')) return
        el.textContent = v.history || (canEditLibrary() ? 'Add history…' : '')
        el.classList.toggle('pp-empty', !v.history)
      }
      const parsed = p => { try { return JSON.parse(p.proposed) || {} } catch (_) { return {} } }
      const c = lomaxController({
        skill: 'venue', subjectType: 'venue', subjectId: id, alive,
        afterAccept: async props => {
          if (props.some(p => LOC.some(([f]) => f === p.field))) {
            const fresh = await API.venues.get(id)
            for (const [f] of LOC) v[f] = fresh[f]
            paintLoc()
          }
        },
        afterRestore: run => { v.history = (run.result && run.result.replaced && run.result.replaced.text) || ''; paintHist() },
        onDone: run => {
          const rep = run.result && run.result.replaced
          if (rep && rep.written) { v.history = rep.written; paintHist() }
        },
      })
      c.addView(ctl => {
        if (!alive()) return false
        const rep = ctl.runs.slice().reverse().find(r => r.status === 'done' && r.result && r.result.replaced)
        lxRepaint(lxHist, rep ? lomaxRestoreLink(ctl, rep, v.history) : '')

        const run = ctl.latest()
        const edit = canEditLibrary()
        const same = (a, b) => String(a || '').trim().toLowerCase() === String(b || '').trim().toLowerCase()
        const locProps = edit ? lxProps(run).filter(p => LOC.some(([f]) => f === p.field) && !p.agrees && lxShown(p) &&
          (p.decision === 'accepted' || !same(v[p.field], p.proposed))) : []
        lxRepaint(lxLoc, locProps.length ? `<table class="lx-tbl lx-sugg"><thead><tr><th>Field</th><th>Now</th><th>Lomax</th><th></th></tr></thead><tbody>${
          locProps.map(p => `<tr><th scope="row">${esc(LOC.find(([f]) => f === p.field)[1])}</th><td><span class="lx-tv">${esc(v[p.field] || '')}</span></td>` +
            `<td>${lomaxSuggestionCell(p)}</td><td class="lx-act-td">${lomaxActions(p)}</td></tr>`).join('')}</tbody></table>` : '')

        const names = edit ? lxProps(run).filter(p => p.field === 'former_name' && lxShown(p)) : []
        lxRepaint(lxNames, names.length ? `<div class="pp-sec">Former names</div><table class="lx-tbl lx-sugg"><tbody>${names.map(p => {
          const n = parsed(p), quiet = p.decision === 'accepted'
          const years = (n.from || n.to) ? `${n.from || '?'} – ${n.to || '?'}` : ''
          return `<tr><td><span class="${quiet ? 'lx-same' : 'lx-val'}">${esc(n.name || '')}</span></td>` +
            `<td><span class="${quiet ? 'lx-same' : 'lx-val'}">${esc(years)}</span></td>` +
            `<td class="lx-act-td">${lomaxActions(p, { accept: 'Add as alias' })}</td></tr>`
        }).join('')}</tbody></table>` : '')

        const asked = ctl.runs.filter(r => r.question && r.status === 'done' && r.result && String(r.result.answer || '').trim())
        const err = ctl.lastError()
        const bar = lomaxAskBar({ placeholder: 'Anything else you want to learn? (optional)',
          note: "Lomax researches this venue's history, former names and location. Add anything else below.",
          last: run, busy: !!ctl.pending })
        lxRepaint(lxAsk, !(asked.length || ctl.pending || err || bar) ? '' : `<div class="lx-ask-wrap">
          ${asked.length ? `<div class="pp-sec">Questions</div><div class="lx-qs">${asked.map(r =>
            `<div class="lx-qa"><div class="lx-qa-q">${esc(r.question)}</div><div class="lx-answer">${esc(stripCitations(r.result.answer))}</div>` +
            `<div class="lx-qa-d">${esc(lxDay(r.created_at))}</div></div>`).join('')}</div>` : ''}
          ${ctl.pending ? lomaxRunState(ctl.pending) : (err ? `<div class="lx-error" role="alert">${esc(err)}</div>` : '')}
          ${bar}
        </div>`)
        return true
      })
      for (const el of [lxHist, lxLoc, lxNames, lxAsk]) if (el) el.setAttribute('data-lx-ctl', c.id)
      c.refresh()
      c.load()
    }

    makeInlineEditable(document.getElementById('vn-history'), {
      multiline: true, placeholder: 'Add history…',
      get: () => v.history || '',
      onSave: async val => { val = val.trim(); v.history = val; await saveField({ history: val || null }) },
    })

    onAdminClick('vn-delete', async () => {
      if (!confirm(`Delete venue "${v.name}"? This can't be undone.`)) return
      try { await API.venues.remove(id); refreshSidebar(); window.location.hash = '#/venues' }
      catch (e) { alert(e.message) }
    })
  }

  // ══ Event page ═════════════════════════════════════════════════════════════
  //
  // The fifth dimension page (Ryan, 2026-09-01). Event has been in the schema
  // since the beginning and reachable from nowhere: the ingest form's
  // autocomplete could CREATE one, and after that it existed only as a foreign
  // key. `GET /api/events/<id>` raised AttributeError the moment a performance
  // was attached to it and nobody noticed for months, because nothing ever
  // called it.
  //
  // Built on the same shell, the same tabs and the same editing helpers as
  // Venue — an event is very nearly a venue with dates, and every difference
  // that isn't the dates would just be inconsistency.
  async function renderEventView(id) {
    setActiveNav('events'); setActiveArtist(null); setLoading()
    let e
    try { e = await API.events.get(id) }
    catch (err) {
      invalidateDims('events')
      setMainHTML(`<div class="empty-state"><div class="empty-title">This event no longer exists</div></div>`)
      return
    }
    setNavCurrent(e.name)
    const navBack = state.navBack
    const notesText = e.notes && e.notes.trim()
    const rows = e.recordings || []
    const photoCount = (e.images || []).length

    // An event's own location, or the anchor venue's. Performances inside an
    // event may override both (see Performance's location resolution order) —
    // this line is the event's default, not a claim about every show in it.
    const loc = fmtLocation(e.city, e.state, e.country)
    const dates = fmtEventDates(e)

    // One row per performance, since a festival's interesting shape is WHO
    // played and on what stage, which a flat recording list flattens away. The
    // Recordings tab beside it still gives the plain chronological list.
    const perfRowsHtml = (e.performances || []).map(p => `
      <div class="ev-perf">
        <span class="ev-perf-date">${esc(p.date || '—')}</span>
        <a class="ev-perf-name truncate" href="#/artist/${p.artist_id}">${esc(p.artist || 'Unknown artist')}</a>
        ${p.stage ? `<span class="ev-perf-stage">${esc(p.stage)}</span>` : ''}
        <span class="ev-perf-count">${p.recording_count ? _plural(p.recording_count, 'recording') : 'no recordings'}</span>
      </div>`).join('')

    setMainHTML(entityShellHtml({
      navBack,
      pageClass: 'venue-page',    // square portrait + square gallery tiles — an
                                  // event is a poster or a gate shot, not a face
      portrait: '<div id="ev-portrait"></div>',
      title: esc(e.name),
      titleId: 'ev-name',
      titleEditable: true,
      chips: [dates ? `<span class="pp-hero-fact">${esc(dates)}</span>` : '',
              e.venue_name ? `<a class="pp-hero-fact" href="#/venue/${e.venue_id}">${esc(e.venue_name)}</a>`
                           : (loc ? `<span class="pp-hero-fact">${esc(loc)}</span>` : '')].join(''),
      stats: [
        [rows.length, rows.length === 1 ? 'Recording' : 'Recordings'],
        [e.performance_count || 0, (e.performance_count === 1) ? 'Show' : 'Shows'],
      ],
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="ev-delete" title="Delete event">Delete</button>`,
      tabs: [
        { id: 'recordings', label: 'Recordings', count: rows.length, active: true,
          // showArtist: true — an event holds many different acts, same as a
          // venue, so the row has to name who played.
          html: recordingsPaneHtml(rows, { showArtist: true, mountId: 'rec-table-event',
                                           empty: 'No recordings from this event yet' }) },
        { id: 'about', label: 'About', html: `
            <div class="pp-sec">Dates</div>
            <div class="vn-loc">
              <span class="vn-field"><label>Start</label><span class="pp-editable vn-val ${e.start_year ? '' : 'pp-empty'}" id="ev-start">${
                e.start_year ? esc(_evDateStr(e, 'start')) : '—'}</span></span>
              <span class="vn-field"><label>End</label><span class="pp-editable vn-val ${e.end_year ? '' : 'pp-empty'}" id="ev-end">${
                e.end_year ? esc(_evDateStr(e, 'end')) : '—'}</span></span>
            </div>

            <div class="pp-sec">Location</div>
            <div class="vn-loc">
              <span class="vn-field"><label>Venue</label><span class="pp-editable vn-val ${e.venue_id ? '' : 'pp-empty'}" id="ev-venue" title="Click to edit">${
                e.venue_name ? esc(e.venue_name) : 'Link a venue…'}</span></span>
              <span class="vn-field"><label>City</label><span class="pp-editable vn-val ${e.city ? '' : 'pp-empty'}" id="ev-city">${e.city ? esc(e.city) : '—'}</span></span>
              <span class="vn-field"><label>State / Region</label><span class="pp-editable vn-val ${e.state ? '' : 'pp-empty'}" id="ev-state">${e.state ? esc(e.state) : '—'}</span></span>
              <span class="vn-field"><label>Country</label><span class="pp-editable vn-val ${e.country ? '' : 'pp-empty'}" id="ev-country">${e.country ? esc(e.country) : '—'}</span></span>
            </div>
            <div class="pp-block-hint">A show inside this event can override any of these with its own venue or city. This is the event's default, not a claim about every night.</div>

            <div class="pp-sec">Shows</div>
            ${perfRowsHtml || '<div class="pp-empty">No performances linked to this event yet.</div>'}

            <div class="pp-sec">Notes</div>
            <div class="pp-desc pp-editable ${notesText ? '' : 'pp-empty'}" id="ev-notes" title="Click to edit">${notesText ? esc(e.notes) : 'Add notes…'}</div>` },
        { id: 'photos', label: 'Photos', count: photoCount || null,
          html: '<div id="ev-photos"></div>' },
      ],
    }))
    wireEntityShell(mainContent, navBack)
    wireRecordingRows(mainContent)
    if (rows.length) wireDateAddedSort(document.getElementById('rec-table-event'), rows, true)

    const evPortrait = () => {
      const el = document.getElementById('ev-portrait')
      if (!el) return
      const primary = (e.images || [])[0]
      el.innerHTML = heroPortraitHtml(e.name, primary ? API.events.imageUrl(primary.id) : null)
    }
    createPhotoGallery({
      mountId: 'ev-photos', api: API.events, entityId: id, images: e.images || [],
      // "festival" reads better than the year for a search: the year is usually
      // already in the event's own name ("Bonnaroo 2009").
      linkTiles: photoSearchTiles(e.name, 'festival concert'),
      onChange: imgs => {
        e.images = imgs
        evPortrait()
        const tab = mainContent.querySelector('.pp-tab[data-pane="photos"]')
        if (tab) tab.innerHTML = 'Photos' + (imgs.length ? `<span class="pp-tab-n">${imgs.length}</span>` : '')
      },
    })

    const refreshSidebar = () => invalidateDims('events')
    async function saveField(patch) {
      try { await API.events.update(id, patch); refreshSidebar() }
      catch (err) { alert('Save failed: ' + err.message) }
    }

    makeInlineEditable(document.getElementById('ev-name'), {
      tabTo: shift => shift ? null : 'ev-start',
      get: () => e.name,
      onSave: async val => { val = val.trim(); if (!val || val === e.name) return; e.name = val; await saveField({ name: val }) },
    })

    // Dates are edited as ONE box each, not six. A partial date is the norm
    // here — a tour run often has only a year — and three number inputs per end
    // makes the common case ("1989") six times more work than typing it. The
    // parser takes 1989, 1989-06 or 1989-06-11 and returns nulls for whatever
    // was not supplied; anything it cannot read is rejected rather than
    // guessed, because a silently mis-parsed date is exactly the failure that
    // made "AI suggests, humans approve" a rule in this app.
    ;['start', 'end'].forEach((which, i) => {
      makeInlineEditable(document.getElementById('ev-' + which), {
        placeholder: '—',
        get: () => _evDateStr(e, which),
        tabTo: shift => shift ? (i ? 'ev-start' : 'ev-name') : (i ? 'ev-city' : 'ev-end'),
        onSave: async val => {
          const parsed = _parsePartialDate(val)
          if (parsed === undefined) { alert(`Couldn't read "${val}" as a date. Use 1989, 1989-06 or 1989-06-11.`); return }
          const [y, m, d] = parsed
          e[which + '_year'] = y; e[which + '_month'] = m; e[which + '_day'] = d
          await saveField({ [which + '_year']: y, [which + '_month']: m, [which + '_day']: d })
        },
      })
    })

    ;['city', 'state', 'country'].forEach((f, i, arr) => {
      makeInlineEditable(document.getElementById('ev-' + f), {
        placeholder: '—',
        get: () => e[f] || '',
        onSave: async val => { val = val.trim(); e[f] = val; await saveField({ [f]: val || null }) },
        tabTo: shift => shift ? (i > 0 ? 'ev-' + arr[i - 1] : 'ev-end')
                              : (i < arr.length - 1 ? 'ev-' + arr[i + 1] : 'ev-notes'),
      })
    })

    makeInlineEditable(document.getElementById('ev-notes'), {
      multiline: true, placeholder: 'Add notes…',
      get: () => e.notes || '',
      onSave: async val => { val = val.trim(); e.notes = val; await saveField({ notes: val || null }) },
    })

    // ── Venue link — an existing-venues-only picker ──────────────────────────
    // Same shape as the Artist page's Genre field: a click-to-edit dropdown,
    // no "+ Create" row. Creating a venue as a side effect of typing here would
    // put a second, un-reviewed creation path on a dimension that already has a
    // real create form — and the ingest wizard has been burned by exactly that
    // before (placeholder venues). Clearing the box unlinks.
    const venueEl = document.getElementById('ev-venue')
    function showVenue() {
      if (!venueEl) return
      venueEl.className = 'pp-editable vn-val' + (e.venue_id ? '' : ' pp-empty')
      venueEl.textContent = e.venue_name || 'Link a venue…'
    }
    venueEl?.addEventListener('click', () => {
      if (!canEditLibrary() || venueEl.querySelector('input')) return
      venueEl.innerHTML = `<span class="artist-picker-wrap" style="display:inline-block; min-width:180px">
        <input type="text" class="pp-inline-input" id="ev-venue-input" value="${esc(e.venue_name || '')}" autocomplete="off" />
        <div class="artist-dropdown" id="ev-venue-dd" style="display:none"></div></span>`
      const input = document.getElementById('ev-venue-input')
      const dd    = document.getElementById('ev-venue-dd')
      input.focus(); input.select()
      let committed = false
      const commit = async ({ id: vid, name }) => {
        if (committed) return; committed = true
        try {
          await API.events.update(id, { venue_id: vid || null })
          e.venue_id = vid || null
          e.venue_name = vid ? name : null
        } catch (err) { alert('Failed: ' + err.message) }
        showVenue()
      }
      wirePickerDropdown(input, dd, q => API.venues.list(q), commit)   // no createLabel → no create row
      input.addEventListener('keydown', ev => {
        ev.stopPropagation()
        if (ev.key === 'Enter') {
          ev.preventDefault()
          // An emptied box means "unlink", which no dropdown row can express.
          if (!input.value.trim()) { commit({ id: null, name: null }); return }
          const m = firstPickerResult(dd); if (m) commit(m)
        } else if (ev.key === 'Escape') { committed = true; showVenue() }
      })
    })

    onAdminClick('ev-delete', async () => {
      if (!confirm(`Delete event "${e.name}"? This can't be undone.`)) return
      try { await API.events.remove(id); refreshSidebar(); window.location.hash = '#/events' }
      // A 409 here is the guard doing its job — performances still point at
      // this event — and its message names the fix, so show it as-is.
      catch (err) { alert(err.message) }
    })
  }

  // "1989-06-11" / "1989-06" / "1989" / "" — the round trip of _parsePartialDate.
  function _evDateStr(e, which) {
    const y = e[which + '_year'], m = e[which + '_month'], d = e[which + '_day']
    if (!y) return ''
    if (m && d) return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`
    if (m)      return `${y}-${String(m).padStart(2, '0')}`
    return String(y)
  }

  // Returns [year, month|null, day|null], or `undefined` for input it cannot
  // read — DISTINCT from [null,null,null], which is a deliberate clear. A
  // caller that conflates the two silently wipes a date when someone fat-fingers
  // one, so the two answers are deliberately different types.
  function _parsePartialDate(raw) {
    const v = (raw || '').trim()
    if (!v) return [null, null, null]
    const m = v.match(/^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?$/)
    if (!m) return undefined
    const year  = parseInt(m[1], 10)
    const month = m[2] ? parseInt(m[2], 10) : null
    const day   = m[3] ? parseInt(m[3], 10) : null
    if (month != null && (month < 1 || month > 12)) return undefined
    if (day   != null && (day   < 1 || day   > 31)) return undefined
    return [year, month, day]
  }

  // ── Genre (2026-08-02) ───────────────────────────────────────────────────────
  // A proper dimension — its own table, one FK from Artist — see the Genre
  // design spec in Context Library. Three surfaces: the #/genre/<id> page
  // (mirrors the Venue page, but a genre's "recordings" are reached through
  // its artists, one extra hop the Venue page doesn't need), the #/genres
  // INDEX (renderDimIndexPage, shared with the other four dimensions since
  // 2026-09-01 — it was a copy of #/venues' split list/detail admin screen
  // until then), and the bulk assignment screen (the actual population
  // mechanism — see the design spec's coverage math on why
  // sort-by-recording-count matters).

  async function renderGenreView(id) {
    setActiveNav('genres'); setActiveArtist(null); setLoading()
    let g
    try { g = await API.genres.get(id) }
    catch (e) {
      invalidateDims('genres')
      setMainHTML(`<div class="empty-state"><div class="empty-title">This genre no longer exists</div></div>`)
      return
    }
    setNavCurrent(g.name)
    const navBack = state.navBack
    const descText = g.description && g.description.trim()
    const artists = g.artists || []

    const perfSectionsHtml = artists.map(p => {
      return `
      <div class="genre-artist-section">
        <div class="genre-artist-head">
          <a class="genre-artist-name" href="#/artist/${p.id}">${esc(p.name)}</a>
          <span class="genre-artist-count">${p.recording_count} recording${p.recording_count !== 1 ? 's' : ''}</span>
        </div>
        <div class="rec-table">${p.recordings.map(r => flatRowHtml({ ...r, artist: p.name }, false)).join('')}</div>
      </div>`
    }).join('')

    setMainHTML(entityShellHtml({
      navBack,
      // Colour swatch instead of a portrait — a genre has no likeness, but it
      // does have the colour that tints every card of its artists, so
      // showing it here is both the identity and a live preview of the picker.
      portrait: `<div class="gn-swatch" style="--genre-fg:${esc(g.color || 'var(--t2)')}"></div>`,
      title: esc(g.name),
      titleId: 'gn-name',
      titleEditable: true,
      chips: g.color ? `<span class="pp-hero-fact">${esc(g.color)}</span>` : '',
      stats: [
        [g.artist_count || 0, (g.artist_count === 1) ? 'Artist' : 'Artists'],
        [g.recording_count || 0, (g.recording_count === 1) ? 'Recording' : 'Recordings'],
      ],
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="gn-delete" title="Delete genre">Delete</button>`,
      // No Photos tab (Ryan, 2026-08-07) — a genre has nothing to photograph.
      tabs: [
        { id: 'recordings', label: 'Recordings', count: g.recording_count || 0, active: true,
          html: artists.length
              ? perfSectionsHtml
              : `<div class="empty-state" style="min-height:160px"><div class="empty-title">No artists assigned to this genre yet</div><div class="empty-sub">Assign some from the <a href="#/genres/assign">bulk assignment screen</a>.</div></div>` },
        { id: 'about', label: 'About', html: `
            <div class="pp-sec">Description</div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="gn-desc" title="Click to edit">${descText ? esc(g.description) : 'Add a description\u2026'}</div>` },
      ],
    }))
    wireEntityShell(mainContent, navBack)
    wireRecordingRows(mainContent)

    const refreshSidebar = () => invalidateDims('genres')
    async function saveField(patch) {
      try { await API.genres.update(id, patch); refreshSidebar() }
      catch (e) { alert('Save failed: ' + e.message) }
    }
    makeInlineEditable(document.getElementById('gn-name'), {
      get: () => g.name,
      onSave: async val => { val = val.trim(); if (!val || val === g.name) return; g.name = val; await saveField({ name: val }) },
    })
    makeInlineEditable(document.getElementById('gn-desc'), {
      multiline: true, placeholder: 'Add a description…',
      get: () => g.description || '',
      onSave: async val => { val = val.trim(); g.description = val; await saveField({ description: val || null }) },
    })

    onAdminClick('gn-delete', async () => {
      if (!confirm(`Delete genre "${g.name}"? This can't be undone.`)) return
      try { await API.genres.remove(id); refreshSidebar(); window.location.hash = '#/genres' }
      catch (e) { alert(e.message) }
    })
  }

  // Ryan assigns all 164 artists' genres by hand — no AI suggestion (see
  // design spec). One row per artist, sorted by recording count DESC: the
  // library is a long tail (85 of 164 artists have exactly one recording),
  // so the top ~30 acts by recording count cover ~62% of the library. Sorted
  // this way, stopping early after ten minutes is a legitimate end state, not
  // an unfinished migration. Each pick is its own PUT — no bulk save button,
  // nothing lost by closing the tab mid-way.
  async function renderGenreAssignView() {
    setActiveNav('genres'); setActiveArtist(null)
    setNavCurrent('Assign Genres')
    setLoading()

    let artists = []
    try { artists = await API.artists.list() } catch (_) {}
    const sorted = artists.slice().sort((a, b) => (b.recording_count || 0) - (a.recording_count || 0))

    let showAll = false
    const rowsToShow = () => showAll ? sorted : sorted.filter(p => !p.genre_id)

    setMainHTML(`
      <div class="action-bar">
        <!-- Titled, not back-linked: the App Header's arrow is the way back
             from every view now (2026-08-22). -->
        <span style="font-size:13px; font-weight:500; color:var(--t0)">Assign Genres</span>
        <label class="genre-assign-toggle" style="margin-left:auto">
          <input type="checkbox" id="ga-show-all" /> Show all <span class="genre-assign-toggle-hint">(default: unassigned only)</span>
        </label>
      </div>
      <div class="genre-assign-hint">Sorted by recording count, descending. The top acts cover most of the library fastest. Each pick saves immediately.</div>
      <div class="genre-assign-list" id="genre-assign-list"></div>`)

    function rowHtml(p) {
      return `
        <div class="genre-assign-row" data-id="${p.id}">
          <span class="genre-assign-name truncate">${esc(p.name)}</span>
          <span class="genre-assign-count">${p.recording_count || 0} rec</span>
          <span class="artist-picker-wrap genre-assign-picker-wrap">
            <input type="text" class="genre-assign-input" id="ga-input-${p.id}" autocomplete="off"
                   value="${esc(p.genre_name || '')}" placeholder="Pick a genre…" />
            <div class="artist-dropdown" id="ga-dd-${p.id}" style="display:none"></div>
          </span>
          <span class="genre-assign-status" id="ga-status-${p.id}"></span>
        </div>`
    }

    function focusRowInput(list, idx) {
      const next = list[idx]
      if (next) document.getElementById(`ga-input-${next.id}`)?.focus()
    }

    function wireRows(list) {
      list.forEach((p, idx) => {
        const input    = document.getElementById(`ga-input-${p.id}`)
        const dd       = document.getElementById(`ga-dd-${p.id}`)
        const statusEl = document.getElementById(`ga-status-${p.id}`)
        if (!input) return
        // No one-shot "committed" guard here (unlike the venue/event/artist-
        // page pickers): this input stays live in place rather than being
        // swapped for a display element after a pick, specifically so a
        // mis-click can be corrected by just picking again.
        const commit = async ({ id, name }) => {
          statusEl.textContent = 'Saving…'
          try {
            await API.artists.update(p.id, { genre_id: id })
            p.genre_id = id; p.genre_name = name
            statusEl.textContent = 'Done'
            invalidateDims('genres')
            if (!showAll) {
              // The row falls out of the unassigned-only view — re-render and
              // focus advances to whatever now sits at the same index.
              renderRows()
              focusRowInput(rowsToShow(), idx)
            } else {
              input.value = name
              statusEl.textContent = ''
              focusRowInput(list, idx + 1)
            }
          } catch (e) {
            statusEl.textContent = 'Failed: ' + e.message
          }
        }
        wirePickerDropdown(input, dd, API.genres.list, commit)   // no createLabel → existing genres only
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') {
            e.preventDefault()
            const m = firstPickerResult(dd)
            if (m) commit(m)
          }
        })
      })
    }

    function renderRows() {
      const list = rowsToShow()
      const box = document.getElementById('genre-assign-list')
      box.innerHTML = list.length
        ? list.map(rowHtml).join('')
        : `<div class="empty-state" style="min-height:120px"><div class="empty-title">${showAll ? 'No artists yet' : 'Every artist has a genre. Nothing left to assign'}</div></div>`
      wireRows(list)
    }

    document.getElementById('ga-show-all').addEventListener('change', e => {
      showAll = e.target.checked
      renderRows()
    })

    renderRows()
  }

  // ══ Dimension index — one page, five dimensions ════════════════════════════
  //
  // Replaces three different answers to the same question (Ryan, 2026-09-01):
  //
  //   #/venues  and #/genres  were a split list/detail ADMIN screen — a narrow
  //     scrolling column beside an edit form, with inline styles, a Save
  //     button, and `prompt('Venue name:')` behind "+ New venue". It predated
  //     the entity shell and the create forms by months and had drifted into
  //     being a second, worse Venue page: two places to edit one record, and
  //     the one you reached from the sidebar was the wrong one.
  //   #/artists was a bare three-column text list with no page furniture, no
  //     photos, and — despite the hash — ARTISTS in it.
  //   Artists and Events had no index at all.
  //
  // So: one component, parameterised. Every dimension gets the same shell as
  // its own detail page (hero, stats, one pane), the same create form, the
  // same tile. Editing happens on the record, once, where it already happened
  // for Artist and Musician.
  //
  // Filtering and sorting are CLIENT-SIDE over the already-fetched list, and
  // that is a deliberate ceiling, not an oversight: every list endpoint here is
  // uncapped and full-library (the sidebar reads the same payloads on every
  // render), so the rows are in hand before the page paints. A server round
  // trip per keystroke would be slower and would buy nothing until the library
  // is an order of magnitude larger than it is.
  //
  // cfg:
  //   nav        string            — setActiveNav key
  //   title      string            — page title, plural
  //   singular   string            — for "+ New …" and the empty state
  //   load       fn -> rows        — the list endpoint
  //   hashFor    fn(row) -> hash   — the record's detail page
  //   newHash    string | null     — create form; null hides the + button
  //   shape      'round'|'square'|'swatch'
  //   art        fn(row) -> {url, color} — photo url and/or accent colour
  //   sub        fn(row) -> string  — the tile's second line (may be '')
  //   meta       fn(row) -> string  — the tile's third line, counts
  //   searchable fn(row) -> string  — haystack for the filter box
  //   sorts      [{id, label, cmp}] — first is the default
  //   stats      fn(rows) -> [[n,label], …]
  //   extraActions html | ''        — e.g. Genres' "Bulk assign"
  const _dimIndexSort = {}          // per-dimension, per-session sort choice

  async function renderDimIndexPage(cfg) {
    setActiveNav(cfg.nav)
    setActiveArtist(null)
    setNavCurrent(cfg.title)
    setLoading()

    let rows = []
    try { rows = await cfg.load() } catch (e) {
      // An index that fails to load must SAY so. CONTEXT.md's standing trap:
      // a failed fetch that renders as "None yet" is indistinguishable from an
      // empty library, and we have shipped that mistake more than once.
      setMainHTML(`<div class="empty-state">
        <div class="empty-title">Could not load ${esc(cfg.title.toLowerCase())}</div>
        <div class="empty-sub">${esc(e.message)}</div></div>`)
      return
    }

    const sorts = cfg.sorts
    let sortId = _dimIndexSort[cfg.nav] || sorts[0].id
    if (!sorts.some(s => s.id === sortId)) sortId = sorts[0].id
    let query = ''

    setMainHTML(entityShellHtml({
      title: esc(cfg.title),
      chips: `<span class="pp-hero-fact">${rows.length} ${esc(
                rows.length === 1 ? cfg.singular.toLowerCase() : cfg.title.toLowerCase())}</span>`,
      stats: cfg.stats ? cfg.stats(rows) : [],
      // Creating is an admin verb, so it rides `actions` and the shell drops it
      // in Playback mode on its own — see entityShellHtml. Nothing here may
      // wire it with a bare getElementById; onAdminClick exists for that.
      actions: cfg.newHash
        ? `<button class="btn btn-ghost btn-sm" id="dx-new">+ New ${esc(cfg.singular.toLowerCase())}</button>`
        : '',
      actionsPlayback: cfg.extraActionsPlayback || '',
      tabs: [{
        id: 'all', label: 'All',
        html: `
          <div class="dim-bar">
            <input type="text" class="dim-search" id="dx-search" autocomplete="off"
                   placeholder="Filter ${esc(cfg.title.toLowerCase())}…" />
            <div class="dim-sorts" id="dx-sorts">
              ${sorts.map(s => `<button class="dim-sort${s.id === sortId ? ' active' : ''}"
                     data-sort="${esc(s.id)}">${esc(s.label)}</button>`).join('')}
            </div>
            <span class="dim-count" id="dx-count"></span>
          </div>
          <div class="dim-grid dim-grid--${esc(cfg.shape)}" id="dx-grid"></div>`,
      }],
    }))
    wireEntityShell(mainContent, null)

    function tileHtml(row) {
      const art  = cfg.art(row)
      const sub  = cfg.sub(row)
      const meta = cfg.meta(row)
      // Initials, never a silhouette. Most entities in this library have no
      // photograph and never will, so the no-photo state IS the page's normal
      // appearance and has to look intentional.
      const initials = String(row.name || '?').split(/\s+/).filter(Boolean).slice(0, 2)
        .map(w => w[0]).join('').toUpperCase()
      const face = art.url
        ? `<img class="dim-tile-img" src="${art.url}" alt="" loading="lazy">`
        : `<span class="dim-tile-initials">${esc(initials)}</span>`
      return `
        <a class="dim-tile" href="${esc(cfg.hashFor(row))}"
           style="--dim-fg:${esc(art.color || 'var(--bd-1)')}">
          <span class="dim-tile-art">${cfg.shape === 'swatch' ? '' : face}</span>
          <span class="dim-tile-body">
            <span class="dim-tile-name truncate">${esc(row.name)}</span>
            ${sub  ? `<span class="dim-tile-sub truncate">${esc(sub)}</span>`   : ''}
            ${meta ? `<span class="dim-tile-meta truncate">${esc(meta)}</span>` : ''}
          </span>
        </a>`
    }

    function paint() {
      const q = query.trim().toLowerCase()
      const filtered = q
        ? rows.filter(r => cfg.searchable(r).toLowerCase().includes(q))
        : rows
      const sorted = filtered.slice().sort(sorts.find(s => s.id === sortId).cmp)

      const grid  = document.getElementById('dx-grid')
      const count = document.getElementById('dx-count')
      if (!grid) return
      // Three states, three messages. "No results for a filter" and "this
      // dimension is empty" are different facts and a shared "None yet" hides
      // which one you are looking at.
      grid.innerHTML = sorted.length
        ? sorted.map(tileHtml).join('')
        : `<div class="dim-empty">${rows.length
            ? `Nothing matches “${esc(query.trim())}”.`
            : `No ${esc(cfg.title.toLowerCase())} yet.`}</div>`
      count.textContent = q && rows.length
        ? `${sorted.length} of ${rows.length}`
        : ''
    }

    const search = document.getElementById('dx-search')
    search.addEventListener('input', e => { query = e.target.value; paint() })
    document.getElementById('dx-sorts').addEventListener('click', e => {
      const btn = e.target.closest('.dim-sort')
      if (!btn) return
      sortId = btn.dataset.sort
      _dimIndexSort[cfg.nav] = sortId
      mainContent.querySelectorAll('.dim-sort').forEach(b =>
        b.classList.toggle('active', b === btn))
      paint()
    })
    onAdminClick('dx-new', () => { window.location.hash = cfg.newHash })

    paint()
  }

  // Shared comparators. `byName` sorts on the DISPLAYED name rather than
  // sort_name — ⚠ artist.sort_name and musician.sort_name are NULL for every
  // row in this library (CONTEXT.md trap; the backfill script has never been
  // run here), so sorting on it alone ties every row and falls back to whatever
  // order SQLite felt like. The API already applies COALESCE server-side; this
  // is the client-side half of the same rule.
  const _byName  = (a, b) => (a.sort_name || a.name).localeCompare(b.sort_name || b.name)
  const _byCount = key => (a, b) => (b[key] || 0) - (a[key] || 0) || _byName(a, b)

  const _plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

  // ── Venues ─────────────────────────────────────────────────────────────────
  function renderVenuesPage() {
    return renderDimIndexPage({
      nav: 'venues', title: 'Venues', singular: 'Venue',
      load: () => API.venues.list(),
      hashFor: v => `#/venue/${v.id}`,
      newHash: '#/venue/new',
      // Square, not round: a hall is a building. The Venue detail page has made
      // the same distinction since 2026-08-07 via its `venue-page` pageClass.
      shape: 'square',
      art:  v => ({ url: v.image_id ? API.venues.imageUrl(v.image_id) : null }),
      sub:  v => fmtLocation(v.city, v.state, v.country) || '',
      meta: v => [v.recording_count ? _plural(v.recording_count, 'recording') : '',
                  v.performance_count ? _plural(v.performance_count, 'show') : '']
                 .filter(Boolean).join(' · '),
      searchable: v => [v.name, v.city, v.state, v.country].filter(Boolean).join(' '),
      sorts: [
        { id: 'name',  label: 'A–Z',        cmp: _byName },
        { id: 'recs',  label: 'Recordings', cmp: _byCount('recording_count') },
        { id: 'shows', label: 'Shows',      cmp: _byCount('performance_count') },
      ],
      stats: rows => [
        [rows.length, rows.length === 1 ? 'Venue' : 'Venues'],
        [rows.reduce((n, v) => n + (v.recording_count || 0), 0), 'Recordings'],
      ],
    })
  }

  // ── Artists (acts) ──────────────────────────────────────────────────────
  function renderArtistsIndexPage() {
    return renderDimIndexPage({
      nav: 'artists', title: 'Artists', singular: 'Artist',
      load: () => API.artists.list(),
      hashFor: p => `#/artist/${p.id}`,
      newHash: '#/artist/new',
      shape: 'round',
      // Genre colour is the accent — CONTEXT.md: it is the most complete visual
      // signal the library owns, far more so than photographs.
      art: p => ({ url: p.image_id ? API.artists.imageUrl(p.image_id) : null,
                   color: p.genre_color || null }),
      sub:  p => (p.members || []).join(', '),
      meta: p => [p.genre_name || '',
                  p.recording_count ? _plural(p.recording_count, 'recording') : '']
                 .filter(Boolean).join(' · '),
      searchable: p => [p.name, p.genre_name, ...(p.members || [])].filter(Boolean).join(' '),
      sorts: [
        { id: 'name', label: 'A–Z',        cmp: _byName },
        { id: 'recs', label: 'Recordings', cmp: _byCount('recording_count') },
      ],
      stats: rows => [
        [rows.length, rows.length === 1 ? 'Artist' : 'Artists'],
        [rows.reduce((n, p) => n + (p.recording_count || 0), 0), 'Recordings'],
      ],
    })
  }

  // ── Musicians (people) ───────────────────────────────────────────────────────
  function renderMusiciansIndexPage() {
    return renderDimIndexPage({
      nav: 'musicians', title: 'Musicians', singular: 'Musician',
      load: () => API.musicians.list(),
      hashFor: a => `#/musician/${a.id}`,
      newHash: '#/musician/new',
      shape: 'round',
      art: a => ({ url: a.image_id ? API.musicians.imageUrl(a.image_id) : null }),
      sub:  a => a.artist_count ? _plural(a.artist_count, 'artist') : '',
      meta: a => a.recording_count ? _plural(a.recording_count, 'recording') : '',
      searchable: a => [a.name, a.sort_name].filter(Boolean).join(' '),
      sorts: [
        { id: 'name', label: 'A–Z',        cmp: _byName },
        { id: 'recs', label: 'Recordings', cmp: _byCount('recording_count') },
      ],
      stats: rows => [
        [rows.length, rows.length === 1 ? 'Musician' : 'Musicians'],
        [rows.filter(a => a.recording_count).length, 'On record'],
      ],
    })
  }

  // ── Genres ─────────────────────────────────────────────────────────────────
  function renderGenresPage() {
    return renderDimIndexPage({
      nav: 'genres', title: 'Genres', singular: 'Genre',
      load: () => API.genres.list(),
      hashFor: g => `#/genre/${g.id}`,
      newHash: '#/genre/new',
      // A genre has no likeness — the tile's face IS its colour, which is also
      // a live preview of what tints every card its artists appear on.
      shape: 'swatch',
      art:  g => ({ color: g.color || 'var(--t3)' }),
      sub:  g => g.description || '',
      meta: g => [g.artist_count ? _plural(g.artist_count, 'artist') : '',
                  g.recording_count ? _plural(g.recording_count, 'recording') : '']
                 .filter(Boolean).join(' · '),
      searchable: g => [g.name, g.description].filter(Boolean).join(' '),
      sorts: [
        { id: 'name', label: 'A–Z',        cmp: _byName },
        { id: 'recs', label: 'Recordings', cmp: _byCount('recording_count') },
        { id: 'acts', label: 'Artists', cmp: _byCount('artist_count') },
      ],
      stats: rows => [
        [rows.length, rows.length === 1 ? 'Genre' : 'Genres'],
        [rows.reduce((n, g) => n + (g.recording_count || 0), 0), 'Recordings'],
      ],
      // Bulk assign is the actual population mechanism for this dimension (see
      // the Genre design spec's coverage math), so it belongs on the index
      // rather than buried. It IS admin-only — it writes — but it is gated by
      // ADMIN_ONLY_HASHES in route(), which bounces a listener who arrives by
      // any door, so it does not also need the shell's `actions` gate.
      extraActionsPlayback: canEditLibrary()
        ? `<a class="btn btn-ghost btn-sm" href="#/genres/assign">Bulk assign →</a>` : '',
    })
  }

  // ── Events ─────────────────────────────────────────────────────────────────
  function renderEventsPage() {
    return renderDimIndexPage({
      nav: 'events', title: 'Events', singular: 'Event',
      load: () => API.events.list(),
      hashFor: e => `#/event/${e.id}`,
      newHash: '#/event/new',
      shape: 'square',
      art:  e => ({ url: e.image_id ? API.events.imageUrl(e.image_id) : null }),
      sub:  e => [fmtEventDates(e), e.venue_name || fmtLocation(e.city, e.state, e.country)]
                 .filter(Boolean).join(' · '),
      meta: e => [e.recording_count ? _plural(e.recording_count, 'recording') : '',
                  e.performance_count ? _plural(e.performance_count, 'show') : '']
                 .filter(Boolean).join(' · '),
      searchable: e => [e.name, e.city, e.state, e.country, e.venue_name,
                        e.start_year].filter(Boolean).join(' '),
      sorts: [
        { id: 'name', label: 'A–Z',        cmp: _byName },
        // Newest first — an event list is a timeline, and the thing you want is
        // almost always the recent one. Undated events sort last rather than
        // first: 0 would put every unlabelled row above 2009.
        { id: 'date', label: 'Newest',     cmp: (a, b) =>
            (b.start_year || 0) - (a.start_year || 0) ||
            (b.start_month || 0) - (a.start_month || 0) ||
            (b.start_day || 0) - (a.start_day || 0) || _byName(a, b) },
        { id: 'recs', label: 'Recordings', cmp: _byCount('recording_count') },
      ],
      stats: rows => [
        [rows.length, rows.length === 1 ? 'Event' : 'Events'],
        [rows.reduce((n, e) => n + (e.recording_count || 0), 0), 'Recordings'],
      ],
    })
  }

  // "2009" / "June 2009" / "11–14 June 2009" / "2009–2011". Partial dates are
  // the norm here, not the exception — a tour run often has only years — so
  // every component is optional and the formatter degrades rather than
  // pretending to a precision the row does not have.
  function fmtEventDates(e) {
    // Guard on the YEAR rather than calling fmtDate blind: fmtDate answers
    // 'Unknown date' for a null year, which is right in a recording row (where
    // a missing date is a gap) and wrong here (where it is just a tour with no
    // dates recorded, and the honest rendering is nothing at all).
    const a = e.start_year ? fmtDateLong(e.start_year, e.start_month, e.start_day) : ''
    const b = e.end_year   ? fmtDateLong(e.end_year,   e.end_month,   e.end_day)   : ''
    if (a && b && a !== b) return `${a} – ${b}`
    return a || b || ''
  }

  // ── Router ─────────────────────────────────────────────────────────────────

  // Hash of the page route() last actually dispatched to — module-scope, not
  // state.*, since this is purely a "have we already been here" bookkeeping
  // detail for the navBack snapshot below, not app state anything else reads.
  let _lastRouteHash = null

  // ── App-header navigation ────────────────────────────────────────────────
  //
  // Its own stack rather than history.back()/forward(). Two reasons:
  //
  //   1. The browser gives no way to ask whether a forward entry exists, so a
  //      forward arrow driven by history.forward() can only ever be permanently
  //      enabled — and an arrow that is always lit and usually does nothing is
  //      worse than no arrow. With our own stack both buttons can be honest.
  //   2. This is a PyWebView desktop app; the browser history also contains
  //      whatever preceded the app, which is not ours to walk back into.
  //
  // route() is the single funnel every navigation passes through, so the stack
  // is maintained there. `_navMoving` marks the hashchange we caused ourselves,
  // so stepping back does not get recorded as a new destination.
  const navHist = []
  let navPos = -1
  let _navMoving = false
  let _navReplace = false

  function _navRecord(hash) {
    if (_navMoving) { _navMoving = false; return }
    if (navHist[navPos] === hash) return          // re-dispatch of the same page
    if (_navReplace && navPos >= 0) {
      // Standing in for a page that turned out to be off-limits: overwrite it
      // rather than stack on top of it, so Back does not walk straight into the
      // page we just bounced out of. The forward tail goes too — it was reached
      // through that page.
      _navReplace = false
      navHist.splice(navPos + 1)
      navHist[navPos] = hash
      return
    }
    _navReplace = false
    navHist.splice(navPos + 1)                    // a new branch drops the forward tail
    navHist.push(hash)
    navPos = navHist.length - 1
  }

  // Views that exist only to edit the library. The three ingest steps — source
  // picker, triage queue and metadata review — all live under '#/ingest'.
  //
  // Enforced in route(), not in setViewMode (Ryan, 2026-08-22). Toggling to
  // Playback while standing on one of these is the case that prompted it, but
  // it is not the only way to arrive: Back, Forward and a typed or bookmarked
  // URL all get there too, and a listener has no toggle at all. One check on
  // the way in covers every route.
  const ADMIN_ONLY_HASHES = [
    '#/ingest',          // Add Recordings — source picker, metadata review
    '#/batch',           // Retired; redirects to #/ingest
    '#/genres/assign',   // Assign Genres
    '#/peers',           // Sharing
    '#/venue/new', '#/artist/new', '#/musician/new',
    '#/genre/new', '#/event/new',
    '#/bulk-ingest',        // Bulk Ingest
    '#/archive/lma',        // Live Music Archive catalog
    '#/downloads',          // Downloads folder
    '#/workshop',           // Workshop folder (2026-10-01)
    '#/backlog',            // Backlog folder (2026-10-01)
  ]
  const isAdminOnlyHash = h => ADMIN_ONLY_HASHES.includes(
    (h || '').split('?')[0].replace(/^(#\/bulk-ingest)\/\d+$/, '$1'))

  // ── In-page Back ──────────────────────────────────────────────────────────
  // A view whose steps all share one hash (Add Recordings) registers a handler
  // here. It is called with no argument to PERFORM a Back press and returns
  // true if it consumed it; called with `true` it only reports whether it
  // would. Registration is per-render and route() clears it on the way to any
  // other view, so a handler can never outlive the page that installed it.
  let _inPageBack = null
  function setInPageBack(fn) { _inPageBack = fn || null }
  const canStepBackInPage = () => !!(_inPageBack && _inPageBack(true))

  /** Correct the hash recorded at the current stack position, for the callers
   *  that fix window.location.hash with replaceState (no hashchange, so
   *  _navRecord never sees it). Without this the stack still points at the
   *  page we just left and Back lands back on it. */
  function _navRewrite(hash) {
    if (navPos < 0) return
    navHist[navPos] = hash
    // Collapse an adjacent duplicate. Rewriting '#/ingest' to '#/batch' when
    // '#/batch' is already the entry behind it (always, in the fromBatch flow:
    // that entry is where "Review" was clicked) would otherwise leave two
    // identical entries, and a Back press across them changes nothing.
    if (navPos > 0 && navHist[navPos - 1] === hash) {
      navHist.splice(navPos, 1)
      navPos--
    }
  }

  function _navGo(delta) {
    // In-page steps first: Back inside a multi-step view means the previous
    // step, not the previous URL.
    if (delta < 0 && _inPageBack && _inPageBack()) { paintNavButtons(); return }
    const target = navPos + delta
    // Repaint even when there is nowhere to go: this is the exit a stale
    // enabled state would otherwise survive, since every other one repaints.
    if (target < 0 || target >= navHist.length) { paintNavButtons(); return }
    navPos = target
    const hash = navHist[navPos]
    // The entry we are stepping onto can already BE the current hash — a view
    // that paints itself directly and corrects window.location.hash with
    // replaceState leaves the two out of step. Assigning a hash its own value
    // fires no hashchange, so route() would never run and the press would look
    // dead; dispatch it directly instead. _navMoving stays false on purpose:
    // route() finds this same hash already at navPos and records nothing.
    if ((window.location.hash || '#/') === hash) {
      route()
      paintNavButtons()
      return
    }
    _navMoving = true
    window.location.hash = hash
  }

  function paintNavButtons() {
    const back = document.getElementById('nav-back')
    const fwd  = document.getElementById('nav-fwd')
    if (back) back.disabled = navPos <= 0 && !canStepBackInPage()
    if (fwd)  fwd.disabled  = navPos >= navHist.length - 1
  }

  function wireHeaderNav() {
    // nav-home removed 2026-08-23 — "My Library" in the sidebar is the Home
    // link now (see renderSidebar). Back/Forward are unchanged.
    document.getElementById('nav-back')?.addEventListener('click', () => _navGo(-1))
    document.getElementById('nav-fwd')?.addEventListener('click', () => _navGo(1))

    // Wordmark (spec 7c): the other home control, alongside the sidebar's
    // My Library link (renderSidebar) -- both go to #/bulk-ingest while a Bulk
    // Ingest run is running or paused, and to Library otherwise. This is
    // the single place that decision is made for a plain click.
    document.querySelector('.app-wordmark')?.addEventListener('click', () => {
      window.location.hash = homeHash()
    })

    // "Add Recordings" is a same-hash link whenever the user is already on
    // #/ingest — mid-triage, sitting on "All done", or looking at a filled
    // review form. A same-hash click fires no hashchange, so route() never
    // runs and renderIngestView() never fires: the page just sits there
    // stale (Ryan, 2026-08-26 — the "still shows the previous job" bug).
    // Same class of issue as the documented fromBatch case on the in-page
    // back-link below; fixed the same way, by not depending on the hash
    // actually changing.
    document.getElementById('sidebar-nav')?.addEventListener('click', e => {
      if (!e.target.closest('.nav-add-btn')) return
      if (window.location.hash === '#/ingest') {
        e.preventDefault()
        renderIngestView()
      }
    })

    // Sidebar collapse toggle (2026-08-23). Plain localStorage flag, same
    // idiom as setPalette() above — applied before first paint by the inline
    // script at the top of <body> in index.html so there's no flash, flipped on
    // click here.
    const sbToggle = document.getElementById('nav-sidebar-toggle')
    sbToggle?.setAttribute('aria-pressed', String(document.body.classList.contains('sidebar-collapsed')))
    sbToggle?.addEventListener('click', () => {
      const collapsed = document.body.classList.toggle('sidebar-collapsed')
      localStorage.setItem('trellisSidebarCollapsed', collapsed ? '1' : '0')
      sbToggle.setAttribute('aria-pressed', String(collapsed))
    })
  }

  // ── Bulk Ingest (spec 1.9/4, chunk 7b) ─────────────────────────────────────────────
  //
  // One run at a time over LIBRARY_ROOT (app/utils/bulk_ingest_run.py). This is
  // the only page for it, at #/bulk-ingest; renderSidebar's nav item and the
  // wordmark/My Library home routing (wireHeaderNav) both read state.bulkIngest
  // rather than fetching their own copy.

  // docs site root — the-data-model page lives at
  // trellismusiclibrary/src/content/docs/the-data-model.md and publishes at
  // this path (N8: an earlier comment here said this URL was invented).
  const BULK_INGEST_DOCS_URL = 'https://trellismusiclibrary.com/docs/the-data-model'

  let _bulkIngestPollTimer = null
  function _stopBulkIngestPoll() {
    if (_bulkIngestPollTimer) { clearInterval(_bulkIngestPollTimer); _bulkIngestPollTimer = null }
  }

  // /api/bulk-ingest/current's shape: {run: null} when nothing has ever been
  // ingested, or the run's own fields directly (no wrapper) when one exists.
  function _bulkIngestRun(data) {
    return (data && data.run === undefined) ? data : null
  }

  async function _fetchBulkIngestStatus() {
    try { state.bulkIngest = _bulkIngestRun(await API.bulkIngest.current()) }
    catch (e) { state.bulkIngest = null }
    await _fetchBulkIngestRuns()
  }

  // The listed runs (unfinished, or a Review First Queue still holding
  // ready/review items). Drives the Add Recordings badge and where Add
  // Recordings goes. Fetched on boot, on navigation (route) and when the
  // import page opens, never on a timer.
  async function _fetchBulkIngestRuns() {
    try {
      const d = await API.bulkIngest.runs()
      state.biRuns = d.runs || []
      state.biWaiting = typeof d.waiting === 'number' ? d.waiting : null
    } catch (e) { state.biRuns = []; state.biWaiting = 0 }
  }

  // Distinct folders waiting on a person (ready + needs review) across every
  // listed run. The server dedupes by folder; the per-run sum is only a
  // fallback, since one folder can sit in two runs' queues.
  function _biWaitingCount() {
    if (typeof state.biWaiting === 'number') return state.biWaiting
    return (state.biRuns || []).reduce((n, r) => {
      const c = r.counts || {}
      return n + (c.ready || 0) + (c.review || 0)
    }, 0)
  }

  // The import page of the most recent listed run, or null when none is listed.
  function _biOpenRunHash() {
    const runs = state.biRuns || []
    if (!runs.length) return null
    return '#/bulk-ingest/' + runs.reduce((a, b) => (b.id > a.id ? b : a)).id
  }

  // On navigation: refetch the list and repaint the sidebar only when it
  // changed (renderSidebar also clears the dimension caches, so not every time).
  let _biRunsSig = null
  async function _biRefreshRunsOnNav() {
    if (!canEditLibrary()) return
    await _fetchBulkIngestRuns()
    const sig = (state.biRuns || []).map(r => r.id + ':' + r.status).join(',') + '|' + _biWaitingCount()
    if (_biRunsSig !== null && sig !== _biRunsSig) renderSidebar()
    _biRunsSig = sig
  }

  // Import mode memory. Import Automatically / Review First is remembered per
  // placement (inside the library vs outside) so a choice made for Downloads
  // never changes what the library folder offers. Absent or unreadable storage
  // falls back to the server default (auto inside the library, hold outside).
  const _BI_MODE_KEY = 'trellisBulkIngestMode'
  function _biDefaultMode(inLibrary) { return inLibrary ? 'auto' : 'hold' }
  function _biMode(inLibrary) {
    try {
      const m = (JSON.parse(localStorage.getItem(_BI_MODE_KEY)) || {})[inLibrary ? 'library' : 'outside']
      if (m === 'auto' || m === 'hold') return m
    } catch (e) { /* storage blocked or malformed: use the default */ }
    return _biDefaultMode(inLibrary)
  }
  function _biRememberMode(inLibrary, mode) {
    try {
      const all = JSON.parse(localStorage.getItem(_BI_MODE_KEY)) || {}
      all[inLibrary ? 'library' : 'outside'] = mode
      localStorage.setItem(_BI_MODE_KEY, JSON.stringify(all))
    } catch (e) { /* best effort */ }
  }

  // The row under the header on both Add Recordings pages: the mode toggle
  // with a line describing it, then the scan button and Reset Queue. `run` is
  // null on the picker (nothing scanned yet). The toggle is locked while the
  // run is running or paused and applies to the next scan once it is done.
  function _biScanControlsHtml({ run, mode }) {
    const status = run && run.status
    const locked = status === 'running' || status === 'paused'
    const seg = (m, label) => `<button type="button" class="${mode === m ? 'on' : ''}" data-bi-mode="${m}"${locked ? ' disabled' : ''}>${label}</button>`
    const desc = mode === 'auto'
      ? 'Recordings with no issues are imported as soon as they are scanned. Anything that needs attention waits in the queue.'
      : 'Every recording waits in the queue for you to review and import.'
    // Start, Pause and Resume share the one tinted style; Rescan Folder is
    // the quieter ghost.
    const scan = !run ? ['start', 'Start Scan', 'btn-ingest-secondary']
      : status === 'running' ? ['pause', 'Pause Scan', 'btn-ingest-secondary']
      : status === 'paused' ? ['resume', 'Resume Scan', 'btn-ingest-secondary']
      : ['rescan', 'Rescan Folder', 'btn-ghost']
    const c = (run && run.counts) || {}
    const queued = (c.ready || 0) + (c.review || 0) + (c.pending || 0)
    // The Lomax row sits under Mode. Without a key the toggle is locked to Off.
    const hasKey = lxHasKey()
    const lxLine = 'Lomax checks recordings that need review and helps to fill out info.'
    const lxDesc = hasKey ? lxLine : lxLine + ' Lomax requires your Anthropic key to be set.'
    return `<div class="bi-scan-mode">
        <span class="bi-scan-lbl">Mode</span>
        <div class="bi-scan-cell">
          <div class="seg" role="group" aria-label="Import mode">${seg('auto', 'Import Automatically')}${seg('hold', 'Review First')}</div>
          <span class="bi-scan-desc">${desc}</span>
        </div>
        ${canEditLibrary() ? `<span class="bi-scan-lbl bi-scan-lbl--lx">${icon('lomax')}Lomax</span>
        <div class="bi-scan-cell">
          ${lomaxImportSwitch({ on: lxImportLevel() !== 'off', disabled: !hasKey })}
          <span class="bi-scan-desc">${lxDesc}</span>
        </div>` : ''}
      </div>
      <div class="bi-scan-row">
        <button type="button" class="btn ${scan[2]}" data-scan="${scan[0]}">${scan[1]}</button>
        ${queued ? '<button type="button" class="btn btn-ghost" data-scan="reset">Reset Queue</button>' : ''}
      </div>`
  }

  // Where the picker opens after Reset Queue (one-shot).
  let _biPickerPath = null

  // Start a run on `path` and open its import page. Shared by the Add
  // Recordings picker and the Downloads / Workshop / Backlog Ingest buttons.
  async function _biStartAndOpen(path, mode) {
    const run = await API.bulkIngest.start(path, mode)
    await refreshBulkIngestStatus()
    window.location.hash = '#/bulk-ingest/' + run.id
  }

  // Called after an action on the bulkIngest page itself might have changed run
  // status (pause/resume/scan again) so the nav item and home routing follow
  // without a full reload. Boot uses _fetchBulkIngestStatus directly (see
  // init()) since loadArtistList() right after it already repaints the
  // sidebar once.
  async function refreshBulkIngestStatus() {
    await _fetchBulkIngestStatus()
    renderSidebar()
  }

  // The run this page shows. #/bulk-ingest/<id> names it; bare #/bulk-ingest
  // resolves to the current run once and then sticks to that id, so a later
  // run starting never swaps the table under the person.
  let _biRun = null
  let _biPageRunId = null

  async function renderBulkIngestView(runId) {
    setActiveNav('ingest')
    setActiveArtist(null)
    setNavCurrent('Add Recordings')
    _stopBulkIngestPoll()
    _biResetProgressState()
    _lxQ = _lxQNew()
    await getPrefs()   // the Lomax toggle needs has_api_key before the first paint

    let data
    try {
      data = await API.bulkIngest.current(runId || null)
    } catch (e) {
      setMainHTML(`
        <div class="empty-state">
          <div class="empty-title">Could not open this page</div>
          <div class="empty-sub" style="color:var(--red)">${esc((e && e.message) || String(e))}</div>
        </div>`)
      return
    }
    const run = _bulkIngestRun(data)
    // Nav item and home routing follow the CURRENT run only.
    if (!runId) state.bulkIngest = run
    await _fetchBulkIngestRuns()
    renderSidebar()
    if (!run) { window.location.hash = '#/'; return }

    _biRun = run
    _biPageRunId = run.id
    // The blanket values live on the server; the block starts from them.
    _biAa = _biAaFromApplied(run.applied)
    _biApplied = run.applied || null
    _biApplyOpen = false

    await _paintBulkIngestPage(run)
    _biStartPoll()
  }

  // 3s poll while the run is running, then the slower scoring poll below.
  function _biStartPoll() {
    _stopBulkIngestPoll()
    const run = _biRun
    if (!run) return
    if (run.status !== 'running') { _maybeStartScoringPoll(run); return }
    const pid = run.id
    _bulkIngestPollTimer = setInterval(async () => {
      // One tick at a time: a slow tick used to let the next ones pile up on
      // top of it, and nothing moved on screen until they all drained.
      if (_biTickBusy) return
      _biTickBusy = true
      try {
        let d
        try { d = await API.bulkIngest.current(pid) } catch (e) { return }
        const r = _bulkIngestRun(d)
        if (!r) { _stopBulkIngestPoll(); return }
        _biRun = r
        if (state.bulkIngest && state.bulkIngest.id === r.id) state.bulkIngest = r
        // The Add Recordings badge comes from this same payload's counts.
        const badgeChanged = _biSyncRunCounts(state.biRuns, r)
        // The badge is a deduped count from /runs; refetch it only when this
        // run's waiting count moved, not on every tick.
        if (badgeChanged) await _fetchBulkIngestRuns()
        await _paintBulkIngestPage(r)
        if (r.status !== 'running') {
          _stopBulkIngestPoll(); renderSidebar(); _maybeStartScoringPoll(r)
        } else if (badgeChanged) renderSidebar()
      } finally { _biTickBusy = false }
    }, 3000)
  }

  let _biTickBusy = false

  // Copy a freshly polled run's status and counts into the listed-runs entry
  // the sidebar badge reads, so the badge follows the poll without another
  // request. Returns true when the waiting count changed.
  function _biSyncRunCounts(runs, r) {
    const entry = (runs || []).find(x => x.id === r.id)
    if (!entry) return false
    const before = ((entry.counts || {}).ready || 0) + ((entry.counts || {}).review || 0)
    entry.counts = r.counts
    entry.status = r.status
    const after = ((r.counts || {}).ready || 0) + ((r.counts || {}).review || 0)
    return before !== after
  }

  // After an action that woke the worker (Ingest, Convert, Ingest all ready,
  // Move): refetch the run, repaint, patch the rows, and make sure the poll is
  // running. A finished run is flipped back to running by those routes, so the
  // poll has something to follow.
  async function _biAfterAction() {
    let d
    try { d = await API.bulkIngest.current(_biPageRunId) } catch (e) { return }
    const r = _bulkIngestRun(d)
    if (!r) return
    _biRun = r
    if (state.bulkIngest && state.bulkIngest.id === r.id) state.bulkIngest = r
    await _paintBulkIngestPage(r)
    // The worker may already have finished, which skips the poll's own table
    // patch, so patch here.
    await _biTableRefresh()
    _biStartPoll()
    renderSidebar()
  }

  // Listening Quality keeps scoring in the background after a run finishes
  // (the follow-up queue in app/api/ingest.py) -- a slower, separate poll
  // than the 3s one above, only while there is still something to wait for.
  function _maybeStartScoringPoll(run) {
    const scorable = (run && run.scorable) || 0
    const scored   = (run && run.scored)   || 0
    if (!(run && run.status === 'done' && scorable > 0 && scored < scorable)) return
    const pid = run.id
    _bulkIngestPollTimer = setInterval(async () => {
      let d
      try { d = await API.bulkIngest.current(pid) } catch (e) { return }
      const r = _bulkIngestRun(d)
      if (r) _biRun = r
      if (r && state.bulkIngest && state.bulkIngest.id === r.id) state.bulkIngest = r
      if (!r) { _stopBulkIngestPoll(); return }
      await _paintBulkIngestPage(r)
      if ((r.scored || 0) >= (r.scorable || 0)) _stopBulkIngestPoll()
    }, 5000)
  }

  // ── Progress table (2026-09-27 redesign) ─────────────────────────────────
  // Rebuilt onto Review & Ingest's own shell (renderTriageView / _lqCompactRow)
  // rather than Batch Import's -- same header shape (title, subtitle, source
  // chip, mode strip, progress bar, primary action top-right), same table
  // shape (.lq-brow-head / .lq-brow, coloured spine, triage spinner and
  // caret). What is new lives under `.bi-*`: the FORMAT/TYPE/STATUS columns
  // this table needs that Review & Ingest's Sound-Quality/Metadata/
  // fingerprint columns do not.
  //
  // The item table is paged 100-at-a-time (GET .../items?status=all, id
  // ascending -- discovery order, so a running list reads like the triage
  // queue) and, once loaded, is never rebuilt by a poll: _biTableRefresh
  // patches only the rows whose status changed and appends rows discovered
  // since the last check, both by id, in place.
  let _biRowsCache = new Map()   // id -> item, every row loaded so far
  let _biLoadedCount = 0
  let _biTableExhausted = false
  let _biTableLoading = false
  let _biTableIO = null
  let _biTableRunId = null
  let _biOpenRows = new Set()    // ids whose expand panel is open
  let _biReviewFilter = false    // header "Review"/"Show all" toggle (2026-09-27)
  let _biTab = 'queue'           // 'queue' | 'live' | 'album' (2026-10-01)

  // Rolling rate/ETA window -- unrelated to the table above, kept across
  // polls the same way.
  let _biRateSamples = []     // [{t, processed}], rolling 5-minute window
  let _biLastPaintedKey = null   // run.id + ':' + (done ? 'done' : 'active')

  function _biResetProgressState() {
    _biRateSamples = []
    _biLastPaintedKey = null
    _biRowsCache = new Map()
    _biLoadedCount = 0
    _biTableExhausted = false
    _biTableLoading = false
    _biTableIO?.disconnect()
    _biTableIO = null
    _biTableRunId = null
    _biOpenRows = new Set()
    _biReviewFilter = false
    _biTab = 'queue'
  }

  // Integers, largest unit, minimum "1 minute" (spec).
  function _biFmtDuration(minutes) {
    const m = Math.max(1, Math.round(minutes))
    if (m < 60) return `${m} minute${m === 1 ? '' : 's'}`
    const h = Math.round(m / 60)
    if (h < 24) return `${h} hour${h === 1 ? '' : 's'}`
    const d = Math.max(1, Math.round(h / 24))
    return `${d} day${d === 1 ? '' : 's'}`
  }

  // Records one (timestamp, processed) sample, trims the window to the last
  // five minutes, and returns {rateText, etaText} computed from it -- or
  // null until there are at least two samples to compare. The ETA half is
  // withheld until the window itself spans at least two minutes (spec);
  // the rate can show sooner off whatever samples exist so far.
  function _biRateAndEta(processed, found) {
    const now = Date.now()
    _biRateSamples.push({ t: now, processed })
    const cutoff = now - 5 * 60 * 1000
    _biRateSamples = _biRateSamples.filter(s => s.t >= cutoff)
    if (_biRateSamples.length < 2) return null
    const oldest = _biRateSamples[0]
    const spanMs = now - oldest.t
    const delta = processed - oldest.processed
    if (spanMs <= 0 || delta <= 0) return null
    const perMinute = delta / (spanMs / 60000)
    const rateText = `${Math.round(perMinute)} per minute`
    let etaText = null
    if (spanMs >= 2 * 60 * 1000 && perMinute > 0) {
      const minutesLeft = Math.max(0, found - processed) / perMinute
      etaText = `about ${_biFmtDuration(minutesLeft)} left`
    }
    return { rateText, etaText }
  }

  const _BI_SKIPPED_REASON_PHRASE = {
    already_in_library: 'already in your library',
    rejected:           'rejected earlier',
    duplicate_content:  'exact duplicate',
  }

  // BulkIngestItem.reason is one String(32) column (app/models/bulk_ingest.py)
  // -- several resolver reasons land in it comma-joined, same as the one
  // other reader, app/api/quality.py's _bulk_ingest_review_reasons, already
  // splits apart. The run summary's counts (run.reasons / run.skipped_reasons)
  // are grouped server-side on that same raw column, so a composite key like
  // "needs_artist,needs_day" arrives as ONE count under that whole string --
  // split it back into its codes and re-bucket before reading it here.
  function _biSplitReasonCounts(reasons) {
    const out = {}
    for (const [key, n] of Object.entries(reasons || {})) {
      for (const code of key.split(',')) {
        if (code) out[code] = (out[code] || 0) + n
      }
    }
    return out
  }

  // Meta line for a finished bulk-ingest row: Artist · date · venue, or
  // Artist · title for a studio record. Same separator markup as the
  // Review & Ingest row builder (_lqBuildIqRow).
  function _biMetaLine(it) {
    const second = it.kind === 'studio' ? (it.title || it.date_text) : it.date_text
    const third  = it.kind === 'studio' ? '' : (it.venue || it.location)
    const parts = [it.artist, second, third].filter(Boolean).map(esc)
    return parts.length ? parts.join('<span class="sep">·</span>') : ''
  }

  function _biKindLabel(it) {
    return it.kind === 'studio' ? 'Album' : (it.kind === 'live' ? 'Live' : '')
  }

  // Normalizes one GET .../items row into the shared ingest-queue-table row
  // shape (see the component doc comment near ingestQueueTable/_iqRow).
  function _biBuildIqRow(it) {
    // A ready/review item the person just sent to Ingest shows as pending
    // until the poll sees what became of it.
    const pending    = it.status === 'pending'
      || (!!it.ingest_requested && (it.status === 'ready' || it.status === 'review'))
    const inProgress = it.status === 'in_progress'
    const status = pending ? 'pending' : inProgress ? 'ingesting' : it.status // ingested|review|skipped|failed
    const needsReview = it.status === 'review'
    // Held back for audio Trellis does not import: only Convert and Move apply.
    const unsupported = needsReview && String(it.reason || '').split(',').includes('unsupported_format')
    // it.reason may hold several resolver codes comma-joined (see
    // _biSplitReasonCounts above) -- _ingestReasonLabels splits and maps all
    // of them, so a row needing both an artist and a day shows both, not
    // just the first.
    const reviewIssues = needsReview ? _ingestReasonLabels(it.reason) : []
    if (needsReview && !reviewIssues.length) reviewIssues.push('Needs review')
    const reviewLabel = reviewIssues[0] || null
    // Same fragment-map style as before ("Skipped already in your library"),
    // extended to every reason code it.reason might now hold comma-joined.
    const skippedCodes = it.status === 'skipped' ? String(it.reason || '').split(',').filter(Boolean) : []
    const skippedPhrases = skippedCodes.map(c => _BI_SKIPPED_REASON_PHRASE[c]).filter(Boolean)
    const statusText = it.status === 'skipped'
      ? `Skipped${skippedPhrases.length ? ' ' + skippedPhrases.join(', ') : ''}`
      : it.status === 'failed' ? (it.detail || 'Could not be read') : null
    const meta = (status === 'pending' || status === 'ingesting' || status === 'failed')
      ? '' : _biMetaLine(it)
    const basename = _biItemName(it)
    // Completed tabs (2026-10-01): the recording's own name leads and the
    // folder name drops to the grey line. Live: Artist - date - venue, place.
    // Album: Artist - Title.
    const imported = _biTab !== 'queue' && it.status === 'ingested'
    // Ready / issue labels live in their own status column (_iqStatusCell),
    // not in this line.
    let title = basename
    let sub = meta
    if (imported) {
      const place = it.venue && it.location && !String(it.venue).includes(it.location)
        ? `${it.venue}, ${it.location}` : (it.venue || it.location)
      const parts = it.kind === 'studio' ? [it.artist, it.title] : [it.artist, it.date_text, place]
      const joined = parts.filter(Boolean).join(' - ')
      // No grey line on the Imported tabs (Ryan, 2026-10-05): the folder name
      // was redundant there. Hover on the title still shows the path.
      if (joined) { title = joined; sub = '' }
    }
    return {
      id: it.id,
      name: title,
      meta: sub,
      format: it.format || null,
      kind: it.kind || null,
      sound_band: it.sound_band || null,
      meta_band: it.meta_band || null,
      convertible: unsupported ? _biConvertKind(it) : null,
      unsupported,
      _it: it,
      needs_review: needsReview,
      review_reason: reviewLabel,
      review_issues: reviewIssues,
      status,
      recording_id: it.recording_id,
      // Completed tabs only: the recording's image at the left (2026-10-01).
      thumb: _biTab !== 'queue' && it.status === 'ingested'
        ? { url: it.image_url || null, initials: String(it.artist || it.title || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase() }
        : null,
      status_text: statusText,
      detail: {
        artist: it.artist, date: it.date_text, venue: it.venue,
        location: it.location, source: it.source, lineage: it.lineage,
        tracksText: it.track_count != null ? String(it.track_count) : null,
        trackListing: (it.tracks || []).map(t =>
          `<div class="iq-track"><span class="iq-track-n">${esc(String(t.n ?? '').padStart(2, '0'))}</span> ${esc(t.title)}</div>`).join(''),
        issuesHtml: '',
        path: it.rel_path,
      },
    }
  }

  // An outside single-show run has one item with rel_path "." -- its folder
  // is the run root, so that is the name to show.
  function _biItemName(it) {
    if (it.rel_path && it.rel_path !== '.') return it.rel_path.split('/').pop()
    return String((_biRun && _biRun.root) || '').replace(/\/+$/, '').split('/').pop()
  }

  // Only FLAC and MP3 are imported; a folder holding any other lossless
  // format (even beside FLAC) is what Convert applies to, the same rule
  // detect_convertible enforces server-side.
  function _biConvertKind(it) {
    const f = String(it.format || '').toUpperCase().split(/[,\s]+/).filter(Boolean)
    if (f.includes('SHN')) return { kind: 'shn' }
    return f.some(x => x === 'WAV' || x === 'AIFF' || x === 'APE' || x === 'WV') ? { kind: 'wav' } : null
  }

  // Review First runs score live folders before ingest, so only their Queue
  // shows the Sound Quality column.
  // ── Lomax: the import queue ─────────────────────────────────────────────────
  // With Lomax on (Add Recordings), Lomax runs by itself, one
  // recording at a time, on every row that needs review. A run is filed against
  // the row's folder, so opening the row's Resolver finds it already there, and
  // it moves to the recording when the row is imported. The Lomax column and
  // the summary line read the state kept here.
  function _lxQNew() {
    return { cells: new Map(), items: new Map(), queue: [], running: false, paused: false, pauseRead: false,
             touched: false, tokens: 0, searches: 0, listedAt: 0 }
  }
  // Pause lives in storage against the run id: the queue object is rebuilt on every visit to the
  // page, and a pause held only there was lost the moment the person navigated away.
  function _lxQReadPause(q) {
    if (q.pauseRead || !_biPageRunId) return
    q.pauseRead = true
    q.paused = String(lxGet(LX_PAUSED_KEY) || '') === String(_biPageRunId)
  }
  function _lxQSetPaused(paused) {
    _lxQ.paused = paused
    _lxQ.pauseRead = true
    lxSet(LX_PAUSED_KEY, paused && _biPageRunId ? String(_biPageRunId) : '')
  }
  let _lxQ = _lxQNew()

  const _lxColShown = () => _biTab === 'queue' && canEditLibrary() && (lxImportLevel() !== 'off' || _lxQ.touched)

  function _biAbsPath(it) {
    const root = (_biRun && _biRun.root) || ''
    return it.abs_path || (root.replace(/\/+$/, '') + '/' + it.rel_path)
  }

  // What the page would send for a folder it has scanned but not opened: the
  // same fields the Add Recording form is prefilled with, read from the resolver.
  function lomaxCurrentFromScan(scan) {
    const r = (scan && scan.resolved) || {}
    const val = k => { const v = r[k] && r[k].value; return v == null || typeof v === 'object' ? '' : String(v) }
    const d = (r.date && r.date.value) || {}
    const p2 = n => String(n).padStart(2, '0')
    const date = d.year ? `${d.year}${d.month ? '-' + p2(d.month) : ''}${d.month && d.day ? '-' + p2(d.day) : ''}` : ''
    return {
      artist: val('artist'), date, venue: val('venue'), city: val('city'), state: val('state'),
      country: val('country'), event: val('event'), stage: val('stage'), source: val('source'),
      lineage: val('lineage'),
      tracks: (r.tracks || []).map(t => ({
        number: t.track_number, title: t.title, duration: t.duration,
        songwriter: t.songwriter || '', notes: t.notes || '',
      })),
      info_file_content: (scan && scan.info_file_content) || '',
      resolved: r,
      fingerprint: lxFingerprint(scan),
    }
  }

  // The album shape of the same thing: what the Add Recording album form would send.
  function lomaxAlbumCurrentFromScan(scan) {
    const r = (scan && scan.resolved) || {}
    const val = k => { const v = r[k] && r[k].value; return v == null || typeof v === 'object' ? '' : String(v) }
    const d = (r.date && r.date.value) || {}
    return {
      artist: val('artist'), title: val('album'), year: d.year ? String(d.year) : '',
      tracks: (r.tracks || []).map(t => ({
        number: t.track_number, title: t.title, duration: t.duration, songwriter: t.songwriter || '',
      })),
      info_file_content: (scan && scan.info_file_content) || '',
      notes: '',
      fingerprint: lxFingerprint(scan),
    }
  }

  function _lxQueueCell(row) {
    const it = row._it
    if (row.status === 'ready') return '<span class="lx-qc lx-qc--dim">Not needed</span>'
    if (!row.needs_review || !it) return ''
    const cell = _lxQ.cells.get(it.id)
    let inner = ''
    if (cell && cell.state === 'working') inner = '<span class="lq-spin"></span>Working'
    else if (cell && cell.state === 'done') {
      inner = cell.n
        ? `<a href="#" class="lx-qc-link" data-lx-review="${esc(it.id)}">${lxPlural(cell.n, 'suggestion')}</a>`
        : 'Nothing to add'
    } else if (cell && cell.state === 'skipped') inner = 'Skipped: not reachable'
    else if (cell || lxImportLevel() !== 'off') inner = 'Queued'
    return `<span class="lx-qc" data-lx-qc="${esc(it.id)}">${inner}</span>`
  }

  function _lxQSetCell(id, cell) {
    _lxQ.cells.set(id, cell)
    _lxQ.touched = true
    const el = document.querySelector(`#bi-rows [data-lx-qc="${id}"]`)
    const it = _biRowsCache.get(id)
    if (el && it) {
      const tmp = document.createElement('div')
      tmp.innerHTML = _lxQueueCell(_biBuildIqRow(it))
      if (tmp.firstElementChild) el.replaceWith(tmp.firstElementChild)
    }
    _lxQPaintLine()
  }

  function _lxQPaintLine() {
    const el = document.getElementById('bi-lomax-line')
    if (!el) return
    const q = _lxQ
    _lxQReadPause(q)
    if (!_biRun || !_lxColShown() || !q.items.size) { el.innerHTML = ''; return }
    let checked = 0
    for (const c of q.cells.values()) if (c.state === 'done' || c.state === 'skipped') checked++
    el.innerHTML = `<span class="bi-lx-lbl">${icon('lomax')}Lomax</span>` +
      `<span class="bi-lx-sum">${checked} of ${q.items.size} checked</span>` +
      (checked >= q.items.size ? '' : `<button type="button" class="btn btn-sm lx-go" data-lx-pause>${q.paused ? 'Resume' : 'Pause'}</button>`)
  }

  // Redraw the header and every loaded row (the column came or went).
  function _lxQRepaintAll() {
    const head = document.getElementById('bi-thead')
    if (head) head.innerHTML = _biTheadHtml()
    for (const id of Array.from(_biRowsCache.keys())) _biRepaintRow(id)
    _lxQPaintLine()
  }

  // Every row that needs review, paged from the server, not just the ones the
  // table has scrolled into view. Throttled: the page ticks every few seconds.
  async function _lxQLoadItems(q) {
    if (Date.now() - q.listedAt < 5000) return
    q.listedAt = Date.now()
    for (let page = 1; ; page++) {
      let body
      try { body = await API.bulkIngest.items(_biPageRunId, 'review', page) } catch (_) { return }
      const items = (body && body.items) || []
      for (const it of items) {
        if (it.status !== 'review' || q.items.has(it.id)) continue
        q.items.set(it.id, it)
        q.queue.push(it.id)
        _lxQSetCell(it.id, { state: 'queued' })
      }
      if (items.length < 100) break
    }
  }

  async function _lxQProcess(q, id) {
    const it = q.items.get(id)
    const abs = _biAbsPath(it)
    const level = lxImportLevel()
    const alive = () => _lxQ === q
    _lxQSetCell(id, { state: 'working' })
    try {
      const skill = it.kind === 'studio' ? 'album' : 'recording'
      const runs = ((await API.lomax.runs({ skill, subject_type: 'folder', subject_key: abs })) || {}).runs || []
      const last = runs[runs.length - 1]
      let run = runs.slice().reverse().find(r => r.status === 'done')
      let fresh = false
      if (!run) {
        if (last && last.status === 'error') throw new Error('earlier run failed')
        if (last) run = await lomaxWaitRun(last.id, { alive })
        else {
          const scan = await API.recordings.scan(abs)
          const started = await API.lomax.start({
            skill, subject_type: 'folder', subject_key: abs, level,
            current: skill === 'album' ? lomaxAlbumCurrentFromScan(scan) : lomaxCurrentFromScan(scan),
          })
          fresh = true
          run = await lomaxWaitRun(started.id, { alive })
        }
      }
      if (!alive()) return
      if (!run || run.status !== 'done') throw new Error((run && run.error) || 'no result')
      if (fresh && run.usage) {
        q.tokens += run.usage.total_tokens || 0
        q.searches += run.usage.web_search_requests || 0
      }
      _lxQSetCell(id, { state: 'done', n: lxSuggestions(run).length })
    } catch (e) {
      if (!alive()) return
      if (/no_api_key/.test(String(e && e.message))) {
        // No key: nothing will run. Put the toggle back to Off and leave the row queued.
        appPrefs = null
        q.paused = true
        _lxQSetCell(id, { state: 'queued' })
        return
      }
      _lxQSetCell(id, { state: 'skipped' })
    }
  }

  async function _lxQKick() {
    const q = _lxQ
    _lxQReadPause(q)
    if (q.running || !_biRun || !_lxColShown() || lxImportLevel() === 'off') return
    q.running = true
    try {
      await _lxQLoadItems(q)
      while (_lxQ === q && !q.paused && lxImportLevel() !== 'off' && document.getElementById('bi-rows')) {
        const id = q.queue.shift()
        if (id == null) break
        await _lxQProcess(q, id)
      }
    } finally {
      q.running = false
      if (_lxQ === q) _lxQPaintLine()
    }
  }

  // The Add Recordings toggle changed (the toggle itself updated in place).
  function _lxImportLevelChanged(level) {
    if (!document.getElementById('bi-rows')) return   // the picker: nothing running yet
    _lxQSetPaused(false)
    _lxQRepaintAll()
    if (level !== 'off') _lxQKick()
  }

  document.addEventListener('click', e => {
    const link = e.target.closest && e.target.closest('[data-lx-review]')
    if (link) {
      e.preventDefault()
      const it = _biRowsCache.get(Number(link.dataset.lxReview))
      if (it) _biOpenReviewFor(it)
      return
    }
    if (e.target.closest && e.target.closest('[data-lx-pause]')) {
      _lxQSetPaused(!_lxQ.paused)
      _lxQPaintLine()
      if (!_lxQ.paused) _lxQKick()
    }
  })

  function _biSoundCol() {
    return !!(_biRun && _biRun.mode === 'hold' && _biTab === 'queue')
  }

  function _biTheadHtml() {
    const sq = _biSoundCol()
    const lx = _lxColShown()
    return `<div class="lq-brow-head iq-brow${sq ? '' : ' iq-brow--nosq'}${lx ? ' iq-brow--lx' : ''}">
      <span>Recording</span>
      <span>Format</span><span>Type</span>
      ${sq ? '<span>Sound Quality</span>' : ''}
      <span>Metadata</span><span></span>${lx ? '<span>Lomax</span>' : ''}<span></span><span></span>
    </div>`
  }

  // Row actions on the Queue: Import, Review, Move (bring-in runs only) and
  // Convert (unsupported audio). Every other state defers to the shared defaults.
  function _biActionsHtml(row) {
    const it = row._it
    if (row.status === 'moved') return ''
    if (!it || (row.status !== 'ready' && row.status !== 'review')) return _iqDefaultActions(row, {})
    if (!canEditLibrary()) return ''
    const id = esc(row.id)
    // A row's own Convert is tracked here; Convert All's rows come from the
    // server (it.converting) and have no Stop.
    const cv = _biConverting.get(it.id) || it.converting
    if (cv) {
      return `<span class="lq-act-running" data-convert-for="${id}">
          <span class="lq-spin"></span>${esc(_lqConvertText(cv))}</span>
        ${cv.jobId ? `<button class="lq-act lq-act--cancel" data-convert-cancel="${id}">Stop</button>` : ''}`
    }
    const btns = []
    const err = _biConvertErr.get(it.id) || (row.unsupported && it.detail)
    if (err) btns.push(_lqErrorChip(err, 'Could not convert this folder'))
    // Unsupported audio is never imported, so Import and Review are not
    // offered. A paused run refuses import requests (409), so the button is
    // not offered then either.
    if (!row.unsupported && !(_biRun && _biRun.status === 'paused')) {
      btns.push(`<button type="button" class="lq-act lq-act--ingest" data-path="${id}">Import</button>`)
    }
    if (!row.unsupported) {
      btns.push(`<button type="button" class="lq-act lq-act--review" data-path="${id}">Review</button>`)
    }
    // Convert is offered everywhere, library folders included: it only runs
    // on an explicit click, behind a confirmation.
    if (row.convertible) {
      btns.push(`<button class="lq-act lq-act--convert" data-convert="${id}">Convert to FLAC</button>`)
    }
    // Move is for brought-in folders; one inside the library is never moved.
    if (_biRun && _biRun.placement === 'bring_in' && triageDests().length) {
      btns.push(`<div class="lq-move-wrap">
        <button type="button" class="lq-act lq-act--move" data-path="${id}">Move ${chevronIcon('caret-ic--down lq-act-chev')}</button>
        <div class="lq-move-menu" hidden>${triageDests().map(d =>
          `<button type="button" class="lq-move-opt" data-path="${id}" data-dest="${esc(d)}">${esc(TRIAGE_LABELS[d] || d)}</button>`).join('')}</div>
      </div>`)
    }
    return btns.join('')
  }

  function _biRowHtml(it) {
    return _iqRow(_biBuildIqRow(it), {
      soundQuality: _biSoundCol(),
      canIngest: true,
      moveTargets: [],
      actionsHtml: _biActionsHtml,
      lomaxCell: _lxColShown() ? _lxQueueCell : null,
      isOpen: r => _biOpenRows.has(r.id),
    })
  }

  function _biSetupTableSentinel() {
    const sentinel = document.getElementById('bi-sentinel')
    if (!sentinel) return
    _biTableIO?.disconnect()
    if (_biTableExhausted) { sentinel.innerHTML = ''; _biTableIO = null; return }
    _biTableIO = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) _biTableLoadMore()
    }, { root: mainContent, rootMargin: '400px' })
    _biTableIO.observe(sentinel)
  }

  // Next 100-row page, appended at the end -- infinite scroll, same shape as
  // Review & Ingest's own log used before this redesign.
  async function _biTableLoadMore() {
    if (_biTableLoading || _biTableExhausted || !_biTableRunId) return
    _biTableLoading = true
    const page = Math.floor(_biLoadedCount / 100) + 1
    let body
    try { body = await API.bulkIngest.items(_biTableRunId, _biItemsFilter(), page) }
    catch (e) { _biTableLoading = false; return }
    const items = body.items || []
    for (const it of items) _biRowsCache.set(it.id, it)
    const listEl = document.getElementById('bi-rows')
    if (listEl) listEl.insertAdjacentHTML('beforeend', items.map(_biRowHtml).join(''))
    _biLoadedCount += items.length
    _biTableExhausted = items.length < 100 || _biLoadedCount >= (body.total || 0)
    _biTableLoading = false
    _biSetupTableSentinel()
  }

  // Which slice of the run the table shows: the active tab, narrowed to
  // needs-review rows when the Needs Review toggle is on (Queue only).
  function _biItemsFilter() {
    if (_biTab === 'queue') return _biReviewFilter ? 'review' : 'queue'
    return _biTab
  }

  // Routine 3s poll while running. Rows now move between tabs (a Queue row
  // that finishes ingesting leaves it for Live Recordings or Albums, 2026-
  // 10-01), so this re-fetches every loaded page and reconciles by id: rows
  // gone from the slice are removed, changed rows replaced in place, new
  // rows inserted in order. Unchanged rows are left alone, so an open
  // expand panel or scroll position survives.
  async function _biTableRefresh() {
    if (!_biTableRunId) return
    const pages = Math.max(1, Math.ceil(_biLoadedCount / 100))
    const all = []
    let total = 0
    for (let p = 1; p <= pages; p++) {
      let body
      try { body = await API.bulkIngest.items(_biTableRunId, _biItemsFilter(), p) }
      catch (e) { return }
      total = body.total || 0
      all.push(...(body.items || []))
      if ((body.items || []).length < 100) break
    }
    _biReconcile(all)
    _biTableExhausted = all.length >= total
    _biSetupTableSentinel()
  }

  // Does this item belong in the slice the active tab shows? Mirrors the
  // server's items filters.
  function _biRowBelongs(it) {
    const f = _biItemsFilter()
    if (f === 'queue') return it.status !== 'ingested' && it.status !== 'moved'
    if (f === 'review') return it.status === 'review'
    if (f === 'album') return it.status === 'ingested' && it.kind === 'studio'
    return it.status === 'ingested' && it.kind !== 'studio'
  }

  // Rows that can still change on their own: being worked, waiting on a
  // request, or pending. Capped, lowest id first (the worker's order).
  function _biLiveRowIds(cache) {
    const ids = []
    for (const it of cache.values()) {
      if (it.status === 'in_progress' || it.ingest_requested || it.status === 'pending' || it.converting) ids.push(it.id)
    }
    return ids.sort((a, b) => a - b).slice(0, 100)
  }

  // Pure: given the cached rows and the fresh copies of some of them, which
  // rows leave the slice and which are redrawn. `askedIds` that did not come
  // back are gone.
  function _biPlanRowUpdates(cache, askedIds, fetched, belongs) {
    const remove = [], replace = []
    const got = new Set(fetched.map(i => i.id))
    for (const id of askedIds) if (!got.has(id)) remove.push(id)
    for (const it of fetched) {
      if (!belongs(it)) { remove.push(it.id); continue }
      const prev = cache.get(it.id)
      if (!prev || prev.status !== it.status || !!prev.ingest_requested !== !!it.ingest_requested
          || JSON.stringify(prev.converting || null) !== JSON.stringify(it.converting || null)) replace.push(it)
    }
    return { remove, replace }
  }

  // Poll tick for the table. Normally only the few live rows are refetched, so
  // each ingested row leaves the Queue the moment its own ingest finishes. The
  // full paged reconcile is kept for what the cheap path cannot see: newly
  // discovered rows, and tabs other than the plain Queue.
  async function _biTableTick(run) {
    const want = _biTabCounts(run)[_biTab]
    const plainQueue = _biTab === 'queue' && !_biReviewFilter
    if (!plainQueue || (_biTableExhausted && want > _biLoadedCount)) { await _biTableRefresh(); return }
    const ids = _biLiveRowIds(_biRowsCache)
    if (!ids.length) return
    let body
    try { body = await API.bulkIngest.itemsByIds(_biTableRunId, ids) } catch (e) { return }
    const plan = _biPlanRowUpdates(_biRowsCache, ids, body.items || [], _biRowBelongs)
    const listEl = document.getElementById('bi-rows')
    if (!listEl) return
    for (const id of plan.remove) {
      listEl.querySelector(`:scope > .iq-row[data-id="${id}"]`)?.remove()
      if (_biRowsCache.delete(id)) _biLoadedCount--
    }
    for (const it of plan.replace) {
      const el = listEl.querySelector(`:scope > .iq-row[data-id="${it.id}"]`)
      _biRowsCache.set(it.id, it)
      if (!el) continue
      const tmp = document.createElement('div')
      tmp.innerHTML = _biRowHtml(it).trim()
      el.replaceWith(tmp.firstElementChild)
    }
  }

  function _biReconcile(items) {
    const listEl = document.getElementById('bi-rows')
    if (!listEl) return
    const keep = new Set(items.map(i => String(i.id)))
    Array.from(listEl.children).forEach(el => {
      if (!el.classList.contains('iq-row') || !keep.has(el.dataset.id)) {
        if (el.dataset.id) _biRowsCache.delete(Number(el.dataset.id))
        el.remove()
      }
    })
    let prevEl = null
    for (const it of items) {
      let el = listEl.querySelector(`:scope > .iq-row[data-id="${it.id}"]`)
      const prev = _biRowsCache.get(it.id)
      if (!el || !prev || prev.status !== it.status || !!prev.ingest_requested !== !!it.ingest_requested
          || JSON.stringify(prev.converting || null) !== JSON.stringify(it.converting || null)) {
        const tmp = document.createElement('div')
        tmp.innerHTML = _biRowHtml(it).trim()
        const fresh = tmp.firstElementChild
        if (el) el.replaceWith(fresh)
        el = fresh
      }
      _biRowsCache.set(it.id, it)
      const want = prevEl ? prevEl.nextElementSibling : listEl.firstElementChild
      if (want !== el) {
        if (prevEl) prevEl.after(el); else listEl.prepend(el)
      }
      prevEl = el
    }
    _biLoadedCount = items.length
  }

  // Tab strip above the table, with live counts from the run summary.
  function _biTabCounts(run) {
    const c = run.counts || {}
    const total = Object.values(c).reduce((a, b) => a + b, 0)
    const ingested = c.ingested || 0
    const album = run.studio || 0
    return { queue: total - ingested - (c.moved || 0), live: ingested - album, album }
  }

  function _biTabsHtml(run) {
    const n = _biTabCounts(run)
    const tabs = [['queue', 'Queue'], ['live', 'Imported - Live Recordings'], ['album', 'Imported - Albums']]
    // Imported tabs appear only once they hold rows (Ryan, 2026-10-02).
    const left = tabs.filter(([id]) => id === 'queue' || n[id] > 0).map(([id, label]) => `
      <button type="button" class="pp-tab bi-tab--${id}${_biTab === id && !_biApplyOpen ? ' active' : ''}" data-bitab="${id}">${label}<span class="pp-tab-n">${n[id]}</span></button>`).join('')
    // Bulk Value Apply sits at the right end and replaces the table while open; it is not a
    // filter of rows, so it is page state (_biApplyOpen), never a _biTab value the table reads.
    return left + (_biApplyAvailable(run) ? `
      <button type="button" class="pp-tab bi-tab--apply${_biApplyOpen ? ' active' : ''}" data-bitab="apply">Bulk Value Apply</button>` : '')
  }
  function _biApplyAvailable(run) { return canEditLibrary() && !!run && _biTabCounts(run).queue > 0 }
  function _biApplyMode() {
    document.getElementById('bi-shell')?.classList.toggle('bi-applymode', _biApplyOpen)
  }

  // Line above the rows: the review note on Queue only. The completed tabs'
  // View buttons were removed (Ryan, 2026-10-01).
  function _biTabNoteHtml(run) {
    if (_biTab === 'queue') {
      // The Queue's instruction line was removed (Ryan, 2026-10-05).
      return ''
    }
    return ''
  }

  function _biPaintTabs(run) {
    // A selected tab that has emptied is hidden, so fall back to Queue.
    if (_biTab !== 'queue' && !_biTabCounts(run)[_biTab]) _biTab = 'queue'
    if (_biApplyOpen && !_biApplyAvailable(run)) { _biApplyOpen = false; _biApplyMode(); _biPaintApplyAll() }
    const tabsEl = document.getElementById('bi-tabs')
    if (tabsEl) tabsEl.innerHTML = _biTabsHtml(run)
    const noteEl = document.getElementById('bi-tab-note')
    if (noteEl) noteEl.innerHTML = _biTabNoteHtml(run)
    const allEl = document.getElementById('bi-ingest-all-wrap')
    if (allEl) allEl.innerHTML = _biIngestAllHtml(run)
  }

  // Rows held back for unsupported audio, from the run's reason breakdown
  // (keys are comma-joined reason codes, see _biSplitReasonCounts).
  function _biUnsupportedCount(run) {
    return Object.entries(run.reasons || {})
      .filter(([k]) => k.split(',').includes('unsupported_format'))
      .reduce((n, [, c]) => n + c, 0)
  }

  // Above the Queue list: Import All Ready (Review First only) imports every
  // item that is ready; Convert All to FLAC (any mode) shows only while a
  // Queue row has unsupported audio. Same button family as the row actions,
  // one size up.
  function _biIngestAllHtml(run) {
    if (!canEditLibrary() || _biTab !== 'queue') return ''
    const idle = run.status !== 'paused'
    let html = ''
    if (run.mode === 'hold') {
      const ready = (run.counts && run.counts.ready) || 0
      html += `<button type="button" class="lq-act lq-act--ingest lq-act--lg" id="bi-ingest-all"${ready && idle ? '' : ' disabled'}>Import All Ready</button>`
    }
    if (_biUnsupportedCount(run) > 0) {
      html += `<button type="button" class="lq-act lq-act--convert lq-act--lg" id="bi-convert-all"${idle ? '' : ' disabled'}>Convert All to FLAC</button>`
    }
    return html
  }

  async function _biTableInit(runId) {
    _biTableRunId = runId
    _biRowsCache = new Map()
    _biLoadedCount = 0
    _biTableExhausted = false
    _biTableLoading = false
    _biOpenRows = new Set()
    const headEl = document.getElementById('bi-thead')
    if (headEl) headEl.innerHTML = _biTheadHtml()
    const listEl = document.getElementById('bi-rows')
    if (listEl) listEl.innerHTML = ''
    await _biTableLoadMore()
    _lxQKick()
  }

  // Toggle a done row's expand panel -- delegated on the table container so
  // it keeps working across every poll's patches and appends without being
  // rewired.
  // A review-status item's folder, opened pre-scanned in the Add Recording
  // wizard -- both the Ingest and the Review button call this ("the
  // existing confirm path for that folder, in place", spec chunk 7d): a
  // bulk item flagged needs_artist/needs_date/unsupported_format cannot be
  // safely one-click auto-ingested, so both buttons land on the same form a
  // human finishes.
  async function _biOpenReviewFor(it, btn) {
    const root = (_biRun && _biRun.root) || ''
    const abs = it.abs_path || (root.replace(/\/+$/, '') + '/' + it.rel_path)
    if (btn) { btn.disabled = true; btn.textContent = '…' }
    try {
      const scan = await API.recordings.scan(abs)
      ingest.scan = scan; ingest.step = 'review'; ingest.folderPath = abs
      ingest.form = {}; ingest.tracks = []
      ingest.returnTo = _biRun ? '#/bulk-ingest/' + _biRun.id : null; ingest._resume = true
      window.location.hash = '#/ingest'
      renderIngestStep()
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Review' }
      alert(`Scan failed: ${e.message}`)
    }
  }

  // ── Queue row actions ────────────────────────────────────────────────────
  let _biConverting = new Map()   // item id -> { jobId, done, total, current, kind }
  const _biConvertErr = new Map() // item id -> message

  function _biRepaintRow(id) {
    const it = _biRowsCache.get(id)
    const el = document.querySelector(`#bi-rows > .iq-row[data-id="${id}"]`)
    if (it && el) el.outerHTML = _biRowHtml(it)
  }

  function _biSetItem(it) { _biRowsCache.set(it.id, it); _biRepaintRow(it.id) }

  // Ingest runs on the server's worker: flag the row pending now, and let the
  // poll show what became of it (gone to the library, or back in review or
  // skipped with its reason).
  async function _biIngestItem(it, btn) {
    if (btn) btn.disabled = true
    try { await API.bulkIngest.ingestItem(it.id) }
    catch (e) { if (btn) btn.disabled = false; alert(e.message); return }
    _biSetItem({ ...it, ingest_requested: true })
    await _biAfterAction()
  }

  async function _biIngestAllReady(btn) {
    if (btn) btn.disabled = true
    try { await API.bulkIngest.ingestReady(_biPageRunId) }
    catch (e) { alert(e.message); await _biAfterAction(); return }
    await _biAfterAction()
  }

  async function _biMoveItem(it, dest, btn) {
    if (btn) { btn.disabled = true; btn.textContent = '…' }
    try { await API.bulkIngest.moveItem(it.id, dest) }
    catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = TRIAGE_LABELS[dest] || dest }
      alert(`Move failed: ${e.message}`)
      return
    }
    // A moved item leaves the Queue.
    _biRowsCache.delete(it.id)
    document.querySelector(`#bi-rows > .iq-row[data-id="${it.id}"]`)?.remove()
    await _biAfterAction()
  }

  // A small in-app confirmation (same modal classes as the Settings and Write
  // Tags dialogs), never window.confirm.
  function _confirmDialog(message, confirmLabel, onConfirm) {
    const wrap = document.createElement('div')
    wrap.className = 'modal-overlay'
    wrap.innerHTML = `
      <div class="modal-card" role="dialog" aria-modal="true">
        <div class="modal-body"><p>${esc(message)}</p></div>
        <div class="modal-footer">
          <button class="btn btn-sm btn-ghost" data-cd="cancel">Cancel</button>
          <button class="btn btn-sm btn-primary" data-cd="confirm">${esc(confirmLabel)}</button>
        </div>
      </div>`
    document.body.appendChild(wrap)
    const onKey = e => { if (e.key === 'Escape') close() }
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey) }
    document.addEventListener('keydown', onKey)
    wrap.querySelector('[data-cd="cancel"]').addEventListener('click', close)
    wrap.addEventListener('click', e => { if (e.target === wrap) close() })
    wrap.querySelector('[data-cd="confirm"]').addEventListener('click', () => { close(); onConfirm() })
  }

  // Convert deletes the originals, so it always asks first.
  function _biConvert(id) {
    _confirmDialog('Replace the original files with FLAC? The originals will be deleted. This can\'t be undone.',
      'Convert to FLAC', () => { _biConvertRun(id) })
  }

  // Server-side, one folder at a time. Rows show a converting state (from the
  // server) and then turn importable; a watcher patches them meanwhile, since
  // a finished run has no poll of its own.
  let _biConvertAllTimer = null
  function _biConvertAll(btn) {
    const n = _biUnsupportedCount(_biRun || {})
    _confirmDialog(`Replace the SHN and WAV files in ${n} recordings with FLAC? The originals will be deleted. This can't be undone.`,
      'Convert All to FLAC', async () => {
        if (btn) btn.disabled = true
        try { await API.bulkIngest.convertUnsupported(_biPageRunId) }
        catch (e) { if (btn) btn.disabled = false; alert(e.message); return }
        await _biTableRefresh()
        clearInterval(_biConvertAllTimer)
        _biConvertAllTimer = setInterval(async () => {
          const ids = [..._biRowsCache.values()].filter(it => it.converting).map(it => it.id)
          if (!document.getElementById('bi-rows') || !ids.length) {
            clearInterval(_biConvertAllTimer); _biConvertAllTimer = null
            if (document.getElementById('bi-rows')) await _biAfterAction()
            return
          }
          let body
          try { body = await API.bulkIngest.itemsByIds(_biTableRunId, ids) } catch (e) { return }
          for (const it of body.items || []) {
            const prev = _biRowsCache.get(it.id)
            if (prev && prev.status === it.status && JSON.stringify(prev.converting) === JSON.stringify(it.converting)) continue
            _biSetItem(it)
          }
        }, 2000)
      })
  }

  // Same job and poll Review & Ingest uses; afterwards the server re-reads the
  // folder (reanalyze) because its format and verdict have changed.
  async function _biConvertRun(id) {
    const it = _biRowsCache.get(id)
    if (!it || _biConverting.has(id)) return
    _biConvertErr.delete(id)
    let start
    try { start = await API.quality.convert(it.abs_path) }
    catch (e) {
      _biConvertErr.set(id, e.message || 'Could not start the conversion.')
      _biRepaintRow(id)
      return
    }
    _biConverting.set(id, { jobId: start.job_id, done: 0, total: start.total || 0, current: null, kind: start.kind })
    _biRepaintRow(id)

    let final = null
    while (true) {
      await new Promise(r => setTimeout(r, 900))
      let st
      try { st = await API.quality.convertStatus(start.job_id) }
      catch (e) { _biConvertErr.set(id, e.message || 'Lost contact with the conversion job.'); break }
      const cv = _biConverting.get(id)
      if (cv) {
        cv.done = st.done || 0
        cv.total = st.total || cv.total
        cv.current = st.current || null
        // Patch the label in place; a repaint every 900ms would close menus.
        const el = document.querySelector(`[data-convert-for="${CSS.escape(String(id))}"]`)
        if (el) { const spin = el.querySelector('.lq-spin'); el.textContent = _lqConvertText(cv); if (spin) el.prepend(spin) }
      }
      if (st.status !== 'running') { final = st; break }
    }
    _biConverting.delete(id)
    if (!final || final.status === 'error') {
      if (!_biConvertErr.has(id)) _biConvertErr.set(id, (final && final.error) || 'The conversion failed.')
      _biRepaintRow(id)
      return
    }
    // Done or cancelled: whatever finished IS converted, so read the folder again.
    try { await API.bulkIngest.reanalyzeItem(id) }
    catch (e) { _biConvertErr.set(id, e.message); _biRepaintRow(id); return }
    _biSetItem({ ...it, status: 'pending', reason: null, ingest_requested: false })
    await _biAfterAction()
  }

  // ── "Apply values to every recording below" (Queue tab) ───────────────────────
  // Same block as Review & Ingest's, but the staged values are saved on the
  // run (PUT .../applied) and come back in the run payload, so they survive a
  // reload and reach the worker.
  const _biEmptyAa = () => ({
    event: '', stage: '', artist: '', venue: { id: null, name: '' },
    city: '', state: '', country: '', source: '', source_tag: '', lineage: '', notes: '',
  })
  let _biAa = _biEmptyAa()
  let _biApplyOpen = false   // the Bulk Value Apply tab is showing in place of the table (2026-10-05)
  let _biApplied = null
  let _biAaTimer = null

  function _biAaFromApplied(a) {
    const aa = _biEmptyAa()
    if (!a) return aa
    for (const k of ['artist', 'city', 'state', 'country', 'event', 'stage', 'source', 'source_tag', 'lineage', 'notes']) aa[k] = a[k] || ''
    aa.venue = { id: a.venue_id || null, name: a.venue || '' }
    return aa
  }

  function _biAaState() {
    const typed = _aaTyped(_biAa)
    if (!_aaCount(typed) && !_biApplied) return 'empty'
    return _aaFingerprint(typed) === _aaFingerprint(_biApplied) ? 'applied' : 'dirty'
  }

  function _biPaintApplyAll() {
    const el = document.getElementById('bi-applyall')
    if (!el) return
    if (!(_biApplyOpen && _biApplyAvailable(_biRun))) {
      el.innerHTML = ''
      return
    }
    el.innerHTML = _applyAllHtml({
      aa: _biAa, tabbed: true, busy: false, applied: _biApplied,
      typed: _aaTyped(_biAa), state: _biAaState(),
    })
    document.getElementById('lq-applyall-apply')?.addEventListener('click', async () => {
      const typed = _aaTyped(_biAa)
      try { await API.bulkIngest.setApplied(_biPageRunId, typed) }
      catch (e) { alert(e.message); return }
      _biApplied = typed
      _biPaintApplyAll()
    })
    document.getElementById('lq-applyall-clear')?.addEventListener('click', async () => {
      try { await API.bulkIngest.setApplied(_biPageRunId, {}) }
      catch (e) { alert(e.message); return }
      _biAa = _biEmptyAa()
      _biApplied = null
      _biPaintApplyAll()
    })
    _wireApplyAll({
      aa: _biAa, rerender: _biPaintApplyAll,
      repaintSoon: (delay = 0) => { clearTimeout(_biAaTimer); _biAaTimer = setTimeout(_biPaintApplyAll, delay) },
    })
  }

  function _biOnTableClick(ev) {
    const moveBtn = ev.target.closest('.lq-act--move')
    if (moveBtn) {
      ev.stopPropagation()
      const menu = moveBtn.nextElementSibling
      const wasHidden = menu.hidden
      mainContent.querySelectorAll('.lq-move-menu').forEach(m => { m.hidden = true })
      menu.hidden = !wasHidden
      return
    }
    const itemOf = el => _biRowsCache.get(Number(el.dataset.path))
    const moveOpt = ev.target.closest('.lq-move-opt')
    if (moveOpt) {
      const it = itemOf(moveOpt)
      if (it) _biMoveItem(it, moveOpt.dataset.dest, moveOpt)
      return
    }
    const ingestBtn = ev.target.closest('.lq-act--ingest')
    if (ingestBtn) {
      const it = itemOf(ingestBtn)
      if (it) _biIngestItem(it, ingestBtn)
      return
    }
    const reviewBtn = ev.target.closest('.lq-act--review')
    if (reviewBtn) {
      const it = itemOf(reviewBtn)
      if (it) _biOpenReviewFor(it, reviewBtn)
      return
    }
    const convertBtn = ev.target.closest('[data-convert]')
    if (convertBtn) { _biConvert(Number(convertBtn.dataset.convert)); return }
    const stopBtn = ev.target.closest('[data-convert-cancel]')
    if (stopBtn) {
      const cv = _biConverting.get(Number(stopBtn.dataset.convertCancel))
      if (cv) { stopBtn.disabled = true; API.quality.convertCancel(cv.jobId).catch(() => {}) }
      return
    }
    const caret = ev.target.closest('[data-expand]')
    if (!caret) return
    const id = Number(caret.dataset.expand)
    if (_biOpenRows.has(id)) _biOpenRows.delete(id); else _biOpenRows.add(id)
    const it = _biRowsCache.get(id)
    const rowEl = caret.closest('.iq-row')
    if (it && rowEl) rowEl.outerHTML = _biRowHtml(it)
  }

  // Notices above the queue table, shown once the run is done: the
  // scoring-continues line and the Possible Duplicates list. The counts and
  // elapsed line this block used to lead with were removed (Ryan, 2026-10-02).
  function _biNoticesHtml(run) {
    if (run.status !== 'done') return ''
    const scorable = run.scorable || 0
    const scored   = run.scored   || 0
    const scoring  = scorable > 0 && scored < scorable

    const dupes = run.duplicates || []
    const dupesHtml = dupes.length ? `
      <div class="bi-dupes">
        <h2>Possible Duplicates</h2>
        <ul>
          ${dupes.map(d => `<li>
            <a href="#/recording/${d.recording_id}">${esc(d.rel_path_basename)}</a>
            <a href="#/recording/${d.duplicate_of}">${esc(d.duplicate_basename || String(d.duplicate_of))}</a>
          </li>`).join('')}
        </ul>
      </div>` : ''

    return `${scoring ? `<p class="batch-subtitle" id="bi-scoring-line">Listening quality scored for ${scored} of ${scorable} recordings. Scoring continues in the background.</p>` : ''}${dupesHtml}`
  }

  // Header's second line: which File Handling mode is active, with a text
  // link to where it is changed. Page-specific so the shared strip used on
  // the other ingest screens stays as it was.
  function _biFileHandlingHtml(fh) {
    const text = (fh && fh.file_handling_mode === 'organize'
      ? 'File Handling set to move/organize into Trellis folders'
      : 'File Handling set to keep files as-is (do not move or copy)')
      + (fh && fh.write_tags_on_ingest ? ' and write tags on import' : '')
    return `${esc(text)} <a href="#/settings/files">Change in Settings</a>`
  }

  // The page title and Source Folder block shared by the import page and the
  // empty Add Recordings page, so the two cannot drift apart.
  function _biSrcHeaderHtml(root) {
    return `<h2>Add Recordings</h2>
            <div class="bi-src">
              <div class="bi-src-line">
                <span class="bi-src-label">Source Folder</span>
                <span class="bi-src-path" title="${esc(root || '')}">${esc(_lqShortPath(root))}</span>
                ${canEditLibrary() ? `<button type="button" class="btn btn-ghost btn-sm" id="bi-browse">Browse…</button>` : ''}
              </div>
              <div class="bi-src-line" id="bi-fh-line"></div>
            </div>`
  }

  async function _wireBiFileHandling() {
    const el = document.getElementById('bi-fh-line')
    if (!el) return
    try {
      el.innerHTML = _biFileHandlingHtml(await fileHandling())
    } catch (e) { /* leave it empty rather than showing a broken line */ }
  }

  // Progress bar -- same markup as Review & Ingest's own (.lq-progress),
  // processed/found plus the in-progress folder name, then rate and ETA in
  // the same muted style once they are available. Hidden once done.
  function _biProgressHtml(run) {
    if (run.status === 'done') return ''
    const c = run.counts || {}
    const found    = Object.values(c).reduce((a, b) => a + b, 0)
    const added    = c.ingested || 0
    const review   = c.review || 0
    const skipped  = c.skipped || 0
    const failed   = run.failed || 0
    const processed = added + review + skipped + failed
    const rate = _biRateAndEta(processed, found)
    return `
      <div class="lq-progress">
        <div class="lq-progress-bar"><i style="width:${
          Math.round(100 * processed / Math.max(1, found))}%"></i></div>
        <span class="lq-progress-count">${processed}/${found}</span>
        ${run.now ? `<span class="lq-progress-current">${esc(run.now)}</span>` : ''}
        ${rate ? `<span class="lq-progress-current">${esc(rate.rateText)}</span>` : ''}
        ${rate && rate.etaText ? `<span class="lq-progress-current">${esc(rate.etaText)}</span>` : ''}
      </div>`
  }

  // The mode the import page's toggle shows. Follows the run while it runs
  // or is paused; once done the person's choice applies to the next scan.
  let _biScanMode = 'hold'
  let _biScanHtml = ''

  // Repaint the toggle/scan row from the polled run, only when it changed so a
  // poll never rebuilds a control under the pointer.
  function _biPaintScan(run) {
    const el = document.getElementById('bi-scan')
    if (!el) return
    if (!canEditLibrary()) { el.innerHTML = ''; _biScanHtml = ''; return }
    if (run.status === 'running' || run.status === 'paused') _biScanMode = run.mode
    const html = _biScanControlsHtml({ run, mode: _biScanMode })
    if (html === _biScanHtml) return
    _biScanHtml = html
    el.innerHTML = html
  }

  function _biWireScan() {
    document.getElementById('bi-scan')?.addEventListener('click', async e => {
      const run = _biRun
      if (!run) return
      const m = e.target.closest('[data-bi-mode]')
      if (m) {
        if (m.disabled) return
        _biScanMode = m.dataset.biMode
        _biRememberMode(run.placement === 'in_place', _biScanMode)
        _biPaintScan(run)
        return
      }
      const b = e.target.closest('[data-scan]')
      if (!b) return
      const act = b.dataset.scan
      if (act === 'reset') {
        _confirmDialog('Remove every recording from the queue? Your files are not touched.', 'Reset Queue', async () => {
          try { await API.bulkIngest.resetQueue(run.id) } catch (err) {}
          await refreshBulkIngestStatus()
          _biPickerPath = run.root
          window.location.hash = '#/ingest?new=1'
        })
        return
      }
      if (act === 'pause' || act === 'resume') {
        try { await API.bulkIngest[act](run.id) } catch (err) {}
        await refreshBulkIngestStatus()
        renderBulkIngestView(run.id)
      } else if (act === 'rescan') {
        let again = null
        try { again = await API.bulkIngest.start(run.root, _biScanMode) } catch (err) {}
        await refreshBulkIngestStatus()
        renderBulkIngestView(again && again.id)
      }
    })
  }

  async function _paintBulkIngestPage(run) {
    const done = run.status === 'done'
    const key = run.id + ':' + (done ? 'done' : 'active')
    const isFreshBuild = _biLastPaintedKey !== key
    _biLastPaintedKey = key

    // Incremental path: same run, same done/active bucket as last paint (a
    // routine 3s poll while running, or a scoring poll while done). Only the
    // header body and the progress bar are repainted wholesale; the table
    // itself is patched by id, never rebuilt (spec).
    if (!isFreshBuild) {
      _biPaintScan(run)
      const noticesEl = document.getElementById('bi-notices')
      if (noticesEl) noticesEl.innerHTML = _biNoticesHtml(run)
      const progEl = document.getElementById('bi-progress-wrap')
      if (progEl) progEl.innerHTML = _biProgressHtml(run)
      const tabBefore = _biTab
      _biPaintTabs(run)
      // The open tab emptied and fell back to Queue: reload what it shows.
      if (_biTab !== tabBefore) {
        _biPaintApplyAll()
        await _biTableInit(run.id)
        return
      }
      _lxQKick()   // rows that arrived since the last tick, and a run that has just finished scanning
      if (!done) await _biTableTick(run)
      return
    }

    // Full (re)build: first paint of this view, or a status bucket change
    // (running/paused -> done).
    setMainHTML(`
      <div class="batch-shell lq-shell" id="bi-shell">
        <div class="lq-header">
          <div style="min-width:0">
            ${_biSrcHeaderHtml(run.root)}
          </div>
        </div>
        <div id="bi-scan" class="bi-scan"></div>

        <div id="bi-progress-wrap">${_biProgressHtml(run)}</div>

        <div class="pp-tabs bi-tabs" id="bi-tabs" role="tablist">${_biTabsHtml(run)}</div>
        <div class="bi-tab-note-wrap" id="bi-tab-note">${_biTabNoteHtml(run)}</div>
        <div id="bi-applyall"></div>
        <div id="bi-ingest-all-wrap" class="bi-ingest-all-wrap">${_biIngestAllHtml(run)}</div>
        <div id="bi-lomax-line" class="bi-lomax-line"></div>
        <div id="bi-notices">${_biNoticesHtml(run)}</div>

        <div class="lq-cards lq-cards--compact" id="bi-table">
          <div id="bi-thead">${_biTheadHtml()}</div>
          <div id="bi-rows"></div>
          <div id="bi-sentinel"></div>
        </div>
      </div>`)
    _biApplyMode()
    _biPaintApplyAll()
    document.addEventListener('click', _closeMoveMenus)
    document.getElementById('bi-ingest-all-wrap')?.addEventListener('click', e => {
      const b = e.target.closest('#bi-ingest-all')
      if (b) _biIngestAllReady(b)
      const c = e.target.closest('#bi-convert-all')
      if (c) _biConvertAll(c)
    })

    _biScanMode = run.mode
    _biScanHtml = ''
    _biPaintScan(run)
    _biWireScan()
    _wireBiFileHandling()
    document.getElementById('bi-browse')?.addEventListener('click', () => { location.hash = '#/ingest?new=1' })
    document.getElementById('bi-table')?.addEventListener('click', _biOnTableClick)
    document.getElementById('bi-tabs')?.addEventListener('click', async e => {
      const t = e.target.closest('[data-bitab]')
      if (!t) return
      if (t.dataset.bitab === 'apply') {
        if (_biApplyOpen) return
        _biApplyOpen = true
        _biPaintTabs(_biRun || run)
        _biApplyMode()
        _biPaintApplyAll()
        document.getElementById('lq-apply-artist')?.focus()
        return
      }
      const wasApply = _biApplyOpen
      _biApplyOpen = false
      _biApplyMode()
      if (t.dataset.bitab === _biTab) {
        if (wasApply) { _biPaintTabs(_biRun || run); _biPaintApplyAll() }
        return
      }
      _biTab = t.dataset.bitab
      if (_biTab !== 'queue') _biReviewFilter = false
      _biPaintTabs(_biRun || run)
      _biPaintApplyAll()
      await _biTableInit((_biRun || run).id)
    })
    await _biTableInit(run.id)
  }

  // ══════════════════════════════════════════════════════════════════════════
  // Archive Downloads (spec "Archive Downloads v1", sections 2, 6, 7, 8)
  //
  // Three surfaces share one piece of state, DL:
  //   - the Live Music Archive catalog at #/archive/lma (+ its drawer),
  //   - the queue tab and panel, mounted once on the content column so they
  //     exist on every page,
  //   - the Downloads page at #/downloads.
  //
  // One queue per install, one download at a time; the server worker does the
  // work and this file only mirrors it. DL.jobs is the last /queue answer and
  // everything painted here is derived from it, so there is no second copy to
  // drift. The poll runs only while something is queued or active.
  //
  // Every user-visible string below is a proposal pending Ryan's approval
  // (spec section 8); labels come from the approved mockups.
  // ══════════════════════════════════════════════════════════════════════════

  const DL = {
    jobs: [],            // last /api/downloads/queue answer
    paused: false,
    loaded: false,       // true once a /queue answer has been applied
    byKey: new Map(),    // 'source:id' -> job, queued or active only
    booted: false,
    refused: false,      // the server said 403 once: do not ask again this session
    tabShown: false,     // latched: a Download click this session, or live jobs at launch
    panelOpen: false,
    timer: null,
    polling: false,
    folder: null,        // last /api/downloads/folder answer
    folderCount: null,
    dragging: false,
    dragStart: [],
    chromeReady: false,
    docWired: false,
  }
  // The server is admin-only on every Archive Downloads route, and
  // canEditLibrary() is also true for the archivist role, so both must hold.
  const _dlAllowed = () => isAdmin() && canEditLibrary()
  const _dlIsLive = j => j.status === 'queued' || j.status === 'active'

  const ARC_SORTS = [['newest', 'Newest'], ['date', 'Show date'], ['az', 'A–Z']]
  const ARC_SRC_CLASSES = new Set(['sbd', 'aud', 'mtx', 'fm'])

  const _arc = {
    sort: 'newest', q: '', page: 0, hasMore: false, loading: false, seq: 0,
    items: [], byId: new Map(), io: null, qTimer: null,
    openId: null, tab: 'tracks', detail: null, detailErr: '', detailSeq: 0,
  }

  // ── Small formatters ───────────────────────────────────────────────────────

  // "2h", "3d" -- how long ago an item reached the archive.
  function _arcAgo(iso) {
    const t = Date.parse(iso)
    if (!iso || isNaN(t)) return ''
    const mins = Math.max(0, Math.floor((Date.now() - t) / 60000))
    if (mins < 60) return `${mins}m`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs}h`
    const days = Math.floor(hrs / 24)
    if (days < 30) return `${days}d`
    if (days < 365) return `${Math.floor(days / 30)}mo`
    return `${Math.floor(days / 365)}y`
  }

  // h:mm:ss for a whole show, m:ss under an hour.
  function _arcClock(secs) {
    if (!secs) return ''
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    const s = Math.floor(secs % 60)
    const ss = String(s).padStart(2, '0')
    return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
  }

  // Downloads page "Modified": Today / Yesterday / Sep 28.
  function _dlModified(v) {
    if (v == null || v === '') return ''
    const d = new Date(typeof v === 'number' && v < 1e12 ? v * 1000 : v)
    if (isNaN(d.getTime())) return String(v)
    const today = new Date()
    const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
    const diff = Math.round((day(today) - day(d)) / 86400000)
    if (diff === 0) return 'Today'
    if (diff === 1) return 'Yesterday'
    const opts = d.getFullYear() === today.getFullYear()
      ? { month: 'short', day: 'numeric' }
      : { month: 'short', day: 'numeric', year: 'numeric' }
    return d.toLocaleDateString('en-US', opts)
  }

  const _dlPct = j => (j && j.total_bytes)
    ? Math.max(0, Math.min(100, Math.round(j.done_bytes * 100 / j.total_bytes))) : 0

  // ── Shared state, poll ─────────────────────────────────────────────────────

  function _dlRebuildKeys() {
    DL.byKey = new Map()
    DL.jobs.forEach(j => { if (_dlIsLive(j)) DL.byKey.set(`${j.source}:${j.source_id}`, j) })
  }

  // Returns true when a job moved to done since the last answer, which means
  // the Downloads folder changed on disk.
  function _dlApplyQueue(res) {
    const prev = new Map(DL.jobs.map(j => [j.id, j.status]))
    DL.jobs = (res && res.jobs) || []
    DL.paused = !!(res && res.paused)
    DL.loaded = true
    _dlRebuildKeys()
    // Any job that WAS live and no longer is (done, failed, or cancelled and
    // therefore gone from the list) means the folder list changed: a cancel
    // deletes its folder server-side (Ryan, 2026-10-01), and nothing else
    // would tell the Downloads page, because polling stops once idle.
    const now = new Map(DL.jobs.map(j => [j.id, j]))
    return [...prev].some(([id, st]) =>
      (st === 'queued' || st === 'active') && !(now.has(id) && _dlIsLive(now.get(id))))
  }

  function _dlUpsert(job) {
    const i = DL.jobs.findIndex(j => j.id === job.id)
    if (i >= 0) DL.jobs[i] = job
    else DL.jobs.push(job)
    _dlRebuildKeys()
  }

  function _dlSetFolder(res) {
    DL.folder = res
    DL.folderCount = ((res && res.folders) || []).length
    document.querySelectorAll('[data-dl-count]').forEach(el => {
      el.textContent = DL.folderCount || ''
    })
  }

  async function _dlRefreshFolder() {
    if (!_dlAllowed()) return null
    try { _dlSetFolder(await API.downloads.folder()) } catch (_) { /* keep the last answer */ }
    return DL.folder
  }

  async function _dlRefreshQueue() {
    if (!_dlAllowed()) return
    let res
    try { res = await API.downloads.queue() } catch (_) { return }
    const finished = _dlApplyQueue(res)
    const onPage = (window.location.hash || '').split('?')[0] === '#/downloads'
    if (finished || (onPage && DL.jobs.some(_dlIsLive))) await _dlRefreshFolder()
    _dlAfterChange()
  }

  // Everything that mirrors the queue repaints from here.
  function _dlAfterChange() {
    _dlPaintChrome()
    _dlPaintPanel()
    _arcRepaintActions()
    _dlRepaintDownloadsPage()
    _dlSchedule()
  }

  function _dlSchedule() {
    // Nothing to watch while paused with nothing active, nor when this
    // session may not use the feature (Playback, archivist). Resume and a new
    // enqueue both refresh the queue, which re-evaluates this.
    const active = DL.jobs.some(j => j.status === 'active')
    const busy = _dlAllowed() && DL.jobs.some(_dlIsLive) && !(DL.paused && !active)
    if (busy && !DL.timer) {
      DL.timer = setInterval(async () => {
        if (DL.polling) return
        DL.polling = true
        try { await _dlRefreshQueue() } finally { DL.polling = false }
      }, 2000)
    } else if (!busy && DL.timer) {
      clearInterval(DL.timer)
      DL.timer = null
    }
  }

  // Once per session, from renderSidebar: the queue's launch state decides
  // whether the tab shows, and the folder answer feeds the sidebar count.
  async function _dlBoot() {
    if (DL.booted || DL.refused || !_dlAllowed()) return
    DL.booted = true
    _dlEnsureChrome()
    try { _dlApplyQueue(await API.downloads.queue()) } catch (e) { if (e && e.status === 403) DL.refused = true; else DL.booted = false; return }
    if (DL.jobs.some(_dlIsLive)) DL.tabShown = true
    _dlAfterChange()
    _dlRefreshFolder()
  }

  function _dlSidebarSync() {
    _dlPaintChrome()
    _dlPaintPanel()
    _dlSchedule()
    _dlBoot()
  }

  // ── Queue tab and panel ────────────────────────────────────────────────────

  function _dlLayout() {
    const col = document.querySelector('.content-column')
    if (col) col.style.setProperty('--dlq-top', mainContent.offsetTop + 'px')
  }

  function _dlEnsureChrome() {
    if (DL.chromeReady) return
    const col = document.querySelector('.content-column')
    if (!col) return
    DL.chromeReady = true
    col.insertAdjacentHTML('beforeend', `
      <button type="button" class="dlq-tab" id="dlq-tab" aria-controls="dlq-panel" aria-expanded="false" hidden></button>
      <aside class="dlq-panel" id="dlq-panel" aria-label="Downloads" aria-hidden="true"></aside>`)
    const tab = document.getElementById('dlq-tab')
    const panel = document.getElementById('dlq-panel')
    tab.addEventListener('click', () => {
      DL.panelOpen = !DL.panelOpen
      if (DL.panelOpen && !DL.folder) _dlRefreshFolder().then(() => _dlPaintPanel())
      _dlPaintPanel()
    })
    window.addEventListener('resize', _dlLayout)

    panel.addEventListener('click', async e => {
      const b = e.target.closest('[data-dlq]')
      if (!b) return
      const act = b.dataset.dlq
      const id = parseInt(b.dataset.id)
      try {
        if (act === 'close') { DL.panelOpen = false; _dlPaintPanel(); return }
        if (act === 'pause') await API.downloads.pause()
        else if (act === 'resume') await API.downloads.resume()
        else if (act === 'cancel') {
          await API.downloads.cancel(id)
          // The worker deletes an active job's folder a moment AFTER the
          // cancel returns, so look again once it has had time to.
          setTimeout(() => _dlRefreshFolder().then(_dlAfterChange), 2500)
        }
        else if (act === 'retry') await API.downloads.retry(id)
        else if (act === 'remove') await API.downloads.remove(id)
        else if (act === 'clear') {
          const gone = DL.jobs.filter(j => j.status === 'done' || j.status === 'failed')
          for (const j of gone) await API.downloads.remove(j.id)
        }
      } catch (err) {
        alert(err.message)
      }
      await _dlRefreshQueue()
    })

    // Drag to reorder Up next. Native drag and drop on the rows; the poll
    // leaves the panel alone while a drag is in flight (_dlPaintPanel).
    panel.addEventListener('dragstart', e => {
      const row = e.target.closest('.dlq-q')
      if (!row) return
      DL.dragging = true
      DL.dragStart = [...panel.querySelectorAll('.dlq-q')].map(r => parseInt(r.dataset.id))
      row.classList.add('dragging')
      e.dataTransfer.effectAllowed = 'move'
      e.dataTransfer.setData('text/plain', row.dataset.id)
    })
    panel.addEventListener('dragover', e => {
      const list = e.target.closest('.dlq-list')
      const dragged = panel.querySelector('.dlq-q.dragging')
      if (!DL.dragging || !list || !dragged) return
      e.preventDefault()
      const rows = [...list.querySelectorAll('.dlq-q:not(.dragging)')]
      const after = rows.find(r => {
        const box = r.getBoundingClientRect()
        return e.clientY < box.top + box.height / 2
      })
      if (after) list.insertBefore(dragged, after)
      else list.appendChild(dragged)
    })
    panel.addEventListener('dragend', async () => {
      const order = [...panel.querySelectorAll('.dlq-q')].map(r => parseInt(r.dataset.id))
      DL.dragging = false
      panel.querySelectorAll('.dlq-q.dragging').forEach(r => r.classList.remove('dragging'))
      if (order.length && order.some((id, i) => id !== DL.dragStart[i])) {
        try { await API.downloads.reorder(order) } catch (err) { alert(err.message) }
      }
      await _dlRefreshQueue()
    })
  }

  // Ring + "n of m". m counts the live jobs plus the ones finished since the
  // oldest live job was queued, so yesterday's finished downloads do not
  // inflate it.
  function _dlTabState() {
    const live = DL.jobs.filter(_dlIsLive)
    const active = DL.jobs.find(j => j.status === 'active')
    let label = ''
    if (live.length) {
      const t0 = Math.min(...live.map(j => Date.parse(j.created_at) || Infinity))
      const done = DL.jobs.filter(j => j.status === 'done'
        && (Date.parse(j.finished_at) || 0) >= t0).length
      label = `${done + 1} of ${done + live.length}`
    }
    const frac = active ? _dlPct(active) / 100 : (live.length ? 0 : 1)
    return { frac, label }
  }

  function _dlPaintChrome() {
    const tab = document.getElementById('dlq-tab')
    const hash = (window.location.hash || '').split('?')[0]
    document.body.classList.toggle('archive-on', hash === '#/archive/lma')
    if (!tab) return
    const show = _dlAllowed() && DL.tabShown
    tab.hidden = !show
    document.body.classList.toggle('dlq-on', show)
    if (show) {
      const { frac, label } = _dlTabState()
      const html = `
        <svg viewBox="0 0 36 36" width="18" height="18" aria-hidden="true">
          <circle class="dlq-ring-track" cx="18" cy="18" r="15" fill="none" stroke-width="4"></circle>
          <circle class="dlq-ring-arc" cx="18" cy="18" r="15" fill="none" stroke-width="4"
                  stroke-dasharray="${(frac * 94.25).toFixed(1)} 94.25" transform="rotate(-90 18 18)"></circle>
        </svg>
        <span>Downloads</span>${label ? `<span class="dlq-tab-n">${esc(label)}</span>` : ''}`
      if (tab._html !== html) { tab._html = html; tab.innerHTML = html }
    }
    _dlLayout()
  }

  function _dlPanelHtml() {
    const active = DL.jobs.find(j => j.status === 'active')
    const queued = DL.jobs.filter(j => j.status === 'queued')
      .sort((a, b) => (a.position || 0) - (b.position || 0))
    const finished = DL.jobs.filter(j => j.status === 'done' || j.status === 'failed')
      .sort((a, b) => String(b.finished_at || '').localeCompare(String(a.finished_at || '')))
    const meta = j => [j.date, j.venue].filter(Boolean).join(' · ')
    const bytes = j => j.total_bytes
      ? `${fmtBytes(j.done_bytes)} of ${fmtBytes(j.total_bytes)}`
      : (j.done_bytes ? fmtBytes(j.done_bytes) : '')
    const dated = j => [j.date, j.total_bytes ? fmtBytes(j.total_bytes) : ''].filter(Boolean).join(' · ')

    const now = active ? `
      <div class="dlq-sec">
        <div class="dlq-h">Now</div>
        <div class="dlq-now">
          <div class="dlq-now-top">
            <span class="dlq-name">${esc(active.artist || '')}</span>
            <button type="button" class="dlq-btn dlq-btn--flat" data-dlq="cancel" data-id="${active.id}">Cancel</button>
          </div>
          <span class="dlq-sub">${esc(meta(active))}</span>
          <div class="dlq-bar"><div class="dlq-bar-fill" style="width:${_dlPct(active)}%"></div></div>
          <span class="dlq-sub dlq-sub--dim">${esc(bytes(active))}</span>
        </div>
      </div>` : ''

    const next = queued.length ? `
      <div class="dlq-sec">
        <div class="dlq-h">Up next</div>
        <div class="dlq-list">${queued.map(j => `
          <div class="dlq-q" draggable="true" data-id="${j.id}">
            ${icon('grip-vertical', 'dlq-grip')}
            <span class="dlq-col">
              <span class="dlq-name">${esc(j.artist || '')}</span>
              <span class="dlq-sub dlq-sub--dim">${esc(dated(j))}</span>
            </span>
            <button type="button" class="dlq-btn" data-dlq="remove" data-id="${j.id}" aria-label="Remove from queue">${icon('x')}</button>
          </div>`).join('')}
        </div>
      </div>` : ''

    const done = finished.length ? `
      <div class="dlq-sec">
        <div class="dlq-h dlq-h--row">Done
          <button type="button" class="dlq-btn dlq-btn--flat" data-dlq="clear">Clear</button>
        </div>
        ${finished.map(j => j.status === 'failed' ? `
          <div class="dlq-r">
            <span class="dlq-col">
              <span class="dlq-name">${esc(j.artist || '')}</span>
              <span class="dlq-sub dlq-sub--bad">${esc(j.error || '')}</span>
            </span>
            <button type="button" class="dlq-btn dlq-btn--acc" data-dlq="retry" data-id="${j.id}">Retry</button>
          </div>` : `
          <div class="dlq-r">
            <span class="dlq-col">
              <span class="dlq-name">${esc(j.artist || '')}</span>
              <span class="dlq-sub dlq-sub--dim">${esc(dated(j))}</span>
            </span>
            <a class="dlq-link" href="#/downloads">Downloads</a>
          </div>`).join('')}
      </div>` : ''

    return `
      <div class="dlq-head">
        <span class="dlq-title">Downloads</span>
        <button type="button" class="dlq-btn" data-dlq="${DL.paused ? 'resume' : 'pause'}">${DL.paused ? 'Resume' : 'Pause'}</button>
        <button type="button" class="dlq-btn" data-dlq="close" aria-label="Close">${icon('x')}</button>
      </div>
      <div class="dlq-body">${now}${next}${done}</div>
      <div class="dlq-foot"><span class="dlq-path">${esc((DL.folder && DL.folder.path) || '')}</span></div>`
  }

  function _dlPaintPanel() {
    const panel = document.getElementById('dlq-panel')
    const tab = document.getElementById('dlq-tab')
    if (!panel) return
    const open = DL.panelOpen && DL.tabShown && _dlAllowed()
    if (!open) DL.panelOpen = false
    panel.classList.toggle('open', open)
    panel.setAttribute('aria-hidden', open ? 'false' : 'true')
    if (tab) {
      tab.setAttribute('aria-expanded', open ? 'true' : 'false')
      tab.classList.toggle('on', open)
    }
    if (!open || DL.dragging) return
    const html = _dlPanelHtml()
    if (panel._html === html) return
    const prev = panel.querySelector('.dlq-body')
    const top = prev ? prev.scrollTop : 0
    panel._html = html
    panel.innerHTML = html
    const body = panel.querySelector('.dlq-body')
    if (body) body.scrollTop = top
  }

  // ── Catalog: #/archive/lma ─────────────────────────────────────────────────

  function _arcJob(it) {
    const j = DL.byKey.get(`${it.source || 'lma'}:${it.id}`) || (DL.loaded ? null : it.job)
    return j && _dlIsLive(j) ? j : null
  }

  function _arcActionHtml(it) {
    const own = (it.in_library && it.in_library.length) ? '<span class="arc-owned">In library</span>' : ''
    if (it.stream_only) return `${own}<span class="arc-note">Stream only</span>`
    const j = _arcJob(it)
    if (j) return `${own}<span class="arc-state">${j.status === 'active' ? 'Downloading' : 'Queued'}</span>`
    return `${own}<button type="button" class="arc-act" data-act="dl" data-id="${esc(it.id)}">Download</button>`
  }

  function _arcRowHtml(it) {
    const st = String(it.source_type || '').toLowerCase()
    return `
      <div class="brow arc-row${_arc.openId === it.id ? ' sel' : ''}" data-id="${esc(it.id)}">
        <span class="brow-perf">${esc(it.artist || '')}</span>
        <span class="brow-date">${esc(it.date || '')}</span>
        <span class="brow-venue">${esc(it.venue || '')}</span>
        <span class="brow-loc">${esc(it.location || '')}</span>
        <span class="arc-fmt">${esc(it.format || '')}</span>
        <span class="brow-srccell">${st ? `<span class="arc-src arc-src--${ARC_SRC_CLASSES.has(st) ? st : 'other'}">${esc(it.source_type)}</span>` : ''}</span>
        <span class="arc-num">${esc(it.size_bytes ? fmtBytes(it.size_bytes) : '')}</span>
        <span class="arc-num arc-num--dim">${esc(_arcAgo(it.added))}</span>
        <span class="arc-actcell">${_arcActionHtml(it)}</span>
      </div>`
  }

  function _arcMore(has) {
    const moreEl = document.getElementById('arc-more')
    if (!moreEl) return
    _arc.io?.disconnect()
    if (!has) { moreEl.innerHTML = ''; return }
    moreEl.innerHTML = '<div class="browse-sentinel" id="arc-sentinel"></div>'
    _arc.io = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) _arcLoad(false)
    }, { root: mainContent, rootMargin: '400px' })
    _arc.io.observe(document.getElementById('arc-sentinel'))
  }

  const _arcSay = text => {
    const el = document.getElementById('arc-msg')
    if (el) el.innerHTML = text ? `<div class="arc-err">${esc(text)}</div>` : ''
  }

  async function _arcLoad(reset) {
    const rowsEl = () => document.getElementById('arc-rows')
    if (!rowsEl()) return
    if (reset) {
      _arc.seq++
      _arc.loading = false
      _arc.page = 0
      _arc.hasMore = false
      _arc.items = []
      _arc.byId = new Map()
      rowsEl().innerHTML = ''
      _arcMore(false)
      _arcSay('')
    }
    if (_arc.loading) return
    const seq = _arc.seq
    _arc.loading = true
    let res
    try {
      res = _arc.q
        ? await API.archive.lma.search(_arc.q, _arc.page + 1, _arc.sort)
        : await API.archive.lma.recent(_arc.page + 1, _arc.sort)
    } catch (e) {
      if (seq !== _arc.seq) return
      _arc.loading = false
      _arcMore(false)
      _arcSay(e.message)
      return
    }
    if (seq !== _arc.seq || !rowsEl()) return
    _arc.loading = false
    const fresh = ((res && res.items) || []).filter(it => it && it.id != null && !_arc.byId.has(it.id))
    _arc.page = (res && res.page) || _arc.page + 1
    _arc.hasMore = !!(res && res.has_more)
    fresh.forEach(it => { _arc.items.push(it); _arc.byId.set(it.id, it) })
    rowsEl().insertAdjacentHTML('beforeend', fresh.map(_arcRowHtml).join(''))
    _arcMore(_arc.hasMore)
  }

  function _arcRepaintActions() {
    const list = document.getElementById('arc-rows')
    if (list) {
      list.querySelectorAll('.arc-row').forEach(row => {
        const it = _arc.byId.get(row.dataset.id)
        const cell = row.querySelector('.arc-actcell')
        if (!it || !cell) return
        const html = _arcActionHtml(it)
        if (cell.innerHTML !== html) cell.innerHTML = html
      })
    }
    if (_arc.openId != null) _arcPaintDrawer()
  }

  async function _arcEnqueue(it) {
    try {
      const job = await API.downloads.enqueue(it.source || 'lma', it.id)
      if (job && job.id != null) _dlUpsert(job)
      DL.tabShown = true
      _dlAfterChange()
    } catch (e) {
      // 409: already queued or active, which the refresh below resolves.
      if (e.status !== 409) { alert(e.message); return }
    }
    await _dlRefreshQueue()
  }

  async function renderArchiveLmaView() {
    setActiveNav('archive-lma')
    setActiveArtist(null)
    setNavCurrent('Live Music Archive')
    _arcCloseDrawer()
    _arc.sort = 'newest'
    _arc.q = ''
    _arc.openId = null
    _arc.seq++

    setMainHTML(`
      <div class="archive-space" id="arc-page">
        <div class="arc-sticky">
          <div class="arc-head">
            <h1 class="arc-title">Live Music Archive</h1>
            <label class="arc-search">${icon('search')}<input type="text" id="arc-q" aria-label="Search the archive" placeholder="Artist, venue or date" autocomplete="off" spellcheck="false"></label>
          </div>
          <div class="arc-bar">
            <div class="browse-sorts" id="arc-sorts">
              ${ARC_SORTS.map(([k, label]) =>
                `<button type="button" class="sortb${k === _arc.sort ? ' on' : ''}" data-sort="${k}">${esc(label)}</button>`).join('')}
            </div>
          </div>
          <div class="brow arc-row arc-cols">
            <span class="arc-hd">Artist</span><span class="arc-hd">Date</span><span class="arc-hd">Venue</span><span class="arc-hd">Location</span>
            <span class="arc-hd arc-hd--r">Fmt</span><span class="arc-hd arc-hd--r">Src</span><span class="arc-hd arc-hd--r">Size</span><span class="arc-hd arc-hd--r">Added</span><span></span>
          </div>
        </div>
        <div class="arc-rows" id="arc-rows"></div>
        <div class="arc-msg" id="arc-msg"></div>
        <div class="arc-more" id="arc-more"></div>
      </div>`)

    const input = document.getElementById('arc-q')
    input.addEventListener('input', () => {
      clearTimeout(_arc.qTimer)
      _arc.qTimer = setTimeout(() => {
        const q = input.value.trim()
        if (q !== _arc.q) { _arc.q = q; _arcLoad(true) }
      }, 300)
    })
    input.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return
      clearTimeout(_arc.qTimer)
      const q = input.value.trim()
      if (q !== _arc.q) { _arc.q = q; _arcLoad(true) }
    })
    document.getElementById('arc-sorts').addEventListener('click', e => {
      const b = e.target.closest('.sortb')
      if (!b || b.dataset.sort === _arc.sort) return
      _arc.sort = b.dataset.sort
      document.querySelectorAll('#arc-sorts .sortb').forEach(x => x.classList.toggle('on', x === b))
      _arcLoad(true)
    })
    document.getElementById('arc-rows').addEventListener('click', e => {
      const row = e.target.closest('.arc-row')
      if (!row) return
      const it = _arc.byId.get(row.dataset.id)
      if (!it) return
      if (e.target.closest('[data-act="dl"]')) { _arcEnqueue(it); return }
      _arcOpenDrawer(it)
    })
    _arcLoad(true)
  }

  // ── Drawer ─────────────────────────────────────────────────────────────────

  function _arcCloseDrawer() {
    _arc.openId = null
    _arc.detail = null
    _arc.detailErr = ''
    _arc.detailSeq++
    document.getElementById('arc-drawer')?.remove()
    document.querySelectorAll('#arc-rows .arc-row.sel').forEach(r => r.classList.remove('sel'))
  }

  function _arcDrawerHtml(it, d) {
    const x = d ? Object.assign({}, it, d) : it
    const tracks = (d && d.tracks) || []
    const secs = tracks.reduce((n, t) => n + (t.length_s || 0), 0)
    const prov = [['Source', d && d.source_text], ['Lineage', d && d.lineage],
                  ['Taper', d && d.taper], ['Transferer', d && d.transferer]].filter(([, v]) => v)
    const size = (d && d.audio_bytes) || it.size_bytes
    const facts = [
      tracks.length ? `${tracks.length} track${tracks.length === 1 ? '' : 's'}` : '',
      _arcClock(secs),
      it.format || '',
      size ? fmtBytes(size) : '',
    ].filter(Boolean).join(' · ')
    const own = (x.in_library && x.in_library.length) ? '<span class="arc-owned">In library</span>' : ''
    const j = _arcJob(it)
    const btn = it.stream_only
      ? '<button type="button" class="arc-primary" disabled>Stream only</button>'
      : j
        ? `<button type="button" class="arc-primary" disabled>${j.status === 'active' ? 'Downloading' : 'Queued'}</button>`
        : `<button type="button" class="arc-primary" data-act="dl" data-id="${esc(it.id)}">Download</button>`
    const url = (d && d.url) || `https://archive.org/details/${encodeURIComponent(it.id)}`
    const tabs = [['tracks', 'Tracks'], ['info', 'Info File']]
    if (d && Array.isArray(d.files)) tabs.push(['files', 'Files'])
    if (!tabs.some(([k]) => k === _arc.tab)) _arc.tab = 'tracks'

    let body = ''
    if (_arc.detailErr) body = `<div class="arc-err">${esc(_arc.detailErr)}</div>`
    else if (_arc.tab === 'tracks') {
      body = tracks.map(t => `
        <div class="arc-tr">
          <span class="arc-tr-n">${esc(String(t.n).padStart(2, '0'))}</span>
          <span class="arc-tr-t">${esc(t.title || '')}</span>
          <span class="arc-tr-l">${esc(t.length_s ? fmtDuration(t.length_s) : '')}</span>
        </div>`).join('')
    } else if (_arc.tab === 'info') {
      body = `<pre class="info-file-content">${esc((d && d.info_text) || '')}</pre>`
    } else if (_arc.tab === 'files') {
      body = d.files.map(f => `
        <div class="arc-tr arc-tr--file">
          <span class="arc-tr-t">${esc(f.name || '')}</span>
          <span class="arc-tr-l">${esc(f.size_bytes ? fmtBytes(f.size_bytes) : '')}</span>
        </div>`).join('')
    }

    return `
      <div class="arc-dr-head">
        <div class="arc-dr-top">
          <h2 class="arc-dr-title">${esc(it.artist || '')}</h2>
          <button type="button" class="arc-dr-x" data-act="close" aria-label="Close">${icon('x')}</button>
        </div>
        <div class="arc-dr-date">${esc(it.date || '')}</div>
        <div class="arc-dr-venue">${esc([it.venue, it.location].filter(Boolean).join(' · '))}</div>
        ${prov.length ? `<div class="arc-prov">${prov.map(([k, v]) =>
          `<span class="arc-lb">${esc(k)}</span><span class="arc-vl">${esc(v)}</span>`).join('')}</div>` : ''}
        <div class="arc-dr-act">
          ${btn}${own}
          <span class="arc-facts">${esc(facts)}</span>
          <span class="arc-dr-gap"></span>
          <a class="arc-ext" href="${esc(url)}" target="_blank" rel="noopener noreferrer">archive.org</a>
        </div>
      </div>
      <div class="arc-tabs">${tabs.map(([k, label]) =>
        `<button type="button" class="arc-tab${k === _arc.tab ? ' on' : ''}" data-tab="${k}">${esc(label)}</button>`).join('')}</div>
      <div class="arc-dr-body">${body}</div>`
  }

  function _arcPaintDrawer() {
    const el = document.getElementById('arc-drawer')
    const it = _arc.byId.get(_arc.openId)
    if (!el || !it) return
    const html = _arcDrawerHtml(it, _arc.detail)
    if (el._html === html) return
    const prev = el.querySelector('.arc-dr-body')
    const top = prev ? prev.scrollTop : 0
    el._html = html
    el.innerHTML = html
    const body = el.querySelector('.arc-dr-body')
    if (body) body.scrollTop = top
  }

  async function _arcOpenDrawer(it) {
    const col = document.querySelector('.content-column')
    if (!col) return
    _dlLayout()
    document.getElementById('arc-drawer')?.remove()
    _arc.openId = it.id
    _arc.tab = 'tracks'
    _arc.detail = null
    _arc.detailErr = ''
    const seq = ++_arc.detailSeq
    document.querySelectorAll('#arc-rows .arc-row').forEach(r =>
      r.classList.toggle('sel', r.dataset.id === it.id))
    col.insertAdjacentHTML('beforeend',
      `<aside class="arc-drawer" id="arc-drawer" aria-label="${esc(it.artist || '')}"></aside>`)
    const el = document.getElementById('arc-drawer')
    el.addEventListener('click', e => {
      const t = e.target.closest('[data-tab]')
      if (t) { _arc.tab = t.dataset.tab; _arcPaintDrawer(); return }
      const a = e.target.closest('[data-act]')
      if (!a) return
      if (a.dataset.act === 'close') _arcCloseDrawer()
      else if (a.dataset.act === 'dl') {
        const cur = _arc.byId.get(_arc.openId)
        if (cur) _arcEnqueue(cur)
      }
    })
    _arcPaintDrawer()
    try {
      const d = await API.archive.lma.item(it.id)
      if (seq !== _arc.detailSeq) return
      _arc.detail = d
    } catch (e) {
      if (seq !== _arc.detailSeq) return
      _arc.detailErr = e.message
    }
    _arcPaintDrawer()
  }

  // ── Downloads page: #/downloads ────────────────────────────────────────────

  // Checksum verdict from the server: 'verified', 'failed' (with the file
  // names in checksum_errors) or null. Same fingerprint glyph, tip box and
  // green/red tokens as the checksum verdicts elsewhere in the app.
  function _dlChecksumHtml(f) {
    if (f.checksums !== 'verified' && f.checksums !== 'failed') return '<span></span>'
    const bad = f.checksums === 'failed'
    const heading = bad ? 'Checksums failed' : 'Checksums verified'
    const files = bad ? (f.checksum_errors || []).join(', ') : ''
    return `<span class="lq-brow-fp lq-tip dl-fp dl-fp--${bad ? 'bad' : 'ok'}" role="img"
                  aria-label="${esc(files ? `${heading}: ${files}` : heading)}">
      ${icon('fingerprint', 'lq-fp-ic')}
      <span class="lq-tipbox">
        <div class="tt">${esc(heading)}</div>
        ${files ? `<div class="ab">${esc(files)}</div>` : ''}
      </span></span>`
  }

  function _dlRowHtml(f) {
    let job = f.downloading ? DL.jobs.find(j => j.id === f.job_id) : null
    // Progress shows only for a download that is genuinely running: the
    // server's flag, and a job that has not since been cancelled or failed.
    const busy = f.downloading === true && (!job || _dlIsLive(job))
    if (!busy) job = null
    const dests = (DL.folder && DL.folder.destinations) || []
    const order = ['workshop', 'backlog']
    const sorted = dests.slice().sort((a, b) => {
      const ia = order.indexOf(a), ib = order.indexOf(b)
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
    })
    const actions = busy
      ? `<span class="dl-prog"><span class="dl-bar"><span class="dl-bar-fill" style="width:${_dlPct(job)}%"></span></span>${_dlPct(job)}%</span>`
      : `<button type="button" class="lq-act dl-ingest" data-name="${esc(f.name)}">Import</button>
         ${sorted.length ? `
         <div class="lq-move-wrap">
           <button type="button" class="lq-act dl-move" data-name="${esc(f.name)}">Move ${chevronIcon('caret-ic--down lq-act-chev')}</button>
           <div class="lq-move-menu" hidden>
             ${sorted.map(d => `<button type="button" class="lq-move-opt" data-name="${esc(f.name)}" data-dest="${esc(d)}">${esc(TRIAGE_LABELS[d] || d)}</button>`).join('')}
           </div>
         </div>` : ''}`
    return `
      <div class="brow dl-row" data-name="${esc(f.name)}">
        <span class="arc-name">${esc(f.name)}</span>
        ${_dlChecksumHtml(f)}
        <span class="arc-num">${esc(f.files != null ? f.files : '')}</span>
        <span class="arc-num">${esc(f.size_bytes ? fmtBytes(f.size_bytes) : '')}</span>
        <span class="arc-num">${esc(f.format || '')}</span>
        <span class="arc-num arc-num--dim">${esc(busy ? 'Now' : _dlModified(f.modified))}</span>
        <span class="dl-acts">${actions}</span>
      </div>`
  }

  function _dlRepaintDownloadsPage() {
    const rows = document.getElementById('dl-rows')
    if (!rows || !DL.folder) return
    if (rows.querySelector('.lq-move-menu:not([hidden])')) return   // a menu is open
    const html = (DL.folder.folders || []).map(_dlRowHtml).join('')
    if (rows._html === html) return
    rows._html = html
    rows.innerHTML = html
    const path = document.getElementById('dl-path')
    if (path) path.textContent = DL.folder.path || ''
  }

  function _dlCloseMenus() {
    document.querySelectorAll('#dl-rows .lq-move-menu').forEach(m => { m.hidden = true })
  }

  async function renderDownloadsView() {
    setActiveNav('downloads')
    setActiveArtist(null)
    setNavCurrent('Downloads')
    setLoading()
    try {
      _dlSetFolder(await API.downloads.folder())
    } catch (e) {
      setMainHTML(`
        <div class="empty-state">
          <div class="empty-title">Could not open this page</div>
          <div class="empty-sub" style="color:var(--red)">${esc((e && e.message) || String(e))}</div>
        </div>`)
      return
    }
    const f = DL.folder
    setMainHTML(`
      <div class="dl-page">
        <div class="arc-head">
          <h1 class="arc-title">Downloads</h1>
          <span class="dl-path" id="dl-path">${esc(f.path || '')}</span>
        </div>
        <div class="dl-list">
          <div class="brow dl-row dl-cols">
            <span class="arc-hd">Name</span><span></span><span class="arc-hd arc-hd--r">Files</span><span class="arc-hd arc-hd--r">Size</span>
            <span class="arc-hd arc-hd--r">Format</span><span class="arc-hd arc-hd--r">Modified</span><span></span>
          </div>
          <div class="dl-rows" id="dl-rows"></div>
        </div>
      </div>`)
    _dlRepaintDownloadsPage()

    if (!DL.docWired) {
      DL.docWired = true
      document.addEventListener('click', _dlCloseMenus)
    }
    const dir = name => `${String(DL.folder.path || '').replace(/\/+$/, '')}/${name}`
    document.getElementById('dl-rows').addEventListener('click', async e => {
      const ingest = e.target.closest('.dl-ingest')
      if (ingest) {
        // Outside the library: the remembered outside mode, Review First by
        // default. A folder still downloading is refused server-side.
        _biStartAndOpen(dir(ingest.dataset.name), _biMode(false)).catch(err => alert(err.message))
        return
      }
      const mv = e.target.closest('.dl-move')
      if (mv) {
        e.stopPropagation()
        const menu = mv.nextElementSibling
        const wasHidden = menu.hidden
        _dlCloseMenus()
        menu.hidden = !wasHidden
        return
      }
      const opt = e.target.closest('.lq-move-opt')
      if (!opt) return
      const name = opt.dataset.name
      _dlCloseMenus()
      // No Delete on any working-folder page (Ryan, 2026-10-01): Move only.
      try { await API.quality.move(dir(name), opt.dataset.dest) }
      catch (err) { alert(`Move failed: ${err.message}`); return }
      await _dlRefreshFolder()
      _dlRepaintDownloadsPage()
    })
  }

  // ── Workshop / Backlog pages: #/workshop, #/backlog (2026-10-01) ─────────
  // The Downloads page design for the other two working folders: the same
  // folder rows, Ingest, and Move to the other working folder. No Delete:
  // Trellis deletes nothing from a working folder (2026-10-01).
  function _wfRowHtml(f, dests) {
    return `
      <div class="brow dl-row" data-name="${esc(f.name)}">
        <span class="arc-name">${esc(f.name)}</span>
        <span></span>
        <span class="arc-num">${esc(f.files != null ? f.files : '')}</span>
        <span class="arc-num">${esc(f.size_bytes ? fmtBytes(f.size_bytes) : '')}</span>
        <span class="arc-num">${esc(f.format || '')}</span>
        <span class="arc-num arc-num--dim">${esc(_dlModified(f.modified))}</span>
        <span class="dl-acts">
          <button type="button" class="lq-act dl-ingest" data-name="${esc(f.name)}">Import</button>
          ${dests.length ? `
          <div class="lq-move-wrap">
            <button type="button" class="lq-act dl-move" data-name="${esc(f.name)}">Move ${chevronIcon('caret-ic--down lq-act-chev')}</button>
            <div class="lq-move-menu" hidden>
              ${dests.map(d => `<button type="button" class="lq-move-opt" data-name="${esc(f.name)}" data-dest="${esc(d)}">${esc(TRIAGE_LABELS[d] || d)}</button>`).join('')}
            </div>
          </div>` : ''}
        </span>
      </div>`
  }

  async function renderWorkingFolderView(which) {
    const label = TRIAGE_LABELS[which]
    setActiveNav(which)
    setActiveArtist(null)
    setNavCurrent(label)
    setLoading()
    let data
    const load = async () => { data = await API.downloads.folder(which) }
    try { await load() } catch (e) {
      setMainHTML(`
        <div class="empty-state">
          <div class="empty-title">Could not open this page</div>
          <div class="empty-sub" style="color:var(--red)">${esc((e && e.message) || String(e))}</div>
        </div>`)
      return
    }
    const paint = () => {
      const rows = document.getElementById('wf-rows')
      if (rows) rows.innerHTML = (data.folders || []).map(f => _wfRowHtml(f, data.destinations || [])).join('')
    }
    setMainHTML(`
      <div class="dl-page">
        <div class="arc-head">
          <h1 class="arc-title">${esc(label)}</h1>
          <span class="dl-path">${esc(data.path || '')}</span>
        </div>
        <div class="dl-list">
          <div class="brow dl-row dl-cols">
            <span class="arc-hd">Name</span><span></span><span class="arc-hd arc-hd--r">Files</span><span class="arc-hd arc-hd--r">Size</span>
            <span class="arc-hd arc-hd--r">Format</span><span class="arc-hd arc-hd--r">Modified</span><span></span>
          </div>
          <div class="dl-rows" id="wf-rows"></div>
        </div>
      </div>`)
    paint()
    const closeMenus = () => document.querySelectorAll('#wf-rows .lq-move-menu').forEach(m => { m.hidden = true })
    document.addEventListener('click', closeMenus, { once: true })
    const dir = name => `${String(data.path || '').replace(/\/+$/, '')}/${name}`
    document.getElementById('wf-rows').addEventListener('click', async e => {
      const ingestBtn = e.target.closest('.dl-ingest')
      if (ingestBtn) {
        _biStartAndOpen(dir(ingestBtn.dataset.name), _biMode(false)).catch(err => alert(err.message))
        return
      }
      const mv = e.target.closest('.dl-move')
      if (mv) {
        e.stopPropagation()
        const menu = mv.nextElementSibling
        const wasHidden = menu.hidden
        closeMenus()
        menu.hidden = !wasHidden
        document.addEventListener('click', closeMenus, { once: true })
        return
      }
      const opt = e.target.closest('.lq-move-opt')
      if (!opt) return
      closeMenus()
      try { await API.quality.move(dir(opt.dataset.name), opt.dataset.dest) }
      catch (err) { alert(`Move failed: ${err.message}`); return }
      try { await load() } catch (_) {}
      paint()
    })
  }

  function route() {
    const hash = window.location.hash || '#/'

    // Bounce out of an admin-only view when there is no edit permission in
    // force — Playback mode, or a listener who was sent the URL. The library is
    // the honest destination: it is the one page everybody can use.
    const archiveHash = hash === '#/archive/lma' || hash === '#/downloads'
                     || hash === '#/workshop' || hash === '#/backlog'
    if ((!canEditLibrary() || (archiveHash && !isAdmin())) && isAdminOnlyHash(hash)) {
      // The Back/Forward step that landed here is ABANDONED, so clear the flag
      // marking it as ours (2026-08-28). Left set, _navRecord treated the
      // bounce as our own move and recorded nothing: navPos stayed pointing at
      // an entry that was not on screen, and the _navReplace set on the next
      // line was never consumed, so the next genuine navigation overwrote a
      // history entry instead of pushing one.
      _navMoving  = false
      _navReplace = true
      window.location.hash = '#/'   // hashchange re-enters route() with '#/'
      return
    }

    _navRecord(hash)
    _biRefreshRunsOnNav()
    // Any handler belongs to the view we are leaving. The incoming view
    // re-registers one if it has steps of its own, and repaints the buttons
    // itself when it does — this paint only has to be right for views that
    // don't.
    setInPageBack(null)
    paintNavButtons()
    // The bulkIngest page's own poll (renderBulkIngestView) is a setInterval, not
    // a generation-stamped loop like pollAnalysis — it has no other way to
    // know it has been navigated away from, so every route dispatch clears it
    // unconditionally. Harmless when nothing is running.
    _stopBulkIngestPoll()
    // The archive drawer and its scroll observer belong to the page we are
    // leaving; the queue tab and panel are global and stay.
    _arcCloseDrawer()
    _arc.io?.disconnect()
    clearTimeout(_arc.qTimer)
    _dlPaintChrome()

    // Snapshot "where we're coming from" for the destination page's Back
    // link (state.navCurrent/navBack) — but only on a genuine navigation.
    // Guard against two false positives: the very first dispatch this
    // session (_lastRouteHash is null — nothing preceded it, navBack stays
    // null) and a same-hash re-dispatch (some code sets window.location.hash
    // to its OWN current value, or history.replaceState is used elsewhere to
    // correct the recorded hash without a real navigation — neither should
    // overwrite a real back target with the page's own info).
    if (_lastRouteHash !== null && hash !== _lastRouteHash) {
      state.navBack = state.navCurrent
    }
    _lastRouteHash = hash

    // Search first — its hash carries a query string ('#/search?q=hot+rize'),
    // and any prefix match below would read the '?q=…' tail as an id.
    if (hash.startsWith('#/search')) {
      renderSearchView(hash)

    } else if (hash.startsWith('#/recording/')) {
      const id = parseInt(hash.split('/')[2])
      if (id) renderRecordingView(id)
      else    renderLibraryView()

    // Create forms MUST precede the '#/<thing>/<id>' prefix matches below —
    // otherwise '#/musician/new' is parsed as id "new" (NaN) and renders a broken
    // detail page instead of the form.
    } else if (hash === '#/venue/new') {
      renderVenueForm()
    } else if (hash === '#/artist/new') {
      renderArtistForm()
    } else if (hash === '#/musician/new') {
      renderMusicianForm()
    } else if (hash === '#/genre/new') {
      renderGenreForm()
    } else if (hash === '#/event/new') {
      renderEventForm()
    } else if (hash.startsWith('#/artist/')) {
      const id = parseInt(hash.split('/')[2])
      if (id) renderArtistView(id)
      else    renderLibraryView()

    } else if (hash === '#/recent') {
      renderRecentView()

    } else if (hash === '#/albums') {
      renderAlbumsView()

    } else if (hash === '#/batch') {
      // Retired page: old bookmarks land on Add Recordings.
      _navReplace = true
      window.location.hash = '#/ingest'

    } else if (hash.split('?')[0] === '#/ingest') {
      // Add Recordings opens the current run while one is listed; ?new=1 is
      // the import page's way to the picker. _navReplace so Back does not
      // land on the redirecting entry.
      // ingest._resume marks the import page's own per-item Review (the
      // Add Recording form), which must not bounce back to the run.
      if (hash === '#/ingest' && canEditLibrary() && !ingest._resume) {
        _fetchBulkIngestRuns().then(() => {
          // The person may have navigated away while the fetch ran; the new
          // route has already rendered, so do not redirect or render over it.
          if (window.location.hash !== '#/ingest') return
          const open = _biOpenRunHash()
          if (open) { _navReplace = true; window.location.hash = open }
          else renderIngestView()
        })
      } else {
        renderIngestView()
      }

    } else if (/^#\/bulk-ingest(\/\d+)?$/.test(hash.split('?')[0])) {
      // #/bulk-ingest/<run_id> is one run; bare #/bulk-ingest is the current run.
      renderBulkIngestView(Number((hash.split('?')[0].split('/')[2])) || null)

    } else if (hash === '#/archive/lma') {
      renderArchiveLmaView()

    } else if (hash === '#/downloads') {
      renderDownloadsView()
    } else if (hash === '#/workshop' || hash === '#/backlog') {
      renderWorkingFolderView(hash.slice(2))

    } else if (hash === '#/venues') {
      renderVenuesPage()

    } else if (hash.startsWith('#/venue/')) {
      const id = parseInt(hash.split('/')[2])
      if (id) renderVenueView(id)
      else    renderVenuesPage()

    } else if (hash === '#/genres/assign') {
      renderGenreAssignView()

    } else if (hash === '#/genres') {
      renderGenresPage()

    } else if (hash.startsWith('#/genre/')) {
      const id = parseInt(hash.split('/')[2])
      if (id) renderGenreView(id)
      else    renderGenresPage()

    } else if (hash === '#/events') {
      renderEventsPage()

    } else if (hash.startsWith('#/event/')) {
      const id = parseInt(hash.split('/')[2])
      if (id) renderEventView(id)
      else    renderEventsPage()

    // '#/artists' is the acts and '#/musicians' the people (2026-09-16).
    // Both hashes have meant the other thing before; see CONTEXT.md §2.
    } else if (hash === '#/artists') {
      renderArtistsIndexPage()

    } else if (hash === '#/musicians') {
      renderMusiciansIndexPage()

    } else if (hash.startsWith('#/musician/')) {
      // Edit-in-place, so #/musician/<id> and any legacy /edit both land on the view.
      const id = parseInt(hash.split('/')[2])
      if (id) renderPersonView(id)
      else    renderLibraryView()

    } else if (hash === '#/settings' || hash.startsWith('#/settings/')) {
      renderSettingsPage(hash.split('/')[2] || 'profile')
    } else if (hash === '#/peers') {
      renderPeersPage()
    } else if (hash === '#/collections') {
      renderCollectionsIndex()

    } else if (hash === '#/collection/new') {
      renderCollectionForm()

    } else if (hash.startsWith('#/collection/')) {
      // Edit-in-place, so #/collection/<id> and any legacy /edit both land on the view.
      const id = parseInt(hash.split('/')[2])
      if (id) renderCollectionView(id)
      else    renderCollectionsIndex()

    } else {
      renderLibraryView()
    }
  }

  // ── Auth ───────────────────────────────────────────────────────────────────

  function showLogin() {
    loginScreen.classList.remove('hidden')
    appShell.classList.add('hidden')
  }

  function showApp() {
    loginScreen.classList.add('hidden')
    appShell.classList.remove('hidden')
  }

  function setUserUI(user) {
    const initials = user.username.slice(0,2).toUpperCase()
    userAvatar.textContent = initials
    userName.textContent   = user.username
    // The view mode depends on the role, so it can only be resolved once we
    // know who this is. Both entry points (init and login) call setUserUI, so
    // this is the one place that covers a cold start and a fresh sign-in.
    initViewMode()
  }

  // ── Login form ─────────────────────────────────────────────────────────────

  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault()
    const username = document.getElementById('login-username').value.trim()
    const password = document.getElementById('login-password').value
    const errEl    = document.getElementById('login-error')
    const submitBtn = document.getElementById('login-submit')

    errEl.classList.add('hidden')
    submitBtn.disabled = true
    submitBtn.textContent = 'Signing in...'

    try {
      const user = await API.auth.login(username, password)
      state.user = user
      setUserUI(user)
      showApp()
      libraryDrive.start()
      await loadRemotes()
      await loadArtistList()
      route()
    } catch (e) {
      errEl.textContent = e.message || 'Invalid credentials'
      errEl.classList.remove('hidden')
    } finally {
      submitBtn.disabled = false
      submitBtn.textContent = 'Sign in'
    }
  })

  // ── Logout ─────────────────────────────────────────────────────────────────

  document.getElementById('logout-btn').addEventListener('click', async () => {
    try { await API.auth.logout() } catch (_) {}
    state.user = null
    showLogin()
  })

  // ── Settings modal ───────────────────────────────────────────────────────────

  // ── Settings ───────────────────────────────────────────────────────────────
  //
  // A PAGE, not a modal (Ryan, 2026-08-25 — the dialog was "too small and
  // strange"). Settings had outgrown a box: profile, appearance, library
  // behaviour, AI, sharing and an About panel do not belong in something you
  // dismiss by clicking beside it.
  //
  // Nothing here has a Save button except the API key, and that is deliberate —
  // the same reasoning the Peers page already uses for grants: a control that
  // needs a separate Save is a control that gets left unsaved. A name commits
  // when you leave the field, a menu when you change it, the theme the instant
  // you click it. The key is the exception because it is a secret you paste
  // once and cannot read back to check.

  function _settingsInitial(name) {
    return (name || '?').trim().charAt(0).toUpperCase()
  }

  // A short confirmation beside the thing that changed. Deliberately not a
  // toast: at the moment you change a setting you are looking AT the setting,
  // and a message in the corner is a message somewhere you are not.
  function _settingsSaved(el, text = 'Saved') {
    if (!el) return
    el.textContent = text
    el.classList.add('is-on')
    clearTimeout(el._t)
    el._t = setTimeout(() => el.classList.remove('is-on'), 1600)
  }

  function _settingsAvatarHtml(me) {
    if (me.has_avatar) {
      return `<img src="${esc(me.avatar_url)}" alt="" class="set-avatar-img">`
    }
    return `<span class="set-avatar-initial">${esc(_settingsInitial(me.name))}</span>`
  }

  /** The Appearance picker. Each card's miniature carries data-pal-preview, so
   *  it is painted by that scheme's own block in main.css — this function
   *  writes no colours at all. The card frame sits OUTSIDE the miniature on
   *  purpose, so the selected ring is drawn in the palette currently in force
   *  rather than in the one the card is advertising. */
  function _palettePickerHtml() {
    const active = currentPalette().id
    const group = (title, grp) => `
      <div class="pal-group">
        <div class="pal-group-title">${title}</div>
        <div class="pal-grid">
          ${PALETTES.filter(p => p.group === grp).map(p => `
            <button type="button" class="pal-card${p.id === active ? ' active' : ''}"
                    data-palette="${esc(p.id)}" aria-pressed="${p.id === active}">
              <span class="pal-mini" data-pal-preview="${esc(p.id)}">
                <span class="pal-mini-nav"></span>
                <span class="pal-mini-body">
                  <span class="pal-mini-card">
                    <span class="pal-mini-line a"></span>
                    <span class="pal-mini-line b"></span>
                    <span class="pal-mini-line c"></span>
                  </span>
                </span>
              </span>
              <span class="pal-meta">
                <span class="pal-name">${esc(p.label)}</span>
                <span class="pal-note">${esc(p.note)}</span>
              </span>
            </button>`).join('')}
        </div>
      </div>`
    return `<div class="pal-picker" id="set-palette" role="group" aria-label="Palette">
        ${group('Light', 'light')}${group('In between', 'mid')}${group('Dark', 'dark')}
      </div>`
  }

  /** Settings › Files (2026-10-05 tabbed redesign). The naming controls are
   *  rendered only while Rename Files is on, and the template field only for
   *  Custom: a preset's template is not something the user can act on, and a
   *  dimmed block of controls that do nothing is noise. Placement moved to the
   *  Folders tab. */
  function _fileHandlingSectionHtml(fh) {
    const renaming = !!fh.rename_files
    const isCustom = fh.naming_scheme === 'custom'
    const naming = !renaming ? '' : `
      <div class="set-dep">
        <div class="set-field">
          <label class="set-label" for="fh-scheme">Naming Scheme</label>
          <div class="set-actions">
            <select class="set-input" id="fh-scheme">
              ${NAMING_SCHEME_LABELS.map(([v, label]) =>
                `<option value="${v}"${v === fh.naming_scheme ? ' selected' : ''}>${esc(label)}</option>`).join('')}
            </select>
            <span class="set-flash" id="fh-scheme-flash"></span>
          </div>
        </div>
        ${isCustom ? `
        <div class="set-field">
          <label class="set-label" for="fh-template">Template</label>
          <input type="text" class="set-input mono" id="fh-template" value="${esc(fh.naming_template || '')}">
          <div class="tokens">${NAMING_TOKENS.map(t => `<span>{${t}}</span>`).join('')}</div>
          <p class="set-hint">Modifiers: <span class="mono">:lower</span> <span class="mono">:upper</span>
            <span class="mono">:nospace</span> <span class="mono">:underscore</span>. Text in square brackets
            is dropped when a token inside it is empty.</p>
          <span class="set-flash" id="fh-template-flash"></span>
        </div>` : ''}
        <div class="set-field">
          <span class="set-label">Preview</span>
          <table class="fh-prev set-prev" id="fh-preview"></table>
        </div>
      </div>`
    return `
      <div class="set-field">
        <span class="set-label">Mode</span>
        <div class="set-actions">
          <div class="seg" id="fh-mode" role="group" aria-label="File handling mode">
            <button type="button" class="${fh.file_handling_mode !== 'organize' ? 'on' : ''}" data-mode="keep">Keep my files as-is</button>
            <button type="button" class="${fh.file_handling_mode === 'organize' ? 'on' : ''}" data-mode="organize">Organize my files</button>
          </div>
          <span class="set-flash" id="fh-mode-flash"></span>
        </div>
      </div>

      <div class="set-field">
        <span class="set-label">Rename</span>
        <label class="check"><input type="checkbox" id="fh-rename-folders" ${fh.rename_folders ? 'checked' : ''}><div>Folders</div></label>
        <label class="check"><input type="checkbox" id="fh-rename-files" ${renaming ? 'checked' : ''}><div>Files</div></label>
        <span class="set-flash" id="fh-switches-flash"></span>
      </div>
      ${naming}

      <div class="set-field">
        <span class="set-label">Tags</span>
        <label class="check"><input type="checkbox" id="fh-write-tags-ingest" ${fh.write_tags_on_ingest ? 'checked' : ''}>
          <div>Write tags when a recording is added<div class="hint">FFP and ST5 checksums still verify. MD5 will not.</div></div></label>
        <label class="check"><input type="checkbox" id="fh-write-tags-default" ${fh.write_tags_default ? 'checked' : ''}>
          <div>Write tags to files without asking each time</div></label>
        <span class="set-flash" id="fh-tags-flash"></span>
      </div>`
  }

  /** Settings › Folders: where the library is, where new recordings land in
   *  it, and the three working folders (filled in by _wireSettings, desktop
   *  only, since the folder dialog is PyWebView's). */
  function _foldersSectionHtml(fh, about) {
    return `
      <div class="set-field">
        <span class="set-label">Library</span>
        <div class="set-path">${esc(about.library_root || '')}</div>
      </div>
      <div class="set-field">
        <span class="set-label">Placement</span>
        <label class="check"><input type="radio" name="fh-place" value="artist" ${fh.placement === 'artist' ? 'checked' : ''}><div>Under the artist folder</div></label>
        <label class="check"><input type="radio" name="fh-place" value="root" ${fh.placement === 'root' ? 'checked' : ''}><div>Library root</div></label>
        <span class="set-flash" id="fh-place-flash"></span>
      </div>
      <div id="set-folders"></div>`
  }

  // The Settings tab in force. Kept across the in-page repaints that follow a
  // change (renderSettingsPage() with no argument), so flipping a switch never
  // throws you back to the first tab.
  let _settingsTab = 'profile'

  async function renderSettingsPage(tab) {
    setActiveNav('settings')
    setNavCurrent('Settings')
    if (tab) _settingsTab = tab
    setLoading()

    let prefs = {}, me = {}, about = {}, fh = {}
    try {
      [prefs, me, about, fh] = await Promise.all([
        API.preferences.get(), API.auth.me(), API.system.about(),
        fileHandling(true),
      ])
    } catch (e) {
      setMainHTML(`<div class="empty-state">
        <div class="empty-title">Could not load settings</div>
        <div class="empty-sub">${esc(e.message)}</div></div>`)
      return
    }

    const keySet     = prefs.has_api_key
    const noKeychain = prefs.keychain_available === false
    const model      = prefs.ai_model || 'claude-sonnet-5'
    const editable   = canEditLibrary()

    // Files and Folders are install-level and admin-only, so in Playback mode
    // they are absent rather than shown and refused.
    const tabs = [
      ['profile', 'Profile'], ['appearance', 'Appearance'],
      ...(editable ? [['files', 'Files'], ['folders', 'Folders']] : []),
      ['lomax', 'Lomax'], ['about', 'About'],
    ]
    if (!tabs.some(([id]) => id === _settingsTab)) {
      _settingsTab = 'profile'
      if (location.hash.startsWith('#/settings/')) history.replaceState(null, '', '#/settings/profile')
    }

    const panes = {
      profile: `
        <div class="set-field">
          <label class="set-label" for="set-name">Display Name</label>
          <div class="set-actions">
            <input class="set-input" id="set-name" maxlength="120"
                   value="${esc(me.display_name || '')}"
                   placeholder="${esc(me.username)}" autocomplete="off">
            <span class="set-flash" id="set-name-flash"></span>
          </div>
        </div>
        <div class="set-field">
          <label class="set-label" for="set-username">Sign-in Name</label>
          <div class="set-actions">
            <input class="set-input" id="set-username" maxlength="64"
                   value="${esc(me.username)}" autocomplete="off"
                   autocorrect="off" autocapitalize="off" spellcheck="false">
            <span class="set-flash" id="set-username-flash"></span>
          </div>
        </div>
        <div class="set-field">
          <span class="set-label">Picture</span>
          <div class="set-person">
            <div class="set-avatar" id="set-avatar">${_settingsAvatarHtml(me)}</div>
            <div class="set-actions">
              <input type="file" id="set-avatar-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>
              <button class="btn btn-ghost btn-sm" id="set-avatar-pick">
                ${me.has_avatar ? 'Replace' : 'Choose Picture'}</button>
              ${me.has_avatar
                ? '<button class="btn btn-ghost btn-sm" id="set-avatar-clear">Remove</button>' : ''}
              <span class="set-flash" id="set-avatar-flash"></span>
            </div>
          </div>
        </div>
        ${editable ? `
        <div class="set-field">
          <span class="set-label">Sharing</span>
          <button class="btn btn-ghost btn-sm" id="set-peers">Manage Sharing</button>
        </div>` : ''}`,

      appearance: _palettePickerHtml(),

      files:   editable ? _fileHandlingSectionHtml(fh) : '',
      folders: editable ? _foldersSectionHtml(fh, about) : '',

      lomax: `
        <div class="set-field">
          <label class="set-label" for="set-key">Anthropic API Key</label>
          <div class="set-actions">
            <input class="set-input" type="password" id="set-key" autocomplete="off"
                   placeholder="${keySet ? '•••••••••••••  (a key is saved)' : 'sk-ant-…'}">
            <button class="btn btn-primary btn-sm" id="set-key-save">Save Key</button>
            ${keySet ? '<button class="btn btn-ghost btn-sm" id="set-key-clear">Clear</button>' : ''}
            <span class="set-flash" id="set-key-flash"></span>
          </div>
          ${noKeychain ? '<p class="set-hint">This machine has no usable keychain, so a key cannot be stored.</p>' : ''}
        </div>
        <div class="set-field">
          <label class="set-label" for="set-model">Model</label>
          <div class="set-actions">
            <select class="set-input" id="set-model">
              <option value="claude-sonnet-5" ${model === 'claude-sonnet-5' ? 'selected' : ''}>Sonnet: stronger research</option>
              <option value="claude-haiku-4-5" ${model === 'claude-haiku-4-5' ? 'selected' : ''}>Haiku: faster and cheaper</option>
            </select>
            <span class="set-flash" id="set-model-flash"></span>
          </div>
        </div>
        <div class="set-field" id="set-lx-usage"></div>`,

      about: `
        <dl class="set-about">
          <dt>Version</dt><dd>${esc(about.app_name || 'Trellis')} ${esc(about.version || '')}${
            about.installed ? '' : ' <span class="set-about-tag">running from source</span>'}</dd>
          <dt>Library Data</dt><dd class="set-path">${esc(about.database || '')}</dd>
        </dl>
        <p class="set-about-credit">Place data from GeoNames (geonames.org), CC BY 4.0.</p>`,
    }

    setMainHTML(`
      <div class="set-wrap">
        <h1 class="set-h1">Settings</h1>
        <div class="pp-tabs set-tabs" role="tablist">
          ${tabs.map(([id, label]) => `
            <button class="pp-tab${id === _settingsTab ? ' active' : ''}" data-set-tab="${id}"
                    role="tab" aria-selected="${id === _settingsTab}">${label}</button>`).join('')}
        </div>
        ${tabs.map(([id]) => `
          <section class="set-pane${id === _settingsTab ? ' active' : ''}" data-set-pane="${id}" role="tabpanel">
            ${panes[id]}
          </section>`).join('')}
      </div>`)

    // Switching tabs is in-page: every pane is already rendered and wired, so
    // the hash is replaced (not pushed, and no hashchange) to keep a reload or
    // a deep link on the same tab without a refetch.
    document.querySelectorAll('[data-set-tab]').forEach(btn => btn.addEventListener('click', () => {
      _settingsTab = btn.dataset.setTab
      document.querySelectorAll('[data-set-tab]').forEach(b => {
        const on = b === btn
        b.classList.toggle('active', on)
        b.setAttribute('aria-selected', String(on))
      })
      document.querySelectorAll('[data-set-pane]').forEach(p =>
        p.classList.toggle('active', p.dataset.setPane === _settingsTab))
      history.replaceState(null, '', '#/settings/' + _settingsTab)
    }))

    _wireSettings(me)
  }

  function _wireSettings(me) {
    const $ = id => document.getElementById(id)

    // ── Display name — commits on blur or Enter, never on every keystroke ────
    const nameInput = $('set-name')
    let lastName = me.display_name || ''
    const commitName = async () => {
      const v = nameInput.value.trim()
      if (v === lastName) return
      try {
        const updated = await API.auth.updateProfile({ display_name: v })
        lastName = updated.display_name || ''
        nameInput.value = lastName
        state.user = { ...(state.user || {}), ...updated }
        _settingsSaved($('set-name-flash'))
        // The initial in the picture placeholder is derived from the name, so
        // it has to follow it.
        if (!updated.has_avatar) $('set-avatar').innerHTML =
          `<span class="set-avatar-initial">${esc(_settingsInitial(updated.name))}</span>`
      } catch (e) {
        nameInput.value = lastName
        _settingsSaved($('set-name-flash'), e.message)
      }
    }
    nameInput?.addEventListener('blur', commitName)
    nameInput?.addEventListener('keydown', e => { if (e.key === 'Enter') nameInput.blur() })

    // ── Sign-in name — same commit-on-blur shape (Ryan, 2026-08-28) ─────────
    // Editable at last. It is the credential, so a rejected value snaps back
    // to the last one the server accepted rather than sitting there looking
    // saved. Changing it does NOT sign you out: Flask-Login carries the row
    // id, not the name.
    const userInput = $('set-username')
    let lastUsername = me.username
    const commitUsername = async () => {
      const v = userInput.value.trim()
      if (v === lastUsername) return
      try {
        const updated = await API.auth.updateProfile({ username: v })
        lastUsername = updated.username
        userInput.value = lastUsername
        state.user = { ...(state.user || {}), ...updated }
        _settingsSaved($('set-username-flash'))
        // The display-name field shows the sign-in name as its placeholder —
        // that is the "leave it empty and go by this" promise, so it has to
        // follow a rename or it quietly promises the old name.
        if (nameInput) nameInput.placeholder = lastUsername
        // Same for the picture initial when there is no display name and no
        // picture: it is derived from whichever name is in force.
        if (!updated.has_avatar) $('set-avatar').innerHTML =
          `<span class="set-avatar-initial">${esc(_settingsInitial(updated.name))}</span>`
      } catch (e) {
        userInput.value = lastUsername
        _settingsSaved($('set-username-flash'), e.message)
      }
    }
    userInput?.addEventListener('blur', commitUsername)
    userInput?.addEventListener('keydown', e => { if (e.key === 'Enter') userInput.blur() })

    // ── Picture ─────────────────────────────────────────────────────────────
    const file = $('set-avatar-file')
    $('set-avatar-pick')?.addEventListener('click', () => file.click())
    file?.addEventListener('change', async () => {
      const f = file.files && file.files[0]
      if (!f) return
      try {
        const updated = await API.auth.uploadAvatar(f)
        $('set-avatar').innerHTML = `<img src="${esc(updated.avatar_url)}" alt="" class="set-avatar-img">`
        _settingsSaved($('set-avatar-flash'))
        renderSettingsPage()          // repaint so Remove appears
      } catch (e) { _settingsSaved($('set-avatar-flash'), e.message) }
      finally { file.value = '' }
    })
    $('set-avatar-clear')?.addEventListener('click', async () => {
      try { await API.auth.removeAvatar(); renderSettingsPage() }
      catch (e) { _settingsSaved($('set-avatar-flash'), e.message) }
    })

    // ── Palette — applies live, so there is nothing to save ─────────────────
    // Repainted from the clicked card rather than from currentPalette(),
    // because setPalette() returning null (an id this build does not carry)
    // must leave the selection where it was instead of clearing every card.
    const palWrap = $('set-palette')
    palWrap?.addEventListener('click', e => {
      const card = e.target.closest('.pal-card')
      if (!card || !palWrap.contains(card)) return
      if (!setPalette(card.dataset.palette)) return
      palWrap.querySelectorAll('.pal-card').forEach(c => {
        const on = c === card
        c.classList.toggle('active', on)
        c.setAttribute('aria-pressed', on ? 'true' : 'false')
      })
    })

    // ── Menus — commit on change ────────────────────────────────────────────
    const menu = (id, key) => $(id)?.addEventListener('change', async e => {
      try {
        await API.preferences.update({ [key]: e.target.value })
        // Keep the cached prefs object honest, or the next screen to read it
        // renders the value this page just replaced.
        if (appPrefs) appPrefs[key] = e.target.value
        _settingsSaved($(`${id}-flash`))
      } catch (err) { _settingsSaved($(`${id}-flash`), err.message) }
    })
    // ── File handling (spec section 6.2) ─────────────────────────────────────
    // Every control is install-level (admin_required server-side), so a
    // failure is always surfaced, never swallowed, and the fh cache is
    // dropped on every successful save so the ingest screens' strip and any
    // other open Settings tab pick the new value up on next read.
    // Spec 1.1: changing the mode in Settings shows a one-sentence
    // confirmation before it rewrites every switch below to match --
    // unlike first run, this can flip switches an admin already set
    // deliberately, so it does not apply silently on a click.
    function _confirmModeChange(mode, onConfirm) {
      const label = mode === 'organize' ? 'Organize my files' : 'Keep my files as-is'
      const wrap = document.createElement('div')
      wrap.className = 'modal-overlay'
      wrap.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="fhmode-title">
          <div class="modal-header"><h3 id="fhmode-title">Change file handling</h3></div>
          <div class="modal-body">
            <p>Switching to "${esc(label)}" rewrites the switches below to match it.</p>
          </div>
          <div class="modal-footer">
            <button class="btn btn-sm btn-ghost" id="fhmode-cancel">Cancel</button>
            <button class="btn btn-sm btn-primary" id="fhmode-confirm">Change</button>
          </div>
        </div>`
      document.body.appendChild(wrap)
      const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey) }
      const onKey = e => { if (e.key === 'Escape') close() }
      document.addEventListener('keydown', onKey)
      wrap.querySelector('#fhmode-cancel').addEventListener('click', close)
      wrap.addEventListener('click', e => { if (e.target === wrap) close() })
      wrap.querySelector('#fhmode-confirm').addEventListener('click', () => { close(); onConfirm() })
    }
    if ($('fh-mode')) {
      $('fh-mode').addEventListener('click', e => {
        const btn = e.target.closest('button[data-mode]')
        if (!btn) return
        const mode = btn.dataset.mode
        if (btn.classList.contains('on')) return
        _confirmModeChange(mode, async () => {
          try {
            const result = await API.system.setFileHandling({ file_handling_mode: mode })
            await fileHandling(true)
            _settingsSaved($('fh-mode-flash'))
            renderSettingsPage()   // switches/scheme/tags all follow the mode
          } catch (err) {
            _settingsSaved($('fh-mode-flash'), err.message || 'Could not save')
          }
        })
      })
    }

    async function _fhSaveField(key, value, flashId) {
      try {
        await API.system.setFileHandling({ [key]: value })
        await fileHandling(true)
        _settingsSaved($(flashId))
        return true
      } catch (err) {
        _settingsSaved($(flashId), err.message || 'Could not save')
        return false
      }
    }

    $('fh-rename-folders')?.addEventListener('change', async e => {
      const el = e.target
      if (!await _fhSaveField('rename_folders', el.checked, 'fh-switches-flash')) el.checked = !el.checked
    })
    $('fh-rename-files')?.addEventListener('change', async e => {
      const el = e.target
      const ok = await _fhSaveField('rename_files', el.checked, 'fh-switches-flash')
      if (!ok) { el.checked = !el.checked; return }
      renderSettingsPage()   // naming controls appear or go
    })
    $('fh-scheme')?.addEventListener('change', async e => {
      const el = e.target
      const prev = Array.from(el.options).find(o => o.defaultSelected)?.value
      const ok = await _fhSaveField('naming_scheme', el.value, 'fh-scheme-flash')
      if (!ok) { if (prev) el.value = prev; return }
      renderSettingsPage()   // template field appears for Custom only
    })
    $('fh-template')?.addEventListener('blur', async e => {
      const el = e.target
      await _fhSaveField('naming_template', el.value, 'fh-template-flash')
      _fhRefreshPreview()
    })
    $('fh-write-tags-ingest')?.addEventListener('change', async e => {
      const el = e.target
      if (!await _fhSaveField('write_tags_on_ingest', el.checked, 'fh-tags-flash')) el.checked = !el.checked
    })
    $('fh-write-tags-default')?.addEventListener('change', async e => {
      const el = e.target
      if (!await _fhSaveField('write_tags_default', el.checked, 'fh-tags-flash')) el.checked = !el.checked
    })
    document.querySelectorAll('input[name="fh-place"]').forEach(radio => {
      radio.addEventListener('change', async e => {
        const el = e.target
        if (!el.checked) return
        await _fhSaveField('placement', el.value, 'fh-place-flash')
      })
    })

    // Working folders (2026-09-26; every library since 2026-10-01). First run
    // no longer asks for most of them, so they are set here. Desktop only:
    // the folder dialog is PyWebView's.
    ;(async () => {
      const host = $('set-folders')
      const api  = window.pywebview && window.pywebview.api
      if (!host || !canEditLibrary() || !api || !api.get_working_folders) return
      let wf
      try { wf = await api.get_working_folders() } catch (_) { return }
      if (!wf || !wf.editable) return
      const rows = [['import_dir', 'Downloads'], ['backlog_dir', 'Backlog'], ['workshop_dir', 'Workshop']]
      host.innerHTML = rows.map(([key, label]) => `
        <div class="set-field">
          <span class="set-label">${label}</span>
          <div class="set-actions">
            <span class="set-path" id="wf-${key}">${esc(wf[key] || (key === 'import_dir' && wf.effective_import_dir) || 'not set')}</span>
            <button class="btn btn-ghost btn-sm" data-wf="${key}">Choose…</button>
            <span class="set-flash" id="wf-${key}-flash"></span>
          </div>
        </div>`).join('')
      host.addEventListener('click', async e => {
        const btn = e.target.closest('[data-wf]')
        if (!btn) return
        const key = btn.dataset.wf
        const picked = await api.pick_folder()
        if (!picked) return
        const res = await api.set_working_folder(key, picked)
        if (res && res.ok) {
          $('wf-' + key).textContent = res.path
          _settingsSaved($('wf-' + key + '-flash'))
          // A newly set Workshop/Backlog gets its nav entry right away.
          appPrefs = null
          renderSidebar()
        } else {
          _settingsSaved($('wf-' + key + '-flash'), (res && res.error) || 'Could not save')
        }
      })
    })()

    // Preview table — refreshed whenever the scheme select changes, and once
    // on load so the panel opens populated (spec section 6.2's "Shown ...
    // with the etree preset, so the preview is populated").
    async function _fhRefreshPreview() {
      const tbl = $('fh-preview')
      if (!tbl) return
      const scheme = $('fh-scheme')?.value
      const template = $('fh-template') ? $('fh-template').value : undefined
      // Custom with nothing typed yet: nothing to preview, and no error to show.
      if (template !== undefined && !template.trim()) {
        tbl.innerHTML = ''
        tbl.closest('.set-field').hidden = true
        return
      }
      try {
        const result = await API.naming.preview({ scheme, template })
        const rows = (result.plan || []).slice(0, 3).map(p => `
          <tr><td class="fh-prev-old">${esc(p.current)}</td><td class="fh-prev-arrow">→</td><td>${esc(p.proposed)}</td></tr>`).join('')
        tbl.innerHTML = rows
        // An empty library has nothing to preview; the label alone would be a
        // heading over nothing.
        tbl.closest('.set-field').hidden = !rows
      } catch (err) {
        tbl.innerHTML = `<tr><td colspan="3">${esc(err.message)}</td></tr>`
        tbl.closest('.set-field').hidden = false
      }
    }
    if ($('fh-preview')) _fhRefreshPreview()

    menu('set-model',    'ai_model')

    // ── The one explicit Save: a secret you paste and cannot read back ──────
    $('set-key-save')?.addEventListener('click', async () => {
      const key = $('set-key').value.trim()
      if (!key) return _settingsSaved($('set-key-flash'), 'Paste a key first')
      try {
        await API.preferences.update({ api_key: key })
        appPrefs = null   // the level toggles read has_api_key
        $('set-key').value = ''
        _settingsSaved($('set-key-flash'))
      } catch (e) { _settingsSaved($('set-key-flash'), e.message) }
    })
    $('set-key-clear')?.addEventListener('click', async () => {
      try { await API.preferences.update({ clear_api_key: true }); appPrefs = null; renderSettingsPage() }
      catch (e) { _settingsSaved($('set-key-flash'), e.message) }
    })

    // ── Lomax usage: four totals and the run history (tokens and searches only) ──
    ;(async function () {
      const host = $('set-lx-usage')
      if (!host) return
      const p2 = n => String(n).padStart(2, '0')
      const date = s => {
        const t = lxTs(s)
        if (isNaN(t)) return ''
        const d = new Date(t)
        return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`
      }
      const PATH = { recording: 'recording', artist: 'artist', venue: 'venue' }
      const subject = r => {
        const label = esc(r.subject_label || '')
        return PATH[r.subject_type] && r.subject_id && label ? `<a href="#/${PATH[r.subject_type]}/${r.subject_id}">${label}</a>` : label
      }
      const row = r => `<tr><td>${esc(date(r.created_at))}</td><td>${esc(r.skill_label || '')}</td><td>${subject(r)}</td>` +
        `<td class="num">${r.proposals || 0}</td>` +
        `<td class="num">${(r.total_tokens || 0).toLocaleString()}</td><td class="num">${r.web_searches || 0}</td></tr>`
      let data
      try { data = await API.lomax.usage(50, 0) } catch (_) { return }
      const t = data.totals || {}
      let loaded = (data.runs || []).length
      const tot = (n, l) => `<div class="lx-tot"><div class="lx-tot-n">${n}</div><div class="lx-tot-l">${l}</div></div>`
      host.innerHTML = `<div class="lx-tots">${tot((t.runs || 0).toLocaleString(), 'Runs')}${tot((t.total_tokens || 0).toLocaleString(), 'Tokens')}` +
        `${tot((t.web_searches || 0).toLocaleString(), 'Web searches')}${tot(`${t.accepted || 0} of ${t.proposals || 0}`, 'Suggestions accepted')}</div>` +
        ((data.runs || []).length ? `<table class="lx-tbl lx-hist"><thead><tr><th>Date</th><th>Job</th><th>Subject</th>` +
          `<th class="num">Suggestions</th><th class="num">Tokens</th><th class="num">Searches</th></tr></thead>` +
          `<tbody id="set-lx-rows">${data.runs.map(row).join('')}</tbody></table>` +
          `<button type="button" class="btn btn-ghost btn-sm" id="set-lx-more"${loaded >= (data.total || 0) ? ' hidden' : ''}>Show more</button>` : '')
      $('set-lx-more')?.addEventListener('click', async e => {
        const btn = e.currentTarget
        btn.disabled = true
        try {
          const more = await API.lomax.usage(50, loaded)
          const runs = more.runs || []
          $('set-lx-rows').insertAdjacentHTML('beforeend', runs.map(row).join(''))
          loaded += runs.length
          if (!runs.length || loaded >= (more.total || 0)) btn.hidden = true
        } catch (_) { /* leave the button for another try */ }
        btn.disabled = false
      })
    })()

    $('set-peers')?.addEventListener('click', () => { window.location.hash = '#/peers' })
  }

  document.getElementById('settings-btn')?.addEventListener('click',
    () => { window.location.hash = '#/settings' })

  // ── Search (IO-46, 2026-08-18) ─────────────────────────────────────────────
  //
  // THE RULE: act, person, venue, date, or any combination. Track titles and
  // provenance text are OUT of v1 — see app/utils/search.py before widening
  // anything here. IO-46's Jira description still promises song identity and
  // is out of date, not a spec.
  //
  // Local library only. The Search Bar hides itself in peer mode via CSS
  // (html.peer-mode .search-bar) rather than by a JS check, so it is gone
  // before first paint instead of flickering into view and back out.

  const SEARCH_DEBOUNCE_MS   = 200
  // Below this, a query is noise: one character matches most of the library and
  // costs a round trip to say so (Ryan, 2026-08-23). Verified against the live
  // DB before choosing 3 — no artist, venue or musician name is shorter than
  // that, so nothing real is currently unreachable. If a two-letter act ever
  // lands (a "U2" case), this is the one number to change.
  const SEARCH_MIN_CHARS     = 3
  const SEARCH_DROPDOWN_MAX  = 5      // per group in the dropdown
  const SEARCH_OVERVIEW_MAX  = 25     // per group on the results page
  const SEARCH_PAGE_SIZE     = 50     // per "Load more" on a single-group page

  const searchBar      = document.getElementById('search-bar')
  const searchInput    = document.getElementById('search-input')
  const searchDropdown = document.getElementById('search-dropdown')
  const searchClearBtn = document.getElementById('search-clear')
  const searchField    = searchInput ? searchInput.closest('.search-field') : null

  function setSearchFieldFilled(filled) {
    searchClearBtn.classList.toggle('hidden', !filled)
    searchField?.classList.toggle('has-text', !!filled)
  }

  let _searchTimer = null
  let _searchSeq   = 0        // guards against an out-of-order slow response
  let _searchItems = []       // flat list of {hash} for arrow-key navigation
  let _searchActive = -1

  function searchGroupLine(item) {
    // One dropdown row. Shapes differ per group, which is the point — a show
    // without its date is not identifiable, and an act without its count
    // gives no sense of what's behind the click.
    if (item.type === 'recording') {
      const where = [item.venue, item.city].filter(Boolean).join(' · ')
      return `<span class="search-item-date">${esc(item.date || '—')}</span>
              <span class="search-item-name">${esc(item.artist || 'Unknown')}</span>
              <span class="search-item-meta">${esc(where)}</span>`
    }
    if (item.type === 'album') {
      // Studio Records spec v1, section 7 — an album row leads with its
      // title, never a date/venue slot it does not have. No title: the
      // name slot already falls back to the artist, so the meta line is
      // the year alone -- repeating the artist there would read as an echo.
      const meta = item.title ? [item.artist, item.year].filter(Boolean).join(' · ') : (item.year || '')
      return `<span class="search-item-name">${esc(item.title || item.artist || 'Unknown')}</span>
              <span class="search-item-meta">${esc(meta)}</span>`
    }
    if (item.type === 'venue') {
      const where = [item.city, item.state].filter(Boolean).join(', ')
      return `<span class="search-item-name">${esc(item.name)}</span>
              <span class="search-item-meta">${esc(where)}</span>`
    }
    if (item.type === 'musician') {
      return `<span class="search-item-name">${esc(item.name)}</span>
              <span class="search-item-meta">${esc((item.member_of || []).join(', '))}</span>`
    }
    const n = item.recording_count
    return `<span class="search-item-name">${esc(item.name)}</span>
            <span class="search-item-meta">${n} recording${n === 1 ? '' : 's'}</span>`
  }

  function renderSearchDropdown(body) {
    _searchItems  = []
    _searchActive = -1

    if (!body.groups.length) {
      // An honest empty state, not a fuzzy guess. With 178 acts in the
      // library a "did you mean" would confidently suggest nonsense.
      searchDropdown.innerHTML =
        `<div class="search-dropdown-empty">No artists, musicians, venues or dates match
         <b>${esc(body.query)}</b>.</div>`
      openSearchDropdown()
      return
    }

    let html = ''
    for (const g of body.groups) {
      const extra = g.total - g.items.length
      html += `<div class="search-group-label">
                 <span>${esc(g.label)}</span>
                 <span class="search-group-count">${g.total}</span>
               </div>`
      for (const item of g.items) {
        const i = _searchItems.length
        _searchItems.push(item)
        html += `<div class="search-item" role="option" data-idx="${i}">${searchGroupLine(item)}</div>`
      }
      if (extra > 0) {
        html += `<div class="search-more" data-all="1">Show all ${g.total} ${esc(g.label.toLowerCase())} →</div>`
      }
    }
    searchDropdown.innerHTML = html
    openSearchDropdown()
  }

  function openSearchDropdown() {
    searchDropdown.classList.remove('hidden')
    searchInput.setAttribute('aria-expanded', 'true')
  }

  function closeSearchDropdown() {
    searchDropdown.classList.add('hidden')
    searchInput.setAttribute('aria-expanded', 'false')
    _searchActive = -1
  }

  function setSearchActive(next) {
    const rows = searchDropdown.querySelectorAll('.search-item')
    if (!rows.length) return
    if (_searchActive >= 0) rows[_searchActive]?.classList.remove('is-active')
    _searchActive = (next + rows.length) % rows.length
    const el = rows[_searchActive]
    el.classList.add('is-active')
    el.scrollIntoView({ block: 'nearest' })
  }

  async function runSearchDropdown(q) {
    const seq = ++_searchSeq
    let body
    try {
      body = await API.search.all(q, SEARCH_DROPDOWN_MAX)
    } catch (e) {
      // A failed fetch that renders as an empty dropdown is indistinguishable
      // from "nothing matched" — a trap this project has hit repeatedly with
      // remote calls (CONTEXT, "Remote failures disguise themselves").
      if (seq !== _searchSeq) return
      searchDropdown.innerHTML =
        `<div class="search-dropdown-empty">Search failed: ${esc(e.message)}</div>`
      openSearchDropdown()
      return
    }
    // A slower earlier request must never overwrite a newer result.
    if (seq !== _searchSeq) return
    renderSearchDropdown(body)
  }

  function searchResultsHash(q, type) {
    const base = `#/search?q=${encodeURIComponent(q)}`
    return type ? `${base}&type=${encodeURIComponent(type)}` : base
  }

  function submitSearch() {
    const q = searchInput.value.trim()
    if (q.length < SEARCH_MIN_CHARS) return
    closeSearchDropdown()
    searchInput.blur()
    window.location.hash = searchResultsHash(q)
  }

  function wireSearchBar() {
    if (!searchInput) return

    searchInput.addEventListener('input', () => {
      const q = searchInput.value.trim()
      setSearchFieldFilled(searchInput.value)
      clearTimeout(_searchTimer)
      if (q.length < SEARCH_MIN_CHARS) {
        _searchSeq++            // cancel anything in flight
        closeSearchDropdown()
        return
      }
      _searchTimer = setTimeout(() => runSearchDropdown(q), SEARCH_DEBOUNCE_MS)
    })

    searchInput.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown')      { e.preventDefault(); setSearchActive(_searchActive + 1) }
      else if (e.key === 'ArrowUp')   { e.preventDefault(); setSearchActive(_searchActive - 1) }
      else if (e.key === 'Escape')    { closeSearchDropdown(); searchInput.blur() }
      else if (e.key === 'Enter') {
        e.preventDefault()
        // An arrowed-to row goes straight to that thing; a bare Enter opens
        // the full results page.
        if (_searchActive >= 0 && _searchItems[_searchActive]) {
          const item = _searchItems[_searchActive]
          closeSearchDropdown()
          searchInput.blur()
          window.location.hash = item.hash
        } else {
          submitSearch()
        }
      }
    })

    searchInput.addEventListener('focus', () => {
      if (searchInput.value.trim() && searchDropdown.innerHTML) openSearchDropdown()
    })

    searchDropdown.addEventListener('mousedown', e => {
      // mousedown, not click: the input's blur handler would otherwise close
      // the dropdown before the click ever lands on the row.
      const more = e.target.closest('.search-more')
      if (more) { e.preventDefault(); submitSearch(); return }
      const row = e.target.closest('.search-item')
      if (!row) return
      e.preventDefault()
      const item = _searchItems[parseInt(row.dataset.idx, 10)]
      if (!item) return
      closeSearchDropdown()
      searchInput.blur()
      window.location.hash = item.hash
    })

    searchClearBtn.addEventListener('click', () => {
      searchInput.value = ''
      setSearchFieldFilled(false)
      _searchSeq++
      closeSearchDropdown()
      searchInput.focus()
    })

    document.addEventListener('mousedown', e => {
      if (!searchBar.contains(e.target)) closeSearchDropdown()
    })

    // "/" focuses the box from anywhere — but never while the user is typing
    // into something else, which would eat the character.
    document.addEventListener('keydown', e => {
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target
      const tag = (t && t.tagName || '').toLowerCase()
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || (t && t.isContentEditable)) return
      e.preventDefault()
      searchInput.focus()
      searchInput.select()
    })

    // Cmd-R (Ctrl-R elsewhere) reloads the UI (Ryan, 2026-10-01). The desktop
    // window has no browser menu, so nothing else provides it. The hash route
    // survives the reload, so the same page comes back.
    document.addEventListener('keydown', e => {
      if ((e.key === 'r' || e.key === 'R') && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
        e.preventDefault()
        window.location.reload()
      }
    })
  }

  // ── Results page ───────────────────────────────────────────────────────────

  function parseSearchHash(hash) {
    const qs = hash.slice(hash.indexOf('?') + 1)
    const params = new URLSearchParams(hash.includes('?') ? qs : '')
    return { q: params.get('q') || '', type: params.get('type') || null }
  }

  function searchRowHtml(item) {
    if (item.type === 'recording') {
      const where = [item.venue, item.city, item.state].filter(Boolean).join(' · ')
      // Recording-artwork thumbnail (Studio Records spec v1, chunk 5) -- no
      // placeholder when the recording has no image.
      const thumb = item.image_url
        ? `<img class="rec-thumb-sm" src="${esc(item.image_url)}" alt="" loading="lazy">`
        : `<span class="rec-thumb-sm rec-thumb-sm--initials">${esc(recInitials({ artist: item.artist }))}</span>`
      return `<div class="search-row" data-hash="${esc(item.hash)}">
                <span class="search-row-date">${esc(item.date || '—')}</span>
                ${thumb}
                <div class="search-row-main">
                  <div class="search-row-name">${esc(item.artist || 'Unknown')}</div>
                  <div class="search-row-meta">${esc(where || 'No venue recorded')}</div>
                </div>
                <div class="search-row-right">${sourceBadge(item.source)}</div>
              </div>`
    }
    if (item.type === 'album') {
      // Studio Records spec v1, section 7 — title leads, then artist and
      // year; never a venue slot (a studio record has none). No title: the
      // name slot falls back to the artist, so the meta line is the year
      // alone -- repeating the artist there would read as an echo.
      const thumb = item.image_url
        ? `<img class="rec-thumb-sm" src="${esc(item.image_url)}" alt="" loading="lazy">`
        : `<span class="rec-thumb-sm rec-thumb-sm--initials">${esc(recInitials({ artist: item.artist }))}</span>`
      const meta = item.title ? [item.artist, item.year].filter(Boolean).join(' · ') : (item.year || '')
      return `<div class="search-row" data-hash="${esc(item.hash)}">
                ${thumb}
                <div class="search-row-main">
                  <div class="search-row-name">${esc(item.title || item.artist || 'Unknown')}</div>
                  <div class="search-row-meta">${esc(meta)}</div>
                </div>
              </div>`
    }
    if (item.type === 'venue') {
      const where = [item.city, item.state, item.country].filter(Boolean).join(', ')
      const n = item.recording_count
      return `<div class="search-row" data-hash="${esc(item.hash)}">
                <div class="search-row-main">
                  <div class="search-row-name">${esc(item.name)}</div>
                  <div class="search-row-meta">${esc(where)}</div>
                </div>
                <div class="search-row-right"><span class="search-row-meta">${n} recording${n === 1 ? '' : 's'}</span></div>
              </div>`
    }
    if (item.type === 'musician') {
      return `<div class="search-row" data-hash="${esc(item.hash)}">
                <div class="search-row-main">
                  <div class="search-row-name">${esc(item.name)}</div>
                  <div class="search-row-meta">${esc((item.member_of || []).join(', ') || 'No artists recorded')}</div>
                </div>
              </div>`
    }
    const n = item.recording_count
    return `<div class="search-row" data-hash="${esc(item.hash)}">
              <div class="search-row-main"><div class="search-row-name">${esc(item.name)}</div></div>
              <div class="search-row-right"><span class="search-row-meta">${n} recording${n === 1 ? '' : 's'}</span></div>
            </div>`
  }

  function searchZeroStateHtml(q) {
    // Name the miss, then offer real doors in. Deliberately not a fuzzy
    // suggestion (Ryan, 2026-08-18): a dead end the user understands beats a
    // confident wrong guess.
    return `<div class="search-zero">
      <div class="search-zero-title">Nothing matches “${esc(q)}”.</div>
      <div class="search-zero-body">
        Search covers artists, the musicians in them, venues and show dates.
        Try a shorter name, or a year on its own like <b>1983</b>.
      </div>
      <div class="search-zero-doors">
        <button class="btn btn-ghost btn-sm" data-hash="#/">Browse the library</button>
        <button class="btn btn-ghost btn-sm" data-hash="#/recent">Recently added</button>
        <button class="btn btn-ghost btn-sm" data-hash="#/artists">All artists</button>
        <button class="btn btn-ghost btn-sm" data-hash="#/venues">All venues</button>
      </div>
    </div>`
  }

  function wireSearchRows(root) {
    root.querySelectorAll('[data-hash]').forEach(el =>
      el.addEventListener('click', () => { window.location.hash = el.dataset.hash }))
  }

  function searchTermsSummary(body) {
    const bits = []
    if (body.text_terms.length) bits.push(body.text_terms.join(' + '))
    for (const [y, m, d] of body.date_terms) {
      bits.push(d ? `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`
                  : m ? `${y}-${String(m).padStart(2,'0')}` : `${y}`)
    }
    return bits.join(' · ')
  }

  // ── Search page ────────────────────────────────────────────────────────────
  // A full-page search box (Ryan, 2026-08-23). Same engine, same hash and the
  // same row markup as the App Header's field — but the results render
  // UNDERNEATH as you type rather than into a dropdown. A dropdown floating
  // over the page you are already looking at would be covering its own
  // results; the header field needs one because it has no page of its own.
  //
  // The box is rendered ONCE and never re-rendered while typing. Re-rendering
  // it would take the caret with it — which is why only #search-page-results
  // is replaced, and why keystrokes use history.replaceState rather than
  // setting location.hash (that would re-enter route() and rebuild everything).
  function searchPageHtml(q) {
    return `
      <div class="artist-header">
        <div class="artist-header-row">
          <h1>Search My Library</h1>
        </div>
      </div>
      <div class="search-page">
        <div class="search-page-field">
          ${icon('search', 'search-page-ic')}
          <input type="text" id="search-page-input" class="search-page-input"
                 placeholder="Search artists, musicians, venues, cities, years"
                 autocomplete="off" spellcheck="false" value="${esc(q)}" />
          <button class="search-page-clear${q ? '' : ' hidden'}" id="search-page-clear"
                  title="Clear" tabindex="-1">${icon('x')}</button>
        </div>
        <div class="search-page-results" id="search-page-results"></div>
      </div>`
  }

  function searchPageHintHtml(q) {
    const short = q && q.length > 0 && q.length < SEARCH_MIN_CHARS
    return `<div class="search-page-hint">
      ${short ? `<div class="search-page-hint-min">Keep typing. Searches start at ${SEARCH_MIN_CHARS} characters.</div>` : ''}
    </div>`
  }

  let _searchPageTimer = null

  async function renderPageResults(q) {
    const box = document.getElementById('search-page-results')
    if (!box) return
    // Short queries are not searched at all — no request, no flicker of
    // results for "j" that vanish at "jo". The hint stays put instead.
    if (q.length < SEARCH_MIN_CHARS) { box.innerHTML = searchPageHintHtml(q); return }

    let body
    try {
      body = await API.search.all(q, SEARCH_OVERVIEW_MAX)
    } catch (e) {
      box.innerHTML = `<div class="empty-state"><div class="empty-title">Search failed</div>
                       <div class="empty-sub">${esc(e.message)}</div></div>`
      return
    }
    // The box is live, so a stale response must not overwrite a newer query.
    const live = document.getElementById('search-page-input')
    if (live && live.value.trim() !== q) return

    if (!body.groups.length) { box.innerHTML = searchZeroStateHtml(q); wireSearchRows(box); return }

    const terms = searchTermsSummary(body)
    let html = `<div class="search-results">
      <div class="search-results-head">
        <div class="search-results-title">${body.total} result${body.total === 1 ? '' : 's'} for <b>${esc(q)}</b></div>
        ${terms ? `<div class="search-results-sub">Matching ${esc(terms)}</div>` : ''}
      </div>`
    for (const g of body.groups) {
      html += `<div class="search-section">
        <div class="search-section-head">
          <span class="search-section-title">${esc(g.label)}</span>
          <span class="search-section-count">${g.total}</span>
        </div>
        ${g.items.map(searchRowHtml).join('')}
        ${g.total > g.items.length
          ? `<button class="btn btn-ghost btn-sm search-more-btn"
                     data-hash="${esc(searchResultsHash(q, g.type))}">Show all ${g.total}</button>`
          : ''}
      </div>`
    }
    html += `</div>`
    box.innerHTML = html
    wireSearchRows(box)
  }

  function wireSearchPage() {
    const input = document.getElementById('search-page-input')
    const clear = document.getElementById('search-page-clear')
    if (!input) return
    input.focus()
    input.setSelectionRange(input.value.length, input.value.length)

    const run = () => {
      const q = input.value.trim()
      clear?.classList.toggle('hidden', !q)
      // replaceState, not location.hash: the URL stays shareable and correct
      // without re-entering route() on every keystroke.
      try { history.replaceState(null, '', q ? searchResultsHash(q) : '#/search') } catch (_) {}
      renderPageResults(q)
    }
    input.addEventListener('input', () => {
      clearTimeout(_searchPageTimer)
      _searchPageTimer = setTimeout(run, 180)
    })
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        clearTimeout(_searchPageTimer)
        // A real navigation on Enter, so this search lands in history and Back
        // returns to where the user came from.
        const q = input.value.trim()
        if (q.length >= SEARCH_MIN_CHARS) window.location.hash = searchResultsHash(q)
        else run()
      } else if (e.key === 'Escape') {
        input.value = ''; run()
      }
    })
    clear?.addEventListener('click', () => { input.value = ''; run(); input.focus() })
  }

  async function renderSearchView(hash) {
    const { q, type } = parseSearchHash(hash)
    setActiveNav('search')
    setActiveArtist(null)
    setNavCurrent('Search')          // omitting this breaks nav-back everywhere
    searchInput.value = q
    setSearchFieldFilled(q)
    closeSearchDropdown()

    // "Show all N" drill-downs keep their own full-page layout.
    if (type) { setLoading(); return renderSearchGroupPage(q, type) }

    setMainHTML(searchPageHtml(q))
    wireSearchPage()
    renderPageResults(q)
  }

  async function renderSearchGroupPage(q, type) {
    let body
    try {
      body = await API.search.group(q, type, SEARCH_PAGE_SIZE, 0)
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Search failed</div>
                   <div class="empty-sub">${esc(e.message)}</div></div>`)
      return
    }

    setMainHTML(`<div class="search-results">
      <div class="search-results-head">
        <div class="search-results-title">${body.total} ${esc(body.label.toLowerCase())} for <b>${esc(q)}</b></div>
        <div class="search-results-sub"><span class="breadcrumb" data-hash="${esc(searchResultsHash(q))}">All results</span></div>
      </div>
      <div id="search-group-rows">${body.items.map(searchRowHtml).join('')}</div>
      <div id="search-group-more"></div>
    </div>`)

    const rowsEl = document.getElementById('search-group-rows')
    const moreEl = document.getElementById('search-group-more')
    let loaded = body.items.length

    const drawMore = () => {
      moreEl.innerHTML = loaded < body.total
        ? `<button class="btn btn-ghost btn-sm search-more-btn" id="search-load-more">
             Load more (${body.total - loaded} left)</button>`
        : ''
      document.getElementById('search-load-more')?.addEventListener('click', async () => {
        const next = await API.search.group(q, type, SEARCH_PAGE_SIZE, loaded)
        rowsEl.insertAdjacentHTML('beforeend', next.items.map(searchRowHtml).join(''))
        loaded += next.items.length
        wireSearchRows(rowsEl)
        drawMore()
      })
    }

    wireSearchRows(mainContent)
    drawMore()
  }

  // ── Hash routing ───────────────────────────────────────────────────────────

  window.addEventListener('hashchange', route)

  wireSearchBar()
  wireHeaderNav()

  // ── Init ───────────────────────────────────────────────────────────────────

  // ── Library drive status ──────────────────────────────────────────────────
  //
  // LIBRARY_ROOT lives on an SMB share that macOS drops on update, reboot and
  // sleep. The database is local SQLite and stays perfectly usable, so the app
  // must keep browsing — what it must NOT do is pretend nothing happened and
  // render a wall of broken images.
  //
  // Two inputs, deliberately:
  //   * a 30s poll, so the banner appears even if you touch nothing
  //   * a 'trellis:library-disconnected' event from api.js the instant any
  //     request 503s, so you never sit inside the poll window wondering
  //
  // Repair is not our job — tools/mount_library.py owns mounting. When the
  // LaunchAgent fixes it, the next poll clears the banner on its own.
  const libraryDrive = (() => {
    const POLL_MS = 30000
    let offline = false
    let last    = null

    const els = () => ({
      bar:  document.getElementById('library-banner'),
      text: document.getElementById('library-banner-text'),
    })

    function render(st) {
      last = st
      const nowOffline = !st.connected
      const { bar, text } = els()
      if (!bar) return

      bar.classList.toggle('hidden', !nowOffline)
      if (nowOffline) {
        // Server-side copy: the message and the diagnosis that produced it
        // live together in api/system.py and cannot drift apart.
        text.textContent = st.message || 'The library drive is not connected.'
      }

      // One class drives every disabled affordance in the CSS. Cheaper and far
      // more reliable than hunting play buttons through 10k lines of renderers.
      document.body.classList.toggle('drive-offline', nowOffline)

      // Commit the flag BEFORE any side effect. route() re-renders an entire
      // view and can throw for reasons that have nothing to do with the drive;
      // if it does, isOffline() must not be left stuck reporting the old value
      // while the banner already says we recovered.
      const recovered = offline && !nowOffline
      offline = nowOffline

      // Re-render so the placeholder SVGs the server handed out during the
      // outage get replaced by real artwork.
      if (recovered) {
        try { route() } catch (e) { console.warn('post-reconnect re-render failed', e) }
      }
    }

    async function check({ force = false } = {}) {
      try {
        render(force ? await API.system.libraryRecheck()
                     : await API.system.libraryStatus())
      } catch (e) {
        // Auth expiry or the server being down are different problems with
        // their own handling. Leave the last known state rather than claiming
        // the drive is gone on the strength of a failed fetch.
      }
    }

    let started = false
    function start() {
      if (started) return          // login path and boot path can both reach here
      started = true
      check()
      setInterval(check, POLL_MS)

      // Instant signal from any 503 — see api.js.
      window.addEventListener('trellis:library-disconnected', (e) => {
        render({ connected: false, ...(e.detail || {}) })
      })

      document.getElementById('library-banner-recheck')
        ?.addEventListener('click', () => check({ force: true }))
    }

    return {
      start,
      check,
      isOffline: () => offline,
      message:   () => (last && last.message) || 'The library drive is not connected.',
    }
  })()

  // Boot in TWO stages, deliberately.
  //
  // Stage 1 is the only authentication question. Stage 2 is everything that
  // can fail for a hundred unrelated reasons. They used to share one try/catch
  // whose handler said "Not logged in — show login screen", so ANY error after
  // the auth check — a render bug, a bad endpoint, an unmounted drive — logged
  // you out of a session that was perfectly valid, swallowed the real error,
  // and sent whoever was debugging it into the auth code. That cost an hour on
  // 2026-08-23; the actual fault was a shadowed variable in renderSidebar().
  //
  // The rule: never report a failure as a different, more familiar failure.
  async function init() {
    let user
    try {
      user = await API.auth.me()
    } catch (e) {
      if (e && e.status && e.status !== 401) {
        // The server answered, and it was not "who are you?" — a 500 or a 503
        // is not a credentials problem and must not be dressed up as one.
        bootFailure(e, 'Could not reach the server')
        return
      }
      // 401, or the fetch never completed. Both land the user at the login
      // screen, which is correct: one needs credentials, the other cannot
      // prove they have any.
      showLogin()
      document.getElementById('login-screen').classList.remove('hidden')
      return
    }

    try {
      state.user = user
      setUserUI(user)
      // Announced rather than polled: debug.js loads before login and needs to
      // know the moment a user exists, without asking repeatedly whether one
      // does. Any other boot-time listener can use the same event.
      window.dispatchEvent(new CustomEvent('trellis:user', { detail: user }))
      showApp()
      libraryDrive.start()
      // Before the sidebar renders: the library selector reads
      // libraryState.remotes, and a selector that appears as a plain label and
      // then sprouts a dropdown a moment later reads as a glitch.
      await loadRemotes()
      // Bulk Ingest (spec 7a/7c) — fetched once here, before the sidebar,
      // so its nav item (renderSidebar) is right on the very first paint
      // rather than popping in a moment later.
      await _fetchBulkIngestStatus()
      await loadArtistList()
      const bulkIngestActive = state.bulkIngest
        && (state.bulkIngest.status === 'running' || state.bulkIngest.status === 'paused')
      if (bulkIngestActive && (window.location.hash || '#/') === '#/') {
        window.location.hash = '#/bulk-ingest'   // hashchange re-enters route()
      } else {
        route()
      }
    } catch (e) {
      // Authenticated, but the app failed to come up. Say THAT.
      bootFailure(e, 'The app failed to start')
    }
  }

  // A boot failure the user can actually act on: the real message, the real
  // stack, on screen. Silent failure is what made this class of bug expensive
  // — the error existed, was caught, and was then thrown away.
  function bootFailure(err, headline) {
    console.error('[boot]', headline, err)
    try { showApp() } catch (_) {}
    const msg   = (err && err.message) || String(err)
    const stack = (err && err.stack)   || ''
    const el = document.createElement('div')
    el.className = 'boot-error'
    el.innerHTML = `
      <div class="boot-error-head">${esc(headline)}</div>
      <div class="boot-error-msg">${esc(msg)}</div>
      ${stack ? `<pre class="boot-error-stack">${esc(stack)}</pre>` : ''}
      <div class="boot-error-foot">
        <button class="btn btn-ghost btn-xs" id="boot-error-reload">Reload</button>
        <button class="btn btn-ghost btn-xs" id="boot-error-dismiss">Dismiss</button>
      </div>`
    document.body.appendChild(el)
    el.querySelector('#boot-error-reload').onclick  = () => location.reload()
    el.querySelector('#boot-error-dismiss').onclick = () => el.remove()
  }

  init()

  // Wire player bar skip toggle (always present in the DOM)
  document.getElementById('skip-filter-player')?.addEventListener('change', function () {
    setSkipFilter(this.checked)
  })

  // Expose minimal state for debug panel
  window.trellisState = {
    get recordingId() { return state.currentRecId },
    get trackCount()  { return state._lastTrackCount || null },
    // Paula's full scan-time breakdown (score + every flag/component per
    // attribute + track completeness) — surfaced to the debug panel's
    // dedicated Paula section. Null outside the Add Recording flow, or
    // before a folder's been scanned.
    get paula()       { return (typeof ingest !== 'undefined' && ingest?.scan?.paula) || null },
  }

  return { onTrackChange, syncPlayButtons, libraryDrive }

})()
