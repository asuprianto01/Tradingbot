# Screener Saham Indonesia (IDX)

Dua screener saham BEI dalam satu aplikasi, dibangun di atas mesin analisis bot
crypto yang sudah ada:

- **📈 Teknikal (trading)** — sinyal confluence multi-timeframe + rencana Entry / Stop Loss / Take Profit.
- **💎 Quality Value (investasi)** — skor fundamental (mutu bisnis × kemurahan harga) untuk saham jangka menengah/panjang.

> **Disclaimer:** ini alat bantu screening, **bukan nasihat keuangan**. Data
> Yahoo Finance bisa telat/tidak lengkap. Selalu verifikasi ke laporan resmi &
> harga live broker sebelum mengambil keputusan.

---

## Instalasi

```powershell
pip install -r requirements.txt
```

Tanpa API key — semua data publik lewat Yahoo Finance (`yfinance`).

---

## Menjalankan

### UI Web (disarankan)
Dobel-klik **`run_screener_ui.bat`**, atau:
```powershell
python -m streamlit run app.py
```
Terbuka di browser. Read-only, tidak pernah menaruh order.

**Auto-scan (tab Teknikal):** berjalan otomatis **hanya saat pasar buka**
(Sen–Jum, jam bursa WIB). Di luar jam / hari libur / akhir pekan, auto-scan
**berhenti** (hemat request) — pakai tombol **🔍 Scan sekarang** untuk scan manual
gaya yang sedang ditampilkan. Setiap gaya punya **interval sendiri**, mengikuti
durasi bar timeframe entry-nya — scan lebih sering dari itu sia-sia (candle
belum ganti, data Yahoo delay ~15 mnt):

| Gaya | TF entry | Interval auto-scan |
|---|---|---|
| Intraday | 1H | 15 menit |
| Swing Trading | 1D | 60 menit |
| Long Term | 1W | 1× per hari |

Matikan **🔁 Rotasi semua gaya** untuk auto-scan hanya gaya yang sedang
ditampilkan. QV tetap manual.

**Auto-kirim Telegram:** bila diaktifkan (panel kiri → 📤 Telegram → *Auto-kirim
saat ada sinyal baru*, default ON), setiap muncul sinyal BELI baru yang actionable
langsung dikirim ke penerima Telegram (perlu token & penerima terisi).

## Pelacakan sinyal & Winrate

Setiap sinyal BELI yang muncul menjadi **posisi terpantau** (bukan sekadar
notifikasi sekali). Sistem melacaknya sampai selesai:

- **Buka posisi** saat sinyal actionable muncul (disimpan permanen di
  `open_signals.json`) — otomatis anti-duplikat (satu posisi per gaya+saham).
- **Exit: trailing stop** (default `exit.trail_r_mult = 1.5`, lihat Konfigurasi).
  Stop dinaikkan mengikuti **high tertinggi sejak entry** (jarak 1.5× risiko awal
  R), tak pernah turun. TP1/TP2/TP3 tetap dicatat sebagai **milestone** (event +
  notifikasi Telegram) tapi **tidak menutup posisi** — trailing stop yang
  membiarkan pemenang lari melewati target lama, dan memotong rugi cepat kalau
  momentum gagal.
- **Posisi ditutup** saat **low menembus trailing stop**: event **TRAIL** (profit
  terkunci) bila di atas entry, atau **SL** (rugi) bila di bawah/di stop awal.
  Cooldown re-entry hanya berlaku setelah **SL**, bukan TRAIL.
- **Semua progres dicatat ke `signal_events.csv`** (OPEN / TP1 / TP2 / TP3 / TRAIL / SL).

> Ini pengganti mekanisme lama (TP1/TP2/TP3 tetap + breakeven-only). Backtest
> sistematis (529 saham, train vs test) menunjukkan trailing menaikkan
> ekspektasi ~3× dibanding TP tetap 2R, karena tidak memenggal pemenang.
> Nonaktifkan lewat `"exit": {"trailing": false}` untuk kembali ke TP tetap.

**Hasil scan tersimpan** ke `last_scan.json` — saat app dibuka ulang, tabel scan
terakhir langsung tampil (tanpa harus scan dulu), lalu di-refresh otomatis saat
pasar buka. Posisi terpantau & jurnal ada di `open_signals.json` /
`signal_events.csv`.

