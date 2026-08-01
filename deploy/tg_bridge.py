#!/usr/bin/env python3
"""Telegram bridge - talks in plain language.

You can type naturally:
    buy 200 doge                    how are the bots doing
    buy doge at 0.15                what's main doing
    buy 100 doge at 0.15 sl 0.13    show positions
    sell doge                       any pending orders
    yes / go ahead                  no / cancel

Slash commands still work (/positions /buy /sell /confirm /cancel /orders).

Orders go to the Binance FUTURES TESTNET (play money). Every order previews
first and waits for a confirmation. Only the chat that first messages the bot
is ever served.
"""
import json, os, re, time, hmac, hashlib, urllib.request, urllib.parse, urllib.error

CONF = '/home/bots/telegram/tg.conf'
BASE = 'https://testnet.binancefuture.com'
MAIN_CONFIG = '/home/bots/main/config.py'
NL = chr(10)

BOTS = {
    'main':    ('/home/bots/main',    'positions_binance.json', 'trades_binance.json'),
    'reverse': ('/home/bots/reverse', 'positions_reverse.json', 'trades_reverse.json'),
    'hype':    ('/home/bots/hype',    'positions_hype.json',    'trades_hype.json'),
}
OWNED = {'BTCUSDT': 'main bot', 'SOLUSDT': 'main bot'}
DEFAULT_USD = 100.0

# ── config ────────────────────────────────────────────────────────────────
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
    with urllib.request.urlopen(url, urllib.parse.urlencode(params).encode(),
                                timeout=35) as r:
        return json.loads(r.read())

# ── binance ───────────────────────────────────────────────────────────────
def keys():
    src = open(MAIN_CONFIG, encoding='utf-8').read()
    return (re.search(r'API_KEY\s*=\s*["\']([^"\']+)', src).group(1),
            re.search(r'API_SECRET\s*=\s*["\']([^"\']+)', src).group(1))

def binance(method, path, params=None, auth=False):
    params = dict(params or {}); headers = {}
    if auth:
        k, sec = keys()
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 10000
        q = urllib.parse.urlencode(params)
        params['signature'] = hmac.new(sec.encode(), q.encode(),
                                       hashlib.sha256).hexdigest()
        headers['X-MBX-APIKEY'] = k
    req = urllib.request.Request(
        BASE + path + '?' + urllib.parse.urlencode(params),
        method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:200])

_steps, _ticks, _syms = {}, {}, set()
def _load_filters():
    if _steps:
        return
    for s in binance('GET', '/fapi/v1/exchangeInfo').get('symbols', []):
        if s.get('status') != 'TRADING':
            continue
        _syms.add(s['symbol'])
        for f in s.get('filters', []):
            if f['filterType'] == 'LOT_SIZE':
                _steps[s['symbol']] = float(f['stepSize'])
            elif f['filterType'] == 'PRICE_FILTER':
                _ticks[s['symbol']] = float(f['tickSize'])

def _prec(x):
    xs = ('%f' % x).rstrip('0')
    return len(xs.split('.')[-1]) if '.' in xs else 0

