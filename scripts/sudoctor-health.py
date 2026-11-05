#!/usr/bin/env python3
"""Standard-library health collector; single JSON document on stdout."""
import datetime
import csv
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def command(args, timeout=12):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                              env={**os.environ, 'LC_ALL': 'C.UTF-8'})
    except (OSError, subprocess.TimeoutExpired):
        return None


def active_service(name):
    p = command(['systemctl', 'is-active', name])
    return p is not None and p.returncode == 0 and p.stdout.strip() == 'active'


def inspect_docker(name):
    p = command(['docker', 'inspect', '--format', '{{json .State}}', name])
    if p is None or p.returncode != 0:
        return {'exists': False, 'running': False, 'health': None}
    try:
        state = json.loads(p.stdout)
        return {'exists': True, 'running': bool(state.get('Running')),
                'health': state.get('Health', {}).get('Status'),
                'restarts': state.get('Restarting', False)}
    except (ValueError, TypeError):
        return {'exists': False, 'running': False, 'health': None}


def check_tcp(host, port, timeout=3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def check_http(url, expected):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'sudoctor/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == int(expected), resp.status
    except urllib.error.HTTPError as e:
        return e.code == int(expected), e.code
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return False, str(e)


def check_tmux(user, session, windows):
    p = command(['runuser', '-u', user, '--', 'tmux', 'list-panes', '-s', '-t', '=' + session,
                 '-F', '#{window_name}\t#{pane_id}\t#{@sudoctor_name}\t#{pane_dead}\t#{pane_current_command}'])
    rows = [row.split('\t', 4) for row in p.stdout.splitlines()] if p and p.returncode == 0 else []
    malformed = any(len(row) != 5 or row[3] not in ('0', '1') for row in rows)
    rows = [row for row in rows if len(row) == 5 and row[3] in ('0', '1')]
    actual = {row[0] for row in rows}
    missing = []
    for window in windows:
        if 'panes' in window:
            live = {row[2] for row in rows if row[0] == window['name'] and row[3] == '0'}
            missing.extend(window['name'] + '/' + pane['name'] for pane in window['panes']
                           if pane['name'] not in live)
        elif window['name'] in actual and not any(row[0] == window['name'] and row[3] == '0' for row in rows):
            missing.append(window['name'] + '/' + window['name'])
    return {'running': bool(rows), 'windows_missing': [w['name'] for w in windows if w['name'] not in actual],
            'panes_missing': missing,
            'error': 'Could not parse tmux pane output' if malformed else None,
            'panes': [{'window': row[0], 'id': row[1], 'name': row[2] or None,
                       'dead': row[3] == '1', 'program': row[4]} for row in rows]}


def read_text(path):
    try:
        return pathlib.Path(path).read_text()
    except OSError:
        return None


def os_update_checks(now=None):
    """Inspect local package metadata only. Never refresh indexes or install packages."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    checks = []

    def add(key, label, value, message, status='ok'):
        checks.append({'id': 'os_updates.' + key, 'category': 'os_updates', 'label': label,
                       'value': value, 'message': message, 'status': status})

    release = {}
    for line in (read_text('/etc/os-release') or '').splitlines():
        key, sep, value = line.partition('=')
        if sep:
            release[key] = value.strip('"\'')
    add('release', 'Operating system', release.get('PRETTY_NAME'),
        release.get('PRETTY_NAME', 'OS release unavailable'), 'ok' if release else 'unknown')
    add('kernel', 'Running kernel', os.uname().release, os.uname().release)
    kernels = sorted(p.name[8:] for p in pathlib.Path('/boot').glob('vmlinuz-*'))
    add('installed_kernels', 'Installed kernels', kernels or None,
        (', '.join(kernels) + '. Activation order is not inferred across kernel flavors.') if kernels else 'Kernel images unavailable in /boot', 'ok' if kernels else 'unknown')

    eol = None
    distro = release.get('ID', '')
    if distro in ('ubuntu', 'debian'):
        data = read_text('/usr/share/distro-info/' + distro + '.csv')
        for row in csv.DictReader((data or '').splitlines()):
            if row.get('series') == release.get('VERSION_CODENAME'):
                try:
                    eol = datetime.date.fromisoformat(row.get('eol', ''))
                except ValueError:
                    pass
    remaining = (eol - now.date()).days if eol else None
    add('support', 'Standard OS support', eol.isoformat() if eol else None,
        ('Standard support ended ' if remaining is not None and remaining < 0 else 'Standard support ends ') + str(eol)
        if eol else 'No local lifecycle data. Extended support entitlement is not checked.',
        'unknown' if eol is None else 'warning' if remaining <= 90 else 'ok')

    apt = command(['apt', 'list', '--upgradable'], timeout=30)
    lines = [line for line in apt.stdout.splitlines() if '[upgradable from:' in line] if apt is not None and apt.returncode == 0 else None
    pending = len(lines) if lines is not None else None
    add('available', 'Available updates', pending,
        f'{pending} upgradable packages in local APT indexes' if pending is not None else 'APT indexes could not be read',
        'unknown' if pending is None else 'warning' if pending else 'ok')
    # Count candidates with a security suite, not every historical security version.
    security = sum(bool(re.search(r'/[^ ]*security', line.split(' ', 1)[0])) for line in lines) if lines is not None else None
    add('security', 'Security update candidates', security,
        f'{security} candidates from security suites. Cached suite names only; not a vulnerability scan.' if security is not None else 'Security candidates unavailable',
        'unknown' if security is None else 'warning' if security else 'ok')
    stamp = pathlib.Path('/var/lib/apt/periodic/update-success-stamp')
    try:
        updated = datetime.datetime.fromtimestamp(stamp.stat().st_mtime, datetime.timezone.utc)
        age = (now - updated).total_seconds() / 86400
        add('indexes', 'APT index freshness', updated.isoformat(),
            f'Last recorded successful index refresh {age:.1f} days ago', 'warning' if age > 7 or age < 0 else 'ok')
    except OSError:
        add('indexes', 'APT index freshness', None, 'No successful refresh timestamp. Update counts may be stale.', 'unknown')

    audit = command(['dpkg', '--audit'])
    known = audit is not None and audit.returncode == 0
    broken = bool(audit.stdout.strip()) if known else None
    add('packages', 'Package configuration', broken,
        audit.stdout.strip()[:2000] if broken else 'No incomplete package states reported by dpkg' if known else 'dpkg audit unavailable',
        'warning' if broken else 'ok' if known else 'unknown')
    deps = command(['apt-get', 'check', '-o', 'Debug::NoLocking=1',
                    '-o', 'Dir::Cache::pkgcache=', '-o', 'Dir::Cache::srcpkgcache='], timeout=30)
    add('dependencies', 'Package dependencies', None if deps is None else deps.returncode == 0,
        'Dependencies satisfied' if deps is not None and deps.returncode == 0 else 'Dependency check failed or unavailable',
        'unknown' if deps is None else 'ok' if deps.returncode == 0 else 'warning')
    held = command(['apt-mark', 'showhold'])
    names = held.stdout.splitlines() if held is not None and held.returncode == 0 else None
    add('held', 'Held packages', names, ', '.join(names) if names else 'None' if names == [] else 'Held packages unavailable',
        'unknown' if names is None else 'warning' if names else 'ok')
    reboot = pathlib.Path('/var/run/reboot-required').exists()
    add('reboot', 'Pending reboot', reboot, 'Reboot required to finish updates' if reboot else 'No reboot-required marker', 'warning' if reboot else 'ok')
    history = read_text('/var/log/apt/history.log')
    dates = re.findall(r'^End-Date:\s*(.+)$', history or '', re.MULTILINE)
    add('last_operation', 'Last completed APT operation', dates[-1] if dates else None,
        dates[-1] + ' host local time. Completion does not prove every package succeeded.' if dates else 'No completed operation in current APT history. Rotated logs are not inspected.',
        'ok' if dates else 'unknown')
    errors = re.findall(r'^Error:.*$', history or '', re.MULTILINE)
    add('failures', 'APT history errors', len(errors) if history is not None else None,
        f'{len(errors)} error entries in current APT history. Historical errors may already be resolved.' if history is not None else 'APT history unavailable',
        'unknown' if history is None else 'warning' if errors else 'ok')
    result = command(['systemctl', 'show', 'unattended-upgrades.service', '-p', 'LoadState', '-p', 'Result'])
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line) if result is not None and result.returncode == 0 else {}
    service_result = props.get('Result') if props.get('LoadState') == 'loaded' else None
    add('unattended', 'Unattended upgrades service result', service_result,
        f'Last systemd result: {service_result}. This does not confirm automatic update policy.' if service_result else 'Unattended upgrades result unavailable',
        'unknown' if service_result is None else 'ok' if service_result == 'success' else 'warning')
    config = command(['apt-config', 'dump'])
    policy = dict(re.findall(r'^APT::Periodic::(Update-Package-Lists|Unattended-Upgrade) "([^"\n]+)";', config.stdout, re.MULTILINE)) if config is not None and config.returncode == 0 else {}
    add('automatic_policy', 'Automatic APT update policy', policy or None,
        ', '.join(key + '=' + value for key, value in sorted(policy.items())) + '. Timer execution is not verified.'
        if policy else 'No explicit periodic update policy could be read. Effective scheduling is unknown.', 'ok' if policy else 'unknown')
    add('restarts', 'Pending service restarts', None, 'Not collected. Requires an optional restart inspection tool.', 'skipped')
    return checks, pending, reboot


def collect(cfg):
    issues = []
    warnings = []
    details = {'services': {}, 'docker': {}, 'tcp': {}, 'http': {}, 'tmux': {}}
    usage = shutil.disk_usage('/')
    disk_used = round(100 * usage.used / usage.total, 1)
    free_mb = round(usage.free / 1024**2)
    if disk_used >= 95 or free_mb < int(cfg.get('min_free_mb', 1024)):
        issues.append('root disk critical: used %.1f%%, free %d MB' % (disk_used, free_mb))
    elif disk_used >= int(cfg.get('disk_warning_percent', 85)):
        warnings.append('root disk warning: used %.1f%%' % disk_used)

    try:
        memory = {}
        for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
            k, _, v = line.partition(':')
            if k in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                memory[k] = int(v.split()[0]) // 1024
        uptime_hours = round(float(pathlib.Path('/proc/uptime').read_text().split()[0]) / 3600, 1)
    except (OSError, ValueError):
        memory, uptime_hours = {}, None

    for service in cfg.get('services', []):
        ok = active_service(service)
        details['services'][service] = ok
        if not ok:
            issues.append('systemd not active: ' + service)

    if cfg.get('docker') and not active_service('docker.service'):
        issues.append('docker.service not active')
    for item in cfg.get('docker', []):
        name = item if isinstance(item, str) else item['name']
        result = inspect_docker(name)
        details['docker'][name] = result
        if not result['running']:
            issues.append('docker not running: ' + name)
        elif result['health'] == 'unhealthy':
            issues.append('docker unhealthy: ' + name)
        elif isinstance(item, dict) and item.get('require_healthy', False) and result['health'] != 'healthy':
            issues.append('docker missing healthy state: ' + name)

    for item in cfg.get('tcp', []):
        ok = check_tcp(item['host'], item['port'])
        details['tcp'][item['name']] = ok
        if not ok:
            issues.append('tcp unreachable: ' + item['name'])
    for item in cfg.get('http', []):
        ok, result = check_http(item['url'], item.get('status', 200))
        details['http'][item['name']] = {'ok': ok, 'response': result}
        if not ok:
            issues.append('http unhealthy: ' + item['name'])
    for item in cfg.get('tmux', []):
        result = check_tmux(item['user'], item['name'], item['windows'])
        details['tmux'][item['name']] = result
        if result.get('error'):
            issues.append('tmux inspection failed: ' + item['name'] + ': ' + result['error'])
        elif not result['running'] or result['windows_missing'] or result['panes_missing']:
            issues.append('tmux missing session/windows/panes: ' + item['name'])

    checks, pending, reboot = os_update_checks()
    warnings.extend(check['label'] + ': ' + check['message'] for check in checks if check['status'] == 'warning')
    return {
        'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'hostname': socket.gethostname(),
        'status': 'critical' if issues else 'warning' if warnings else 'ok',
        'issues': issues,
        'warnings': warnings,
        'uptime_hours': uptime_hours,
        'load_average': [round(v, 2) for v in os.getloadavg()],
        'memory_mb': memory,
        'root_disk_used_percent': disk_used,
        'root_disk_free_mb': free_mb,
        'pending_updates': pending,
        'reboot_required': reboot,
        'details': details,
        'checks': checks,
    }


def main():
    if len(sys.argv) not in (2, 4) or (len(sys.argv) == 4 and sys.argv[2] != '--wait'):
        sys.exit('Usage: sudoctor-health.py config.json [--wait SECONDS]')
    cfg = json.loads(pathlib.Path(sys.argv[1]).read_text())
    timeout = int(sys.argv[3]) if len(sys.argv) == 4 else 0
    deadline = time.monotonic() + timeout
    while True:
        result = collect(cfg)
        if result['status'] != 'critical' or time.monotonic() >= deadline:
            print(json.dumps(result, sort_keys=True))
            break
        time.sleep(min(10, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    main()
