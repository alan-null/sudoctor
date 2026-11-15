"""JSON Schema checks; install optional validators in Docker as documented."""
import copy
import json
import pathlib
import unittest

try:
    import jsonschema
    import yaml
    from referencing import Registry, Resource
except ImportError:
    jsonschema = yaml = None


ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(jsonschema and yaml, 'schema tests require jsonschema and PyYAML (see README)')
class Schemas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {path.name: json.loads(path.read_text()) for path in (ROOT / 'schemas').glob('*.json')}
        registry = Registry().with_resources(
            ((ROOT / 'schemas' / name).as_uri(), Resource.from_contents(schema))
            for name, schema in cls.schemas.items())
        cls.validators = {}
        for name, schema in cls.schemas.items():
            jsonschema.Draft7Validator.check_schema(schema)
            cls.validators[name] = jsonschema.Draft7Validator(
                {'$ref': (ROOT / 'schemas' / name).as_uri()}, registry=registry)

    def valid(self, config, schema='inventory.schema.json'):
        self.validators[schema].validate(config)

    def invalid(self, config):
        self.assertTrue(list(self.validators['inventory.schema.json'].iter_errors(config)), config)

    def test_inventory_files_and_generated_configs(self):
        defaults = yaml.safe_load((ROOT / 'inventory/group_vars/servers.yml').read_text())
        for folder in ('host_vars', 'group_vars'):
            for path in (ROOT / 'inventory' / folder).glob('*.yml'):
                with self.subTest(path=path):
                    config = yaml.safe_load(path.read_text())
                    self.valid(config)
                    merged = {**defaults, **config}
                    health = {key: merged['sudoctor_' + key] for key in ('services', 'docker', 'tcp', 'http', 'tmux', 'disk_warning_percent', 'min_free_mb', 'security')}
                    self.valid(health, 'health.schema.json')
                    for session in merged['sudoctor_tmux']:
                        self.valid(session, 'tmux.schema.json')

    def test_partial_inventory_and_other_ansible_variables(self):
        self.valid({})
        self.valid({'ansible_user': 'alan', 'custom_app_dir': '/srv/app', 'sudoctor_update_enabled': True})
        self.valid({'sudoctor_docker': ['worker', {'name': 'api', 'require_healthy': True}]})
        self.valid({'sudoctor_security': {'ssh': {'context': {'user': 'alice', 'addr': '192.0.2.1'}}, 'allowed_listeners': ['127.0.0.1:22'], 'certificates': [{'name': 'api', 'host': 'api.example.test', 'warning_days': 14}]}})

    def test_schema_examples(self):
        for example in self.schemas['inventory.schema.json']['examples']:
            self.valid(example)

    def test_reject_invalid_inventory(self):
        for config in (
            {'sudoctor_tmxx': []},
            {'sudoctor_auto_reboot': 'false'},
            {'sudoctor_min_free_mb': -1},
            {'sudoctor_disk_warning_percent': 101},
            {'sudoctor_services': ['ssh.service', 'ssh.service']},
            {'sudoctor_docker': [{'name': 'app', 'healthy': True}]},
            {'sudoctor_tcp': [{'name': 'api', 'host': 'localhost', 'port': 65536}]},
            {'sudoctor_http': [{'name': 'api', 'url': 'ftp://localhost', 'status': 200}]},
            {'sudoctor_http': [{'name': 'api', 'url': 'http://localhost', 'status': 999}]},
            {'sudoctor_security': {'firewall': {'backend': 'ufw'}}},
            {'sudoctor_security': {'certificates': [{'name': 'api'}]}},
        ):
            with self.subTest(config=config):
                self.invalid(config)

    def test_reject_invalid_tmux(self):
        example = self.schemas['inventory.schema.json']['examples'][0]
        for field, value in (('layout', 'horizontal'), ('panes', []), ('cwd', '~/app'), ('command', 'exec bash')):
            config = copy.deepcopy(example)
            config['sudoctor_tmux'][0]['windows'][0][field] = value
            with self.subTest(field=field):
                self.invalid(config)
        for field, value in (('command', '   '), ('name', 'bad:name'), ('cwd', 'relative'), ('unexpected', True)):
            config = copy.deepcopy(example)
            config['sudoctor_tmux'][0]['windows'][0]['panes'][0][field] = value
            with self.subTest(pane_field=field):
                self.invalid(config)
        config = copy.deepcopy(example)
        del config['sudoctor_tmux'][0]['user']
        self.invalid(config)
        config = copy.deepcopy(example)
        del config['sudoctor_tmux'][0]['windows'][0]['panes']
        self.invalid(config)

    def test_notify_schema(self):
        validator = self.validators['notify.schema.json']
        example = json.loads((ROOT / 'docker/notify.example.json').read_text())
        validator.validate(example)
        for config in self.schemas['notify.schema.json']['examples']:
            validator.validate(config)
        validator.validate({'providers': {'mine': {'plugin': 'telegram', 'options': {'chat_id': 1, 'token_file': '/t'}}}, 'rules': []})
        rule = {'name': 'r', 'conditions': [], 'providers': ['p']}
        for change in (
            {'scope': 'disk'}, {'for': '5 min'}, {'severity': 'urgent'}, {'when': {}}, {'providers': []},
            {'conditions': [{'field': 'check_id', 'op': '=', 'value': 'x'}]},
            {'conditions': [{'field': 'load_1m', 'op': '>', 'value': '1'}]},
            {'conditions': [{'field': 'status', 'op': 'in', 'value': 'critical'}]},
            {'conditions': [{'field': 'status', 'op': '=', 'value': 'down'}]},
            {'conditions': [{'field': 'reboot_required', 'op': '>', 'value': 1}]},
            {'conditions': [{'field': 'nope', 'op': '=', 'value': 1}]},
        ):
            with self.subTest(change=change):
                self.assertTrue(list(validator.iter_errors({'providers': {}, 'rules': [{**rule, **change}]})))
        for provider in ({'plugin': 'ntfy'}, {'plugin': 'ntfy', 'options': {'url': 'ftp://x'}},
                         {'plugin': 'discord', 'options': {'webhook_url': 'http://x'}}, {'plugin': 'Bad'}):
            with self.subTest(provider=provider):
                self.assertTrue(list(validator.iter_errors({'providers': {'p': provider}, 'rules': []})))
        validator.validate({'providers': {}, 'rules': [{**rule, 'scope': 'check',
                            'conditions': [{'field': 'check_status', 'op': 'in', 'value': ['warning', 'critical']}]}]})

    def test_shorthand_and_readiness(self):
        self.valid({'sudoctor_tmux': [{'name': 'worker', 'user': 'alan', 'vpn_unit': 'netbird.service',
                                      'ready_tcp': {'host': '10.0.0.1', 'port': 443, 'timeout': 180},
                                      'windows': [{'name': 'app', 'command': 'exec /srv/app/start.sh'}]}]})


if __name__ == '__main__':
    unittest.main()
