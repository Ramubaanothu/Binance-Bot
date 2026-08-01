#!/usr/bin/env python3
"""Telegram bridge for the trading bots: read-only monitoring PLUS manual
buy/sell on the Binance FUTURES TESTNET (play money), commanded by the bound
chat only.

Monitoring:
  /positions /all       open positions + today across every bot
  /main /reverse /hype  one bot in detail

Manual trading (futures testnet, the same demo wallet the bots use):
  /buy SYMBOL USD       e.g. /buy DOGEUSDT 100   -> preview, then /confirm
  /sell SYMBOL          close YOUR position on that symbol -> /confirm
  /confirm              execute the pending order (expires in 60s)
  /cancel               drop the pending order

Safety:
  - only the FIRST chat to /start is ever answered (bound in tg.conf)
  - every order needs an explicit /confirm within 60 seconds
  - symbols the automated bots OWN are blocked: the account is in one-way
    position mode, so a manual order on their symbol would NET against their
    live position and corrupt both books
  - USDC pairs are blocked entirely (the reverse bot's whole universe)
"""
import json, os, re, time, hmac, hashlib, urllib.request, urllib.parse

CONF = '/home/bots/telegram/tg.conf'
BASE = 'https://testnet.binancefuture.com'
MAIN_CONFIG = '/home/bots/main/config.py'

BOTS = {
    'main':    ('/home/bots/main',    'positions_binance.json', 'trades_binance.json'),
    'reverse': ('/home/bots/reverse', 'positions_reverse.json', 'trades_reverse.json'),
    'hype':    ('/home/bots/hype',    'positions_hype.json',    'trades_hype.json'),
}
# Symbols the automated bots own — manual orders here would net against them.
OWNED = {'BTCUSDT', 'SOLUSDT'}

# ── config / telegram plumbing ────────────────────────────────────────────
def conf():
    d = {}
    if os.path.exists(CONF):
        for ln in open(CONF):
            if '=' in ln:
                k, v = ln.split('=', 1); d[k.strip()] = v.strip()
    return d

def save_conf(**kv):
    c = conf(); c.update({k: str(v) for k, v in kv.items()})
    with open(CONF, 'w') as f:
        for k, v in c.items(): f.write(f'{k}={v}\n')

def tg(token, method, **params):
    url = f'https://api.telegram.org/bot{token}/{method}'
    with urllib.request.urlopen(url, urllib.parse.urlencode(params).encode(),
                                timeout=35) as r:
        return json.loads(r.read())

# ── binance futures testnet client (keys read from the main bot's config) ─
def keys():
    src = open(MAIN_CONFIG, encoding='utf-8').read()
    k = re.search(r'API_KEY\s*=\s*["\']([^"\']+)', src).group(1)
    s = re.search(r'API_SECRET\s*=\s*["\']([^"\']+)', src).group(1)
    return k, s

def binance(method, path, params=None, auth=False):
    params = dict(params or {})
    headers = {}
    if auth:
        k, sec = keys()
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 10000
        q = urllib.parse.urlencode(params)
        params['signature'] = hmac.new(sec.encode(), q.encode(),
                                       hashlib.sha256).hexdigest()
        headers['X-MBX-APIKEY'] = k
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f'{BASE}{path}?{q}', method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:200])

_steps = {}
def step_of(sym):
    global _steps
    if not _steps:
        for s in binance('GET', '/fapi/v1/exchangeInfo').get('symbols', []):
            for f in s.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    _steps[s['symbol']] = float(f['stepSize'])
    return _steps.get(sym, 0.001)

