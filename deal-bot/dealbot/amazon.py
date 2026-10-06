"""Places an order on Amazon.in, paid only from your Amazon Pay balance, in a
headless browser that uses login cookies you exported from your own browser.

The bot never sees your password or OTP. Every step checks before it acts;
anything unexpected stops the order and says why, with a screenshot:
logged out, deal over, the balance does not cover the full total, or Amazon
showing a CAPTCHA / OTP page (the bot never answers those).

Amazon changes its pages, so selectors are lists tried in order. When a step
fails, the screenshot shows what the page looked like.
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from . import config

log = logging.getLogger('dealbot.amazon')

PRICE_SEL = [
    '#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen',
    '#corePrice_feature_div .a-price .a-offscreen',
    '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen',
    '#apex_desktop .priceToPay .a-offscreen',
    '#tp_price_block_total_price_ww .a-offscreen',
    '#priceblock_dealprice',
    '#priceblock_ourprice',
]
MRP_SEL = [
    '#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen',
    '.basisPrice .a-offscreen',
    '#listPrice',
]
BUY_NOW_SEL = ['#buy-now-button', 'input[name="submit.buy-now"]']
PLACE_ORDER_SEL = [
    '#placeOrder', '#submitOrderButtonId input', '#bottomSubmitOrderButtonId input',
    'input[name="placeYourOrder1"]', '#placeYourOrder input',
]
TURBO_PLACE_SEL = ['#turbo-checkout-pyo-button', 'input[name="placeYourOrder1"]']
USE_PAYMENT_SEL = [
    'input[name*="SetPaymentPlanSelectContinueEvent"]',
    '#orderSummaryPrimaryActionBtn input',
]
CHANGE_PAYMENT_SEL = ['#payChangeButtonId', '#payment-information a:has-text("Change")']
ACCOUNT_SEL = ['#nav-link-accountList-nav-line-1', '#nav-link-accountList']

BALANCE_RE = re.compile(r'amazon\s*pay\s*balance', re.I)
# "Amazon Pay Balance: ₹1,234.00" / "Use your ₹1,234 Amazon Pay balance"
BALANCE_AMT_RES = [
    re.compile(r'amazon\s*pay\s*balance[^₹\n]{0,40}₹\s*([\d,]+(?:\.\d+)?)', re.I),
    re.compile(r'₹\s*([\d,]+(?:\.\d+)?)\s*(?:available\s*)?(?:in\s*)?(?:your\s*)?amazon\s*pay\s*balance', re.I),
]
PLACE_RE = re.compile(r'place\s+(your\s+)?order', re.I)
USE_PAYMENT_RE = re.compile(r'use\s+this\s+payment\s+method', re.I)
TOTAL_RE = re.compile(r'order\s+total\s*:?\s*₹\s*([\d,]+(?:\.\d+)?)', re.I)
RUPEES_RE = re.compile(r'₹\s*([\d,]+(?:\.\d+)?)')
DONE_RE = re.compile(r'order\s+placed|thank\s+you,?\s+your\s+order|order\s+has\s+been\s+placed', re.I)
CHALLENGE_RE = re.compile(r'enter the characters|type the characters|captcha|one time password|'
                          r'enter otp|verification code', re.I)

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')


@dataclass
class Result:
    status: str             # placed | dry_run | unknown | failed
    reason: str = ''
    price: float = None     # item price seen on the product page
    total: float = None     # order total seen at checkout
    balance: float = None   # Amazon Pay balance seen at checkout
    screenshot: str = None


def _amt(s):
    return float(s.replace(',', ''))


def rupees(text):
    m = RUPEES_RE.search(text or '')
    return _amt(m.group(1)) if m else None


def balance_in(text):
    for rx in BALANCE_AMT_RES:
        m = rx.search(text or '')
        if m:
            return _amt(m.group(1))
    return None


def total_in(text):
    m = TOTAL_RE.search(text or '')
    return _amt(m.group(1)) if m else None


def price_ok(page_price, page_mrp, alert_price):
    """Is the price on the page still the deal that was posted?

    If the post gave a price, the page must be within 25% (+₹10) of it; the
    deal has usually ended when it is not. If the post only gave a %, the
    page's own MRP must show at least MIN_DISCOUNT.
    """
    if page_price is None:
        return False, 'could not read the price on the product page'
    if alert_price is not None:
        if page_price > alert_price * 1.25 + 10:
            return False, 'price is now ₹{:,.0f}, the post said ₹{:,.0f}; deal over'.format(
                page_price, alert_price)
        return True, ''
    if not page_mrp:
        return False, 'post had no price and the page shows no MRP to check the discount'
    off = (1 - page_price / page_mrp) * 100
    if off < config.MIN_DISCOUNT:
        return False, 'page shows only {:.0f}% off now'.format(off)
    return True, ''


def payment_ok(total, balance):
    """The whole total must come out of the Amazon Pay balance."""
    if total is None:
        return False, 'could not read the order total'
    if balance is None:
        return False, 'could not read your Amazon Pay balance'
    if total > balance:
        return False, 'order total ₹{:,.0f} is more than your ₹{:,.0f} Amazon Pay balance'.format(
            total, balance)
    if config.MAX_ORDER_PRICE and total > config.MAX_ORDER_PRICE:
        return False, 'order total ₹{:,.0f} is above your ₹{:,.0f} cap'.format(
            total, config.MAX_ORDER_PRICE)
    return True, ''


async def _first(scope, selectors, timeout=0):
    """First visible match among selectors, or None."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        for sel in selectors:
            loc = scope.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                pass
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)


