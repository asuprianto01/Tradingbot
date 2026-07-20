"""
Professional-style market analysis engine.

Approach (top-down, the way a discretionary pro works a chart):
  1. Higher timeframe (default 4h) establishes the TREND — we only want to
     trade with it, never against it.
  2. Entry timeframe (default 1h) provides TIMING via a weighted confluence
     of momentum, mean-reversion, volume and structure signals.
  3. Structure (swing-based support/resistance) sanity-checks the trade:
     no longs straight into resistance, no shorts straight into support.

Every factor contributes a weighted score in [-1, +1]; the aggregate maps to
STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL with a written rationale,
so you can always audit *why* the bot wants to act.
"""

import logging
import indicators as ta

log = logging.getLogger("strategy")

SIGNAL_STRONG_BUY = "STRONG_BUY"
SIGNAL_BUY = "BUY"
SIGNAL_NEUTRAL = "NEUTRAL"
SIGNAL_SELL = "SELL"
SIGNAL_STRONG_SELL = "STRONG_SELL"


class Analysis:
    def __init__(self, instrument, signal, score, price, atr_value, trend, factors,
                 nearest_resistance, nearest_support,
                 resistance_levels=None, support_levels=None, accumulation="NEUTRAL",
                 divergence="NONE", high=None, low=None,
                 srsi_dip=False, smart_money_dip=False, turnover=None):
        self.instrument = instrument
        self.signal = signal
        self.score = score              # -1.0 .. +1.0
        self.price = price
        self.atr = atr_value
        self.trend = trend              # "UP" / "DOWN" / "SIDEWAYS"
        self.factors = factors          # list of (name, score, weight, comment)
        self.nearest_resistance = nearest_resistance
        self.nearest_support = nearest_support
        # Full swing levels (for multi-target take-profits, etc.)
        self.resistance_levels = resistance_levels or []
        self.support_levels = support_levels or []
        self.accumulation = accumulation   # "ACCUMULATION" / "NEUTRAL" / "DISTRIBUTION"
        self.divergence = divergence       # "BULLISH" / "BEARISH" / "NONE"
        self.high = high if high is not None else price   # high bar terakhir
        self.low = low if low is not None else price      # low bar terakhir
        self.srsi_dip = srsi_dip                # StochRSI bullish cross dari oversold
        self.smart_money_dip = smart_money_dip  # True bila sinyal ini dari jalur Smart-Money Dip
        self.turnover = turnover                # median nilai transaksi/bar (Rp) — likuiditas

    def to_dict(self):
        """Serialisasi ke dict JSON-able (untuk menyimpan hasil scan ke disk)."""
        return {
            "instrument": self.instrument, "signal": self.signal, "score": self.score,
            "price": self.price, "atr": self.atr, "trend": self.trend,
            "factors": [list(f) for f in self.factors],
            "nearest_resistance": self.nearest_resistance,
            "nearest_support": self.nearest_support,
            "resistance_levels": self.resistance_levels,
            "support_levels": self.support_levels,
            "accumulation": self.accumulation, "divergence": self.divergence,
            "high": self.high, "low": self.low,
            "srsi_dip": self.srsi_dip, "smart_money_dip": self.smart_money_dip,
            "turnover": self.turnover,
        }

    @staticmethod
    def from_dict(d):
        return Analysis(
            d["instrument"], d["signal"], d["score"], d["price"], d["atr"], d["trend"],
            [tuple(f) for f in d.get("factors", [])],
            d.get("nearest_resistance"), d.get("nearest_support"),
            resistance_levels=d.get("resistance_levels"),
            support_levels=d.get("support_levels"),
            accumulation=d.get("accumulation", "NEUTRAL"),
            divergence=d.get("divergence", "NONE"),
            high=d.get("high"), low=d.get("low"),
            srsi_dip=d.get("srsi_dip", False),
            smart_money_dip=d.get("smart_money_dip", False),
            turnover=d.get("turnover"))

    def report(self):
        lines = [
            f"=== {self.instrument} @ {self.price:,.6g} ===",
            f"Higher-TF trend: {self.trend}   |   ATR: {self.atr:,.6g}",
            f"Signal: {self.signal}   (confluence score {self.score:+.2f})",
        ]
        if self.smart_money_dip:
            lines.append("  -> Smart-Money Dip (STRONG): tren UP + StochRSI oversold cross + akumulasi")
        lines += ["", "Factor breakdown:"]
        for name, score, weight, comment in self.factors:
            lines.append(f"  [{score:+.2f} x{weight:.1f}] {name:<18} {comment}")
        if self.nearest_support or self.nearest_resistance:
            sup = f"{self.nearest_support:,.6g}" if self.nearest_support else "n/a"
            res = f"{self.nearest_resistance:,.6g}" if self.nearest_resistance else "n/a"
            lines.append("")
            lines.append(f"Structure: support {sup}  /  resistance {res}")
        return "\n".join(lines)


