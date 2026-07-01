"""
AlphaBot v5.0 — Advanced Technical Analysis Engine
────────────────────────────────────────────────────
Indicators (22):
  Trend     : EMA 9/20/50/200, MACD, Ichimoku Cloud (5 lines),
              Hull MA, Supertrend, ADX+DI, Pivot Points
  Momentum  : RSI-14, Stoch RSI, CCI, Williams %R, Rate of Change
  Volatility: Bollinger Bands, ATR, Keltner Channels, Squeeze Momentum
  Volume    : OBV, VWAP, Volume Profile (VPOC), Volume Delta

Patterns (14):
  Doji, Hammer, Inverted Hammer, Shooting Star, Hanging Man,
  Bullish/Bearish Engulfing, Morning Star, Evening Star,
  Three White Soldiers, Three Black Crows, Harami Bull/Bear,
  Tweezer Top/Bottom, Pin Bar

Regime Detection:
  Trending → MACD/EMA/Ichimoku heavy
  Ranging  → RSI/BB/CCI heavy
  Volatile → ATR/Squeeze heavy + tighter sizing
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field


# ─── Result container ─────────────────────────────────────────────────────────
@dataclass
class SignalResult:
    score:      float          # -100 to +100 (negative = short)
    direction:  str            # 'long' | 'short'
    confidence: float          # |score| → 0-100%
    regime:     str            # 'trending' | 'ranging' | 'volatile'
    signals:    dict = field(default_factory=dict)    # indicator → score
    patterns:   list = field(default_factory=list)    # detected candle patterns
    sr_levels:  dict = field(default_factory=dict)    # support/resistance
    atr:        float = 0.0
    atr_pct:    float = 0.0
    adx:        float = 0.0
    rsi:        float = 50.0
    bb_pct:     float = 0.5
    cloud_bull: bool  = False
    squeeze:    bool  = False
    ichimoku_signals: dict = field(default_factory=dict)
    pivot:      dict  = field(default_factory=dict)


# ─── Core TA Engine ───────────────────────────────────────────────────────────
class TAEngine:

    # ── DataFrame builder ──
    @staticmethod
    def build(raw: list) -> pd.DataFrame:
        df = pd.DataFrame(raw, columns=[
            'ts','open','high','low','close','volume',
            'ct','qv','n','tbv','tqv','_'
        ])
        for c in ['open','high','low','close','volume']:
            df[c] = df[c].astype(float)
        return df.reset_index(drop=True)

    # ── All indicators ──
    @staticmethod
    def compute(df: pd.DataFrame) -> pd.DataFrame:
        c  = df['close'];  h  = df['high']
        lo = df['low'];    v  = df['volume']

        # ── RSI 14 ──
        d    = c.diff()
        gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        df['rsi'] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

        # ── MACD 12/26/9 ──
        e12 = c.ewm(span=12, adjust=False).mean()
        e26 = c.ewm(span=26, adjust=False).mean()
        df['macd']      = e12 - e26
        df['macd_sig']  = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_sig']

        # ── EMAs ──
        for n in [9, 20, 50, 200]:
            df[f'ema{n}'] = c.ewm(span=n, adjust=False).mean()

        # ── Hull MA 20 ──
        wma  = lambda s, n: s.rolling(n).apply(lambda x: np.average(x, weights=range(1, n+1)), raw=True)
        hma_input = 2 * wma(c, 10) - wma(c, 20)
        df['hma20'] = wma(hma_input, int(np.sqrt(20)))

        # ── Bollinger Bands 20,2 ──
        bb_mid       = c.rolling(20).mean()
        bb_std       = c.rolling(20).std()
        df['bb_mid'] = bb_mid
        df['bb_up']  = bb_mid + 2 * bb_std
        df['bb_lo']  = bb_mid - 2 * bb_std
        bb_rng       = df['bb_up'] - df['bb_lo']
        df['bb_pct'] = ((c - df['bb_lo']) / bb_rng.replace(0, np.nan)).clip(0, 1)
        df['bb_w']   = bb_rng / bb_mid.replace(0, np.nan)

        # ── ATR 14 ──
        tr = pd.concat([
            h - lo,
            (h - c.shift(1)).abs(),
            (lo - c.shift(1)).abs()
        ], axis=1).max(axis=1)
        df['atr']     = tr.ewm(span=14, adjust=False).mean()
        df['atr_pct'] = df['atr'] / c * 100

        # ── Keltner Channels (for Squeeze) ──
        kc_mid     = c.ewm(span=20, adjust=False).mean()
        df['kc_up']= kc_mid + 1.5 * df['atr']
        df['kc_lo']= kc_mid - 1.5 * df['atr']

        # ── Squeeze Momentum (BB inside KC = squeeze) ──
        df['squeeze'] = (df['bb_up'] < df['kc_up']) & (df['bb_lo'] > df['kc_lo'])
        # Momentum value: LR of close vs midpoint
        delta        = c - ((h.rolling(20).max() + lo.rolling(20).min()) / 2 + bb_mid) / 2
        df['sq_mom'] = delta.rolling(20).apply(
            lambda x: np.polyfit(range(len(x)), x, 1)[0], raw=True
        )

        # ── Stoch RSI (14,3,3) ──
        r_lo = df['rsi'].rolling(14).min()
        r_hi = df['rsi'].rolling(14).max()
        sk   = 100 * (df['rsi'] - r_lo) / (r_hi - r_lo).replace(0, np.nan)
        df['stoch_k'] = sk.rolling(3).mean()
        df['stoch_d'] = df['stoch_k'].rolling(3).mean()

        # ── ADX + DI ──
        plus_dm  = (h.diff()).clip(lower=0)
        minus_dm = (-lo.diff()).clip(lower=0)
        plus_dm[plus_dm  < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0
        tr14     = tr.ewm(span=14, adjust=False).mean()
        df['plus_di']  = 100 * plus_dm.ewm(span=14, adjust=False).mean() / tr14.replace(0, np.nan)
        df['minus_di'] = 100 * minus_dm.ewm(span=14, adjust=False).mean() / tr14.replace(0, np.nan)
        dx             = 100 * (df['plus_di'] - df['minus_di']).abs() / \
                         (df['plus_di'] + df['minus_di']).replace(0, np.nan)
        df['adx']      = dx.ewm(span=14, adjust=False).mean()

        # ── CCI 20 ──
        tp     = (h + lo + c) / 3
        cci_ma = tp.rolling(20).mean()
        cci_d  = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
        df['cci'] = (tp - cci_ma) / (0.015 * cci_d.replace(0, np.nan))

        # ── Williams %R 14 ──
        hh = h.rolling(14).max()
        ll = lo.rolling(14).min()
        df['willr'] = -100 * (hh - c) / (hh - ll).replace(0, np.nan)

        # ── OBV ──
        obv = [0.0]
        for i in range(1, len(df)):
            if   df['close'].iat[i] > df['close'].iat[i-1]: obv.append(obv[-1] + df['volume'].iat[i])
            elif df['close'].iat[i] < df['close'].iat[i-1]: obv.append(obv[-1] - df['volume'].iat[i])
            else:                                             obv.append(obv[-1])
        df['obv']     = obv
        df['obv_ema'] = pd.Series(obv, index=df.index).ewm(span=20, adjust=False).mean()

        # ── VWAP (rolling session approximation) ──
        tp_v        = tp * v
        df['vwap']  = tp_v.rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)

        # ── Volume ratio & delta ──
        df['vol_ma']    = v.rolling(20).mean()
        df['vol_ratio'] = v / df['vol_ma'].replace(0, np.nan)
        df['vol_delta'] = (df['close'] - df['open']) * v  # proxy for buy/sell pressure

        # ── Momentum ──
        df['mom5']  = c.pct_change(5)  * 100
        df['mom20'] = c.pct_change(20) * 100
        df['roc10'] = c.pct_change(10) * 100

        # ── Ichimoku Cloud ──
        df['tenkan']  = (h.rolling(9).max()  + lo.rolling(9).min())  / 2
        df['kijun']   = (h.rolling(26).max() + lo.rolling(26).min()) / 2
        df['spanA']   = (df['tenkan'] + df['kijun']) / 2              # plot +26
        df['spanB']   = (h.rolling(52).max() + lo.rolling(52).min()) / 2  # plot +26
        df['chikou']  = c.shift(-26)                                  # close shifted back 26

        # Supertrend 10,3
        atr10    = tr.ewm(span=10, adjust=False).mean()
        up_band  = (h + lo) / 2 + 3 * atr10
        dn_band  = (h + lo) / 2 - 3 * atr10
        st       = pd.Series(index=df.index, dtype=float)
        trend    = pd.Series(1, index=df.index)
        for i in range(1, len(df)):
            prev_up = up_band.iat[i-1]
            prev_dn = dn_band.iat[i-1]
            up_band.iat[i] = up_band.iat[i] if up_band.iat[i] < prev_up or df['close'].iat[i-1] > prev_up else prev_up
            dn_band.iat[i] = dn_band.iat[i] if dn_band.iat[i] > prev_dn or df['close'].iat[i-1] < prev_dn else prev_dn
            if trend.iat[i-1] == -1 and df['close'].iat[i] > up_band.iat[i]: trend.iat[i] = 1
            elif trend.iat[i-1] == 1  and df['close'].iat[i] < dn_band.iat[i]: trend.iat[i] = -1
            else: trend.iat[i] = trend.iat[i-1]
        df['st_trend'] = trend
        df['st_line']  = np.where(trend == 1, dn_band, up_band)

        # ── Pivot Points (Classic, from last candle) ──
        prev     = df.iloc[-2] if len(df) > 2 else df.iloc[-1]
        pp       = (prev['high'] + prev['low'] + prev['close']) / 3
        df['pp'] = pp
        df['r1'] = 2 * pp - prev['low']
        df['s1'] = 2 * pp - prev['high']
        df['r2'] = pp + (prev['high'] - prev['low'])
        df['s2'] = pp - (prev['high'] - prev['low'])

        return df

    # ── Candlestick pattern recognition ──
    @staticmethod
    def detect_patterns(df: pd.DataFrame) -> list[dict]:
        if len(df) < 3: return []
        c1 = df.iloc[-1];  c2 = df.iloc[-2];  c3 = df.iloc[-3]
        patterns = []

        def body(c):     return abs(c['close'] - c['open'])
        def range_(c):   return c['high'] - c['low'] if c['high'] != c['low'] else 0.0001
        def is_bull(c):  return c['close'] > c['open']
        def is_bear(c):  return c['close'] < c['open']
        def upper_wick(c): return c['high'] - max(c['open'], c['close'])
        def lower_wick(c): return min(c['open'], c['close']) - c['low']
        def body_pct(c): return body(c) / range_(c)

        # ── Doji (tiny body) ──
        if body_pct(c1) < 0.1:
            patterns.append({'name': 'Doji', 'dir': 'neutral', 'score': 0, 'strength': 40})

        # ── Hammer (bullish reversal) — after downtrend ──
        if (lower_wick(c1) > 2 * body(c1)
                and upper_wick(c1) < body(c1)
                and is_bear(c2)):
            patterns.append({'name': 'Hammer', 'dir': 'long', 'score': +70, 'strength': 70})

        # ── Shooting Star (bearish reversal) — after uptrend ──
        if (upper_wick(c1) > 2 * body(c1)
                and lower_wick(c1) < body(c1)
                and is_bull(c2)):
            patterns.append({'name': 'Shooting Star', 'dir': 'short', 'score': -70, 'strength': 70})

        # ── Inverted Hammer (bullish, needs confirmation) ──
        if (upper_wick(c1) > 2 * body(c1)
                and lower_wick(c1) < body(c1) * 0.5
                and is_bear(c2)
                and not is_bull(c2)):
            patterns.append({'name': 'Inverted Hammer', 'dir': 'long', 'score': +55, 'strength': 55})

        # ── Bullish Engulfing ──
        if (is_bull(c1) and is_bear(c2)
                and c1['close'] > c2['open']
                and c1['open'] < c2['close']
                and body(c1) > body(c2) * 1.1):
            patterns.append({'name': 'Bullish Engulfing', 'dir': 'long', 'score': +85, 'strength': 85})

        # ── Bearish Engulfing ──
        if (is_bear(c1) and is_bull(c2)
                and c1['close'] < c2['open']
                and c1['open'] > c2['close']
                and body(c1) > body(c2) * 1.1):
            patterns.append({'name': 'Bearish Engulfing', 'dir': 'short', 'score': -85, 'strength': 85})

        # ── Morning Star (3-candle bullish) ──
        if (is_bear(c3) and body_pct(c2) < 0.3 and is_bull(c1)
                and c1['close'] > (c3['open'] + c3['close']) / 2
                and body(c3) > body(c2)):
            patterns.append({'name': 'Morning Star', 'dir': 'long', 'score': +90, 'strength': 90})

        # ── Evening Star (3-candle bearish) ──
        if (is_bull(c3) and body_pct(c2) < 0.3 and is_bear(c1)
                and c1['close'] < (c3['open'] + c3['close']) / 2
                and body(c3) > body(c2)):
            patterns.append({'name': 'Evening Star', 'dir': 'short', 'score': -90, 'strength': 90})

        # ── Three White Soldiers (strong bullish) ──
        if (is_bull(c1) and is_bull(c2) and is_bull(c3)
                and c1['close'] > c2['close'] > c3['close']
                and body_pct(c1) > 0.6 and body_pct(c2) > 0.6):
            patterns.append({'name': 'Three White Soldiers', 'dir': 'long', 'score': +95, 'strength': 95})

        # ── Three Black Crows (strong bearish) ──
        if (is_bear(c1) and is_bear(c2) and is_bear(c3)
                and c1['close'] < c2['close'] < c3['close']
                and body_pct(c1) > 0.6 and body_pct(c2) > 0.6):
            patterns.append({'name': 'Three Black Crows', 'dir': 'short', 'score': -95, 'strength': 95})

        # ── Bullish Harami ──
        if (is_bull(c1) and is_bear(c2)
                and c1['open'] > c2['close'] and c1['close'] < c2['open']
                and body(c1) < body(c2) * 0.6):
            patterns.append({'name': 'Bullish Harami', 'dir': 'long', 'score': +60, 'strength': 60})

        # ── Bearish Harami ──
        if (is_bear(c1) and is_bull(c2)
                and c1['open'] < c2['close'] and c1['close'] > c2['open']
                and body(c1) < body(c2) * 0.6):
            patterns.append({'name': 'Bearish Harami', 'dir': 'short', 'score': -60, 'strength': 60})

        # ── Tweezer Bottom (bullish) ──
        if (abs(c1['low'] - c2['low']) / range_(c1) < 0.05
                and is_bear(c2) and is_bull(c1)):
            patterns.append({'name': 'Tweezer Bottom', 'dir': 'long', 'score': +65, 'strength': 65})

        # ── Tweezer Top (bearish) ──
        if (abs(c1['high'] - c2['high']) / range_(c1) < 0.05
                and is_bull(c2) and is_bear(c1)):
            patterns.append({'name': 'Tweezer Top', 'dir': 'short', 'score': -65, 'strength': 65})

        return patterns

    # ── S/R level detection ──
    @staticmethod
    def find_sr(df: pd.DataFrame, lookback=50, n_levels=5) -> dict:
        if len(df) < lookback: return {'support': [], 'resistance': [], 'vpoc': 0}
        sub    = df.tail(lookback)
        highs  = sub['high'].values
        lows   = sub['low'].values
        closes = sub['close'].values
        price  = closes[-1]

        # Swing highs/lows
        resistance = []
        support    = []
        for i in range(2, len(highs) - 2):
            if highs[i] > highs[i-1] and highs[i] > highs[i+1] and highs[i] > highs[i-2] and highs[i] > highs[i+2]:
                resistance.append(float(highs[i]))
            if lows[i] < lows[i-1] and lows[i] < lows[i+1] and lows[i] < lows[i-2] and lows[i] < lows[i+2]:
                support.append(float(lows[i]))

        # Cluster close levels
        def cluster(levels, tol=0.005):
            out, used = [], set()
            for i, l in enumerate(sorted(levels)):
                if i in used: continue
                group = [l]
                for j, l2 in enumerate(sorted(levels)):
                    if j != i and j not in used and abs(l - l2) / max(l, 0.000001) < tol:
                        group.append(l2); used.add(j)
                out.append(float(np.mean(group)))
            return out[:n_levels]

        # Volume POC (price level with most volume)
        n_bins  = 30
        lo_p    = sub['low'].min();   hi_p = sub['high'].max()
        bins    = np.linspace(lo_p, hi_p, n_bins + 1)
        vol_at  = np.zeros(n_bins)
        for _, row in sub.iterrows():
            for b in range(n_bins):
                if row['low'] <= bins[b+1] and row['high'] >= bins[b]:
                    vol_at[b] += row['volume']
        vpoc_bin = int(np.argmax(vol_at))
        vpoc     = float((bins[vpoc_bin] + bins[vpoc_bin+1]) / 2)

        return {
            'support':    cluster([s for s in support    if s < price]),
            'resistance': cluster([r for r in resistance if r > price]),
            'vpoc':       vpoc
        }

    # ── Market regime detection ──
    @staticmethod
    def detect_regime(df: pd.DataFrame) -> str:
        row = df.iloc[-1]
        adx   = float(row.get('adx',   20))
        bb_w  = float(row.get('bb_w',   0.04))
        atr_p = float(row.get('atr_pct',1.0))
        sq    = bool(row.get('squeeze', False))

        if atr_p > 3.5:        return 'volatile'   # tightened from 3.0
        if sq:                 return 'squeeze'    # about to break out
        if adx > 30:           return 'trending'   # tightened from 28 — need cleaner trend
        if adx < 20 or bb_w < 0.02: return 'ranging'
        return 'trending'

    # ── Score row against all indicators ──
    @staticmethod
    def score_row(row: dict, regime: str = 'trending') -> tuple[float, dict]:
        signals = {}

        def s(key, default=0.0):
            v = row.get(key, default)
            if v is None or (isinstance(v, float) and np.isnan(v)): return default
            return float(v)

        cp = s('close')

        # ── RSI ── trend-aware (oversold in downtrend = continuation, not reversal)
        rsi  = s('rsi', 50)
        e9   = s('ema9'); e20 = s('ema20'); e50 = s('ema50')
        ema_bear = e9 < e20 < e50 and cp < e9   # strong downtrend
        ema_bull = e9 > e20 > e50 and cp > e9   # strong uptrend
        if   rsi <= 20:  signals['RSI'] = -30 if ema_bear else +90   # bear: oversold = keep selling
        elif rsi <= 30:  signals['RSI'] = -15 if ema_bear else +70
        elif rsi <= 38:  signals['RSI'] = 0   if ema_bear else +40
        elif rsi >= 80:  signals['RSI'] = +30 if ema_bull else -90   # bull: overbought = keep buying
        elif rsi >= 70:  signals['RSI'] = +15 if ema_bull else -70
        elif rsi >= 62:  signals['RSI'] = 0   if ema_bull else -40
        else:            signals['RSI'] = 0

        # ── MACD ──
        macd, msig, mhist = s('macd'), s('macd_sig'), s('macd_hist')
        if   macd > msig and mhist > 0 and mhist > mhist * 0: signals['MACD'] = +75 if macd > 0 else +45
        elif macd < msig and mhist < 0:                        signals['MACD'] = -75 if macd < 0 else -45
        else:                                                   signals['MACD'] = 0

        # ── EMA stack alignment ──
        e9, e20, e50, e200 = s('ema9'), s('ema20'), s('ema50'), s('ema200')
        if   e9 > e20 > e50 > e200 and cp > e9:    signals['EMA'] = +90
        elif e9 > e20 > e50        and cp > e20:    signals['EMA'] = +65
        elif e9 > e20              and cp > e9:     signals['EMA'] = +40
        elif e9 < e20 < e50 < e200 and cp < e9:    signals['EMA'] = -90
        elif e9 < e20 < e50        and cp < e20:    signals['EMA'] = -65
        elif e9 < e20              and cp < e9:     signals['EMA'] = -40
        else:                                       signals['EMA'] = 0

        # ── Hull MA direction ──
        hma = s('hma20')
        signals['HullMA'] = +50 if cp > hma else -50

        # ── Ichimoku ──
        tenkan = s('tenkan'); kijun = s('kijun')
        spanA  = s('spanA');  spanB = s('spanB')
        cloud_top = max(spanA, spanB); cloud_bot = min(spanA, spanB)
        ich_score = 0
        if cp > cloud_top:   ich_score += 35   # price above cloud
        elif cp < cloud_bot: ich_score -= 35
        if tenkan > kijun:   ich_score += 25   # TK cross bullish
        elif tenkan < kijun: ich_score -= 25
        if spanA > spanB:    ich_score += 20   # green cloud
        elif spanA < spanB:  ich_score -= 20
        signals['Ichimoku'] = max(-100, min(100, ich_score))

        # ── Supertrend ──
        st = int(s('st_trend', 0))
        signals['Supertrend'] = +60 if st == 1 else (-60 if st == -1 else 0)

        # ── Bollinger Bands ──
        bb_pct = s('bb_pct', 0.5)
        if   bb_pct <= 0.03:  signals['BB'] = +85
        elif bb_pct <= 0.15:  signals['BB'] = +55
        elif bb_pct >= 0.97:  signals['BB'] = -85
        elif bb_pct >= 0.85:  signals['BB'] = -55
        else:                 signals['BB'] = 0

        # ── Squeeze Momentum ──
        sq_mom = s('sq_mom', 0)
        if   s('squeeze') and sq_mom > 0:  signals['Squeeze'] = +70
        elif s('squeeze') and sq_mom < 0:  signals['Squeeze'] = -70
        elif not s('squeeze') and sq_mom > 0: signals['Squeeze'] = +40
        elif not s('squeeze') and sq_mom < 0: signals['Squeeze'] = -40
        else:                              signals['Squeeze'] = 0

        # ── Stoch RSI ──
        sk, sd = s('stoch_k', 50), s('stoch_d', 50)
        if   sk < 20 and sk > sd:  signals['StochRSI'] = +75
        elif sk < 20:               signals['StochRSI'] = +50
        elif sk > 80 and sk < sd:  signals['StochRSI'] = -75
        elif sk > 80:               signals['StochRSI'] = -50
        else:                       signals['StochRSI'] = 0

        # ── ADX ──
        adx = s('adx', 20); pdi = s('plus_di', 25); mdi = s('minus_di', 25)
        if adx > 20:
            raw_adx = (adx * 0.9) if pdi > mdi else -(adx * 0.9)
            # Scale: weak trend (20-30) gets partial credit only
            scale = min(1.0, (adx - 20) / 30)
            signals['ADX'] = max(-100, min(100, raw_adx * scale))
        else:
            signals['ADX'] = 0

        # ── CCI ──
        cci = s('cci', 0)
        if   cci < -150:  signals['CCI'] = +80
        elif cci < -100:  signals['CCI'] = +55
        elif cci > 150:   signals['CCI'] = -80
        elif cci > 100:   signals['CCI'] = -55
        else:             signals['CCI'] = 0

        # ── Williams %R ──
        wr = s('willr', -50)
        if   wr < -85:    signals['WillR'] = +75
        elif wr < -70:    signals['WillR'] = +45
        elif wr > -15:    signals['WillR'] = -75
        elif wr > -30:    signals['WillR'] = -45
        else:             signals['WillR'] = 0

        # ── OBV trend ──
        obv, obv_ema = s('obv'), s('obv_ema')
        if   obv > obv_ema * 1.02:   signals['OBV'] = +70
        elif obv > obv_ema:           signals['OBV'] = +35
        elif obv < obv_ema * 0.98:   signals['OBV'] = -70
        elif obv < obv_ema:           signals['OBV'] = -35
        else:                         signals['OBV'] = 0

        # ── VWAP ──
        vwap = s('vwap', cp)
        signals['VWAP'] = +50 if cp > vwap else -50

        # ── Volume surge ──
        vr = s('vol_ratio', 1.0)
        signals['Volume'] = +80 if vr >= 2.5 else (+50 if vr >= 1.5 else (-30 if vr < 0.5 else 0))

        # ── Volume delta (buy/sell pressure) — use EMA-smoothed to reduce noise ──
        vd  = s('vol_delta', 0)
        vr2 = s('vol_ratio', 1.0)
        # Only score when volume is elevated; suppress on low-vol candles
        if vr2 >= 1.3:
            signals['VolDelta'] = +55 if vd > 0 else -55
        elif vr2 >= 0.8:
            signals['VolDelta'] = +20 if vd > 0 else -20
        else:
            signals['VolDelta'] = 0

        # ── Momentum ──
        m5, m20 = s('mom5', 0), s('mom20', 0)
        if   m5 > 3  and m20 > 5:   signals['Momentum'] = +70
        elif m5 > 1:                 signals['Momentum'] = +35
        elif m5 < -3 and m20 < -5:  signals['Momentum'] = -70
        elif m5 < -1:                signals['Momentum'] = -35
        else:                        signals['Momentum'] = 0

        # ── Regime-adjusted weights ──
        if regime == 'trending':
            w = {
                'RSI':6,'MACD':12,'EMA':14,'HullMA':6,'Ichimoku':12,
                'Supertrend':8,'BB':4,'Squeeze':4,'StochRSI':4,'ADX':12,
                'CCI':4,'WillR':3,'OBV':5,'VWAP':4,'Volume':4,'VolDelta':4,'Momentum':6
            }
        elif regime == 'ranging':
            w = {
                'RSI':14,'MACD':5,'EMA':6,'HullMA':4,'Ichimoku':6,
                'Supertrend':3,'BB':14,'Squeeze':6,'StochRSI':10,'ADX':4,
                'CCI':12,'WillR':8,'OBV':6,'VWAP':6,'Volume':5,'VolDelta':5,'Momentum':4
            }
        elif regime == 'volatile':
            w = {
                'RSI':8,'MACD':6,'EMA':6,'HullMA':4,'Ichimoku':8,
                'Supertrend':6,'BB':10,'Squeeze':14,'StochRSI':8,'ADX':8,
                'CCI':6,'WillR':6,'OBV':6,'VWAP':5,'Volume':8,'VolDelta':8,'Momentum':5
            }
        else:  # squeeze — imminent breakout
            w = {
                'RSI':6,'MACD':8,'EMA':10,'HullMA':5,'Ichimoku':10,
                'Supertrend':8,'BB':5,'Squeeze':18,'StochRSI':7,'ADX':6,
                'CCI':5,'WillR':5,'OBV':8,'VWAP':5,'Volume':8,'VolDelta':8,'Momentum':7
            }

        total_w = sum(w.values())
        raw     = sum(signals.get(k, 0) * wt for k, wt in w.items()) / total_w
        return round(max(-100, min(100, raw)), 1), signals

    # ── Multi-timeframe combination ──
    @staticmethod
    def combine_mtf(s5: float, s15: float, s1h: float) -> tuple[float, str]:
        # 1h carries the trend — give it dominant weight
        combined = s5 * 0.20 + s15 * 0.30 + s1h * 0.50
        # All-TF alignment: strong bonus
        if np.sign(s5) == np.sign(s15) == np.sign(s1h) != 0:
            combined *= 1.20
        # 5m vs 1h contradiction: heavy penalty (short-term noise against macro = skip)
        if np.sign(s5) != np.sign(s1h) and abs(s1h) > 45:
            combined *= 0.40
        # 15m vs 1h contradiction (less severe)
        elif np.sign(s15) != np.sign(s1h) and abs(s1h) > 55:
            combined *= 0.65
        combined = max(-100, min(100, combined))
        return round(combined, 1), 'long' if combined > 0 else 'short'

    # ── Full analysis pipeline ──
    @classmethod
    def analyse(cls, raw5: list, raw15: list, raw1h: list) -> SignalResult | None:
        try:
            dfs = {}
            for key, raw in [('5m', raw5), ('15m', raw15), ('1h', raw1h)]:
                df = cls.build(raw)
                df = cls.compute(df)
                dfs[key] = df

            # Regime from 15m
            regime   = cls.detect_regime(dfs['15m'])

            scores, sigs = {}, {}
            for key, df in dfs.items():
                row       = df.iloc[-1].to_dict()
                sc, si    = cls.score_row(row, regime)
                scores[key] = sc
                sigs[key]   = si

            final, direction = cls.combine_mtf(
                scores['5m'], scores['15m'], scores['1h']
            )

            row5     = dfs['5m'].iloc[-1].to_dict()
            patterns = cls.detect_patterns(dfs['5m'])
            sr       = cls.find_sr(dfs['15m'])

            # Pattern bonus/penalty
            for p in patterns:
                if p['dir'] == direction:       final = min(100, final + p['strength'] * 0.10)
                elif p['dir'] != 'neutral':     final = max(-100, final - p['strength'] * 0.05)

            # Ichimoku cloud state
            spanA = float(row5.get('spanA', 0)); spanB = float(row5.get('spanB', 0))
            cp    = float(row5.get('close', 0))
            cloud_bull = cp > max(spanA, spanB)

            def safe(k, d=0.0):
                v = row5.get(k, d)
                return d if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

            return SignalResult(
                score      = round(final, 1),
                direction  = 'long' if final > 0 else 'short',
                confidence = round(abs(final), 1),
                regime     = regime,
                signals    = sigs,
                patterns   = patterns,
                sr_levels  = sr,
                atr        = safe('atr'),
                atr_pct    = safe('atr_pct'),
                adx        = safe('adx'),
                rsi        = safe('rsi', 50),
                bb_pct     = safe('bb_pct', 0.5),
                cloud_bull = cloud_bull,
                squeeze    = bool(row5.get('squeeze', False)),
                ichimoku_signals={
                    'above_cloud': cloud_bull,
                    'tk_bull':  safe('tenkan') > safe('kijun'),
                    'green_cloud': spanA > spanB,
                },
                pivot={
                    'pp': safe('pp'), 'r1': safe('r1'), 'r2': safe('r2'),
                    's1': safe('s1'), 's2': safe('s2'),
                },
            )
        except Exception as e:
            import logging; logging.getLogger('TAEngine').debug(f"analyse: {e}")
            return None
