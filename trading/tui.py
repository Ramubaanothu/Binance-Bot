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
from datetime import datetime

try:
    import msvcrt
    _WINDOWS = True
except ImportError:
    _WINDOWS = False
    import tty, termios, select

import websockets
from rich.align import Align
from rich.console import Console
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
              else 'http://localhost:8080/terminal.html'
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
_balance_history  = deque(maxlen=120)     # (unix_ts, balance) for chart
_bal_last_ts      = 0.0                   # throttle: record at most every 30s

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

    ts = datetime.utcnow().strftime('%H:%M:%S UTC')

    # ── Row 1: Brand + Status + Balance ─────────────────────────────────────
    r1 = Text(justify='center')
    r1.append(f'  {BOT_LABEL} v5.0  ', style='bold bright_white on grey11')
    r1.append('  ', style='')
    r1.append(conn_lbl, style=f'bold {conn_col}')
    if paused:
        r1.append('  PAUSED', style='bold yellow')
    r1.append('  ', style='')
    r1.append(mode_lbl, style=f'bold {mode_col}')
    r1.append('    Balance ', style='dim')
    r1.append(f'${bal:>12,.2f}', style='bold bright_white')
    r1.append('    Day ', style='dim')
    r1.append(f'{dpnl:+,.2f}', style=f'bold {dpnl_col}')
    r1.append('    Total ', style='dim')
    r1.append(f'{tpnl:+,.2f}', style=f'bold {tpnl_col}')
    r1.append('    WR ', style='dim')
    r1.append(f'{wr:.0f}%', style=f'bold {wr_col}')
    r1.append(f'  {wins}W/{losses}L', style='dim')
    r1.append(f'    Pos {pos_n}', style='dim')
    r1.append(f'    {ts}', style='dim')

    # ── Row 2: BTC multi-timeframe + RSI bar ────────────────────────────────
    r2 = Text(justify='center')
    r2.append('  BTC ', style='dim')
    r2.append('1H:', style='dim')
    r2.append(f'{trend}', style=f'bold {trend_col}')
    r2.append('  4H:', style='dim')
    r2.append(f'{trend4h}', style=f'bold {t4h_col}')
    r2.append('  DAILY:', style='dim')
    r2.append('BULL' if daily_bull else 'BEAR', style=f'bold {daily_col}')
    r2.append('    RSI ', style='dim')
    r2.append_text(rsi_bar(rsi, 18))
    r2.append(f' {rsi:.0f} ', style=f'bold {rsi_col}')
    r2.append(rsi_lbl, style=rsi_col)
    r2.append('    10m:', style='dim')
    r2.append(f'{btc5m:+.2f}%', style=f'bold {mom_col}')
    r2.append(f'    {sess} session    Scan #{scan}', style='dim')

    # ── Row 3: Fear & Greed + Market breadth ────────────────────────────────
    r3 = Text(justify='center')
    r3.append('  Fear & Greed: ', style='dim')
    fng_filled = max(0, min(20, int(fng / 5)))
    r3.append('█' * fng_filled, style=fng_col)
    r3.append('░' * (20 - fng_filled), style='bright_black')
    r3.append(f' {fng} ', style=f'bold {fng_col}')
    r3.append(fng_lbl, style=fng_col)
    up_n = mi.get('up_count', 0)
    dn_n = mi.get('down_count', 0)
    tot_p = mi.get('total_scanned', 0)
    btc_chg = mi.get('btc_change', 0) or 0
    r3.append(f'    Market: ', style='dim')
    r3.append(mood, style=f'bold {mood_col}')
    r3.append(f'  {up_n}', style='bright_green')
    r3.append('up ', style='dim green')
    r3.append(f'{dn_n}', style='bright_red')
    r3.append(f'dn /{tot_p}', style='dim')
    r3.append('    BTC 24h:', style='dim')
    r3.append(f'{btc_chg:+.2f}%', style='bright_green' if btc_chg >= 0 else 'bright_red')

    body = Text()
    body.append_text(r1)
    body.append('\n')
    body.append_text(r2)
    body.append('\n')
    body.append_text(r3)

    return Panel(body, style='bright_black', height=5, padding=(0, 1))

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

