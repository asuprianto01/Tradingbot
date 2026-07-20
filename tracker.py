"""
Pelacak sinyal / jurnal trade.

Alur:
  * Saat sinyal BELI actionable muncul -> BUKA posisi (disimpan permanen di
    open_signals.json) + catat event OPEN ke signal_events.csv.
  * Tiap scan, harga terkini dicek vs SL / TP1 / TP2 / TP3. Tiap level yang
    tersentuh dicatat sebagai event (untuk dikirim ke Telegram).
  * Posisi DITUTUP (dihapus dari open_signals.json) saat kena SL atau TP3.
  * Dari signal_events.csv kita hitung winrate per minggu / bulan.

Semua state ada di file (bukan session), jadi lintas restart tetap konsisten.
Model sederhana: entry diasumsikan di harga pasar saat sinyal (reco['entry']),
cek sentuhan level pakai harga terakhir (close). WIN = pernah kena TP1+.
"""

import csv
import json
import os
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
OPEN_FILE = "open_signals.json"
EVENTS_CSV = "signal_events.csv"
SL_COOLDOWN_FILE = "sl_cooldowns.json"

# Minimum hours to wait before re-entering the same ticker+style after SL.
# Prevents the re-entry loop when a signal persists while price keeps falling.
SL_COOLDOWN_HOURS = {
    "Intraday": 3,          # 3 jam — cukup biar candle 1H berubah arah
    "Swing Trading": 48,    # 2 hari — biar price action konfirmasi balik
    "Long Term": 168,       # 7 hari — untuk pergerakan mingguan
}

EVENT_HEADER = ["timestamp", "date", "style", "ticker", "signal", "event",
                "price", "entry", "stop", "tp1", "tp2", "tp3", "gain_pct", "trade_id"]


def _now_wib():
    return datetime.now(timezone.utc).astimezone(WIB)


