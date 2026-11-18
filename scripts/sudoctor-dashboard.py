#!/usr/bin/env python3
"""Serve dashboard assets and check history."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('SUDOCTOR_STATE_DIR', '/state'))
LOCAL = '--local' in sys.argv[1:]
REPORTS = Path(os.environ.get('SUDOCTOR_REPORT_DIR') or Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'sudoctor/reports').expanduser().resolve()
STALE = int(os.environ.get('SUDOCTOR_STALE_SECONDS', '3600'))
UPDATE_CHECK = os.environ.get('SUDOCTOR_UPDATE_CHECK', 'true').strip().lower() not in ('0', 'false', 'no', 'off')
UPDATE_URL = os.environ.get('SUDOCTOR_UPDATE_URL', 'https://raw.githubusercontent.com/alan-null/sudoctor/master/VERSION')
RELEASES_URL = 'https://github.com/alan-null/sudoctor/releases'
UPDATE_TTL, UPDATE_RETRY, UPDATE_TIMEOUT = 86400, 3600, 3
SEMVER = re.compile(r'(\d+)\.(\d+)\.(\d+)')
_latest = {'value': None, 'expires': 0.0}
_latest_lock = threading.Lock()


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
    reports = []
    archive_root = STATE / 'reports'
    for name, host in state['hosts'].items():
        reports.append({'name': name + '.json', 'alias': name,
                        'data': host.get('report') or {'status': 'unknown', 'hostname': name}})
        for path in sorted((archive_root / name).glob('*.json')):
            if not path.resolve().is_relative_to(archive_root.resolve()):
                continue
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and isinstance(data.get('status'), str):
                    reports.append({'name': name + '/' + path.name, 'alias': name, 'data': data})
            except (OSError, ValueError):
                continue
    state['reports'] = reports
    current = dt.datetime.now(dt.timezone.utc)
    for host in state['hosts'].values():
        attempt = host.get('attempt', {})
        try:
            end = dt.datetime.fromisoformat(attempt['ended_at'])
            host['stale'] = (current - end).total_seconds() > STALE
        except (KeyError, TypeError, ValueError):
            host['stale'] = True
    return state


def current_version():
    try:
        return (ROOT / 'VERSION').read_text().strip() or None
    except OSError:
        return None


def parse_version(value):
    match = SEMVER.fullmatch(str(value or '').strip().removeprefix('v'))
    return tuple(map(int, match.groups())) if match else None


def latest_version():
    """Return the newest published version, fetched at most once per TTL.

    Failures are cached for a shorter retry window so an unreachable upstream
    never costs more than one outbound request per hour.
    """
    with _latest_lock:
        now = time.monotonic()
        if now < _latest['expires']:
            return _latest['value']
        value = None
        try:
            request = urllib.request.Request(UPDATE_URL, headers={'User-Agent': 'sudoctor-dashboard'})
            with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT) as response:
                text = response.read(64).decode('ascii', 'replace').strip()
            value = text if parse_version(text) else None
        except (OSError, ValueError):
            value = None
        _latest.update(value=value, expires=now + (UPDATE_TTL if value else UPDATE_RETRY))
        return value


def version_info():
    """Report each running component's version against the newest one known.

    The target is the newest of the published release and every running
    component, so a mismatch between dashboard and scheduler shows even when
    the release check is disabled or offline.
    """
    components = [{'name': 'Dashboard', 'version': current_version()}]
    if not LOCAL:
        try:
            scheduler = read_state().get('scheduler_version')
        except (OSError, ValueError):
            scheduler = None
        if scheduler:
            components.append({'name': 'Scheduler', 'version': str(scheduler)})
    releases = [parse_version(c['version']) for c in components]
    # Development or unknown versions are never compared against releases.
    latest = latest_version() if UPDATE_CHECK and any(releases) else None
    known = [v for v in releases + [parse_version(latest)] if v]
    target = max(known) if known else None
    for component, release in zip(components, releases):
        component['outdated'] = bool(release and release < target)
    update = any(c['outdated'] for c in components)
    return {'latest': '.'.join(map(str, target)) if update else latest, 'update_available': update,
            'url': f'{RELEASES_URL}/tag/v{".".join(map(str, target))}' if update else RELEASES_URL,
            'components': components}


STATIC = {
    '/favicon.ico': ('favicon.ico', 'image/x-icon'),
    '/assets/favicon-32.png': ('assets/favicon-32.png', 'image/png'),
    '/assets/apple-touch-icon.png': ('assets/apple-touch-icon.png', 'image/png'),
    '/assets/sudoctor-mascot.png': ('assets/sudoctor-mascot.png', 'image/png'),
}


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
        elif self.path == '/api/version':
            body, content_type = json.dumps(version_info()).encode(), 'application/json'
        elif self.path == '/style.css':
            body, content_type = (ROOT / 'dashboard/style.css').read_bytes(), 'text/css; charset=utf-8'
        elif self.path in STATIC:
            name, content_type = STATIC[self.path]
            body = (ROOT / 'dashboard' / name).read_bytes()
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
