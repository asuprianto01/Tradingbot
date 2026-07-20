"""
Walk-forward backtester & parameter optimizer for the IDX screener.

Measures the REAL accuracy of strategy.analyze() the same way the live screener
trades it: on each daily bar it runs the exact analysis, and when a BUY /
STRONG_BUY fires it builds a risk-managed plan (risk.RiskManager) and simulates
the trade forward bar-by-bar — TP before SL = win — instead of the old naive
"did price go up in N bars" check.

Design (lazy + fast): analyze() is called ONCE per (ticker, bar) in an expensive
first pass that records each bar's factor breakdown, trend, accumulation, price,
ATR, structure and the forward price path. Because thresholds only re-gate a
score, weights only re-aggregate the same factors, and risk settings only re-plan
from the same price/ATR/structure, EVERY optimization run after that is pure
arithmetic over the cached records — no re-fetch, no re-analyze.

No lookahead: weekly trend candles are sliced to <= the current daily date, and
ML/meta models are disabled during the walk (they may have trained on this same
history). This backtests the pure rule engine, which is what we tune here.

Usage:
    python backtest_optimizer.py                 # default universe (IDX30)
    python backtest_optimizer.py --universe LQ45
    python backtest_optimizer.py BBCA BBRI TLKM  # specific tickers
    python backtest_optimizer.py --horizon 30    # hold up to 30 trading days
    python backtest_optimizer.py --selftest      # run the trade-sim self-check
"""

import json
import os
import sys
import time
from datetime import datetime

import idx_data
import indicators as ta
import strategy
import risk
import universes

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".backtest_cache")
BUY_SIGNALS = (strategy.SIGNAL_BUY, strategy.SIGNAL_STRONG_BUY)

# Signal-gating knobs the walk records but does NOT bake in, so we can re-gate
# cheaply. Defaults mirror strategy.analyze().
DEFAULT_PARAMS = {
    "signal_strong_buy_threshold": 0.45,
    "signal_buy_threshold": 0.30,
    "weight_overrides": {},           # factor name -> new weight
    "risk": {},                       # passed straight to RiskManager
    "require_index_bull": False,      # only take longs when IHSG is risk-on
    "max_runup": None,                # skip if price already ran > this over 20 bars
    "max_ext_atr": None,              # skip if price > this many ATRs above 20-bar base
}

INDEX_TICKER = "^JKSE"                 # IHSG (Jakarta Composite) on Yahoo Finance


def _bar_date(t_ms):
    return datetime.fromtimestamp(t_ms / 1000, idx_data.WIB).date()


def index_regime_map(index_candles):
    """date -> bullish? IHSG risk-on = price above its long MA with a rising EMA50.

    Computed once over the whole index series (cheap). Warm-up bars default to
    bullish so we don't blanket-block the early history.
    """
    closes = [c["c"] for c in index_candles]
    ema50 = ta.ema(closes, 50)
    ema200 = ta.ema(closes, 200)
    m = {}
    for i, c in enumerate(index_candles):
        e50, e200 = ema50[i], ema200[i]
        if e50 is None:
            bull = True
        else:
            prior = ema50[i - 10] if i >= 10 and ema50[i - 10] is not None else e50
            ref = e200 if e200 is not None else e50
            bull = c["c"] > ref and e50 >= prior
        m[_bar_date(c["t"])] = bull
    return m


def annotate_index(records, regime_map):
    for r in records:
        r["index_bull"] = regime_map.get(_bar_date(r["t"]), True)


def stochrsi(closes, period=14, smooth=3):
    """StochRSI (%K, %D) — delegates to indicators.stochrsi (single source)."""
    return ta.stochrsi(closes, period, smooth, smooth)


