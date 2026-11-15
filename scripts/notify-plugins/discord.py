"""Discord channel webhook. Each notification becomes an embed colored by severity.

Options: webhook_url (required), username (display name override).
"""
from sudoctor_notify import post_json, state_label

REQUIRED_OPTIONS = ('webhook_url',)
COLORS = {'critical': 0xE74C3C, 'warning': 0xF1C40F, 'info': 0x3498DB, 'ok': 0x2ECC71}
MAX_EMBEDS = 10        # Discord limit per message
TEXT_BUDGET = 5500     # Discord allows 6000 characters across all embeds


def validate_options(options):
    return [] if str(options.get('webhook_url', '')).startswith('https://') else ['webhook_url must start with https://']


def cut(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:max(limit - 1, 0)] + '…'


def embed(item, budget, message):
    title = cut(item['summary'], 256)
    lines = '\n'.join('• ' + line for line in item.get('details', []))
    footer = cut(state_label(item).capitalize() + ' · ' + ', '.join(item['rules']), 200)
    result = {
        'title': title,
        'color': COLORS['ok' if item['kind'] == 'resolved' else item['severity']],
        'footer': {'text': footer},
        'timestamp': message['timestamp_utc'],
    }
    room = budget - len(title) - len(footer)
    if lines and room > 20:
        result['description'] = cut(lines, min(room, 4096))
    if message.get('dashboard_url'):
        result['url'] = message['dashboard_url']
    return result


def send(message, options):
    items = message['items']
    shown = items if len(items) <= MAX_EMBEDS else items[:MAX_EMBEDS - 1]
    budget = TEXT_BUDGET // (len(shown) + (len(shown) < len(items)))
    embeds = [embed(item, budget, message) for item in shown]
    if len(shown) < len(items):
        rest = items[len(shown):]
        embeds.append({'title': f'+{len(rest)} more', 'color': COLORS['info'],
                       'description': cut('\n'.join('• ' + i['summary'] for i in rest), budget)})
    body = {'content': cut(message['title'], 2000), 'embeds': embeds, 'allowed_mentions': {'parse': []}}
    if options.get('username'):
        body['username'] = options['username']
    post_json(options['webhook_url'], body)
