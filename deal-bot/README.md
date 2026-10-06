# Deal Bot: 90%+ loot alerts and auto-orders for Amazon and Flipkart

Watches Indian loot-deal Telegram channels through **your own Telegram
account** and pings you the moment a post shows an Amazon or Flipkart deal at
**90% off or more**, or a post that says "price error" / "glitch".

For Amazon deals it can also **place the order automatically, paid only
from your Amazon Pay balance**. You review each order in the Amazon app
afterwards and cancel the ones you don't want. See
[Auto-ordering](#auto-ordering-on-amazon) below.

No Amazon or Flipkart API and no scraping: the channels have already found the
deal, and the bot's job is to get it to you within seconds, filtered and
de-duplicated. The same deal posted by ten channels gives you one alert.

```
🚨 98% OFF · Amazon · ⚠️ price error?
₹299  (MRP ₹19,990)

PRICE ERROR Sony WH-1000XM5 ...

from Loot Deals India · 3s after posting
[ 🛒 Open on Amazon ]
```

## How it decides

- **Links:** it needs an amazon.in / amzn.to / flipkart.com / fkrt.it link.
  Links hidden behind "Buy now" text or under the post as buttons count too.
- **Discount:** the larger of a stated "95% off" and the % worked out from the
  prices (`₹199` vs `MRP ₹4,490`). "Up to 90% off" sale banners are ignored.
- **Glitch words:** "price error", "glitch" or "bug price" alert even with no
  price in the post (turn off with `ALERT_ON_GLITCH_WORDS=false`).
- **Edits:** edited posts are re-checked, because channels often post first
  and add the price a few seconds later.

To try the filter on any post without Telegram:

```
python -m dealbot test "Boat earbuds ₹99 MRP ₹2,999 https://amzn.to/x"
```

## One-time setup (about 10 minutes)

1. **Telegram API ID:** log in at <https://my.telegram.org>, open *API
   development tools* and create an app (any name). Note the `api_id` and
   `api_hash`.
2. **Alert bot:** in Telegram, message **@BotFather**, send `/newbot`, and
   note the token. Send your new bot a "hi". Then get your chat id from
   **@userinfobot**.
3. **Join the loot channels** you trust, from the same Telegram account.

## On the droplet

Assumes the droplet was prepared with `deploy/setup_server.sh` (it has a
`bots` user and `/home/bots/venv`).

```bash
# as root
install -d -o bots -g bots /home/bots/dealbot
# copy the deal-bot folder's contents into /home/bots/dealbot, then:
sudo -u bots /home/bots/venv/bin/pip install -r /home/bots/dealbot/requirements.txt
cd /home/bots/dealbot
sudo -u bots cp .env.example .env && sudo -u bots nano .env     # fill in the values

# first login: asks for your phone number and the code Telegram sends you
sudo -u bots /home/bots/venv/bin/python -m dealbot login
# optional: list your channels, to put some of them in CHANNELS=
sudo -u bots /home/bots/venv/bin/python -m dealbot channels

cp dealbot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now dealbot
journalctl -u dealbot -f          # watch it work
```

The login creates `dealbot.session`. **Treat it like a password**: anyone
with this file can read your Telegram account. It is git-ignored; keep it
readable only by the `bots` user (`chmod 600 dealbot.session`).

## Settings (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `MIN_DISCOUNT` | `90` | alert at or above this % off |
| `CHANNELS` | *(all joined)* | comma-separated `@usernames` or ids |
| `PLATFORMS` | `amazon,flipkart` | which stores to alert on |
| `ALERT_ON_GLITCH_WORDS` | `true` | alert on "price error" posts with no % |
| `DEDUPE_HOURS` | `6` | same product at the same price alerts once per window |

A deeper drop on the same product (for example ₹99, then ₹9) alerts again.

## Auto-ordering on Amazon

When an alert is an Amazon deal at `MIN_DISCOUNT` or more, a headless
browser on the droplet orders **one unit** with your Amazon Pay balance and
sends you a screenshot:

```
✅ ORDERED on Amazon
https://www.amazon.in/dp/B0XXXXXXXX
Paid ₹139 from Amazon Pay balance (balance was ₹500).
Review and cancel if not wanted: https://www.amazon.in/gp/your-account/order-history
```

**It only orders when all of these hold. Otherwise you get "❌ Not
ordered" with the reason:**

- The post shows a price-based saving of at least `MIN_DISCOUNT`. A post
  that only says "price error" alerts you but never orders.
- The product page still shows the deal: within 25% (+₹10) of the posted
  price, or at least `MIN_DISCOUNT` off its MRP.
- **Your Amazon Pay balance covers the whole order total**, delivery
  included. If it doesn't, nothing is ordered, so your card and UPI are
  never used. The balance you load is your spending limit.
- The same product has never been ordered before.
- The optional caps `MAX_ORDER_PRICE` / `MAX_ORDERS_PER_DAY` aren't hit.
- Amazon shows no CAPTCHA or OTP page. The bot never answers one; it stops
  and tells you.

Orders run one at a time, so two deals can't race for the same balance.
`python -m dealbot orders` lists every attempt.

### Logging the bot in: cookie import

The bot never sees your password or OTP. It uses the login cookies from your
own browser:

1. On your computer, log in to **amazon.in** in Chrome or Edge.
2. Install a cookie-export extension (for example "Cookie-Editor"). On
   amazon.in, export the cookies as **JSON** (or Netscape `cookies.txt`).
3. Copy the file to the droplet and load it:
   ```bash
   scp amazon-cookies.json root@<droplet-ip>:/home/bots/dealbot/
   sudo -u bots /home/bots/venv/bin/python -m dealbot amazon-cookies amazon-cookies.json
   rm /home/bots/dealbot/amazon-cookies.json
   ```
   It prints `Logged in: "Hello, <name>"`.
4. When Amazon logs the bot out, usually after some weeks, you get
   "🔴 not logged in to Amazon" and failed orders. Repeat steps 1–3.

The cookies end up in `browser-profile/`. **Treat that folder like your
Amazon password**: anyone with it can use your account. It is git-ignored.

### Before the first real deal

1. **Pay one order yourself with Amazon Pay balance.** Amazon's quick "Buy
   Now" popup uses your last payment method, and the bot needs it to be
   the balance.
2. Do a dry run on any product. It goes up to "Place your order",
   screenshots and stops:
   ```bash
   sudo -u bots MIN_DISCOUNT=0 /home/bots/venv/bin/python -m dealbot amazon-test B0XXXXXXXX
   ```
   (`MIN_DISCOUNT=0` because a normal product isn't 90% off.) The
   screenshot is in `screenshots/`. If it says "could not find…", Amazon's
   page differs from what the bot expects; send the screenshot to get the
   selectors fixed.

### Installing the browser on the droplet

```bash
sudo -u bots /home/bots/venv/bin/pip install -r /home/bots/dealbot/requirements.txt
/home/bots/venv/bin/playwright install-deps chromium          # as root: system libraries
sudo -u bots /home/bots/venv/bin/playwright install chromium  # the browser itself
```

Chromium needs about 300–400 MB of RAM. On a 1 GB droplet, add swap if the
trading bots run there too.

### Things to know

- **Amazon's terms forbid automated ordering.** Amazon can cancel these
  orders or close the account, and the balance can be held with it. Price
  error orders are also often cancelled by Amazon itself.
- The checkout steps were tested against a mock Amazon page, not the live
  site. Amazon changes its pages, so expect the first live runs to need a
  selector fix. Every failure comes with a screenshot.

## Tests

```
pip install pytest telethon playwright
python -m pytest deal-bot/tests
```
