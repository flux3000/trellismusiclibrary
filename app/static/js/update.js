/* update.js -- tells the owner a newer Trellis exists. Check-only: it never
 * downloads or installs. The server decides who may see it (admin only), so a
 * 403 or any failure here is silent.
 *
 * Loaded last; talks to /api/system/update directly so it does not depend on
 * app.js internals.
 */
(() => {
  const DELAY_MS = 8000

  function openExternal(url) {
    if (!url) return
    const api = window.pywebview && window.pywebview.api
    if (api && api.open_in_browser) api.open_in_browser(url)
    else window.open(url, '_blank', 'noopener')
  }

  async function getJSON(path, opts) {
    const res = await fetch(path, { credentials: 'same-origin', ...opts })
    if (!res.ok) throw new Error(String(res.status))
    return res.json()
  }

  function show(info) {
    const wrap = document.createElement('div')
    wrap.className = 'modal-overlay'
    wrap.innerHTML =
      '<div class="modal-card modal-card--notice" role="dialog" aria-modal="true">' +
      '<div class="modal-header"><h3></h3></div>' +
      '<div class="modal-body"><div class="notice-text"></div></div>' +
      '<div class="modal-footer">' +
      '<button class="btn btn-sm btn-ghost" type="button" data-act="skip">Skip this version</button>' +
      '<button class="btn btn-sm btn-ghost" type="button" data-act="later">Later</button>' +
      '<button class="btn btn-sm btn-primary" type="button" data-act="download">Download</button>' +
      '</div></div>'
    wrap.querySelector('h3').textContent = 'Trellis ' + info.latest + ' is available'
    const body = wrap.querySelector('.notice-text')
    body.textContent = 'You have ' + info.current + '.'
    if (info.notes_url) {
      const a = document.createElement('a')
      a.href = info.notes_url
      a.textContent = 'Release notes'
      a.addEventListener('click', e => { e.preventDefault(); openExternal(info.notes_url) })
      body.append(document.createElement('br'), a)
    }
    const dl = wrap.querySelector('[data-act="download"]')
    if (!info.download_url) dl.hidden = true

    const close = () => { document.removeEventListener('keydown', onKey, true); wrap.remove() }
    const onKey = e => {
      const open = document.querySelectorAll('.modal-overlay')
      if (open[open.length - 1] !== wrap) return
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close() }
    }
    wrap.addEventListener('click', e => {
      const act = e.target.closest('[data-act]')
      if (!act) return
      if (act.dataset.act === 'download') { openExternal(info.download_url); close() }
      else if (act.dataset.act === 'later') close()
      else if (act.dataset.act === 'skip') {
        getJSON('/api/system/update/skip', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version: info.latest }),
        }).catch(() => {})
        close()
      }
    })
    document.addEventListener('keydown', onKey, true)
    document.body.appendChild(wrap)
    dl.hidden ? wrap.querySelector('[data-act="later"]').focus() : dl.focus()
  }

  async function run() {
    try {
      const info = await getJSON('/api/system/update')
      if (info && info.available && !info.skipped) show(info)
    } catch (e) { /* silent by design */ }
  }

  setTimeout(run, DELAY_MS)
})()
