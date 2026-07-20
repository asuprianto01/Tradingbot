"""
Quality-Value screener for IDX stocks.

Scores each stock on two independent pillars, each 0-100:

  * VALUE   — how cheap is it? (P/E, P/B, dividend yield, PEG)
  * QUALITY — how good is the business? (ROE, ROA, profit margin,
              earnings growth, debt/equity)

Each metric is mapped to 0-100 by simple "great .. bad" bands, then a pillar
is the average of its *available* metrics (missing data doesn't drag the score
to zero). The two pillars combine into a verdict — the whole point of quality-
value investing is to buy good businesses cheaply while avoiding value traps
(cheap because they deserve to be).

Read-only. Data is Yahoo Finance and can lag / be incomplete. Not financial
advice — treat this as a first-pass filter, then read the actual reports.

Usage:
    python qvscreener.py               # score the whole watchlist
    python qvscreener.py BBCA          # detailed metric breakdown for one stock
    python qvscreener.py --sort value  # sort by value / quality / qv (default qv)
"""

import json
import sys

import fundamentals
import universes

# (great, bad) thresholds. For "lower is better" metrics great < bad; the
# scorer detects direction automatically.
VALUE_METRICS = {
    "pe":             {"great": 10.0, "bad": 25.0, "label": "P/E"},
    "pb":             {"great": 1.0,  "bad": 4.0,  "label": "P/B"},
    "dividend_yield": {"great": 6.0,  "bad": 0.0,  "label": "Div Yield %"},
    "peg":            {"great": 1.0,  "bad": 3.0,  "label": "PEG"},
}
QUALITY_METRICS = {
    "roe":               {"great": 20.0, "bad": 5.0,   "label": "ROE %"},
    "roa":               {"great": 10.0, "bad": 1.0,   "label": "ROA %"},
    "profit_margin":     {"great": 20.0, "bad": 2.0,   "label": "Profit Margin %"},
    "net_profit_cagr_3y":{"great": 15.0, "bad": -5.0,  "label": "Net Profit Growth 3Y %"},
    "revenue_cagr_3y":   {"great": 12.0, "bad": -5.0,  "label": "Revenue Growth 3Y %"},
    "debt_to_equity":    {"great": 25.0, "bad": 150.0, "label": "Debt/Equity %"},
}


