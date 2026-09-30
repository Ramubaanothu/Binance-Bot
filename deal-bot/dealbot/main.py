"""Listens to loot-deal channels through your Telegram account and alerts
you on Amazon / Flipkart posts at or above MIN_DISCOUNT."""
import asyncio
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

from telethon import TelegramClient, events

from . import config, notify
from .parse import analyse, product_key
from .store import Seen

log = logging.getLogger('dealbot')

SHORT_HOSTS = ('amzn.to', 'amzn.in', 'amzn.eu', 'a.co', 'fkrt.it', 'fkrt.cc', 'fktr.in', 'fkrt.co')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def expand(url, hops=5):
    """Follow a short link's redirects just far enough to see the product URL.

    Only the Location headers are read; the product page itself is never
    downloaded. Any failure returns the last URL reached.
    """
    for _ in range(hops):
        if not any(h in url for h in SHORT_HOSTS):
            return url
        req = urllib.request.Request(url, method='HEAD',
                                     headers={'User-Agent': 'Mozilla/5.0'})
        try:
            _opener.open(req, timeout=5)
            return url
        except urllib.error.HTTPError as e:
            loc = e.headers.get('Location')
            if not loc or e.code not in (301, 302, 303, 307, 308):
                return url
            url = urljoin(url, loc)
        except Exception:
            return url
    return url


def _url_of(obj):
    # Read by attribute, not class: Telethon renames these types between
    # layers (KeyboardButtonUrl became KeyboardButton(type=...)).
    url = getattr(obj, 'url', None)
    if isinstance(url, str):
        return url
    t = getattr(obj, 'type', None)
    return _url_of(t) if t is not None and t is not obj else None


def hidden_urls(msg):
    """Links behind text ("Buy now") and under the post (inline buttons)."""
    out = [u for u in map(_url_of, msg.entities or []) if u]
    for row in getattr(getattr(msg, 'reply_markup', None), 'rows', None) or []:
        out += [u for u in map(_url_of, row.buttons) if u]
    return out


def wanted(deal):
    if deal is None or deal.platform not in config.PLATFORMS:
        return False
    if deal.discount >= config.MIN_DISCOUNT:
        return True
    return config.ALERT_ON_GLITCH_WORDS and deal.glitch_words


async def handle(event, seen):
    msg = event.message
    text = msg.message or ''
    deal = analyse(text, hidden_urls(msg))
    if not wanted(deal):
        return

    link = await asyncio.to_thread(expand, deal.links[0])
    key = '{}@{}'.format(product_key(link), deal.price)
    if not seen.check_and_add(key):
        log.info('duplicate %s', key)
        return

    chat = await event.get_chat()
    source = getattr(chat, 'title', None) or getattr(chat, 'username', None) or str(event.chat_id)
    age = max(0.0, time.time() - msg.date.timestamp())
    body = notify.format_alert(deal, text, source, age)
    log.info('ALERT %s%% %s %s', deal.discount, deal.platform, key)
    if config.BOT_TOKEN and config.ALERT_CHAT_ID:
        await asyncio.to_thread(notify.send, config.BOT_TOKEN, config.ALERT_CHAT_ID,
                                body, link, deal.platform)
    else:
        # No alert bot set up: fall back to Saved Messages (arrives silently).
        await event.client.send_message('me', body + '\n' + link, parse_mode='html')


def client():
    if not (config.API_ID and config.API_HASH):
        raise SystemExit('Set TG_API_ID and TG_API_HASH in deal-bot/.env (from my.telegram.org).')
    return TelegramClient(config.SESSION, config.API_ID, config.API_HASH)


async def run():
    seen = Seen(config.DB_PATH, config.DEDUPE_HOURS)
    tg = client()
    await tg.start()
    chats = config.CHANNELS or None

    async def on_msg(event):
        if chats is None and not event.is_channel:
            return
        try:
            await handle(event, seen)
        except Exception:
            log.exception('failed on message %s in %s', event.message.id, event.chat_id)

    # Loot channels often post first and edit the price in a few seconds later.
    tg.add_event_handler(on_msg, events.NewMessage(chats=chats))
    tg.add_event_handler(on_msg, events.MessageEdited(chats=chats))

    log.info('watching %s, alerting at >= %s%% on %s',
             ', '.join(config.CHANNELS) or 'all joined channels',
             config.MIN_DISCOUNT, '/'.join(config.PLATFORMS))
    await tg.run_until_disconnected()


async def list_channels():
    tg = client()
    await tg.start()
    async for d in tg.iter_dialogs():
        if d.is_channel:
            uname = getattr(d.entity, 'username', None)
            print('{:>16}  {:<32}  {}'.format(d.id, '@' + uname if uname else '-', d.name))
    await tg.disconnect()