async def _text_of(scope, selectors):
    loc = await _first(scope, selectors)
    if loc is None:
        return None
    return (await loc.text_content()) or (await loc.get_attribute('value')) or ''


# ── cookie import ─────────────────────────────────────────────────────────
_SAMESITE = {'strict': 'Strict', 'lax': 'Lax', 'none': 'None', 'no_restriction': 'None'}


def read_cookie_file(path):
    """Cookies exported from your browser, as Playwright cookies.

    Accepts the JSON that cookie-export extensions save (a list, or
    {"cookies": [...]}) and Netscape cookies.txt. Only amazon.in cookies
    are kept.
    """
    raw = open(path, encoding='utf-8').read().strip()
    out = []
    if raw.startswith('[') or raw.startswith('{'):
        data = json.loads(raw)
        items = data.get('cookies', []) if isinstance(data, dict) else data
        for c in items:
            ck = {'name': c['name'], 'value': c['value'],
                  'domain': c.get('domain') or c.get('host', ''),
                  'path': c.get('path', '/'),
                  'secure': bool(c.get('secure', False)),
                  'httpOnly': bool(c.get('httpOnly', False))}
            exp = c.get('expirationDate', c.get('expires'))
            if exp and not c.get('session'):
                ck['expires'] = int(float(exp))
            ss = _SAMESITE.get(str(c.get('sameSite', '')).lower())
            if ss:
                ck['sameSite'] = ss
            out.append(ck)
    else:
        for ln in raw.splitlines():
            if ln.startswith('#HttpOnly_'):
                ln, http_only = ln[len('#HttpOnly_'):], True
            elif not ln.strip() or ln.startswith('#'):
                continue
            else:
                http_only = False
            f = ln.split('\t')
            if len(f) < 7:
                continue
            ck = {'name': f[5], 'value': f[6], 'domain': f[0], 'path': f[2],
                  'secure': f[3].upper() == 'TRUE', 'httpOnly': http_only}
            if f[4] not in ('', '0'):
                ck['expires'] = int(f[4])
            out.append(ck)
    return [c for c in out if c['domain'].lstrip('.').endswith('amazon.in')]


