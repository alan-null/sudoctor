"""OS health evidence and failure handling without inspecting the host."""
import datetime
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('health', Path(__file__).resolve().parents[1] / 'scripts/sudoctor-health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class OSHealthTests(unittest.TestCase):
    def collect(self, commands, files=None):
        with patch.object(health, 'command', side_effect=lambda args, **kwargs: commands.get(args[0])), patch.object(health, 'read_text', side_effect=lambda path: (files or {}).get(path)), patch.object(health.pathlib.Path, 'glob', return_value=[]), patch.object(health.pathlib.Path, 'stat', side_effect=OSError), patch.object(health.pathlib.Path, 'exists', return_value=False):
            checks, pending, reboot = health.os_update_checks(datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
        return {c['id'].split('.', 1)[1]: c for c in checks}, pending, reboot

    def test_missing_evidence_is_unknown(self):
        checks, pending, reboot = self.collect({})
        self.assertIsNone(pending)
        for key in ('release', 'available', 'security', 'indexes', 'support', 'packages', 'held', 'last_operation', 'unattended', 'automatic_policy'):
            self.assertEqual(checks[key]['status'], 'unknown', key)
        self.assertEqual(checks['restarts']['status'], 'skipped')

    def test_cached_candidates_holds_and_incomplete_packages(self):
        def result(stdout='', code=0):
            return SimpleNamespace(stdout=stdout, returncode=code)
        checks, pending, _ = self.collect({
            'apt': result('Listing...\nopenssl/jammy-security,jammy-updates 3 amd64 [upgradable from: 2]\ncurl/jammy-updates 4 amd64 [upgradable from: 3]\n'),
            'dpkg': result('Package libexample is only half configured'),
            'apt-get': result(code=100), 'apt-mark': result('linux-image-generic\n'),
            'apt-config': result('APT::Periodic::Unattended-Upgrade "1";\n'),
        }, {'/etc/os-release': 'ID=ubuntu\nVERSION_CODENAME=jammy\nPRETTY_NAME="Ubuntu 22.04"',
            '/usr/share/distro-info/ubuntu.csv': 'series,eol\njammy,2027-06-01\n',
            '/var/log/apt/history.log': 'End-Date: 2026-11-01  03:00:00\nError: previous failure\n'})
        self.assertEqual(pending, 2)
        self.assertEqual(checks['security']['value'], 1)
        for key in ('available', 'security', 'packages', 'dependencies', 'held', 'failures'):
            self.assertEqual(checks[key]['status'], 'warning', key)
        self.assertEqual(checks['support']['value'], '2027-06-01')
        self.assertIn('does not prove', checks['last_operation']['message'])
        self.assertEqual(checks['automatic_policy']['value'], {'Unattended-Upgrade': '1'})

    def test_unsupported_or_expired_lifecycle(self):
        checks, _, _ = self.collect({}, {'/etc/os-release': 'ID=debian\nVERSION_CODENAME=buster', '/usr/share/distro-info/debian.csv': 'series,eol\nbuster,2022-09-10\n'})
        self.assertEqual(checks['support']['status'], 'warning')
        self.assertIn('ended', checks['support']['message'])
        checks, _, _ = self.collect({}, {'/etc/os-release': 'ID=debian\nVERSION_CODENAME=unknown'})
        self.assertEqual(checks['support']['status'], 'unknown')

    def test_command_uses_stable_locale_and_timeout(self):
        with patch.object(health.subprocess, 'run') as run:
            health.command(['apt', 'list'], timeout=30)
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C.UTF-8')
        self.assertEqual(run.call_args.kwargs['timeout'], 30)


if __name__ == '__main__':
    unittest.main()
