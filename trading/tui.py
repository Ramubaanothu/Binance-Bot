"""
AlphaBot v5.0 — Beautiful CMD Terminal Dashboard
═══════════════════════════════════════════════════
Full-screen rich TUI that mirrors the web dashboard.
Connects to bot.py via WebSocket on ws://localhost:8765

Usage:
    python tui.py              — Binance bot dashboard
    python tui.py --poly       — Poly bot dashboard (port 8766)

Controls:
    Q / Ctrl-C  — Quit
    R           — Restart bot
    B           — Open browser dashboard
    P           — Pause / resume bot
"""

import asyncio
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import webbrowser

# ─── Windows UTF-8 + ANSI fix (must run before rich imports) ─────────────────
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass
    # Enable ANSI/VT100 in Windows 10+ console
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        for h in (-10, -11):   # STDIN, STDOUT
            hnd  = k32.GetStdHandle(h)
            mode = ctypes.c_ulong(0)
            k32.GetConsoleMode(hnd, ctypes.byref(mode))
            k32.SetConsoleMode(hnd, mode.value | 0x0004 | 0x0008)
    except Exception:
        pass
from collections import deque
from datetime import datetime, timedelta

try:
    import msvcrt
    _WINDOWS = True
except ImportError:
    _WINDOWS = False
    import tty, termios, select

import websockets
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich import box

# ─── Config ───────────────────────────────────────────────────────────────────
POLY_MODE   = '--poly' in sys.argv
WS_URL      = 'ws://localhost:8766' if POLY_MODE else 'ws://localhost:8765'
BOT_SCRIPT  = 'poly_bot.py'         if POLY_MODE else 'bot.py'
BROWSER_URL = 'http://localhost:8080/poly_terminal.html' if POLY_MODE \
              else pathlib.Path(__file__).with_name('atlas.html').as_uri()
REFRESH_HZ  = 6       # screen updates per second
BOT_LABEL   = 'PolyAlphaBot' if POLY_MODE else 'AlphaBot'

# ─── Spinner frames ───────────────────────────────────────────────────────────
SPIN_A = ['⣷', '⣯', '⣟', '⡿', '⢿', '⣻', '⣽', '⣾']
SPIN_B = ['◐', '◓', '◑', '◒']
SPIN_C = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█', '▇', '▆', '▅', '▄', '▃', '▂']

# RSI bar gradient (20 chars)
def rsi_bar(rsi: float, width: int = 20) -> Text:
    filled = max(0, min(width, int(rsi / 100 * width)))
    empty  = width - filled
    t = Text()
    if   rsi <= 22: col = 'bright_green'
    elif rsi <= 35: col = 'green'
    elif rsi <= 45: col = 'cyan'
    elif rsi >= 78: col = 'bright_red'
    elif rsi >= 65: col = 'red'
    elif rsi >= 55: col = 'yellow'
    else:           col = 'white'
    t.append('█' * filled, style=col)
    t.append('░' * empty,  style='bright_black')
    return t

# ─── Shared state (thread-safe) ───────────────────────────────────────────────
_lock             = threading.Lock()
_S                = {}                    # latest WS state snapshot
_log_feed         = deque(maxlen=120)     # raw log entries for scanner panel
_connected        = False
_quit_evt         = threading.Event()
_action           = None                  # pending keyboard action
_balance_history  = deque(maxlen=20000)   # (unix_ts, balance) — FULL wallet history
_bal_last_ts      = 0.0                   # throttle: record at most every 30s
_bal_save_ts      = 0.0                   # throttle disk writes
_BAL_FILE         = pathlib.Path(__file__).parent / 'balance_history.json'

def _load_balance_history():
    """Load the full wallet-balance history from disk so the chart spans from
    the first recorded point (wallet start) through today, across restarts."""
    try:
        if _BAL_FILE.exists():
            data = json.loads(_BAL_FILE.read_text(encoding='utf-8'))
            for ts, bal in data[-20000:]:
                _balance_history.append((float(ts), float(bal)))
    except Exception:
        pass

def _save_balance_history():
    try:
        _BAL_FILE.write_text(json.dumps(list(_balance_history)), encoding='utf-8')
    except Exception:
        pass

_load_balance_history()

# ─── WebSocket listener ───────────────────────────────────────────────────────
def _ws_thread():
    global _connected
    # Resolve to IPv4 explicitly — avoids localhost→::1 on Windows
    ws_url = WS_URL.replace('localhost', '127.0.0.1')

    async def _run():
        global _connected
        while not _quit_evt.is_set():
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=None,   # no ping timeout — bot may be slow during scans
                    open_timeout=10,
                ) as ws:
                    _connected = True
                    async for raw in ws:
                        try:
                            d = json.loads(raw)
                            with _lock:
                                _S.clear()
                                _S.update(d)
                                for e in d.get('log', []):
                                    if not _log_feed or _log_feed[0].get('msg') != e.get('msg'):
                                        _log_feed.appendleft(e)
                        except Exception:
                            pass
            except Exception as _exc:
                _connected = False
                # Store last error for header display
                with _lock:
                    _S['_ws_err'] = str(_exc)[:60]
                await asyncio.sleep(3)
    asyncio.run(_run())

# ─── Keyboard listener ────────────────────────────────────────────────────────
def _kb_thread():
    global _action
    if _WINDOWS:
        while not _quit_evt.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                try:
                    _action = ch.decode('utf-8').lower()
                except Exception:
                    pass
            time.sleep(0.04)
    else:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while not _quit_evt.is_set():
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    _action = sys.stdin.read(1).lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ─── Bot control ──────────────────────────────────────────────────────────────
def restart_bot():
    """Kill existing bot process and restart it."""
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'cmdline']):
            cmd = ' '.join(proc.info.get('cmdline') or [])
            if BOT_SCRIPT in cmd and 'tui.py' not in cmd:
                proc.kill()
        time.sleep(2)
    except ImportError:
        # fallback on Windows
        os.system(f'taskkill /F /FI "WINDOWTITLE eq {BOT_SCRIPT}" >nul 2>&1')

    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), BOT_SCRIPT)
    flags = subprocess.CREATE_NO_WINDOW if _WINDOWS else 0
    subprocess.Popen(
        [sys.executable, bot_path],
        cwd=os.path.dirname(bot_path),
        stdout=open(os.path.join(os.path.dirname(bot_path), 'bot.log'), 'a', encoding='utf-8'),
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )

# ─── ATLAS design palette (matches AlphaBot Dashboard v4 design) ──────────────
TEAL   = '#00ffcc'
PINK   = '#ff0080'
VIOLET = '#a366ff'
AMBER  = '#f59e0b'
CYAN2  = '#06b6d4'
TXT    = '#c0c0e8'
FAINT  = '#5a5a8a'

def _pcol(v: float) -> str:
    return TEAL if v >= 0 else PINK

_SPARK_CH = '▁▂▃▄▅▆▇█'
_pos_spark: dict = {}   # symbol → [pnl_pct history] for position sparklines