# ─── Panel: Ring gauges ───────────────────────────────────────────────────────
def _gauges(S: dict) -> Panel:
    wins   = S.get('wins', 0)
    losses = S.get('losses', 0)
    tot    = wins + losses
    wr     = wins / tot * 100 if tot else 0
    perf   = S.get('perf') or {}
    sharpe = float(perf.get('sharpe', S.get('sharpe', 0)) or 0)
    dd     = abs(float(S.get('max_dd', 0) or 0))
    fng    = int(S.get('fear_greed', 50))

    wr_col  = 'bright_green' if wr >= 55 else ('yellow' if wr >= 45 else 'bright_red')
    sh_col  = 'bright_green' if sharpe >= 1.5 else ('green' if sharpe >= 0.5 else 'yellow')
    dd_col  = 'bright_red'   if dd > 15  else ('yellow' if dd > 5 else 'bright_green')
    if fng <= 20:   fg_col = 'bright_green'
    elif fng >= 80: fg_col = 'bright_red'
    elif fng <= 40: fg_col = 'green'
    elif fng >= 60: fg_col = 'yellow'
    else:           fg_col = 'dim white'

    sh_pct  = min(100, max(0, sharpe / 3.0 * 100))
    dd_pct  = max(0, 100 - dd * 5)          # 0% DD → 100%, 20% DD → 0%
    fng_pct = fng                            # 0–100 directly

    g1 = _ring3(wr,     f'{wr:.0f}%', 'WR',  wr_col,  w=7)
    g2 = _ring3(sh_pct, f'{sharpe:.1f}', 'SH', sh_col, w=7)
    g3 = _ring3(dd_pct, f'{dd:.1f}%', 'DD',  dd_col,  w=7)
    g4 = _ring3(fng_pct, str(fng),   'F&G', fg_col,  w=7)

    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(justify='center')
    grid.add_column(justify='center')
    grid.add_column(justify='center')
    grid.add_column(justify='center')
    grid.add_row(g1, g2, g3, g4)

    return Panel(grid, title='[bold bright_white]GAUGES[/]',
                 border_style='bright_black', expand=True, padding=(0, 1))


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
    tbl.add_column('P&L %',  width=18, no_wrap=True)
    tbl.add_column('P&L $',  width=8,  justify='right', no_wrap=True)
    tbl.add_column('Ph',      width=5,  no_wrap=True)

    if not positions:
        tbl.add_row(
            Text('No positions — scanning', style='dim'),
            '', '', '', '', '', '', ''
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

            is_long = d == 'long'
            dir_col = 'bold bright_green' if is_long else 'bold bright_red'
            dir_sym = '▲' if is_long else '▼'
            lev_col = ('bright_yellow' if lev >= 20 else 'yellow' if lev >= 15 else 'dim')
            pp_col  = 'bright_green' if pp > 0 else ('bright_red' if pp < 0 else 'dim')

            pnl_cell = Text()
            pnl_cell.append(f'{pp:+6.2f}%', style=f'bold {pp_col}')
            pnl_cell.append(' ', style='')
            pnl_cell.append_text(_pnl_bar(pp, 9))

            ph_lbl, ph_col = _PHASE_NAMES.get(phase, (phase.upper()[:4], 'dim'))

            tbl.add_row(
                Text(sym, style='bold white'),
                Text(dir_sym, style=dir_col),
                Text(f'{lev}x', style=lev_col),
                Text(_fmt_price(entry), style='dim'),
                Text(_fmt_price(curr),  style='white'),
                pnl_cell,
                Text(f'{pu:+.1f}', style=f'bold {pp_col}'),
                Text(ph_lbl, style=ph_col),
            )

    open_n  = len(positions)
    longs   = sum(1 for p in positions if p.get('direction') == 'long')
    shorts  = open_n - longs
    tot_pnl = sum(p.get('pnl_usd', 0) for p in positions)
    tot_col = 'bright_green' if tot_pnl >= 0 else 'bright_red'

    title = (
        f'[bold bright_blue]POSITIONS[/]  '
        f'[bold white]{open_n}[/][dim]/5[/]  '
        f'[bright_green]{longs}L[/] [bright_red]{shorts}S[/]  '
        f'[dim]P&L [/][{tot_col}]{tot_pnl:+.2f}$[/{tot_col}]'
    )
    return Panel(tbl, title=title, border_style='blue', expand=True, padding=(0, 0))

# ─── Panel: Scanner feed ──────────────────────────────────────────────────────
_TYPE_CFG = {
    'pass':  ('bright_cyan',   '✓ PASS'),
    'fail':  ('bright_black',  '· fail'),
    'exec':  ('bright_green',  '▶ EXEC'),
    'exit':  ('yellow',        '◼ EXIT'),
    'warn':  ('yellow',        '! WARN'),
    'info':  ('white',         '  info'),
    'scan':  ('bright_black',  '──────'),
    'error': ('bright_red',    '✗ ERR '),
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

    # Status line
    if _connected:
        body.append(f'  {spin} ', style='bright_cyan')
        body.append(f'Live  Scan #{scan_n}   ', style='cyan')
        body.append(f'{pass_n} pass  ', style='bright_cyan')
        body.append(f'{fail_n} filtered  ', style='bright_black')
        body.append(f'{exec_n} opened  ', style='bright_green')
        body.append(f'{exit_n} closed\n\n', style='yellow')
    else:
        body.append('  ✗ OFFLINE — waiting for bot...\n\n', style='bold bright_red')

    # Log feed — skip pure 'scan' separator lines, show meaningful events
    max_lines = 28
    shown = 0
    for e in log_lines:
        if shown >= max_lines: break
        etype = e.get('type', 'info')
        if etype == 'scan': continue   # skip ── Scan separator lines
        msg = e.get('msg', '')
        ts  = e.get('ts', '')
        col, tag = _TYPE_CFG.get(etype, ('dim', '      '))

        # Trim message to fit
        max_w = 68
        if len(msg) > max_w:
            msg = msg[:max_w - 1] + '…'

        body.append(f'  {ts} ', style='bright_black')
        body.append(f'{tag} ', style=col)
        body.append(f'{msg}\n', style=col)
        shown += 1

    title = (
        f'[bold bright_cyan]SCANNER[/]  '
        f'[dim]#{scan_n}[/]  '
        f'[bright_cyan]{pass_n} pass[/]  '
        f'[bright_green]{exec_n} opened[/]  '
        f'[yellow]{exit_n} closed[/]'
    )
    return Panel(body, title=title, border_style='cyan', expand=True)

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
        f'[bold bright_yellow]MARKET[/]  '
        f'[{mood_col}]{mood}[/{mood_col}]  '
        f'[dim]{spin}[/]  '
        f'[dim]{tot} perps[/]'
    )
    return Panel(t, title=title, border_style='yellow', expand=True)

# ─── Panel: Trades ────────────────────────────────────────────────────────────
def _trades(S: dict) -> Panel:
    trades    = list(reversed(S.get('trades') or []))[:10]
    wins      = S.get('wins', 0)
    losses    = S.get('losses', 0)
    tot       = wins + losses
    wr        = wins / tot * 100 if tot else 0
    total_pnl = S.get('total_pnl', 0)
    avg_win   = S.get('avg_win',  0)
    avg_loss  = S.get('avg_loss', 0)

    tbl = Table(
        box=box.SIMPLE_HEAD, expand=True, padding=(0, 1),
        header_style='bold dim', show_edge=False,
    )
    tbl.add_column('#',      width=3,  justify='right')
    tbl.add_column('Symbol', style='bold white', no_wrap=True, min_width=11)
    tbl.add_column('Dir',    width=7)
    tbl.add_column('Lev',    width=4,  justify='right')
    tbl.add_column('Entry',             justify='right')
    tbl.add_column('Exit',              justify='right')
    tbl.add_column('P&L %',  width=8,  justify='right')
    tbl.add_column('P&L $',  width=8,  justify='right')
    tbl.add_column('Result', width=6)
    tbl.add_column('Reason', width=14)
    tbl.add_column('Time',   width=9)

    if not trades:
        tbl.add_row('', '[dim]No closed trades yet — bot scanning[/]',
                    '', '', '', '', '', '', '', '', '')
    else:
        for i, tr in enumerate(trades, 1):
            d      = tr.get('direction', '')
            pp     = tr.get('pnl_pct', 0)
            pu     = tr.get('pnl_usd', 0)
            lev    = tr.get('leverage', 10)
            entry  = tr.get('entry', 0)
            exit_p = tr.get('exit', tr.get('close', 0))
            reason = tr.get('reason', tr.get('exit_reason', '—'))[:12]
            ctime  = str(tr.get('close_time', tr.get('open_time', '')))[-8:]

            won     = pu > 0
            dir_mk  = '[bright_green]▲ L[/]' if d == 'long' else '[bright_red]▼ S[/]'
            res_mk  = '[bold bright_green]WIN[/]' if won else '[bold bright_red]LOSS[/]'
            pp_col  = 'bright_green' if won else 'bright_red'
            lev_col = 'bright_yellow' if lev >= 20 else ('yellow' if lev >= 15 else 'white')

            def fp(v):
                if not v: return '—'
                return f'{v:,.2f}' if v >= 10 else f'{v:.4f}'

            tbl.add_row(
                str(i),
                tr.get('symbol', ''),
                Text.from_markup(dir_mk),
                Text.from_markup(f'[{lev_col}]{lev}X[/]'),
                fp(entry),
                fp(exit_p),
                Text.from_markup(f'[{pp_col}]{pp:+.2f}%[/]'),
                Text.from_markup(f'[{pp_col}]{pu:+.2f}[/]'),
                Text.from_markup(res_mk),
                Text(reason, style='dim'),
                ctime,
            )

    wr_col  = 'bright_green' if wr >= 55 else ('yellow' if wr >= 45 else 'bright_red')
    pnl_col = 'bright_green' if total_pnl >= 0 else 'bright_red'
    rr = abs(avg_win / avg_loss) if avg_loss else 0

    title = (
        f'[bold bright_magenta]TRADES[/]  '
        f'[dim]{wins}W / {losses}L[/]  '
        f'[{wr_col}]WR {wr:.0f}%[/]  '
        f'[dim]Total [/][{pnl_col}]{total_pnl:+.2f}$[/{pnl_col}]  '
        f'[dim]RR {rr:.1f}x   AvgW {avg_win:+.2f}%   AvgL {avg_loss:+.2f}%[/]'
    )
    return Panel(tbl, title=title, border_style='magenta', expand=True)

# ─── Panel: Stats (sidebar strip) ────────────────────────────────────────────
def _stats(S: dict) -> Panel:
    perf    = S.get('perf') or {}
    sharpe  = perf.get('sharpe', S.get('sharpe', 0)) or 0
    sortino = perf.get('sortino', S.get('sortino', 0)) or 0
    calmar  = perf.get('calmar', 0) or 0
    dd      = S.get('max_dd', 0) or 0
    positions = S.get('positions') or []
    scan_results = S.get('scan_results') or []
    pass_n = len([r for r in scan_results if r.get('confidence', 0) >= 38])

    def metric(label, value, color='white', suffix=''):
        t = Text()
        t.append(f'  {label:<12}', style='dim')
        t.append(f'{value}{suffix}\n', style=f'bold {color}')
        return t

    sharpe_col  = 'bright_green' if sharpe >= 1 else ('yellow' if sharpe >= 0 else 'bright_red')
    dd_col      = 'bright_green' if dd < 5 else ('yellow' if dd < 10 else 'bright_red')

    t = Text()
    t.append_text(metric('Sharpe',  f'{sharpe:.2f}',  sharpe_col))
    t.append_text(metric('Sortino', f'{sortino:.2f}', sharpe_col))
    t.append_text(metric('Calmar',  f'{calmar:.2f}',  sharpe_col))
    t.append_text(metric('MaxDD',   f'{dd:.1f}',      dd_col, '%'))
    t.append('\n', style='')
    t.append_text(metric('OpenPos', str(len(positions)), 'white'))
    t.append_text(metric('Signals', str(pass_n),         'cyan'))

    title = '[bold]⚡ STATS[/]'
    return Panel(t, title=title, border_style='bright_black', expand=True)

# ─── Panel: Balance Chart ─────────────────────────────────────────────────────
_SPARK = '▁▂▃▄▅▆▇█'

def _balance_chart(S: dict) -> Panel:
    global _bal_last_ts
    bal  = S.get('balance', 0.0)
    dpnl = S.get('daily_pnl', 0.0)
    tpnl = S.get('total_pnl', 0.0)

    # Record a new datapoint at most every 30 s, or when balance shifts by >$0.05
    now = time.time()
    if bal > 0:
        last_val = _balance_history[-1][1] if _balance_history else 0.0
        if now - _bal_last_ts >= 30 or abs(bal - last_val) >= 0.05:
            _balance_history.append((now, bal))
            _bal_last_ts = now

    vals = [v for _, v in _balance_history]
    dpnl_col = 'bright_green' if dpnl >= 0 else 'bright_red'
    tpnl_col = 'bright_green' if tpnl >= 0 else 'bright_red'

    body = Text()
    body.append(f'  ${bal:>12,.2f}\n', style='bold bright_white')
    body.append('  Day  ', style='dim')
    body.append(f'{dpnl:+,.2f}', style=f'bold {dpnl_col}')
    body.append('   Total  ', style='dim')
    body.append(f'{tpnl:+,.2f}\n', style=f'bold {tpnl_col}')

    if len(vals) < 3:
        body.append('\n  Collecting data…\n', style='dim')
        body.append('  Chart appears after\n', style='dim')
        body.append('  30 seconds\n', style='dim')
        return Panel(body, title='[bold bright_green]BALANCE[/]',
                     border_style='green', expand=True)

    lo  = min(vals)
    hi  = max(vals)
    rng = hi - lo

    # Downsample to fit width (~32 chars)
    w       = 32
    step    = max(1, len(vals) // w)
    sampled = vals[::step][-w:]

    # Build sparkline
    spark = Text()
    start_val = vals[0]
    for v in sampled:
        idx = int((v - lo) / rng * 7) if rng > 0.001 else 3
        col = 'bright_green' if v >= start_val else 'bright_red'
        spark.append(_SPARK[idx], style=col)

    # Session change %
    ses_pct = (vals[-1] - vals[0]) / vals[0] * 100 if vals[0] else 0
    ses_col = 'bright_green' if ses_pct >= 0 else 'bright_red'

    # High/low in session
    body.append(f'\n  Hi  ${hi:>10,.2f}\n', style='dim green')
    body.append(f'  Lo  ${lo:>10,.2f}\n\n', style='dim red')

    # Sparkline
    body.append('  ', style='')
    body.append_text(spark)
    body.append('\n', style='')
    body.append('  ', style='dim')
    body.append(f'{"←60min":<16}{"now→":>16}\n', style='dim')

    body.append(f'\n  Session  ', style='dim')
    body.append(f'{ses_pct:+.3f}%', style=f'bold {ses_col}')

    n = len(vals)
    mins = int((now - _balance_history[0][0]) / 60) if _balance_history else 0
    body.append(f'   [{n}pts/{mins}m]\n', style='dim')

    title_col = 'bright_green' if ses_pct >= 0 else 'bright_red'
    title = (
        f'[bold bright_green]BALANCE[/]  '
        f'[{title_col}]{ses_pct:+.2f}%[/{title_col}]'
    )
    return Panel(body, title=title, border_style='green', expand=True)


# ─── Footer ───────────────────────────────────────────────────────────────────
def _footer() -> Rule:
    mode = 'POLY' if POLY_MODE else 'BINANCE'
    return Rule(
        f'[dim]  [bold white]Q[/] Quit   '
        f'[bold white]R[/] Restart Bot   '
        f'[bold white]B[/] Browser   '
        f'[bold white]P[/] Pause/Resume   '
        f'|  {mode}   ws://127.0.0.1:{"8766" if POLY_MODE else "8765"}[/]',
        style='bright_black',
    )

# ─── Layout builder ───────────────────────────────────────────────────────────
def _build(S: dict, frame: int) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name='header',  size=5),
        Layout(name='body'),
        Layout(name='trades',  size=12),
        Layout(name='footer',  size=1),
    )
    layout['body'].split_row(
        Layout(name='positions', ratio=7),
        Layout(name='scanner',   ratio=5),
        Layout(name='right_col', ratio=4),
    )
    layout['body']['right_col'].split_column(
        Layout(name='gauges', size=5),
        Layout(name='market', ratio=3),
        Layout(name='chart',  ratio=2),
    )
    layout['header'].update(_header(S))
    layout['body']['positions'].update(_positions(S))
    layout['body']['scanner'].update(_scanner(S, frame))
    layout['body']['right_col']['gauges'].update(_gauges(S))
    layout['body']['right_col']['market'].update(_market(S, frame))
    layout['body']['right_col']['chart'].update(_balance_chart(S))
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
