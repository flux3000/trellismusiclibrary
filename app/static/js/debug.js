/**
 * debug.js — Trellis debug drawer.
 *
 * Rebuilt 2026-08-23. The previous panel was a floating top-right overlay you
 * could only reach by typing a backtick, gated on DEV_MODE — which meant it did
 * not exist in the app Ryan actually runs (`python3 run.py`, no DEV_MODE), so
 * it was never there at the moment something broke.
 *
 * What changed, and why:
 *   · A persistent tab at the bottom-left. Discoverable without knowing a
 *     keystroke. The backtick still works; it is now a shortcut, not the door.
 *   · Gated on the ADMIN ROLE, not DEV_MODE. Available while testing the real
 *     app. Listeners never see it — the server refuses the endpoints too, so
 *     this is not merely a hidden button.
 *   · Bottom drawer, full width, drag to resize. Log lines and stack traces
 *     need horizontal room; the old overlay wrapped them into noise.
 *   · Three panes, no more: Errors, Network, Server. App State and the Paula
 *     breakdown were dropped (Ryan, 2026-08-23) — both were readable elsewhere
 *     and neither was what anyone opened this for.
 *   · The pop-out window and its BroadcastChannel sync went with them. The
 *     drawer is resizable and headless runs have real devtools; a second window
 *     kept in sync by hand was a lot of machinery for a rare case.
 *
 * Instrumentation is installed BEFORE the admin check, so an error thrown
 * during boot is captured even though the UI does not exist yet.
 *
 * Exports: window.fluxDebug = { open, close, toggle, refresh }
 */

