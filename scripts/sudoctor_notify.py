"""Notification rules for scheduled checks.

A plugin knows how to send, a provider is a named and configured plugin
instance, and a rule decides which providers hear about a host or check.
See docs/notifications.md and schemas/notify.schema.json.
"""
import datetime as dt
import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import re
import string
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BUILTIN_PLUGINS = Path(__file__).resolve().parent / 'notify-plugins'
REPORT_OUTCOMES = ('ok', 'warning', 'critical')
OUTCOME_TEXT = {'unreachable': 'is unreachable', 'timeout': 'check timed out', 'check_error': 'check failed'}
SEVERITY = {'ok': 0, 'info': 1, 'warning': 2, 'critical': 3}
UNKNOWN = object()  # No fresh data: the rule keeps its previous state.

HOST_FIELDS = (
    'host', 'groups', 'outcome', 'previous_outcome', 'outcome_changed',
    'status', 'previous_status', 'status_changed', 'issues', 'warnings',
    'root_disk_used_percent', 'root_disk_free_mb', 'memory_available_mb', 'load_1m',
    'uptime_hours', 'pending_updates', 'reboot_required', 'report_age_seconds')
CHECK_FIELDS = (
    'check_id', 'check_category', 'check_label', 'check_status', 'check_previous_status',
    'check_status_changed', 'check_message', 'check_value', 'check_days_left')
OPERATORS = ('=', '!=', '>', '>=', '<', '<=', 'contains', 'not_contains', 'matches', 'in', 'not_in')
RULE_KEYS = {'name', 'scope', 'match', 'conditions', 'providers', 'for', 'repeat',
             'notify_recovery', 'severity', 'message', 'stop_on_match'}


class ConfigError(ValueError):
    pass


def config_path(env=os.environ):
    if env.get('SUDOCTOR_NOTIFY_CONFIG'):
        return Path(env['SUDOCTOR_NOTIFY_CONFIG']).expanduser()
    return Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'sudoctor/notify.json'


def plugins_dir(env=os.environ):
    return Path(env.get('SUDOCTOR_PLUGINS_DIR') or ROOT / 'plugins').expanduser()


def iso(moment):
    return moment.isoformat()


def parse_time(text):
    try:
        moment = dt.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def parse_duration(text):
    match = re.fullmatch(r'([0-9]+)([smhd])', str(text))
    if not match:
        raise ConfigError(f'Invalid duration {text!r}; use a number followed by s, m, h or d')
    return int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]


# Plugins -------------------------------------------------------------------

def load_plugins(user_dir):
    """Load built-in plugins, then *.py files from user_dir. A file's name is its plugin name."""
    plugins = {}
    for directory in (BUILTIN_PLUGINS, Path(user_dir)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob('*.py')):
            name = path.stem
            if name.startswith('_'):
                continue
            if not re.fullmatch(r'[a-z][a-z0-9_-]*', name):
                raise ConfigError(f'Plugin file {path} must be named with lowercase letters, digits, - or _')
            if name in plugins:
                raise ConfigError(f'Plugin {path} has the same name as a built-in plugin; rename the file')
            spec = importlib.util.spec_from_file_location('sudoctor_plugin_' + name.replace('-', '_'), path)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                raise ConfigError(f'Plugin {path} failed to load: {type(exc).__name__}: {exc}') from exc
            if not callable(getattr(module, 'send', None)):
                raise ConfigError(f'Plugin {path} has no send(message, options) function')
            plugins[name] = module
    return plugins


def post(url, data, headers, timeout=10):
    """POST bytes; raise on network errors and HTTP status >= 400. For use by plugins."""
    if not re.match(r'https?://', str(url)):
        raise ValueError('URL must start with http:// or https://')
    request = urllib.request.Request(url, data=data, method='POST', headers={'User-Agent': 'sudoctor', **headers})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read(65536)


def post_json(url, body, headers=None, timeout=10):
    post(url, json.dumps(body).encode(), {'Content-Type': 'application/json', **(headers or {})}, timeout)


# Configuration -------------------------------------------------------------

