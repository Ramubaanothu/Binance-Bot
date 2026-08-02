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

def is_paper(d):
    """A paper book's positions are simulated and will NOT match the exchange.
    Labelling this is essential: the hype bot showed five holdings while
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

    return ("I didn't catch that. Try:" + NL +
            '   *report* - full performance breakdown' + NL +
            '   *positions* - what is open right now' + NL +
            '   *today* - how today is going' + NL +
            '   *server status* - the droplet' + NL +
            '   *buy 100 doge* / *sell doge* - place a trade' + NL + NL +
            '_Plain English is fine._')

def preview(chat, action, sym, side, qty, px, limit, sl, warn, extra=''):
    PENDING[chat] = dict(action=action, sym=sym, side=side, qty=qty,
                         limit=limit, sl=sl, ts=time.time())
    kind = ('limit @ %g' % limit) if limit else ('market, now ~%g' % px)
    lines = ['📋 *%s %s*' % (action, sym),
             '   *%g units*, %s' % (qty, kind),
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

def do_buy(chat, sym, usd=None, limit=None, sl=None, qty_units=None):
    if qty_units is None and usd is None:
        usd = DEFAULT_USD
    try:
        px = mark(sym)
        if limit: limit = round_price(sym, limit)
        if sl:    sl    = round_price(sym, sl)
        # explicit units win over a dollar amount
        qty = (round_qty(sym, qty_units) if qty_units is not None
               else round_qty(sym, usd / (limit or px)))
    except Exception as e:
        return "Couldn't price %s: %s" % (sym, e)
    if qty <= 0:
        if qty_units is not None:
            return ('%g units is below the minimum lot for %s.' % (qty_units, sym))
        return '$%g is too small for %s (below the minimum lot).' % (usd, sym)
    if sl and sl >= (limit or px):
        return ('That stop (%g) is above the entry (%g) — for a buy it has to '
                'be below.' % (sl, limit or px))
    extra = ''
    if limit:
        extra = 'market is %g, so this waits %s' % (
            px, 'for a dip' if limit < px else 'for a rise')
    return preview(chat, 'BUY', sym, 'BUY', qty, px, limit, sl, guard(sym), extra)

def do_sell(chat, sym, usd=None, limit=None, sl=None, qty_units=None):
    try:
        amt, entry = my_position(sym)
        px = mark(sym)
    except Exception as e:
        return 'Position check failed: %s' % e
    if limit: limit = round_price(sym, limit)
    if abs(amt) > 1e-9:
        side = 'SELL' if amt > 0 else 'BUY'
        if qty_units is not None:
            qty = min(abs(amt), round_qty(sym, qty_units))
        elif usd is not None:
            qty = min(abs(amt), round_qty(sym, usd / (limit or px)))
        else:
            qty = abs(amt)
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
    if p and p.get('action') == 'RESTART':
        return do_restart(p['bots'])
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
    for sv in ('alphabot-main', 'alphabot-reverse', 'alphabot-hype', 'alphabot-telegram'):
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

def today_summary():
    """Short 'how did today go' answer."""
    from collections import defaultdict
    today = time.strftime('%Y-%m-%d')
    tot, n, per = 0.0, 0, []
    for label in BOTS:
        d, posf, trf = BOTS[label]
        ts = [t for t in jload(os.path.join(d, trf)).get('trades', [])
              if t.get('close_date') == today]
        opn = len(jload(os.path.join(d, posf)).get('positions', {}))
        pnl = sum(t.get('pnl_usd', 0) for t in ts)
        tot += 0 if is_paper(d) else pnl
        n += len(ts)
        per.append('   %-8s %d closed  $%+.2f   %d open%s'
                   % (label, len(ts), pnl, opn, '  (paper)' if is_paper(d) else ''))
    head = ('\U0001F4C5 *Today* - %d trades closed, $%+.2f on the live books'
            % (n, tot))
    if n == 0:
        head += NL + '   Quiet so far. Nothing has closed yet.'
    return NL.join([head, ''] + per)

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

    # full performance report
    if re.search(r'\b(report|analys|analyz|performance|how am i doing|'
                 r'summary|stats|statistic|review)', t):
        return trade_report()

    # today
    if re.search(r'\b(today|so far|this morning|tonight)', t):
        return today_summary()

    # server / infrastructure health
    if re.search(r'\b(server|vps|vcpu|cpu|droplet|machine|host|uptime|health|memory|ram|disk|load|service)', t):
        return server_status()

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
                 r'summary|report|show|update)', t):
        return all_bots()

    # trading intent
    sell = bool(re.search(r'\b(sell|close|exit|dump|short|offload)', t))
    buy  = bool(re.search(r'\b(buy|long|purchase|grab|enter|get)', t))
    if not (buy or sell):
        return None

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
                 'exit', 'dump', 'get', 'me', 'please', 'and', 'with', 'a'):
            continue
        sym = resolve_symbol(w)
        if sym:
            break
    if not sym:
        return ("I couldn't work out which coin you mean. Try something like "
                '"buy 200 doge" or "sell btc".')

    # A bare number is ambiguous. Default it to DOLLARS (the common case) but
    # the preview says so explicitly, because reading '2' as $2 silently bought
    # 0.02 units of a $100 coin.
    if usd is None and qty_units is None:
        for n in re.findall(NUM, t):
            if limit is not None and float(n) == limit: continue
            if sl is not None and float(n) == sl: continue
            usd = float(n)
            break

    return (do_sell if sell else do_buy)(chat, sym, usd, limit, sl, qty_units)

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