def _spark_line(hist: list, width: int = 12) -> str:
    if not hist: return '─' * width
    h = hist[-width:]
    lo, hi = min(h), max(h)
    rng = (hi - lo) or 1.0
    s = ''.join(_SPARK_CH[int((v - lo) / rng * (len(_SPARK_CH) - 1))] for v in h)
    return s.rjust(width, '─')

# ─── Helpers ──────────────────────────────────────────────────────────────────
def _pnl_bar(pct: float, width: int = 14) -> Text:
    """Compact visual bar: green for profit, red for loss."""
    t = Text()
    clamped = max(-100, min(100, pct))
    filled  = max(0, min(width, int(abs(clamped) / 100 * width)))
    empty   = width - filled
    col     = 'bright_green' if pct >= 0 else 'bright_red'
    t.append('█' * filled, style=col)
    t.append('░' * empty,  style='bright_black')
    return t

def _fmt_price(v: float) -> str:
    if v == 0:    return '—'
    if v >= 1000: return f'{v:,.1f}'
    if v >= 10:   return f'{v:.2f}'
    if v >= 1:    return f'{v:.3f}'
    if v >= 0.01: return f'{v:.4f}'
    return f'{v:.6f}'

# ─── Panel: Header ────────────────────────────────────────────────────────────
def _header(S: dict) -> Panel:
    bal       = S.get('balance', 0)
    dpnl      = S.get('daily_pnl', 0)
    tpnl      = S.get('total_pnl', 0)
    rsi       = float(S.get('btc_rsi', (S.get('market_intel') or {}).get('btc_rsi', 50) or 50))
    trend     = str(S.get('btc_trend', '—')).upper()
    trend4h   = str(S.get('btc_4h_trend', '—')).upper()
    daily_bull= S.get('btc_daily_bull', True)
    btc5m     = float(S.get('btc_5m_mom', 0))
    fng       = int(S.get('fear_greed', 50))
    scan      = S.get('scan_count', 0)
    sess      = S.get('session', '—')
    paused    = S.get('paused', False)
    paper     = S.get('paper_mode', False)
    mi        = S.get('market_intel') or {}
    wins      = S.get('wins', 0)
    losses    = S.get('losses', 0)
    tot_t     = wins + losses
    wr        = wins / tot_t * 100 if tot_t else 0
    pos_n     = len(S.get('positions') or [])

    # Colors
    conn_col   = 'bright_green'  if _connected  else 'bright_red'
    conn_lbl   = '● LIVE'        if _connected  else '● OFFLINE'
    mode_col   = 'yellow'        if paper        else 'bright_green'
    mode_lbl   = 'PAPER'         if paper        else 'LIVE'
    dpnl_col   = 'bright_green'  if dpnl >= 0   else 'bright_red'
    tpnl_col   = 'bright_green'  if tpnl >= 0   else 'bright_red'
    wr_col     = 'bright_green'  if wr >= 55    else ('yellow' if wr >= 40 else 'bright_red')
    trend_col  = {'BEAR':'bright_red','BULL':'bright_green'}.get(trend,  'white')
    t4h_col    = {'BEAR':'bright_red','BULL':'bright_green'}.get(trend4h,'white')
    daily_col  = 'bright_green'  if daily_bull  else 'bright_red'
    mom_col    = 'bright_green'  if btc5m > 0.15 else ('bright_red' if btc5m < -0.15 else 'dim white')
    mood       = str(mi.get('market_mood', '—'))
    mood_col   = 'bright_green'  if 'BULL' in mood else ('bright_red' if 'BEAR' in mood else 'yellow')

    if rsi <= 22:   rsi_col, rsi_lbl = 'bright_green', 'WHALE BUY'
    elif rsi >= 78: rsi_col, rsi_lbl = 'bright_red',   'DIST.'
    elif rsi <= 35: rsi_col, rsi_lbl = 'green',         'OVERSOLD'
    elif rsi >= 65: rsi_col, rsi_lbl = 'yellow',        'OVERBOUGHT'
    else:           rsi_col, rsi_lbl = 'dim white',     'NEUTRAL'

    if fng <= 20:   fng_col, fng_lbl = 'bright_green', 'EXTREME FEAR  BUY'
    elif fng <= 40: fng_col, fng_lbl = 'green',         'FEAR'
    elif fng >= 80: fng_col, fng_lbl = 'bright_red',    'EXTREME GREED  FADE'
    elif fng >= 60: fng_col, fng_lbl = 'yellow',         'GREED'
    else:           fng_col, fng_lbl = 'dim white',      'NEUTRAL'

    _now_utc = datetime.utcnow()
    ts = (_now_utc + timedelta(hours=5, minutes=30)).strftime('%H:%M:%S IST')
    start_bal = S.get('start_bal', 0) or 0
    sess_pct  = ((bal / start_bal) - 1) * 100 if start_bal else 0.0

    # ── Row 1: ATLAS status bar ──────────────────────────────────────────────
    r1 = Text(justify='center')
    r1.append('● ', style=f'bold {TEAL}' if _connected else f'bold {PINK}')
    r1.append('A T L A S', style=f'bold {TEAL}')
    r1.append(' v5.0', style=FAINT)
    if paused:
        r1.append('  ⏸ PAUSED', style=f'bold {AMBER}')
    r1.append('  │  ', style=FAINT)
    r1.append('BAL ', style=FAINT)
    r1.append(f'${bal:,.2f}', style='bold #e0e0ff')
    r1.append('   DAY ', style=FAINT)
    r1.append(f'{dpnl:+,.2f}', style=f'bold {_pcol(dpnl)}')
    r1.append('   TOTAL ', style=FAINT)
    r1.append(f'{tpnl:+,.2f}', style=f'bold {_pcol(tpnl)}')
    r1.append('   SESSION ', style=FAINT)
    r1.append(f'{sess_pct:+.3f}%', style=f'bold {_pcol(sess_pct)}')
    r1.append('  │  ', style=FAINT)
    r1.append('WR ', style=FAINT)
    r1.append(f'{wr:.0f}%', style=f'bold {AMBER}')
    r1.append(f'  {wins}W/{losses}L', style=FAINT)
    r1.append('  POS ', style=FAINT)
    r1.append(str(pos_n), style=f'bold {TEAL}')
    r1.append('  SCAN ', style=FAINT)
    r1.append(f'#{scan}', style=f'bold {VIOLET}')
    r1.append('  │  ', style=FAINT)
    r1.append(f'{str(sess).upper()} SESSION', style=CYAN2)
    r1.append(f'  {ts}', style=f'bold {TEAL}')
    if paper:
        r1.append('  PAPER', style=f'bold {AMBER}')

    # ── Row 2: BTC multi-timeframe + RSI ────────────────────────────────────
    tcol = lambda tr: TEAL if tr == 'BULL' else (PINK if tr == 'BEAR' else AMBER)
    r2 = Text(justify='center')
    r2.append('1H: ', style=FAINT)
    r2.append(trend, style=f'bold {tcol(trend)}')
    r2.append('   4H: ', style=FAINT)
    r2.append(trend4h, style=f'bold {tcol(trend4h)}')
    r2.append('   DAILY: ', style=FAINT)
    r2.append('BULL' if daily_bull else 'BEAR', style=f'bold {TEAL if daily_bull else PINK}')
    r2.append('   BTC 10m: ', style=FAINT)
    r2.append(f'{btc5m:+.2f}%', style=f'bold {_pcol(btc5m)}')
    r2.append('   RSI ', style=FAINT)
    r2.append_text(rsi_bar(rsi, 16))
    r2.append(f' {rsi:.0f} {rsi_lbl}', style=f'bold {rsi_col}')

    # ── Row 3: Fear & Greed + breadth ────────────────────────────────────────
    r3 = Text(justify='center')
    r3.append('F&G ', style=FAINT)
    fng_filled = max(0, min(20, int(fng / 5)))
    r3.append('█' * fng_filled, style=AMBER)
    r3.append('░' * (20 - fng_filled), style='grey15')
    r3.append(f' {fng} {fng_lbl}', style=f'bold {fng_col}')
    up_n = mi.get('up_count', 0)
    dn_n = mi.get('down_count', 0)
    btc_chg = mi.get('btc_change', 0) or 0
    r3.append('   MARKET ', style=FAINT)
    r3.append(mood, style=f'bold {mood_col}')
    r3.append(f'  {up_n}', style=TEAL)
    r3.append('▲ ', style=TEAL)
    r3.append(f'{dn_n}', style=PINK)
    r3.append('▼', style=PINK)
    r3.append('   BTC 24h ', style=FAINT)
    r3.append(f'{btc_chg:+.2f}%', style=f'bold {_pcol(btc_chg)}')

    body = Text()
    body.append_text(r1)
    body.append('\n')
    body.append_text(r2)
    body.append('\n')
    body.append_text(r3)

    return Panel(body, style=FAINT, border_style='#123a33', height=5, padding=(0, 1))

