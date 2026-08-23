# Generated from the LIVE droplet config. Keys replaced with
# placeholders. config.py itself is gitignored because it holds real
# credentials - this file exists so the SETTINGS survive the droplet.
# Copy to config.py and fill in your own key/secret to run.

# ─── Binance Futures Live Config ───────────────────────────────────────────────
# Get keys from: https://www.binance.com → Account → API Management

API_KEY    = "PUT_YOUR_BINANCE_DEMO_API_KEY_HERE"
API_SECRET = "PUT_YOUR_BINANCE_DEMO_API_SECRET_HERE"

# ─── Trading params ────────────────────────────────────────────────────────────
LEVERAGE          = 10       # default leverage (volatility-adjusted per coin)
POSITION_SIZE_PCT = 0.02     # legacy — sizing is now risk-based (below)

# PRO RISK MODEL: risk a fixed % of equity per trade; position size is derived
# from the stop distance. Scales identically from a $100 to a $1M account.
RISK_PER_TRADE_PCT = 3.0    # was 5.0. More trades at the same size would
                            # simply multiply the same edge.
                             # (user choice for testnet; margin cap still limits
                             # effective exposure per trade)
MIN_NOTIONAL_USDT  = 6.0     # exchange minimum order value (bumped up, not skipped)
MAX_POSITIONS     = 8        # was 5 - room to hold more at once
MIN_CONFIDENCE    = 42       # was 50. 94% of rejects were this gate.
PAPER_MODE        = False    # True = simulate orders (no real API calls)

# ─── ROI take-profit bands (leveraged P&L %, matches dashboard) ──────────────
# TWO profiles, chosen per-trade at entry. SL stays untouched in both.
#
# SCALP (default) — quick concrete gain on ordinary momentum trades:
TAKE_PROFIT_ROI_1       = 25.0  # full close at +25% ROI (3.125% price at 8x)
                                # each win covers more of the round-trip fee cost)
TAKE_PROFIT_ROI_1_SCALE = 1.0   # 1.0 = close the ENTIRE position at the target
TAKE_PROFIT_ROI_2       = 10.0  # legacy second rung (unused when SCALE=1.0)
SL_MAX_ROI              = 25.0  # symmetric with the target
                                # (SL price clamped + internal ROI backstop)
# PROFIT RATCHET — replaces the old ATR trail for full-take scalps. Evidence
# (2026-07-22..24, 13 trades): 62% win rate but NET NEGATIVE, because wins were
# cut by the ATR trail at +1.4/+2.8/+5.5% while every loss rode the full -7%.
# Only 2 of 8 wins ever reached the +8% target. The ratchet arms LATE (well past
# halfway to target) and keeps most of the gain, so winners can still reach +8%
# but can no longer round-trip from +7% back to -7%.
PROFIT_RATCHET_ARM_ROI  = 0     # OFF - see RATCHET_STEPS
                                # A trade at +24% may now round-trip to -25%.
                                # fifth of the target and booked ~+3%.
PROFIT_RATCHET_KEEP     = 0.60  # once armed, floor = 60% of peak ROI
#
# RUNNER ("Solana-type") — capitulation-reversal caught at the bottom/top;
# let it run for the big recovery move (60→80 style):
RUNNER_TP_ROI_1         = 20.0  # bank part at +20% ROI
RUNNER_TP_ROI_1_SCALE   = 0.5   # fraction closed at the +20% rung
RUNNER_TP_ROI_2         = 50.0  # close the remainder at +50% ROI
RUNNER_MIN_CONF         = 58    # only high-conviction reversals earn the big target
# After TP1 banks, the runner's stop locks at least this ROI% (not pure breakeven)
# — a runner should never round-trip back to ~0 after showing profit (BCH gave
# back to +0.39% when it should protect a minimum).
MIN_RUNNER_PROFIT_ROI   = 2.5

