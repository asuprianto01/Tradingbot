"""
Trade execution + portfolio tracking.

Two modes:
  * paper (default): simulated fills against live prices; state persisted to
    paper_state.json so the bot can be stopped/restarted. No keys needed.
  * live: real MARKET orders on Crypto.com. Requires API keys AND the
    config flag "i_understand_live_trading_risks": true. Stops/targets are
    monitored by the bot loop and closed at market when touched.

Every fill (paper or live) is appended to trades.csv as a journal.
"""

import csv
import json
import os
import time
import uuid
import logging
from datetime import datetime, timezone
import math

log = logging.getLogger("trader")

STATE_FILE = "paper_state.json"
JOURNAL_FILE = "trades.csv"
JOURNAL_FIELDS = ["timestamp", "mode", "instrument", "side", "action", "price",
                  "quantity", "notional", "stop_loss", "take_profit", "pnl", "reason"]

FEE_RATE = 0.00075  # taker fee assumption used for paper fills


def calculate_correlation(prices1, prices2, min_periods=20):
    """Calculate Pearson correlation coefficient between two price series.
    
    Returns correlation between -1 and 1, or None if insufficient data.
    """
    if len(prices1) < min_periods or len(prices2) < min_periods:
        return None
    
    # Use the last min_periods data points
    p1 = prices1[-min_periods:]
    p2 = prices2[-min_periods:]
    
    # Calculate returns
    returns1 = [(p1[i] - p1[i-1]) / p1[i-1] for i in range(1, len(p1))]
    returns2 = [(p2[i] - p2[i-1]) / p2[i-1] for i in range(1, len(p2))]
    
    if len(returns1) != len(returns2):
        return None
    
    n = len(returns1)
    if n == 0:
        return None
    
    # Calculate means
    mean1 = sum(returns1) / n
    mean2 = sum(returns2) / n
    
    # Calculate covariance and standard deviations
    covariance = sum((r1 - mean1) * (r2 - mean2) for r1, r2 in zip(returns1, returns2))
    std1 = math.sqrt(sum((r1 - mean1) ** 2 for r1 in returns1))
    std2 = math.sqrt(sum((r2 - mean2) ** 2 for r2 in returns2))
    
    if std1 == 0 or std2 == 0:
        return None
    
    correlation = covariance / (n * std1 * std2)
    return max(-1.0, min(1.0, correlation))  # Clamp to [-1, 1]


class Position:
    def __init__(self, instrument, side, entry, quantity, stop_loss, take_profit, opened_at=None, price_history=None):
        self.instrument = instrument
        self.side = side
        self.entry = entry
        self.quantity = quantity
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.opened_at = opened_at or time.time()
        self.price_history = price_history or []  # Store price history for correlation checks

    def unrealized_pnl(self, price):
        direction = 1 if self.side == "BUY" else -1
        return (price - self.entry) * self.quantity * direction

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d):
        return cls(d["instrument"], d["side"], d["entry"], d["quantity"],
                   d["stop_loss"], d["take_profit"], d.get("opened_at"))