def resolve_options(name, options):
    """Read *_file options from disk so secrets stay out of the configuration file."""
    resolved = {}
    for key, value in options.items():
        if key.endswith('_file'):
            target = key[:-5]
            if target in options:
                raise ConfigError(f'Provider {name}: set either {target} or {key}, not both')
            try:
                resolved[target] = Path(value).expanduser().read_text().strip()
            except (OSError, TypeError) as exc:
                raise ConfigError(f'Provider {name}: cannot read {key}: {exc}') from exc
        else:
            resolved[key] = value
    return resolved


def validate_condition(where, condition, scope):
    if not isinstance(condition, dict) or set(condition) != {'field', 'op', 'value'}:
        return [f'{where}: a condition needs exactly field, op and value']
    errors = []
    field, op, value = condition['field'], condition['op'], condition['value']
    allowed = HOST_FIELDS + (CHECK_FIELDS if scope == 'check' else ())
    if field not in allowed:
        errors.append(f'{where}: unknown field {field!r}' + (' (check_* fields need scope check)' if field in CHECK_FIELDS else ''))
    if op not in OPERATORS:
        errors.append(f'{where}: unknown operator {op!r}')
    elif op in ('in', 'not_in') and not (isinstance(value, list) and value):
        errors.append(f'{where}: {op} needs a non-empty list')
    elif op in ('>', '>=', '<', '<=') and (isinstance(value, bool) or not isinstance(value, (int, float))):
        errors.append(f'{where}: {op} needs a number')
    elif op in ('contains', 'not_contains', 'matches') and not isinstance(value, str):
        errors.append(f'{where}: {op} needs a string')
    elif op in ('=', '!=') and isinstance(value, (list, dict)):
        errors.append(f'{where}: {op} needs a single value; use in or not_in for lists')
    return errors


def make_provider(name, plugin, options, plugins):
    """Return (plugin name, module, resolved options) or raise ConfigError."""
    where = f'provider {name}'
    module = plugins.get(plugin)
    if module is None:
        raise ConfigError(f'{where}: unknown plugin {plugin!r}; available: {", ".join(sorted(plugins))}')
    if not isinstance(options, dict):
        raise ConfigError(f'{where}: options must be an object')
    options = resolve_options(name, options)
    errors = [f'{where}: plugin {plugin} requires option {key} (or {key}_file)'
              for key in getattr(module, 'REQUIRED_OPTIONS', ()) if not options.get(key)]
    validate = getattr(module, 'validate_options', None)
    if callable(validate) and not errors:
        errors.extend(f'{where}: {message}' for message in validate(options) or ())
    if errors:
        raise ConfigError('; '.join(errors))
    return plugin, module, options


def load_config(path, plugins):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f'Cannot read {path}: {exc}') from exc
    errors = []
    if not isinstance(data, dict) or not isinstance(data.get('providers'), dict) or not isinstance(data.get('rules'), list):
        raise ConfigError(f'{path}: needs a providers object and a rules list')
    if set(data) - {'$schema', 'providers', 'rules'}:
        errors.append('unknown top-level keys: ' + ', '.join(sorted(set(data) - {'$schema', 'providers', 'rules'})))
    providers = {}
    for name, provider in data['providers'].items():
        where = f'provider {name}'
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            errors.append(f'{where}: names may contain letters, digits, dots, underscores or hyphens')
        if not isinstance(provider, dict) or set(provider) - {'plugin', 'options'} or 'plugin' not in provider:
            errors.append(f'{where}: needs plugin and optional options only')
            continue
        try:
            providers[name] = make_provider(name, provider['plugin'], provider.get('options', {}), plugins)
        except ConfigError as exc:
            errors.append(str(exc))
    rules, names = [], set()
    for index, rule in enumerate(data['rules']):
        where = f'rule {index + 1}'
        if not isinstance(rule, dict):
            errors.append(f'{where}: must be an object')
            continue
        if isinstance(rule.get('name'), str) and rule['name']:
            where = f'rule {rule["name"]!r}'
            if rule['name'] in names:
                errors.append(f'{where}: names must be unique')
            names.add(rule['name'])
        else:
            errors.append(f'{where}: needs a name')
        if set(rule) - RULE_KEYS:
            errors.append(f'{where}: unknown keys ' + ', '.join(sorted(set(rule) - RULE_KEYS)))
        scope = rule.get('scope', 'host')
        if scope not in ('host', 'check'):
            errors.append(f'{where}: scope must be host or check')
        if rule.get('match', 'all') not in ('all', 'any'):
            errors.append(f'{where}: match must be all or any')
        if not isinstance(rule.get('conditions'), list):
            errors.append(f'{where}: conditions must be a list')
        else:
            for number, condition in enumerate(rule['conditions']):
                errors.extend(validate_condition(f'{where} condition {number + 1}', condition, scope))
        targets = rule.get('providers')
        if not isinstance(targets, list) or not targets:
            errors.append(f'{where}: providers must be a non-empty list')
            targets = []
        for target in targets:
            if target not in data['providers']:
                errors.append(f'{where}: unknown provider {target!r}')
        for key in ('notify_recovery', 'stop_on_match'):
            if not isinstance(rule.get(key, False), bool):
                errors.append(f'{where}: {key} must be true or false')
        if rule.get('severity', 'info') not in ('info', 'warning', 'critical'):
            errors.append(f'{where}: severity must be info, warning or critical')
        if not isinstance(rule.get('message', ''), str):
            errors.append(f'{where}: message must be a string')
        timing = {}
        for key in ('for', 'repeat'):
            try:
                timing[key] = parse_duration(rule[key]) if key in rule else None
            except ConfigError as exc:
                errors.append(f'{where}: {exc}')
        rules.append({**rule, 'scope': scope, 'match': rule.get('match', 'all'),
                      'for_seconds': timing.get('for') or 0, 'repeat_seconds': timing.get('repeat')})
    if errors:
        raise ConfigError(f'{path}: ' + '; '.join(errors))
    return {'providers': providers, 'rules': rules}


