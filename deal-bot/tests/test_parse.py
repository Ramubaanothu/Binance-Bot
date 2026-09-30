import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('MIN_DISCOUNT', '90')

from dealbot.parse import analyse, product_key, platform_of  # noqa: E402
from dealbot.main import wanted, hidden_urls  # noqa: E402
from dealbot.store import Seen  # noqa: E402


def test_mrp_and_price_computes_discount():
    d = analyse('LOOT 🔥 boAt Airdopes 141\nDeal Price: ₹199\nMRP: ₹4,490\nhttps://amzn.to/3abcXYZ')
    assert d.platform == 'amazon'
    assert d.price == 199 and d.mrp == 4490
    assert d.discount == 95.6
    assert wanted(d)


def test_stated_percent():
    d = analyse('Flat 92% OFF on Philips trimmer https://fkrt.it/abc123')
    assert d.platform == 'flipkart'
    assert d.discount == 92
    assert wanted(d)


def test_up_to_banner_ignored():
    d = analyse('Big Billion Days: Up to 90% off on fashion! https://www.flipkart.com/fashion')
    assert d.discount == 0
    assert not wanted(d)


def test_below_threshold_no_alert():
    d = analyse('Redmi 13 at Rs. 8,999 (MRP Rs 14,999) https://www.amazon.in/dp/B0D1234567')
    assert round(d.discount) == 40
    assert not wanted(d)


def test_two_prices_without_mrp_label():
    d = analyse('Samsung 1TB SSD ₹9,999 ➡️ ₹99 only!! https://amzn.to/xyz')
    assert d.price == 99 and d.discount > 98
    assert wanted(d)


def test_glitch_words_alert_without_prices():
    d = analyse('PRICE ERROR!! order fast https://amzn.to/xyz')
    assert d.glitch_words and wanted(d)


def test_digits_in_url_not_prices():
    d = analyse('Check this https://www.amazon.in/dp/B0C9999999?tag=x-21 @499')
    assert d.price == 499 and d.mrp is None and d.discount == 0


def test_was_without_currency_not_mrp():
    d = analyse('Posted, was 5 min ago, now ₹299 https://amzn.to/q')
    assert d.mrp is None


def test_no_supported_link():
    assert analyse('95% off at Myntra https://myntra.com/x') is None
    assert not wanted(None)


def test_hidden_link_used():
    d = analyse('₹9 only, MRP ₹999 — tap Buy Now', ['https://amzn.to/hidden'])
    assert d.links == ['https://amzn.to/hidden'] and wanted(d)


def test_platform_hosts():
    assert platform_of('https://www.amazon.in/x') == 'amazon'
    assert platform_of('https://dl.flipkart.com/s/x') == 'flipkart'
    assert platform_of('https://notamazon.in/x') is None


def test_product_keys():
    assert product_key('https://www.amazon.in/boAt-Airdopes/dp/B0BZ8TT1QX/ref=sr_1?tag=a-21') == 'amazon:B0BZ8TT1QX'
    assert product_key('https://www.amazon.in/gp/product/b0bz8tt1qx') == 'amazon:B0BZ8TT1QX'
    assert product_key('https://www.flipkart.com/x/p/itm123abc?pid=MOBGT5F2&affid=z') == 'flipkart:MOBGT5F2'
    assert product_key('https://www.flipkart.com/x/p/itm123abc?affid=z') == 'flipkart:itm123abc'


def test_hidden_urls_from_entities_and_buttons():
    from types import SimpleNamespace as NS
    # Old layers put .url on the button; newer ones on button.type.
    msg = NS(entities=[NS(url='https://amzn.to/a'), NS(offset=0)],
             reply_markup=NS(rows=[NS(buttons=[
                 NS(text='Grab', url='https://fkrt.it/b'),
                 NS(text='Also', type=NS(url='https://amzn.to/c')),
                 NS(text='Callback', type=NS(data=b'x'))])]))
    assert hidden_urls(msg) == ['https://amzn.to/a', 'https://fkrt.it/b', 'https://amzn.to/c']


def test_seen_dedupes(tmp_path):
    s = Seen(str(tmp_path / 's.db'), hours=1)
    assert s.check_and_add('amazon:X@99')
    assert not s.check_and_add('amazon:X@99')
    assert s.check_and_add('amazon:X@49')  # deeper drop alerts again