# ─── Panel: Positions ─────────────────────────────────────────────────────────
_PHASE_NAMES = {
    'open':      ('OPEN',  'dim white'),
    'be-locked': ('BE-LK', 'bold yellow'),
    'tp1-trail': ('TP1',   'bold cyan'),
    'tp2-trail': ('TP2',   'bold bright_cyan'),
    'tp3-trail': ('TP3',   'bold bright_green'),
    'imported':  ('IMP',   'dim'),
}

# ─── Ring gauge helper (3 lines, clockwise fill) ──────────────────────────────
def _ring3(pct: float, val_str: str, label: str, col: str, w: int = 7) -> Text:
    """
    3-line donut ring. Clockwise fill:
      top arc  fills left→right (0–50%)
      bottom arc fills right→left (50–100%)

    At 72%, w=7  →  14 total segments, 10 filled:
      ╭███████╮
      │72% WR │
      ╰░░░████╯
    """
    total  = w * 2
    filled = round(min(100.0, max(0.0, pct)) / 100.0 * total)
    t_fill = min(w, filled)
    b_fill = max(0, filled - w)
    t = Text()
    # top arc
    t.append('╭', style='dim')
    t.append('█' * t_fill,       style=col)
    t.append('░' * (w - t_fill), style='bright_black')
    t.append('╮\n', style='dim')
    # center value + label
    inner = f'{val_str} {label}'
    t.append('│', style='dim')
    t.append(f'{inner[:w]:^{w}}', style=f'bold {col}')
    t.append('│\n', style='dim')
    # bottom arc (right→left completion)
    t.append('╰', style='dim')
    t.append('░' * (w - b_fill), style='bright_black')
    t.append('█' * b_fill,       style=col)
    t.append('╯\n', style='dim')
    return t

# ─── Panel: Gauges (ATLAS horizontal bars) ────────────────────────────────────
def _gauges(S: dict) -> Panel:
    wins   = S.get('wins', 0)
    losses = S.get('losses', 0)
    tot    = wins + losses
    wr     = wins / tot * 100 if tot else 0
    dd     = abs(float(S.get('max_dd', 0) or 0))
    fng    = int(S.get('fear_greed', 50))
    rsi    = float(S.get('btc_rsi', 50) or 50)

    def bar_row(label: str, pct: float, val: str, color: str, width: int = 16) -> Text:
        t = Text()
        filled = max(0, min(width, round(pct / 100 * width)))
        t.append(f' {label:<9}', style=FAINT)
        t.append('━' * filled, style=color)
        t.append('━' * (width - filled), style='grey15')
        t.append(f' {val:>5}\n', style=f'bold {color}')
        return t

    body = Text()
    body.append_text(bar_row('WIN RATE', wr,               f'{wr:.0f}%',  TEAL))
    body.append_text(bar_row('F&G',      fng,              str(fng),      AMBER))
    body.append_text(bar_row('DRAWDOWN', min(100, dd * 3), f'{dd:.1f}%',  PINK))
    body.append_text(bar_row('BTC RSI',  rsi,              f'{rsi:.0f}',  VIOLET))

    return Panel(body, title=f'[bold {TXT}]GAUGES[/]',
                 border_style='#123a33', expand=True, padding=(0, 1))


