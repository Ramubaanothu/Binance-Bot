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
import json, os, re, time, hmac, hashlib, subprocess
import urllib.request, urllib.parse, urllib.error

CONF = '/home/bots/telegram/tg.conf'
BASE = 'https://testnet.binancefuture.com'
MAIN_CONFIG = '/home/bots/main/config.py'
NL = chr(10)

BOTS = {
    'main':    ('/home/bots/main',    'positions_binance.json', 'trades_binance.json'),
    'reverse': ('/home/bots/reverse', 'positions_reverse.json', 'trades_reverse.json'),
    'spot':    ('/home/bots/spot',    'positions_spot.json',    'trades_spot.json'),
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

# ── keyboards ─────────────────────────────────────────────────────────────
def _kb(rows, once=False):
    return json.dumps({'keyboard': [[{'text': b} for b in r] for r in rows],
                       'resize_keyboard': True,
                       'one_time_keyboard': once})

# The everyday menu. Kept to six so the buttons stay wide enough to read.
MENU = _kb([['\U0001F4CA Positions',  '\U0001F4BC Balance'],
            ['\U0001F4C5 Today',      '\U0001F4C6 Yesterday'],
            ['\U0001F4C8 Report',     '\U0001F4CB Orders'],
            ['\U0001F5A5 Server',     '\u2753 Help']])

# Shown ONLY while an order is waiting. Deliberately a different shape to
# the menu, so 'yes' never lands where 'Positions' just was.
CONFIRM = _kb([['\u2705 YES, place it', '\u274C Cancel']], once=True)

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

# Demo Trading is one account, one key, two hosts. Everything below is
# keyed by venue so a spot order never lands on the futures book.
SPOT_BASE = 'https://demo-api.binance.com'
VENUE = {
    'fut':  dict(base=BASE, ex='/fapi/v1/exchangeInfo',
                 tick='/fapi/v1/ticker/price', order='/fapi/v1/order',
                 open='/fapi/v1/openOrders', cancel='/fapi/v1/allOpenOrders'),
    'spot': dict(base=SPOT_BASE, ex='/api/v3/exchangeInfo',
                 tick='/api/v3/ticker/price', order='/api/v3/order',
                 open='/api/v3/openOrders', cancel='/api/v3/openOrders'),
}

def binance(method, path, params=None, auth=False, venue='fut'):
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
        VENUE[venue]['base'] + path + '?' + urllib.parse.urlencode(params),
        method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:200])

_F = {v: dict(steps={}, ticks={}, minnot={}, base={}, syms=set())
      for v in ('fut', 'spot')}

def _load_filters(venue='fut'):
    c = _F[venue]
    if c['steps']:
        return c
    for s in binance('GET', VENUE[venue]['ex'], venue=venue).get('symbols', []):
        if s.get('status') != 'TRADING':
            continue
        sym = s['symbol']
        c['syms'].add(sym)
        c['base'][sym] = s.get('baseAsset', '')
        for f in s.get('filters', []):
            if f['filterType'] == 'LOT_SIZE':
                c['steps'][sym] = float(f['stepSize'])
            elif f['filterType'] == 'PRICE_FILTER':
                c['ticks'][sym] = float(f['tickSize'])
            elif f['filterType'] in ('NOTIONAL', 'MIN_NOTIONAL'):
                c['minnot'][sym] = float(f.get('minNotional', 0) or 0)
    return c

def _prec(x):
    xs = ('%f' % x).rstrip('0')
    return len(xs.split('.')[-1]) if '.' in xs else 0