;(function initDebug() {
  'use strict'

  const MAX_ERRORS  = 100
  const MAX_NETWORK = 200
  const LS_OPEN     = 'trellisDebugOpen'
  const LS_HEIGHT   = 'trellisDebugHeight'

  const errors  = []
  const network = []
  let pane      = 'errors'
  let seenErrors = 0          // for the tab badge — resets when Errors is viewed
  let els = null              // DOM refs, null until/unless the UI is built

  // ══ Instrumentation — installed first, unconditionally ═════════════════════
  // Before the admin check and before any await. An error during boot is
  // exactly the one worth catching, and it happens before we know who is
  // logged in.

  const _fetch = window.fetch

  window.fetch = async function (input, init) {
    const url    = typeof input === 'string' ? input : (input && input.url) || String(input)
    const method = ((init && init.method) || 'GET').toUpperCase()
    const t0     = performance.now()

    // Recorded as PENDING immediately, not on resolve. A request still in
    // flight is the interesting case — a stuck scan used to be invisible here
    // because nothing was written until it came back.
    const entry = { method, url, status: null, ms: null, ts: new Date(), pending: true }
    push(network, entry, MAX_NETWORK)
    render()

    try {
      const res = await _fetch(input, init)
      entry.status  = res.status
      entry.ok      = res.ok
      entry.ms      = Math.round(performance.now() - t0)
      entry.pending = false
      render()
      return res
    } catch (e) {
      entry.status  = 'ERR'
      entry.ok      = false
      entry.ms      = Math.round(performance.now() - t0)
      entry.pending = false
      entry.note    = e && e.message
      render()
      throw e
    }
  }

  window.addEventListener('error', e => {
    addError({
      kind:    'error',
      message: e.message || 'Unknown error',
      stack:   e.error && e.error.stack,
      source:  e.filename ? `${short(e.filename)}:${e.lineno}:${e.colno}` : '',
    })
  })

  window.addEventListener('unhandledrejection', e => {
    const r = e.reason
    addError({
      kind:    'promise',
      message: (r && r.message) || String(r),
      stack:   r && r.stack,
      source:  r && r.status ? `HTTP ${r.status}` : '',
    })
  })

  // console.error is the third source, and the one that catches anything the
  // app handled itself but still wants to shout about — including bootFailure().
  const _consoleError = console.error
  console.error = function (...args) {
    addError({
      kind:    'console',
      message: args.map(a => (a && a.stack) ? a.message : fmt(a)).join(' '),
      stack:   (args.find(a => a && a.stack) || {}).stack,
      source:  '',
    })
    _consoleError.apply(console, args)
  }

  function addError(err) {
    err.ts = new Date()
    push(errors, err, MAX_ERRORS)
    if (els) {
      updateBadge()
      if (pane === 'errors') render()
      // A new error while the drawer is shut is the whole reason the badge
      // exists — but it must not steal focus mid-task, so nothing auto-opens.
    }
  }

  function push(arr, item, max) {
    arr.unshift(item)
    if (arr.length > max) arr.length = max
  }

  // ══ Gate on the admin role ════════════════════════════════════════════════
  // /api/auth/me rather than a config flag: the question is "is this person an
  // admin", and the server answers it the same way every other surface asks.
  // The debug endpoints enforce the same rule independently — hiding the tab is
  // a courtesy, not the boundary.

  ;(async function gate() {
    let me = null
    try {
      const res = await _fetch('/api/auth/me', { credentials: 'same-origin' })
      if (res.ok) me = await res.json()
    } catch (_) { /* not logged in yet — retried below */ }

    if (!me || me.role !== 'admin') {
      // Boot races login: on a cold start nobody is authenticated yet. Re-check
      // once the app announces a user rather than polling.
      window.addEventListener('flux:user', ev => {
        if (ev.detail && ev.detail.role === 'admin' && !els) build()
      }, { once: true })
      return
    }
    build()
  })()

  // ══ UI ════════════════════════════════════════════════════════════════════

  function build() {
    const tab = document.createElement('button')
    tab.className = 'dev-tab'
    tab.id = 'dev-tab'
    tab.innerHTML = `<span class="dev-tab-label">Debug</span><span class="dev-tab-badge" id="dev-tab-badge"></span>`

    const drawer = document.createElement('div')
    drawer.className = 'dev-drawer'
    drawer.id = 'dev-drawer'
    drawer.innerHTML = `
      <div class="dev-resize" id="dev-resize" title="Drag to resize"></div>
      <div class="dev-head">
        <div class="dev-panes">
          <button class="dev-pane-btn is-on" data-pane="errors">Errors <span class="dev-count" id="dev-c-errors">0</span></button>
          <button class="dev-pane-btn" data-pane="network">Network <span class="dev-count" id="dev-c-network">0</span></button>
          <button class="dev-pane-btn" data-pane="server">Server</button>
        </div>
        <div class="dev-head-right">
          <label class="dev-check"><input type="checkbox" id="dev-fails-only"> Failures only</label>
          <button class="dev-act" id="dev-clear">Clear</button>
          <button class="dev-act" id="dev-close" title="Close (\`)">Close</button>
        </div>
      </div>
      <div class="dev-body" id="dev-body"></div>`

    document.body.appendChild(tab)
    document.body.appendChild(drawer)

    els = {
      tab, drawer,
      body:     drawer.querySelector('#dev-body'),
      badge:    tab.querySelector('#dev-tab-badge'),
      cErrors:  drawer.querySelector('#dev-c-errors'),
      cNetwork: drawer.querySelector('#dev-c-network'),
      failsOnly: drawer.querySelector('#dev-fails-only'),
    }

    let h = parseInt(read(LS_HEIGHT), 10)
    if (!h || h < 120 || h > window.innerHeight - 80) h = 320
    drawer.style.height = h + 'px'

    tab.addEventListener('click', open)
    drawer.querySelector('#dev-close').addEventListener('click', close)
    drawer.querySelector('#dev-clear').addEventListener('click', () => {
      if (pane === 'errors')  errors.length = 0
      if (pane === 'network') network.length = 0
      seenErrors = errors.length
      updateBadge(); render()
    })
    els.failsOnly.addEventListener('change', render)

    drawer.querySelectorAll('.dev-pane-btn').forEach(b => {
      b.addEventListener('click', () => {
        pane = b.dataset.pane
        drawer.querySelectorAll('.dev-pane-btn').forEach(x => x.classList.toggle('is-on', x === b))
        if (pane === 'errors') { seenErrors = errors.length; updateBadge() }
        pane === 'server' ? startServerPoll() : stopServerPoll()
        render()
      })
    })

    // Expand/collapse a stack trace. Delegated: rows are re-rendered constantly.
    els.body.addEventListener('click', e => {
      const row = e.target.closest('.dev-err[data-stack="1"]')
      if (row) row.classList.toggle('is-open')
    })

    initResize(drawer)

    document.addEventListener('keydown', e => {
      if (e.key !== '`' || e.ctrlKey || e.metaKey || e.altKey) return
      if (['INPUT', 'TEXTAREA'].includes(document.activeElement && document.activeElement.tagName)) return
      toggle()
    })

    if (read(LS_OPEN) === '1') open()
    updateBadge()
    render()
  }

  function initResize(drawer) {
    const grip = drawer.querySelector('#dev-resize')
    let startY = 0, startH = 0, dragging = false
    grip.addEventListener('mousedown', e => {
      dragging = true; startY = e.clientY; startH = drawer.offsetHeight
      document.body.style.userSelect = 'none'
      e.preventDefault()
    })
    window.addEventListener('mousemove', e => {
      if (!dragging) return
      const h = Math.min(Math.max(120, startH + (startY - e.clientY)), window.innerHeight - 80)
      drawer.style.height = h + 'px'
    })
    window.addEventListener('mouseup', () => {
      if (!dragging) return
      dragging = false
      document.body.style.userSelect = ''
      write(LS_HEIGHT, drawer.offsetHeight)
    })
  }

  // ══ Rendering ═════════════════════════════════════════════════════════════

  function render() {
    if (!els || !els.drawer.classList.contains('is-open')) { updateCounts(); return }
    updateCounts()
    if (pane === 'errors')  els.body.innerHTML = renderErrors()
    if (pane === 'network') els.body.innerHTML = renderNetwork()
    if (pane === 'server')  els.body.innerHTML = renderServer()
  }

  function updateCounts() {
    if (!els) return
    els.cErrors.textContent  = errors.length
    els.cErrors.classList.toggle('is-bad', errors.length > 0)
    els.cNetwork.textContent = network.length
  }

  function updateBadge() {
    if (!els) return
    const n = errors.length - seenErrors
    els.badge.textContent = n > 0 ? (n > 99 ? '99+' : n) : ''
    els.badge.classList.toggle('is-on', n > 0)
    els.tab.classList.toggle('has-errors', n > 0)
  }

  function renderErrors() {
    if (!errors.length) return empty('No errors.', 'Anything thrown, rejected, or sent to console.error lands here.')
    return errors.map(e => {
      const hasStack = !!e.stack
      return `
        <div class="dev-err${hasStack ? '' : ' no-stack'}" data-stack="${hasStack ? 1 : 0}">
          <div class="dev-err-top">
            <span class="dev-time">${time(e.ts)}</span>
            <span class="dev-kind dev-kind--${e.kind}">${e.kind}</span>
            <span class="dev-err-msg">${esc(e.message)}</span>
            ${e.source ? `<span class="dev-err-src">${esc(e.source)}</span>` : ''}
            ${hasStack ? '<span class="dev-err-more">stack</span>' : ''}
          </div>
          ${hasStack ? `<pre class="dev-err-stack">${esc(e.stack)}</pre>` : ''}
        </div>`
    }).join('')
  }

  function renderNetwork() {
    const rows = els.failsOnly.checked
      ? network.filter(n => n.pending || n.ok === false)
      : network
    if (!rows.length) return empty('Nothing yet.', 'Every fetch the app makes, including requests still in flight.')
    return `<div class="dev-net">${rows.map(n => {
      const cls = n.pending ? 'is-pending' : (n.ok ? 'is-ok' : 'is-bad')
      return `
        <div class="dev-net-row ${cls}">
          <span class="dev-time">${time(n.ts)}</span>
          <span class="dev-net-method">${esc(n.method)}</span>
          <span class="dev-net-status">${n.pending ? '···' : esc(String(n.status))}</span>
          <span class="dev-net-url" title="${esc(n.url)}">${esc(path(n.url))}</span>
          <span class="dev-net-ms">${n.pending ? '' : n.ms + ' ms'}</span>
          ${n.note ? `<span class="dev-net-note">${esc(n.note)}</span>` : ''}
        </div>`
    }).join('')}</div>`
  }

  // ── Server pane ───────────────────────────────────────────────────────────
  // Checkpoints pushed from inside slow server pipelines (scan_folder,
  // batch-scan) via app/utils/debug_log.py. The point is not what the request
  // returned — Network already has that — but WHERE inside a long request it
  // currently is, which is the only way to tell a slow scan from a hung one.
  let serverRows = null, serverTimer = null, serverErr = null

  function renderServer() {
    if (serverErr)     return empty('Server log unavailable.', esc(serverErr))
    if (!serverRows)   return empty('Listening…', 'Checkpoints logged from inside server-side pipelines.')
    if (!serverRows.length) return empty('Nothing logged.', 'Run a scan or a batch import and steps will appear here.')
    return `<div class="dev-net">${serverRows.slice().reverse().map(r => `
      <div class="dev-net-row">
        <span class="dev-time">${esc(r.ts || '')}</span>
        <span class="dev-net-method">${esc(r.kind || 'step')}</span>
        <span class="dev-net-url">${esc(r.message || r.step || fmt(r))}</span>
      </div>`).join('')}</div>`
  }

  function startServerPoll() {
    if (serverTimer) return
    const tick = async () => {
      try {
        const res = await _fetch('/api/debug/live', { credentials: 'same-origin' })
        if (!res.ok) throw new Error('HTTP ' + res.status)
        serverRows = await res.json(); serverErr = null
      } catch (e) {
        serverErr = (e && e.message) || 'request failed'
      }
      if (pane === 'server') render()
    }
    tick()
    serverTimer = setInterval(tick, 2000)
  }
  function stopServerPoll() {
    if (serverTimer) { clearInterval(serverTimer); serverTimer = null }
  }

  // ══ Open / close ══════════════════════════════════════════════════════════

  function open() {
    if (!els) return
    els.drawer.classList.add('is-open')
    els.tab.classList.add('is-hidden')
    document.body.classList.add('dev-drawer-open')
    if (pane === 'errors') { seenErrors = errors.length; updateBadge() }
    if (pane === 'server') startServerPoll()
    write(LS_OPEN, '1')
    render()
  }
  function close() {
    if (!els) return
    els.drawer.classList.remove('is-open')
    els.tab.classList.remove('is-hidden')
    document.body.classList.remove('dev-drawer-open')
    stopServerPoll()
    write(LS_OPEN, '0')
  }
  function toggle() {
    if (!els) return
    els.drawer.classList.contains('is-open') ? close() : open()
  }

  window.fluxDebug = { open, close, toggle, refresh: render }

  // ══ Helpers ═══════════════════════════════════════════════════════════════

  function empty(title, sub) {
    return `<div class="dev-empty"><div class="dev-empty-t">${esc(title)}</div><div class="dev-empty-s">${esc(sub)}</div></div>`
  }
  function time(d) {
    try { return d.toLocaleTimeString('en-US', { hour12: false }) } catch (_) { return '' }
  }
  function path(u) {
    try { return new URL(u, location.origin).pathname } catch (_) { return String(u) }
  }
  function short(f) {
    try { return new URL(f).pathname.split('/').pop() } catch (_) { return String(f) }
  }
  function fmt(a) {
    if (typeof a === 'string') return a
    try { return JSON.stringify(a) } catch (_) { return String(a) }
  }
  function esc(s) {
    if (s == null) return ''
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  }
  // localStorage is a convenience here and never load-bearing: a private window
  // or cleared site data just means the drawer opens closed at its default size.
  function read(k)    { try { return localStorage.getItem(k) } catch (_) { return null } }
  function write(k, v){ try { localStorage.setItem(k, v) } catch (_) {} }

})()
