"""python -m dealbot <command>

  run                      watch the channels (what the service runs)
  login                    one-time Telegram login
  channels                 list the channels your account has joined
  test "post text"         try the filter on a post, no Telegram needed
  amazon-cookies FILE      load Amazon login cookies exported from your browser
  amazon-test ASIN|URL     dry-run an order: goes to Place order and stops
  orders                   list recent auto-order attempts
"""
import asyncio
import logging
import sys

from . import config


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('telethon').setLevel(logging.WARNING)

    if cmd == 'test':
        # Try the filter on a pasted post without touching Telegram.
        from .main import wanted
        from .parse import analyse
        deal = analyse(' '.join(sys.argv[2:]) or sys.stdin.read())
        print(deal)
        print('ALERT' if wanted(deal) else 'no alert',
              '(threshold {}%)'.format(config.MIN_DISCOUNT))
        return

    if cmd == 'amazon-cookies' and len(sys.argv) == 3:
        from .amazon import AmazonBuyer

        async def load():
            b = AmazonBuyer()
            try:
                n, greeting = await b.import_cookies(sys.argv[2])
            finally:
                await b.close()
            print('Loaded {} amazon.in cookies.'.format(n))
            if greeting:
                print('Logged in: "{}". You can delete the cookie file now.'.format(greeting.strip()))
            else:
                sys.exit('Amazon does not show you as logged in. Log in on amazon.in in '
                         'your browser, export the cookies again, and retry.')
        asyncio.run(load())
        return

    if cmd == 'amazon-test' and len(sys.argv) == 3:
        from .amazon import AmazonBuyer
        from .main import expand
        from .parse import product_key
        arg = sys.argv[2]
        key = product_key(expand(arg)) if '/' in arg else 'amazon:' + arg.upper()
        if not key.startswith('amazon:'):
            sys.exit('Could not find an Amazon product id (ASIN) in ' + arg)

        async def dry():
            b = AmazonBuyer()
            try:
                # alert_price=None: checks the page MRP shows MIN_DISCOUNT,
                # so for a normal product expect "only N% off" unless you
                # set MIN_DISCOUNT=0 for this test.
                res = await b.buy(key.split(':', 1)[1], None, dry_run=True)
            finally:
                await b.close()
            print(res)
        asyncio.run(dry())
        return

    if cmd == 'orders':
        import time
        from .store import Orders
        rows = Orders(config.DB_PATH).db.execute(
            'SELECT ts, asin, status, price, note FROM orders ORDER BY ts DESC LIMIT 30')
        for ts, asin, status, total, note in rows:
            print(time.strftime('%d %b %H:%M', time.localtime(ts)), asin, status,
                  '' if total is None else '₹{:,.0f}'.format(total), note)
        return

    from . import main as m
    if cmd == 'login':
        # First run only: asks for your phone number and the code Telegram sends.
        async def login():
            tg = m.client()
            await tg.start()
            me = await tg.get_me()
            print('Logged in as', me.first_name, '- session saved.')
            await tg.disconnect()
        asyncio.run(login())
    elif cmd == 'channels':
        asyncio.run(m.list_channels())
    elif cmd == 'run':
        asyncio.run(m.run())
    else:
        sys.exit(__doc__)


main()
