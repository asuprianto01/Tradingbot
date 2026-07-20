"""
Stock universes (watchlists) for the screeners.

"Selected" uses your own list from config.json / the UI text box. The others
are index constituents and a broad-market set.

IMPORTANT: index membership changes every quarter (BEI reshuffles LQ45/IDX30
etc.). These lists are a best-effort snapshot — verify and edit them here when
they drift. For a true "every listed stock" scan, drop a plain-text file named
`idx_all.txt` next to this file (one ticker per line, without .JK); ALL will use
it automatically.
"""

import json
import os

# --- Large caps (IDX30 core) --------------------------------------------------
IDX30 = [
    "BBCA", "BBRI", "BMRI", "BBNI", "TLKM", "ASII", "UNVR", "ICBP", "INDF",
    "KLBF", "UNTR", "ANTM", "ADRO", "PGAS", "PTBA", "GGRM", "MDKA", "AMRT",
    "CPIN", "SMGR", "INTP", "TPIA", "BRPT", "ACES", "MEDC", "ITMG", "INCO",
    "ARTO", "GOTO", "BUKA",
]

# --- LQ45 = IDX30 + 15 more liquid names -------------------------------------
_LQ45_EXTRA = [
    "HMSP", "CTRA", "BSDE", "PWON", "SMRA", "ERAA", "EXCL", "TOWR", "MNCN",
    "MAPI", "JPFA", "AKRA", "INKP", "TKIM", "HRUM",
]
LQ45 = sorted(set(IDX30 + _LQ45_EXTRA))

# --- IDX80 = LQ45 + more ------------------------------------------------------
_IDX80_EXTRA = [
    "BBTN", "BRIS", "BJBR", "BJTM", "PNBN", "BFIN", "ADMF", "WIKA", "PTPP",
    "ADHI", "JSMR", "ISAT", "SIDO", "MYOR", "ULTJ", "CMRY", "AALI", "LSIP",
    "DSNG", "SSMS", "ELSA", "PGEO", "NCKL", "TINS", "AVIA", "MAPA", "BIRD",
    "SCMA", "TBIG", "EMTK", "KIJA", "LPKR", "WTON", "PTRO", "BYAN",
]
IDX80 = sorted(set(LQ45 + _IDX80_EXTRA))

# --- KOMPAS100 = IDX80 + more -------------------------------------------------
_KOMPAS_EXTRA = [
    "CUAN", "AMMN", "MBMA", "HEAL", "MIKA", "SILO", "KAEF", "BTPS", "TUGU",
    "RAJA", "BRMS", "ENRG", "DOID", "BSSR", "TOBA", "DMAS", "ASRI", "DILD",
    "MTLA", "PRDA",
]
KOMPAS100 = sorted(set(IDX80 + _KOMPAS_EXTRA))

# --- Broad market fallback for ALL (when idx_all.txt is absent) ---------------
_BROAD_EXTRA = [
    "BNGA", "NISP", "BDMN", "PNLF", "BBKP", "AGRO", "SRTG", "BUMI", "DEWA",
    "ELTY", "PSAB", "SMDR", "TMAS", "HITS", "BULL", "SHIP", "APLN", "BEST",
    "DGNS", "PANI", "MLPL", "MLPT", "LINK", "ASGR", "MTDL", "DCII", "EDGE",
    "FILM", "MSIN", "WIFI", "IPCC", "IPCM", "PORT", "TCPI", "KEEN", "BREN",
    "CBDK", "MTEL", "AMAR", "BANK", "BBHI", "BBSI", "BGTG", "DNAR", "KKGI",
]
_BROAD = sorted(set(KOMPAS100 + _BROAD_EXTRA))


def filtered(min_price=None, min_mcap=None, path=None):
    """Ticker dari idx_universe.json yang lolos ambang harga & kapitalisasi.

    Ambang default = ambang dasar cache; nilai yang lebih ketat difilter lokal
    (cache menyimpan price/mcap per saham). Kosong bila cache belum ada -
    jalankan `python refresh_universe.py` dulu.
    """
    path = path or os.path.join(os.path.dirname(__file__), "idx_universe.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    stocks = d.get("stocks", {})
    mp = d.get("min_price", 0) if min_price is None else min_price
    mm = d.get("min_mcap", 0) if min_mcap is None else min_mcap
    return sorted(t for t, v in stocks.items()
                  if (v.get("price") or 0) >= mp and (v.get("mcap") or 0) >= mm)


def available():
    """Universe names, in display order."""
    return ["Selected", "FILTER", "IDX30", "LQ45", "IDX80", "KOMPAS100", "ALL"]


def get_universe(name, selected=None):
    """Return the ticker list for a universe. `selected` is the user's own list
    (used when name == 'Selected')."""
    lookup = {
        "IDX30": IDX30,
        "LQ45": LQ45,
        "IDX80": IDX80,
        "KOMPAS100": KOMPAS100,
    }
    if name in lookup:
        return list(lookup[name])
    if name == "FILTER":
        return filtered()
    if name == "ALL":
        path = os.path.join(os.path.dirname(__file__), "idx_all.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                tickers = [ln.strip().upper() for ln in f if ln.strip()
                           and not ln.startswith("#")]
            if tickers:
                return tickers
        return list(_BROAD)
    # "Selected" or anything unknown -> the user's own list.
    return list(selected or [])