# --------------------------------------------------------------------------- #
# Data (day-cached so re-runs don't hammer Yahoo)                             #
# --------------------------------------------------------------------------- #
def fetch_candles(ticker, timeframe, count):
    os.makedirs(CACHE_DIR, exist_ok=True)
    day = time.strftime("%Y%m%d")
    path = os.path.join(CACHE_DIR, f"{idx_data.display_symbol(ticker)}_{timeframe}_{day}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    candles = idx_data.get_candles(ticker, timeframe, count=count)
    with open(path, "w") as f:
        json.dump(candles, f)
    return candles


# --------------------------------------------------------------------------- #
# Pass 1: one analyze() per bar, record everything an optimizer could need    #
# --------------------------------------------------------------------------- #
def build_records(ticker, cfg, entry_tf="1d", trend_tf="1wk",
                  warmup=210, horizon=20, max_bars=450):
    """Walk every daily bar; return a list of per-signal-candidate records.

    Each record is a dict with the bar's factors + the forward (h, l, c) path,
    enough to re-derive the signal and simulate the trade under any params.
    """
    daily = fetch_candles(ticker, entry_tf, count=max_bars + horizon + 10)
    weekly = fetch_candles(ticker, trend_tf, count=520)
    name = idx_data.display_symbol(ticker)

    srsi_k, srsi_d = stochrsi([c["c"] for c in daily])   # causal: value at i uses closes<=i
    records = []
    start = max(warmup, len(daily) - max_bars - horizon)
    end = len(daily) - horizon - 1          # need `horizon` future bars to grade
    for i in range(start, end + 1):
        entry_window = daily[: i + 1]
        cutoff_t = daily[i]["t"]
        trend_window = [w for w in weekly if w["t"] <= cutoff_t]
        if len(trend_window) < 30:          # not enough weekly warmup yet
            continue
        a = strategy.analyze(name, entry_window, trend_window, cfg)
        future = [(daily[j]["h"], daily[j]["l"], daily[j]["c"])
                  for j in range(i + 1, i + 1 + horizon)]
        # Extension: how far price has already run — to spot chasing a top.
        c20 = daily[i - 20]["c"] if i >= 20 else daily[0]["c"]
        runup_20 = (a.price / c20 - 1) if c20 else 0.0
        base = min(daily[j]["l"] for j in range(max(0, i - 19), i + 1))
        ext_atr = (a.price - base) / a.atr if a.atr else 0.0
        # StochRSI bullish cross from oversold: %K crosses above %D while <30.
        kn, dn, kp, dp = srsi_k[i], srsi_d[i], srsi_k[i - 1], srsi_d[i - 1]
        srsi_dipcross = (kn is not None and dn is not None and kp is not None and dp is not None
                         and kp <= dp and kn > dn and (kp < 30 or kn < 30))
        records.append({
            "ticker": name,
            "t": daily[i]["t"],
            "runup_20": runup_20,          # % gain over last 20 bars
            "ext_atr": ext_atr,            # ATRs above the 20-bar base low
            "srsi_k": srsi_k[i],           # StochRSI %K (0-100)
            "srsi_dipcross": srsi_dipcross,  # bullish cross from oversold (<30)
            "factors": [list(f) for f in a.factors],   # (name, score, weight, comment)
            "trend": a.trend,
            "accumulation": a.accumulation,
            "divergence": a.divergence,
            "price": a.price,
            "atr": a.atr,
            "nearest_support": a.nearest_support,
            "nearest_resistance": a.nearest_resistance,
            "future": future,
        })
    return records


# --------------------------------------------------------------------------- #
# Cheap re-derivation from cached records                                     #
# --------------------------------------------------------------------------- #
def aggregate_score(factors, weight_overrides):
    num = den = 0.0
    for name, s, w, _ in factors:
        w = weight_overrides.get(name, w)
        num += s * w
        den += w
    return num / den if den else 0.0


def gate_buy(score, trend, accumulation, index_bull, params):
    """Long-only gate mirroring strategy.analyze() (SELLs ignored — IDX retail)."""
    if trend != "UP":
        return None
    if accumulation == "DISTRIBUTION":       # hard block, same as production
        return None
    if params.get("require_index_bull") and not index_bull:
        return None                          # IHSG risk-off: stand aside
    if score >= params["signal_strong_buy_threshold"]:
        return strategy.SIGNAL_STRONG_BUY
    if score >= params["signal_buy_threshold"]:
        return strategy.SIGNAL_BUY
    return None


def simulate_trade(entry, stop, target, future):
    """Return (R_multiple, outcome). SL checked before TP within a bar (conservative)."""
    risk_per_unit = entry - stop
    if risk_per_unit <= 0:
        return None
    rr = (target - entry) / risk_per_unit
    for h, l, c in future:
        if l <= stop:
            return -1.0, "SL"
        if h >= target:
            return rr, "TP"
    # Held to the horizon: mark to the last close.
    return (future[-1][2] - entry) / risk_per_unit, "TIME"


def evaluate(all_records, params, cost_pct=0.3):
    """Run the whole recorded universe under one param set. Returns metrics dict.

    cost_pct = round-trip transaction cost as % of price (IDX fee+spread, ~0.3%).
    Subtracted from every trade's R so the edge reported is net of costs.
    """
    rm = risk.RiskManager({"risk": params.get("risk", {})})
    wov = params.get("weight_overrides", {})
    results = []
    for r in all_records:
        score = aggregate_score(r["factors"], wov)
        sig = gate_buy(score, r["trend"], r["accumulation"], r.get("index_bull", True), params)
        if sig is None:
            continue
        if params.get("max_runup") is not None and r.get("runup_20", 0) > params["max_runup"]:
            continue
        if params.get("max_ext_atr") is not None and r.get("ext_atr", 0) > params["max_ext_atr"]:
            continue
        plan = rm.plan_trade(r["ticker"], "BUY", r["price"], r["atr"],
                             equity=100_000_000,
                             nearest_support=r["nearest_support"],
                             nearest_resistance=r["nearest_resistance"])
        if plan is None:                     # failed reward:risk / max-stop filter
            continue
        sim = simulate_trade(plan.entry, plan.stop_loss, plan.take_profit, r["future"])
        if sim is None:
            continue
        R, outcome = sim
        risk_per_unit = plan.entry - plan.stop_loss
        cost_R = (cost_pct / 100.0) * plan.entry / risk_per_unit if risk_per_unit else 0
        results.append((R - cost_R, outcome))     # net of round-trip cost
    return summarize(results)


def summarize(results):
    n = len(results)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "tp_rate": 0.0,
                "expectancy_R": 0.0, "profit_factor": 0.0}
    wins = [R for R, _ in results if R > 0]
    losses = [R for R, _ in results if R <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "tp_rate": sum(1 for _, o in results if o == "TP") / n,
        "expectancy_R": sum(R for R, _ in results) / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "avg_win_R": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_R": (-gross_loss / len(losses)) if losses else 0.0,
    }