def _positions(S: dict) -> Panel:
    positions = S.get('positions') or []

    tbl = Table(
        box=box.SIMPLE_HEAD, expand=True, padding=(0, 0),
        header_style='bold dim', show_edge=False, show_footer=False,
    )
    # Tight columns — no Lev column, combined Entry→Now arrow
    tbl.add_column('Symbol',  no_wrap=True, min_width=10, max_width=13)
    tbl.add_column('Dir',     width=5,  no_wrap=True)
    tbl.add_column('Lev',     width=4,  justify='right', no_wrap=True)
    tbl.add_column('Entry',   width=9,  justify='right', no_wrap=True)
    tbl.add_column('Now',     width=9,  justify='right', no_wrap=True)
    tbl.add_column('P&L %',  width=8,  justify='right', no_wrap=True)
    tbl.add_column('Trend',   width=11, no_wrap=True)
    tbl.add_column('P&L $',  width=8,  justify='right', no_wrap=True)
    tbl.add_column('Value',   width=9,  justify='right', no_wrap=True)   # total position $
    tbl.add_column('Margin',  width=8,  justify='right', no_wrap=True)   # invested $
    tbl.add_column('Ph',      width=5,  no_wrap=True)

    seen = set()
    if not positions:
        tbl.add_row(
            Text('No positions — scanning…', style=FAINT),
            '', '', '', '', '', '', '', '', '', ''
        )
    else:
        for p in positions:
            sym   = p.get('symbol', '')
            d     = p.get('direction', '')
            lev   = p.get('leverage', 10)
            entry = p.get('entry', 0)
            curr  = p.get('current', entry)
            pp    = p.get('pnl_pct', 0)
            pu    = p.get('pnl_usd', 0)
            phase = p.get('phase', 'open')
            notional = p.get('size_usd', 0) or 0            # total position value $
            margin   = notional / lev if lev else notional  # amount invested $

            # sparkline history — append only on change
            seen.add(sym)
            h = _pos_spark.setdefault(sym, [])
            if not h or abs(h[-1] - pp) > 0.005:
                h.append(pp)
                if len(h) > 40: del h[:-40]

            is_long = d == 'long'
            dir_col = f'bold {TEAL}' if is_long else f'bold {PINK}'
            dir_sym = '▲ L' if is_long else '▼ S'
            lev_col = (AMBER if lev >= 15 else FAINT)
            pp_col  = _pcol(pp) if pp else FAINT

            ph_lbl, _old = _PHASE_NAMES.get(phase, (phase.upper()[:4], 'dim'))

            tbl.add_row(
                Text(sym, style=f'bold {TXT}'),
                Text(dir_sym, style=dir_col),
                Text(f'{lev}x', style=lev_col),
                Text(_fmt_price(entry), style=FAINT),
                Text(_fmt_price(curr),  style=TXT),
                Text(f'{pp:+.2f}%', style=f'bold {pp_col}'),
                Text(_spark_line(h, 10), style=pp_col),
                Text(f'{pu:+.2f}', style=f'bold {pp_col}'),
                Text(f'${notional:,.0f}', style=TXT),
                Text(f'${margin:,.0f}', style=FAINT),
                Text(ph_lbl, style=f'bold {VIOLET}'),
            )
    # drop sparkline history for closed positions
    for s in list(_pos_spark):
        if positions and s not in seen: del _pos_spark[s]

    open_n  = len(positions)
    longs   = sum(1 for p in positions if p.get('direction') == 'long')
    shorts  = open_n - longs
    tot_pnl = sum(p.get('pnl_usd', 0) for p in positions)
    tot_inv = sum((p.get('size_usd', 0) or 0) / (p.get('leverage', 10) or 10) for p in positions)

    title = (
        f'[bold {TEAL}]POSITIONS[/]  '
        f'[bold {TXT}]{open_n}[/][{FAINT}]/5[/]  '
        f'[{TEAL}]{longs}L[/] [{PINK}]{shorts}S[/]  '
        f'[{_pcol(tot_pnl)}]{tot_pnl:+.2f}$[/]  '
        f'[{FAINT}]invested[/] [{VIOLET}]${tot_inv:,.0f}[/]'
    )
    return Panel(tbl, title=title, border_style='#123a33', expand=True, padding=(0, 0))

# ─── Panel: Signal feed (ATLAS design) ────────────────────────────────────────
_TYPE_CFG = {
    'pass':  (TEAL,    'SIGNAL'),
    'fail':  (AMBER,   'FILTER'),
    'exec':  (TEAL,    '▶ EXEC'),
    'exit':  (PINK,    '◼ EXIT'),
    'warn':  (PINK,    'ALERT '),
    'info':  (VIOLET,  'INFO  '),
    'scan':  (FAINT,   '──────'),
    'error': (PINK,    '✗ ERR '),
}

def _scanner(S: dict, frame: int) -> Panel:
    scan_n    = S.get('scan_count', 0)
    log_lines = list(_log_feed)
    spin      = SPIN_A[frame % len(SPIN_A)]

    pass_n = sum(1 for e in log_lines[:80] if e.get('type') == 'pass')
    fail_n = sum(1 for e in log_lines[:80] if e.get('type') == 'fail')
    exec_n = sum(1 for e in log_lines[:80] if e.get('type') == 'exec')
    exit_n = sum(1 for e in log_lines[:80] if e.get('type') == 'exit')

    body = Text()

    # ── Live scan progress bar — updates every second even mid-scan ──────────
    prog  = S.get('scan_progress') or {}
    p_done, p_tot = prog.get('done', 0), prog.get('total', 0)
    if _connected:
        body.append(f'  {spin} ', style=TEAL)
        body.append(f'#{scan_n} ', style=CYAN2)
        if p_tot:
            bw     = 22
            filled = max(0, min(bw, round(p_done / p_tot * bw)))
            body.append('▰' * filled, style=TEAL)
            body.append('▱' * (bw - filled), style='grey15')
            body.append(f' {p_done}/{p_tot} ', style=f'bold {TXT}')
            body.append(f'{prog.get("sym","")}\n', style=FAINT)
        else:
            body.append('waiting for scan…\n', style=FAINT)
        body.append('  ', style='')
        body.append(f'{pass_n}', style=f'bold {TEAL}')
        body.append(' sig  ', style=FAINT)
        body.append(f'{fail_n}', style=f'bold {AMBER}')
        body.append(' fil  ', style=FAINT)
        body.append(f'{exec_n}', style=f'bold {TEAL}')
        body.append(' exec  ', style=FAINT)
        body.append(f'{exit_n}', style=f'bold {PINK}')
        body.append(' exit\n\n', style=FAINT)
    else:
        body.append('  ✗ OFFLINE — waiting for bot...\n\n', style=f'bold {PINK}')

    # ── Card-style feed rows: TIME [CHIP] SYMBOL ▲ CONF ▰▰▰▱▱ detail ─────────
    _sym_re  = re.compile(r'\b([A-Z0-9]{2,20}USDT)\b')
    _conf_re = re.compile(r'(\d{1,3}(?:\.\d)?)%')
    max_lines = 26
    shown = 0
    # SIGNALS ONLY — real trade events (passes, executions, exits, alerts).
    # Rejected 'FILTER' spam and info lines are excluded; the scan counter above
    # already shows how many were filtered. Dedupe repeated same-symbol events.
    _SHOW_TYPES = {'pass', 'exec', 'exit', 'warn', 'error'}
    _last_key = None
    for e in log_lines:
        if shown >= max_lines: break
        etype = e.get('type', 'info')
        if etype not in _SHOW_TYPES: continue
        _msg0 = e.get('msg', '')
        _mk   = _sym_re.search(_msg0)
        _dedup_key = (etype, _mk.group(1) if _mk else _msg0[:20])
        if _dedup_key == _last_key: continue   # skip consecutive duplicate
        _last_key = _dedup_key
        msg = e.get('msg', '')
        ts  = e.get('ts', '')
        col, tag = _TYPE_CFG.get(etype, (FAINT, '      '))

        m_sym  = _sym_re.search(msg)
        sym    = m_sym.group(1) if m_sym else ''
        m_conf = _conf_re.search(msg) if etype in ('pass', 'fail', 'exec') else None
        conf   = min(99.0, float(m_conf.group(1))) if m_conf else None
        is_l   = 'LONG' in msg
        is_s   = 'SHORT' in msg

        body.append(f'  {ts} ', style=FAINT)
        body.append(f'{tag:<6}', style=f'bold {col}')
        body.append(f' {sym[:13]:<13}', style=f'bold {TXT}' if sym else FAINT)
        if is_l:   body.append(' ▲', style=f'bold {TEAL}')
        elif is_s: body.append(' ▼', style=f'bold {PINK}')
        else:      body.append('  ')
        if conf is not None:
            k = max(0, min(5, round(conf / 20)))
            body.append(f' {conf:>3.0f}% ', style=f'bold {col}')
            body.append('▰' * k, style=col)
            body.append('▱' * (5 - k), style='grey15')
        else:
            body.append(' ' * 11)
        # detail: message minus symbol / conf% / direction already shown as chips
        detail = msg.replace(sym, '', 1).strip(' |—-') if sym else msg
        detail = re.sub(r'^\d{1,3}(?:\.\d)?%\s*', '', detail)
        detail = re.sub(r'^(LONG|SHORT)\s*\|?\s*', '', detail).strip(' |')
        if len(detail) > 34: detail = detail[:33] + '…'
        body.append(f'  {detail}\n',
                    style=col if etype in ('exec', 'exit', 'warn', 'pass') else FAINT)
        shown += 1

    title = (
        f'[bold {TEAL}]● SIGNAL FEED[/]  '
        f'[{FAINT}]#{scan_n}[/]  '
        f'[{TEAL}]{pass_n}[/][{FAINT}] sig[/]  '
        f'[{AMBER}]{fail_n}[/][{FAINT}] fil[/]  '
        f'[{PINK}]{exit_n}[/][{FAINT}] exit[/]'
    )
    return Panel(body, title=title, border_style='#123a33', expand=True)

