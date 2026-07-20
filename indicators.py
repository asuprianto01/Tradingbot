"""
Technical indicators, implemented in plain Python (no heavy dependencies).

All functions take a list of candle dicts ({t, o, h, l, c, v}, oldest first)
or a list of floats, and return a list aligned with the input (None where the
indicator has not "warmed up" yet).
"""


def closes(candles):
    return [c["c"] for c in candles]


def sma(values, period):
    out = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values, period=14):
    """Wilder's RSI."""
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100 - 100 / (1 + (avg_gain / avg_loss)) if avg_loss else 100.0
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100 - 100 / (1 + (avg_gain / avg_loss)) if avg_loss else 100.0
    return out


def macd(values, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    # Signal line: EMA of the macd line, computed over its non-None tail.
    first = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line = [None] * len(values)
    if first is not None:
        tail_signal = ema(macd_line[first:], signal)
        for j, v in enumerate(tail_signal):
            signal_line[first + j] = v
    hist = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, hist


def bollinger(values, period=20, num_std=2.0):
    """Returns (upper, middle, lower)."""
    mid = sma(values, period)
    upper = [None] * len(values)
    lower = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        mean = mid[i]
        variance = sum((v - mean) ** 2 for v in window) / period
        std = variance ** 0.5
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return upper, mid, lower


def atr(candles, period=14):
    """Wilder's Average True Range."""
    out = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    prev = sum(trs[:period]) / period
    out[period] = prev
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + trs[i - 1]) / period
        out[i] = prev
    return out


def stochastic(candles, k_period=14, d_period=3):
    """Returns (%K smoothed over 3, %D)."""
    raw_k = [None] * len(candles)
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1: i + 1]
        hh = max(c["h"] for c in window)
        ll = min(c["l"] for c in window)
        raw_k[i] = 100 * (candles[i]["c"] - ll) / (hh - ll) if hh != ll else 50.0
    k = _smooth(raw_k, 3)
    d = _smooth(k, d_period)
    return k, d


def _smooth(values, period):
    out = [None] * len(values)
    for i in range(len(values)):
        window = [v for v in values[max(0, i - period + 1): i + 1] if v is not None]
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def stochrsi(closes, period=14, k_smooth=3, d_smooth=3):
    """StochRSI (%K, %D) in 0-100 — the Stochastic oscillator of the RSI series.
    Matches TradingView 'Stoch RSI 14 14 3 3'. Causal: value at i uses closes<=i."""
    rsis = rsi(closes, period)
    raw = [None] * len(rsis)
    for i in range(len(rsis)):
        window = [v for v in rsis[max(0, i - period + 1): i + 1] if v is not None]
        if len(window) == period:
            lo, hi = min(window), max(window)
            raw[i] = 100 * (rsis[i] - lo) / (hi - lo) if hi != lo else 50.0
    k = _smooth(raw, k_smooth)
    d = _smooth(k, d_smooth)
    return k, d


def stochrsi_bullish_cross(closes, oversold=30):
    """True when StochRSI %K crosses above %D on the latest bar from an oversold
    reading (either side of the cross was below `oversold`)."""
    k, d = stochrsi(closes)
    kk = [v for v in k if v is not None]
    dd = [v for v in d if v is not None]
    if len(kk) < 2 or len(dd) < 2:
        return False
    kp, kn, dp, dn = kk[-2], kk[-1], dd[-2], dd[-1]
    return kp <= dp and kn > dn and (kp < oversold or kn < oversold)


def volume_sma(candles, period=20):
    return sma([c["v"] for c in candles], period)


def obv(candles):
    """
    On-Balance Volume: running total that adds the bar's volume on up-closes and
    subtracts it on down-closes. A rising OBV while price is flat signals quiet
    accumulation; a falling OBV while price rises warns of distribution.
    """
    out = [None] * len(candles)
    if not candles:
        return out
    total = 0.0
    out[0] = 0.0
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i - 1]["c"]:
            total += candles[i]["v"]
        elif candles[i]["c"] < candles[i - 1]["c"]:
            total -= candles[i]["v"]
        out[i] = total
    return out


