/* notify.js -- the one way the app tells a person something went wrong.
 *
 * Replaces the system alert(), which freezes the window and looks like it came
 * from the operating system. Loaded before api.js and player.js, which call it.
 *
 *   Notify.error(msg)   toast, stays until dismissed (an error that fades on a
 *                       timer is an error nobody saw)
 *   Notify.info(msg)    toast, fades after a few seconds
 *   Notify.notice(msg)  dialog for a long or multi-line message; returns a
 *                       Promise that resolves when it is closed
 *
 * Text only goes in through textContent, so a message can carry a filename or a
 * server error without being parsed as markup.
 */
const Notify = (() => {
  const INFO_MS = 5000
  const MAX_TOASTS = 4
  let host = null

  // Lucide "x", paths verbatim (see ICONS in app.js).
  const X_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>'

  const text = m => (m && m.message) ? m.message : String(m == null ? '' : m)

  function getHost() {
    if (host && host.isConnected) return host
    host = document.createElement('div')
    host.className = 'toast-host'
    host.setAttribute('role', 'region')
    host.setAttribute('aria-label', 'Notifications')
    document.body.appendChild(host)
    return host
  }

  function toast(kind, message) {
    const msg = text(message)
    if (!msg) return
    const h = getHost()
    // The same failure repeated while one is still showing adds nothing.
    for (const el of h.children) {
      if (el.dataset.kind === kind && el.firstChild.textContent === msg) return
    }
    const el = document.createElement('div')
    el.className = 'toast toast--' + kind
    el.dataset.kind = kind
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status')
    const body = document.createElement('div')
    body.className = 'toast-text'
    body.textContent = msg
    const x = document.createElement('button')
    x.className = 'toast-x'
    x.type = 'button'
    x.setAttribute('aria-label', 'Dismiss')
    x.innerHTML = X_SVG
    el.append(body, x)
    let timer = null
    const close = () => { clearTimeout(timer); el.remove() }
    x.addEventListener('click', close)
    if (kind === 'info') timer = setTimeout(close, INFO_MS)
    h.appendChild(el)
    while (h.children.length > MAX_TOASTS) h.firstChild.remove()
  }

  function notice(message) {
    const msg = text(message)
    return new Promise(resolve => {
      const wrap = document.createElement('div')
      wrap.className = 'modal-overlay'
      wrap.innerHTML =
        '<div class="modal-card modal-card--notice" role="alertdialog" aria-modal="true">' +
        '<div class="modal-body"><div class="notice-text"></div></div>' +
        '<div class="modal-footer"><button class="btn btn-sm btn-primary" type="button">OK</button></div>' +
        '</div>'
      wrap.querySelector('.notice-text').textContent = msg
      const ok = wrap.querySelector('button')
      const close = () => { document.removeEventListener('keydown', onKey, true); wrap.remove(); resolve() }
      const onKey = e => {
        const open = document.querySelectorAll('.modal-overlay')
        if (open[open.length - 1] !== wrap) return
        if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close() }
      }
      ok.addEventListener('click', close)
      document.addEventListener('keydown', onKey, true)
      document.body.appendChild(wrap)
      ok.focus()
    })
  }

  return {
    error:  m => toast('error', m),
    info:   m => toast('info', m),
    notice,
  }
})()
