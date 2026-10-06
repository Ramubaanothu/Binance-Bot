"""Runs the real checkout code in a real browser against a tiny fake
Amazon, to check the step logic: price check, Buy Now, choosing Amazon Pay
balance, the balance-covers-total rule, dry run, and Place order.

It cannot prove the selectors match the live amazon.in; the first real
attempts (and their screenshots) do that. Skipped if Playwright/Chromium
are not installed.
"""
import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytest.importorskip('playwright')
from dealbot import config  # noqa: E402
from dealbot.amazon import AmazonBuyer  # noqa: E402

ASIN = 'B0TEST0001'
STATE = {}

NAV = '<div id="nav-link-accountList"><span id="nav-link-accountList-nav-line-1">{}</span></div>'


def page(body, logged_in=True):
    nav = NAV.format('Hello, Ramu' if logged_in else 'Hello, sign in')
    return '<html><body>{}{}</body></html>'.format(nav, body)


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        s = STATE
        p = self.path.split('?')[0]
        if p == '/':
            html = page('home', s['logged_in'])
        elif p == '/dp/' + ASIN:
            html = page('''
              <div id="corePriceDisplay_desktop_feature_div">
                <span class="priceToPay"><span class="a-offscreen">₹{price}</span></span>
                <span class="basisPrice"><span class="a-offscreen">₹9,999</span></span>
              </div>
              <select id="quantity"><option>1</option><option>2</option></select>
              <input id="buy-now-button" type="submit" value="Buy Now"
                     onclick="location.href='/checkout/pay'">'''.format(price=s['price']),
                        s['logged_in'])
        elif p == '/checkout/pay':
            html = page('''
              <h2>Select a payment method</h2>
              <label><input type="checkbox" id="bal"> Use your ₹{bal} Amazon Pay balance</label>
              <label><input type="radio" name="pm"> UPI</label>
              <input type="submit" name="ppw-widgetEvent:SetPaymentPlanSelectContinueEvent"
                     value="Use this payment method"
                     onclick="location.href='/checkout/review?bal='+document.getElementById('bal').checked">
              '''.format(bal=s['balance']))
        elif p == '/checkout/review':
            paying = ('Paying with Amazon Pay Balance' if 'bal=true' in self.path
                      else 'Paying with UPI')
            html = page('''
              <div>{paying}</div>
              <div id="subtotals">Items: ₹{price}<br>Delivery: ₹40<br>Order Total: ₹{total}</div>
              <span id="submitOrderButtonId"><input type="submit" value="Place your order"
                     onclick="location.href='/gp/buy/thankyou'"></span>
              '''.format(paying=paying, price=s['price'], total=s['price'] + 40))
        elif p == '/gp/buy/thankyou':
            s['placed'] += 1
            html = page('Order placed, thank you!')
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_amazon(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STATE.clear()
    STATE.update(logged_in=True, price=99, balance='500.00', placed=0)
    monkeypatch.setattr(config, 'AMAZON_BASE', 'http://127.0.0.1:{}'.format(srv.server_port))
    monkeypatch.setattr(config, 'BROWSER_PROFILE', str(tmp_path / 'profile'))
    monkeypatch.setattr(config, 'SCREENSHOT_DIR', str(tmp_path / 'shots'))
    monkeypatch.setattr(config, 'MAX_ORDER_PRICE', 0)
    monkeypatch.setattr(config, 'MIN_DISCOUNT', 90)
    if not config.CHROMIUM_PATH and os.path.exists('/opt/pw-browsers/chromium'):
        monkeypatch.setattr(config, 'CHROMIUM_PATH', '/opt/pw-browsers/chromium')
    yield STATE
    srv.shutdown()


def buy(alert_price=99, dry_run=False):
    async def go():
        b = AmazonBuyer()
        try:
            return await b.buy(ASIN, alert_price, dry_run)
        finally:
            await b.close()
    return asyncio.run(go())


def test_places_order_with_balance(fake_amazon):
    r = buy()
    assert r.status == 'placed', r.reason
    assert (r.price, r.total, r.balance) == (99, 139, 500)
    assert fake_amazon['placed'] == 1
    assert r.screenshot and os.path.exists(r.screenshot)


def test_dry_run_stops_before_place(fake_amazon):
    r = buy(dry_run=True)
    assert r.status == 'dry_run', r.reason
    assert fake_amazon['placed'] == 0


def test_balance_too_low(fake_amazon):
    fake_amazon['balance'] = '100.00'
    r = buy()
    assert r.status == 'failed' and 'balance' in r.reason
    assert fake_amazon['placed'] == 0


def test_deal_over(fake_amazon):
    fake_amazon['price'] = 999
    r = buy(alert_price=99)
    assert r.status == 'failed' and 'deal over' in r.reason
    assert fake_amazon['placed'] == 0


def test_logged_out(fake_amazon):
    fake_amazon['logged_in'] = False
    r = buy()
    assert r.status == 'failed' and 'logged out' in r.reason
    assert fake_amazon['placed'] == 0