# Fields and conditions -----------------------------------------------------

def host_fields(observation, now):
    previous, current = observation['previous'], observation['current']
    outcome = current.get('attempt', {}).get('outcome')
    previous_outcome = previous.get('attempt', {}).get('outcome')
    last = current.get('report')
    reported = parse_time(last.get('timestamp_utc')) if isinstance(last, dict) else None
    fields = {
        'host': observation['host'],
        'groups': list(observation.get('groups', [])),
        'outcome': outcome,
        'previous_outcome': previous_outcome,
        'outcome_changed': previous_outcome is not None and previous_outcome != outcome,
        'report_age_seconds': round((now - reported).total_seconds()) if reported else None,
    }
    report = last if observation['fresh'] and isinstance(last, dict) else None
    if report is None:
        fields.update(dict.fromkeys(HOST_FIELDS[5:-1], UNKNOWN))
        return fields
    previous_status = (previous.get('report') or {}).get('status')
    memory = report.get('memory_mb') or {}
    load = report.get('load_average') or [None]
    fields.update({
        'status': report.get('status'),
        'previous_status': previous_status,
        'status_changed': previous_status is not None and previous_status != report.get('status'),
        'issues': list(report.get('issues') or []),
        'warnings': list(report.get('warnings') or []),
        'root_disk_used_percent': report.get('root_disk_used_percent'),
        'root_disk_free_mb': report.get('root_disk_free_mb'),
        'memory_available_mb': memory.get('MemAvailable'),
        'load_1m': load[0],
        'uptime_hours': report.get('uptime_hours'),
        'pending_updates': report.get('pending_updates'),
        'reboot_required': report.get('reboot_required'),
    })
    return fields


def days_left(value, now):
    if not isinstance(value, str):
        return None
    moment = parse_time(value)
    return round((moment - now).total_seconds() / 86400, 1) if moment else None


def subjects(observation, now):
    """Yield (scope, check_id, fields) for the host and, with a fresh report, each check."""
    fields = host_fields(observation, now)
    yield 'host', None, fields
    report = observation['current'].get('report')
    if not observation['fresh'] or not isinstance(report, dict):
        return
    previous = {c.get('id'): c.get('status') for c in (observation['previous'].get('report') or {}).get('checks') or []
                if isinstance(c, dict)}
    for check in report.get('checks') or []:
        if not isinstance(check, dict) or not isinstance(check.get('id'), str):
            continue
        before = previous.get(check['id'])
        yield 'check', check['id'], {
            **fields,
            'check_id': check['id'],
            'check_category': check.get('category'),
            'check_label': check.get('label'),
            'check_status': check.get('status'),
            'check_previous_status': before,
            'check_status_changed': before is not None and before != check.get('status'),
            'check_message': check.get('message'),
            'check_value': check.get('value'),
            'check_days_left': days_left(check.get('value'), now),
        }


