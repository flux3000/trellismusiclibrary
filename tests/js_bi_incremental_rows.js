// Extracts the Import page's pure row-update helpers from app.js and runs them
// against a fake before/after item list. Usage: node <this> app/static/js/app.js
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8')
function grab(name) {
  const i = src.indexOf(`function ${name}(`)
  if (i < 0) throw new Error('missing ' + name)
  const st = src.lastIndexOf('\n', i) + 1
  let j = src.indexOf('{', src.indexOf(')', i)), d = 0, k = j
  for (; k < src.length; k++) { if (src[k] === '{') d++; else if (src[k] === '}') { d--; if (d === 0) break } }
  return src.slice(st, k + 1)
}
const code = ['_biLiveRowIds', '_biPlanRowUpdates', '_biSyncRunCounts'].map(grab).join('\n')
const { _biLiveRowIds, _biPlanRowUpdates, _biSyncRunCounts } = new Function(code + '\nreturn { _biLiveRowIds, _biPlanRowUpdates, _biSyncRunCounts }')()
let bad = 0
const chk = (c, m) => { if (!c) { bad++; console.log('FAIL', m) } }
const queueBelongs = it => it.status !== 'ingested' && it.status !== 'moved'

const before = new Map([
  [1, { id: 1, status: 'ready', ingest_requested: true }],
  [2, { id: 2, status: 'ready', ingest_requested: true }],
  [3, { id: 3, status: 'review' }],
])
chk(_biLiveRowIds(before).join() === '1,2', 'live ids are the requested rows only')
// Row 1 finished ingesting, row 2 is now being worked, row 3 untouched.
const after = [{ id: 1, status: 'ingested', ingest_requested: false }, { id: 2, status: 'in_progress', ingest_requested: true }]
const plan = _biPlanRowUpdates(before, [1, 2], after, queueBelongs)
chk(plan.remove.join() === '1', 'ingested row removed: ' + plan.remove)
chk(plan.replace.map(i => i.id).join() === '2', 'in-progress row redrawn, row 3 untouched')
// A requested row that vanished from the server is dropped too.
chk(_biPlanRowUpdates(before, [1, 2], [after[1]], queueBelongs).remove.join() === '1', 'missing row removed')
// Nothing changed -> nothing to do.
const same = _biPlanRowUpdates(before, [1], [{ id: 1, status: 'ready', ingest_requested: true }], queueBelongs)
chk(!same.remove.length && !same.replace.length, 'unchanged row left alone')

// Badge follows the poll's counts without a request.
const runs = [{ id: 7, status: 'done', counts: { ready: 2, review: 1 } }]
chk(_biSyncRunCounts(runs, { id: 7, status: 'running', counts: { ready: 1, review: 1 } }) === true, 'badge change reported')
chk(runs[0].counts.ready === 1 && runs[0].status === 'running', 'entry updated')
chk(_biSyncRunCounts(runs, { id: 7, status: 'running', counts: { ready: 1, review: 1, ingested: 3 } }) === false, 'no change reported')
chk(_biSyncRunCounts(runs, { id: 99, counts: {} }) === false, 'unknown run ignored')
console.log(bad ? 'FAILED' : 'incremental ok')
process.exit(bad ? 1 : 0)
