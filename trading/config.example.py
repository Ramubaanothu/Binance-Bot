# ─── Binance Futures Config — copy this to config.py and fill in your keys ───
# Get testnet keys: https://testnet.binancefuture.com → Login → API Management
# Get live keys:    https://www.binance.com → Account → API Management

API_KEY    = "YOUR_API_KEY_HERE"
API_SECRET = "YOUR_API_SECRET_HERE"

# ─── Trading params ────────────────────────────────────────────────────────────
LEVERAGE          = 10       # default leverage
POSITION_SIZE_PCT = 0.02     # 2% of balance per trade
MAX_POSITIONS     = 5        # max concurrent open positions
MIN_CONFIDENCE    = 54       # minimum signal score to open trade
PAPER_MODE        = False    # True = simulate orders (no real API calls)

# ─── Premium majors — variable leverage up to 25X ─────────────────────────────
MAJOR_LEVERAGE = {
    'BTCUSDT': 25,
    'ETHUSDT': 20,
    'BNBUSDT': 15,
    'SOLUSDT': 15,
    'XRPUSDT': 15,
}
PRIORITY_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']

# ─── Multi-timeframe ───────────────────────────────────────────────────────────
TF_FAST    = '5m'
TF_MED     = '15m'
TF_SLOW    = '1h'
KLINE_LIMIT = 120

# ─── Exit params ───────────────────────────────────────────────────────────────
USE_ATR_EXITS    = True
ATR_SL_MULT      = 1.6
ATR_TP1_MULT     = 2.8
ATR_TP2_MULT     = 5.0
ATR_TP3_MULT     = 8.5
ATR_TRAIL_MULT   = 1.0

ATR_SL_MULT_MAJOR  = 1.3
ATR_TP1_MULT_MAJOR = 2.2
ATR_TP2_MULT_MAJOR = 4.0
ATR_TP3_MULT_MAJOR = 7.0

FIXED_SL_PCT      = 5.0
FIXED_TP1_PCT     = 8.0
FIXED_TP2_PCT     = 16.0
FIXED_TP3_PCT     = 28.0
BE_LOCK_PCT       = 1.5
MAX_GAIN_PCT      = 200.0
MAX_CONSEC_LOSSES = 2

# ─── Pro-trader gates ─────────────────────────────────────────────────────────
MIN_RR_RATIO     = 1.8
VOL_SPIKE_MULT   = 1.15
BEAR_LONG_EXTRA  = 20
BULL_SHORT_EXTRA = 20

# ─── Risk management ───────────────────────────────────────────────────────────
MAX_DAILY_LOSS_PCT   = 8.0
MAX_DRAWDOWN_PCT     = 15.0
MAX_CORRELATED_PAIRS = 2
MIN_VOLUME_USDT      = 500_000

# ─── Scanner ───────────────────────────────────────────────────────────────────
SCAN_ALL_PERPS    = True
TOP_N_SYMBOLS     = 300
TOP_MOVERS_N      = 60
SCAN_INTERVAL_SEC = 20
RATE_LIMIT_DELAY  = 0.04

# ─── Sentiment thresholds ─────────────────────────────────────────────────────
RSI_OVERSOLD_EXTREME   = 22
RSI_OVERBOUGHT_EXTREME = 78
FUNDING_EXTREME_POS    = 0.15
FUNDING_EXTREME_NEG    = -0.10
OI_SURGE_THRESH        = 8.0

# ─── Server ────────────────────────────────────────────────────────────────────
WS_HOST = 'localhost'
WS_PORT = 8765
