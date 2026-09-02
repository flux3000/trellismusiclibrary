/**
 * tools/check_js_names.mjs — scope-aware undefined-name check for the frontend.
 *
 * The JS twin of tests/test_no_undefined_names.py, and it exists for the same
 * reason that one does: `node --check` is a SYNTAX check, and whether a name
 * exists is a runtime question. A half-applied patch and a missing helper both
 * pass --check and are ReferenceErrors on the first click. app.js is one
 * 14,000-line IIFE, so a name that resolves to nothing has no module boundary
 * to trip over on the way to production.
 *
 * ⚠ OPTIONAL AND NOT WIRED INTO ANYTHING. It needs node plus two npm packages,
 * which this repo does not otherwise depend on, and adding a node toolchain to
 * a Python project is Ryan's call, not a side effect of a feature branch. Run
 * it by hand:
 *
 *     npm install acorn acorn-walk        # once, anywhere on the path
 *     node tools/check_js_names.mjs app/static/js/app.js
 *     node tools/check_js_names.mjs app/static/js/api.js
 *
 * Exits non-zero and lists line:column for anything unresolved.
 *
 * Scope awareness is the whole job — the Python checker's first version
 * reported sixteen false positives by ignoring closures, and a check that cries
 * wolf gets muted. This walks function, block, catch and class scopes, hoists
 * var and function declarations to the enclosing function scope and let/const/
 * class to the block, and skips non-reference positions (property keys,
 * non-computed member access, labels, declaration targets, params).
 *
 * ALWAYS run a positive control before trusting a clean result — append a call
 * to a name that certainly does not exist and confirm it is reported. Verified
 * that way 2026-09-01, against both files.
 *
 * GLOBALS below is a hand-maintained allowlist of browser and vendor globals.
 * A new legitimate global shows up here as a false positive; add it rather than
 * loosening the resolver.
 */
import fs from 'fs'
import * as acorn from 'acorn'
import * as walk from 'acorn-walk'

const file = process.argv[2]
const src = fs.readFileSync(file, 'utf8')
const ast = acorn.parse(src, { ecmaVersion: 2023, sourceType: 'script', locations: true })

const GLOBALS = new Set([
  'window','document','console','fetch','FormData','Date','Math','JSON','Object','Array',
  'String','Number','Boolean','Promise','Set','Map','WeakMap','Error','TypeError','RegExp',
  'setTimeout','clearTimeout','setInterval','clearInterval','requestAnimationFrame',
  'localStorage','sessionStorage','navigator','location','history','alert','confirm','prompt',
  'Intl','encodeURIComponent','decodeURIComponent','parseInt','parseFloat','isNaN','isFinite',
  'CustomEvent','Event','FileReader','Blob','URL','AbortController','Audio','Image','undefined',
  'NaN','Infinity','globalThis','arguments','this','WaveSurfer','API','Player','Debug','structuredClone',
  'IntersectionObserver','ResizeObserver','MutationObserver','performance','crypto','Uint8Array',
  'Float32Array','ArrayBuffer','DataView','TextDecoder','TextEncoder','queueMicrotask','Symbol',
  'CSS','URLSearchParams','Node','Element','HTMLElement','DOMParser','XMLHttpRequest','matchMedia','getComputedStyle','App',
])

// ── Collect declarations per scope ─────────────────────────────────────────
function newScope(parent, fnLike) { return { parent, fnLike, names: new Set() } }
const scopeOf = new Map()

function declarePattern(node, scope) {
  if (!node) return
  switch (node.type) {
    case 'Identifier': scope.names.add(node.name); break
    case 'ObjectPattern': node.properties.forEach(pr =>
      declarePattern(pr.type === 'RestElement' ? pr.argument : pr.value, scope)); break
    case 'ArrayPattern': node.elements.forEach(el => declarePattern(el, scope)); break
    case 'AssignmentPattern': declarePattern(node.left, scope); break
    case 'RestElement': declarePattern(node.argument, scope); break
  }
}

// Hoist var + function declarations to the nearest function scope; let/const/class
// to the nearest block scope.
function fnScope(s) { let c = s; while (c && !c.fnLike) c = c.parent; return c || s }

function build(node, scope) {
  scopeOf.set(node, scope)
  const isFn = /Function(Declaration|Expression)$|ArrowFunctionExpression/.test(node.type)
  const isBlock = node.type === 'BlockStatement' || node.type === 'Program' ||
                  node.type === 'ForStatement' || node.type === 'ForInStatement' ||
                  node.type === 'ForOfStatement' || node.type === 'SwitchStatement'
  let inner = scope
  if (isFn) {
    if (node.type === 'FunctionDeclaration' && node.id) fnScope(scope).names.add(node.id.name)
    inner = newScope(scope, true)
    if (node.id && node.type !== 'FunctionDeclaration') inner.names.add(node.id.name)
    node.params.forEach(p => declarePattern(p, inner))
  } else if (isBlock) {
    inner = newScope(scope, node.type === 'Program')
  } else if (node.type === 'CatchClause') {
    inner = newScope(scope, false)
    declarePattern(node.param, inner)
  } else if (node.type === 'ClassDeclaration' && node.id) {
    scope.names.add(node.id.name)
  }
  if (node.type === 'VariableDeclaration') {
    const target = node.kind === 'var' ? fnScope(scope) : scope
    node.declarations.forEach(d => declarePattern(d.id, target))
  }
  for (const key of Object.keys(node)) {
    const v = node[key]
    if (Array.isArray(v)) v.forEach(c => c && c.type && build(c, inner))
    else if (v && typeof v === 'object' && v.type) build(v, inner)
  }
}
build(ast, newScope(null, true))

// A second pass is needed because a function may be CALLED above its
// declaration (hoisting) — the build above already put declarations in their
// scope's set before any lookup happens here.
const problems = []
function resolve(name, scope) {
  let s = scope
  while (s) { if (s.names.has(name)) return true; s = s.parent }
  return GLOBALS.has(name)
}

walk.ancestor(ast, {
  Identifier(node, state, ancestors) {
    const parent = ancestors[ancestors.length - 2]
    if (!parent) return
    // Skip non-reference positions: property keys, member .props, labels,
    // declarations themselves, params.
    if (parent.type === 'MemberExpression' && parent.property === node && !parent.computed) return
    if (parent.type === 'Property' && parent.key === node && !parent.computed) return
    if (parent.type === 'MethodDefinition' && parent.key === node) return
    if (/Function|ClassDeclaration|ClassExpression/.test(parent.type) && parent.id === node) return
    if (parent.type === 'VariableDeclarator' && parent.id === node) return
    if (/Pattern$/.test(parent.type)) return
    if (parent.type === 'LabeledStatement' || parent.type === 'BreakStatement' ||
        parent.type === 'ContinueStatement') return
    if (Array.isArray(parent.params) && parent.params.includes(node)) return
    const scope = scopeOf.get(node) || scopeOf.get(parent)
    if (!resolve(node.name, scope)) {
      problems.push(`${node.loc.start.line}:${node.loc.start.column}  ${node.name}`)
    }
  },
})

const uniq = [...new Set(problems)]
if (uniq.length) {
  console.log(`${file}: ${uniq.length} unresolved identifier(s)`)
  uniq.slice(0, 60).forEach(l => console.log('  ' + l))
  process.exit(1)
}
console.log(`${file}: clean`)
