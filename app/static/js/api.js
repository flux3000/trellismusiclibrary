/**
 * api.js — Thin wrapper around fetch for all Flux Audio API calls.
 * All functions return parsed JSON or throw on non-2xx responses.
 * Authentication is cookie-based (Flask-Login session).
 */

const API = (() => {

  // ── Library context (peer sharing milestone 2, 2026-08-08) ─────────────────
  //
  // `null` = my own library. A number = the id of a joined remote_node, and
  // every eligible request is rewritten to travel through the local proxy:
  //
  //     /api/performers/12  →  /api/remotes/3/performers/12
  //
  // Rewriting HERE, in one function, is what lets the existing Performer,
  // Venue, Artist and Genre pages render a remote library with no changes of
  // their own. The alternative — a parallel set of peer-only pages — was
  // rejected in the Peer UX design session precisely because it would fall out
  // of step with the local pages within a month.
  //
  // switchLibrary() in app.js is the only caller of setLibraryContext.
  let _libraryContext = null

  // Only these first path segments have a peer-facing equivalent in
  // api/share.py. Everything else — auth, preferences, peers, remotes, ingest,
  // quality — is LOCAL ONLY and must never be rewritten: asking someone else's
  // node about my preferences is meaningless, and asking it about my peers
  // would be a bug worth catching loudly rather than proxying politely.
  const REMOTE_CAPABLE = new Set([
    'collections', 'recordings', 'performances', 'performers', 'venues',
    'artists', 'genres', 'stream', 'search',
  ])

  function contextualise(path) {
    if (_libraryContext == null) return path
    const m = path.match(/^\/api\/([^/?]+)/)
    if (!m || !REMOTE_CAPABLE.has(m[1])) return path
    return `/api/remotes/${_libraryContext}` + path.slice('/api'.length)
  }

  async function request(method, path, body) {
    // Backstop: nothing may be written to a library that isn't mine. The
    // visible guard is the CSS gate that hides edit controls in peer mode, but
    // a gate depends on every control being tagged, and one that isn't would
    // otherwise send a PUT to a proxy that only speaks GET. Failing here means
    // a missed tag is a console error in development, not a confusing 403 in
    // front of a user.
    // Scoped to paths that would actually be proxied — leaving a remote is a
    // DELETE against MY database (/api/remotes/<id>) and must still work while
    // that remote is the one on screen.
    if (method !== 'GET' && contextualise(path) !== path) {
      throw new Error('This library is read-only — you are viewing a shared library.')
    }

    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
    }
    if (body !== undefined) opts.body = JSON.stringify(body)

    const res = await fetch(contextualise(path), opts)
    if (res.status === 204) return null

    const data = await res.json()

    // The library drive vanished mid-request. The banner poll would notice
    // within 30s, but the user is looking at the consequence RIGHT NOW, so
    // announce it immediately. Dispatched as an event rather than calling
    // App directly: api.js loads before app.js and must not depend on it.
    if (res.status === 503 && data && data.code === 'library_disconnected') {
      window.dispatchEvent(new CustomEvent('flux:library-disconnected', { detail: data }))
    }

    if (!res.ok) {
      // Carry the status ON the error. A bare Error message forces callers to
      // string-match or, worse, assume — init() assumed every failure meant
      // "logged out", so a TypeError in the sidebar presented as a login
      // screen (2026-08-23). `status` lets a caller ask the only question
      // that actually matters: was this 401, or was it something else?
      const err = new Error(data.error || `HTTP ${res.status}`)
      err.status = res.status
      err.payload = data
      throw err
    }

    // Folder-rename-on-metadata-edit (app/utils/folder_naming.py) is
    // deliberately non-fatal — a filesystem problem must never block a
    // metadata save — so it never throws. It rides along as
    // folder_rename_error/-s on whichever PUT triggered it (recordings,
    // performances) instead. Checked centrally here rather than at each of
    // the dozen call sites that can cause a rename (quick edits, AI Assist
    // apply, members edits...) so it can't be missed at a call site nobody
    // remembered to check.
    if (data && (data.folder_rename_error || data.folder_rename_errors)) {
      const msgs = data.folder_rename_error ? [data.folder_rename_error] : data.folder_rename_errors
      console.warn('Folder rename failed:', msgs)
      setTimeout(() => alert(
        'Metadata saved, but the folder could not be renamed to match:\n' + msgs.join('\n')), 0)
    }

    return data
  }

  const get  = (path)        => request('GET',  path)
  const post = (path, body)  => request('POST', path, body)
  const put  = (path, body)  => request('PUT',  path, body)

  return {
    // ── Library context ─────────────────────────────────────────────────────
    setLibraryContext: (nodeId) => { _libraryContext = nodeId },
    getLibraryContext: ()       => _libraryContext,
    // Non-fetch consumers (an <img src>, the audio element) need the same
    // rewriting, since they never pass through request().
    resolve:           (path)   => contextualise(path),

    // ── Remotes (outbound sharing — libraries I consume) ─────────────────────
    // Local-only by definition: this is how I manage MY list of remotes, so it
    // is deliberately absent from REMOTE_CAPABLE above.
    remotes: {
      list:   ()        => get('/api/remotes/'),
      enroll: (invite)  => post('/api/remotes/enroll', { invite }),
      leave:  (id)      => request('DELETE', `/api/remotes/${id}`),
    },

    // MY favourites inside a library I joined. Local rows about someone else's
    // recordings — never proxied, never seen by the sharer.
    //
    // `remote-favorites` is deliberately absent from REMOTE_CAPABLE, so
    // contextualise() leaves these paths alone and the non-GET backstop above
    // lets the writes through. Starring is not an edit to their library; it
    // does not touch their node at all.
    remoteFavorites: {
      ids:    (nodeId)        => get(`/api/remote-favorites/${nodeId}/ids`),
      list:   (nodeId, card)  => get(`/api/remote-favorites/${nodeId}${card ? '?card=1' : ''}`),
      add:    (nodeId, recId) => post(`/api/remote-favorites/${nodeId}`, { recording_id: recId }),
      remove: (nodeId, recId) => request('DELETE', `/api/remote-favorites/${nodeId}/${recId}`),
    },

    // ── System ──────────────────────────────────────────────────────────────
    // Deliberately absent from REMOTE_CAPABLE: this reports on MY drive. When
    // browsing a peer's library the answer is still true and still worth
    // showing, but it must never be asked of the peer.
    system: {
      libraryStatus:  () => get('/api/system/library-status'),
      libraryRecheck: () => post('/api/system/library-recheck'),
      // Which version am I running, and where is my data? Read by Settings.
      about:          () => get('/api/system/about'),
    },

    // ── Auth ────────────────────────────────────────────────────────────────
    auth: {
      me:     ()             => get('/api/auth/me'),
      login:  (username, password) => post('/api/auth/login', { username, password }),
      logout: ()             => post('/api/auth/logout'),

      // ── Profile (2026-08-25) ───────────────────────────────────────────────
      // The display name and picture. NOT the username, which is the credential
      // and is not editable here.
      updateProfile: (data) => request('PATCH', '/api/auth/me', data),
      // Plain URL for an <img src>, cache-busted by the server so replacing a
      // picture actually repaints instead of showing the old one.
      avatarUrl:     (v)    => `/api/auth/me/avatar${v ? `?v=${encodeURIComponent(v)}` : ''}`,
      uploadAvatar:  async (file) => {
        const form = new FormData()
        form.append('image', file)
        const res = await fetch('/api/auth/me/avatar',
          { method: 'POST', body: form, credentials: 'same-origin' })
        const data = await res.json()
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
        return data
      },
      removeAvatar:  ()     => request('DELETE', '/api/auth/me/avatar'),
    },


    // ── Artists (people) ──────────────────────────────────────────────────────
    artists: {
      search: (q)        => get(`/api/artists/search?q=${encodeURIComponent(q)}`),
      list:   ()         => get('/api/artists/'),
      get:    (id)       => get(`/api/artists/${id}`),
      create: (data)     => post('/api/artists/', data),
      update: (id, data) => put(`/api/artists/${id}`, data),
      remove: (id)       => request('DELETE', `/api/artists/${id}`),
      addPerformer:    (id, data)   => post(`/api/artists/${id}/performers`, data),
      removePerformer: (id, perfId) => request('DELETE', `/api/artists/${id}/performers/${perfId}`),
    },

    // ── Collections ───────────────────────────────────────────────────────────
    collections: {
      list:            ()               => get('/api/collections/'),
      get:             (id)             => get(`/api/collections/${id}`),
      create:          (data)           => post('/api/collections/', data),
      update:          (id, data)       => put(`/api/collections/${id}`, data),
      remove:          (id)             => request('DELETE', `/api/collections/${id}`),
      addRecording:    (id, recId)      => post(`/api/collections/${id}/recordings`, { recording_id: recId }),
      removeRecording: (id, recId)      => request('DELETE', `/api/collections/${id}/recordings/${recId}`),
    },

    // ── Performers (acts) ─────────────────────────────────────────────────────
    performers: {
      search:        (q)         => get(`/api/performers/search?q=${encodeURIComponent(q)}`),
      list:          ()          => get('/api/performers/'),
      allRecordings: ()          => get('/api/performers/all-recordings'),
      get:           (id)        => get(`/api/performers/${id}`),
      recordings:    (id)        => get(`/api/performers/${id}/recordings`),
      create:        (data)      => post('/api/performers/', data),
      update:        (id, data)  => put(`/api/performers/${id}`, data),
      remove:        (id)        => request('DELETE', `/api/performers/${id}`),
      addStint:      (id, artistId, data) => post(`/api/performers/${id}/members/${artistId}/stints`, data),
      updateStint:   (stintId, data)      => put(`/api/performers/stints/${stintId}`, data),
      removeStint:   (stintId)            => request('DELETE', `/api/performers/stints/${stintId}`),

      // Profile pictures (2026-07-22; MULTI-IMAGE 2026-08-07) — a raw upload,
      // not JSON, so it bypasses request()'s JSON.stringify/Content-Type:
      // letting the browser set its own multipart boundary is required for a
      // file upload to parse server-side. imageUrl() is a plain URL string for
      // an <img src>, not a fetch call — the browser requests it directly
      // (same-origin session cookie covers the @login_required check, same as
      // the waveform/spectrogram images already do).
      //
      // Images are addressed BY IMAGE ID, not by performer: a performer now has
      // several and "the performer's image" no longer identifies one.
      // Contextualised: an <img src> never passes through request(), so it
      // would otherwise resolve against localhost while viewing a remote
      // library and 404 on every photo.
      imageUrl:     (imageId) => contextualise(`/api/performers/images/${imageId}`),
      listImages:   (id)      => get(`/api/performers/${id}/images`),
      // Accepts a FileList or array — the whole drop goes in one request.
      uploadImages: async (id, files) => {
        const form = new FormData()
        for (const f of Array.from(files)) form.append('image', f)
        const res = await fetch(`/api/performers/${id}/images`,
          { method: 'POST', body: form, credentials: 'same-origin' })
        const data = await res.json()
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
        return data
      },
      // Wikidata → Wikimedia Commons photo lookup. Returns {found:false} when
      // the act simply has no freely-licensed photo — an ordinary outcome, not
      // an error, so it resolves rather than throwing.
      fetchImage:      (id)            => post(`/api/performers/${id}/images/fetch`),
      setPrimaryImage: (imageId)       => post(`/api/performers/images/${imageId}/primary`),
      updateImage:     (imageId, data) => put(`/api/performers/images/${imageId}`, data),
      removeImage:     (imageId)       => request('DELETE', `/api/performers/images/${imageId}`),

      // AI Assist — AI-drafted bio + suggested resource links, background job
      // (same shape as API.ingest.aiAssist*). The ROUTES keep the older
      // "dossier" name: renaming a working endpoint to match a UI label buys
      // nothing and breaks anything already pointed at it. The user-facing
      // wording is AI Assist everywhere (Ryan, 2026-08-07).
      // Pre-flight cost RANGE for one pass — see utils/ai_assist.py.
      aiEstimate:     ()           => get('/api/performers/ai-estimate'),
      startDossier:   (id)         => post(`/api/performers/${id}/dossier`),
      dossierStatus:  (id, jobId)  => get(`/api/performers/${id}/dossier/${jobId}`),

      // MusicBrainz — structured facts, separate from AI Assist by design
      // (curated database, no hallucination surface; see utils/musicbrainz.py).
      // Looks up AND links if the match is unambiguous — see api/performers.py.
      // Returns {status:'matched'} or {status, candidates:[...]} to choose from.
      mbLookup:     (id, q)    => post(`/api/performers/${id}/musicbrainz/lookup`, q ? { q } : {}),
      mbCandidates: (id, q) =>
        get(`/api/performers/${id}/musicbrainz/candidates${q ? '?q=' + encodeURIComponent(q) : ''}`),
      mbResolve:    (id, mbid) => post(`/api/performers/${id}/musicbrainz`, { mbid }),
      mbMembers:    (id)       => get(`/api/performers/${id}/musicbrainz/members`),
    },

    // ── Peers (inbound sharing — who I share TO) ─────────────────────────────
    // Every one of these endpoints has existed since 2026-07-16 and was
    // curl-only until now; this is a client for them, not new surface.
    peers: {
      list:        ()             => get('/api/peers/'),
      get:         (id)           => get(`/api/peers/${id}`),
      create:      (data)         => post('/api/peers/', data),
      update:      (id, data)     => request('PATCH', `/api/peers/${id}`, data),
      revoke:      (id)           => post(`/api/peers/${id}/revoke`),
      addGrants:   (id, ids)      => post(`/api/peers/${id}/grants`, { collection_ids: ids }),
      revokeGrant: (id, colId)    => request('DELETE', `/api/peers/${id}/grants/${colId}`),
      // Returns the raw code ONCE — it is hashed at rest and unrecoverable.
      mintInvite:  (id, days)     => post(`/api/peers/${id}/invites`, days ? { expires_days: days } : {}),
      // Cancels ONE unused invite. Not `revoke`, which kills the peer.
      deleteInvite: (id, inviteId) => request('DELETE', `/api/peers/${id}/invites/${inviteId}`),
      activity:    (id)           => get(`/api/peers/${id}/activity`),
    },

    // ── Performances ─────────────────────────────────────────────────────────
    performances: {
      get:    (id)       => get(`/api/performances/${id}`),
      create: (data)     => post('/api/performances/', data),
      update: (id, data) => put(`/api/performances/${id}`, data),
      // Per-show instrument / note. NO CALLER as of 2026-08-22 — the inline
      // editor was cut from V1 (Ryan) and a name click now opens the Artist
      // page. Kept deliberately: the column, the resolver and the endpoint all
      // still carry this, and the UI is the only piece that went.
      updatePersonnelRow: (perfId, personnelId, data) =>
        put(`/api/performances/${perfId}/personnel/${personnelId}`, data),
    },

    // ── Recordings ───────────────────────────────────────────────────────────
    recordings: {
      get:        (id)       => get(`/api/recordings/${id}`),
      // Two independent opt-ins, both off by default so the List view's flat
      // table request is unchanged (see app/utils/serialize.py):
      //   `waveform` — downsampled peak strip. Nothing requests it since the
      //                card became a handbill; kept because it's real and tested.
      //   `card`     — genre colour + performer primary image, for Browse's
      //                Recently Added row cards.
      recent:     (limit, opts) => {
        const o = opts || {}
        const qs = [`limit=${limit || 50}`]
        if (o.offset)   qs.push(`offset=${o.offset}`)
        if (o.waveform) qs.push('waveform=1')
        if (o.card)     qs.push('card=1')
        return get(`/api/recordings/recent?${qs.join('&')}`)
      },
      // Browse's Recommended module — 3 cards by default, seeded by date
      // (stable within a day). `reroll` is an incrementing counter kept in
      // memory client-side, not persisted — bumping it is the "Show me
      // three more" control.
      recommended: (limit, reroll) =>
        get(`/api/recordings/recommended?limit=${limit || 3}&reroll=${reroll || 0}`),
      // Browse's On This Day module — recordings whose date matches today's
      // month/day, any year. Empty most days; the module hides itself then.
      // Sends the BROWSER's month/day. "Today" depends on where the reader is
      // standing, and the server's UTC date is already tomorrow for most of a
      // US evening.
      onThisDay:  () => {
        const d = new Date()
        return get(`/api/recordings/on-this-day?month=${d.getMonth() + 1}&day=${d.getDate()}`)
      },
      // Sidebar Favorites. Complete rather than capped — see the endpoint.
      favorites:  ()          => get('/api/recordings/favorites'),
      scan:       (folder)   => post('/api/recordings/scan', { folder_path: folder }),
      update:     (id, data) => put(`/api/recordings/${id}`, data),
      // deleteFiles is an explicit opt-in from the confirm dialog's checkbox.
      // The server resolves the folder itself and refuses anything that does not
      // land inside LIBRARY_ROOT — the client never names a path.
      delete:     (id, deleteFiles) => request('DELETE',
        `/api/recordings/${id}${deleteFiles ? '?delete_files=1' : ''}`),
      writeTags:  (id)       => post(`/api/recordings/${id}/write-tags`),
      // Info file save — DB always, plus the .txt on disk when the library
      // folder already has one. Deliberately separate from update(): it can
      // touch the filesystem, so it is its own explicit action.
      saveInfoFile: (id, content) => post(`/api/recordings/${id}/info-file`, { content }),
      revealFolder: (id)     => post(`/api/recordings/${id}/reveal`),
      // Move the folder out of the library to Workshop/Backlog and unpublish
      // the row. Destination is a key, not a path — the server owns the paths.
      moveOut:    (id, destination) => post(`/api/recordings/${id}/move`, { destination }),
      fileTags:   (id)       => get(`/api/recordings/${id}/tags`),
      reprocess:  (id)       => post(`/api/recordings/${id}/reprocess`),
      verifyChecksums: (id)  => post(`/api/recordings/${id}/verify-checksums`),
    },

    // ── Tracks ───────────────────────────────────────────────────────────────
    tracks: {
      update:  (id, data) => put(`/api/tracks/${id}`, data),
      logPlay: (id, data) => post(`/api/tracks/${id}/play`, data),
    },

    // ── Venues ───────────────────────────────────────────────────────────────
    venues: {
      list:   (q)        => get(`/api/venues/${q ? '?q=' + encodeURIComponent(q) : ''}`),
      get:    (id)       => get(`/api/venues/${id}`),
      create: (data)     => post('/api/venues/', data),
      update: (id, data) => put(`/api/venues/${id}`, data),
      remove: (id)       => request('DELETE', `/api/venues/${id}`),

      // Photos (2026-08-07) — same shapes and semantics as performers, sharing
      // one server-side implementation (app/utils/entity_images.py), so the
      // frontend gallery component works against either namespace unchanged.
      imageUrl:        (imageId) => `/api/venues/images/${imageId}`,
      listImages:      (id)      => get(`/api/venues/${id}/images`),
      uploadImages:    async (id, files) => {
        const form = new FormData()
        for (const f of Array.from(files)) form.append('image', f)
        const res = await fetch(`/api/venues/${id}/images`,
          { method: 'POST', body: form, credentials: 'same-origin' })
        const data = await res.json()
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
        return data
      },
      setPrimaryImage: (imageId) => post(`/api/venues/images/${imageId}/primary`),
      removeImage:     (imageId) => request('DELETE', `/api/venues/images/${imageId}`),
    },

    // ── Genres ───────────────────────────────────────────────────────────────
    genres: {
      list:   (q)        => get(`/api/genres/${q ? '?q=' + encodeURIComponent(q) : ''}`),
      get:    (id)       => get(`/api/genres/${id}`),
      create: (data)     => post('/api/genres/', data),
      update: (id, data) => put(`/api/genres/${id}`, data),
      remove: (id)       => request('DELETE', `/api/genres/${id}`),
    },

    // ── Search (IO-46, 2026-08-18) ───────────────────────────────────────────
    //
    // Deliberately absent from REMOTE_CAPABLE above, so contextualise() never
    // rewrites it: this searches the LOCAL library only. A peer search has to
    // filter every group through the visible set or it leaks holdings, which
    // is IO-48's job. The Search Bar hides itself in peer mode (CSS) so the
    // box is never offered against a library it cannot answer for.
    search: {
      // Omnibox: a few per group plus honest totals.
      all:   (q, limit = 5) =>
        get(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),
      // Results page: one group, paged.
      group: (q, type, limit = 25, offset = 0) =>
        get(`/api/search?q=${encodeURIComponent(q)}&type=${encodeURIComponent(type)}` +
            `&limit=${limit}&offset=${offset}`),
    },

    // ── Events ───────────────────────────────────────────────────────────────
    events: {
      list:   (q)        => get(`/api/events/${q ? '?q=' + encodeURIComponent(q) : ''}`),
      search: (q)        => get(`/api/events/search?q=${encodeURIComponent(q)}`),
      get:    (id)       => get(`/api/events/${id}`),
      create: (data)     => post('/api/events/', data),
      update: (id, data) => put(`/api/events/${id}`, data),
    },

    // ── Preferences ──────────────────────────────────────────────────────────
    preferences: {
      get:    ()     => get('/api/preferences'),
      update: (data) => put('/api/preferences', data),
    },

    // ── Ingest ───────────────────────────────────────────────────────────────
    ingest: {
      confirm:       (data)  => post('/api/ingest/confirm', data),
      confirmStatus: (jobId) => get(`/api/ingest/confirm/${jobId}`),
      // Cooperative cancel. The worker stops between files, undoes its own
      // filesystem work and rolls back its uncommitted DB session. Recordings
      // already finished earlier in a queue are untouched.
      confirmCancel: (jobId) => post(`/api/ingest/confirm/${jobId}/cancel`, {}),
      aiAssist:          (payload) => post('/api/ingest/ai-assist', payload),
      aiAssistRecording: (recId)   => post(`/api/ingest/ai-assist-recording/${recId}`),
      aiAssistStatus:    (jobId)   => get(`/api/ingest/ai-assist/${jobId}`),
      saveInfoFile:   (payload) => post('/api/ingest/save-info-file', payload),
      checkExisting:  ({ artist_name, year, month, day }) => {
        const p = new URLSearchParams({ artist_name })
        p.set('year', year)
        if (month) p.set('month', month)
        if (day)   p.set('day', day)
        return get(`/api/ingest/check-existing?${p.toString()}`)
      },
      health:         (scan)    => post('/api/ingest/health', scan),
      batchScan:  (source_dir) => post('/api/ingest/batch-scan', { source_dir }),
    },

    // ── Listening Quality ────────────────────────────────────────────────────
    // Stage 1+2 of the unified ingestion flow (2026-07-30). See
    // app/api/quality.py for the endpoint contracts.
    quality: {
      analyze: (source_dir, reanalyze) => post('/api/quality/analyze', { source_dir, reanalyze: !!reanalyze }),
      analyzeStatus: (jobId, sourceDir) =>
        get(`/api/quality/analyze/${jobId}?source_dir=${encodeURIComponent(sourceDir)}`),
      triage:     (folder_path, status) => post('/api/quality/triage', { folder_path, status }),
      triageBulk: (folder_paths, status) => post('/api/quality/triage-bulk', { folder_paths, status }),
      staging:         (sourceDir)  => get(`/api/quality/staging?source_dir=${encodeURIComponent(sourceDir)}`),
      stagingFeatures: (folderPath) => get(`/api/quality/staging/features?folder_path=${encodeURIComponent(folderPath)}`),
      // features=1 also returns the plain-English `interpretation` block (group
      // verdicts + advanced metric rows), which is what the View Recording
      // Listening Quality pane renders. Without it you get the bare scores.
      forRecording:    (recId, features) =>
        get(`/api/quality/recording/${recId}${features ? '?features=1' : ''}`),
      // Physically moves a show out of the queue into Backlog or Working.
      // Touches real files — see app/api/quality.py::move_out_of_queue for the
      // guards (allowlisted destinations, import-root check, never overwrites).
      move: (folder_path, destination) => post('/api/quality/move', { folder_path, destination }),
      browse: (path) => get(`/api/quality/browse?path=${encodeURIComponent(path || '')}`),
      // The DEEP fingerprint pass — hashes whole files, so it is explicit and
      // per-folder. Triage verifies FFP/ST5 for free; MD5 waits for this.
      verifyFingerprints: (folder_path) =>
        post('/api/quality/verify-fingerprints', { folder_path }),
    },
  }
})()
