"""Read a loot-deal post and work out whether it is worth an alert.

Channel posts are free text, so this is heuristics, not a grammar. It looks
for three things:

  * links to Amazon / Flipkart (including short links like amzn.to, fkrt.it)
  * rupee prices, and which one is the MRP
  * an explicit "95% off", or glitch words like "price error"

The discount is the larger of the stated % and the % worked out from the
prices, so a post that says "₹199 (MRP ₹9,999)" alerts even with no % in it.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

AMAZON_HOSTS = ('amazon.in', 'amzn.to', 'amzn.in', 'amzn.eu', 'a.co')
FLIPKART_HOSTS = ('flipkart.com', 'fkrt.it', 'fkrt.cc', 'fktr.in', 'fkrt.co')

URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.I)

# ₹499 | Rs. 1,299 | Rs 99/- | INR 4999.00 | @499
_NUM = r'(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?'
PRICE_RE = re.compile(r'(?:₹|rs\.?|inr|@)\s*' + _NUM, re.I)
# "MRP ₹4,999" / "M.R.P: 4999" / "worth ₹2000" / "was ₹999"
# The words other than MRP need a currency sign, so "was 5 min ago" is not a price.
MRP_RE = re.compile(r'(?:m\.?\s?r\.?\s?p\.?\s*[:\-]?\s*(?:₹|rs\.?|inr)?'
                    r'|(?:worth|was|original(?:ly)?)\s*[:\-]?\s*(?:₹|rs\.?|inr))\s*' + _NUM, re.I)
# "95% off", "flat 90 % OFF", "90% discount". "Up to 90% off" is sale-banner
# talk, not a price on a product, so it is ignored.
PCT_RE = re.compile(r'(up\s*to\s*|upto\s*)?(\d{1,3}(?:\.\d+)?)\s*%\s*(?:off|discount)', re.I)

GLITCH_RE = re.compile(r'price\s*(?:error|glitch|mistake|bug)|pricing\s*error|'
                       r'\bglitch\b|\bbug\s*deal\b|\bbug\s*price\b', re.I)


@dataclass
class Deal:
    platform: str                 # 'amazon' | 'flipkart'
    links: list
    discount: float               # 0..100, best estimate
    price: float = None           # deal price if found
    mrp: float = None
    glitch_words: bool = False


def _num(s):
    return float(s.replace(',', ''))


def platform_of(url):
    host = (urlparse(url).hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    for h in AMAZON_HOSTS:
        if host == h or host.endswith('.' + h):
            return 'amazon'
    for h in FLIPKART_HOSTS:
        if host == h or host.endswith('.' + h):
            return 'flipkart'
    return None


def product_key(url):
    """A stable id for the product behind a (full) link, for de-duplication.

    Amazon: the ASIN. Flipkart: the pid query param or the /p/itm... id.
    Anything else: host + path with the tracking query stripped.
    """
    p = urlparse(url)
    host = (p.hostname or '').lower()
    m = re.search(r'/(?:dp|gp/product|gp/aw/d|d)/([A-Z0-9]{10})(?:[/?]|$)', p.path, re.I)
    if m and 'amazon' in host:
        return 'amazon:' + m.group(1).upper()
    if 'flipkart' in host:
        pid = parse_qs(p.query).get('pid')
        if pid:
            return 'flipkart:' + pid[0].upper()
        m = re.search(r'/p/(itm[0-9a-z]+)', p.path, re.I)
        if m:
            return 'flipkart:' + m.group(1).lower()
    return host.removeprefix('www.') + p.path.rstrip('/')


def extract_links(text, extra_urls=()):
    seen, out = set(), []
    for u in list(URL_RE.findall(text or '')) + list(extra_urls):
        u = u.rstrip('.,;:!*')
        if u not in seen and platform_of(u):
            seen.add(u)
            out.append(u)
    return out


def analyse(text, extra_urls=()):
    """Return a Deal for an Amazon/Flipkart post, or None if it has no such link."""
    links = extract_links(text, extra_urls)
    if not links:
        return None
    platform = platform_of(links[0])

    # Prices are read from the text with links removed, so digits inside a
    # URL (…/dp/B0C99…, ?pid=…) are never taken for a price.
    body = URL_RE.sub(' ', text or '')

    stated = 0.0
    for upto, pct in PCT_RE.findall(body):
        if not upto:
            stated = max(stated, min(float(pct), 100.0))

    mrps = [_num(m) for m in MRP_RE.findall(body)]
    mrp_spans = [m.span() for m in MRP_RE.finditer(body)]
    prices = []
    for m in PRICE_RE.finditer(body):
        if any(a <= m.start() < b for a, b in mrp_spans):
            continue
        prices.append(_num(m.group(1)))

    price = min(prices) if prices else None
    ref = max(mrps) if mrps else (max(prices) if len(prices) >= 2 else None)
    computed = 0.0
    if price is not None and ref and ref > price:
        computed = round((1 - price / ref) * 100, 1)

    return Deal(platform=platform, links=links,
                discount=max(stated, computed),
                price=price, mrp=ref if ref and price and ref > price else None,
                glitch_words=bool(GLITCH_RE.search(body)))