**Winrate** dihitung dari jurnal itu, tampil di tab Teknikal (**📊 Winrate &
Jurnal Trade**) — total, per **minggu**, dan per **bulan**. Jurnal bisa **diunduh**
sebagai **Excel (.xlsx)** rapi (4 sheet: Jurnal Event + Winrate Mingguan +
Winrate Bulanan + Posisi Terbuka; header berwarna, freeze, autofilter) untuk
analisa sendiri. Konvensi: sebuah trade **WIN** bila pernah menyentuh **TP1+**
atau ditutup via **TRAIL** (trailing stop di atas entry = profit terkunci),
**LOSS** bila ditutup via **SL** tanpa pernah TP; dihitung saat trade
**tertutup**. Posisi yang masih terbuka ditampilkan terpisah.

> Model: entry diasumsikan di harga pasar saat sinyal; sentuhan level dicek pakai
> **high/low bar terakhir** (menangkap wick TP/SL, bukan hanya close). Karena data
> Yahoo delayed ~15-20 mnt & bar diperbarui tiap scan, sentuhan yang terjadi &
> pulih di antara dua scan pada timeframe kecil masih mungkin terlewat — untuk
> evaluasi strategi mingguan/bulanan sudah representatif.

### CLI
```powershell
# Teknikal
python screener.py                     # scan watchlist (Selected)
python screener.py --all               # tampilkan semua, bukan hanya BUY+
python screener.py --min BUY           # ambang sinyal minimum
python screener.py --universe LQ45     # pakai index tertentu
python screener.py BBCA                # laporan detail 1 saham

# Quality Value
python qvscreener.py                   # skor watchlist
python qvscreener.py --sort quality    # urut: quality | value | qv
python qvscreener.py --universe KOMPAS100
python qvscreener.py BBRI              # rincian metrik 1 saham
```

---

## Universe (daftar saham)

Dipilih di panel kiri UI atau lewat `--universe` di CLI:

| Universe | Isi |
|---|---|
| **Selected** | Daftar custom-mu (bisa disimpan sebagai default) |
| **IDX30 / LQ45 / IDX80 / KOMPAS100** | Konstituen index (~30/45/80/100) |
| **ALL** | Broad market (~145), atau taruh `idx_all.txt` untuk daftar penuh |

- Daftar konstituen ada di [universes.py](universes.py) — **snapshot best-effort**, BEI reshuffle tiap kuartal, verifikasi/edit bila bergeser.
- Daftar **Selected** bisa diedit di UI lalu **💾 Simpan** (jadi default permanen di `config.json`) atau **🗑️ Kosongkan**.
- Scan index/ALL berjalan **paralel (8 thread)**; universe >120 saham diberi peringatan (lambat & rentan rate-limit Yahoo).

### Aturan tampilan
- **Selected** → tampilkan semua sesuai filter.
- **Selain Selected:**
  - Teknikal → hanya **STRONG_BUY → BUY**, terurut sinyal terkuat.
  - Quality Value → hanya verdict **"Quality Value"**, urut QV tertinggi (top 40).

---

## Tab Teknikal

Memakai `strategy.analyze()` (mesin yang sama dengan bot crypto). Pilih lewat
**preset Gaya Trading** (mencegah salah set timeframe):

| Gaya Trading | TF entry / tren | Untuk |
|---|---|---|
| **Swing Trading** ⭐ | 1D / 1W | Swing (hari–minggu) — disarankan |
| **Intraday** | 1H / 1D | Timing masuk intraday |
| **Long Term** | 1W / 1M | Investor / posisi jangka panjang |

**Pantau semua gaya (default ON).** Tiap siklus scan, ketiga gaya dinilai sekaligus
(timeframe di-fetch sekali & dipakai ulang), jadi **sinyal di gaya mana pun langsung
ketahuan**. Ringkasan *"Sinyal per gaya → Swing: 2 · Intraday: 0 · Long Term: 1"*
tampil di atas tabel; tabel menampilkan gaya yang kamu pilih. Panel detail
menampilkan perbandingan sinyal **lintas gaya** untuk saham itu.

> ⚠️ **Rate-limit:** "semua gaya" × universe besar (mis. ALL ~145) = banyak request
> per siklus → rentan throttle Yahoo. Untuk pemakaian berkelanjutan, pakai universe
> lebih kecil atau matikan "Pantau semua gaya". Interval 30 menit sudah membantu.

### Rule sinyal (tervalidasi backtest, 529 saham, train ≈ test)

Sinyal BUY/STRONG_BUY **tidak** lagi dari skor confluence — hasil pencarian
sistematis atas ratusan kombinasi menunjukkan rule sederhana ini paling
konsisten out-of-sample:

- **STRONG_BUY** = tren UP (weekly EMA) **+** Akumulasi smart-money aktif (OBV
  naik + CMF positif) **+** MACD bullish
