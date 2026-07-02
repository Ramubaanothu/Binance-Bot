"""
AlphaBot v5.0 — Advanced Binance Futures Testnet Bot
──────────────────────────────────────────────────────
• Scans top 80 USDT perpetual futures (all coins)
• Multi-timeframe: 5m + 15m + 1h analysis
• 22 indicators including Ichimoku, Squeeze, Supertrend, Hull MA
• 14 candlestick patterns with bonus scoring
• LONG + SHORT bidirectional trading
• Open Interest momentum + Long/Short ratio contrarian
• Funding rate trend (last 3 readings)
• Dynamic regime detection → adjusted indicator weights
• Kelly Criterion position sizing
• Triple TP system (TP1→TP2→TP3 trail)
• Breakeven lock + ATR-based trailing stop
• Daily loss circuit breaker + Max drawdown guard
• Session-aware (Asia/EU/US hours)
• Real-time performance: Sharpe, Sortino, Win rate, Calmar
• WebSocket broadcast to dashboards on port 8765
"""

import asyncio
import json
import time
import hmac
import hashlib
import logging
import math
import threading
from pathlib import Path
from datetime import datetime, date
from collections import deque, defaultdict

import requests
import numpy as np
import pandas as pd
import websockets

import config
from indicators import TAEngine, SignalResult

# ─── Logger ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8'),
    ]
)
log = logging.getLogger('AlphaBot')
BASE = 'https://testnet.binancefuture.com'
# Real-time push streams (mark prices + account events) — matches BASE environment
WS_STREAM = 'wss://stream.binancefuture.com' if 'testnet' in BASE else 'wss://fstream.binance.com'


# ─── REST Client ──────────────────────────────────────────────────────────────
class Client:
    def __init__(self):
        self.key    = config.API_KEY
        self.secret = config.API_SECRET
        self.sess   = requests.Session()
        self.sess.headers['X-MBX-APIKEY'] = self.key
        self.step: dict[str, float] = {}
        self._rate_ts   = 0.0
        self._rate_lock = threading.Lock()
        self._time_offset = 0.0   # ms: Binance server time - local time
        self._offset_ts   = 0.0
        self.sync_time()

    def sync_time(self):
        """PC clocks drift — Binance rejects requests >1s off (-1021).
        Measure the offset against the server and apply it to every timestamp."""
        try:
            t0 = time.time() * 1000
            st = float(self._get('/fapi/v1/time')['serverTime'])
            t1 = time.time() * 1000
            self._time_offset = st - (t0 + t1) / 2
            self._offset_ts   = time.time()
            log.info(f"[TIME] clock offset vs Binance: {self._time_offset:+.0f}ms (auto-corrected)")
        except Exception as e:
            log.warning(f"[TIME] server time sync failed: {e}")

    def _throttle(self):
        with self._rate_lock:
            now = time.time()
            gap = config.RATE_LIMIT_DELAY - (now - self._rate_ts)
            self._rate_ts = time.time() + max(0.0, gap)
        if gap > 0:
            time.sleep(gap)

    def _sign(self, p: dict) -> str:
        if time.time() - self._offset_ts > 1800:   # re-measure drift every 30 min
            self._offset_ts = time.time()          # set first — avoids recursion loops
            self.sync_time()
        p['timestamp']  = int(time.time() * 1000 + self._time_offset)
        p['recvWindow'] = 10000
        qs  = '&'.join(f"{k}={v}" for k, v in p.items())
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    def _get(self, path, params=None, auth=False):
        self._throttle()
        params = dict(params or {})
        if auth:
            url = f"{BASE}{path}?{self._sign(dict(params))}"
        else:
            qs  = '&'.join(f"{k}={v}" for k, v in params.items())
            url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
        r = self.sess.get(url, timeout=12)
        if auth and r.status_code >= 400 and '-1021' in r.text:
            self.sync_time()   # clock drifted — resync and retry once
            r = self.sess.get(f"{BASE}{path}?{self._sign(dict(params))}", timeout=12)
        r.raise_for_status(); return r.json()

    def _send(self, method, path, params: dict):
        r = getattr(self.sess, method)(f"{BASE}{path}?{self._sign(dict(params))}", timeout=12)
        if r.status_code >= 400 and '-1021' in r.text:
            self.sync_time()   # clock drifted — resync and retry once
            r = getattr(self.sess, method)(f"{BASE}{path}?{self._sign(dict(params))}", timeout=12)
        if r.status_code >= 400:
            # Surface Binance's error code/msg (e.g. -1111 precision, -2019 margin)
            raise RuntimeError(f"{r.status_code} {path}: {r.text[:200]}")
        return r.json()

    def _post(self, path, params: dict):
        return self._send('post', path, params)

    def _delete(self, path, params: dict):
        return self._send('delete', path, params)

    # ── Public endpoints ──
    def klines(self, sym, tf, n=120):
        return self._get('/fapi/v1/klines', {'symbol': sym, 'interval': tf, 'limit': n})

    def price(self, sym) -> float:
        return float(self._get('/fapi/v1/ticker/price', {'symbol': sym})['price'])

    def tickers_24h(self):
        return self._get('/fapi/v1/ticker/24hr')

    def exchange_info(self):
        return self._get('/fapi/v1/exchangeInfo')

    def open_interest_hist(self, sym, period='5m', n=3):
        try:
            return self._get('/futures/data/openInterestHist',
                             {'symbol': sym, 'period': period, 'limit': n})
        except: return []

    def long_short_ratio(self, sym, period='5m') -> float:
        try:
            d = self._get('/futures/data/globalLongShortAccountRatio',
                          {'symbol': sym, 'period': period, 'limit': 1})
            return float(d[0]['longShortRatio']) if d else 1.0
        except: return 1.0

    def funding_history(self, sym, n=3):
        try:
            return self._get('/fapi/v1/fundingRate', {'symbol': sym, 'limit': n})
        except: return []

    # ── Private endpoints ──
    def account(self):
        return self._get('/fapi/v2/account', {}, auth=True)

    def set_leverage(self, sym, lev):
        return self._post('/fapi/v1/leverage', {'symbol': sym, 'leverage': lev})

    def set_margin_type(self, sym):
        try: self._post('/fapi/v1/marginType', {'symbol': sym, 'marginType': 'ISOLATED'})
        except: pass

    @staticmethod
    def _fmt_qty(qty) -> str:
        """326.0 → '326', 0.0010 → '0.001' — Binance rejects trailing zeros beyond stepSize."""
        return f'{float(qty):.8f}'.rstrip('0').rstrip('.')

    def market_order(self, sym, side, qty, reduce=False, current_price=0.0):
        if config.PAPER_MODE:
            return {'orderId': f'PAPER-{int(time.time()*1000)}', 'avgPrice': str(current_price), 'status': 'FILLED'}
        p = {'symbol': sym, 'side': side, 'type': 'MARKET', 'quantity': self._fmt_qty(qty)}
        if reduce: p['reduceOnly'] = 'true'
        return self._post('/fapi/v1/order', p)

    def stop_market_order(self, sym, side, qty, stop_price):
        """Place STOP_MARKET order on exchange (hard stop-loss)."""
        if config.PAPER_MODE:
            return {'orderId': f'PAPER-SL-{int(time.time()*1000)}'}
        p = {
            'symbol':      sym, 'side': side,
            'type':        'STOP_MARKET',
            'quantity':    self._fmt_qty(qty),
            'stopPrice':   f"{stop_price:.8f}".rstrip('0').rstrip('.'),
            'closeOnly':   'true',
            'workingType': 'CONTRACT_PRICE',
        }
        try:
            return self._post('/fapi/v1/order', p)
        except requests.HTTPError as e:
            detail = ''
            try: detail = e.response.json()
            except: pass
            raise requests.HTTPError(f"{e} | detail={detail}", response=e.response)

    def take_profit_market_order(self, sym, side, qty, stop_price):
        """Place TAKE_PROFIT_MARKET order on exchange (exchange-side TP1)."""
        if config.PAPER_MODE:
            return {'orderId': f'PAPER-TP-{int(time.time()*1000)}'}
        p = {
            'symbol':      sym, 'side': side,
            'type':        'TAKE_PROFIT_MARKET',
            'quantity':    self._fmt_qty(qty),
            'stopPrice':   f"{stop_price:.8f}".rstrip('0').rstrip('.'),
            'closeOnly':   'true',
            'workingType': 'CONTRACT_PRICE',
        }
        try:
            return self._post('/fapi/v1/order', p)
        except requests.HTTPError as e:
            detail = ''
            try: detail = e.response.json()
            except: pass
            raise requests.HTTPError(f"{e} | detail={detail}", response=e.response)

    def listen_key(self) -> str:
        """User-data stream key — lets Binance push account/position events to us."""
        r = self.sess.post(f"{BASE}/fapi/v1/listenKey", timeout=10)
        r.raise_for_status()
        return r.json()['listenKey']

    def keepalive_listen_key(self):
        try: self.sess.put(f"{BASE}/fapi/v1/listenKey", timeout=10)
        except Exception: pass

    def cancel_order(self, sym, order_id):
        if config.PAPER_MODE or not order_id or str(order_id).startswith('PAPER'):
            return
        try:
            self._delete('/fapi/v1/order', {'symbol': sym, 'orderId': order_id})
        except Exception: pass

    def cancel_all_orders(self, sym):
        if config.PAPER_MODE: return
        try:
            self._delete('/fapi/v1/allOpenOrders', {'symbol': sym})
        except Exception: pass

    def position_risk(self):
        """All open positions from exchange (/fapi/v2/positionRisk)."""
        return self._get('/fapi/v2/positionRisk', {}, auth=True)

    def usdt_balance(self, paper_balance: float = 0.0) -> float:
        if config.PAPER_MODE and paper_balance > 0:
            return paper_balance
        try:
            acc = self.account()
            for a in acc.get('assets', []):
                if a['asset'] == 'USDT': return float(a['availableBalance'])
            wb = float(acc.get('totalWalletBalance', 0))
            if wb > 0: return wb
            return 10000.0
        except Exception as e:
            log.warning(f"balance error ({e}) — paper mode: $10000 USDT")
            return 10000.0

    def load_symbols(self):
        info = self.exchange_info()
        for s in info.get('symbols', []):
            for f in s.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    self.step[s['symbol']] = float(f['stepSize'])

    def round_qty(self, sym: str, qty: float) -> float:
        step = self.step.get(sym, 0.001)
        if step == 0: return round(qty, 3)
        prec = len(str(step).rstrip('0').split('.')[-1]) if '.' in str(step) else 0
        return round(math.floor(qty / step) * step, prec)