def test_condition(condition, fields):
    got, op, want = fields.get(condition['field']), condition['op'], condition['value']
    if got is UNKNOWN:
        return UNKNOWN
    items = got if isinstance(got, list) else [got]
    if op in ('=', '!='):
        same = got == want and isinstance(got, bool) == isinstance(want, bool)
        return same if op == '=' else not same
    if op in ('>', '>=', '<', '<='):
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return False
        return {'>': got > want, '>=': got >= want, '<': got < want, '<=': got <= want}[op]
    if op in ('contains', 'not_contains'):
        found = any(isinstance(item, str) and want in item for item in items)
        return found if op == 'contains' else not found
    if op == 'matches':
        return any(isinstance(item, str) and fnmatch.fnmatchcase(item, want) for item in items)
    found = any(not isinstance(item, (list, dict)) and item in want for item in items)
    return found if op == 'in' else not found


def rule_matches(rule, fields):
    results = [test_condition(condition, fields) for condition in rule['conditions']]
    if any(result is UNKNOWN for result in results):
        return UNKNOWN
    return all(results) if rule['match'] == 'all' else any(results) or not results


def plain(value):
    if value is UNKNOWN:
        return None
    if isinstance(value, (list, dict, str, int, float, bool)) or value is None:
        return value
    return str(value)


def summary(rule, fields, scope):
    if rule.get('message'):
        values = {key: ', '.join(map(str, value)) if isinstance(value, list) else '' if value in (None, UNKNOWN) else str(value)
                  for key, value in fields.items()}
        return string.Template(rule['message']).safe_substitute(values, rule=rule['name'])
    if scope == 'check':
        return f'{fields["host"]}: {fields["check_label"] or fields["check_id"]} is {fields["check_status"]}'
    if fields['outcome'] not in REPORT_OUTCOMES:
        return f'{fields["host"]} {OUTCOME_TEXT.get(fields["outcome"], "check " + str(fields["outcome"]))}'
    return f'{fields["host"]} is {fields["status"]}'


def details(fields, scope, kind):
    """Supporting lines shown under the summary: the check message, or the host's issues and warnings."""
    if kind == 'resolved':
        return []
    if scope == 'check':
        return [fields['check_message']] if fields.get('check_message') else []
    if fields['outcome'] not in REPORT_OUTCOMES:
        return []
    return [str(line) for line in (fields['issues'] or []) + (fields['warnings'] or [])][:10]


def severity(rule, fields, scope):
    if rule.get('severity'):
        return rule['severity']
    status = fields['check_status'] if scope == 'check' else fields['status']
    if scope == 'host' and fields['outcome'] not in REPORT_OUTCOMES:
        return 'critical'
    return status if status in ('warning', 'critical') else 'info'


# Evaluation and dispatch ---------------------------------------------------

def evaluate(config, observations, hosts, alerts, now):
    """Update alert state in place and return notifications to send: (rule, key, item)."""
    outbox, seen = [], set()
    fresh = {o['host'] for o in observations if o['fresh']}
    for observation in observations:
        for scope, check, fields in subjects(observation, now):
            stopped = False
            for rule in config['rules']:
                if rule['scope'] != scope:
                    continue
                key = '|'.join((rule['name'], observation['host'], check or ''))
                seen.add(key)
                matched = rule_matches(rule, fields)
                if matched is UNKNOWN:
                    continue
                entry = alerts.get(key)
                if matched:
                    if entry is None:
                        entry = alerts[key] = {'rule': rule['name'], 'host': observation['host'], 'check': check,
                                               'since': iso(now), 'notified_at': None}
                    notified = parse_time(entry['notified_at']) if entry.get('notified_at') else None
                    if notified is None:
                        due = (now - parse_time(entry['since'])).total_seconds() >= rule['for_seconds']
                    else:
                        due = rule['repeat_seconds'] is not None and (now - notified).total_seconds() >= rule['repeat_seconds']
                    if due and stopped:
                        entry['notified_at'] = iso(now)
                    elif due:
                        kind = 'firing' if notified is None else 'repeat'
                        outbox.append((rule, key, item(rule, fields, scope, check, kind, entry)))
                    stopped = stopped or rule.get('stop_on_match', False)
                elif entry is not None:
                    if entry.get('notified_at') and rule.get('notify_recovery') and not stopped:
                        outbox.append((rule, key, item(rule, fields, scope, check, 'resolved', entry)))
                    del alerts[key]
    rules = {rule['name'] for rule in config['rules']}
    for key, entry in list(alerts.items()):
        # Drop alerts for removed rules or hosts, and checks missing from a fresh report.
        if entry.get('rule') not in rules or entry.get('host') not in hosts or (key not in seen and entry.get('host') in fresh):
            del alerts[key]
    return outbox