def load_open():
    try:
        with open(OPEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def load_cooldowns():
    """Return dict {position_key: iso_timestamp_of_last_sl}."""
    try:
        with open(SL_COOLDOWN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_cooldowns(cd):
    try:
        with open(SL_COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(cd, f, indent=2)
    except OSError:
        pass


def save_open(positions):
    try:
        with open(OPEN_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2)
    except OSError:
        pass


def position_key(style, ticker):
    return f"{style}::{ticker}"


def _append_event(row):
    exists = os.path.exists(EVENTS_CSV)
    try:
        with open(EVENTS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=EVENT_HEADER)
            if not exists:
                w.writeheader()
            w.writerow(row)
    except OSError:
        pass


def _event_row(pos, event, price, gain, now):
    return {
        "timestamp": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "style": pos["style"], "ticker": pos["ticker"], "signal": pos["signal"],
        "event": event, "price": round(price), "entry": round(pos["entry_ref"]),
        "stop": round(pos["stop"]),
        "tp1": pos.get("tp1"), "tp2": pos.get("tp2"), "tp3": pos.get("tp3"),
        "gain_pct": round(gain, 2), "trade_id": pos["trade_id"],
    }


def open_position(positions, style, ticker, signal, reco,
                  current_low=None, cooldowns=None):
    """
    Buka posisi baru bila belum ada. Return True bila baru dibuka.

    current_low : low bar terakhir untuk gaya ini — kalau sudah di bawah stop,
                  tolak (cegah instant-SL: posisi dibuka tapi bar sudah tembus stop).
    cooldowns   : dict {key: iso_ts_last_sl} dari load_cooldowns() — kalau kunci
                  masih dalam cooldown setelah SL, tolak re-entry.
    """
    key = position_key(style, ticker)
    if key in positions:
        return False

    # Blokir re-entry kalau masih dalam periode cooldown setelah SL
    if cooldowns and key in cooldowns:
        hours = SL_COOLDOWN_HOURS.get(style, 24)
        try:
            sl_at = datetime.fromisoformat(cooldowns[key])
            elapsed = (_now_wib() - sl_at).total_seconds() / 3600
            if elapsed < hours:
                return False
        except (ValueError, TypeError):
            pass

    # Blokir kalau bar aktif sudah menembus stop (instant-SL prevention)
    if current_low is not None and current_low <= reco["stop"]:
        return False

    now = _now_wib()
    tps = [round(t["price"]) for t in reco.get("targets", [])]
    pos = {
        "style": style, "ticker": ticker, "signal": signal,
        "entry_ref": reco["entry"], "entry_low": reco["entry_low"],
        "entry_high": reco["entry_high"], "stop": reco["stop"],
        "risk0": max(1e-9, reco["entry"] - reco["stop"]),   # R awal untuk trailing
        "highest": reco["entry"],                            # high tertinggi sejak entry
        "tp1": tps[0] if len(tps) > 0 else None,
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
        "opened_at": now.isoformat(timespec="seconds"),
        "trade_id": f"{key}::{now.strftime('%Y%m%d%H%M%S')}",
        "hits": [], "status": "open",
    }
    positions[key] = pos
    _append_event(_event_row(pos, "OPEN", reco["entry"], 0.0, now))
    return True


def update_positions(positions, bars, style=None, cooldowns=None, trail_mult=None):
    """
    bars      : {ticker: (high, low)} dari bar terakhir gaya yang BARU discan.
    cooldowns : dict {key: iso_ts} dari load_cooldowns(), dimodifikasi in-place
                saat SL terpicu — simpan dengan save_cooldowns() setelah selesai.
    trail_mult: bila diset (mis. 1.5), pakai EXIT TRAILING STOP — stop dinaikkan
                mengikuti high tertinggi (jarak trail_mult × R awal), tutup saat
                low menembusnya. TP hanya dicatat sbg milestone (tak menutup).
                Bila None → perilaku lama (TP tetap TP1/2/3 + breakeven).

    Cek sentuhan level pakai high (TP) & low (SL) — menangkap wick, bukan close.
    Hanya perbarui posisi gaya == `style`. Return daftar event.
    """
    events = []
    now = _now_wib()
    for key in list(positions.keys()):
        pos = positions[key]
        if style is not None and pos["style"] != style:
            continue
        hl = bars.get(pos["ticker"])
        if not hl:
            continue
        high, low = hl
        entry = pos["entry_ref"]

        if trail_mult:
            # === EXIT TRAILING STOP (strategi pemenang) ===
            risk0 = pos.get("risk0") or max(1e-9, entry - pos["stop"])
            hi = pos["highest"] = max(pos.get("highest", entry), high)
            new_stop = round(hi - trail_mult * risk0)
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop
            # Sentuhan TP = milestone (dicatat, tak menutup — trailing yang menutup).
            for lvl, name in ((pos.get("tp1"), "TP1"), (pos.get("tp2"), "TP2"),
                              (pos.get("tp3"), "TP3")):
                if lvl is not None and name not in pos["hits"] and high >= lvl:
                    pos["hits"].append(name)
                    g = (lvl - entry) / entry * 100 if entry else 0.0
                    _append_event(_event_row(pos, name, lvl, g, now))
                    events.append(_notify(pos, name, lvl, g, closed=False, level=lvl))
            # Tutup saat low menembus trailing stop.
            if low <= pos["stop"]:
                g = (pos["stop"] - entry) / entry * 100 if entry else 0.0
                ev_name = "TRAIL" if g > 0 else "SL"      # profit terkunci vs rugi
                _append_event(_event_row(pos, ev_name, pos["stop"], g, now))
                events.append(_notify(pos, ev_name, pos["stop"], g, closed=True, level=pos["stop"]))
                if cooldowns is not None and g <= 0:      # cooldown hanya bila rugi
                    cooldowns[key] = now.isoformat(timespec="seconds")
                del positions[key]
            continue

        # Stop loss diutamakan (risiko dulu): low menembus stop.
        if low <= pos["stop"]:
            gain = (pos["stop"] - entry) / entry * 100 if entry else 0.0
            _append_event(_event_row(pos, "SL", pos["stop"], gain, now))
            events.append(_notify(pos, "SL", pos["stop"], gain, closed=True, level=pos["stop"]))
            # Catat waktu SL untuk cooldown re-entry
            if cooldowns is not None:
                cooldowns[key] = now.isoformat(timespec="seconds")
            del positions[key]
            continue

        # Take profit bertingkat: high mencapai level. Posisi TUTUP saat
        # menyentuh TP TERTINGGI yang tersedia (bisa TP1/TP2/TP3, tergantung
        # berapa target yang terbentuk dari struktur).
        avail = [(lvl, name) for lvl, name in
                 ((pos.get("tp1"), "TP1"), (pos.get("tp2"), "TP2"), (pos.get("tp3"), "TP3"))
                 if lvl is not None]
        last_name = avail[-1][1] if avail else None
        for lvl, name in avail:
            if name in pos["hits"]:
                continue
            if high >= lvl:
                pos["hits"].append(name)
                gain = (lvl - entry) / entry * 100 if entry else 0.0
                closed = (name == last_name)
                # Trailing stop: amankan profit setelah TP (kecuali TP penutup).
                new_stop = None
                if not closed:
                    if name == "TP1":
                        new_stop = round(entry)        # breakeven
                    elif name == "TP2":
                        new_stop = pos.get("tp1")      # naik ke TP1
                    if new_stop is not None and new_stop > pos["stop"]:
                        pos["stop"] = new_stop
                    else:
                        new_stop = None
                _append_event(_event_row(pos, name, lvl, gain, now))
                ev = _notify(pos, name, lvl, gain, closed=closed, level=lvl)
                ev["new_stop"] = new_stop
                events.append(ev)
                if closed:
                    del positions[key]
                    break
    return events


def _notify(pos, event, price, gain, closed, level):
    return {"style": pos["style"], "ticker": pos["ticker"], "event": event,
            "price": round(price), "gain_pct": round(gain, 2), "closed": closed,
            "entry": round(pos["entry_ref"]), "stop": round(pos["stop"]),
            "level": round(level)}


def winrate(period="W"):
    """
    Hitung winrate dari signal_events.csv, dikelompokkan per minggu ('W') atau
    bulan ('M'). Trade dihitung WIN begitu kena TP1+ (hasil sudah terkunci Win:
    SL naik ke breakeven, jadi tak bisa lagi loss), atau LOSS bila kena SL tanpa
    pernah sentuh TP. Trade yang belum menyentuh TP1 maupun SL belum dihitung.
    Return list dict siap ditabelkan.
    """
    if not os.path.exists(EVENTS_CSV):
        return []
    trades = {}
    try:
        with open(EVENTS_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                tid = row["trade_id"]
                tr = trades.setdefault(tid, {"max_tp": 0, "avail": 0, "sl": False,
                                             "sl_date": None, "tp_date": None})
                # Berapa TP tersedia (dari kolom tp1/tp2/tp3).
                av = 0
                for i, col in enumerate(("tp1", "tp2", "tp3"), 1):
                    if str(row.get(col) or "").strip() not in ("", "None"):
                        av = i
                tr["avail"] = max(tr["avail"], av)
                ev = row["event"]
                if ev in ("TP1", "TP2", "TP3"):
                    tr["max_tp"] = max(tr["max_tp"], int(ev[2]))
                    tr["tp_date"] = row["date"]
                elif ev == "TRAIL":            # trailing-exit profit = WIN terkunci
                    tr["trail_win"] = True
                    tr["tp_date"] = row["date"]
                elif ev == "SL":
                    tr["sl"] = True
                    tr["sl_date"] = row["date"]
    except OSError:
        return []

    buckets = {}
    for tr in trades.values():
        # WIN begitu kena TP1+ (SL sudah naik ke breakeven -> hasil terkunci Win,
        # entah nanti tutup di TP tertinggi atau balik ke breakeven). Kalau belum
        # kena TP tapi sudah SL -> LOSS. Selain itu masih murni terbuka.
        if tr["max_tp"] >= 1 or tr.get("trail_win"):
            close_date, win = tr["tp_date"], True
        elif tr["sl"]:
            close_date, win = tr["sl_date"], False
        else:
            continue                         # belum kena TP1 & belum SL -> belum dihitung
        try:
            d = datetime.strptime(close_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if period == "M":
            label = d.strftime("%Y-%m")
        else:
            iso = d.isocalendar()
            label = f"{iso[0]}-W{iso[1]:02d}"
        b = buckets.setdefault(label, {"total": 0, "win": 0, "loss": 0})
        b["total"] += 1
        if win:
            b["win"] += 1
        else:
            b["loss"] += 1

    out = []
    for label in sorted(buckets):
        b = buckets[label]
        wr = b["win"] / b["total"] * 100 if b["total"] else 0
        out.append({"Periode": label, "Trade": b["total"], "Win": b["win"],
                    "Loss": b["loss"], "Winrate %": round(wr, 1)})
    return out


def get_entry_zone_reminders(positions, prices):
    """
    Cek posisi dari hari SEBELUMNYA yang harganya masih di zona entry.
    Kembalikan list (key, pos, current_price) yang belum diingatkan hari ini.

    positions : dict open positions (dari load_open)
    prices    : {ticker: current_price} dari analisis terkini
    """
    today = _now_wib().strftime("%Y-%m-%d")
    reminders = []
    for key, pos in positions.items():
        opened_date = pos.get("opened_at", "")[:10]
        if opened_date >= today:          # posisi dibuka hari ini — bukan reminder
            continue
        if pos.get("last_reminder_date") == today:   # sudah diingatkan hari ini
            continue
        if pos.get("hits"):               # sudah kena TP — bukan lagi di zona entry awal
            continue
        price = prices.get(pos["ticker"])
        if price is None:
            continue
        entry_low = pos.get("entry_low", 0)
        entry_high = pos.get("entry_high", pos.get("entry_ref", 0))
        if entry_low <= price <= entry_high:
            reminders.append((key, pos, price))
    return reminders


def mark_reminded(positions, keys):
    """Tandai posisi sudah diingatkan hari ini (update last_reminder_date in-place)."""
    today = _now_wib().strftime("%Y-%m-%d")
    for key in keys:
        if key in positions:
            positions[key]["last_reminder_date"] = today


def reset_journal():
    """Hapus jurnal, posisi terbuka, & cooldown (mulai dari nol)."""
    for path in (OPEN_FILE, EVENTS_CSV, SL_COOLDOWN_FILE):
        try:
            os.remove(path)
        except OSError:
            pass
