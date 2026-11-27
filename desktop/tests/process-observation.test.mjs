import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { appendFileSync, existsSync, readdirSync, readFileSync } from 'node:fs'
import { access, mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { setTimeout as pause } from 'node:timers/promises'
import { createTestRun, testRun } from './run-context.mjs'

const source = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const run = process.env.MYRK_TEST_RUN_DIRECTORY ? testRun() : await createTestRun(source)
const parent = join(run.fixtures, 'native-client')
const controlledPythonProgram = "import os,sys,time\nfrom pathlib import Path\nfrom tests.support.process_receipt import write_process_receipt\nwrite_process_receipt(Path(os.environ['MYRK_PROCESS_RECEIPT_DIRECTORY']), __file__)\nwhile not Path(os.environ['MYRK_TEST_NORMAL_EXIT_FLAG']).exists(): time.sleep(0.01)\nsys.exit(9)\n"
console.info(JSON.stringify({ testRunDirectory: run.directory }))

test('a real directly spawned program stays owned through normal flag exit', async () => {
  const { ProcessObservation } = await import('./process-observation.mjs')
  await mkdir(parent, { recursive: true })
  const directory = await mkdtemp(join(parent, 'direct-'))
  const observer = await ProcessObservation.start(directory)
  const flag = join(directory, 'finish.flag')
  const argv = [process.execPath, '-e', "const fs=require('node:fs');const timer=setInterval(()=>{if(fs.existsSync(process.argv[1])){clearInterval(timer);process.exitCode=7}},10)", flag]
  const child = spawn(argv[0], argv.slice(1), { cwd: source, windowsHide: true, stdio: 'ignore' })
  const closed = new Promise(resolve => child.once('close', resolve))
  try {
    const observation = await observer.capture(child, argv, source)
    assert.equal(observation.state, 'running')
    assert.equal(observation.ownership, 'owned')
    assert.match(observation.identity.created_filetime, /^\d+$/)
    await writeFile(flag, '')
    const report = await observer.wait(5000)
    assert.equal(report.complete, true)
    assert.equal(report.processes[0].exit_code, 7)
    assert.equal(await closed, 7)
  } finally {
    await writeFile(flag, '')
    await closed
    await observer.close()
  }
})

test('a controlled Python startup receipt separates its launcher from its real interpreter', async () => {
  const { ProcessObservation } = await import('./process-observation.mjs')
  await access(join(source, 'tests', 'support', 'process_receipt.py'))
  await mkdir(parent, { recursive: true })
  const directory = await mkdtemp(join(parent, 'python-'))
  const receipts = join(directory, 'receipts')
  await mkdir(receipts)
  const flag = join(directory, 'finish.flag')
  const owner = join(directory, 'owner.json')
  const fixture = join(directory, 'fixture.py')
  await writeFile(fixture, controlledPythonProgram)
  const observer = await ProcessObservation.start(directory)
  const argv = [process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', '-B', fixture]
  const child = spawn(argv[0], argv.slice(1), { cwd: source, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: {
    ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: source,
    MYRK_PROCESS_RECEIPT_DIRECTORY: receipts, MYRK_PROCESS_OWNER_FILE: owner, MYRK_TEST_NORMAL_EXIT_FLAG: flag,
  } })
  child.stdout.on('data', chunk => appendFileSync(join(directory, 'fixture-stdout.txt'), chunk))
  child.stderr.on('data', chunk => appendFileSync(join(directory, 'fixture-stderr.txt'), chunk))
  const closed = new Promise(resolve => child.once('close', resolve))
  try {
    const launcher = await observer.capture(child, argv, source)
    await writeFile(owner, JSON.stringify(launcher))
    const deadline = Date.now() + 10000
    while (observer.report.processes.length < 2) {
      assert.ok(Date.now() < deadline, 'Timed out waiting for the real interpreter receipt')
      assert.equal(child.exitCode, null, `Fixture exited; diagnostics: ${directory}`)
      await observer.captureReceipts(receipts)
      await pause(20)
    }
    assert.equal(observer.report.processes[0].pid, child.pid)
    assert.notEqual(observer.report.processes[1].pid, child.pid)
    assert.deepEqual(observer.report.processes.map(item => item.ownership), ['owned', 'owned'])
    await writeFile(flag, '')
    assert.equal((await observer.wait(5000)).complete, true)
    assert.equal(await closed, 9)
  } finally {
    await writeFile(flag, '')
    await closed
    await observer.close()
  }
})

test('a controlled child receipt proves the actual parent of the Python startup shell', async () => {
  const { ProcessObservation } = await import('./process-observation.mjs')
  await mkdir(parent, { recursive: true })
  const directory = await mkdtemp(join(parent, 'tree-'))
  const receipts = join(directory, 'receipts')
  await mkdir(receipts)
  const flag = join(directory, 'finish.flag')
  const owner = join(directory, 'owner.json')
  const fixture = join(directory, 'fixture.py')
  await writeFile(fixture, controlledPythonProgram)
  const observer = await ProcessObservation.start(directory)
  const python = process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe'
  const program = "const {spawn}=require('node:child_process');const fs=require('node:fs');const argv=[process.env.MYRK_TEST_PYTHON,'-B',process.env.MYRK_TEST_FIXTURE];const child=spawn(argv[0],argv.slice(1),{windowsHide:true,stdio:'inherit'});fs.writeFileSync(process.env.MYRK_TEST_SPAWN_RECORD,JSON.stringify({pid:child.pid,launcher_pid:process.pid,argv,cwd:process.cwd()}));child.once('close',code=>process.exitCode=code)"
  const argv = [process.execPath, '-e', program]
  const child = spawn(argv[0], argv.slice(1), { cwd: source, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: {
    ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: source,
    MYRK_TEST_PYTHON: python, MYRK_TEST_FIXTURE: fixture, MYRK_TEST_SPAWN_RECORD: join(directory, 'actual-spawn.json'),
    MYRK_PROCESS_RECEIPT_DIRECTORY: receipts, MYRK_PROCESS_OWNER_FILE: owner, MYRK_TEST_NORMAL_EXIT_FLAG: flag,
  } })
  child.stdout.on('data', chunk => appendFileSync(join(directory, 'fixture-stdout.txt'), chunk))
  child.stderr.on('data', chunk => appendFileSync(join(directory, 'fixture-stderr.txt'), chunk))
  const closed = new Promise(resolve => child.once('close', resolve))
  try {
    await writeFile(owner, JSON.stringify(await observer.capture(child, argv, source)))
    const deadline = Date.now() + 10000
    while (observer.report.processes.length < 3) {
      assert.ok(Date.now() < deadline, 'Timed out waiting for the startup shell and interpreter')
      assert.equal(child.exitCode, null, `Fixture exited; diagnostics: ${directory}`)
      await observer.captureReceipts(receipts)
      await pause(20)
    }
    assert.deepEqual(observer.report.processes.map(item => item.ownership), ['owned', 'owned', 'owned'])
    await writeFile(flag, '')
    assert.equal((await observer.wait(5000)).complete, true)
    assert.equal(await closed, 9)
  } finally {
    await writeFile(flag, '')
    await closed
    await observer.close()
  }
})

test('observer startup failure returns only after its original close receipt is saved', async () => {
  const { ProcessObservation } = await import('./process-observation.mjs')
  await mkdir(parent, { recursive: true })
  const directory = await mkdtemp(join(parent, 'missing-'))
  const original = process.env.MYRK_PYTHON
  process.env.MYRK_PYTHON = join(directory, 'missing-python.exe')
  try {
    await assert.rejects(ProcessObservation.start(directory), /ENOENT/)
    const launch = readdirSync(directory).find(name => name.endsWith('-launch.jsonl'))
    const lines = readFileSync(join(directory, launch), 'utf8').trim().split('\n').map(JSON.parse)
    assert.equal(typeof lines.at(-1).code, 'number')
  } finally {
    if (original === undefined) delete process.env.MYRK_PYTHON
    else process.env.MYRK_PYTHON = original
  }
})

test('a matching owner PID with a wrong raw creation time cannot authorize a fixture', async () => {
  const { ProcessObservation } = await import('./process-observation.mjs')
  await mkdir(parent, { recursive: true })
  const directory = await mkdtemp(join(parent, 'wrong-owner-'))
  const receipts = join(directory, 'receipts')
  await mkdir(receipts)
  const flag = join(directory, 'finish.flag')
  const owner = join(directory, 'owner.json')
  const ownerAfter = join(directory, 'owner-after.txt')
  const fixture = join(directory, 'fixture.py')
  await writeFile(fixture, "import os,sys,time\nfrom pathlib import Path\nfrom tests.support.process_receipt import write_process_receipt\nwrite_process_receipt(Path(os.environ['MYRK_PROCESS_RECEIPT_DIRECTORY']), __file__)\nPath(os.environ['MYRK_TEST_OWNER_AFTER']).write_text(os.environ['MYRK_PROCESS_OWNER_FILE'])\nwhile not Path(os.environ['MYRK_TEST_NORMAL_EXIT_FLAG']).exists(): time.sleep(0.01)\nsys.exit(9)\n")
  const observer = await ProcessObservation.start(directory)
  const argv = [process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe', '-B', fixture]
  const child = spawn(argv[0], argv.slice(1), { cwd: source, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: {
    ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: source,
    MYRK_PROCESS_RECEIPT_DIRECTORY: receipts, MYRK_PROCESS_OWNER_FILE: owner, MYRK_TEST_NORMAL_EXIT_FLAG: flag,
    MYRK_TEST_OWNER_AFTER: ownerAfter,
  } })
  child.stdout.on('data', chunk => appendFileSync(join(directory, 'fixture-stdout.txt'), chunk))
  child.stderr.on('data', chunk => appendFileSync(join(directory, 'fixture-stderr.txt'), chunk))
  const closed = new Promise(resolve => child.once('close', resolve))
  try {
    const original = await observer.capture(child, argv, source)
    const wrong = { ...original, identity: { ...original.identity, created_filetime: (BigInt(original.identity.created_filetime) + 1n).toString() } }
    await writeFile(owner, JSON.stringify(wrong))
    const deadline = Date.now() + 10000
    while (observer.report.processes.length < 2 || !existsSync(ownerAfter)) {
      assert.ok(Date.now() < deadline, 'Timed out waiting for the mismatched owner result')
      assert.equal(child.exitCode, null, `Fixture exited; diagnostics: ${directory}`)
      await observer.captureReceipts(receipts)
      await pause(20)
    }
    assert.equal(observer.report.processes[1].ownership, 'unknown')
    assert.equal(observer.report.complete, false)
    assert.equal(readFileSync(ownerAfter, 'utf8'), owner)
    await writeFile(flag, '')
    assert.equal((await observer.wait(5000)).complete, true)
    assert.equal(await closed, 9)
  } finally {
    await writeFile(flag, '')
    await closed
    await observer.close()
  }
})

test('a real service runner check and child inherit only their publicly observed owners', async () => {
  const { ProcessObservation } = await import('./process-observation.mjs')
  await mkdir(parent, { recursive: true })
  const directory = await mkdtemp(join(parent, 'chain-'))
  const receipts = join(directory, 'receipts')
  await mkdir(receipts)
  const flag = join(directory, 'finish.flag')
  const owner = join(directory, 'owner.json')
  for (const [role, next, ownReceipt, interpreter] of [
    ['service', 'runner', true, 'sys.executable'],
    ['runner', 'check', false, "os.environ['MYRK_TEST_PYTHON']"],
    ['check', 'child', true, "os.environ['MYRK_TEST_PYTHON']"],
  ]) {
    await writeFile(join(directory, `${role}.py`), `import json,os,subprocess,sys\nfrom pathlib import Path\n${ownReceipt ? "from tests.support.process_receipt import write_process_receipt\nwrite_process_receipt(Path(os.environ['MYRK_PROCESS_RECEIPT_DIRECTORY']), __file__)\n" : ''}root=Path(os.environ['MYRK_TEST_CHAIN_ROOT'])\n(root/'${role}-owner.json').write_text(json.dumps({'pid':os.getpid(),'owner_file':os.environ['MYRK_PROCESS_OWNER_FILE']}))\nif Path(os.environ['MYRK_TEST_NORMAL_EXIT_FLAG']).exists(): sys.exit(0)\nargv=[${interpreter},'-B',str(root/'${next}.py')]\nchild=subprocess.Popen(argv,creationflags=subprocess.CREATE_NO_WINDOW)\nwith (root/'actual-spawns.jsonl').open('a') as stream: stream.write(json.dumps({'role':'${role}','pid':child.pid,'launcher_pid':os.getpid(),'argv':argv,'cwd':os.getcwd()})+'\\n')\nsys.exit(child.wait())\n`)
  }
  await writeFile(join(directory, 'child.py'), "import json,os,sys,time\nfrom pathlib import Path\nfrom tests.support.process_receipt import write_process_receipt\nwrite_process_receipt(Path(os.environ['MYRK_PROCESS_RECEIPT_DIRECTORY']), __file__)\nroot=Path(os.environ['MYRK_TEST_CHAIN_ROOT'])\n(root/'child-owner.json').write_text(json.dumps({'pid':os.getpid(),'owner_file':os.environ['MYRK_PROCESS_OWNER_FILE']}))\nwhile not Path(os.environ['MYRK_TEST_NORMAL_EXIT_FLAG']).exists(): time.sleep(0.01)\nsys.exit(9)\n")
  const observer = await ProcessObservation.start(directory)
  const python = process.env.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe'
  const program = "const{spawn}=require('node:child_process');const fs=require('node:fs');const argv=[process.env.MYRK_TEST_PYTHON,'-B',process.env.MYRK_TEST_FIXTURE];const child=spawn(argv[0],argv.slice(1),{windowsHide:true,stdio:'inherit'});fs.writeFileSync(process.env.MYRK_TEST_SPAWN_RECORD,JSON.stringify({pid:child.pid,launcher_pid:process.pid,argv,cwd:process.cwd()}));child.once('close',code=>process.exitCode=code)"
  const argv = [process.execPath, '-e', program]
  const child = spawn(argv[0], argv.slice(1), { cwd: source, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: {
    ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONPATH: source,
    MYRK_TEST_PYTHON: python, MYRK_TEST_FIXTURE: join(directory, 'service.py'), MYRK_TEST_CHAIN_ROOT: directory,
    MYRK_TEST_SPAWN_RECORD: join(directory, 'actual-root-spawn.json'),
    MYRK_PROCESS_RECEIPT_DIRECTORY: receipts, MYRK_PROCESS_OWNER_FILE: owner, MYRK_TEST_NORMAL_EXIT_FLAG: flag,
  } })
  child.stdout.on('data', chunk => appendFileSync(join(directory, 'fixture-stdout.txt'), chunk))
  child.stderr.on('data', chunk => appendFileSync(join(directory, 'fixture-stderr.txt'), chunk))
  const closed = new Promise(resolve => child.once('close', resolve))
  try {
    await writeFile(owner, JSON.stringify(await observer.capture(child, argv, source)))
    const deadline = Date.now() + 10000
    while (observer.report.processes.length < 9 || !existsSync(join(directory, 'child-owner.json'))) {
      assert.ok(Date.now() < deadline, 'Timed out waiting for the service runner check child chain')
      assert.equal(child.exitCode, null, `Fixture exited; diagnostics: ${directory}`)
      await observer.captureReceipts(receipts)
      await pause(20)
    }
    assert.deepEqual(observer.report.processes.map(item => item.ownership), Array(9).fill('owned'))
    for (const role of ['service', 'check', 'child']) {
      const current = JSON.parse(readFileSync(join(directory, `${role}-owner.json`), 'utf8'))
      const acknowledged = JSON.parse(readFileSync(current.owner_file, 'utf8'))
      assert.equal(acknowledged.pid, current.pid)
      assert.equal(acknowledged.ownership, 'owned')
    }
    await writeFile(flag, '')
    assert.equal((await observer.wait(5000)).complete, true)
    assert.equal(await closed, 9)
  } finally {
    await writeFile(flag, '')
    await closed
    await observer.close()
  }
})
