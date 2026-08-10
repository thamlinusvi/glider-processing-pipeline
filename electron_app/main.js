'use strict'

const { app, BrowserWindow, shell, ipcMain } = require('electron')
const { spawn }  = require('child_process')
const path       = require('path')
const http       = require('http')

// ── Config ────────────────────────────────────────────────────────────────────
const PORT     = 5757
const BASE_DIR = path.resolve(__dirname, '..')   // electron_app/../ = pipeline root

let pythonProcess = null
let mainWindow    = null

// ── Python server ─────────────────────────────────────────────────────────────
function startPython () {
  const script = path.join(__dirname, 'python', 'server.py')
  pythonProcess = spawn('python', [script, BASE_DIR, String(PORT)], {
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
  if (tries <= 0) { console.error('Python server never started'); return }
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
