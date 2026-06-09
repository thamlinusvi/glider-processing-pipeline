"""Minimal HTTP server to preview the Electron renderer in a browser.
Serves renderer/ files and injects window.APP before app.js loads."""
import http.server, os, sys, time

PORT = 3000
RENDERER = os.path.join(os.path.dirname(__file__), 'renderer')
BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLASK_PORT = 5757

INJECTION = f"""<script>
window.APP = {{
  port:    {FLASK_PORT},
  baseDir: {repr(BASE_DIR)},
  shellOpen: function(p) {{ console.log('open:', p); }}
}};
</script>"""

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=RENDERER, **kw)

    def do_GET(self):
        # Strip query string for path matching (allows cache-busting ?v=... URLs)
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            html = open(os.path.join(RENDERER, 'index.html'), encoding='utf-8').read()
            # Bust the JS/CSS cache on every server restart so edits show immediately
            _v = int(time.time())
            html = html.replace('<script src="app.js"></script>',
                                INJECTION + f'\n  <script src="app.js?v={_v}"></script>')
            html = html.replace('<link rel="stylesheet" href="styles.css">',
                                f'<link rel="stylesheet" href="styles.css?v={_v}">')
            body = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        else:
            # Add no-cache headers to all other assets (JS, CSS) to prevent stale previews
            self.path = path   # drop query string before serving file
            self.send_response(200)
            ext = path.rsplit('.', 1)[-1].lower()
            mime = {'js': 'application/javascript', 'css': 'text/css'}.get(ext, 'application/octet-stream')
            try:
                data = open(os.path.join(RENDERER, path.lstrip('/')), 'rb').read()
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', len(data))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_error(404)

    def log_message(self, *a): pass   # suppress request noise

print(f'Preview: http://127.0.0.1:{PORT}  (Flask backend on :{FLASK_PORT})', flush=True)
http.server.HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