- **BUY** = tren UP + Akumulasi (MACD belum konfirmasi)
- Selain itu → **NEUTRAL**

Dua gerbang tambahan sebelum sinyal jadi **rencana beli actionable** (lihat
Konfigurasi):
- **Likuiditas** — turnover (nilai transaksi) harus ≥ `min_turnover` (default
  Rp3 M/hari). Sinyal di saham lebih tipis tetap tampil tapi tidak dijadikan
  rencana beli — hasil backtest saham illikuid tidak realistis (slippage, ARB).
- **Regime IHSG** — bila `require_market_risk_on` aktif (default), rencana beli
  hanya dibuat saat IHSG risk-on (harga di atas EMA200, EMA50 naik). Saat
  IHSG risk-off (mis. crash), screener menahan diri — inilah yang
  menyelamatkan strategi ini di periode bearish saat backtest.

**Faktor confluence** (HTF trend, RSI, MACD, Bollinger, Stochastic, Volume,
Akumulasi, Divergensi, Struktur) masih dihitung & ditampilkan di panel detail
sebagai **rincian/konteks** ("kenapa sinyal ini muncul"), tapi tidak lagi
menentukan sinyal.

**Kolom penting:**
- **Sinyal** — STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
- **Akumulasi** — 🟢 Akumulasi / ⚪ Netral / 🔴 Distribusi (arah aliran dana via OBV & CMF)
- **Divergensi** — 🟢 Bullish / 🔴 Bearish (harga vs OBV, konteks tambahan)
- **Transaksi (M/hr)** — turnover harian; di bawah ambang = sinyal tampil tapi tak actionable
- **Zona Beli** — rentang akumulasi (batas bawah ≈ support/pullback, batas atas = harga pasar). Beli di dekat batas bawah = R:R lebih baik.
- **Stop Loss** — stop **awal**; setelah entry, exit sesungguhnya mengikuti **trailing stop** (lihat Pelacakan sinyal & Winrate)
- **TP ladder** — ditampilkan sebagai referensi target (TP1 = min. 2R / resistance terdekat, TP2/TP3 = resistance berikutnya) + **Potensi%**, tapi posisi **tidak** ditutup otomatis di TP — lihat trailing stop
- **Ukuran Posisi** — jumlah lot dari modal & risiko per trade

---

## Tab Quality Value

Memakai [fundamentals.py](fundamentals.py) + [qvscreener.py](qvscreener.py). Dua pilar skor 0–100:

- **Value (kemurahan):** P/E, P/B, Dividend Yield, PEG
- **Quality (mutu bisnis):** ROE, ROA, Profit Margin, **Net Profit Growth 3Y**, **Revenue Growth 3Y**, Debt/Equity

**Kolom tambahan:**
- **F-Score** — Piotroski (0–9), sembilan uji profitabilitas/leverage/efisiensi. Kriteria yang datanya tak tersedia (umum untuk bank) ditandai n/a.
- **Harga Wajar & MoS%** — Graham Number `√(22.5 × EPS × BVPS)`; MoS = margin of safety vs harga (+ = di bawah wajar).
- **Verdict** — 💎 Quality Value (bagus & murah) / Bagus tapi mahal / ⚠️ Murah tapi berisiko (value trap) / Hindari / Biasa saja.

Ambang skor bisa diubah di `VALUE_METRICS` / `QUALITY_METRICS` pada [qvscreener.py](qvscreener.py).

---

## Panel detail (klik baris)

Di kedua tab, **klik satu baris** tabel → panel detail muncul di bawahnya:
- **Nama perusahaan + sektor/industri + profil usaha** (dari Yahoo)
- **Alasan sinyal** — rincian faktor (Teknikal) atau rincian metrik + F-Score + MoS (QV)
- **Verdict / rencana beli** — jadi jelas *kenapa* saham itu bagus/tidak untuk dibeli

---

## Indikator kesegaran data

Bar di atas tab menampilkan:
- **Status pasar IDX** — BUKA / JEDA / TUTUP / akhir pekan (dari jam WIB)
- **Jam kuotasi terakhir + delay** (ref: BBCA)
- **Peringatan** saat pasar buka: candle timeframe berjalan **belum final**

> Yahoo untuk IDX **delay ~15–20 menit** dan **bukan real-time**. Cocok untuk
> screening harian/swing; untuk eksekusi presisi cek harga live di broker.
> Hari libur bursa tidak terdeteksi — patokan sebenarnya adalah jam kuotasi.

---

## Kirim sinyal ke Telegram

Screener bisa mengirim sinyal BELI yang actionable (Zona Beli / SL / TP ladder /
akumulasi / MTF) ke satu atau banyak penerima Telegram.

