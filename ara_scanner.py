"""
Pendeteksi kandidat breakout untuk saham IDX (ARA gocap ATAU awal tren likuid).

Berbeda dari screener.py yang berbasis confluence trend-following (butuh tren naik
sudah terbentuk), modul ini memburu saham SEBELUM meledak. Dua lapis:

  1. EOD  (scan_eod)      - tanda coil/akumulasi dari candle harian.
                            Output: watchlist untuk besok. Probabilistik, bukan timing.
  2. Intraday (scan_intraday) - lonjakan volume sesi berjalan.
                            Output: alert saat markup/breakout mulai. Actionable.

Dua profil ambang (pilih via --profile):
  gocap  - saham receh/low-float; target ARA; volume bisa 30x; metrik RVOL vs
           volume sehari penuh kemarin.
  liquid - saham likuid, susah dimanipulasi; target awal leg tren 5-15% (bukan
           ARA); metrik RVOL time-of-day (cum vol jam-T vs baseline jam-T yang
           sama beberapa sesi terakhir) - jauh lebih tepat untuk volume bertahap.

Semua indikator direuse dari indicators.py. Read-only; tidak pernah order.

Usage:
    python ara_scanner.py                          # EOD gocap watchlist
    python ara_scanner.py --profile liquid         # EOD saham likuid
    python ara_scanner.py --intraday               # intraday gocap (RVOL vs kemarin)
    python ara_scanner.py --intraday --profile liquid   # intraday likuid (RVOL time-of-day)
    python ara_scanner.py LAPD MINA                # ticker spesifik
"""

import sys
from datetime import datetime
from statistics import mean, median

import idx_data
import indicators as ind
import notify_telegram
import screener  # reuse load_config / watchlist plumbing

# Ambang per profil. EOD sedikit dilonggarkan untuk likuid (volume kurang spiky,
# gerakan lebih kecil sehingga "extended" tercapai lebih cepat). Intraday:
#   dayvol -> cum vol hari ini / volume PENUH kemarin (cocok gocap yg meledak).
#   tod    -> cum vol s/d jam-T / median cum vol s/d jam-T sesi2 sebelumnya.
PROFILES = {
    "gocap": {
        "DRYUP_MULT": 0.5, "SQUEEZE_MULT": 0.7, "NEAR_SUP_ATR": 1.5,
        "CMF_ACCUM": 0.05, "EXT_MAX": 1.15,
        "MODE": "dayvol", "RVOL_MIN": 0.8, "GAP_MIN": 1.03,
    },
    "liquid": {
        "DRYUP_MULT": 0.6, "SQUEEZE_MULT": 0.8, "NEAR_SUP_ATR": 2.0,
        "CMF_ACCUM": 0.03, "EXT_MAX": 1.10,
        "MODE": "tod", "RVOL_MIN": 1.7, "GAP_MIN": 1.012,
    },
}


def scan_eod(ticker, candles, prof=None):
    """Skor coil/akumulasi dari candle harian. Return dict atau None jika data kurang.

    dry_up wajib (inti tanda float mengetat); sisanya menambah skor.
    Kandidat = dry_up TERPENUHI, belum extended, dan skor total >= 2.
    """
    prof = prof or PROFILES["gocap"]
    if len(candles) < 30:
        return None
    price = candles[-1]["c"]

    vsma = ind.volume_sma(candles, 20)[-1]
    vol_recent = mean(c["v"] for c in candles[-3:])
    dry_up = bool(vsma) and vol_recent < prof["DRYUP_MULT"] * vsma

    atr = ind.atr(candles, 14)[-1]
    range_recent = mean(c["h"] - c["l"] for c in candles[-3:])
    squeeze = bool(atr) and range_recent < prof["SQUEEZE_MULT"] * atr

    _, supports = ind.swing_levels(candles, lookback=5)
    _, sup = ind.nearest_levels(price, [], supports)
    above_base = bool(sup) and atr and (price - sup) <= prof["NEAR_SUP_ATR"] * atr

    obv = ind.obv(candles)
    cmf_now = ind.cmf(candles, 20)[-1]
    accum = ind.obv_divergence(candles, obv, lookback=30) == "BULLISH" \
        or (cmf_now is not None and cmf_now > prof["CMF_ACCUM"])

    low10 = min(c["l"] for c in candles[-10:])
    extended = price > prof["EXT_MAX"] * low10   # sudah lari duluan -> bukan coil

    factors = {"dry_up": dry_up, "squeeze": squeeze,
               "above_base": above_base, "accum": accum}
    score = sum(factors.values())
    return {
        "ticker": idx_data.display_symbol(ticker),
        "price": price, "support": sup, "score": score, "factors": factors,
        "extended": extended,
        "candidate": dry_up and not extended and score >= 2,
        "reasons": [k for k, v in factors.items() if v],
    }


