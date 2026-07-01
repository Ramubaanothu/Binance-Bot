# Binance Bot

Automated Binance Futures trading bot with a full-screen terminal dashboard. Scans 500+ USDT perpetual futures, applies 22 technical indicators across 3 timeframes, and manages positions with a triple take-profit + trailing stop system.

---

## Features

- Scans all USDT perpetual futures (500+ pairs) every 20 seconds
- Multi-timeframe analysis: 5m + 15m + 1h with 22 indicators
- LONG and SHORT with dynamic regime detection
- Triple TP system: TP1 → BE-lock → TP2 → TP3 trail
- BTC macro filters: 1H / 4H / Daily trend gates
- Fear & Greed Index integration (relaxes gates at extremes)
- Open Interest + Funding Rate sentiment overlay
- Full-screen terminal dashboard (TUI) with live P&L, scanner feed, balance chart, and ring gauges
- WebSocket broadcast — TUI and web dashboard subscribe independently
- Positions survive bot restarts (JSON persistence + exchange reconciliation)
- Orphan position safety: positions losing >3% at startup are closed immediately
- Consecutive loss circuit breaker (pauses bot after N straight losses)

---

## Quick Start

**1. Clone and install:**
```
git clone https://github.com/YOUR_USERNAME/Binance-Bot.git
cd Binance-Bot
pip install -r trading/requirements.txt
```

**2. Add your API keys:**
```
copy trading\config.example.py trading\config.py
```
Edit `trading/config.py` and paste your Binance Futures API key and secret.

- Testnet keys: https://testnet.binancefuture.com → API Management
- Live keys: https://www.binance.com → Account → API Management

**3. Launch:**
```
START.bat
```
This starts the bot engine in the background and opens the terminal dashboard.

---

## File Structure

```
Binance-Bot/
├── START.bat               — one-click launcher (bot + dashboard)
├── START_POLY.bat          — launcher for Polygon mode
├── README.md
├── .gitignore
└── trading/
    ├── bot.py              — main engine (scanner, signals, position management)
    ├── tui.py              — full-screen terminal dashboard
    ├── config.py           — your API keys and parameters (excluded from git)
    ├── config.example.py   — template — copy to config.py
    ├── indicators.py       — TA engine (22 indicators, 14 patterns)
    ├── poly_bot.py         — Polygon futures variant
    ├── dashboard.html      — web dashboard (open in browser)
    ├── terminal.html       — web terminal view
    ├── poly_terminal.html  — Polygon web terminal
    ├── close_shorts.py     — emergency position closer utility
    └── requirements.txt    — Python dependencies
```

---

## Dashboard Controls

| Key | Action |
|-----|--------|
| Q | Quit dashboard (bot keeps running) |
| R | Restart bot engine |
| B | Open web dashboard in browser |
| P | Pause / resume trading |

---

## Configuration

Key settings in `trading/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| POSITION_SIZE_PCT | 0.02 | 2% of balance per trade |
| MAX_POSITIONS | 5 | Maximum open positions |
| MIN_CONFIDENCE | 54 | Minimum signal score (0-100) |
| SCAN_INTERVAL_SEC | 20 | Seconds between full scans |
| PAPER_MODE | False | Simulate orders without real API calls |
| MAX_DAILY_LOSS_PCT | 8.0 | Daily loss circuit breaker |

---

## Requirements

- Python 3.10+
- Binance Futures account (testnet or live)
- Windows tested — Linux/Mac compatible

Install dependencies:
```
pip install -r trading/requirements.txt
```

---

## Disclaimer

This bot is for educational and research purposes. Futures trading carries significant risk of loss. Always test on testnet before using real funds.
