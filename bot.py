"""
Crypto.com trading bot - main entry point.

Commands:
  python bot.py analyze [INSTRUMENT]   one-shot professional analysis (no keys needed)
  python bot.py run                    start the trading loop (paper mode by default)
  python bot.py status                 show portfolio, open positions, P&L
  python bot.py balance                show real Crypto.com account balance (needs API keys)
"""

import json
import logging
import os
import sys
import time

from dotenv import load_dotenv

import strategy
from exchange import CryptoComExchange, ExchangeError
from risk import RiskManager
from trader import Trader

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_crypto.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("bot")


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def build_exchange():
    load_dotenv()
    return CryptoComExchange(os.getenv("CRYPTO_API_KEY"), os.getenv("CRYPTO_API_SECRET"))


def run_analysis(exchange, config, instrument):
    entry_tf = config.get("entry_timeframe", "1h")
    trend_tf = config.get("trend_timeframe", "4h")
    entry_candles = exchange.get_candles(instrument, entry_tf, 300)
    trend_candles = exchange.get_candles(instrument, trend_tf, 300)
    if len(entry_candles) < 60 or len(trend_candles) < 60:
        raise ExchangeError(-1, f"not enough candle history for {instrument}", "analysis")
    return strategy.analyze(instrument, entry_candles, trend_candles, config), entry_candles


def cmd_analyze(args):
    config = load_config()
    exchange = build_exchange()
    instruments = args or config.get("instruments", ["BTC_USDT"])
    for instrument in instruments:
        analysis, _ = run_analysis(exchange, config, instrument)
        print()
        print(analysis.report())
    print()


def cmd_status():
    config = load_config()
    exchange = build_exchange()
    trader = Trader(config, exchange)
    prices = {}
    for inst in trader.open_positions():
        try:
            prices[inst] = exchange.get_ticker(inst)["last"]
        except ExchangeError:
            pass
    print(f"\nMode: {trader.mode}")
    print(f"Cash: {trader.state['cash']:,.2f} {trader.quote_currency}")
    print(f"Equity: {trader.equity(prices):,.2f} {trader.quote_currency}")
    print(f"Realized P&L: {trader.state['realized_pnl']:+,.2f}   Today: {trader.daily_pnl():+,.2f}")
    positions = trader.open_positions()
    if positions:
        print("\nOpen positions:")
        for inst, pos in positions.items():
            price = prices.get(inst, pos.entry)
            print(f"  {inst} {pos.side} qty {pos.quantity:.8g} @ {pos.entry:,.6g}  "
                  f"now {price:,.6g}  uPnL {pos.unrealized_pnl(price):+,.2f}  "
                  f"SL {pos.stop_loss:,.6g}  TP {pos.take_profit:,.6g}")
    else:
        print("\nNo open positions.")
    print()


def cmd_balance():
    exchange = build_exchange()
    account = exchange.get_balance()
    print(f"\nTotal available balance: {account.get('total_available_balance')} "
          f"{account.get('instrument_name', 'USD')}")
    for pb in account.get("position_balances", []):
        print(f"  {pb.get('instrument_name')}: {pb.get('quantity')} "
              f"(market value {pb.get('market_value')})")
    print()


def cmd_run():
    config = load_config()
    exchange = build_exchange()
    trader = Trader(config, exchange)
    risk = RiskManager(config)
    instruments = config.get("instruments", ["BTC_USDT"])
    interval = config.get("poll_seconds", 300)
    min_signal = config.get("min_entry_signal", "STRONG_BUY")
    allowed_entries = {"STRONG_BUY"} if min_signal == "STRONG_BUY" else {"STRONG_BUY", "BUY"}

    log.info("Starting bot in %s mode | instruments: %s | poll every %ss",
             trader.mode.upper(), ", ".join(instruments), interval)
    if trader.mode == "live":
        log.warning("LIVE TRADING ENABLED - real orders will be placed on Crypto.com")

    while True:
        try:
            tick(exchange, trader, risk, instruments, allowed_entries, config)
        except ExchangeError as e:
            log.error("Exchange error: %s", e)
        except Exception:
            log.exception("Unexpected error in tick")
        time.sleep(interval)


def tick(exchange, trader, risk, instruments, allowed_entries, config):
    prices = {}
    analyses = {}
    for instrument in instruments:
        analysis, entry_candles = run_analysis(exchange, config, instrument)
        analyses[instrument] = (analysis, entry_candles)
        prices[instrument] = analysis.price

    equity = trader.equity(prices)
    daily = trader.daily_pnl()

    # 1. Manage open positions first (stops/targets/signal flips).
    for instrument, (analysis, entry_candles) in analyses.items():
        pos = trader.open_positions().get(instrument)
        if not pos:
            continue
        # Update price history for correlation checks
        trader.update_price_history(instrument, analysis.price)
        closed, pnl, reason = trader.manage_position(instrument, entry_candles[-1])
        if closed:
            log.info("CLOSED %s (%s) P&L %+.2f", instrument, reason, pnl)
            continue
        # Exit early if the analysis flips hard against the position.
        flip = (pos.side == "BUY" and analysis.signal == strategy.SIGNAL_STRONG_SELL) or \
               (pos.side == "SELL" and analysis.signal == strategy.SIGNAL_STRONG_BUY)
        if flip:
            pnl = trader.close_position(instrument, analysis.price, "signal reversed")
            log.info("CLOSED %s (signal reversed) P&L %+.2f", instrument, pnl)

    # 2. Circuit breaker: stop opening new trades after a bad day.
    if risk.daily_circuit_breaker(equity, daily):
        log.warning("Daily loss limit hit (%.2f). No new entries today.", daily)
        return

    # 3. Look for new entries (spot bot: long entries only).
    for instrument, (analysis, entry_candles) in analyses.items():
        if instrument in trader.open_positions():
            continue
        log.info("%s -> %s (score %+.2f, trend %s, price %.6g)",
                 instrument, analysis.signal, analysis.score, analysis.trend, analysis.price)
        if analysis.signal not in allowed_entries:
            continue
        if len(trader.open_positions()) >= risk.max_open_positions:
            log.info("Max open positions reached - skipping %s", instrument)
            continue
        plan = risk.plan_trade(instrument, "BUY", analysis.price, analysis.atr, equity,
                               analysis.nearest_support, analysis.nearest_resistance)
        if not plan:
            log.info("%s signal fired but no plan met the reward:risk requirement", instrument)
            continue
        reason = f"{analysis.signal} score {analysis.score:+.2f} trend {analysis.trend}"
        # Get price history for correlation check
        price_history = [c["c"] for c in entry_candles[-50:]]  # Last 50 candles
        trader.open_position(plan, reason, new_instrument_prices=price_history)
        log.info("OPENED %s BUY qty %.8g @ %.6g | SL %.6g TP %.6g | risking %.2f (R:R %.1f)",
                 instrument, plan.quantity, plan.entry, plan.stop_loss,
                 plan.take_profit, plan.risk_amount, plan.reward_risk)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "analyze":
        cmd_analyze(rest)
    elif cmd == "run":
        cmd_run()
    elif cmd == "status":
        cmd_status()
    elif cmd == "balance":
        cmd_balance()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