def _sessions_by_minute(candles):
    """Kelompokkan candle intraday per tanggal WIB -> list (menit_sejak_00, v, h).

    Menit dipakai agar 'cum vol s/d jam-T' bisa dibandingkan antar sesi pada slot
    waktu yang sama (dasar RVOL time-of-day).
    """
    out = {}
    for c in candles:
        dt = datetime.fromtimestamp(c["t"] / 1000, idx_data.WIB)
        out.setdefault(dt.date(), []).append((dt.hour * 60 + dt.minute, c["v"], c["h"]))
    return out


def _tod_rvol(sessions):
    """RVOL time-of-day: cum vol hari ini s/d menit terakhir dibanding median cum
    vol sesi-sesi sebelumnya s/d menit yang sama. Return (rvol, cutoff, day_high)."""
    dates = sorted(sessions)
    today = sessions[dates[-1]]
    cutoff = max(m for m, _, _ in today)
    today_cum = sum(v for m, v, _ in today if m <= cutoff)
    priors = [sum(v for m, v, _ in sessions[d] if m <= cutoff) for d in dates[:-1]]
    priors = [p for p in priors if p > 0]
    if not priors:
        return None
    day_high = max(h for _, _, h in today)
    return today_cum / median(priors), cutoff, day_high


def scan_intraday(ticker, prof=None, timeframe="5m"):
    """Deteksi lonjakan volume sesi berjalan. Metrik tergantung profil (MODE).

    Sinyal butuh: RVOL >= RVOL_MIN, high hari ini > high kemarin, dan
    harga >= GAP_MIN x close kemarin. Kode sama untuk live pagi maupun replay.
    """
    prof = prof or PROFILES["gocap"]
    daily = idx_data.get_candles(ticker, "1d", count=3)
    if len(daily) < 2:
        return None
    prev = daily[-2]

    intraday = idx_data.get_candles(ticker, timeframe, count=400)
    today = idx_data.last_session(intraday)
    if not today:
        return None
    last = today[-1]["c"]
    day_high = max(c["h"] for c in today)

    if prof["MODE"] == "tod":
        tod = _tod_rvol(_sessions_by_minute(intraday))
        if tod is None:          # kurang sesi historis -> fallback ke dayvol
            rvol = sum(c["v"] for c in today) / prev["v"] if prev["v"] else 0.0
            metric = "dayvol*"
        else:
            rvol, _, day_high = tod
            metric = "tod"
    else:
        rvol = sum(c["v"] for c in today) / prev["v"] if prev["v"] else 0.0
        metric = "dayvol"

    broke_high = day_high > prev["h"]
    gap_up = last >= prof["GAP_MIN"] * prev["c"]
    return {
        "ticker": idx_data.display_symbol(ticker),
        "last": last, "prev_close": prev["c"],
        "chg_pct": (last - prev["c"]) / prev["c"] * 100 if prev["c"] else 0.0,
        "rvol": rvol, "metric": metric, "broke_high": broke_high,
        "signal": rvol >= prof["RVOL_MIN"] and broke_high and gap_up,
    }