# ─── Market Context (OI, funding, L/S) ────────────────────────────────────────
class MarketContext:
    def __init__(self, client: Client):
        self.c    = client
        self._cache: dict[str, dict] = {}
        self._ts:    dict[str, float] = {}
        self.TTL  = 300

    def get(self, sym: str) -> dict:
        now = time.time()
        if sym in self._cache and now - self._ts.get(sym, 0) < self.TTL:
            return self._cache[sym]

        ctx = {'oi_bias': 0, 'ls_ratio': 1.0, 'ls_bias': 0, 'funding_trend': 0, 'funding_rate': 0.0}
        try:
            oi_hist = self.c.open_interest_hist(sym, '5m', 3)
            if len(oi_hist) >= 2:
                oi_now  = float(oi_hist[-1]['sumOpenInterest'])
                oi_prev = float(oi_hist[0]['sumOpenInterest'])
                oi_chg  = (oi_now - oi_prev) / max(oi_prev, 1e-9) * 100
                ctx['oi_bias'] = min(max(oi_chg * 10, -100), 100)

            ls = self.c.long_short_ratio(sym)
            ctx['ls_ratio'] = ls
            if ls > 1.8:   ctx['ls_bias'] = -30
            elif ls < 0.6: ctx['ls_bias'] = +30

            fh = self.c.funding_history(sym, 3)
            if fh:
                rates = [float(f['fundingRate']) for f in fh]
                ctx['funding_rate']  = rates[-1] * 100
                if len(rates) >= 2:
                    ctx['funding_trend'] = +1 if rates[-1] > rates[0] else (-1 if rates[-1] < rates[0] else 0)
        except:
            pass

        self._cache[sym] = ctx
        self._ts[sym]    = now
        return ctx


# ─── Performance Tracker ──────────────────────────────────────────────────────
class PerfTracker:
    def __init__(self):
        self.returns: deque = deque(maxlen=200)
        self.win_streak      = 0
        self.loss_streak     = 0
        self.max_win_streak  = 0

    def add(self, pnl_pct: float, is_win: bool):
        self.returns.append(pnl_pct)
        if is_win:
            self.win_streak += 1; self.loss_streak = 0
            self.max_win_streak = max(self.max_win_streak, self.win_streak)
        else:
            self.loss_streak += 1; self.win_streak = 0

    @property
    def sharpe(self) -> float:
        if len(self.returns) < 5: return 0.0
        r = np.array(self.returns)
        return float(np.mean(r) / (np.std(r) + 1e-9) * np.sqrt(252))

    @property
    def sortino(self) -> float:
        if len(self.returns) < 5: return 0.0
        r = np.array(self.returns)
        neg = r[r < 0]
        return float(np.mean(r) / (np.std(neg) + 1e-9) * np.sqrt(252)) if len(neg) else 0.0

    def summary(self) -> dict:
        return {
            'sharpe':          round(self.sharpe, 2),
            'sortino':         round(self.sortino, 2),
            'win_streak':      self.win_streak,
            'loss_streak':     self.loss_streak,
            'max_win_streak':  self.max_win_streak,
        }


# ─── Session helper ───────────────────────────────────────────────────────────
def current_session() -> str:
    h = datetime.utcnow().hour
    if 0 <= h < 9:   return 'Asia'
    if 9 <= h < 17:  return 'Europe'
    return 'US'

def session_min_conf() -> float:
    return {
        'Asia':   config.MIN_CONFIDENCE + 5,
        'Europe': config.MIN_CONFIDENCE - 2,
        'US':     config.MIN_CONFIDENCE - 5,
    }.get(current_session(), config.MIN_CONFIDENCE)


# ─── Kelly Criterion ──────────────────────────────────────────────────────────
def kelly_size(win_rate: float, avg_win: float, avg_loss: float,
               base_pct: float, atr_pct: float) -> float:
    if avg_loss == 0 or win_rate <= 0: return base_pct
    odds   = abs(avg_win / avg_loss) if avg_loss else 1.0
    q      = 1 - win_rate
    k_full = (win_rate * odds - q) / max(odds, 0.001)
    k_half = max(base_pct * 0.3, min(k_full * 0.5, 0.10))
    if atr_pct > 3.0:   k_half *= 0.5
    elif atr_pct > 2.0: k_half *= 0.7
    return round(max(0.01, k_half), 4)