# ─── Panel: Market Intel ──────────────────────────────────────────────────────
def _market(S: dict, frame: int) -> Panel:
    mi      = S.get('market_intel') or {}
    mood    = str(mi.get('market_mood', '—'))
    up      = mi.get('up_count', 0)
    dn      = mi.get('down_count', 0)
    tot     = mi.get('total_scanned', mi.get('total_perps', 0))
    btc_chg = mi.get('btc_change', 0) or 0
    mover   = mi.get('top_mover', '—')
    mover_p = mi.get('top_mover_pct', 0) or 0
    gainers = (mi.get('top_gainers') or [])[:5]
    losers  = (mi.get('top_losers')  or [])[:5]
    fng     = int(S.get('fear_greed', 50))
    spin    = SPIN_B[frame % len(SPIN_B)]

    mood_col  = 'bright_green' if 'BULL' in mood else ('bright_red' if 'BEAR' in mood else 'yellow')
    btc_col   = 'bright_green' if btc_chg >= 0 else 'bright_red'
    mov_col   = 'bright_green' if mover_p >= 0 else 'bright_red'
    if fng <= 20:   fng_col, fng_lbl = 'bright_green', 'EXT.FEAR'
    elif fng <= 40: fng_col, fng_lbl = 'green',         'FEAR'
    elif fng >= 80: fng_col, fng_lbl = 'bright_red',    'EXT.GREED'
    elif fng >= 60: fng_col, fng_lbl = 'yellow',         'GREED'
    else:           fng_col, fng_lbl = 'dim white',      'NEUTRAL'

    t = Text()

    # Market mood + BTC
    t.append('  MOOD  ', style='dim')
    t.append(f'{mood:<10}', style=f'bold {mood_col}')
    t.append('BTC 24h: ', style='dim')
    t.append(f'{btc_chg:+.2f}%\n', style=btc_col)

    # Fear & Greed bar
    t.append('  F&G   ', style='dim')
    fbar = max(0, min(18, int(fng / 100 * 18)))
    t.append('█' * fbar,        style=fng_col)
    t.append('░' * (18 - fbar), style='bright_black')
    t.append(f'  {fng} ', style=f'bold {fng_col}')
    t.append(f'{fng_lbl}\n', style=fng_col)

    # Breadth bar
    total_coins = up + dn
    bull_pct = up / total_coins if total_coins else 0.5
    bfilled  = max(0, min(18, int(bull_pct * 18)))
    t.append('  BREADTH ', style='dim')
    t.append('█' * bfilled,        style='bright_green')
    t.append('█' * (18 - bfilled), style='bright_red')
    t.append(f'  {up}', style='bright_green')
    t.append('/', style='dim')
    t.append(f'{dn}\n', style='bright_red')

    # Top mover
    t.append(f'\n  HOT:  ', style='dim')
    t.append(f'{mover:<14}', style='bold white')
    t.append(f'{mover_p:+.1f}%\n', style=mov_col)

    # Gainers
    if gainers:
        t.append('\n  LONG CANDIDATES\n', style='bold dim green')
        for g in gainers:
            pct = g.get('change_pct', 0) or 0
            sym = g.get('symbol', '')[:13]
            vol = g.get('volume', 0)
            t.append(f'  {sym:<13} ', style='white')
            t.append(f'{pct:+6.1f}%', style='bright_green')
            if vol:
                t.append(f'  {vol:.0f}M\n', style='dim')
            else:
                t.append('\n')

    # Losers
    if losers:
        t.append('\n  SHORT CANDIDATES\n', style='bold dim red')
        for l in losers:
            pct = l.get('change_pct', 0) or 0
            sym = l.get('symbol', '')[:13]
            vol = l.get('volume', 0)
            t.append(f'  {sym:<13} ', style='white')
            t.append(f'{pct:+6.1f}%', style='bright_red')
            if vol:
                t.append(f'  {vol:.0f}M\n', style='dim')
            else:
                t.append('\n')

    title = (
        f'[bold {AMBER}]MARKET[/]  '
        f'[{mood_col}]{mood}[/{mood_col}]  '
        f'[{FAINT}]{spin}  {tot} perps[/]'
    )
    return Panel(t, title=title, border_style='#3a2a08', expand=True)

