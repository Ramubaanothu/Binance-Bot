#!/usr/bin/env python3
"""Telegram bridge: monitor the bots and place manual orders on the Binance
FUTURES TESTNET (play money). Answers only the chat that first claims it.

Monitoring
  /positions /all        every bot: open positions, today, lifetime
  /main /reverse /hype   one bot in detail
  /orders                resting limit orders

Trading  (size defaults to DEFAULT_USD when omitted)
  /buy  SYMBOL                 market buy, default size
  /buy  SYMBOL 250             market buy, $250
  /buy  SYMBOL @0.15           LIMIT buy at 0.15, default size
  /buy  SYMBOL 250 @0.15       LIMIT buy at 0.15, $250
  /sell SYMBOL [USD] [@price]  close a position, or open a short if flat
  /confirm                     execute the preview (expires after 60s)
  /cancel                      drop the preview
  /cancelorder SYMBOL [ID]     cancel resting order(s)

Safety
  - only the bound chat is served; the first /start claims it
  - every order previews first and needs an explicit /confirm
  - symbols the bots trade are allowed but WARN loudly: the account is
    one-way, so a manual order nets against the bot's live position
  - manual orders carry no stop-loss, and the receipt says so
"""
import json, os, re, time, hmac, hashlib, urllib.request, urllib.parse, urllib.error

CONF = '/home/bots/telegram/tg.conf'
BASE = 'https://testnet.binancefuture.com'
MAIN_CONFIG = '/home/bots/main/config.py'

BOTS = {
    'main':    ('/home/bots/main',    'positions_binance.json', 'trades_binance.json'),
    'reverse': ('/home/bots/reverse', 'positions_reverse.json', 'trades_reverse.json'),
    'hype':    ('/home/bots/hype',    'positions_hype.json',    'trades_hype.json'),
}
OWNED = {'BTCUSDT': 'main bot', 'SOLUSDT': 'main bot'}
DEFAULT_USD = 100.0

NL = chr(10)

# ── config / telegram ─────────────────────────────────────────────────────
def conf():
    d = {}
    if os.path.exists(CONF):
        for ln in open(CONF):
            if '=' in ln:
                k, v = ln.split('=', 1)
                d[k.strip()] = v.strip()
    return d

def save_conf(**kv):
    c = conf(); c.update({k: str(v) for k, v in kv.items()})
    with open(CONF, 'w') as f:
        for k, v in c.items():
            f.write(k + '=' + v + NL)

def tg(token, method, **params):
    url = 'https://api.telegram.org/bot' + token + '/' + method
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data, timeout=35) as r:
        return json.loads(r.read())

# ── binance ───────────────────────────────────────────────────────────────
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
    req = urllib.request.Request(BASE + path + '?' + q, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:200])

_steps, _ticks = {}, {}
def _load_filters():
    if _steps:
        return
    for s in binance('GET', '/fapi/v1/exchangeInfo').get('symbols', []):
        for f in s.get('filters', []):
            if f['filterType'] == 'LOT_SIZE':
                _steps[s['symbol']] = float(f['stepSize'])
            elif f['filterType'] == 'PRICE_FILTER':
                _ticks[s['symbol']] = float(f['tickSize'])

def _prec(x):
    xs = ('%f' % x).rstrip('0')
    return len(xs.split('.')[-1]) if '.' in xs else 0

