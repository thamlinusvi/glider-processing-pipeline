"""Minimal HTTP server to preview the Electron renderer in a browser.
Serves renderer/ files and injects window.APP before app.js loads."""
import http.server, os, sys

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
        if self.path in ('/', '/index.html'):
            html = open(os.path.join(RENDERER, 'index.html'), encoding='utf-8').read()
            html = html.replace('<script src="app.js"></script>',
                                INJECTION + '\n  <script src="app.js"></script>')
            body = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *a): pass   # suppress request noise

print(f'Preview: http://127.0.0.1:{PORT}  (Flask backend on :{FLASK_PORT})', flush=True)
http.server.HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
