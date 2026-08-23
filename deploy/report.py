"""Trade report across all deployed books. Runs on the droplet."""
import json, os
from collections import defaultdict

BOOKS = [('MAIN (USDT, live)',    '/home/bots/main/trades_binance.json'),
         ('REVERSE (USDC, live)', '/home/bots/reverse/trades_reverse.json'),
         ('SPOT (paper)',   '/home/bots/spot/trades_spot.json')]

def load(p):
    try:
        return json.load(open(p, encoding='utf-8')).get('trades', [])
    except Exception:
        return []

def stats(ts):
    if not ts:
        return None
    w = [t for t in ts if t.get('pnl_usd', 0) > 0]
    l = [t for t in ts if t.get('pnl_usd', 0) <= 0]
    gw = sum(t['pnl_usd'] for t in w)
    gl = -sum(t['pnl_usd'] for t in l)
    return dict(n=len(ts), w=len(w), wr=len(w) / len(ts) * 100,
                pf=(gw / gl if gl else float('inf')),
                net=sum(t.get('pnl_usd', 0) for t in ts),
                aw=(gw / len(w) if w else 0), al=(gl / len(l) if l else 0),
                best=max((t['pnl_usd'] for t in ts), default=0),
                worst=min((t['pnl_usd'] for t in ts), default=0))

def line(s):
    pf = ' inf' if s['pf'] == float('inf') else '%.2f' % s['pf']
    return ('n=%-4d WR %5.1f%%  PF %-5s  net $%+9.2f  avgW $%6.2f  avgL $%6.2f'
            % (s['n'], s['wr'], pf, s['net'], s['aw'], s['al']))

for name, path in BOOKS:
    ts = load(path)
    print('=' * 78)
    print(name)
    print('=' * 78)
    s = stats(ts)
    if not s:
        print('  no closed trades yet'); print(); continue
    print('  ' + line(s))
    print('  best $%+.2f | worst $%+.2f' % (s['best'], s['worst']))

    by = defaultdict(list)
    for t in ts:
        by[t.get('symbol', '?')].append(t)
    rank = sorted(by.items(), key=lambda x: -sum(t.get('pnl_usd', 0) for t in x[1]))
    print()
    print('  per symbol (min 2 trades):')
    for sym, v in rank:
        if len(v) < 2:
            continue
        ss = stats(v)
        flag = ''
        if ss['net'] < -40 and ss['n'] >= 3:
            flag = '   <-- persistent loser'
        elif ss['net'] > 40:
            flag = '   <-- best'
        print('    %-14s n=%-3d WR %5.1f%%  net $%+8.2f%s'
              % (sym, ss['n'], ss['wr'], ss['net'], flag))

    rs = defaultdict(list)
    for t in ts:
        rs[str(t.get('reason', '?')).split('(')[0].strip()].append(t)
    print()
    print('  exit reasons:')
    for r, v in sorted(rs.items(), key=lambda x: -sum(t.get('pnl_usd', 0) for t in x[1])):
        print('    %-26s n=%-3d net $%+8.2f' % (r[:26], len(v),
              sum(t.get('pnl_usd', 0) for t in v)))

    dd = defaultdict(list)
    for t in ts:
        if t.get('close_date'):
            dd[t['close_date']].append(t)
    if dd:
        print()
        print('  last 5 days:')
        for d in sorted(dd)[-5:]:
            ss = stats(dd[d])
            print('    %s  n=%-3d WR %5.1f%%  net $%+8.2f' % (d, ss['n'], ss['wr'], ss['net']))
    print()

print('=' * 78)
print('COMBINED (live books only)')
print('=' * 78)
allt = load(BOOKS[0][1]) + load(BOOKS[1][1])
s = stats(allt)
if s:
    print('  ' + line(s))
    print()
    print('  Reading: PF is gross-win / gross-loss. Below 1.00 means the book')
    print('  loses money regardless of how healthy the win rate looks.')
