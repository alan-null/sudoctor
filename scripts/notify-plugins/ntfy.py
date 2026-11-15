"""ntfy (https://ntfy.sh or self-hosted): publishes to a topic as JSON, so titles and text keep full Unicode.

Options: url (topic URL, required), token (access token), priority (min, low, default, high, urgent;
default follows the message severity), tags (extra tag names, added after the severity emoji).
"""
from urllib.parse import urlsplit

from sudoctor_notify import post_json

REQUIRED_OPTIONS = ('url',)
PRIORITIES = {'min': 1, 'low': 2, 'default': 3, 'high': 4, 'urgent': 5}
PRIORITY = {'critical': 'high', 'warning': 'default', 'info': 'low', 'ok': 'low'}
# Tags that are emoji short codes show as an icon before the title.
TAG = {'critical': 'rotating_light', 'warning': 'warning', 'info': 'information_source', 'ok': 'white_check_mark'}
MARK = {'critical': '🔴', 'warning': '🟡', 'info': '🔵', 'ok': '✅'}
# ntfy turns messages over 4096 bytes into attachments.
MAX_BYTES = 3900


def split_url(url):
    parts = urlsplit(str(url))
    base, _, topic = parts.path.rstrip('/').rpartition('/')
    return f'{parts.scheme}://{parts.netloc}{base}', topic


def validate_options(options):
    errors = []
    url = str(options.get('url', ''))
    if not url.startswith(('http://', 'https://')):
        errors.append('url must start with http:// or https://')
    elif not split_url(url)[1]:
        errors.append('url must end with a topic, for example https://ntfy.sh/my-topic')
    if options.get('priority') not in (None, *PRIORITIES):
        errors.append('priority must be min, low, default, high or urgent')
    return errors


def block(item):
    mark = MARK['ok' if item['kind'] == 'resolved' else item['severity']]
    reminder = ' (reminder)' if item['kind'] == 'reminder' else ''
    return '\n'.join([f'{mark} {item["summary"]}{reminder}', *('• ' + line for line in item.get('details', []))])


def body(items):
    blocks = [block(item) for item in items]
    text = '\n\n'.join(blocks)
    shown = len(blocks)
    while len(text.encode()) > MAX_BYTES and shown > 1:
        shown -= 1
        text = '\n\n'.join(blocks[:shown] + [f'+{len(blocks) - shown} more'])
    if len(text.encode()) > MAX_BYTES:
        text = text.encode()[:MAX_BYTES].decode(errors='ignore') + '…'
    return text


def send(message, options):
    base, topic = split_url(options['url'])
    severity = message['severity']
    payload = {
        'topic': topic,
        'title': message['title'],
        'message': body(message['items']) if message.get('items') else message['text'],
        'priority': PRIORITIES[options.get('priority') or PRIORITY.get(severity, 'default')],
        'tags': [TAG.get(severity, 'warning'), *(options.get('tags') or [])],
    }
    if message.get('dashboard_url'):
        payload['click'] = message['dashboard_url']
        payload['actions'] = [{'action': 'view', 'label': 'Open dashboard', 'url': message['dashboard_url']}]
    headers = {'Authorization': 'Bearer ' + options['token']} if options.get('token') else None
    post_json(base, payload, headers)
