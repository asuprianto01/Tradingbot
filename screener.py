"""
IDX stock screener.

Scans a configurable list of Indonesian stocks, runs the same top-down
confluence analysis the trading bot uses (strategy.analyze), and prints a
ranked table of candidates. Read-only: it never places an order.

Timeframes for stocks: daily (1d) for entry timing, weekly (1wk) for trend —
the stock-market equivalent of the bot's 1h / 4h split.

Usage:
    python screener.py                 # screen the whole configured watchlist
    python screener.py BBCA            # full factor-by-factor report for one stock
    python screener.py BBCA TLKM       # full reports for several stocks
    python screener.py --all           # show every stock, not just BUY signals
    python screener.py --min BUY       # minimum signal to show (default from config)

Config lives in config.json under the "screener" key.
"""

import json
import sys

import idx_data
import risk
import strategy
import universes

SIGNAL_RANK = {
    strategy.SIGNAL_STRONG_SELL: 0,
    strategy.SIGNAL_SELL: 1,
    strategy.SIGNAL_NEUTRAL: 2,
    strategy.SIGNAL_BUY: 3,
    strategy.SIGNAL_STRONG_BUY: 4,
}

DEFAULTS = {
    "tickers": ["BBCA", "BBRI", "BMRI", "TLKM", "ASII", "UNVR", "GOTO", "ANTM"],
    "entry_timeframe": "1d",
    "trend_timeframe": "1wk",
    "min_signal": "BUY",
    "capital": 100_000_000,
    "lot_size": 100,
    "min_turnover": 3_000_000_000,   # Rp/hari — sinyal di saham lebih tipis tak dijadikan trade plan
    "require_market_risk_on": True,  # gate regime: tak buat rencana beli saat IHSG risk-off
}

BUY_SIGNALS = (strategy.SIGNAL_BUY, strategy.SIGNAL_STRONG_BUY)


def load_config(path="config_screener.json"):
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            full = json.load(f)
        cfg.update(full.get("screener", {}))
        # Keep the shared risk block so RiskManager can size positions.
        cfg["risk"] = full.get("risk", {})
        # Keep strategy thresholds/flags (e.g. stochrsi_smartmoney_entry)
        cfg["strategy"] = full.get("strategy", {})
        # Ambang likuiditas (top-level override, fallback ke DEFAULTS)
        cfg["min_turnover"] = full.get("min_turnover", cfg["min_turnover"])
        cfg["require_market_risk_on"] = full.get("require_market_risk_on",
                                                 cfg["require_market_risk_on"])
        # Config exit (trailing stop) — dibaca oleh app/tracker
        cfg["exit"] = full.get("exit", {"trailing": True, "trail_r_mult": 1.5})
    except FileNotFoundError:
        pass
    return cfg


def save_tickers(tickers, path="config_screener.json"):
    """Persist the 'Selected' watchlist to config_screener.json (screener.tickers)."""
    try:
        with open(path, encoding="utf-8") as f:
            full = json.load(f)
    except FileNotFoundError:
        full = {}
    full.setdefault("screener", {})["tickers"] = tickers
    with open(path, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2)