# ─── Premium majors — variable leverage up to 25X ─────────────────────────────
# Whales use high leverage on liquid coins with tight spreads & deep OB
# NOTE: this table no longer sets leverage - dynamic_leverage() does, from
# ATR. It is kept ONLY as the "is this a major?" membership test, which the
# rest of the bot uses for sizing and exit multipliers. The old values
# (BTC 25x, ETH 20x) put the stop inside a single candle and needed a 64%
# hit rate to break even.
MAJOR_LEVERAGE = {
    'BTCUSDT': 8,
    'ETHUSDT': 8,
    'BNBUSDT': 8,
    'SOLUSDT': 8,
    'XRPUSDT': 8,
}
# These are always scanned first, bypassing volume filter
PRIORITY_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
# Majors move gradually so their indicator scores rarely spike like small caps;
# a lower bar levels the field (their signals are cleaner per confidence point)
MAJOR_CONF_DISCOUNT = 8

# ─── BTC-ONLY PURE-CHART MODE ─────────────────────────────────────────────────
# Pauses the altcoin scanner entirely. Trades only BTCUSDT from pure price
# action on 1m/5m/15m/1h charts: structure (HH/HL vs LH/LL), price vs EMA20,
# and candle momentum vote a direction; strong agreement opens the position.
# Counter-trend block: chart mode only reads 1m..1h, so it kept shorting into
# a 4h+daily uptrend. Journal (111 trades) says that is where the losses came
# from — main bot shorts PF 0.46, reverse bot's trend-aligned longs PF 5.51.
# Requires 4h AND 1d agreement with the trade direction. Set False to disable.
CHART_HTF_BLOCK     = True

BTC_ONLY_MODE       = True
BTC_CHART_INTERVAL  = 45     # seconds between chart reads
BTC_CHART_MIN_SCORE = 1.1    # |weighted vote| needed to trade (max 2.5)
BTC_CHART_LEV       = 10     # fixed leverage in chart mode (25x is too hot for scalps)
# Journal evidence, 78 chart-mode trades (2026-07-26 analysis) — per symbol:
#   BTCUSDT  n=21  PF 1.65  +$149.56   ← the only real winner
#   SOLUSDT  n=22  PF 0.94   -$23.28   ← ~breakeven, keeps sample size up
#   XRPUSDT  n=12  PF 0.80   -$44.52
#   BNBUSDT  n=10  PF 0.15  -$254.38   ← dropped
#   ETHUSDT  n=13  PF 0.08  -$421.91   ← dropped
# ETH+BNB alone cost $676 and flipped the whole chart book negative:
#   all 5 symbols = PF 0.63 (-$594)   vs   BTC+SOL = PF 1.20 (+$126)
CHART_SYMBOLS       = ['BTCUSDT', 'SOLUSDT']
# Journal evidence: chart mode = PF 2.24 (+$17/trade), alt scanner = PF 0.19
# (-$25/trade). So the winning chart engine now covers the 5 liquid majors,
# and alts stay ON but on a short leash (half risk) — not abandoned.

# ─── New-listing policy: lottery tickets get lottery sizing ──────────────────
NEW_COIN_DAYS       = 14     # listed less than N days ago = "new coin"
NEW_COIN_RISK_PCT   = 2.0    # new coins risk only 2% of wallet per trade
NEW_COIN_MAX_LOSSES = 2      # 2 consecutive losses on a new coin = benched 24h

# ALT trading OFF — the evidence never improved: alt scanner PF 0.19 lifetime
# vs chart mode PF 1.59. Half-risk and higher confidence bars didn't fix it;
# it kept taxing chart mode's profits (latest: VANRY -$5.55 on 2026-07-15).
# Chart mode (majors) + reverse bot are the two strategies being evaluated.
ALT_TRADING         = False  # OFF. Even with the $100M floor and the
                             # blacklist, alts since 8 Jul: n=48 WR 27.1%
                             # PF 0.36 -$597. Majors over the same window:
                             # n=100 WR 48.0% PF 0.61. Turning alts off is
                             # worth +$651 on that sample.
ALT_RISK_FACTOR     = 0.5    # alts risk HALF of RISK_PER_TRADE_PCT