def fmt(m):
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"trades={m['trades']:<4} win={m['win_rate']:.1%}  TP={m['tp_rate']:.1%}  "
            f"expectancy={m['expectancy_R']:+.3f}R  PF={pf}")


# --------------------------------------------------------------------------- #
# Optimization sweeps (all cheap — reuse the same records)                    #
# --------------------------------------------------------------------------- #
def base_params(**over):
    p = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULT_PARAMS.items()}
    p.update(over)
    return p


WEIGHT_VARIANTS = {
    "default": {},
    "trend-heavy": {"HTF trend": 3.5, "Market Regime": 1.5},
    "momentum-heavy": {"RSI(14)": 2.0, "MACD": 2.0, "Stochastic": 1.2},
    "momentum-extreme": {"RSI(14)": 2.5, "MACD": 2.5, "Stochastic": 1.5, "ADX": 1.5},
    "structure-heavy": {"Structure": 2.0, "Accumulation": 1.8},
    "quality": {"RSI(14)": 2.0, "MACD": 2.0, "ADX": 1.6, "Accumulation": 1.6,
                "Market Regime": 1.5},
}


def candidate_params():
    """All configs the holdout search considers (cheap: pure re-aggregation)."""
    out = []
    for idx_bull in (False, True):
        for wname, wov in WEIGHT_VARIANTS.items():
            for buy in (0.25, 0.30, 0.35, 0.40):
                for stop_mult in (1.0, 1.5, 2.0):
                    for min_rr in (1.5, 2.0, 2.5):
                        tag = ("+IHSG " if idx_bull else "") + \
                              f"{wname} buy{buy} stop{stop_mult} rr{min_rr}"
                        out.append((tag, base_params(
                            signal_buy_threshold=buy,
                            weight_overrides=dict(wov),
                            risk={"stop_atr_multiple": stop_mult,
                                  "min_reward_risk": min_rr},
                            require_index_bull=idx_bull)))
    return out


