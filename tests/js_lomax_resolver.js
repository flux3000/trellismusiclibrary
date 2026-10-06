// Runs the real Lomax helpers from app.js headlessly: the Resolver table (separate Title, Songwriter
// and Notes cells, each with its own accept and dismiss), Accept all, the Genre row, the stored
// decision being re-applied to a fresh form, the Ask button, the billing comparison and the import
// queue's Metadata pill. Usage: node <this> app/static/js/app.js
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8')
const a = src.indexOf('  // ── Lomax ─────')
const b = src.indexOf('  /** Recording detail — split panel: tracks + info file */')
if (a < 0 || b < 0) throw new Error('lomax section not found')
function grabFn(name) {
  const i = src.indexOf(`function ${name}(`)
  if (i < 0) throw new Error('missing ' + name)
  const st = src.lastIndexOf('\n', i) + 1
  let j = src.indexOf('{', src.indexOf(')', i)), d = 0, k = j
  for (; k < src.length; k++) { if (src[k] === '{') d++; else if (src[k] === '}') { d--; if (d === 0) break } }
  return src.slice(st, k + 1)
}
const stubs = `
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  const icon = n => '<i data-ic="' + n + '"></i>'
  let _edit = true
  const canEditLibrary = () => _edit
  const stripCitations = s => s
  let appPrefs = { has_api_key: true }
  const API = { lomax: {} }
  const localStorage = { getItem: () => null, setItem: () => {} }
  const document = { addEventListener() {}, querySelectorAll: () => [], body: { contains: () => true } }
  const window = { innerHeight: 800, innerWidth: 1200 }
  const setInterval = () => 0, clearInterval = () => {}
  const _lxImportLevelChanged = () => {}
`
const section = src.slice(a, b)
const names = ['lxRecordingResolverOpts', 'lxFingerprint', 'lomaxResolverHtml', 'lxSyncResolver', 'lxAcceptedToReapply', 'lomaxAskBar', 'lxFieldProps', 'lxTrackProps']
const api = new Function(stubs + section + `\nreturn { ${names.join(', ')}, setEdit: v => { _edit = v } }`)()
const normBilling = new Function(grabFn('_normBilling') + '\nreturn _normBilling')()
const iqBandPill = new Function("const esc = s => String(s)\nconst _LQ_BAND_TEXT = { red: 'Low', green: 'High' }\n" + grabFn('_iqBandPill') + '\nreturn _iqBandPill')()

let bad = 0
const chk = (c, m) => { if (!c) { bad++; console.log('FAIL', m) } }
const count = (h, re) => (h.match(re) || []).length

// A finished run: Title, Songwriter and Note proposals for track 1 (all open), a Songwriter for
// track 2 that Lomax agrees with, and a state proposal.
const P = (id, field, proposed, extra) => Object.assign({ id, field, proposed, current: '', decision: null, agrees: false }, extra)
const run = { id: 1, status: 'done', level: 'study', finished_at: '2026-10-05T10:00:00Z', result: { proposals: [
  P(1, 'state', 'TN'),
  P(2, 'track.1.title', 'Nardis'), P(3, 'track.1.songwriter', 'Miles Davis'), P(4, 'track.1.note', 'with Jerry Garcia on pedal steel'),
  P(5, 'track.2.songwriter', 'Bill Evans', { agrees: true }),
  P(6, 'genre', 'Jazz'),
] } }
const ctl = (r, extra) => Object.assign({ latest: () => r, lastError: () => null, pending: null }, extra)
const tracks = [{ track_number: 1, title: 'Track 1', songwriter: '', notes: '' },
                { track_number: 2, title: 'Peri', songwriter: 'Bill Evans', notes: '' }]
const opts = (o) => Object.assign({ resolved: {}, value: k => ({ artist: 'X', state: '' })[k] || '', tracks }, o)

