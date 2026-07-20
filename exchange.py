"""
Crypto.com Exchange API client (REST v1).

Public endpoints (candles, tickers) need no API key.
Private endpoints (balance, orders) require CRYPTO_API_KEY / CRYPTO_API_SECRET
and use Crypto.com's HMAC-SHA256 request signing.

API docs: https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html
"""

import hashlib
import hmac
import time

try:
    # Use the Windows/OS certificate store for TLS (fixes CERTIFICATE_VERIFY_FAILED
    # on machines where Python's bundled CA list can't verify the connection).
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

BASE_URL = "https://api.crypto.com/exchange/v1/"


class ExchangeError(Exception):
    def __init__(self, code, message, method=""):
        self.code = code
        super().__init__(f"{method} failed with code {code}: {message}")


class CryptoComExchange:
    def __init__(self, api_key=None, api_secret=None, timeout=15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.session = requests.Session()
        self._req_id = 0

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    def _public_get(self, method, params=None):
        resp = self.session.get(BASE_URL + method, params=params or {}, timeout=self.timeout)
        data = resp.json()
        if data.get("code", 0) != 0:
            raise ExchangeError(data.get("code"), data.get("message", resp.text), method)
        return data["result"]

    @staticmethod
    def _params_to_str(obj, level=0):
        """Flatten params exactly the way Crypto.com's signature spec requires."""
        if level >= 3:
            return str(obj)
        out = ""
        for key in sorted(obj):
            value = obj[key]
            out += key
            if value is None:
                out += "null"
            elif isinstance(value, bool):
                out += str(value).lower()
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        out += CryptoComExchange._params_to_str(item, level + 1)
                    else:
                        out += str(item)
            elif isinstance(value, dict):
                out += CryptoComExchange._params_to_str(value, level + 1)
            else:
                out += str(value)
        return out

    def _private_post(self, method, params=None):
        if not self.api_key or not self.api_secret:
            raise ExchangeError(-1, "API key/secret not configured (set CRYPTO_API_KEY and CRYPTO_API_SECRET in .env)", method)
        params = params or {}
        req_id = self._next_id()
        nonce = int(time.time() * 1000)
        payload = method + str(req_id) + self.api_key + self._params_to_str(params) + str(nonce)
        sig = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        body = {
            "id": req_id,
            "method": method,
            "api_key": self.api_key,
            "params": params,
            "nonce": nonce,
            "sig": sig,
        }
        resp = self.session.post(BASE_URL + method, json=body, timeout=self.timeout)
        data = resp.json()
        if data.get("code", 0) != 0:
            raise ExchangeError(data.get("code"), data.get("message", resp.text), method)
        return data.get("result", {})

    # ------------------------------------------------------------------
    # Public market data
    # ------------------------------------------------------------------

    def get_candles(self, instrument, timeframe="1h", count=300):
        """Return list of candles (oldest first): dicts with t, o, h, l, c, v."""
        result = self._public_get(
            "public/get-candlestick",
            {"instrument_name": instrument, "timeframe": timeframe, "count": count},
        )
        candles = []
        for row in result.get("data", []):
            candles.append({
                "t": int(row["t"]),
                "o": float(row["o"]),
                "h": float(row["h"]),
                "l": float(row["l"]),
                "c": float(row["c"]),
                "v": float(row["v"]),
            })
        candles.sort(key=lambda x: x["t"])
        return candles

    def get_ticker(self, instrument):
        result = self._public_get("public/get-tickers", {"instrument_name": instrument})
        data = result.get("data", [])
        if not data:
            raise ExchangeError(-1, f"no ticker data for {instrument}", "public/get-tickers")
        t = data[0]
        return {
            "last": float(t["a"]) if t.get("a") else None,
            "bid": float(t["b"]) if t.get("b") else None,
            "ask": float(t["k"]) if t.get("k") else None,
            "high_24h": float(t["h"]) if t.get("h") else None,
            "low_24h": float(t["l"]) if t.get("l") else None,
            "volume_24h": float(t["v"]) if t.get("v") else None,
            "change_24h_pct": float(t["c"]) * 100 if t.get("c") else None,
        }

    # ------------------------------------------------------------------
    # Private account / trading
    # ------------------------------------------------------------------

    def get_balance(self):
        result = self._private_post("private/user-balance")
        accounts = result.get("data", [])
        return accounts[0] if accounts else {}

    def create_order(self, instrument, side, order_type, quantity=None, price=None, notional=None, client_oid=None):
        # Input validation
        if not instrument or not isinstance(instrument, str):
            raise ValueError("instrument must be a non-empty string")
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")
        if order_type not in ("MARKET", "LIMIT"):
            raise ValueError("order_type must be 'MARKET' or 'LIMIT'")
        if order_type == "LIMIT" and price is None:
            raise ValueError("price is required for LIMIT orders")
        if quantity is not None and quantity <= 0:
            raise ValueError("quantity must be positive")
        if notional is not None and notional <= 0:
            raise ValueError("notional must be positive")
        if price is not None and price <= 0:
            raise ValueError("price must be positive")
        
        params = {
            "instrument_name": instrument,
            "side": side,            # BUY / SELL
            "type": order_type,      # MARKET / LIMIT
        }
        if quantity is not None:
            params["quantity"] = str(quantity)
        if notional is not None:
            params["notional"] = str(notional)
        if price is not None:
            params["price"] = str(price)
        if client_oid:
            params["client_oid"] = client_oid
        return self._private_post("private/create-order", params)

    def cancel_order(self, order_id):
        return self._private_post("private/cancel-order", {"order_id": str(order_id)})

    def get_open_orders(self, instrument=None):
        params = {"instrument_name": instrument} if instrument else {}
        return self._private_post("private/get-open-orders", params).get("data", [])
