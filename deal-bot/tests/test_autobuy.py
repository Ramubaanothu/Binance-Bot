import asyncio
import json
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dealbot import autobuy, config  # noqa: E402
from dealbot.amazon import (Result, balance_in, payment_ok, price_ok,  # noqa: E402
                            read_cookie_file, total_in)
from dealbot.parse import analyse  # noqa: E402
from dealbot.store import Orders  # noqa: E402


def test_balance_and_total_parsing():
    assert balance_in('Amazon Pay Balance: ₹1,234.50') == 1234.5
    assert balance_in('Use your ₹500.00 Amazon Pay balance') == 500
    assert balance_in('Pay with UPI') is None
    assert total_in('Items: ₹99\nOrder Total: ₹139.00') == 139


def test_payment_needs_balance_to_cover_total(monkeypatch):
    monkeypatch.setattr(config, 'MAX_ORDER_PRICE', 0)
    assert payment_ok(99, 500)[0]
    assert payment_ok(500, 500)[0]
    ok, why = payment_ok(501, 500)
    assert not ok and 'balance' in why
    assert not payment_ok(None, 500)[0]
    assert not payment_ok(99, None)[0]
    monkeypatch.setattr(config, 'MAX_ORDER_PRICE', 50)
    assert not payment_ok(99, 500)[0]


def test_price_still_a_deal(monkeypatch):
    monkeypatch.setattr(config, 'MIN_DISCOUNT', 90)
    assert price_ok(99, 9999, 99)[0]
    assert price_ok(120, None, 99)[0]          # within 25% + ₹10
    assert not price_ok(999, 9999, 99)[0]      # deal over
    assert price_ok(500, 9999, None)[0]        # % only: page MRP shows 95% off
    assert not price_ok(5000, 9999, None)[0]
    assert not price_ok(500, None, None)[0]
    assert not price_ok(None, 9999, 99)[0]


def test_cookie_file_formats(tmp_path):
    j = tmp_path / 'c.json'
    j.write_text(json.dumps([
        {'name': 'session-id', 'value': 'abc', 'domain': '.amazon.in', 'path': '/',
         'expirationDate': 1893456000.5, 'secure': True, 'httpOnly': False,
         'sameSite': 'no_restriction'},
        {'name': 'x', 'value': 'y', 'domain': '.google.com', 'path': '/'},
    ]))
    cs = read_cookie_file(str(j))
    assert cs == [{'name': 'session-id', 'value': 'abc', 'domain': '.amazon.in', 'path': '/',
                   'secure': True, 'httpOnly': False, 'expires': 1893456000, 'sameSite': 'None'}]

    t = tmp_path / 'c.txt'
    t.write_text('# Netscape HTTP Cookie File\n'
                 '.amazon.in\tTRUE\t/\tTRUE\t1893456000\tat-acbin\ttok\n'
                 '#HttpOnly_.amazon.in\tTRUE\t/\tTRUE\t0\tsess-at\tz\n'
                 '.example.com\tTRUE\t/\tFALSE\t0\ta\tb\n')
    cs = read_cookie_file(str(t))
    assert [c['name'] for c in cs] == ['at-acbin', 'sess-at']
    assert cs[1]['httpOnly'] and 'expires' not in cs[1]


def test_eligibility(monkeypatch):
    monkeypatch.setattr(config, 'AUTO_ORDER', True)
    monkeypatch.setattr(config, 'MIN_DISCOUNT', 90)
    good = analyse('₹99 MRP ₹9,999 https://amzn.to/x')
    assert autobuy.eligible(good, 'amazon:B0TEST0001') is None
    assert autobuy.eligible(good, 'amzn.to/x')                        # no ASIN
    glitch = analyse('PRICE ERROR https://amzn.to/x')
    assert autobuy.eligible(glitch, 'amazon:B0TEST0001')              # words alone never order
    fk = analyse('₹99 MRP ₹9,999 https://fkrt.it/x')
    assert autobuy.eligible(fk, 'flipkart:ABC')
    low = analyse('₹5,000 MRP ₹9,999 https://amzn.to/x')
    assert autobuy.eligible(low, 'amazon:B0TEST0001')
    monkeypatch.setattr(config, 'AUTO_ORDER', False)
    assert autobuy.eligible(good, 'amazon:B0TEST0001')


class FakeBuyer:
    def __init__(self, status='placed'):
        self.lock = asyncio.Lock()
        self.calls = []
        self.status = status

    async def buy(self, asin, price, dry_run=False):
        self.calls.append(asin)
        await asyncio.sleep(0.01)
        return Result(self.status, total=99, balance=500)


def test_orderer_never_orders_same_asin_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MAX_ORDERS_PER_DAY', 0)
    said = []
    o = autobuy.AutoOrderer(FakeBuyer(), Orders(str(tmp_path / 'o.db')), said.append)

    async def go():
        # Two channels post the same deal at the same moment.
        await asyncio.gather(o.order('B0TEST0001', 99), o.order('B0TEST0001', 99))
    asyncio.run(go())
    assert o.buyer.calls == ['B0TEST0001']
    assert any('ORDERED' in s for s in said)


def test_orderer_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MAX_ORDERS_PER_DAY', 2)
    said = []
    o = autobuy.AutoOrderer(FakeBuyer(), Orders(str(tmp_path / 'o.db')), said.append)

    async def go():
        for a in ('A000000001', 'A000000002', 'A000000003'):
            await o.order(a, 99)
    asyncio.run(go())
    assert o.buyer.calls == ['A000000001', 'A000000002']
    assert 'Daily limit' in said[-1]


def test_failed_attempt_can_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MAX_ORDERS_PER_DAY', 0)
    o = autobuy.AutoOrderer(FakeBuyer('failed'), Orders(str(tmp_path / 'o.db')))
    asyncio.run(o.order('B0TEST0001', 99))
    asyncio.run(o.order('B0TEST0001', 99))
    assert o.buyer.calls == ['B0TEST0001', 'B0TEST0001']


def test_report_text():
    t = autobuy.report_text('B0TEST0001', Result('placed', total=99, balance=500))
    assert 'ORDERED' in t and '₹99' in t and '₹500' in t and 'order-history' in t
    t = autobuy.report_text('B0TEST0001', Result('failed', 'balance <low>'))
    assert 'Not ordered' in t and '&lt;low&gt;' in t


def test_submit_skips_ineligible(monkeypatch):
    monkeypatch.setattr(config, 'AUTO_ORDER', True)
    o = autobuy.AutoOrderer(NS(), NS())
    assert o.submit(analyse('PRICE ERROR https://amzn.to/x'), 'amazon:B0TEST0001') is None
