#!/usr/bin/env python3
"""Run scheduled host checks."""
import copy
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sudoctor_notify as notify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('SUDOCTOR_STATE_DIR', '/state'))
TIMEOUT = int(os.environ.get('SUDOCTOR_HOST_TIMEOUT', '180'))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def current_version():
    try:
        return (ROOT / 'VERSION').read_text().strip() or None
    except OSError:
        return None


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


def inventory_groups(data, host):
    """Return every group that contains host directly or through child groups."""
    def contains(group, path=()):
        value = data.get(group, {})
        return host in value.get('hosts', []) or any(
            child not in path and contains(child, path + (group,)) for child in value.get('children', []))
    return sorted(group for group in data if group not in ('_meta', 'all', 'ungrouped') and contains(group))


def notify_sweep(state, observations, hosts):
    """Send notifications; a configuration or plugin problem is recorded, never fatal to checks."""
    try:
        status = state.setdefault('notify', {})
        enabled = notify.run(observations, hosts, status.setdefault('alerts', {}), status=status)
        state['notify']['error'] = None
        if not enabled:
            state.pop('notify')
    except notify.ConfigError as exc:
        state['notify']['error'] = str(exc)
        print('notify: ' + str(exc), file=sys.stderr, flush=True)


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


def check_account(variables, directory, env):
    """Log in as the host's sudoctor_check_user, if configured."""
    user = variables.get('sudoctor_check_user')
    if not user:
        return
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', str(user)):
        raise ValueError('Invalid sudoctor_check_user')
    lines = ['ansible_user: ' + json.dumps(user)]
    if variables.get('sudoctor_check_key_file'):
        lines.append('ansible_ssh_private_key_file: ' + json.dumps(str(variables['sudoctor_check_key_file'])))
    if variables.get('sudoctor_check_password_file'):
        password = Path(variables['sudoctor_check_password_file']).read_text().rstrip('\n')
        lines.append('ansible_password: !unsafe ' + json.dumps(password))
        # BatchMode=yes would stop sshpass from answering the password prompt.
        env['ANSIBLE_SSH_ARGS'] = '-o StrictHostKeyChecking=yes -o ConnectTimeout=15'
    path = Path(directory, 'check-account.yml')
    path.write_text('\n'.join(lines) + '\n')
    # Extra vars outrank inventory, so setup and manual runs keep the normal SSH account.
    env['SUDOCTOR_BECOME_ARGS'] = (env.get('SUDOCTOR_BECOME_ARGS', '') + ' -e @' + str(path)).strip()


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
        # The dashboard reads this from the shared volume to spot an outdated scheduler image.
        state['scheduler_version'] = current_version()
        save_state(state)
        env = os.environ.copy()
        inventory = Path(env.get('SUDOCTOR_INVENTORY') or Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'sudoctor/inventory/hosts.ini').expanduser().resolve()
        env.update(SUDOCTOR_INVENTORY=str(inventory), ANSIBLE_CONFIG=str(ROOT / 'ansible.cfg'))
        env.update(ANSIBLE_NOCOLOR='1', ANSIBLE_HOST_KEY_CHECKING='True', ANSIBLE_SSH_ARGS='-o BatchMode=yes -o ConnectTimeout=15')
        try:
            code, output = run_command(['ansible-inventory', '--inventory', str(inventory), '--list'], TIMEOUT, env)
            if code != 0:
                raise ValueError('Inventory could not be loaded')
            data = json.loads(output)
            hosts = inventory_hosts(data)
            hostvars = data.get('_meta', {}).get('hostvars', {})
        except (ValueError, OSError, json.JSONDecodeError):
            state['error'] = 'Inventory could not be loaded or contains no valid hosts'
            save_state(state)
            print(state['error'], file=sys.stderr)
            return 1
        state['hosts'] = {name: state['hosts'].get(name, {}) for name in hosts}
        save_state(state)
        observations = []
        for name in hosts:
            host = state['hosts'][name]
            previous = copy.deepcopy(host)
            attempt = {'id': str(uuid.uuid4()), 'started_at': now(), 'ended_at': None, 'outcome': 'running', 'error': None}
            host['attempt'] = attempt
            save_state(state)
            with tempfile.TemporaryDirectory(prefix='sudoctor-') as directory:
                host_env = dict(env, SUDOCTOR_REPORT_DIR=directory)
                try:
                    check_account(hostvars.get(name, {}), directory, host_env)
                    code, output = run_command([str(ROOT / 'scripts/sudoctor'), 'check', name], TIMEOUT, host_env)
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
            observations.append({'host': name, 'groups': inventory_groups(data, name), 'previous': previous,
                                 'current': host, 'fresh': outcome in ('ok', 'warning', 'critical')})
        notify_sweep(state, observations, hosts)
        state['last_sweep'] = now()
        save_state(state)
    return 0


if __name__ == '__main__':
    if TIMEOUT < 1:
        sys.exit('Timeout must be positive')
    if len(sys.argv) == 2 and sys.argv[1] == 'sweep':
        sys.exit(sweep())
    try:
        if len(sys.argv) == 2 and sys.argv[1] == 'notify-check':
            config = notify.load_config(notify.config_path(), notify.load_plugins(notify.plugins_dir()))
            print(f'{notify.config_path()}: {len(config["providers"])} provider(s), {len(config["rules"])} rule(s)')
            for name, (plugin, _, _) in sorted(config['providers'].items()):
                print(f'  provider {name}: {plugin}')
            sys.exit(0)
        if len(sys.argv) >= 2 and sys.argv[1] == 'notify-test':
            import sudoctor_notify_preview
            sys.exit(sudoctor_notify_preview.main(sys.argv[2:]))
    except notify.ConfigError as exc:
        sys.exit(str(exc))
    sys.exit('Usage: sudoctor-scheduled.py sweep | notify-check | notify-test [PROVIDER] [options; see --help]')