def _last(series):
    for v in reversed(series):
        if v is not None:
            return v
    return None


def timeframe_trend(candles):
    """Public: classify a timeframe's trend (UP/DOWN/SIDEWAYS) from its candles.
    Used for multi-timeframe confirmation."""
    return _trend_from_emas(candles)


def _trend_from_emas(candles, slope_threshold=0.001):
    """Classify higher-timeframe trend using EMA 20/50/200 alignment + slope."""
    cl = ta.closes(candles)
    e20, e50, e200 = ta.ema(cl, 20), ta.ema(cl, 50), ta.ema(cl, 200)
    last20, last50, last200 = _last(e20), _last(e50), _last(e200)
    price = cl[-1]
    if last20 is None or last50 is None:
        return "SIDEWAYS"
    # Slope of EMA50 over the last 10 bars
    prior50 = e50[-11] if len(e50) > 11 and e50[-11] is not None else last50
    slope = (last50 - prior50) / prior50 if prior50 else 0
    bullish = price > last50 and last20 > last50 and slope > slope_threshold
    bearish = price < last50 and last20 < last50 and slope < -slope_threshold
    if last200 is not None:
        bullish = bullish and price > last200
        bearish = bearish and price < last200
    if bullish:
        return "UP"
    if bearish:
        return "DOWN"
    return "SIDEWAYS"


def detect_market_regime(candles, adx_strong_threshold=25, adx_developing_threshold=20):
    """Detect overall market regime: BULL, BEAR, or CHOPPY.
    
    Uses:
    - Price position relative to 200 EMA (bull/bear bias)
    - ADX strength (trending vs choppy)
    - EMA alignment (trend confirmation)
    
    Returns: "BULL", "BEAR", or "CHOPPY"
    """
    cl = ta.closes(candles)
    e20, e50, e200 = ta.ema(cl, 20), ta.ema(cl, 50), ta.ema(cl, 200)
    last20, last50, last200 = _last(e20), _last(e50), _last(e200)
    price = cl[-1]
    
    if last200 is None:
        return "CHOPPY"
    
    # Check ADX for trend strength
    adx_series, _, _ = ta.adx(candles, 14)
    adx_now = _last(adx_series)
    
    # If ADX is low, market is choppy regardless of EMA alignment
    if adx_now is not None and adx_now < adx_developing_threshold:
        return "CHOPPY"
    
    # Determine bias from 200 EMA
    if price > last200:
        bias = "BULL"
    elif price < last200:
        bias = "BEAR"
    else:
        return "CHOPPY"
    
    # Confirm with EMA alignment
    if bias == "BULL" and last20 > last50 > last200 and adx_now >= adx_strong_threshold:
        return "BULL"
    elif bias == "BEAR" and last20 < last50 < last200 and adx_now >= adx_strong_threshold:
        return "BEAR"
    
    # If ADX is developing but not strong, still consider choppy
    if adx_now is not None and adx_now < adx_strong_threshold:
        return "CHOPPY"
    
    # Default to bias if ADX is strong enough
    return bias


