"""
Kirim sinyal screener ke Telegram.

Butuh:
  * Bot token — buat lewat @BotFather di Telegram, taruh di env var
    TELEGRAM_BOT_TOKEN (disarankan) atau di config.json -> telegram.token.
  * Chat ID penerima — daftar di config.json -> telegram.recipients.
    Penerima bisa ditambah kapan saja (lewat UI atau edit config.json).

Cara mendapat chat_id: penerima chat dulu ke bot-mu (kirim /start), lalu buka
    https://api.telegram.org/bot<TOKEN>/getUpdates
dan lihat "chat":{"id": ...}. Bisa juga id grup/channel (bot harus jadi anggota).
"""

import json
import os

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def load_config(path="config_screener.json"):
    """Return (token, recipients). Token: env var wins over config.json.
    Recipients: env var TELEGRAM_CHAT_IDS wins over config.json."""
    tg = {}
    try:
        with open(path, encoding="utf-8") as f:
            tg = json.load(f).get("telegram", {}) or {}
    except FileNotFoundError:
        pass
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("token", "") or ""
    
    # Priority: env var > config.json
    env_recipients = os.environ.get("TELEGRAM_CHAT_IDS", "")
    if env_recipients:
        recipients = [str(r).strip() for r in env_recipients.split(",") if str(r).strip()]
    else:
        recipients = [str(r).strip() for r in tg.get("recipients", []) if str(r).strip()]
    
    return token.strip(), recipients


def save_recipients(recipients, path="config_screener.json"):
    """Persist the recipient list to config_screener.json (telegram.recipients)."""
    try:
        with open(path, encoding="utf-8") as f:
            full = json.load(f)
    except FileNotFoundError:
        full = {}
    # de-dup, keep order
    seen, clean = set(), []
    for r in recipients:
        r = str(r).strip()
        if r and r not in seen:
            seen.add(r)
            clean.append(r)
    full.setdefault("telegram", {})["recipients"] = clean
    with open(path, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2)
    return clean


def send_message(text, token, chat_id, timeout=15):
    """Send one message. Returns (ok, description)."""
    if not token:
        return False, "TELEGRAM_BOT_TOKEN belum diset"
    try:
        resp = requests.post(
            API_URL.format(token=token),
            data={"chat_id": str(chat_id), "text": text,
                  "disable_web_page_preview": True},
            timeout=timeout,
        )
        data = resp.json()
        return bool(data.get("ok")), data.get("description", "")
    except Exception as e:  # network / JSON errors
        return False, str(e)


def broadcast(text, token=None, recipients=None):
    """
    Send `text` to every recipient. Loads token/recipients from config if not
    given. Returns list of (chat_id, ok, description).
    """
    if token is None or recipients is None:
        cfg_token, cfg_recipients = load_config()
        token = token or cfg_token
        recipients = recipients if recipients is not None else cfg_recipients
    return [(cid, *send_message(text, token, cid)) for cid in recipients]


if __name__ == "__main__":
    # Uji cepat: python notify_telegram.py "pesan uji"
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "Uji koneksi Telegram dari screener IDX ✅"
    tok, recs = load_config()
    if not tok:
        print("Token belum diset (TELEGRAM_BOT_TOKEN atau config.json telegram.token).")
    elif not recs:
        print("Belum ada penerima (config.json telegram.recipients).")
    else:
        for cid, ok, desc in broadcast(msg, tok, recs):
            print(f"{cid}: {'OK' if ok else 'GAGAL - ' + desc}")