def round_qty(sym, q):
    _load_filters(); st = _steps.get(sym, 0.001)
    return round((q // st) * st, _prec(st))

def round_price(sym, p):
    _load_filters(); tk = _ticks.get(sym, 0.01)
    return round(round(p / tk) * tk, _prec(tk))

def mark(sym):
    return float(binance('GET', '/fapi/v1/ticker/price', {'symbol': sym})['price'])

def my_position(sym):
    for p in binance('GET', '/fapi/v2/positionRisk', auth=True):
        if p['symbol'] == sym and abs(float(p.get('positionAmt', 0))) > 1e-9:
            return float(p['positionAmt']), float(p['entryPrice'])
    return 0.0, 0.0

def resolve_symbol(word):
    """'doge' -> DOGEUSDT. Accepts full symbols, bare bases, 1000-prefixed."""
    w = word.upper().strip()
    _load_filters()
    for cand in (w, w + 'USDT', w + 'USDC', '1000' + w + 'USDT'):
        if cand in _syms:
            return cand
    return None

# ── monitoring ────────────────────────────────────────────────────────────
def jload(p):
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return {}

def fmt_bot(label):
    d, posf, trf = BOTS[label]
    if not os.path.isdir(d):
        return label.upper() + ': not running here'
    pos = jload(os.path.join(d, posf)).get('positions', {})
    tr = jload(os.path.join(d, trf))
    today = time.strftime('%Y-%m-%d')
    tt = [t for t in tr.get('trades', []) if t.get('close_date') == today]
    out = ['*' + label.upper() + '*']
    if pos:
        for s, p in pos.items():
            emo = '🟢' if p.get('pnl_usd', 0) >= 0 else '🔴'
            out.append('%s %s %s  %+.2f%%  $%+.2f' % (
                emo, s, str(p.get('direction', '')).upper(),
                p.get('pnl_pct', 0), p.get('pnl_usd', 0)))
    else:
        out.append('   no open positions')
    out.append('   today: %d closed, $%+.2f' % (
        len(tt), sum(t.get('pnl_usd', 0) for t in tt)))
    out.append('   all time: %dW/%dL  $%+.2f' % (
        tr.get('wins', 0), tr.get('losses', 0), tr.get('total_pnl', 0)))
    return NL.join(out)

def all_bots():
    return (NL + NL).join(fmt_bot(b) for b in BOTS)

# ── trading ───────────────────────────────────────────────────────────────
PENDING = {}

def guard(sym):
    if sym in OWNED:
        return ('⚠️ Heads up: %s is traded by the %s. This order will net '
                'against its live position and desync its book.' % (sym, OWNED[sym]))
    if sym.endswith('USDC'):
        return '⚠️ Heads up: USDC pairs belong to the reverse bot.'
    return None

def preview(chat, action, sym, side, qty, px, limit, sl, warn, extra=''):
    PENDING[chat] = dict(action=action, sym=sym, side=side, qty=qty,
                         limit=limit, sl=sl, ts=time.time())
    kind = ('limit @ %g' % limit) if limit else ('market, now ~%g' % px)
    lines = ['📋 *%s %s*' % (action, sym),
             '   %g units, %s' % (qty, kind),
             '   ≈ $%s' % format(qty * (limit or px), ',.2f')]
    if sl:
        risk = abs((limit or px) - sl) * qty
        lines.append('   🛑 stop-loss %g  (risk ≈ $%s)' % (sl, format(risk, ',.2f')))
    else:
        lines.append('   ⚠️ no stop-loss')
    if extra:
        lines.append('   ' + extra)
    if warn:
        lines += ['', warn]
    lines += ['', 'Reply *yes* to place it, *no* to cancel.']
    return NL.join(lines)

def do_buy(chat, sym, usd=None, limit=None, sl=None):
    usd = DEFAULT_USD if usd is None else usd
    try:
        px = mark(sym)
        if limit: limit = round_price(sym, limit)
        if sl:    sl    = round_price(sym, sl)
        qty = round_qty(sym, usd / (limit or px))
    except Exception as e:
        return "Couldn't price %s: %s" % (sym, e)
    if qty <= 0:
        return '$%g is too small for %s (below the minimum lot).' % (usd, sym)
    if sl and sl >= (limit or px):
        return ('That stop (%g) is above the entry (%g) — for a buy it has to '
                'be below.' % (sl, limit or px))
    extra = ''
    if limit:
        extra = 'market is %g, so this waits %s' % (
            px, 'for a dip' if limit < px else 'for a rise')
    return preview(chat, 'BUY', sym, 'BUY', qty, px, limit, sl, guard(sym), extra)

def do_sell(chat, sym, usd=None, limit=None, sl=None):
    try:
        amt, entry = my_position(sym)
        px = mark(sym)
    except Exception as e:
        return 'Position check failed: %s' % e
    if limit: limit = round_price(sym, limit)
    if abs(amt) > 1e-9:
        side = 'SELL' if amt > 0 else 'BUY'
        qty = abs(amt) if usd is None else min(abs(amt), round_qty(sym, usd / (limit or px)))
        extra = 'closing %s %g from %g — P&L ≈ $%+.2f' % (
            'long' if amt > 0 else 'short', abs(amt), entry, (px - entry) * amt)
        return preview(chat, 'CLOSE', sym, side, qty, px, limit, None,
                       guard(sym), extra)
    usd = DEFAULT_USD if usd is None else usd
    qty = round_qty(sym, usd / (limit or px))
    if qty <= 0:
        return '$%g is too small for %s.' % (usd, sym)
    if sl: sl = round_price(sym, sl)
    return preview(chat, 'SHORT', sym, 'SELL', qty, px, limit, sl, guard(sym),
                   "you don't hold this — so this OPENS a short")

def do_confirm(chat):
    p = PENDING.pop(chat, None)
    if not p:
        return "Nothing waiting. Tell me what to buy or sell first."
    if time.time() - p['ts'] > 90:
        return 'That preview expired. Ask me again.'
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
        return '❌ Exchange rejected it: %s' % e
    msg = ['✅ %s %s — %g units%s (order #%s)' % (
        'Placed' if p['limit'] else 'Filled', p['sym'], p['qty'],
        (' @ %g' % p['limit']) if p['limit'] else '', r.get('orderId'))]
    # attach the stop-loss as a separate reduce-only STOP_MARKET
    if p.get('sl') and p['action'] in ('BUY', 'SHORT'):
        try:
            close_side = 'SELL' if p['side'] == 'BUY' else 'BUY'
            s = binance('POST', '/fapi/v1/order', {
                'symbol': p['sym'], 'side': close_side, 'type': 'STOP_MARKET',
                'stopPrice': p['sl'], 'closePosition': 'true'}, auth=True)
            msg.append('🛑 Stop-loss set at %g (#%s)' % (p['sl'], s.get('orderId')))
        except Exception as e:
            msg.append('⚠️ Order went through but the STOP-LOSS FAILED: %s' % e)
            msg.append('   The position is unprotected — set it manually.')
    elif p['action'] in ('BUY', 'SHORT'):
        msg.append('⚠️ No stop-loss — this runs until you close it.')
    return NL.join(msg)

def do_orders():
    try:
        o = binance('GET', '/fapi/v1/openOrders', auth=True)
    except Exception as e:
        return "Couldn't fetch orders: %s" % e
    if not o:
        return 'No pending orders.'
    out = ['*Pending orders*']
    for x in o:
        out.append('   #%s %s %s %s %s @ %s' % (
            x['orderId'], x['symbol'], x['side'], x['type'],
            x['origQty'], x.get('stopPrice') or x.get('price') or '-'))
    out.append('')
    out.append('Say "cancel orders on doge" to clear one.')
    return NL.join(out)

def do_cancel_orders(sym):
    try:
        binance('DELETE', '/fapi/v1/allOpenOrders', {'symbol': sym}, auth=True)
        return 'Cleared all pending orders on %s.' % sym
    except Exception as e:
        return 'Cancel failed: %s' % e

# ── natural language ──────────────────────────────────────────────────────
YES = {'yes', 'y', 'yeah', 'yep', 'ok', 'okay', 'go', 'go ahead', 'do it',
       'confirm', 'sure', 'proceed', 'send it', 'buy it', 'sell it', 'correct'}
NO  = {'no', 'n', 'nope', 'cancel', 'stop', 'abort', 'nevermind', 'never mind',
       'dont', "don't", 'wait'}

NUM = r'(\d+(?:\.\d+)?)'

def parse(chat, text):
    t = ' '.join((text or '').lower().split())
    if not t:
        return None
    bare = t.lstrip('/')

    if bare in YES or t in YES:
        return do_confirm(chat)
    if bare in NO or t in NO:
        return 'Cancelled.' if PENDING.pop(chat, None) else 'Nothing to cancel.'
    if bare in ('start', 'help', 'commands') or 'what can you do' in t:
        return HELP

    # status questions
    if re.search(r'\b(order|pending)s?\b', t) and not re.search(r'\b(buy|sell)\b', t):
        m = re.search(r'cancel.*\bon\s+([a-z0-9]+)', t)
        if m or 'cancel' in t:
            w = (m.group(1) if m else '')
            sym = resolve_symbol(w) if w else None
            if sym: return do_cancel_orders(sym)
        return do_orders()
    for name in BOTS:
        if re.search(r'\b' + name + r'\b', t):
            return fmt_bot(name)
    if re.search(r'\b(position|status|doing|pnl|p&l|profit|balance|how are|'
                 r'summary|report|show|update)\b', t):
        return all_bots()

    # trading intent
    sell = bool(re.search(r'\b(sell|close|exit|dump|short|offload)\b', t))
    buy  = bool(re.search(r'\b(buy|long|purchase|grab|enter|get)\b', t))
    if not (buy or sell):
        return None

    usd = None
    m = re.search(r'(?:\$|usd\s*)' + NUM, t) or re.search(NUM + r'\s*(?:usd|dollar)', t)
    if m: usd = float(m.group(1))

    limit = None
    m = re.search(r'(?:@|\bat\b|\bwhen it hits\b|\bprice\b)\s*' + NUM, t)
    if m: limit = float(m.group(1))

    sl = None
    m = re.search(r'\b(?:sl|stop(?:\s*loss)?)\b\s*(?:at\s*)?' + NUM, t)
    if m: sl = float(m.group(1))

    # symbol: try every word, skipping ones already consumed as numbers
    used = {str(x) for x in (usd, limit, sl) if x is not None}
    sym = None
    for w in re.findall(r'[a-z0-9]+', t):
        if w in used or w.isdigit():
            continue
        if w in ('buy', 'sell', 'close', 'long', 'short', 'at', 'sl', 'stop',
                 'loss', 'usd', 'dollar', 'dollars', 'of', 'worth', 'the',
                 'exit', 'dump', 'get', 'me', 'please', 'and', 'with', 'a'):
            continue
        sym = resolve_symbol(w)
        if sym:
            break
    if not sym:
        return ("I couldn't work out which coin you mean. Try something like "
                '"buy 200 doge" or "sell btc".')

    # a bare number with no $ is treated as the size
    if usd is None:
        for n in re.findall(NUM, t):
            if limit is not None and float(n) == limit: continue
            if sl is not None and float(n) == sl: continue
            usd = float(n); break

    return (do_sell if sell else do_buy)(chat, sym, usd, limit, sl)

HELP = NL.join([
    '👋 Talk to me normally. Examples:',
    '',
    '*Checking in*',
    '   how are the bots doing',
    '   show positions',
    "   what's main doing",
    '   any pending orders',
    '',
    '*Trading* (futures testnet, default $%g)' % DEFAULT_USD,
    '   buy 200 doge',
    '   buy doge at 0.15',
    '   buy 100 doge at 0.15 sl 0.13',
    '   sell doge',
    '   cancel orders on doge',
    '',
    'I always show a preview first — reply *yes* to place it, *no* to cancel.',
])

def handle(chat, text):
    try:
        r = parse(chat, text)
    except Exception as e:
        return 'Something went wrong: %s' % e
    return r or ("Not sure what you meant. Try \"show positions\" or "
                 "\"buy 100 doge\" — or say *help*.")

def main():
    print('waiting for token...', flush=True)
    while True:
        token = conf().get('TOKEN', '')
        if token: break
        time.sleep(30)
    print('polling', flush=True)
    offset = 0
    while True:
        try:
            for u in tg(token, 'getUpdates', offset=offset, timeout=30).get('result', []):
                offset = u['update_id'] + 1
                msg = u.get('message') or {}
                chat = str(msg.get('chat', {}).get('id', ''))
                if not chat: continue
                bound = conf().get('CHAT', '')
                if not bound:
                    save_conf(CHAT=chat); bound = chat
                if chat != bound: continue
                tg(token, 'sendMessage', chat_id=chat, parse_mode='Markdown',
                   text=handle(chat, msg.get('text', ''))[:4000])
        except Exception as e:
            print('poll error: %s' % e, flush=True)
            time.sleep(10)

if __name__ == '__main__':
    main()