def analyze(instrument, entry_candles, trend_candles, config=None):
    """
    entry_candles: candles on the entry timeframe (e.g. 1h), oldest first.
    trend_candles: candles on the higher timeframe (e.g. 4h).
    config: optional dict with strategy thresholds (defaults to hardcoded values).
    """
    # Load config or use defaults
    if config is None:
        config = {}
    strat_cfg = config.get("strategy", {})
    
    # Threshold defaults
    ema_slope_threshold = strat_cfg.get("ema_slope_threshold", 0.001)
    rsi_oversold = strat_cfg.get("rsi_oversold", 30)
    rsi_oversold_bound = strat_cfg.get("rsi_oversold_bound", 45)
    rsi_overbought_bound = strat_cfg.get("rsi_overbought_bound", 55)
    rsi_overbought = strat_cfg.get("rsi_overbought", 70)
    macd_hist_lookback = strat_cfg.get("macd_hist_lookback", 4)
    bollinger_lower_threshold = strat_cfg.get("bollinger_lower_threshold", 0.1)
    bollinger_upper_threshold = strat_cfg.get("bollinger_upper_threshold", 0.9)
    stoch_oversold = strat_cfg.get("stoch_oversold", 20)
    stoch_overbought = strat_cfg.get("stoch_overbought", 80)
    volume_high_ratio = strat_cfg.get("volume_high_ratio", 1.5)
    volume_low_ratio = strat_cfg.get("volume_low_ratio", 0.5)
    accumulation_lookback = strat_cfg.get("accumulation_lookback", 20)
    accumulation_threshold = strat_cfg.get("accumulation_threshold", 0.25)
    cmf_threshold = strat_cfg.get("cmf_threshold", 0.2)
    divergence_lookback = strat_cfg.get("divergence_lookback", 30)
    structure_atr_distance = strat_cfg.get("structure_atr_distance", 1.0)
    signal_strong_buy_threshold = strat_cfg.get("signal_strong_buy_threshold", 0.45)
    signal_buy_threshold = strat_cfg.get("signal_buy_threshold", 0.30)
    signal_strong_sell_threshold = strat_cfg.get("signal_strong_sell_threshold", -0.45)
    signal_sell_threshold = strat_cfg.get("signal_sell_threshold", -0.25)
    min_atr_pct = strat_cfg.get("min_atr_pct", 0.5)
    min_adx_strong = strat_cfg.get("min_adx_strong", 25)
    min_adx_developing = strat_cfg.get("min_adx_developing", 20)
    
    cl = ta.closes(entry_candles)
    price = cl[-1]
    factors = []

    trend = _trend_from_emas(trend_candles, ema_slope_threshold)
    trend_score = {"UP": 1.0, "DOWN": -1.0, "SIDEWAYS": 0.0}[trend]
    factors.append(("HTF trend", trend_score, 2.5,
                    f"4h EMA structure says trend is {trend}"))

    # --- Market regime detection ---
    regime = detect_market_regime(trend_candles, min_adx_strong, min_adx_developing)
    if regime == "BULL":
        regime_score = 0.5
        regime_note = "Bullish regime - favorable for longs"
    elif regime == "BEAR":
        regime_score = -0.5
        regime_note = "Bearish regime - unfavorable for longs"
    else:
        regime_score = -0.2
        regime_note = "Choppy regime - no clear direction"
    factors.append(("Market Regime", regime_score, 1.0, regime_note))

    # --- Volatility filter: ATR % ---
    atr_series = ta.atr(entry_candles, 14)
    atr_now = _last(atr_series) or price * 0.01
    atr_pct = (atr_now / price) * 100 if price > 0 else 0
    if atr_pct < min_atr_pct:
        factors.append(("Volatility", -0.5, 1.0, f"Market too flat (ATR {atr_pct:.2f}% < {min_atr_pct}%) - no edge"))
    else:
        factors.append(("Volatility", 0.2, 0.5, f"Normal volatility (ATR {atr_pct:.2f}%)"))

    # --- Trend strength: ADX ---
    adx_series, plus_di, minus_di = ta.adx(entry_candles, 14)
    adx_now = _last(adx_series)
    if adx_now is not None:
        if adx_now >= min_adx_strong:
            s, note = 0.6, f"Strong trend (ADX {adx_now:.1f})"
        elif adx_now >= min_adx_developing:
            s, note = 0.3, f"Developing trend (ADX {adx_now:.1f})"
        else:
            s, note = -0.4, f"Weak/no trend (ADX {adx_now:.1f}) - choppy market"
        factors.append(("ADX", s, 1.0, note))

    # --- Momentum: RSI ---
    rsi_series = ta.rsi(cl, 14)
    rsi_now = _last(rsi_series)
    if rsi_now is not None:
        if rsi_now < rsi_oversold:
            s, note = 0.8, f"RSI {rsi_now:.1f} oversold - bounce potential"
        elif rsi_now < rsi_oversold_bound:
            s, note = 0.3, f"RSI {rsi_now:.1f} cooling, room to run up"
        elif rsi_now > rsi_overbought:
            s, note = -0.8, f"RSI {rsi_now:.1f} overbought - exhaustion risk"
        elif rsi_now > rsi_overbought_bound:
            s, note = -0.3, f"RSI {rsi_now:.1f} elevated"
        else:
            s, note = 0.0, f"RSI {rsi_now:.1f} neutral"
        factors.append(("RSI(14)", s, 1.5, note))

    # --- Momentum: MACD cross + histogram direction ---
    macd_line, signal_line, hist = ta.macd(cl)
    m, sg = _last(macd_line), _last(signal_line)
    if m is not None and sg is not None:
        recent_hist = [h for h in hist[-macd_hist_lookback:] if h is not None]
        hist_rising = len(recent_hist) >= 2 and recent_hist[-1] > recent_hist[0]
        if m > sg and hist_rising:
            s, note = 1.0, "MACD above signal with rising histogram (momentum building)"
        elif m > sg:
            s, note = 0.4, "MACD above signal but histogram flattening"
        elif m < sg and not hist_rising:
            s, note = -1.0, "MACD below signal with falling histogram (momentum fading)"
        else:
            s, note = -0.4, "MACD below signal but histogram recovering"
        factors.append(("MACD", s, 1.5, note))

    # --- Mean reversion: Bollinger position ---
    upper, mid, lower = ta.bollinger(cl, 20, 2.0)
    u, md, lo = _last(upper), _last(mid), _last(lower)
    if u is not None and lo is not None and u != lo:
        pos = (price - lo) / (u - lo)  # 0 = at lower band, 1 = at upper band
        if pos < bollinger_lower_threshold:
            s, note = 0.7, f"Price hugging lower Bollinger band ({pos:.0%})"
        elif pos > bollinger_upper_threshold:
            s, note = -0.7, f"Price hugging upper Bollinger band ({pos:.0%})"
        else:
            s, note = (0.5 - pos) * 0.6, f"Price at {pos:.0%} of Bollinger range"
        factors.append(("Bollinger", s, 1.0, note))

    # --- Stochastic confirmation ---
    k, d = ta.stochastic(entry_candles)
    k_now, d_now = _last(k), _last(d)
    if k_now is not None and d_now is not None:
        if k_now < stoch_oversold and k_now > d_now:
            s, note = 0.8, f"Stoch %K {k_now:.0f} curling up from oversold"
        elif k_now > stoch_overbought and k_now < d_now:
            s, note = -0.8, f"Stoch %K {k_now:.0f} rolling over from overbought"
        elif k_now > d_now:
            s, note = 0.3, f"Stoch bullish cross in progress (%K {k_now:.0f})"
        else:
            s, note = -0.3, f"Stoch bearish cross in progress (%K {k_now:.0f})"
        factors.append(("Stochastic", s, 0.8, note))

    # --- Volume confirmation ---
    vol_avg = _last(ta.volume_sma(entry_candles, 20))
    last_vol = entry_candles[-1]["v"]
    last_green = entry_candles[-1]["c"] >= entry_candles[-1]["o"]
    if vol_avg:
        ratio = last_vol / vol_avg
        direction = 1 if last_green else -1
        if ratio > volume_high_ratio:
            s = 0.8 * direction
            note = f"Volume {ratio:.1f}x average confirms the {'buyers' if last_green else 'sellers'}"
        elif ratio < volume_low_ratio:
            s, note = 0.0, f"Volume {ratio:.1f}x average - thin tape, low conviction"
        else:
            s = 0.2 * direction
            note = f"Volume {ratio:.1f}x average ({'green' if last_green else 'red'} candle)"
        factors.append(("Volume", s, 1.0, note))

    # --- Accumulation / distribution: OBV trend + CMF pressure ---
    accumulation = "NEUTRAL"
    obv_series = ta.obv(entry_candles)
    cmf_series = ta.cmf(entry_candles, 20)
    cmf_now = _last(cmf_series)
    lookback = accumulation_lookback
    obv_slope = 0.0
    if len(obv_series) > lookback and obv_series[-1 - lookback] is not None:
        avg_vol = _last(ta.volume_sma(entry_candles, 20)) or 1.0
        # Fraction of the period's volume that flowed net-in (roughly -1..+1).
        obv_slope = (obv_series[-1] - obv_series[-1 - lookback]) / (avg_vol * lookback)
    if cmf_now is not None:
        obv_c = max(-1.0, min(1.0, obv_slope))
        cmf_c = max(-1.0, min(1.0, cmf_now / cmf_threshold))
        acc_score = 0.6 * obv_c + 0.4 * cmf_c
        if acc_score > accumulation_threshold:
            accumulation = "ACCUMULATION"
            note = f"OBV rising, CMF {cmf_now:+.2f} - accumulation (smart-money buying)"
        elif acc_score < -accumulation_threshold:
            accumulation = "DISTRIBUTION"
            note = f"OBV falling, CMF {cmf_now:+.2f} - distribution (selling pressure)"
        else:
            note = f"CMF {cmf_now:+.2f}, OBV flat - no clear accumulation"
        factors.append(("Accumulation", acc_score, 1.2, note))

    # --- Divergence: price vs OBV (early reversal warning) ---
    divergence = ta.obv_divergence(entry_candles, obv_series, divergence_lookback)
    if divergence == "BULLISH":
        factors.append(("Divergence", 0.7, 0.8,
                        "Bullish divergence: price lower low but OBV higher low (hidden accumulation)"))
    elif divergence == "BEARISH":
        factors.append(("Divergence", -0.7, 0.8,
                        "Bearish divergence: price higher high but OBV lower high (hidden distribution)"))

    # --- Structure: support / resistance proximity ---
    resistances, supports = ta.swing_levels(entry_candles, lookback=5)
    res, sup = ta.nearest_levels(price, resistances, supports)
    atr_series = ta.atr(entry_candles, 14)
    atr_now = _last(atr_series) or price * 0.01
    s, note = 0.0, "No nearby structure"
    if res is not None and (res - price) < atr_now * structure_atr_distance:
        s, note = -0.8, f"Resistance {res:,.6g} within {structure_atr_distance} ATR overhead - bad spot to chase longs"
    elif sup is not None and (price - sup) < atr_now * structure_atr_distance:
        s, note = 0.8, f"Support {sup:,.6g} within {structure_atr_distance} ATR below - favourable long location"
    elif res is not None and sup is not None:
        room_up = res - price
        room_down = price - sup
        rr = room_up / room_down if room_down else 0
        if rr > 2:
            s, note = 0.5, f"Room to resistance is {rr:.1f}x room to support"
        elif rr < 0.5 and rr > 0:
            s, note = -0.5, f"Room to support is {1 / rr:.1f}x room to resistance"
        else:
            s, note = 0.0, "Mid-range between support and resistance"
    factors.append(("Structure", s, 1.2, note))

    # --- Candlestick patterns ---
    patterns = ta.candlestick_patterns(entry_candles)
    pattern_score = 0.0
    pattern_notes = []
    if patterns["pin_bar_bullish"]:
        pattern_score += 0.5
        pattern_notes.append("bullish pin bar")
    if patterns["engulfing_bullish"]:
        pattern_score += 0.6
        pattern_notes.append("bullish engulfing")
    if patterns["pin_bar_bearish"]:
        pattern_score -= 0.5
        pattern_notes.append("bearish pin bar")
    if patterns["engulfing_bearish"]:
        pattern_score -= 0.6
        pattern_notes.append("bearish engulfing")
    if pattern_notes:
        note = ", ".join(pattern_notes)
        factors.append(("Candlestick", pattern_score, 0.8, note))

    # --- Aggregate ---
    total_weight = sum(w for _, _, w, _ in factors)
    score = sum(s * w for _, s, w, _ in factors) / total_weight

    # --- Pengecekan Syarat Utama ---
    srsi_dip = ta.stochrsi_bullish_cross(cl)

    # === Strategi pemenang (pencarian sistematis 529 saham, train ≈ test) ===
    #   STRONG_BUY = tren UP + akumulasi smart-money + MACD bullish
    #   BUY        = tren UP + akumulasi (MACD belum konfirmasi)
    # StochRSI dilepas (pencarian: tak menambah edge, hanya memangkas sampel).
    # Gate IHSG (regime pasar) & likuiditas di lapisan screener; exit = trailing
    # stop 1.5R di tracker. Confluence score tetap untuk rincian faktor saja.
    macd_bull = any(nm == "MACD" and sc > 0 for nm, sc, w, note in factors)
    smart_money_dip = False
    if trend == "UP" and accumulation == "ACCUMULATION":
        if macd_bull:
            signal = SIGNAL_STRONG_BUY
            smart_money_dip = True          # kombo penuh: tren + akumulasi + MACD
        else:
            signal = SIGNAL_BUY             # tren + akumulasi, MACD belum konfirmasi
    else:
        signal = SIGNAL_NEUTRAL
    last_bar = entry_candles[-1]
    # Likuiditas: median nilai transaksi (harga×volume) 20 bar terakhir dari
    # timeframe entry. Untuk Swing (1D) ini = nilai transaksi harian.
    vals = sorted(c["c"] * c["v"] for c in entry_candles[-20:])
    turnover = vals[len(vals) // 2] if vals else 0.0
    return Analysis(instrument, signal, score, price, atr_now, trend, factors, res, sup,
                    resistance_levels=resistances, support_levels=supports,
                    accumulation=accumulation, divergence=divergence,
                    high=last_bar["h"], low=last_bar["l"],
                    srsi_dip=srsi_dip, smart_money_dip=smart_money_dip, turnover=turnover)
