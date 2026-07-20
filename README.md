# TradingBot

A Python application for a crypto trading bot and an Indonesian stock screener.
This project has two main workflows:

- Crypto.com crypto trading bot through the CLI (`bot.py`)
- IDX stock screener through the Streamlit web UI (`app.py`)

> Disclaimer: this is an analysis tool, not financial advice. Trading and
> investing are risky. Run paper mode first before using a live account.

## Main Features

- Multi-timeframe technical analysis
- IDX stock screener using Yahoo Finance (`yfinance`)
- Quality Value screener for medium-to-long-term stock ideas
- Streamlit web UI
- Telegram notifications when a token is configured
- Local signal journal and scan history
- Paper trading mode for the crypto bot

## Requirements

- Python 3.10+
- `pip`
- Packages from `requirements.txt`

Main dependencies:

```text
requests
python-dotenv
truststore
yfinance
streamlit
openpyxl
```

## Install on Windows

Run from the project folder:

```powershell
cd C:\xampp\htdocs\TradingBot
python -m venv .venv
.\.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

Start the web UI:

```powershell
python -m streamlit run app.py
```

## Install on an Ubuntu VM

Install Python:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

Open the application folder:

```bash
cd /home/agus/TradingBot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Start the UI so it can be opened from another computer on the local network:

```bash
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Open it in a browser:

```text
http://VM_IP:8501
```

Example:

```text
http://192.168.1.28:8501
```

## Ubuntu Firewall

For local access, open only the Streamlit port:

```bash
sudo ufw allow 8501/tcp
sudo ufw status
```

For a cleaner local setup, allow only the local network:

```bash
sudo ufw delete allow 8501/tcp
sudo ufw allow from 192.168.1.0/24 to any port 8501 proto tcp
```

Do not expose the app directly to the public internet without extra protection
such as HTTPS, authentication, a tunnel, or a reverse proxy.

## Public Access

The app is currently best used on the local network through the VM IP. To access
it from outside your home or office network, use one of these options:

- Router port forwarding
- VPS/cloud server
- Cloudflare Tunnel
- Tailscale
- ngrok

Recommended for private access: Tailscale or Cloudflare Tunnel.

## Environment File

Copy the example configuration:

```bash
cp .env.example .env
```

On Windows:

```powershell
copy .env.example .env
```

Fill `.env` if you use API keys or Telegram. The `.env` file is already listed
in `.gitignore`, so do not upload it to GitHub.

## Crypto Bot Commands

```bash
python bot.py analyze
python bot.py analyze BTC_USDT
python bot.py run
python bot.py status
python bot.py balance
```

For live trading:

1. Create a Crypto.com Exchange API key.
2. Enable Read + Trade only, never Withdraw.
3. Fill `.env`.
4. Change `config.json` to live mode.
5. Start with a small position size.

## IDX Screener Commands

Web UI:

```bash
python -m streamlit run app.py
```

Technical CLI:

```bash
python screener.py
python screener.py --all
python screener.py --min BUY
python screener.py --universe LQ45
python screener.py BBCA
```

Quality Value CLI:

```bash
python qvscreener.py
python qvscreener.py --sort quality
python qvscreener.py --universe KOMPAS100
python qvscreener.py BBRI
```

## Important Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI |
| `bot.py` | Crypto bot CLI |
| `screener.py` | IDX technical screener |
| `qvscreener.py` | Quality Value screener |
| `strategy.py` | Technical analysis engine |
| `risk.py` | Risk management |
| `trader.py` | Paper/live execution |
| `exchange.py` | Crypto.com API client |
| `notify_telegram.py` | Telegram notifications |
| `config.json` | Crypto bot configuration |
| `config_screener.json` | Screener configuration |
| `requirements.txt` | Python dependency list |

## GitHub Notes

Before uploading to GitHub, check:

```bash
git status
```

Make sure secret files such as `.env` are not included in a commit.