let h = api.lomaxResolverHtml(ctl(run), opts({ genreRow: true }))
// One Lomax cell, one accept and one dismiss, per piece.
chk(count(h, /data-lx-ids="2"/g) === 2, 'title has its own accept and dismiss')
chk(count(h, /data-lx-ids="3"/g) === 2, 'songwriter has its own accept and dismiss')
chk(count(h, /data-lx-ids="4"/g) === 2, 'note has its own accept and dismiss')
chk(!/data-lx-act="(accept|dismiss)" data-lx-ids="[^"]*,/.test(h), 'no row shares one action across pieces')
chk(/>Title<\/th>/.test(h) && />Songwriter<\/th>/.test(h) && />Notes<\/th>/.test(h), 'each piece is labelled')
chk(h.includes('<th>Trellis</th><th>Lomax</th>'), 'track table has Trellis and Lomax columns')
chk(h.includes('data-lx-tp="1:title"') && h.includes('data-lx-tp="2:songwriter"'), 'Trellis values have their own cells')
chk(h.includes('Miles Davis') && h.includes('Bill Evans'), 'Lomax values shown')
chk(!/Songwriter: |Note: /.test(h), 'the labelled rows replace the prefixes')
// A "Tracks" header above the table; the column header row stays; a track's pieces share one # cell.
chk(h.includes('<div class="lx-h">Tracks</div><table class="lx-tbl lx-tracks">'), 'Tracks header above the track table')
chk(h.includes('<th>#</th><th>Track</th><th>Trellis</th><th>Lomax</th><th class="lx-act-td">'), 'the column header row is kept')
chk(/<td class="lx-n" rowspan="3">01<\/td>/.test(h) && !/<td class="lx-n"><\/td>/.test(h), 'a track\'s Title, Songwriter and Notes share its # cell')
// Accept all: titles and songwriters, never notes.
const all = /data-lx-act="accept-all" data-lx-ids="([^"]*)"/.exec(h)
chk(all && all[1] === '2,3', 'Accept all takes title and songwriter ids only, got ' + (all && all[1]))
// An empty Trellis songwriter/notes with no Lomax value gets no row; track 2 has a songwriter, so it does.
chk(count(h, /data-lx-tp="2:notes"/g) === 0, 'an empty pair is not a row')
chk(count(h, /data-lx-tp="2:songwriter"/g) === 1, 'a filled Trellis value is a row')
// Agreeing value is quiet and has no buttons.
chk(/<td data-lx-tp="2:songwriter">Bill Evans<\/td><td><span class="lx-same">Bill Evans<\/span><\/td><td class="lx-act-td"><\/td>/.test(h), 'agreement is quiet with no actions')
// Lomax values stay blue (lx-val); no confidence shown anywhere.
chk(h.includes('<span class="lx-val">Nardis</span>'), 'Lomax value in the Lomax class')
chk(!/confidence|high|medium/i.test(h), 'no confidence shown')
// Genre row: Lomax's proposal with actions.
chk(/<th scope="row">Genre<\/th>[\s\S]*?<span class="lx-val">Jazz<\/span>/.test(h) && h.includes('data-lx-ids="6"'), 'genre row carries Lomax\'s proposal and actions')
// Decide Title and Songwriter, dismiss Notes: states render independently.
const run2 = JSON.parse(JSON.stringify(run))
run2.result.proposals[1].decision = 'accepted'; run2.result.proposals[2].decision = 'accepted'; run2.result.proposals[3].decision = 'rejected'
h = api.lomaxResolverHtml(ctl(run2), opts({}))
chk(count(h, /Accepted<\/span>/g) === 2 && !h.includes('pedal steel'), 'accepted pieces say Accepted; the dismissed note is gone')
chk(!/data-lx-act="accept-all"/.test(h), 'no Accept all once nothing is open')

