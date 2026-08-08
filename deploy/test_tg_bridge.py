#!/usr/bin/env python3
"""Replay real conversations through the bridge and check what comes back.

Run on the droplet:
    cd /home/bots/telegram && /home/bots/venv/bin/python test_tg_bridge.py

Every case below is something that actually broke in a live chat. Previews are
generated but never confirmed, so no order is placed.

NOT covered here on purpose: pause / resume / restart. Those write flag files
and really do stop the bots - running them as a "test" once paused reverse for
a minute. Test them deliberately, not in a sweep.
"""
import importlib.util, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, 'tg_bridge.py')

spec = importlib.util.spec_from_file_location('tb', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
if len(sys.argv) > 1:                       # local run: point at a config copy
    m.MAIN_CONFIG = sys.argv[1]

# (chat_id, message, must_match_regex, must_NOT_match_regex)
CASES = [
    # ── the dead-ends from the 3 Aug screenshot ──────────────────────────
    ('a', 'buy eth 0.5',       r'too small|at least',        None),
    ('a', '100$',              r'BUY ETHUSDT',               r"didn't catch"),
    ('a', 'Eth price?',        r'ETHUSDT\* is at',           None),
    ('a', 'Buy 100 eth at 1860', r'limit @ 1860',            None),
    ('a', 'Want to buy spot',  r'BUY ETHUSDT.*SPOT',         r"couldn't work out"),
    ('a', 'make it 200',       r'SPOT',                      None),

    # a size with no history must still ask, not invent a trade.
    # (guard on the preview marker - the fallback text itself says "buy 100
    # doge" as an example, so matching on the word 'buy' is useless here)
    ('z', '250',               r"didn't catch",              r'Reply \*yes\*'),

    # ── dollars vs units ─────────────────────────────────────────────────
    ('b', 'buy 2 qty sol',     r'\*1\.99 units\*',           r'0\.02 units'),
    ('b', 'buy sol 5 @73',     r'took 5 as DOLLARS',         None),

    # ── venue routing ────────────────────────────────────────────────────
    ('c', 'spot buy 200 sol',  r'SPOT',                      None),
    ('c', 'buy 200 sol',       r'_perp_',                    r'SPOT'),
    ('d', 'spot sell sol',     r'no shorting',               None),
    ('e', 'spot wallet',       r'Spot wallet',               r'\*SPOT\*  '),
    ('e', 'spot bot',          r'\*SPOT\*',                  r'Spot wallet'),
    ('e', 'how is the spot bot doing', r'\*SPOT\*',          None),
    ('e', 'spot orders',       r'spot orders',               r'What you are trading'),
    ('e', 'what am i trading', r'What you are trading',      None),

    # ── the bugs from the 8 Aug screenshots ──────────────────────────────
    ('h', 'buy 100 uni at 3.97', r'BUY UNIUSDT',              None),
    ('h', 'Qty',                 r'\*100 units\*',            r"didn't catch"),
    ('h', '✅ YES, place it', r'Placed|Filled|expired|Nothing waiting', None),
    ('i', 'Spot uni 100qty @3.9', r'SPOT',                    r'What you are trading'),
    ('i', 'uni 50qty',           r'\*50 units\*',             None),
    ('i', 'uniswap',             r'UNIUSDT',                  None),
    ('i', 'buy 300 gold',        r'XAUUSDT',                  None),
    ('i', '❌ Cancel',       r'ancel',                    None),

    # ── day reports (a 00:0x 'today' used to just say nothing) ───────────
    ('g', 'today',             r'Today',                     None),
    ('g', 'yesterday',         r'Yesterday',                 r'Trade report'),
    ('g', 'yesterday report',  r'Yesterday',                 r'Trade report'),
    ('g', 'report for 2026-08-02', r'\*2026-08-02\*',         r'Trade report'),
    ('g', 'report',            r'Trade report',              None),

    # ── read-only status commands ────────────────────────────────────────
    ('f', 'positions',         r'MAIN|YOURS',                None),
    ('f', 'analyse the trades', r'Trade report',             None),
    ('f', 'show me positions', r'MAIN|YOURS',                None),
    ('f', 'today',             r'Today',                     None),
    ('f', 'server status',     r'Server',                    None),
    ('f', 'orders',            r'orders',                    None),
    ('f', 'hi',                r'Hey',                       None),
    ('f', 'help',              r'Talk to me normally',       None),
]

fails = 0
for chat, msg, want, avoid in CASES:
    try:
        r = str(m.parse(chat, msg) or '')
    except Exception as e:
        print('ERROR  %-22s %s: %s' % (msg, type(e).__name__, e))
        fails += 1
        continue
    bad = []
    if want and not re.search(want, r, re.S | re.I):
        bad.append('expected /%s/' % want)
    if avoid and re.search(avoid, r, re.S | re.I):
        bad.append('should NOT contain /%s/' % avoid)
    if bad:
        fails += 1
        print('FAIL   %-22s %s' % (msg, '; '.join(bad)))
        print('       got: %s' % r.replace(chr(10), ' | ')[:150])
    else:
        print('ok     %-22s %s' % (msg, r.split(chr(10))[0][:52]))

print()
print('%d/%d passed' % (len(CASES) - fails, len(CASES)))
sys.exit(1 if fails else 0)