# --- CLI --------------------------------------------------------------------

def run_eod(tickers, prof, pname):
    print(f"Scan EOD (coil/akumulasi) [{pname}] - {len(tickers)} saham\n")
    rows = []
    for t in tickers:
        try:
            r = scan_eod(t, idx_data.get_candles(t, "1d", count=400), prof)
            if r:
                rows.append(r)
        except Exception as e:
            print(f"  ! {idx_data.display_symbol(t):<6} skip: {e}")
    rows.sort(key=lambda r: (r["candidate"], r["score"]), reverse=True)
    print(f"\n{'STOCK':<7}{'PRICE':>9}{'SUP':>8}{'SCORE':>7}  FLAGS")
    print("-" * 52)
    for r in rows:
        star = "*" if r["candidate"] else " "
        sup = f"{r['support']:,.0f}" if r["support"] else "-"
        ext = " (extended)" if r["extended"] else ""
        print(f"{star}{r['ticker']:<6}{r['price']:>9,.0f}{sup:>8}{r['score']:>7}  "
              f"{', '.join(r['reasons']) or '-'}{ext}")
    cand = [r["ticker"] for r in rows if r["candidate"]]
    print(f"\n* Kandidat besok ({len(cand)}): {', '.join(cand) or 'tidak ada'}")


def run_intraday(tickers, prof, pname):
    print(f"Scan intraday (early volume surge) [{pname}] - {len(tickers)} saham\n")
    rows = []
    for t in tickers:
        try:
            r = scan_intraday(t, prof)
            if r:
                rows.append(r)
        except Exception as e:
            print(f"  ! {idx_data.display_symbol(t):<6} skip: {e}")
    rows.sort(key=lambda r: r["rvol"], reverse=True)
    print(f"\n{'STOCK':<7}{'LAST':>8}{'CHG%':>8}{'RVOL':>7}{'':>7}  BREAKOUT")
    print("-" * 50)
    for r in rows:
        sig = ">>" if r["signal"] else "  "
        hi = "HIGH" if r["broke_high"] else "-"
        print(f"{sig}{r['ticker']:<5}{r['last']:>8,.0f}{r['chg_pct']:>+7.1f}%"
              f"{r['rvol']:>6.1f}x {r['metric']:>6}  {hi}")
    hot = [r["ticker"] for r in rows if r["signal"]]
    print(f"\n>> Sedang meledak ({len(hot)}): {', '.join(hot) or 'tidak ada'}")


def _traded_today(ref="BBCA"):
    """True jika bursa jelas trading hari ini (hindari alert data basi akhir pekan/libur)."""
    try:
        s = idx_data.last_session(idx_data.get_candles(ref, "5m", count=100))
        if not s:
            return False
        d = datetime.fromtimestamp(s[-1]["t"] / 1000, idx_data.WIB).date()
        return d == datetime.now(idx_data.WIB).date()
    except Exception:
        return True   # jangan gagal senyap; biarkan tetap jalan


def run_alert(tickers, prof, pname, dry_run=False):
    """Scan intraday, kirim ke Telegram HANYA jika ada sinyal. Untuk dijadwalkan pagi."""
    if not _traded_today():
        print("Bursa tidak trading hari ini - alert dilewati.")
        return
    hits = []
    for t in tickers:
        try:
            r = scan_intraday(t, prof)
            if r and r["signal"]:
                hits.append(r)
        except Exception as e:
            print(f"  ! {idx_data.display_symbol(t):<6} skip: {e}")
    if not hits:
        print("Tidak ada sinyal - tidak ada yang dikirim.")
        return
    hits.sort(key=lambda r: r["rvol"], reverse=True)
    now = datetime.now(idx_data.WIB).strftime("%H:%M")
    lines = [f"[ALERT] Breakout [{pname}] {now} WIB"]
    for r in hits:
        hi = " tembus-high" if r["broke_high"] else ""
        lines.append(f"{r['ticker']}  {r['last']:,.0f}  {r['chg_pct']:+.1f}%  "
                     f"RVOL {r['rvol']:.1f}x{hi}")
    msg = "\n".join(lines)
    if dry_run:
        print("[DRY-RUN] pesan yang akan dikirim:\n" + msg)
        return
    for cid, ok, desc in notify_telegram.broadcast(msg):
        print(f"{cid}: {'OK' if ok else 'GAGAL - ' + desc}")