# ─── Panel: Trades (ATLAS card grid — last 6 trades as cells) ─────────────────
def _trades(S: dict) -> Panel:
    all_trades = S.get('trades') or []
    trades    = list(reversed(all_trades))[:6]
    wins      = S.get('wins', 0)
    losses    = S.get('losses', 0)
    tot       = wins + losses
    wr        = wins / tot * 100 if tot else 0
    total_pnl = S.get('total_pnl', 0)
    avg_win   = S.get('avg_win',  0)
    avg_loss  = S.get('avg_loss', 0)

    gross_w = sum(t.get('pnl_usd', 0) for t in all_trades if t.get('is_win'))
    gross_l = abs(sum(t.get('pnl_usd', 0) for t in all_trades if not t.get('is_win')))
    pf = gross_w / gross_l if gross_l > 0 else (99.0 if gross_w > 0 else 0.0)

    grid = Table.grid(padding=(0, 1), expand=True)
    for _ in range(6):
        grid.add_column(ratio=1, justify='center')

    if not trades:
        empty = Text('No closed trades yet — scanning…', style=FAINT)
        grid.add_row(empty, '', '', '', '', '')
    else:
        cells = []
        for tr in trades:
            d      = tr.get('direction', '')
            pp     = tr.get('pnl_pct', 0)
            pu     = tr.get('pnl_usd', 0)
            won    = tr.get('is_win', pu > 0)
            reason = str(tr.get('reason', '—'))[:14]
            ctime  = str(tr.get('close_time', ''))[-8:]
            rc     = TEAL if won else PINK
            dc     = TEAL if d == 'long' else PINK

            c = Text(justify='center')
            c.append('▔' * 12 + '\n', style=rc)
            c.append(f"{'▲' if d == 'long' else '▼'} ", style=f'bold {dc}')
            c.append(f"{tr.get('symbol', '')[:12]}\n", style=f'bold {TXT}')
            c.append(f'{pp:+.2f}%\n', style=f'bold {_pcol(pp)}')
            c.append(f"{'WIN' if won else 'LOSS'}", style=f'bold {rc}')
            c.append(f' · {reason}\n', style=FAINT)
            c.append(f'{pu:+.2f}$  {ctime}', style=FAINT)
            cells.append(c)
        while len(cells) < 6:
            cells.append(Text(''))
        grid.add_row(*cells)

    pf_col = TEAL if pf >= 1.5 else (AMBER if pf >= 1.0 else PINK)
    title = (
        f'[bold {PINK}]TRADES[/]  '
        f'[{FAINT}]{wins}W / {losses}L[/]  '
        f'[{AMBER}]WR {wr:.0f}%[/]  '
        f'[{_pcol(total_pnl)}]Total {total_pnl:+.2f}$[/]  '
        f'[{pf_col}]PF {pf:.2f}[/]  '
        f'[{FAINT}]AvgW {avg_win:+.2f}%  AvgL {avg_loss:+.2f}%[/]'
    )

    # ── Last-20 ribbon — see recent history beyond the 6 detailed cards ──────
    ribbon = Text()
    last20 = list(reversed(all_trades))[:20]
    if last20:
        ribbon.append('  Last 20:  ', style=FAINT)
        for tr in last20:   # newest → oldest, left → right
            won = tr.get('is_win', tr.get('pnl_usd', 0) > 0)
            rc  = TEAL if won else PINK
            sym = tr.get('symbol', '').replace('USDT', '')[:4]
            ribbon.append(f"{sym}", style=rc)
            ribbon.append(f"{tr.get('pnl_pct', 0):+.0f} ", style=f'bold {rc}')
    return Panel(Group(grid, ribbon), title=title, border_style='#3a0a22', expand=True)

# ─── Panel: Stats (sidebar strip) ────────────────────────────────────────────
def _stats(S: dict) -> Panel:
    perf    = S.get('perf') or {}
    sharpe  = perf.get('sharpe', S.get('sharpe', 0)) or 0
    sortino = perf.get('sortino', S.get('sortino', 0)) or 0
    dd      = S.get('max_dd', 0) or 0
    trades  = S.get('trades') or []
    wins    = S.get('wins', 0)
    losses  = S.get('losses', 0)
    tot     = wins + losses
    wr      = S.get('win_rate', 0)

    # Analytics from actual trade history
    win_usd  = [t['pnl_usd'] for t in trades if t.get('is_win')]
    loss_usd = [t['pnl_usd'] for t in trades if not t.get('is_win')]
    gross_win  = sum(win_usd)
    gross_loss = abs(sum(loss_usd))
    pf   = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    avg_w = gross_win / len(win_usd) if win_usd else 0.0
    avg_l = -gross_loss / len(loss_usd) if loss_usd else 0.0
    expect = (wr / 100 * avg_w) + ((1 - wr / 100) * avg_l) if tot else 0.0
    best  = max((t['pnl_usd'] for t in trades), default=0.0)
    worst = min((t['pnl_usd'] for t in trades), default=0.0)
    ws    = perf.get('win_streak', 0)
    ls    = perf.get('loss_streak', 0)

    def metric(label, value, color='white', suffix=''):
        t = Text()
        t.append(f'  {label:<11}', style='dim')
        t.append(f'{value}{suffix}\n', style=f'bold {color}')
        return t

    pf_col  = 'bright_green' if pf >= 1.5 else ('yellow' if pf >= 1.0 else 'bright_red')
    ex_col  = 'bright_green' if expect > 0 else 'bright_red'
    wr_col  = 'bright_green' if wr >= 50 else ('yellow' if wr >= 40 else 'bright_red')
    sh_col  = 'bright_green' if sharpe >= 1 else ('yellow' if sharpe >= 0 else 'bright_red')
    dd_col  = 'bright_green' if dd < 5 else ('yellow' if dd < 10 else 'bright_red')

    t = Text()
    t.append_text(metric('Risk/trade', f"${S.get('risk_per_trade', 0):.2f} (0.4%)", CYAN2))
    t.append_text(metric('ProfitFac', f'{pf:.2f}',              pf_col))
    t.append_text(metric('Expectancy',f'${expect:+.2f}/trade',  ex_col))
    t.append_text(metric('WinRate',   f'{wr:.0f}% ({wins}W/{losses}L)', wr_col))
    t.append_text(metric('AvgWin',    f'${avg_w:+.2f}',         'bright_green'))
    t.append_text(metric('AvgLoss',   f'${avg_l:+.2f}',         'bright_red'))
    t.append_text(metric('Best/Worst',f'${best:+.2f} / ${worst:+.2f}', 'white'))
    t.append_text(metric('Streak',    f'{ws}W' if ws else f'{ls}L', 'bright_green' if ws else ('bright_red' if ls else 'white')))
    t.append_text(metric('Sharpe',    f'{sharpe:.2f} / {sortino:.2f}', sh_col))
    t.append_text(metric('MaxDD',     f'{dd:.1f}',              dd_col, '%'))

    title = f'[bold {TXT}]📊 TRADE ANALYTICS[/]'
    return Panel(t, title=title, border_style='#123a33', expand=True)