def round_qty(sym, q, venue='fut'):
    st = _load_filters(venue)['steps'].get(sym, 0.001)
    return round((q // st) * st, _prec(st))

def round_price(sym, p, venue='fut'):
    tk = _load_filters(venue)['ticks'].get(sym, 0.01)
    return round(round(p / tk) * tk, _prec(tk))

def min_notional(sym, venue='fut'):
    return _load_filters(venue)['minnot'].get(sym, 0.0)

def mark(sym, venue='fut'):
    return float(binance('GET', VENUE[venue]['tick'], {'symbol': sym},
                         venue=venue)['price'])

def spot_free(asset):
    for b in binance('GET', '/api/v3/account', auth=True,
                     venue='spot').get('balances', []):
        if b['asset'] == asset:
            return float(b['free'])
    return 0.0

def my_position(sym, venue='fut'):
    """(signed qty, entry). On spot there is no short and no entry price -
    what you hold is simply the free balance of the base asset."""
    if venue == 'spot':
        base = _load_filters('spot')['base'].get(sym, '')
        return (spot_free(base) if base else 0.0), 0.0
    for p in binance('GET', '/fapi/v2/positionRisk', auth=True):
        if p['symbol'] == sym and abs(float(p.get('positionAmt', 0))) > 1e-9:
            return float(p['positionAmt']), float(p['entryPrice'])
    return 0.0, 0.0

def resolve_symbol(word, venue='fut'):
    """'doge' -> DOGEUSDT. Accepts full symbols, bare bases, 1000-prefixed."""
    w = word.upper().strip()
    syms = _load_filters(venue)['syms']
    cands = ((w, w + 'USDT', w + 'USDC') if venue == 'spot'
             else (w, w + 'USDT', w + 'USDC', '1000' + w + 'USDT'))
    for cand in cands:
        if cand in syms:
            return cand
    return None

# ── monitoring ────────────────────────────────────────────────────────────
def jload(p):
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return {}

def is_paper(d):
    """A paper book's positions are simulated and will NOT match the exchange.
    Labelling this is essential: the spot bot showed five holdings while
    Binance held none of them, which reads as a bug rather than by design."""
    try:
        src = open(os.path.join(d, 'config.py'), encoding='utf-8').read()
        m = re.search(r'^PAPER_MODE\s*=\s*(\w+)', src, re.M)
        return bool(m and m.group(1) == 'True')
    except Exception:
        return False

def fmt_bot(label):
    d, posf, trf = BOTS[label]
    if not os.path.isdir(d):
        return label.upper() + ': not running here'
    pos = jload(os.path.join(d, posf)).get('positions', {})
    tr = jload(os.path.join(d, trf))
    today = time.strftime('%Y-%m-%d')
    tt = [t for t in tr.get('trades', []) if t.get('close_date') == today]
    paper = is_paper(d)
    tag = ('  📄 _paper_' if paper else '  🔴 _live_')
    if is_paused(label):
        tag += '  ⏸ _paused_'
    out = ['*' + label.upper() + '*' + tag]
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

def bot_owned_symbols():
    """Every symbol currently claimed by a bot's position file."""
    owned = {}
    for label in BOTS:
        d, posf, _ = BOTS[label]
        for sym in jload(os.path.join(d, posf)).get('positions', {}):
            owned[sym] = label
    return owned

def manual_positions():
    """Live exchange positions that no bot claims - i.e. placed by hand here.

    These carry NO stop-loss and nothing manages them. Worse, a bot whose
    symbol list covers one WILL adopt it on its next reconcile, so they need
    to be visible rather than silently sitting on the account.
    """
    try:
        rows = binance('GET', '/fapi/v2/positionRisk', auth=True)
    except Exception as e:
        return None, str(e)
    owned = bot_owned_symbols()
    out = []
    for p in rows:
        amt = float(p.get('positionAmt', 0) or 0)
        if abs(amt) < 1e-9:
            continue
        sym = p['symbol']
        if sym in owned:
            continue
        out.append(dict(sym=sym, amt=amt,
                        entry=float(p.get('entryPrice', 0) or 0),
                        pnl=float(p.get('unRealizedProfit', 0) or 0)))
    return out, None

def fmt_manual():
    rows, err = manual_positions()
    if err:
        return '\u26A0 Could not read the exchange: %s' % err[:80]
    if not rows:
        return None
    lines = ['*YOURS* \U0001F464 _placed by hand, no bot manages these_']
    for r in rows:
        emo = '\U0001F7E2' if r['pnl'] >= 0 else '\U0001F534'
        lines.append('%s %s %s  %g @ %g  $%+.2f' % (
            emo, r['sym'], 'LONG' if r['amt'] > 0 else 'SHORT',
            abs(r['amt']), r['entry'], r['pnl']))
    lines.append('   \u26A0 no stop-loss - say *sell %s* to close'
                 % rows[0]['sym'].replace('USDT', '').lower())
    return NL.join(lines)

def money(x, dp=0):
    return ('-$' if x < 0 else '$') + format(abs(x), ',.%df' % dp)

def signed(x, dp=0):
    return ('+' if x >= 0 else '-') + '$' + format(abs(x), ',.%df' % dp)

def short_sym(s):
    """1000PEPEUSDC -> 1000PEPE. The quote asset is the same all the way
    down a column, so printing it 8 times just costs width."""
    for q in ('USDT', 'USDC'):
        if s.endswith(q):
            return s[:-len(q)]
    return s

def price_map(venue='fut'):
    """Every price in one call. Position files carry a P&L that is only as
    fresh as that bot's last loop - spot's showed +0.0% on seven bags."""
    try:
        d = binance('GET', VENUE[venue]['tick'], venue=venue)
        return {x['symbol']: float(x['price']) for x in d}
    except Exception:
        return {}

def live_pnl(sym, p, px):
    """(pct, usd) from the CURRENT price, falling back to the stored value."""
    e = float(p.get('entry', 0) or 0)
    q = float(p.get('qty', 0) or 0)
    if not (px and e and q):
        return (p.get('pnl_pct', 0) or 0), (p.get('pnl_usd', 0) or 0)
    sign = -1.0 if str(p.get('direction', '')).lower() == 'short' else 1.0
    usd = sign * (px - e) * q
    lev = float(p.get('leverage', 1) or 1)
    return sign * (px - e) / e * 100 * lev, usd

def fmt_positions():
    """ONLY what is open. No history, no totals, no lifetime stats."""
    blocks, n = [], 0
    fut_px, spot_px = price_map('fut'), price_map('spot')
    for label in BOTS:
        d, posf, _ = BOTS[label]
        pos = jload(os.path.join(d, posf)).get('positions', {})
        if not pos:
            continue
        n += len(pos)
        pm = spot_px if label == 'spot' else fut_px
        scored = []
        for sym, p in pos.items():
            scored.append((sym, p) + live_pnl(sym, p, pm.get(sym)))
        lines = []
        for sym, p, pct, usd in sorted(scored, key=lambda r: r[3]):
            lev = p.get('leverage')
            risk = '' if (p.get('sl') or p.get('trail_sl')) else ' !'
            lines.append('%-9s %-5s %-4s %+6.1f%% %8s%s'
                         % (short_sym(sym)[:9],
                            str(p.get('direction', ''))[:5],
                            ('%gx' % lev) if lev else '',
                            pct, signed(usd), risk))
        head = '*%s*' % label.upper()
        if is_paused(label):
            head += '  \u23F8 paused'
        blocks.append(head + NL + '`' + (NL.join(lines)) + '`' + NL)
    if not blocks:
        return '\U0001F4CA *Open positions*' + NL + NL + '   Nothing open.'
    out = ['\U0001F4CA *Open positions*  (%d)' % n, ''] + blocks
    if any('!' in b for b in blocks):
        out += ['', '_!  = no stop-loss on that position_']
    mine = fmt_manual()
    if mine:
        out += ['', mine]
    return NL.join(out)

def book_equity():
    """Equity per book. main/reverse are futures margin scoped to their own
    quote asset; spot is cash plus the market value of the coins held."""
    out = {}
    try:
        for a in binance('GET', '/fapi/v2/account', auth=True).get('assets', []):
            if a['asset'] == 'USDT':
                out['main'] = float(a.get('marginBalance', 0) or 0)
            elif a['asset'] == 'USDC':
                out['reverse'] = float(a.get('marginBalance', 0) or 0)
    except Exception:
        pass
    try:
        tot = 0.0
        for b in binance('GET', '/api/v3/account', auth=True,
                         venue='spot').get('balances', []):
            q = float(b.get('free', 0)) + float(b.get('locked', 0))
            if q <= 0:
                continue
            if b['asset'] in ('USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD'):
                tot += q
            else:
                try:
                    tot += q * mark(b['asset'] + 'USDT', 'spot')
                except Exception:
                    pass
        out['spot'] = tot
    except Exception:
        pass
    return out

def fmt_balance():
    """ONLY the money: what it is worth, and what today did to it."""
    eq = book_equity()
    today = time.strftime('%Y-%m-%d')
    total = sum(eq.values())
    day, rows, deployed = 0.0, [], 0.0
    for label in BOTS:
        d, posf, trf = BOTS[label]
        tt = [t for t in jload(os.path.join(d, trf)).get('trades', [])
              if t.get('close_date') == today]
        pnl = sum(t.get('pnl_usd', 0) or 0 for t in tt)
        if not is_paper(d):
            day += pnl
        pos = jload(os.path.join(d, posf)).get('positions', {})
        # MARGIN, not notional. size_usd is the full position value, so at 8x
        # a $400 margin position reads as $3,200 - summing that against equity
        # claimed 69% deployed when the real figure was a fraction of it.
        deployed += sum(float(p.get('size_usd', 0) or 0)
                        / float(p.get('leverage', 1) or 1) for p in pos.values())
        rows.append('%-8s %10s %9s' % (label, money(eq.get(label, 0)),
                                       signed(pnl)))
    arrow = '\u25B2' if day >= 0 else '\u25BC'
    out = ['\U0001F4BC *%s*    %s %s today' % (money(total), arrow, signed(day)),
           '', '`' + NL.join(rows) + '`']
    if total > 0:
        out += ['', '`margin used  %-10s (%.0f%%)`'
                % (money(deployed), deployed / total * 100),
                '`free         %-10s`' % money(total - deployed)]
    return NL.join(out)

def all_bots():
    parts = [fmt_bot(b) for b in BOTS]
    mine = fmt_manual()
    if mine:
        parts.append(mine)
    return (NL + NL).join(parts)

# ── trading ───────────────────────────────────────────────────────────────
PENDING = {}

def guard(sym, venue='fut'):
    if venue == 'spot':
        # main/reverse are futures and spot trades on paper, so nothing of
        # theirs can be netted against by a spot order
        return None
    if sym in OWNED:
        return ('⚠️ Heads up: %s is traded by the %s. This order will net '
                'against its live position and desync its book.' % (sym, OWNED[sym]))
    if sym.endswith('USDC'):
        return '⚠️ Heads up: USDC pairs belong to the reverse bot.'
    return None

def preview(chat, action, sym, side, qty, px, limit, sl, warn, extra='',
            ambiguous=None, venue='fut'):
    PENDING[chat] = dict(action=action, sym=sym, side=side, qty=qty,
                         limit=limit, sl=sl, ts=time.time(), venue=venue)
    kind = ('limit @ %g' % limit) if limit else ('market, now ~%g' % px)
    lines = ['📋 *%s %s*  _%s_' % (action, sym,
                                  'SPOT' if venue == 'spot' else 'perp'),
             '   *%g units*, %s' % (qty, kind),
             '   ≈ $%s' % format(qty * (limit or px), ',.2f')]
    if sl:
        risk = abs((limit or px) - sl) * qty
        lines.append('   🛑 stop-loss %g  (risk ≈ $%s)' % (sl, format(risk, ',.2f')))
    else:
        lines.append('   ⚠️ no stop-loss')
    if extra:
        lines.append('   ' + extra)
    if ambiguous is not None:
        # 'buy sol 5 @73' produced 0.06 units because the 5 was read as
        # dollars. State the reading taken and show the alternative.
        lines.append('   _took %g as DOLLARS - for %g units say_ `%g qty`'
                     % (ambiguous, ambiguous, ambiguous))
    if warn:
        lines += ['', warn]
    lines += ['', 'Reply *yes* to place it, *no* to cancel.']
    return NL.join(lines)

def do_buy(chat, sym, usd=None, limit=None, sl=None, qty_units=None,
           ambiguous=False, venue='fut'):
    if qty_units is None and usd is None:
        usd = DEFAULT_USD
    try:
        px = mark(sym, venue)
        if limit: limit = round_price(sym, limit, venue)
        if sl:    sl    = round_price(sym, sl, venue)
        # explicit units win over a dollar amount
        qty = (round_qty(sym, qty_units, venue) if qty_units is not None
               else round_qty(sym, usd / (limit or px), venue))
    except Exception as e:
        return "Couldn't price %s: %s" % (sym, e)
    if qty <= 0:
        if qty_units is not None:
            return ('%g units is below the minimum lot for %s.' % (qty_units, sym))
        return '$%g is too small for %s (below the minimum lot).' % (usd, sym)
    # spot enforces a notional floor and rejects the order outright
    mn = min_notional(sym, venue)
    if mn and qty * (limit or px) < mn:
        return ('%s needs at least $%g per order — %g units is only $%.2f. '
                'Try a bigger size.' % (sym, mn, qty, qty * (limit or px)))
    if venue == 'spot':
        cash = spot_free(sym[:-4] if sym.endswith(('USDT', 'USDC')) else '')
        quote = 'USDC' if sym.endswith('USDC') else 'USDT'
        have = spot_free(quote)
        need = qty * (limit or px)
        if need > have:
            return ('Not enough %s on the spot wallet: need $%.2f, have $%.2f.'
                    % (quote, need, have))
    if sl and sl >= (limit or px):
        return ('That stop (%g) is above the entry (%g) — for a buy it has to '
                'be below.' % (sl, limit or px))
    extra = ''
    if limit:
        extra = 'market is %g, so this waits %s' % (
            px, 'for a dip' if limit < px else 'for a rise')
    return preview(chat, 'BUY', sym, 'BUY', qty, px, limit, sl,
                   guard(sym, venue), extra,
                   ambiguous=(usd if ambiguous else None), venue=venue)

def do_sell(chat, sym, usd=None, limit=None, sl=None, qty_units=None,
            ambiguous=False, venue='fut'):
    try:
        amt, entry = my_position(sym, venue)
        px = mark(sym, venue)
    except Exception as e:
        return 'Position check failed: %s' % e
    if limit: limit = round_price(sym, limit, venue)
    if abs(amt) > 1e-9:
        side = 'SELL' if amt > 0 else 'BUY'
        if qty_units is not None:
            qty = min(abs(amt), round_qty(sym, qty_units, venue))
        elif usd is not None:
            qty = min(abs(amt), round_qty(sym, usd / (limit or px), venue))
        else:
            qty = round_qty(sym, abs(amt), venue)
        if venue == 'spot':
            extra = 'selling %g of %g held' % (qty, abs(amt))
        else:
            extra = 'closing %s %g from %g — P&L ≈ $%+.2f' % (
                'long' if amt > 0 else 'short', abs(amt), entry,
                (px - entry) * amt)
        return preview(chat, 'CLOSE', sym, side, qty, px, limit, None,
                       guard(sym, venue), extra, venue=venue)
    if venue == 'spot':
        # spot cannot short. Saying 'sell' with an empty wallet is far more
        # likely a mistake than a request to open one on the perp book.
        base = _load_filters('spot')['base'].get(sym, sym)
        return ('You hold no %s on the spot wallet, and spot has no shorting '
                '— you can only sell what you own.%s'
                'To short it, drop the word "spot": `sell %s`.'
                % (base, NL + NL, sym))
    usd = DEFAULT_USD if usd is None else usd
    qty = round_qty(sym, usd / (limit or px))
    if qty <= 0:
        return '$%g is too small for %s.' % (usd, sym)
    if sl: sl = round_price(sym, sl)
    return preview(chat, 'SHORT', sym, 'SELL', qty, px, limit, sl, guard(sym),
                   "you don't hold this — so this OPENS a short")

def do_confirm(chat):
    p = PENDING.pop(chat, None)
    if p and p.get('action') == 'RESTART':
        return do_restart(p['bots'])
    if not p:
        return "Nothing waiting. Tell me what to buy or sell first."
    if time.time() - p['ts'] > 90:
        return 'That preview expired. Ask me again.'
    v = p.get('venue', 'fut')
    params = {'symbol': p['sym'], 'side': p['side'], 'quantity': p['qty']}
    if p['limit']:
        params.update({'type': 'LIMIT', 'price': p['limit'], 'timeInForce': 'GTC'})
    else:
        params['type'] = 'MARKET'
    # reduceOnly is a futures concept - spot rejects the parameter
    if p['action'] == 'CLOSE' and v == 'fut':
        params['reduceOnly'] = 'true'
    try:
        r = binance('POST', VENUE[v]['order'], params, auth=True, venue=v)
    except Exception as e:
        return '❌ Exchange rejected it: %s' % e
    msg = ['✅ %s %s — %g units%s (order #%s)' % (
        'Placed' if p['limit'] else 'Filled', p['sym'], p['qty'],
        (' @ %g' % p['limit']) if p['limit'] else '', r.get('orderId'))]
    # attach the stop-loss as a separate reduce-only STOP_MARKET
    if p.get('sl') and p['action'] in ('BUY', 'SHORT'):
        try:
            close_side = 'SELL' if p['side'] == 'BUY' else 'BUY'
            # spot has no closePosition/STOP_MARKET - it needs an explicit
            # quantity on a STOP_LOSS order
            sp = ({'symbol': p['sym'], 'side': close_side, 'type': 'STOP_LOSS',
                   'stopPrice': p['sl'], 'quantity': p['qty']} if v == 'spot'
                  else {'symbol': p['sym'], 'side': close_side,
                        'type': 'STOP_MARKET', 'stopPrice': p['sl'],
                        'closePosition': 'true'})
            s = binance('POST', VENUE[v]['order'], sp, auth=True, venue=v)
            msg.append('🛑 Stop-loss set at %g (#%s)' % (p['sl'], s.get('orderId')))
        except Exception as e:
            msg.append('⚠️ Order went through but the STOP-LOSS FAILED: %s' % e)
            msg.append('   The position is unprotected — set it manually.')
    elif p['action'] in ('BUY', 'SHORT'):
        msg.append('⚠️ No stop-loss — this runs until you close it.')
    return NL.join(msg)

def do_orders(venue='fut'):
    try:
        o = binance('GET', VENUE[venue]['open'], auth=True, venue=venue)
    except Exception as e:
        return "Couldn't fetch orders: %s" % e
    if not o:
        return 'No pending %s orders.' % ('spot' if venue == 'spot' else 'perp')
    out = ['*Pending %s orders*' % ('spot' if venue == 'spot' else 'perp')]
    for x in o:
        out.append('   #%s %s %s %s %s @ %s' % (
            x['orderId'], x['symbol'], x['side'], x['type'],
            x['origQty'], x.get('stopPrice') or x.get('price') or '-'))
    out.append('')
    out.append('Say "cancel orders on doge" to clear one.')
    return NL.join(out)

def do_cancel_orders(sym, venue='fut'):
    try:
        binance('DELETE', VENUE[venue]['cancel'], {'symbol': sym}, auth=True,
                venue=venue)
        return 'Cleared all pending orders on %s.' % sym
    except Exception as e:
        return 'Cancel failed: %s' % e

def server_status():
    """Health of the droplet itself. Reads /proc and systemd - no root needed."""
    out = ['🖥 *Server*']
    try:
        up = float(open('/proc/uptime').read().split()[0])
        d, h, m = int(up // 86400), int(up % 86400 // 3600), int(up % 3600 // 60)
        out.append('   up %s%dh %dm' % (('%dd ' % d) if d else '', h, m))
    except Exception:
        pass
    try:
        mi = {}
        for ln in open('/proc/meminfo'):
            k, v = ln.split(':', 1)
            mi[k] = int(v.split()[0])
        tot = mi['MemTotal'] // 1024
        avail = mi.get('MemAvailable', mi['MemFree']) // 1024
        used = tot - avail
        out.append('   RAM  %d / %d MB  (%d%%)' % (used, tot, used * 100 // tot))
    except Exception:
        pass
    try:
        st = os.statvfs('/')
        out.append('   Disk %.1f GB free of %.1f' % (
            st.f_bavail * st.f_frsize / 1e9, st.f_blocks * st.f_frsize / 1e9))
    except Exception:
        pass
    try:
        la = open('/proc/loadavg').read().split()[:3]
        ncpu = os.cpu_count() or 1
        flag = '  (high)' if float(la[0]) > ncpu * 1.5 else ''
        out.append('   Load %s %s %s  on %d vCPU%s' % (la[0], la[1], la[2], ncpu, flag))
    except Exception:
        pass

    out.append('')
    out.append('⚙ *Services*')
    for sv in ('alphabot-main', 'alphabot-reverse', 'alphabot-spot', 'alphabot-telegram'):
        try:
            state = subprocess.run(['systemctl', 'is-active', sv], capture_output=True,
                                   text=True, timeout=8).stdout.strip()
            n = subprocess.run(['systemctl', 'show', '-p', 'NRestarts', '--value', sv],
                               capture_output=True, text=True, timeout=8).stdout.strip()
        except Exception:
            state, n = 'unknown', '?'
        emo = '🟢' if state == 'active' else '🔴'
        extra = ('  (%s restarts)' % n) if n not in ('0', '?', '') else ''
        out.append('%s %-9s %s%s' % (emo, sv.replace('alphabot-', ''), state, extra))

    out.append('')
    out.append('📈 *Last bot activity*')
    for label in BOTS:
        try:
            with open(os.path.join(BOTS[label][0], 'bot.log'), 'rb') as f:
                f.seek(0, 2); f.seek(max(0, f.tell() - 4000))
                last = f.read().decode('utf-8', 'ignore').strip().splitlines()[-1]
            ts = last[:19]
            age = ''
            try:
                secs = int(time.time() - time.mktime(
                    time.strptime(ts, '%Y-%m-%d %H:%M:%S')))
                age = ('  (%ds ago)' % secs) if secs < 300 else ('  STALE %dm' % (secs // 60))
            except Exception:
                pass
            out.append('   %-9s %s%s' % (label, ts, age))
        except Exception:
            out.append('   %-9s no log' % label)
    return NL.join(out)




# ── bot control ───────────────────────────────────────────────────────────
def _flag(label):
    return os.path.join(BOTS[label][0], 'PAUSED')

def is_paused(label):
    return os.path.exists(_flag(label))

def do_pause(labels):
    out = []
    for b in labels:
        try:
            open(_flag(b), 'w').write(time.strftime('%Y-%m-%d %H:%M:%S'))
            out.append('\u23F8 *%s* paused - no new entries.' % b)
        except Exception as e:
            out.append('Could not pause %s: %s' % (b, e))
    out.append('')
    out.append('_Open positions are still managed and their stops stay live. '
               'Say *resume* to re-enable entries._')
    return NL.join(out)

def do_resume(labels):
    out = []
    for b in labels:
        f = _flag(b)
        if os.path.exists(f):
            try:
                os.remove(f)
                out.append('\u25B6 *%s* resumed - taking entries again.' % b)
            except Exception as e:
                out.append('Could not resume %s: %s' % (b, e))
        else:
            out.append('*%s* was not paused.' % b)
    return NL.join(out)

def do_restart(labels):
    out = []
    for b in labels:
        try:
            r = subprocess.run(['systemctl', 'restart', 'alphabot-' + b],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                out.append('\U0001F504 *%s* restarted.' % b)
            else:
                out.append('%s restart failed: %s' % (b, (r.stderr or '')[:80]))
        except Exception as e:
            out.append('%s restart failed: %s' % (b, e))
    out.append('')
    out.append('_It reconciles its open positions from the exchange on boot._')
    return NL.join(out)

def which_bots(t):
    """Which bots is this message about? Defaults to all."""
    named = [b for b in BOTS if re.search(r'\b' + b + r'\b', t)]
    # a venue word alone must not be read as naming the spot BOT, or
    # "spot buy sol" would look like an instruction aimed at that bot
    if named == ['spot'] and not re.search(r'\bspot\s*bot\b', t) \
            and re.search(r'\b(buy|sell|wallet|order)', t):
        named = []
    if named:
        return named
    if re.search(r'\b(all|every|both)\b', t):
        return list(BOTS)
    return list(BOTS)

# ── conversation memory ───────────────────────────────────────────────────
LAST = {}          # chat -> {'sym': 'DOGEUSDT', 'bot': 'main'}

def remember(chat, **kw):
    LAST.setdefault(chat, {}).update({k: v for k, v in kw.items() if v})

def recall(chat, key):
    return LAST.get(chat, {}).get(key)

def remember_intent(chat, **kw):
    """Unlike remember(), this overwrites with None too - so a fresh
    'buy btc' clears the limit price left over from 'buy eth at 1860'."""
    LAST.setdefault(chat, {}).update(kw)

def same_coin_on(sym, venue):
    """Carry a symbol across books: 1000PEPEUSDT (perp) -> PEPEUSDT (spot)."""
    if not sym:
        return None
    if sym in _load_filters(venue)['syms']:
        return sym
    base = _F['fut']['base'].get(sym) or _F['spot']['base'].get(sym)
    return resolve_symbol(base, venue) if base else None

def _stats(ts):
    if not ts:
        return None
    w = [t for t in ts if t.get('pnl_usd', 0) > 0]
    l = [t for t in ts if t.get('pnl_usd', 0) <= 0]
    gw = sum(t['pnl_usd'] for t in w)
    gl = -sum(t['pnl_usd'] for t in l)
    return dict(n=len(ts), wr=len(w) / len(ts) * 100,
                pf=(gw / gl if gl else None),
                net=sum(t.get('pnl_usd', 0) for t in ts),
                aw=(gw / len(w) if w else 0), al=(gl / len(l) if l else 0))

def venue_info():
    """What market do my orders actually hit? Asked once and got nothing back."""
    return NL.join([
        '\U0001F4CD *What you are trading here*',
        '',
        'Binance *Demo Trading* - one account, one API key, two books.',
        'Play money on both.',
        '',
        '*PERPETUALS* (the default)',
        '   \u2022 buy = open a LONG perp',
        '   \u2022 sell = close it, or open a SHORT if you are flat',
        '   \u2022 leveraged, funded in USDT / USDC',
        '   \u2022 say _buy 200 sol_',
        '',
        '*SPOT* - say the word "spot" and it routes there',
        '   \u2022 buy = you own the coin outright, no leverage',
        '   \u2022 sell = sell what you hold. There is NO shorting',
        '   \u2022 say _spot buy 200 sol_ or _spot wallet_',
        '',
        '*The bots:*',
        '   main / reverse - perps, live demo fills',
        '   spot - spot prices, simulated fills (paper by choice, not',
        '   by limitation)',
    ])

def trade_report():
    """Honest performance summary across every book."""
    from collections import defaultdict
    out = ['\U0001F4CA *Trade report*', '']
    live = []
    for label in BOTS:
        d, _, trf = BOTS[label]
        ts = jload(os.path.join(d, trf)).get('trades', [])
        st = _stats(ts)
        paper = is_paper(d)
        if not paper:
            live += ts
        head = '*%s*%s' % (label.upper(), '  _(paper)_' if paper else '')
        if not st:
            out += [head, '   no closed trades yet', '']
            continue
        pf = 'n/a' if st['pf'] is None else '%.2f' % st['pf']
        out += [head,
                '   %d trades, %.0f%% won, PF %s' % (st['n'], st['wr'], pf),
                '   net $%+.2f   avg win $%.2f vs loss $%.2f' % (
                    st['net'], st['aw'], st['al'])]
        by = defaultdict(float)
        for t in ts:
            by[t.get('symbol', '?')] += t.get('pnl_usd', 0)
        if by:
            rank = sorted(by.items(), key=lambda x: -x[1])
            out.append('   best %s $%+.0f | worst %s $%+.0f' % (
                rank[0][0], rank[0][1], rank[-1][0], rank[-1][1]))
        out.append('')

    st = _stats(live)
    if st:
        pf = 'n/a' if st['pf'] is None else '%.2f' % st['pf']
        out += ['*Live books combined*',
                '   %d trades, %.0f%% won, PF %s' % (st['n'], st['wr'], pf),
                '   net $%+.2f' % st['net'], '']
        if st['pf'] is not None and st['pf'] < 1:
            out.append('\u26A0 PF below 1.00 - the book loses money even at a '
                       '%.0f%% win rate, because the average loss ($%.2f) is '
                       'bigger than the average win ($%.2f).'
                       % (st['wr'], st['al'], st['aw']))
    return NL.join(out)

def day_rows(day):
    """(live P&L, trade count, per-bot lines) for one calendar day.
    Dates come from the bots' own close_date, which is server-local
    (Asia/Kolkata) - so a 'day' here means an IST day."""
    tot, n, per = 0.0, 0, []
    for label in BOTS:
        d, posf, trf = BOTS[label]
        ts = [t for t in jload(os.path.join(d, trf)).get('trades', [])
              if t.get('close_date') == day]
        opn = len(jload(os.path.join(d, posf)).get('positions', {}))
        pnl = sum(t.get('pnl_usd', 0) for t in ts)
        tot += 0 if is_paper(d) else pnl
        n += len(ts)
        per.append('   %-8s %d closed  $%+.2f   %d open%s'
                   % (label, len(ts), pnl, opn, '  (paper)' if is_paper(d) else ''))
    return tot, n, per

def day_block(day, title=None):
    """title=None heads the block with the bare date, for an explicit day."""
    tot, n, per = day_rows(day)
    stamp = ('*%s* %s' % (title, day)) if title else ('*%s*' % day)
    head = ('\U0001F4C5 %s - %d closed, $%+.2f on the live books'
            % (stamp, n, tot))
    return NL.join([head, ''] + per), n

def today_summary():
    """'How did today go'. Just after midnight this used to report nothing at
    all, which reads as a broken bot when you watched trades close minutes
    earlier - so when today is still empty, yesterday is shown too."""
    now = time.time()
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    body, n = day_block(today, 'Today')
    if n:
        return body
    age_min = int(time.strftime('%H', time.localtime(now))) * 60 + \
        int(time.strftime('%M', time.localtime(now)))
    yday = time.strftime('%Y-%m-%d', time.localtime(now - 86400))
    ybody, yn = day_block(yday, 'Yesterday')
    note = ('   Nothing closed yet - the trading day is only %dh %02dm old.'
            % (age_min // 60, age_min % 60))
    if yn == 0:
        return NL.join([body, note])
    return NL.join([body, note, '', ybody])

def spot_wallet():
    """What is actually sitting in the spot demo wallet."""
    try:
        a = binance('GET', '/api/v3/account', auth=True, venue='spot')
    except Exception as e:
        return "Couldn't reach the spot wallet: %s" % e
    held = [b for b in a.get('balances', [])
            if float(b['free']) + float(b['locked']) > 0]
    cash = [b for b in held if b['asset'] in ('USDT', 'USDC')]
    coins = [b for b in held if b['asset'] not in ('USDT', 'USDC')]
    out = ['\U0001F4B0 *Spot wallet*  _demo_']
    for b in cash:
        out.append('   %-6s $%s free' % (b['asset'],
                                         format(float(b['free']), ',.2f')))
    if coins:
        out.append('')
        out.append('*Holdings*')
        for b in sorted(coins, key=lambda x: x['asset']):
            q = float(b['free']) + float(b['locked'])
            try:
                val = ' ≈ $%s' % format(
                    q * mark(b['asset'] + 'USDT', 'spot'), ',.2f')
            except Exception:
                val = ''
            out.append('   %-6s %g%s' % (b['asset'], q, val))
    else:
        out.append('')
        out.append('   No coins held — cash only.')
    out.append('')
    out.append('_Say_ `spot buy 200 sol` _to trade here. '
               'This wallet is separate from the perp bots._')
    return NL.join(out)

# ── natural language ──────────────────────────────────────────────────────
YES = {'yes', 'y', 'yeah', 'yep', 'ok', 'okay', 'go', 'go ahead', 'do it',
       'confirm', 'sure', 'proceed', 'send it', 'buy it', 'sell it', 'correct'}
NO  = {'no', 'n', 'nope', 'cancel', 'stop', 'abort', 'nevermind', 'never mind',
       'dont', "don't", 'wait'}

NUM = r'(\d+(?:\.\d+)?)'

# '100$', '$100', '100 usd', 'make it 200', '5 qty' - a size and nothing
# else. On its own it is meaningless; after a trade attempt it means
# 'same thing, this size'.
SIZE_ONLY = re.compile(
    r'(?:make it|makeit|try|use|do|instead)?\s*\$?\s*' + NUM +
    r'\s*(\$|usd[t]?|dollars?|bucks|qty|units?|coins?)?\s*'
    r'(?:instead|please)?\s*')

def bare_size(t):
    m = SIZE_ONLY.fullmatch(t)
    if not m:
        return None
    unit = (m.group(2) or '').rstrip('s')
    return float(m.group(1)), unit in ('qty', 'unit', 'coin')

def _clean(text):
    """Buttons send their label, emoji and all. Drop leading non-letters so
    '\U0001F4CA Positions' parses exactly like 'positions'."""
    t = (text or '').strip()
    t = re.sub(r'^[^0-9A-Za-z/$]+', '', t)
    return re.sub(r'[^0-9A-Za-z/$@.,\s\'&?-]+', ' ', t).strip()

def parse(chat, text):
    t = ' '.join(_clean(text).lower().split())
    if not t:
        return None
    bare = t.lstrip('/')

    if bare in YES or t in YES:
        return do_confirm(chat)
    if bare in NO or t in NO:
        return 'Cancelled.' if PENDING.pop(chat, None) else 'Nothing to cancel.'
    if bare in ('start', 'help', 'commands') or 'what can you do' in t:
        return HELP

    # a size on its own continues whatever we were just doing
    bs = bare_size(t)
    if bs is not None:
        last = LAST.get(chat, {})
        if last.get('act') and last.get('sym'):
            amt, as_units = bs
            fn = do_sell if last['act'] == 'sell' else do_buy
            return fn(chat, last['sym'],
                      None if as_units else amt, last.get('limit'),
                      last.get('sl'), amt if as_units else None,
                      False, last.get('venue', 'fut'))

    # greetings / small talk
    if re.fullmatch(r'(hi|hey|hello|yo|good\s*(morning|evening|afternoon))!?', t):
        return ('Hey. ' + today_summary() + NL + NL +
                '_Ask me for a report, positions, server status, or place a trade._')
    if re.search(r'\b(thanks|thank you|thx|good job|nice)', t):
        return 'Anytime. Say *report* whenever you want the full picture.'

    # bot control: pause / resume / restart
    if re.search(r'\b(pause|paus|halt|freeze|hold off|stop trading|'
                 r'stop entries)', t):
        return do_pause(which_bots(t))
    if re.search(r'\b(resume|unpause|un-pause|continue|start trading|'
                 r'carry on)', t):
        return do_resume(which_bots(t))
    if re.search(r'\b(restart|reboot|reload)', t):
        bots = which_bots(t)
        PENDING[chat] = dict(action='RESTART', bots=bots, ts=time.time())
        return ('Restart *%s*?' % ', '.join(bots) + NL +
                '_Open positions are reconciled from the exchange on boot._' +
                NL + NL + 'Reply *yes* to go ahead.')

    # what market am I trading? Only the bare question — "spot wallet" and
    # "spot orders" are requests for data and are handled further down.
    if re.search(r'\b(perp|perpetual|futures|spot|margin|leverage|'
                 r'what market|which market|what am i trading)', t) \
            and not re.search(r'\b(buy|sell|wallet|balance|holding|order|'
                              r'pending|position|account|cancel|bot)', t):
        return venue_info()

    # A named day is checked BEFORE the all-time report, otherwise "yesterday
    # report" and "report for 2026-08-01" both match 'report' and return the
    # whole history instead of that day.
    if re.search(r'\b(yesterday|last night|previous day|last day)', t):
        return day_block(time.strftime('%Y-%m-%d',
                                       time.localtime(time.time() - 86400)),
                         'Yesterday')[0]
    m = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', t)
    if m:
        return day_block(m.group(1))[0]
    if re.search(r'\b(today|so far|this morning|tonight)', t):
        return today_summary()

    # full performance report
    if re.search(r'\b(report|analys|analyz|performance|how am i doing|'
                 r'summary|stats|statistic|review)', t):
        return trade_report()

    # server / infrastructure health
    if re.search(r'\b(server|vps|vcpu|cpu|droplet|machine|host|uptime|health|memory|ram|disk|load|service)', t):
        return server_status()

    # status questions
    if re.search(r'\b(order|pending)s?\b', t) and not re.search(r'\b(buy|sell)\b', t):
        v = 'spot' if re.search(r'\bspot\b', t) else 'fut'
        m = re.search(r'cancel.*\bon\s+([a-z0-9]+)', t)
        if m or 'cancel' in t:
            w = (m.group(1) if m else '')
            sym = resolve_symbol(w, v) if w else None
            if sym: return do_cancel_orders(sym, v)
        return do_orders(v)
    # 'spot' is now BOTH a bot name and a venue, so order matters here:
    # most specific first, or "spot buy sol" returns a status card.
    if re.search(r'\bspot\s*bot\b', t):
        return fmt_bot('spot')
    if re.search(r'\bspot\b', t) and re.search(
            r'\b(wallet|balance|holding|position|account|what.*hold)', t):
        return spot_wallet()
    for name in BOTS:
        if name == 'spot':
            continue                      # handled just above
        if re.search(r'\b' + name + r'\b', t):
            return fmt_bot(name)
    if re.search(r'\b(balance|equity|wallet|worth|how much|money|capital)', t):
        return fmt_balance()
    if re.search(r'\b(position|open|holding|status|doing|pnl|p&l|profit|'
                 r'how are|summary|show|update)', t):
        return fmt_positions()

    # Which book? Demo Trading is one account on two hosts, so the only
    # thing that decides this is the wording.
    venue = 'spot' if re.search(r'\bspot\b', t) else 'fut'

    # trading intent
    sell = bool(re.search(r'\b(sell|close|exit|dump|short|offload)', t))
    buy  = bool(re.search(r'\b(buy|long|purchase|grab|enter|get)', t))
    if not (buy or sell):
        # ── nothing matched: guess what they meant rather than dead-end ──
        words = set(re.findall(r'[a-z0-9]+', t))

        # did they name a coin? offer the obvious actions
        for w in words:
            if len(w) < 2 or w in ('the', 'and', 'for', 'you', 'how', 'what',
                                   'bot', 'bots', 'is', 'it', 'my', 'me', 'a'):
                continue
            sym = resolve_symbol(w)
            if sym:
                remember(chat, sym=sym)
                try:
                    px = mark(sym)
                    amt, entry = my_position(sym)
                except Exception:
                    px, amt, entry = 0, 0, 0
                lines = ['*%s* is at %g' % (sym, px)]
                if abs(amt) > 1e-9:
                    lines.append('You hold %g (entry %g).' % (amt, entry))
                    lines.append('Say *sell %s* to close it.' % w)
                else:
                    lines.append('No position. Say *buy %g %s* to open one, '
                                 'or *buy %s at <price>* for a limit.'
                                 % (DEFAULT_USD, w, w))
                return NL.join(lines)

        # pronouns referring to the last symbol we discussed
        if re.search(r'\b(it|that|this one)\b', t):
            sym = recall(chat, 'sym')
            if sym:
                if re.search(r'\b(sell|close|dump|exit|out)\b', t):
                    return do_sell(chat, sym, None, None)
                if re.search(r'\b(buy|more|add|long)\b', t):
                    return do_buy(chat, sym, None, None, None)
                return 'You mean %s? Say *buy* or *sell* with it.' % sym

        if re.search(r'\b(help|how|what can)\b', t):
            return HELP

        return ("I didn't catch that \U0001F937" + NL + NL +
                '*Tap a button below* for the usual things, or type:' + NL +
                '   *buy 100 doge*        open a perp' + NL +
                '   *spot buy 200 sol*    buy on spot' + NL +
                '   *sell btc*            close it' + NL +
                '   *yesterday*           how yesterday went' + NL +
                '   *pause reverse bot*   stop new entries' + NL + NL +
                '_Plain English is fine - I do not need exact commands._')

    # explicit QUANTITY: '2 qty', 'qty 2', '2 units', '3 coins', '5x'
    qty_units = None
    m = (re.search(NUM + r'\s*(?:qty|units?|coins?|tokens?|contracts?|shares?)', t)
         or re.search(r'(?:qty|quantity|units?)\s*(?:of\s*)?' + NUM, t)
         or re.search(NUM + r'\s*x\b', t))
    if m: qty_units = float(m.group(1))

    usd = None
    m = (re.search(r'(?:\$|usd\s*)' + NUM, t)
         or re.search(NUM + r'\s*(?:usd|dollars?|worth|bucks)', t))
    if m: usd = float(m.group(1))

    limit = None
    m = re.search(r'(?:@|\bat\b|\bwhen it hits\b|\bprice\b)\s*' + NUM, t)
    if m: limit = float(m.group(1))

    sl = None
    m = re.search(r'\b(?:sl|stop(?:\s*loss)?)\b\s*(?:at\s*)?' + NUM, t)
    if m: sl = float(m.group(1))

    # symbol: try every word, skipping ones already consumed as numbers
    used = {str(x) for x in (usd, limit, sl, qty_units) if x is not None}
    sym = None
    for w in re.findall(r'[a-z0-9]+', t):
        if w in used or w.isdigit():
            continue
        if w in ('buy', 'sell', 'close', 'long', 'short', 'at', 'sl', 'stop',
                 'loss', 'usd', 'dollar', 'dollars', 'of', 'worth', 'the',
                 'exit', 'dump', 'get', 'me', 'please', 'and', 'with', 'a',
                 'spot', 'perp', 'perpetual', 'futures', 'future', 'qty',
                 'units', 'unit', 'coins', 'market', 'order', 'now'):
            continue
        sym = resolve_symbol(w, venue)
        if sym:
            break
    carried = ''
    if not sym:
        # they named no coin - they almost certainly mean the one we were
        # just discussing ('want to buy spot' straight after talking ETH)
        sym = same_coin_on(recall(chat, 'sym'), venue)
        if sym:
            carried = NL + NL + '_(carried on from %s - say the coin if you '
            carried = (carried % sym) + 'meant another)_'
        else:
            return ("I couldn't work out which coin you mean. Try something "
                    'like "buy 200 doge" or "sell btc".')

    # A bare number is ambiguous. Default it to DOLLARS (the common case) but
    # the preview says so explicitly, because reading '2' as $2 silently bought
    # 0.02 units of a $100 coin.
    ambiguous = False
    if usd is None and qty_units is None:
        for n in re.findall(NUM, t):
            if limit is not None and float(n) == limit: continue
            if sl is not None and float(n) == sl: continue
            usd = float(n)
            # a bare number is genuinely ambiguous - default to dollars
            # but flag it so the preview shows both readings
            ambiguous = True
            break

    # Record BEFORE dispatching, so even a rejected attempt (below the
    # minimum size, say) can be retried by replying with just a number.
    remember_intent(chat, sym=sym, act=('sell' if sell else 'buy'),
                    limit=limit, sl=sl, venue=venue)
    return (do_sell if sell else do_buy)(chat, sym, usd, limit, sl,
                                         qty_units, ambiguous, venue) + carried

HELP = NL.join([
    '👋 Talk to me normally. Examples:',
    '',
    '*Checking in*',
    '   how are the bots doing',
    '   show positions',
    "   what's main doing",
    '   any pending orders',
    '   server status',
    '',
    '*Trading perps* (the default, $%g if you name no size)' % DEFAULT_USD,
    '   buy 200 doge',
    '   buy doge at 0.15',
    '   buy 100 doge at 0.15 sl 0.13',
    '   buy 5 qty sol          _units, not dollars_',
    '   sell doge',
    '   cancel orders on doge',
    '',
    '*Trading spot* — just say "spot"',
    '   spot buy 200 sol',
    '   spot sell sol',
    '   spot wallet',
    '',
    'I always show a preview first — reply *yes* to place it, *no* to cancel.',
])

# ── live notifications ────────────────────────────────────────────────────
# The books are JSON files on disk. Snapshot them, and on every poll report
# whatever changed. Seeded from the CURRENT state at boot so a restart never
# replays history as if it just happened.
_seen = {}

def _snapshot():
    snap = {}
    for label, (d, posf, trf) in BOTS.items():
        pos = jload(os.path.join(d, posf)).get('positions', {})
        tr = jload(os.path.join(d, trf)).get('trades', [])
        snap[label] = {
            'open': {k: float(v.get('entry', 0) or 0) for k, v in pos.items()},
            'closed': len(tr),
            'last': tr[-1] if tr else None,
        }
    return snap

def changes():
    """Lines describing anything that opened or closed since last look."""
    global _seen
    out = []
    now = _snapshot()
    for label, cur in now.items():
        old = _seen.get(label)
        if old is None:
            continue                     # first look: seed, announce nothing
        paper = is_paper(BOTS[label][0])
        tag = ' _(paper)_' if paper else ''
        pos = jload(os.path.join(BOTS[label][0], BOTS[label][1])).get('positions', {})
        for sym, entry in cur['open'].items():
            if sym in old['open']:
                continue
            p = pos.get(sym, {})
            lev = p.get('leverage')
            size = float(p.get('size_usd', 0) or 0)
            sl = p.get('sl')
            bits = ['\U0001F7E2 *OPENED*  %s%s' % (label, tag),
                    '`%s  %s%s`' % (short_sym(sym),
                                    str(p.get('direction', '')).upper(),
                                    ('  %gx' % lev) if lev else '')]
            bits.append('`entry %-10.6g size %s`' % (entry, money(size)))
            if sl:
                risk = abs(entry - float(sl)) / entry * 100 * (lev or 1)
                bits.append('`stop  %-10.6g risk %.1f%%`' % (float(sl), risk))
            else:
                bits.append('`no stop-loss`')
            out.append(NL.join(bits))
        if cur['closed'] > old['closed'] and cur['last']:
            t = cur['last']
            p = t.get('pnl_usd', 0) or 0
            today = time.strftime('%Y-%m-%d')
            tr = jload(os.path.join(BOTS[label][0], BOTS[label][2])).get('trades', [])
            tt = [x for x in tr if x.get('close_date') == today]
            w = len([x for x in tt if (x.get('pnl_usd') or 0) > 0])
            held = t.get('held_min') or 0
            hh = ('%dm' % held) if held < 60 else ('%.1fh' % (held / 60.0))
            out.append(NL.join([
                '%s *CLOSED*  %s   %s%s'
                % ('\u2705' if p > 0 else '\U0001F534', label, signed(p, 2), tag),
                '`%s  %+.2f%%  held %s`'
                % (short_sym(t.get('symbol', '?')), t.get('pnl_pct', 0) or 0, hh),
                '`%s`' % (t.get('reason') or '')[:28],
                '`today %s   %dW %dL`'
                % (signed(sum(x.get('pnl_usd') or 0 for x in tt), 2),
                   w, len(tt) - w)]))
    _seen = now
    return out

def handle(chat, text):
    """Returns (reply, keyboard)."""
    try:
        r = parse(chat, text)
    except Exception as e:
        return ('Something went wrong: %s' % e, MENU)
    if not r:
        r = ("I didn't catch that. Tap a button below, or type things like:"
             + NL + '   *buy 100 doge*   *sell btc*   *spot buy 200 sol*'
             + NL + '   *yesterday*   *pause reverse bot*')
    # an order waiting on a yes gets the confirm pad instead of the menu
    return (r, CONFIRM if chat in PENDING else MENU)

def main():
    print('waiting for token...', flush=True)
    while True:
        token = conf().get('TOKEN', '')
        if token: break
        time.sleep(30)
    print('polling', flush=True)
    changes()          # seed: adopt current state without announcing it
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
                reply, kb = handle(chat, msg.get('text', ''))
                tg(token, 'sendMessage', chat_id=chat, parse_mode='Markdown',
                   reply_markup=kb, text=reply[:4000])
            # push anything the bots did while we were waiting
            bound = conf().get('CHAT', '')
            if bound:
                for line in changes():
                    try:
                        tg(token, 'sendMessage', chat_id=bound,
                           parse_mode='Markdown', text=line[:4000])
                    except Exception:
                        pass
        except Exception as e:
            print('poll error: %s' % e, flush=True)
            time.sleep(10)

if __name__ == '__main__':
    main()
