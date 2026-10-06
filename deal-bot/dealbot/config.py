"""Settings come from deal-bot/.env (KEY=value lines). Never commit that file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / '.env'


def _load_env():
    if ENV_FILE.exists():
        for ln in ENV_FILE.read_text(encoding='utf-8').splitlines():
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


_load_env()


def _list(name):
    return [x.strip() for x in os.environ.get(name, '').split(',') if x.strip()]


def _bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ('1', 'true', 'yes', 'on')


# Your own Telegram account (reads the loot channels). From my.telegram.org.
API_ID = int(os.environ.get('TG_API_ID') or 0)
API_HASH = os.environ.get('TG_API_HASH', '')
SESSION = str(ROOT / os.environ.get('TG_SESSION', 'dealbot'))

# The bot that sends YOU the alerts (from @BotFather), and where to send them.
BOT_TOKEN = os.environ.get('ALERT_BOT_TOKEN', '')
ALERT_CHAT_ID = os.environ.get('ALERT_CHAT_ID', '')

# Channels to watch: @usernames or numeric ids. Empty = every channel you joined.
CHANNELS = _list('CHANNELS')

MIN_DISCOUNT = float(os.environ.get('MIN_DISCOUNT', 90))
PLATFORMS = _list('PLATFORMS') or ['amazon', 'flipkart']
# Posts saying "price error" / "glitch" alert even when no % can be read.
ALERT_ON_GLITCH_WORDS = _bool('ALERT_ON_GLITCH_WORDS', True)
# Skip anything already alerted for the same product at the same price within this window.
DEDUPE_HOURS = float(os.environ.get('DEDUPE_HOURS', 6))

DB_PATH = str(ROOT / 'seen.sqlite3')

# ── Amazon auto-order (paid from Amazon Pay balance only) ─────────────────
AUTO_ORDER = _bool('AUTO_ORDER', False)
# true = go all the way to "Place your order", screenshot, and stop there.
ORDER_DRY_RUN = _bool('ORDER_DRY_RUN', False)
# Optional extra caps; 0 = off. The Amazon Pay balance is always a hard limit:
# an order whose total the balance does not fully cover is never placed.
MAX_ORDER_PRICE = float(os.environ.get('MAX_ORDER_PRICE', 0))     # order total, ₹
MAX_ORDERS_PER_DAY = int(os.environ.get('MAX_ORDERS_PER_DAY', 0))  # IST calendar day
# The browser profile holds your Amazon login. Treat the folder like a password.
BROWSER_PROFILE = str(ROOT / os.environ.get('BROWSER_PROFILE', 'browser-profile'))
HEADLESS = _bool('HEADLESS', True)
CHROMIUM_PATH = os.environ.get('CHROMIUM_PATH') or None   # default: Playwright's own
AMAZON_BASE = os.environ.get('AMAZON_BASE', 'https://www.amazon.in').rstrip('/')
SCREENSHOT_DIR = str(ROOT / 'screenshots')
