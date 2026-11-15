"""Try notification plugins without a sweep: built-in samples, an events file, or saved reports.

Run it through the scheduler script: sudoctor-scheduled.py notify-test --help
"""
import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

import sudoctor_notify as notify

ITEM_KEYS = {'kind', 'rules', 'severity', 'host', 'check_id', 'since', 'summary', 'details', 'fields'}
SAMPLE_ORDER = ('mixed', 'critical', 'warning', 'unreachable', 'check', 'info', 'reminder', 'resolved', 'long', 'overflow')
SECRET_HEADERS = {'authorization', 'x-api-key', 'x-auth-token'}


class PreviewError(ValueError):
    pass


def make_item(data, now, where='item'):
    """Fill defaults for one notification item and reject unknown keys or values."""
    if not isinstance(data, dict):
        raise PreviewError(f'{where}: must be an object')
    if set(data) - ITEM_KEYS:
        raise PreviewError(f'{where}: unknown keys ' + ', '.join(sorted(set(data) - ITEM_KEYS)))
    kind = data.get('kind', 'firing')
    if kind not in ('firing', 'repeat', 'resolved'):
        raise PreviewError(f'{where}: kind must be firing, repeat or resolved')
    severity = 'ok' if kind == 'resolved' else data.get('severity', 'warning')
    if severity not in notify.SEVERITY:
        raise PreviewError(f'{where}: severity must be info, warning or critical')
    details = data.get('details', [])
    rules = data.get('rules', ['test'])
    if not isinstance(details, list) or not isinstance(rules, list):
        raise PreviewError(f'{where}: details and rules must be lists')
    host = str(data.get('host', 'example'))
    return {
        'kind': kind,
        'rules': [str(rule) for rule in rules],
        'severity': severity,
        'host': host,
        'check_id': data.get('check_id'),
        'since': data.get('since', notify.iso(now)),
        'summary': str(data.get('summary', f'{host} is {severity}')),
        'details': [str(line) for line in details],
        'fields': data.get('fields', {}),
    }


def samples(now):
    """Named example messages; each value is the item list of one message."""
    earlier = notify.iso(now - dt.timedelta(hours=3))
    def make(**data):
        return make_item(data, now)
    critical = make(host='web-1', severity='critical', summary='web-1 is critical', rules=['Production critical'],
                    details=['systemd not active: nginx.service', 'http unhealthy: api', 'root disk warning: used 91.2%'])
    warning = make(host='db-1', summary='db-1 is warning', rules=['Status changed'],
                   details=['Standard OS support: Standard support ended 2026-07-11',
                            'SSH root login: Root can log in with an SSH key. Password and keyboard-interactive login '
                            'are disabled for root. Checked global/default context; Match branches may differ'])
    unreachable = make(host='edge-1', severity='critical', summary='edge-1 is unreachable', rules=['Host unreachable'])
    check = make(host='web-1', check_id='security.certificate.api', summary='web-1: Certificate: api is warning',
                 rules=['Certificate expiring'], details=['Expires in 9.5 days; direct TLS with system trust'])
    info = make(host='app-1', severity='info', summary='app-1 has needed a reboot for 7 days', rules=['Reboot pending'])
    reminder = make(kind='repeat', host='edge-1', severity='critical', summary='edge-1 is unreachable',
                    rules=['Host unreachable'], since=earlier)
    resolved = make(kind='resolved', host='edge-1', summary='edge-1 is ok', rules=['Host unreachable'], since=earlier)
    long = make(host='web-1', severity='critical', summary='web-1 ' + 'has a very long summary line ' * 12,
                details=[f'issue {n}: ' + 'details that keep going ' * 15 for n in range(10)])
    overflow = [make(host=f'host-{n:02}', severity=('critical', 'warning', 'info')[n % 3], summary=f'host-{n:02} needs attention',
                     details=[f'example detail {n}']) for n in range(15)]
    return {
        'mixed': [critical, warning, check, resolved],
        'critical': [critical], 'warning': [warning], 'unreachable': [unreachable], 'check': [check], 'info': [info],
        'reminder': [reminder], 'resolved': [resolved], 'long': [long], 'overflow': overflow,
    }


