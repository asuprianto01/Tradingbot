"""
Persist per-bar feature records (post-analyze) for fast reuse.

Building the records calls strategy.analyze on every bar of every stock — the
slow part of any backtest / rule-research / training run. Saving that result to
disk means later runs LOAD in seconds instead of re-analyzing for minutes.

Records are rule-AGNOSTIC: each bar stores trend, accumulation, divergence,
StochRSI, every confluence factor (name→score→weight→note), structure,
price/ATR and the forward price path (`future`). That is enough to derive ANY
signal rule and simulate the trade — so you build once, then test many rules.

Regenerate when strategy.analyze's feature logic changes (metadata carries the
build date). Fetch itself is already day-cached in .backtest_cache/.

CLI:
    python dataset.py            # build+save universe ALL
    python dataset.py LQ45       # build+save a smaller universe
"""

import gzip
import os
import pickle
import sys
import time

import backtest_optimizer as bo
import universes

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset")


def path_for(universe):
    return os.path.join(DIR, f"records_{universe}.pkl.gz")


def build(universe="ALL", warmup=60, horizon=20, progress=True):
    """Walk every stock in `universe`; return (records, meta)."""
    tickers = universes.get_universe(universe)
    recs, done, fail = [], 0, 0
    for t in tickers:
        try:
            recs.extend(bo.build_records(t, {}, horizon=horizon, warmup=warmup))
            done += 1
        except Exception:
            fail += 1
        if progress and (done + fail) % 100 == 0:
            print(f"... {done + fail}/{len(tickers)} ({len(recs)} bar, {fail} gagal)", flush=True)
    meta = {"universe": universe, "warmup": warmup, "horizon": horizon,
            "tickers_ok": done, "tickers_fail": fail, "n_records": len(recs),
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return recs, meta


def save(records, meta, path=None):
    path = path or path_for(meta["universe"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as f:
        pickle.dump({"meta": meta, "records": records}, f, protocol=pickle.HIGHEST_PROTOCOL)
    return path, os.path.getsize(path)


def load(path_or_universe):
    """Load records+meta. Accepts a file path or a universe name."""
    path = path_or_universe if path_or_universe.endswith(".gz") else path_for(path_or_universe)
    with gzip.open(path, "rb") as f:
        d = pickle.load(f)
    return d["records"], d["meta"]


def load_or_build(universe="ALL", warmup=60, horizon=20):
    """Load the saved dataset if present, else build+save it."""
    p = path_for(universe)
    if os.path.exists(p):
        return load(p)
    recs, meta = build(universe, warmup, horizon)
    save(recs, meta, p)
    return recs, meta


if __name__ == "__main__":
    uni = sys.argv[1].upper() if len(sys.argv) > 1 else "ALL"
    recs, meta = build(uni)
    p, size = save(recs, meta)
    print(f"\nSaved {meta['n_records']:,} records "
          f"({meta['tickers_ok']} saham ok, {meta['tickers_fail']} gagal) "
          f"-> {p} ({size / 1e6:.1f} MB)")
