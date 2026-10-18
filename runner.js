/**
 * Runtime bridge for dsh-valuation-service.
 *
 * Runs core/valuation_service.py with an interpreter that has the Python
 * standard library only — no third-party packages are required. The service
 * prints one JSON object when called with --json, which this runner forwards
 * to the tool layer.
 *
 * @module dsh-valuation-service/runner
 */
import { spawn } from 'node:child_process'
import { existsSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'

function dshHome() {
  const override = process.env.DSH_HOME
  return (override !== undefined && override.length > 0 ? override : join(homedir(), '.dsh'))
}

function dshPython() {
  const runtime = join(dshHome(), 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python')
  const candidates = [
    join(runtime, 'bin', 'python3'),
    join(runtime, 'bin', 'python'),
    join(runtime, 'python.exe'),
    join(runtime, 'bin', 'python.exe'),
  ]
  for (const c of candidates) {
    try {
      if (existsSync(c) && statSync(c).isFile()) return c
    } catch { /* ignore */ }
  }
  return undefined
}

function fileExists(p) {
  try { return existsSync(p) && statSync(p).isFile() } catch { return false }
}

/**
 * Ordered Python candidates. The service only needs the standard library, so
 * any python3 works; prefer an explicit override, then the DSH runtime, then
 * PATH, then the common Anaconda location.
 */
export function findValuationPythonCandidates() {
  const list = []
  const explicit = process.env.DSH_VALUATION_PYTHON
  if (explicit && explicit.length > 0) list.push({ python: explicit, source: 'env' })
  const dsh = dshPython()
  if (dsh) list.push({ python: dsh, source: 'dsh-builtin' })
  list.push({ python: 'python3', source: 'path' })
  const anaconda = '/opt/anaconda3/bin/python3'
  if (fileExists(anaconda)) list.push({ python: anaconda, source: 'anaconda' })
  const seen = new Set()
  return list.filter((c) => {
    if (seen.has(c.python)) return false
    seen.add(c.python)
    return true
  })
}

function exec(command, args, options = {}) {
  return new Promise((resolvePromise) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: { ...process.env, ...(options.env ?? {}) },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    let settled = false
    const timeoutMs = options.timeoutMs ?? 300_000
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      child.kill('SIGKILL')
      resolvePromise({ code: -1, stdout, stderr: `${stderr}\n[timeout after ${timeoutMs}ms]`, timedOut: true })
    }, timeoutMs)
    child.stdout.on('data', (chunk) => { stdout += String(chunk) })
    child.stderr.on('data', (chunk) => { stderr += String(chunk) })
    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolvePromise({ code: -1, stdout, stderr: `${stderr}\n${String(error?.message ?? error)}`, spawnError: error })
    })
    child.on('close', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolvePromise({ code: code ?? -1, stdout, stderr })
    })
  })
}

/**
 * Run the valuation service.
 *
 * @param {object} options
 * @param {string} [options.base] - workpaper root (absolute)
 * @param {string|string[]} [options.projects] - project directory names
 * @param {boolean} [options.write] - write the aggregate file
 * @param {string} [options.out] - output path override
 * @param {string} [options.cwd] - working directory
 * @param {string} options.engineRoot - plugin root (contains core/valuation_service.py)
 */
export async function runValuation(options) {
  const engine = join(options.engineRoot, 'core', 'valuation_service.py')
  if (!fileExists(engine)) return { ok: false, error: `valuation engine missing: ${engine}` }

  const cwd = options.cwd && options.cwd.length > 0 ? options.cwd : process.cwd()
  const args = [engine]
  if (options.base) args.push('--base', resolve(options.base))
  if (options.projects) {
    const projects = Array.isArray(options.projects) ? options.projects.join(',') : String(options.projects)
    if (projects.length > 0) args.push('--projects', projects)
  }
  if (options.write) args.push('--write')
  if (options.out) args.push('--out', resolve(cwd, options.out))
  args.push('--json')

  const tried = []
  for (const cand of findValuationPythonCandidates()) {
    const result = await exec(cand.python, args, { cwd })
    if (result.code === -1 && result.spawnError) {
      tried.push({ source: cand.source, python: cand.python, error: 'spawn failed' })
      continue
    }
    const text = (result.stdout || '').trim()
    if (text.length > 0) {
      try {
        const payload = JSON.parse(text)
        if (payload && typeof payload === 'object') {
          payload.interpreter = cand.source
          if (tried.length > 0) payload.fallback_used = tried
          return payload
        }
      } catch { /* fall through to error */ }
    }
    const detail = (result.stderr || result.stdout || '').trim().split('\n').slice(-8).join('\n')
    tried.push({ source: cand.source, python: cand.python, error: `exit ${result.code}: ${detail.slice(0, 300)}` })
  }

  return {
    ok: false,
    error: `no Python interpreter could run the valuation service (tried: ${tried.map((t) => t.source).join(', ') || 'none'})`,
    tried,
  }
}
