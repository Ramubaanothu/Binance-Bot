#!/usr/bin/env python3
"""Telegram bridge for the trading bots.

Answers on demand — it never trades, never touches keys, only READS each
bot's state files and replies with a summary.

Commands:
  /positions          open positions across every bot
  /main /reverse /hype  one bot in detail (balance, open, today, last trades)
  /all                everything
  /start /help        list commands

Setup: put the bot token in /home/bots/telegram/tg.conf as
    TOKEN=123456:ABC-...
On the FIRST /start it binds to that chat id (written to the same file) and
from then on ignores every other chat — so only the person who claimed it
first can query it.
"""
import json, os, time, urllib.request, urllib.parse

CONF = '/home/bots/telegram/tg.conf'
BOTS = {   # label -> (directory, positions file, trades file)
    'main':    ('/home/bots/main',    'positions_binance.json', 'trades_binance.json'),
    'reverse': ('/home/bots/reverse', 'positions_reverse.json', 'trades_reverse.json'),
    'hype':    ('/home/bots/hype',    'positions_hype.json',    'trades_hype.json'),
}

def conf():
    d = {}
    if os.path.exists(CONF):
        for ln in open(CONF):
            if '=' in ln:
                k, v = ln.split('=', 1)
                d[k.strip()] = v.strip()
    return d

def save_chat(chat_id):
    c = conf(); c['CHAT'] = str(chat_id)
    with open(CONF, 'w') as f:
        for k, v in c.items():
            f.write(f'{k}={v}\n')

def api(token, method, **params):
    url = f'https://api.telegram.org/bot{token}/{method}'
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data, timeout=35) as r:
        return json.loads(r.read())

def jload(path):
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return {}

def fmt_bot(label):
    d, posf, trf = BOTS[label]
    if not os.path.isdir(d):
        return f'{label.upper()}: not deployed on this server'
    pos = jload(os.path.join(d, posf)).get('positions', {})
    tr  = jload(os.path.join(d, trf))
    trades = tr.get('trades', [])
    today  = time.strftime('%Y-%m-%d')
    tt     = [t for t in trades if t.get('close_date') == today]
    day    = sum(t.get('pnl_usd', 0) for t in tt)
    out = [f'━ {label.upper()} ━']
    if pos:
        for s, p in pos.items():
            out.append(f"  {s} {p.get('direction','?').upper()} "
                       f"{p.get('pnl_pct',0):+.2f}% (${p.get('pnl_usd',0):+.2f}) "
                       f"e:{p.get('entry',0):g}")
    else:
        out.append('  no open positions')
    out.append(f"  today: {len(tt)} closed, ${day:+.2f} | "
               f"total: {tr.get('wins',0)}W/{tr.get('losses',0)}L "
               f"${tr.get('total_pnl',0):+.2f}")
    return '\n'.join(out)

def handle(text):
    t = (text or '').strip().lower()
    if t in ('/start', '/help'):
        return ('Bot monitor. Commands:\n/positions - open positions, all bots\n'
                '/main /reverse /hype - one bot in detail\n/all - everything')
    if t == '/positions' or t == '/all':
        return '\n\n'.join(fmt_bot(b) for b in BOTS)
    key = t.lstrip('/')
    if key in BOTS:
        return fmt_bot(key)
    return 'Unknown command. /help for the list.'

def main():
    print('tg_bridge waiting for token...', flush=True)
    while True:                                # wait until the user adds a token
        token = conf().get('TOKEN', '')
        if token: break
        time.sleep(30)
    print('token found, polling', flush=True)
    offset = 0
    while True:
        try:
            r = api(token, 'getUpdates', offset=offset, timeout=30)
            for u in r.get('result', []):
                offset = u['update_id'] + 1
                msg = u.get('message') or {}
                chat = str(msg.get('chat', {}).get('id', ''))
                if not chat:
                    continue
                bound = conf().get('CHAT', '')
                if not bound:
                    save_chat(chat)            # first /start claims the bot
                    bound = chat
                if chat != bound:
                    continue                   # everyone else is ignored
                api(token, 'sendMessage', chat_id=chat,
                    text=handle(msg.get('text', ''))[:4000])
        except Exception as e:
            print(f'poll error: {e}', flush=True)
            time.sleep(10)

if __name__ == '__main__':
    main()