def cmf(candles, period=20):
    """
    Chaikin Money Flow: volume weighted by where each close sits in its range,
    averaged over `period`. > 0 = net buying pressure (accumulation),
    < 0 = net selling pressure (distribution). Typically ranges about -0.25..0.25.
    """
    out = [None] * len(candles)
    mfv = []  # money-flow volume per bar
    for c in candles:
        rng = c["h"] - c["l"]
        mult = ((c["c"] - c["l"]) - (c["h"] - c["c"])) / rng if rng > 0 else 0.0
        mfv.append(mult * c["v"])
    for i in range(period - 1, len(candles)):
        vol_sum = sum(c["v"] for c in candles[i - period + 1: i + 1])
        out[i] = sum(mfv[i - period + 1: i + 1]) / vol_sum if vol_sum else 0.0
    return out


def obv_divergence(candles, obv_values, lookback=30):
    """
    Detect divergence between price and OBV over the last `lookback` bars by
    comparing the older half of the window to the recent half:

      * BULLISH — price prints a LOWER low but OBV prints a HIGHER low
        (selling exhausts; quiet accumulation under a falling price).
      * BEARISH — price prints a HIGHER high but OBV prints a LOWER high
        (buying exhausts; distribution under a rising price).

    Returns "BULLISH" / "BEARISH" / "NONE".
    """
    n = len(candles)
    if n < lookback or obv_values[-1] is None:
        return "NONE"
    start = n - lookback
    mid = start + lookback // 2
    older = range(start, mid)
    recent = range(mid, n)

    def ok(i):
        return obv_values[i] is not None

    # Bullish: lower low in price, higher low in OBV.
    io = min(older, key=lambda i: candles[i]["l"])
    ir = min(recent, key=lambda i: candles[i]["l"])
    if candles[ir]["l"] < candles[io]["l"] and ok(io) and ok(ir) and obv_values[ir] > obv_values[io]:
        return "BULLISH"

    # Bearish: higher high in price, lower high in OBV.
    jo = max(older, key=lambda i: candles[i]["h"])
    jr = max(recent, key=lambda i: candles[i]["h"])
    if candles[jr]["h"] > candles[jo]["h"] and ok(jo) and ok(jr) and obv_values[jr] < obv_values[jo]:
        return "BEARISH"

    return "NONE"


def swing_levels(candles, lookback=5):
    """
    Find swing highs/lows (fractal pivots): a high/low with `lookback` lower
    highs / higher lows on each side. Returns (resistance_levels, support_levels),
    most recent first.
    """
    highs, lows = [], []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i]["h"]
        l = candles[i]["l"]
        if all(candles[j]["h"] < h for j in range(i - lookback, i + lookback + 1) if j != i):
            highs.append((candles[i]["t"], h))
        if all(candles[j]["l"] > l for j in range(i - lookback, i + lookback + 1) if j != i):
            lows.append((candles[i]["t"], l))
    highs.sort(reverse=True)
    lows.sort(reverse=True)
    return [h for _, h in highs], [l for _, l in lows]


def nearest_levels(price, resistances, supports):
    """Nearest resistance above price and support below price."""
    res_above = min((r for r in resistances if r > price), default=None)
    sup_below = max((s for s in supports if s < price), default=None)
    return res_above, sup_below


