# -*- coding: utf-8 -*-
"""ATLAS EVIDENCE REPORT — the Einstein layer.

Reads trade_journal.jsonl (every closed trade, immutable) and answers the only
question that matters: which conditions actually make money? Groups trades by
feature buckets and reports win-rate, expectancy, and profit factor per bucket
so strategy changes are driven by evidence, not by the last loss that hurt.

    python journal_report.py            # full report
    python journal_report.py --min 8    # only buckets with >= 8 trades
"""
import json, sys, pathlib
from collections import defaultdict

MIN_N = 5
for i, a in enumerate(sys.argv):
    if a == '--min' and i + 1 < len(sys.argv):
        MIN_N = int(sys.argv[i + 1])

JF = pathlib.Path(__file__).parent / 'trade_journal.jsonl'
# fall back to trades_binance.json if the journal is short/empty
trades = []
if JF.exists():
    for ln in JF.read_text(encoding='utf-8').splitlines():
        try: trades.append(json.loads(ln))
        except Exception: pass
if len(trades) < 5:
    tf = pathlib.Path(__file__).parent / 'trades_binance.json'
    if tf.exists():
        trades = json.loads(tf.read_text(encoding='utf-8')).get('trades', [])

if not trades:
    print('No trades to analyse yet.'); sys.exit()

C = {'g': '\033[92m', 'r': '\033[91m', 'y': '\033[93m', 'c': '\033[96m',
     'd': '\033[90m', 'b': '\033[1m', 'x': '\033[0m'}

def stats(rows):
    n = len(rows)
    w = sum(1 for t in rows if t.get('is_win'))
    pnl = sum(t.get('pnl_usd', 0) for t in rows)
    gw  = sum(t.get('pnl_usd', 0) for t in rows if t.get('pnl_usd', 0) > 0)
    gl  = abs(sum(t.get('pnl_usd', 0) for t in rows if t.get('pnl_usd', 0) < 0))
    pf  = gw / gl if gl else (99 if gw else 0)
    exp = pnl / n if n else 0
    return n, (w / n * 100 if n else 0), pf, exp, pnl

def bucket_report(title, keyfn):
    groups = defaultdict(list)
    for t in trades:
        try: k = keyfn(t)
        except Exception: k = None
        if k is not None: groups[k].append(t)
    rows = []
    for k, g in groups.items():
        n, wr, pf, exp, pnl = stats(g)
        if n >= MIN_N: rows.append((k, n, wr, pf, exp, pnl))
    if not rows: return
    rows.sort(key=lambda r: -r[4])   # by expectancy
    print(f"\n{C['b']}{C['c']}══ {title} ══{C['x']}")
    print(f"  {'bucket':<20}{'N':>4}{'Win%':>7}{'PF':>7}{'Exp$':>9}{'Total$':>10}")
    for k, n, wr, pf, exp, pnl in rows:
        wc = C['g'] if wr >= 50 else C['r']
        ec = C['g'] if exp > 0 else C['r']
        print(f"  {str(k):<20}{n:>4}{wc}{wr:>6.0f}%{C['x']}{pf:>7.2f}"
              f"{ec}{exp:>8.2f}{C['x']}{pnl:>+10.2f}")

# ── overall ──
n, wr, pf, exp, pnl = stats(trades)
print(f"\n{C['b']}ATLAS EVIDENCE REPORT{C['x']}  ·  {n} trades")
oc = C['g'] if pnl > 0 else C['r']
print(f"  Win rate {wr:.1f}%   Profit factor {pf:.2f}   "
      f"Expectancy {exp:+.2f}$/trade   Total {oc}{pnl:+.2f}${C['x']}")
print(f"  {C['d']}(buckets need >= {MIN_N} trades; sorted by expectancy — best at top){C['x']}")

def conf_bucket(t):
    c = t.get('conf', 0)
    return f"{int(c // 10) * 10}-{int(c // 10) * 10 + 9}%"
def rsi_bucket(t):
    r = t.get('rsi', 0)
    return 'oversold<35' if r < 35 else ('overbought>65' if r > 65 else 'mid 35-65')
def adx_bucket(t):
    a = t.get('adx', 0)
    return 'weak<25' if a < 25 else ('strong>40' if a > 40 else 'ok 25-40')
def hour_bucket(t):
    h = t.get('entry_hour_ist')
    if h is None: return None
    return f"{int(h):02d}:00 IST"

bucket_report('DIRECTION',      lambda t: t.get('direction', '?').upper())
bucket_report('SESSION',        lambda t: (t.get('session') or '?'))
bucket_report('REGIME',         lambda t: (t.get('regime') or '?'))
bucket_report('EXIT REASON',    lambda t: (t.get('reason') or '?'))
bucket_report('CONFIDENCE',     conf_bucket)
bucket_report('ENTRY RSI',      rsi_bucket)
bucket_report('ENTRY ADX',      adx_bucket)
bucket_report('MAJOR vs ALT',   lambda t: 'MAJOR' if t.get('is_major') else 'ALT')
bucket_report('RUNNER vs SCALP', lambda t: 'RUNNER' if t.get('is_runner') else 'SCALP')
bucket_report('ENTRY HOUR',     hour_bucket)
bucket_report('TOP SYMBOLS',    lambda t: t.get('symbol', '?'))

# ── verdict ──
print(f"\n{C['b']}{C['y']}VERDICT{C['x']}")
def best_worst(title, keyfn):
    groups = defaultdict(list)
    for t in trades:
        try: k = keyfn(t)
        except Exception: k = None
        if k is not None: groups[k].append(t)
    scored = [(k, *stats(g)) for k, g in groups.items() if len(g) >= MIN_N]
    if len(scored) < 2: return
    scored.sort(key=lambda r: -r[4])
    b, w = scored[0], scored[-1]
    print(f"  {title:<12} best: {C['g']}{b[0]} ({b[4]:+.2f}$/trade, {b[2]:.0f}% WR){C['x']}"
          f"   worst: {C['r']}{w[0]} ({w[4]:+.2f}$/trade){C['x']}")
best_worst('Direction', lambda t: t.get('direction', '?').upper())
best_worst('Session',   lambda t: (t.get('session') or '?'))
best_worst('Regime',    lambda t: (t.get('regime') or '?'))
best_worst('Major/Alt', lambda t: 'MAJOR' if t.get('is_major') else 'ALT')
print(f"  {C['d']}Change ONE variable toward the green, freeze, collect 50+ trades, repeat.{C['x']}\n")