// No genre row unless the act needs one; MusicBrainz suggestion shown neutral with its own icons.
h = api.lomaxResolverHtml(ctl(null), opts({}))
chk(!h.includes('>Genre<'), 'no genre row for an act that has a genre')
h = api.lomaxResolverHtml(ctl(null), opts({ genreRow: true, mbGenre: { name: 'Hard Bop', state: undefined } }))
chk(/class="lx-mb">Hard Bop</.test(h) && h.includes('data-lx-act="mb-genre-accept"') && h.includes('data-lx-act="mb-genre-dismiss"'), 'MusicBrainz genre offered with accept and dismiss')
chk(!/class="lx-val">Hard Bop/.test(h), 'MusicBrainz genre is not in Lomax blue')
h = api.lomaxResolverHtml(ctl(null), opts({ genreRow: true, mbGenre: { name: 'Hard Bop', state: 'dismissed' } }))
chk(!h.includes('Hard Bop'), 'a dismissed suggestion is gone')
h = api.lomaxResolverHtml(ctl(null), opts({ genreRow: true, mbGenre: { name: 'Hard Bop', state: 'accepted' } }))
chk(h.includes('Accepted') && !h.includes('mb-genre-accept'), 'an accepted suggestion says Accepted')
// Playback mode: no controls.
api.setEdit(false)
h = api.lomaxResolverHtml(ctl(run), opts({ genreRow: true }))
chk(!/data-lx-act="(accept|dismiss|accept-all|mb-genre)/.test(h), 'no accept or dismiss in Playback')
api.setEdit(true)

// ── The stored decision is the truth ─────────────────────────────────────────
const stored = [
  { id: 10, status: 'done', result: { proposals: [P(1, 'venue', 'Barley\'s Tap Room', { decision: 'accepted' }), P(2, 'city', 'Knoxville'),
    P(3, 'track.1.title', 'Nardis', { decision: 'accepted' }), P(4, 'track.1.songwriter', 'Miles Davis', { decision: 'rejected' }),
    P(5, 'state', 'TN', { decision: 'accepted', agrees: true })] } },
  { id: 11, status: 'done', result: { proposals: [P(6, 'venue', 'Barleys Tap Room', { decision: 'accepted' }), P(7, 'genre', 'Jazz', { decision: 'accepted' })] } },
  { id: 12, status: 'error', result: null },
]
const form = { venue: '', city: '', 'track.1.title': 'Track 1', genre: '' }
const read = f => form[f] || ''
let todo = api.lxAcceptedToReapply(stored, read)
chk(todo.map(p => p.field).sort().join() === 'genre,track.1.title,venue', 'accepted values that differ come back: ' + todo.map(p => p.field))
chk(todo.find(p => p.field === 'venue').proposed === 'Barleys Tap Room', 'the newest accepted proposal per field wins')
// A value the form already holds is not touched; rejected and agreeing ones are never re-applied.
form.venue = 'Barleys Tap Room'; form['track.1.title'] = 'Nardis'
todo = api.lxAcceptedToReapply(stored, read)
chk(todo.map(p => p.field).join() === 'genre', 'equal values are left alone, got ' + todo.map(p => p.field))
// Already applied in this form (by id): skipped.
todo = api.lxAcceptedToReapply(stored, read, p => p.id === 7)
chk(todo.length === 0, 'a proposal this form already took is skipped')
chk(api.lxAcceptedToReapply([], read).length === 0 && api.lxAcceptedToReapply(null, read).length === 0, 'no runs, nothing to do')

// ── B3: show identity and hand edits ─────────────────────────────────────────
const scanA = { audio_file_count: 2, audio_files: [{ rel_path: 'a.flac' }, { rel_path: 'b.flac' }], info_file_content: 'Go Kurosawa\n01. Nardis' }
const scanB = { audio_file_count: 2, audio_files: [{ rel_path: 'a.flac' }, { rel_path: 'b.flac' }], info_file_content: 'Another Band\n01. Other' }
const fpA = api.lxFingerprint(scanA), fpB = api.lxFingerprint(scanB)
chk(fpA && fpA !== fpB && fpA === api.lxFingerprint(JSON.parse(JSON.stringify(scanA))), 'fingerprint identifies content, not path')
const fpRuns = [
  { id: 20, status: 'done', fingerprint: fpA, result: { proposals: [P(1, 'venue', 'Barley\'s', { decision: 'accepted' })] } },
  { id: 21, status: 'done', fingerprint: fpB, result: { proposals: [P(2, 'city', 'Elsewhere', { decision: 'accepted' })] } },
  { id: 22, status: 'done', result: { proposals: [P(3, 'state', 'XX', { decision: 'accepted' })] } },
]
const none = () => ''
chk(api.lxAcceptedToReapply(fpRuns, none, null, fpA).map(p => p.field).join() === 'venue', 'same path, different content: not re-applied; a run without identity is left alone')
chk(api.lxAcceptedToReapply(fpRuns, none, null, fpB).map(p => p.field).join() === 'city', 'the other show gets its own run only')
const dirty = { venue: true }
chk(api.lxAcceptedToReapply(fpRuns, none, p => dirty[p.field], fpA).length === 0, 'a field typed by hand is never re-applied')
chk(/oninput = ev =>/.test(src) && /_ingestMarkDirty\(`track\.\$\{ingest\.tracks\[i\]\.track_number\}\.note`\)/.test(src), 'typing and inline track edits mark fields dirty')

// ── Ask Lomax button: no accent class, one dedicated token class ─────────────
const bar = api.lomaxAskBar({})
chk(/class="btn btn-sm lx-go"/.test(bar) && !/btn-primary/.test(bar), 'Ask Lomax uses the Lomax token class only')

// ── 9a billing comparison ────────────────────────────────────────────────────
chk(normBilling('Bela Fleck and Edgar Meyer') === normBilling('Bela Fleck & Edgar Meyer'), '& and "and" read the same')
chk(normBilling('The Meters') === normBilling('Meters') && normBilling('Béla Fleck') === 'bela fleck', 'The and accents ignored')
chk(normBilling('Bela Fleck') !== normBilling('Bela Fleck and Edgar Meyer'), 'a different billing differs')

// ── 9b no Metadata pill without a band ───────────────────────────────────────
chk(iqBandPill(null) === '' && iqBandPill(undefined) === '', 'no pill when meta_band is null')
chk(iqBandPill('green').includes('High'), 'a real band still renders')
chk(!/row\.meta_band \|\| 'red'/.test(src), 'the red default is gone')

// ── 9a initAddArtistMembers: the resolver's Members only for the unchanged billing, no similar act ──
function grabConst1(name) {
  const i = src.indexOf(`const ${name} = `)
  const e = src.indexOf('\n', i)
  return src.slice(i, e)
}
const buildInit = (ingestState, apiState) => new Function('ingest', 'API', [grabConst1('_NAME_SPLIT_RE'), grabFn('splitArtistNameCandidates'),
  grabFn('_normBilling'), grabFn('initAddArtistMembers')].join('\n') + '\nreturn initAddArtistMembers')(ingestState, apiState)
async function members(name, billing, opts) {
  const ingestState = { form: { artist_name: name }, scan: { resolved: { artist: { value: billing }, members: ['Bela Fleck', 'Edgar Meyer'] } } }
  let asked = 0
  const apiState = {
    artists: { search: async () => [], get: async () => ({}) },
    musicians: { search: async () => [] },
    ingest: { similarActs: async () => { asked++; if (opts.fail) throw new Error('x'); return { acts: opts.similar ? [{ id: 1, name: 'Bela Fleck & Edgar Meyer' }] : [] } } },
  }
  await buildInit(ingestState, apiState)({ renderChips() {}, setGenreFromArtist() {} })
  return { names: ingestState.form.members.map(m => m.name), asked }
}
;(async () => {
  let r = await members('Bela Fleck and Edgar Meyer', 'Bela Fleck and Edgar Meyer', {})
  chk(r.names.join() === 'Bela Fleck,Edgar Meyer', 'unchanged billing, no similar act: Members pre-filled, got ' + r.names)
  r = await members('Bela Fleck and Edgar Meyer', 'Bela Fleck and Edgar Meyer', { similar: true })
  chk(r.names.length === 0 && r.asked === 1, 'a similar act exists: no pre-fill')
  r = await members('Bela Fleck Duo', 'Bela Fleck and Edgar Meyer', {})
  chk(r.names.length === 0 && r.asked === 0, 'artist changed from the billing: the resolver Members are not read')
  r = await members('The Bela Fleck & Edgar Meyer', 'Bela Fleck and Edgar Meyer', {})
  chk(r.names.join() === 'Bela Fleck,Edgar Meyer', 'the billing compares by library name rules')
  r = await members('Bela Fleck and Edgar Meyer', 'Bela Fleck and Edgar Meyer', { fail: true })
  chk(r.names.length === 0, 'an unanswered similar-act check does not pre-fill')
  // genre_source: a hand pick says 'hand', an accepted suggestion says 'suggestion', the act's own genre says nothing
  chk(/const setGenre = \(\{ id, name \}, source\) =>[\s\S]*?genre_source = source === undefined \? 'hand' : source/.test(src), 'the Genre field defaults to a hand pick')
  chk(src.includes("{ id: null, name }, 'suggestion')"), 'an accepted suggestion is marked as one')
  chk(/store\.genre_source = null/.test(src), 'the act\'s own genre carries no source')
  chk(/genre_source: f\.genre_source \|\| null,/.test(src), 'Confirm sends genre_source')
  // View Recording uses this same renderer: a saved recording's values in the Trellis column, the stored
// evidence for Sources and Tentative, the run on the recording in the Lomax column.
{
  const rec = { source: 'SBD', lineage: 'DAT > FLAC', tracks, resolver_json: { artist: { confidence: 'tentative' },
    sources_plain: { artist: [{ label: 'Info File', text: 'Miles Davis' }] } } }
  const perf = { artist: 'Miles Davis', venue_name: 'Fillmore', city: 'SF', state: 'CA', country: 'US', event_name: null,
    stage: null, start_year: 1971, start_month: 10, start_day: 26, artist_genre: null }
  const ro = api.lxRecordingResolverOpts(rec, perf)
  chk(ro.value('date') === '1971-10-26' && ro.value('artist') === 'Miles Davis' && ro.value('lineage') === 'DAT > FLAC', 'saved values')
  chk(ro.value('event') === '' && ro.genreRow === true && ro.tracks === tracks, 'empty values, genre row for an act with none')
  let v = api.lomaxResolverHtml(ctl(run), ro)
  chk(count(v, /<th scope="row">/g) >= 11 && v.includes('Tentative') && v.includes('data-lx-ask'), 'same table, tentative mark, ask bar')
  chk(v.includes('Nardis') && v.includes('lx-val'), 'Lomax column from the run on the recording')
  api.setEdit(false)
  v = api.lomaxResolverHtml(ctl(run), ro)
  chk(!v.includes('data-lx-ask') && !/data-lx-act="(accept|dismiss)"/.test(v), 'read-only: no ask bar, no actions')
  api.setEdit(true)
  v = api.lomaxResolverHtml(ctl(null), api.lxRecordingResolverOpts({ tracks: [] }, null))
  chk(v.includes('Field') && v.includes('data-lx-ask') && !v.includes('lx-tent'), 'no run, no resolver_json, no perf: empty columns, ask bar present')
}
// The MusicBrainz Genre row looks up the act the form will save: the Artist field now, debounced, cached.
chk(/name = \(getFormField\('artist'\) \|\| f\.artist_name \|\| ''\)\.trim\(\)/.test(src), 'genre lookup reads the Artist field')
chk(/f\._mbCache\[key\] = got/.test(src) && /setTimeout\(offerGenre, 600\)/.test(src), 'genre lookup is cached and debounced')
chk(/addEventListener\('input', scheduleGenre\)/.test(src) && /await applyLocal\(props\); scheduleGenre\(\)/.test(src), 'genre lookup re-queries when the artist is typed or accepted')
// The page itself must build the table through the shared renderer and keep the old pane out.
chk(/lomaxResolverHtml\(ctl, lxRecordingResolverOpts\(rec, perf\)\)/.test(src) && !/buildResolverPaneHtml/.test(src), 'View Recording mounts the shared renderer')

console.log(bad ? 'FAILED' : 'lomax resolver ok')
  process.exit(bad ? 1 : 0)
})()
