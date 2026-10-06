"""Decides which alerts become orders, enforces the limits, and reports back.

Rules (all must hold):
  * AUTO_ORDER is on, the deal is on Amazon and has an ASIN
  * the post shows a real price-based saving of at least MIN_DISCOUNT
    ("price error" wording alone never orders)
  * this ASIN has never been ordered before
  * MAX_ORDERS_PER_DAY not reached (when set)
Then AmazonBuyer checks the live page, and pays only if the Amazon Pay
balance covers the whole total.

One order at a time: a second deal waits for the first to finish, so two
orders can never race for the same balance.
"""
import asyncio
import html
import logging

from . import config, notify

log = logging.getLogger('dealbot.autobuy')

ORDERS_URL = '/gp/your-account/order-history'


def asin_of(key):
    return key.split(':', 1)[1] if key.startswith('amazon:') else None


def eligible(deal, product):
    """Why not to auto-order this deal, or None if it qualifies."""
    if not config.AUTO_ORDER:
        return 'auto-order is off'
    if deal.platform != 'amazon':
        return 'not Amazon'
    if not asin_of(product):
        return 'no Amazon product id in the link'
    if deal.discount < config.MIN_DISCOUNT:
        return 'saving below {}%'.format(config.MIN_DISCOUNT)
    return None


def _rs(x):
    return '₹{:,.0f}'.format(x) if x is not None else '?'


def report_text(asin, res):
    base = config.AMAZON_BASE
    product = '{}/dp/{}'.format(base, asin)
    if res.status == 'placed':
        head = '✅ <b>ORDERED</b> on Amazon'
        tail = ('Paid {} from Amazon Pay balance (balance was {}).\n'
                'Review and cancel if not wanted: {}{}').format(
                    _rs(res.total), _rs(res.balance), base, ORDERS_URL)
    elif res.status == 'dry_run':
        head = '\U0001F9EA <b>Dry run</b> reached Place order'
        tail = 'Total {}, balance {}. Nothing was ordered.'.format(_rs(res.total), _rs(res.balance))
    elif res.status == 'unknown':
        head = '⚠️ <b>Order status unclear</b>'
        tail = html.escape(res.reason) + '\n{}{}'.format(base, ORDERS_URL)
    else:
        head = '❌ <b>Not ordered</b>'
        tail = html.escape(res.reason)
    return '{}\n{}\n{}'.format(head, product, tail)


class AutoOrderer:
    def __init__(self, buyer, orders, send_text=None, send_photo=None):
        self.buyer = buyer
        self.orders = orders
        self.send_text = send_text
        self.send_photo = send_photo
        self._tasks = set()

    def submit(self, deal, product):
        """Start an order in the background if the deal qualifies."""
        why = eligible(deal, product)
        if why:
            log.info('no auto-order for %s: %s', product, why)
            return None
        t = asyncio.create_task(self.order(asin_of(product), deal.price))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def order(self, asin, alert_price):
        async with self.buyer.lock:
            if self.orders.already_ordered(asin):
                log.info('already ordered %s; skipping', asin)
                return None
            cap = config.MAX_ORDERS_PER_DAY
            if cap and self.orders.today_count() >= cap:
                await self.say('⏸ Daily limit of {} orders reached; not ordering {}'.format(
                    cap, asin))
                return None
            res = await self.buyer.buy(asin, alert_price, dry_run=config.ORDER_DRY_RUN)
            self.orders.record(asin, res.status, res.total, res.reason)
        log.info('order %s -> %s %s', asin, res.status, res.reason)
        await self._report(asin, res)
        return res

    async def say(self, text):
        if self.send_text:
            try:
                await asyncio.to_thread(self.send_text, text)
            except Exception:
                log.exception('could not send message')

    async def _report(self, asin, res):
        text = report_text(asin, res)
        if self.send_photo and res.screenshot:
            try:
                await asyncio.to_thread(self.send_photo, res.screenshot, text)
                return
            except Exception:
                log.exception('could not send screenshot')
        await self.say(text)


def from_config():
    """Build the orderer from .env, or None if auto-order is off."""
    if not config.AUTO_ORDER:
        return None
    from .amazon import AmazonBuyer
    from .store import Orders
    send_text = send_photo = None
    if config.BOT_TOKEN and config.ALERT_CHAT_ID:
        def send_text(text):
            return notify.send_text(config.BOT_TOKEN, config.ALERT_CHAT_ID, text)

        def send_photo(path, caption):
            return notify.send_photo(config.BOT_TOKEN, config.ALERT_CHAT_ID, path, caption)
    return AutoOrderer(AmazonBuyer(), Orders(config.DB_PATH), send_text, send_photo)