class AmazonBuyer:
    def __init__(self):
        self._pw = None
        self.ctx = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.ctx:
            return
        from playwright.async_api import async_playwright
        os.makedirs(config.SCREENSHOT_DIR, exist_ok=True)
        self._pw = await async_playwright().start()
        self.ctx = await self._pw.chromium.launch_persistent_context(
            config.BROWSER_PROFILE, headless=config.HEADLESS,
            executable_path=config.CHROMIUM_PATH, user_agent=UA,
            locale='en-IN', timezone_id='Asia/Kolkata',
            viewport={'width': 1366, 'height': 900})
        self.ctx.set_default_timeout(15000)

    async def close(self):
        if self.ctx:
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()
        self.ctx = self._pw = None

    async def _shot(self, page, tag):
        path = os.path.join(config.SCREENSHOT_DIR,
                            '{}-{}.png'.format(time.strftime('%Y%m%d-%H%M%S'), tag))
        try:
            await page.screenshot(path=path)
            return path
        except Exception:
            return None

    async def logged_in(self, page):
        t = await _text_of(page, ACCOUNT_SEL)
        return bool(t) and 'sign in' not in t.lower()

    async def import_cookies(self, path):
        """Load exported cookies into the bot's browser; returns the greeting."""
        cookies = read_cookie_file(path)
        if not cookies:
            raise SystemExit('No amazon.in cookies found in ' + path)
        await self.start()
        await self.ctx.add_cookies(cookies)
        page = await self.ctx.new_page()
        try:
            await page.goto(config.AMAZON_BASE + '/', wait_until='domcontentloaded')
            ok = await self.logged_in(page)
            return len(cookies), (await _text_of(page, ACCOUNT_SEL) if ok else None)
        finally:
            await page.close()

    async def buy(self, asin, alert_price=None, dry_run=False):
        """Try to order one unit of `asin`, paid from the Amazon Pay balance."""
        await self.start()
        page = await self.ctx.new_page()
        try:
            res = await self._buy(page, asin, alert_price, dry_run)
        except Exception as e:
            log.exception('order %s crashed', asin)
            res = Result('failed', 'error: {}'.format(str(e).splitlines()[0][:200]))
        if res.screenshot is None:
            res.screenshot = await self._shot(page, asin + '-' + res.status)
        await page.close()
        return res

    async def _buy(self, page, asin, alert_price, dry_run):
        await page.goto('{}/dp/{}'.format(config.AMAZON_BASE, asin), wait_until='domcontentloaded')

        if not await self.logged_in(page):
            return Result('failed', 'logged out of Amazon; export fresh cookies and run '
                          'python -m dealbot amazon-cookies <file>')

        price = rupees(await _text_of(page, PRICE_SEL))
        mrp = rupees(await _text_of(page, MRP_SEL))
        ok, why = price_ok(price, mrp, alert_price)
        if not ok:
            return Result('failed', why, price=price)

        qty = page.locator('select#quantity')
        if await qty.count():
            await qty.select_option('1')

        buy = await _first(page, BUY_NOW_SEL)
        if buy is None:
            return Result('failed', 'no Buy Now button (out of stock?)', price=price)
        await buy.click()

        # Buy Now either opens a quick-checkout popup on the same page, or
        # goes to the full checkout. Wait for whichever appears.
        where = await self._wait_checkout(page)
        if where == 'signin':
            return Result('failed', 'Amazon asked to sign in again; export fresh cookies', price=price)
        if where == 'turbo':
            return await self._turbo(page, price, dry_run)
        if where == 'full':
            return await self._full(page, price, dry_run)
        return Result('failed', 'checkout did not open after Buy Now', price=price)

    async def _wait_checkout(self, page, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            url = page.url
            if '/ap/signin' in url:
                return 'signin'
            if '/checkout' in url or '/gp/buy' in url:
                await page.wait_for_load_state('domcontentloaded')
                return 'full'
            frame = page.locator('#turbo-checkout-iframe')
            if await frame.count() and await frame.is_visible():
                return 'turbo'
            await asyncio.sleep(0.25)
        return None

    async def _turbo(self, page, price, dry_run):
        frame = page.frame_locator('#turbo-checkout-iframe')
        await frame.locator('body').wait_for()
        text = await frame.locator('body').inner_text()
        if not BALANCE_RE.search(text):
            return Result('failed', "Amazon's quick Buy Now is set to another payment. Pay one "
                          'order with Amazon Pay balance yourself so it becomes the default.',
                          price=price)
        balance = balance_in(text) or await self._wallet_balance()
        total = total_in(text)
        ok, why = payment_ok(total, balance)
        if not ok:
            return Result('failed', why, price=price, total=total, balance=balance)
        btn = await _first(frame, TURBO_PLACE_SEL) or frame.get_by_role('button', name=PLACE_RE).first
        return await self._place(page, btn, price, total, balance, dry_run)

    async def _full(self, page, price, dry_run):
        balance = None
        if not await self._on_payment_step(page):
            change = await _first(page, CHANGE_PAYMENT_SEL, timeout=2000)
            if change is not None:
                await change.click()
        if await self._on_payment_step(page, wait=3000):
            text = await page.locator('body').inner_text()
            if not BALANCE_RE.search(text):
                return Result('failed', 'Amazon Pay balance is not offered for this order', price=price)
            balance = balance_in(text)
            box = page.get_by_role('checkbox', name=BALANCE_RE).first
            if await box.count():
                if not await box.is_checked():
                    await box.check()
            else:
                await page.get_by_text(BALANCE_RE).first.click()
            use = await _first(page, USE_PAYMENT_SEL, timeout=3000) or \
                page.get_by_role('button', name=USE_PAYMENT_RE).first
            await use.click()

        btn = await _first(page, PLACE_ORDER_SEL, timeout=10000)
        if btn is None:
            btn = page.get_by_role('button', name=PLACE_RE).first
            if not await btn.count():
                return Result('failed', 'could not find the Place order button', price=price)

        text = await page.locator('body').inner_text()
        if CHALLENGE_RE.search(text):
            return Result('failed', 'Amazon is showing a CAPTCHA/OTP page; order it yourself', price=price)
        if not BALANCE_RE.search(text):
            return Result('failed', 'could not confirm Amazon Pay balance is the payment', price=price)
        balance = balance_in(text) or balance or await self._wallet_balance()
        total = total_in(text)
        ok, why = payment_ok(total, balance)
        if not ok:
            return Result('failed', why, price=price, total=total, balance=balance)
        return await self._place(page, btn, price, total, balance, dry_run)

    async def _on_payment_step(self, page, wait=0):
        if await _first(page, USE_PAYMENT_SEL, timeout=wait) is not None:
            return True
        return await page.get_by_role('button', name=USE_PAYMENT_RE).count() > 0

    async def _wallet_balance(self):
        """Fallback: read the balance from the Amazon Pay page in a second tab."""
        page = await self.ctx.new_page()
        try:
            await page.goto(config.AMAZON_BASE + '/gp/sva/dashboard', wait_until='domcontentloaded')
            return balance_in(await page.locator('body').inner_text())
        except Exception:
            return None
        finally:
            await page.close()

    async def _place(self, page, btn, price, total, balance, dry_run):
        if dry_run:
            shot = await self._shot(page, 'dry-run')
            return Result('dry_run', 'stopped before Place order (dry run)',
                          price, total, balance, shot)
        await btn.click()
        end = time.monotonic() + 20
        while time.monotonic() < end:
            await asyncio.sleep(0.5)
            try:
                text = await page.locator('body').inner_text()
            except Exception:
                continue
            if 'thankyou' in page.url or DONE_RE.search(text):
                return Result('placed', '', price, total, balance)
            if '/ap/' in page.url or CHALLENGE_RE.search(text):
                return Result('failed', 'Amazon showed a CAPTCHA/OTP page after Place order; '
                              'check your orders', price, total, balance)
        return Result('unknown', 'clicked Place order, no confirmation seen; check your orders',
                      price, total, balance)