**Setup:**
1. Buat bot lewat **@BotFather** di Telegram → salin **token**.
2. Taruh token di **`.env`** → `TELEGRAM_BOT_TOKEN=...` (atau `config.json` → `telegram.token`).
3. Dapatkan **chat_id** penerima: penerima kirim `/start` ke bot-mu, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` dan lihat `chat.id`.
   (Bisa juga id grup/channel; bot harus jadi anggota.)
4. Tambah penerima di UI: panel kiri → **📤 Telegram** → isi chat_id → **➕ Tambah**.
   Penerima bisa **ditambah/dihapus kapan saja** (disimpan di `config.json`).

**Mengirim:** setelah scan teknikal, klik **📤 Kirim ke Telegram** (ada pratinjau
pesan sebelum kirim). CLI uji: `python notify_telegram.py "pesan uji"`.

> Token adalah rahasia — simpan di `.env` (sudah masuk `.gitignore`), jangan di
> repo publik.

## Konfigurasi ([config_screener.json](config_screener.json))

```json
{
  "tickers": ["BBCA", "BBRI", ...],       // daftar Selected
  "entry_timeframe": "1d",
  "trend_timeframe": "1wk",
  "min_signal": "BUY",
  "capital": 100000000,                   // modal untuk sizing (Rp)
  "lot_size": 100,                        // 1 lot = 100 lembar
  "min_turnover": 3000000000,             // Rp/hari — gerbang likuiditas
  "require_market_risk_on": true,         // gerbang regime IHSG
  "exit": {
    "trailing": true,                     // false = kembali ke TP tetap + breakeven
    "trail_r_mult": 1.5                   // jarak trailing stop, dalam kelipatan R
  },
  "risk": {},                             // stop_atr_multiple, min_reward_risk, dst
  "strategy": {}                          // override threshold strategy.analyze bila perlu
}
```
Sizing tunduk pada blok `risk` yang dipakai bersama bot crypto
(`risk_per_trade_pct`, `max_position_pct`, `stop_atr_multiple`, `min_reward_risk`).

Alat riset: [backtest_optimizer.py](backtest_optimizer.py) (walk-forward,
train/test split, sweep parameter) dan [dataset.py](dataset.py) (simpan hasil
`strategy.analyze` per-bar seluruh universe ke `dataset/` — sekali build, dipakai
ulang untuk riset rule apa pun tanpa re-analyze).

---

## File

| File | Fungsi |
|---|---|
| [app.py](app.py) | UI web Streamlit (2 tab + detail + bar kesegaran) |
| [screener.py](screener.py) | Screener teknikal + rencana trade + CLI |
| [qvscreener.py](qvscreener.py) | Screener Quality-Value + CLI |
| [idx_data.py](idx_data.py) | Data harga IDX (candle) + status pasar/kesegaran |
| [fundamentals.py](fundamentals.py) | Data fundamental + Piotroski + Graham + profil |
| [universes.py](universes.py) | Daftar index & broad market |
| [notify_telegram.py](notify_telegram.py) | Kirim sinyal ke Telegram (broadcast ke banyak penerima) |
| [tracker.py](tracker.py) | Pelacak posisi (trailing stop), jurnal `signal_events.csv`, winrate |
| [indicators.py](indicators.py) | Indikator (EMA, RSI, MACD, ATR, **OBV**, **CMF**, **StochRSI**, dll) — dipakai bersama |
| [strategy.py](strategy.py) | Mesin sinyal (rule tervalidasi backtest) + faktor confluence untuk rincian — dipakai bersama |
| [risk.py](risk.py) | Sizing posisi, stop/target — dipakai bersama |
| [backtest_optimizer.py](backtest_optimizer.py) | Walk-forward backtest, train/test split, sweep parameter |
| [dataset.py](dataset.py) | Simpan/muat hasil `analyze` per-bar seluruh universe (riset cepat) |
| `run_screener_ui.bat` | Peluncur UI klik-ganda |

---

## Catatan penting

- **Restart app setelah mengubah kode.** Streamlit me-reload `app.py` tapi tak
  selalu re-import modul lain — kalau muncul `AttributeError`/`KeyError` setelah
  update, **tutup terminal → jalankan ulang `run_screener_ui.bat`**. (Tombol
  🧹 Reset hasil hanya membersihkan hasil scan, bukan modul.)
- **Long-only.** Rekomendasi trade hanya untuk sinyal BELI (short ritel IDX sulit).
- **Bukan nasihat keuangan.** Selalu cek ulang sebelum transaksi.