def build_recommendation(analysis, cfg):
    """
    Turn a BUY/STRONG_BUY analysis into an actionable long plan sized in lots.
    Returns a dict, or None if no valid plan (bad reward:risk, capital too small).
    Retail IDX trading is long-only, so SELL signals get no trade plan.
    """
    if analysis.signal not in BUY_SIGNALS:
        return None

    # Gerbang likuiditas: sinyal di saham tipis tak bisa dieksekusi (slippage +
    # ARB). Backtest menunjukkan hasil saham illikuid itu fiksi. Tolak di bawah ambang.
    min_turnover = cfg.get("min_turnover", 0)
    if min_turnover and (analysis.turnover or 0) < min_turnover:
        return None

    # Gate regime IHSG: jangan buat rencana beli saat pasar risk-off (crash).
    # Bagian dari strategi pemenang; test-set (crash 2026) tetap +1.5R karena gate ini.
    if cfg.get("require_market_risk_on") and not idx_data.market_risk_on():
        return None

    rm = risk.RiskManager(cfg)
    plan = rm.plan_trade(
        analysis.instrument, "BUY", analysis.price, analysis.atr,
        equity=cfg["capital"],
        nearest_support=analysis.nearest_support,
        nearest_resistance=analysis.nearest_resistance,
    )
    if plan is None:
        return None

    lot_size = cfg["lot_size"]
    lots = int(plan.quantity // lot_size)
    shares = lots * lot_size
    cost = shares * plan.entry
    actual_risk = shares * (plan.entry - plan.stop_loss)

    targets = build_targets(plan.entry, plan.stop_loss, plan.take_profit,
                            analysis.resistance_levels)
    ez_mult = cfg.get("risk", {}).get("entry_zone_atr_mult", 0.5)
    entry_low, entry_high = build_entry_zone(
        plan.entry, plan.stop_loss, analysis.atr, analysis.nearest_support,
        atr_mult=ez_mult)
    return {
        "signal": analysis.signal,
        "entry": plan.entry,            # market price (upper bound of zone)
        "entry_low": entry_low,         # ideal accumulation / pullback bound
        "entry_high": entry_high,
        "stop": plan.stop_loss,
        "target": plan.take_profit,     # TP1 (primary, structure-capped 2R)
        "reward_risk": plan.reward_risk,
        "targets": targets,             # list of {price, gain_pct, rr} incl. TP1
        "lots": lots,
        "shares": shares,
        "cost": cost,
        "risk_amount": actual_risk,
    }


MTF_ARROWS = {"UP": "⬆", "DOWN": "⬇", "SIDEWAYS": "➡"}


def mtf_confirmation(ticker, timeframes):
    """
    Classify the trend on each of several timeframes for multi-timeframe
    confirmation. Returns a list of (timeframe, trend) in the given order.
    A signal is 'confirmed' when the higher/lower timeframes agree with it.
    """
    out = []
    for tf in timeframes:
        try:
            candles = idx_data.get_candles(ticker, tf, count=400)
            out.append((tf, strategy.timeframe_trend(candles)))
        except Exception:
            out.append((tf, "SIDEWAYS"))
    return out


def mtf_alignment(trends, signal):
    """How many timeframes agree with the signal's direction (agree, total)."""
    dirs = [t for _, t in trends]
    total = len(dirs)
    if signal in BUY_SIGNALS:
        agree = dirs.count("UP")
    elif signal in (strategy.SIGNAL_SELL, strategy.SIGNAL_STRONG_SELL):
        agree = dirs.count("DOWN")
    else:
        agree = 0
    return agree, total


def build_entry_zone(price, stop, atr, nearest_support, atr_mult=0.5):
    """
    Zona beli: dari harga pasar (batas atas) turun ke level akumulasi ideal
    (batas bawah). Batas bawah = pullback `atr_mult × ATR` atau support terdekat.
    Membeli di batas bawah memberi reward:risk lebih baik.

    atr_mult berbeda per gaya (diset dari cfg_for_style):
      Swing 0.5 × 1D ATR → ~1-2%  — pas
      Intraday 1.0 × 1H ATR → ~0.5-1%  — perlu lebih lebar agar bermakna
      Long Term 0.3 × 1W ATR → ~1.5-3%  — 1W ATR besar, zona dipersempit
    """
    high = price
    low = price - atr_mult * atr
    # Jika support ada di dalam zona (max 1.5 ATR dari harga), jadikan batas bawah.
    if nearest_support and stop < nearest_support < price and (price - nearest_support) <= 1.5 * atr:
        low = nearest_support
    low = max(low, stop + 0.1 * atr)         # tidak boleh menyentuh/melewati stop
    low = min(low, high)
    return round(low), round(high)


def build_targets(entry, stop, tp1, resistance_levels, max_targets=3):
    """
    Hybrid take-profit ladder. TP1 is the primary (already min(2R, nearest
    resistance)); TP2/TP3 are the next swing-high resistances above TP1, so the
    trader can see the further upside. Each target carries its % gain and the
    reward:risk it would represent.
    """
    risk_per_share = entry - stop
    prices = [tp1]
    for r in sorted(resistance_levels or []):
        if r > tp1 * 1.001 and len(prices) < max_targets:   # comfortably above TP1
            prices.append(r)

    targets = []
    for p in prices:
        targets.append({
            "price": p,
            "gain_pct": (p - entry) / entry * 100 if entry else None,
            "rr": (p - entry) / risk_per_share if risk_per_share > 0 else None,
        })
    return targets


def screen_one(ticker, entry_tf, trend_tf, cfg=None):
    """Fetch data and analyze a single ticker. Returns Analysis or raises."""
    if cfg is None:
        cfg = load_config()
    entry = idx_data.get_candles(ticker, entry_tf, count=400)
    trend = idx_data.get_candles(ticker, trend_tf, count=400)
    name = idx_data.display_symbol(ticker)
    return strategy.analyze(name, entry, trend, config=cfg)


def analyze_styles(ticker, styles, cfg=None):
    """
    Analyze one ticker for several trading styles at once.
    `styles`: dict name -> (entry_tf, trend_tf). Each needed timeframe is
    fetched only ONCE and reused across styles (keeps request count down).
    Returns dict name -> Analysis.
    """
    if cfg is None:
        cfg = load_config()
    needed = set()
    for entry_tf, trend_tf in styles.values():
        needed.add(entry_tf)
        needed.add(trend_tf)
    candles = {tf: idx_data.get_candles(ticker, tf, count=400) for tf in needed}
    name = idx_data.display_symbol(ticker)
    return {sname: strategy.analyze(name, candles[e], candles[t], config=cfg)
            for sname, (e, t) in styles.items()}


def run_screen(cfg, min_signal, show_all):
    tickers = cfg["tickers"]
    entry_tf = cfg["entry_timeframe"]
    trend_tf = cfg["trend_timeframe"]
    min_rank = SIGNAL_RANK.get(min_signal, SIGNAL_RANK[strategy.SIGNAL_BUY])

    print(f"Screening {len(tickers)} IDX stocks "
          f"(entry {entry_tf} / trend {trend_tf}) ...\n")

    results = []
    errors = []
    for t in tickers:
        try:
            a = screen_one(t, entry_tf, trend_tf, cfg)
            results.append(a)
        except Exception as e:  # keep scanning even if one ticker fails
            errors.append((idx_data.display_symbol(t), str(e)))
            print(f"  ! {idx_data.display_symbol(t):<6} skipped: {e}")

    if errors:
        print()

    # Rank: strongest signal first, then by confluence score.
    results.sort(key=lambda a: (SIGNAL_RANK.get(a.signal, 2), a.score), reverse=True)

    shown = [a for a in results if show_all or SIGNAL_RANK.get(a.signal, 2) >= min_rank]

    _print_table(shown)

    hidden = len(results) - len(shown)
    if hidden > 0 and not show_all:
        print(f"\n({hidden} stock(s) below {min_signal} hidden - use --all to show them)")
    if errors:
        print(f"({len(errors)} stock(s) skipped due to data errors)")

    _print_recommendations(results, cfg)


def _print_recommendations(results, cfg):
    """Actionable long setups for the BUY/STRONG_BUY names, sized in lots."""
    recos = []
    for a in results:
        reco = build_recommendation(a, cfg)
        if reco:
            recos.append((a, reco))

    print(f"\n=== REKOMENDASI (modal Rp {cfg['capital']:,.0f}, "
          f"risiko {cfg['risk'].get('risk_per_trade_pct', 1.0)}%/trade) ===")

    if not recos:
        print("Tidak ada setup BUY yang memenuhi syarat reward:risk hari ini.")
        # Point out any BUY signals that failed the risk filter so it's not a mystery.
        weak = [a.instrument for a in results if a.signal in BUY_SIGNALS]
        if weak:
            print(f"(Sinyal BUY tapi reward:risk kurang layak: {', '.join(weak)})")
        return

    for a, r in recos:
        print(f"\n  {a.instrument}  [{r['signal']}]")
        print(f"    Zona beli : {r['entry_low']:,.0f} - {r['entry_high']:,.0f}  "
              f"(pasar {r['entry']:,.0f})")
        print(f"    Stop loss : {r['stop']:,.0f}")
        for n, tgt in enumerate(r["targets"], start=1):
            print(f"    TP{n}       : {tgt['price']:,.0f}   "
                  f"(+{tgt['gain_pct']:.1f}%, R:R {tgt['rr']:.1f}:1)")
        if r["lots"] >= 1:
            print(f"    Ukuran    : {r['lots']} lot ({r['shares']:,} lembar)  "
                  f"~ Rp {r['cost']:,.0f}   Risiko: Rp {r['risk_amount']:,.0f}")
        else:
            print(f"    Ukuran    : < 1 lot pada modal ini (butuh modal lebih besar "
                  f"atau risiko/trade lebih tinggi)")


def _print_table(analyses):
    if not analyses:
        print("No stocks matched the filter.")
        return

    header = f"{'STOCK':<7} {'PRICE':>12} {'TREND':<9} {'SIGNAL':<12} {'SCORE':>7}   STRUCTURE (sup / res)"
    print(header)
    print("-" * len(header))
    for a in analyses:
        sup = f"{a.nearest_support:,.0f}" if a.nearest_support else "-"
        res = f"{a.nearest_resistance:,.0f}" if a.nearest_resistance else "-"
        print(f"{a.instrument:<7} {a.price:>12,.2f} {a.trend:<9} {a.signal:<12} "
              f"{a.score:>+7.2f}   {sup} / {res}")


def report_one(tickers, cfg):
    """Print the full factor breakdown for one or more specific stocks."""
    entry_tf = cfg["entry_timeframe"]
    trend_tf = cfg["trend_timeframe"]
    for t in tickers:
        try:
            a = screen_one(t, entry_tf, trend_tf)
            print(a.report())
            reco = build_recommendation(a, cfg)
            if reco:
                print()
                tps = "  |  ".join(
                    f"TP{n} {t['price']:,.0f} (+{t['gain_pct']:.1f}%)"
                    for n, t in enumerate(reco["targets"], start=1))
                print(f"Rekomendasi: BELI  zona {reco['entry_low']:,.0f}-{reco['entry_high']:,.0f}"
                      f"  |  SL {reco['stop']:,.0f}  |  R:R {reco['reward_risk']:.1f}:1")
                print(f"             {tps}")
                if reco["lots"] >= 1:
                    print(f"            {reco['lots']} lot ({reco['shares']:,} lembar) "
                          f"~ Rp {reco['cost']:,.0f}, risiko Rp {reco['risk_amount']:,.0f}")
            elif a.signal in BUY_SIGNALS:
                print("\nRekomendasi: sinyal BUY tapi reward:risk belum layak - tunggu setup lebih baik.")
            else:
                print("\nRekomendasi: hindari / bukan setup beli (sinyal bukan BUY).")
            print()
        except Exception as e:
            print(f"{idx_data.display_symbol(t)}: could not analyze - {e}\n")


def main(argv):
    cfg = load_config()

    show_all = False
    min_signal = cfg["min_signal"]
    universe = "Selected"
    tickers = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--all", "-a"):
            show_all = True
        elif arg in ("--min", "-m") and i + 1 < len(argv):
            min_signal = argv[i + 1].upper()
            i += 1
        elif arg in ("--universe", "-u") and i + 1 < len(argv):
            universe = argv[i + 1].upper() if argv[i + 1].lower() != "selected" else "Selected"
            i += 1
        elif arg in ("-h", "--help"):
            print(__doc__)
            print("Universe: " + ", ".join(universes.available()))
            return 0
        else:
            tickers.append(arg)
        i += 1

    if tickers:
        # Specific stocks named -> full report for each.
        report_one(tickers, cfg)
    else:
        cfg["tickers"] = universes.get_universe(universe, cfg["tickers"])
        run_screen(cfg, min_signal, show_all)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