def score_metric(value, great, bad):
    """Map a value onto 0..100 by linear interpolation between great and bad."""
    if value is None:
        return None
    if great == bad:
        return 50.0
    # Clamp into [0,100] whichever direction "better" points.
    lo, hi = (bad, great) if great > bad else (great, bad)
    frac = (value - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    # If great > bad, higher value = better -> frac already points the right way.
    # If great < bad (lower is better), invert.
    return round((frac if great > bad else 1 - frac) * 100, 1)


def _pillar_score(fund, metrics):
    scores = {}
    for key, spec in metrics.items():
        s = score_metric(fund.get(key), spec["great"], spec["bad"])
        if s is not None:
            scores[key] = s
    pillar = round(sum(scores.values()) / len(scores), 1) if scores else None
    return pillar, scores


def verdict(quality, value):
    """Combine the two pillars into a plain-language call."""
    if quality is None or value is None:
        return "Data kurang"
    hi = 60
    lo = 40
    if quality >= hi and value >= hi:
        return "Quality Value - menarik"
    if quality >= hi and value < lo:
        return "Bagus tapi mahal"
    if quality < lo and value >= hi:
        return "Murah tapi berisiko"
    if quality < lo and value < lo:
        return "Hindari"
    return "Biasa saja"


def evaluate(ticker):
    """Fetch + score one ticker. Returns a result dict."""
    fund = fundamentals.get_fundamentals(ticker)
    q, q_scores = _pillar_score(fund, QUALITY_METRICS)
    v, v_scores = _pillar_score(fund, VALUE_METRICS)
    qv = round((q + v) / 2, 1) if (q is not None and v is not None) else None
    return {
        "fund": fund,
        "quality": q,
        "value": v,
        "qv": qv,
        "verdict": verdict(q, v),
        "quality_scores": q_scores,
        "value_scores": v_scores,
    }


def load_tickers(path="config_screener.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("screener", {}).get(
                "tickers", ["BBCA", "BBRI", "BMRI", "TLKM", "ASII"])
    except FileNotFoundError:
        return ["BBCA", "BBRI", "BMRI", "TLKM", "ASII"]


def _fmt(x, spec="{:.1f}"):
    return spec.format(x) if x is not None else "-"


def run(sort_key="qv", universe="Selected"):
    tickers = universes.get_universe(universe, load_tickers())
    print(f"Menilai {len(tickers)} saham ({universe}, Quality + Value) ...\n")

    results, errors = [], []
    for t in tickers:
        try:
            results.append(evaluate(t))
        except Exception as e:
            errors.append((t, str(e)))
            print(f"  ! {t:<6} dilewati: {e}")
    if errors:
        print()

    key_map = {"qv": "qv", "value": "value", "quality": "quality"}
    sk = key_map.get(sort_key, "qv")
    results.sort(key=lambda r: (r[sk] is not None, r[sk] or 0), reverse=True)

    header = (f"{'SAHAM':<7} {'HARGA':>9} {'QUAL':>5} {'VAL':>5} {'QV':>5} {'F':>3}   "
              f"{'P/E':>6} {'ROE%':>6} {'NP3y%':>7} {'REV3y%':>7} {'D/E%':>6} {'MoS%':>7}   "
              f"VERDICT")
    print(header)
    print("-" * len(header))
    for r in results:
        f = r["fund"]
        fscore = f"{f['f_score']}/9" if f.get("f_score") is not None else "-"
        print(f"{f['ticker']:<7} {_fmt(f['price'], '{:,.0f}'):>9} "
              f"{_fmt(r['quality']):>5} {_fmt(r['value']):>5} {_fmt(r['qv']):>5} {fscore:>3}   "
              f"{_fmt(f['pe']):>6} {_fmt(f['roe']):>6} {_fmt(f['net_profit_cagr_3y']):>7} "
              f"{_fmt(f['revenue_cagr_3y']):>7} {_fmt(f['debt_to_equity']):>6} "
              f"{_fmt(f['margin_of_safety']):>7}   {r['verdict']}")

    if errors:
        print(f"\n({len(errors)} saham gagal diambil datanya)")
    print("\nData: Yahoo Finance (bisa telat/tidak lengkap). Bukan nasihat keuangan.")


def report_one(ticker):
    r = evaluate(ticker)
    f = r["fund"]
    print(f"=== {f['ticker']}  {f.get('name') or ''} ===")
    print(f"Sektor: {f.get('sector') or '-'}   Harga: {_fmt(f['price'], '{:,.0f}')}")
    print(f"\nQUALITY: {_fmt(r['quality'])}/100    VALUE: {_fmt(r['value'])}/100    "
          f"QV: {_fmt(r['qv'])}/100")
    print(f"VERDICT: {r['verdict']}\n")

    print("Value:")
    for key, spec in VALUE_METRICS.items():
        s = r["value_scores"].get(key)
        print(f"  {spec['label']:<20} {_fmt(f.get(key)):>10}   skor {_fmt(s, '{:.0f}') if s is not None else 'n/a':>4}")
    print("Quality:")
    for key, spec in QUALITY_METRICS.items():
        s = r["quality_scores"].get(key)
        print(f"  {spec['label']:<24} {_fmt(f.get(key)):>10}   skor {_fmt(s, '{:.0f}') if s is not None else 'n/a':>4}")

    # Piotroski F-Score breakdown
    fs = f.get("f_score")
    if fs is not None:
        na = f.get("f_score_na", 0)
        na_note = f" ({na} kriteria n/a)" if na else ""
        print(f"\nPiotroski F-Score: {fs}/9{na_note}")
        for label, awarded in f.get("f_criteria", []):
            mark = "OK " if awarded is True else ("-- " if awarded is False else "n/a")
            print(f"  [{mark}] {label}")

    # Graham fair value / margin of safety
    fv = f.get("fair_value")
    if fv is not None:
        mos = f.get("margin_of_safety")
        verdict_mos = "UNDERVALUED" if mos and mos > 0 else "overvalued"
        print(f"\nHarga wajar (Graham): {fv:,.0f}   |   Harga kini: {_fmt(f['price'], '{:,.0f}')}")
        print(f"Margin of Safety: {mos:+.1f}%  ({verdict_mos})")
    else:
        print("\nMargin of Safety: n/a (EPS/nilai buku negatif atau kosong)")


def main(argv):
    sort_key = "qv"
    universe = "Selected"
    tickers = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--sort", "-s") and i + 1 < len(argv):
            sort_key = argv[i + 1].lower()
            i += 1
        elif a in ("--universe", "-u") and i + 1 < len(argv):
            universe = argv[i + 1].upper() if argv[i + 1].lower() != "selected" else "Selected"
            i += 1
        elif a in ("-h", "--help"):
            print(__doc__)
            print("Universe: " + ", ".join(universes.available()))
            return 0
        else:
            tickers.append(a)
        i += 1

    if tickers:
        for t in tickers:
            try:
                report_one(t)
                print()
            except Exception as e:
                print(f"{t}: gagal - {e}\n")
    else:
        run(sort_key, universe)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