def item(rule, fields, scope, check, kind, entry):
    return {
        'kind': kind,
        'rules': [rule['name']],
        'severity': 'ok' if kind == 'resolved' else severity(rule, fields, scope),
        'host': fields['host'],
        'check_id': check,
        'since': entry['since'],
        'summary': summary(rule, fields, scope),
        'details': details(fields, scope, kind),
        'fields': {key: plain(value) for key, value in fields.items()},
    }


def state_label(entry):
    if entry['kind'] == 'resolved':
        return 'resolved'
    return entry['severity'] + (' reminder' if entry['kind'] == 'repeat' else '')


def build_message(items, now, dashboard_url=None):
    active = [i for i in items if i['kind'] != 'resolved']
    level = max((i['severity'] for i in active), key=SEVERITY.get, default='ok')
    if len(items) == 1:
        title = f'sudoctor {state_label(items[0])}: {items[0]["host"]}'
    else:
        counts = {}
        for entry in items:
            label = 'resolved' if entry['kind'] == 'resolved' else entry['severity']
            counts[label] = counts.get(label, 0) + 1
        order = ('critical', 'warning', 'info', 'resolved')
        title = 'sudoctor: ' + ', '.join(f'{counts[label]} {label}' for label in order if label in counts)
    lines = []
    for entry in items:
        lines.append(f'[{state_label(entry)}] {entry["summary"]}')
        lines.extend('  - ' + line for line in entry.get('details', []))
    if dashboard_url:
        lines.append(dashboard_url)
    return {'title': title, 'text': '\n'.join(lines), 'severity': level, 'timestamp_utc': iso(now),
            'dashboard_url': dashboard_url, 'items': items}


def dispatch(config, outbox, now, dashboard_url=None, log=print):
    """Send one message per provider and return the alert keys that at least one provider accepted."""
    queues = {}
    for rule, key, entry in outbox:
        for provider in rule['providers']:
            queue = queues.setdefault(provider, [])
            twin = next((q for q in queue if (q[1]['host'], q[1]['check_id'], q[1]['kind']) ==
                         (entry['host'], entry['check_id'], entry['kind'])), None)
            if twin:
                twin[0].add(key)
                twin[1]['rules'] = twin[1]['rules'] + [rule['name']]
                twin[1]['severity'] = max(twin[1]['severity'], entry['severity'], key=SEVERITY.get)
            else:
                queue.append(({key}, dict(entry)))
    delivered = set()
    for provider, queue in queues.items():
        plugin, module, options = config['providers'][provider]
        try:
            sent = module.send(build_message([q[1] for q in queue], now, dashboard_url), dict(options)) is not False
        except Exception as exc:  # A broken provider must not stop the others.
            log(f'notify: {provider} ({plugin}) failed: {type(exc).__name__}: {exc}', file=sys.stderr)
            sent = False
        if sent:
            log(f'notify: {provider} sent {len(queue)} notification(s)')
            for keys, _ in queue:
                delivered.update(keys)
    return delivered


def run(observations, hosts, alerts, now=None, env=os.environ, log=print):
    """Evaluate rules for one sweep. Return False when no configuration file exists."""
    path = config_path(env)
    if not path.is_file():
        return False
    config = load_config(path, load_plugins(plugins_dir(env)))
    now = now or dt.datetime.now(dt.timezone.utc)
    outbox = evaluate(config, observations, set(hosts), alerts, now)
    delivered = dispatch(config, outbox, now, env.get('SUDOCTOR_PUBLIC_URL') or None, log)
    for _, key, entry in outbox:
        if key in delivered and entry['kind'] != 'resolved' and key in alerts:
            alerts[key]['notified_at'] = iso(now)
    return True

