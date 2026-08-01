# Deploying main + reverse bots to a DigitalOcean droplet

Replace `<IP>` with your droplet's address throughout. Run these from your
Windows machine (Git Bash) unless a step says "on the server".

---

## 0. Before you start — stop the local bots

They must not run in two places at once. Both would trade the same account,
place competing stop orders, and corrupt each other's books.

```bash
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
```

**The Looser bot must stay stopped.** It shares the USDT wallet with the main
bot. Even though they trade different symbols, both size positions from the
same balance, and a shared wallet across two machines has no coordination.

---

## 1. Prepare the server

```bash
scp "deploy/setup_server.sh" root@<IP>:/root/
ssh root@<IP> "bash /root/setup_server.sh"
```

Installs Python, creates a non-root `bots` user, sets the timezone to
Asia/Kolkata (the bots' session logic is IST-based), and enables a firewall
that allows **only SSH**.

---

## 2. Copy the bot code

```bash
cd "/c/Users/Ramu Baanothu/OneDrive/Documents/Paython utilities/Claude code"

scp solana-arbitrage-bot/trading/{bot.py,tui.py,indicators.py,journal_report.py,atlas.html} \
    root@<IP>:/home/bots/main/

scp binance-reverse-bot/{bot.py,tui.py,indicators.py,journal_report.py,atlas.html} \
    root@<IP>:/home/bots/reverse/
```

## 3. Copy the API keys separately

`config.py` is gitignored because it holds live Binance keys. It has to be
copied by hand — it is never in the repo and never passes through chat.

```bash
scp solana-arbitrage-bot/trading/config.py root@<IP>:/home/bots/main/
scp binance-reverse-bot/config.py         root@<IP>:/home/bots/reverse/
```

Then lock them down and hand ownership to the bot user:

```bash
ssh root@<IP> "chown -R bots:bots /home/bots/main /home/bots/reverse && chmod 600 /home/bots/*/config.py"
```

## 4. Copy trade history (optional but recommended)

Without these the bots start from a blank book and you lose the evidence base.

```bash
scp solana-arbitrage-bot/trading/trades_binance.json root@<IP>:/home/bots/main/
scp binance-reverse-bot/trades_reverse.json          root@<IP>:/home/bots/reverse/
ssh root@<IP> "chown bots:bots /home/bots/*/trades_*.json"
```

Do **not** copy `positions_*.json`. Let each bot reconcile its open positions
from the exchange on first boot — that is the authoritative source, and a stale
positions file would make it believe it holds something it does not.

---

## 5. Install the services

```bash
scp deploy/alphabot-*.service root@<IP>:/etc/systemd/system/
ssh root@<IP> "systemctl daemon-reload && systemctl enable --now alphabot-main alphabot-reverse"
```

`enable` means they also start on reboot. `Restart=always` means they come back
within 10 seconds of any crash — which is the entire reason for doing this.

---

## 6. Verify

```bash
ssh root@<IP> "systemctl status alphabot-main alphabot-reverse --no-pager | head -30"
ssh root@<IP> "journalctl -u alphabot-main -n 30 --no-pager"
```

Look for `LIVE MODE | Balance:` and a `[SCAN` line. Confirm the balances match
what you saw locally.

---

## 7. View the dashboards

The firewall deliberately does not expose ports 8765/8767. An open dashboard
would give anyone your account view and a restart button. Tunnel instead:

```bash
ssh -L 8765:localhost:8765 -L 8767:localhost:8767 root@<IP>
```

Leave that running, then open the local `atlas.html` files as usual — they
connect to `localhost`, which the tunnel forwards to the server.

---

## Everyday commands

```bash
ssh root@<IP> "systemctl restart alphabot-main"        # restart one
ssh root@<IP> "systemctl stop alphabot-reverse"        # stop one
ssh root@<IP> "journalctl -u alphabot-main -f"         # live log
ssh root@<IP> "systemctl status alphabot-*"            # both at a glance
```

## Updating a bot after a code change

```bash
scp solana-arbitrage-bot/trading/bot.py root@<IP>:/home/bots/main/
ssh root@<IP> "chown bots:bots /home/bots/main/bot.py && systemctl restart alphabot-main"
```

---

## Security notes

- Bots run as the unprivileged `bots` user, not root.
- `config.py` is `chmod 600` — readable only by its owner.
- Only port 22 is open. Dashboards are reachable solely through the tunnel.
- Use SSH keys, not passwords. A password login on a public IP gets
  brute-forced within hours.
- These are testnet keys today. If you ever switch to live keys, restrict them
  in the Binance console to this droplet's IP and disable withdrawals.
