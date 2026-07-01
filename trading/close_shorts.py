"""
Emergency close script — closes specific losing short positions immediately.
Run from the trading/ folder:  python close_shorts.py
"""
import sys, time, hmac, hashlib, requests

sys.path.insert(0, '.')
import config

BASE = 'https://testnet.binancefuture.com'
sess = requests.Session()
sess.headers['X-MBX-APIKEY'] = config.API_KEY

TARGETS = {'HUMAUSDT', 'LTCUSDT', 'AAVEUSDT', 'UNIUSDT'}


def _sign(params: dict) -> str:
    params['timestamp'] = int(time.time() * 1000)
    qs  = '&'.join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(config.API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return qs + '&signature=' + sig


def get_positions():
    p = {}
    url = f"{BASE}/fapi/v2/positionRisk?{_sign(p)}"
    r = sess.get(url, timeout=12)
    r.raise_for_status()
    return r.json()


def close_position(sym: str, side: str, qty: float):
    p = {
        'symbol': sym, 'side': side,
        'type': 'MARKET', 'quantity': f"{qty:.8f}".rstrip('0').rstrip('.'),
        'reduceOnly': 'true',
    }
    url = f"{BASE}/fapi/v1/order?{_sign(p)}"
    r = sess.post(url, timeout=12)
    return r.json()


def get_price(sym: str) -> float:
    p = {'symbol': sym}
    url = f"{BASE}/fapi/v1/ticker/price?{'&'.join(f'{k}={v}' for k,v in p.items())}"
    r = sess.get(url, timeout=12)
    return float(r.json()['price'])


if __name__ == '__main__':
    print('AlphaBot — Emergency Short Closer')
    print('=' * 45)

    try:
        positions = get_positions()
    except Exception as e:
        print(f'ERROR: Could not fetch positions: {e}')
        sys.exit(1)

    found = 0
    for pos in positions:
        sym = pos['symbol']
        if sym not in TARGETS:
            continue

        amt = float(pos['positionAmt'])
        if abs(amt) < 1e-9:
            print(f'{sym:<14} already closed (qty=0)')
            continue

        direction  = 'LONG' if amt > 0 else 'SHORT'
        entry      = float(pos['entryPrice'])
        close_side = 'SELL' if amt > 0 else 'BUY'

        try:
            price   = get_price(sym)
            pnl_pct = ((price - entry) / entry if amt > 0 else (entry - price) / entry) * 100
        except Exception:
            price   = float(pos.get('markPrice', entry))
            pnl_pct = 0.0

        print(f'{sym:<14} {direction} qty={abs(amt):.4f}  entry={entry:.6g}  '
              f'now={price:.6g}  P&L={pnl_pct:+.2f}% ...', end='  ')

        try:
            resp = close_position(sym, close_side, abs(amt))
            status = resp.get('status', '?')
            if status == 'FILLED':
                print(f'CLOSED ✓')
            else:
                print(f'resp={resp}')
        except Exception as e:
            print(f'FAILED: {e}')

        found += 1
        time.sleep(0.5)

    if found == 0:
        print('None of the target positions were found (may already be closed).')

    print('=' * 45)
    print('Done. Restart the bot (START.bat) to reload positions.')
