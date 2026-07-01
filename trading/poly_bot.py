"""
PolyAlpha — Polymarket Paper Trading Bot
Scans prediction markets, scores for edge, paper trades YES/NO positions.
WebSocket server → poly_terminal.html dashboard on ws://localhost:8766
"""

import asyncio, json, logging, time, math, re
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from collections import deque
from pathlib import Path
from typing import Optional

import requests
import websockets

# ── Config ─────────────────────────────────────────────────────────────────────
PAPER_BALANCE       = 1000.0    # starting USDC
POSITION_SIZE_PCT   = 0.08      # 8% per trade
MAX_POSITIONS       = 8
MIN_SCORE           = 35        # minimum signal score to enter (0-100)
MIN_VOLUME          = 20_000    # minimum market volume USDC
MIN_LIQUIDITY       = 5_000     # minimum liquidity
SL_PCT              = 0.35      # stop loss: close if price moves 35% against us
TP1_PCT             = 0.25      # take profit 1: 25% gain
TP2_PCT             = 0.55      # take profit 2: 55% gain
SCAN_INTERVAL       = 60        # seconds between full scans
WS_PORT             = 8766
FETCH_LIMIT         = 80        # markets to fetch per scan

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)-8s] %(message)s',
    handlers=[
        logging.FileHandler('poly_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class Market:
    id: str
    question: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    end_date: str
    slug: str
    score: float = 0.0
    direction: str = ''      # 'yes' or 'no'
    signals: dict = field(default_factory=dict)
    category: str = ''

@dataclass
class Position:
    id: str
    question: str
    slug: str
    direction: str           # 'yes' or 'no'
    entry_price: float
    current_price: float
    size_usdc: float
    shares: float
    tp1: float
    tp2: float
    sl: float
    phase: str = 'open'      # open / tp1-hit / closed
    tp1_hit: bool = False
    pnl_pct: float = 0.0
    pnl_usdc: float = 0.0
    open_time: str = ''
    volume: float = 0.0
    score: float = 0.0

@dataclass
class Trade:
    id: int
    question: str
    direction: str
    entry: float
    exit_price: float
    pnl_pct: float
    pnl_usdc: float
    reason: str
    open_time: str
    close_time: str
    is_win: bool

# ── Signal scorer ──────────────────────────────────────────────────────────────
BULLISH_KEYWORDS = [
    'will btc','will bitcoin','will eth','will crypto','will sol','will bnb',
    'will trump','will fed','will rate cut','will inflation','above','higher than',
    'reach','hit','exceed','pass','approve','launch','win','pass','yes majority'
]
BEARISH_KEYWORDS = [
    'will crash','will fall','will drop','ban','fail','below','under','reject',
    'not reach','lose','fall below'
]

def score_market(m: Market) -> tuple[float, str, dict]:
    """Score a market 0-100, return (score, direction, signals)"""
    signals = {}
    score = 0.0

    yes_p = m.yes_price
    no_p  = m.no_price

    # ── 1. Volume signal (0-25 pts) ───────────────────────────────────────────
    vol_score = min(25, math.log10(max(m.volume, 1)) / math.log10(10_000_000) * 25)
    signals['volume'] = round(vol_score, 1)
    score += vol_score

    # ── 2. Price range — avoid 50/50 noise and extreme certainty ─────────────
    # Sweet spot: 0.55-0.85 for YES or 0.55-0.85 for NO (i.e. not at 50/50)
    spread_from_mid = abs(yes_p - 0.5)
    if 0.05 <= spread_from_mid <= 0.40:
        range_score = spread_from_mid / 0.40 * 20
    else:
        range_score = 0  # too close to 50/50 or too extreme (>90%)
    signals['price_edge'] = round(range_score, 1)
    score += range_score

    # ── 3. Momentum direction — pick the favoured side ────────────────────────
    # YES > 0.55 → bet YES;  NO > 0.55 → bet NO
    if yes_p >= 0.55:
        direction = 'yes'
        mom_score = (yes_p - 0.55) / 0.45 * 20
    elif no_p >= 0.55:
        direction = 'no'
        mom_score = (no_p - 0.55) / 0.45 * 20
    else:
        direction = 'yes' if yes_p >= no_p else 'no'
        mom_score = 2.0
    signals['momentum'] = round(mom_score, 1)
    score += mom_score

    # ── 4. Liquidity quality (0-15 pts) ──────────────────────────────────────
    liq_score = min(15, math.log10(max(m.liquidity, 1)) / math.log10(500_000) * 15)
    signals['liquidity'] = round(liq_score, 1)
    score += liq_score

    # ── 5. Question keyword sentiment (0-20 pts) ──────────────────────────────
    q = m.question.lower()
    kw_score = 0.0
    for kw in BULLISH_KEYWORDS:
        if kw in q:
            kw_score += 4; break
    for kw in BEARISH_KEYWORDS:
        if kw in q:
            kw_score += 4; break
    # Crypto markets get bonus during any scan
    if any(x in q for x in ['bitcoin','btc','eth','crypto','solana','defi','nft','blockchain']):
        kw_score += 8
        m.category = 'Crypto'
    elif any(x in q for x in ['trump','president','election','senate','congress','fed','rate']):
        kw_score += 6
        m.category = 'Politics'
    elif any(x in q for x in ['nba','nfl','premier','champions','world cup','super bowl']):
        kw_score += 5
        m.category = 'Sports'
    elif any(x in q for x in ['ai','openai','gpt','model','nvidia','apple','microsoft','google']):
        kw_score += 7
        m.category = 'Tech'
    else:
        m.category = 'Other'
    signals['keyword'] = round(min(kw_score, 20), 1)
    score += min(kw_score, 20)

    # ── Penalise very short end dates (< 3 days) ─────────────────────────────
    try:
        end = datetime.fromisoformat(m.end_date.replace('Z', '+00:00'))
        days_left = (end - datetime.now(timezone.utc)).days
        if days_left < 1:
            score *= 0.3   # almost expired
        elif days_left < 3:
            score *= 0.7
    except Exception:
        pass

    return round(min(score, 100), 1), direction, signals


def entry_exits(direction: str, price: float) -> tuple[float, float, float]:
    """Return (tp1, tp2, sl) for a position"""
    if direction == 'yes':
        tp1 = min(0.99, price * (1 + TP1_PCT))
        tp2 = min(0.99, price * (1 + TP2_PCT))
        sl  = max(0.01, price * (1 - SL_PCT))
    else:
        tp1 = max(0.01, price * (1 - TP1_PCT))
        tp2 = max(0.01, price * (1 - TP2_PCT))
        sl  = min(0.99, price * (1 + SL_PCT))
    return round(tp1, 4), round(tp2, 4), round(sl, 4)


# ── PolyAlpha bot ──────────────────────────────────────────────────────────────
class PolyAlphaBot:
    _TRADES_FILE = Path(__file__).parent / 'trades_poly.json'

    def __init__(self):
        self.balance     = PAPER_BALANCE
        self.start_bal   = PAPER_BALANCE
        self.peak_bal    = PAPER_BALANCE
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.markets: list[Market] = []
        self.scan_count  = 0
        self.trade_id    = 0
        self.ws_clients: set = set()
        self.daily_pnl   = 0.0
        self._load_trades()

    def _load_trades(self):
        if not self._TRADES_FILE.exists():
            return
        try:
            data = json.loads(self._TRADES_FILE.read_text(encoding='utf-8'))
            raw  = data.get('trades', [])
            loaded = []
            for r in raw:
                try:
                    loaded.append(Trade(**{k: v for k, v in r.items() if k in Trade.__dataclass_fields__}))
                except Exception:
                    pass
            self.trades    = loaded
            self.trade_id  = data.get('trade_id', len(self.trades))
            self.balance   = data.get('balance', PAPER_BALANCE)
            self.start_bal = data.get('start_bal', PAPER_BALANCE)
            self.peak_bal  = data.get('peak_bal', PAPER_BALANCE)
            log.info(f"[STATE  ] Loaded {len(self.trades)} trades, balance=${self.balance:.2f}")
        except Exception as e:
            log.warning(f"[STATE  ] Could not load trades: {e}")

    def _save_trades(self):
        try:
            self._TRADES_FILE.write_text(json.dumps({
                'trades':    [asdict(t) for t in self.trades[-500:]],
                'trade_id':  self.trade_id,
                'balance':   round(self.balance, 4),
                'start_bal': self.start_bal,
                'peak_bal':  round(self.peak_bal, 4),
            }), encoding='utf-8')
        except Exception as e:
            log.warning(f"[STATE  ] Trade save error: {e}")

    # ── REST fetcher ────────────────────────────────────────────────────────────
    def fetch_markets(self) -> list[Market]:
        try:
            params = {
                'limit': FETCH_LIMIT,
                'active': 'true',
                'closed': 'false',
                'order': 'volume',
                'ascending': 'false',
            }
            r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            log.error(f"fetch_markets failed: {e}")
            return []

        markets = []
        for m in raw:
            try:
                prices = json.loads(m.get('outcomePrices', '[0.5, 0.5]'))
                yes_p  = float(prices[0])
                no_p   = float(prices[1])
                vol    = float(m.get('volume', 0))
                liq    = float(m.get('liquidity', 0))
                if vol < MIN_VOLUME or liq < MIN_LIQUIDITY:
                    continue
                if m.get('closed') or not m.get('active'):
                    continue
                markets.append(Market(
                    id       = m['id'],
                    question = m.get('question', '?')[:120],
                    yes_price= yes_p,
                    no_price = no_p,
                    volume   = vol,
                    liquidity= liq,
                    end_date = m.get('endDate', ''),
                    slug     = m.get('slug', m['id']),
                ))
            except Exception:
                continue
        return markets

    def refresh_prices(self) -> None:
        """Update prices for open positions"""
        if not self.positions:
            return
        slugs = [p.slug for p in self.positions.values()]
        try:
            r = requests.get(f"{GAMMA_API}/markets",
                params={'limit': 30, 'active': 'true', 'closed': 'false'}, timeout=10)
            raw = r.json()
            price_map = {}
            for m in raw:
                try:
                    prices = json.loads(m.get('outcomePrices', '[0.5, 0.5]'))
                    price_map[m.get('slug', '')] = (float(prices[0]), float(prices[1]))
                    price_map[m.get('id', '')]   = (float(prices[0]), float(prices[1]))
                except Exception:
                    pass
            for pos in self.positions.values():
                if pos.slug in price_map:
                    yp, np2 = price_map[pos.slug]
                    pos.current_price = yp if pos.direction == 'yes' else np2
        except Exception as e:
            log.warning(f"refresh_prices: {e}")

    # ── Trade logic ─────────────────────────────────────────────────────────────
    def open_position(self, m: Market, score: float, direction: str, signals: dict) -> None:
        if m.id in self.positions:
            return
        if len(self.positions) >= MAX_POSITIONS:
            return
        size = self.balance * POSITION_SIZE_PCT
        if size < 5:
            log.warning("Balance too low to trade")
            return

        price  = m.yes_price if direction == 'yes' else m.no_price
        tp1, tp2, sl = entry_exits(direction, price)

        # Guard: for NO (short), SL must be above entry; for YES (long), SL must be below entry
        if direction == 'no' and sl <= price:
            log.info(f"[SKIP   ] NO {m.question[:50]} — price {price:.4f} too high, SL={sl:.4f} <= entry (would trigger immediately)")
            return
        if direction == 'yes' and sl >= price:
            log.info(f"[SKIP   ] YES {m.question[:50]} — price {price:.4f} too low, SL={sl:.4f} >= entry")
            return

        shares = size / price
        pos = Position(
            id           = m.id,
            question     = m.question,
            slug         = m.slug,
            direction    = direction,
            entry_price  = price,
            current_price= price,
            size_usdc    = size,
            shares       = round(shares, 2),
            tp1          = tp1,
            tp2          = tp2,
            sl           = sl,
            open_time    = datetime.now(timezone.utc).strftime('%H:%M:%S'),
            volume       = m.volume,
            score        = score,
        )
        self.positions[m.id] = pos
        self.balance -= size
        log.info(f"[EXEC   ] {direction.upper()} {m.question[:60]} @ {price:.4f} | "
                 f"size=${size:.2f} | tp1={tp1:.4f} tp2={tp2:.4f} sl={sl:.4f} | score={score:.0f}%")

    def update_positions(self) -> None:
        to_close = []
        for mid, pos in self.positions.items():
            cp = pos.current_price
            ep = pos.entry_price

            if pos.direction == 'yes':
                pos.pnl_pct  = (cp - ep) / ep * 100
                hit_tp1 = cp >= pos.tp1
                hit_tp2 = cp >= pos.tp2
                hit_sl  = cp <= pos.sl
            else:
                pos.pnl_pct  = (ep - cp) / ep * 100
                hit_tp1 = cp <= pos.tp1
                hit_tp2 = cp <= pos.tp2
                hit_sl  = cp >= pos.sl

            pos.pnl_usdc = pos.shares * (cp - ep) if pos.direction == 'yes' \
                           else pos.shares * (ep - cp)

            if hit_tp2:
                to_close.append((mid, 'TP2'))
                pos.phase = 'tp2-hit'
            elif hit_tp1 and not pos.tp1_hit:
                pos.tp1_hit = True
                pos.phase   = 'tp1-hit'
                log.info(f"[TP1    ] {pos.question[:50]} hit TP1 @ {cp:.4f} ({pos.pnl_pct:+.1f}%)")
            elif hit_sl:
                to_close.append((mid, 'SL'))
                pos.phase = 'sl-hit'

        for mid, reason in to_close:
            self.close_position(mid, reason)

    def close_position(self, mid: str, reason: str) -> None:
        pos = self.positions.pop(mid, None)
        if not pos:
            return
        cp = pos.current_price
        ep = pos.entry_price
        pnl_usdc = pos.shares * (cp - ep) if pos.direction == 'yes' \
                   else pos.shares * (ep - cp)
        pnl_pct  = (pnl_usdc / pos.size_usdc) * 100
        self.balance   += pos.size_usdc + pnl_usdc
        self.daily_pnl += pnl_usdc
        self.peak_bal   = max(self.peak_bal, self.balance)
        self.trade_id  += 1
        is_win = pnl_usdc > 0
        self.trades.append(Trade(
            id         = self.trade_id,
            question   = pos.question,
            direction  = pos.direction,
            entry      = ep,
            exit_price = cp,
            pnl_pct    = round(pnl_pct, 2),
            pnl_usdc   = round(pnl_usdc, 4),
            reason     = reason,
            open_time  = pos.open_time,
            close_time = datetime.now(timezone.utc).strftime('%H:%M:%S'),
            is_win     = is_win,
        ))
        self._save_trades()
        icon = '✅' if is_win else '❌'
        log.info(f"[{reason:<6}] {icon} {pos.direction.upper()} {pos.question[:50]} "
                 f"exit={cp:.4f} pnl={pnl_pct:+.1f}% (${pnl_usdc:+.2f})")

    # ── WebSocket ───────────────────────────────────────────────────────────────
    async def ws_handler(self, ws) -> None:
        self.ws_clients.add(ws)
        log.info(f"Dashboard connected ({len(self.ws_clients)} clients)")
        try:
            await ws.wait_closed()
        finally:
            self.ws_clients.discard(ws)

    async def broadcast(self) -> None:
        if not self.ws_clients:
            return
        wins   = sum(1 for t in self.trades if t.is_win)
        losses = len(self.trades) - wins
        wr     = (wins / len(self.trades) * 100) if self.trades else 0
        total_pnl = self.balance - self.start_bal

        pos_list = []
        for p in self.positions.values():
            pos_list.append({
                'id': p.id, 'question': p.question, 'direction': p.direction,
                'entry_price': p.entry_price, 'current_price': p.current_price,
                'size_usdc': p.size_usdc, 'shares': p.shares,
                'tp1': p.tp1, 'tp2': p.tp2, 'sl': p.sl,
                'phase': p.phase, 'tp1_hit': p.tp1_hit,
                'pnl_pct': round(p.pnl_pct, 2),
                'pnl_usdc': round(p.pnl_usdc, 4),
                'open_time': p.open_time, 'volume': p.volume, 'score': p.score,
            })

        trade_list = [asdict(t) for t in self.trades[-100:]]

        market_list = []
        for m in self.markets[:60]:
            market_list.append({
                'id': m.id, 'question': m.question,
                'yes_price': m.yes_price, 'no_price': m.no_price,
                'volume': m.volume, 'liquidity': m.liquidity,
                'score': m.score, 'direction': m.direction,
                'signals': m.signals, 'category': m.category,
                'end_date': m.end_date[:10] if m.end_date else '?',
                'in_pos': m.id in self.positions,
            })

        payload = json.dumps({
            'type': 'state',
            'balance': round(self.balance, 4),
            'start_bal': self.start_bal,
            'peak_bal': round(self.peak_bal, 4),
            'daily_pnl': round(self.daily_pnl, 4),
            'total_pnl': round(total_pnl, 4),
            'wins': wins, 'losses': losses, 'win_rate': round(wr, 1),
            'scan_count': self.scan_count,
            'positions': pos_list,
            'trades': trade_list,
            'markets': market_list,
        })
        dead = set()
        for ws in self.ws_clients:
            try:
                await ws.send(payload)
            except Exception:
                dead.add(ws)
        self.ws_clients -= dead

    # ── Main loops ──────────────────────────────────────────────────────────────
    async def scan_loop(self) -> None:
        await asyncio.sleep(2)
        while True:
            self.scan_count += 1
            log.info(f"[SCAN   ] ── Scan #{self.scan_count} | balance=${self.balance:.2f} | open={len(self.positions)}/{MAX_POSITIONS} ──")

            # Refresh prices for open positions
            if self.positions:
                self.refresh_prices()
                self.update_positions()

            # Fetch and score markets
            raw_markets = self.fetch_markets()
            scored = []
            for m in raw_markets:
                sc, direction, signals = score_market(m)
                m.score     = sc
                m.direction = direction
                m.signals   = signals
                scored.append(m)

            scored.sort(key=lambda x: x.score, reverse=True)
            self.markets = scored

            # Log top candidates
            passes = [m for m in scored if m.score >= MIN_SCORE]
            for m in passes[:5]:
                log.info(f"[PASS   ] {m.direction.upper()} '{m.question[:60]}' "
                         f"score={m.score:.0f}% yes={m.yes_price:.3f} "
                         f"vol=${m.volume:,.0f} cat={m.category}")

            fails = [m for m in scored if m.score < MIN_SCORE]
            for m in fails[:3]:
                log.info(f"[FAIL   ] '{m.question[:55]}' score={m.score:.0f}% → below {MIN_SCORE}%")

            # Open new positions
            for m in passes:
                if len(self.positions) >= MAX_POSITIONS:
                    break
                if m.id not in self.positions:
                    self.open_position(m, m.score, m.direction, m.signals)

            await self.broadcast()
            log.info(f"[SCAN   ] #{self.scan_count} done | {len(passes)} pass | balance=${self.balance:.2f}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def price_update_loop(self) -> None:
        """Refresh prices every 20 seconds and broadcast"""
        await asyncio.sleep(10)
        while True:
            if self.positions:
                self.refresh_prices()
                self.update_positions()
                await self.broadcast()
            await asyncio.sleep(20)

    async def run(self) -> None:
        log.info("━" * 65)
        log.info("  PolyAlpha Paper Trading Bot")
        log.info(f"  Balance: ${self.balance:.2f} USDC | Max positions: {MAX_POSITIONS}")
        log.info(f"  Min score: {MIN_SCORE}% | SL: {SL_PCT*100:.0f}% | TP1: {TP1_PCT*100:.0f}% | TP2: {TP2_PCT*100:.0f}%")
        log.info("━" * 65)

        server = await websockets.serve(self.ws_handler, 'localhost', WS_PORT)
        log.info(f"WebSocket server: ws://localhost:{WS_PORT}")
        log.info(f"Dashboard:        open trading/poly_terminal.html in Chrome")
        log.info("━" * 65)

        await asyncio.gather(
            self.scan_loop(),
            self.price_update_loop(),
        )


if __name__ == '__main__':
    asyncio.run(PolyAlphaBot().run())
