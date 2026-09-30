"""Formats the alert and sends it through your alert bot (Telegram Bot API).

A message from a bot rings your phone like any chat; a message you send to
your own Saved Messages does not, which is why alerts go through a bot.
"""
import html
import json
import urllib.parse
import urllib.request

NAMES = {'amazon': 'Amazon', 'flipkart': 'Flipkart'}


def _rs(x):
    return '₹{:,.0f}'.format(x) if x is not None else '?'


def format_alert(deal, text, source, age_s):
    head = '\U0001F6A8 <b>{}% OFF</b> · {}'.format(
        int(deal.discount) if deal.discount else '??', NAMES[deal.platform])
    if deal.glitch_words:
        head += ' · ⚠️ price error?'
    lines = [head]
    if deal.price is not None:
        lines.append('<b>{}</b>'.format(_rs(deal.price))
                     + ('  (MRP {})'.format(_rs(deal.mrp)) if deal.mrp else ''))
    snippet = (text or '').strip()
    if len(snippet) > 500:
        snippet = snippet[:500] + '…'
    lines += ['', html.escape(snippet), '',
              '<i>from {} · {}s after posting</i>'.format(html.escape(source), int(age_s))]
    return '\n'.join(lines)


def send(token, chat_id, text, link, platform):
    params = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'false',
        'reply_markup': json.dumps({'inline_keyboard': [[
            {'text': '\U0001F6D2 Open on ' + NAMES[platform], 'url': link}]]}),
    }
    url = 'https://api.telegram.org/bot{}/sendMessage'.format(token)
    with urllib.request.urlopen(url, urllib.parse.urlencode(params).encode(), timeout=15) as r:
        return json.loads(r.read())
