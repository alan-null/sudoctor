#!/usr/bin/env python3
"""Run scheduled host checks."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('SUDOCTOR_STATE_DIR', '/state'))
TIMEOUT = int(os.environ.get('SUDOCTOR_HOST_TIMEOUT', '180'))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_state():
    try:
        return json.loads((STATE / 'state.json').read_text())
    except FileNotFoundError:
        return {'hosts': {}, 'error': None, 'last_sweep': None}


def save_state(state):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=STATE, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, STATE / 'state.json')
    finally:
        if os.path.exists(path):
            os.unlink(path)


def inventory_hosts(data):
    found, visited = set(), set()
    def visit(group):
        if group in visited:
            return
        visited.add(group)
        value = data.get(group, {})
        found.update(value.get('hosts', []))
        for child in value.get('children', []):
            visit(child)
    visit('servers')
    if not found:
        raise ValueError('The servers inventory group is empty or missing')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', host) for host in found):
        raise ValueError('Inventory aliases must contain only letters, digits, dots, underscores, or hyphens')
    return sorted(found)


def run_command(command, timeout, env):
    # A temporary file keeps a noisy Ansible process from exhausting memory.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output, stderr=errors, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return None, ''
        output.seek(0)
        text = output.read(1024 * 1024).decode('utf-8', errors='replace')
        if command[0] != 'ansible-inventory':
            errors.seek(0)
            text += errors.read(1024 * 1024).decode('utf-8', errors='replace')
        return code, text


def sweep():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / 'sweep.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A sweep is already running', file=sys.stderr)
            return 1
        state = read_state()
        for host in state['hosts'].values():
            if host.get('attempt', {}).get('outcome') == 'running':
                host['attempt'].update(outcome='check_error', ended_at=now(), error='Check interrupted')
        state['error'] = None
        save_state(state)
        env = os.environ.copy()
        inventory = Path(env.get('SUDOCTOR_INVENTORY') or Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'sudoctor/inventory/hosts.ini').expanduser().resolve()
        env.update(SUDOCTOR_INVENTORY=str(inventory), ANSIBLE_CONFIG=str(ROOT / 'ansible.cfg'))
        env.update(ANSIBLE_NOCOLOR='1', ANSIBLE_HOST_KEY_CHECKING='True', ANSIBLE_SSH_ARGS='-o BatchMode=yes -o ConnectTimeout=15')
        try:
            code, output = run_command(['ansible-inventory', '--inventory', str(inventory), '--list'], TIMEOUT, env)
            if code != 0:
                raise ValueError('Inventory could not be loaded')
            hosts = inventory_hosts(json.loads(output))
        except (ValueError, OSError, json.JSONDecodeError):
            state['error'] = 'Inventory could not be loaded or contains no valid hosts'
            save_state(state)
            print(state['error'], file=sys.stderr)
            return 1
        state['hosts'] = {name: state['hosts'].get(name, {}) for name in hosts}
        save_state(state)
        for name in hosts:
            host = state['hosts'][name]
            attempt = {'id': str(uuid.uuid4()), 'started_at': now(), 'ended_at': None, 'outcome': 'running', 'error': None}
            host['attempt'] = attempt
            save_state(state)
            with tempfile.TemporaryDirectory(prefix='sudoctor-') as directory:
                env['SUDOCTOR_REPORT_DIR'] = directory
                try:
                    code, output = run_command([str(ROOT / 'scripts/sudoctor'), 'check', name], TIMEOUT, env)
                    report_path = Path(directory) / (name + '.json')
                    report = json.loads(report_path.read_text()) if report_path.is_file() else None
                    valid = isinstance(report, dict) and report.get('status') in ('ok', 'warning', 'critical') and isinstance(report.get('timestamp_utc'), str)
                    if code is None:
                        outcome, error = 'timeout', 'Check timed out'
                    elif valid and (code == 0 or report['status'] == 'critical'):
                        outcome, error = report['status'], None
                        host['report'] = report
                        host['report_received_at'] = now()
                        archive = STATE / 'reports' / name
                        archive.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.copyfile(report_path, archive / (attempt['id'] + '.json'))
                    elif re.search(r'UNREACHABLE!|unreachable=[1-9]', output, re.I):
                        outcome, error = 'unreachable', 'SSH or Ansible could not reach the host'
                    else:
                        outcome, error = 'check_error', 'Check failed or returned an invalid report'
                except (OSError, ValueError):
                    outcome, error = 'check_error', 'Check failed or returned an invalid report'
            attempt.update(ended_at=now(), outcome=outcome, error=error)
            save_state(state)
            print(f'{name}: {outcome}', flush=True)
        state['last_sweep'] = now()
        save_state(state)
    return 0


if __name__ == '__main__':
    if TIMEOUT < 1:
        sys.exit('Timeout must be positive')
    if len(sys.argv) == 2 and sys.argv[1] == 'sweep':
        sys.exit(sweep())
    sys.exit('Usage: sudoctor-scheduled.py sweep')