# ─── Multi-timeframe ───────────────────────────────────────────────────────────
TF_FAST   = '5m'
TF_MED    = '15m'
TF_SLOW   = '1h'
KLINE_LIMIT = 120

# ─── Exit params ───────────────────────────────────────────────────────────────
USE_ATR_EXITS    = True
ATR_SL_MULT      = 2.2       # wider SL — 1.6 ATR sat inside 5m noise (12/13 trades died by SL)
ATR_TP1_MULT     = 3.4       # scaled with SL to keep R:R ≈ 1.55
ATR_TP2_MULT     = 5.5       # TP2 (scale out)
ATR_TP3_MULT     = 9.0       # TP3 (trail from here)
ATR_TRAIL_MULT   = 1.5       # looser trail — 1.0 cut the only winner at just +1.49%

# Premium majors: tighter SL (more liquid), bigger targets (higher leverage)
ATR_SL_MULT_MAJOR  = 1.8    # BTC/ETH tight SL — deep OB, less slippage
ATR_TP1_MULT_MAJOR = 2.8
ATR_TP2_MULT_MAJOR = 4.5
ATR_TP3_MULT_MAJOR = 7.5

# Minimum SL distance as % of entry — blocks instant stopouts on micro-ATR coins
# (a trade was stopped <1s after entry with SL only 0.61% away)
MIN_SL_DIST_PCT  = 0.7

# Fraction of position closed at TP1 — banks profit at the planned R:R instead
# of letting the trail give it back (trade data: winners retraced from TP1 to +1.0-1.5%)
TP1_SCALE_OUT    = 0.5

FIXED_SL_PCT     = 5.0
FIXED_TP1_PCT    = 8.0
FIXED_TP2_PCT    = 16.0
FIXED_TP3_PCT    = 28.0
BE_LOCK_ENABLED  = False     # stays off - the R-ladder moved stops to
                             # breakeven at 0.5R and booked near-zero exits
BE_LOCK_PCT      = 1.5       # lock breakeven at this PRICE move %
BE_LOCK_ROI      = 3.0       # OR lock breakeven at this ROI% (leverage-independent —
                             # protects low-leverage coins whose small price move
                             # still earns a real ROI, e.g. XPL 4x peaked +4% then lost)
MAX_GAIN_PCT     = 200.0
MAX_CONSEC_LOSSES = 4        # was 2. At a 35% win rate a 2-streak is routine,
                             # so the brake was idling the bot for hours.

# Pro-trader gates
MIN_RR_RATIO     = 1.5       # TP1_dist / SL_dist must exceed this before entry.
                             # ATR exits give ~1.55 (TP1 3.4 ATR / SL 2.2 ATR), so
                             # 2.0 rejected EVERY standard setup — now consistent.
VOL_SPIKE_MULT   = 1.2       # last closed candle vs 20-bar avg — overnight candles
                             # run 0.5-0.8x; only reject truly dead volume
BEAR_LONG_EXTRA  = 12        # extra confidence needed to go LONG in bear market
BULL_SHORT_EXTRA = 12        # extra confidence needed to SHORT in bull market

# ─── Risk management ───────────────────────────────────────────────────────────
MAX_DAILY_LOSS_PCT   = 20.0   # widened for 10% risk-per-trade (user's choice)
MAX_DRAWDOWN_PCT     = 15.0   # 15% tripped constantly at 10% risk; halt only on
                              # genuine catastrophe. Auto-resumes when recovered.
MAX_CORRELATED_PAIRS = 2
MIN_VOLUME_USDT      = 100_000_000  # was $10M, which admitted every coin that
                                    # blew up: XMR $21M/day cost -$1,094, ARB
                                    # $23M, MIRA $4M, MANA $3M, TRB $2M. At
                                    # $100M only 31 symbols qualify.
SMALLCAP_VOLUME_USDT = 250_000_000  # below this counts as small: stricter entry
SMALLCAP_MAX_POSITIONS = 3         # at most 3 of 5 slots in small caps at once
SMALLCAP_EXTRA_CONF  = 2           # small caps need +2% confidence (night watch #2:
                                   # 36 of 107 passing signals died here; venue-price
                                   # gate now covers the divergence risk)