# ─── AlphaBot ─────────────────────────────────────────────────────────────────
class AlphaBot:
    def __init__(self):
        self.client   = Client()
        self.ctx_mgr  = MarketContext(self.client)
        self.perf     = PerfTracker()

        self.positions: dict[str, dict] = {}
        self.trades:    list[dict]       = []
        self.scan_log:  deque            = deque(maxlen=2000)
        self.scan_results: list[dict]    = []
        self.ws_clients: set             = set()
        self.market_intel: dict          = {}   # movers, market mood, breadth

        self.balance      = 0.0
        self.paper_balance= 10000.0   # tracks paper P&L internally
        self.start_bal    = 0.0
        self.peak_bal     = 0.0
        self.daily_pnl    = 0.0
        self.daily_start  = 0.0
        self.trade_day    = date.today()

        self.wins         = 0
        self.losses       = 0
        self.total_pnl    = 0.0
        self.avg_win      = 0.0
        self.avg_loss     = 0.0
        self.scan_count   = 0
        self._last_risk_sync = 0.0   # unix ts of last positionRisk sync
        self._mark_prices: dict[str, float] = {}   # real-time mark prices pushed by Binance
        self._mark_stream_ts = 0.0                 # last mark-price push received
        self._upd_busy = False                     # re-entry guard for live update loop
        self.running      = True
        self.paused       = False
        self.pause_reason = ''
        self.consec_loss_pause_until = 0.0   # unix ts — new entries blocked until this time
        self._btc_trend: str = 'neutral'     # 'bull' | 'bear' | 'neutral' — 1H macro filter
        self._btc_4h_trend: str = 'neutral'  # 4H institutional timeframe
        self._btc_daily_bull: bool = True    # daily macro regime
        self._btc_5m_mom: float = 0.0       # BTC 10-min momentum for altcoin timing
        self._btc_rsi:   float = 50.0        # BTC 1h RSI — whale capitulation detector
        self._fear_greed: int  = 50          # 0=extreme fear, 100=extreme greed
        self._fear_greed_ts: float = 0.0
        self._sentiment: dict  = {}          # funding rates for majors

        self.sector: dict[str, str] = {
            'BTCUSDT':'L1','ETHUSDT':'L1','BNBUSDT':'L1','SOLUSDT':'L1',
            'AVAXUSDT':'L1','DOTUSDT':'L1','ADAUSDT':'L1','MATICUSDT':'L1',
            'XRPUSDT':'Pay','XLMUSDT':'Pay','LTCUSDT':'Pay','BCHUSDT':'Pay',
            'DOGEUSDT':'Meme','SHIBUSDT':'Meme','PEPEUSDT':'Meme','FLOKIUSDT':'Meme',
            'LINKUSDT':'Ora','BANDUSDT':'Ora',
            'UNIUSDT':'DeFi','AAVEUSDT':'DeFi','SUSHIUSDT':'DeFi','CRVUSDT':'DeFi',
        }
        self._load_trades()
        self._load_positions()   # restore positions from disk on every startup

    _TRADES_FILE    = Path(__file__).parent / 'trades_binance.json'
    _POSITIONS_FILE = Path(__file__).parent / 'positions_binance.json'

    def _load_trades(self):
        if not self._TRADES_FILE.exists():
            return
        try:
            data = json.loads(self._TRADES_FILE.read_text(encoding='utf-8'))
            self.trades  = data.get('trades', [])
            self.wins    = data.get('wins', 0)
            self.losses  = data.get('losses', 0)
            self.total_pnl = data.get('total_pnl', 0.0)
            self.avg_win   = data.get('avg_win', 0.0)
            self.avg_loss  = data.get('avg_loss', 0.0)
            log.info(f"[STATE  ] Loaded {len(self.trades)} trades from disk ({self.wins}W/{self.losses}L)")
        except Exception as e:
            log.warning(f"[STATE  ] Could not load trades: {e}")

    def _save_trades(self):
        try:
            self._TRADES_FILE.write_text(json.dumps({
                'trades':    self.trades[-500:],
                'wins':      self.wins,
                'losses':    self.losses,
                'total_pnl': round(self.total_pnl, 4),
                'avg_win':   round(self.avg_win, 4),
                'avg_loss':  round(self.avg_loss, 4),
            }, default=str), encoding='utf-8')
        except Exception as e:
            log.warning(f"[STATE  ] Trade save error: {e}")

    # ── Position persistence ──────────────────────────────────────────────────
    def _load_positions(self):
        if not self._POSITIONS_FILE.exists():
            return
        try:
            data = json.loads(self._POSITIONS_FILE.read_text(encoding='utf-8'))
            self.positions = data.get('positions', {})
            if self.positions:
                log.info(f"[STATE  ] Loaded {len(self.positions)} open positions from disk")
        except Exception as e:
            log.warning(f"[STATE  ] Could not load positions: {e}")

    def _save_positions(self):
        try:
            self._POSITIONS_FILE.write_text(
                json.dumps({'positions': self.positions}, default=str),
                encoding='utf-8'
            )
        except Exception as e:
            log.warning(f"[STATE  ] Position save error: {e}")

    # ── Exchange order guards (SL + TP1 placed as real orders) ───────────────
    def _place_exchange_guards(self, sym: str, pos: dict):
        """Place STOP_MARKET + TAKE_PROFIT_MARKET on exchange at entry.
        NOTE: Binance Futures testnet disables conditional orders (error -4120).
        In production these work and protect positions when bot is offline.
        The bot's internal update_positions() loop manages SL/TP while running."""
        d          = pos['direction']
        close_side = 'SELL' if d == 'long' else 'BUY'
        qty        = pos['qty']
        try:
            sl_resp = self.client.stop_market_order(sym, close_side, qty, pos['sl'])
            pos['sl_order_id'] = str(sl_resp.get('orderId', ''))
            log.info(f"[GUARDS ] {sym} SL={pos['sl']:.6g} placed (#{pos['sl_order_id']})")
        except Exception as e:
            errmsg = str(e)
            if '-4120' in errmsg:
                log.debug(f"[GUARDS ] {sym} SL: testnet conditional orders disabled (production-only feature)")
            else:
                log.warning(f"[GUARDS ] {sym} SL order failed: {errmsg[:80]}")
        try:
            tp_resp = self.client.take_profit_market_order(sym, close_side, qty, pos['tp1'])
            pos['tp1_order_id'] = str(tp_resp.get('orderId', ''))
            log.info(f"[GUARDS ] {sym} TP1={pos['tp1']:.6g} placed (#{pos['tp1_order_id']})")
        except Exception as e:
            errmsg = str(e)
            if '-4120' in errmsg:
                log.debug(f"[GUARDS ] {sym} TP1: testnet conditional orders disabled (production-only feature)")
            else:
                log.warning(f"[GUARDS ] {sym} TP1 order failed: {errmsg[:80]}")

    def _cancel_exchange_guards(self, sym: str, pos: dict):
        """Cancel all open orders for sym when we are closing the position ourselves."""
        try:
            self.client.cancel_all_orders(sym)
        except Exception:
            pass

    def _move_sl_to_breakeven(self, sym: str, pos: dict):
        """After TP1 hits: cancel old SL + TP1 orders, place new SL at entry (breakeven)."""
        d          = pos['direction']
        sign       = +1 if d == 'long' else -1
        be_price   = round(pos['entry'] * (1 + sign * 0.0005), 8)  # entry + 0.05% buffer
        close_side = 'SELL' if d == 'long' else 'BUY'
        try:
            self.client.cancel_all_orders(sym)
            time.sleep(0.3)
            sl_resp = self.client.stop_market_order(sym, close_side, pos['qty'], be_price)
            pos['sl_order_id']  = str(sl_resp.get('orderId', ''))
            pos['tp1_order_id'] = ''
            pos['trail_sl']     = be_price
            log.info(f"[GUARDS ] {sym} SL moved to BE={be_price:.6g} (#{pos['sl_order_id']})")
        except Exception as e:
            errmsg = str(e)
            pos['trail_sl'] = be_price  # still update internal SL even if exchange order fails
            if '-4120' not in errmsg:
                log.warning(f"[GUARDS ] {sym} BE move failed: {errmsg[:80]}")

    # ── Startup reconciliation ────────────────────────────────────────────────
    async def _reconcile_positions(self):
        """Verify disk positions against exchange AND import any orphan exchange positions."""
        self.emit('info', "[RECONCILE] Syncing with exchange...")
        try:
            loop  = asyncio.get_event_loop()
            risks = await loop.run_in_executor(None, self.client.position_risk)

            # Build map: symbol → full exchange position row
            ex_map = {
                r['symbol']: r
                for r in risks
                if abs(float(r.get('positionAmt', 0))) > 1e-9
            }

            # 1. Remove stale disk positions (closed on exchange)
            stale = [s for s in list(self.positions) if s not in ex_map]
            for sym in stale:
                self.emit('warn', f"[RECONCILE] {sym} closed on exchange — removing from bot")
                del self.positions[sym]

            # 2. Re-place guards for known positions
            for sym, pos in list(self.positions.items()):
                self.emit('info', f"[RECONCILE] {sym} LIVE — refreshing exchange guards")
                await loop.run_in_executor(None, self.client.cancel_all_orders, sym)
                await asyncio.sleep(0.3)
                self._place_exchange_guards(sym, pos)

            # 3. Evaluate orphan exchange positions (positions bot doesn't know about)
            orphans = [s for s in ex_map if s not in self.positions]
            imported_n = 0
            closed_n   = 0

            if orphans:
                self.emit('warn',
                    f"[RECONCILE] {len(orphans)} orphan(s) found — evaluating before import")

                lev_info = {}
                try:
                    acc = await loop.run_in_executor(None, self.client.account)
                    for p in acc.get('positions', []):
                        lev_info[p['symbol']] = int(p.get('leverage', config.LEVERAGE))
                except Exception:
                    pass

                # Build scored list: (pnl_pct, sym, r, amt, direction, entry, lev, price)
                scored = []
                for sym in orphans:
                    r   = ex_map[sym]
                    amt = float(r['positionAmt'])
                    if amt == 0:
                        continue
                    direction = 'long' if amt > 0 else 'short'
                    entry     = float(r['entryPrice'])
                    lev       = lev_info.get(sym, config.MAJOR_LEVERAGE.get(sym, config.LEVERAGE))
                    try:
                        price = await loop.run_in_executor(None, self.client.price, sym)
                    except Exception:
                        price = float(r.get('markPrice', entry))
                    if entry <= 0:
                        price = price or 1.0; entry = price
                    pnl_pct = ((price - entry) / entry if direction == 'long'
                               else (entry - price) / entry) * 100 * lev
                    scored.append((pnl_pct, sym, r, amt, direction, entry, lev, price))

                # Rule A — close immediately if already losing > 3%
                # (no real ATR data; too dangerous to manage blind)
                to_import = []
                for row in scored:
                    pnl_pct, sym, r, amt, direction, entry, lev, price = row
                    if pnl_pct < -3.0:
                        close_side = 'SELL' if direction == 'long' else 'BUY'
                        self.emit('warn',
                            f"[RECONCILE] {sym} orphan losing {pnl_pct:.1f}% "
                            f"— closing immediately (no SL data)")
                        try:
                            await loop.run_in_executor(
                                None, self.client.market_order,
                                sym, close_side, abs(amt), True
                            )
                            closed_n += 1
                            self.emit('info', f"[RECONCILE] Closed orphan {sym}")
                        except Exception as e:
                            self.emit('warn', f"[RECONCILE] Close failed {sym}: {e}")
                        await asyncio.sleep(0.3)
                    else:
                        to_import.append(row)

                # Rule B — cap imports at remaining position slots
                slots = max(0, config.MAX_POSITIONS - len(self.positions))
                if len(to_import) > slots:
                    # Keep best P&L orphans, close the rest
                    to_import.sort(key=lambda x: x[0], reverse=True)
                    excess = to_import[slots:]
                    to_import = to_import[:slots]
                    for pnl_pct, sym, r, amt, direction, entry, lev, price in excess:
                        close_side = 'SELL' if direction == 'long' else 'BUY'
                        self.emit('warn',
                            f"[RECONCILE] {sym} over position cap — closing")
                        try:
                            await loop.run_in_executor(
                                None, self.client.market_order,
                                sym, close_side, abs(amt), True
                            )
                            closed_n += 1
                        except Exception as e:
                            self.emit('warn', f"[RECONCILE] Close failed {sym}: {e}")
                        await asyncio.sleep(0.3)

                # Import remaining survivors
                for pnl_pct, sym, r, amt, direction, entry, lev, price in to_import:
                    atr   = price * 0.015
                    exits = self.compute_exits(direction, entry, atr, sym)
                    qty   = abs(amt)
                    self.positions[sym] = {
                        'symbol':    sym,
                        'direction': direction,
                        'entry':     entry,
                        'current':   price,
                        'qty':       qty,
                        'size_usd':  round(qty * entry, 4),
                        'leverage':  lev,
                        'is_major':  sym in config.MAJOR_LEVERAGE,
                        'sl':        exits['sl'],
                        'tp1':       exits['tp1'],
                        'tp2':       exits['tp2'],
                        'tp3':       exits['tp3'],
                        'trail_sl':  exits['sl'],
                        'trail_dist':exits['trail_dist'],
                        'be_locked': False,
                        'tp1_hit':   False,
                        'tp2_hit':   False,
                        'phase':     'imported',
                        'pnl_pct':   round(pnl_pct, 2),
                        'pnl_usd':   0.0,
                        'conf':      50,
                        'regime':    'unknown',
                        'patterns':  [],
                        'atr':       atr,
                        'atr_pct':   1.5,
                        'adx':       25.0,
                        'rsi':       50.0,
                        'cloud_bull':direction == 'long',
                        'squeeze':   False,
                        'funding':   0.0,
                        'ls_ratio':  1.0,
                        'oi_bias':   0,
                        'pivot':     {},
                        'open_time': datetime.now().strftime('%H:%M:%S'),
                        'session':   current_session(),
                        'vol_ratio': 1.0,
                        'rr_ratio':  1.8,
                        'order_id':     '',
                        'sl_order_id':  '',
                        'tp1_order_id': '',
                    }
                    self._place_exchange_guards(sym, self.positions[sym])
                    self.emit('info',
                        f"[RECONCILE] IMPORTED {direction.upper()} {sym} "
                        f"entry={entry:.6g} qty={qty} lev={lev}x P&L={pnl_pct:.1f}%"
                    )
                    imported_n += 1
                    await asyncio.sleep(0.3)

            self._save_positions()
            self.emit('info',
                f"[RECONCILE] Done — {len(self.positions)} active | "
                f"{imported_n} imported | {closed_n} orphans closed"
            )
        except Exception as e:
            self.emit('warn', f"[RECONCILE] Failed: {e}")

    def emit(self, kind: str, msg: str):
        e = {'ts': datetime.now().strftime('%H:%M:%S'), 'type': kind, 'msg': msg}
        self.scan_log.appendleft(e)
        log.info(f"[{kind.upper():8}] {msg}")

    async def broadcast(self, data: dict):
        if not self.ws_clients: return
        pl   = json.dumps(data, default=str)
        dead = set()
        for ws in list(self.ws_clients):
            try: await ws.send(pl)
            except: dead.add(ws)
        self.ws_clients -= dead

    async def push(self):
        tot = self.wins + self.losses
        wr  = round(self.wins / tot * 100, 1) if tot else 0
        dd  = round((self.peak_bal - self.balance) / self.peak_bal * 100, 2) if self.peak_bal else 0
        await self.broadcast({
            'type':         'state',
            'ts':           datetime.now().strftime('%H:%M:%S'),
            'balance':      round(self.balance, 4),
            'start_bal':    round(self.start_bal, 4),
            'peak_bal':     round(self.peak_bal, 4),
            'daily_pnl':    round(self.daily_pnl, 4),
            'total_pnl':    round(self.total_pnl, 4),
            'wins':         self.wins,
            'losses':       self.losses,
            'win_rate':     wr,
            'max_dd':       dd,
            'scan_count':   self.scan_count,
            'session':      current_session(),
            'paused':       self.paused,
            'pause_reason': self.pause_reason,
            'positions':    list(self.positions.values()),
            'trades':       self.trades[-200:],
            'log':          list(self.scan_log)[:200],
            'scan_results': self.scan_results[:50],
            'perf':         self.perf.summary(),
            'avg_win':      round(self.avg_win, 4),
            'avg_loss':     round(self.avg_loss, 4),
            'paper_mode':      config.PAPER_MODE,
            'btc_trend':       self._btc_trend,
            'btc_4h_trend':    self._btc_4h_trend,
            'btc_daily_bull':  self._btc_daily_bull,
            'btc_5m_mom':      self._btc_5m_mom,
            'btc_rsi':         self._btc_rsi,
            'fear_greed':      self._fear_greed,
            'market_intel':    self.market_intel,
        })

    def check_daily(self):
        today = date.today()
        if today != self.trade_day:
            self.daily_pnl   = 0.0
            self.daily_start = self.balance
            self.trade_day   = today
            self.paused      = False
            self.pause_reason= ''
            self.emit('info', f"📅 New day — P&L reset. Balance: ${self.balance:.4f}")

    def guards_ok(self) -> bool:
        if self.paused: return False
        if self.daily_start > 0:
            dloss = (self.daily_pnl / self.daily_start) * 100
            if dloss <= -config.MAX_DAILY_LOSS_PCT:
                self.paused = True
                self.pause_reason = f"Daily loss limit {dloss:.1f}% — resumes tomorrow"
                self.emit('warn', f"🚨 CIRCUIT BREAKER: {self.pause_reason}")
                return False
        if self.peak_bal > 0:
            dd = (self.peak_bal - self.balance) / self.peak_bal * 100
            if dd >= config.MAX_DRAWDOWN_PCT:
                self.paused = True
                self.pause_reason = f"Max drawdown {dd:.1f}% — manual review required"
                self.emit('warn', f"🚨 DRAWDOWN GUARD: {self.pause_reason}")
                return False
        # Consecutive loss cooldown — pause new entries for 20 min after N straight losses
        if self.consec_loss_pause_until > time.time():
            remaining = int(self.consec_loss_pause_until - time.time()) // 60
            self.emit('info', f"⏸ Consecutive loss cooldown — {remaining}m remaining")
            return False
        return True

    def _update_btc_trend(self):
        # ── 1H trend + RSI ────────────────────────────────────────────────────
        try:
            raw1h = self.client.klines('BTCUSDT', '1h', 60)
            df1h  = TAEngine.build(raw1h)
            c1h   = df1h['close'].astype(float)
            ema20 = c1h.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = c1h.ewm(span=50, adjust=False).mean().iloc[-1]
            price = float(c1h.iloc[-1])
            delta = c1h.diff()
            gain  = delta.clip(lower=0).rolling(14).mean().iloc[-1]
            loss  = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
            self._btc_rsi = round(100 - 100 / (1 + gain / loss), 1) if loss > 0 else 50.0
            if price > ema20 > ema50:   self._btc_trend = 'bull'
            elif price < ema20 < ema50: self._btc_trend = 'bear'
            else:                        self._btc_trend = 'neutral'
        except:
            self._btc_trend = 'neutral'
            self._btc_rsi   = 50.0

        # ── 4H trend — institutional timeframe (big money uses 4H) ────────────
        try:
            raw4h = self.client.klines('BTCUSDT', '4h', 60)
            df4h  = TAEngine.build(raw4h)
            c4h   = df4h['close'].astype(float)
            ema20_4h = c4h.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50_4h = c4h.ewm(span=50, adjust=False).mean().iloc[-1]
            p4h   = float(c4h.iloc[-1])
            if p4h > ema20_4h > ema50_4h:   self._btc_4h_trend = 'bull'
            elif p4h < ema20_4h < ema50_4h: self._btc_4h_trend = 'bear'
            else:                             self._btc_4h_trend = 'neutral'
        except:
            pass  # keep last known value

        # ── Daily trend — macro regime (avoid trading against the macro) ───────
        try:
            raw1d = self.client.klines('BTCUSDT', '1d', 30)
            df1d  = TAEngine.build(raw1d)
            c1d   = df1d['close'].astype(float)
            ema20_1d = c1d.ewm(span=20, adjust=False).mean().iloc[-1]
            self._btc_daily_bull = float(c1d.iloc[-1]) > ema20_1d
        except:
            pass  # keep last known value

        # ── BTC 5m momentum — real-time timing filter for altcoin entries ──────
        try:
            raw5 = self.client.klines('BTCUSDT', '5m', 5)
            closes5 = [float(k[4]) for k in raw5[-3:]]
            self._btc_5m_mom = round(
                (closes5[-1] - closes5[0]) / closes5[0] * 100, 3
            ) if len(closes5) >= 2 and closes5[0] else 0.0
        except:
            self._btc_5m_mom = 0.0

    def _fetch_sentiment(self) -> dict:
        """Fetch funding rates and OI for majors — whale intelligence layer."""
        sent = {}
        try:
            r = requests.get(
                'https://testnet.binancefuture.com/fapi/v1/premiumIndex',
                timeout=5
            ).json()
            for item in r:
                sym = item.get('symbol', '')
                if sym in config.PRIORITY_SYMBOLS or sym == 'BTCUSDT':
                    sent[sym] = {
                        'mark':    float(item.get('markPrice', 0)),
                        'index':   float(item.get('indexPrice', 0)),
                        'funding': float(item.get('lastFundingRate', 0)) * 100,
                    }
        except:
            pass
        return sent

    def _fetch_fear_greed(self) -> int:
        """Fear & Greed Index 0-100 from alternative.me. Cached 1h.
        <20 = extreme fear (buy zone), >80 = extreme greed (fade zone)."""
        now = time.time()
        if now - self._fear_greed_ts < 3600:
            return self._fear_greed
        try:
            r = requests.get(
                'https://api.alternative.me/fng/?limit=1', timeout=6
            ).json()
            self._fear_greed    = int(r['data'][0]['value'])
            self._fear_greed_ts = now
        except:
            pass
        return self._fear_greed

    def sector_count(self, sym: str) -> int:
        sec = self.sector.get(sym, 'Other')
        if sec == 'Other':
            return 0  # uncategorized coins are not assumed correlated
        return sum(1 for s in self.positions if self.sector.get(s, 'Other') == sec)

    def analyse_symbol(self, sym: str, price_hint: float = 0.0) -> dict | None:
        try:
            raw5  = self.client.klines(sym, config.TF_FAST, config.KLINE_LIMIT)
            raw15 = self.client.klines(sym, config.TF_MED,  config.KLINE_LIMIT)
            raw1h = self.client.klines(sym, config.TF_SLOW, config.KLINE_LIMIT)
        except Exception as e:
            log.debug(f"klines {sym}: {e}"); return None

        sig = TAEngine.analyse(raw5, raw15, raw1h)
        if sig is None: return None

        ctx     = self.ctx_mgr.get(sym)
        score   = sig.score
        score  += ctx.get('oi_bias', 0) * 0.10
        score  += ctx.get('ls_bias', 0) * 0.08
        funding = ctx.get('funding_rate', 0)
        if sig.direction == 'long'  and funding > 0.08: score *= 0.85
        if sig.direction == 'short' and funding < -0.08: score *= 0.85
        score = max(-100, min(100, score))

        # Volume ratio: current candle volume vs 20-bar average (breakout confirmation)
        try:
            vols = [float(k[5]) for k in raw5[-21:]]
            vol_ratio = vols[-1] / (sum(vols[:-1]) / len(vols[:-1])) if vols[:-1] else 1.0
        except:
            vol_ratio = 1.0

        # 1h timeframe confirmation: is the 1h candle agreeing with direction?
        try:
            df1h    = TAEngine.build(raw1h)
            c1h     = df1h['close'].astype(float)
            ema20_1h= c1h.ewm(span=20, adjust=False).mean().iloc[-1]
            h1_bull = float(c1h.iloc[-1]) > ema20_1h
        except:
            h1_bull = True  # default: no filter

        price = price_hint if price_hint > 0 else 0.0
        if price <= 0:
            try: price = self.client.price(sym)
            except: price = 0.0

        return {
            'symbol':    sym,
            'price':     price,
            'score':     round(abs(score), 1),
            'raw_score': round(score, 1),
            'direction': 'long' if score > 0 else 'short',
            'confidence':round(abs(score), 1),
            'regime':    sig.regime,
            'rsi':       round(sig.rsi, 1),
            'atr':       sig.atr,
            'atr_pct':   round(sig.atr_pct, 3),
            'adx':       round(sig.adx, 1),
            'cloud_bull':sig.cloud_bull,
            'squeeze':   sig.squeeze,
            'patterns':  [p['name'] for p in sig.patterns],
            'sr_levels': sig.sr_levels,
            'pivot':     sig.pivot,
            'funding':   funding,
            'ls_ratio':  ctx.get('ls_ratio', 1.0),
            'oi_bias':   ctx.get('oi_bias', 0),
            'vol_ratio': round(vol_ratio, 2),   # breakout volume confirmation
            'h1_bull':   h1_bull,               # 1h trend alignment
            'sig':       sig,
        }

    def dynamic_leverage(self, sym: str, atr_pct: float, conf: float) -> int:
        """Volatility-adjusted leverage: calm coins earn more, wild coins get less.
        atr_pct is the coin's own 5m ATR as % of price — its recent volatility."""
        if sym in config.MAJOR_LEVERAGE:
            # BTC/ETH/etc: deep liquidity, keep premium leverage but trim if unusually wild
            base = config.MAJOR_LEVERAGE[sym]
            if atr_pct > 1.0: base = max(10, base - 5)
            return base
        if   atr_pct <= 0.50: base = 12   # very calm — tight, predictable moves
        elif atr_pct <= 0.90: base = 10   # normal
        elif atr_pct <= 1.50: base = 7    # lively
        elif atr_pct <= 2.50: base = 5    # volatile
        else:                 base = 3    # wild meme-coin territory
        if   conf >= 75: base += 2        # strong signal earns a little extra
        elif conf <  60: base -= 1        # weak signal gets trimmed
        return max(2, min(base, 15))

    def compute_exits(self, direction: str, entry: float, atr: float, sym: str = '') -> dict:
        sign   = +1 if direction == 'long' else -1
        is_maj = sym in config.MAJOR_LEVERAGE
        if config.USE_ATR_EXITS and atr > 0:
            sl_m  = config.ATR_SL_MULT_MAJOR  if is_maj else config.ATR_SL_MULT
            tp1_m = config.ATR_TP1_MULT_MAJOR if is_maj else config.ATR_TP1_MULT
            tp2_m = config.ATR_TP2_MULT_MAJOR if is_maj else config.ATR_TP2_MULT
            tp3_m = config.ATR_TP3_MULT_MAJOR if is_maj else config.ATR_TP3_MULT
            sl_d  = max(atr * sl_m, entry * config.MIN_SL_DIST_PCT / 100)
            tp1_d = atr * tp1_m
            tp2_d = atr * tp2_m
            tp3_d = atr * tp3_m
            trl_d = atr * config.ATR_TRAIL_MULT
        else:
            sl_d  = entry * config.FIXED_SL_PCT  / 100
            tp1_d = entry * config.FIXED_TP1_PCT / 100
            tp2_d = entry * config.FIXED_TP2_PCT / 100
            tp3_d = entry * config.FIXED_TP3_PCT / 100
            trl_d = entry * 0.03
        return {
            'sl':         round(entry - sign * sl_d,  8),
            'tp1':        round(entry + sign * tp1_d, 8),
            'tp2':        round(entry + sign * tp2_d, 8),
            'tp3':        round(entry + sign * tp3_d, 8),
            'trail_dist': trl_d,
        }

    async def open_position(self, a: dict):
        sym = a['symbol']
        if sym in self.positions: return
        if not self.guards_ok(): return

        # ── Max-5 gate with swap-weakest logic ───────────────────────────────
        if len(self.positions) >= config.MAX_POSITIONS:
            weakest_sym  = min(self.positions, key=lambda s: self.positions[s]['conf'])
            weakest_conf = self.positions[weakest_sym]['conf']
            if a['confidence'] > weakest_conf + 10:
                self.emit('info',
                    f"⚡ SWAP: closing {weakest_sym} ({weakest_conf:.0f}%) → "
                    f"opening {sym} ({a['confidence']:.0f}%)"
                )
                wp   = self.positions[weakest_sym]
                loop = asyncio.get_event_loop()
                cur  = await loop.run_in_executor(None, self.client.price, weakest_sym)
                sign = +1 if wp['direction'] == 'long' else -1
                wpnl = sign * ((cur / wp['entry']) - 1) * wp.get('leverage', config.LEVERAGE) * 100
                await self.close(weakest_sym, wp, wpnl, f'SWAPPED → {sym}')
            else:
                return  # new signal not strong enough to displace weakest

        direction = a['direction']
        min_conf  = session_min_conf()
        conf      = a['confidence']

        if conf < min_conf:
            self.emit('fail', f"{sym} conf={conf}% < {min_conf:.0f}% → SKIP"); return
        if self.sector_count(sym) >= config.MAX_CORRELATED_PAIRS:
            self.emit('fail', f"{sym} sector cap → SKIP"); return
        if a['atr_pct'] > 4.0:
            self.emit('fail', f"{sym} ATR={a['atr_pct']:.1f}% → too volatile → SKIP"); return
        if a['atr_pct'] < 0.30:
            self.emit('fail', f"{sym} ATR={a['atr_pct']:.2f}% → too flat → SKIP"); return

        is_major   = sym in config.MAJOR_LEVERAGE
        is_priority= sym in config.PRIORITY_SYMBOLS
        lev        = self.dynamic_leverage(sym, a['atr_pct'], a['confidence'])
        btc_rsi    = self._btc_rsi
        btc        = self._btc_trend

        btc_extreme_oversold   = btc_rsi <= config.RSI_OVERSOLD_EXTREME
        btc_extreme_overbought = btc_rsi >= config.RSI_OVERBOUGHT_EXTREME

        # ── Fear & Greed override — extreme readings flip the script ──────────
        fng = self._fear_greed
        if fng <= 20:                       # extreme fear = capitulation = buy zone
            btc_extreme_oversold   = True   # relax all bear-market LONG penalties
        elif fng >= 80:                     # extreme greed = euphoria = short zone
            btc_extreme_overbought = True   # relax all bull-market SHORT penalties

        # ── 4H trend gate — institutional money runs 4H; don't fight it ───────
        if direction == 'long' and self._btc_4h_trend == 'bear' and not btc_extreme_oversold:
            self.emit('fail', f"{sym} 4H BEAR → LONG blocked (FNG={fng}) → SKIP"); return
        if direction == 'short' and self._btc_4h_trend == 'bull' and not btc_extreme_overbought:
            self.emit('fail', f"{sym} 4H BULL → SHORT blocked (FNG={fng}) → SKIP"); return

        # ── Daily macro gate — needs big extra confidence against daily trend ──
        if not self._btc_daily_bull and direction == 'long' and not btc_extreme_oversold:
            need = min_conf + 12
            if conf < need:
                self.emit('fail', f"{sym} Daily BEAR LONG needs {need:.0f}%+ got {conf:.0f}% → SKIP"); return
        if self._btc_daily_bull and direction == 'short' and not btc_extreme_overbought:
            need = min_conf + 12
            if conf < need:
                self.emit('fail', f"{sym} Daily BULL SHORT needs {need:.0f}%+ got {conf:.0f}% → SKIP"); return

        # ── BTC 10m momentum gate — don't open altcoin entries against BTC flow
        if not is_major and abs(self._btc_5m_mom) > 0.40:
            if direction == 'long' and self._btc_5m_mom < -0.40:
                self.emit('fail', f"{sym} BTC 10m={self._btc_5m_mom:+.2f}% falling → no altcoin LONG → SKIP"); return
            if direction == 'short' and self._btc_5m_mom > 0.40:
                self.emit('fail', f"{sym} BTC 10m={self._btc_5m_mom:+.2f}% rising → no altcoin SHORT → SKIP"); return

        # ── Pro gate 1: BTC macro filter — much stricter bear/bull filters ────
        if btc == 'bear' and direction == 'long':
            if btc_extreme_oversold and is_priority:
                extra = 0   # capitulation zone + major = green light
            elif btc_extreme_oversold:
                extra = 8   # oversold but altcoin = slight premium
            elif is_priority:
                extra = config.BEAR_LONG_EXTRA - 5  # major in bear needs 63%+
            else:
                extra = config.BEAR_LONG_EXTRA  # altcoin LONG in bear = needs 68%+
            if conf < min_conf + extra:
                self.emit('fail', f"{sym} BTC=bear RSI={btc_rsi:.0f} LONG needs >{min_conf+extra:.0f}% got {conf:.0f}% → SKIP"); return

        if btc == 'bull' and direction == 'short':
            if btc_extreme_overbought and is_priority:
                extra = 0
            elif btc_extreme_overbought:
                extra = 8
            elif is_priority:
                extra = config.BULL_SHORT_EXTRA - 5
            else:
                extra = config.BULL_SHORT_EXTRA
            if conf < min_conf + extra:
                self.emit('fail', f"{sym} BTC=bull RSI={btc_rsi:.0f} SHORT needs >{min_conf+extra:.0f}% got {conf:.0f}% → SKIP"); return

        # ── Pro gate 2: 1H trend alignment — don't fight the hourly trend ─────
        h1_bull = a.get('h1_bull', True)
        if direction == 'long'  and not h1_bull and not btc_extreme_oversold:
            self.emit('fail', f"{sym} 1H trend bearish → LONG misaligned → SKIP"); return
        if direction == 'short' and h1_bull and not btc_extreme_overbought:
            self.emit('fail', f"{sym} 1H trend bullish → SHORT misaligned → SKIP"); return

        # ── Pro gate 3: Volume confirmation — breakout needs volume ──────────
        vol_ratio = a.get('vol_ratio', 1.0)
        if vol_ratio < config.VOL_SPIKE_MULT and conf < 62:
            self.emit('fail', f"{sym} vol={vol_ratio:.2f}x avg → no breakout volume → SKIP"); return

        # ── Pro gate 4: ADX strength — don't trade choppy markets ────────────
        if a['adx'] < 18 and a['regime'] != 'squeeze':
            self.emit('fail', f"{sym} ADX={a['adx']:.0f} → market too choppy → SKIP"); return

        # ── Funding rate sentiment filter ─────────────────────────────────────
        sym_funding = self._sentiment.get(sym, {}).get('funding', a.get('funding', 0))
        if direction == 'long'  and sym_funding > config.FUNDING_EXTREME_POS:
            self.emit('fail', f"{sym} funding={sym_funding:.3f}% extreme positive → longs crowded → SKIP"); return
        if direction == 'short' and sym_funding < config.FUNDING_EXTREME_NEG:
            self.emit('fail', f"{sym} funding={sym_funding:.3f}% extreme negative → shorts crowded → SKIP"); return

        # ── Pro gate 5: Counter-trend requires strong pattern ─────────────────
        is_counter_trend = (btc == 'bear' and direction == 'long') or \
                           (btc == 'bull' and direction == 'short')
        STRONG_PATTERNS = {
            'Morning Star', 'Evening Star', 'Three White Soldiers', 'Three Black Crows',
            'Bullish Engulfing', 'Bearish Engulfing', 'Piercing Line', 'Dark Cloud Cover',
        }
        has_strong_pattern = bool(set(a.get('patterns', [])) & STRONG_PATTERNS)
        if is_counter_trend and not has_strong_pattern and not btc_extreme_oversold and not btc_extreme_overbought:
            self.emit('fail', f"{sym} counter-trend without strong pattern → SKIP"); return

        # ── Pro gate 6: R:R check — must be worth the risk ───────────────────
        tmp_exits = self.compute_exits(direction, a['price'], a['atr'], sym)
        sign_rr   = +1 if direction == 'long' else -1
        sl_dist   = abs(a['price'] - tmp_exits['sl'])
        tp1_dist  = abs(tmp_exits['tp1'] - a['price'])
        rr_ratio  = tp1_dist / sl_dist if sl_dist > 0 else 0
        if rr_ratio < config.MIN_RR_RATIO:
            self.emit('fail', f"{sym} R:R={rr_ratio:.2f}x < {config.MIN_RR_RATIO}x minimum → SKIP"); return

        # ── Position sizing: Kelly + confidence-scaled ───────────────────────
        tot = self.wins + self.losses
        pct = kelly_size(
            win_rate = self.wins / tot if tot > 10 else 0.5,
            avg_win  = self.avg_win,
            avg_loss = abs(self.avg_loss),
            base_pct = config.POSITION_SIZE_PCT,
            atr_pct  = a['atr_pct'],
        )
        # Confidence-based size multiplier — bet bigger on high-conviction signals
        if conf >= 68:   pct *= 1.5   # very high conviction → 150%
        elif conf >= 60: pct *= 1.2   # high conviction → 120%
        elif conf < 54:  pct *= 0.75  # marginal → 75% size
        if is_major:
            pct = min(pct * 1.25, 0.10)
        else:
            pct = min(pct, 0.06)      # cap altcoin exposure at 6%

        price   = a['price']
        raw_qty = (self.balance * pct) / price
        qty     = self.client.round_qty(sym, raw_qty)
        if qty <= 0:
            self.emit('warn', f"{sym} qty=0 bal=${self.balance:.2f}"); return

        try:
            self.client.set_margin_type(sym)
            self.client.set_leverage(sym, lev)
        except: pass

        side = 'BUY' if direction == 'long' else 'SELL'
        try:
            resp = self.client.market_order(sym, side, qty, current_price=price)
        except Exception as e:
            self.emit('warn', f"Order FAIL {sym}: {e}"); return

        entry = float(resp.get('avgPrice') or price) or price
        exits = self.compute_exits(direction, entry, a['atr'], sym)

        self.positions[sym] = {
            'symbol':    sym,
            'direction': direction,
            'entry':     entry,
            'current':   entry,
            'qty':       qty,
            'size_usd':  round(qty * entry, 4),
            'leverage':  lev,
            'is_major':  is_major,
            'sl':        exits['sl'],
            'tp1':       exits['tp1'],
            'tp2':       exits['tp2'],
            'tp3':       exits['tp3'],
            'trail_sl':  exits['sl'],
            'trail_dist':exits['trail_dist'],
            'be_locked': False,
            'tp1_hit':   False,
            'tp2_hit':   False,
            'phase':     'open',
            'pnl_pct':   0.0,
            'pnl_usd':   0.0,
            'conf':      a['confidence'],
            'regime':    a['regime'],
            'patterns':  a['patterns'],
            'atr':       a['atr'],
            'atr_pct':   a['atr_pct'],
            'adx':       a['adx'],
            'rsi':       a['rsi'],
            'cloud_bull':a['cloud_bull'],
            'squeeze':   a['squeeze'],
            'funding':   a['funding'],
            'ls_ratio':  a['ls_ratio'],
            'oi_bias':   a['oi_bias'],
            'pivot':     a['pivot'],
            'open_time': datetime.now().strftime('%H:%M:%S'),
            'session':   current_session(),
            'vol_ratio': a.get('vol_ratio', 1.0),
            'rr_ratio':  round(rr_ratio, 2),
            'order_id':     resp.get('orderId', ''),
            'sl_order_id':  '',   # filled by _place_exchange_guards
            'tp1_order_id': '',
        }
        # Place hard SL + TP1 orders on exchange — survive bot offline
        self._place_exchange_guards(sym, self.positions[sym])
        self._save_positions()
        if config.PAPER_MODE:
            self.paper_balance -= qty * entry / lev
        self.balance = self.client.usdt_balance(self.paper_balance)
        pats  = ', '.join(a['patterns']) if a['patterns'] else '—'
        star  = '⭐' if is_major else ''
        self.emit('exec',
            f"{star}{'LONG' if direction=='long' else 'SHORT'} {sym} @ {entry:.6g} | "
            f"qty={qty} lev={lev}x | conf={a['confidence']:.0f}% | regime={a['regime']} | "
            f"SL={exits['sl']:.5g} TP1={exits['tp1']:.5g} TP3={exits['tp3']:.5g} | "
            f"pats=[{pats}] | {current_session()} | "
            f"BTC 1H:{self._btc_trend} 4H:{self._btc_4h_trend} {'D:BULL' if self._btc_daily_bull else 'D:BEAR'} "
            f"RSI={btc_rsi:.0f} F&G={self._fear_greed}"
        )

    async def update_positions(self):
        # Every 30s sync P&L directly from exchange (mark price + funding fees)
        # so dashboard values match what the Binance app shows
        _exchange_pnl: dict[str, tuple[float, float]] = {}  # sym → (mark_price, unrealized_pnl)
        now = time.time()
        if self.positions and now - self._last_risk_sync >= 10:
            try:
                loop = asyncio.get_event_loop()
                risks = await loop.run_in_executor(None, self.client.position_risk)
                for r in risks:
                    sym = r.get('symbol', '')
                    if sym in self.positions:
                        mp  = float(r.get('markPrice', 0) or 0)
                        upl = float(r.get('unRealizedProfit', 0) or 0)
                        if mp > 0:
                            _exchange_pnl[sym] = (mp, upl)
                self._last_risk_sync = now
            except Exception as _re:
                log.debug(f"[RISK SYNC] {_re}")

        for sym in list(self.positions):
            pos = self.positions.get(sym)
            if pos is None: continue
            try:
                # Conventions (matching the Binance app):
                #   pnl_usd = true dollars gained/lost  (notional × price move)
                #   pnl_pct = ROI% on margin            (price move × leverage)
                d        = pos['direction']
                sign     = +1 if d == 'long' else -1
                entry    = pos['entry']
                lev_used = pos.get('leverage', config.LEVERAGE)
                if sym in _exchange_pnl:
                    # exchange mark price + exact unrealized P&L
                    current, ex_pnl_usd = _exchange_pnl[sym]
                    pos['current'] = current
                    pos['pnl_usd'] = round(ex_pnl_usd, 4)
                    margin = pos['size_usd'] / lev_used if pos['size_usd'] else 0
                    pos['pnl_pct'] = round(ex_pnl_usd / margin * 100 if margin else 0, 2)
                else:
                    # Prefer the real-time pushed mark price (updates every second);
                    # fall back to REST only if the stream is stale
                    mark = self._mark_prices.get(sym, 0.0)
                    if mark > 0 and time.time() - self._mark_stream_ts < 10:
                        current = mark
                    else:
                        current = self.client.price(sym)
                    pos['current'] = current
                    move_pct = sign * ((current / entry) - 1) * 100      # raw price move
                    pos['pnl_pct'] = round(move_pct * lev_used, 2)       # ROI on margin
                    pos['pnl_usd'] = round(pos['size_usd'] * move_pct / 100, 4)  # true $
                pnl_pct = pos['pnl_pct']

                lev_used = pos.get('leverage', config.LEVERAGE)

                # Breakeven lock
                if not pos['be_locked']:
                    be_trig = entry * (1 + sign * config.BE_LOCK_PCT / 100)
                    if sign * (current - be_trig) >= 0:
                        pos['trail_sl'] = entry * (1 + sign * 0.001)
                        pos['be_locked'] = True
                        pos['phase']     = 'be-locked'
                        self.emit('info', f"🔒 BE-locked {sym} @ {current:.6g}")
                        self._save_positions()

                # TP1 — bank half the position, move SL to breakeven, let rest run
                if not pos['tp1_hit'] and sign * (current - pos['tp1']) >= 0:
                    pos['tp1_hit'] = True
                    pos['phase']   = 'tp1-trail'
                    await self._partial_close(sym, pos, config.TP1_SCALE_OUT, current)
                    self.emit('info', f"🎯 TP1 {sym} → SL→BE, runner trails to TP2={pos['tp2']:.5g}")
                    self._move_sl_to_breakeven(sym, pos)
                    self._save_positions()

                # TP2 — tighten trail
                if pos['tp1_hit'] and not pos['tp2_hit'] and sign * (current - pos['tp2']) >= 0:
                    pos['tp2_hit']    = True
                    pos['phase']      = 'tp2-trail'
                    pos['trail_dist'] *= 0.5
                    self.emit('info', f"🎯 TP2 {sym} → tight trail to TP3={pos['tp3']:.5g}")

                # Update trailing stop
                if pos['tp1_hit'] or pos['be_locked']:
                    new_sl = current - sign * pos['trail_dist']
                    if sign * (new_sl - pos['trail_sl']) > 0:
                        pos['trail_sl'] = round(new_sl, 8)

                # Exit triggers
                sl_hit  = sign * (pos['trail_sl'] - current) >= 0
                tp3_hit = sign * (current - pos['tp3']) >= 0
                max_g   = pnl_pct >= config.MAX_GAIN_PCT

                if tp3_hit:
                    await self.close(sym, pos, pnl_pct, 'TP3 HIT')
                elif sl_hit:
                    r = 'TRAIL SL' if pos['tp1_hit'] else ('BE EXIT' if pos['be_locked'] else 'STOP LOSS')
                    await self.close(sym, pos, pnl_pct, r)
                elif max_g:
                    await self.close(sym, pos, pnl_pct, 'MAX GAIN CAP')

            except Exception as e:
                log.debug(f"update {sym}: {e}")

    async def _partial_close(self, sym: str, pos: dict, frac: float, price: float):
        """Close a fraction of the position at market and bank the profit.
        Called at TP1 so winners lock in gains instead of giving them back on retrace."""
        part = self.client.round_qty(sym, pos['qty'] * frac)
        if part <= 0 or part >= pos['qty']:
            return  # too small to split — keep full runner
        d    = pos['direction']
        sign = +1 if d == 'long' else -1
        try:
            self.client.market_order(sym, 'SELL' if d == 'long' else 'BUY', part,
                                     reduce=True, current_price=price)
        except Exception as e:
            self.emit('warn', f"TP1 scale-out fail {sym}: {e}")
            return
        entry      = pos['entry']
        closed_usd = part * entry
        pnl_usd    = sign * (price - entry) * part
        self.total_pnl += pnl_usd
        self.daily_pnl += pnl_usd
        if config.PAPER_MODE:
            self.paper_balance += pnl_usd + closed_usd / pos.get('leverage', config.LEVERAGE)
        pos['qty']      = round(pos['qty'] - part, 8)
        pos['size_usd'] = round(pos['qty'] * entry, 4)
        self.balance = self.client.usdt_balance(self.paper_balance)
        self.emit('exec',
            f"💰 TP1 BANKED {sym}: {frac*100:.0f}% closed @ {price:.6g} → "
            f"{pnl_usd:+.4f}$ | runner qty={pos['qty']} → TP2={pos['tp2']:.5g}")

    async def close(self, sym: str, pos: dict, pnl_pct: float, reason: str):
        d = pos['direction']
        if time.time() < pos.get('_close_block_until', 0):
            return   # recent close attempt failed — wait before retrying
        # Cancel exchange SL/TP orders before closing — prevents double-close
        self._cancel_exchange_guards(sym, pos)
        try:
            self.client.market_order(sym, 'SELL' if d == 'long' else 'BUY', pos['qty'], reduce=True)
        except Exception as e:
            # CRITICAL: keep the position — deleting it here would leave an
            # unmanaged orphan open on the exchange with no stop-loss
            pos['_close_block_until'] = time.time() + 10
            pos['phase'] = 'close-retry'
            self.emit('warn', f"⚠ Close FAIL {sym} ({reason}): {e} — position KEPT, retrying in 10s")
            return

        is_win  = pnl_pct > 0.01
        pnl_usd = pos['pnl_usd']

        if is_win:
            self.wins  += 1
            self.avg_win  = (self.avg_win  * (self.wins  - 1) + pnl_pct) / self.wins
        else:
            self.losses += 1
            self.avg_loss = (self.avg_loss * (self.losses - 1) + pnl_pct) / self.losses

        self.total_pnl     += pnl_usd
        self.daily_pnl     += pnl_usd
        # Return margin using the SAME leverage it was deducted with at open
        self.paper_balance += pnl_usd + pos['size_usd'] / pos.get('leverage', config.LEVERAGE)
        self.perf.add(pnl_pct, is_win)

        # Consecutive loss guard — block new entries for 20 min after N straight losses
        if not is_win and self.perf.loss_streak >= config.MAX_CONSEC_LOSSES:
            self.consec_loss_pause_until = time.time() + 20 * 60
            self.emit('warn', f"⏸ {self.perf.loss_streak} consecutive losses → entry cooldown 20 min")

        t = {
            'id':         len(self.trades) + 1,
            'symbol':     sym,
            'direction':  d,
            'entry':      pos['entry'],
            'exit':       pos['current'],
            'qty':        pos['qty'],
            'pnl_pct':    round(pnl_pct, 2),
            'pnl_usd':    round(pnl_usd, 4),
            'reason':     reason,
            'phase':      pos['phase'],
            'conf':       pos['conf'],
            'regime':     pos['regime'],
            'patterns':   pos['patterns'],
            'session':    pos.get('session', ''),
            'open_time':  pos['open_time'],
            'close_time': datetime.now().strftime('%H:%M:%S'),
            'is_win':     is_win,
            'sharpe_now': round(self.perf.sharpe, 2),
        }
        self.trades.append(t)
        self._save_trades()
        del self.positions[sym]
        self._save_positions()

        self.balance  = self.client.usdt_balance(self.paper_balance)
        self.peak_bal = max(self.peak_bal, self.balance)

        self.emit('exit',
            f"{'✅' if is_win else '❌'} #{t['id']} {sym} {reason} | "
            f"{d.upper()} {'+' if is_win else ''}{pnl_pct:.2f}% (${pnl_usd:.4f}) | "
            f"Bal ${self.balance:.4f} | Sharpe={self.perf.sharpe:.2f} | Streak={self.perf.win_streak}W/{self.perf.loss_streak}L"
        )

    async def scan_loop(self):
        self.emit('info', '🤖 AlphaBot v5.0 — Advanced Multi-TF + Ichimoku + Squeeze + Patterns')

        try:
            self.client.load_symbols()
            self.emit('info', f'📐 {len(self.client.step)} symbol precisions loaded')
        except Exception as e:
            self.emit('warn', f"Precision load: {e}")

        self.balance    = self.client.usdt_balance(self.paper_balance)
        self.start_bal  = self.balance
        self.peak_bal   = self.balance
        self.daily_start= self.balance
        mode = "📄 PAPER MODE" if config.PAPER_MODE else "🔴 LIVE MODE"
        self.emit('info', f"💰 {mode} | Balance: ${self.balance:.4f} USDT")

        # Reconcile disk positions with exchange on every startup
        await self._reconcile_positions()

        try:
            tickers = self.client.tickers_24h()
            # All USDT perps with minimum volume filter (very low to catch everything)
            all_perps = [t for t in tickers
                         if t['symbol'].endswith('USDT')
                         and '_' not in t['symbol']]
            perps = [t for t in all_perps
                     if float(t.get('quoteVolume', 0)) >= config.MIN_VOLUME_USDT]

            # Top movers (biggest 24h % moves — both up AND down) — scan these FIRST
            by_move = sorted(perps, key=lambda x: abs(float(x.get('priceChangePercent', 0))), reverse=True)
            movers  = [t['symbol'] for t in by_move[:config.TOP_MOVERS_N]]

            # Volume leaders (ALL if SCAN_ALL_PERPS, else top N)
            by_vol  = sorted(perps, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
            vol_top = [t['symbol'] for t in by_vol] if config.SCAN_ALL_PERPS else [t['symbol'] for t in by_vol[:config.TOP_N_SYMBOLS]]

            # Priority symbols always included (BTC/ETH/BNB/SOL/XRP first regardless of volume)
            seen, symbols = set(), []
            for s in config.PRIORITY_SYMBOLS + movers + vol_top:
                if s not in seen: seen.add(s); symbols.append(s)

            # Build initial market intel from tickers
            gainers = sorted(perps, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)
            losers  = sorted(perps, key=lambda x: float(x.get('priceChangePercent', 0)))
            up_n    = sum(1 for t in perps if float(t.get('priceChangePercent', 0)) > 0)
            dn_n    = len(perps) - up_n
            self.market_intel = {
                'top_gainers': [{'symbol': t['symbol'],
                                 'change_pct': round(float(t.get('priceChangePercent', 0)), 2),
                                 'price': t.get('lastPrice', '0'),
                                 'volume': round(float(t.get('quoteVolume', 0)) / 1e6, 1)}
                                for t in gainers[:15]],
                'top_losers':  [{'symbol': t['symbol'],
                                 'change_pct': round(float(t.get('priceChangePercent', 0)), 2),
                                 'price': t.get('lastPrice', '0'),
                                 'volume': round(float(t.get('quoteVolume', 0)) / 1e6, 1)}
                                for t in losers[:15]],
                'up_count': up_n, 'down_count': dn_n,
                'market_mood': 'BULLISH' if up_n > dn_n * 1.3 else ('BEARISH' if dn_n > up_n * 1.3 else 'MIXED'),
                'total_perps': len(perps),
                'btc_change': round(float(next((t.get('priceChangePercent', 0) for t in perps if t['symbol'] == 'BTCUSDT'), 0)), 2),
            }
            self.emit('info',
                f"🔭 Universe: {len(symbols)} USDT perps | "
                f"📈 {up_n} up / 📉 {dn_n} down | mood={self.market_intel['market_mood']} | "
                f"🔥 Top mover: {by_move[0]['symbol']} {float(by_move[0].get('priceChangePercent',0)):+.1f}%"
            )
        except Exception as e:
            self.emit('warn', f"Universe: {e}")
            symbols = [
                'BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT',
                'ADAUSDT','AVAXUSDT','MATICUSDT','LINKUSDT','DOTUSDT','LTCUSDT',
                'UNIUSDT','AAVEUSDT','ATOMUSDT','NEARUSDT','FTMUSDT','INJUSDT',
                'ARBUSDT','OPUSDT','SHIBUSDT','PEPEUSDT','APTUSDT','SUIUSDT',
            ]

        while self.running:
          try:
            self.check_daily()
            self.scan_count += 1
            session  = current_session()
            min_conf = session_min_conf()

            # Update BTC macro trend + sentiment + refresh market movers every scan
            # Run in executor so blocking requests don't freeze the WS event loop
            loop2 = asyncio.get_event_loop()
            await loop2.run_in_executor(None, self._update_btc_trend)
            self._sentiment    = await loop2.run_in_executor(None, self._fetch_sentiment)
            self._fear_greed   = await loop2.run_in_executor(None, self._fetch_fear_greed)
            try:
                tickers   = await loop2.run_in_executor(None, self.client.tickers_24h)
                all_perps = [t for t in tickers if t['symbol'].endswith('USDT') and '_' not in t['symbol']]
                perps     = [t for t in all_perps if float(t.get('quoteVolume', 0)) >= config.MIN_VOLUME_USDT]
                gainers   = sorted(perps, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)
                losers    = sorted(perps, key=lambda x: float(x.get('priceChangePercent', 0)))
                up_n      = sum(1 for t in perps if float(t.get('priceChangePercent', 0)) > 0)
                dn_n      = len(perps) - up_n
                by_move   = sorted(perps, key=lambda x: abs(float(x.get('priceChangePercent', 0))), reverse=True)
                self.market_intel = {
                    'top_gainers': [{'symbol': t['symbol'],
                                     'change_pct': round(float(t.get('priceChangePercent', 0)), 2),
                                     'price': t.get('lastPrice', '0'),
                                     'volume': round(float(t.get('quoteVolume', 0)) / 1e6, 1)}
                                    for t in gainers[:15]],
                    'top_losers':  [{'symbol': t['symbol'],
                                     'change_pct': round(float(t.get('priceChangePercent', 0)), 2),
                                     'price': t.get('lastPrice', '0'),
                                     'volume': round(float(t.get('quoteVolume', 0)) / 1e6, 1)}
                                    for t in losers[:15]],
                    'up_count': up_n, 'down_count': dn_n,
                    'market_mood': 'BULLISH' if up_n > dn_n * 1.3 else ('BEARISH' if dn_n > up_n * 1.3 else 'MIXED'),
                    'total_perps': len(perps),
                    'total_scanned': len(all_perps),
                    'btc_change': round(float(next((t.get('priceChangePercent', 0) for t in perps if t['symbol'] == 'BTCUSDT'), 0)), 2),
                    'btc_rsi': self._btc_rsi,
                    'top_mover': by_move[0]['symbol'] if by_move else '—',
                    'top_mover_pct': round(float(by_move[0].get('priceChangePercent', 0)), 2) if by_move else 0,
                    'sentiment': {sym: s.get('funding', 0) for sym, s in self._sentiment.items()},
                }
                # Rebuild symbols list: priority → movers → all remaining by volume
                by_vol  = sorted(perps, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
                movers_sym = [t['symbol'] for t in by_move[:config.TOP_MOVERS_N]]
                vol_sym    = [t['symbol'] for t in by_vol] if config.SCAN_ALL_PERPS else [t['symbol'] for t in by_vol[:config.TOP_N_SYMBOLS]]
                seen, symbols = set(), []
                for s in config.PRIORITY_SYMBOLS + movers_sym + vol_sym:
                    if s not in seen: seen.add(s); symbols.append(s)
            except Exception as e:
                log.debug(f"movers refresh: {e}")

            btc_fund = self._sentiment.get('BTCUSDT', {}).get('funding', 0)
            fng_label = ('EXTREME FEAR' if self._fear_greed < 20 else
                         'FEAR'         if self._fear_greed < 40 else
                         'GREED'        if self._fear_greed > 60 else
                         'EXTREME GREED'if self._fear_greed > 80 else 'NEUTRAL')
            self.emit('info',
                f"BTC 1H:{self._btc_trend.upper()} 4H:{self._btc_4h_trend.upper()} "
                f"{'D:BULL' if self._btc_daily_bull else 'D:BEAR'} "
                f"RSI={self._btc_rsi:.0f} 5m:{self._btc_5m_mom:+.2f}% | "
                f"F&G={self._fear_greed} ({fng_label}) | "
                f"fund={btc_fund:+.4f}% | "
                f"up={self.market_intel.get('up_count', 0)} dn={self.market_intel.get('down_count', 0)} "
                f"mood={self.market_intel.get('market_mood', '—')} | "
                f"{'COOLDOWN' if self.consec_loss_pause_until > time.time() else 'ENTRIES_OPEN'}"
            )

            self.emit('scan',
                f"── Scan #{self.scan_count} | {session} | {len(symbols)} coins | "
                f"min_conf={min_conf:.0f}% | BTC_RSI={self._btc_rsi:.0f} | open={len(self.positions)}/{config.MAX_POSITIONS} ──"
            )

            candidates   = []
            scan_results = []
            scan_syms    = symbols if self.running else []

            # Pre-build price map from tickers — saves 1 API call per coin
            _prices: dict[str, float] = {}
            try:
                _tick = await asyncio.get_event_loop().run_in_executor(None, self.client.tickers_24h)
                _prices = {t['symbol']: float(t['lastPrice']) for t in _tick if t.get('lastPrice')}
            except Exception:
                pass

            loop = asyncio.get_event_loop()
            _par = max(1, getattr(config, 'SCAN_PARALLEL', 6))
            for _ci in range(0, len(scan_syms), _par):
              if not self.running: break
              _chunk   = scan_syms[_ci:_ci + _par]
              _futs    = [loop.run_in_executor(None, self.analyse_symbol, s, _prices.get(s, 0.0))
                          for s in _chunk]
              _results = await asyncio.gather(*_futs, return_exceptions=True)
              for sym, a in zip(_chunk, _results):
                if isinstance(a, Exception):
                    log.warning(f"[SCAN] {sym}: {a}")
                    continue
                if a is None: continue

                r = {k: v for k, v in a.items() if k != 'sig'}
                scan_results.append(r)

                if a['confidence'] >= min_conf:
                    reject = None
                    if a['atr_pct'] < 0.2:
                        reject = f"ATR={a['atr_pct']:.2f}% — too flat"
                    elif a['ls_ratio'] > 2.5 and a['direction'] == 'long':
                        reject = f"L/S={a['ls_ratio']:.1f} — longs overcrowded"
                    elif a['ls_ratio'] < 0.4 and a['direction'] == 'short':
                        reject = f"L/S={a['ls_ratio']:.1f} — shorts overcrowded"

                    if reject:
                        self.emit('fail', f"{sym} {a['confidence']:.0f}% | {reject} → FILTERED")
                    else:
                        pats = ', '.join(a['patterns']) if a['patterns'] else '—'
                        self.emit('pass',
                            f"{sym} {a['confidence']:.0f}% {a['direction'].upper()} | "
                            f"regime={a['regime']} squeeze={'⚡' if a['squeeze'] else '○'} "
                            f"cloud={'☁↑' if a['cloud_bull'] else '☁↓'} | "
                            f"RSI={a['rsi']} ADX={a['adx']:.0f} ATR={a['atr_pct']:.2f}% | "
                            f"patterns=[{pats}] | OI={a['oi_bias']:+.0f}"
                        )
                        candidates.append(a)
                        # Execute IMMEDIATELY while the signal is fresh — waiting
                        # for scan end (minutes) made entries stale and meant an
                        # interrupted scan never opened anything
                        if len(self.positions) < config.MAX_POSITIONS and sym not in self.positions:
                            try:
                                await self.open_position(a)
                            except Exception as _open_err:
                                log.error(f"[OPEN] {sym}: {_open_err}")
                else:
                    self.emit('fail', f"{sym} {a['confidence']:.0f}% {a['direction'].upper()} | regime={a['regime']} | ADX={a['adx']:.0f} RSI={a['rsi']} → below {min_conf:.0f}%")

            candidates.sort(key=lambda x: x['confidence'], reverse=True)
            self.scan_results = sorted(scan_results, key=lambda x: x['confidence'], reverse=True)

            for a in candidates:
                if len(self.positions) >= config.MAX_POSITIONS: break
                if a['symbol'] not in self.positions:
                    await self.open_position(a)

            tot = self.wins + self.losses
            self.emit('info',
                f"Scan #{self.scan_count} | {len(candidates)} pass | "
                f"WR={round(self.wins/tot*100,1) if tot else 0}% | "
                f"Sharpe={self.perf.sharpe:.2f} | Sortino={self.perf.sortino:.2f} | "
                f"Daily={'+' if self.daily_pnl>=0 else ''}{self.daily_pnl:.4f}"
            )
            await self.push()

            # Position updates + dashboard pushes run in _live_update_loop (always on)
            for _ in range(config.SCAN_INTERVAL_SEC):
                if not self.running: break
                await asyncio.sleep(1)

          except Exception as _cycle_err:
            import traceback as _tb2
            log.error(f"[SCAN CYCLE CRASH] {_cycle_err}\n{_tb2.format_exc()}")
            log.info("[SCAN CYCLE] Auto-recovering in 10s...")
            await asyncio.sleep(10)

    async def _live_update_loop(self):
        """Always-on 1-second heartbeat: refresh positions from real-time mark
        prices and push to the dashboard — even while a scan is running."""
        while self.running:
            try:
                if not self._upd_busy:
                    self._upd_busy = True
                    try:
                        await self.update_positions()
                        await self.push()
                    finally:
                        self._upd_busy = False
            except Exception as e:
                log.error(f"[LIVE] {e}")
            await asyncio.sleep(1)

    # ── Real-time Binance streams (push, not poll) ────────────────────────────
    async def _mark_price_stream(self):
        """Binance pushes mark prices for ALL symbols every second — real-time P&L."""
        url = WS_STREAM + '/ws/!markPrice@arr@1s'
        while self.running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              max_size=2**23) as ws:
                    log.info('[STREAM ] Mark-price stream connected — real-time prices ON')
                    async for raw in ws:
                        data = json.loads(raw)
                        for it in data:
                            self._mark_prices[it['s']] = float(it['p'])
                        self._mark_stream_ts = time.time()
            except Exception as e:
                log.warning(f'[STREAM ] Mark-price stream lost: {e} — reconnect in 5s')
                await asyncio.sleep(5)

    async def _user_data_stream(self):
        """Binance pushes account events the instant they happen:
        balance changes, position P&L, order fills."""
        if config.PAPER_MODE:
            return
        loop = asyncio.get_event_loop()
        while self.running:
            try:
                key = await loop.run_in_executor(None, self.client.listen_key)
                last_ka = time.time()
                async with websockets.connect(f'{WS_STREAM}/ws/{key}',
                                              ping_interval=20, ping_timeout=20) as ws:
                    log.info('[STREAM ] User-data stream connected — real-time account sync ON')
                    while self.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw:
                            evt = json.loads(raw)
                            et  = evt.get('e', '')
                            if et == 'ACCOUNT_UPDATE':
                                a = evt.get('a', {})
                                for b in a.get('B', []):
                                    if b.get('a') == 'USDT':
                                        try: self.balance = float(b['wb'])
                                        except Exception: pass
                                for P in a.get('P', []):
                                    sym = P.get('s', '')
                                    pos = self.positions.get(sym)
                                    if pos:
                                        up = float(P.get('up', 0) or 0)
                                        pos['pnl_usd'] = round(up, 4)
                                        lev = pos.get('leverage', config.LEVERAGE)
                                        margin = pos['size_usd'] / lev if pos['size_usd'] else 0
                                        pos['pnl_pct'] = round(up / margin * 100 if margin else 0, 2)
                                await self.push()   # instant dashboard refresh
                            elif et == 'ORDER_TRADE_UPDATE':
                                o = evt.get('o', {})
                                if o.get('X') == 'FILLED':
                                    log.info(f"[STREAM ] Fill: {o.get('s')} {o.get('S')} "
                                             f"qty={o.get('q')} @ {o.get('ap')}")
                            elif et == 'listenKeyExpired':
                                break   # get a fresh key
                        if time.time() - last_ka > 1500:   # keepalive every 25 min
                            await loop.run_in_executor(None, self.client.keepalive_listen_key)
                            last_ka = time.time()
            except Exception as e:
                log.warning(f'[STREAM ] User-data stream lost: {e} — reconnect in 10s')
                await asyncio.sleep(10)

    async def ws_handler(self, ws):
        self.ws_clients.add(ws)
        self.emit('info', f"📊 Dashboard connected ({len(self.ws_clients)} clients)")
        try:
            await self.push()
            await ws.wait_closed()
        finally:
            self.ws_clients.discard(ws)

    async def run(self):
        # Log any asyncio task exception that would otherwise be silently swallowed
        def _async_exc_handler(loop, context):
            exc = context.get('exception', context.get('message', '?'))
            log.error(f"[ASYNCIO UNHANDLED] {exc}")
        asyncio.get_event_loop().set_exception_handler(_async_exc_handler)

        # Bind the dashboard port patiently: after a restart Windows can hold the
        # old socket for a few seconds — wait for it instead of crashing
        server = None
        for _attempt in range(30):
            try:
                server = await websockets.serve(self.ws_handler, config.WS_HOST, config.WS_PORT)
                break
            except OSError:
                if _attempt == 0:
                    log.info(f"[BOOT] Port {config.WS_PORT} still held by previous instance — waiting...")
                await asyncio.sleep(1)
        if server is None:
            log.error(f"[BOOT] Port {config.WS_PORT} blocked for 30s — is another bot already running?")
            raise SystemExit(1)
        # Real-time push streams from Binance (auto-reconnect, REST stays as fallback)
        asyncio.get_event_loop().create_task(self._mark_price_stream())
        asyncio.get_event_loop().create_task(self._user_data_stream())
        # Always-on 1s dashboard heartbeat — live values even mid-scan
        asyncio.get_event_loop().create_task(self._live_update_loop())
        sep = '=' * 70
        dash= '-' * 70
        print(sep)
        print("  AlphaBot v5.0 -- Advanced Binance Futures Testnet")
        print(dash)
        print(f"  Universe   : Top {config.TOP_N_SYMBOLS} USDT perpetuals (all coins)")
        print(f"  Analysis   : 5m + 15m + 1h multi-timeframe")
        print(f"  Indicators : 22 - Ichimoku, Squeeze, Supertrend, Hull MA + more")
        print(f"  Patterns   : 14 candlestick patterns")
        print(f"  Directions : LONG + SHORT (score -100 to +100)")
        print(f"  Sizing     : Kelly Criterion (half-Kelly, ATR-adjusted)")
        print(f"  Exits      : TP1 -> TP2 -> TP3 trail | BE lock | ATR SL")
        print(f"  Context    : OI momentum + L/S ratio + Funding trend")
        print(f"  Guards     : Daily {config.MAX_DAILY_LOSS_PCT}% loss + {config.MAX_DRAWDOWN_PCT}% drawdown")
        print(f"  WebSocket  : ws://{config.WS_HOST}:{config.WS_PORT}")
        print(dash)
        print("  Open: terminal.html in Chrome")
        print(sep)
        # Run scan loop; server runs in background as long as event loop is alive
        await self.scan_loop()


if __name__ == '__main__':
    import traceback as _tb
    _restart_delay = 5
    while True:
        bot = AlphaBot()
        try:
            asyncio.run(bot.run())
            log.info("AlphaBot run() returned cleanly — restarting...")
        except (KeyboardInterrupt, SystemExit):
            log.info("AlphaBot stopped by user.")
            break
        except BaseException as _e:
            log.error(f"CRASH [{type(_e).__name__}]: {_e}\n{_tb.format_exc()}")
            log.info(f"Auto-restarting in {_restart_delay}s...")
        time.sleep(_restart_delay)
