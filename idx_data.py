"""
Indonesian stock (IDX / Bursa Efek Indonesia) market data via Yahoo Finance.

This is the stock-market counterpart to exchange.py: it returns candles in the
exact same shape the analysis engine expects — a list of dicts
({t, o, h, l, c, v}, oldest first) — so strategy.analyze() works unchanged.

No API key is needed. IDX tickers use the ".JK" suffix on Yahoo Finance
(e.g. BBCA -> BBCA.JK). This module adds the suffix for you, so you can pass
either "BBCA" or "BBCA.JK".

Requires: pip install yfinance
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    import yfinance as yf
except ImportError as e:  # pragma: no cover - surfaced as a friendly message
    raise ImportError(
        "yfinance is required for IDX stock data. Install it with:\n"
        "    pip install yfinance"
    ) from e

from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))   # Jakarta / IDX trading time


class DataError(Exception):
    pass


# Yahoo Finance interval codes keyed by the timeframe names used in this project.
_INTERVALS = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "1d": "1d",
    "1wk": "1wk",
    "1mo": "1mo",
}

# How far back to pull so indicators (up to EMA200) have room to warm up.
# Yahoo caps how much history it returns per interval; these are safe periods.
_PERIODS = {
    "5m": "5d",     # intraday scan only needs today; 5d survives weekends/holidays
    "15m": "5d",
    "1h": "6mo",    # ~800 hourly bars (Yahoo caps intraday history)
    "1d": "2y",     # ~500 trading days
    "1wk": "10y",   # ~520 weeks
    "1mo": "max",
}


def last_session(candles):
    """Keep only the bars belonging to the most recent WIB trading date.

    Intraday fetches span several days; the intraday scanner wants just the
    latest session (whatever Yahoo's freshest date is — live or replayed).
    """
    if not candles:
        return candles
    last_date = datetime.fromtimestamp(candles[-1]["t"] / 1000, WIB).date()
    return [c for c in candles
            if datetime.fromtimestamp(c["t"] / 1000, WIB).date() == last_date]


def to_yahoo_symbol(ticker):
    """Normalise a ticker to Yahoo Finance form: 'BBCA' -> 'BBCA.JK'."""
    ticker = ticker.strip().upper()
    if not ticker:
        raise DataError("empty ticker")
    if ticker.startswith("^") or ticker.endswith(".JK"):   # index (^JKSE) or already-suffixed
        return ticker
    return ticker + ".JK"


def display_symbol(ticker):
    """The bare IDX code for display: 'BBCA.JK' -> 'BBCA'."""
    return ticker.strip().upper().removesuffix(".JK")


def get_candles(ticker, timeframe="1d", count=300):
    """
    Return a list of candles (oldest first) as dicts with t, o, h, l, c, v.

    t is a POSIX timestamp in milliseconds (to match exchange.py); o/h/l/c/v
    are floats. Rows with missing OHLC are dropped.
    """
    interval = _INTERVALS.get(timeframe)
    if interval is None:
        raise DataError(f"unsupported timeframe {timeframe!r}; use one of {list(_INTERVALS)}")

    symbol = to_yahoo_symbol(ticker)
    period = _PERIODS[timeframe]

    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise DataError(f"no data returned for {symbol} ({timeframe})")

    candles = []
    for ts, row in df.iterrows():
        o, h, l, c = row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")
        v = row.get("Volume", 0)
        # Skip rows where any OHLC value is missing (NaN != itself).
        if any(x is None or x != x for x in (o, h, l, c)):
            continue
        candles.append({
            "t": int(ts.timestamp() * 1000),
            "o": float(o),
            "h": float(h),
            "l": float(l),
            "c": float(c),
            "v": float(v) if v == v else 0.0,
        })

    candles.sort(key=lambda x: x["t"])
    if not candles:
        raise DataError(f"no usable candles for {symbol} ({timeframe})")
    return candles[-count:] if count else candles


def get_quote(ticker):
    """Latest price snapshot for a single ticker (best-effort)."""
    symbol = to_yahoo_symbol(ticker)
    candles = get_candles(symbol, "1d", count=2)
    last = candles[-1]
    prev_close = candles[-2]["c"] if len(candles) > 1 else last["o"]
    change_pct = (last["c"] - prev_close) / prev_close * 100 if prev_close else None
    return {
        "last": last["c"],
        "high": last["h"],
        "low": last["l"],
        "volume": last["v"],
        "change_pct": change_pct,
    }


def market_status(now=None):
    """
    Heuristic IDX open/closed state from the WIB clock. Trading sessions:
      Mon-Thu  09:00-12:00 & 13:30-15:50
      Fri      09:00-11:30 & 14:00-15:50
    Public holidays are NOT accounted for (Yahoo data is the source of truth).
    """
    now = now or datetime.now(timezone.utc)
    wib = now.astimezone(WIB)
    minutes = wib.hour * 60 + wib.minute
    if wib.weekday() >= 5:
        return {"open": False, "label": "TUTUP (akhir pekan)", "wib": wib}
    if wib.weekday() <= 3:            # Mon-Thu
        s1, s2 = (540, 720), (810, 950)          # 09:00-12:00, 13:30-15:50
    else:                            # Fri
        s1, s2 = (540, 690), (840, 950)          # 09:00-11:30, 14:00-15:50
    if s1[0] <= minutes < s1[1] or s2[0] <= minutes < s2[1]:
        return {"open": True, "label": "BUKA", "wib": wib}
    if minutes < s1[0]:
        return {"open": False, "label": "TUTUP (pra-pembukaan)", "wib": wib}
    if s1[1] <= minutes < s2[0]:
        return {"open": False, "label": "JEDA (istirahat sesi)", "wib": wib}
    return {"open": False, "label": "TUTUP", "wib": wib}


_REGIME_CACHE = {"ts": 0.0, "val": True}


def market_risk_on(now=None):
    """IHSG (^JKSE) risk-on? True bila harga di atas MA panjang (EMA200, fallback
    EMA50) DAN EMA50 sedang naik. Dipakai sebagai gate regime: jangan entry saat
    pasar risk-off. Di-cache 1 jam; fail-open (True) bila fetch indeks gagal."""
    import time as _t
    import indicators as _ta
    if _t.time() - _REGIME_CACHE["ts"] < 3600:
        return _REGIME_CACHE["val"]
    try:
        closes = [c["c"] for c in get_candles("^JKSE", "1d", count=300)]
        e50, e200 = _ta.ema(closes, 50), _ta.ema(closes, 200)
        last50, last200 = e50[-1], e200[-1]
        prior50 = e50[-11] if len(e50) > 11 and e50[-11] is not None else last50
        ref = last200 if last200 is not None else last50
        val = last50 is not None and closes[-1] > ref and last50 >= prior50
    except Exception:
        val = True
    _REGIME_CACHE.update(ts=_t.time(), val=bool(val))
    return _REGIME_CACHE["val"]


def quote_freshness(ticker="BBCA", now=None):
    """
    Last trade time (WIB) and delay in minutes for a liquid reference ticker,
    so the UI can show how fresh Yahoo's data currently is. Returns None on error.
    """
    now = now or datetime.now(timezone.utc)
    try:
        info = yf.Ticker(to_yahoo_symbol(ticker)).info or {}
        rmt = info.get("regularMarketTime")
        if not rmt:
            return None
        qt = datetime.fromtimestamp(int(rmt), tz=timezone.utc)
        return {"quote_wib": qt.astimezone(WIB),
                "delay_min": (now - qt).total_seconds() / 60.0}
    except Exception:
        return None