# ─── Scanner ───────────────────────────────────────────────────────────────────
SCAN_ALL_PERPS   = True      # scan every USDT perp above the $15M volume floor
TOP_N_SYMBOLS    = 300       # fallback: top N by volume if SCAN_ALL fails
TOP_MOVERS_N     = 40        # top N by abs(24h%) — scanned first

# ─── Trend-strength & anti-chase (overextension) filters ─────────────────────
ADX_MIN            = 20.0    # require a real trend — no ADX (<20), no trade
SL_COOLDOWN_MIN    = 30      # don't re-enter a symbol for N min after a stop-loss
# Reject a LONG that has already run too far, too fast (chasing). Runup measured
# from the recent low over each window; mirror (drawdown from high) for SHORT.
MOVE_15M_MAX       = 8.0     # % — 15-min move cap
MOVE_1H_MAX        = 12.0    # % — 1-hour move cap
MOVE_4H_MAX        = 20.0    # % — 4-hour move cap
MOVE_24H_MAX       = 35.0    # % — 24-hour move cap
EMA_DISTANCE_MAX   = 4.5     # % — max distance of price from 5m EMA20
VWAP_DISTANCE_MAX  = 4.0     # % — max distance from rolling VWAP (2% strangled
                             # everything; crypto routinely trades 5-15%/day)
LAST_CANDLE_ATR_MAX= 1.8     # last 5m candle range must be < this × ATR (no climax spike)
CHANGE_24H_MAX     = 18.0    # % — max absolute 24h ticker change
EXTENSION_REJECT   = 40      # composite extension score at/above this = reject
SCAN_INTERVAL_SEC = 40      # pause between scan cycles
SCAN_PARALLEL    = 6        # coins analysed simultaneously (scan ~3x faster)
RATE_LIMIT_DELAY = 0.12     # global gap between API calls — keeps 6 parallel
                            # workers under Binance's 2400 weight/min budget

# ─── Whale / sentiment thresholds ─────────────────────────────────────────────
RSI_OVERSOLD_EXTREME  = 25   # RSI below this = capitulation, allow LONG even in bear
RSI_OVERBOUGHT_EXTREME= 75   # RSI above this = euphoria, allow SHORT even in bull
FUNDING_EXTREME_POS   = 0.15 # > 0.15% = longs crowded → fade longs, favour shorts
FUNDING_EXTREME_NEG   = -0.10 # < -0.10% = shorts crowded → fade shorts, favour longs
OI_SURGE_THRESH       = 8.0  # OI change >8% on breakout = institutional entry signal

# ─── Server ────────────────────────────────────────────────────────────────────
WS_HOST = 'localhost'
WS_PORT = 8765

# Symbols banned by name regardless of volume - each lost heavily and HYPE
# clears the volume floor on turnover alone.
ALT_BLACKLIST = {'HYPEUSDT', 'XMRUSDT', 'ARBUSDT', 'MIRAUSDT', 'MANAUSDT',
                 'ETHFIUSDT', 'TRBUSDT', 'APTUSDT', 'XLMUSDT', 'PENGUUSDT',
                 'DEXEUSDT', 'ESPORTSUSDT'}

