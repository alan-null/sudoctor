import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import sudoctor_notify as notify  # noqa: E402

spec = importlib.util.spec_from_file_location('scheduled_notify', ROOT / 'scripts/sudoctor-scheduled.py')
scheduled = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduled)

T0 = dt.datetime(2026, 11, 15, 8, 0, tzinfo=dt.timezone.utc)
RECORDER = '''
SENT = []
REQUIRED_OPTIONS = ('target',)
def send(message, options):
    SENT.append((options['target'], message))
    return options.get('fail') is not True
'''


def report(status='ok', checks=(), **extra):
    return {'status': status, 'timestamp_utc': T0.isoformat(), 'issues': [], 'warnings': [], 'checks': list(checks), **extra}


def observation(host, outcome, current_report=None, previous=None, groups=()):
    current = {'attempt': {'outcome': outcome}}
    if current_report is not None:
        current['report'] = current_report
    elif previous and previous.get('report'):
        current['report'] = previous['report']
    return {'host': host, 'groups': list(groups), 'previous': previous or {}, 'current': current,
            'fresh': outcome in ('ok', 'warning', 'critical')}


class NotifyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.plugins_dir = self.dir / 'plugins'
        self.plugins_dir.mkdir()
        (self.plugins_dir / 'recorder.py').write_text(RECORDER)

    def config(self, rules, providers=None):
        path = self.dir / 'notify.json'
        path.write_text(json.dumps({'providers': providers or {
            'a': {'plugin': 'recorder', 'options': {'target': 'a'}},
            'b': {'plugin': 'recorder', 'options': {'target': 'b'}}}, 'rules': rules}))
        self.plugins = notify.load_plugins(self.plugins_dir)
        return notify.load_config(path, self.plugins)

    def sweep(self, config, observations, alerts, now, hosts=None):
        outbox = notify.evaluate(config, observations, set(hosts or [o['host'] for o in observations]), alerts, now)
        delivered = notify.dispatch(config, outbox, now, log=lambda *a, **k: None)
        for _, key, item in outbox:
            if key in delivered and item['kind'] != 'resolved' and key in alerts:
                alerts[key]['notified_at'] = notify.iso(now)
        return [(target, [(i['kind'], i['host'], i['check_id']) for i in message['items']])
                for target, message in self.plugins['recorder'].SENT]

    def test_example_configuration_loads(self):
        example = json.loads((ROOT / 'docker/notify.example.json').read_text())
        for provider in example['providers'].values():
            for key, value in list(provider.get('options', {}).items()):
                if key.endswith('_file'):
                    secret = self.dir / Path(value).name
                    secret.write_text('https://example.test/secret\n')
                    provider['options'][key] = str(secret)
        path = self.dir / 'example.json'
        path.write_text(json.dumps(example))
        config = notify.load_config(path, notify.load_plugins(self.plugins_dir))
        self.assertEqual(config['providers']['phone'][2]['token'], 'https://example.test/secret')
        self.assertEqual(len(config['rules']), len(example['rules']))

    def test_transition_repeat_and_recovery(self):
        config = self.config([{'name': 'critical', 'conditions': [{'field': 'status', 'op': '=', 'value': 'critical'}],
                               'repeat': '1h', 'notify_recovery': True, 'providers': ['a']}])
        alerts, sent = {}, self.plugins['recorder'].SENT
        bad = report('critical', issues=['systemd not active: nginx.service'])
        self.assertEqual(self.sweep(config, [observation('web', 'critical', bad)], alerts, T0), [('a', [('firing', 'web', None)])])
        self.assertEqual(sent[0][1]['text'], '[critical] web is critical\n  - systemd not active: nginx.service')
        self.assertEqual(sent[0][1]['title'], 'sudoctor critical: web')
        self.assertEqual(sent[0][1]['severity'], 'critical')
        sent.clear()
        self.sweep(config, [observation('web', 'critical', bad, {'report': bad})], alerts, T0 + dt.timedelta(minutes=15))
        self.assertEqual(sent, [])
        self.assertEqual(self.sweep(config, [observation('web', 'critical', bad, {'report': bad})], alerts, T0 + dt.timedelta(hours=1)),
                         [('a', [('repeat', 'web', None)])])
        sent.clear()
        # An unreachable host has no current status: the alert neither fires nor resolves.
        self.sweep(config, [observation('web', 'unreachable', previous={'report': bad})], alerts, T0 + dt.timedelta(hours=1, minutes=15))
        self.assertEqual(sent, [])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(self.sweep(config, [observation('web', 'ok', report(), {'report': bad})], alerts, T0 + dt.timedelta(hours=2)),
                         [('a', [('resolved', 'web', None)])])
        self.assertEqual(sent[0][1]['severity'], 'ok')
        self.assertEqual(alerts, {})

    def test_for_delay_and_retry_after_failure(self):
        config = self.config([{'name': 'down', 'for': '30m', 'providers': ['a'],
                               'conditions': [{'field': 'outcome', 'op': 'in', 'value': ['unreachable', 'timeout']}]}],
                             {'a': {'plugin': 'recorder', 'options': {'target': 'a', 'fail': True}}})
        alerts, sent = {}, self.plugins['recorder'].SENT
        self.sweep(config, [observation('db', 'unreachable')], alerts, T0)
        self.assertEqual(sent, [])
        self.sweep(config, [observation('db', 'timeout')], alerts, T0 + dt.timedelta(minutes=30))
        self.sweep(config, [observation('db', 'timeout')], alerts, T0 + dt.timedelta(minutes=45))
        self.assertEqual([i[1]['items'][0]['kind'] for i in sent], ['firing', 'firing'])
        self.assertIsNone(alerts['down|db|']['notified_at'])
        self.assertEqual(sent[0][1]['items'][0]['summary'], 'db check timed out')

    def test_changes_check_scope_templates_and_merging(self):
        config = self.config([
            {'name': 'changed', 'conditions': [{'field': 'status_changed', 'op': '=', 'value': True}], 'providers': ['a', 'b']},
            {'name': 'prod', 'conditions': [{'field': 'groups', 'op': 'in', 'value': ['prod']}, {'field': 'status', 'op': '!=', 'value': 'ok'}],
             'providers': ['a']},
            {'name': 'cert', 'scope': 'check', 'providers': ['b'], 'message': '$host $check_label in $check_days_left days',
             'conditions': [{'field': 'check_id', 'op': 'matches', 'value': 'security.certificate.*'},
                            {'field': 'check_days_left', 'op': '<=', 'value': 14}]}])
        cert = {'id': 'security.certificate.api', 'label': 'Certificate: api', 'status': 'warning',
                'value': (T0 + dt.timedelta(days=10)).isoformat(), 'message': 'Expires soon'}
        other = {'id': 'os_updates.support', 'status': 'ok', 'value': '2030-01-01'}
        sent = self.sweep(config, [observation('web', 'warning', report('warning', [cert, other]), {'report': report()}, ['prod', 'servers'])],
                          {}, T0)
        self.assertEqual(sent, [('a', [('firing', 'web', None)]), ('b', [('firing', 'web', None), ('firing', 'web', 'security.certificate.api')])])
        messages = {target: message for target, message in self.plugins['recorder'].SENT}
        self.assertEqual(messages['a']['items'][0]['rules'], ['changed', 'prod'])
        self.assertEqual(messages['b']['items'][1]['summary'], 'web Certificate: api in 10.0 days')
        self.assertEqual(messages['b']['title'], 'sudoctor: 2 warning')

    def test_first_report_is_not_a_change_and_stop_on_match(self):
        config = self.config([
            {'name': 'first', 'stop_on_match': True, 'conditions': [{'field': 'host', 'op': '=', 'value': 'web'}], 'providers': ['a']},
            {'name': 'second', 'conditions': [], 'providers': ['b']},
            {'name': 'changed', 'conditions': [{'field': 'status_changed', 'op': '=', 'value': True}], 'providers': ['b']}])
        alerts = {}
        sent = self.sweep(config, [observation('web', 'ok', report()), observation('db', 'ok', report())], alerts, T0)
        self.assertEqual(sent, [('a', [('firing', 'web', None)]), ('b', [('firing', 'db', None)])])
        self.assertIsNotNone(alerts['second|web|']['notified_at'])

    def test_stale_alerts_are_pruned(self):
        config = self.config([{'name': 'any', 'scope': 'check', 'conditions': [], 'providers': ['a']}])
        alerts = {}
        checks = [{'id': 'x', 'status': 'ok'}, {'id': 'y', 'status': 'ok'}]
        self.sweep(config, [observation('web', 'ok', report(checks=checks)), observation('db', 'ok', report(checks=checks))], alerts, T0)
        self.assertEqual(len(alerts), 4)
        # db is unreachable (keeps alerts), web lost check y, and gone was removed from the inventory.
        alerts['gone|old|'] = {'rule': 'any', 'host': 'old', 'check': None, 'since': T0.isoformat(), 'notified_at': None}
        self.sweep(config, [observation('web', 'ok', report(checks=checks[:1])), observation('db', 'unreachable', previous={'report': report()})],
                   alerts, T0, hosts=['web', 'db'])
        self.assertEqual(sorted(alerts), ['any|db|x', 'any|db|y', 'any|web|x'])

    def test_operators(self):
        fields = {'n': 5, 'flag': True, 'text': 'disk full', 'items': ['a-1', 'b-2'], 'none': None}
        cases = [('n', '>=', 5, True), ('n', '<', 5, False), ('none', '>', 1, False), ('flag', '=', 1, False),
                 ('flag', '=', True, True), ('text', 'contains', 'full', True), ('text', 'not_contains', 'full', False),
                 ('items', 'contains', '-2', True), ('items', 'matches', 'a-*', True), ('items', 'in', ['b-2'], True),
                 ('items', 'not_in', ['c'], True), ('n', 'in', [1, 5], True), ('none', '=', None, True)]
        for field, op, value, expected in cases:
            with self.subTest(field=field, op=op, value=value):
                self.assertIs(notify.test_condition({'field': field, 'op': op, 'value': value}, fields), expected)
        self.assertIs(notify.test_condition({'field': 'n', 'op': '=', 'value': 1}, {'n': notify.UNKNOWN}), notify.UNKNOWN)

    def test_configuration_errors(self):
        secret = self.dir / 'secret'
        secret.write_text('x')
        cases = {
            'unknown plugin': ({'p': {'plugin': 'nope'}}, []),
            'requires option': ({'p': {'plugin': 'ntfy'}}, []),
            'url must start': ({'p': {'plugin': 'webhook', 'options': {'url': 'ftp://x'}}}, []),
            'not both': ({'p': {'plugin': 'webhook', 'options': {'url': 'https://x', 'url_file': str(secret)}}}, []),
            'cannot read': ({'p': {'plugin': 'webhook', 'options': {'url_file': str(self.dir / 'missing')}}}, []),
            'unknown provider': ({}, [{'name': 'r', 'conditions': [], 'providers': ['p']}]),
            'scope check': ({}, [{'name': 'r', 'conditions': [{'field': 'check_id', 'op': '=', 'value': 'x'}], 'providers': ['a']}]),
            'needs a number': ({}, [{'name': 'r', 'conditions': [{'field': 'load_1m', 'op': '>', 'value': '1'}], 'providers': ['a']}]),
            'Invalid duration': ({}, [{'name': 'r', 'for': '5 min', 'conditions': [], 'providers': ['a']}]),
            'unique': ({}, [{'name': 'r', 'conditions': [], 'providers': ['a']}] * 2),
            'unknown keys': ({}, [{'name': 'r', 'conditions': [], 'providers': ['a'], 'when': {}}]),
        }
        for expected, (providers, rules) in cases.items():
            with self.subTest(expected):
                with self.assertRaisesRegex(notify.ConfigError, expected):
                    self.config(rules, {'a': {'plugin': 'debug'}, **providers})

    def test_plugin_loading(self):
        self.assertTrue({'webhook', 'ntfy', 'discord', 'slack', 'debug', 'recorder'} <= set(notify.load_plugins(self.plugins_dir)))
        (self.plugins_dir / '_helper.py').write_text('raise RuntimeError')
        notify.load_plugins(self.plugins_dir)
        (self.plugins_dir / 'ntfy.py').write_text(RECORDER)
        with self.assertRaisesRegex(notify.ConfigError, 'built-in'):
            notify.load_plugins(self.plugins_dir)
        (self.plugins_dir / 'ntfy.py').unlink()
        (self.plugins_dir / 'broken.py').write_text('def nope(): pass')
        with self.assertRaisesRegex(notify.ConfigError, 'no send'):
            notify.load_plugins(self.plugins_dir)

    def test_builtin_plugins_build_requests(self):
        plugins = notify.load_plugins(self.plugins_dir)
        message = notify.build_message([{'kind': 'firing', 'rules': ['r'], 'severity': 'critical', 'host': 'wéb', 'check_id': None,
                                         'since': T0.isoformat(), 'summary': 'wéb down', 'details': ['nginx inactive'], 'fields': {}}], T0, 'https://dash.test')
        requests = []
        with patch.object(notify.urllib.request, 'urlopen', lambda request, timeout: requests.append(request) or unittest.mock.MagicMock()):
            plugins['ntfy'].send(message, {'url': 'https://ntfy.test/topic', 'token': 't'})
            plugins['webhook'].send(message, {'url': 'https://hook.test', 'headers': {'X-A': 'b'}})
            plugins['discord'].send(message, {'webhook_url': 'https://discord.test'})
            plugins['slack'].send(message, {'webhook_url': 'https://slack.test'})
        ntfy, webhook, discord, slack = requests
        self.assertEqual((ntfy.full_url, ntfy.get_header('Authorization')), ('https://ntfy.test', 'Bearer t'))
        self.assertEqual(json.loads(ntfy.data), {
            'topic': 'topic', 'title': 'sudoctor critical: wéb', 'message': '🔴 wéb down\n• nginx inactive', 'priority': 4,
            'tags': ['rotating_light'], 'click': 'https://dash.test',
            'actions': [{'action': 'view', 'label': 'Open dashboard', 'url': 'https://dash.test'}]})
        self.assertEqual(json.loads(webhook.data)['items'][0]['host'], 'wéb')
        self.assertEqual(webhook.get_header('X-a'), 'b')
        discord = json.loads(discord.data)
        self.assertEqual(discord['content'], 'sudoctor critical: wéb')
        self.assertEqual(discord['embeds'], [{'title': 'wéb down', 'color': 0xE74C3C, 'description': '• nginx inactive',
                                              'footer': {'text': 'Critical · r'}, 'timestamp': T0.isoformat(), 'url': 'https://dash.test'}])
        many = notify.build_message([dict(message['items'][0], summary=f'host {n}') for n in range(12)], T0)
        requests.clear()
        with patch.object(notify.urllib.request, 'urlopen', lambda request, timeout: requests.append(request) or unittest.mock.MagicMock()):
            plugins['discord'].send(many, {'webhook_url': 'https://discord.test'})
        embeds = json.loads(requests[0].data)['embeds']
        self.assertEqual((len(embeds), embeds[-1]['title']), (10, '+3 more'))
        self.assertLessEqual(sum(len(e['title']) + len(e.get('description', '')) + len(e.get('footer', {}).get('text', '')) for e in embeds), 6000)
        slack = json.loads(slack.data)
        self.assertEqual(slack['text'], 'sudoctor critical: wéb')
        self.assertEqual(slack['blocks'][0]['accessory']['url'], 'https://dash.test')
        self.assertEqual(slack['attachments'], [{'color': '#E74C3C', 'fallback': 'wéb down', 'blocks': [
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '*wéb down*\n• nginx inactive'}},
            {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'Critical · r'}]}]}])
        requests.clear()
        with patch.object(notify.urllib.request, 'urlopen', lambda request, timeout: requests.append(request) or unittest.mock.MagicMock()):
            plugins['slack'].send(notify.build_message([dict(message['items'][0], summary=f'<!channel> {n} & co') for n in range(25)], T0),
                                  {'webhook_url': 'https://slack.test'})
        attachments = json.loads(requests[0].data)['attachments']
        self.assertEqual(len(attachments), 20)
        self.assertTrue(attachments[0]['blocks'][0]['text']['text'].startswith('*&lt;!channel&gt; 0 &amp; co*'))
        self.assertTrue(attachments[-1]['blocks'][0]['text']['text'].startswith('*+6 more*'))
        requests.clear()
        many = notify.build_message([dict(message['items'][0], summary=f'host {n}', details=['x' * 300]) for n in range(30)]
                                    + [dict(message['items'][0], kind='resolved')], T0)
        with patch.object(notify.urllib.request, 'urlopen', lambda request, timeout: requests.append(request) or unittest.mock.MagicMock()):
            plugins['ntfy'].send(many, {'url': 'https://ntfy.test/base/topic/', 'priority': 'urgent', 'tags': ['srv']})
        sent = json.loads(requests[0].data)
        self.assertEqual((requests[0].full_url, sent['topic'], sent['priority'], sent['tags']),
                         ('https://ntfy.test/base', 'topic', 5, ['rotating_light', 'srv']))
        self.assertLessEqual(len(sent['message'].encode()), 3900)
        self.assertTrue(sent['message'].endswith(' more'))

    def test_scheduler_sweep_sends_and_records_errors(self):
        state_dir = self.dir / 'state'
        config = self.dir / 'notify.json'
        config.write_text(json.dumps({'providers': {'a': {'plugin': 'recorder', 'options': {'target': 'a'}}},
                                      'rules': [{'name': 'bad', 'conditions': [{'field': 'status', 'op': '!=', 'value': 'ok'}], 'providers': ['a']}]}))
        inventory = {'servers': {'children': ['prod']}, 'prod': {'hosts': ['web']}}
        def command(cmd, timeout, env):
            if cmd[0] == 'ansible-inventory':
                return 0, json.dumps(inventory)
            Path(env['SUDOCTOR_REPORT_DIR'], 'web.json').write_text(json.dumps(report('critical')))
            return 2, ''
        env = {'SUDOCTOR_NOTIFY_CONFIG': str(config), 'SUDOCTOR_PLUGINS_DIR': str(self.plugins_dir)}
        loaded = {}
        real_load = notify.load_plugins
        def load_plugins(directory):
            loaded.update(real_load(directory))
            return loaded
        with patch.object(scheduled, 'STATE', state_dir), patch.object(scheduled, 'run_command', command), \
                patch.dict(scheduled.os.environ, env), patch.object(notify, 'load_plugins', load_plugins), \
                patch('builtins.print'):
            self.assertEqual(scheduled.sweep(), 0)
            state = scheduled.read_state()
            self.assertEqual(state['notify']['error'], None)
            self.assertEqual(list(state['notify']['alerts']), ['bad|web|'])
            self.assertEqual(loaded['recorder'].SENT[0][1]['items'][0]['fields']['groups'], ['prod', 'servers'])
            config.write_text('{')
            self.assertEqual(scheduled.sweep(), 0)
            self.assertIn('Cannot read', scheduled.read_state()['notify']['error'])
            config.unlink()
            self.assertEqual(scheduled.sweep(), 0)
            self.assertNotIn('notify', scheduled.read_state())

    def test_inventory_groups(self):
        data = {'_meta': {}, 'all': {'children': ['servers']}, 'servers': {'children': ['prod', 'eu']},
                'prod': {'hosts': ['web']}, 'eu': {'children': ['prod']}, 'db': {'hosts': ['db']}}
        self.assertEqual(scheduled.inventory_groups(data, 'web'), ['eu', 'prod', 'servers'])


if __name__ == '__main__':
    unittest.main()
