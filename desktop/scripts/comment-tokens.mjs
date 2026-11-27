import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { extname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

const desktopRequire = createRequire(join(process.argv[2], 'package.json'))
const [{ tokenize, tokenTypes }, { parse, parseFragment }, { parseSync, Visitor }] = await Promise.all(
  ['css-tree', 'parse5', 'vite'].map((name) => import(pathToFileURL(desktopRequire.resolve(name)))),
)
const javascriptMime = /^(?:application\/(?:x-)?(?:java|ecma)script|text\/(?:(?:x-)?(?:java|ecma)script|javascript1\.[0-5]|jscript|livescript))$/
const physicalLineBreaks = /(\r\n|[\r\n])/
const javascriptLineBreaks = /(\r\n|[\r\n\u2028\u2029])/
const javascriptEscapes = /\\(?:\r\n|[\r\n\u2028\u2029]|x[\da-fA-F]{2}|u(?:\{[\da-fA-F]+\}|[\da-fA-F]{4})|[0-3][0-7]{0,2}|[4-7][0-7]?|.)|\r\n/gs
let document

function sourceLines(raw, firstLine, lineBreaks, decodedLength) {
  const parts = raw.split(lineBreaks)
  const ends = []
  let decodedEnd = 0
  for (let i = 0; i + 1 < parts.length; i += 2) {
    decodedEnd += decodedLength(parts[i] + parts[i + 1])
    ends.push(decodedEnd)
  }
  return offset => {
    const line = ends.findIndex(end => offset < end)
    return firstLine + (line === -1 ? ends.length : line)
  }
}

function decodedScriptLength(raw) {
  return raw.replace(javascriptEscapes, escape => {
    if (escape === '\r\n') return '\n'
    if (javascriptLineBreaks.test(escape[1])) return ''
    return escape.startsWith('\\u{') && parseInt(escape.slice(3, -1), 16) > 0xffff ? 'xx' : 'x'
  }).length
}

function propertyName(node) {
  const key = node.property ?? node.key
  return node.computed || key?.type === 'Literal' ? key?.value : key?.name
}

function rendererMethod(callee) {
  if (callee?.type === 'MemberExpression' && callee.object.type === 'MemberExpression' &&
      propertyName(callee.object) === 'webContents') return propertyName(callee)
}

function rendererScripts(program, filename) {
  const scenario = /^tests\/support\/agent_acceptance\/desktop\/[^/]+\.mjs$/.test(filename)
  const calls = []
  const assignments = []
  const moduleScope = { parent: null, bindings: new Map(), functionScope: null }
  let scope = moduleScope
  const enter = () => { scope = { parent: scope, bindings: new Map(), functionScope: scope.functionScope } }
  const leave = () => { scope = scope.parent }
  const bind = (bindings, node, renderer = false, scenarioOptions = false) => {
    switch (node?.type) {
      case 'Identifier': bindings.set(node.name, renderer); break
      case 'AssignmentPattern': bind(bindings, node.left, renderer, scenarioOptions); break
      case 'RestElement': bind(bindings, node.argument); break
      case 'ArrayPattern': for (const element of node.elements) bind(bindings, element); break
      case 'ObjectPattern':
        for (const property of node.properties) {
          if (property.type === 'Property') bind(bindings, property.value, scenarioOptions && propertyName(property) === 'execute')
          else bind(bindings, property)
        }
        break
    }
  }
  const enterFunction = node => {
    if (node.type === 'FunctionDeclaration') bind(scope.bindings, node.id, node)
    enter()
    scope.functionScope = { node, bindings: scope.bindings }
    if (node.type === 'FunctionExpression') bind(scope.bindings, node.id, node)
    for (const [i, param] of node.params.entries()) {
      bind(scope.bindings, param, false, scenario && i === 0 && /^run[A-Z]/.test(node.id?.name ?? ''))
    }
  }
  new Visitor({
    BlockStatement: enter, 'BlockStatement:exit': leave,
    ForStatement: enter, 'ForStatement:exit': leave,
    ForInStatement: enter, 'ForInStatement:exit': leave,
    ForOfStatement: enter, 'ForOfStatement:exit': leave,
    CatchClause(node) { enter(); bind(scope.bindings, node.param) }, 'CatchClause:exit': leave,
    ArrowFunctionExpression: enterFunction, 'ArrowFunctionExpression:exit': leave,
    FunctionDeclaration: enterFunction, 'FunctionDeclaration:exit': leave,
    FunctionExpression: enterFunction, 'FunctionExpression:exit': leave,
    VariableDeclaration(node) {
      const bindings = node.kind === 'var' ? (scope.functionScope?.bindings ?? moduleScope.bindings) : scope.bindings
      for (const declaration of node.declarations) bind(bindings, declaration.id, declaration.init ?? null)
    },
    AssignmentExpression(node) {
      if (node.operator === '=' && node.left.type === 'Identifier') assignments.push({ node, scope })
    },
    CallExpression(node) { calls.push({ node, scope }) },
  }).visit(program)
  const binding = (scope, name) => {
    for (let local = scope; local; local = local.parent) {
      if (local.bindings.has(name)) return local.bindings
    }
  }
  const wrappers = new Set()
  for (const { node, scope } of calls) {
    const declaration = scope.functionScope
    const parameter = declaration?.node.params[0]
    const argument = node.arguments[0]
    if (rendererMethod(node.callee) === 'executeJavaScript' && parameter?.type === 'Identifier' &&
        argument?.type === 'Identifier' && parameter.name === argument.name &&
        binding(scope, parameter.name) === declaration.bindings) wrappers.add(declaration.node)
  }
  const isRenderer = value => value === true || wrappers.has(value)
  const scripts = []
  for (const { node, scope } of [...calls, ...assignments].sort((a, b) => a.node.start - b.node.start)) {
    if (node.type === 'AssignmentExpression') {
      binding(scope, node.left.name)?.set(node.left.name, isRenderer(node.right))
      continue
    }
    const method = rendererMethod(node.callee)
    if (method === 'executeJavaScriptInIsolatedWorld' && node.arguments[1]?.type === 'ArrayExpression') {
      scripts.push(...node.arguments[1].elements.flatMap(script => script?.type === 'ObjectExpression'
        ? script.properties.filter(property => property.type === 'Property' && propertyName(property) === 'code')
          .map(property => property.value)
        : []))
    }
    if (method === 'executeJavaScript' || (node.callee.type === 'Identifier' &&
        isRenderer(binding(scope, node.callee.name)?.get(node.callee.name)))) scripts.push(...node.arguments.slice(0, 1))
  }
  return scripts
}

function webComments(filename, source) {
  const comments = []
  const extension = extname(filename).toLowerCase()
  const lineBreaks = new RegExp(extension === '.html' || extension === '.css'
    ? physicalLineBreaks : javascriptLineBreaks, 'g')
  const sourceLine = offset => (source.slice(0, offset).match(lineBreaks)?.length ?? 0) + 1
  const record = (text, { start, end }, lineAt) => {
    comments.push({ line: lineAt(start), text: text.slice(start, end) })
  }
  const scanCss = (text, lineAt) => tokenize(text, (type, start, end) => {
    if (type === tokenTypes.Comment) record(text, { start, end }, lineAt)
  })
  const scanScript = (name, text, lineAt) => {
    const result = parseSync(name, text)
    if (result.errors.length) throw new Error(`${filename}: ${result.errors[0].message}`)
    for (const comment of result.comments) record(text, comment, lineAt)
    return result.program
  }
  const scanRendererScript = argument => {
    const quasis = argument.type === 'TemplateLiteral' ? argument.quasis :
      argument.type === 'Literal' && typeof argument.value === 'string'
        ? [{ start: argument.start + 1, end: argument.end - 1, value: { cooked: argument.value } }]
        : []
    if (!quasis.length || quasis.some(part => part.value.cooked == null)) return
    const sections = []
    let text = ''
    for (const part of quasis) {
      if (sections.length) text += '_interpolated'
      const lineAt = sourceLines(source.slice(part.start, part.end), sourceLine(part.start),
        javascriptLineBreaks, decodedScriptLength)
      sections.push({ start: text.length, lineAt })
      text += part.value.cooked
    }
    for (const comment of parseSync('renderer.js', text).comments) {
      const section = sections.findLast(part => part.start <= comment.start)
      record(text, comment, start => section.lineAt(start - section.start))
    }
  }
  const scanHtml = (node) => {
    const location = node.sourceCodeLocation
    if (node.nodeName === '#comment' && location) {
      record(source, { start: location.startOffset, end: location.endOffset }, sourceLine)
    }
    if (location?.startTag) {
      const start = location.startTag.endOffset
      const body = source.slice(start, location.endTag?.startOffset ?? location.endOffset)
      const lineAt = offset => sourceLine(start + offset)
      if (node.tagName === 'style') scanCss(body, lineAt)
      if (node.tagName === 'script' && !node.attrs.some(({ name }) => name === 'src')) {
        const type = node.attrs.find(({ name }) => name === 'type')?.value.toLowerCase().trim()
        if (!type || type === 'module' || javascriptMime.test(type.split(';', 1)[0].trim())) {
          scanScript('inline.js', body, lineAt)
        }
      }
    }
    for (const { name, value } of node.attrs ?? []) {
      const style = name === 'style' && value.includes('/*')
      const handler = name.startsWith('on') && ['//', '/*', '<!--', '-->'].some(marker => value.includes(marker))
      if (!style && !handler) continue
      const attribute = location?.attrs?.[name]
      if (!attribute) continue
      const raw = source.slice(attribute.startOffset, attribute.endOffset)
      const header = /^[^=\s]+\s*=\s*(["'])?/.exec(raw)
      if (!header) continue
      const quote = header[1] ?? ''
      const lineAt = sourceLines(raw.slice(header[0].length, raw.length - quote.length),
        sourceLine(attribute.startOffset + header[0].length), physicalLineBreaks,
        part => parseFragment(`<x a=${quote}${part}${quote}>`).childNodes[0].attrs[0].value.length)
      if (style) scanCss(value, lineAt)
      if (handler) {
        if (!document) {
          const { JSDOM } = desktopRequire('jsdom')
          document = new JSDOM().window.document
        }
        if (name in document.createElementNS(node.namespaceURI, node.tagName)) {
          const prefix = 'function handler(event) {'
          scanScript('handler.js', `${prefix}${value}\n}`, offset => lineAt(offset - prefix.length))
        }
      }
    }
    for (const child of node.childNodes ?? []) scanHtml(child)
    if (node.content) scanHtml(node.content)
  }
  switch (extension) {
    case '.css': scanCss(source, sourceLine); break
    case '.html': scanHtml(parse(source, { sourceCodeLocationInfo: true, scriptingEnabled: false })); break
    default:
      for (const argument of rendererScripts(scanScript(filename, source, sourceLine), filename)) {
        scanRendererScript(argument)
      }
  }
  return comments.sort((a, b) => a.line - b.line)
}

const sources = JSON.parse(readFileSync(0, 'utf8'))
if (sources === null || typeof sources !== 'object' || Array.isArray(sources) ||
    Object.values(sources).some(source => typeof source !== 'string')) {
  throw new TypeError('Expected an object mapping file paths to source strings')
}
process.stdout.write(JSON.stringify(Object.fromEntries(
  Object.entries(sources).map(([filename, source]) => [filename, webComments(filename, source)]),
)))