# ─── TOKENIZED EQUITIES ───────────────────────────────────────────────────
# Equity/ETF perps on Binance futures. Verified tradable on the TESTNET (where
# orders actually land) AND liquid on the live book. Volumes at time of adding:
#   INTC $170M | QQQ $141M | PLTR $133M | CRCL $109M | NVDA $104M
#   TSLA  $82M | MSTR $76M | GOOGL $66M | SPY  $54M  | META  $52M
# Not included: SOXL/SOXS carry the biggest books of all but the testnet does
# not list them. DIAUSDT is the crypto DIA oracle token, not the Dow ETF.
STOCK_TRADING         = True
STOCK_SYMBOLS         = [
    # Commodities carry by far the deepest books of any non-crypto here:
    # gold $2.2bn/day, silver $1.17bn - more than every equity combined.
    'XAUUSDT', 'XAGUSDT',
    # Equities + ETFs, all verified tradable on the TESTNET where orders land
    'NVDAUSDT', 'INTCUSDT', 'PLTRUSDT', 'QQQUSDT', 'CRCLUSDT',
    'TSLAUSDT', 'MSTRUSDT', 'GOOGLUSDT', 'SPYUSDT', 'METAUSDT',
    'COINUSDT', 'AMZNUSDT', 'HOODUSDT',
]   # thin ones are dropped live by STOCK_MIN_VOLUME_USDT each scan
STOCK_MIN_VOLUME_USDT = 25_000_000   # was 50M; admits SPY/META/COIN too
# These track a market that is CLOSED most of the day. Left 24/7 by choice -
# outside US hours the underlying does not move, so expect flat price action
# and thinner books in the Asia session.
STOCK_US_HOURS_ONLY   = False

# ─── LEVERAGE: the stop must clear the noise ──────────────────────────────
# leverage = SL_MAX_ROI / (LEV_ATR_MULT * atr_pct), clamped 2..LEV_MAX.
# Fees are 0.08% of notional per round trip, i.e. 0.08*L % of MARGIN, so the
# break-even hit rate is 51.7% at 3x, 55.7% at 10x, 64.3% at 25x. Measured
# baseline for a symmetric barrier is ~52.7% - which is why the old BTC=25x
# setting could not win no matter what the signal did.
LEV_ATR_MULT = 1.5    # stop must sit at least 1.5 ATR from entry
LEV_MAX      = 8      # back to 8x: at 8x a 25% ROI stop is a 3.125%
                      # price move, which clears the measured p75 adverse
                      # excursion (-3.14%). The stop is wide because the
                      # ROI is wide, not because leverage is low.
                      # on 10 majors over 15 days, 8%/7% ROI barriers:
                      #   8x  50.5% vs 50.9% needed   (break-even)
                      #   5x  50.8% vs 49.3%          (+1.5)
                      #   4x  52.0% vs 48.8%          (+3.2)
                      #   3x  57.9% vs 48.3%          (+9.6)
                      # The structure was never the problem - at 8x the stop
                      # sits inside the noise, so the higher-timeframe trend
                      # never gets room to pay off.

# _clamp_sl_roi only ever NARROWS a wide stop; a tighter ATR stop survives it.
# With SL_MAX_ROI at 25% that would have left the real stop at ~6% ROI and the
# setting would have done nothing. This forces the stop to exactly SL_MAX_ROI.
SL_FIXED_ROI = True

# The 25/25 edge was measured on a 24-HOUR hold and decays to break-even
# beyond it. Nothing in the bot closed on time before, so without this the
# configuration is not the one that was tested.
MAX_HOLD_HOURS = 0    # OFF (user): no time exit. A position rides until
                      # +25% or -25%. NOTE the measurement this gives up:
                      # 25/25 scored 70.7% on a 24h hold, 49.5% at 72h,
                      # 51.8% at 168h - the edge was in the short hold.

# Stepped profit ratchet: once peak ROI reaches the first number, the stop is
# moved to guarantee the second. Ascending, last match wins. The floor only
# moves in the profitable direction, so a step reached is never given back.
RATCHET_STEPS = ()   # OFF: replaying all 16 ratchet exits, 13 would have reached
                     # +25% and only 1 would have hit the stop. The +10%->+5% step
                     # is a 0.625% price move at 8x - smaller than BTC noise, so
                     # every rally pause tripped it and booked ~+5% instead of +25%.

# ─── PORTFOLIO EXPOSURE CAP (cross-book) ──────────────────────────────────
# Each bot policed only its own book, so three programs independently built
# $7,883 of one-way long beta on $15,700 of equity - none of them in breach
# of its own MAX_POSITIONS or daily-loss limit. This caps NET directional
# exposure across main + reverse + spot combined. Only the side that makes
# the imbalance worse is blocked; closing and hedging stay open.
MAX_PORTFOLIO_EXPOSURE_PCT = 60.0
