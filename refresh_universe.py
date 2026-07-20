"""
Bangun/refresh daftar induk saham IDX lewat Yahoo screener (filter harga +
kapitalisasi di sisi server). Simpan ke idx_universe.json supaya screener &
ara_scanner membaca daftar terfilter tanpa fetch ratusan ticker satu-satu.

Cache menyimpan harga & mcap per saham, jadi UI bisa MEMPERKETAT ambang tanpa
fetch ulang (filter lokal). Refresh berkala saja (mis. mingguan) untuk ikut
IPO/delisting & perubahan mcap.

    python refresh_universe.py
    python refresh_universe.py --min-price 50 --min-mcap 500e9
"""

import json
import os
import sys
import time

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from yfinance import EquityQuery, screen

CACHE = os.path.join(os.path.dirname(__file__), "idx_universe.json")
DEFAULT_MIN_PRICE = 50
DEFAULT_MIN_MCAP = 500_000_000_000   # Rp500 Miliar (mcap kecil pun sudah puluhan miliar)
PAGE = 250                            # batas per halaman screener Yahoo


def fetch(min_price, min_mcap):
    """Semua ekuitas IDX yang lolos ambang, via paginasi screener Yahoo."""
    q = EquityQuery("and", [
        EquityQuery("eq", ["region", "id"]),
        EquityQuery("gt", ["intradayprice", min_price]),
        EquityQuery("gt", ["intradaymarketcap", min_mcap]),
    ])
    out, offset = {}, 0
    while True:
        r = screen(q, offset=offset, size=PAGE,
                   sortField="intradaymarketcap", sortAsc=False)
        quotes = r.get("quotes", [])
        if not quotes:
            break
        for x in quotes:
            sym = x.get("symbol", "")
            if not sym.endswith(".JK"):
                continue
            out[sym[:-3]] = {
                "price": x.get("regularMarketPrice"),
                "mcap": x.get("marketCap"),
                "name": x.get("shortName") or x.get("longName"),
            }
        offset += PAGE
        if offset >= (r.get("total") or 0):
            break
        time.sleep(0.3)
    return out


def refresh(min_price=DEFAULT_MIN_PRICE, min_mcap=DEFAULT_MIN_MCAP, path=CACHE):
    data = fetch(min_price, min_mcap)
    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "min_price": min_price, "min_mcap": min_mcap,
        "count": len(data), "stocks": data,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    # Tulis juga idx_all.txt supaya universe "ALL" ikut daftar penuh ini
    # (universes.get_universe("ALL") otomatis membaca file ini bila ada).
    all_path = os.path.join(os.path.dirname(path), "idx_all.txt")
    with open(all_path, "w", encoding="utf-8") as f:
        f.write("# dibuat otomatis oleh refresh_universe.py\n")
        f.write("\n".join(sorted(data)) + "\n")
    return payload


def load(path=CACHE):
    """Isi cache, atau None jika belum pernah di-refresh."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _args(argv):
    mp, mm = DEFAULT_MIN_PRICE, DEFAULT_MIN_MCAP
    i = 0
    while i < len(argv):
        if argv[i] == "--min-price" and i + 1 < len(argv):
            mp = float(argv[i + 1]); i += 1
        elif argv[i] == "--min-mcap" and i + 1 < len(argv):
            mm = float(argv[i + 1]); i += 1
        i += 1
    return mp, mm


if __name__ == "__main__":
    mp, mm = _args(sys.argv[1:])
    p = refresh(mp, mm)
    print(f"{p['count']} saham (harga>={mp:.0f}, mcap>={mm:.0e}) "
          f"-> {os.path.basename(CACHE)} @ {p['generated']}")
