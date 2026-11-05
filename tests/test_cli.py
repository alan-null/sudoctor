"""Exercise launcher path selection without connecting to any hosts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        stub = self.bin / 'ansible-playbook'
        stub.write_text('#!/usr/bin/env python3\nimport json, os, sys\nprint(json.dumps({"args": sys.argv[1:], "reports": os.environ["SUDOCTOR_REPORT_DIR"], "config": os.environ["ANSIBLE_CONFIG"]}))\n')
        stub.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('SUDOCTOR_', 'XDG_', 'ANSIBLE_'))}
        self.env.update(HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def inventory(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[servers]\nweb01\n')
        return path

    def run_cli(self, *args):
        return subprocess.run([str(ROOT / 'scripts/sudoctor'), *args], cwd=self.home, env=self.env, text=True, capture_output=True)

    def test_default_inventory_and_single_host(self):
        inventory = self.inventory(self.home / '.config/sudoctor/inventory/hosts.ini')
        result = self.run_cli('check', 'web01')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['args'][-4:], ['--inventory', str(inventory), '--limit', 'web01'])
        self.assertEqual(data['reports'], str(self.home / '.local/state/sudoctor/reports'))
        self.assertEqual(data['config'], str(ROOT / 'ansible.cfg'))

    def test_explicit_relative_inventory_overrides_environment(self):
        inventory = self.inventory(self.home / 'private inventory/hosts.ini')
        self.env.update(SUDOCTOR_INVENTORY='/missing', SUDOCTOR_REPORT_DIR='local reports')
        result = self.run_cli('check', '--inventory', 'private inventory/hosts.ini', 'web01')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn(str(inventory), data['args'])
        self.assertEqual(data['reports'], str(self.home / 'local reports'))

    def test_environment_and_xdg_defaults(self):
        inventory = self.inventory(self.home / 'custom/sudoctor/inventory/hosts.ini')
        self.env.update(XDG_CONFIG_HOME=str(self.home / 'custom'), XDG_STATE_HOME=str(self.home / 'state'))
        result = self.run_cli('check')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn(str(inventory), data['args'])
        self.assertEqual(data['args'][-1], 'servers')
        self.assertEqual(data['reports'], str(self.home / 'state/sudoctor/reports'))
        self.env['SUDOCTOR_INVENTORY'] = str(inventory)
        self.env['XDG_CONFIG_HOME'] = '/missing'
        self.assertEqual(self.run_cli('check').returncode, 0)

    def test_missing_inventory_and_invalid_arguments(self):
        result = self.run_cli('check')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Inventory not found:', result.stderr)
        for args in [('check', '--inventory'), ('check', 'servers', 'web01')]:
            self.assertEqual(self.run_cli(*args).returncode, 2)

    def test_dashboard_uses_local_server_without_inventory(self):
        stub = self.bin / 'python3'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        result = self.run_cli('dashboard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(ROOT / 'scripts/sudoctor-dashboard.py'), '--local'])

    def test_destructive_action_still_requires_confirmation(self):
        self.inventory(self.home / '.config/sudoctor/inventory/hosts.ini')
        self.assertEqual(self.run_cli('reboot', 'web01').returncode, 2)
        self.assertEqual(self.run_cli('reboot', 'web01', '--yes').returncode, 0)
