import datetime as dt
import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('scheduled', Path(__file__).resolve().parents[1] / 'scripts/sudoctor-scheduled.py')
scheduled = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduled)
dashboard_spec = importlib.util.spec_from_file_location('dashboard', Path(__file__).resolve().parents[1] / 'scripts/sudoctor-dashboard.py')
dashboard = importlib.util.module_from_spec(dashboard_spec)
dashboard_spec.loader.exec_module(dashboard)


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_patch = patch.object(scheduled, 'STATE', Path(self.temp.name))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.dashboard_state_patch = patch.object(dashboard, 'STATE', Path(self.temp.name))
        self.dashboard_state_patch.start()
        self.addCleanup(self.dashboard_state_patch.stop)

    def test_nested_inventory(self):
        self.assertEqual(scheduled.inventory_hosts({'servers': {'hosts': ['b'], 'children': ['group']}, 'group': {'hosts': ['a']}}), ['a', 'b'])
        with self.assertRaises(ValueError):
            scheduled.inventory_hosts({'servers': {'hosts': ['host:all']}})

    def test_outcomes_and_previous_report(self):
        responses = {'a': (0, 'ok'), 'b': (2, 'critical'), 'c': (2, None), 'd': (None, None), 'e': (1, None), 'f': (0, 'warning')}
        def command(cmd, timeout, env):
            if cmd[0] == 'ansible-inventory':
                return 0, json.dumps({'servers': {'hosts': list(responses)}})
            name = cmd[-1]
            code, status = responses[name]
            if status:
                Path(env['SUDOCTOR_REPORT_DIR'], name + '.json').write_text(json.dumps({'status': status, 'timestamp_utc': '2026-01-01T00:00:00Z'}))
            return code, 'UNREACHABLE!' if name == 'c' else ''
        scheduled.save_state({'hosts': {'c': {'report': {'status': 'ok'}}}, 'error': None})
        with patch.object(scheduled, 'run_command', command):
            self.assertEqual(scheduled.sweep(), 0)
        state = scheduled.read_state()
        self.assertEqual([state['hosts'][name]['attempt']['outcome'] for name in responses], ['ok', 'critical', 'unreachable', 'timeout', 'check_error', 'warning'])
        self.assertEqual(state['hosts']['c']['report']['status'], 'ok')
        self.assertEqual(len(list((scheduled.STATE / 'reports' / 'b').iterdir())), 1)

    def test_invalid_inventory_and_interrupted_attempt(self):
        scheduled.save_state({'hosts': {'a': {'attempt': {'outcome': 'running'}}}})
        with patch.object(scheduled, 'run_command', return_value=(0, '{}')):
            self.assertEqual(scheduled.sweep(), 1)
        state = scheduled.read_state()
        self.assertEqual(state['hosts']['a']['attempt']['outcome'], 'check_error')
        self.assertTrue(state['error'])

    def test_http_routes(self):
        scheduled.save_state({'hosts': {}, 'error': None})
        server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(base + '/api/overview') as response:
            self.assertEqual(json.load(response)['hosts'], {})
        with urllib.request.urlopen(base + '/health') as response:
            self.assertEqual(response.status, 200)
        with urllib.request.urlopen(base + '/style.css') as response:
            self.assertEqual(response.headers.get_content_type(), 'text/css')
            self.assertIn(b'--green:#2bea5b', response.read())
        for path in ('/state.json', '/inventory/hosts.ini', '/%2e%2e/state.json'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

    def test_local_reports_and_history(self):
        reports = Path(self.temp.name) / 'local-reports'
        (reports / 'web01').mkdir(parents=True)
        report = {'status': 'ok', 'timestamp_utc': '2026-01-01T00:00:00Z'}
        (reports / 'web01.json').write_text(json.dumps(report))
        (reports / 'web01' / 'snapshot.json').write_text(json.dumps(report))
        (reports / 'broken.json').write_text('{')
        (reports / 'invalid.json').write_text('[]')
        outside = Path(self.temp.name) / 'private.json'
        outside.write_text(json.dumps(report))
        (reports / 'linked.json').symlink_to(outside)
        with patch.object(dashboard, 'LOCAL', True), patch.object(dashboard, 'REPORTS', reports):
            state = dashboard.overview()
        self.assertEqual(len(state['reports']), 2)
        self.assertEqual({r['alias'] for r in state['reports']}, {'web01'})
        self.assertEqual(len(state['errors']), 2)
        self.assertEqual(state['source'], str(reports))

    def test_local_empty_report_directory(self):
        with patch.object(dashboard, 'LOCAL', True), patch.object(dashboard, 'REPORTS', Path(self.temp.name) / 'missing'):
            self.assertEqual(dashboard.overview()['reports'], [])

    def test_stale_and_lock(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=dashboard.STALE + 1)).isoformat()
        scheduled.save_state({'hosts': {'a': {'attempt': {'ended_at': old}}}})
        self.assertTrue(dashboard.overview()['hosts']['a']['stale'])
        with (scheduled.STATE / 'sweep.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(scheduled.sweep(), 1)


if __name__ == '__main__':
    unittest.main()
