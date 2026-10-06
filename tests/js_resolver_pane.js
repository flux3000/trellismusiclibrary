// Extracts the review-reason labels from app.js and runs them. Usage: node <this> app/static/js/app.js
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
const names = ['_ingestReasonLabel', '_ingestReasonLabels']
const code = [grabConst('_INGEST_FIELD_LABEL'), grabConst('_INGEST_REASON_LABEL'),
  'const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")',
  ...names.map(grabFn)].join('\n')
const api = new Function(code + `\nreturn { ${names.join(', ')} }`)()
let bad = 0
const chk = (c, m) => { if (!c) { bad++; console.log('FAIL', m) } }
// Review reasons.
chk(api._ingestReasonLabel('tentative:artist') === 'Check artist', 'tentative artist label')
chk(api._ingestReasonLabel('tentative:date') === 'Check date', 'tentative date label')
chk(api._ingestReasonLabels('needs_day,tentative:artist').join('|') === 'Year and month known, day missing|Check artist', 'labels split')
console.log(bad ? 'FAILED' : 'reason labels ok')
process.exit(bad ? 1 : 0)
