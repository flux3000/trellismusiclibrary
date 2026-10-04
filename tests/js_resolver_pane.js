// Extracts the Resolver pane builder and the review-reason labels from app.js and runs
// them against resolver dicts, with and without evidence. Usage: node <this> app/static/js/app.js
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8')
function grabFn(name) {
  const i = src.indexOf(`function ${name}(`)
  if (i < 0) throw new Error('missing ' + name)
  const st = src.lastIndexOf('\n', i) + 1
  let j = src.indexOf('{', src.indexOf(')', i)), d = 0, k = j
  for (; k < src.length; k++) { if (src[k] === '{') d++; else if (src[k] === '}') { d--; if (d === 0) break } }
  return src.slice(st, k + 1)
}
function grabConst(name) {
  const i = src.indexOf(`const ${name} = `)
  if (i < 0) throw new Error('missing ' + name)
  const open = src[src.indexOf('=', i) + 2]
  const close = open === '[' ? ']' : '}'
  let k = src.indexOf(open, i), d = 0
  for (; k < src.length; k++) { if (src[k] === open) d++; else if (src[k] === close) { d--; if (d === 0) break } }
  return src.slice(i, k + 1)
}
const names = ['_resolverValueText', '_resolverFieldTitle', '_resolverRow', '_resolverPaneConflicts',
  '_resolverPaneFields', 'buildResolverPaneHtml', '_ingestReasonLabel', '_ingestReasonLabels']
const code = [grabConst('_RESOLVER_SOURCE_LABEL'), grabConst('_RESOLVER_PANE_FIELDS'),
  grabConst('_INGEST_FIELD_LABEL'), grabConst('_INGEST_REASON_LABEL'),
  'const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")',
  ...names.map(grabFn)].join('\n')
const api = new Function(code + `\nreturn { ${names.join(', ')} }`)()
let bad = 0
const chk = (c, m) => { if (!c) { bad++; console.log('FAIL', m) } }
const D = (y, m, d) => ({ year: y, month: m, day: d })
const ev = (source, text, line) => ({ source, text, line, role: null, extractor: null, score: 1, notes: '' })

// An older recording: no evidence anywhere, one conflict. Today's view, nothing else.
const old = {
  date: { value: D(1979, null, null), source: 'tags', conflict: true,
          candidates: { tags: D(1979, null, null), info: D(1980, 6, 14) } },
  artist: { value: 'Test Band', source: 'info', candidates: { info: 'Test Band' }, conflict: false },
}
let h = api.buildResolverPaneHtml(old)
chk(h.includes('Date') && h.includes('1980-06-14') && h.includes('Info File'), 'old row shows the conflict')
chk(!h.includes('Test Band') && !h.includes('Tentative'), 'old row lists only conflicts')
chk(api.buildResolverPaneHtml({ artist: old.artist }) === '', 'old row with no conflict renders no pane')
chk(api.buildResolverPaneHtml(null) === '' && api.buildResolverPaneHtml(undefined) === '', 'no resolver data, no pane')

// A new recording: every field with a value is listed, a tentative one is marked, the
// runner-up shows, source and line number come through, and text is escaped.
const nw = {
  date: { value: D(1991, 7, 26), source: 'info', conflict: false, confidence: 'confident',
          candidates: {}, evidence: [ev('info', 'July 26th 1991', 3), ev('folder', '1991-07-26', null)], runner_up: null },
  artist: { value: 'Fela Kuti', source: 'info', conflict: false, confidence: 'tentative', candidates: {},
            evidence: [ev('info', 'Fela <b>Kuti</b>', 0), ev('atlas', 'Fela Kuti', 0), ev('library', 'Fela Kuti', 0)],
            runner_up: { value: 'Femi Kuti', source: 'info', text: 'Femi Kuti', line: 5, score: 4 } },
  venue: { value: null, source: null, conflict: false, confidence: 'empty', candidates: {}, evidence: [], runner_up: null },
  city: { value: 'Berkeley', source: 'info', conflict: false, confidence: 'confident', candidates: {},
          evidence: [ev('info', 'Berkeley, CA', 2)], runner_up: null },
}
h = api.buildResolverPaneHtml(nw)
chk(h.includes('Date') && h.includes('1991-07-26') && h.includes('July 26th 1991'), 'date row')
chk(h.includes('Artist') && h.includes('Tentative'), 'tentative artist is marked')
chk((h.match(/Tentative/g) || []).length === 1, 'only the tentative field is marked')
chk(h.includes('Femi Kuti'), 'runner-up shown')
chk(h.includes('Your Library') && h.includes('Atlas') && h.includes('Folder') && h.includes('Info File'), 'source labels')
chk(h.includes('&lt;b&gt;Kuti&lt;/b&gt;') && !h.includes('<b>Kuti'), 'evidence text escaped')
chk(h.includes('>4<'), 'line numbers are 1-based (line index 3 shows 4)')
chk(!h.includes('Venue'), 'a field with no value is not listed')
chk(h.includes('Berkeley'), 'confident field listed without a mark')
chk(!/em dash|—/.test(h), 'no em dash in the pane')

// A new recording whose sources disagree lists the other source's value.
const cf = { date: { value: D(1979, 6, 14), source: 'info', conflict: true, confidence: 'tentative',
  candidates: { info: D(1979, 6, 14), tags: D(1980, 6, 14) }, evidence: [ev('info', 'June 14, 1979', 1)], runner_up: null } }
h = api.buildResolverPaneHtml(cf)
chk(h.includes('1980-06-14') && h.includes('Tags'), 'conflicting source value listed')

// Review reasons.
chk(api._ingestReasonLabel('tentative:artist') === 'Check artist', 'tentative artist label')
chk(api._ingestReasonLabel('tentative:date') === 'Check date', 'tentative date label')
chk(api._ingestReasonLabels('needs_day,tentative:artist').join('|') === 'Year and month known, day missing|Check artist', 'labels split')
console.log(bad ? 'FAILED' : 'resolver pane ok')
process.exit(bad ? 1 : 0)