# ─── Panel: Balance Chart (full-width line chart) ─────────────────────────────
def _braille_line(vals, w_cells, h_cells, start_val):
    """Smooth connected line chart using Braille (2×4 sub-dots per cell) — far
    higher resolution and cleaner than block/dash rows, in a fraction of the height.
    Returns a list of Text rows (top→bottom)."""
    DOT = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][col] bit
    DW, DH = w_cells * 2, h_cells * 4
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    sampled = [vals[min(n - 1, int(i * n / DW))] for i in range(DW)] if n else []
    ys = [int((v - lo) / rng * (DH - 1)) for v in sampled]   # 0=bottom
    bits, colv = {}, {}
    for x in range(DW):
        dots = {ys[x]}
        if x > 0:                                            # connect to previous
            dots |= set(range(min(ys[x - 1], ys[x]), max(ys[x - 1], ys[x]) + 1))
        for yy in dots:
            cell = ((DH - 1 - yy) // 4, x // 2)
            bits[cell] = bits.get(cell, 0) | DOT[(DH - 1 - yy) % 4][x % 2]
            colv.setdefault(cell, sampled[x])
    rows = []
    for r in range(h_cells):
        line = Text()
        for c in range(w_cells):
            b = bits.get((r, c), 0)
            if b:
                line.append(chr(0x2800 + b),
                            style=TEAL if colv.get((r, c), start_val) >= start_val else PINK)
            else:
                line.append(' ')
        rows.append(line)
    return rows, lo, hi


def _balance_chart(S: dict) -> Panel:
    global _bal_last_ts, _bal_save_ts
    bal  = S.get('balance', 0.0)
    dpnl = S.get('daily_pnl', 0.0)
    tpnl = S.get('total_pnl', 0.0)

    now = time.time()
    if bal > 0:
        last_val = _balance_history[-1][1] if _balance_history else 0.0
        # Record steadily — keep the FULL wallet history including drawdowns
        # (no clearing). One point per ~60s or on any real balance change.
        if now - _bal_last_ts >= 60 or (last_val and abs(bal - last_val) >= 0.01):
            _balance_history.append((now, bal))
            _bal_last_ts = now
            if now - _bal_save_ts >= 30:
                _save_balance_history()
                _bal_save_ts = now

    vals   = [v for _, v in _balance_history]
    n      = len(vals)
    span_s = (now - _balance_history[0][0]) if _balance_history else 0
    mins   = int(span_s / 60)
    ses_pct = (vals[-1] - vals[0]) / vals[0] * 100 if n >= 2 and vals[0] else 0.0
    ses_col  = _pcol(ses_pct)
    dpnl_col = _pcol(dpnl)
    tpnl_col = _pcol(tpnl)

    # Human-readable span of the whole wallet history
    if   span_s >= 86400: span_txt = f'{span_s/86400:.1f}d'
    elif span_s >= 3600:  span_txt = f'{span_s/3600:.1f}h'
    else:                 span_txt = f'{mins}min'

    # Stats line always shown at top
    stats = Text()
    stats.append('  $', style=f'bold {TEAL}')
    stats.append(f'{bal:,.2f}  ', style='bold #e8e8ff')
    stats.append('DAY ', style=FAINT)
    stats.append(f'{dpnl:+,.2f}  ', style=f'bold {dpnl_col}')
    stats.append('TOTAL ', style=FAINT)
    stats.append(f'{tpnl:+,.2f}  ', style=f'bold {tpnl_col}')
    stats.append('ALL-TIME ', style=FAINT)
    stats.append(f'{ses_pct:+.2f}%  ', style=f'bold {ses_col}')
    stats.append(f'[{n}pts / {span_txt} wallet history]\n', style=FAINT)

    if n < 3:
        stats.append('\n  Collecting balance history — line chart appears after ~60 seconds\n', style=FAINT)
        return Panel(stats, title=f'[bold {TEAL}]◆ ATLAS · BALANCE[/]',
                     border_style='#123a33', expand=True)

    start_val = vals[0]
    CHART_H = 3      # cell rows (Braille = 4 sub-dots each → 12 vertical levels)
    CHART_W = 150    # cell columns (Braille = 2 sub-dots each → 300 samples)

    rows, lo, hi = _braille_line(vals, CHART_W, CHART_H, start_val)
    max_lw = len(f'${hi:,.2f}')
    # y-labels: top row = hi, bottom row = lo, middle = midpoint
    y_labels = [f'${hi:,.2f}', f'${(hi+lo)/2:,.2f}', f'${lo:,.2f}']

    body = stats
    for i, line in enumerate(rows):
        label = y_labels[i] if i < len(y_labels) else ''
        body.append(f'  {label:>{max_lw}} │', style='dim')
        body.append_text(line)
        body.append('\n')

    # X-axis rule + time labels
    body.append(f'  {" " * max_lw} └', style='dim')
    body.append('─' * CHART_W, style='bright_black')
    body.append('\n')
    ago_str = f'← wallet start ({span_txt} ago)'
    now_str = 'now →'
    gap = CHART_W - len(ago_str) - len(now_str)
    body.append(f'  {" " * (max_lw + 1)} ', style='dim')
    body.append(ago_str, style='dim')
    if gap > 0:
        body.append(' ' * gap, style='dim')
    body.append(now_str, style='dim')

    title = (
        f'[bold {TEAL}]◆ ATLAS · BALANCE[/]  '
        f'[{_pcol(ses_pct)}]{ses_pct:+.3f}%[/]  '
        f'[{FAINT}]Hi [/][{TEAL}]${hi:,.2f}[/]   [{FAINT}]Lo [/][{PINK}]${lo:,.2f}[/]'
    )
    return Panel(body, title=title, border_style='#123a33', expand=True)


# ─── Footer ───────────────────────────────────────────────────────────────────
def _footer() -> Rule:
    mode = 'POLY' if POLY_MODE else 'BINANCE'
    return Rule(
        f'[{FAINT}]  [bold {TEAL}]Q[/] Quit   '
        f'[bold {VIOLET}]R[/] Restart   '
        f'[bold {AMBER}]B[/] Browser   '
        f'[bold {CYAN2}]P[/] Pause   '
        f'│  [bold {TEAL}]{mode}[/]  ws://127.0.0.1:{"8766" if POLY_MODE else "8765"}  [{TEAL}]●[/][/]',
        style='#123a33',
    )

# ─── Layout builder ───────────────────────────────────────────────────────────
def _build(S: dict, frame: int) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name='header',    size=5),
        Layout(name='body'),
        Layout(name='bal_chart', size=6),   # compact Braille balance chart (-30%)
        Layout(name='trades',    size=12),  # ATLAS trade card grid + last-20 ribbon (more room)
        Layout(name='footer',    size=1),
    )
    layout['body'].split_row(
        Layout(name='positions', ratio=7),
        Layout(name='scanner',   ratio=5),
        Layout(name='right_col', ratio=4),
    )
    layout['body']['right_col'].split_column(
        Layout(name='gauges', size=5),
        Layout(name='stats',  size=12),     # trade analytics — risk, profit factor, expectancy
        Layout(name='market', ratio=1),     # market fills remaining space
    )
    layout['header'].update(_header(S))
    layout['body']['positions'].update(_positions(S))
    layout['body']['scanner'].update(_scanner(S, frame))
    layout['body']['right_col']['gauges'].update(_gauges(S))
    layout['body']['right_col']['stats'].update(_stats(S))
    layout['body']['right_col']['market'].update(_market(S, frame))
    layout['bal_chart'].update(_balance_chart(S))
    layout['trades'].update(_trades(S))
    layout['footer'].update(_footer())
    return layout

# ─── Terminal capability probe ────────────────────────────────────────────────
def _probe_terminal() -> bool:
    """Return True if terminal supports full-screen rich rendering."""
    # Must be a real TTY (not redirected to file)
    if not sys.stdout.isatty() and not os.environ.get('FORCE_TUI'):
        return False
    # Windows: check if ANSI/VT processing is enabled
    if sys.platform == 'win32':
        try:
            import ctypes
            h = ctypes.windll.kernel32.GetStdHandle(-11)  # STDOUT
            mode = ctypes.c_ulong(0)
            ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode))
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            if not (mode.value & 0x0004):
                # Try to enable it
                ctypes.windll.kernel32.SetConsoleMode(h, mode.value | 0x0004)
                # Re-check
                ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode))
                return bool(mode.value & 0x0004)
            return True
        except Exception:
            return False
    return True


