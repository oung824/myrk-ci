import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const scanner = fileURLToPath(new URL('../scripts/comment-tokens.mjs', import.meta.url))
const desktop = fileURLToPath(new URL('..', import.meta.url))

function runScanner(sources) {
  return spawnSync(process.execPath, [scanner, desktop], {
    input: JSON.stringify(sources),
    encoding: 'utf8',
  })
}

function scan(filename, lines) {
  const result = runScanner({ [filename]: lines.join('\n') })
  assert.equal(result.status, 0, result.stderr)
  return JSON.parse(result.stdout)[filename]
}

test('scan code passed through an Electron renderer wrapper and respect shadowed names', () => {
  assert.deepEqual(scan('desktop/tests/connection.electron.mjs', [
    'let execute',
    'execute = script => window.webContents.executeJavaScript(script)',
    'await execute(`(async () => {',
    '  // renderer comment',
    '  return ${message}',
    '})()`)',
    'function log(execute) { execute("// payload text") }',
    'function format(code) {',
    '  function nested(code) { return window.webContents.executeJavaScript(code) }',
    '  return String(code)',
    '}',
    'format("/* nested function does not mark its parent */")',
    'let reused = code => window.webContents.executeJavaScript(code)',
    'reused("/* renderer code before rebinding */")',
    'reused = String',
    'reused("/* not renderer code after rebinding */")',
    'const payload = "/* data text */"',
  ]), [
    { line: 4, text: '// renderer comment' },
    { line: 14, text: '/* renderer code before rebinding */' },
  ])
})

test('scan direct Electron calls, including isolated renderer scripts', () => {
  assert.deepEqual(scan('desktop/src/main/sample.ts', [
    'await window.webContents.executeJavaScript("0;\\n// direct comment")',
    'await window.webContents.executeJavaScriptInIsolatedWorld(1001, [{ code: `(() => {',
    '  /* isolated comment */ return 1',
    '})()` }])',
  ]), [{ line: 1, text: '// direct comment' }, { line: 3, text: '/* isolated comment */' }])
})

test('keep lexical loop bindings inside the loop without hiding outer renderer calls', () => {
  assert.deepEqual(scan('desktop/sample.js', [
    'const execute = code => window.webContents.executeJavaScript(code)',
    'for (let execute = String; false;) execute("/* lexical for data */")',
    'for (const execute in {}) execute("/* lexical for-in data */")',
    'for (const execute of [String]) execute("/* lexical for-of data */")',
    'execute("/* outer renderer comment */")',
    'for (var forward = code => window.webContents.executeJavaScript(code); false;) {}',
    'forward("/* function-scoped renderer comment */")',
  ]), [
    { line: 5, text: '/* outer renderer comment */' },
    { line: 7, text: '/* function-scoped renderer comment */' },
  ])
})

test('scan code passed to the desktop acceptance scenario executor', () => {
  assert.deepEqual(scan('tests/support/agent_acceptance/desktop/scenario.mjs', [
    'export function runScenario({ execute: renderer }) {',
    '  return renderer("0; /* scenario comment */")',
    '}',
  ]), [{ line: 2, text: '/* scenario comment */' }])
})

test('scan noscript markup and report physical lines within decoded attributes', () => {
  assert.deepEqual(scan('desktop/src/renderer/sample.html', [
    '<noscript><!-- fallback markup --></noscript>',
    '<button onclick="0;&#10; /* attribute comment */',
    ' return false; // second attribute comment">x</button>',
    '<div style="color: red;&NewLine; /* CSS comment */"></div>',
    '<button onclick="&#10;--> classic comment&#10;return false">x</button>',
  ]), [
    { line: 1, text: '<!-- fallback markup -->' },
    { line: 2, text: '/* attribute comment */' },
    { line: 3, text: '// second attribute comment' },
    { line: 4, text: '/* CSS comment */' },
    { line: 5, text: '--> classic comment' },
  ])
})

test('count Unicode script separators only in JavaScript source positions', () => {
  assert.deepEqual(scan('desktop/sample.js', ['0;\u2028/* JS comment */']), [
    { line: 2, text: '/* JS comment */' },
  ])
  assert.deepEqual(scan('desktop/sample.html', ['<p>text\u2028</p><!-- HTML comment -->']), [
    { line: 1, text: '<!-- HTML comment -->' },
  ])
  assert.deepEqual(scan('desktop/sample.css', [':root{content:"\u2029"}/* CSS comment */']), [
    { line: 1, text: '/* CSS comment */' },
  ])
  assert.deepEqual(scan('desktop/sample.html', ['<button onclick="0;\u2028/* handler comment */">x</button>']), [
    { line: 1, text: '/* handler comment */' },
  ])
})

test('reject input other than an object of source strings', () => {
  for (const invalid of [[], { 'desktop/sample.js': 42 }]) {
    const result = runScanner(invalid)
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /Expected an object mapping file paths to source strings/)
  }
})
