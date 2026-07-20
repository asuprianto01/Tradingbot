"""
Risk management - the part most amateur bots skip and most pros live by.

Rules enforced here:
  * Fixed fractional position sizing: risk at most `risk_per_trade_pct` of
    equity on any single trade, with the stop distance defining size.
  * ATR-based stop loss and a minimum reward:risk ratio for the target.
  * Hard caps: max open positions, max daily loss (circuit breaker),
    max position notional as % of equity.
"""

from dataclasses import dataclass


@dataclass
class TradePlan:
    instrument: str
    side: str               # BUY / SELL
    entry: float
    stop_loss: float
    take_profit: float
    quantity: float
    notional: float
    risk_amount: float      # currency at risk if stop is hit
    reward_risk: float


class RiskManager:
    def __init__(self, config):
        r = config.get("risk", {})
        self.risk_per_trade_pct = r.get("risk_per_trade_pct", 1.0)
        self.max_position_pct = r.get("max_position_pct", 20.0)
        self.max_open_positions = r.get("max_open_positions", 3)
        self.max_daily_loss_pct = r.get("max_daily_loss_pct", 4.0)
        self.stop_atr_multiple = r.get("stop_atr_multiple", 1.5)
        self.min_reward_risk = r.get("min_reward_risk", 2.0)
        # Stop terlalu jauh dari entry = saham spekulatif / penny stock dengan ATR gila.
        # Default 10%: filter saham seperti BRMS (43%), DEWA (42%), BYAN (16%).
        self.max_stop_pct = r.get("max_stop_pct", 10.0)

    def daily_circuit_breaker(self, equity, daily_pnl):
        """True = trading halted for the day."""
        return daily_pnl < 0 and abs(daily_pnl) >= equity * self.max_daily_loss_pct / 100

    def plan_trade(self, instrument, side, price, atr, equity,
                   nearest_support=None, nearest_resistance=None):
        """
        Build a complete trade plan, or return None if it can't meet the
        reward:risk requirement. Long-only sizing also works for sells
        (used to exit / short on margin accounts).
        """
        stop_dist = atr * self.stop_atr_multiple
        if side == "BUY":
            stop = price - stop_dist
            # Tuck the stop under structure if support is close (pro habit:
            # stops belong behind levels, not at arbitrary distances).
            if nearest_support and price > nearest_support > stop:
                stop = nearest_support - atr * 0.25
            # Tolak kalau stop terlalu jauh — saham spekulatif / ATR terlalu besar.
            stop_pct = (price - stop) / price * 100 if price > 0 else 999
            if stop_pct > self.max_stop_pct:
                return None
            
            if nearest_resistance and nearest_resistance > price:
                target = nearest_resistance
            else:
                target = price + (price - stop) * self.min_reward_risk
        else:
            stop = price + stop_dist
            if nearest_resistance and price < nearest_resistance < stop:
                stop = nearest_resistance + atr * 0.25
            
            if nearest_support and nearest_support < price:
                target = nearest_support
            else:
                target = price - (stop - price) * self.min_reward_risk

        risk_per_unit = abs(price - stop)
        reward_per_unit = abs(target - price)
        if risk_per_unit <= 0 or price <= 0:
            return None
        rr = reward_per_unit / risk_per_unit
        if rr < self.min_reward_risk * 0.75:  # allow slight slack when structure capped the target
            return None

        risk_amount = equity * self.risk_per_trade_pct / 100
        quantity = risk_amount / risk_per_unit
        notional = quantity * price
        max_notional = equity * self.max_position_pct / 100
        if notional > max_notional:
            notional = max_notional
            quantity = notional / price
            risk_amount = quantity * risk_per_unit

        return TradePlan(
            instrument=instrument, side=side, entry=price,
            stop_loss=round(stop, 8), take_profit=round(target, 8),
            quantity=quantity, notional=notional,
            risk_amount=risk_amount, reward_risk=rr,
        )
