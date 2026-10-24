"""Сервер-образец для проверки модуля: /api/info с build, /api/restart, страница с web/buildinfo.js.
Запуск из корня временного «проекта» (в нём common/buildinfo - копия модуля, buildinfo.json, app/)."""
import http.server
import json
import sys

from common.buildinfo import buildinfo as bi

PORT = int(sys.argv[1])
BUILD = bi.at_start()
PAGE = b'<!doctype html><html lang="ru"><body><h1>sample</h1><script src="/common/buildinfo/buildinfo.js"></script>' \
       b'<script>BuildInfo.watch({every: 1000})</script></body></html>'


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype='application/json'):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/info':
            return self._send(json.dumps({'name': 'sample', 'version': '1.0', **bi.info()}).encode())
        if self.path == '/common/buildinfo/buildinfo.js':
            return self._send((bi.project_root() / 'common/buildinfo/web/buildinfo.js').read_bytes(), 'text/javascript')
        return self._send(PAGE, 'text/html; charset=utf-8')

    def do_POST(self):
        if self.path == '/api/restart':
            bi.restart_later()
            return self._send(b'{"ok": true}')

    def log_message(self, *a):
        pass


http.server.HTTPServer(('127.0.0.1', PORT), H).serve_forever()
