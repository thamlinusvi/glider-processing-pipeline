'use strict'

const { app, BrowserWindow, shell, ipcMain, dialog } = require('electron')
const { spawn, spawnSync } = require('child_process')
const path       = require('path')
const http       = require('http')
const fs         = require('fs')

// ── Config ────────────────────────────────────────────────────────────────────
const PORT = 5757

// If an installer wrote glider_config.json, use its dataDir as BASE_DIR so the
// app code and data directories can live in different places.  Falls back to the
// repo-root sibling of electron_app/ for direct git-clone installs.
function resolveBaseDir () {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, 'glider_config.json'), 'utf8'))
    if (cfg && cfg.dataDir) return cfg.dataDir
  } catch (_) {}
  return path.resolve(__dirname, '..')
}
const BASE_DIR = resolveBaseDir()

let pythonProcess = null
let mainWindow    = null

// ── Python server ─────────────────────────────────────────────────────────────
// Resolve which command actually launches Python. Prefers the official
// Windows "py" launcher when present: it's only ever installed by a real
// python.org/Microsoft Store *interpreter* install, never by the separate
// Microsoft Store "app execution alias" stub that can also masquerade as
// python.exe on PATH and silently no-ops (or redirects to the Store) instead
// of running anything -- so a bare 'python' lookup isn't reliable on its own.
// `where`/`which` only resolves PATH, it doesn't execute the candidate, so
// this can't itself trigger that stub's Store-redirect behavior.
function resolvePythonCommand () {
  const candidates = process.platform === 'win32'
    ? [['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]]
  const whereCmd = process.platform === 'win32' ? 'where' : 'which'
  for (const [cmd, extraArgs] of candidates) {
    const found = spawnSync(whereCmd, [cmd], { stdio: 'ignore' })
    if (found.status === 0) return { cmd, extraArgs }
  }
  return null
}

function reportStartupFailure (message) {
  console.error(message)
  dialog.showErrorBox('Glider Pipeline failed to start', message)
  app.quit()
}

function startPython () {
  const script  = path.join(__dirname, 'python', 'server.py')
  const resolved = resolvePythonCommand()
  if (!resolved) {
    reportStartupFailure(
      'No Python interpreter found on PATH (tried py, python, python3).\n' +
      'Install Python and make sure it is on PATH, then restart the app.'
    )
    return
  }
  pythonProcess = spawn(resolved.cmd, [...resolved.extraArgs, script, BASE_DIR, String(PORT)], {
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  pythonProcess.stdout.on('data', d => process.stdout.write('[py] ' + d))
  pythonProcess.stderr.on('data', d => process.stderr.write('[py] ' + d))
  pythonProcess.on('exit', code => console.log(`[py] exited ${code}`))
}

function waitForServer (cb, tries = 40) {
  http.get(`http://127.0.0.1:${PORT}/api/ping`, res => {
    if (res.statusCode === 200) return cb()
    retry(cb, tries)
  }).on('error', () => retry(cb, tries))
}

function retry (cb, tries) {
  if (tries <= 0) {
    reportStartupFailure(
      `The Python backend did not respond at http://127.0.0.1:${PORT}/api/ping in time.\n` +
      'Check the terminal/log output above for [py] errors.'
    )
    return
  }
  setTimeout(() => waitForServer(cb, tries - 1), 300)
}

// ── Browser window ────────────────────────────────────────────────────────────
function createWindow () {
  mainWindow = new BrowserWindow({
    width:     1300,
    height:    860,
    minWidth:  960,
    minHeight: 640,
    title:     'UVI Glider Pipeline',
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration:  false,
      webSecurity:      false,   // allow file:// iframes and localhost fetches
    },
  })

  // Expose config to renderer via env vars read in preload
  process.env.GLIDER_PORT     = String(PORT)
  process.env.GLIDER_BASE_DIR = BASE_DIR

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'))

  // Open external links in the system browser, not in-app
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })
}

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  startPython()
  waitForServer(createWindow)
})

// Ask the Python server to kill any running stage's Docker container before we
// kill the server itself -- killing 'docker run' alone leaves the container
// running (see server.py's shutdown_cleanup for why). Best-effort: never blocks
// quitting for more than a couple seconds if the server is slow or unresponsive.
function cleanupBeforeQuit () {
  return new Promise(resolve => {
    const req = http.request(
      { hostname: '127.0.0.1', port: PORT, path: '/api/shutdown-cleanup', method: 'POST', timeout: 3000 },
      res => { res.resume(); res.on('end', resolve) }
    )
    req.on('error', resolve)
    req.on('timeout', () => { req.destroy(); resolve() })
    req.end()
  })
}

app.on('window-all-closed', async () => {
  if (pythonProcess) {
    await cleanupBeforeQuit()
    pythonProcess.kill()
    pythonProcess = null
  }
  app.quit()
})

// IPC: open a file/folder in the system shell
ipcMain.handle('shell-open', (_, filePath) => shell.openPath(filePath))

// IPC: open a URL in the default system browser (used for OAuth flows)
ipcMain.handle('open-url', (_, url) => shell.openExternal(url))
