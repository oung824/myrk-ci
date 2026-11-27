import { spawnSync } from 'node:child_process'
import { lstat, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { projectRoot } from '../tests/project-paths.mjs'
import prototypeConfig from '../prototype.config.mjs'

const arguments_ = process.argv.slice(2)
if (arguments_.length > 1 || arguments_.some(argument => !['--check', '--build'].includes(argument))) {
  throw new Error('使用 node scripts/run-prototype.mjs [--check|--build]。')
}
const mode = arguments_[0] ?? '--serve'
const desktopDirectory = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const sourceRoot = dirname(desktopDirectory)
const projectDirectory = projectRoot(sourceRoot)
const dependenciesDirectory = join(projectDirectory, 'desktop', 'node_modules')
const runtimeParent = join(projectDirectory, '.scratch', 'runtime')
if (await realpath(dirname(runtimeParent)) !== dirname(runtimeParent)) {
  throw new Error('原型运行资料不能经过链接目录。')
}
await mkdir(runtimeParent, { recursive: true })
if (await realpath(runtimeParent) !== runtimeParent) {
  throw new Error('原型运行目录必须直接位于项目内。')
}
const resourceDirectory = await mkdtemp(join(runtimeParent, 'p-claude-prototype-'))
const ownerRecord = JSON.stringify({
  owner: '/root Claude Desktop UI prototype',
  task_id: 'claude-desktop-prototype-20261114',
  project_root: projectDirectory,
  source_root: sourceRoot,
  runtime_directory: resourceDirectory,
  purpose: mode === '--serve' ? 'Serve the in-memory UI prototype for user review.' : `Run prototype ${mode.slice(2)} only.`,
  pid: process.pid,
  process_started_at: new Date(Date.now() - process.uptime() * 1000).toISOString(),
  created_at: new Date().toISOString(),
}, null, 2) + '\n'
await writeFile(join(resourceDirectory, 'owner.json'), ownerRecord, { encoding: 'utf8', flag: 'wx' })

let server
let cleanupPromise
function cleanup() {
  cleanupPromise ??= (async () => {
    await server?.close()
    const entry = await lstat(resourceDirectory)
    if (dirname(resourceDirectory) !== runtimeParent || !basename(resourceDirectory).startsWith('p-claude-prototype-')
      || !entry.isDirectory() || entry.isSymbolicLink() || await realpath(resourceDirectory) !== resourceDirectory
      || await readFile(join(resourceDirectory, 'owner.json'), 'utf8') !== ownerRecord) {
      throw new Error(`原型运行资料的路径或归属不符，停止清理：${resourceDirectory}`)
    }
    await rm(resourceDirectory, { recursive: true })
    console.log(`已清理原型运行资料：${resourceDirectory}`)
  })()
  return cleanupPromise
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, async () => {
    await cleanup()
    process.exit(0)
  })
}

let serving = false
try {
  const temporaryDirectory = join(resourceDirectory, 'temporary')
  await mkdir(temporaryDirectory)
  Object.assign(process.env, {
    TEMP: temporaryDirectory,
    TMP: temporaryDirectory,
    TMPDIR: temporaryDirectory,
    LOCALAPPDATA: join(resourceDirectory, 'tool-profile', 'local'),
    APPDATA: join(resourceDirectory, 'tool-profile', 'roaming'),
    XDG_CACHE_HOME: join(resourceDirectory, 'cache', 'xdg'),
    XDG_CONFIG_HOME: join(resourceDirectory, 'tool-profile', 'config'),
    npm_config_cache: join(resourceDirectory, 'cache', 'npm'),
    NODE_COMPILE_CACHE: join(resourceDirectory, 'cache', 'node'),
  })
  if (mode === '--check') {
    const result = spawnSync(process.execPath, [
      join(dependenciesDirectory, 'typescript', 'bin', 'tsc'),
      '--project', join(desktopDirectory, 'prototype.tsconfig.json'), '--pretty', 'false',
    ], { cwd: desktopDirectory, env: process.env, stdio: 'inherit', windowsHide: true })
    if (result.error) throw result.error
    process.exitCode = result.status ?? 1
  } else {
    const vite = await import(pathToFileURL(join(dependenciesDirectory, 'vite', 'dist', 'node', 'index.js')).href)
    const { default: react } = await import(pathToFileURL(join(dependenciesDirectory, '@vitejs', 'plugin-react', 'dist', 'index.js')).href)
    const configuration = prototypeConfig({ desktopDirectory, dependenciesDirectory, resourceDirectory, react })
    if (mode === '--build') {
      await vite.build(configuration)
      console.log('原型构建完成；本次构建文件随运行资料清理。')
    } else {
      server = await vite.createServer(configuration)
      await server.listen()
      serving = true
      console.log('Myrk 界面原型：http://127.0.0.1:5186/')
      console.log(`运行资料：${resourceDirectory}`)
      console.log('按 Ctrl+C 停止并清理本次运行资料。')
    }
  }
} finally {
  if (!serving) await cleanup()
}