def events_messages(path, now):
    """Read an events file: one item, a list of items, {"items": [...]}, or a list of such objects."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreviewError(f'Cannot read {path}: {exc}') from exc
    if isinstance(data, list) and data and all(isinstance(entry, dict) and 'items' in entry for entry in data):
        groups = [entry['items'] for entry in data]
    elif isinstance(data, dict) and 'items' in data:
        groups = [data['items']]
    else:
        groups = [data if isinstance(data, list) else [data]]
    messages = []
    for number, group in enumerate(groups, 1):
        if not isinstance(group, list) or not group:
            raise PreviewError(f'{path}: message {number} needs a non-empty items list')
        messages.append([make_item(entry, now, f'{path} message {number} item {index}') for index, entry in enumerate(group, 1)])
    return messages


def report_observations(paths):
    """Turn report files, report directories, or a scheduler state.json into observations."""
    observations = []
    for path in map(Path, paths):
        files = sorted(path.glob('*.json')) if path.is_dir() else [path]
        for file in files:
            try:
                data = json.loads(file.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise PreviewError(f'Cannot read {file}: {exc}') from exc
            if isinstance(data, dict) and isinstance(data.get('hosts'), dict):
                for host, entry in sorted(data['hosts'].items()):
                    outcome = entry.get('attempt', {}).get('outcome')
                    observations.append({'host': host, 'groups': [], 'previous': {}, 'current': entry,
                                         'fresh': outcome in notify.REPORT_OUTCOMES and isinstance(entry.get('report'), dict)})
            elif isinstance(data, dict) and data.get('status') in notify.REPORT_OUTCOMES:
                observations.append({'host': file.stem, 'groups': [], 'previous': {},
                                     'current': {'attempt': {'outcome': data['status']}, 'report': data}, 'fresh': True})
            elif path.is_file():
                raise PreviewError(f'{file} is neither a sudoctor report nor a scheduler state.json')
    if not observations:
        raise PreviewError('No reports found in ' + ', '.join(map(str, paths)))
    return observations


def report_messages(config, target, observations, now, everyone):
    """Items the configured rules would send to target for these reports, ignoring 'for' delays and past state."""
    rules = [dict(rule, for_seconds=0) for rule in config['rules'] if everyone or target in rule['providers']]
    outbox = notify.evaluate({**config, 'rules': rules}, observations, {o['host'] for o in observations}, {}, now)
    merged = []
    for rule, _, entry in outbox:
        twin = next((m for m in merged if (m['host'], m['check_id']) == (entry['host'], entry['check_id'])), None)
        if twin:
            twin['rules'] = twin['rules'] + [rule['name']]
            twin['severity'] = max(twin['severity'], entry['severity'], key=notify.SEVERITY.get)
        else:
            merged.append(dict(entry))
    return [merged] if merged else []


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return b''


def show_request(request, timeout=None, out=None):
    """Print a request instead of sending it. Secrets in URL paths, auth headers and ntfy topics are hidden."""
    out = out or sys.stdout
    url = urllib.parse.urlsplit(request.full_url)
    print(f'--- {request.get_method()} {url.scheme}://{url.netloc}/… (path hidden)', file=out)
    for key, value in request.header_items():
        print(f'{key}: {"<hidden>" if key.lower() in SECRET_HEADERS else value}', file=out)
    body = request.data or b''
    try:
        data = json.loads(body)
        if isinstance(data, dict) and 'topic' in data:
            data['topic'] = '<hidden>'  # an ntfy topic name works like a password
        text = json.dumps(data, indent=2, ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        text = body.decode('utf-8', 'replace')
    print('\n' + text + '\n', file=out, flush=True)
    return FakeResponse()


@contextlib.contextmanager
def dry_run(enabled):
    if not enabled:
        yield
        return
    original = urllib.request.urlopen
    urllib.request.urlopen = show_request
    try:
        yield
    finally:
        urllib.request.urlopen = original


def parse_options(pairs):
    options = {}
    for pair in pairs:
        key, sep, value = pair.partition('=')
        if not sep or not key:
            raise PreviewError(f'--option needs KEY=VALUE, got {pair!r}')
        try:
            options[key] = json.loads(value)
        except json.JSONDecodeError:
            options[key] = value
    return options


def parser():
    p = argparse.ArgumentParser(
        prog='sudoctor-scheduled.py notify-test',
        description='Send sample or recorded notifications to one provider without running a sweep. '
                    'Without a source, sends the "mixed" sample.')
    p.add_argument('provider', nargs='?', help='provider name from notify.json')
    p.add_argument('--plugin', help='use this plugin directly instead of a configured provider')
    p.add_argument('--option', action='append', default=[], metavar='KEY=VALUE',
                   help='plugin option for --plugin; JSON values allowed; KEY_file reads a file')
    p.add_argument('--sample', action='append', default=[], metavar='NAME',
                   help='built-in sample to send; repeat for several, or use "all" (see --list-samples)')
    p.add_argument('--events', action='append', default=[], metavar='FILE', help='JSON file with items to send')
    p.add_argument('--reports', nargs='+', metavar='PATH',
                   help='report files, a report directory, or a scheduler state.json, evaluated with your rules')
    p.add_argument('--all-rules', action='store_true', help='with --reports, use every rule, not only rules for this provider')
    p.add_argument('--dry-run', action='store_true', help='print the HTTP requests instead of sending them')
    p.add_argument('--list-samples', action='store_true', help='list sample names and exit')
    return p


def main(argv, env=None, log=print):
    env = env if env is not None else notify.os.environ
    args = parser().parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    if args.list_samples:
        for name, items in samples(now).items():
            log(f'{name:12} {len(items)} item(s): ' + '; '.join(i['summary'][:40] for i in items[:3]))
        return 0
    try:
        if bool(args.provider) == bool(args.plugin):
            raise PreviewError('Give a provider name from notify.json, or --plugin NAME')
        if args.option and not args.plugin:
            raise PreviewError('--option only applies with --plugin; configured providers use notify.json options')
        plugins = notify.load_plugins(notify.plugins_dir(env))
        config = None
        if args.provider or args.reports:
            config = notify.load_config(notify.config_path(env), plugins)
        if args.plugin:
            target = args.plugin
            provider = notify.make_provider(target, args.plugin, parse_options(args.option), plugins)
        else:
            target = args.provider
            if target not in config['providers']:
                raise PreviewError(f'Unknown provider {target!r}; configured: {", ".join(sorted(config["providers"]))}')
            provider = config['providers'][target]
        catalog = samples(now)
        names = list(SAMPLE_ORDER) if 'all' in args.sample else args.sample
        if not (names or args.events or args.reports):
            names = ['mixed']
        unknown = [name for name in names if name not in catalog]
        if unknown:
            raise PreviewError(f'Unknown sample {", ".join(unknown)}; choose from {", ".join(SAMPLE_ORDER)} or all')
        messages = [(f'sample {name}', catalog[name]) for name in names]
        for path in args.events:
            messages += [(f'{path} #{n}', items) for n, items in enumerate(events_messages(path, now), 1)]
        if args.reports:
            found = report_messages(config, target, report_observations(args.reports), now, args.plugin or args.all_rules)
            if not found:
                log('No rule matched these reports' + ('' if args.plugin or args.all_rules else f' for provider {target}'))
            messages += [('reports', items) for items in found]
    except (PreviewError, notify.ConfigError) as exc:
        log(str(exc), file=sys.stderr)
        return 2
    plugin, module, options = provider
    url = env.get('SUDOCTOR_PUBLIC_URL') or None
    failures = 0
    with dry_run(args.dry_run):
        for number, (label, items) in enumerate(messages):
            if number and not args.dry_run:
                time.sleep(1)  # Stay under chat services' webhook rate limits.
            try:
                ok = module.send(notify.build_message(items, now, url), dict(options)) is not False
                error = '' if ok else 'plugin returned False'
            except Exception as exc:
                ok, error = False, f'{type(exc).__name__}: {exc}'
            failures += not ok
            log(f'{label}: {target} ({plugin}) ' + (('printed (dry run)' if args.dry_run else 'sent') if ok else 'failed: ' + error),
                **({} if ok else {'file': sys.stderr}))
    return 1 if failures else 0
