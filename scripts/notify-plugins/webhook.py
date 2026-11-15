"""Generic JSON webhook: POSTs the whole message as JSON.

Options: url (required), token (sent as a Bearer token), headers (object of extra headers).
"""
from sudoctor_notify import post_json

REQUIRED_OPTIONS = ('url',)


def validate_options(options):
    if not str(options.get('url', '')).startswith(('http://', 'https://')):
        return ['url must start with http:// or https://']
    if not isinstance(options.get('headers', {}), dict):
        return ['headers must be an object']
    return []


def send(message, options):
    headers = {str(k): str(v) for k, v in (options.get('headers') or {}).items()}
    if options.get('token'):
        headers['Authorization'] = 'Bearer ' + options['token']
    post_json(options['url'], message, headers)
