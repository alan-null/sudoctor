import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dashboard_version', ROOT / 'scripts/sudoctor-dashboard.py')
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


def serve(test, handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test.addCleanup(server.server_close)
    test.addCleanup(thread.join)
    test.addCleanup(server.shutdown)
    return f'http://127.0.0.1:{server.server_port}'


class UpstreamState:
    body = b'0.2.0\n'
    status = 200
    hits = 0


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        UpstreamState.hits += 1
        self.send_response(UpstreamState.status)
        self.end_headers()
        self.wfile.write(UpstreamState.body)

    def log_message(self, *args):
        pass


class VersionTests(unittest.TestCase):
    def setUp(self):
        UpstreamState.body, UpstreamState.status, UpstreamState.hits = b'0.2.0\n', 200, 0
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        Path(self.root.name, 'VERSION').write_text('0.1.0\n')
        url = serve(self, Upstream) + '/VERSION'
        for name, value in (('ROOT', Path(self.root.name)), ('STATE', Path(self.root.name)), ('LOCAL', False),
                            ('UPDATE_URL', url), ('UPDATE_CHECK', True), ('_latest', {'value': None, 'expires': 0.0})):
            patcher = patch.object(dashboard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_repository_version_is_semver(self):
        self.assertIsNotNone(dashboard.parse_version((ROOT / 'VERSION').read_text()))

    def test_parse_version(self):
        self.assertEqual(dashboard.parse_version('v1.2.10'), (1, 2, 10))
        self.assertGreater(dashboard.parse_version('0.10.0'), dashboard.parse_version('0.9.9'))
        for value in (None, '', '0.2.0-dev', 'latest', '<html>'):
            self.assertIsNone(dashboard.parse_version(value))

    def write_scheduler(self, version):
        Path(self.root.name, 'state.json').write_text(json.dumps({'hosts': {}, 'scheduler_version': version}))

    def versions(self, info):
        return {c['name']: (c['version'], c['outdated']) for c in info['components']}

    def test_update_available(self):
        info = dashboard.version_info()
        self.assertEqual((info['latest'], info['update_available']), ('0.2.0', True))
        self.assertEqual(self.versions(info), {'Dashboard': ('0.1.0', True)})
        self.assertTrue(info['url'].endswith('/releases/tag/v0.2.0'))

    def test_up_to_date_and_newer_local(self):
        UpstreamState.body = b'0.1.0'
        info = dashboard.version_info()
        self.assertFalse(info['update_available'])
        self.assertTrue(info['url'].endswith('/releases'))
        Path(self.root.name, 'VERSION').write_text('0.3.0')
        self.assertFalse(dashboard.version_info()['update_available'])

    def test_scheduler_version_from_state(self):
        self.write_scheduler('0.1.0')
        info = dashboard.version_info()
        self.assertEqual(self.versions(info), {'Dashboard': ('0.1.0', True), 'Scheduler': ('0.1.0', True)})
        self.write_scheduler('0.2.0')
        self.assertEqual(self.versions(dashboard.version_info()), {'Dashboard': ('0.1.0', True), 'Scheduler': ('0.2.0', False)})

    def test_component_mismatch_without_release_check(self):
        self.write_scheduler('0.3.0')
        with patch.object(dashboard, 'UPDATE_CHECK', False):
            info = dashboard.version_info()
        self.assertEqual((info['latest'], info['update_available']), ('0.3.0', True))
        self.assertEqual(self.versions(info), {'Dashboard': ('0.1.0', True), 'Scheduler': ('0.3.0', False)})
        self.assertEqual(UpstreamState.hits, 0)

    def test_local_mode_ignores_scheduler_state(self):
        self.write_scheduler('0.3.0')
        with patch.object(dashboard, 'LOCAL', True):
            self.assertEqual(list(self.versions(dashboard.version_info())), ['Dashboard'])

    def test_missing_scheduler_version_is_omitted(self):
        Path(self.root.name, 'state.json').write_text('{"hosts": {}}')
        self.assertEqual(list(self.versions(dashboard.version_info())), ['Dashboard'])

    def test_result_is_cached(self):
        dashboard.version_info()
        dashboard.version_info()
        self.assertEqual(UpstreamState.hits, 1)

    def test_failures_are_cached_and_hidden(self):
        for status, body in ((500, b'0.9.0'), (200, b'<html>not a version</html>')):
            with self.subTest(status=status):
                dashboard._latest.update(value=None, expires=0.0)
                UpstreamState.status, UpstreamState.body, UpstreamState.hits = status, body, 0
                info = dashboard.version_info()
                self.assertEqual((info['latest'], info['update_available']), (None, False))
                self.assertEqual(self.versions(info), {'Dashboard': ('0.1.0', False)})
                dashboard.version_info()
                self.assertEqual(UpstreamState.hits, 1)

    def test_disabled_or_dev_version_skips_request(self):
        with patch.object(dashboard, 'UPDATE_CHECK', False):
            self.assertEqual(self.versions(dashboard.version_info()), {'Dashboard': ('0.1.0', False)})
        Path(self.root.name, 'VERSION').write_text('0.2.0-dev')
        self.assertFalse(dashboard.version_info()['update_available'])
        Path(self.root.name, 'VERSION').unlink()
        self.assertEqual(self.versions(dashboard.version_info()), {'Dashboard': (None, False)})
        self.assertEqual(UpstreamState.hits, 0)

    def test_http_route(self):
        base = serve(self, dashboard.Handler)
        with urllib.request.urlopen(base + '/api/version') as response:
            self.assertEqual(response.headers.get_content_type(), 'application/json')
            data = json.load(response)
        self.assertEqual((data['latest'], data['update_available']), ('0.2.0', True))
        self.assertEqual(data['components'], [{'name': 'Dashboard', 'version': '0.1.0', 'outdated': True}])


if __name__ == '__main__':
    unittest.main()
