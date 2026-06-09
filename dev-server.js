/**
 * Minimal dev server for previewing the Electron renderer in a browser.
 * Injects window.APP shim so app.js can find the API port and baseDir.
 */
const http = require('http')
const fs   = require('fs')
const path = require('path')

const PORT     = 3131
const BASE_DIR = path.join(__dirname, 'electron_app', 'renderer')
const API_PORT = 5757
const GLIDER_BASE = __dirname.replace(/\\/g, '\\\\')

const APP_SHIM = `<script>
window.APP = {
  port:    ${API_PORT},
  baseDir: '${GLIDER_BASE}',
  shellOpen: function(p) { console.log('shellOpen:', p) }
}
</script>`

const MIME = {
  '.html': 'text/html',
  '.css':  'text/css',
  '.js':   'application/javascript',
  '.png':  'image/png',
  '.svg':  'image/svg+xml',
  '.ico':  'image/x-icon',
}

http.createServer((req, res) => {
  let url = req.url.split('?')[0]
  if (url === '/') url = '/index.html'
  const fp = path.join(BASE_DIR, url)

  fs.readFile(fp, (err, data) => {
    if (err) { res.writeHead(404); res.end('Not found'); return }
    const ext  = path.extname(fp)
    const mime = MIME[ext] || 'application/octet-stream'
    res.writeHead(200, { 'Content-Type': mime })

    if (ext === '.html') {
      // Inject window.APP shim just before </head>
      const patched = data.toString().replace('</head>', APP_SHIM + '\n</head>')
      res.end(patched)
    } else {
      res.end(data)
    }
  })
}).listen(PORT, '127.0.0.1', () => {
  console.log(`Dev server running at http://127.0.0.1:${PORT}`)
})
