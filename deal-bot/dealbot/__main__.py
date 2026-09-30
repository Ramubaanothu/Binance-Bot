"""python -m dealbot [run | login | channels | test "post text"]"""
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