# ─── Simple fallback display (for terminals that don't support full-screen) ───
def _simple_display(S: dict, frame: int):
    """Plain-text dashboard — works in any terminal."""
    os.system('cls' if sys.platform == 'win32' else 'clear')

    spin = SPIN_B[frame % len(SPIN_B)]
    bal     = S.get('balance', 0)
    dpnl    = S.get('daily_pnl', 0)
    tpnl    = S.get('total_pnl', 0)
    rsi     = float(S.get('btc_rsi', 50) or 50)
    trend   = str(S.get('btc_trend', '?')).upper()
    scan    = S.get('scan_count', 0)
    wins    = S.get('wins', 0)
    losses  = S.get('losses', 0)
    wr      = wins / (wins + losses) * 100 if (wins + losses) else 0
    mi      = S.get('market_intel') or {}
    mood    = mi.get('market_mood', '?')
    up      = mi.get('up_count', 0)
    dn      = mi.get('down_count', 0)
    tot     = mi.get('total_scanned', 0)
    mover   = mi.get('top_mover', '?')
    mover_p = mi.get('top_mover_pct', 0) or 0
    paused  = S.get('paused', False)
    sess    = S.get('session', '?')
    conn    = 'LIVE' if _connected else 'OFFLINE'

    rsi_zone = 'WHALE BUY' if rsi <= 22 else ('WHALE SELL' if rsi >= 78 else 'neutral')

    print('=' * 70)
    print(f'  AlphaBot v5.0   {conn}   Bal: ${bal:,.2f}   Day: {dpnl:+.2f}   Total: {tpnl:+.2f}')
    print(f'  BTC: {trend}   RSI: {rsi:.0f} ({rsi_zone})   Scan #{scan}   {sess}   {spin}')
    print(f'  Market: {mood}   Up:{up}  Dn:{dn}  /{tot} perps   Top: {mover} {mover_p:+.1f}%')
    if paused:
        print(f'  *** PAUSED ***')
    print('=' * 70)

    # Positions
    positions = S.get('positions') or []
    print(f'\n  OPEN POSITIONS ({len(positions)}):')
    if not positions:
        print('  (none)')
    for p in positions:
        sym   = p.get('symbol', '')
        d     = p.get('direction', '')
        lev   = p.get('leverage', 10)
        pp    = p.get('pnl_pct', 0)
        pu    = p.get('pnl_usd', 0)
        phase = p.get('phase', 'open')
        entry = p.get('entry', 0)
        curr  = p.get('current', entry)
        sl    = p.get('sl', 0)
        dir_s = 'LONG ' if d == 'long' else 'SHORT'
        sign  = '+' if pp >= 0 else ''
        print(f'  {sym:<14} {dir_s} {lev}X | Entry:{entry:.2f} Now:{curr:.2f} | '
              f'P&L:{sign}{pp:.2f}% {sign}{pu:.2f}$ | {phase} | SL:{sl:.2f}')

    # Recent trades
    trades = list(reversed(S.get('trades') or []))[:5]
    print(f'\n  RECENT TRADES   WR:{wr:.0f}%  {wins}W/{losses}L  Total:{tpnl:+.2f}$:')
    if not trades:
        print('  (none)')
    for tr in trades:
        sym   = tr.get('symbol', '')
        d     = tr.get('direction', '')
        pp    = tr.get('pnl_pct', 0)
        pu    = tr.get('pnl_usd', 0)
        lev   = tr.get('leverage', 10)
        res   = 'WIN ' if pu > 0 else 'LOSS'
        sign  = '+' if pp >= 0 else ''
        ctime = str(tr.get('close_time', ''))[-8:]
        print(f'  {res} {sym:<14} {"L" if d=="long" else "S"} {lev}X | '
              f'{sign}{pp:.2f}% {sign}{pu:.2f}$  {ctime}')

    # Log feed
    log_lines = list(_log_feed)[:6]
    print(f'\n  SCANNER LOG:')
    for e in log_lines:
        t   = e.get('type', '?')
        msg = e.get('msg', '')[:65]
        ts  = e.get('ts', '')
        icon = {'pass':'PASS','fail':'----','exec':'EXEC','exit':'EXIT','info':'INFO'}.get(t,'    ')
        print(f'  [{ts}] {icon} {msg}')

    print('\n  Controls: Q=Quit  R=Restart Bot  B=Browser Dashboard')
    print('  (type letter then press Enter)')


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    global _action

    # Start background threads
    threading.Thread(target=_ws_thread, daemon=True).start()
    threading.Thread(target=_kb_thread,  daemon=True).start()

    # Wait briefly for first WS connection
    time.sleep(1.5)

    use_rich = _probe_terminal()
    frame    = 0

    if use_rich:
        # ── Full-screen rich TUI ──────────────────────────────────────────────
        console = Console(force_terminal=True, force_jupyter=False)
        try:
            with Live(
                _build({}, frame),
                screen=True,
                refresh_per_second=REFRESH_HZ,
                console=console,
                transient=False,
            ) as live:
                while not _quit_evt.is_set():
                    if _action:
                        act     = _action
                        _action = None
                        if act in ('q', '\x03', '\x1b'):
                            _quit_evt.set()
                            break
                        elif act == 'r':
                            restart_bot()
                        elif act == 'b':
                            webbrowser.open(BROWSER_URL)

                    with _lock:
                        snap = dict(_S)
                    live.update(_build(snap, frame))
                    frame += 1
                    time.sleep(1 / REFRESH_HZ)

        except Exception as e:
            # Rich failed — drop to simple mode
            print(f'\n[TUI] Switching to simple mode: {e}\n')
            use_rich = False

    if not use_rich:
        # ── Simple text fallback — works in ALL terminals ─────────────────────
        print('AlphaBot v5.0 — Simple Dashboard (press Enter after each key)')
        print('Controls: q=Quit  r=Restart  b=Browser')
        print('Connecting to bot...\n')
        time.sleep(2)

        while not _quit_evt.is_set():
            with _lock:
                snap = dict(_S)
            _simple_display(snap, frame)
            frame += 1

            # Non-blocking input check (Windows)
            if _WINDOWS and msvcrt.kbhit():
                ch = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                if ch == 'q':
                    break
                elif ch == 'r':
                    print('\n  Restarting bot...')
                    restart_bot()
                    time.sleep(3)
                elif ch == 'b':
                    webbrowser.open(BROWSER_URL)

            time.sleep(2)   # refresh every 2 seconds in simple mode

    _quit_evt.set()
    print()
    print('AlphaBot TUI closed.')
    print('Bot engine continues running in background.')
    print('Run: python tui.py   to reconnect.')


if __name__ == '__main__':
    main()
