#!/usr/bin/env python3
"""Paper-log the "coin just ran >=8% in 24h" signal. Places NOTHING.

Why this exists: the idea backtested at 65.9% against a 50.9% break-even, but
those 138 samples collapsed to just 19 distinct runs once overlapping windows
were merged - error bar +/-11.5 on a 15-point edge. Not enough to trade. So
collect real, independent episodes instead and decide on evidence.

Design notes that matter for the honesty of the sample:

  ONE RECORD PER RUN. A coin that stays up >=8% for six hours is one episode,
  not six. A symbol is not recorded again until EPISODE_COOLDOWN_H has passed
  AND it has dropped back under the threshold - otherwise the log fills with
  correlated copies of the same event, which is exactly the error that made
  the backtest look better than it was.

  BARRIERS FIXED AT RECORD TIME. Target and stop are written when the episode
  opens and never adjusted, so resolution cannot drift toward a nicer answer.

  PUBLIC DATA ONLY. No API key, no account access, no orders. Read-only.
"""
import json, os, time, urllib.request

OUT = '/home/bots/signals/runner_signal.jsonl'
DATA = 'https://fapi.binance.com'

THRESH_PCT = 8.0          # what counts as "just ran"
LEV = 8                   # the leverage the perp books actually use
SL_ROI, TP_ROI = 7.0, 8.0
STOP_PCT = SL_ROI / LEV   # 0.875 % price move
TARG_PCT = TP_ROI / LEV   # 1.000 % price move
HORIZON_H = 24            # how long an episode gets to resolve
EPISODE_COOLDOWN_H = 24
MIN_VOL = 50_000_000      # ignore illiquid coins we would never trade


def get(path, **params):
    q = '&'.join('%s=%s' % (k, v) for k, v in params.items())
    url = '%s%s%s' % (DATA, path, ('?' + q) if q else '')
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read())


def load():
    rows = []
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


def rewrite(rows):
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    os.replace(tmp, OUT)


def record_new(rows):
    """Open an episode for anything that has just crossed the threshold."""
    now = time.time()
    recent = {}
    for r in rows:
        recent[r['symbol']] = max(recent.get(r['symbol'], 0), r['ts'])
    try:
        tk = get('/fapi/v1/ticker/24hr')
    except Exception as e:
        print('ticker fetch failed: %s' % e)
        return rows, 0
    added = 0
    for t in tk:
        sym = t['symbol']
        if not sym.endswith('USDT') or '_' in sym:
            continue
        try:
            chg = float(t['priceChangePercent'])
            px = float(t['lastPrice'])
            vol = float(t['quoteVolume'])
        except Exception:
            continue
        if chg < THRESH_PCT or vol < MIN_VOL or px <= 0:
            continue
        if now - recent.get(sym, 0) < EPISODE_COOLDOWN_H * 3600:
            continue                      # same run, already logged
        rows.append({
            'ts': now, 'symbol': sym, 'entry': px, 'chg24': round(chg, 2),
            'vol_usdt': round(vol),
            # SHORT is the side under test - fading the runner
            'target': px * (1 - TARG_PCT / 100),
            'stop':   px * (1 + STOP_PCT / 100),
            'horizon_h': HORIZON_H, 'lev': LEV,
            'result': None, 'resolved_ts': None,
        })
        added += 1
    return rows, added


def resolve(rows):
    """Walk 1m candles from entry and see which barrier was touched first."""
    now = time.time()
    done = 0
    for r in rows:
        if r.get('result') is not None:
            continue
        age_h = (now - r['ts']) / 3600.0
        if age_h < 0.25:
            continue                      # give it a few candles
        try:
            k = get('/fapi/v1/klines', symbol=r['symbol'], interval='1m',
                    startTime=int(r['ts'] * 1000), limit=1500)
        except Exception:
            continue
        outcome = None
        for c in k:
            hi, lo = float(c[2]), float(c[3])
            if lo <= r['target']:
                outcome = 'win'; break
            if hi >= r['stop']:
                outcome = 'loss'; break
        if outcome is None and age_h >= r['horizon_h']:
            outcome = 'timeout'
        if outcome:
            r['result'] = outcome
            r['resolved_ts'] = now
            done += 1
        time.sleep(0.05)
    return rows, done


def summary(rows):
    fin = [r for r in rows if r.get('result') in ('win', 'loss')]
    n = len(fin)
    w = sum(1 for r in fin if r['result'] == 'win')
    open_n = sum(1 for r in rows if r.get('result') is None)
    to = sum(1 for r in rows if r.get('result') == 'timeout')
    fee = 0.08 * LEV
    be = (SL_ROI + fee) / ((TP_ROI - fee) + (SL_ROI + fee)) * 100
    line = ('episodes: %d resolved (%d win / %d loss), %d open, %d timeout'
            % (n, w, n - w, open_n, to))
    if n:
        p = w / n * 100
        se = (0.25 / n) ** 0.5 * 100
        line += ('\n  hit %.1f%% (+/-%.1f)  break-even %.1f%%  ->  %s'
                 % (p, se, be,
                    'ahead' if p > be else 'behind'))
        if n < 40:
            line += '\n  NOT ENOUGH YET - want ~50 independent episodes'
    return line


if __name__ == '__main__':
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    rows = load()
    rows, added = record_new(rows)
    rows, done = resolve(rows)
    rewrite(rows)
    print('%s  +%d new, %d resolved' % (time.strftime('%Y-%m-%d %H:%M'), added, done))
    print('  ' + summary(rows))