def optimize_holdout(train, test, cost_pct=0.3, min_trades=30):
    """Optimize on TRAIN, report the winner's out-of-sample TEST performance.

    Objective = net win-rate (what the user asked to maximize), guarded by a
    minimum trade count and a positive net expectancy so we don't 'win' with a
    degenerate tiny-target / huge-stop config that loses money.
    """
    base = base_params()
    base_tr, base_te = evaluate(train, base, cost_pct), evaluate(test, base, cost_pct)
    print("=== BASELINE (net of {:.2f}% cost) ===".format(cost_pct))
    print(f"  train: {fmt(base_tr)}")
    print(f"  test : {fmt(base_te)}")

    filt = base_params(require_index_bull=True)
    filt_tr, filt_te = evaluate(train, filt, cost_pct), evaluate(test, filt, cost_pct)
    print("=== BASELINE + IHSG regime filter ===")
    print(f"  train: {fmt(filt_tr)}")
    print(f"  test : {fmt(filt_te)}   <- the direct effect of the filter alone")

    scored = []
    for name, p in candidate_params():
        m = evaluate(train, p, cost_pct)
        if m["trades"] >= min_trades and m["expectancy_R"] > 0:
            scored.append((name, p, m))
    scored.sort(key=lambda t: (t[2]["win_rate"], t[2]["expectancy_R"]), reverse=True)

    print(f"\n=== TOP 8 BY TRAIN WIN-RATE (net), then validated on TEST ===")
    print(f"  {'config':<42} {'TRAIN':<40} TEST")
    winner = None
    for name, p, m_tr in scored[:8]:
        m_te = evaluate(test, p, cost_pct)
        print(f"  {name:<42} win {m_tr['win_rate']:.1%}/{m_tr['trades']:<3} "
              f"exp {m_tr['expectancy_R']:+.2f}R   |  "
              f"win {m_te['win_rate']:.1%}/{m_te['trades']:<3} exp {m_te['expectancy_R']:+.2f}R")

    # The pick: best TRAIN win-rate that STILL holds on TEST (positive net expectancy,
    # enough test trades). This is the anti-overfit gate.
    for name, p, m_tr in scored:
        m_te = evaluate(test, p, cost_pct)
        if m_te["trades"] >= 10 and m_te["expectancy_R"] > 0 and m_te["win_rate"] >= base_te["win_rate"]:
            winner = (name, p, m_tr, m_te)
            break

    print("\n" + "=" * 72)
    if winner is None:
        print("NO config beat baseline out-of-sample after costs.")
        print("-> The current edge is at the noise floor. Maximal *robust* win-rate")
        print("   is essentially the baseline; tuning harder only overfits.")
        print("=" * 72)
        return
    name, p, m_tr, m_te = winner
    print(f"WINNER (max robust win-rate): {name}")
    print(f"  TRAIN: {fmt(m_tr)}")
    print(f"  TEST : {fmt(m_te)}   <- out-of-sample, net of cost")
    print(f"  baseline TEST win-rate {base_te['win_rate']:.1%} -> {m_te['win_rate']:.1%} "
          f"({m_te['win_rate'] - base_te['win_rate']:+.1%})")
    print("\nconfig_screener.json:")
    print(f'  "strategy": {{ "signal_buy_threshold": {p["signal_buy_threshold"]} }}')
    print(f'  "risk": {json.dumps(p["risk"])}')
    if p["weight_overrides"]:
        print(f'  (factor weights in strategy.py) {json.dumps(p["weight_overrides"])}')
    print("=" * 72)


def report_window(records, label, cost_pct=0.3):
    """Plain performance report for entries inside a fixed date window
    (no train/test split, no optimization — just 'how did it do here')."""
    print(f"\n=== {label}  ({len(records)} candidate bars, net of {cost_pct:.2f}% cost) ===")
    base = base_params()
    filt = base_params(require_index_bull=True)
    mb, mf = evaluate(records, base, cost_pct), evaluate(records, filt, cost_pct)
    print(f"  strategy as-is        : {fmt(mb)}")
    print(f"  + IHSG regime filter  : {fmt(mf)}")