class Trader:
    def __init__(self, config, exchange):
        self.config = config
        self.exchange = exchange
        self.mode = config.get("mode", "paper")
        self.quote_currency = config.get("quote_currency", "USDT")
        if self.mode == "live" and not config.get("i_understand_live_trading_risks"):
            raise SystemExit(
                'Refusing to start in live mode: set "i_understand_live_trading_risks": true '
                "in config_crypto.json after you have tested in paper mode."
            )
        self.state = self._load_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                raw = json.load(f)
            raw["positions"] = {k: Position.from_dict(v) for k, v in raw.get("positions", {}).items()}
            return raw
        return {
            "cash": self.config.get("paper_starting_balance", 10000.0),
            "positions": {},
            "realized_pnl": 0.0,
            "daily_pnl": 0.0,
            "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

    def _save_state(self):
        raw = dict(self.state)
        raw["positions"] = {k: p.to_dict() for k, p in self.state["positions"].items()}
        # Atomic write: write to temp file first, then rename
        temp_file = STATE_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(raw, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # Atomic rename (cross-platform)
        if os.path.exists(STATE_FILE):
            os.replace(temp_file, STATE_FILE)
        else:
            os.rename(temp_file, STATE_FILE)

    def _roll_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state["daily_date"] != today:
            self.state["daily_date"] = today
            self.state["daily_pnl"] = 0.0

    def _journal(self, **row):
        exists = os.path.exists(JOURNAL_FILE)
        with open(JOURNAL_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})

    # ------------------------------------------------------------------
    # Portfolio queries
    # ------------------------------------------------------------------

    def equity(self, price_lookup):
        """price_lookup: dict instrument -> last price."""
        eq = self.state["cash"]
        for inst, pos in self.state["positions"].items():
            price = price_lookup.get(inst, pos.entry)
            eq += pos.quantity * price if pos.side == "BUY" else pos.unrealized_pnl(price)
        return eq

    def daily_pnl(self):
        self._roll_daily()
        return self.state["daily_pnl"]

    def open_positions(self):
        return self.state["positions"]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def open_position(self, plan, reason="", new_instrument_prices=None):
        """plan: risk.TradePlan.
        new_instrument_prices: optional price history for correlation check."""
        # Input validation
        if not plan:
            raise ValueError("plan cannot be None")
        if plan.instrument in self.state["positions"]:
            return None  # already in - never pyramid blindly
        if plan.notional <= 0:
            raise ValueError(f"plan.notional must be positive, got {plan.notional}")
        if plan.quantity <= 0:
            raise ValueError(f"plan.quantity must be positive, got {plan.quantity}")
        if plan.entry <= 0:
            raise ValueError(f"plan.entry must be positive, got {plan.entry}")
        if plan.stop_loss <= 0:
            raise ValueError(f"plan.stop_loss must be positive, got {plan.stop_loss}")
        if plan.take_profit <= 0:
            raise ValueError(f"plan.take_profit must be positive, got {plan.take_profit}")
        
        # Correlation filter
        max_corr = self.config.get("risk", {}).get("max_correlation", 0.7)
        if max_corr > 0 and new_instrument_prices and self.state["positions"]:
            for inst, pos in self.state["positions"].items():
                if pos.price_history and len(pos.price_history) >= 20:
                    corr = calculate_correlation(new_instrument_prices, pos.price_history)
                    if corr is not None and abs(corr) > max_corr:
                        log.warning(f"Skipping {plan.instrument}: correlation with {inst} is {corr:.2f} > {max_corr}")
                        return None
        
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if self.mode == "live":
            if plan.side == "BUY":
                self.exchange.create_order(
                    plan.instrument, "BUY", "MARKET",
                    notional=round(plan.notional, 2),
                    client_oid=str(uuid.uuid4()),
                )
            else:
                self.exchange.create_order(
                    plan.instrument, "SELL", "MARKET",
                    quantity=plan.quantity,
                    client_oid=str(uuid.uuid4()),
                )
        else:
            fee = plan.notional * FEE_RATE
            self.state["cash"] -= plan.notional + fee

        pos = Position(plan.instrument, plan.side, plan.entry, plan.quantity,
                       plan.stop_loss, plan.take_profit, price_history=new_instrument_prices)
        self.state["positions"][plan.instrument] = pos
        self._save_state()
        self._journal(timestamp=ts, mode=self.mode, instrument=plan.instrument,
                      side=plan.side, action="OPEN", price=f"{plan.entry:.8g}",
                      quantity=f"{plan.quantity:.8g}", notional=f"{plan.notional:.2f}",
                      stop_loss=f"{plan.stop_loss:.8g}", take_profit=f"{plan.take_profit:.8g}",
                      reason=reason)
        return pos

    def update_price_history(self, instrument, price):
        """Update price history for an open position (for correlation checks)."""
        pos = self.state["positions"].get(instrument)
        if pos and pos.price_history is not None:
            pos.price_history.append(price)
            # Keep only last 100 points to save memory
            if len(pos.price_history) > 100:
                pos.price_history = pos.price_history[-100:]
            self._save_state()

    def close_position(self, instrument, price, reason=""):
        pos = self.state["positions"].pop(instrument, None)
        if not pos:
            return None
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pnl = pos.unrealized_pnl(price)

        if self.mode == "live":
            exit_side = "SELL" if pos.side == "BUY" else "BUY"
            if exit_side == "SELL":
                self.exchange.create_order(instrument, "SELL", "MARKET",
                                           quantity=pos.quantity, client_oid=str(uuid.uuid4()))
            else:
                self.exchange.create_order(instrument, "BUY", "MARKET",
                                           notional=round(pos.quantity * price, 2),
                                           client_oid=str(uuid.uuid4()))
        else:
            proceeds = pos.quantity * price
            fee = proceeds * FEE_RATE
            self.state["cash"] += proceeds - fee
            pnl -= fee

        self._roll_daily()
        self.state["realized_pnl"] += pnl
        self.state["daily_pnl"] += pnl
        self._save_state()
        self._journal(timestamp=ts, mode=self.mode, instrument=instrument,
                      side=pos.side, action="CLOSE", price=f"{price:.8g}",
                      quantity=f"{pos.quantity:.8g}", notional=f"{pos.quantity * price:.2f}",
                      pnl=f"{pnl:.2f}", reason=reason)
        return pnl

    def manage_position(self, instrument, candle):
        """
        Check stop/target against the latest candle; close if touched.
        Returns (closed: bool, pnl or None, reason).
        """
        pos = self.state["positions"].get(instrument)
        if not pos:
            return False, None, ""
        high, low, close = candle["h"], candle["l"], candle["c"]
        if pos.side == "BUY":
            if low <= pos.stop_loss:
                return True, self.close_position(instrument, pos.stop_loss, "stop loss hit"), "stop loss hit"
            if high >= pos.take_profit:
                return True, self.close_position(instrument, pos.take_profit, "take profit hit"), "take profit hit"
        else:
            if high >= pos.stop_loss:
                return True, self.close_position(instrument, pos.stop_loss, "stop loss hit"), "stop loss hit"
            if low <= pos.take_profit:
                return True, self.close_position(instrument, pos.take_profit, "take profit hit"), "take profit hit"
        return False, None, ""
