"""OS health evidence and failure handling without inspecting the host."""
import datetime
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('health', Path(__file__).resolve().parents[1] / 'scripts/sudoctor-health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class OSHealthTests(unittest.TestCase):
    def test_http_error_status_matches_expectation(self):
        error = urllib.error.HTTPError('http://example.test', 401, 'Unauthorized', {}, None)
        with patch.object(health.urllib.request, 'urlopen', side_effect=error):
            self.assertEqual(health.check_http('http://example.test', 401), (True, 401))
            self.assertEqual(health.check_http('http://example.test', 200), (False, 401))

    def collect(self, commands, files=None, stamps=None):
        def stat(path):
            if stamps and path.name in stamps:
                return SimpleNamespace(st_mtime=stamps[path.name])
            raise OSError

        with patch.object(health, 'command', side_effect=lambda args, **kwargs: commands.get(args[0])), patch.object(health, 'read_text', side_effect=lambda path: (files or {}).get(path)), patch.object(health.pathlib.Path, 'glob', return_value=[]), patch.object(health.pathlib.Path, 'stat', autospec=True, side_effect=stat), patch.object(health.pathlib.Path, 'exists', return_value=False):
            checks, pending, reboot = health.os_update_checks(datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc))
        return {c['id'].split('.', 1)[1]: c for c in checks}, pending, reboot

    def test_missing_evidence_is_unknown(self):
        checks, pending, reboot = self.collect({})
        self.assertIsNone(pending)
        for key in ('release', 'available', 'security', 'indexes', 'support', 'packages', 'held', 'last_operation', 'unattended', 'automatic_policy'):
            self.assertEqual(checks[key]['status'], 'unknown', key)
        self.assertNotIn('restarts', checks)

    def test_apt_index_freshness_accepts_periodic_and_success_stamps(self):
        recent = datetime.datetime(2026, 11, 3, tzinfo=datetime.timezone.utc).timestamp()
        old = datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc).timestamp()
        checks, _, _ = self.collect({}, stamps={'update-stamp': recent})
        self.assertEqual(checks['indexes']['status'], 'ok')
        self.assertEqual(checks['indexes']['value'], '2026-11-03T00:00:00+00:00')
        checks, _, _ = self.collect({}, stamps={'update-success-stamp': old, 'update-stamp': recent})
        self.assertEqual(checks['indexes']['value'], '2026-11-03T00:00:00+00:00')

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

    def test_security_missing_evidence_stays_unknown(self):
        with patch.object(health, 'command', return_value=None), patch.object(health, 'read_text', return_value=None):
            checks = health.security_checks({}, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc), [])
        by_id = {c['id']: c for c in checks}
        for key in ('security.ssh_passwordauthentication', 'security.ssh_permitrootlogin', 'security.ssh_service', 'security.firewall', 'security.failed_logins', 'security.listeners', 'security.time'):
            self.assertEqual(by_id[key]['status'], 'unknown', key)

    def test_security_policy_and_allowlist(self):
        def result(stdout, code=0):
            return SimpleNamespace(stdout=stdout, returncode=code)
        def run(args, **kwargs):
            if args[:2] == ['sshd', '-T']:
                self.assertIn('user=alice', args[-1])
                return result('passwordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin prohibit-password\n')
            if args[0] == 'nft':
                return result('chain input {\n type filter hook input priority 0; policy drop;\n tcp dport 22 accept\n }')
            if args[0] == 'systemctl':
                return result('active\n')
            if args[0] == 'journalctl':
                return result('Failed password\nAccepted publickey\n')
            if args[0] == 'timedatectl':
                return result('yes\n')
            return None
        cfg = {'security': {'ssh': {'context': {'user': 'alice', 'addr': '192.0.2.1'}},
                            'firewall': {'backend': 'nftables', 'expected_rules': ['tcp dport 22 accept']},
                            'allowed_listeners': ['127.0.0.1:22']}}
        files = {'/proc/net/tcp': 'header\n 0: 0100007F:0016 00000000:0000 0A\n', '/proc/net/tcp6': 'header\n'}
        with patch.object(health, 'command', side_effect=run), patch.object(health, 'read_text', side_effect=lambda path: files.get(path)):
            checks = health.security_checks(cfg, datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc), [])
        by_id = {c['id']: c for c in checks}
        self.assertEqual(by_id['security.ssh_passwordauthentication']['status'], 'ok')
        self.assertEqual(by_id['security.ssh_permitrootlogin']['status'], 'warning')
        self.assertEqual(by_id['security.firewall']['status'], 'ok')
        self.assertEqual(by_id['security.listeners']['status'], 'ok')
        self.assertEqual(by_id['security.failed_logins']['value'], 1)

    def test_security_no_firewall_rule_or_listener_baseline_is_not_pass(self):
        def result(stdout, code=0):
            return SimpleNamespace(stdout=stdout, returncode=code)
        with patch.object(health, 'command', side_effect=lambda args, **kwargs: result('table inet filter {}') if args[0] == 'nft' else None), patch.object(health, 'read_text', return_value='header\n'):
            checks = health.security_checks({'security': {'firewall': {'backend': 'nftables'}}}, datetime.datetime.now(datetime.timezone.utc), [])
        by_id = {c['id']: c for c in checks}
        self.assertEqual(by_id['security.firewall']['status'], 'unknown')
        self.assertEqual(by_id['security.listeners']['status'], 'unknown')

    def test_chronyc_time_fallback(self):
        def run(args, **kwargs):
            if args[0] == 'chronyc':
                return SimpleNamespace(stdout='Leap status     : Normal\n', returncode=0)
            return None
        with patch.object(health, 'command', side_effect=run), patch.object(health, 'read_text', return_value=None):
            checks = health.security_checks({}, datetime.datetime.now(datetime.timezone.utc), [])
        self.assertEqual(next(c for c in checks if c['id'] == 'security.time')['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
