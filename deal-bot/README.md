# Deal Bot: 90%+ loot alerts for Amazon and Flipkart

Watches Indian loot-deal Telegram channels through **your own Telegram
account** and pings you the moment a post shows an Amazon or Flipkart deal at
**90% off or more**, or a post that says "price error" / "glitch".

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

## Tests

```
pip install pytest telethon
python -m pytest deal-bot/tests
```