def adx(candles, period=14):
    """Average Directional Index (trend strength indicator).
    
    Returns tuple: (adx_values, plus_di_values, minus_di_values)
    ADX > 25: strong trend
    ADX 20-25: trend developing
    ADX < 20: weak/no trend (choppy)
    """
    out_adx = [None] * len(candles)
    out_plus_di = [None] * len(candles)
    out_minus_di = [None] * len(candles)
    
    if len(candles) <= period * 2:
        return out_adx, out_plus_di, out_minus_di
    
    # Calculate True Range
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c["h"] - c["l"])
        else:
            prev = candles[i - 1]
            tr.append(max(c["h"] - c["l"], abs(c["h"] - prev["c"]), abs(c["l"] - prev["c"])))
    
    # Calculate +DM and -DM
    plus_dm = []
    minus_dm = []
    for i, c in enumerate(candles):
        if i == 0:
            plus_dm.append(0)
            minus_dm.append(0)
        else:
            prev = candles[i - 1]
            up_move = c["h"] - prev["h"]
            down_move = prev["l"] - c["l"]
            
            if up_move > down_move and up_move > 0:
                plus_dm.append(up_move)
            else:
                plus_dm.append(0)
                
            if down_move > up_move and down_move > 0:
                minus_dm.append(down_move)
            else:
                minus_dm.append(0)
    
    # Smooth TR, +DM, -DM using Wilder's smoothing
    smoothed_tr = [None] * len(tr)
    smoothed_plus_dm = [None] * len(plus_dm)
    smoothed_minus_dm = [None] * len(minus_dm)
    
    # Initial smoothing
    smoothed_tr[period - 1] = sum(tr[:period]) / period
    smoothed_plus_dm[period - 1] = sum(plus_dm[:period]) / period
    smoothed_minus_dm[period - 1] = sum(minus_dm[:period]) / period
    
    for i in range(period, len(tr)):
        smoothed_tr[i] = (smoothed_tr[i - 1] * (period - 1) + tr[i]) / period
        smoothed_plus_dm[i] = (smoothed_plus_dm[i - 1] * (period - 1) + plus_dm[i]) / period
        smoothed_minus_dm[i] = (smoothed_minus_dm[i - 1] * (period - 1) + minus_dm[i]) / period
    
    # Calculate +DI and -DI
    for i in range(period - 1, len(candles)):
        if smoothed_tr[i] and smoothed_tr[i] > 0:
            out_plus_di[i] = 100 * (smoothed_plus_dm[i] / smoothed_tr[i])
            out_minus_di[i] = 100 * (smoothed_minus_dm[i] / smoothed_tr[i])
    
    # Calculate DX and ADX
    dx = []
    for i in range(period - 1, len(candles)):
        if out_plus_di[i] is not None and out_minus_di[i] is not None:
            di_diff = abs(out_plus_di[i] - out_minus_di[i])
            di_sum = out_plus_di[i] + out_minus_di[i]
            if di_sum > 0:
                dx.append(100 * (di_diff / di_sum))
            else:
                dx.append(0)
        else:
            dx.append(0)
    
    # Smooth DX to get ADX
    if len(dx) >= period:
        adx = sum(dx[:period]) / period
        out_adx[period * 2 - 1] = adx
        for i in range(period, len(dx)):
            adx = (adx * (period - 1) + dx[i]) / period
            out_adx[period - 1 + i] = adx
    
    return out_adx, out_plus_di, out_minus_di


def candlestick_patterns(candles):
    """Detect basic candlestick patterns.
    
    Returns dict with pattern names as keys and boolean values for the last candle.
    Patterns: pin_bar_bullish, pin_bar_bearish, engulfing_bullish, engulfing_bearish
    """
    if len(candles) < 2:
        return {"pin_bar_bullish": False, "pin_bar_bearish": False,
                "engulfing_bullish": False, "engulfing_bearish": False}
    
    current = candles[-1]
    prev = candles[-2]
    
    body_size = abs(current["c"] - current["o"])
    total_range = current["h"] - current["l"]
    upper_wick = current["h"] - max(current["c"], current["o"])
    lower_wick = min(current["c"], current["o"]) - current["l"]
    
    # Pin bar: small body, long wick on one side
    is_pin_bullish = (body_size < total_range * 0.3 and 
                      lower_wick > total_range * 0.6 and 
                      current["c"] > current["o"])
    
    is_pin_bearish = (body_size < total_range * 0.3 and 
                      upper_wick > total_range * 0.6 and 
                      current["c"] < current["o"])
    
    # Engulfing: current body completely engulfs previous body
    prev_body_size = abs(prev["c"] - prev["o"])
    is_engulfing_bullish = (current["c"] > current["o"] and 
                            prev["c"] < prev["o"] and
                            current["o"] < prev["c"] and 
                            current["c"] > prev["o"] and
                            body_size > prev_body_size)
    
    is_engulfing_bearish = (current["c"] < current["o"] and 
                            prev["c"] > prev["o"] and
                            current["o"] > prev["c"] and 
                            current["c"] < prev["o"] and
                            body_size > prev_body_size)
    
    return {
        "pin_bar_bullish": is_pin_bullish,
        "pin_bar_bearish": is_pin_bearish,
        "engulfing_bullish": is_engulfing_bullish,
        "engulfing_bearish": is_engulfing_bearish
    }
