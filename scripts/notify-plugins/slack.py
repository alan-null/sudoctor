"""Slack incoming webhook. Each notification becomes an attachment with a severity-colored bar.

Options: webhook_url (required), channel (override for legacy webhooks).
"""
from sudoctor_notify import post_json, state_label

REQUIRED_OPTIONS = ('webhook_url',)
COLORS = {'critical': '#E74C3C', 'warning': '#F1C40F', 'info': '#3498DB', 'ok': '#2ECC71'}
MAX_ATTACHMENTS = 20   # Slack advises at most 20 attachments per message
SECTION_LIMIT = 3000   # Slack limit for section text


def validate_options(options):
    return [] if str(options.get('webhook_url', '')).startswith('https://') else ['webhook_url must start with https://']


def esc(text):
    # Slack mrkdwn needs only these three escaped; this also stops <!channel> style mentions.
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def mrkdwn(text):
    return {'type': 'mrkdwn', 'text': text}


def attachment(item):
    lines = [f'*{esc(item["summary"])}*', *('• ' + esc(line) for line in item.get('details', []))]
    footer = state_label(item).capitalize() + ' · ' + ', '.join(item['rules'])
    return {
        'color': COLORS['ok' if item['kind'] == 'resolved' else item['severity']],
        'fallback': item['summary'],
        'blocks': [{'type': 'section', 'text': mrkdwn(cut('\n'.join(lines), SECTION_LIMIT))},
                   {'type': 'context', 'elements': [mrkdwn(cut(esc(footer), 2000))]}],
    }


def send(message, options):
    items = message['items']
    shown = items if len(items) <= MAX_ATTACHMENTS else items[:MAX_ATTACHMENTS - 1]
    attachments = [attachment(item) for item in shown]
    if len(shown) < len(items):
        rest = '\n'.join('• ' + esc(i['summary']) for i in items[len(shown):])
        attachments.append({'color': COLORS['info'], 'blocks': [
            {'type': 'section', 'text': mrkdwn(cut(f'*+{len(items) - len(shown)} more*\n{rest}', SECTION_LIMIT))}]})
    header = {'type': 'section', 'text': mrkdwn(f'*{esc(message["title"])}*')}
    if message.get('dashboard_url'):
        header['accessory'] = {'type': 'button', 'text': {'type': 'plain_text', 'text': 'Open dashboard'},
                               'url': message['dashboard_url']}
    # 'text' is the notification preview and the fallback for clients without blocks.
    body = {'text': message['title'], 'blocks': [header], 'attachments': attachments}
    if options.get('channel'):
        body['channel'] = options['channel']
    post_json(options['webhook_url'], body)
