#!/usr/bin/env python3
"""Serve dashboard assets and current check results."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('SUDOCTOR_STATE_DIR', '/state'))
LOCAL = '--local' in sys.argv[1:]
REPORTS = Path(os.environ.get('SUDOCTOR_REPORT_DIR') or Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'sudoctor/reports').expanduser().resolve()
STALE = int(os.environ.get('SUDOCTOR_STALE_SECONDS', '3600'))


def read_state():
    try:
        return json.loads((STATE / 'state.json').read_text())
    except FileNotFoundError:
        return {'hosts': {}, 'error': None, 'last_sweep': None}


def local_overview():
    reports, errors = [], []
    for path in sorted(REPORTS.glob('*.json')) + sorted(REPORTS.glob('*/*.json')):
        # Never read files linked outside the configured report directory.
        if not path.resolve().is_relative_to(REPORTS):
            continue
        alias = path.stem if path.parent == REPORTS else path.parent.name
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get('status'), str):
                raise ValueError('not a health report')
            reports.append({'name': str(path.relative_to(REPORTS)), 'alias': alias, 'data': data})
        except (OSError, ValueError) as error:
            errors.append(f'{path.name}: {error}')
    return {'reports': reports, 'errors': errors, 'source': str(REPORTS)}


def overview():
    if LOCAL:
        return local_overview()
    state = read_state()
    current = dt.datetime.now(dt.timezone.utc)
    for host in state['hosts'].values():
        attempt = host.get('attempt', {})
        try:
            end = dt.datetime.fromisoformat(attempt['ended_at'])
            host['stale'] = (current - end).total_seconds() > STALE
        except (KeyError, TypeError, ValueError):
            host['stale'] = True
    return state


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body, content_type = b'ok\n', 'text/plain; charset=utf-8'
        elif self.path == '/api/overview':
            try:
                body = json.dumps(overview()).encode()
            except (OSError, ValueError):
                self.send_error(503, 'State unavailable')
                return
            content_type = 'application/json'
        elif self.path == '/style.css':
            body, content_type = (ROOT / 'dashboard/style.css').read_bytes(), 'text/css; charset=utf-8'
        elif self.path == '/assets/sudoctor-mascot.png':
            body, content_type = (ROOT / 'dashboard/assets/sudoctor-mascot.png').read_bytes(), 'image/png'
        elif self.path in ('/', '/index.html'):
            body, content_type = (ROOT / 'dashboard/index.html').read_bytes(), 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    if STALE < 1:
        sys.exit('Stale threshold must be positive')
    address = '127.0.0.1' if LOCAL else '0.0.0.0'
    port = int(os.environ.get('SUDOCTOR_HTTP_PORT', '8000'))
    print(f'Dashboard: http://{address}:{port}', flush=True)
    ThreadingHTTPServer((address, port), Handler).serve_forever()