def round_qty(sym, qty):
    _load_filters()
    stp = _steps.get(sym, 0.001)
    return round((qty // stp) * stp, _prec(stp))

def round_price(sym, px):
    _load_filters()
    tk = _ticks.get(sym, 0.01)
    return round(round(px / tk) * tk, _prec(tk))

def mark(sym):
    return float(binance('GET', '/fapi/v1/ticker/price', {'symbol': sym})['price'])

def my_position(sym):
    for p in binance('GET', '/fapi/v2/positionRisk', auth=True):
        if p['symbol'] == sym and abs(float(p.get('positionAmt', 0))) > 1e-9:
            return float(p['positionAmt']), float(p['entryPrice'])
    return 0.0, 0.0

# ── monitoring ────────────────────────────────────────────────────────────
def jload(p):
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return {}

def fmt_bot(label):
    d, posf, trf = BOTS[label]
    if not os.path.isdir(d):
        return label.upper() + ': not deployed on this server'
    pos = jload(os.path.join(d, posf)).get('positions', {})
    tr = jload(os.path.join(d, trf))
    trades = tr.get('trades', [])
    today = time.strftime('%Y-%m-%d')
    tt = [t for t in trades if t.get('close_date') == today]
    out = ['- ' + label.upper() + ' -']
    if pos:
        for s, p in pos.items():
            out.append('  %s %s %+.2f%% ($%+.2f) e:%g' % (
                s, str(p.get('direction', '?')).upper(),
                p.get('pnl_pct', 0), p.get('pnl_usd', 0), p.get('entry', 0)))
    else:
        out.append('  no open positions')
    out.append('  today: %d closed, $%+.2f | total: %dW/%dL $%+.2f' % (
        len(tt), sum(t.get('pnl_usd', 0) for t in tt),
        tr.get('wins', 0), tr.get('losses', 0), tr.get('total_pnl', 0)))
    return NL.join(out)

# ── trading ───────────────────────────────────────────────────────────────
PENDING = {}

def guard_symbol(sym):
    """(fatal, warning). Bot symbols warn rather than block: it is the user's
    own testnet account and the preview states the consequence."""
    if not (sym.endswith('USDT') or sym.endswith('USDC')):
        return 'Only USDT/USDC perpetual symbols exist on this venue.', None
    if sym in OWNED:
        return None, ('WARNING: %s is traded by the %s. One-way mode means '
                      'this order NETS against its live position - that book '
                      'will desync until it reconciles.' % (sym, OWNED[sym]))
    if sym.endswith('USDC'):
        return None, ('WARNING: USDC pairs are the reverse bot universe. This '
                      'may net against a position it holds.')
    return None, None

def _preview(chat, action, sym, side, qty, px, limit, warn, extra=''):
    PENDING[chat] = dict(action=action, sym=sym, side=side, qty=qty,
                         limit=limit, ts=time.time())
    kind = ('LIMIT @ %g' % limit) if limit else ('MARKET (~%g)' % px)
    lines = ['PREVIEW - %s %s' % (action, sym),
             '  qty %g   %s   ~$%s' % (qty, kind, format(qty * (limit or px), ',.2f')),
             '  venue: FUTURES TESTNET, no stop-loss attached']
    if extra:
        lines.append('  ' + extra)
    if warn:
        lines.append('')
        lines.append(warn)
    lines.append('')
    lines.append('Reply /confirm within 60s, or /cancel.')
    return NL.join(lines)

def cmd_buy(chat, sym, usd=None, limit=None):
    err, warn = guard_symbol(sym)
    if err:
        return err
    usd = DEFAULT_USD if usd is None else usd
    try:
        px = mark(sym)
        if limit:
            limit = round_price(sym, limit)
        qty = round_qty(sym, usd / (limit or px))
    except Exception as e:
        return 'Could not price %s: %s' % (sym, e)
    if qty <= 0:
        return '$%g is below the minimum lot for %s.' % (usd, sym)
    extra = ''
    if limit:
        extra = 'mark is %g - your limit sits %s it' % (
            px, 'below' if limit < px else 'above')
    return _preview(chat, 'BUY', sym, 'BUY', qty, px, limit, warn, extra)

def cmd_sell(chat, sym, usd=None, limit=None):
    err, warn = guard_symbol(sym)
    if err:
        return err
    try:
        amt, entry = my_position(sym)
        px = mark(sym)
    except Exception as e:
        return 'Position check failed: %s' % e
    if limit:
        limit = round_price(sym, limit)
    if abs(amt) > 1e-9:
        side = 'SELL' if amt > 0 else 'BUY'
        qty = abs(amt) if usd is None else min(abs(amt),
                                               round_qty(sym, usd / (limit or px)))
        extra = '%s %g @ e:%g, now %g  (P&L ~$%+.2f)' % (
            'long' if amt > 0 else 'short', abs(amt), entry, px, (px - entry) * amt)
        return _preview(chat, 'CLOSE', sym, side, qty, px, limit, warn, extra)
    usd = DEFAULT_USD if usd is None else usd
    qty = round_qty(sym, usd / (limit or px))
    if qty <= 0:
        return '$%g is below the minimum lot for %s.' % (usd, sym)
    return _preview(chat, 'SHORT', sym, 'SELL', qty, px, limit, warn,
                    'no existing position - this OPENS a short')

def cmd_confirm(chat):
    p = PENDING.pop(chat, None)
    if not p:
        return 'Nothing pending. /buy or /sell first.'
    if time.time() - p['ts'] > 60:
        return 'Pending order expired (60s). Start again.'
    params = {'symbol': p['sym'], 'side': p['side'], 'quantity': p['qty']}
    if p['limit']:
        params.update({'type': 'LIMIT', 'price': p['limit'], 'timeInForce': 'GTC'})
    else:
        params['type'] = 'MARKET'
    if p['action'] == 'CLOSE':
        params['reduceOnly'] = 'true'
    try:
        r = binance('POST', '/fapi/v1/order', params, auth=True)
    except Exception as e:
        return 'Order REJECTED by exchange: %s' % e
    kind = 'Resting LIMIT' if p['limit'] else 'Filled MARKET'
    msg = '%s order placed: %s %s qty %g%s (#%s)' % (
        kind, p['action'], p['sym'], p['qty'],
        (' @ %g' % p['limit']) if p['limit'] else '', r.get('orderId'))
    if p['action'] in ('BUY', 'SHORT'):
        msg += NL + 'NOTE: no stop-loss - unmanaged until you close it.'
    return msg

def cmd_orders():
    try:
        o = binance('GET', '/fapi/v1/openOrders', auth=True)
    except Exception as e:
        return 'Could not list orders: %s' % e
    if not o:
        return 'No resting orders.'
    out = ['Resting orders:']
    for x in o:
        out.append('  #%s %s %s %s qty %s @ %s' % (
            x['orderId'], x['symbol'], x['side'], x['type'],
            x['origQty'], x.get('price', '-')))
    out.append('')
    out.append('/cancelorder SYMBOL [ID]')
    return NL.join(out)

def cmd_cancel_order(sym, oid=None):
    try:
        if oid:
            binance('DELETE', '/fapi/v1/order',
                    {'symbol': sym, 'orderId': oid}, auth=True)
            return 'Cancelled #%s on %s.' % (oid, sym)
        binance('DELETE', '/fapi/v1/allOpenOrders', {'symbol': sym}, auth=True)
        return 'Cancelled all resting orders on %s.' % sym
    except Exception as e:
        return 'Cancel failed: %s' % e

HELP = NL.join([
    'Monitor:',
    '/positions /all - every bot',
    '/main /reverse /hype - one bot',
    '/orders - resting limit orders',
    '',
    'Trade (futures TESTNET, default $%g):' % DEFAULT_USD,
    '/buy SYMBOL            market, default size',
    '/buy SYMBOL 250        market, $250',
    '/buy SYMBOL @0.15      limit at 0.15',
    '/buy SYMBOL 250 @0.15  limit, $250',
    '/sell SYMBOL [USD] [@price]',
    '/confirm  /cancel',
    '/cancelorder SYMBOL [ID]',
])

ARG = re.compile(r'^/(buy|sell)\s+([A-Za-z0-9]+)'
                 r'(?:\s+(\d+(?:\.\d+)?))?'
                 r'(?:\s*@\s*(\d+(?:\.\d+)?))?\s*$', re.I)

def handle(chat, text):
    t = (text or '').strip()
    low = t.lower()
    if low in ('/start', '/help'):
        return HELP
    if low in ('/positions', '/all'):
        return (NL + NL).join(fmt_bot(b) for b in BOTS)
    if low.lstrip('/') in BOTS:
        return fmt_bot(low.lstrip('/'))
    if low == '/orders':
        return cmd_orders()
    if low == '/confirm':
        return cmd_confirm(chat)
    if low == '/cancel':
        return 'Cancelled.' if PENDING.pop(chat, None) else 'Nothing pending.'
    m = re.match(r'^/cancelorder\s+([A-Za-z0-9]+)(?:\s+(\d+))?$', t, re.I)
    if m:
        return cmd_cancel_order(m.group(1).upper(), m.group(2))
    m = ARG.match(t)
    if m:
        cmd, sym = m.group(1).lower(), m.group(2).upper()
        usd = float(m.group(3)) if m.group(3) else None
        lim = float(m.group(4)) if m.group(4) else None
        return (cmd_buy if cmd == 'buy' else cmd_sell)(chat, sym, usd, lim)
    return 'Unknown command. /help for the list.'

def main():
    print('tg_bridge waiting for token...', flush=True)
    while True:
        token = conf().get('TOKEN', '')
        if token:
            break
        time.sleep(30)
    print('token found, polling', flush=True)
    offset = 0
    while True:
        try:
            r = tg(token, 'getUpdates', offset=offset, timeout=30)
            for u in r.get('result', []):
                offset = u['update_id'] + 1
                msg = u.get('message') or {}
                chat = str(msg.get('chat', {}).get('id', ''))
                if not chat:
                    continue
                bound = conf().get('CHAT', '')
                if not bound:
                    save_conf(CHAT=chat)
                    bound = chat
                if chat != bound:
                    continue
                tg(token, 'sendMessage', chat_id=chat,
                   text=handle(chat, msg.get('text', ''))[:4000])
        except Exception as e:
            print('poll error: %s' % e, flush=True)
            time.sleep(10)

if __name__ == '__main__':
    main()
