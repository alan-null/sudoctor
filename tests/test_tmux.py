"""Integration checks on a private tmux server; no changes to user sessions."""
import copy
import importlib.util
import pathlib
import shutil
import subprocess
import unittest
import uuid
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


restorer = load('restorer', 'sudoctor-tmux.py')
health = load('health', 'sudoctor-health.py')


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required')
class TmuxIntegration(unittest.TestCase):
    def setUp(self):
        self.socket = 'sudoctor-test-' + uuid.uuid4().hex
        self.cfg = {'name': 'test-apps', 'cwd': '/tmp', 'windows': [
            {'name': 'worker', 'layout': 'even-horizontal', 'panes': [
                {'name': 'app', 'command': 'exec sleep 600'},
                {'name': 'logs', 'command': 'exec sleep 600'}]},
            {'name': 'legacy', 'command': 'exec sleep 600'}]}
        self.run_patch = patch.object(restorer, 'run', self.run_tmux)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.addCleanup(self.run_tmux, 'tmux', 'kill-server')

    def run_tmux(self, *args):
        return subprocess.run([args[0], '-L', self.socket, '-f', '/dev/null', *args[1:]],
                              capture_output=True, text=True, check=False)

    def report(self):
        with patch.object(health, 'command', lambda args: self.run_tmux(*args[4:])):
            return health.check_tmux('unused', self.cfg['name'], self.cfg['windows'])

    def test_restore_health_recovery_and_idempotency(self):
        restorer.restore(self.cfg)
        first = self.report()
        self.assertFalse(first['panes_missing'])
        self.assertFalse(first['windows_missing'])
        self.assertEqual(len(first['panes']), 3)
        self.assertTrue(all(p['program'] == 'sleep' for p in first['panes']))
        self.assertEqual(restorer.tmux('display-message', '-p', '-t', '=test-apps:=worker', '#{pane_current_path}'), '/tmp')
        restorer.restore(self.cfg)
        self.assertEqual(first['panes'], self.report()['panes'])
        app = next(p for p in first['panes'] if p['name'] == 'app')
        restorer.tmux('respawn-pane', '-k', '-t', app['id'], '/bin/true')
        # A round trip to the server lets the exited process be reaped.
        subprocess.run(['sleep', '0.1'], check=True)
        self.assertIn('worker/app', self.report()['panes_missing'])
        restorer.restore(self.cfg)
        self.assertFalse(self.report()['panes_missing'])
        logs = next(p for p in first['panes'] if p['name'] == 'logs')
        restorer.tmux('kill-pane', '-t', logs['id'])
        self.assertIn('worker/logs', self.report()['panes_missing'])
        restorer.restore(self.cfg)
        self.assertEqual(len(self.report()['panes']), 3)
        restorer.tmux('kill-window', '-t', '=test-apps:=legacy')
        self.assertIn('legacy', self.report()['windows_missing'])
        restorer.restore(self.cfg)
        self.assertFalse(self.report()['windows_missing'])

    def test_exited_command_fails_restore(self):
        self.cfg['windows'] = [{'name': 'exits', 'panes': [{'name': 'app', 'command': 'exit 1'}]}]
        with self.assertRaisesRegex(RuntimeError, 'Pane missing or exited'):
            restorer.restore(self.cfg)
        self.assertIn('exits/app', self.report()['panes_missing'])

    def test_adopt_legacy_window_without_restarting(self):
        restorer.tmux('new-session', '-d', '-s', 'test-apps', '-n', 'legacy', 'sleep 600')
        before = restorer.tmux('display-message', '-p', '-t', '=test-apps:=legacy', '#{pane_pid}')
        restorer.restore(self.cfg)
        after = restorer.tmux('display-message', '-p', '-t', '=test-apps:=legacy', '#{pane_pid}')
        self.assertEqual(before, after)

    def test_collector_locale_preserves_tmux_tabs(self):
        restorer.restore(self.cfg)
        result = health.command(['tmux', '-L', self.socket, 'list-panes', '-s', '-t', '=test-apps',
                                 '-F', '#{window_name}\t#{pane_id}\t#{@sudoctor_name}\t#{pane_dead}\t#{pane_current_command}'])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()), 3)
        self.assertTrue(all(len(row.split('\t')) == 5 for row in result.stdout.splitlines()))


class Validation(unittest.TestCase):
    def test_malformed_pane_output_does_not_crash_or_pass(self):
        output = subprocess.CompletedProcess([], 0, 'main_%0_Discord_0_./_BasicBot\n')
        with patch.object(health, 'command', return_value=output):
            report = health.check_tmux('alan', 'apps', [{'name': 'main', 'command': 'sleep 600'}])
        self.assertFalse(report['running'])
        self.assertEqual(report['windows_missing'], ['main'])
        self.assertEqual(report['error'], 'Could not parse tmux pane output')

    def test_malformed_row_alongside_valid_pane_is_reported(self):
        output = subprocess.CompletedProcess([], 0, 'main\t%0\t\t0\tsleep\nbad row\n')
        with patch.object(health, 'command', return_value=output):
            report = health.check_tmux('alan', 'apps', [{'name': 'main', 'command': 'sleep 600'}])
        self.assertTrue(report['error'])
        self.assertEqual(len(report['panes']), 1)
        self.assertIsNone(report['panes'][0]['name'])

    def test_invalid_schemas(self):
        valid = {'name': 'apps', 'windows': [{'name': 'worker', 'panes': [{'name': 'app', 'command': 'sleep 600'}]}]}
        cases = []
        for key, value in [('panes', []), ('layout', 'wrong'), ('command', 'sleep 600'), ('cwd', '~/apps')]:
            cfg = copy.deepcopy(valid)
            cfg['windows'][0][key] = value
            cases.append(cfg)
        cfg = copy.deepcopy(valid)
        cfg['windows'].append(copy.deepcopy(cfg['windows'][0]))
        cases.append(cfg)
        cfg = copy.deepcopy(valid)
        cfg['windows'][0]['panes'] *= 2
        cases.append(cfg)
        for cfg in cases:
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                restorer.validate(cfg)


if __name__ == '__main__':
    unittest.main()