# --------------------------------------------------------------------------- #
def _selftest():
    """The money path: SL-before-TP, TP hit, and time exit must grade right."""
    # entry 100, stop 90 (risk 10), target 120 (rr 2)
    r, o = simulate_trade(100, 90, 120, [(105, 95, 100), (121, 100, 120)])
    assert o == "TP" and abs(r - 2.0) < 1e-9, (r, o)
    r, o = simulate_trade(100, 90, 120, [(105, 89, 92)])            # low pierces stop
    assert o == "SL" and r == -1.0, (r, o)
    r, o = simulate_trade(100, 90, 120, [(125, 89, 120)])           # both -> SL first
    assert o == "SL" and r == -1.0, (r, o)
    r, o = simulate_trade(100, 90, 120, [(105, 95, 110)])           # neither -> time
    assert o == "TIME" and abs(r - 1.0) < 1e-9, (r, o)              # (110-100)/10
    # aggregate_score: override doubles trend weight
    f = [["HTF trend", 1.0, 2.5, ""], ["RSI(14)", -1.0, 1.5, ""]]
    assert abs(aggregate_score(f, {}) - (2.5 - 1.5) / 4.0) < 1e-9
    assert abs(aggregate_score(f, {"HTF trend": 5.0}) - (5.0 - 1.5) / 6.5) < 1e-9
    # gate: needs UP trend and not distribution
    p = base_params()
    assert gate_buy(0.5, "UP", "NEUTRAL", True, p) == strategy.SIGNAL_STRONG_BUY
    assert gate_buy(0.5, "SIDEWAYS", "NEUTRAL", True, p) is None
    assert gate_buy(0.5, "UP", "DISTRIBUTION", True, p) is None
    assert gate_buy(0.32, "UP", "NEUTRAL", True, p) == strategy.SIGNAL_BUY
    # index filter: risk-off blocks only when required
    pf = base_params(require_index_bull=True)
    assert gate_buy(0.5, "UP", "NEUTRAL", False, pf) is None
    assert gate_buy(0.5, "UP", "NEUTRAL", True, pf) == strategy.SIGNAL_STRONG_BUY
    assert gate_buy(0.5, "UP", "NEUTRAL", False, p) == strategy.SIGNAL_STRONG_BUY
    print("selftest OK")


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return 0

    horizon = 20
    universe = "IDX30"
    d_from = d_to = None
    tickers = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--horizon", "-H") and i + 1 < len(argv):
            horizon = int(argv[i + 1]); i += 1
        elif a in ("--universe", "-u") and i + 1 < len(argv):
            universe = argv[i + 1].upper(); i += 1
        elif a == "--from" and i + 1 < len(argv):
            d_from = datetime.strptime(argv[i + 1], "%Y-%m-%d").date(); i += 1
        elif a == "--to" and i + 1 < len(argv):
            d_to = datetime.strptime(argv[i + 1], "%Y-%m-%d").date(); i += 1
        elif a in ("-h", "--help"):
            print(__doc__); return 0
        else:
            tickers.append(a.upper())
        i += 1

    if not tickers:
        tickers = universes.get_universe(universe)
    cfg = {}   # rule engine only; thresholds/weights swept from records

    print(f"Walk-forward backtest: {len(tickers)} tickers, horizon {horizon} bars "
          f"(1d entry / 1wk trend)\n")

    try:
        regime_map = index_regime_map(fetch_candles(INDEX_TICKER, "1d", count=520))
        print(f"IHSG regime loaded ({sum(regime_map.values())}/{len(regime_map)} days bullish)\n")
    except Exception as e:
        print(f"IHSG fetch failed ({e}) — regime filter disabled\n")
        regime_map = {}

    all_recs = []
    for t in tickers:
        try:
            recs = build_records(t, cfg, horizon=horizon)   # chronological per ticker
            annotate_index(recs, regime_map)
            all_recs.extend(recs)
        except Exception as e:
            print(f"  {idx_data.display_symbol(t):<6} skipped: {e}")

    if not all_recs:
        print("\nNo data — is yfinance reachable? Try fewer tickers or check network.")
        return 1

    # Fixed date window (e.g. --from 2026-01-01 --to 2026-03-31): just report it.
    if d_from or d_to:
        lo = d_from or datetime.min.date()
        hi = d_to or datetime.max.date()
        window = [r for r in all_recs if lo <= _bar_date(r["t"]) <= hi]
        if not window:
            print(f"\nNo entries between {lo} and {hi} in the fetched data.")
            return 1
        report_window(window, f"WINDOW {lo} .. {hi}")
        return 0

    # Otherwise: time-split optimization over the whole fetched history.
    all_recs.sort(key=lambda r: r["t"])
    cut = int(len(all_recs) * 0.7)
    train, test = all_recs[:cut], all_recs[cut:]
    print(f"\nTrain bars: {len(train)}   Test bars: {len(test)}   (time-split, no lookahead)\n")
    optimize_holdout(train, test)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
