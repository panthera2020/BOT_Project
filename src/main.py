"""
Main runtime orchestrator tying together Strategy 1, Risk Engine, Exit Policy, and Exchange Adapter.
Can execute in DEMO (Bybit Demo account), BACKTEST, or LIVE mode.
"""
import asyncio
import logging
from datetime import datetime, timezone
from src.core.models import BTCUSDT_CONFIG, OrderSide, TrendDirection
from src.core.risk_schedule import evaluate_risk_schedule
from src.core.position_sizing import calculate_position_size
from src.core.net_rr_gate import calculate_net_rr
from src.core.daily_cap import DailyTradeTracker
from src.core.exit_policy import build_trade_exit_plan, ExitPolicyMonitor
from src.strategy.strategy1 import Strategy1StateMachine, Candle
from src.exchange.adapter import FakeExchangeAdapter
from src.exchange.bybit_adapter import BybitV5Adapter
from src.storage.state_store import StateStore
from src.alerts.notifier import AlertDispatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("BotRunner")


class BotEngine:
    def __init__(
        self,
        mode: str = "DEMO",
        demo_key: str = "",
        demo_secret: str = "",
        db_path: str = "trading_bot.db",
    ):
        self.mode = mode
        self.config = BTCUSDT_CONFIG
        self.state_store = StateStore(db_path=db_path)
        self.daily_tracker = DailyTradeTracker()
        self.strategy = Strategy1StateMachine(symbol=self.config.symbol)
        self.notifier = AlertDispatcher()

        if mode == "DEMO" and demo_key and demo_secret:
            self.exchange = BybitV5Adapter(api_key=demo_key, api_secret=demo_secret, is_demo=True)
        else:
            self.exchange = FakeExchangeAdapter(initial_balance=500.0)

        self.is_running = False

    async def start(self) -> None:
        await self.state_store.init_db()
        self.is_running = True
        logger.info(f"Bot Engine initialized in [{self.mode}] mode for symbol [{self.config.symbol}].")
        await self.notifier.send_alert(
            title="BOT_STARTED",
            body=f"Engine running in {self.mode} mode for {self.config.symbol}.",
            level="INFO",
        )

    async def stop(self) -> None:
        self.is_running = False
        logger.info("Bot Engine stopped.")
        await self.notifier.send_alert(title="BOT_STOPPED", body="Engine stopped by operator.", level="WARNING")


if __name__ == "__main__":
    runner = BotEngine(mode="BACKTEST")
    asyncio.run(runner.start())
