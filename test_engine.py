"""
Offline verification of the full pipeline using real Crypto.com market data
saved in testdata/ - no network access needed.

Run:  python test_engine.py
"""

import json
import os
from datetime import datetime, timezone

import indicators as ta
import strategy
from risk import RiskManager
from trader import Trader

HERE = os.path.dirname(os.path.abspath(__file__))


def load_candles(path):
    with open(path) as f:
        raw = json.load(f)
    candles = []
    for row in raw["data"]:
        ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        candles.append({
            "t": int(ts.timestamp() * 1000),
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
            "v": float(row["volume"]),
        })
    candles.sort(key=lambda x: x["t"])
    return candles


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
    return condition


def main():
    failures = 0
    h1 = load_candles(os.path.join(HERE, "testdata", "btc_1h.json"))
    h4 = load_candles(os.path.join(HERE, "testdata", "btc_4h.json"))

    print("\n--- Indicators on real BTC_USDT data ---")
    cl = ta.closes(h1)
    rsi_now = [v for v in ta.rsi(cl, 14) if v is not None][-1]
    failures += not check("RSI in valid range", 0 <= rsi_now <= 100, f"RSI={rsi_now:.1f}")
    ema20 = [v for v in ta.ema(cl, 20) if v is not None][-1]
    failures += not check("EMA20 near price", abs(ema20 - cl[-1]) / cl[-1] < 0.05, f"EMA20={ema20:.0f}")
    atr_now = [v for v in ta.atr(h1, 14) if v is not None][-1]
    failures += not check("ATR positive and sane", 0 < atr_now < cl[-1] * 0.1, f"ATR={atr_now:.0f}")
    upper, mid, lower = ta.bollinger(cl)
    u, m, l = upper[-1], mid[-1], lower[-1]
    failures += not check("Bollinger ordering", l < m < u, f"{l:.0f} < {m:.0f} < {u:.0f}")
    macd_line, sig_line, hist = ta.macd(cl)
    failures += not check("MACD computed", macd_line[-1] is not None and sig_line[-1] is not None)
    res_levels, sup_levels = ta.swing_levels(h1)
    failures += not check("Swing levels found", len(res_levels) > 0 and len(sup_levels) > 0,
                          f"{len(res_levels)} resistances, {len(sup_levels)} supports")

    print("\n--- Full analysis (strategy engine) ---")
    analysis = strategy.analyze("BTC_USDT", h1, h4)
    print()
    print(analysis.report())
    print()
    failures += not check("Signal is a valid value",
                          analysis.signal in ("STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"))
    failures += not check("Score bounded", -1 <= analysis.score <= 1, f"{analysis.score:+.2f}")
    failures += not check("All 7 factors present", len(analysis.factors) == 7,
                          f"{len(analysis.factors)} factors")

    print("\n--- Risk manager ---")
    config = json.load(open(os.path.join(HERE, "config.json")))
    risk = RiskManager(config)
    plan = risk.plan_trade("BTC_USDT", "BUY", analysis.price, analysis.atr, 10000,
                           analysis.nearest_support, analysis.nearest_resistance)
    if plan:
        print(f"  Plan: BUY {plan.quantity:.6f} BTC @ {plan.entry:,.0f} | "
              f"SL {plan.stop_loss:,.0f} TP {plan.take_profit:,.0f} | "
              f"risk ${plan.risk_amount:.2f} | R:R {plan.reward_risk:.2f}")
        failures += not check("Stop below entry for a long", plan.stop_loss < plan.entry)
        failures += not check("Target above entry for a long", plan.take_profit > plan.entry)
        failures += not check("Risk <= 1% of equity", plan.risk_amount <= 100.01,
                              f"${plan.risk_amount:.2f}")
        failures += not check("Notional <= 20% of equity", plan.notional <= 2000.01,
                              f"${plan.notional:.2f}")
    else:
        print("  No plan produced (reward:risk requirement not met at current structure) - acceptable")
    failures += not check("Circuit breaker trips at -4%", risk.daily_circuit_breaker(10000, -400))
    failures += not check("Circuit breaker quiet at -1%", not risk.daily_circuit_breaker(10000, -100))

    print("\n--- Paper trader open/close cycle ---")
    # Use a scratch state file so we don't pollute the real paper portfolio.
    import trader as trader_mod
    trader_mod.STATE_FILE = os.path.join(HERE, "testdata", "_test_state.json")
    trader_mod.JOURNAL_FILE = os.path.join(HERE, "testdata", "_test_trades.csv")
    for f in (trader_mod.STATE_FILE, trader_mod.JOURNAL_FILE):
        if os.path.exists(f):
            os.remove(f)
    t = Trader({"mode": "paper", "paper_starting_balance": 10000.0}, exchange=None)
    test_plan = risk.plan_trade("BTC_USDT", "BUY", 62000.0, 600.0, 10000)
    pos = t.open_position(test_plan, "test")
    failures += not check("Position opened", pos is not None and "BTC_USDT" in t.open_positions())
    failures += not check("Cash reduced by notional", t.state["cash"] < 10000)
    pnl = t.close_position("BTC_USDT", 63000.0, "test close")
    failures += not check("Position closed with profit at higher price", pnl is not None and pnl > 0,
                          f"PnL=${pnl:.2f}")
    failures += not check("Journal written", os.path.exists(trader_mod.JOURNAL_FILE))
    failures += not check("Live mode refused without consent flag", _live_mode_refused())

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}\n")
    return failures


def _live_mode_refused():
    try:
        Trader({"mode": "live", "i_understand_live_trading_risks": False}, exchange=None)
        return False
    except SystemExit:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
