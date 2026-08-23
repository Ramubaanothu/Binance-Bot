#!/usr/bin/env python3
"""Publish each book's equity for the cross-book exposure cap.

Deliberately OUTSIDE the bots. Wiring it into their boot sequence failed
repeatedly - the call sat next to a log line that demonstrably ran, yet never
fired - and every failure mode was silent because the helper swallowed
exceptions. A separate timer has no coupling to bot internals, cannot deadlock
the cap, and its failures are visible in its own journal.

Reads the exchange directly. If a book cannot be read its file is left stale,
and the cap fails open rather than blocking on a data gap.
"""
import json, os, re, time, hmac, hashlib, urllib.request, urllib.parse

OUT = '/home/bots/shared'
FUT = 'https://testnet.binancefuture.com'
SPOT = 'https://demo-api.binance.com'
STABLES = {'USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD'}


def keys(cfg):
    s = open(cfg, encoding='utf-8').read()
    return (re.search(r'API_KEY\s*=\s*["\']([^"\']+)', s).group(1),
            re.search(r'API_SECRET\s*=\s*["\']([^"\']+)', s).group(1))


def signed(base, path, cfg):
    k, sec = keys(cfg)
    q = 'timestamp=%d&recvWindow=10000' % int(time.time() * 1000)
    sig = hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
    r = urllib.request.Request('%s%s?%s&signature=%s' % (base, path, q, sig),
                               headers={'X-MBX-APIKEY': k})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read())


def write(name, eq):
    p = os.path.join(OUT, 'equity_%s.json' % name)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'equity': round(float(eq), 2), 'ts': time.time()}, f)
    os.replace(tmp, p)
    print('%-8s $%.2f' % (name, eq))


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, cfg, asset in (('main', '/home/bots/main/config.py', 'USDT'),
                             ('reverse', '/home/bots/reverse/config.py', 'USDC')):
        try:
            acc = signed(FUT, '/fapi/v2/account', cfg)
            for a in acc.get('assets', []):
                if a['asset'] == asset:
                    write(name, float(a.get('marginBalance', 0) or 0))
                    break
        except Exception as e:
            print('%-8s SKIP %s' % (name, e))
    try:
        bal = signed(SPOT, '/api/v3/account', '/home/bots/spot/config.py').get('balances', [])
        tot = 0.0
        px = {}
        try:
            d = json.loads(urllib.request.urlopen(SPOT + '/api/v3/ticker/price', timeout=20).read())
            px = {x['symbol']: float(x['price']) for x in d}
        except Exception:
            pass
        for b in bal:
            q = float(b.get('free', 0)) + float(b.get('locked', 0))
            if q <= 0:
                continue
            if b['asset'] in STABLES:
                tot += q
            else:
                tot += q * px.get(b['asset'] + 'USDT', 0.0)
        write('spot', tot)
    except Exception as e:
        print('spot     SKIP %s' % e)


if __name__ == '__main__':
    main()
