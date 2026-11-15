import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import sudoctor_notify as notify  # noqa: E402
import sudoctor_notify_preview as preview  # noqa: E402

RECORDER = '''
SENT = []
def send(message, options):
    SENT.append((options, message))
    return options.get('fail') is not True
'''


class PreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        (self.dir / 'plugins').mkdir()
        (self.dir / 'plugins' / 'recorder.py').write_text(RECORDER)
        self.config = self.dir / 'notify.json'
        self.config.write_text(json.dumps({
            'providers': {'rec': {'plugin': 'recorder', 'options': {'target': 'rec'}},
                          'other': {'plugin': 'recorder', 'options': {'target': 'other'}}},
            'rules': [{'name': 'not ok', 'conditions': [{'field': 'status', 'op': '!=', 'value': 'ok'}], 'providers': ['rec']},
                      {'name': 'slow', 'for': '1d', 'conditions': [{'field': 'outcome', 'op': '=', 'value': 'unreachable'}],
                       'providers': ['rec']},
                      {'name': 'elsewhere', 'conditions': [], 'providers': ['other']}]}))
        self.env = {'SUDOCTOR_NOTIFY_CONFIG': str(self.config), 'SUDOCTOR_PLUGINS_DIR': str(self.dir / 'plugins')}
        self.loaded = {}
        real = notify.load_plugins
        def load_plugins(directory):
            self.loaded.update(real(directory))
            return self.loaded
        load_patch = patch.object(notify, 'load_plugins', load_plugins)
        load_patch.start()
        self.addCleanup(load_patch.stop)
        sleep_patch = patch.object(preview.time, 'sleep')
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def run_cli(self, *argv):
        lines = []
        code = preview.main(list(argv), self.env, log=lambda *a, **k: lines.append(' '.join(map(str, a))))
        return code, lines, [message for _, message in self.loaded.get('recorder').SENT] if 'recorder' in self.loaded else []

    def test_default_sample_and_all_samples(self):
        code, lines, sent = self.run_cli('rec')
        self.assertEqual(code, 0)
        self.assertEqual(lines, ['sample mixed: rec (recorder) sent'])
        self.assertEqual(sent[0]['title'], 'sudoctor: 1 critical, 2 warning, 1 resolved')
        self.loaded['recorder'].SENT.clear()
        code, lines, sent = self.run_cli('rec', '--sample', 'all')
        self.assertEqual(len(sent), len(preview.SAMPLE_ORDER))
        self.assertEqual(preview.time.sleep.call_count, len(preview.SAMPLE_ORDER) - 1)

    def test_events_file_shapes_and_errors(self):
        now = notify.dt.datetime.now(notify.dt.timezone.utc)
        example = preview.events_messages(ROOT / 'docker/notify-events.example.json', now)
        self.assertEqual([len(items) for items in example], [2, 1])
        self.assertEqual(example[1][0]['severity'], 'ok')
        path = self.dir / 'events.json'
        for data, count in (({'host': 'a'}, [1]), ([{'host': 'a'}, {'host': 'b'}], [2]), ({'items': [{}]}, [1])):
            path.write_text(json.dumps(data))
            self.assertEqual([len(items) for items in preview.events_messages(path, now)], count)
        for data in ({'hots': 'a'}, {'kind': 'fired'}, {'severity': 'bad'}, {'details': 'x'}, {'items': []}, [1]):
            path.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(preview.PreviewError):
                preview.events_messages(path, now)
        path.write_text(json.dumps({'host': 'a', 'summary': 'custom', 'details': ['x']}))
        code, _, sent = self.run_cli('rec', '--events', str(path))
        self.assertEqual((code, sent[0]['text']), (0, '[warning] custom\n  - x'))

    def test_reports_use_rules_for_provider_and_ignore_for(self):
        reports = self.dir / 'reports'
        reports.mkdir()
        (reports / 'web.json').write_text(json.dumps({'status': 'critical', 'timestamp_utc': '2026-01-01T00:00:00Z', 'issues': ['disk']}))
        (reports / 'db.json').write_text(json.dumps({'status': 'ok', 'timestamp_utc': '2026-01-01T00:00:00Z'}))
        state = self.dir / 'state.json'
        state.write_text(json.dumps({'hosts': {'edge': {'attempt': {'outcome': 'unreachable'}}}}))
        code, _, sent = self.run_cli('rec', '--reports', str(reports), str(state))
        self.assertEqual(code, 0)
        items = sent[0]['items']
        self.assertEqual([(i['host'], i['rules']) for i in items], [('web', ['not ok']), ('edge', ['slow'])])
        self.loaded['recorder'].SENT.clear()
        code, _, sent = self.run_cli('rec', '--reports', str(reports), '--all-rules')
        self.assertEqual([(i['host'], i['rules']) for i in sent[0]['items']], [('db', ['elsewhere']), ('web', ['not ok', 'elsewhere'])])
        bad = self.dir / 'bad.json'
        bad.write_text('{"hello": 1}')
        self.assertEqual(self.run_cli('rec', '--reports', str(bad))[0], 2)

    def test_dry_run_prints_requests_and_hides_secrets(self):
        secret = self.dir / 'url'
        secret.write_text('https://discord.test/api/webhooks/1/SECRET\n')
        for plugin, options in (('discord', [f'webhook_url_file={secret}']), ('slack', ['webhook_url=https://slack.test/SECRET']),
                                ('ntfy', ['url=https://ntfy.test/SECRET', 'token=SECRET', 'tags=["a","b"]']),
                                ('webhook', ['url=https://hook.test/SECRET', 'headers={"X-Api-Key":"SECRET"}'])):
            with self.subTest(plugin=plugin):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code, lines, _ = self.run_cli('--plugin', plugin, *sum((['--option', o] for o in options), []),
                                                  '--sample', 'all', '--dry-run')
                self.assertEqual(code, 0, lines)
                self.assertTrue(all(line.endswith('printed (dry run)') for line in lines), lines)
                self.assertNotIn('SECRET', out.getvalue())
                self.assertEqual(out.getvalue().count('--- POST'), len(preview.SAMPLE_ORDER))

    def test_ntfy_tags_option_is_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_cli('--plugin', 'ntfy', '--option', 'url=https://ntfy.test/t', '--option', 'tags=["a","b"]', '--dry-run')
        sent = json.loads(out.getvalue().split('\n\n', 1)[1])
        self.assertEqual((sent['tags'], sent['topic']), (['rotating_light', 'a', 'b'], '<hidden>'))

    def test_errors_and_failures(self):
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli('rec', '--plugin', 'debug')[0], 2)
        self.assertEqual(self.run_cli('rec', '--option', 'a=b')[0], 2)
        self.assertEqual(self.run_cli('missing')[0], 2)
        self.assertEqual(self.run_cli('--plugin', 'discord')[0], 2)
        self.assertEqual(self.run_cli('rec', '--sample', 'nope')[0], 2)
        self.assertEqual(self.run_cli('--plugin', 'recorder', '--option', 'fail=true')[0], 1)
        # --plugin works without a notify.json.
        self.config.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cli('--plugin', 'debug', '--sample', 'check')[0], 0)


if __name__ == '__main__':
    unittest.main()