def round_qty(sym, qty):
    stp = step_of(sym)
    prec = max(0, len(str(stp).rstrip('0').split('.')[-1])) if '.' in str(stp) else 0
    return round((qty // stp) * stp, prec)

def mark(sym):
    return float(binance('GET', '/fapi/v1/ticker/price', {'symbol': sym})['price'])

def my_position(sym):
    for p in binance('GET', '/fapi/v2/positionRisk', auth=True):
        if p['symbol'] == sym and abs(float(p.get('positionAmt', 0))) > 1e-9:
            return float(p['positionAmt']), float(p['entryPrice'])
    return 0.0, 0.0

# ── monitoring ────────────────────────────────────────────────────────────
def jload(p):
    try: return json.load(open(p, encoding='utf-8'))
    except Exception: return {}

def fmt_bot(label):
    d, posf, trf = BOTS[label]
    if not os.path.isdir(d):
        return f'{label.upper()}: not deployed on this server'
    pos = jload(os.path.join(d, posf)).get('positions', {})
    tr  = jload(os.path.join(d, trf)); trades = tr.get('trades', [])
    today = time.strftime('%Y-%m-%d')
    tt = [t for t in trades if t.get('close_date') == today]
    out = [f'━ {label.upper()} ━']
    if pos:
        for s, p in pos.items():
            out.append(f"  {s} {p.get('direction','?').upper()} "
                       f"{p.get('pnl_pct',0):+.2f}% (${p.get('pnl_usd',0):+.2f}) "
                       f"e:{p.get('entry',0):g}")
    else:
        out.append('  no open positions')
    out.append(f"  today: {len(tt)} closed, ${sum(t.get('pnl_usd',0) for t in tt):+.2f}"
               f" | total: {tr.get('wins',0)}W/{tr.get('losses',0)}L "
               f"${tr.get('total_pnl',0):+.2f}")
    return '\n'.join(out)

# ── manual trading with confirm step ──────────────────────────────────────
PENDING = {}          # chat -> dict(action, sym, side, qty, usd, ts)

def guard_symbol(sym):
    if sym in OWNED:
        return (f'{sym} is OWNED by the automated bots. The account is in '
                'one-way mode — a manual order would net against their live '
                'position and corrupt both books. Blocked.')
    if sym.endswith('USDC'):
        return 'USDC pairs are the reverse bot\'s universe — blocked for the same reason.'
    if not sym.endswith('USDT'):
        return 'Only USDT perpetual symbols are supported (futures testnet).'
    return None

def cmd_buy(chat, sym, usd):
    err = guard_symbol(sym)
    if err: return err
    try:
        px  = mark(sym)
        qty = round_qty(sym, usd / px)
    except Exception as e:
        return f'Could not price {sym}: {e}'
    if qty <= 0:
        return f'${usd:g} is below the minimum lot for {sym}.'
    PENDING[chat] = dict(action='BUY', sym=sym, side='BUY', qty=qty,
                         usd=usd, ts=time.time())
    return (f'PREVIEW — BUY {sym}\n  qty {qty:g} @ ~{px:g}  (~${qty*px:,.2f})\n'
            f'  venue: FUTURES TESTNET, 1x market order, no stop-loss\n'
            f'Reply /confirm within 60s to execute, /cancel to drop.')

def cmd_sell(chat, sym):
    err = guard_symbol(sym)
    if err: return err
    try:
        amt, entry = my_position(sym)
    except Exception as e:
        return f'Position check failed: {e}'
    if abs(amt) < 1e-9:
        return f'No open position on {sym}.'
    side = 'SELL' if amt > 0 else 'BUY'
    PENDING[chat] = dict(action='CLOSE', sym=sym, side=side, qty=abs(amt),
                         usd=0, ts=time.time())
    px = mark(sym)
    pnl = (px - entry) * amt
    return (f'PREVIEW — CLOSE {sym}\n  {"long" if amt>0 else "short"} {abs(amt):g} '
            f'@ e:{entry:g}, now {px:g}  (P&L ~${pnl:+,.2f})\n'
            f'Reply /confirm within 60s to execute, /cancel to drop.')

def cmd_confirm(chat):
    p = PENDING.pop(chat, None)
    if not p:
        return 'Nothing pending. /buy SYMBOL USD or /sell SYMBOL first.'
    if time.time() - p['ts'] > 60:
        return 'Pending order expired (60s). Start again.'
    params = {'symbol': p['sym'], 'side': p['side'], 'type': 'MARKET',
              'quantity': p['qty']}
    if p['action'] == 'CLOSE':
        params['reduceOnly'] = 'true'
    try:
        r = binance('POST', '/fapi/v1/order', params, auth=True)
    except Exception as e:
        return f'Order REJECTED by exchange: {e}'
    return (f"EXECUTED {p['action']} {p['sym']} qty {p['qty']:g} "
            f"(order #{r.get('orderId')}).\n"
            + ('Note: NO stop-loss was placed — this position is unmanaged '
               'until you /sell it.' if p['action'] == 'BUY' else ''))

HELP = ('Monitor:\n/positions /all - every bot\n/main /reverse /hype - detail\n\n'
        'Manual trading (futures TESTNET):\n'
        '/buy SYMBOL USD  e.g. /buy DOGEUSDT 100\n'
        '/sell SYMBOL - close your position\n/confirm - execute pending\n'
        '/cancel - drop pending\n\n'
        'BTCUSDT/SOLUSDT and all USDC pairs are blocked (bot-owned).')

def handle(chat, text):
    t = (text or '').strip()
    low = t.lower()
    if low in ('/start', '/help'): return HELP
    if low in ('/positions', '/all'): return '\n\n'.join(fmt_bot(b) for b in BOTS)
    if low.lstrip('/') in BOTS: return fmt_bot(low.lstrip('/'))
    if low == '/confirm': return cmd_confirm(chat)
    if low == '/cancel':
        return 'Cancelled.' if PENDING.pop(chat, None) else 'Nothing pending.'
    m = re.match(r'/buy\s+([A-Za-z0-9]+)\s+(\d+(?:\.\d+)?)$', t, re.I)
    if m: return cmd_buy(chat, m.group(1).upper(), float(m.group(2)))
    m = re.match(r'/sell\s+([A-Za-z0-9]+)$', t, re.I)
    if m: return cmd_sell(chat, m.group(1).upper())
    return 'Unknown command. /help for the list.'

def main():
    print('tg_bridge waiting for token...', flush=True)
    while True:
        token = conf().get('TOKEN', '')
        if token: break
        time.sleep(30)
    print('token found, polling', flush=True)
    offset = 0
    while True:
        try:
            for u in tg(token, 'getUpdates', offset=offset, timeout=30).get('result', []):
                offset = u['update_id'] + 1
                msg  = u.get('message') or {}
                chat = str(msg.get('chat', {}).get('id', ''))
                if not chat: continue
                bound = conf().get('CHAT', '')
                if not bound:
                    save_conf(CHAT=chat); bound = chat
                if chat != bound: continue
                tg(token, 'sendMessage', chat_id=chat,
                   text=handle(chat, msg.get('text', ''))[:4000])
        except Exception as e:
            print(f'poll error: {e}', flush=True); time.sleep(10)

if __name__ == '__main__':
    main()
