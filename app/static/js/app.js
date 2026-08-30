/**
 * app.js — Flux Audio SPA: router, state, and view renderers.
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
    // covered Recording→Performer/Venue, and several hardcoded '#/'
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
  }

  // ── Track flag registry — single source of truth ─────────────────────────
  // Every flag list/label/skip-set is derived from this one array. `nonMusic`
  // marks flags whose tracks are skipped by the "Skip non-music" filter.
  const TRACK_FLAGS = [
    { key: 'start_truncated', label: 'Start Truncated', nonMusic: false },
    { key: 'end_truncated',   label: 'End Truncated',   nonMusic: false },
    { key: 'incomplete',      label: 'Incomplete',      nonMusic: false },
    { key: 'unknown_title',   label: 'Unknown Title',   nonMusic: false },
    { key: 'banter',          label: 'Banter',          nonMusic: true  },
    { key: 'tuning',          label: 'Tuning',          nonMusic: true  },
    { key: 'audience',        label: 'Audience',        nonMusic: true  },
    { key: 'medley',          label: 'Medley',          nonMusic: false },
    { key: 'announcement',    label: 'Announcement',    nonMusic: true  },
    { key: 'interview',       label: 'Interview',       nonMusic: true  },
    { key: 'introduction',    label: 'Introduction',    nonMusic: true  },
    { key: 'band_intros',     label: 'Band Intros',     nonMusic: true  },
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
    step:       'source',  // 'source' | 'triage' | 'review' | 'success' — unified ingestion flow
                           // (2026-07-30): source picker -> Listening Quality triage -> metadata
                           // review (renders as the '#/batch' stage, see the `batch`/`lq` state
                           // below) -> ingest. (Confirm step removed 2026-07-15 — review's own
                           // "Add Recording →" button now submits directly)
    folderPath: null,
    scan:       null,      // full scan API response
    // 'copy' | 'move'. DEFAULT MOVE as of 2026-08-07 (Ryan): copy leaves the
    // source in place, so a re-scan re-offers the same show and duplicates
    // creep in. Move makes a second ingest of the same files impossible.
    behavior:   'move',
    form: {},              // resolved metadata (populated on review step)
    tracks:     [],        // array of { track_number, title, set, duration, filename }
    // True when this review was opened via Bulk Import's "Review →" (see
    // _batchOpenReview) rather than a fresh Add Recording nav — drives the
    // standardized back-link (top of the review page) and the post-submit
    // redirect target (Ryan, 2026-07-15: bulk reviewers need a fast way back
    // to the queue, not a forced detour through the new recording's page).
    fromBatch:  false,
    // True when this review was opened from the Listening Quality triage
    // queue's "Review" action. Distinct from fromBatch because the two return
    // to different places — triage returns to '#/ingest' in its triage step,
    // which is the only place that knows the queue's state.
    fromTriage: false,
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

  // ── Theme ──────────────────────────────────────────────────────────────────
  // The ◑ button left the sidebar header on 2026-08-22 (that header is gone —
  // the app name and icon moved to the App Header). Light/dark is a preference,
  // set rarely, so it lives in Settings with the other preferences rather than
  // taking permanent space in the chrome. Still localStorage, not the server:
  // it is per-machine, and it must apply before the first paint.
  function setTheme(light) {
    document.body.classList.toggle('theme-light', light)
    localStorage.setItem('fluxTheme', light ? 'light' : 'dark')
  }
  function isLightTheme() { return document.body.classList.contains('theme-light') }

  // ── Resizable sidebar ──────────────────────────────────────────────────────
  ;(function () {
    const MIN = 200, MAX = 460
    const setW = w => document.documentElement.style.setProperty('--sidebar-w', Math.round(w) + 'px')
    const saved = parseInt(localStorage.getItem('fluxSidebarW'), 10)
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
      localStorage.setItem('fluxSidebarW', parseInt(cur, 10))
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
  // told in pictures: a Performer is the ACT that took the stage (users), an
  // Artist is a PERSON (user).
  //
  // Only icons actually in use belong here. A grab-bag of unused glyphs is how
  // icons end up sprinkled on everything.
  const ICONS = {
    // AI Assist (2026-08-28). Was U+2728 SPARKLES, drawn by the OS colour
    // emoji font at its own weight, baseline and palette — the same objection
    // that got the speaker emoji out of the player bar on 08-23. Lucide
    // 'sparkles', so it strokes and colours like every other icon here.
    'sparkles':     '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/>',
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
    'plus':         '<path d="M5 12h14"/><path d="M12 5v14"/>',
    'minus':        '<path d="M5 12h14"/>',
    'library':      '<path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/>',
    'search':       '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>',
    'clock':        '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
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
  function chevronIcon(cls) {
    return `<svg class="caret-ic${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>`
  }

  // Canonical form for comparing filesystem paths client-side. macOS hands out
  // decomposed (NFD) filenames; the quality API's staging rows are always
  // NFC-normalised server-side (app/utils/quality_store.py), but batch-scan's
  // item.path is a raw, un-normalised os.scandir() result — so a straight ===
  // between the two can silently miss a match on any accented folder name
  // (the "Guitar Trio" bug, 2026-07-28). Normalise both sides before comparing.
  const nfc = s => (s || '').normalize('NFC')

  // Title-case a string: capitalize each word, lowercase the rest.
  // Keeps short connective words lowercase unless they're the first word.
  const _lcWords = new Set(['a','an','the','and','but','or','for','nor','on','at',
                             'to','by','in','of','up','as','is','with','vs','feat'])
  function titleCase(s) {
    if (!s) return s
    return s.split(' ').map((w, i) => {
      if (!w) return w
      const lo = w.toLowerCase()
      // Words can start with punctuation ("(Bill", "\"Song", "-Encore") — find
      // the first actual letter to capitalize instead of blindly upper-casing
      // index 0, which no-ops on the punctuation and leaves the real first
      // letter (and everything else) lowercase. Minor-word lowering only
      // applies to the plain no-punctuation case, same as before.
      const m = lo.match(/[a-z]/)
      if (!m) return lo
      const idx = m.index
      if (idx === 0 && i !== 0 && _lcWords.has(lo)) return lo
      return lo.slice(0, idx) + lo.charAt(idx).toUpperCase() + lo.slice(idx + 1)
    }).join(' ')
  }

  // ── Track flag auto-detection ─────────────────────────────────────────────
  // JS port of app/utils/ingest.py::detect_track_flags — kept deliberately
  // conservative. Words like "talk"/"speak"/"crowd" also show up in real
  // song titles ("Don't Talk", "Speak Low"), so ambiguous flags only fire on
  // a whole-segment match, never a loose substring. These are suggestions
  // pre-checked in the ingest wizard for the archivist to approve or remove
  // — never applied silently.
  const _FLAG_START_TRUNC = /^\s*\/\//
  const _FLAG_END_TRUNC   = /\/\/\s*$/
  const _FLAG_INCOMPLETE  = /\(\s*x\s*\)\s*$/i
  const _FLAG_TRAILING_PAREN = /^(.*?)\s*\([^)]*\)\s*$/
  const _FLAG_SEGMENT_SPLIT  = /\s*(?:,|\/|&|\band\b)\s*/i
  // Whole-segment thesaurus — mirrors _FLAG_SEGMENT_SYNONYMS in
  // app/utils/ingest.py::detect_track_flags exactly. Adding a synonym here
  // MUST be added there too, or the two engines disagree.
  const _FLAG_SEGMENT_SYNONYMS = {
    tuning:       ['tuning'],
    banter:       ['banter', 'dialogue', 'chatter', 'crosstalk'],
    audience:     ['audience', 'crowd'],
    band_intros:  ['band intro', 'band introduction'],
    introduction: ['intro', 'introduction'],
  }
  function _segmentPattern(words) {
    const alts = words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
    return new RegExp(`^(?:${alts})s?\\.?$`, 'i')
  }
  const _FLAG_SEGMENT_PATTERNS = Object.entries(_FLAG_SEGMENT_SYNONYMS)
    .map(([key, words]) => [key, _segmentPattern(words)])
  const _FLAG_WORD_PATTERNS = [
    ['announcement', /\bannouncements?\b/i],
    ['interview',    /\binterviews?\b/i],
  ]

  // ── The ONE track builder ────────────────────────────────────────────────
  //
  // Every path that posts to /api/ingest/confirm builds its track list here:
  // the triage page's Ingest button and bulk queue (ingestOne), Batch Import's
  // auto-ingest (_batchIngestOne), and the Add Recording wizard
  // (renderIngestReview). There were three copies of this logic and they had
  // drifted badly (Ryan, 2026-08-28: "ensure parity ... no matter if it happens
  // from a bulk run or from a single addition"):
  //
  //   * the two AUTO paths never called detectTrackFlags at all, so every
  //     recording ingested without opening the wizard arrived with zero track
  //     flags — and since Quick Add became the default, that is nearly
  //     everything. The Skip Filter, which reads those flags, was doing nothing
  //     for recent material;
  //   * the two AUTO paths were not set-aware, so a multi-disc source could
  //     produce two tracks numbered 1. That collision was found and fixed for
  //     the wizard on 2026-07-14 and never ported;
  //   * only the wizard title-cased, so the same folder produced differently
  //     capitalised titles depending on which button was pressed.
  //
  // Returns the array the confirm endpoint expects. Pure: no DOM, no state, no
  // network, so the three callers cannot diverge again without editing this.
  function buildIngestTracks(scan) {
    const tags  = scan?.suggestions?.from_tags || {}
    const info  = scan?.suggestions?.from_info_file || {}
    const files = scan?.audio_files || []
    const tagTracks  = tags.tracks || []
    const infoTracks = info.tracks || []

    // Info-file titles are keyed by their printed track NUMBER, which lines up
    // with the scan's 1-based index.
    const infoMap = {}
    infoTracks.forEach(t => { infoMap[t.number] = t.title })

    // Songwriter credits the parser split out of a title's trailing
    // "(Composer Name)" (Ryan, 2026-08-30) — keyed the same way, and offered
    // as a fallback regardless of which source wins the TITLE, since the
    // credit only ever comes from the info file's text either way.
    const infoSongwriterMap = {}
    infoTracks.forEach(t => { if (t.songwriter) infoSongwriterMap[t.number] = t.songwriter })

    // rel_path, not bare filename: a multi-disc source has a "01.flac" per
    // disc and the bare name collides.
    const setByRelPath = {}
    files.forEach(af => {
      if (af.set_number && af.rel_path) setByRelPath[af.rel_path] = af.set_number
    })
    const setsDetected = !!scan?.sets_detected

    const mk = (title, relPath, trackNumber, duration, setNumber, songwriter) => ({
      track_number: trackNumber,
      title,
      songwriter:   songwriter || null,
      set_number:   setNumber || null,
      duration:     duration || null,
      filename:     relPath,
      // Suggestions, not assertions. The wizard shows them as pills to approve
      // or remove; the auto paths accept them as-is, which is the same bargain
      // the auto paths already make with every other extracted field.
      flags:        detectTrackFlags(title),
    })

    // Preferred: one entry per tagged audio file.
    if (tagTracks.length) {
      return tagTracks.map(t => {
        const relPath = t.rel_path || t.filename
        return mk(
          titleCase(t.title || infoMap[t.index]) || `Track ${t.index}`,
          relPath,
          // Multi-disc sources reset TRACKNUMBER per disc, so the tag is only
          // trustworthy when there is a single set. The scan index is already
          // continuous across discs in the right order.
          (!setsDetected && t.track_number) ? parseInt(t.track_number) : t.index,
          t.duration,
          setByRelPath[relPath],
          infoSongwriterMap[t.index])
      })
    }

    // Nothing tagged: fall back to the info file's listing, positionally.
    if (infoTracks.length) {
      return infoTracks.map(t => {
        const f = files[t.number - 1] || {}
        const relPath = f.rel_path || f.filename || ''
        return mk(titleCase(t.title) || `Track ${t.number}`,
                  relPath, t.number, null, setByRelPath[relPath], t.songwriter)
      })
    }

    // Neither source has anything to say — describe the files themselves
    // rather than posting an empty track list.
    return files.map((f, idx) => {
      const relPath = f.rel_path || f.filename || ''
      return mk(`Track ${idx + 1}`, relPath, idx + 1, null, f.set_number)
    })
  }

  function detectTrackFlags(title) {
    if (!title) return []
    const flags = new Set()
    const raw = title.trim()

    if (_FLAG_START_TRUNC.test(raw)) flags.add('start_truncated')
    if (_FLAG_END_TRUNC.test(raw))   flags.add('end_truncated')
    if (_FLAG_INCOMPLETE.test(raw))  flags.add('incomplete')

    // One trailing parenthetical is stripped as an attribution ("(Bobby)").
    // ⚠ Unless that leaves NOTHING: "(Chatter)", "(Introduction)",
    // "(Cox Family Intro)" are titles that are ENTIRELY a parenthetical, and
    // the strip reduced them to "" so nothing could match. 9 of 53 missed
    // non-music tracks across 1,499 real titles were this one case.
    // Mirrors detect_track_flags in app/utils/ingest.py — change both.
    const parenMatch = raw.match(_FLAG_TRAILING_PAREN)
    let base = parenMatch ? parenMatch[1].trim() : raw
    if (parenMatch && !base) {
      const inner = raw.match(/^\s*\((.*)\)\s*$/)
      base = inner ? inner[1].trim() : raw
    }

    base.split(_FLAG_SEGMENT_SPLIT).forEach(segment => {
      segment = segment.trim()
      if (!segment) return
      _FLAG_SEGMENT_PATTERNS.forEach(([key, pattern]) => {
        if (pattern.test(segment)) flags.add(key)
      })
    })

    _FLAG_WORD_PATTERNS.forEach(([key, pattern]) => {
      if (pattern.test(base)) flags.add(key)
    })

    return [...flags].sort()
  }

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
    return localStorage.getItem('fluxViewMode') === 'playback' ? 'playback' : 'admin'
  }

  function setViewMode(mode) {
    const next = mode === 'playback' ? 'playback' : 'admin'
    localStorage.setItem('fluxViewMode', next)
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
  // onPick receives {id|null, name}. Used for the Performer and Member pickers.
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

  // ── Reusable Performer + Members/Guests widget ───────────────────────────────
  // Bound to a `store` object holding `.members`, `.guests` (+ .performer_name/
  // .performer_id). `ids.field` is a mount point div — renderChips() rebuilds
  // its full innerHTML each call (both rows + pills + add controls) and
  // rewires events, the same rebuild-and-rewire pattern already used for
  // buildAiResultsHtml, rather than DOM-patching individual chips.
  //
  // Members/Guests two-row redesign (2026-07-22), replacing one flat Artists
  // pill row + descriptive subtext: a small (+) button per row reveals an
  // inline add-picker input on click. Removing a pill is a plain splice —
  // this is still draft form state until Confirm, no server round-trip.
  function createMembersWidget(store, ids) {
    const pill = (p, i, role) => `
      <span class="member-chip ${role === 'guest' ? 'member-chip--guest' : ''}">
        ${esc(p.name)} <span class="member-chip-x" data-role="${role}" data-idx="${i}" title="Remove">${icon('x')}</span>
      </span>`
    const row = (role, label, items) => `
      <div class="mg-row">
        <span class="mg-row-label">${label}</span>
        ${items.map((p, i) => pill(p, i, role)).join('')}
        <button type="button" class="mg-add-btn" data-role="${role}" title="Add ${label === 'Members' ? 'Member' : 'Guest'} Name">+</button>
        <span class="artist-picker-wrap mg-add-picker" data-role="${role}" style="display:none">
          <input type="text" class="member-input mg-role-input" data-role="${role}" autocomplete="off" placeholder="Add ${label === 'Members' ? 'Member' : 'Guest'} Name…" />
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

      field.querySelectorAll('.mg-add-btn').forEach(btn =>
        btn.addEventListener('click', () => {
          const picker = field.querySelector(`.mg-add-picker[data-role="${btn.dataset.role}"]`)
          const input  = picker?.querySelector('.mg-role-input')
          if (!picker || !input) return
          const showing = picker.style.display !== 'none'
          // Only one add-picker open at a time.
          field.querySelectorAll('.mg-add-picker').forEach(p => { p.style.display = 'none' })
          picker.style.display = showing ? 'none' : 'inline-flex'
          if (!showing) input.focus()
        }))

      field.querySelectorAll('.mg-role-input').forEach(input => {
        const role = input.dataset.role
        const dd   = field.querySelector(`.mg-role-dd[data-role="${role}"]`)
        wirePickerDropdown(input, dd, API.artists.search,
          ({ id, name }) => { addMember(name, id, role); input.value = '' }, 'Add new artist')
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); addMember(input.value, null, role); input.value = '' }
        })
      })
    }

    function addMember(name, id, role = 'member') {
      name = (name || '').trim()
      if (!name) return
      const list = role === 'guest' ? (store.guests = store.guests || []) : (store.members = store.members || [])
      if (list.some(m => m.name.toLowerCase() === name.toLowerCase())) return
      list.push(id ? { id, name } : { name })
      renderChips()
    }

    // Performer picked (existing act → load its current roster into Members;
    // new act → no members by default, Artists are optional and only added
    // for special collaborations). Guests always reset — a freshly (re)picked
    // act has no per-show guests carried over from whatever was typed before.
    async function onPerformerPick({ id, name }) {
      const el = document.getElementById(ids.performerInput)
      if (el) el.value = name
      store.performer_name = name
      store.performer_id   = id || null
      store.guests = []
      if (id) {
        try { const p = await API.performers.get(id); store.members = (p.members || []).map(m => ({ id: m.id, name: m.name })) }
        catch (_) { store.members = [] }
      } else {
        store.members = []
      }
      renderChips()
    }
    function mount() {
      wirePickerDropdown(document.getElementById(ids.performerInput), document.getElementById(ids.performerDropdown),
        API.performers.search, onPerformerPick, 'Create new performer')
      renderChips()
    }
    return { renderChips, addMember, onPerformerPick, mount }
  }

  // Splits a billed-act name into candidate individual-person names, for
  // matching against existing Artists when the Performer itself doesn't
  // exist yet (2026-07-22) — e.g. "Bela Fleck & Edgar Meyer" ->
  // ["Bela Fleck", "Edgar Meyer"]. Conservative separators only; a missed
  // split is harmless (that name just stays unmatched), which is why exact
  // matching below matters more than aggressive splitting here.
  const _NAME_SPLIT_RE = /\s*(?:&|,|\/|\+|\bwith\b|\bfeat\.?\b|\bfeaturing\b|\band\b)\s*/i
  function splitPerformerNameCandidates(raw) {
    return (raw || '').split(_NAME_SPLIT_RE).map(s => s.trim()).filter(Boolean)
  }

  // Add flow: preload Members if the scanned Performer (act) already exists
  // in the DB — pulls its current roster. If the act itself is new (e.g. a
  // one-off duo billing), fall back to splitting the act name into candidate
  // person names and matching each against existing Artists — EXACT
  // (case-insensitive) name match only, never a fuzzy/substring hit, since a
  // wrong auto-attached person is worse than an unmatched name Ryan fills in
  // by hand. Ryan chose auto-fill over a click-to-confirm suggestion step
  // for this (2026-07-22), weighing it against the AI-Assist auto-apply bug
  // fixed earlier the same session.
  async function initAddPerformerMembers(widget) {
    const f = ingest.form
    const name = (f.artist_name || '').trim()
    if (f._membersInit) { widget.renderChips(); return }
    f._membersInit = true
    if (!name) { f.members = f.members || []; widget.renderChips(); return }
    try {
      const matches = await API.performers.search(name)
      const exact = matches.find(m => m.name.toLowerCase() === name.toLowerCase())
      if (exact) {
        f.performer_id = exact.id
        const p = await API.performers.get(exact.id)
        f.members = (p.members || []).map(m => ({ id: m.id, name: m.name }))
      } else {
        const found = []
        for (const cand of splitPerformerNameCandidates(name)) {
          try {
            const results = await API.artists.search(cand)
            const hit = results.find(r => r.name.toLowerCase() === cand.toLowerCase())
            if (hit) found.push({ id: hit.id, name: hit.name })
          } catch (_) { /* best-effort — a failed lookup just leaves that name unmatched */ }
        }
        f.members = found
      }
    } catch (_) { f.members = f.members || [] }
    widget.renderChips()
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
  // data fetch succeeds for a dynamic one like a performer/venue/recording
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
    if (nav) nav.querySelectorAll('[data-nav]').forEach(el =>
      el.classList.toggle('active', el.dataset.nav === active))
  }

  function setActiveArtist(id) {
    document.querySelectorAll('#sidebar-nav .nav-record[data-dim="performers"]').forEach(el =>
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
      else if (dim === 'performers')   rows = await API.performers.list()
      else if (dim === 'artists')      rows = await API.artists.list()
      else if (dim === 'collections')  rows = await API.collections.list()
      else if (dim === 'genres')       rows = await API.genres.list()
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
    const target = { venues: 'venue', performers: 'artist',
                     artists: 'person', collections: 'collection', genres: 'genre' }[dim]
    if (!rows.length) {
      box.innerHTML = `<div class="nav-record nav-record--empty">None yet</div>`
      return
    }
    // Collections got its own row shape and click behaviour 2026-08-23: a
    // collection name is now click-to-expand-in-place (showing the recordings
    // it holds) rather than a navigation link, unindented to sit at the same
    // level as the COLLECTIONS header itself, and without the recording-count
    // badge every other dimension row carries ("out of context" — Ryan). Every
    // other dimension (Venues, Performers, Artists, Genres) is untouched.
    box.innerHTML = dim === 'collections'
      ? rows.map(c => {
          const open = _colOpenIds.has(c.id)
          return `
            <div class="nav-col-item">
              <div class="nav-record nav-record--flush" data-col-id="${c.id}">
                <span class="nav-caret nav-caret--pm nav-pm-box ${open ? 'open' : ''}"
                      >${icon('plus', 'pm-plus')}${icon('minus', 'pm-minus')}</span>
                <span class="truncate">${esc(c.name)}</span>
              </div>
              <div class="nav-col-recs" id="nav-col-recs-${c.id}" style="display:${open ? '' : 'none'}"></div>
            </div>`
        }).join('')
      : rows.map(r => `<div class="nav-record" data-dim="${dim}" data-id="${r.id}">
           <span class="truncate">${esc(r.name)}</span>${r.recording_count ? `<span class="nav-record-count">${r.recording_count}</span>` : ''}
         </div>`).join('')
    if (dim === 'collections') {
      box.querySelectorAll('.nav-record--flush[data-col-id]').forEach(el =>
        el.addEventListener('click', () => _toggleCollectionRow(parseInt(el.dataset.colId, 10))))
      _colOpenIds.forEach(id => { if (document.getElementById(`nav-col-recs-${id}`)) _renderCollectionRecs(id) })
    } else {
      box.querySelectorAll('.nav-record[data-id]').forEach(el =>
        el.addEventListener('click', () => { window.location.hash = `#/${target}/${el.dataset.id}` }))
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
    const row   = document.querySelector(`.nav-record--flush[data-col-id="${id}"]`)
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
  // supplies `image_id` for the small performer thumbnail Ryan asked for.
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
    const full = esc([r.performer, r.date, r.venue].filter(Boolean).join(' · '))
    const initials = String(r.performer || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    const avatar = r.image_id
      ? `<img class="nav-fav-avatar" src="${API.performers.imageUrl(r.image_id)}" alt="">`
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
  // Call after edits that can prune/create performers, venues, or artists.
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
    // and performers/artists used a window.prompt().
    if (dim === 'collections')     window.location.hash = '#/collection/new'
    else if (dim === 'venues')     window.location.hash = '#/venue/new'
    else if (dim === 'genres')     window.location.hash = '#/genres'
    else if (dim === 'performers') window.location.hash = '#/performer/new'
    else if (dim === 'artists')    window.location.hash = '#/artist/new'
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
          ${icon('plus', 'lib-join-ic')}Join a library
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
             <span>Join a library…</span>
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
  // map, say, /performer/12 onto a different database's ids.
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
        <div class="modal-header"><h3 id="join-title">Join a library</h3></div>
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
        return fail('That looks like just the code. The invite needs the address too — "https://their-library#CODE".')
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

  async function renderSidebar() {
    const nav = document.getElementById('sidebar-nav')
    if (!nav) return
    _dimCache.venues = _dimCache.performers =
      _dimCache.artists = _dimCache.collections = _dimCache.genres = null

    // A shared library offers a deliberately narrower sidebar. This is not
    // squeamishness about peer mode — it is that api/share.py has no LIST
    // endpoint for venues, performers or artists, by design: a peer reaches
    // those pages FROM a recording they were granted, never by browsing an
    // index of everything the owner holds. Rendering sections that could only
    // ever be empty would advertise a door that isn't there.
    //
    // Collections and Genres do have peer-facing lists (both scoped to the
    // visible set), so both stay. Add Recordings and Sharing are local
    // operations on my own library and are meaningless here.
    // A shared library gets Library + Collections and nothing else (Ryan,
    // 2026-08-08). The dimension indexes are dropped entirely rather than
    // moved: a peer reaches a performer, venue or genre page FROM a recording
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
    // independently, while `.nav-dims-foot` (Venues/Performers/Artists/Genres
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
    // endpoints now exist (venues, artists, favorites, search, collections),
    // so the reasoning has expired rather than been overruled.
    //
    // Nothing here branches on `remote` except the header LABEL. It does not
    // need to: `canEditLibrary()` is false in a remote library, so Add
    // Recordings and every "+" drop out on their own. A second template is how
    // the two drift apart — which is exactly what happened last time.
    const shelfTitle = remote
      ? (active ? active.display_name : 'Shared Library')
      : 'My Library'
    nav.innerHTML = `
      <div class="nav-scroll">
        ${canEditLibrary() ? `<a class="nav-add-btn" data-nav="ingest" href="#/ingest"><span class="nav-add-plus">${icon('plus')}</span> Add Recordings</a>` : ''}
        <div class="nav-item nav-top nav-shelf-head nav-shelf-head--static truncate">${esc(shelfTitle)}</div>
        <a class="nav-item" data-nav="library" href="#/">${icon('library', 'nav-ic')}Browse</a>
        <a class="nav-item" data-nav="search" href="#/search">${icon('search', 'nav-ic')}Search</a>
        <a class="nav-item" data-nav="recent" href="#/recent">${icon('clock', 'nav-ic')}Recently Added</a>
        <div class="nav-item nav-top nav-shelf-head nav-shelf-head--static nav-shelf-head--spaced">Collections</div>
        <div class="nav-records" id="nav-records-collections"></div>
        <div class="nav-favorites" id="nav-favorites-flat"></div>
      </div>
      <div class="nav-dims-foot">
        ${_dimSection('venues', icon('map-pin'), 'Venues')}
        ${_dimSection('performers', icon('users'), 'Performers')}
        ${_dimSection('artists', icon('user'), 'Artists')}
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
        const dim = el.closest('.nav-dim').dataset.dim
        if (el.dataset.act === 'refresh') _refreshDim(dim)
        else createInDim(dim)
      })
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
  }

  // Back-compat alias — call sites still say loadArtistList().
  const loadArtistList = renderSidebar

  // ── Shared compact recording row (one line, all show info) ───────────────────
  function flatRowHtml(r, showPerformer) {
    const date    = fmtDate(r.start_year, r.start_month, r.start_day)
    const loc     = fmtLocation(r.city, r.state, r.country)
    const quality = r.quality || ''
    const runtime = fmtRuntime(r.duration_sec)
    const inc     = r.is_complete ? '' : '<span class="rec-inc" title="Incomplete recording">inc</span>'
    return `
      <div class="rec-row rec-row--flat ${showPerformer ? 'with-performer' : ''}" data-rec-id="${r.id}">
        ${showPerformer ? `<span class="rec-performer-cell truncate">${esc(r.performer || '')}</span>` : ''}
        <span class="rec-date truncate">${esc(date)}</span>
        <span class="rec-venue truncate">${esc(r.venue || '(unknown venue)')}</span>
        <span class="rec-location truncate">${esc(loc)}</span>
        <span>${sourceBadge(r.source)}</span>
        <span class="quality ${qualityClass(quality)}">${esc(quality)}</span>
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
  function recTableHeadHtml(showPerformer) {
    return `
      <div class="rec-table-head ${showPerformer ? 'with-performer' : ''}">
        ${showPerformer ? '<span></span>' : ''}
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
  function wireDateAddedSort(mountEl, rows, showPerformer) {
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
      mountEl.innerHTML = sorted.map(r => flatRowHtml(r, showPerformer)).join('')
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
  // Hero + tab strip + panes, used by Performer, Venue, Artist (person), Genre
  // and Collection (Ryan, 2026-08-07). Extracted rather than copied five times:
  // four copies is exactly the situation that produced today's three-copy
  // analysis refactor and the .rec-row class collision, and a spacing or
  // navigation fix should not need applying five times.
  //
  // NAMING: the CSS keeps its `.pp-*` prefix. It reads as "performer page" and
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
  // mode (found 2026-08-23 via the debug drawer, on the Artist page:
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
    // passes `actions` passes an admin verb there (Delete performer / venue /
    // genre / collection / artist, + Add peer). Enforced in the shell rather
    // than at each caller so a new entity page cannot forget.
    // `actionsPlayback` renders in BOTH modes, for a verb that is content
    // rather than editing. "+ New collection" moved here 2026-08-22 (Ryan) —
    // making a collection is something a listener does too; deleting one
    // stays admin-only via `actions`.
    const shellEditable = canEditLibrary()
    const titleEditable = opts.titleEditable && shellEditable
    const heroActions   = (shellEditable ? (opts.actions || '') : '') + (opts.actionsPlayback || '')
    return `
      <div class="performer-page${opts.pageClass ? ' ' + opts.pageClass : ''}">

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
  // The Photos tab for any entity with images — Performer and Venue today
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
  //                                  — optional extra tile (Performer's
  //                                    Wikimedia lookup); Venue has none
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
        </div>
        <input type="file" data-input="1" multiple
               accept="image/png,image/jpeg,image/webp" style="display:none" />
        <div class="pp-fetch-msg" data-msg="1"></div>
        <div class="pp-gal-note">${
          !galEditable
            ? (images.length ? '' : 'No photos yet.')
            : images.length
              ? 'The primary photo is the one shown on this page and on cards.'
              : 'No photos yet. The primary photo appears on this page and on cards.'
        }</div>`

      const input = box.querySelector('[data-input]')
      const msg   = box.querySelector('[data-msg]')
      input.addEventListener('change', e => { upload(e.target.files); input.value = '' })

      // Delegated, and always re-fetching after a mutation rather than patching
      // the local array: the SERVER owns the one-primary rule and the
      // promote-on-delete rule, so mirroring them here would be a second
      // implementation waiting to disagree with the first.
      box.querySelector('[data-gal]').addEventListener('click', async e => {
        const btn = e.target.closest('.pp-ph-btn')
        if (!btn) return
        e.preventDefault()
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

      const drop = box.querySelector('[data-drop]')
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

  // ══ Shared "create entity" form ════════════════════════════════════════════
  //
  // One simple form per dimension (Ryan, 2026-08-07). The + buttons previously
  // did two different wrong things: Venues navigated to the ADMIN LIST — a
  // view-and-edit screen inconsistent with everything else and not a create
  // flow at all — while Performers and Artists used a bare window.prompt().
  //
  // Built on the entity shell so a create form looks like the page it will
  // become, and deliberately minimal: name plus whatever else is genuinely
  // required. Everything else is editable in place afterwards, which is the
  // established pattern for every object in this app.
  //
  // opts: { title, backHash, fields: [{id, label, placeholder, required}],
  //         onSave(values) -> hash to navigate to, invalidate: dimName }
  function renderCreateForm(opts) {
    setNavCurrent(opts.title)
    const fields = opts.fields
    setMainHTML(entityShellHtml({
      navBack: opts.backHash ? { label: opts.backLabel || 'Back', hash: opts.backHash } : null,
      title: esc(opts.title),
      tabs: [{
        id: 'form', label: 'New',
        html: `
          <div class="create-form">
            ${fields.map(f => `
              <div class="ingest-field">
                <label for="cf-${f.id}">${esc(f.label)}${f.required ? '' : ' <span class="cf-opt">(optional)</span>'}</label>
                ${f.multiline
                  ? `<textarea id="cf-${f.id}" placeholder="${esc(f.placeholder || '')}"></textarea>`
                  : `<input type="text" id="cf-${f.id}" placeholder="${esc(f.placeholder || '')}" autocomplete="off" />`}
              </div>`).join('')}
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
          <div class="peer-layout">
            <div class="peer-list" id="peer-list"></div>
            <div class="peer-detail" id="peer-detail"></div>
          </div>`,
      }],
    }))
    wireEntityShell(mainContent, null)

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
            : p.pending_invites ? 'Invited — not joined'
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
            : `<span class="peer-badge peer-badge--revoked" style="margin-left:auto">Revoked</span>`}
        </div>
        <div class="pp-desc pp-editable ${p.contact_note ? '' : 'pp-empty'}" id="peer-note" title="Click to edit">${
          p.contact_note ? esc(p.contact_note) : 'Add a note — who is this?'}</div>

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
              return `<div class="peer-empty">No Full Library collection in this database — run <span class="join-hash">scripts/migrate_add_system_collections.py</span>.</div>`
            }
            const on = granted.has(full.id)
            return `
            <div class="peer-grants">
              <label class="peer-grant peer-grant--system${on ? ' is-on' : ''}">
                <input type="checkbox" data-col-id="${full.id}" ${on ? 'checked' : ''} ${p.is_active ? '' : 'disabled'}>
                <span class="peer-grant-name truncate">Share my library</span>
                <span class="peer-grant-count">${full.recording_count}</span>
                <span class="peer-grant-note">They see everything on the shelf — Browse, Search, your collections and your favorites — but cannot change anything.</span>
              </label>
            </div>`
          })()}
        </div>

        <div class="pp-block">
          <h2 class="pp-block-title">Invite</h2>
          <div class="pp-block-hint">Generates a one-time code. It is shown once and stored only as a hash — if it's lost, mint a new one.</div>
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
        multiline: true, placeholder: 'Add a note — who is this?',
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
                <div class="peer-invite-warn">No public address configured, so this is the bare code. Set <code>SHARE_BASE_URL</code> and mint again to get a single paste-able string.</div>`}
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
                'joined your library is unaffected — this is not the same as ' +
                'revoking access.')) return
          btn.disabled = true
          try {
            await API.peers.deleteInvite(p.id, Number(btn.dataset.inviteId))
            peers = await API.peers.list()
            renderList(); renderDetail()
          } catch (e) { btn.disabled = false; alert('Failed: ' + e.message) }
        }))

      document.getElementById('peer-revoke')?.addEventListener('click', async () => {
        if (!confirm(`Revoke all access for "${p.name}"?\n\nThis kills every grant and every device token at once. It cannot be undone — you would need to invite them again.`)) return
        try {
          await API.peers.revoke(p.id)
          peers = await API.peers.list()
          renderList(); renderDetail()
        } catch (e) { alert('Failed: ' + e.message) }
      })

      try {
        const acts = await API.peers.activity(p.id)
        const box = document.getElementById('peer-activity')
        if (box) {
          box.innerHTML = acts.length
            ? `<div>${acts.slice(0, 12).map(a => `
                <div class="peer-act">
                  <span class="truncate">${esc([a.performer, a.date].filter(Boolean).join(' · ') || a.track_title || 'track')}</span>
                  <span class="peer-act-when">${esc(fmtDateAdded(a.occurred_at))}</span>
                </div>`).join('')}</div>`
            : `<div class="peer-empty">Nothing streamed yet.</div>`
        }
      } catch (_) { /* activity is nice-to-have, never blocks the page */ }
    }

    onAdminClick('peer-new', async () => {
      const name = prompt('Peer name — your own label for this person:')
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
    fields: [
      { id: 'name',    label: 'Venue name', required: true, placeholder: 'The Fillmore' },
      { id: 'city',    label: 'City',    placeholder: 'San Francisco' },
      { id: 'state',   label: 'State / Region', placeholder: 'CA' },
      { id: 'country', label: 'Country', placeholder: 'United States' },
    ],
    onSave: async v => `#/venue/${(await API.venues.create(v)).id}`,
  })

  const renderPerformerForm = () => renderCreateForm({
    title: 'New performer', backHash: '#/', backLabel: 'Library', invalidate: 'performers',
    fields: [
      // Name only. Everything else — genre, members, description — is edited in
      // place on the performer page, and a MusicBrainz lookup runs on create,
      // so asking for more here would mostly duplicate what it fetches.
      { id: 'name', label: 'Performer name', required: true, placeholder: 'The Meters' },
    ],
    onSave: async v => `#/performer/${(await API.performers.create(v)).id}`,
  })

  const renderArtistForm = () => renderCreateForm({
    title: 'New artist', backHash: '#/artists', backLabel: 'Artists', invalidate: 'artists',
    fields: [
      { id: 'name',      label: 'Artist name', required: true, placeholder: 'George Porter Jr.' },
      // "Last, First" — drives sidebar ordering via COALESCE(sort_name, name).
      { id: 'sort_name', label: 'Sort name', placeholder: 'Porter, George Jr.' },
    ],
    onSave: async v => `#/person/${(await API.artists.create(v)).id}`,
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

  // A recordings pane using the flat catalog table — the shape Venue, Artist,
  // Genre and Performer all want. `showPerformer` differs per page: a Performer
  // page needn't repeat the act's name on every row, a Venue page must.
  function recordingsPaneHtml(rows, { showPerformer = false, mountId = 'rec-table-entity', empty = 'No recordings yet' } = {}) {
    if (!rows.length) {
      return `<div class="empty-state" style="min-height:180px"><div class="empty-title">${esc(empty)}</div></div>`
    }
    return recTableHeadHtml(showPerformer)
         + `<div class="rec-table" id="${mountId}">${rows.map(r => flatRowHtml(r, showPerformer)).join('')}</div>`
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
    const colRows = c.recordings || []
    const descText = c.description && c.description.trim()
    const navBack = state.navBack

    // HANDBILL CARDS, no Browse/List toggle (Ryan, 2026-08-07). The toggle used
    // to put this page into the global dashboard — Recommended, Performers, On
    // This Day — instead of the collection's own recordings, which is the one
    // place the global-preference decision produced a plainly wrong result
    // (flagged as a judgment call on 08-02, now resolved by removing the
    // choice). A collection is a curated set; cards are the right register for
    // it and there is no second mode to pick.
    setMainHTML(entityShellHtml({
      navBack,
      title: esc(c.name),
      titleId: 'col-name',
      titleEditable: true,
      stats: [[colRows.length, colRows.length === 1 ? 'Recording' : 'Recordings']],
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="col-delete" title="Delete collection">Delete</button>`,
      tabs: [{
        id: 'recordings', label: 'Recordings',
        html: `
          <div class="pp-sec">Description</div>
          <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="col-desc" title="Click to edit">${descText ? esc(c.description) : 'Add a description\u2026'}</div>
          <div class="pp-sec" style="margin-top:24px">Recordings</div>
          ${colRows.length
            ? `<div class="lib-module-grid">${colRows.map(recCardHtml).join('')}</div>`
            : `<div class="empty-state" style="min-height:160px"><div class="empty-title">Empty collection</div><div class="empty-sub">Right-click a recording anywhere to add it here.</div></div>`}`,
      }],
    }))
    wireEntityShell(mainContent, navBack)


    const refreshSidebar = () => { _dimCache.collections = null; if (state.expandedDims.has('collections')) _renderDimRecords('collections') }
    async function saveField(patch) {
      try { await API.collections.update(id, patch); refreshSidebar() }
      catch (e) { alert('Save failed: ' + e.message) }
    }
    makeInlineEditable(document.getElementById('col-name'), {
      get: () => c.name,
      onSave: async v => { v = v.trim(); if (!v || v === c.name) return; c.name = v; await saveField({ name: v }) },
    })
    makeInlineEditable(document.getElementById('col-desc'), {
      multiline: true, placeholder: 'Add a description…',
      get: () => c.description || '',
      onSave: async v => { v = v.trim(); c.description = v; await saveField({ description: v || null }) },
    })

    onAdminClick('col-delete', async () => {
      if (!confirm(`Delete collection "${c.name}"? Recordings are not affected.`)) return
      try { await API.collections.remove(id); refreshSidebar(); window.location.hash = '#/collections' }
      catch (e) { alert(e.message) }
    })
  }

  // Artist (person) page — editable info + Performer associations + appearances,
  // grouped by Performer alphabetically. Mirrors the Performer page.
  async function renderPersonView(id) {
    setActiveNav('artists'); setActiveArtist(null); setLoading()
    let a
    try { a = await API.artists.get(id) }
    catch (e) {
      invalidateDims('artists')   // heal the sidebar if this person was removed
      setMainHTML(`<div class="empty-state"><div class="empty-title">This artist no longer exists</div></div>`)
      return
    }
    setNavCurrent(a.name)
    // Performers the person is a member of (already sorted by the API).
    let performers = (a.performers || []).map(p => ({ id: p.id, name: p.name }))

    // Fetch each act's recordings so we can group appearances by performer.
    let perfRecs = []
    try {
      perfRecs = await Promise.all(performers.map(p =>
        API.performers.recordings(p.id).then(rs => ({ performer: p, performances: rs.filter(x => (x.recordings || []).length) }))))
    } catch (_) {}

    const totalRecordings = perfRecs.reduce((n, g) => n + g.performances.reduce((m, p) => m + p.recordings.length, 0), 0)

    // One <section> per performer (alpha), each with a header + flat recording rows.
    const groupsHtml = perfRecs.map(g => {
      const ordered = g.performances.slice().sort((x, y) =>
        (x.start_year || 0) - (y.start_year || 0) ||
        (x.start_month || 0) - (y.start_month || 0) ||
        (x.start_day || 0) - (y.start_day || 0))
      const rows = ordered.map(p =>
        p.recordings.map(r => flatRowHtml({
          id: r.id, performer: p.performer_name,
          start_year: p.start_year, start_month: p.start_month, start_day: p.start_day,
          venue: p.venue_name, city: p.city, state: p.state, country: p.country,
          source: r.source, quality: r.quality,
          is_complete: r.is_complete,
          track_count: r.track_count, duration_sec: r.duration_sec,
        }, false)).join('')).join('')
      if (!rows) return ''
      return `<div class="pp-group">
        <div class="pp-group-head"><a href="#/performer/${g.performer.id}">${esc(g.performer.name)}</a></div>
        <div class="rec-table">${rows}</div>
      </div>`
    }).join('')

    // Guest / sit-in appearances — performance_personnel rows on acts this
    // person isn't formally a Membership of (2026-07-18 Per-Show Personnel,
    // ripple item 3: "Béla's page would finally surface his All-Stars
    // sit-ins"). Grouped by performer like the section above, but kept
    // visually separate and tagged "guest" since it's not the same thing as
    // full membership — this is a different act's recording that happens to
    // include this person for one show.
    const guestAppearances = a.guest_appearances || []
    const guestByPerformer = {}
    guestAppearances.forEach(g => {
      const key = g.performer_id
      if (!guestByPerformer[key]) guestByPerformer[key] = { performer_id: g.performer_id, performer_name: g.performer_name, appearances: [] }
      guestByPerformer[key].appearances.push(g)
    })
    const totalGuestRecordings = guestAppearances.reduce((n, g) => n + (g.recordings || []).length, 0)

    const guestGroupsHtml = Object.values(guestByPerformer).map(g => {
      const ordered = g.appearances.slice().sort((x, y) =>
        (x.start_year || 0) - (y.start_year || 0) ||
        (x.start_month || 0) - (y.start_month || 0) ||
        (x.start_day || 0) - (y.start_day || 0))
      const rows = ordered.map(ap =>
        (ap.recordings || []).map(r => flatRowHtml({
          id: r.id, performer: g.performer_name,
          start_year: ap.start_year, start_month: ap.start_month, start_day: ap.start_day,
          venue: ap.venue_name, city: ap.city, state: ap.state, country: ap.country,
          source: r.source, quality: r.quality,
          is_complete: r.is_complete,
          track_count: r.track_count, duration_sec: r.duration_sec,
        }, false)).join('')).join('')
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
        <div class="pp-group-head"><a href="#/performer/${g.performer_id}">${esc(g.performer_name)}</a>${allGuest ? ' <span class="pp-guest-tag">guest</span>' : ''}</div>
        <div class="rec-table">${rows}</div>
      </div>`
    }).join('')

    const descText = a.bio && a.bio.trim()
    const navBack = state.navBack

    setMainHTML(entityShellHtml({
      navBack,
      portrait: heroPortraitHtml(a.name, null),
      title: esc(a.name),
      titleId: 'pn-name',
      titleEditable: true,
      chips: `<span class="pp-hero-fact">${performers.length} performer${performers.length !== 1 ? 's' : ''}</span>`,
      stats: [
        [totalRecordings, totalRecordings === 1 ? 'Recording' : 'Recordings'],
        ...(totalGuestRecordings ? [[totalGuestRecordings, 'Guest']] : []),
      ],
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="pn-delete" title="Delete artist">Delete</button>`,
      // No Photos tab: photos are scoped to Performer level (Ryan, 2026-08-07).
      // A person's likeness would need its own table and its own Commons path,
      // and the card surfaces all key off the act, not the individual.
      tabs: [
        { id: 'overview', label: 'Overview', active: true, html: `
            <div class="pp-sec">Performers</div>
            <div class="pp-artists" id="pn-performers"></div>

            <div class="pp-sec">Bio</div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="pn-desc" title="Click to edit">${descText ? esc(a.bio) : 'Add a bio\u2026'}</div>` },
        { id: 'recordings', label: 'Recordings', count: totalRecordings + totalGuestRecordings, html: `
            ${groupsHtml || (guestGroupsHtml ? '' : '<div class="empty-state" style="min-height:160px"><div class="empty-title">No appearances yet</div></div>')}
            ${guestGroupsHtml ? `<div class="pp-sec" style="margin-top:24px">Guest appearances</div>${guestGroupsHtml}` : ''}` },
      ],
    }))
    wireEntityShell(mainContent, navBack)

    wireRecordingRows(mainContent)

    const refreshSidebar = () => invalidateDims('artists', 'performers')
    async function saveField(patch) {
      try { await API.artists.update(id, patch); refreshSidebar() }
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

    // ── Editable Performer associations ─────────────────────────────────────
    function renderPerformers() {
      const box = document.getElementById('pn-performers')
      box.innerHTML =
        performers.map((p, i) => `<span class="member-chip">${esc(p.name)} <span class="member-chip-x" data-i="${i}" title="Remove from this act">${icon('x')}</span></span>`).join('') +
        `<span class="artist-picker-wrap pp-add-wrap">
           <input type="text" class="member-input pp-add-input" autocomplete="off" placeholder="Add to a performer…" />
           <div class="artist-dropdown" id="pn-add-dd" style="display:none"></div>
         </span>`
      box.querySelectorAll('.member-chip-x').forEach(x =>
        x.addEventListener('click', async () => {
          const p = performers[parseInt(x.dataset.i)]
          try { await API.artists.removePerformer(id, p.id); invalidateDims('performers') } catch (e) { alert(e.message); return }
          renderPersonView(id)   // reload so the grouped appearances update
        }))
      const input = box.querySelector('.pp-add-input')
      wirePickerDropdown(input, document.getElementById('pn-add-dd'), API.performers.search,
        async ({ id: pid, name }) => {
          try { await API.artists.addPerformer(id, pid ? { performer_id: pid } : { performer_name: name }); invalidateDims('performers') }
          catch (e) { alert(e.message); return }
          renderPersonView(id)
        }, 'Create new performer')
    }
    renderPerformers()

    onAdminClick('pn-delete', async () => {
      if (!confirm(`Delete artist "${a.name}"? This can't be undone.`)) return
      try { await API.artists.remove(id); refreshSidebar(); window.location.hash = '#/' }
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

  // Defensive strip of citation markup in stored AI text. The real fix is
  // server-side in performer_research.py (_clean_prose), so what gets SAVED is
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

  // Genre colour with the agreed fallback. 70 of 164 performers have no genre,
  // so NULL is the common case, not an error case — those cards get a neutral
  // warm grey and read as quiet rather than broken (Ryan, 2026-08-07). The
  // fallback lives HERE and nowhere else: the serializer deliberately sends
  // null rather than substituting a default, so there is exactly one place
  // that decides what "no genre" looks like.
  function genreColor(r) {
    return (r && r.genre_color) ? r.genre_color : 'var(--t2)'
  }

  // Performer avatar for the cards — photo when there is one, INITIALS when
  // there isn't (Ryan, 2026-08-07). It previously rendered nothing without a
  // photo, which made cards for photographed and un-photographed acts two
  // different shapes. Since most acts have no photo, the initials disc IS the
  // normal appearance, and using the same one the Performer page hero uses
  // makes every card and page agree.
  function perfPhotoHtml(r, cls) {
    if (!r) return ''
    if (r.image_id) {
      return `<img class="${cls}" src="${API.performers.imageUrl(r.image_id)}"
                   alt="" loading="lazy">`
    }
    const initials = String(r.performer || '?').split(/\s+/).filter(Boolean)
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
  // Colour comes from the performer's GENRE, not the source — source is a
  // technical attribute and makes a poor identity, whereas genre groups the
  // library the way a listener actually browses it.
  function recCardHtml(r) {
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
        <div class="rec-card-performer">${esc(r.performer || '')}</div>
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
  function recentRowCardHtml(r) {
    const date = handbillDate(r.start_year, r.start_month, r.start_day)
    const loc  = fmtLocation(r.city, r.state, r.country)
    const foot = recCardFootParts(r)
    const photo = perfPhotoHtml(r, 'rec-rowcard-photo')
    const venue = [r.venue || '(unknown venue)', loc].filter(Boolean).join(' · ')
    return `
      <a class="rec-rowcard" href="#/recording/${r.id}" data-rec-id="${r.id}"
         style="--genre-fg:${esc(genreColor(r))}">
        <div class="rec-rowcard-spine"></div>
        <div class="rec-rowcard-avatar">${photo}</div>
        <div class="rec-rowcard-date">${esc(date || '—')}</div>
        <div class="rec-rowcard-main">
          <div class="rec-rowcard-performer truncate">${esc(r.performer || '')}</div>
          <div class="rec-rowcard-venue truncate">${esc(venue)}</div>
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

  // Shuffle. Replaces only the TILES, not the whole section — re-rendering the
  // heading and its button meant re-wiring the button every time, which is how
  // the old version worked and one listener leak away from not working.
  async function _refreshRecommendedModule() {
    const grid = document.getElementById('tops-grid')
    if (!grid) return
    _libRecommendedReroll++
    let recs = []
    try { recs = await API.recordings.recommended(BROWSE_TOP_N, _libRecommendedReroll) } catch (_) {}
    if (!recs.length) { document.getElementById('lib-mod-tops')?.remove(); return }
    grid.innerHTML = recs.map(_topTileHtml).join('')
    grid.scrollLeft = 0
    _updateTopsNav()
  }

  // Top Shelf left/right nav (2026-08-24, fixed same day — see below). The
  // strip scrolls natively (mouse wheel, trackpad, touch); these two buttons
  // are an explicit affordance for it and the reason cards no longer wrap to
  // a second row at narrow widths — see .tops-grid in main.css.
  //
  // FIX 1: the right button used to start with .hidden and only clear it once
  // the strip actually overflowed — on a wide enough window all 6 tiles fit
  // with room to spare, so the button never appeared at all ("I am not
  // seeing the left-right icons," Ryan). The spec only ever conditioned the
  // LEFT button on scroll position ("only if the user has scrolled to the
  // right already"); the right one was always supposed to be there. Right is
  // now unconditional — rendered without .hidden and never toggled — a click
  // with nothing left to scroll is just a harmless no-op. Left still starts
  // hidden and only appears once scrolled.
  //
  // FIX 2 (2026-08-24): nav used to scroll by 80% of the visible viewport —
  // a "paging" jump that could carry 3+ tiles at once depending on how many
  // fit on screen. Ryan's spec is a one-at-a-time shift: click right, the
  // leftmost tile slides fully out of view on the left and exactly one
  // previously-offscreen tile comes into view on the right. The scroll
  // distance now equals exactly one tile's width plus the grid's gap,
  // measured live off the DOM (_topsStep) rather than hardcoded, so it stays
  // correct if the tile width or gap ever changes in CSS.
  //
  // Wired fresh on every renderBrowseModules() call; a Shuffle re-uses the
  // same grid and button elements, so it only needs to recompute the left
  // button's visibility, not rewire anything.
  function _updateTopsNav() {
    const grid = document.getElementById('tops-grid')
    const navL = document.getElementById('tops-nav-l')
    if (!grid || !navL) return
    navL.classList.toggle('hidden', grid.scrollLeft <= 2)
  }
  function _topsStep(grid) {
    const tile = grid.querySelector('.top-tile')
    if (!tile) return grid.clientWidth
    const gap = parseFloat(getComputedStyle(grid).columnGap) || 0
    return tile.getBoundingClientRect().width + gap
  }
  function _wireTopsNav() {
    const grid = document.getElementById('tops-grid')
    const navL = document.getElementById('tops-nav-l')
    const navR = document.getElementById('tops-nav-r')
    if (!grid || !navL || !navR) return
    navL.addEventListener('click', () => grid.scrollBy({ left: -_topsStep(grid), behavior: 'smooth' }))
    navR.addEventListener('click', () => grid.scrollBy({ left: _topsStep(grid), behavior: 'smooth' }))
    grid.addEventListener('scroll', _updateTopsNav)
    _updateTopsNav()
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
  //   Performers grid    → gone entirely ("unusable"). Collectors expect a
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
  // api/performers.py — was ~2000+ per-request DB round trips, now ~7); this
  // just keeps the DOM small on top of that.
  const BROWSE_LIST_INITIAL = 16   // first paint
  const BROWSE_LIST_PAGE    = 16   // each subsequent reveal, scrolled into view

  let _browseRows = []
  let _browseFilters = { quality: 'any', source: 'any', genre: 'any' }
  let _browseSort = 'az'
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
    const byDate = (a, b) =>
      (a.start_year || 0) - (b.start_year || 0) ||
      (a.start_month || 0) - (b.start_month || 0) ||
      (a.start_day || 0) - (b.start_day || 0)
    if (_browseSort === 'az')      out = out.slice().sort((a, b) =>
      (a.performer || '').localeCompare(b.performer || '') || byDate(a, b))
    if (_browseSort === 'newest')  out = out.slice().sort((a, b) => byDate(b, a))
    if (_browseSort === 'oldest')  out = out.slice().sort(byDate)
    if (_browseSort === 'added')   out = out.slice().sort((a, b) =>
      String(b.created_at || '').localeCompare(String(a.created_at || '')))
    return out
  }

  function _browseRowHtml(r) {
    const initials = String(r.performer || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    const loc = fmtLocation(r.city, r.state, r.country)
    const c = r.genre_color || 'var(--t2)'
    return `
      <a class="brow" href="#/recording/${r.id}" data-rec-id="${r.id}" style="--genre-fg:${esc(c)}">
        <span class="brow-spine"></span>
        <span class="brow-av">${esc(initials)}</span>
        <span class="brow-date">${esc(handbillDate(r.start_year, r.start_month, r.start_day) || '—')}</span>
        <span class="brow-perf">${esc(r.performer || '')}</span>
        <span class="brow-venue">${esc([r.venue, loc].filter(Boolean).join(', ') || '(unknown venue)')}</span>
        <span class="brow-tail">
          ${r.source ? `<span class="brow-src">${esc(r.source)}</span>` : ''}
          ${r.quality ? `<span class="brow-grade">${esc(r.quality)}</span>` : ''}
        </span>
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
           <div class="empty-sub">Widen one of them — quality is the narrowest, since only
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
    _browseSort = 'az'

    const [tops, collections] = await Promise.all([
      API.recordings.recommended(BROWSE_TOP_N, 0).catch(() => []),
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
    const topsHtml = tops.length ? `
      <section class="lib-module" id="lib-mod-tops">
        <div class="lib-module-head">
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

    mountEl.innerHTML = `
      ${topsHtml}

      <section class="lib-module" id="lib-mod-all">
        <div class="browse-bar">
          <div class="browse-sorts" id="browse-sorts">
            <button class="sortb on" data-sort="az">A–Z</button>
            <button class="sortb" data-sort="newest">Newest</button>
            <button class="sortb" data-sort="oldest">Oldest</button>
            <button class="sortb" data-sort="added">Recently added</button>
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

    mountEl.querySelector('#browse-shuffle')?.addEventListener('click', _refreshRecommendedModule)
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
  // that's supported, not just a performer headshot crop. Performer photo
  // when there is one (312 of 580 shows have one), a genre-colour field with
  // initials when there is not — the no-photo case is the NORMAL case for
  // 46% of the library, so it has to look deliberate rather than like a
  // failed image, hence the initials rather than a blank/broken square.
  //
  // Text (performer, venue, date · grade) now lives in .top-overlay, a
  // gradient scrim over the BOTTOM of the art rather than a separate white
  // panel below it — the image is the whole tile now, and the scrim exists
  // purely for legibility (dark gradient works over both a photo and a
  // genre-colour field, light or saturated).
  function _topTileHtml(r) {
    const initials = String(r.performer || '?').split(/\s+/).filter(Boolean).slice(0, 2)
      .map(w => w[0]).join('').toUpperCase()
    const c = r.genre_color || 'var(--bg-4)'
    const art = r.image_id
      ? `<img class="top-img" src="${API.performers.imageUrl(r.image_id)}" alt="" loading="lazy">`
      : `<span class="top-initials">${esc(initials)}</span>`
    return `
      <a class="top-tile" href="#/recording/${r.id}" style="--genre-fg:${esc(c)}">
        <span class="top-art">${art}</span>
        <span class="top-overlay">
          <span class="top-perf">${esc(r.performer || '')}</span>
          ${r.venue ? `<span class="top-venue">${esc(r.venue)}</span>` : ''}
          <span class="top-meta">${esc(handbillDate(r.start_year, r.start_month, r.start_day) || '')}${
            r.quality ? ` · ${esc(r.quality)}` : ''}</span>
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

  async function renderLibraryView() {
    setActiveNav('library')
    setActiveArtist(null)
    state.selectedArtist = null
    setNavCurrent('Library')
    setLoading()

    let allArtists
    try {
      allArtists = await API.performers.allRecordings()
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Failed to load library</div></div>`)
      return
    }

    if (!allArtists.length) {
      setMainHTML(`
        <div class="empty-state">
            <div class="empty-title">No recordings yet</div>
          <div class="empty-sub">Add a recording to get started</div>
        </div>`)
      return
    }

    // The "Library" H1 and the "580 recordings · 184 performers" subtitle were
    // gone for a while (Ryan, 2026-08-23: "it's not important enough") because
    // the header was just the Browse/List toggle. The toggle is retired
    // (2026-08-24), so the page gets an H1 back — "Browse My Library".
    const headerHtml = `
      <div class="artist-header">
        <div class="artist-header-row">
          <h1>Browse My Library</h1>
        </div>
      </div>`

    // Flatten to one row per recording — performer + date + venue on every line,
    // already ordered by performer (backend) then chronologically old→new.
    // genre/genre_color ride on the PERFORMER (one genre per act) and colour
    // the spine as well as driving Browse's genre filter.
    const rows = allArtists.flatMap(artist =>
      artist.performances.flatMap(p =>
        p.recordings.map(r => ({
          id: r.id, performer: artist.performer_name,
          genre: artist.genre, genre_color: artist.genre_color,
          start_year: p.start_year, start_month: p.start_month, start_day: p.start_day,
          venue: p.venue_name, city: p.city, state: p.state, country: p.country,
          source: r.source, quality: r.quality,
          is_complete: r.is_complete,
          track_count: r.track_count, duration_sec: r.duration_sec, created_at: r.created_at,
        }))
      )
    )
    setMainHTML(`${headerHtml}<div class="lib-modules" id="lib-modules-mount"></div>`)
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
      <div class="rec-rowcard-list" id="recent-rowcards"></div>
      <div class="recent-more" id="recent-more"></div>`)

    // Endless scroll (Ryan, 2026-08-23) — was a hardcoded 50 with a
    // "Show all" button. A sentinel below the list fetches the next page
    // when it comes into view; the observer disconnects when the server
    // returns a short page, which is how we know we have reached the end.
    const listEl = document.getElementById('recent-rowcards')
    const moreEl = document.getElementById('recent-more')
    listEl.innerHTML = rows.map(recentRowCardHtml).join('')

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
      listEl.insertAdjacentHTML('beforeend', next.map(recentRowCardHtml).join(''))
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

  /** Performer page — editable info + member Artists + recording catalog. */
  async function renderArtistView(performerId) {
    setActiveNav('library')
    setActiveArtist(performerId)
    setLoading()

    let performer, performances
    try {
      [performer, performances] = await Promise.all([
        API.performers.get(performerId),
        API.performers.recordings(performerId),
      ])
    } catch (e) {
      // Likely a performer that was pruned after reassignment — heal the stale
      // sidebar so the phantom entry disappears.
      invalidateDims('performers')
      setMainHTML(`<div class="empty-state"><div class="empty-title">This performer no longer exists</div><div class="empty-sub">It may have been removed after its recordings were reassigned.</div></div>`)
      return
    }
    setNavCurrent(performer.name)
    // Whatever page brought us here (2026-07-23 generic mechanism — see
    // state.navBack) — shown as a "← Back" breadcrumb below, replacing the
    // old one-shot recFrom that only covered arriving via a Recording's ↗
    // nav-link icon. Read directly, not consumed/cleared: route() already
    // refreshes it on every real navigation, and a same-page reload (e.g.
    // after an inline edit) never touches it.
    const navBack = state.navBack

    state.selectedArtist = performer
    // Local, mutable copy of the roster — edited in place, persisted on each change.
    // Each member also carries `.stints` (date-bounded tenures; usually one
    // unbounded row = "always a member") — see the stint editor below.
    let members = (performer.members || []).map(m => ({ id: m.id, name: m.name, stints: m.stints || [] }))
    let defaultPersonnelMode = performer.default_personnel_mode || 'inherit'
    let expandedMemberId = null   // which member's stint editor drawer is open, if any

    const withRecs = performances.filter(p => (p.recordings || []).length > 0)
    const totalRecordings = withRecs.reduce((n, p) => n + p.recordings.length, 0)

    // Flat one row per recording, oldest→newest. No year headers (one performer).
    const ordered = withRecs.slice().sort((a, b) =>
      (a.start_year || 0) - (b.start_year || 0) ||
      (a.start_month || 0) - (b.start_month || 0) ||
      (a.start_day || 0) - (b.start_day || 0))
    const perfRows = ordered.flatMap(p =>
      p.recordings.map(r => ({
        id: r.id, performer: p.performer_name,
        start_year: p.start_year, start_month: p.start_month, start_day: p.start_day,
        venue: p.venue_name, city: p.city, state: p.state, country: p.country,
        source: r.source, quality: r.quality,
        is_complete: r.is_complete,
        track_count: r.track_count, duration_sec: r.duration_sec, created_at: r.created_at,
      }))
    )
    const rowsHtml = perfRows.map(r => flatRowHtml(r, false)).join('')

    const descText = performer.bio && performer.bio.trim()

    const mbf = performer.musicbrainz || {}

    // ── Hero stat: recordings only (Ryan, 2026-08-07) ────────────────────────
    // Venue count and total runtime were cut as uninteresting. Span was cut as
    // potentially INCONSISTENT with our own data — it was derived from whatever
    // dates happen to be filled in, so an act with one undated show would
    // advertise a range that contradicts the recordings listed right below it.
    // The derivations for all three are gone rather than left computed-unused.
    const stats = [
      [totalRecordings, totalRecordings === 1 ? 'Recording' : 'Recordings'],
    ]

    // MusicBrainz one-liner: type · origin · active years, whichever exist.
    const mbBits = [
      mbf.type ? `<b>${esc(mbf.type)}</b>` : '',
      mbf.area ? esc(mbf.area) : '',
      mbf.begin ? `active <b>${esc(mbf.begin)}${mbf.end ? '–' + esc(mbf.end) : '–present'}</b>` : '',
    ].filter(Boolean).join(' · ')

    const photoCount = (performer.images || []).length

    setMainHTML(entityShellHtml({
      navBack,
      portrait: '<div id="pp-hero-portrait"></div>',
      title: esc(performer.name),
      titleId: 'pp-name',
      titleEditable: true,
      // ONE genre element, and it is the editable one. A static colour pill
      // alongside it (as first built) showed the same value twice and only one
      // of them responded to a click.
      chips: `<span class="pp-editable pp-genre-field" id="pp-genre"
                    style="--genre-fg:${esc((performer.genre && performer.genre.color) || 'var(--t2)')}"
                    title="Click to edit"></span>`
           + (mbBits ? `<span class="pp-hero-fact">${mbBits}</span>` : ''),
      stats,
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="pp-delete" title="Delete performer">Delete</button>`,
      tabs: [
        { id: 'overview',   label: 'Overview', active: true, html: `
            <!-- Members first (Ryan, 2026-08-07): who the act IS comes before
                 prose about it, and Description is empty on most performers so
                 leading with it opened the page on a placeholder. -->
            <div class="pp-sec">Members</div>
            <div class="pp-artists" id="pp-artists"></div>
            <div class="pp-stint-editor" id="pp-stint-editor" style="display:none"></div>

            <!-- AI Assist sits ON the Description header: it's an enrichment
                 action for this one field, not a research panel, so it belongs
                 where its output lands. Clicking it OVERWRITES the description
                 — a deliberate exception to "AI suggests, human approves",
                 made because a biography is low-stakes and freely re-editable. -->
            <div class="pp-sec-row">
              <div class="pp-sec">Description</div>
              <button type="button" class="btn btn-ghost btn-xs iq-ai-btn" id="pp-dossier-run">${icon('sparkles')} AI Assist</button>
              <span class="pp-sec-msg" id="pp-dossier-msg"></span>
            </div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="pp-desc" title="Click to edit">${descText ? esc(performer.bio) : 'Add a description\u2026'}</div>

            <div class="pp-block">
              <h2 class="pp-block-title">MusicBrainz</h2>
              <div class="pp-block-hint">Links this act to its MusicBrainz entry, so future ingests know where to look for information about it.</div>
              <div id="pp-mb"></div>
            </div>

            <!-- Reframed from "Resources" to sources the ENRICHMENT JOBS should
                 trust. Flux already knows to consult Wikipedia and setlist.fm;
                 what it can't know is the act-specific archive a collector
                 knows about. Ordered last because it's what you fill in AFTER
                 seeing what the automated passes missed. -->
            <div class="pp-block">
              <h2 class="pp-block-title">Trusted sources</h2>
              <div class="pp-block-hint">Sites worth trusting for this act specifically \u2014 a fan-maintained show database, an archivist's site. We already check the obvious ones, so add what we wouldn't know to look for. These are treated as sources of truth in future research and ingest jobs.</div>
              <div class="pp-resources" id="pp-resources"></div>
            </div>` },
        { id: 'recordings', label: 'Recordings', count: totalRecordings,
          html: recordingsPaneHtml(perfRows, { mountId: 'rec-table-performer' }) },
        { id: 'photos',     label: 'Photos', count: photoCount || null,
          html: '<div id="pp-photos"></div>' },
      ],
    }))
    wireEntityShell(mainContent, navBack)

    wireRecordingRows(mainContent)
    if (perfRows.length) wireDateAddedSort(document.getElementById('rec-table-performer'), perfRows, false)

    const refreshSidebar = () => { _dimCache.performers = null; if (state.expandedDims.has('performers')) _renderDimRecords('performers') }

    // ── Inline-editable name / description ──────────────────────────────────
    async function saveField(patch) {
      try { await API.performers.update(performerId, patch); refreshSidebar() }
      catch (e) { alert('Save failed: ' + e.message) }
    }
    makeInlineEditable(document.getElementById('pp-name'), {
      get: () => performer.name,
      onSave: async v => {
        v = v.trim(); if (!v || v === performer.name) return
        performer.name = v; state.selectedArtist.name = v
        await saveField({ name: v })
      },
    })
    makeInlineEditable(document.getElementById('pp-desc'), {
      multiline: true, placeholder: 'Add a description…',
      get: () => performer.bio || '',
      onSave: async v => {
        v = v.trim(); performer.bio = v
        await saveField({ bio: v || null })
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
      const hasGenre = !!performer.genre
      genreEl.innerHTML = hasGenre
        ? `<span class="genre-pill">${esc(performer.genre.name)}</span>`
        : `<span class="pp-empty">Add genre…</span>`
    }
    showGenre()
    genreEl?.addEventListener('click', () => {
      if (genreEl.querySelector('input')) return
      genreEl.innerHTML = `<span class="artist-picker-wrap" style="display:inline-block; min-width:160px">
        <input type="text" class="pp-inline-input" id="pp-genre-input" value="${esc(performer.genre?.name || '')}" autocomplete="off" />
        <div class="artist-dropdown" id="pp-genre-dd" style="display:none"></div></span>`
      const input = document.getElementById('pp-genre-input')
      const dd    = document.getElementById('pp-genre-dd')
      input.focus(); input.select()
      let committed = false
      const commitGenre = async ({ id, name }) => {
        if (committed) return; committed = true
        try {
          await API.performers.update(performerId, { genre_id: id || null })
          performer.genre = id ? { id, name } : null
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

    // ── Editable Artists (members) + per-person stint dates ──────────────────
    // A member usually has one unbounded stint ("always a member" — zero UI
    // tax, matches every pre-2026-07-18 row). Click a chip's name to expand
    // an inline drawer for real tenure dates (era lineups, second stints like
    // Mickey Hart) — see Per-Show Personnel design doc §7.6.
    async function persistMembers() { await saveField({ members: members.map(m => m.name) }) }

    async function refreshRoster() {
      // Stint mutations happen against Membership rows directly (not via the
      // plain-name-list sync), so re-fetch rather than hand-patch local state.
      const fresh = await API.performers.get(performerId)
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

    function renderArtists() {
      const box = document.getElementById('pp-artists')
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
          renderArtists(); renderStintEditor()
        }))

      box.querySelectorAll('.member-chip-x').forEach(x =>
        x.addEventListener('click', async () => {
          const removedId = members[parseInt(x.dataset.i)]?.id
          members.splice(parseInt(x.dataset.i), 1)
          if (expandedMemberId === removedId) expandedMemberId = null
          await persistMembers(); renderArtists(); renderStintEditor()
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
      wirePickerDropdown(input, document.getElementById('pp-add-dd'), API.artists.search,
        async ({ name }) => {
          name = (name || '').trim()
          if (name && !members.some(m => m.name.toLowerCase() === name.toLowerCase())) {
            members.push({ name }); await persistMembers()   // set_performer_members creates new people as needed
            await refreshRoster()
          }
          renderArtists()
        }, 'Create new artist')
    }

    function renderStintEditor() {
      const box = document.getElementById('pp-stint-editor')
      const member = members.find(m => m.id === expandedMemberId)
      if (!member) { box.style.display = 'none'; box.innerHTML = ''; return }
      box.style.display = ''
      const single = member.stints.length <= 1
      box.innerHTML = `
        <div class="pp-stint-editor-head">
          <span class="pp-stint-editor-title">Stint dates — <b>${esc(member.name)}</b></span>
          <span class="pp-stint-editor-close" title="Close">${icon('x')}</span>
        </div>
        <div class="pp-stint-rows">
          ${member.stints.map(s => `
            <div class="pp-stint-row" data-stint-id="${s.id}">
              ${isUnbounded(s) ? '<span class="pp-stint-always">Always a member — leave blank, or set dates for a specific tenure</span>' : ''}
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
        expandedMemberId = null; renderArtists(); renderStintEditor()
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
            try { await API.performers.updateStint(stintId, read()); await refreshRoster(); renderArtists(); renderStintEditor() }
            catch (e) { alert('Failed to save stint: ' + e.message) }
          }))
        const del = row.querySelector('.pp-stint-del')
        if (del) del.addEventListener('click', async () => {
          try { await API.performers.removeStint(stintId); await refreshRoster(); renderArtists(); renderStintEditor() }
          catch (e) { alert('Failed to remove stint: ' + e.message) }
        })
      })

      box.querySelector('.pp-stint-add-btn').addEventListener('click', async () => {
        try {
          await API.performers.addStint(performerId, member.id, {})   // unbounded until edited
          await refreshRoster(); renderArtists(); renderStintEditor()
        } catch (e) { alert('Failed to add stint: ' + e.message) }
      })
    }

    renderArtists()
    renderStintEditor()

    // Performer.default_personnel_mode is still a real field (new
    // performances of this act still start in whatever mode it's set to,
    // and the case-5 auto-flip still fires per-show) — it just has no
    // manual UI control on this page anymore, per the 2026-07-22 Members/
    // Guests redesign. `defaultPersonnelMode` is kept around unused here
    // only because refreshRoster() still reads it off a fresh fetch; nothing
    // reads the local variable itself now.

    // ── Editable reference Resources (external DBs / discographies) ──────────
    let resources = (performer.resources || []).map(r => ({ label: r.label, url: r.url }))
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
          input.placeholder = 'Label (optional) — Enter to save'
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
    // A performer holds many images with exactly one primary — the primary is
    // the face on Browse cards, the rest are simply available. The big frame
    // shows the primary; thumbnails below switch which one that is.
    //
    // Drag-and-drop accepts a whole selection at once and posts it in a single
    // request. Uploading is the ONLY population route today; the AI/Commons
    // fetch job (Ryan, 2026-08-07) will land images here as non-primary
    // candidates, which is why `origin` exists on the row from day one.
    let ppImages = performer.images || []

    // Small round portrait in the hero. Read-only — all management lives in the
    // Photos tab, so the hero never grows buttons and stays a header.
    function renderHeroPortrait() {
      const box = document.getElementById('pp-hero-portrait')
      if (!box) return
      const primary = ppImages[0] || null    // server orders primary-first
      const ring = performer.genre && performer.genre.color
        ? performer.genre.color : 'var(--bd-1)'
      box.innerHTML = primary
        ? `<img class="pp-portrait-img" style="--ring:${esc(ring)}"
                src="${API.performers.imageUrl(primary.id)}" alt="${esc(performer.name)}">`
        // Initials rather than a silhouette icon: with 1 of 164 performers
        // photographed, the no-photo state IS the page's normal appearance and
        // should look intentional, not like a broken image.
        : `<div class="pp-portrait-blank" style="--ring:${esc(ring)}">${esc(
             performer.name.split(/\s+/).filter(Boolean).slice(0, 2)
               .map(w => w[0]).join('').toUpperCase())}</div>`
    }

    // Full gallery — the Photos tab. Every image is manageable here: make
    // primary, delete, or drop new ones.
    function renderPhotosPane() {
      const box = document.getElementById('pp-photos')
      if (!box) return
      // Read fresh each render — `performer` is REASSIGNED when a MusicBrainz
      // match lands, and this pane has to reflect that (it previously kept
      // saying "match this act first" after a successful match, because it was
      // only ever rendered at page load).
      const hasMbid = !!(performer.musicbrainz && performer.musicbrainz.mbid)
      box.innerHTML = `
        <div class="pp-gal" id="pp-gal">
          ${ppImages.map(img => `
            <div class="pp-ph${img.is_primary ? ' is-primary' : ''}" data-img-id="${img.id}"
                 title="${img.credit ? esc(img.credit) : ''}">
              <img src="${API.performers.imageUrl(img.id)}" alt="" loading="lazy">
              ${img.is_primary ? '<span class="pp-ph-tag">Primary</span>' : ''}
              <!-- CC BY / BY-SA both require credit, so a fetched photo always
                   carries its attribution visibly, not just in the DB. -->
              ${img.credit ? `<span class="pp-ph-credit">${esc(img.credit)}</span>` : ''}
              <div class="pp-ph-acts">
                ${img.is_primary ? '' : `<button type="button" class="pp-ph-btn" data-act="primary">Make primary</button>`}
                <button type="button" class="pp-ph-btn" data-act="delete">Delete</button>
              </div>
            </div>`).join('')}
          <div class="pp-drop" id="pp-image-drop">
            <span class="pp-drop-plus">${icon('plus')}</span>
            <span>Drop photos here<br>or click to browse</span>
            <div class="pp-drop-veil">Drop to upload</div>
          </div>
          <!-- Wikimedia lookup sits as a TILE beside the drop zone (Ryan,
               2026-08-07): both are ways of getting a photo in, so they belong
               side by side rather than one being a footnote below the gallery.
               Disabled rather than hidden when there's no MusicBrainz match —
               the Wikidata link comes from there, and a tile that explains why
               it's unavailable teaches the dependency where hiding it wouldn't. -->
          <div class="pp-drop pp-fetch-tile${hasMbid ? '' : ' is-disabled'}"
               id="pp-fetch-photo" ${hasMbid ? '' : 'aria-disabled="true"'}>
            <span class="pp-drop-plus">☁</span>
            ${hasMbid
              ? `<span>Find a free photo<br><span class="pp-drop-sub">Wikimedia Commons</span></span>`
              : `<span class="pp-drop-sub">Match on MusicBrainz first<br>(Overview tab)</span>`}
          </div>
          <!-- Google Images search tile (Ryan, 2026-08-08) — sits next to the
               Commons tile as a second way IN, for the acts Commons doesn't
               cover. No fetch, no licence check, nothing lands in the gallery
               automatically: it just opens a search tab and Ryan drags/saves
               into the drop zone by hand. Needs no MusicBrainz match, so it's
               never disabled. -->
          <a class="pp-drop pp-google-tile" id="pp-google-photo"
             href="${esc(`https://www.google.com/search?tbm=isch&q=${encodeURIComponent(performer.name)}`)}"
             target="_blank" rel="noopener noreferrer"
             title="Open a Google Images search for this performer in a new tab">
            <span class="pp-drop-plus">🔍</span>
            <span>Search the web<br><span class="pp-drop-sub">Google Images</span></span>
          </a>
        </div>
        <input type="file" id="pp-image-input" multiple
               accept="image/png,image/jpeg,image/webp" style="display:none" />
        <div id="pp-fetch-msg" class="pp-fetch-msg"></div>
        <div class="pp-gal-note">${
          ppImages.length
            ? 'The primary photo is the one shown on this page and on Browse cards.'
            : 'No photos yet. The primary photo appears on this page and on Browse cards.'
        }</div>`

      const input = document.getElementById('pp-image-input')
      input.addEventListener('change', e => { upload(e.target.files); input.value = '' })

      // Delegated so the handler survives every re-render without rebinding.
      // Re-fetches after each mutation rather than patching the local array:
      // the server owns the one-primary rule (set_primary clears siblings in
      // the same transaction, and deleting a primary promotes a survivor), so
      // mirroring that here would be a second implementation waiting to
      // disagree with the first.
      box.querySelector('#pp-gal').addEventListener('click', async e => {
        const btn = e.target.closest('.pp-ph-btn')
        if (!btn) return
        e.preventDefault()
        const id = Number(btn.closest('.pp-ph').dataset.imgId)
        try {
          if (btn.dataset.act === 'primary') {
            await API.performers.setPrimaryImage(id)
          } else {
            if (!confirm('Delete this photo?')) return
            await API.performers.removeImage(id)
          }
          await refreshImages()
        } catch (err) { alert('Failed: ' + err.message) }
      })

      document.getElementById('pp-fetch-photo')?.addEventListener('click', async e => {
        const btn = e.currentTarget
        if (!hasMbid || btn.classList.contains('is-busy')) return
        const msg = document.getElementById('pp-fetch-msg')
        btn.classList.add('is-busy')
        msg.textContent = 'Searching Wikimedia Commons…'
        msg.className = 'pp-fetch-msg'
        try {
          const res = await API.performers.fetchImage(performerId)
          if (!res.found) {
            // Not an error. Most acts genuinely have no freely-licensed photo,
            // and the long tail of this library especially so — saying
            // "failed" would misrepresent an ordinary outcome.
            msg.textContent = 'No freely-licensed photo found for this act.'
            btn.classList.remove('is-busy')
            return
          }
          // refreshImages() re-renders this pane, so the message has to be
          // written AFTER it — otherwise it's wiped by the redraw.
          await refreshImages()
          const m = document.getElementById('pp-fetch-msg')
          m.textContent = 'Added: ' + (res.image.credit || 'Wikimedia Commons')
          m.className = 'pp-fetch-msg is-ok'
        } catch (err) {
          msg.textContent = err.message
          msg.className = 'pp-fetch-msg is-err'
          btn.classList.remove('is-busy')
        }
      })

      const drop = document.getElementById('pp-image-drop')
      drop.addEventListener('click', () => input.click())
      // Counter, not a boolean — dragenter/dragleave fire for every child
      // element crossed, so a flag flickers off halfway through the tile.
      let depth = 0
      drop.addEventListener('dragover', e => { e.preventDefault() })
      drop.addEventListener('dragenter', e => {
        e.preventDefault(); depth++; drop.classList.add('is-dropping')
      })
      drop.addEventListener('dragleave', () => {
        if (--depth <= 0) { depth = 0; drop.classList.remove('is-dropping') }
      })
      drop.addEventListener('drop', e => {
        e.preventDefault(); depth = 0; drop.classList.remove('is-dropping')
        const files = Array.from(e.dataTransfer.files || [])
          .filter(f => f.type.startsWith('image/'))
        if (files.length) upload(files)
      })
    }

    async function refreshImages() {
      ppImages = await API.performers.listImages(performerId)
      performer.images = ppImages
      renderHeroPortrait()
      renderPhotosPane()
      // Keep the tab's count badge honest after an add or delete.
      const tab = mainContent.querySelector('.pp-tab[data-pane="photos"]')
      if (tab) {
        tab.innerHTML = 'Photos' + (ppImages.length ? `<span class="pp-tab-n">${ppImages.length}</span>` : '')
      }
    }

    async function upload(files) {
      if (!files || !files.length) return
      try {
        const res = await API.performers.uploadImages(performerId, files)
        await refreshImages()
        // Partial success is a 200 with an `errors` list — 4 of 5 photos
        // landing should not read as a failure, but the rejected one must say
        // why rather than vanishing.
        if (res.errors && res.errors.length) alert('Some files were skipped:\n' + res.errors.join('\n'))
      } catch (err) { alert('Upload failed: ' + err.message) }
    }

    renderHeroPortrait()
    renderPhotosPane()

    // ── MusicBrainz block (Overview tab) ────────────────────────────────────
    // Four states, and they must look different: matched (facts + links),
    // ambiguous (needs you to pick), none (looked, found nothing), and null
    // (never looked up — pre-existing rows, or created while offline).
    function renderMusicBrainz() {
      const box = document.getElementById('pp-mb')
      if (!box) return
      const mb = performer.musicbrainz || {}
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
               title="View this entry on musicbrainz.org">${esc(mb.name || performer.name)} ↗</a>
            ${facts ? `<span class="pp-mb-facts">${esc(facts)}</span>` : ''}
          </div>
          <div class="pp-mb-foot">
            <span class="pp-mb-dot"></span>${how}
            <button type="button" class="btn btn-ghost btn-xs" id="pp-mb-change">Change match</button>
          </div>`
      } else {
        const msg = mb.status === 'ambiguous'
          ? 'More than one act goes by this name — pick the right one.'
          : mb.status === 'none'
            ? 'Nothing found for this name.'
            : 'Not looked up yet.'
        // Editable search term (Ryan, 2026-08-08): a billing variant like
        // "Aaron Parks Trio" is the act's real name but often not what
        // MusicBrainz indexed it under, so the string actually sent to the API
        // needs to be adjustable without renaming the Performer. Pre-filled
        // with the Performer's name; edits here are one-shot — nothing saved.
        box.innerHTML = `
          <div class="pp-mb-empty">${msg}</div>
          <div class="pp-mb-searchrow">
            <input type="text" class="pp-mb-input" id="pp-mb-term"
                   value="${esc(performer.name)}" placeholder="Search term"
                   title="Sent to MusicBrainz as the artist name — edit if a billing variant (e.g. “Trio”, “Quartet”) is causing a miss">
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
    // Performer's own name (the "Change match" entry point has no box — it
    // starts from an already-matched state). Must be called BEFORE the panel
    // is overwritten with "Searching…", since that swap removes the input.
    function currentMbTerm() {
      const el = document.getElementById('pp-mb-term')
      const v = el && el.value.trim()
      return v || performer.name
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
        const res = await API.performers.mbLookup(performerId, term)
        if (res.status === 'matched') {
          performer = await API.performers.get(performerId)
          renderMusicBrainz()
          // The Photos tab gates its Wikimedia lookup on performer.musicbrainz
          // .mbid. That pane was rendered ONCE at page load, so without this it
          // kept saying "match this act first" after the match succeeded.
          renderPhotosPane()
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
      try { res = await API.performers.mbCandidates(performerId, term) }
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
          <div class="pp-mb-empty">Nothing found for “${esc(query || performer.name)}”.</div>
          <div class="pp-mb-searchrow">
            <input type="text" class="pp-mb-input" id="pp-mb-term"
                   value="${esc(query || performer.name)}" placeholder="Search term"
                   title="Sent to MusicBrainz as the artist name — edit if a billing variant (e.g. “Trio”, “Quartet”) is causing a miss">
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
          cands.length === 1 ? '' : ' — more than one goes by this name'}.</div>
        <!-- Same editable term as the empty state (Ryan, 2026-08-08) — if none
             of these candidates are right, revise and re-search without
             leaving the panel. "Search" only re-lists candidates (no
             auto-link); "Look up" is the gated path that can commit outright. -->
        <div class="pp-mb-searchrow pp-mb-searchrow-sm">
          <input type="text" class="pp-mb-input" id="pp-mb-term"
                 value="${esc(query || performer.name)}" placeholder="Search term"
                 title="Sent to MusicBrainz as the artist name — edit and search again if none of these are right">
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
          ${performer.musicbrainz?.mbid
            ? `<button type="button" class="btn btn-ghost btn-xs" id="pp-mb-clear">Clear match</button>` : ''}
        </div>`

      box.querySelectorAll('.pp-mb-cand').forEach(btn => {
        btn.addEventListener('click', async e => {
          // The View link lives inside the clickable row — following it must
          // not also select the candidate.
          if (e.target.closest('.pp-mb-cand-view')) return
          box.innerHTML = `<div class="pp-mb-empty">Fetching…</div>`
          try {
            await API.performers.mbResolve(performerId, btn.dataset.mbid)
            performer = await API.performers.get(performerId)
            renderMusicBrainz()
            renderPhotosPane()      // see note in runMbLookup
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
          await API.performers.mbResolve(performerId, null)
          performer = await API.performers.get(performerId)
          renderMusicBrainz()
          renderPhotosPane()        // clearing a match disables the lookup again
        } catch (e) { alert('Failed: ' + e.message) }
      })
    }
    renderMusicBrainz()

    // ── AI Assist — biography enrichment, colocated with Description ─────────
    //
    // Reduced 2026-08-07 from a full results panel to a single button beside
    // the Description header. The suggested-resources and pages-consulted
    // lists are gone: they were interesting once and noise thereafter, and the
    // act's own Trusted sources list is where curated links belong.
    //
    // THIS OVERWRITES THE DESCRIPTION — a deliberate, Ryan-approved exception
    // to the project's "AI suggests, human approves" rule (see
    // performer_research.py). That rule exists because a wrong-but-confident
    // date silently overwrote a recording; a biography is a different risk
    // class — visible on screen the moment it lands, freely re-editable, and
    // not a field anything else computes from. The copy-into-place step was
    // pure friction for the only outcome anyone wanted.
    async function runDossier() {
      const btn = document.getElementById('pp-dossier-run')
      const msg = document.getElementById('pp-dossier-msg')
      const descEl = document.getElementById('pp-desc')
      if (!btn || btn.disabled) return
      btn.disabled = true
      const t0 = Date.now()
      msg.className = 'pp-sec-msg'
      msg.textContent = 'Researching the web… this takes a minute or two'
      const tick = setInterval(() => {
        msg.textContent = `Researching the web… ${Math.round((Date.now() - t0) / 1000)}s`
      }, 1000)
      try {
        const { job_id } = await API.performers.startDossier(performerId)
        const result = await pollDossierJob(performerId, job_id, t0)
        clearInterval(tick)

        const bio = stripCitations(result.biography || '')
        if (!bio) {
          msg.textContent = 'No biography could be written for this act.'
          btn.disabled = false
          return
        }
        performer.bio = bio
        await saveField({ bio })
        if (descEl) {
          descEl.textContent = bio
          descEl.classList.remove('pp-empty')
        }
        msg.className = 'pp-sec-msg is-ok'
        // formatAiCost returns an HTML badge, so this must be innerHTML —
        // textContent rendered the literal <span …> markup on the page.
        msg.innerHTML = 'Description updated' +
          (result.usage ? ' · ' + formatAiCost(result.usage) : '')
        btn.textContent = 'AI Assist'
        btn.disabled = false
      } catch (e) {
        clearInterval(tick)
        msg.className = 'pp-sec-msg is-err'
        msg.textContent = 'AI Assist failed: ' + e.message
        btn.disabled = false
      }
    }
    document.getElementById('pp-dossier-run')?.addEventListener('click', runDossier)

    // Cost range on the button's tooltip. Fetched rather than hardcoded so it
    // follows the model chosen in Settings, and fire-and-forget because a
    // failed estimate must never stop the button working.
    API.performers.aiEstimate().then(est => {
      const b = document.getElementById('pp-dossier-run')
      if (!b || est.low_cents == null) return
      b.title = `Researches the web and rewrites the description. `
              + `Roughly ${est.low_cents}–${est.high_cents}¢ per run, `
              + `depending on how many searches it needs.`
    }).catch(() => {})

    if (performer.dossier) {
      // A previous run exists on the record. Nothing is rendered from it any
      // more — the description it produced is already saved — but the label
      // should say this isn't the first pass.
      document.getElementById('pp-dossier-run').textContent = 'AI Assist'
    }

    onAdminClick('pp-delete', async () => {
      if (!confirm(`Delete performer "${performer.name}"? This can't be undone.`)) return
      try { await API.performers.remove(performerId); refreshSidebar(); window.location.hash = '#/' }
      catch (e) { alert(e.message) }
    })
  }

  // Turn an element into a click-to-edit field. opts: {get, onSave, multiline, placeholder}.
  // Every click-to-edit field in the app goes through here — recording notes,
  // performer and venue and genre and collection and artist names and
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
      if (el.querySelector('input, textarea')) return   // already editing
      const cur = opts.get()
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

  // AI Assist on the saved-recording page — open the AI tab, run a research job,
  // render interactive findings (same Apply/auto-update experience as Add
  // Recording, adapted for a live record — see renderRecAiResults).
  async function startRecAiAssist(recordingId, rec, perf) {
    // The button lives inside the (already-open) AI pane; running replaces it.
    const body = document.getElementById('ai-results')
    if (!body) return
    body.innerHTML = `<div class="ai-loading"><div class="loading-spinner"></div><div>Researching the web — this can take a minute or two… <span id="ai-elapsed">0s</span></div></div>`
    const t0 = Date.now()
    try {
      const { job_id } = await API.ingest.aiAssistRecording(recordingId)
      const result = await pollAiJob(job_id, t0)
      if (rec) rec.ai_research = result   // keep local state in sync (server has already saved it)
      renderRecAiResults(result, body, recordingId, rec, perf)
    } catch (e) {
      const secs = Math.round((Date.now() - t0) / 1000)
      const msg = /no_api_key/.test(e.message)
        ? 'No Anthropic API key set — add one in Settings.'
        : `AI Assist failed after ${secs}s: ${esc(e.message)}`
      body.innerHTML = `<div class="ai-assist-cta">
        <p class="ai-res-note" style="color:var(--red)">${msg}</p>
        <button class="btn btn-primary btn-sm iq-ai-btn" id="btn-ai-assist-retry">${icon('sparkles')} Try again</button>
      </div>`
      document.getElementById('btn-ai-assist-retry')?.addEventListener('click', () => startRecAiAssist(recordingId, rec, perf))
    }
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
      return `<div class="info-panel-empty">No checksums on file for this recording yet — click Re-validate to check the library folder for a fingerprint file.</div>`
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
      ? `<p class="cksum-hint">MD5 checks the whole file, tags included — any tag edit (including Write Tags to Files) will flip a match to a mismatch. Expected, not corruption.</p>` : ''
    const st5Note = withData.some(t => t.checksum.type === 'st5')
      ? `<p class="cksum-hint">ST5 verification is best-effort — treat a mismatch as worth a second look, not a hard failure.</p>` : ''
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
    return `<div class="cksum-summary">Found ${fingerprints.length} fingerprint file${fingerprints.length === 1 ? '' : 's'} — verified automatically against the copied files when you confirm.</div>
      <div class="cksum-rows">${rows}</div>`
  }

  // ── Shared AI Assist results template — Add Recording + View Recording ────────
  // One HTML builder for both surfaces so they stay visually/structurally in
  // sync as the feature evolves; each caller wires its own Apply behavior
  // (draft form state vs. live API writes — see renderAiResults / renderRecAiResults).
  // city/state/country are attributes of the Venue record, not the show
  // (Ryan's call, 2026-07-13) — split into a distinct sub-group so that reads
  // clearly regardless of where the proposal ends up getting applied.
  const AI_VENUE_FIELDS = ['city', 'state', 'country']

  // Cost badge — reads the usage block ai_assist.py::_compute_cost attaches
  // to every result (2026-07-21, Problem 3 of the AI Assist Refinement spec).
  // r.usage is null when the model has no pricing entry (see _PRICING in
  // ai_assist.py) rather than showing a misleading "free" — that's the one
  // case this renders nothing.
  function formatAiCost(usage) {
    if (!usage) return ''
    const c = usage.cost_cents
    const label = c >= 100 ? `$${(c / 100).toFixed(2)}` : `${c.toFixed(c < 1 ? 3 : 2)}¢`
    const n = usage.web_search_requests || 0
    const title = `${usage.input_tokens.toLocaleString()} in / ${usage.output_tokens.toLocaleString()} out tokens`
      + (n ? ` + ${n} web search${n === 1 ? '' : 'es'}` : '')
    return `<span class="ai-cost-badge" title="${esc(title)}">${label}</span>`
  }

  function buildAiResultsHtml(r, opts = {}) {
    const proposals = r.proposals || []
    const row = (p, i) => `
      <div class="ai-res-row">
        <span class="ai-res-field">${esc(p.field)}</span>
        <span class="ai-res-value">${esc(p.proposed)}
          <span class="ai-res-conf">${esc(p.confidence || '')}</span>${p.url ? ` <a class="ai-link" href="${esc(p.url)}" target="_blank" rel="noopener">source</a>` : ''}</span>
        <button class="btn btn-ghost btn-xs ai-apply-btn" data-idx="${i}">Apply</button>
      </div>`
    const indexed    = proposals.map((p, i) => ({ p, i }))
    const perfRows   = indexed.filter(x => !AI_VENUE_FIELDS.includes(x.p.field))
    const venueRows  = indexed.filter(x =>  AI_VENUE_FIELDS.includes(x.p.field))
    const propsHtml  = perfRows.map(x => row(x.p, x.i)).join('')
      + (venueRows.length
          ? `<div class="ai-res-subhead">Venue details <span class="ai-res-subhead-note">— the venue record, not this show</span></div>${venueRows.map(x => row(x.p, x.i)).join('')}`
          : '')

    const tt = r.track_titles || []
    const trackSection = tt.length
      ? `<div class="ai-res-section">
           <div class="ai-res-title">Track Listing <button class="btn btn-ghost btn-xs" id="ai-apply-tracks">Apply to tracks</button></div>
           <div class="ai-tt-list">${tt.map(t =>
             `<div class="ai-tt-row"><span class="ai-tt-num">${esc(String(t.number).padStart(2, '0'))}</span><span class="ai-tt-title">${esc(t.title)}</span></div>`).join('')}</div>
         </div>` : ''

    const notes = (title, items) => items && items.length
      ? `<div class="ai-res-section"><div class="ai-res-title">${title}</div>${items.map(v => `<p class="ai-res-note">${esc(v)}</p>`).join('')}</div>` : ''
    const sources = (r.sources || []).length
      ? `<div class="ai-res-section"><div class="ai-res-title">Sources</div>${r.sources.map(s => `<p class="ai-res-note"><a class="ai-link" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a></p>`).join('')}</div>` : ''

    const rerunBtn = opts.showRerun
      ? `<button class="btn btn-ghost btn-xs" id="btn-ai-rerun" title="Run AI Assist again">Run again</button>` : ''

    return `
      <div class="ai-res-section">
        <div class="ai-res-title">Metadata Review ${formatAiCost(r.usage)} ${rerunBtn}</div>
        ${r.thinking ? `<p class="ai-summary">${esc(formatAiThinking(r.thinking))}</p>` : ''}
        ${propsHtml || '<p class="ai-res-empty">No field changes proposed.</p>'}
      </div>
      ${trackSection}
      ${notes('Verify', r.verify_items)}
      ${notes('Provenance', r.provenance_notes)}
      ${sources}`
  }

  // Apply a single AI proposal to the live, saved recording via the same
  // endpoints the page's own inline editors use. city/state/country land on
  // the linked Venue when one exists (and it's a real venue — see
  // isPlaceholderVenue), otherwise on the Performance's own fallback location
  // fields — mirrors how the app resolves location for display everywhere
  // else. `venueRef` is a small mutable holder tracking both the linked
  // venue's id AND name, so a 'venue' proposal applied earlier in the same
  // batch is visible to a 'city'/'state'/'country' proposal applied right
  // after it (and so we know whether that venue is a placeholder).
  async function applyRecProposal(field, value, perf, recordingId, venueRef) {
    const perfId = perf.id
    switch (field) {
      case 'artist':
        await API.performances.update(perfId, { performer_name: value })
        invalidateDims('performers', 'artists')
        break
      case 'date': {
        const p = String(value).split('-')
        await API.performances.update(perfId, {
          start_year:  p[0] ? parseInt(p[0]) : null,
          start_month: p[1] ? parseInt(p[1]) : null,
          start_day:   p[2] ? parseInt(p[2]) : null,
        })
        break
      }
      case 'venue': {
        if (isPlaceholderVenue(value)) {
          // AI proposing "Unknown Venue"/"TBD" isn't a real answer — don't
          // create or link a shared placeholder row. Leave venueRef as-is.
          break
        }
        const existing = await API.venues.list(value)
        let venueId = (existing || []).find(v => v.name.toLowerCase() === value.toLowerCase())?.id
        if (!venueId) { const c = await API.venues.create({ name: value }); venueId = c.id; invalidateDims('venues') }
        await API.performances.update(perfId, { venue_id: venueId })
        venueRef.venue_id = venueId
        venueRef.venue_name = value
        break
      }
      case 'event': {
        const existing = await API.events.search(value)
        let eventId = (existing || []).find(e => e.name.toLowerCase() === value.toLowerCase())?.id
        if (!eventId) { const c = await API.events.create({ name: value }); eventId = c.id }
        await API.performances.update(perfId, { event_id: eventId })
        break
      }
      case 'source':
        await API.recordings.update(recordingId, { source: value, change_note: 'AI Assist' })
        break
      case 'city': case 'state': case 'country':
        // A placeholder-named linked venue ("Unknown Venue", ...) isn't a
        // real canonical place — never write location onto it (that row is
        // shared across unrelated shows). Route to the Performance's own
        // fallback fields instead, same as the no-venue-at-all case.
        if (venueRef.venue_id && !isPlaceholderVenue(venueRef.venue_name)) {
          await API.venues.update(venueRef.venue_id, { [field]: value })
        } else {
          await API.performances.update(perfId, { [field]: value })
        }
        break
    }
  }

  // Apply the AI's researched setlist onto a saved recording's tracks.
  async function applyRecTrackTitles(titles, rec, recordingId) {
    const jobs = (titles || [])
      .map(tt => {
        const track = (rec.tracks || []).find(t => String(t.track_number) === String(tt.number))
        return (track && tt.title) ? API.tracks.update(track.id, { title: tt.title }) : null
      })
      .filter(Boolean)
    if (jobs.length) await Promise.all(jobs)
    renderRecordingView(recordingId)
  }

  // Applied fields land immediately (matches every other field on this page's
  // click-to-edit/auto-save pattern) — no Revert toggle; a full reload
  // refreshes every affected field at once, so "undo" is just editing again.
  //
  // No auto-apply, regardless of confidence (Ryan, 2026-07-20 — AI Assist
  // Refinement spec, Context Library). A rare Danny Gatton/Cellar Door
  // 1/25/79 recording got confidently, silently overwritten with a wrong
  // date twice in a row (a different wrong date each run) — proof the
  // model's own "high confidence" self-rating isn't trustworthy enough to
  // act on unsupervised. Every proposal, at every confidence level, now
  // requires an explicit click on its own Apply button.
  function renderRecAiResults(r, body, recordingId, rec, perf) {
    if (!body) return
    body.innerHTML = buildAiResultsHtml(r, { showRerun: true })
    const venueRef = { venue_id: perf?.venue_id || null, venue_name: perf?.venue || null }

    async function applyOne(idx, btn) {
      const p = (r.proposals || [])[idx]
      if (!p) return
      if (btn) { btn.disabled = true; btn.textContent = '…' }
      try {
        await applyRecProposal(p.field, p.proposed, perf, recordingId, venueRef)
        if (btn) { btn.textContent = 'Applied'; btn.classList.add('applied') }
      } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'Apply' }
        alert('Failed to apply: ' + e.message)
        throw e
      }
    }

    body.querySelectorAll('.ai-apply-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        try { await applyOne(parseInt(btn.dataset.idx), btn) }
        catch (_) { return }
        renderRecordingView(recordingId)
      })
    })
    document.getElementById('ai-apply-tracks')?.addEventListener('click', () =>
      applyRecTrackTitles(r.track_titles || [], rec, recordingId))
    document.getElementById('btn-ai-rerun')?.addEventListener('click', () =>
      startRecAiAssist(recordingId, rec, perf))
  }

  /** Recording detail — split panel: tracks + info file */
  async function renderRecordingView(recordingId) {
    setActiveNav('library')
    setLoading()
    state.currentRecId      = recordingId
    state._lastTrackCount   = null   // reset until rec loads

    let rec
    try {
      rec = await API.recordings.get(recordingId)
      state._lastTrackCount = rec.tracks?.length ?? null
    } catch (e) {
      setMainHTML(`<div class="empty-state"><div class="empty-title">Recording not found</div></div>`)
      return
    }

    // "← Back" points at whatever page immediately preceded this one — the
    // generic navBack mechanism (route()), not the old state.selectedArtist
    // hack that only worked if you'd arrived via a Performer page (Ryan's
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
    const perfName   = perf?.performer || ''
    const perfId     = perf?.performer_id || null
    const eventStr   = perf?.event_name || ''
    setNavCurrent(dateStr || perfName || 'Recording')

    // Small "go to its own page" nav icons (2026-07-23) — same treatment for
    // Performer and Venue, shown regardless of edit permission since it's
    // navigation, not editing. Plain hash links — the generic navBack
    // mechanism (route()) picks up the "came from a recording" reference
    // automatically, no per-link wiring needed.
    const perfNavLink = perfId
      ? `<a class="rec-nav-link" href="#/performer/${perfId}" title="Go to ${esc(perfName)}'s page">↗</a>` : ''
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

    // Inner HTML of a track's title cell: title + official badge + flag chips
    // + inline note. Factored so the right-click quick-edit menu can refresh a
    // single row in place after changing flags or notes.
    function trackTitleInnerHtml(t) {
      const badges = trackBadgesHtml(t)
      return `<span class="track-title-text">${esc(t.title)}</span>${badges ? ' ' + badges : ''}`
    }

    // Flat track list — no disc/set grouping. Note/Songwriter are click-to-edit
    // directly in the row; right-click is Flags (+ Official) only — matches
    // Add Recording's track table treatment (Ryan, 2026-07-15).
    const canEdit  = canEditLibrary()
    const editHint = canEdit ? ' title="Click title to rename · right-click for flags"' : ''
    const trackRows = (rec.tracks || []).map(t => {
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
    }).join('')

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
    const sourceLineageRow = (!qEditable && !rec.source && !rec.lineage && !rec.quality) ? '' : `
      <div class="rec-sl-row">
        <div class="rec-sl-item rec-sl-item--quality">
          <span class="mg-row-label">Rating</span>
          <span class="hm-val hm-val--chip${qc}"${qa('quality')}><span class="quality ${qualityClass(rec.quality)}">${esc(rec.quality || '\u2014')}</span></span>
        </div>
        <div class="rec-sl-item">
          <span class="mg-row-label">Source</span>
          <span class="hm-val hm-val--chip${qc}"${qa('source')}>${sourceBadge(rec.source) || '\u2014'}</span>
        </div>
        <div class="rec-sl-item rec-sl-item--lineage">
          <span class="mg-row-label">Lineage</span>
          <span class="hm-val${qc}"${qa('lineage')}>${esc(lineageDisplay || rec.lineage || '\u2014')}</span>
        </div>
      </div>`

    // Re-Analyze Tracks now lives in the Side Panel's tab strip with every
    // other pane action (2026-08-21) — see the .pane-acts block below.
    //
    // Named "Tracks" deliberately: POST /reprocess re-runs the per-track librosa
    // pass (waveform, spectrogram, the raw readings) and does NOT recompute the
    // recording_quality row — there is no per-recording rescore endpoint today,
    // only the bulk quality_store.rescore_stored(). A button labelled
    // "Re-Analyze" sitting beside a quality verdict it silently leaves alone is
    // exactly the kind of thing that costs an hour later, so the label says
    // what it does.

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
            <!-- Move to — same two destinations as the triage queue's Move,
                 deliberately: Workshop and Backlog are the two real folders a
                 show goes back to, and having a different vocabulary before
                 and after ingest would be the kind of small inconsistency
                 that makes people hesitate. -->
            ${rec.is_published === false ? `
            <div class="actions-note">Out of the library — in Workshop or Backlog</div>` : `
            <button class="actions-item" role="menuitem" data-act="move-toggle" aria-expanded="false">Move to ${chevronIcon()}</button>
            <div class="actions-submenu" id="rec-move-sub" hidden>
              <button class="actions-item actions-item--indent" role="menuitem" data-act="move" data-dest="workshop">Workshop</button>
              <button class="actions-item actions-item--indent" role="menuitem" data-act="move" data-dest="backlog">Backlog</button>
            </div>`}
            <div class="actions-sep"></div>
            <button class="actions-item actions-item--danger" role="menuitem" data-act="delete">Delete Recording…</button>
          </div>
        </div>` : ''}
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
          <!-- Performer avatar (Ryan, 2026-08-18; squared 2026-08-27) — to the
               left of the name and date lines, spanning both. Same
               perfPhotoHtml() the cards and the Performer hero use, so an act
               has one face everywhere; falls back to the initials disc, which
               is the NORMAL appearance rather than an error state (62 of 173
               performers are photographed). Ringed in the performer's genre
               colour, matching the card treatment. -->
          <div class="rec-header-avatar" style="--genre-fg:${esc(perf?.performer_genre_color || 'var(--bd-1)')}">
            ${perfPhotoHtml({ image_id: perf?.performer_image_id, performer: perfName }, 'rec-header-photo')}
          </div>
          <div class="rec-header-lines">
          <div class="rec-name-row">
            <h2 class="rec-perf-name${canEdit ? ' pp-editable' : ''}" id="rec-perf-name"${canEdit ? ' title="Click to reassign performer"' : ''}>${esc(perfName) || (canEdit ? '<span class="pp-empty">Set performer</span>' : '')}</h2>
            ${perfNavLink}
          </div>
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
          </div>
          <div class="rec-artists-row" id="rec-artists"></div>
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
             left it floating above the Performer Name with a big dead gap
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
        <label class="skip-toggle skip-toggle--action" title="Skip announcements, banter &amp; tuning from queue">
          <input type="checkbox" class="skip-filter-cb" id="skip-filter-action" ${state.skipNonMusic ? 'checked' : ''} />
          <span class="skip-toggle-track"></span>
          <span class="skip-toggle-label">Skip Non-Music</span>
        </label>
      </div>
      <div class="detail-panels" id="detail-panels">
        <div class="track-panel" id="track-panel">
          ${trackRows || '<div class="info-panel-empty">No tracks</div>'}
        </div>

        <!-- The Details pane is an ADMIN surface and is not rendered at all in
             Playback mode (Ryan, 2026-08-21) — not hidden with CSS, absent.
             Info file, quality metrics, Vorbis comments and checksums are an
             archivist's working set; a listener opening a show wants the tracks
             and the transport. Removing it also gives the track list the full
             width, which is the point of the mode. -->
        ${canEdit ? `
        <!-- Slide-in right panel — the Details pane.
             Horizontal tab strip (Ryan, 2026-08-18). The vertical strip ran
             out of vertical room once a fifth tab arrived, and it degraded
             badly on a short browser window — rotated text cannot wrap or
             ellipsize. Horizontal tabs scroll sideways instead, which is a
             graceful failure.

             The vertical DETAILS rail is now PERMANENT (Ryan, 2026-08-21).
             It used to appear only while collapsed, which left "click the
             active tab again" as the sole way back to a full-width track
             list — a gesture nothing on the page advertises. The rail is a
             visible, always-present toggle: click to hide, click to show.
             That also makes it the natural affordance for the listener
             layout, where Details is the thing you usually want out of the
             way.

             DOM note: the rail is a sibling of .slide-panel-main (tabs +
             panes) rather than living inside it, so it keeps its own fixed
             28px column in both states.

             It sits AFTER the panel body, i.e. against the window's right
             edge (Ryan, 2026-08-28). It used to lead, which put it on the
             panel's inner edge: the panel grows leftwards when it opens, so
             the rail travelled the panel's whole width every time it was
             clicked. A toggle that jumps out from under the cursor when you
             press it is a bad toggle. Pinned to the outer edge it holds still
             in both states and only the panel body moves, which is also how
             Add Recording draws it. -->
        <div class="slide-panel slide-panel--htabs" id="slide-panel">
          <div class="slide-panel-main">

          <!-- Two rows: navigation, then actions (Ryan, 2026-08-21).
               Every pane used to repeat its own name in a .slide-pane-header
               directly under the tab that already said it, and each pane put
               its action somewhere different — Re-Analyze inside the Quality
               report, AI Assist as a call-to-action block in its pane,
               Re-validate in a pane header, Write Tags all the way down in the
               page's bottom row. The pane headers are gone and every action now
               uses .pane-act in one place.
               That place is a row of its OWN, under the tabs, rather than the
               right end of the tab strip: crammed in beside five tabs the
               longer labels ("Write Tags to Files") pushed the strip into
               horizontal scrolling, so the action could scroll out of sight —
               and an action bar you have to scroll to find is worse than the
               scattered buttons it replaced. The row hides itself when the
               active pane has nothing to offer. -->
          <div class="slide-tabrow">
          <div class="slide-tabs">
            <!-- Info File leads and is the default. It is the taper's own
                 document — the one artifact that arrived with the recording,
                 and the thing you open a show to read. Quality is a machine
                 opinion and can wait one click. -->
            <button class="slide-tab" data-pane="info">Info File</button>
            <!-- "Quality", not "Listening Quality" — the tab is a label in a
                 row of one-or-two-word labels. The score itself is still
                 called Listening Quality everywhere it is described. -->
            <button class="slide-tab" data-pane="quality">Quality</button>
            <!-- The tab itself goes amber when the database holds metadata the
                 FLAC files do not (Ryan, 2026-08-22). The Write Tags button was
                 already marked, but it only exists while you are LOOKING at the
                 File Tags pane — so the one signal that mattered was invisible
                 from every other tab. -->
            <button class="slide-tab${stagedCount > 0 ? ' slide-tab--staged' : ''}" data-pane="filetags">File Tags</button>
            <button class="slide-tab" data-pane="checksums">Checksums</button>
            ${canEdit ? `<button class="slide-tab slide-tab--ai" data-pane="ai">AI Assist</button>` : ''}
          </div>

          <!-- Action row. Every action for every pane is rendered once here and
               shown by data-for as the pane changes, rather than being
               re-created on each switch — so ids stay stable and existing
               wiring (markStaged's #btn-write-tags, wireReanalyze) keeps
               working with no lookup churn. -->
          <div class="pane-acts" id="pane-acts">
              ${canEdit ? `
              <span class="pane-act-status" id="rec-info-save-status" data-for="info"></span>
              <button class="pane-act act-suppressed" id="btn-rec-save-info" data-for="info" hidden disabled>Save to File</button>
              <button class="pane-act" id="btn-info-edit" data-for="info">Edit File</button>` : ''}
              ${canEdit ? `
              <button class="pane-act" id="btn-analyze-audio" data-for="quality"
                      title="Re-runs per-track audio analysis (waveform, spectrogram, raw readings). Does not recompute the Listening Quality score.">Re-Analyze Tracks</button>` : ''}
              ${canEdit ? `
              <span class="pane-act-note${stagedCount > 0 ? '' : ' act-suppressed'}" id="tags-staged-note" data-for="filetags"
                    ${stagedCount > 0 ? '' : 'hidden'}>Edits not yet written to the files</span>
              <button class="pane-act${stagedCount > 0 ? ' pane-act--staged' : ''}" id="btn-write-tags" data-for="filetags"
                      title="Write the database's metadata into the FLAC files' Vorbis comments">Write Tags to Files</button>` : ''}
              <button class="pane-act" id="btn-cksum-revalidate" data-for="checksums"
                      title="Re-check against the files on disk">Re-validate</button>
              ${canEdit ? `
              <button class="pane-act pane-act--primary" id="btn-ai-assist" data-for="ai">${icon('sparkles')} AI Assist</button>` : ''}
          </div>
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

            ${canEdit ? `
            <!-- AI Assist pane. The execute button lives in the tab strip with
                 every other pane action; the pane itself holds results, and
                 renderRecAiResults() replaces this block wholesale — which is
                 the other reason the button had to move out of it. -->
            <div class="slide-pane" id="sp-ai">
              <div class="slide-pane-scroll"><div class="ai-results" id="ai-results">
                <div class="ai-assist-hint">Research the web to verify and fill this recording's metadata.</div>
              </div></div>
            </div>` : ''}

          </div>
          </div>
          <button class="slide-rail" id="slide-rail" title="Show/hide details" aria-expanded="false">Details</button>
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
    // "pause" icon could never actually get back to "play").
    mainContent.querySelectorAll('.track-row[data-track-id]').forEach(row => {
      row.addEventListener('click', () => {
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

    // ── Quick edit: recording metadata (Source/Lineage/Quality) ──────────────
    // Click an editable value → inline input → Enter saves, Esc cancels.
    // Rating dropped 2026-08-18 — see app/models/recording.py.
    // Redisplay after a quick edit must rebuild the SAME chip markup the
    // initial render emitted (2026-08-21) — otherwise editing Source once
    // silently downgrades it from a coloured badge to grey text until reload.
    function metaCellDisplay(field) {
      if (field === 'source')  return sourceBadge(rec.source) || '—'
      if (field === 'lineage') { const l = rec.lineage; return esc(l ? (l.length > 220 ? l.slice(0, 220) + '…' : l) : '—') }
      return `<span class="quality ${qualityClass(rec.quality)}">${esc(rec.quality || '—')}</span>`  // quality
    }
    function startMetaQuickEdit(cell) {
      const field = cell.dataset.qedit
      const raw = field === 'source'  ? (rec.source  || '')
                : field === 'lineage' ? (rec.lineage || '')
                :                       (rec.quality || '')
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
    // The markup lives here, once, and both paths call it: renderRecArtists()
    // inside the editing block, and the read-only render below. Duplicating it
    // would be the classic two-implementations-of-one-thing drift.
    function recPersonnelHtml(personnel, editable) {
      const members = (personnel || []).filter(p => !p.is_guest)
      const guests  = (personnel || []).filter(p =>  p.is_guest)
      // The name is a link to that person's Artist page, in BOTH modes
      // (Ryan, 2026-08-22). It used to open an inline instrument/note editor —
      // see renderPersonnelDetail, removed with it. Every other name in the app
      // navigates when clicked; this one alone opened a form, which is exactly
      // the kind of inconsistency that makes people stop clicking things.
      //
      // artist_id is on every resolved entry (inherited ones have no
      // PerformancePersonnel row, so `id` can be null — `artist_id` cannot).
      // Guard anyway: a name with nowhere to go renders as plain text rather
      // than a link to #/person/undefined.
      const pill = (p, i, role) => `
        <span class="member-chip ${role === 'guest' ? 'member-chip--guest' : ''}">
          ${p.artist_id
            ? `<a class="member-chip-name rec-pill-name" href="#/person/${p.artist_id}" title="Open ${esc(p.name)}">${esc(p.name)}</a>`
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
            <button type="button" class="mg-add-btn" data-role="${role}" title="Add ${label === 'Members' ? 'Member' : 'Guest'} Name">+</button>
            <span class="artist-picker-wrap mg-add-picker" data-role="${role}" style="display:none">
              <input type="text" class="member-input mg-role-input" data-role="${role}" autocomplete="off" placeholder="Add ${label === 'Members' ? 'Member' : 'Guest'} Name…" />
              <div class="artist-dropdown mg-role-dd" data-role="${role}" style="display:none"></div>
            </span>` : ''}
        </div>`
      }
      return row('member', 'Members', members) + row('guest', 'Guests', guests)
    }

    // Read-only render for Playback mode and for listeners. The editable path
    // renders from inside the `if (canEdit && perf)` block below.
    if (!canEdit && perf) {
      const box = document.getElementById('rec-artists')
      if (box) box.innerHTML = recPersonnelHtml(perf.personnel || [], false)
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
    if (canEditLibrary()) {
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
            onChange: async (t) => {
              try { await API.tracks.update(t.id, { flags: t.flags, is_official: t.is_official }); markStaged() }
              catch (e) { console.error(e) }
              refreshTrackRow(t)
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

    // Collection tags (add / remove)
    wireRecCollectionArea(recordingId)

    // ── Inline header editing (performer / date / venue / artists / notes) ─────
    if (canEdit && perf) {
      const reload = () => renderRecordingView(recordingId)

      // Performer name → reassign (autocomplete; Enter commits typed name).
      const nameEl = document.getElementById('rec-perf-name')
      nameEl?.addEventListener('click', () => {
        if (nameEl.querySelector('input')) return
        nameEl.innerHTML = `<span class="artist-picker-wrap" style="display:inline-block; min-width:220px">
          <input type="text" class="pp-inline-input" id="rec-perf-input" value="${esc(perf.performer || '')}" autocomplete="off" />
          <div class="artist-dropdown" id="rec-perf-dd" style="display:none"></div></span>`
        const input = document.getElementById('rec-perf-input')
        input.focus(); input.select()
        let committed = false
        const commit = async name => {
          if (committed) return; committed = true
          name = (name || '').trim()
          if (name && name.toLowerCase() !== (perf.performer || '').toLowerCase()) {
            try { await API.performances.update(perf.id, { performer_name: name }); invalidateDims('performers', 'artists') }
            catch (e) { alert('Failed: ' + e.message) }
          }
          reload()
        }
        wirePickerDropdown(input, document.getElementById('rec-perf-dd'), API.performers.search,
          ({ name }) => commit(name), 'Create new performer')
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
          <a class="field-toggle-link" id="rec-toggle-end-date" href="#">${hasEnd ? '− End date' : '+ End date'}</a>
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
            e.currentTarget.textContent = '+ End date'
            document.getElementById('rec-d-y2').value = ''
            document.getElementById('rec-d-m2').value = ''
            document.getElementById('rec-d-d2').value = ''
          } else {
            box.style.display = 'inline-flex'
            e.currentTarget.textContent = '− End date'
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
              : `Saved — ${res?.reason || 'database only'}`
            infoStatus.title = infoStatus.textContent
          } catch (e) {
            infoStatus.textContent = 'Save failed: ' + e.message
            infoStatus.title = infoStatus.textContent
            infoSaveBtn.disabled = false
          }
        })
      }

      // Members/Guests two-row personnel widget (2026-07-22, replacing the
      // single Artists pill row + Inherit/Explicit mode selector). Pills
      // split purely on perf.personnel[].is_guest — Members = roster/explicit
      // non-guest rows, Guests = is_guest rows — same split used by the Add
      // Recording form's createMembersWidget, matched visually here (mg-row/
      // mg-add-btn/mg-add-picker markup) so both surfaces look identical.
      // The Inherit/Explicit mode is still a real field on Performance (case
      // 5 — dropping a roster member for this one show — still auto-flips it
      // under the hood), it just no longer has a manual UI control; nothing
      // in this Phase needed one, since editing the rows already covers
      // every case the toggle used to require picking by hand.
      const persistPersonnelLists = async (memberNames, guestNames) => {
        try {
          await API.performances.update(perf.id, { members: memberNames, guests: guestNames })
          invalidateDims('artists')
        } catch (e) { alert('Failed: ' + e.message) }
        reload()
      }

      function renderRecArtists() {
        const box = document.getElementById('rec-artists')
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

        box.querySelectorAll('.mg-add-btn').forEach(btn =>
          btn.addEventListener('click', () => {
            const picker = box.querySelector(`.mg-add-picker[data-role="${btn.dataset.role}"]`)
            const input  = picker?.querySelector('.mg-role-input')
            if (!picker || !input) return
            const showing = picker.style.display !== 'none'
            box.querySelectorAll('.mg-add-picker').forEach(p => { p.style.display = 'none' })
            picker.style.display = showing ? 'none' : 'inline-flex'
            if (!showing) input.focus()
          }))

        box.querySelectorAll('.mg-role-input').forEach(input => {
          const role = input.dataset.role
          const dd   = box.querySelector(`.mg-role-dd[data-role="${role}"]`)
          wirePickerDropdown(input, dd, API.artists.search,
            async ({ name }) => {
              name = (name || '').trim()
              if (!name || listFor(role).some(p => p.name.toLowerCase() === name.toLowerCase())) return
              const newMembers = members.map(p => p.name).concat(role === 'member' ? [name] : [])
              const newGuests  = guests.map(p => p.name).concat(role === 'guest'  ? [name] : [])
              await persistPersonnelLists(newMembers, newGuests)
            }, 'Create new artist')
        })
      }

      // renderPersonnelDetail() REMOVED 2026-08-22 (Ryan) — the inline
      // instrument/note editor is out of V1. Clicking a name now opens that
      // person's Artist page instead, which is what every other name in the app
      // does.
      //
      // The DATA and its API survive untouched: PerformancePersonnel still
      // carries `instrument` and `note`, resolve_performance_personnel still
      // returns them, and PATCH /api/performances/<id>/personnel/<row_id>
      // (API.performances.updatePersonnelRow) still writes them. Only the UI
      // went. Rebuilding it is a render function, not a migration.

      renderRecArtists()

      // AI Assist (top-right) — research the web to verify/fill this recording.
      // Scoped inside this block (like the header editors above) since applying
      // a proposal needs perf.id. Non-editors never get the pane in the DOM.
      document.getElementById('btn-ai-assist')?.addEventListener('click', () => startRecAiAssist(recordingId, rec, perf))
      // Saved research from a prior run — render it immediately instead of the CTA.
      if (rec.ai_research) {
        renderRecAiResults(rec.ai_research, document.getElementById('ai-results'), recordingId, rec, perf)
      }
    }

    // Analyze Audio — run Librosa analysis on all tracks. The button lives in
    // the tab strip and is present from page build, so it is bound once below
    // (see wireReanalyze) rather than re-attached on every pane render.
    async function onAnalyzeAudio() {
      const btn = document.getElementById('btn-analyze-audio')
      if (!btn) return
      btn.disabled = true
      btn.textContent = 'Analyzing…'
      try {
        const result = await API.recordings.reprocess(recordingId)
        btn.textContent = `Done (${result.analysed} track${result.analysed === 1 ? '' : 's'})`
        setTimeout(() => {
          if (btn) { btn.disabled = false; btn.textContent = 'Re-Analyze Tracks' }
          renderRecordingView(recordingId)  // reload to show waveform/spectrogram
        }, 1500)
        if (result.errors?.length) {
          console.warn('Analysis errors:', result.errors)
        }
      } catch (e) {
        btn.disabled = false
        btn.textContent = 'Re-Analyze Tracks'
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

    // Write FLAC tags
    document.getElementById('btn-write-tags')?.addEventListener('click', async () => {
      const ok = confirm('Write current metadata as FLAC tags to all tracks in this recording?\n\nThis replaces all existing Vorbis comments in the files.')
      if (!ok) return
      try {
        const btn = document.getElementById('btn-write-tags')
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
        const btn = document.getElementById('btn-write-tags')
        if (btn) { btn.disabled = false; btn.textContent = 'Write Tags to Files' }
      }
    })

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
              Any performer or venue left with nothing attached is pruned too.</p>
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
          // Deleting a recording can prune its performer / venue / artists.
          invalidateDims('performers', 'venues', 'artists')
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
        // A move can empty a performer or venue of everything visible.
        invalidateDims('performers', 'venues', 'artists')
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
      if (row) row.hidden = !any
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
    ;(function () {
      const panel = document.getElementById('slide-panel')
      if (!panel) return
      let activePane = null

      const rail = document.getElementById('slide-rail')

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
        else openPane(state.recLastPane || 'info')
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
      const startPane = (state.recLastPane && state.recLastPane !== 'spectrogram')
        ? state.recLastPane : 'info'
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
    let _qualityLoaded = false
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
        body.innerHTML = `<div class="rq-empty">No listening-quality analysis for this
          recording yet — run Re-Analyze Tracks above.</div>`
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

      if (label) label.textContent = trackTitle ? ` — ${trackTitle}` : ''
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
        const json = JSON.stringify(obj, null, 2)
        body.innerHTML = `<pre class="filetags-json">${esc(json)}</pre>`
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

  // ── Batch Import ────────────────────────────────────────────────────────────

  // State for the batch import session (now the "Metadata review" stage of
  // the unified ingestion flow, reached at '#/batch' once Listening Quality
  // triage has accepted at least one folder — see `lq` state below).
  const batch = {
    sourceDir:   null,   // scanned directory path
    results:     null,   // full scan response
    acceptedPaths: null, // Set of NFC-normalised folder paths triaged 'accepted'
                         // (from GET /api/quality/staging) — null means "no LQ
                         // gate", i.e. this directory was never triaged, in
                         // which case everything scanned is shown (keeps this
                         // view usable if it's ever reached without going
                         // through Listening Quality first).
    ingestedIds: new Map(), // path → recording_id for items ingested this session
    expandedPaths: new Set(), // expanded row paths
    behavior:    null,   // 'copy' | 'move' — synced with the shared ingest_file_behavior pref
  }

  async function renderBatchImportView() {
    setActiveNav('ingest')   // reached from Add Recording; keep it lit
    // No directory to review — this stage is only reachable after Listening
    // Quality triage set one (or via a stale '#/batch' bookmark/back-nav from
    // before 2026-07-30's unification, which no longer has its own picker).
    // Either way, the unified flow's source step is the right place to land.
    if (!batch.sourceDir) { window.location.hash = '#/ingest'; return }
    // Re-scan the last directory every time we land on this route (not just
    // the first time) — so returning here always reflects current disk + DB
    // state and anything ingested (this session or otherwise) drops off the list.
    setMainHTML(`<div class="empty-state">Refreshing <code>${esc(batch.sourceDir)}</code>…</div>`)
    try {
      // Scan + the current triage state both come fresh off the server on
      // every entry, so the accepted-set can never go stale (a re-scan after
      // a later triage change, app restart, etc. is always correct).
      const [scanResult, stagingResult] = await Promise.all([
        API.ingest.batchScan(batch.sourceDir),
        API.quality.staging(batch.sourceDir),
      ])
      batch.results = scanResult
      batch.acceptedPaths = new Set(
        stagingResult.results
          .filter(r => r.triage_status === 'accepted')
          .map(r => nfc(r.folder_path))
      )
    } catch (e) {
      if (/^Directory not found:/.test(e.message)) {
        // Not a real failure — the scanned folder itself is gone, almost
        // certainly because it WAS the "Performer Name" staging folder
        // (Bulk Import pointed directly at one act's folder), and finishing
        // its last show just deleted it as empty (move_to_library's
        // empty-parent cleanup, 2026-07-23 — Ryan hit this immediately:
        // "Mr. Sun"). There's nothing left to import here, not an error.
        // Nothing to fall back to but a fresh run of the unified flow.
        batch.sourceDir = null
        batch.results   = null
        window.location.hash = '#/ingest'
        return
      }
      setMainHTML(`<div class="empty-state" style="color:var(--red)">Scan failed: ${esc(e.message)}</div>`)
      return
    }
    renderBatchResultsView()
  }

  function _batchDateStr(e) {
    return [e.year,
      e.month ? String(e.month).padStart(2,'0') : null,
      e.day   ? String(e.day).padStart(2,'0')   : null,
    ].filter(Boolean).join('-')
  }

  // Render a single compact row — score-driven only; no tier dots/border.
  function _batchRow(item) {
    const e       = item.extracted
    const conf    = item.confidence
    const health  = item.health || { score: 0, band: 'red' }
    const ingestedId = batch.ingestedIds.get(item.path)
    const ingested   = ingestedId != null
    const expanded = batch.expandedPaths.has(item.path)
    const dateStr  = _batchDateStr(e)
    const loc      = [e.city, e.state].filter(Boolean).join(', ')

    // Issue chips
    const issueChips = item.issues.map(iss =>
      `<span class="batch-issue-${iss.severity}">${esc(iss.msg)}</span>`
    ).join('')

    // Action buttons — every uningested row gets both: Auto-Ingest (trust the
    // bot) or Review (open the full wizard, pre-scanned), regardless of score.
    let actionBtn = ''
    if (ingested) {
      actionBtn = `<span class="batch-done-check">Ingested</span>
                   <a class="batch-rec-link" href="#/recording/${ingestedId}">View →</a>`
    } else {
      actionBtn = `<button class="btn btn-primary btn-sm batch-ingest-btn" data-path="${esc(item.path)}">Auto-Ingest</button>
                   <button class="btn btn-ghost btn-sm batch-review-btn" data-path="${esc(item.path)}">Review →</button>`
    }

    // Full inferred per-track listing — exactly what Auto-Ingest would write,
    // so a person can eyeball the setlist before deciding Review vs Auto-Ingest.
    const trackRows = (e.tracks || []).map(t => `
      <div class="batch-track-row">
        <span class="batch-track-num">${t.number}</span>
        <span class="batch-track-title ${!t.title ? 'batch-val-uncertain' : ''}">${esc(t.title || '(no title)')}</span>
        <span class="batch-track-src">${t.source ? (t.source === 'tags' ? 'tag' : 'info file') : ''}</span>
      </div>`).join('')

    // Expanded detail panel — the full inferred data for every field, so a
    // person can decide whether to trust Auto-Ingest or hand-review.
    const detail = expanded ? `
      <div class="batch-expand-panel">
        <div class="batch-expand-grid">
          <div class="batch-expand-row"><span class="batch-expand-label">Artist</span><span class="batch-expand-val ${conf.artist !== 'high' ? 'batch-val-uncertain' : ''}">${esc(e.artist || '—')}</span></div>
          <div class="batch-expand-row"><span class="batch-expand-label">Date</span><span class="batch-expand-val ${conf.date !== 'high' ? 'batch-val-uncertain' : ''}">${esc(dateStr || '—')}</span></div>
          <div class="batch-expand-row"><span class="batch-expand-label">Venue</span><span class="batch-expand-val">${esc(e.venue || '—')}</span></div>
          <div class="batch-expand-row"><span class="batch-expand-label">Location</span><span class="batch-expand-val">${esc(loc || '—')}</span></div>
          <div class="batch-expand-row"><span class="batch-expand-label">Country</span><span class="batch-expand-val">${esc(e.country || '—')}</span></div>
          <div class="batch-expand-row"><span class="batch-expand-label">Source</span><span class="batch-expand-val">${esc(e.source || '—')}</span></div>
          <div class="batch-expand-row"><span class="batch-expand-label">Lineage</span><span class="batch-expand-val">${esc(e.lineage || '—')}</span></div>
          <div class="batch-expand-row"><span class="batch-expand-label">Tracks</span><span class="batch-expand-val">${(() => {
            const audio = e.track_count
            const tagged = e.tracks_titled
            const infoCount = e.info_track_count || 0
            const tagLine = tagged > 0 ? `${tagged}/${audio} titled in tags` : `0 titled in tags`
            if (infoCount > 0) {
              const match = infoCount === audio
                            const matchCls  = match ? '' : 'batch-val-uncertain'
              return `${audio} audio · ${tagLine} · <span class="${matchCls}">${infoCount} in info file${match ? '' : ', count mismatch'}</span>`
            }
            return `${audio} audio · ${tagLine} · no info file track list`
          })()}</span></div>
          ${trackRows ? `
          <div class="batch-expand-row batch-expand-tracklist"><span class="batch-expand-label">Listing</span><div class="batch-track-list">${trackRows}</div></div>` : ''}
          ${issueChips ? `
          <div class="batch-expand-row"><span class="batch-expand-label">Issues</span><span class="batch-expand-val">${issueChips}</span></div>` : ''}
          <div class="batch-expand-row"><span class="batch-expand-label">Path</span><span class="batch-expand-val batch-path-mono">${esc(item.path)}</span></div>
        </div>
      </div>` : ''

    // Summary line for collapsed state
    const summaryParts = [e.artist || '?', dateStr || '?']
    if (e.venue) summaryParts.push(e.venue)
    else if (loc) summaryParts.push(loc)
    summaryParts.push(`${item.audio_count} tracks`)

    return `
      <div class="batch-item-row ${ingested ? 'batch-item-ingested' : ''}"
           data-path="${esc(item.path)}">
        <div class="batch-item-main">
          <button class="batch-expand-btn" data-path="${esc(item.path)}" title="${expanded ? 'Collapse' : 'Expand'}">
            ${chevronIcon(expanded ? 'caret-ic--open' : '')}
          </button>
          <div class="batch-item-info">
            <div class="batch-item-name">${esc(item.name)}</div>
            <div class="batch-item-summary">
              ${summaryParts.map(p => `<span class="batch-meta-field">${esc(p)}</span>`).join('<span class="batch-meta-sep">·</span>')}
            </div>
          </div>
          <span class="batch-score batch-score--${health.band}" title="Metadata completeness">${esc(_metaRating(health))}</span>
          <div class="batch-item-actions">
            <span class="batch-ingest-status" id="batch-status-${item.path.replace(/[^a-zA-Z0-9]/g,'_')}"></span>
            ${actionBtn}
          </div>
        </div>
        ${detail}
      </div>`
  }

  async function renderBatchResultsView() {
    setNavCurrent('Batch Import')
    const r = batch.results
    if (!r) { window.location.hash = '#/ingest'; return }

    // Default the file-behavior choice from the shared preference, once per session.
    if (batch.behavior == null) {
      try {
        const prefs = await API.preferences.get()
        batch.behavior = prefs.ingest_file_behavior || 'move'
      } catch (_) { batch.behavior = 'move' }
    }

    // Listening Quality gate (2026-07-30): only folders the triage step
    // accepted make it to metadata review. `acceptedPaths` is null when this
    // directory was never triaged (e.g. a stale '#/batch' bookmark) — in that
    // case fall back to showing everything scanned, so the page never just
    // silently shows nothing for a reason the user can't see.
    const scannedItems = r.items
    const items = batch.acceptedPaths
      ? scannedItems.filter(i => batch.acceptedPaths.has(nfc(i.path)))
      : scannedItems
    const hiddenCount = scannedItems.length - items.length

    const greens  = items.filter(i => i.tier === 'green')
    const yellows = items.filter(i => i.tier === 'yellow')
    const reds    = items.filter(i => i.tier === 'red')
    const nDone   = batch.ingestedIds.size

    const tierPill = (label, count, cls) => count > 0
      ? `<span class="batch-tier-pill batch-tier-${cls}">${count} ${label}</span>` : ''

    const allRows = items.map(item => _batchRow(item)).join('')

    // Auto-Ingest All covers green + yellow — yellows are frequently good
    // enough to trust (Ryan, 2026-07-16: "the user may be just fine with
    // blank track titles"). Red stays manual — those are missing artist or
    // date entirely, a real gap worth a human look before it lands in the
    // library.
    const autoIngestPending = items.filter(i =>
      (i.tier === 'green' || i.tier === 'yellow') && !batch.ingestedIds.has(i.path))

    setMainHTML(`
      <div class="batch-shell">
        <div class="batch-header">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <h2 style="margin:0">Batch Import</h2>
            <span class="batch-dir-label">${esc(r.source_dir)}</span>
            <button class="btn btn-ghost btn-sm" id="batch-rescan-btn">↺ New Scan</button>
          </div>
          ${hiddenCount > 0 ? `<p class="batch-subtitle" style="margin:6px 0 0">${hiddenCount} scanned folder${hiddenCount === 1 ? '' : 's'} not shown, rejected or still pending in Listening Quality.</p>` : ''}
          <div class="batch-behavior-row">
            <label class="batch-behavior-label" for="batch-behavior-select">File handling</label>
            <select id="batch-behavior-select">
              <option value="move" ${batch.behavior !== 'copy' ? 'selected' : ''}>Move into library (source removed)</option>
              <option value="copy" ${batch.behavior === 'copy' ? 'selected' : ''}>Copy into library (keep source)</option>
            </select>
          </div>
          <div class="batch-tier-pills" style="margin-top:10px">
            ${tierPill('green', greens.length, 'green')}
            ${tierPill('yellow', yellows.length, 'yellow')}
            ${tierPill('red', reds.length, 'red')}
            ${nDone > 0 ? `<span class="batch-tier-pill batch-tier-done">${nDone} ingested</span>` : ''}
            ${autoIngestPending.length > 0
              ? `<button class="btn btn-primary btn-sm" id="batch-ingest-all-btn" style="margin-left:8px">
                   ⇉ Auto-Ingest All Green + Yellow (${autoIngestPending.length})
                 </button>`
              : ''}
            <span class="batch-tier-pill batch-tier-total">${items.length} total</span>
          </div>
        </div>
        <div class="batch-list">${allRows}</div>
        ${items.length === 0 ? `<div class="empty-state">No accepted recordings to review. <a href="#/ingest">Back to Listening Quality</a></div>` : ''}
      </div>`)

    // ── Events ──────────────────────────────────────────────────────────────

    document.getElementById('batch-behavior-select')?.addEventListener('change', async e => {
      batch.behavior = e.target.value
      try { await API.preferences.update({ ingest_file_behavior: batch.behavior }) } catch (_) {}
    })

    document.getElementById('batch-rescan-btn')?.addEventListener('click', () => {
      // Explicit "start over" — restart the whole unified flow (source picker
      // -> Listening Quality) rather than silently reusing this directory
      // (that's what returning to this page already does).
      batch.sourceDir = null
      batch.results = null
      batch.acceptedPaths = null
      window.location.hash = '#/ingest'
    })

    // Ingest All Green + Yellow — red stays manual (missing artist/date entirely).
    document.getElementById('batch-ingest-all-btn')?.addEventListener('click', async () => {
      const btn = document.getElementById('batch-ingest-all-btn')
      const pending = items.filter(i =>
        (i.tier === 'green' || i.tier === 'yellow') && !batch.ingestedIds.has(i.path))
      if (!pending.length) return
      btn.disabled = true

      for (let idx = 0; idx < pending.length; idx++) {
        const item = pending[idx]
        btn.textContent = `⏳ ${idx + 1} / ${pending.length}`

        // Update the row's status inline
        const sid = 'batch-status-' + item.path.replace(/[^a-zA-Z0-9]/g,'_')
        const statusEl = document.getElementById(sid)
        if (statusEl) statusEl.textContent = '⏳'
        const rowBtn = mainContent.querySelector(`.batch-ingest-btn[data-path="${CSS.escape(item.path)}"]`)
        if (rowBtn) { rowBtn.disabled = true; rowBtn.textContent = '⏳' }

        try {
          const recId = await _batchIngestOne(item)
          batch.ingestedIds.set(item.path, recId)
          if (statusEl) statusEl.textContent = 'Done'
          if (rowBtn) rowBtn.textContent = 'Done'
        } catch (err) {
          if (statusEl) statusEl.textContent = 'Failed'
          if (rowBtn) { rowBtn.disabled = false; rowBtn.textContent = 'Auto-Ingest' }
          console.error('Bulk ingest failed for', item.name, err)
          // Continue to next item rather than aborting the whole run
        }
      }

      // Final re-render to show all ingested state cleanly
      renderBatchResultsView()
      loadArtistList()  // refresh sidebar — new artists/counts from this run
    })

    // Expand/collapse
    mainContent.querySelectorAll('.batch-expand-btn').forEach(btn => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation()
        const path = btn.dataset.path
        if (batch.expandedPaths.has(path)) batch.expandedPaths.delete(path)
        else batch.expandedPaths.add(path)
        renderBatchResultsView()
      })
    })

    // Auto-Ingest — available on every row now, regardless of score
    mainContent.querySelectorAll('.batch-ingest-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const path = btn.dataset.path
        const item = r.items.find(i => i.path === path)
        if (!item) return
        btn.disabled = true
        btn.textContent = '⏳ Ingesting…'
        try {
          const recId = await _batchIngestOne(item)
          batch.ingestedIds.set(path, recId)
          renderBatchResultsView()  // re-render only on success (shows ✓ Ingested + View →)
          loadArtistList()          // refresh sidebar — may be a new artist
        } catch (err) {
          btn.disabled = false
          btn.textContent = 'Auto-Ingest'
          const msg = err.message || 'Unknown error'
          // Show inline (ID now uses raw path — no esc() mismatch)
          const sid = 'batch-status-' + path.replace(/[^a-zA-Z0-9]/g,'_')
          const el  = document.getElementById(sid)
          if (el) el.textContent = msg
          // Always alert as fallback so errors are never silently swallowed
          else alert('Ingest failed: ' + msg)
          console.error('Ingest failed:', err)
        }
      })
    })

    // Review (any tier): open the same wizard used by Add Recording, pre-scanned
    mainContent.querySelectorAll('.batch-review-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const path = btn.dataset.path
        const item = r.items.find(i => i.path === path)
        if (item) _batchOpenReview(item)
      })
    })
  }

  // Direct ingest of a single item (green path — no wizard)
  async function _batchIngestOne(item) {
    const scan = await API.recordings.scan(item.path)
    const e    = item.extracted

    const tracks = buildIngestTracks(scan)

    // /api/ingest/confirm returns a job id immediately — the actual copy + DB
    // work runs in the background. Poll it to completion so we never report
    // "ingested" (or silently do nothing) before the job has actually finished.
    const { job_id } = await API.ingest.confirm({
      source_folder_path: item.path,
      artist_name:        e.artist,
      start_year:         e.year,
      start_month:        e.month,
      start_day:          e.day,
      venue_name:         e.venue   || null,
      city:               e.city    || null,
      state:              e.state   || null,
      country:            e.country || null,
      source:             e.source  || null,
      lineage:            e.lineage || null,
      is_complete:        true,
      behavior:           batch.behavior || 'move',   // synced with the shared preference
      info_file_content:  scan.info_file_content || null,
      fingerprints:       scan.fingerprints || [],
      tracks,
    })
    const result = await pollConfirmJob(job_id)
    return result.recording_id
  }

  // Open ingest wizard pre-scanned (yellow / red / manual green)
  // Scan FIRST, then set state, then navigate — so renderIngestView's reset guard
  // sees step='review' + non-null scan and doesn't wipe everything.
  async function _batchOpenReview(item) {
    // Show loading on the button while we scan
    const btn = mainContent.querySelector(`.batch-review-btn[data-path="${CSS.escape(item.path)}"]`)
    if (btn) { btn.disabled = true; btn.textContent = '⏳' }

    try {
      const scan        = await API.recordings.scan(item.path)
      ingest.scan       = scan
      ingest.step       = 'review'
      ingest.folderPath = item.path
      ingest.form       = {}
      ingest.tracks     = []
      ingest.fromBatch  = true    // drives the back-link + post-submit redirect
      ingest._resume    = true   // one-shot: tell renderIngestView to resume here
      window.location.hash = '#/ingest'
    } catch (err) {
      if (btn) { btn.disabled = false; btn.textContent = 'Review →' }
      console.error('Batch review scan failed:', err)
      // Show inline error
      const sid = 'batch-status-' + item.path.replace(/[^a-zA-Z0-9]/g,'_')
      const el  = document.getElementById(sid)
      if (el) el.textContent = 'Scan failed'
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  // Unified ingestion flow (2026-07-30)
  //
  //   Source  →  Listening Quality triage  →  Metadata review  →  Ingest
  //
  // One entry point for single-show and bulk. The backend resolves a folder to
  // its shows either way (utils/ingest.py::resolve_shows_in_dir), so "batch"
  // and "individual" are the same screens — a bulk run just has more cards.
  // ══════════════════════════════════════════════════════════════════════════

  // Server-side preferences snapshot (config.IMPORT_DIR, ingest_file_behavior…).
  // Module-scoped and cached: several views need `import_dir`, it doesn't change
  // within a session, and there is deliberately NO bare `prefs` global — the
  // only other `prefs` in this file is a local inside renderSettingsPage().
  let appPrefs = null
  async function getPrefs() {
    if (appPrefs) return appPrefs
    try { appPrefs = await API.preferences.get() } catch (_) { appPrefs = {} }
    return appPrefs
  }

  // Listening Quality triage state.
  const lq = {
    sourceDir: null,
    rows:      [],          // serialized staging rows (+ health/extracted)
    jobId:     null,
    progress:  null,        // { done, total, current } while analysing
    // folder_path → 'lq' | 'meta'. A Map rather than a Set since 2026-08-02:
    // the card now has two tabs, so "open" is no longer a boolean but a
    // question of WHICH panel. Absent = collapsed. One panel at a time.
    expanded:  new Map(),
    features:  new Map(),   // folder_path → features+interpretation, lazily fetched
    // 'all' | 'green' | 'yellow' | 'red' — which tier chip is active (Ryan,
    // 2026-08-27: "Direction A" from the ingest-redesign specimen). Filters
    // both which cards are drawn AND what "Ingest N" acts on — one flag
    // reused by both, same reasoning as _lqIngestable below: the number on
    // the button can never promise more than the loop actually delivers.
    // Ingestion mode (Ryan, 2026-08-28). 'quick' sends skip_analysis, which
    // stops the confirm job enqueueing the Librosa track analysis — measured
    // at ~57s PER TRACK on a 24/96 source, roughly 16 minutes for an 8-track
    // show, against ~3 seconds for the listening-quality pass everyone assumes
    // is the expensive one. Quick Add still copies, catalogs, verifies
    // checksums and carries the quality score across; only waveform/BPM/
    // spectral are deferred, and Re-Analyze fills those in later against the
    // same rows. Persisted like the other display/behaviour preferences.
    mode: localStorage.getItem('fluxIngestMode') === 'full' ? 'full' : 'quick',
    // Compact mode — one scannable row per recording instead of a full metrics
    // card (Ryan, 2026-08-28; Direction A "The Belt" from the ingest-redesign
    // specimen, the half that did not ship with the tier chips on 08-27).
    // A folder of hundreds is unreadable as cards, and the toggle is the whole
    // point: the dense list is for triage, the cards are for judgement.
    // Persisted like fluxTheme / fluxViewMode — a display preference the user
    // set once, not a per-visit question.
    // folder_path -> which pane is open ('lq' | 'meta' | 'fp'). One piece of
    // state for both the caret and the Tab Strip, so they cannot disagree
    // about whether a row is open. Replaced the old `expanded` Map when the
    // card and the row became one thing (2026-08-28).
    compactOpen: new Map(),
    // Per-row bulk-run state (Ryan, 2026-08-28). Until now the ONLY per-row
    // feedback during a queue run was the row vanishing when it finished, and
    // a run-note sentence above the list naming one show — so seventeen rows
    // sat looking idle while the queue worked through them.
    //   queued       folder_paths this run will reach   → button reads "Queued"
    //   activePath   the one being copied right now     → "Ingesting"
    //   copyProgress folder_path → {copied,total}, straight off the confirm
    //                job's own poll, so the inline bar is the server's count
    //                rather than a guess
    queued:       new Set(),
    activePath:   null,
    copyProgress: new Map(),
    runTotal:     0,
    // ── Values applied to EVERY recording this queue ingests ────────────────
    // One field today (Event), deliberately shaped as a bag so the next one is
    // a line here and a line in the markup rather than a new mechanism.
    //
    // The case: a folder of shows all from Telluride Bluegrass Festival. The
    // server already resolves `event_name` to an Event row, creating it if
    // needed (_do_confirm step 3.5), so the queue only has to carry the string.
    // Uncommon enough that the whole area stays collapsed until asked for.
    applyAll:     { event: '' },
    applyAllOpen: false,
    // Which run each lq.log entry belongs to. Bumped at the start of every
    // bulk run so the completion summary can describe one run rather than
    // every ingest since the folder was scanned.
    runSeq:       0,
    // Set only by runIngestQueue() when a bulk run ends. Re-entering Add
    // Recordings with this set starts fresh instead of redisplaying a job
    // that is over.
    jobFinished:  false,
    // Queue-ingest state. `cancel` is checked between shows; `activeJob` is the
    // in-flight confirm job so Cancel can also stop the current copy.
    running:   false,
    cancel:    false,
    activeJob: null,
    log:       [],          // [{name, status, recording_id|error}]
    // Why the run stopped, when it stopped badly. Rendered as a banner above
    // the cards. Before 2026-08-25 a failed poll or a failed job produced
    // nothing at all on screen — see pollAnalysis().
    error:     null,
  }

  function renderIngestView() {
    setActiveNav('ingest')
    setActiveArtist(null)
    setNavCurrent('Add Recording')
    // Fresh navigation always starts at the source picker. The exception is
    // Metadata review opening a pre-scanned folder, which sets a one-shot
    // _resume flag so the in-progress review isn't wiped.
    //
    // A FINISHED run is not resumable (Ryan, 2026-08-28). The old guard was
    // `ingest.step !== 'triage' || !lq.rows.length`, which resets when a run
    // drained the queue to empty — but a run that ended with any errored or
    // un-ingestable row left `lq.rows` non-empty, so coming back to Add
    // Recordings redisplayed the completed job: its done strip, its log, its
    // per-row Complete badges. `jobFinished` is set by runIngestQueue() and by
    // nothing else, so a single-card ingest still leaves the rest of the batch
    // resumable, which is the case the old guard was protecting.
    // `lq.running` is the belt to jobFinished's braces: a run in flight is
    // never resettable, whatever any other flag says.
    const resumable = ingest.step === 'triage' && lq.rows.length
                      && (lq.running || !lq.jobFinished)
    if (ingest._resume) {
      ingest._resume = false
    } else if (!resumable) {
      resetIngestState()
      _lqReset()
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
      case 'triage':  show(renderTriageView);    break
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
    ingest.aiResult   = null
    ingest.fromBatch  = false
    ingest.fromTriage = false
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
          // A finished single add: the queue if there is still one to return
          // to, otherwise the picker.
          ingest.step = lq.rows.length ? 'triage' : 'source'
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
  // Rebuilt 2026-08-02. The navigator IS the page (Ryan's call). What was here
  // before showed the same path twice — once in a text input, once in the
  // breadcrumbs — behind three buttons with overlapping jobs: Browse (toggle a
  // panel that was already open), Analyze (top right), and Use this folder
  // (which also analysed). Now there is one location, shown in the breadcrumbs,
  // and one primary action in the footer.
  //
  // Reworked again 2026-08-22 (Ryan) to read as a standard navigation pane
  // rather than a port of the standalone quality tool:
  //   - The "Jump to" shortcut row is gone. A single Browse button opens
  //     PyWebView's native folder dialog (`pick_folder()`, defined in run.py
  //     since the app's early days but never wired to anything) — falls back
  //     to "Type a path" in headless/server mode, where that API is absent.
  //   - The "audio" tag and the static ▸/· marker are gone; the marker was
  //     never clickable and read as an expand caret it wasn't.
  //   - Each row now shows the REAL thing: a caret that expands/collapses that
  //     folder's children in place, plus a subfolder count and a size column.
  //     Both are shallow (that folder's direct contents only) — see
  //     `_probe_folder`'s docstring for why a recursive walk is off the table.
  async function renderIngestSource() {
    // Title now matches the sidebar button that gets you here. It previously
    // said "Listening Quality" — the name of the engine, not the task.
    setActiveNav('ingest')
    setNavCurrent('Add Recordings')
    // "Trellis" (Ryan, 2026-08-23) — the NAS folder was renamed from
    // "Flux Audio"; mirrors config.py's IMPORT_DIR default. Only a fallback —
    // getPrefs().import_dir wins whenever the backend actually has one.
    const defaultDir = (await getPrefs()).import_dir
                       || '/Volumes/music/Trellis/Download'

    // No explanatory paragraph (Ryan, 2026-08-28). The page lists the folder
    // and offers a Browse button; a sentence telling you that a folder is a
    // folder is not carrying its weight.
    setMainHTML(`
      <div class="lq-wrap">
        <h1 class="lq-h1">Add Recordings</h1>
        <div id="lq-picker" class="lq-picker"></div>
        <div id="lq-msg"></div>
      </div>`)

    const pickerEl = document.getElementById('lq-picker')
    const msgEl    = document.getElementById('lq-msg')
    const say = t => { msgEl.innerHTML = t ? `<div class="lq-err">${esc(t)}</div>` : '' }

    // Where the navigator currently is. Was previously read back out of the
    // text input on every action, which is why the two could drift apart.
    let here = lq.sourceDir || defaultDir
    let busy = false

    // Browsing a NAS folder is not instant even after the scandir rework, and
    // the old page gave no sign anything was happening — hence "I click Browse
    // and nothing happens". Paint the destination and a spinner immediately, so
    // the click is always acknowledged before the network is.
    function paintLoading(path) {
      pickerEl.innerHTML = `
        <div class="lq-nav-loading">
          <span class="lq-spin"></span>
          <span>Reading <code>${esc(path)}</code>…</span>
        </div>`
    }

    async function openPicker(path) {
      if (busy) return
      const target = path || here
      busy = true
      paintLoading(target)
      // A timeout, because "the spinner never stopped" is not an acceptable
      // failure mode however fast the server usually is (Ryan, 2026-08-28).
      // The underlying cause is fixed server-side, but a folder on a sleeping
      // NAS can still take longer than anyone will wait, and a stuck spinner
      // tells the user nothing and offers them nothing.
      let j
      try {
        j = await Promise.race([
          API.quality.browse(target),
          new Promise((_, reject) => setTimeout(
            () => reject(new Error('That folder is taking too long to read. '
                                 + 'It may be very large, or the drive may be asleep.')),
            15000)),
        ])
      }
      catch (e) { busy = false; say(e.message); paint(null); return }
      finally { busy = false }
      if (j.error) { say(j.error); paint(null); return }
      here = j.path
      // The server climbs to the nearest surviving ancestor when a remembered
      // path has gone (routinely: ingesting an act's last show moves its folder
      // into the library and the empty-parent cleanup removes the act folder).
      // Used to say so here in red — removed 2026-08-26 (Ryan): this is
      // expected, routine behavior, not something worth alarming the user
      // about every time it happens.
      paint(j)
    }

    const fmtFolders = n => n === 1 ? '1 folder' : `${n || 0} folders`

    // One row. `depth` only controls indentation — the caret and the count/size
    // columns are the same at every level, so an expanded child looks like a
    // row, not a demotion.
    // The "In library" / "New performer" badges are GONE (Ryan, 2026-08-28).
    // "New performer" went on 2026-08-27 for adding noise without information;
    // "In library" followed for the same reason once it was the only one left.
    // A folder in the download directory is there to be added, and a badge on
    // most of the rows is wallpaper rather than a signal. The duplicate check
    // that actually matters still runs at triage, per recording, where it can
    // name the specific show — see _lqConcerns.

    function dirRowHtml(d, depth) {
      const caret = d.subdirs
        ? `<span class="lq-dir-caret" data-expand="${esc(d.path)}" role="button" tabindex="0"
                 aria-label="Expand ${esc(d.name)}">${chevronIcon()}</span>`
        : `<span class="lq-dir-caret lq-dir-caret--spacer"></span>`
      return `
        <div class="lq-dir-row" data-depth="${depth}">
          <div class="lq-dir" data-go="${esc(d.path)}" role="button" tabindex="0"
               style="padding-left:${depth * 18}px">
            ${caret}
            <span class="nm">${esc(d.name)}</span>
            <span class="lq-dir-count">${fmtFolders(d.subdir_count)}</span>
            <span class="lq-dir-size">${fmtBytes(d.size_bytes)}</span>
          </div>
          <div class="lq-dir-children"></div>
        </div>`
    }

    // A plain file — no caret, no drill-down. Same row shape as a folder
    // (spacer, name, right-hand pair) so a mixed listing of dirs and files
    // still lines up as one table. "Contents" becomes the extension here,
    // the closest a file has to that column's meaning for a folder.
    function fileRowHtml(fl, depth) {
      return `
        <div class="lq-dir-row" data-depth="${depth}">
          <div class="lq-dir lq-file" style="padding-left:${depth * 18}px">
            <span class="lq-dir-caret lq-dir-caret--spacer"></span>
            <span class="nm">${esc(fl.name)}</span>
            <span class="lq-dir-count">${esc(fl.ext || '—')}</span>
            <span class="lq-dir-size">${fmtBytes(fl.size_bytes)}</span>
          </div>
        </div>`
    }

    // Expands/collapses one row's children IN PLACE — the folder you're
    // looking at ("here") does not change, unlike clicking the row itself.
    // Fetches via the same /browse endpoint openPicker() uses. `kids.loaded`
    // is the only cache this needs: the DOM node it's set on lives exactly as
    // long as its content is valid, and gets torn down (along with everything
    // else in .lq-dirs) the moment `paint()` draws a genuinely new listing —
    // so a second Map tracking the same lifetime would just be two sources of
    // truth for one fact.
    async function toggleExpand(caretEl) {
      const row = caretEl.closest('.lq-dir-row')
      const kids = row.querySelector('.lq-dir-children')
      const path = caretEl.dataset.expand
      const depth = Number(row.dataset.depth || 0)
      const opening = !row.classList.contains('lq-dir-row--open')
      row.classList.toggle('lq-dir-row--open', opening)
      caretEl.classList.toggle('open', opening)
      if (!opening || kids.dataset.loaded) return
      kids.innerHTML = `<div class="lq-nav-loading lq-nav-loading--sm"><span class="lq-spin"></span></div>`
      try {
        const j = await API.quality.browse(path)
        const kidsHtml = (j.dirs || []).map(d => dirRowHtml(d, depth + 1)).join('')
                       + (j.files || []).map(fl => fileRowHtml(fl, depth + 1)).join('')
        kids.innerHTML = kidsHtml
          || `<div class="lq-dir-row lq-dir--empty" style="padding-left:${(depth + 1) * 18}px">This folder is empty</div>`
        kids.dataset.loaded = '1'
      } catch (e) {
        kids.innerHTML = `<div class="lq-err lq-err--sm">Could not read that folder.</div>`
      }
    }

    // `j === null` repaints the shell after a failure so the user is not left
    // staring at a spinner that will never resolve.
    function paint(j) {
      if (!j) {
        pickerEl.innerHTML = `<div class="lq-nav-loading">
          <span>Could not read that folder.</span>
          <button class="btn btn-ghost btn-sm" data-go="${esc(here)}">Retry</button>
          <button class="btn btn-ghost btn-sm" id="lq-browse">Browse…</button>
          <button class="btn btn-ghost btn-sm" data-go="${esc(defaultDir)}">Back to Downloads</button>
          </div>`
        return
      }

      // Breadcrumbs DELETED (Ryan, 2026-08-28). They were a full path
      // navigator: every ancestor was a link, up to and including "/". Nobody
      // adds recordings from the filesystem root, and one stray click on
      // /Volumes/music landed on a 2,233-folder iTunes library that took 45
      // seconds to describe. What replaces them is what a normal app has —
      // the folder you are in, a Browse button, and an Up that stops at the
      // Download folder.
      const atRoot = !j.parent
      // Shown relative to the browsing root (the app folder), so a NAS path
      // that is longer than the row does not push the controls around.
      const base = j.nav_root || j.root
      const shown = base && j.path.startsWith(base + '/')
        ? j.path.slice(base.length + 1)
        : (j.path === base ? j.path.split('/').pop() : j.path)

      const dirsHtml  = j.dirs.map(d => dirRowHtml(d, 0)).join('')
      const filesHtml = (j.files || []).map(fl => fileRowHtml(fl, 0)).join('')
      const dirs = dirsHtml + filesHtml
        || '<div class="lq-dir-row lq-dir--empty">This folder is empty</div>'

      pickerEl.innerHTML = `
        <div class="lq-nav-addr">
          <button class="btn btn-ghost btn-sm lq-browse-btn" id="lq-browse"
                  title="Choose a folder anywhere on this computer">
            ${icon('folder-open', 'lq-browse-ic')} Browse…</button>
          <button class="lq-nav-up" ${atRoot ? 'disabled' : `data-go="${esc(j.parent)}"`}
                  title="${atRoot ? 'This is the top of your download folder'
                                  : 'Up one folder'}">${icon('arrow-left', 'lq-browse-ic')} Up</button>
          <span class="lq-nav-here" title="${esc(j.path)}">${esc(shown)}</span>
        </div>
        <div class="lq-dirs-head">
          <span class="lq-dirs-head-sp"></span>
          <span class="lq-dirs-head-nm">Name</span>
          <span class="lq-dirs-head-count">Contents</span>
          <span class="lq-dirs-head-size">Size</span>
        </div>
        <div class="lq-dirs">${dirs}</div>
        <div class="lq-pick-foot">
          <button class="btn btn-primary" data-use="${esc(j.path)}">
            Review This Folder</button>
          ${j.here_has_audio
            ? '<span>This folder holds audio, so it will be treated as one recording.</span>'
            : ''}
        </div>`
    }

    // No longer a button. Kept as the fallback for browseNative() when there
    // is no PyWebView to open a native dialog (headless / server mode), where
    // otherwise Browse would be a control that does nothing.
    function openTypePath() {
      const addr = pickerEl.querySelector('.lq-nav-addr')
      if (!addr) return
      addr.innerHTML = `<input type="text" id="lq-path" class="lq-path-input"
             spellcheck="false" value="${esc(here)}">
        <button class="btn btn-ghost btn-sm" id="lq-path-go">Go</button>`
      const inp = addr.querySelector('#lq-path')
      inp.focus(); inp.select()
      const go = () => { const v = inp.value.trim(); if (v) openPicker(v) }
      addr.querySelector('#lq-path-go').addEventListener('click', go)
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') go()
        else if (e.key === 'Escape') openPicker(here)
      })
    }

    // Browse opens PyWebView's native folder dialog — `pick_folder()` has
    // existed in run.py since early on but nothing called it. In headless/
    // server mode `window.pywebview` doesn't exist at all, so this falls back
    // to the same in-place path input Type a path already offers, rather than
    // showing a button that does nothing when clicked.
    async function browseNative() {
      const api = window.pywebview && window.pywebview.api
      if (!api || !api.pick_folder) { openTypePath(); return }
      let picked
      try { picked = await api.pick_folder() }
      catch (e) { say('Could not open the folder dialog: ' + e.message); return }
      if (picked) openPicker(picked)
    }

    pickerEl.addEventListener('click', e => {
      if (e.target.closest('#lq-browse')) { browseNative(); return }
      const expand = e.target.closest('[data-expand]')
      if (expand) { toggleExpand(expand); return }
      const go  = e.target.closest('[data-go]')
      const use = e.target.closest('[data-use]')
      if (go) openPicker(go.dataset.go)
      else if (use) startAnalysis(use.dataset.use, false)
    })
    // Folder rows and expand carets are real controls, so they answer the
    // keyboard too.
    pickerEl.addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return
      const caret = e.target.closest('.lq-dir-caret[data-expand]')
      if (caret) { e.preventDefault(); toggleExpand(caret); return }
      const row = e.target.closest('.lq-dir[data-go]')
      if (row) { e.preventDefault(); openPicker(row.dataset.go) }
    })

    openPicker(here)
  }

  async function startAnalysis(sourceDir, reanalyze) {
    const statusEl = document.getElementById('lq-status')
    if (statusEl) statusEl.innerHTML = `<div class="empty-state">Resolving <code>${esc(sourceDir)}</code>…</div>`
    try {
      const res = await API.quality.analyze(sourceDir, reanalyze)
      _lqReset()                       // a new scan is a new session, wholesale
      lq.sourceDir = res.source_dir
      lq.jobId     = res.job_id
      // Placeholder rows so every show is on screen immediately — analysis
      // fills them in at roughly 2s each rather than showing a blank wait.
      lq.rows = res.folders.map(f => ({
        folder_path: f.folder_path, name: f.name,
        triage_status: 'pending', listening_quality: null, _pending: true,
      }))
      lq.progress = { done: 0, total: res.folders.length, current: null }
      ingest.step = 'triage'
      renderTriageView()
      pollAnalysis()
    } catch (e) {
      if (statusEl) statusEl.innerHTML =
        `<div class="empty-state" style="color:var(--red)">${esc(e.message)}</div>`
    }
  }

  async function pollAnalysis() {
    const sleep = ms => new Promise(r => setTimeout(r, ms))
    while (lq.jobId) {
      await sleep(900)
      let s
      try {
        s = await API.quality.analyzeStatus(lq.jobId, lq.sourceDir)
      } catch (e) {
        // A failed poll used to `break` in silence. Every card then sat on
        // "Analysing…" with nothing said anywhere and nothing in the debug
        // drawer — exactly the stall Ryan reported on 2026-08-25. A stop we
        // cannot explain is still a stop the user has to be told about.
        lq.jobId   = null
        lq.progress = null
        lq.error   = e.message || 'Lost contact with the analysis job.'
        _retirePendingRows()
        if (ingest.step === 'triage') renderTriageView({ preserveScroll: true })
        return
      }
      _mergeAnalysisRows(s.results || [], s.ingested || [])
      lq.progress = { done: s.done, total: s.total, current: s.current }
      if (s.status !== 'running') {
        lq.jobId = null
        lq.progress = null
        // The job's own failure was likewise never shown: `error` came back on
        // the payload and nothing read it.
        if (s.status === 'error') {
          lq.error = s.error || 'The analysis job failed.'
        }
        _retirePendingRows()
        _sortAnalysisRows()
      }
      // Don't fight the user: re-render only the parts that change during a run.
      if (ingest.step === 'triage') renderTriageView({ preserveScroll: true })
    }
  }

  // Merge server rows INTO the placeholder list, keyed on folder_path.
  //
  // `lq.rows = s.results` (the old line) had two failure modes, and the second
  // one is what made the page hang:
  //   * a partial result set REPLACED the list, so every show not yet analysed
  //     vanished from the page instead of showing its placeholder — the exact
  //     thing startAnalysis() renders placeholders to avoid;
  //   * an EMPTY result set left the list untouched, so a job that finished
  //     successfully but returned no rows left the whole page on "Analysing…"
  //     forever. That is reachable whenever the scanned directory isn't the one
  //     the rows were first recorded under (fixed server-side in
  //     _adopt_into_scan) and whenever a folder is already an ingested
  //     Recording (reported separately as `ingested`).
  function _mergeAnalysisRows(results, ingested) {
    const byPath = new Map(results.map(r => [r.folder_path, r]))
    const done   = new Set(ingested)
    const seen   = new Set()

    lq.rows = lq.rows.map(row => {
      const hit = byPath.get(row.folder_path)
      if (hit) { seen.add(row.folder_path); return hit }
      // Already in the library: list_staging excludes promoted rows on purpose
      // (a row whose folder has become a Recording has nothing left to triage),
      // so the server names them instead of returning them.
      if (row._pending && done.has(row.folder_path)) {
        return { ...row, _pending: false, _ingestedElsewhere: true }
      }
      return row
    })

    // A row the server knows about that we never placed a card for — a re-scan
    // that resolved the folder differently this time.
    for (const r of results) {
      if (!seen.has(r.folder_path) &&
          !lq.rows.some(x => x.folder_path === r.folder_path)) lq.rows.push(r)
    }
  }

  // Nothing may still read "Analysing…" once the job is over. A placeholder
  // with no server row and no explanation is a real problem, so it becomes a
  // visible error card rather than a spinner that never resolves.
  function _retirePendingRows() {
    lq.rows = lq.rows.map(r => r._pending
      ? { ...r, _pending: false,
          error: 'Analysis finished without returning a result for this folder.' }
      : r)
  }

  // Best score first, un-scored and errored rows last — the same order
  // qs.list_staging() applies server-side. Applied once, at the END of the run:
  // re-sorting on every poll would shuffle cards out from under the pointer
  // while the user is reading them.
  function _sortAnalysisRows() {
    lq.rows = [...lq.rows].sort((a, b) => {
      const an = a.listening_quality == null, bn = b.listening_quality == null
      if (an !== bn) return an ? 1 : -1
      return (b.listening_quality || 0) - (a.listening_quality || 0)
    })
  }

  // ── Stage 2: Listening Quality triage ──────────────────────────────────────

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

  const _fmt1 = v => (v == null ? '—' : Number(v).toFixed(1))
  const _fmtN = (v, unit, dp) =>
    v == null ? '—' : `${Number(v).toFixed(dp == null ? 1 : dp)}${unit || ''}`

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
        <span class="lq-verdict lq-verdict--${esc(band)}">${_LQ_BAND_TEXT[band] || '—'}</span>
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
      const col = hasScale ? _stateColour(m.state) : 'var(--t1)'
      const dp  = m.dp != null ? m.dp : (m.unit === ' Hz' ? 0 : 1)
      const shown = m.abs ? Math.abs(m.value) : m.value
      return `
        <div class="rq-mrow${m.scored ? '' : ' rq-mrow--unscored'}" title="${esc(m.about || '')}">
          <span class="rq-mlabel">${esc(m.label)}${m.scored ? '' : '<span class="rq-star">*</span>'}</span>
          <span class="rq-mval" style="color:${col}">${_fmtN(shown, m.unit, dp)}</span>
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
        <div class="rq-grp-txt">${esc(g.text || '')}</div>
        ${rows.length ? `
          <button class="rq-adv-toggle" aria-expanded="false">
            <span class="rq-caret">${chevronIcon()}</span>${rows.length} metric${rows.length === 1 ? '' : 's'}
          </button>
          <div class="rq-adv">${rows.map(metricRow).join('')}
            ${rows.some(m => !m.scored) ? `<div class="rq-star-note">* measured and shown, but
              carries no weight in the score.</div>` : ''}
          </div>` : ''}
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
      : `<div class="rq-clean">No technical issues detected — no clipping, dead channel,
           phase problem or dropouts.</div>`

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

  function _lqDateStr(e) {
    if (!e) return ''
    return [e.year, e.month ? String(e.month).padStart(2, '0') : null,
            e.day ? String(e.day).padStart(2, '0') : null].filter(Boolean).join('-')
  }

  // ── Metadata Completeness panel ───────────────────────────────────────────
  // The Metadata Completeness tab body: the values the scan CONFIDENTLY infers,
  // one row per field the completeness score actually counts. Showing all nine
  // scored fields (Ryan, 2026-08-02) rather than a curated subset — a panel
  // that omitted a scored field could not explain its own number, and City /
  // State / Country stay separate rows because that is how they are scored.
  //
  // The question this panel answers at a glance is "will I have to type?", so
  // every row is either a value or an explicit "Missing", never a blank.
  // Performer and Date lead but are rendered separately below — Performer
  // because it heads the list, Date because it is graded by precision rather
  // than presence. Everything after them is a plain presence check.
  const _META_FIELDS = [
    ['venue',   'Venue'],
    ['city',    'City'],
    ['state',   'State'],
    ['country', 'Country'],
    ['source',  'Source'],
    ['lineage', 'Lineage'],
  ]

  function _metaRow(label, value, state, note) {
    // state: 'ok' | 'part' | 'missing' — drives the dot and the value colour.
    const cls = state === 'ok' ? 'ok' : state === 'part' ? 'part' : 'missing'
    return `<div class="lq-meta-row lq-meta-row--${cls}">
      <span class="lq-meta-dot"></span>
      <span class="lq-meta-k">${esc(label)}</span>
      <span class="lq-meta-v">${value ? esc(value) : 'Missing'}</span>
      <span class="lq-meta-n">${esc(note || '')}</span>
    </div>`
  }

  function _lqMetaPanel(row) {
    const x = row.extracted, h = row.health
    if (!x) {
      return `<div class="lq-meta">
        <div class="lq-meta-note">No metadata could be read from this folder.</div>
      </div>`
    }

    // Date is graded by PRECISION, not mere presence — same as the score. A
    // year-only date is real information but still costs a field, and the panel
    // has to say so or the number looks wrong.
    const prec = h?.date_precision
    const dateStr = _lqDateStr(x) || ''
    const dateState = prec === 3 ? 'ok' : prec > 0 ? 'part' : 'missing'
    const dateNote  = prec === 3 ? '' : prec === 2 ? 'no day' : prec === 1 ? 'year only' : ''

    const fields = _META_FIELDS.map(([k, label]) =>
      _metaRow(label, x[k], x[k] ? 'ok' : 'missing')).join('')

    // Track titles: N fields, one per audio file. "Real" excludes placeholders
    // ("Track 01", "d1t02", bare numbers) — see app/utils/health.py.
    const named = h?.tracks_named, tot = h?.tracks_total
    const trkState = !tot ? 'missing' : named === tot ? 'ok' : named ? 'part' : 'missing'
    const trkVal = tot ? `${named} of ${tot} named` : 'No audio tracks'
    const trkNote = tot && named < tot
      ? `${tot - named} placeholder${tot - named === 1 ? '' : 's'}` : ''

    // The actual titles, not just the count (Ryan, 2026-08-02). "16 of 16"
    // says the fields are populated; it says nothing about whether they are
    // populated WELL. "Jam >" and "Unknown > Truckin'" both count as real
    // titles and both want a human look, which a ratio can never show.
    // Placeholders are flagged individually so the eye goes straight to them.
    const titles = (x.track_titles || [])
    const titleList = titles.length ? `
      <div class="lq-titles">
        <div class="lq-titles-head">Inferred track titles</div>
        <ol class="lq-titles-list">
          ${titles.map(t => `<li class="${t.real ? '' : 'ph'}">${
            esc(t.title || '—')}${t.real ? '' : '<span class="lq-ph-tag">placeholder</span>'}</li>`).join('')}
        </ol>
      </div>` : ''

    const factors = (h?.factors || []).length
      ? `<div class="lq-meta-gaps">${h.factors.map(f =>
          `<span>${esc(f.msg)}</span>`).join('')}</div>` : ''

    return `<div class="lq-meta">
      <div class="lq-meta-note">Values the scan proposes from the FLAC tags and the
        info file. Anything marked Missing is metadata you will need to supply.</div>
      <div class="lq-meta-grid">
        ${_metaRow('Performer', x.artist, x.artist ? 'ok' : 'missing')}
        ${_metaRow('Date', dateStr, dateState, dateNote)}
        ${fields}
        ${_metaRow('Track Titles', trkVal, trkState, trkNote)}
      </div>
      ${titleList}
      ${factors}
    </div>`
  }

  // ── Fingerprints panel ─────────────────────────────────────────────────────
  // Third tab (Ryan, 2026-08-02). A checksum is the only hard evidence on this
  // card — every other reading is an estimate, this one either matches or it
  // does not. Worth knowing BEFORE ingest, which is when a damaged tape is
  // cheapest to reject.
  //
  // FFP/ST5 verify against the FLAC's own STREAMINFO signature (a header read,
  // free) and are done during triage. MD5 hashes whole files, so it waits for
  // an explicit click — see CHEAP_FP_TYPES in app/utils/checksums.py.
  const _FP_VERDICT = {
    verified:   { text: 'Verified',    cls: 'green'  },
    partial:    { text: 'Partial',     cls: 'yellow' },
    unverified: { text: 'Unverified',  cls: 'yellow' },
    mismatch:   { text: 'Failed',      cls: 'red'    },
    none:       { text: 'None',        cls: 'unknown' },
  }
  const _FP_STATUS_CLS = { match: 'ok', mismatch: 'bad', pending: 'pend',
                           unmatched: 'pend', unverified: 'pend' }
  const _FP_STATUS_TXT = { match: 'verified', mismatch: 'MISMATCH',
                           pending: 'awaiting deep check', unmatched: 'no checksum listed',
                           unverified: 'could not verify' }

  function _lqFpPanel(row) {
    const fp = row.fingerprints
    if (!fp) return `<div class="lq-fp"><div class="lq-meta-note">
      Fingerprint files were not examined for this folder.</div></div>`
    if (!fp.files.length) return `<div class="lq-fp"><div class="lq-meta-note">
      No fingerprint files found. Nothing here can be verified against —
      the audio may still be perfect, but there is no independent proof of it.
    </div></div>`

    const s = fp.summary
    const needsDeep = s.pending_deep > 0

    const files = fp.files.map(f => {
      if (f.error) return `<div class="lq-fpfile">
        <div class="lq-fpfile-head"><span class="nm">${esc(f.filename)}</span>
          <span class="lq-fp-bad">${esc(f.error)}</span></div></div>`
      const rows = f.tracks.map(t => `
        <div class="lq-fptrk lq-fptrk--${_FP_STATUS_CLS[t.status] || 'pend'}">
          <span class="nm">${esc(t.filename)}</span>
          <span class="st">${esc(_FP_STATUS_TXT[t.status] || t.status)}</span>
        </div>`).join('')
      return `
      <div class="lq-fpfile">
        <div class="lq-fpfile-head">
          <span class="nm">${esc(f.filename)}</span>
          <span class="ty">${esc((f.type || '').toUpperCase())}</span>
          <span class="ct">${f.matched_count} of ${f.entry_count} entries matched${
            f.orphan_entries ? ` · ${f.orphan_entries} listed file${
              f.orphan_entries === 1 ? '' : 's'} not present` : ''}</span>
        </div>
        <div class="lq-fptrks">${rows}</div>
      </div>`
    }).join('')

    return `<div class="lq-fp">
      <div class="lq-fp-sum">
        ${s.match ? `<span class="lq-fp-ok">${s.match} verified</span>` : ''}
        ${s.mismatch ? `<span class="lq-fp-bad">${s.mismatch} FAILED</span>` : ''}
        ${s.unmatched ? `<span class="lq-fp-pend">${s.unmatched} without a listed checksum</span>` : ''}
        ${s.pending_deep ? `<span class="lq-fp-pend">${s.pending_deep} awaiting deep check</span>` : ''}
      </div>
      ${needsDeep ? `<div class="lq-fp-deep">
        <button class="btn btn-ghost btn-sm lq-fp-verify" data-path="${esc(row.folder_path)}">
          Verify MD5 now</button>
        <span>MD5 hashes the whole file, so this reads every track off disk.
          seconds to minutes depending on the show. FFP and ST5 are already done.</span>
      </div>` : ''}
      ${files}
    </div>`
  }

  // ── Concerns — the major-issue line under the card title ───────────────────
  // Replaces the prose sound description (Ryan, 2026-08-02), which restated
  // the Sound Quality band in more words and so used the most prominent line
  // on the card to say something the pill already said. This space now carries
  // ONLY things that should stop you: a possible duplicate, a failed checksum,
  // no audio at all, a phase or clipping fault. Empty when there are none, so
  // its presence is the signal.
  function _lqConcerns(row) {
    const cs = row.concerns || []
    if (!cs.length) return ''
    return `<div class="lq-concerns">${cs.map(c => `
      <div class="lq-concern lq-concern--${c.level || 'warn'}">
        <span class="tx">${esc(c.text)}</span>
        ${c.recording_id ? `<a class="lk" href="#/recording/${c.recording_id}">View</a>` : ''}
      </div>`).join('')}</div>`
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

  // Every non-scored state must still emit all THREE right-hand cells. The row
  // is a fixed grid: one missing cell shifts the actions and caret a column to
  // the left and the table stops lining up exactly where an error row is.
  const _lqBrowBlank = () =>
    `<span class="lq-brow-band"></span><span class="lq-brow-meta">—</span>` +
    `<span class="lq-brow-fp"></span>`

  // Column header for the compact table (Ryan, 2026-08-28). Three unlabelled
  // readings in a row is a puzzle, not a table — and the first version had a
  // third number (the 0-100 Sound Quality score) sitting beside the very word
  // that already says the same thing, which is the reason the header was
  // asked for. The number is gone; the word and the metadata score are the
  // only two metrics, and the fingerprint verdict is an icon rather than a
  // third column of text.
  const _lqBrowHead = () => `
    <div class="lq-brow-head">
      <span></span>
      <span>Recording</span>
      <span class="lq-tip">Sound Quality
        <span class="lq-tipbox"><div class="tt">Sound Quality (estimated)</div>
          <div class="ab">Read from the audio itself: three tracks, two windows each.
            Validated at r = 0.55 against 113 graded recordings — informative, not
            definitive. Open a row for the measurements behind it.</div></span></span>
      <span class="lq-tip">Metadata
        <span class="lq-tipbox"><div class="tt">Metadata Completeness, out of 100</div>
          <div class="ab">How much of the show is actually described by its file tags
            and info file: performer, date, venue, track titles, lineage.</div></span></span>
      <span class="lq-tip" aria-label="Fingerprints"
        ><span class="lq-tipbox"><div class="tt">Fingerprints</div>
          <div class="ab">Whether the folder's own checksums (ffp / md5 / st5) match
            the audio that is here. The glyph below carries the verdict.</div></span></span>
      <span></span>
      <span></span>
    </div>`

  // ── Compact row — Direction A, "The Belt" (Ryan, 2026-08-28) ──────────────
  //
  // One grid row per recording: coloured spine, name, a mono facts line, the
  // band pill, the metadata score, a fingerprint glyph, and the SAME action
  // buttons as the card. Reusing `_lqActions()` verbatim is the point — the
  // compact row is a different presentation of a recording, not a second
  // feature with its own subset of things you can do to it, and every handler
  // in _wireTriage() binds by class so nothing needs re-wiring.
  //
  // The caret does NOT open a panel inside the row. It swaps that one row back
  // to its full card, because the card is already the thing that answers "why
  // this score" and a second, thinner version of it would be two places to fix
  // every future change to the metrics panel.
  function _lqCompactRow(row) {
    const done = lq.log.find(l => l.folder_path === row.folder_path)

    // Non-scoreable states get the same row shape so the list stays a table —
    // an error or a pending folder that reverted to a full-width card would
    // break the alignment exactly where the eye is scanning fastest.
    const spineColour =
      row.error ? 'var(--red)'
      : row._pending || row._ingestedElsewhere ? 'var(--bd-2)'
      : ({ green: 'var(--green)', yellow: 'var(--amber)', red: 'var(--red)' }[row.verdict_band]
         || 'var(--bd-2)')

    let sub, right
    if (row.error) {
      // Same treatment as a failed ingest: a marker plus a hover, not a
      // paragraph inside a table row. The analyser's messages run long
      // ("Analysis finished without returning a result for this folder") and
      // wrapped one row to three lines.
      sub   = `${_lqErrorChip(row.error, 'Could not analyze this recording')}
               <span class="lq-brow-err">Analysis failed</span>`
      right = _lqBrowBlank()
    } else if (row._ingestedElsewhere) {
      sub   = 'Already in your library, nothing to triage.'
      right = _lqBrowBlank()
    } else if (row._pending) {
      const base   = (row.folder_path || '').split('/').pop()
      const active = !!lq.progress && lq.progress.current === base
      sub = active
        ? `<span class="lq-spin"></span><span>Analyzing now…</span>`
        : 'Queued'
      right = _lqBrowBlank()
    } else {
      // Same facts, same order, same source as the card's quick-glance line
      // (_lqCard) — tracks, format, bitrate, cutoff. Not re-derived: a compact
      // row that disagreed with the card it expands into would be worse than
      // no compact row at all.
      const q = row.interp?.quick || {}, cut = row.interp?.cutoff || {}
      const n = row.extracted?.track_count
      sub = [
        [q.format, q.bit_depth ? `${q.bit_depth}-bit` : null,
         q.sample_rate_hz ? `${(q.sample_rate_hz / 1000).toFixed(1)} kHz` : null]
          .filter(Boolean).join(' ') || null,
        q.bitrate_kbps ? `${q.bitrate_kbps} kbps` : null,
        cut.khz != null ? `${cut.khz} kHz cutoff` : null,
        n != null ? `${n} track${n === 1 ? '' : 's'}` : null,
      ].filter(Boolean).map(esc).join('<span class="sep">|</span>')

      // Exactly two metrics plus the fingerprint glyph. Metadata Completeness
      // is here because Ryan uses it as his actual review trigger (2026-08-02),
      // so it may not disappear just because the row got shorter.
      const h = row.health
      right = `
        <span class="lq-brow-band">${row.verdict_band
          ? `<span class="lq-verdict lq-verdict--${row.verdict_band}">${
              _LQ_BAND_TEXT[row.verdict_band]}</span>` : ''}</span>
        <span class="lq-brow-meta batch-score--${h?.band || 'red'}">${
          esc(_metaRating(h))}</span>
        ${_lqBrowFp(row)}`
    }

    // Which pane this row has open, if any: 'lq' | 'meta' | 'fp' | undefined.
    //
    // ⚠ This line went missing in an edit and `open` silently resolved to
    // `window.open` instead — truthy, so the panel rendered, but never equal to
    // any pane key, so every pane came out empty with no tab active (Ryan,
    // 2026-08-28). `node --check` cannot see this: the identifier IS defined,
    // just not by us. Same family as tests/test_no_undefined_names.py on the
    // Python side.
    const open = lq.compactOpen.get(row.folder_path)

    return `<div class="lq-row lq-row--compact${open ? ' is-open' : ''}">
      <div class="lq-brow${row._pending ? ' lq-brow--pending' : ''}${
           lq.activePath === row.folder_path ? ' lq-brow--running' : ''}"
           data-path="${esc(row.folder_path)}">
        <span class="lq-brow-spine" style="background:${spineColour}"></span>
        <div class="lq-brow-main">
          <div class="lq-brow-name" title="${esc(row.folder_path)}">${esc(row.name)}</div>
          <div class="lq-brow-sub">${sub}</div>
          ${_lqConcerns(row)}
          ${_lqCopyBar(row)}
        </div>
        ${right}
        ${row._pending || row._ingestedElsewhere ? '<span class="lq-actions"></span>' : _lqActions(row, done)}
        ${row._pending || row._ingestedElsewhere || row.error
          ? '<span class="lq-brow-caret lq-brow-caret--spacer"></span>'
          : `<button type="button" class="lq-brow-caret" data-expand="${esc(row.folder_path)}"
                title="${open ? 'Hide the detail' : 'Show the detail for this recording'}"
                aria-expanded="${!!open}">${chevronIcon(open ? 'caret-ic--up' : 'caret-ic--down')}</button>`}
      </div>
      ${open ? _lqDetail(row, open) : ''}
    </div>`
  }

  // Fingerprint verdict as one glyph. The card gives it a whole tab with a
  // subtitle; a row has space for a colour and a tooltip, and that is enough to
  // answer the only question asked at triage speed — did the checksums match.
  function _lqBrowFp(row) {
    const v = _FP_VERDICT[row.fingerprints?.verdict || 'none']
    const colour = { green: 'var(--green)', yellow: 'var(--amber)',
                     red: 'var(--red)', unknown: 'var(--t3)' }[v.cls]
    return `<span class="lq-brow-fp lq-tip" style="color:${colour}">
      ${icon('fingerprint', 'lq-fp-ic')}
      <span class="lq-tipbox">
        <div class="tt">Fingerprints: ${esc(v.text)}</div>
        <div class="ab">${esc(_fpSub(row))}</div>
      </span></span>`
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
  function _lqDetail(row, open) {

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
        <span class="lq-mlabel">${esc(m.label)}${
          m.scored ? '' : '<span class="lq-star">*</span>'}</span>
        <span class="lq-mval" style="color:${col}">${_fmtN(shown, m.unit, dp)}</span>
        <span class="lq-mverdict">${esc(m.verdict || '')}</span>
        <span class="lq-tipbox">
          <div class="tt">${esc(m.label)}: ${esc(m.verdict || '')}</div>
          <div class="ab">${esc(m.about || '')}</div>
          ${m.scored ? '' : `<div class="ab" style="margin-top:6px;color:var(--t2)">
            Measured and shown, but carries no weight in the score.</div>`}
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
        <div class="lq-grp-txt">${esc(g.text || '')}</div>
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
      </div>` : `<div class="lq-clean"><span class="lq-dot"></span>No technical issues detected.
        no clipping, dead channel, phase problem or dropouts.</div>`

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

    return `<div class="lq-detailwrap" data-path="${esc(row.folder_path)}">
      ${_lqTabs(row, open)}
      <div class="lq-pane">
        ${open === 'lq' ? `
          <div class="lq-samp lq-samp--lead">
            <div class="lq-samp-head">
              <h4>Track Preview</h4>
              <span class="lq-samp-hint">Timestamps mark where each sample was taken</span>
              <div class="lq-trk-player" data-slot-for="${esc(row.folder_path)}"></div>
            </div>
            ${tracks}
          </div>
          ${issues}
          ${groups}
          ${otherMetrics}
          ${flags}` : ''}
        ${open === 'meta' ? _lqMetaPanel(row) : ''}
        ${open === 'fp'   ? _lqFpPanel(row)   : ''}
      </div>
    </div>`
  }

  // Tab Strip for the drill-in (rebuilt 2026-08-28).
  //
  // Was three full-width buttons, each carrying its own label, a subtitle AND
  // its value. Two problems, both Ryan's: the buttons looked nothing like any
  // other control in the app, and every value on them is now ALREADY on the row
  // directly above — so a third of the panel was spent repeating what the user
  // had just read.
  //
  // These are tabs in the sense the rest of the app uses the word (see the UI
  // lexicon's Tab Strip): navigation only, no values, one active at a time.
  function _lqTabs(row, open) {
    const tab = (key, label) => `
      <button class="lq-dtab${open === key ? ' on' : ''}" type="button"
              data-path="${esc(row.folder_path)}" data-tab="${key}"
              aria-selected="${open === key}" role="tab">${label}</button>`
    return `<div class="lq-dtabs" role="tablist">
      ${tab('lq', 'Sound Quality')}
      ${tab('meta', 'Metadata')}
      ${tab('fp', 'Fingerprints')}
    </div>`
  }

  // Subtitle states WHY a verdict is provisional, so "Unverified" never reads
  // as "we looked and found nothing" when it actually means "we have not run
  // the expensive check yet".
  function _fpSub(row) {
    const fp = row.fingerprints
    if (!fp || !fp.files.length) return 'no checksum files found'
    if (fp.summary.pending_deep) return 'MD5 needs a deep check'
    const n = fp.files.length
    return `${n} checksum file${n === 1 ? '' : 's'}`
  }

  // Actions, now INSIDE the card at top-right (Ryan, 2026-08-02), in the slot
  // the Format/Bitrate/Cutoff box used to hold. They sat outside the card
  // boundary on the theory that the card reports and the column acts — true,
  // but it cost a fixed-width gutter down the whole queue and stopped the card
  // ever being full width. Ingest / Review / Move, with Move opening a small
  // menu (Backlog | Working). Once a recording is in, its actions collapse to
  // a single View link to the finished record.
  // "Ingesting 4/12" — the confirm job's own copied/total, not a guess. Blank
  // until the first progress poll lands, so the label never flashes "0/0".
  // Only the copy phase has a file count; the others have a name instead.
  const _lqCopyText = pr =>
    (pr && pr.total && (!pr.phase || pr.phase === 'copying' || pr.phase === 'moving')
      ? ` ${pr.copied}/${pr.total}` : '')

  // The inline completion bar, rendered into BOTH views (Ryan, 2026-08-28).
  // Same markup either way: the compact row puts it under the facts line and
  // the card puts it under the quick-glance line, and one function means they
  // cannot report different numbers for the same copy.
  function _lqCopyBar(row) {
    if (lq.activePath !== row.folder_path) return ''
    const pr = lq.copyProgress.get(row.folder_path)
    return `<div class="lq-copybar" data-copybar-for="${esc(row.folder_path)}">
      <div class="lq-copybar-track"><i style="width:${_lqCopyPct(pr)}%"></i></div>
      <span class="lq-copybar-n">${esc(_lqPhaseText(pr))}</span>
    </div>`
  }

  // What the bar says, and how full it is.
  //
  // Only the copy phase can report real progress (copied/total). The rest are
  // sequential steps of known order, so the bar advances by STEP rather than
  // pretending to know a percentage it cannot have — a bar frozen at 100%
  // through a long checksum pass is exactly the "is it stuck?" the phases were
  // added to answer.
  // Every phase EXCEPT the copy needs an entry here. A missing key falls
  // through to the copy branch, which still holds the finished copy's
  // copied/total and so returns 85 — the bar visibly ran backwards from
  // "checksums" (94) into "signals", which is precisely the "did it restart?"
  // the phase bar exists to prevent. Keep in step with PHASES in api/ingest.py.
  const _LQ_PHASE_PCT = { resolving: 4, copying: null, moving: null,
                          cataloging: 88, checksums: 94, signals: 96,
                          saving: 99, done: 100 }
  function _lqCopyPct(pr) {
    if (!pr) return 2
    const fixed = _LQ_PHASE_PCT[pr.phase]
    if (fixed != null) return fixed
    // Copy phase: 8-85% of the bar, so it never claims to be finished while
    // cataloging and checksums are still to come.
    return pr.total ? 8 + Math.round(77 * pr.copied / pr.total) : 8
  }
  function _lqPhaseText(pr) {
    if (!pr) return 'Starting…'
    if ((pr.phase === 'copying' || pr.phase === 'moving') && pr.total) {
      return `${pr.label || 'Copying'}: ${pr.copied}/${pr.total} files`
    }
    return pr.label || 'Working…'
  }

  function _lqActions(row, done) {
    // `done` is this session's log; `row.recording_id` is the DURABLE fact,
    // written onto the staging row when the ingest committed. Checking only
    // the log meant a folder ingested in an earlier session was offered for
    // ingest all over again (2026-07-31).
    const recId = done?.status === 'done' ? done.recording_id : row.recording_id
    if (recId) {
      // "Complete" while a bulk run is still going, "Ingested" once it is over
      // (Ryan, 2026-08-28). The words are different on purpose: during the run
      // it is the third state of a progression the user is watching; after it,
      // it is a durable fact about the row.
      return `<div class="lq-actions">
        <span class="lq-act-done">${lq.running ? 'Complete' : 'Ingested'}</span>
        <a class="lq-act lq-act--view" href="#/recording/${recId}">View</a>
      </div>`
    }
    // ── Bulk-run states. Checked BEFORE the disk/error states below: a row the
    // queue is actively copying is not offering buttons, so nothing here can be
    // clicked into a second confirm job for the same folder.
    if (lq.activePath === row.folder_path) {
      const pr = lq.copyProgress.get(row.folder_path)
      return `<div class="lq-actions">
        <span class="lq-act-running" data-progress-for="${esc(row.folder_path)}">
          <span class="lq-spin"></span>Ingesting${_lqCopyText(pr)}</span>
      </div>`
    }
    if (lq.running && lq.queued.has(row.folder_path)) {
      return `<div class="lq-actions"><span class="lq-act-queued">Queued</span></div>`
    }
    // Folder is gone from disk but the analysis row remains — nothing here can
    // act on it, so say so instead of offering buttons that must fail.
    if (row.exists === false) {
      return `<div class="lq-actions">
        <span class="lq-act-cancelled" title="${esc(row.folder_path)}">Folder moved</span>
      </div>`
    }
    if (done?.status === 'error') {
      return `<div class="lq-actions">
        ${_lqErrorChip(done.error || 'The ingest failed.', 'Could not add this recording')}
        <button class="lq-act lq-act--ingest" data-path="${esc(row.folder_path)}">Retry</button>
        ${_lqReviewMove(row)}
      </div>`
    }
    if (done?.status === 'cancelled') {
      return `<div class="lq-actions">
        <span class="lq-act-cancelled">Cancelled</span>
        <button class="lq-act lq-act--ingest" data-path="${esc(row.folder_path)}">Ingest</button>
      </div>`
    }
    return `<div class="lq-actions">
      <button class="lq-act lq-act--ingest" data-path="${esc(row.folder_path)}"
              title="Auto-ingest using the metadata shown">Ingest</button>
      ${_lqReviewMove(row)}
    </div>`
  }

  // Review + Move. Shared so the failure state offers exactly what the normal
  // state does — the whole point of the 2026-08-28 fix is that a failed ingest
  // must not strip away the two things that can actually resolve it.
  function _lqReviewMove(row) {
    return `
      <button class="lq-act lq-act--review" data-path="${esc(row.folder_path)}"
              title="Open the full Add Recording page">Review</button>
      <div class="lq-move-wrap">
        <button class="lq-act lq-act--move" data-path="${esc(row.folder_path)}">Move ${chevronIcon('caret-ic--down lq-act-chev')}</button>
        <div class="lq-move-menu" hidden>
          <button class="lq-move-opt" data-dest="backlog" data-path="${esc(row.folder_path)}">Backlog</button>
          <button class="lq-move-opt" data-dest="workshop" data-path="${esc(row.folder_path)}">Workshop</button>
        </div>
      </div>`
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

  function renderTriageView({ preserveScroll = false } = {}) {
    setActiveNav('ingest')
    setNavCurrent('Add Recordings')
    const scrollY = preserveScroll ? window.scrollY : 0

    // Anything still in the list is still a candidate — moving a show to
    // Backlog/Working physically removes it from the scanned folder, so the
    // queue IS the remaining work. No separate accept step to forget.
    const visibleRows = lq.rows
    const queue        = visibleRows.filter(_lqIngestable)
    const analysing    = !!lq.progress && lq.progress.done < lq.progress.total

    const progressBar = analysing ? `
      <div class="lq-progress">
        <div class="lq-progress-bar"><i style="width:${
          Math.round(100 * lq.progress.done / Math.max(1, lq.progress.total))}%"></i></div>
        <span class="lq-progress-count">${lq.progress.done}/${lq.progress.total}</span>
        ${lq.progress.current ? `<span class="lq-progress-current">${esc(lq.progress.current)}</span>` : ''}
      </div>` : ''

    // Why the run stopped, when it stopped badly. Reuses the card's own error
    // styling rather than inventing a banner class.
    const errorBar = lq.error
      ? `<div class="lq-err" style="margin:0 0 12px">${esc(lq.error)}</div>` : ''

    // The single action. Cancel replaces it mid-run rather than sitting next to
    // it, so there is never a question about which button is live. Label and
    // count both track the active tier filter — "Ingest 178 High" can never
    // promise more than the loop (which filters the same way) will do.
    const queueWord = `recording${queue.length === 1 ? '' : 's'}`
    // The note is DERIVED, not poked into the DOM. runIngestQueue() used to
    // set #lq-run-note.textContent and then immediately call renderTriageView(),
    // which re-emitted the span empty — so the sentence never survived to a
    // frame. Same for the cancel handler's "Cancelling…".
    const activeRow = lq.activePath && lq.rows.find(r => r.folder_path === lq.activePath)
    const runNote = lq.cancel
      ? 'Cancelling. Finishing the current copy safely…'
      : (activeRow ? `Ingesting ${activeRow.name}…` : '')
    const runBar = lq.running
      ? `<button class="btn btn-danger" id="lq-cancel-btn">Cancel</button>
         <span class="lq-run-note" id="lq-run-note">${esc(runNote)}</span>`
      : `<button class="btn btn-primary" id="lq-ingest-all-btn" ${queue.length ? '' : 'disabled'}>
           ⇉ Ingest ${queue.length} ${queueWord}
         </button>`

    // Three body states: the queue is genuinely empty (all done / nothing
    // found), a tier filter is hiding everything that IS left, or there are
    // cards to show.
    const cardsBody = !lq.rows.length
      ? (
        // Rows no longer drain on ingest (2026-08-30), so this is only
        // reached when literally nothing was found, or every row was Moved
        // away (the one action that still removes a row outright). Kept as
        // two distinct messages rather than collapsing to one, since "you
        // ingested everything" and "there was nothing here" are different
        // facts worth telling apart.
        lq.log.some(l => l.status === 'done')
          ? `<div class="empty-state">
               <div class="empty-title">All done</div>
               <div class="empty-sub">Everything in this folder has been added to your library.</div>
               <div style="margin-top:14px; display:flex; gap:8px; justify-content:center">
                 <button class="btn btn-primary btn-sm" id="lq-alldone-more">Add More Recordings</button>
                 <a class="btn btn-ghost btn-sm" href="#/">Browse Library</a>
               </div>
             </div>`
          : `<div class="empty-state">
               <div class="empty-title">Nothing to ingest here</div>
               <div class="empty-sub">No audio folders were found under this directory.</div>
               <div style="margin-top:14px; display:flex; gap:8px; justify-content:center">
                 <button class="btn btn-primary btn-sm" id="lq-alldone-more">Add More Recordings</button>
                 <a class="btn btn-ghost btn-sm" href="#/">Browse Library</a>
               </div>
             </div>`)
      : _lqBrowHead() + visibleRows.map(_lqCompactRow).join('')

    // ── Settings bar ─────────────────────────────────────────────────────
    // Horizontal, across the top, matching `.browse-filters` in the Browse
    // view (Ryan, 2026-08-28: "like all the other navigation elements in the
    // app"). It was a left-hand rail for one day; a vertical column of
    // settings is not a shape this app uses anywhere else, and consistency
    // beats the extra room it bought.
    //
    // Mode collapses from two labelled radio cards to a select for the same
    // reason. The cards carried a sentence of explanation each, which now
    // lives on the label's tooltip and in the option text.
    //
    // Disabled as a set while a run is going: changing the destination folder
    // or the file treatment underneath a queue that is already copying is not
    // a thing the user can mean.
    const dis = lq.running ? 'disabled' : ''
    const settingsBar = `
      <div class="lq-setbar">
        <span class="bfilter">Source folder
          <button class="lq-setbar-path" id="lq-back-btn" ${dis}
                  title="Choose another folder. Currently ${esc(lq.sourceDir || '')}">
            ${icon('folder-open', 'lq-setbar-ic')}
            <span>${esc(_lqShortPath(lq.sourceDir))}</span></button>
        </span>

        <label class="bfilter" title="What happens to each source folder once its recording is filed">Source files
          <select id="lq-behavior" ${dis}>
            <option value="move"${batch.behavior !== 'copy' ? ' selected' : ''}>Move into library</option>
            <option value="copy"${batch.behavior === 'copy' ? ' selected' : ''}>Copy, keep originals</option>
          </select>
        </label>

        <label class="bfilter" title="Quick Add files the recording with full metadata, checksums and a sound-quality score, and leaves the per-track audio analysis for later. Complete does that analysis during the ingest, which takes roughly a minute per minute of music.">Mode
          <select id="lq-mode" ${dis}>
            <option value="quick"${lq.mode !== 'full' ? ' selected' : ''}>Quick Add</option>
            <option value="full"${lq.mode === 'full' ? ' selected' : ''}>Add w Audio Analysis</option>
          </select>
        </label>

        <span class="lq-setbar-note" title="${batch.behavior === 'copy'
          ? 'Originals stay in the source folder, so a later scan will offer them again.'
          : 'The source folder is removed once the recording is filed.'}">${batch.behavior === 'copy'
          ? 'Originals stay in the source folder, so a later scan will offer them again.'
          : 'The source folder is removed once the recording is filed.'}</span>
      </div>`

    // ── Apply to all ─────────────────────────────────────────────────────
    // Collapsed to one small link until used, because this is a rare case and
    // a permanently visible empty form above the primary button would tax
    // every ordinary ingest for the sake of an occasional one. Stays open once
    // it holds a value, so a set value can never be invisible.
    const anyApplied = !!lq.applyAll.event.trim()
    const applyAllBar = !lq.rows.length ? '' : (lq.applyAllOpen || anyApplied)
      ? `<div class="lq-applyall">
           <div class="lq-applyall-head">
             <span class="lq-applyall-h">Applies to every recording below</span>
             ${anyApplied ? '' : `<button type="button" class="lq-applyall-x" id="lq-applyall-close"
                                    title="Hide">Hide</button>`}
           </div>
           <label class="bfilter">Event
             <input type="text" id="lq-apply-event" class="lq-applyall-input"
                    placeholder="e.g. Telluride Bluegrass Festival"
                    value="${esc(lq.applyAll.event)}" ${lq.running ? 'disabled' : ''}>
           </label>
           ${anyApplied ? `<button type="button" class="lq-applyall-x" id="lq-applyall-clear"
                             ${lq.running ? 'disabled' : ''}>Clear</button>` : ''}
         </div>`
      : `<button type="button" class="lq-applyall-open" id="lq-applyall-open">
           ${icon('plus', 'lq-applyall-ic')} Add a value for every recording</button>`

    setMainHTML(`
      <div class="batch-shell lq-shell">
        <div class="batch-header lq-header">
          <div>
            <h2>Review &amp; Ingest</h2>
            <p class="batch-subtitle">${lq.rows.length
              ? `${lq.rows.length} recording${lq.rows.length === 1 ? '' : 's'} found`
              : 'Nothing to review'}</p>
          </div>
        </div>

        ${settingsBar}
        ${progressBar}
        ${errorBar}
        ${applyAllBar}
        <div class="lq-runbar">${runBar}</div>
        ${_lqDoneStripHtml()}
        <div class="lq-cards lq-cards--compact" id="lq-cards">${cardsBody}</div>
      </div>`)

    if (preserveScroll) window.scrollTo(0, scrollY)
    _wireTriage()
  }

  function _wireTriage() {
    document.getElementById('lq-back-btn')?.addEventListener('click', () => {
      _lqReset()
      ingest.step = 'source'
      renderIngestSource()
    })
    document.getElementById('lq-alldone-more')?.addEventListener('click', () => {
      _lqReset()
      ingest.step = 'source'
      renderIngestSource()
    })

    document.getElementById('lq-behavior')?.addEventListener('change', e => {
      batch.behavior = e.target.value
      renderTriageView({ preserveScroll: true })   // the rail's note tracks it
    })

    // Apply-to-all. The input is read on INPUT rather than on change so the
    // value is never lost by clicking straight from the field to Ingest, and
    // it does NOT re-render on every keystroke — that would rebuild the field
    // under the cursor and drop focus.
    document.getElementById('lq-applyall-open')?.addEventListener('click', () => {
      lq.applyAllOpen = true
      renderTriageView({ preserveScroll: true })
      document.getElementById('lq-apply-event')?.focus()
    })
    document.getElementById('lq-applyall-close')?.addEventListener('click', () => {
      lq.applyAllOpen = false
      renderTriageView({ preserveScroll: true })
    })
    document.getElementById('lq-applyall-clear')?.addEventListener('click', () => {
      lq.applyAll.event = ''
      lq.applyAllOpen = false
      renderTriageView({ preserveScroll: true })
    })
    document.getElementById('lq-apply-event')?.addEventListener('input', e => {
      lq.applyAll.event = e.target.value
    })
    // Repaint once the field is left, so the button count and the Clear
    // control catch up with a value that was typed but never committed.
    document.getElementById('lq-apply-event')?.addEventListener('blur', () => {
      renderTriageView({ preserveScroll: true })
    })

    // Mode. Persisted, and read at ingest time rather than captured at render
    // time, so switching it mid-queue affects the shows not yet started.
    document.getElementById('lq-mode')?.addEventListener('change', e => {
      lq.mode = e.target.value === 'full' ? 'full' : 'quick'
      try { localStorage.setItem('fluxIngestMode', lq.mode) } catch (_) { /* private mode */ }
    })

    // The caret opens and closes the drill-in beneath the row. It no longer
    // swaps the row for a different component, so nothing above it moves.
    mainContent.querySelectorAll('.lq-brow-caret[data-expand]').forEach(btn =>
      btn.addEventListener('click', () => {
        const p = btn.dataset.expand
        if (lq.compactOpen.has(p)) lq.compactOpen.delete(p)
        else lq.compactOpen.set(p, 'lq')      // Sound Quality is the default pane
        renderTriageView({ preserveScroll: true })
      }))

    // Expand/collapse. BOTH detail panels are already in the DOM (rendered up
    // front, hidden by CSS — same as the standalone app), so this is a class
    // toggle, not a fetch.
    //
    // One panel at a time (Ryan, 2026-08-02): opening Metadata Completeness closes
    // Sound Quality, and clicking the open tab collapses the card. Cards get
    // tall fast in a multi-show queue, and the queue has to stay scannable.
    // Tab Strip inside an expanded row. `compactOpen` holds WHICH pane, so the
    // caret and the tabs share one piece of state and cannot disagree.
    mainContent.querySelectorAll('.lq-dtab').forEach(btn =>
      btn.addEventListener('click', () => {
        lq.compactOpen.set(btn.dataset.path, btn.dataset.tab)
        renderTriageView({ preserveScroll: true })
      }))

    // Move ›  → menu (Backlog | Working). One menu open at a time.
    mainContent.querySelectorAll('.lq-act--move').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation()
        const menu = btn.nextElementSibling
        const wasHidden = menu.hidden
        mainContent.querySelectorAll('.lq-move-menu').forEach(m => { m.hidden = true })
        menu.hidden = !wasHidden
      })
    })
    document.addEventListener('click', _closeMoveMenus)

    // Physically moves the folder out of the scanned directory, so it simply
    // stops being offered — no status to filter on later.
    mainContent.querySelectorAll('.lq-move-opt').forEach(btn => {
      btn.addEventListener('click', async () => {
        const p = btn.dataset.path, dest = btn.dataset.dest
        btn.disabled = true; btn.textContent = '…'
        try {
          await API.quality.move(p, dest)
          lq.rows = lq.rows.filter(r => r.folder_path !== p)
          renderTriageView({ preserveScroll: true })
        } catch (e) {
          btn.disabled = false
          btn.textContent = dest === 'backlog' ? 'Backlog' : 'Working'
          alert(`Move failed: ${e.message}`)
        }
      })
    })

    // Ingest this one now. Sets activePath so a single-row ingest gets the same
    // "Ingesting n/m" label and inline bar as one inside a bulk run — the copy
    // is identical work and there is no reason to report it two ways.
    mainContent.querySelectorAll('.lq-act--ingest').forEach(btn => {
      btn.addEventListener('click', async () => {
        const row = lq.rows.find(r => r.folder_path === btn.dataset.path)
        if (!row || lq.activePath) return
        lq.activePath = row.folder_path
        renderTriageView({ preserveScroll: true })
        await ingestOne(row)
        lq.activePath = null
        renderTriageView({ preserveScroll: true })
      })
    })

    // Review → the existing Add Recording page, pre-scanned.
    mainContent.querySelectorAll('.lq-act--review').forEach(btn => {
      btn.addEventListener('click', async () => {
        const p = btn.dataset.path
        btn.disabled = true; btn.textContent = '⏳'
        try {
          const scan = await API.recordings.scan(p)
          ingest.scan = scan; ingest.step = 'review'; ingest.folderPath = p
          ingest.form = {}; ingest.tracks = []
          // fromTriage, NOT fromBatch: fromBatch sends the back-link and the
          // post-submit redirect to '#/batch', which has no scan in this flow,
          // so it bounced to '#/ingest' and re-rendered this very form — the
          // recording having just been created, complete with an "already in
          // your library" warning (2026-07-31).
          ingest.fromBatch  = false
          ingest.fromTriage = true
          ingest._resume    = true
          // Through renderIngestStep, not renderIngestReview directly: that is
          // where the in-page Back handler is registered and the header nav
          // buttons are repainted. Calling the renderer straight left header
          // Back greyed out on the one step it was built for.
          renderIngestStep()
        } catch (e) {
          btn.disabled = false; btn.textContent = 'Review'
          alert(`Scan failed: ${e.message}`)
        }
      })
    })

    // Preview playback. Deliberately its own <audio> element, NOT the shared
    // player bar: this is a pre-ingest file that has no track ID and no queue,
    // and hijacking the persistent player for it would blow away whatever the
    // user was actually listening to.
    //
    // MOVED INLINE 2026-08-02. It used to be appended to document.body and
    // parked bottom-right, so clicking a timestamp halfway up a long queue
    // started audio in a corner of the window the user was not looking at —
    // indistinguishable from "the click did nothing, and something else began
    // playing". The element now relocates into the slot under the track that
    // was clicked. Still ONE element, so a second click stops the first: two
    // simultaneous previews is never what was wanted.
    const playAt = (folder, file, seek, slot) => {
      let el = document.getElementById('lq-preview-audio')
      if (!el) {
        el = document.createElement('audio')
        el.id = 'lq-preview-audio'
        el.controls = true
        el.className = 'lq-preview-audio'
      }
      const host = slot
        ? mainContent.querySelector(`.lq-trk-player[data-slot-for="${CSS.escape(slot)}"]`)
        : null
      if (host && el.parentElement !== host) {
        const prev = el.parentElement
        el.remove()
        if (prev && prev.classList.contains('lq-trk-player')) prev.classList.remove('on')
        host.appendChild(el)
      }
      if (host) host.classList.add('on')
      if (!el.isConnected) document.body.appendChild(el)

      // The player no longer sits under the track, so the highlight is the
      // only thing saying WHICH track is playing. It has to be reliable.
      mainContent.querySelectorAll('.lq-trk.playing').forEach(r => r.classList.remove('playing'))
      const btn = mainContent.querySelector(
        `.lq-trk [data-file="${CSS.escape(file)}"][data-slot="${CSS.escape(slot || '')}"]`)
      btn?.closest('.lq-trk')?.classList.add('playing')

      const url = `/api/stream/ingest-preview?folder=${encodeURIComponent(folder)}`
                + `&file=${encodeURIComponent(file)}`
      if (el.dataset.src !== url) { el.src = url; el.dataset.src = url }
      const go = () => { try { el.currentTime = seek } catch (_) {} ; el.play() }
      // Seeking before metadata lands silently no-ops, so wait when cold.
      if (el.readyState >= 1) go()
      else el.addEventListener('loadedmetadata', go, { once: true })
    }

    mainContent.querySelectorAll('.lq-trk-play, .lq-win').forEach(btn => {
      btn.addEventListener('click', () => {
        playAt(btn.dataset.folder, btn.dataset.file,
               parseFloat(btn.dataset.seek) || 0, btn.dataset.slot)
      })
    })

    // Deep fingerprint verification — the expensive MD5 pass, on demand.
    mainContent.querySelectorAll('.lq-fp-verify').forEach(btn => {
      btn.addEventListener('click', async () => {
        const path = btn.dataset.path
        btn.disabled = true
        btn.textContent = 'Verifying…'
        try {
          const res = await API.quality.verifyFingerprints(path)
          const row = lq.rows.find(r => r.folder_path === path)
          if (row && !res.error) { row.fingerprints = res; renderTriageView({ preserveScroll: true }) }
          else btn.textContent = res.error || 'Failed'
        } catch (e) {
          btn.disabled = false
          btn.textContent = 'Retry verification'
        }
      })
    })

    document.getElementById('lq-ingest-all-btn')?.addEventListener('click', runIngestQueue)
    document.getElementById('lq-cancel-btn')?.addEventListener('click', async () => {
      lq.cancel = true
      // Repaint so the derived run note says "Cancelling…" — writing to the
      // node directly did not survive the next render.
      renderTriageView({ preserveScroll: true })
      // Also stop the show currently mid-copy. The worker undoes its own
      // filesystem work and rolls back; earlier recordings are untouched.
      if (lq.activeJob) { try { await API.ingest.confirmCancel(lq.activeJob) } catch (_) {} }
    })
  }

  // What "Ingest all" will actually act on. One definition, used by both the
  // button's count and the loop, so the number on the button can never promise
  // more than the loop delivers.
  // Drop a successfully-ingested show from the queue entirely (Ryan,
  // 2026-08-07). It used to stay in the list wearing a "✓ Ingested" badge AND
  // holding whatever panel you had expanded before you walked into the review
  // — so returning from an ingest showed the show you just finished, still
  // open, at the top of the work you had left. The queue is the remaining
  // work; a finished show is not remaining work.
  //
  // Only on SUCCESS. Errors and cancellations stay put, because those are
  // still work to do and hiding them would hide the problem.
  //
  // Also clears the per-row caches: leaving them keyed by folder_path means a
  // re-scan of the same directory re-opens the panel you closed months ago.
  // "Added this session" strip. Cards leave the queue on success, so without
  // this the only feedback for a completed ingest would be the list quietly
  // getting shorter. Driven by lq.log, which already records every outcome.
  // Run summary. Was a count plus the last six folder names run together as
  // links — Ryan, 2026-08-28: "we do not need the concatenated list". A list of
  // folder names is also the wrong thing to show, because a folder name is the
  // one piece of a recording the ingest was busy replacing: `thile2026-06-18.
  // fob-akg481.obrien.flac24` is what the DOWNLOAD was called, not what the
  // recording IS. The result payload carries the canonical folder_name built by
  // build_folder_name(), which is the title, so that is what gets named.
  function _lqDoneStripHtml() {
    // Scoped to the CURRENT run, not the whole session. lq.log accumulates
    // across every run and every single-card ingest in a triage session, so
    // filtering it wholesale against one run's runTotal produced arithmetic
    // like "16 of 11 recordings added" — and re-reported the previous run's
    // cancellation as this one's verdict. `runSeq` stamps each log entry with
    // the run that produced it; single-card ingests get the current value too,
    // so they are counted alongside the run they happen during.
    // One entry per FOLDER, latest wins. A per-row Retry pushes a second
    // entry for a folder that already has an 'error' one, so counting raw log
    // rows made a 3-show batch report "3 of 4 added. Job completed with 1
    // error" — for a failure the user had just fixed. Map insertion order is
    // preserved, so "last added" still names the most recent one.
    const seq = lq.runSeq
    const byPath = new Map()
    for (const l of lq.log) if (l.run === seq) byPath.set(l.folder_path, l)
    const mine = [...byPath.values()]
    const done = mine.filter(l => l.status === 'done')
    const bad  = mine.filter(l => l.status === 'error')
    const cancelled = mine.filter(l => l.status === 'cancelled')
    if (!done.length && !bad.length && !cancelled.length) return ''

    // Y is everything the run set out to do, not just what it managed. A
    // single-card ingest outside a bulk run has no runTotal, so it falls back
    // to what actually happened.
    const attempted = Math.max(lq.runTotal || 0, done.length + bad.length + cancelled.length)
    const last = done[done.length - 1]

    // Only claim the job is over when it actually is. Mid-run this reads as a
    // progress line; the verdict sentence appears when nothing is still going.
    let verdict = ''
    if (!lq.running) {
      if (bad.length) {
        verdict = `<span class="lq-done-verdict is-bad">Job completed with ${
          bad.length} error${bad.length === 1 ? '' : 's'}</span>`
      } else if (cancelled.length) {
        verdict = `<span class="lq-done-verdict is-warn">Job cancelled, ${
          cancelled.length} not added</span>`
      } else {
        verdict = `<span class="lq-done-verdict is-ok">Job completed without errors</span>`
      }
    }

    return `
      <div class="lq-donestrip">
        <span class="lq-donestrip-n">${done.length} of ${attempted} recording${
          attempted === 1 ? '' : 's'} added.</span>
        ${verdict}
        ${last ? `<span class="lq-donestrip-last">Last added:
          ${last.recording_id
            ? `<a href="#/recording/${last.recording_id}">${esc(last.title || last.name)}</a>`
            : `<span>${esc(last.title || last.name)}</span>`}</span>` : ''}
      </div>`
  }

  // The ONE place triage state is cleared. There were three copies of this
  // line and they had already drifted apart — each remembered a different
  // subset of the fields, so which stale bit survived depended on which exit
  // you took. Everything added since (compactOpen, queued, copyProgress,
  // runTotal, jobFinished) would have had to be added to all three.
  function _lqReset() {
    lq.rows = []
    lq.log = []
    lq.jobId = null
    lq.progress = null
    lq.error = null
    lq.running = false
    lq.cancel = false
    lq.activeJob = null
    lq.activePath = null
    lq.runTotal = 0
    lq.jobFinished = false
    lq.applyAll = { event: '' }
    lq.applyAllOpen = false
    lq.expanded.clear()
    lq.features.clear()
    lq.compactOpen.clear()
    lq.queued.clear()
    lq.copyProgress.clear()
  }

  function _lqRemoveRow(folderPath) {
    lq.rows = lq.rows.filter(r => r.folder_path !== folderPath)
    lq.expanded.delete(folderPath)
    lq.features.delete(folderPath)
  }

  function _lqIngestable(r) {
    return !r.error
        && !r._pending
        && !r._ingestedElsewhere                  // already a Recording
        && r.exists !== false                     // folder still on disk
        && !r.recording_id                        // not already in the library
        && !lq.log.some(l => l.folder_path === r.folder_path)
  }

  function _closeMoveMenus() {
    mainContent.querySelectorAll('.lq-move-menu').forEach(m => { m.hidden = true })
  }

  // Ingest ONE recording, recording the outcome in lq.log. Shared by the
  // per-card Ingest button and the queue loop so the two can't drift apart.
  async function ingestOne(row) {
    try {
      const e = row.extracted || {}
      // Auto-ingest needs a performer name and cannot invent one. Failing here
      // with something readable beats letting the server return a bare
      // "artist_name is required" 400 from a button press.
      if (!e.artist) {
        throw new Error('No performer could be read from this folder. '
                      + 'Use Review to fill it in.')
      }
      const scan = await API.recordings.scan(row.folder_path)
      const tracks = buildIngestTracks(scan)

      const { job_id } = await API.ingest.confirm({
        source_folder_path: row.folder_path,
        artist_name: e.artist, start_year: e.year,
        start_month: e.month, start_day: e.day,
        venue_name: e.venue || null, city: e.city || null,
        state: e.state || null, country: e.country || null,
        source: e.source || null, lineage: e.lineage || null,
        is_complete: true,
        // Queue-level value applied to every recording (see lq.applyAll).
        // The server resolves the name to an Event row, creating it once and
        // reusing it for the rest of the queue.
        event_name: lq.applyAll.event.trim() || null,
        behavior: batch.behavior || 'move',
        // Quick Add. The server reads this to decide whether to enqueue the
        // Librosa track analysis; nothing else about the ingest changes.
        skip_analysis: lq.mode !== 'full',
        info_file_content: scan.info_file_content || null,
        fingerprints: scan.fingerprints || [],
        tracks,
      })
      lq.activeJob = job_id
      // The confirm job already reported copied/total on every poll and nobody
      // ever passed a callback (2026-08-28). Feeding it into copyProgress is
      // what puts a real, server-counted bar on the row instead of a spinner
      // that says only "something is happening".
      //
      // Patches the two live nodes directly rather than re-rendering: a full
      // renderTriageView() every 600ms during a copy would rebuild the whole
      // list under the pointer and close any menu the user had open.
      const result = await pollConfirmJob(job_id, (copied, total, st) => {
        lq.copyProgress.set(row.folder_path, {
          copied, total,
          phase: st && st.phase,
          label: (st && st.phase_label) || null,
          detail: (st && st.phase_detail) || null,
        })
        _lqPaintProgress(row.folder_path)
      })
      lq.activeJob = null
      lq.copyProgress.delete(row.folder_path)

      lq.log.push(result === null
        ? { folder_path: row.folder_path, name: row.name, status: 'cancelled', run: lq.runSeq }
        : { folder_path: row.folder_path, name: row.name, run: lq.runSeq,
            // The canonical name build_folder_name() produced — "Chris Thile -
            // 2026-06-18 - Telluride Bluegrass Festival" rather than the
            // download's own folder name, which is what the ingest just
            // replaced. Falls back to the folder name if an older server
            // build doesn't send it.
            title: result.folder_name || row.name,
            status: 'done', recording_id: result.recording_id })
      if (result) {
        invalidateDims()
        // Row stays put — it does not leave the queue at all (Ryan,
        // 2026-08-30: "so a person coming back after a long queue can review
        // everything, rather than just see a blank page that says all
        // done"). It renders via _lqActions()'s existing recording_id branch:
        // "Complete" while a bulk run is still going, "Ingested" + a View
        // link once it (or this single-card ingest) is done. Nothing removes
        // a row on success any more — see the matching change at the end of
        // runIngestQueue().
      }
      return result
    } catch (err) {
      lq.activeJob = null
      lq.copyProgress.delete(row.folder_path)
      lq.log.push({ folder_path: row.folder_path, name: row.name, run: lq.runSeq,
                    status: 'error', error: err.message })
      return null
    }
  }

  // In-place repaint of one row's progress. Both views carry the same two
  // hooks — the bar and the button label — so this does not care which is on
  // screen, and does nothing at all if neither is (the row scrolled out of a
  // re-render, or the user switched folders mid-copy).
  function _lqPaintProgress(folderPath) {
    const pr = lq.copyProgress.get(folderPath)
    if (!pr) return
    const bar = mainContent.querySelector(`[data-copybar-for="${CSS.escape(folderPath)}"]`)
    if (bar) {
      bar.querySelector('.lq-copybar-track i').style.width = `${_lqCopyPct(pr)}%`
      bar.querySelector('.lq-copybar-n').textContent = _lqPhaseText(pr)
    }
    const lbl = mainContent.querySelector(`[data-progress-for="${CSS.escape(folderPath)}"]`)
    if (lbl) lbl.lastChild.textContent = `Ingesting${_lqCopyText(pr)}`
  }

  // Sequential queue ingest. Sequential on purpose: parallel copies to one
  // spinning NAS volume are slower, not faster, and a serial queue makes
  // "stop after the current one" a well-defined thing to ask for.
  async function runIngestQueue() {
    lq.running = true; lq.cancel = false
    // Cleared HERE, not only in _lqReset(). Leaving it true from a previous
    // run made renderIngestView()'s guard treat a run in flight as finished
    // business: navigating to Add Recordings mid-copy called _lqReset(),
    // emptying lq.rows and lq.log and flipping lq.running to false while the
    // loop below was still iterating its captured queue — the ingests carried
    // on invisibly and every later success called _lqRemoveRow() against a
    // list that no longer held it.
    lq.jobFinished = false
    lq.runSeq += 1

    const queue = lq.rows.filter(_lqIngestable)
    // Every row the run WILL reach is marked up front, so the whole queue reads
    // "Queued" from the first frame rather than each row sitting on an Ingest
    // button that is no longer clickable but still looks like one.
    lq.queued = new Set(queue.map(r => r.folder_path))
    // Y in "X of Y added". Captured up front: rows leave lq.rows as they
    // finish, so counting them afterwards would always report X of X.
    lq.runTotal = queue.length
    lq.copyProgress.clear()
    renderTriageView({ preserveScroll: true })

    for (const row of queue) {
      if (lq.cancel) break
      lq.activePath = row.folder_path
      lq.queued.delete(row.folder_path)
      renderTriageView({ preserveScroll: true })   // → this row flips to "Ingesting"
      await ingestOne(row)
      lq.activePath = null
      renderTriageView({ preserveScroll: true })   // → and to "Complete"
    }

    // Finished rows are NOT drained (Ryan, 2026-08-30) — they stay on screen
    // reading "Ingested", each with a View link, so a long queue's cards are
    // still there to review afterwards instead of the page going straight to
    // the empty "All done" panel. _lqIngestable() already excludes anything
    // in lq.log from being offered again, so nothing here can be re-ingested.
    lq.running = false; lq.activeJob = null; lq.activePath = null
    // A cancelled run is NOT finished business. It stops with untouched rows
    // still on screen and live Ingest buttons, and marking it finished meant
    // the next navigation silently reset the session — the user came back to
    // the picker and had to re-scan every folder to reach the shows they had
    // deliberately stopped short of.
    lq.jobFinished = !lq.cancel
    lq.queued.clear(); lq.copyProgress.clear()
    invalidateDims()
    renderTriageView({ preserveScroll: true })
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
      window.fluxDebug?.refresh()   // update the debug panel's Paula section if it's already open
    } catch (e) {
      // Clear the remembered folder: if it has gone away, keeping it means the
      // next visit to this page fails the same way.
      ingest.folderPath = null
      ingest.scan = null
      statusEl.innerHTML = `
        <div style="color:var(--red); font-size:13px; margin-top:12px; padding:12px 16px; background:rgba(224,85,85,0.08); border-radius:var(--r-sm);">
          Scan failed: ${esc(e.message)}
          <div style="color:var(--t2); margin-top:6px">If this show was just ingested, its source folder was moved into the library and is no longer here.</div>
        </div>`
    }
  }

  // ── Step 2: Combined metadata + track review ──────────────────────────────

  function pick(tags, info, field) {
    return tags?.[field] || info?.[field] || ''
  }


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

  // ── AI Assist ─────────────────────────────────────────────────────────────
  // Read the form's current metadata to send to the research pass.
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
      tracks:  (ingest.tracks || []).map(t => ({
        number: t.track_number, title: t.title, duration: t.duration,
      })),
      info_file_content: ingest.scan.info_file_content || '',
    }
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

  // Apply ⇆ revert a single proposal; tracks prior value per field for revert.
  function toggleApplyProposal(p, btn) {
    ingest.aiApplied = ingest.aiApplied || {}
    if (p.field in ingest.aiApplied) {
      setFormField(p.field, ingest.aiApplied[p.field])
      delete ingest.aiApplied[p.field]
      if (btn) { btn.textContent = 'Apply'; btn.classList.remove('applied') }
    } else {
      ingest.aiApplied[p.field] = getFormField(p.field)
      setFormField(p.field, p.proposed)
      if (btn) { btn.textContent = 'Revert'; btn.classList.add('applied') }
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
    const prov = ingest.aiResult?.provenance_notes || []
    if (prov.length) { L.push('', 'Notes:'); prov.forEach(n => L.push(n)) }
    return L.join('\n')
  }

  // Tidy the model's reasoning: drop any leaked tool-call syntax, and break
  // numbered findings ("1. … 2. …") onto their own lines for readability.
  function formatAiThinking(text) {
    if (!text) return ''
    let t = String(text).split(/<\/?thinking>|<parameter\b/i)[0]
    t = t.replace(/\s+/g, ' ').trim()
    t = t.replace(/\s(\d{1,2}\.)\s/g, '\n$1 ')
    return t
  }

  // Apply the AI's researched setlist onto the track rows (human-triggered).
  function applyAiTrackTitles(titles) {
    ;(titles || []).forEach(tt => {
      const idx = (ingest.tracks || []).findIndex(t => String(t.track_number) === String(tt.number))
      if (idx >= 0 && tt.title) {
        ingest.tracks[idx].title = tt.title
        const inp = mainContent.querySelector(`.t-title[data-idx="${idx}"]`)
        if (inp) inp.value = tt.title
      }
    })
    reScore()
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
    const flagPills = TRACK_FLAGS.map(f => {
      const active = (track.flags || []).includes(f.key)
      return `<button class="flag-pill ${active ? 'active' : ''}" data-flag="${f.key}" type="button">${f.label}</button>`
    }).join('')
    // Official-release toggle — opt-in (opts.showOfficial) since View Recording
    // manages that per-track flag elsewhere; Add Recording has no other place
    // for it once the expand row goes away, so it lives here for that caller.
    const officialRow = opts.showOfficial
      ? `<div class="et-detail-field" style="margin-top:6px">
           <label class="check-label check-inline" title="Mark this track as an official release">
             <input type="checkbox" class="track-qmenu-official" ${track.is_official ? 'checked' : ''} />
             <span>Official release</span>
           </label>
         </div>`
      : ''
    const detailGrid = flagsOnly ? '' : `
      <div class="et-detail-grid2">
        <div class="et-detail-field">
          <label>Note</label>
          <textarea class="track-qmenu-note" placeholder="Add a note…">${esc(track.notes || '')}</textarea>
        </div>
        <div class="et-detail-field">
          <label>Songwriter</label>
          <input class="track-qmenu-songwriter" type="text" placeholder="Songwriter…" value="${esc(track.songwriter || '')}" />
        </div>
      </div>`
    menu.innerHTML = `
      <div class="track-qmenu-title">${esc(String(track.track_number || '').padStart(2, '0'))} · ${esc(track.title || '')}</div>
      ${detailGrid}
      <div class="track-qmenu-label">Flags</div>
      <div class="flag-pill-row track-qmenu-flags">${flagPills}</div>
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

    setTimeout(() => {
      document.addEventListener('mousedown', _trackMenuOutside)
      document.addEventListener('keydown', _trackMenuEsc)
    }, 0)
  }

  // Read-only render of a saved AI research blob (recording view AI Assist tab).
  // Same look as the interactive version, minus the Apply controls.
  // Clean, succinct AI results in the AI Assist tab — prose + simple lists, no
  // tables or colour chips. Links are neutral + theme-aware (.ai-link).
  function renderAiResults(r) {
    // Scoped to the ingest panel, NOT a bare getElementById (2026-08-28).
    // View Recording renders an #ai-results of its own, and this job polls for
    // 30 to 90 seconds: "Add & View" mid-run put that page on screen, and the
    // ingest proposals then painted into ITS AI pane, complete with Apply
    // buttons targeting form fields (#f-artist, #f-year) that do not exist
    // there — so they silently did nothing. If the review form has gone, the
    // result has nowhere to land and is dropped.
    const body = document.querySelector('#ingest-slide-panel #ai-results')
    if (!body) return

    body.innerHTML = buildAiResultsHtml(r)

    body.querySelectorAll('.ai-apply-btn').forEach(b =>
      b.addEventListener('click', () => { toggleApplyProposal(r.proposals[parseInt(b.dataset.idx)], b); reScore() }))
    document.getElementById('ai-apply-tracks')?.addEventListener('click', () => applyAiTrackTitles(r.track_titles || []))
    // No auto-apply, regardless of confidence — see renderRecAiResults above
    // for why (2026-07-20, AI Assist Refinement spec). Every proposal needs
    // an explicit click on its own Apply button.
    reScore()
  }

  // Re-score the current form state and update the AI tab's score header.
  async function reScore() {
    if (!ingest.scan) return
    const g = id => (document.getElementById(id)?.value || '').trim()
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
      const scoreEl = document.getElementById('iq-score')
      if (scoreEl) {
        // The RATING word, not the raw number (Ryan, 2026-08-13 — Low/Medium/
        // High replaced the numeric score everywhere). This still wrote
        // `h.score`, so the chip rendered "High" on first paint and then
        // silently became "82" on the first field edit.
        scoreEl.innerHTML = `Metadata <b>${esc(_metaRating(h))}</b>`
        scoreEl.className = 'iq-chip iq-chip--' + h.band
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

  // Poll a background AI job until it finishes. The synchronous call is too slow
  // (30-90s) for the webview's fetch timeout, so we start a job and poll for it.
  function pollAiJob(jobId, t0) {
    const sleep = ms => new Promise(r => setTimeout(r, ms))
    return (async function loop() {
      while (true) {
        await sleep(2000)
        const el = document.getElementById('ai-elapsed')
        if (el) el.textContent = `${Math.round((Date.now() - t0) / 1000)}s`
        let s
        try { s = await API.ingest.aiAssistStatus(jobId) }
        catch (e) { if (/unknown job/.test(e.message)) throw new Error('Job was lost (did the app restart?)'); throw e }
        if (s.status === 'done')  return s.result
        if (s.status === 'error') throw new Error(s.error)
        if (Date.now() - t0 > 5 * 60 * 1000) throw new Error('AI research timed out after 5 minutes')
      }
    })()
  }

  // Same polling pattern as pollAiJob, for the Performer page's AI Assist
  // research job (2026-07-22) — kept separate rather than parameterizing
  // pollAiJob, since the endpoint shape (performerId + jobId) differs.
  // The elapsed timer now lives with the caller (runDossier owns its own
  // interval against #pp-dossier-msg), so this only polls.
  function pollDossierJob(performerId, jobId, t0) {
    const sleep = ms => new Promise(r => setTimeout(r, ms))
    return (async function loop() {
      while (true) {
        await sleep(2000)
        let s
        try { s = await API.performers.dossierStatus(performerId, jobId) }
        catch (e) { if (/unknown job/.test(e.message)) throw new Error('Job was lost (did the app restart?)'); throw e }
        if (s.status === 'done')  return s.result
        if (s.status === 'error') throw new Error(s.error)
        if (Date.now() - t0 > 5 * 60 * 1000) throw new Error('AI Assist timed out after 5 minutes')
      }
    })()
  }

  // Switch which right-column pane is visible in the ingest review.
  /** Show one pane of the Add Recording details panel.
   *
   *  Also opens the panel if it was collapsed, syncs the shared action row,
   *  and kicks the lazy loads. `tabEl` is optional: callers that know only the
   *  pane id (startAiAssist) can leave it out and the tab is found by
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
    row.hidden = !any
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
      body.innerHTML = `<div class="rq-empty">No sound-quality analysis for this folder yet.
        It is measured during Review &amp; Ingest, and again in full once the
        recording is filed.</div>`
    }
  }

  /** The AI Assist results container.
   *
   *  Was `ensureAiPane`, which BUILT the pane and its tab the first time AI
   *  Assist ran (2026-08-28: both are now rendered up front with the rest of
   *  the panel). A tab that only appears once you have already found the
   *  button is a tab that never advertises the feature, and it meant the pane
   *  order differed depending on what you had clicked. */
  function ensureAiPane() {
    return document.getElementById('ai-results')
  }

  async function startAiAssist() {
    const btn  = document.getElementById('btn-ai-assist')
    const body = ensureAiPane()
    if (!body) return
    switchIngestPane('isp-ai')
    if (btn) { btn.disabled = true; btn.textContent = '… researching' }
    body.innerHTML = `<div class="ai-loading"><div class="loading-spinner"></div><div>Researching the web — this can take a minute or two… <span id="ai-elapsed">0s</span></div></div>`
    const t0 = Date.now()
    try {
      const { job_id } = await API.ingest.aiAssist({ folder_path: ingest.folderPath, current: collectCurrentMeta() })
      const result = await pollAiJob(job_id, t0)
      ingest.aiResult = result
      renderAiResults(result)
    } catch (e) {
      const secs = Math.round((Date.now() - t0) / 1000)
      console.error('AI Assist error after', secs, 's:', e)
      if (/no_api_key/.test(e.message)) {
        body.innerHTML = `<p class="ai-res-note">No Anthropic API key set — add one in Settings.</p>`
      } else {
        body.innerHTML = `<p class="ai-res-note" style="color:var(--red)">AI Assist failed after ${secs}s: ${esc(e.message)}</p>`
      }
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = icon('sparkles') + ' AI Assist' }
    }
  }

  /** Leave the metadata review step the way it was entered — reflects how
   *  this review was actually reached (Ryan, 2026-07-15: "scrub our back link
   *  logic for that space"). From Bulk Import's "Review →": straight back to
   *  the in-memory batch results, no rescan — speed is the whole point for a
   *  bulk reviewer working through many folders. From the triage queue: back
   *  to the queue. Otherwise: the folder step.
   *
   *  Shared by the in-page back link and the header Back button. */
  function ingestBackFromReview() {
    if (ingest.fromTriage) {
      ingest.step = 'triage'
      ingest.fromTriage = false
      history.replaceState(null, '', '#/ingest')
      renderIngestStep()
    } else if (ingest.fromBatch) {
      // renderBatchResultsView() paints the batch list directly, bypassing
      // the hash router — but window.location.hash is still '#/ingest' from
      // when we navigated in. Left uncorrected, the NEXT _batchOpenReview()
      // call sets hash to '#/ingest' again, which is a no-op (same value =>
      // no hashchange => route() never runs => renderIngestView() never fires).
      // The scan completes fine and ingest.* state is fully populated — the
      // review form just never gets painted, looking exactly like a stuck
      // hang even though nothing is hung. replaceState fixes the recorded
      // hash without triggering a redundant render (2026-07-20).
      history.replaceState(null, '', '#/batch')
      _navRewrite('#/batch')
      setInPageBack(null)          // leaving the wizard without going through route()
      renderBatchResultsView()
    } else {
      ingest.step = 'folder'
      renderIngestStep()
    }
    // The step we just landed on may no longer offer an in-page Back (the
    // triage queue and the picker both hand Back to history), and the two
    // branches above that paint directly bypass renderIngestStep's repaint.
    paintNavButtons()
  }

  function renderIngestReview() {
    const tags = ingest.scan.suggestions.from_tags
    const info = ingest.scan.suggestions.from_info_file

    // Build the track list on first load; edits survive a back-nav. Same
    // builder the two auto-ingest paths use, so the wizard shows exactly what
    // an unattended ingest would have produced.
    if (!ingest.tracks.length) {
      ingest.tracks = buildIngestTracks(ingest.scan)
    }

    // Pre-fill metadata form on first load
    const f = ingest.form
    if (!f._filled) {
      let tagYear = null, tagMonth = null, tagDay = null
      if (tags.concert_date) {
        const p  = tags.concert_date.split('-')
        tagYear  = parseInt(p[0]) || null
        tagMonth = parseInt(p[1]) || null
        tagDay   = parseInt(p[2]) || null
      }
      f.artist_name     = titleCase(pick(tags, info, 'artist')) || ''
      f.start_year      = tagYear  || info.year  || ''
      f.start_month     = tagMonth || info.month || ''
      f.start_day       = tagDay   || info.day   || ''
      f.venue_name      = pick(tags, info, 'venue') || ''
      f.venue_id        = null
      // FLAC tags take priority; info file fills only what tags didn't supply.
      f.city            = tags.city    || info.city    || ''
      f.state           = tags.state   || info.state   || ''
      f.country         = tags.country || info.country || ''
      f.source          = pick(tags, info, 'source') || ''
      f.quality         = ''
      // Bug (Ryan, 2026-08-09): lineage almost always comes from the info
      // file's "Source:"/"Lineage:" text, not a FLAC tag, but this only ever
      // read tags.lineage — every other field here already falls back to the
      // info file via pick(). The Review & Ingest card shows the inferred
      // lineage correctly (it reads row.extracted, a separate server-side
      // merge); this was Add Recording's own re-scan losing it on the way in.
      f.lineage         = pick(tags, info, 'lineage')
      f.notes           = ''
      f.end_year        = ''
      f.end_month       = ''
      f.end_day         = ''
      f.event_name      = ''
      f.event_id        = null
      f.is_official     = false
      f._filled         = true
    }

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
    const infoDate = (info.year && info.month && info.day)
      ? `${info.year}-${String(info.month).padStart(2,'0')}-${String(info.day).padStart(2,'0')}`
      : info.year ? String(info.year) : null

    const parsedFields = [
      { label: 'Artist', val: titleCase(info.artist),  action: 'apply-artist' },
      { label: 'Date',   val: infoDate,                action: 'apply-date'   },
      { label: 'Venue',  val: titleCase(info.venue),   action: 'apply-venue'  },
      { label: 'City',   val: titleCase(info.city),    action: 'apply-city'   },
      { label: 'State',  val: info.state,              action: 'apply-state'  },
      { label: 'Country',val: titleCase(info.country), action: 'apply-country'},
    ].filter(f => f.val)

    const parsedTrackCount = info.tracks?.length || 0

    // Arrow button is now LEFT of the label
    const parsedRows = parsedFields.map(f => `
      <div class="rev-parsed-row">
        <button class="btn-parsed-apply" data-action="${f.action}" data-val="${esc(f.val)}"
                data-year="${info.year||''}" data-month="${info.month||''}" data-day="${info.day||''}">${icon('arrow-left')}</button>
        <span class="rev-parsed-key">${f.label}</span>
        <span class="rev-parsed-val">${esc(f.val)}</span>
      </div>`).join('')

    // Tracks row: apply button + expandable track list
    const parsedTrackItems = (info.tracks || []).map(t =>
      `<div class="rev-parsed-track-item">${String(t.number).padStart(2,'0')}. ${esc(titleCase(t.title))}</div>`
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
          <span class="info-file-switcher-label">Info file:</span>
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
    const infoText = `${textSwitcher}<textarea class="rev-info-text rev-info-edit${infoLocked ? ' rev-info-text--locked' : ''}" id="rev-info-edit"
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
      // t.flags itself was always clean (detectTrackFlags/detect_track_flags
      // both build off a Set, which can't hold a duplicate key) — purely a
      // rendering double-count.
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
          ingest.fromTriage ? 'Back to Ingest Queue'
          : ingest.fromBatch ? 'Back to Bulk Import' : 'Back'}</a>
        <div class="ingest-topbar-line">
          <h2 class="ingest-topbar-title">Add Recording: <span class="rev-header-folder">${esc(ingest.folderPath?.split('/').pop() || '')}</span></h2>
          <!-- Metadata rating, out of the 34px bar it used to have to itself.
               Keeps the id reScore() writes to (Ryan, 2026-08-28). -->
          <span class="iq-chip iq-chip--${ingest.scan.health?.band || 'yellow'}" id="iq-score"
                title="Metadata completeness">Metadata <b>${esc(_metaRating(ingest.scan.health))}</b></span>
        </div>
      </div>
      <div class="ingest-review-shell">

        <!-- Left: metadata form + track list -->
        <div class="ingest-review-form">
          <div class="ingest-review-form-body">

            <!-- Artist with autocomplete -->
            <div class="ingest-field">
              <label>Performer <span style="color:var(--t3); font-weight:400">(the act, from the FLAC ARTIST tag)</span></label>
              <div class="artist-picker-wrap">
                <input type="text" id="f-artist" class="${paulaCls('performer')}" value="${esc(f.artist_name)}" autocomplete="off" placeholder="Search or type the act…" />
                <div class="artist-dropdown" id="f-artist-dropdown" style="display:none"></div>
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
            <div class="ingest-field-grid ingest-row-ident" style="margin-top:6px">
              <div class="ingest-field"><label>Year</label><input type="number" id="f-year" class="${paulaCls('date')}" value="${esc(f.start_year)}" min="1900" max="2099" /></div>
              <div class="ingest-field"><label>Mo</label><input type="number" id="f-month" class="${paulaCls('date')}" value="${esc(f.start_month)}" min="1" max="12" /></div>
              <div class="ingest-field"><label>Day</label><input type="number" id="f-day" class="${paulaCls('date')}" value="${esc(f.start_day)}" min="1" max="31" /></div>
              <div class="ingest-field">
                <label>Venue</label>
                <div class="venue-picker-wrap">
                  <input type="text" id="f-venue-name" class="${paulaCls('venue_name')}" value="${esc(f.venue_name)}" autocomplete="off" placeholder="Search or type venue name…" />
                  <input type="hidden" id="f-venue-id" value="${esc(String(f.venue_id || ''))}" />
                  <div class="venue-dropdown" id="f-venue-dropdown" style="display:none"></div>
                </div>
              </div>
              <div class="ingest-field">
                <label>Festival / Event</label>
                <div class="event-picker-wrap">
                  <input type="text" id="f-event-name" value="${esc(f.event_name || '')}" autocomplete="off" />
                  <input type="hidden" id="f-event-id" value="${esc(String(f.event_id || ''))}" />
                  <div class="event-dropdown" id="f-event-dropdown" style="display:none"></div>
                </div>
              </div>
            </div>
            <div id="end-date-toggle-row" style="margin-top:3px">
              <a class="field-toggle-link" id="btn-toggle-end-date" href="#">+ End date</a>
            </div>
            <div class="ingest-field-grid date-grid" id="end-date-row" style="margin-top:5px; display:none">
              <div class="ingest-field"><label>End yr</label><input type="number" id="f-end-year" value="${esc(f.end_year)}" min="1900" max="2099" /></div>
              <div class="ingest-field"><label>Mo</label><input type="number" id="f-end-month" value="${esc(f.end_month)}" min="1" max="12" /></div>
              <div class="ingest-field"><label>Day</label><input type="number" id="f-end-day" value="${esc(f.end_day)}" min="1" max="31" /></div>
            </div>

            <!-- Non-blocking: already-in-library warning for this performer+date
                 (checked once both are known — see wireDupCheck). Multiple
                 recordings per show are legitimate, so this never blocks Confirm. -->
            <div class="dup-warn" id="dup-warn" style="display:none">
              <div class="dup-warn-title">Already in your library</div>
              <div class="dup-warn-body" id="dup-warn-body"></div>
            </div>


            <!-- City / State / Country — state is narrow -->
            <div class="ingest-field-grid" style="grid-template-columns:minmax(0,1fr) 64px minmax(0,1fr); gap:10px; margin-top:6px" id="f-location-row">
              <div class="ingest-field"><label>City</label><input type="text" id="f-city" class="${paulaCls('city')}" value="${esc(f.city)}" /></div>
              <div class="ingest-field"><label>State</label><input type="text" id="f-state" class="${paulaCls('state')}" value="${esc(f.state)}" maxlength="6" /></div>
              <div class="ingest-field"><label>Country</label><input type="text" id="f-country" class="${paulaCls('country')}" value="${esc(f.country)}" /></div>
            </div>

            <!-- Quality, Source, Lineage — that order (Ryan, 2026-08-28).
                 Source carries no placeholder: "SBD, AUD, MTX…" read as a
                 value at a glance in a form whose other boxes are pre-filled,
                 and the field is not free text anyway. -->
            <div class="ingest-field-grid ingest-row-src" style="margin-top:6px">
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
              <span>Official release</span>
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
            <!-- The file-treatment control uses .bfilter, the same
                 label-plus-select idiom as the Review & Ingest settings bar
                 (Ryan, 2026-08-28) — it was the one select in the ingest flow
                 wearing its own styling. -->
            <div class="ingest-actions-left">
              <span class="bfilter">
                <label for="ingest-behavior-select">Files</label>
                <select id="ingest-behavior-select" title="What happens to the source folder once this recording is filed">
                <option value="move" ${(appPrefs?.ingest_file_behavior !== 'copy') ? 'selected' : ''}>Move into library (source removed)</option>
                <option value="copy" ${(appPrefs?.ingest_file_behavior === 'copy') ? 'selected' : ''}>Copy into library (keep source)</option>
                </select>
              </span>
            </div>
            <div class="ingest-actions-right">
              <button class="btn btn-primary" id="btn-confirm"
                      data-after="return" title="Add to library and return to the list">Add &amp; Return ↵</button>
              <button class="btn btn-ingest-secondary" id="btn-confirm-view"
                      data-after="view" title="Add to library and open the finished record">Add &amp; View →</button>
            </div>
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
        <div class="ingest-review-raw slide-panel--htabs open" id="ingest-slide-panel">
          <div class="slide-panel-main">
            <div class="slide-tabrow">
            <div class="slide-tabs" id="ingest-tab-rail">
              <button class="slide-tab active" data-ipane="isp-info">Info File</button>
              <button class="slide-tab" data-ipane="isp-quality">Quality</button>
              <button class="slide-tab" data-ipane="isp-filetags">File Tags</button>
              <button class="slide-tab" data-ipane="isp-checksums">Checksums</button>
              <button class="slide-tab slide-tab--ai" data-ipane="isp-ai">AI Assist</button>
            </div>
            <!-- Same three info-file controls, in the same order, as View
                 Recording (Ryan, 2026-08-28). Save leads and is suppressed
                 while the file is locked; the status sits between them. -->
            <div class="pane-acts" id="ingest-pane-acts">
              <span class="pane-act-status" id="info-file-save-status" data-for="isp-info"></span>
              <button class="pane-act act-suppressed" id="btn-save-info-file" data-for="isp-info" hidden disabled>Save to File</button>
              <button class="pane-act" id="btn-ingest-info-edit" data-for="isp-info">Edit File</button>
              <button class="pane-act pane-act--primary" id="btn-ai-assist" data-for="isp-ai">${icon('sparkles')} AI Assist</button>
            </div>
            </div>
            <div class="slide-panel-body" id="ingest-panes">
              <div class="slide-pane active" id="isp-info">
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
              <!-- Permanent, not built on first use (it used to be created by
                   ensureAiPane the moment AI Assist ran). A tab that appears
                   only after you have already found the button is a tab that
                   never advertises the feature. -->
              <div class="slide-pane" id="isp-ai">
                <div class="slide-pane-scroll"><div class="ai-results" id="ai-results">
                  <div class="ai-assist-hint">Research the web to verify and fill this recording's metadata.</div>
                </div></div>
              </div>
            </div>
          </div>
          <button class="slide-rail" id="ingest-slide-rail" title="Show/hide details" aria-expanded="true">Details</button>
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
      if (e.target.matches('input, textarea, select')) reScore()
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
            const titles  = (info.tracks || []).map(t => titleCase(t.title))
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
       *  Not every row does: buildIngestTracks' info-file branch leaves
       *  `filename` empty when the info file lists more tracks than there are
       *  audio files, which is common in the folders that most need checking.
       *  Stepping used to stop dead at the first such row, and if it happened
       *  to be row 1 the transport never loaded anything at all and every
       *  control was inert. Skipping them is the only behaviour that makes
       *  sense — there is nothing to play. */
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
    // Performer + Members/Guests widget.
    const addMembersWidget = createMembersWidget(ingest.form, {
      performerInput: 'f-artist', performerDropdown: 'f-artist-dropdown',
      field: 'f-members-field',
    })
    addMembersWidget.mount()
    initAddPerformerMembers(addMembersWidget)

    // End date toggle — show/hide the row; pre-fill from start date on first reveal
    ;(function () {
      const toggleBtn = document.getElementById('btn-toggle-end-date')
      const endRow    = document.getElementById('end-date-row')
      if (!toggleBtn || !endRow) return

      // If end date was already set (back-nav), show immediately
      if (ingest.form.end_year) {
        endRow.style.display = ''
        toggleBtn.textContent = '− End date'
      }

      toggleBtn.addEventListener('click', e => {
        e.preventDefault()
        const visible = endRow.style.display !== 'none'
        if (visible) {
          // Hide and clear
          endRow.style.display = 'none'
          toggleBtn.textContent = '+ End date'
          document.getElementById('f-end-year').value  = ''
          document.getElementById('f-end-month').value = ''
          document.getElementById('f-end-day').value   = ''
        } else {
          // Show and pre-fill from start date
          endRow.style.display = ''
          toggleBtn.textContent = '− End date'
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

    // Duplicate-in-library check — fires once performer + year are both
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

    document.getElementById('btn-ai-assist')?.addEventListener('click', startAiAssist)

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
        else switchIngestPane(state.ingestLastPane || 'isp-info')
      })
      // Default open on Info File (Ryan, 2026-08-28), or wherever the reviewer
      // was last — a deliberate collapse survives moving between recordings,
      // same rule as recPanelOpen on View Recording.
      _ingestQualityLoaded = false
      if (state.ingestPanelOpen === false) _ingestPanelOpen(false)
      else switchIngestPane(state.ingestLastPane || 'isp-info')
    })()

    const _submitReview = async (ev) => {
      // Which exit the user chose: 'return' (back to the ingest queue) or
      // 'view' (open the finished record).
      const after = ev.currentTarget.dataset.after || 'view'
      // Collect metadata
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
      f.is_official     = document.getElementById('f-is-official').checked
      f.source          = document.getElementById('f-source').value
      f.quality         = document.getElementById('f-quality').value.trim()
      f.lineage         = document.getElementById('f-lineage').value.trim()
      f.notes           = document.getElementById('f-notes').value.trim()

      if (!f.artist_name) { alert('Artist name is required.'); return }

      // Title is still a live input — collect its current value. Notes,
      // songwriter, flags, and official are already staged directly on
      // ingest.tracks by openTrackMenu's onChange (right-click popup).
      mainContent.querySelectorAll('.t-title').forEach(el => {
        const t = ingest.tracks[parseInt(el.dataset.idx)]; if (t) t.title = el.value.trim()
      })

      // Submit directly — the old "Confirm & Add to Library" review screen
      // is gone (Ryan, 2026-07-15: "never very useful, nothing is ever
      // something i need to change"). File-behavior (copy/move) is no longer
      // a per-add choice either — it's a standing preference (Settings ⚙,
      // right next to the Anthropic key), read silently here.
      const btn = ev.currentTarget
      const otherBtn = document.getElementById(
        btn.id === 'btn-confirm' ? 'btn-confirm-view' : 'btn-confirm')
      const btnLabel = btn.innerHTML
      const errEl = document.getElementById('review-submit-error')
      btn.disabled = true
      if (otherBtn) otherBtn.disabled = true
      btn.textContent = 'Adding to library…'
      errEl.style.display = 'none'

      // Copy/move had TWO sources of truth: the triage page's Files dropdown
      // (batch.behavior) and the saved preference read here. A user who set the
      // dropdown to Copy could still get a Move, because Review submitted
      // through this path (2026-07-31). When the review was opened from triage,
      // the dropdown the user was just looking at wins.
      let behavior = 'move'
      const behaviorSel = document.getElementById('ingest-behavior-select')
      if (ingest.fromTriage && batch.behavior) {
        behavior = batch.behavior
      } else if (behaviorSel) {
        behavior = behaviorSel.value
      } else {
        try {
          const prefs = await API.preferences.get()
          behavior = prefs.ingest_file_behavior || 'move'
        } catch (_) { /* fall back to copy */ }
      }

      const payload = {
        source_folder_path: ingest.folderPath,
        ...f,
        behavior,
        tracks: ingest.tracks,
        fingerprints: ingest.scan.fingerprints || [],
        info_file_content: ingest.scan.info_file_content || null,
        members: (f.members || []).map(m => m.name),
        guests:  (f.guests  || []).map(m => m.name),
        // AI Assist may have already been run on this draft (pre-save) — carry
        // the result along so it lands on the new recording instead of being
        // lost the moment confirm creates the row (2026-07-14 bug: it wasn't).
        ai_result: ingest.aiResult || null,
        // Was missing entirely (2026-08-28). Review is the OTHER way into
        // /api/ingest/confirm, so without this the mode set on the triage page
        // was silently ignored the moment a user clicked Review instead of
        // Ingest — same server endpoint, opposite behaviour.
        skip_analysis: lq.mode !== 'full',
      }
      // The queue-level Event applies here too — Review is the same ingest
      // with a form in front of it, and a value set for the batch must not
      // vanish because one show needed a closer look. An event typed into the
      // form itself wins, since that is the more specific statement.
      if (!payload.event_name && !payload.event_id && lq.applyAll.event.trim()) {
        payload.event_name = lq.applyAll.event.trim()
      }

      // Progress UI under the button (copy can take a while for big folders)
      const actions = btn.closest('.ingest-actions')
      let prog = document.getElementById('confirm-progress')
      if (!prog) {
        prog = document.createElement('div')
        prog.id = 'confirm-progress'
        prog.className = 'confirm-progress'
        prog.innerHTML = `<div class="confirm-progress-bar"><div class="confirm-progress-fill" id="confirm-progress-fill"></div></div>
                          <div class="confirm-progress-label" id="confirm-progress-label">Preparing…</div>`
        actions?.parentNode.insertBefore(prog, actions.nextSibling)
      }
      const fill  = document.getElementById('confirm-progress-fill')
      const label = document.getElementById('confirm-progress-label')
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
          await loadArtistList()   // new performer/venue/artist may exist
          batch.ingestedIds.set(ingest.folderPath, result.recording_id)

          // Record the outcome on the triage row so returning to the queue
          // shows "✓ Ingested / View" rather than offering to ingest a folder
          // that is already in the library — which is exactly what produced
          // the "Already in your library" warning on re-entry (2026-07-31).
          if (ingest.fromTriage) {
            const row = lq.rows.find(r => r.folder_path === ingest.folderPath)
            if (!lq.log.some(l => l.folder_path === ingest.folderPath)) {
              lq.log.push({ folder_path: ingest.folderPath,
                            name: row?.name || ingest.folderPath,
                            status: 'done', recording_id: result.recording_id })
            }
            // Gone from the queue, not merely badged — see _lqRemoveRow.
            _lqRemoveRow(ingest.folderPath)
          }

          if (after === 'view') {
            resetIngestState()
            window.location.hash = `#/recording/${result.recording_id}`
          } else if (ingest.fromTriage) {
            // Straight back to the ingest queue, which is the point of
            // "Add & Return" for someone working through a folder of shows.
            ingest.step = 'triage'
            ingest.fromTriage = false
            history.replaceState(null, '', '#/ingest')
            renderIngestStep()
          } else if (ingest.fromBatch) {
            // Legacy metadata-review path — see ingestBackFromReview's note on
            // why the recorded hash has to be corrected without re-rendering,
            // and _navRewrite's on why the nav stack has to be told about it.
            history.replaceState(null, '', '#/batch')
            _navRewrite('#/batch')
            setInPageBack(null)    // leaving the wizard without going through route()
            renderBatchResultsView()
            paintNavButtons()
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
        btn.innerHTML = btnLabel
        prog?.remove()
      }
    }

    document.getElementById('btn-confirm').addEventListener('click', _submitReview)
    document.getElementById('btn-confirm-view')?.addEventListener('click', _submitReview)

    // Resize handle
    // The DETAILS panel is the sized side now, so it can be animated open and
    // shut; the form is flexible and absorbs whatever the panel is not using.
    wireResizablePanel(
      mainContent.querySelector('.ingest-review-shell'),
      document.getElementById('ingest-slide-panel'),
      document.getElementById('rev-divider'),
      240, 300, { side: 'right' }
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
      // Refresh the sidebar (new performer/venue/artist may exist) then navigate.
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

    // Build meta string: Artist · Date · Venue
    const artist = state.selectedArtist?.name || ''
    const perfId = recData?.performance_id
    let dateStr = '', venueStr = '', sourceStr = '', performerName = ''
    if (perfId) {
      try {
        const perf = await API.performances.get(perfId)
        dateStr       = perf ? fmtDateLong(perf.start_year, perf.start_month, perf.start_day) : ''
        venueStr      = perf?.venue_name || ''
        performerName = perf?.performer  || ''
      } catch (_) {}
    }
    if (recData) {
      sourceStr = recData.source || ''
    }
    // Player bar line 2: Date · Venue (artist name is redundant here — it's
    // shown on line 3). Line 3: the artist/band name.
    const metaParts = [dateStr, venueStr].filter(Boolean)
    const meta      = metaParts.join(' · ') || sourceStr || '—'
    const recLabel  = performerName || artist || ''

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

    document.querySelectorAll('.track-row').forEach(el => {
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
    const navBack = state.navBack   // see the Performer page's identical comment
    const descText = v.bio && v.bio.trim()
    // One row per Recording at this venue (showing the performer, since a venue
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
      tabs: [
        { id: 'overview', label: 'Overview', active: true, html: `
            <div class="pp-sec">Location</div>
            <div class="vn-loc">
              <span class="vn-field"><label>City</label><span class="pp-editable vn-val ${v.city ? '' : 'pp-empty'}" id="vn-city">${v.city ? esc(v.city) : '\u2014'}</span></span>
              <span class="vn-field"><label>State / Region</label><span class="pp-editable vn-val ${v.state ? '' : 'pp-empty'}" id="vn-state">${v.state ? esc(v.state) : '\u2014'}</span></span>
              <span class="vn-field"><label>Country</label><span class="pp-editable vn-val ${v.country ? '' : 'pp-empty'}" id="vn-country">${v.country ? esc(v.country) : '\u2014'}</span></span>
            </div>

            <div class="pp-sec">Notes</div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="vn-bio" title="Click to edit">${descText ? esc(v.bio) : 'Add notes\u2026'}</div>` },
        { id: 'recordings', label: 'Recordings', count: venueRows.length,
          // showPerformer: true — a venue hosts many different acts, so the row
          // must name who played. The Performer page omits it for the reverse
          // reason.
          html: recordingsPaneHtml(venueRows, { showPerformer: true, mountId: 'rec-table-venue',
                                                empty: 'No recordings from this venue yet' }) },
        { id: 'photos', label: 'Photos', count: photoCount || null,
          html: '<div id="vn-photos"></div>' },
      ],
    }))
    wireEntityShell(mainContent, navBack)
    wireRecordingRows(mainContent)
    if (venueRows.length) wireDateAddedSort(document.getElementById('rec-table-venue'), venueRows, true)

    // Photos — the shared gallery, no fetch tile: the Wikidata bridge we use
    // runs through the Performer's MusicBrainz match, and venues have no
    // equivalent, so uploads are the only route in.
    const vnPortrait = () => {
      const el = document.getElementById('vn-portrait')
      if (!el) return
      const primary = (v.images || [])[0]
      el.innerHTML = heroPortraitHtml(v.name, primary ? API.venues.imageUrl(primary.id) : null)
    }
    createPhotoGallery({
      mountId: 'vn-photos', api: API.venues, entityId: id, images: v.images || [],
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

    onAdminClick('vn-delete', async () => {
      if (!confirm(`Delete venue "${v.name}"? This can't be undone.`)) return
      try { await API.venues.remove(id); refreshSidebar(); window.location.hash = '#/venues' }
      catch (e) { alert(e.message) }
    })
  }

  // ── Venues admin page ──────────────────────────────────────────────────────

  async function renderVenuesPage(preSelectId = null) {
    setActiveNav('venues')
    setActiveArtist(null)
    setNavCurrent('Venues')
    setLoading()

    let venues = []
    try { venues = await API.venues.list() } catch (_) {}

    setMainHTML(`
      <div class="action-bar">
        <span style="font-size:13px; font-weight:500; color:var(--t0)">Venues</span>
        <button class="btn btn-ghost btn-sm" id="btn-new-venue" style="margin-left:auto">+ New venue</button>
      </div>
      <div class="venues-shell">
        <div class="venues-list-panel">
          <div class="venues-search-bar">
            <input type="text" id="venue-search-input" style="font-size:12px" placeholder="Search…" />
          </div>
          <div class="venue-list-scroll" id="venue-list-scroll"></div>
        </div>
        <div class="venues-detail-panel" id="venues-detail-panel">
          <div class="venue-detail-empty">Select a venue to view or edit</div>
        </div>
      </div>`)

    let allVenues    = venues
    let activeId     = null

    function renderList(list) {
      const scroll = document.getElementById('venue-list-scroll')
      if (!list.length) {
        scroll.innerHTML = '<div style="padding:16px 14px; font-size:12px; color:var(--t2)">No venues found</div>'
        return
      }
      scroll.innerHTML = list.map(v => `
        <div class="venue-list-row ${v.id === activeId ? 'active' : ''}" data-id="${v.id}">
          <div>
            <div class="venue-row-name">${esc(v.name)}</div>
            <div class="venue-row-loc">${esc([v.city, v.state, v.country].filter(Boolean).join(', '))}</div>
          </div>
          <div class="venue-row-count">${v.performance_count}p</div>
        </div>`).join('')

      scroll.querySelectorAll('.venue-list-row').forEach(el => {
        el.addEventListener('click', () => {
          activeId = parseInt(el.dataset.id)
          renderList(list)       // refresh active state
          loadVenueDetail(activeId)
        })
      })
    }

    async function loadVenueDetail(id) {
      const panel = document.getElementById('venues-detail-panel')
      panel.innerHTML = '<div class="venue-detail-empty" style="color:var(--t2)">Loading…</div>'
      let v
      try { v = await API.venues.get(id) } catch (_) {
        panel.innerHTML = '<div class="venue-detail-empty">Failed to load</div>'
        return
      }

      panel.innerHTML = `
        <div style="max-width:580px">
          <h2 style="font-size:18px; font-weight:500; color:var(--t0); margin:0 0 18px">${esc(v.name)}</h2>

          <div class="rev-section-title" style="margin-bottom:12px">Venue info</div>

          <div class="ingest-field" style="margin-bottom:10px">
            <label>Name</label>
            <input type="text" id="vd-name" value="${esc(v.name)}" />
          </div>

          <div class="ingest-field-grid" style="grid-template-columns:1fr 1fr 1fr; gap:10px; margin-bottom:10px">
            <div class="ingest-field">
              <label>City</label>
              <input type="text" id="vd-city" value="${esc(v.city||'')}" />
            </div>
            <div class="ingest-field">
              <label>State / Region</label>
              <input type="text" id="vd-state" value="${esc(v.state||'')}" />
            </div>
            <div class="ingest-field">
              <label>Country</label>
              <input type="text" id="vd-country" value="${esc(v.country||'')}" />
            </div>
          </div>

          <div class="ingest-field" style="margin-bottom:18px">
            <label>Bio / notes</label>
            <textarea id="vd-bio" style="min-height:80px">${esc(v.bio||'')}</textarea>
          </div>

          <div style="display:flex; align-items:center; gap:10px; margin-bottom:28px">
            <button class="btn btn-primary btn-sm" id="vd-save">Save</button>
            <span id="vd-msg" style="font-size:11px; color:var(--t2)"></span>
            <button class="btn btn-ghost btn-sm" id="vd-delete" style="margin-left:auto; color:var(--red)">Delete</button>
          </div>

          ${v.performance_count > 0 ? `
          <div class="rev-section-title" style="margin-bottom:10px">Performances (${v.performance_count})</div>
          <div style="display:flex; flex-direction:column; gap:2px">
            ${v.performances.map(p => `
              <div style="display:flex; align-items:center; gap:12px; padding:5px 0; border-bottom:1px solid var(--bd-0); font-size:12px">
                <span style="color:var(--t2); font-family:var(--font-mono); min-width:80px">${esc(p.date)}</span>
                <a href="#/artist/${p.performer_id}" style="color:var(--t0); text-decoration:none; flex:1">${esc(p.performer)}</a>
              </div>`).join('')}
          </div>` : `<div style="font-size:12px; color:var(--t2)">No performances linked yet</div>`}
        </div>`

      document.getElementById('vd-save').addEventListener('click', async () => {
        const saveBtn = document.getElementById('vd-save')
        const msgEl   = document.getElementById('vd-msg')
        saveBtn.disabled = true
        saveBtn.textContent = 'Saving…'
        try {
          await API.venues.update(id, {
            name:    document.getElementById('vd-name').value.trim(),
            city:    document.getElementById('vd-city').value.trim()    || null,
            state:   document.getElementById('vd-state').value.trim()   || null,
            country: document.getElementById('vd-country').value.trim() || null,
            bio:     document.getElementById('vd-bio').value.trim()     || null,
          })
          // Refresh list so the name updates in the sidebar
          allVenues = await API.venues.list()
          renderList(allVenues)
          // Update panel heading too
          document.querySelector('#venues-detail-panel h2').textContent =
            document.getElementById('vd-name').value.trim()
          msgEl.textContent = 'Saved'
          setTimeout(() => { if (msgEl) msgEl.textContent = '' }, 2000)
        } catch (e) {
          msgEl.style.color = 'var(--red)'
          msgEl.textContent = 'Save failed: ' + e.message
        } finally {
          saveBtn.disabled = false
          saveBtn.textContent = 'Save'
        }
      })

      document.getElementById('vd-delete').addEventListener('click', async () => {
        if (!confirm(`Delete venue "${v.name}"? This can't be undone.`)) return
        const msgEl = document.getElementById('vd-msg')
        try {
          await API.venues.remove(id)
          allVenues = await API.venues.list()
          activeId  = null
          renderList(allVenues)
          document.getElementById('venues-detail-panel').innerHTML =
            '<div class="venue-detail-empty">Select a venue to view or edit</div>'
          _dimCache.venues = null
          if (state.expandedDims.has('venues')) _renderDimRecords('venues')
        } catch (e) {
          msgEl.style.color = 'var(--red)'
          msgEl.textContent = e.message
        }
      })
    }

    // Search filter
    document.getElementById('venue-search-input').addEventListener('input', e => {
      const q = e.target.value.trim().toLowerCase()
      const filtered = q
        ? allVenues.filter(v => v.name.toLowerCase().includes(q) ||
            (v.city  || '').toLowerCase().includes(q) ||
            (v.state || '').toLowerCase().includes(q))
        : allVenues
      renderList(filtered)
    })

    // New venue
    document.getElementById('btn-new-venue').addEventListener('click', async () => {
      const name = prompt('Venue name:')
      if (!name?.trim()) return
      try {
        const created = await API.venues.create({ name: name.trim() })
        allVenues = await API.venues.list()
        activeId  = created.id
        renderList(allVenues)
        loadVenueDetail(created.id)
      } catch (e) { alert('Failed: ' + e.message) }
    })

    renderList(allVenues)

    // Pre-select a venue when navigating from a recording's venue link
    if (preSelectId) {
      activeId = preSelectId
      renderList(allVenues)          // re-render to highlight the active row
      loadVenueDetail(preSelectId)
    }
  }

  // ── Genre (2026-08-02) ───────────────────────────────────────────────────────
  // A proper dimension — its own table, one FK from Performer — see the Genre
  // design spec in Context Library. Three surfaces: the #/genre/<id> page
  // (mirrors the Venue page, but a genre's "recordings" are reached through
  // its performers, one extra hop the Venue page doesn't need), the #/genres
  // admin list (mirrors #/venues' split list/detail), and the bulk assignment
  // screen (the actual population mechanism — see the design spec's coverage
  // math on why sort-by-recording-count matters).

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
    const performers = g.performers || []

    const perfSectionsHtml = performers.map(p => `
      <div class="genre-performer-section">
        <div class="genre-performer-head">
          <a class="genre-performer-name" href="#/artist/${p.id}">${esc(p.name)}</a>
          <span class="genre-performer-count">${p.recording_count} recording${p.recording_count !== 1 ? 's' : ''}</span>
        </div>
        <div class="rec-table">${p.recordings.map(r => flatRowHtml(r, false)).join('')}</div>
      </div>`).join('')

    setMainHTML(entityShellHtml({
      navBack,
      // Colour swatch instead of a portrait — a genre has no likeness, but it
      // does have the colour that tints every card of its performers, so
      // showing it here is both the identity and a live preview of the picker.
      portrait: `<div class="gn-swatch" style="--genre-fg:${esc(g.color || 'var(--t2)')}"></div>`,
      title: esc(g.name),
      titleId: 'gn-name',
      titleEditable: true,
      chips: g.color ? `<span class="pp-hero-fact">${esc(g.color)}</span>` : '',
      stats: [
        [g.performer_count || 0, (g.performer_count === 1) ? 'Performer' : 'Performers'],
        [g.recording_count || 0, (g.recording_count === 1) ? 'Recording' : 'Recordings'],
      ],
      actions: `<button class="btn btn-ghost btn-sm pp-delete" id="gn-delete" title="Delete genre">Delete</button>`,
      // No Photos tab (Ryan, 2026-08-07) — a genre has nothing to photograph.
      tabs: [
        { id: 'overview', label: 'Overview', active: true, html: `
            <div class="pp-sec">Description</div>
            <div class="pp-desc pp-editable ${descText ? '' : 'pp-empty'}" id="gn-desc" title="Click to edit">${descText ? esc(g.description) : 'Add a description\u2026'}</div>` },
        { id: 'recordings', label: 'Recordings', count: g.recording_count || 0, html:
            performers.length
              ? perfSectionsHtml
              : `<div class="empty-state" style="min-height:160px"><div class="empty-title">No performers assigned to this genre yet</div><div class="empty-sub">Assign some from the <a href="#/genres/assign">bulk assignment screen</a>.</div></div>` },
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

  // ── Genres admin page (mirrors renderVenuesPage's split list/detail) ────────

  async function renderGenresPage(preSelectId = null) {
    setActiveNav('genres')
    setActiveArtist(null)
    setNavCurrent('Genres')
    setLoading()

    let genres = []
    try { genres = await API.genres.list() } catch (_) {}

    setMainHTML(`
      <div class="action-bar">
        <span style="font-size:13px; font-weight:500; color:var(--t0)">Genres</span>
        <a class="btn btn-ghost btn-sm" href="#/genres/assign" style="margin-left:auto">Bulk assign →</a>
        <button class="btn btn-ghost btn-sm" id="btn-new-genre">+ New genre</button>
      </div>
      <div class="venues-shell">
        <div class="venues-list-panel">
          <div class="venues-search-bar">
            <input type="text" id="genre-search-input" style="font-size:12px" placeholder="Search…" />
          </div>
          <div class="venue-list-scroll" id="genre-list-scroll"></div>
        </div>
        <div class="venues-detail-panel" id="genres-detail-panel">
          <div class="venue-detail-empty">Select a genre to view or edit</div>
        </div>
      </div>`)

    let allGenres = genres
    let activeId  = null

    function renderList(list) {
      const scroll = document.getElementById('genre-list-scroll')
      if (!list.length) {
        scroll.innerHTML = '<div style="padding:16px 14px; font-size:12px; color:var(--t2)">No genres found</div>'
        return
      }
      // Swatch in the list, not just the editor — picking colours is a
      // comparative job (is Reggae too close to Bluegrass?), and that's
      // impossible one detail pane at a time.
      scroll.innerHTML = list.map(g => `
        <div class="venue-list-row has-swatch ${g.id === activeId ? 'active' : ''}" data-id="${g.id}">
          <span class="genre-row-swatch" style="--genre-fg:${esc(g.color || 'var(--t2)')}"></span>
          <div class="venue-row-name">${esc(g.name)}</div>
          <div class="venue-row-count">${g.performer_count || 0}p</div>
        </div>`).join('')

      scroll.querySelectorAll('.venue-list-row').forEach(el => {
        el.addEventListener('click', () => {
          activeId = parseInt(el.dataset.id)
          renderList(list)
          loadGenreDetail(activeId)
        })
      })
    }

    async function loadGenreDetail(id) {
      const panel = document.getElementById('genres-detail-panel')
      panel.innerHTML = '<div class="venue-detail-empty" style="color:var(--t2)">Loading…</div>'
      let g
      try { g = await API.genres.get(id) } catch (_) {
        panel.innerHTML = '<div class="venue-detail-empty">Failed to load</div>'
        return
      }

      panel.innerHTML = `
        <div style="max-width:580px">
          <h2 style="font-size:18px; font-weight:500; color:var(--t0); margin:0 0 18px">${esc(g.name)}</h2>

          <div class="rev-section-title" style="margin-bottom:12px">Genre info</div>

          <div class="ingest-field" style="margin-bottom:10px">
            <label>Name</label>
            <input type="text" id="gd-name" value="${esc(g.name)}" />
          </div>

          <div class="ingest-field" style="margin-bottom:18px">
            <label>Description</label>
            <textarea id="gd-desc" style="min-height:80px">${esc(g.description||'')}</textarea>
          </div>

          <!-- Colour (2026-08-07) — drives the Browse cards' colour flair.
               Clearable, because NULL is a real state: an uncoloured genre
               renders the same neutral grey as an unassigned performer. -->
          <div class="ingest-field" style="margin-bottom:18px">
            <label>Card colour</label>
            <div class="genre-color-row">
              <input type="color" id="gd-color" class="genre-color-input"
                     value="${esc(g.color || '#7a6e64')}" />
              <input type="text" id="gd-color-hex" class="genre-color-hex"
                     value="${esc(g.color || '')}" placeholder="none — neutral grey"
                     spellcheck="false" maxlength="7" />
              <button type="button" class="btn btn-ghost btn-xs" id="gd-color-clear">Clear</button>
              <span class="genre-color-preview" id="gd-color-preview"
                    style="--genre-fg:${esc(g.color || 'var(--t2)')}"></span>
            </div>
          </div>

          <div style="display:flex; align-items:center; gap:10px; margin-bottom:28px">
            <button class="btn btn-primary btn-sm" id="gd-save">Save</button>
            <span id="gd-msg" style="font-size:11px; color:var(--t2)"></span>
            <button class="btn btn-ghost btn-sm" id="gd-delete" style="margin-left:auto; color:var(--red)">Delete</button>
          </div>

          ${g.performer_count > 0 ? `
          <div class="rev-section-title" style="margin-bottom:10px">Performers (${g.performer_count})</div>
          <div style="display:flex; flex-direction:column; gap:2px">
            ${g.performers.map(p => `
              <div style="display:flex; align-items:center; gap:12px; padding:5px 0; border-bottom:1px solid var(--bd-0); font-size:12px">
                <a href="#/artist/${p.id}" style="color:var(--t0); text-decoration:none; flex:1">${esc(p.name)}</a>
                <span style="color:var(--t2)">${p.recording_count} rec</span>
              </div>`).join('')}
          </div>` : `<div style="font-size:12px; color:var(--t2)">No performers assigned yet</div>`}
        </div>`

      // Colour picker ↔ hex field stay in sync in both directions, and the
      // swatch previews live. The hex field exists so a colour can be pasted
      // or typed exactly — <input type="color"> alone can't express "none",
      // and clearing must remain possible (see the model's note on NULL).
      const colorEl   = document.getElementById('gd-color')
      const hexEl     = document.getElementById('gd-color-hex')
      const previewEl = document.getElementById('gd-color-preview')
      const HEX_RE    = /^#[0-9a-fA-F]{6}$/

      function syncPreview() {
        const v = hexEl.value.trim()
        previewEl.style.setProperty('--genre-fg', HEX_RE.test(v) ? v : 'var(--t2)')
      }
      colorEl.addEventListener('input', () => {
        hexEl.value = colorEl.value.toLowerCase()
        syncPreview()
      })
      hexEl.addEventListener('input', () => {
        const v = hexEl.value.trim()
        if (HEX_RE.test(v)) colorEl.value = v
        syncPreview()
      })
      document.getElementById('gd-color-clear').addEventListener('click', () => {
        hexEl.value = ''
        syncPreview()
      })

      document.getElementById('gd-save').addEventListener('click', async () => {
        const saveBtn = document.getElementById('gd-save')
        const msgEl   = document.getElementById('gd-msg')
        const hexVal  = hexEl.value.trim()
        if (hexVal && !HEX_RE.test(hexVal)) {
          msgEl.style.color = 'var(--red)'
          msgEl.textContent = 'Colour must be #rrggbb'
          return
        }
        saveBtn.disabled = true
        saveBtn.textContent = 'Saving…'
        try {
          await API.genres.update(id, {
            name:        document.getElementById('gd-name').value.trim(),
            description: document.getElementById('gd-desc').value.trim() || null,
            color:       hexVal || null,
          })
          allGenres = await API.genres.list()
          renderList(allGenres)
          document.querySelector('#genres-detail-panel h2').textContent =
            document.getElementById('gd-name').value.trim()
          msgEl.textContent = 'Saved'
          setTimeout(() => { if (msgEl) msgEl.textContent = '' }, 2000)
          invalidateDims('genres')
        } catch (e) {
          msgEl.style.color = 'var(--red)'
          msgEl.textContent = 'Save failed: ' + e.message
        } finally {
          saveBtn.disabled = false
          saveBtn.textContent = 'Save'
        }
      })

      document.getElementById('gd-delete').addEventListener('click', async () => {
        if (!confirm(`Delete genre "${g.name}"? This can't be undone.`)) return
        const msgEl = document.getElementById('gd-msg')
        try {
          await API.genres.remove(id)
          allGenres = await API.genres.list()
          activeId  = null
          renderList(allGenres)
          document.getElementById('genres-detail-panel').innerHTML =
            '<div class="venue-detail-empty">Select a genre to view or edit</div>'
          invalidateDims('genres')
        } catch (e) {
          msgEl.style.color = 'var(--red)'
          msgEl.textContent = e.message
        }
      })
    }

    // Search filter
    document.getElementById('genre-search-input').addEventListener('input', e => {
      const q = e.target.value.trim().toLowerCase()
      const filtered = q ? allGenres.filter(g => g.name.toLowerCase().includes(q)) : allGenres
      renderList(filtered)
    })

    // New genre
    document.getElementById('btn-new-genre').addEventListener('click', async () => {
      const name = prompt('Genre name:')
      if (!name?.trim()) return
      try {
        const created = await API.genres.create({ name: name.trim() })
        allGenres = await API.genres.list()
        activeId  = created.id
        renderList(allGenres)
        loadGenreDetail(created.id)
        invalidateDims('genres')
      } catch (e) { alert('Failed: ' + e.message) }
    })

    renderList(allGenres)

    // Pre-select a genre when navigating from elsewhere
    if (preSelectId) {
      activeId = preSelectId
      renderList(allGenres)
      loadGenreDetail(preSelectId)
    }
  }

  // ── Bulk genre assignment (the actual population mechanism) ─────────────────
  // Ryan assigns all 164 performers' genres by hand — no AI suggestion (see
  // design spec). One row per performer, sorted by recording count DESC: the
  // library is a long tail (85 of 164 performers have exactly one recording),
  // so the top ~30 acts by recording count cover ~62% of the library. Sorted
  // this way, stopping early after ten minutes is a legitimate end state, not
  // an unfinished migration. Each pick is its own PUT — no bulk save button,
  // nothing lost by closing the tab mid-way.
  async function renderGenreAssignView() {
    setActiveNav('genres'); setActiveArtist(null)
    setNavCurrent('Assign Genres')
    setLoading()

    let performers = []
    try { performers = await API.performers.list() } catch (_) {}
    const sorted = performers.slice().sort((a, b) => (b.recording_count || 0) - (a.recording_count || 0))

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
      <div class="genre-assign-hint">Sorted by recording count, descending — the top acts cover most of the library fastest. Each pick saves immediately.</div>
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
        // No one-shot "committed" guard here (unlike the venue/event/performer-
        // page pickers): this input stays live in place rather than being
        // swapped for a display element after a pick, specifically so a
        // mis-click can be corrected by just picking again.
        const commit = async ({ id, name }) => {
          statusEl.textContent = 'Saving…'
          try {
            await API.performers.update(p.id, { genre_id: id })
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
        : `<div class="empty-state" style="min-height:120px"><div class="empty-title">${showAll ? 'No performers yet' : 'Every performer has a genre — nothing left to assign'}</div></div>`
      wireRows(list)
    }

    document.getElementById('ga-show-all').addEventListener('change', e => {
      showAll = e.target.checked
      renderRows()
    })

    renderRows()
  }

  // ── Artists Index ──────────────────────────────────────────────────────────

  async function renderArtistsIndexPage() {
    setActiveNav('artists-index')
    setActiveArtist(null)
    setNavCurrent('Performers')
    setLoading()

    let performers = []
    try { performers = await API.performers.list() } catch (_) {}

    const rowHtml = list => list.map(p => `
      <div class="artist-index-row" data-id="${p.id}">
        <span class="artist-index-name">${esc(p.name)}</span>
        <span class="artist-index-members">${esc((p.members || []).join(', '))}</span>
        <span class="artist-index-count">${p.recording_count || 0} rec</span>
      </div>`).join('')

    setMainHTML(`
      <div class="action-bar">
        <span style="font-size:13px; font-weight:500; color:var(--t0)">Performers</span>
        <input type="text" id="artist-search-input" placeholder="Search performers or members…" style="margin-left:auto; width:240px; font-size:12px" />
      </div>
      <div class="artist-index-list" id="artist-index-list">${rowHtml(performers) || '<div class="empty-state" style="min-height:120px"><div>No performers yet</div></div>'}</div>`)

    function wireRows() {
      mainContent.querySelectorAll('.artist-index-row').forEach(el =>
        el.addEventListener('click', () => { window.location.hash = `#/artist/${el.dataset.id}` }))
    }
    wireRows()

    document.getElementById('artist-search-input').addEventListener('input', e => {
      const q = e.target.value.trim().toLowerCase()
      const filtered = q
        ? performers.filter(p => p.name.toLowerCase().includes(q) ||
            (p.members || []).some(m => m.toLowerCase().includes(q)))
        : performers
      document.getElementById('artist-index-list').innerHTML = rowHtml(filtered)
      wireRows()
    })
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
    '#/ingest',          // Add Recordings — source picker, triage queue, metadata review
    '#/batch',           // Batch import
    '#/genres/assign',   // Assign Genres
    '#/peers',           // Sharing
    '#/venue/new', '#/performer/new', '#/artist/new',
  ]
  const isAdminOnlyHash = h => ADMIN_ONLY_HASHES.includes((h || '').split('?')[0])

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
    // idiom as setTheme() above — applied before first paint by the inline
    // head script in index.html so there's no flash, flipped on click here.
    const sbToggle = document.getElementById('nav-sidebar-toggle')
    sbToggle?.setAttribute('aria-pressed', String(document.body.classList.contains('sidebar-collapsed')))
    sbToggle?.addEventListener('click', () => {
      const collapsed = document.body.classList.toggle('sidebar-collapsed')
      localStorage.setItem('fluxSidebarCollapsed', collapsed ? '1' : '0')
      sbToggle.setAttribute('aria-pressed', String(collapsed))
    })
  }

  function route() {
    const hash = window.location.hash || '#/'

    // Bounce out of an admin-only view when there is no edit permission in
    // force — Playback mode, or a listener who was sent the URL. The library is
    // the honest destination: it is the one page everybody can use.
    if (!canEditLibrary() && isAdminOnlyHash(hash)) {
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
    // Any handler belongs to the view we are leaving. The incoming view
    // re-registers one if it has steps of its own, and repaints the buttons
    // itself when it does — this paint only has to be right for views that
    // don't.
    setInPageBack(null)
    paintNavButtons()

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
    // otherwise '#/artist/new' is parsed as id "new" (NaN) and renders a broken
    // detail page instead of the form.
    } else if (hash === '#/venue/new') {
      renderVenueForm()
    } else if (hash === '#/performer/new') {
      renderPerformerForm()
    } else if (hash === '#/artist/new') {
      renderArtistForm()
    } else if (hash.startsWith('#/artist/')) {
      const id = parseInt(hash.split('/')[2])
      if (id) renderArtistView(id)
      else    renderLibraryView()

    } else if (hash.startsWith('#/performer/')) {
      // The performer page is edit-in-place, so #/performer/<id> and any legacy
      // /edit suffix both land on the same view.
      const id = parseInt(hash.split('/')[2])
      if (id) renderArtistView(id)
      else    renderLibraryView()

    } else if (hash === '#/recent') {
      renderRecentView()

    } else if (hash === '#/batch') {
      renderBatchImportView()

    } else if (hash === '#/ingest') {
      renderIngestView()

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

    } else if (hash === '#/artists') {
      renderArtistsIndexPage()

    } else if (hash.startsWith('#/person/')) {
      // Edit-in-place, so #/person/<id> and any legacy /edit both land on the view.
      const id = parseInt(hash.split('/')[2])
      if (id) renderPersonView(id)
      else    renderLibraryView()

    } else if (hash === '#/settings') {
      renderSettingsPage()
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

  async function renderSettingsPage() {
    setActiveNav('settings')
    setNavCurrent('Settings')
    setLoading()

    let prefs = {}, me = {}, about = {}
    try {
      [prefs, me, about] = await Promise.all([
        API.preferences.get(), API.auth.me(), API.system.about(),
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
    const behavior   = prefs.ingest_file_behavior || 'move'

    setMainHTML(`
      <div class="set-wrap">
        <header class="set-head">
          <h1 class="set-h1">Settings</h1>
          <p class="set-lede">Changes save as you make them.</p>
        </header>

        <section class="set-sec">
          <h2 class="set-sec-title">You</h2>
          <p class="set-sec-hint">Your name and picture appear on any library you
            share — they are how a peer knows whose shelf they are looking at.</p>

          <div class="set-person">
            <div class="set-avatar" id="set-avatar">${_settingsAvatarHtml(me)}</div>
            <div class="set-person-fields">
              <div class="set-field">
                <label class="set-label" for="set-name">Display name</label>
                <input class="set-input" id="set-name" maxlength="120"
                       value="${esc(me.display_name || '')}"
                       placeholder="${esc(me.username)}" autocomplete="off">
                <span class="set-flash" id="set-name-flash"></span>
                <p class="set-hint">Leave it empty to go by your sign-in name.</p>
              </div>
              <div class="set-field">
                <label class="set-label" for="set-username">Sign-in name</label>
                <input class="set-input" id="set-username" maxlength="64"
                       value="${esc(me.username)}" autocomplete="off">
                <span class="set-flash" id="set-username-flash"></span>
                <p class="set-hint">The name you sign in with. Peers see your
                  display name instead, unless you have left that empty.</p>
              </div>
              <div class="set-field">
                <span class="set-label">Picture</span>
                <div class="set-actions">
                  <input type="file" id="set-avatar-file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>
                  <button class="btn btn-ghost btn-sm" id="set-avatar-pick">
                    ${me.has_avatar ? 'Replace' : 'Choose a picture'}</button>
                  ${me.has_avatar
                    ? '<button class="btn btn-ghost btn-sm" id="set-avatar-clear">Remove</button>' : ''}
                  <span class="set-flash" id="set-avatar-flash"></span>
                </div>
                <p class="set-hint">A square image works best. 4 MB maximum.</p>
              </div>
            </div>
          </div>
        </section>

        <section class="set-sec">
          <h2 class="set-sec-title">Appearance</h2>
          <div class="set-field">
            <span class="set-label">Theme</span>
            <div class="view-mode-toggle set-theme" id="set-theme" role="group" aria-label="Theme">
              <button type="button" class="vm-opt" data-theme="dark">Dark</button>
              <button type="button" class="vm-opt" data-theme="light">Light</button>
            </div>
            <p class="set-hint">Applies immediately, and only on this machine.</p>
          </div>
        </section>

        <section class="set-sec">
          <h2 class="set-sec-title">Adding recordings</h2>
          <div class="set-field">
            <label class="set-label" for="set-behavior">Source files</label>
            <select class="set-input" id="set-behavior">
              <option value="move" ${behavior !== 'copy' ? 'selected' : ''}>Move into the library, emptying the source folder</option>
              <option value="copy" ${behavior === 'copy' ? 'selected' : ''}>Copy into the library, leaving the source folder alone</option>
            </select>
            <span class="set-flash" id="set-behavior-flash"></span>
            <p class="set-hint">What happens to a folder after its recording is filed.</p>
          </div>
        </section>

        <section class="set-sec">
          <h2 class="set-sec-title">AI assistance</h2>
          <p class="set-sec-hint">Used to research performers and read info files.
            You bring your own key, so you pay Anthropic directly and Trellis
            never marks it up.</p>

          <div class="set-field">
            <label class="set-label" for="set-key">Anthropic API key</label>
            <div class="set-actions">
              <input class="set-input" type="password" id="set-key" autocomplete="off"
                     placeholder="${keySet ? '•••••••••••••  (a key is saved)' : 'sk-ant-…'}">
              <button class="btn btn-primary btn-sm" id="set-key-save">Save key</button>
              ${keySet ? '<button class="btn btn-ghost btn-sm" id="set-key-clear">Clear</button>' : ''}
              <span class="set-flash" id="set-key-flash"></span>
            </div>
            <p class="set-hint">${noKeychain
              ? 'This machine has no usable keychain, so a key cannot be stored.'
              : 'Stored in your OS keychain, never in the database.'}</p>
          </div>

          <div class="set-field">
            <label class="set-label" for="set-model">Model</label>
            <select class="set-input" id="set-model">
              <option value="claude-sonnet-5" ${model === 'claude-sonnet-5' ? 'selected' : ''}>Sonnet — stronger research</option>
              <option value="claude-haiku-4-5" ${model === 'claude-haiku-4-5' ? 'selected' : ''}>Haiku — faster and cheaper</option>
            </select>
            <span class="set-flash" id="set-model-flash"></span>
          </div>
        </section>

        ${canEditLibrary() ? `
        <section class="set-sec">
          <h2 class="set-sec-title">Sharing</h2>
          <p class="set-sec-hint">Who can reach your library, and what they see.
            Enrolling someone has its own steps and its own page.</p>
          <button class="btn btn-ghost btn-sm" id="set-peers">Manage sharing →</button>
        </section>` : ''}

        <section class="set-sec set-sec--about">
          <h2 class="set-sec-title">About</h2>
          <dl class="set-about">
            <dt>Version</dt><dd>${esc(about.app_name || 'Trellis')} ${esc(about.version || '')}${
              about.installed ? '' : ' <span class="set-about-tag">running from source</span>'}</dd>
            <dt>Library data</dt><dd class="set-path">${esc(about.database || '')}</dd>
            <dt>Audio files</dt><dd class="set-path">${esc(about.library_root || '')}</dd>
          </dl>
        </section>
      </div>`)

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

    // ── Theme — applies live, so there is nothing to save ───────────────────
    const themeWrap = $('set-theme')
    const paintTheme = () => themeWrap?.querySelectorAll('.vm-opt').forEach(b => {
      const on = (b.dataset.theme === 'light') === isLightTheme()
      b.classList.toggle('active', on)
      b.setAttribute('aria-pressed', on ? 'true' : 'false')
    })
    paintTheme()
    themeWrap?.addEventListener('click', e => {
      const b = e.target.closest('.vm-opt')
      if (!b) return
      setTheme(b.dataset.theme === 'light')
      paintTheme()
    })

    // ── Menus — commit on change ────────────────────────────────────────────
    const menu = (id, key) => $(id)?.addEventListener('change', async e => {
      try {
        await API.preferences.update({ [key]: e.target.value })
        _settingsSaved($(`${id}-flash`))
      } catch (err) { _settingsSaved($(`${id}-flash`), err.message) }
    })
    menu('set-behavior', 'ingest_file_behavior')
    menu('set-model',    'ai_model')

    // ── The one explicit Save: a secret you paste and cannot read back ──────
    $('set-key-save')?.addEventListener('click', async () => {
      const key = $('set-key').value.trim()
      if (!key) return _settingsSaved($('set-key-flash'), 'Paste a key first')
      try {
        await API.preferences.update({ api_key: key })
        $('set-key').value = ''
        _settingsSaved($('set-key-flash'))
      } catch (e) { _settingsSaved($('set-key-flash'), e.message) }
    })
    $('set-key-clear')?.addEventListener('click', async () => {
      try { await API.preferences.update({ clear_api_key: true }); renderSettingsPage() }
      catch (e) { _settingsSaved($('set-key-flash'), e.message) }
    })

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
  // DB before choosing 3 — no performer, venue or artist name is shorter than
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
              <span class="search-item-name">${esc(item.performer || 'Unknown')}</span>
              <span class="search-item-meta">${esc(where)}</span>`
    }
    if (item.type === 'venue') {
      const where = [item.city, item.state].filter(Boolean).join(', ')
      return `<span class="search-item-name">${esc(item.name)}</span>
              <span class="search-item-meta">${esc(where)}</span>`
    }
    if (item.type === 'artist') {
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
        `<div class="search-dropdown-empty">No performers, artists, venues or dates match
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
        `<div class="search-dropdown-empty">Search failed — ${esc(e.message)}</div>`
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
      return `<div class="search-row" data-hash="${esc(item.hash)}">
                <span class="search-row-date">${esc(item.date || '—')}</span>
                <div class="search-row-main">
                  <div class="search-row-name">${esc(item.performer || 'Unknown')}</div>
                  <div class="search-row-meta">${esc(where || 'No venue recorded')}</div>
                </div>
                <div class="search-row-right">${sourceBadge(item.source)}</div>
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
    if (item.type === 'artist') {
      return `<div class="search-row" data-hash="${esc(item.hash)}">
                <div class="search-row-main">
                  <div class="search-row-name">${esc(item.name)}</div>
                  <div class="search-row-meta">${esc((item.member_of || []).join(', ') || 'No performers recorded')}</div>
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
        Search covers performers, the artists in them, venues and show dates.
        Try a shorter name, or a year on its own like <b>1983</b>.
      </div>
      <div class="search-zero-doors">
        <button class="btn btn-ghost btn-sm" data-hash="#/">Browse the library</button>
        <button class="btn btn-ghost btn-sm" data-hash="#/recent">Recently added</button>
        <button class="btn btn-ghost btn-sm" data-hash="#/artists">All performers</button>
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
                 placeholder="Search performers, artists, venues, cities, years"
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
      ${short ? `<div class="search-page-hint-min">Keep typing — searches start at ${SEARCH_MIN_CHARS} characters.</div>` : ''}
      Search covers performers, the artists in them, venues, cities and show dates.
      A year on its own works too, like <b>1983</b>.
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
  //   * a 'flux:library-disconnected' event from api.js the instant any
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
      window.addEventListener('flux:library-disconnected', (e) => {
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
      window.dispatchEvent(new CustomEvent('flux:user', { detail: user }))
      showApp()
      libraryDrive.start()
      // Before the sidebar renders: the library selector reads
      // libraryState.remotes, and a selector that appears as a plain label and
      // then sprouts a dropdown a moment later reads as a glitch.
      await loadRemotes()
      await loadArtistList()
      route()
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
  window.fluxState = {
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
