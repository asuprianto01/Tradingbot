"""Mean reversion screener for daily IDX swing setups.

The rules follow the product PRD:
  - Conservative mode: lower Bollinger touch, RSI < 35, trend filter,
    liquidity check, no crash day, acceptable reward:risk, and rebound candle.
  - Aggressive mode: lower Bollinger break, RSI < 30, near support, no crash
    day, and acceptable reward:risk. Trend/volume/rebound still affect score.

This module is intentionally self-contained so the Streamlit tab can stay thin.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

import idx_data
import indicators as ta

MODE_CONSERVATIVE = "conservative"
MODE_AGGRESSIVE = "aggressive"

SIGNAL_BUY = "BUY"
SIGNAL_WATCH = "WATCH"
SIGNAL_NO_TRADE = "NO TRADE"

STATUS_STRONG = "Strong Setup"
STATUS_VALID = "Valid Setup"
STATUS_WEAK = "Weak Setup"
STATUS_NONE = "No Trade"

DEFAULTS = {
    "bollinger_period": 20,
    "bollinger_stddev": 2.0,
    "rsi_period": 14,
    "atr_period": 14,
    "volume_period": 20,
    "ma_fast": 20,
    "ma_mid": 50,
    "ma_slow": 200,
    "support_lookback": 5,
    "min_volume_ratio": 0.8,
    "max_daily_drop_pct": 10.0,
    "support_buffer_pct": 1.0,
    "atr_stop_mult": 1.5,
    "near_support_atr_mult": 1.0,
    "near_support_pct": 3.0,
    "rebound_close_position": 0.6,
    "conservative_rsi_max": 35.0,
    "aggressive_rsi_max": 30.0,
    "conservative_rr_min": 1.5,
    "aggressive_rr_min": 1.2,
}


@dataclass
class MRAnalysis:
    instrument: str
    mode: str
    signal: str
    status: str
    score: int
    price: float
    high: float
    low: float
    prev_close: float
    change_pct: Optional[float]
    ma20: Optional[float]
    ma50: Optional[float]
    ma200: Optional[float]
    bb_upper: Optional[float]
    bb_middle: Optional[float]
    bb_lower: Optional[float]
    rsi14: Optional[float]
    atr14: Optional[float]
    volume_avg20: Optional[float]
    volume_ratio: Optional[float]
    nearest_support: Optional[float]
    nearest_resistance: Optional[float]
    trend_ok: bool
    near_support: bool
    touched_lower_band: bool
    rebound: bool
    crash_day: bool
    risk_reward: Optional[float]
    entry_price: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    stop_loss: Optional[float]
    support_gap_pct: Optional[float]
    close_vs_ma20_pct: Optional[float]
    reasons: list[str]
    blockers: list[str]

    def to_dict(self):
        return asdict(self)


def _last(values):
    return values[-1] if values else None


def _merge_config(config):
    merged = dict(DEFAULTS)
    if config:
        merged.update(config)
    return merged


def _pct_change(curr, prev):
    if curr is None or prev in (None, 0):
        return None
    return (curr - prev) / prev * 100.0


def _status_from_score(score):
    if score >= 80:
        return STATUS_STRONG
    if score >= 65:
        return STATUS_VALID
    if score >= 50:
        return STATUS_WEAK
    return STATUS_NONE


def _near_support(price, support, atr_value, cfg):
    if price is None or support is None or support >= price:
        return False, None
    gap = (price - support) / price * 100.0 if price else None
    limit_pct = cfg["near_support_pct"]
    limit_atr = (atr_value or 0.0) * cfg["near_support_atr_mult"]
    if atr_value and (price - support) <= limit_atr:
        return True, gap
    if gap is not None and gap <= limit_pct:
        return True, gap
    return False, gap


def _rebound_signal(bar, bb_lower, threshold):
    low = bar["l"]
    high = bar["h"]
    close = bar["c"]
    opened = bar["o"]
    rng = max(high - low, 0.0)
    close_position = ((close - low) / rng) if rng else 0.5
    back_inside_band = bb_lower is not None and low <= bb_lower and close >= bb_lower
    strong_close = close_position >= threshold and close >= opened
    return back_inside_band or strong_close


def _build_plan(price, bb_middle, bb_upper, support, resistance, atr_value, cfg):
    if price is None or atr_value is None:
        return None
    atr_stop = price - atr_value * cfg["atr_stop_mult"]
    support_stop = None
    if support is not None and support < price:
        support_stop = support * (1.0 - cfg["support_buffer_pct"] / 100.0)

    stop_candidates = [s for s in (atr_stop, support_stop) if s is not None and s < price]
    if not stop_candidates:
        return None

    # "Most conservative" for longs means the tighter stop (closest to entry).
    stop = max(stop_candidates)
    target_1 = bb_middle if bb_middle is not None else None
    if target_1 is not None and target_1 <= price:
        target_1 = None

    t2_candidates = [v for v in (bb_upper, resistance) if v is not None and v > price]
    target_2 = min(t2_candidates) if t2_candidates else None
    if target_2 is None:
        target_2 = target_1

    if stop >= price or target_2 is None or target_2 <= price:
        return None

    risk = price - stop
    rr = (target_2 - price) / risk if risk > 0 else None
    if rr is None or rr <= 0:
        return None

    return {
        "entry_price": price,
        "stop_loss": stop,
        "target_1": target_1,
        "target_2": target_2,
        "risk_reward": rr,
    }


def build_position_plan(analysis, capital, risk_pct, lot_size=100):
    if analysis.signal != SIGNAL_BUY:
        return None
    if not analysis.entry_price or not analysis.stop_loss or analysis.stop_loss >= analysis.entry_price:
        return None

    risk_amount_target = capital * risk_pct / 100.0
    risk_per_share = analysis.entry_price - analysis.stop_loss
    if risk_per_share <= 0:
        return None

    raw_shares = int(risk_amount_target // risk_per_share)
    raw_lots = raw_shares // lot_size
    max_lots = int(capital // (analysis.entry_price * lot_size))
    lots = min(raw_lots, max_lots)
    shares = lots * lot_size
    if shares <= 0:
        return None

    cost = shares * analysis.entry_price
    actual_risk = shares * risk_per_share
    return {
        "signal": analysis.signal,
        "entry": analysis.entry_price,
        "stop": analysis.stop_loss,
        "target_1": analysis.target_1,
        "target_2": analysis.target_2,
        "reward_risk": analysis.risk_reward,
        "lots": lots,
        "shares": shares,
        "cost": cost,
        "risk_amount": actual_risk,
    }


def build_tracker_recommendation(analysis):
    """Shape a mean-reversion setup so tracker.open_position can journal it."""
    if analysis.signal != SIGNAL_BUY:
        return None
    if not analysis.entry_price or not analysis.stop_loss:
        return None
    targets = []
    for target in (analysis.target_1, analysis.target_2):
        if target is None or target <= analysis.entry_price:
            continue
        if any(abs(target - t["price"]) < 1e-9 for t in targets):
            continue
        risk = analysis.entry_price - analysis.stop_loss
        targets.append({
            "price": target,
            "gain_pct": (target - analysis.entry_price) / analysis.entry_price * 100,
            "rr": (target - analysis.entry_price) / risk if risk > 0 else None,
        })
    if not targets:
        return None
    return {
        "signal": analysis.signal,
        "entry": analysis.entry_price,
        "entry_low": analysis.entry_price,
        "entry_high": analysis.entry_price,
        "stop": analysis.stop_loss,
        "target": targets[0]["price"],
        "reward_risk": analysis.risk_reward,
        "targets": targets,
    }


def _candles_from_history_df(df):
    if df is None or df.empty:
        return []
    candles = []
    for ts, row in df.iterrows():
        o, h, l, c = row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")
        v = row.get("Volume", 0)
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
    return candles


def fetch_daily_history(ticker, period="5y"):
    symbol = idx_data.to_yahoo_symbol(ticker)
    df = idx_data.yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
    candles = _candles_from_history_df(df)
    if not candles:
        raise idx_data.DataError(f"no usable candles for {symbol} ({period})")
    return candles


def _bar_date(candle):
    return datetime.fromtimestamp(candle["t"] / 1000, idx_data.WIB).strftime("%Y-%m-%d")


def _simulate_trade(analysis, future, horizon):
    entry = analysis.entry_price
    stop = analysis.stop_loss
    target = analysis.target_2 or analysis.target_1
    if not entry or not stop or not target:
        return None
    risk = entry - stop
    if risk <= 0 or target <= entry:
        return None

    for idx, bar in enumerate(future[:horizon], start=1):
        if bar["l"] <= stop:
            return {
                "outcome": "SL",
                "exit_price": stop,
                "holding_bars": idx,
                "exit_date": _bar_date(bar),
                "r_multiple": -1.0,
            }
        if bar["h"] >= target:
            rr = (target - entry) / risk
            return {
                "outcome": "TP",
                "exit_price": target,
                "holding_bars": idx,
                "exit_date": _bar_date(bar),
                "r_multiple": rr,
            }

    last = future[min(horizon, len(future)) - 1]
    return {
        "outcome": "TIME",
        "exit_price": last["c"],
        "holding_bars": min(horizon, len(future)),
        "exit_date": _bar_date(last),
        "r_multiple": (last["c"] - entry) / risk,
    }


def _max_drawdown(equity_curve):
    peak = equity_curve[0] if equity_curve else 0
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        dd = (peak - value) / peak if peak else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def backtest_one(
    ticker,
    mode=MODE_CONSERVATIVE,
    period="5y",
    horizon=20,
    initial_capital=10_000_000,
    risk_per_trade=0.01,
    config=None,
):
    candles = fetch_daily_history(ticker, period=period)
    name = idx_data.display_symbol(ticker)
    warmup = DEFAULTS["ma_slow"] + 5
    trades = []
    equity = float(initial_capital)
    equity_curve = [equity]
    i = warmup
    end = len(candles) - horizon - 1

    while i <= end:
        try:
            analysis = analyze(name, candles[: i + 1], mode=mode, config=config)
        except ValueError:
            i += 1
            continue
        if analysis.signal != SIGNAL_BUY:
            i += 1
            continue
        sim = _simulate_trade(analysis, candles[i + 1:], horizon)
        if sim is None:
            i += 1
            continue

        risk_amount = equity * risk_per_trade
        pnl = sim["r_multiple"] * risk_amount
        equity += pnl
        equity_curve.append(equity)
        trades.append({
            "ticker": name,
            "signal_date": _bar_date(candles[i]),
            "exit_date": sim["exit_date"],
            "mode": mode,
            "entry": analysis.entry_price,
            "stop": analysis.stop_loss,
            "target": analysis.target_2 or analysis.target_1,
            "exit": sim["exit_price"],
            "outcome": sim["outcome"],
            "holding_bars": sim["holding_bars"],
            "r_multiple": sim["r_multiple"],
            "pnl": pnl,
            "score": analysis.score,
            "rsi14": analysis.rsi14,
            "risk_reward": analysis.risk_reward,
        })
        i += sim["holding_bars"]

    return summarize_backtest(trades, equity_curve, initial_capital)


def summarize_backtest(trades, equity_curve, initial_capital):
    n = len(trades)
    if not n:
        return {
            "trades": [],
            "metrics": {
                "total_trades": 0,
                "win_rate": 0.0,
                "tp_rate": 0.0,
                "total_return_pct": 0.0,
                "expectancy_R": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_pct": 0.0,
                "avg_holding_bars": 0.0,
                "avg_gain_R": 0.0,
                "avg_loss_R": 0.0,
            },
            "equity_curve": equity_curve,
        }
    wins = [t for t in trades if t["r_multiple"] > 0]
    losses = [t for t in trades if t["r_multiple"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    final_equity = equity_curve[-1]
    return {
        "trades": trades,
        "metrics": {
            "total_trades": n,
            "win_rate": len(wins) / n,
            "tp_rate": sum(1 for t in trades if t["outcome"] == "TP") / n,
            "total_return_pct": (final_equity - initial_capital) / initial_capital * 100,
            "expectancy_R": sum(t["r_multiple"] for t in trades) / n,
            "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
            "max_drawdown_pct": _max_drawdown(equity_curve) * 100,
            "avg_holding_bars": sum(t["holding_bars"] for t in trades) / n,
            "avg_gain_R": (sum(t["r_multiple"] for t in wins) / len(wins)) if wins else 0.0,
            "avg_loss_R": (sum(t["r_multiple"] for t in losses) / len(losses)) if losses else 0.0,
        },
        "equity_curve": equity_curve,
    }


def combine_backtests(results, initial_capital, risk_per_trade=0.01):
    trades = []
    for result in results:
        trades.extend(result.get("trades", []))
    trades.sort(key=lambda t: (t["signal_date"], t["ticker"]))
    equity = float(initial_capital)
    curve = [equity]
    combined_trades = []
    for trade in trades:
        pnl = trade["r_multiple"] * (equity * risk_per_trade)
        equity += pnl
        t = dict(trade)
        t["pnl"] = pnl
        combined_trades.append(t)
        curve.append(equity)
    return summarize_backtest(combined_trades, curve, initial_capital)


def analyze(instrument, candles, mode=MODE_CONSERVATIVE, config=None):
    cfg = _merge_config(config)
    if len(candles) < cfg["ma_slow"] + 5:
        raise ValueError(f"{instrument}: data harian belum cukup untuk MA{cfg['ma_slow']}")

    closes = [c["c"] for c in candles]
    last_bar = candles[-1]
    price = last_bar["c"]
    prev_close = candles[-2]["c"]
    change_pct = _pct_change(price, prev_close)

    ma20_series = ta.sma(closes, cfg["ma_fast"])
    ma50_series = ta.sma(closes, cfg["ma_mid"])
    ma200_series = ta.sma(closes, cfg["ma_slow"])
    rsi_series = ta.rsi(closes, cfg["rsi_period"])
    bb_upper, bb_middle, bb_lower = ta.bollinger(
        closes, cfg["bollinger_period"], cfg["bollinger_stddev"]
    )
    atr_series = ta.atr(candles, cfg["atr_period"])
    volume_avg_series = ta.volume_sma(candles, cfg["volume_period"])

    ma20 = _last(ma20_series)
    ma50 = _last(ma50_series)
    ma200 = _last(ma200_series)
    rsi14 = _last(rsi_series)
    bb_u = _last(bb_upper)
    bb_m = _last(bb_middle)
    bb_l = _last(bb_lower)
    atr14 = _last(atr_series)
    volume_avg20 = _last(volume_avg_series)
    volume_ratio = (last_bar["v"] / volume_avg20) if volume_avg20 not in (None, 0) else None

    resistances, supports = ta.swing_levels(candles, lookback=cfg["support_lookback"])
    nearest_resistance, nearest_support = ta.nearest_levels(price, resistances, supports)
    near_support, support_gap_pct = _near_support(price, nearest_support, atr14, cfg)

    touched_lower_band = bb_l is not None and price <= bb_l
    trend_ok = bool(
        (ma200 is not None and price > ma200)
        or (ma50 is not None and ma200 is not None and ma50 > ma200)
    )
    rebound = _rebound_signal(last_bar, bb_l, cfg["rebound_close_position"])
    crash_day = (change_pct or 0.0) <= -cfg["max_daily_drop_pct"]
    close_vs_ma20_pct = _pct_change(price, ma20) if ma20 not in (None, 0) else None

    plan = _build_plan(price, bb_m, bb_u, nearest_support, nearest_resistance, atr14, cfg)
    entry_price = plan["entry_price"] if plan else None
    stop_loss = plan["stop_loss"] if plan else None
    target_1 = plan["target_1"] if plan else None
    target_2 = plan["target_2"] if plan else None
    risk_reward = plan["risk_reward"] if plan else None

    rr_floor = (
        cfg["conservative_rr_min"]
        if mode == MODE_CONSERVATIVE
        else cfg["aggressive_rr_min"]
    )
    rsi_floor = (
        cfg["conservative_rsi_max"]
        if mode == MODE_CONSERVATIVE
        else cfg["aggressive_rsi_max"]
    )

    score = 0
    reasons = []
    blockers = []

    if touched_lower_band:
        score += 20
        reasons.append("Harga close menyentuh atau menembus lower Bollinger Band.")
    else:
        blockers.append("Belum ada sentuhan lower Bollinger Band.")

    if rsi14 is not None and rsi14 <= rsi_floor:
        score += 20
        reasons.append(f"RSI 14 oversold di {rsi14:.1f}.")
    elif rsi14 is not None:
        blockers.append(f"RSI 14 masih {rsi14:.1f}, belum cukup oversold.")

    if trend_ok:
        score += 20
        reasons.append("Filter tren lolos: harga di atas MA200 atau MA50 > MA200.")
    elif mode == MODE_CONSERVATIVE:
        blockers.append("Filter tren belum lolos untuk mode conservative.")

    if near_support:
        score += 15
        reasons.append("Harga berada dekat area support.")
    elif mode == MODE_AGGRESSIVE:
        blockers.append("Harga belum cukup dekat area support untuk mode aggressive.")

    if volume_ratio is not None and volume_ratio >= cfg["min_volume_ratio"]:
        score += 10
        reasons.append(f"Volume ratio {volume_ratio:.2f}x masih cukup likuid.")
    elif volume_ratio is not None and mode == MODE_CONSERVATIVE:
        blockers.append(f"Volume ratio {volume_ratio:.2f}x di bawah minimum {cfg['min_volume_ratio']:.2f}x.")

    if risk_reward is not None and risk_reward >= rr_floor:
        score += 10
        reasons.append(f"Risk/reward {risk_reward:.2f}:1 memenuhi minimum mode {mode}.")
    else:
        blockers.append(f"Risk/reward belum mencapai minimum {rr_floor:.1f}:1.")

    if rebound:
        score += 5
        reasons.append("Ada tanda rebound: close kembali kuat dari area low/band.")
    elif mode == MODE_CONSERVATIVE:
        blockers.append("Belum ada candle rebound yang cukup jelas.")

    if crash_day:
        blockers.append(
            f"Harga turun {abs(change_pct or 0.0):.1f}% dalam sehari; PRD menyaring crash day."
        )

    if mode == MODE_CONSERVATIVE:
        is_buy = all(
            [
                touched_lower_band,
                rsi14 is not None and rsi14 <= cfg["conservative_rsi_max"],
                trend_ok,
                volume_ratio is not None and volume_ratio >= cfg["min_volume_ratio"],
                not crash_day,
                risk_reward is not None and risk_reward >= cfg["conservative_rr_min"],
                rebound,
            ]
        )
    else:
        is_buy = all(
            [
                touched_lower_band,
                rsi14 is not None and rsi14 <= cfg["aggressive_rsi_max"],
                near_support,
                not crash_day,
                risk_reward is not None and risk_reward >= cfg["aggressive_rr_min"],
            ]
        )

    signal = SIGNAL_BUY if is_buy else (SIGNAL_WATCH if score >= 50 else SIGNAL_NO_TRADE)
    status = _status_from_score(score)

    return MRAnalysis(
        instrument=instrument,
        mode=mode,
        signal=signal,
        status=status,
        score=score,
        price=price,
        high=last_bar["h"],
        low=last_bar["l"],
        prev_close=prev_close,
        change_pct=change_pct,
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        bb_upper=bb_u,
        bb_middle=bb_m,
        bb_lower=bb_l,
        rsi14=rsi14,
        atr14=atr14,
        volume_avg20=volume_avg20,
        volume_ratio=volume_ratio,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        trend_ok=trend_ok,
        near_support=near_support,
        touched_lower_band=touched_lower_band,
        rebound=rebound,
        crash_day=crash_day,
        risk_reward=risk_reward,
        entry_price=entry_price,
        target_1=target_1,
        target_2=target_2,
        stop_loss=stop_loss,
        support_gap_pct=support_gap_pct,
        close_vs_ma20_pct=close_vs_ma20_pct,
        reasons=reasons,
        blockers=blockers,
    )


def screen_one(ticker, mode=MODE_CONSERVATIVE, count=320, config=None):
    candles = idx_data.get_candles(ticker, "1d", count=count)
    return analyze(idx_data.display_symbol(ticker), candles, mode=mode, config=config)
