// tools/eslint.config.mjs — catch undefined names in the frontend (2026-08-28).
//
// WHY THIS EXISTS
//
// `node --check` checks SYNTAX. It cannot tell you that an identifier resolves
// to something you did not intend, because that is a scope question. This
// shipped and broke the Review & Ingest drill-in:
//
//     const open = lq.compactOpen.get(row.folder_path)   // ← line went missing
//     ...
//     ${open ? _lqDetail(row, open) : ''}
//
// With the declaration gone, `open` resolved to `window.open`. Truthy, so the
// panel rendered; never equal to a pane key, so every pane came out empty with
// no tab active. `node --check` passed. Nothing threw. The page just quietly
// did the wrong thing (Ryan, 2026-08-28).
//
// This is the JS counterpart of tests/test_no_undefined_names.py, and it exists
// for the same reason that file does.
//
// The globals list below is deliberately EXPLICIT rather than `env: browser`.
// The whole failure was a browser global silently standing in for a local, so
// enumerating what this app is actually allowed to reach off `window` is the
// point — `open`, `name`, `status`, `length`, `event` and `top` are all real
// window properties and none of them belong in this codebase unqualified.
//
// RUN IT (needs no repo dependency; npx fetches eslint into its own cache):
//     npx eslint@9 -c tools/eslint.config.mjs app/static/js/*.js
//
// Verified with a negative control: reintroducing the bug above makes this
// report `'open' is not defined`, five times, at the right lines.

const BROWSER = [
  'window', 'document', 'console', 'fetch', 'location', 'navigator', 'history',
  'screen', 'performance', 'matchMedia', 'getComputedStyle',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
  'requestAnimationFrame', 'cancelAnimationFrame', 'queueMicrotask',
  'localStorage', 'sessionStorage',
  'alert', 'confirm', 'prompt',
  'Audio', 'Image', 'FormData', 'Blob', 'FileReader', 'URL', 'URLSearchParams',
  'CustomEvent', 'Event', 'HTMLElement', 'Node', 'DOMParser', 'XMLHttpRequest',
  'WebSocket', 'AbortController', 'structuredClone', 'CSS',
  'IntersectionObserver', 'ResizeObserver', 'MutationObserver',
]

// This app's own globals.
const APP = ['App', 'API', 'Player', 'fluxDebug', 'pywebview']

export default [{
  files: ['**/*.js'],
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: 'script',
    globals: Object.fromEntries(
      [...BROWSER, ...APP].map(n => [n, 'readonly'])),
  },
  rules: { 'no-undef': 'error' },
}]