def main(argv):
    pname = "gocap"
    universe = "Selected"
    intraday = alert = dry_run = False
    tickers = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--intraday", "-i"):
            intraday = True
        elif a == "--alert":
            alert = True
        elif a == "--dry-run":
            dry_run = True
        elif a in ("--profile", "-p") and i + 1 < len(argv):
            pname = argv[i + 1].lower()
            i += 1
        elif a in ("--universe", "-u") and i + 1 < len(argv):
            universe = argv[i + 1]
            i += 1
        elif not a.startswith("-"):
            tickers.append(a)
        i += 1
    prof = PROFILES.get(pname)
    if prof is None:
        print(f"profil tidak dikenal: {pname} (pilih: {', '.join(PROFILES)})")
        return 2

    import universes
    cfg = screener.load_config()
    tickers = tickers or universes.get_universe(universe, cfg["tickers"])
    if alert:
        run_alert(tickers, prof, pname, dry_run)
    elif intraday:
        run_intraday(tickers, prof, pname)
    else:
        run_eod(tickers, prof, pname)
    return 0


def _demo():
    """Self-check: coil EOD, RVOL day-vol (gocap), dan RVOL time-of-day (liquid)."""
    def bar(t, o, h, l, c, v):
        return {"t": t * 86400000, "o": o, "h": h, "l": l, "c": c, "v": v}

    # Coil: turun (range lebar, close lemah) -> basis rapat volume kering, close di high.
    seq = [bar(i, 80 - i, 83 - i, 77 - i, 78 - i, 15e6) for i in range(25)]
    seq += [bar(i, 57, 60, 56, 59, 12e6) for i in range(25, 37)]
    seq += [bar(37, 58, 59, 57, 59, 4e6), bar(38, 58, 59, 57, 59, 3e6),
            bar(39, 58, 59, 57, 58, 5e6)]
    eod = scan_eod("LAPD", seq)
    assert eod["candidate"] and eod["factors"]["dry_up"], eod

    # RVOL day-vol (gocap): prev 5M, hari ini 41M tembus high.
    prev, cum = 5e6, 41e6
    assert cum / prev >= PROFILES["gocap"]["RVOL_MIN"]

    # RVOL time-of-day (liquid): 3 sesi, dua slot (09:00, 09:30). Hari ini ~2x
    # pace normal slot yg sama -> harus melewati ambang liquid.
    ms = 86400000
    def ib(day, minute, v, h):
        return {"t": day * ms + minute * 60000, "o": h, "h": h, "l": h, "c": h, "v": v}
    sess = []
    for d in (0, 1):                      # 2 sesi historis, pace normal
        sess += [ib(d, 540, 10e6, 100), ib(d, 570, 10e6, 100)]
    sess += [ib(2, 540, 18e6, 105), ib(2, 570, 18e6, 106)]   # hari ini ~1.8x
    rvol, cutoff, dh = _tod_rvol(_sessions_by_minute(sess))
    assert rvol >= PROFILES["liquid"]["RVOL_MIN"], f"tod rvol {rvol}"
    print(f"demo OK - eod score {eod['score']} | gocap rvol {cum/prev:.0f}x "
          f"| liquid tod rvol {rvol:.1f}x")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        sys.exit(main(sys.argv[1:]))
