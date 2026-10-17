"""
Unified Trade Records & Live/Demo Statistics Service.
Computes total trades, profit/loss (PnL in USDT & R), win rate, and trade histories.
"""
from dataclasses import dataclass
from typing import List, Dict, Any
from datetime import datetime, timezone


@dataclass
class TradeRecord:
    id: int
    mode: str          # "DEMO" or "LIVE"
    symbol: str
    side: str          # "Buy" or "Sell"
    qty: float
    entry_price: float
    exit_price: float
    pnl_usd: float
    pnl_r: float
    is_win: bool
    status: str        # "CLOSED", "OPEN"
    timestamp: str


class TradeStatsTracker:
    def __init__(self):
        self.trades: List[TradeRecord] = []
        self._next_id = 1

    def record_completed_trade(
        self,
        mode: str,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        exit_price: float,
        pnl_usd: float,
        pnl_r: float,
    ) -> TradeRecord:
        is_win = pnl_usd > 0
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        trade = TradeRecord(
            id=self._next_id,
            mode=mode.upper(),
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_usd=round(pnl_usd, 2),
            pnl_r=round(pnl_r, 2),
            is_win=is_win,
            status="CLOSED",
            timestamp=now_str,
        )
        self.trades.insert(0, trade)  # newest first
        self._next_id += 1
        return trade

    def get_stats(self, mode: str) -> Dict[str, Any]:
        filtered = [t for t in self.trades if t.mode == mode.upper()]
        total_trades = len(filtered)
        if total_trades == 0:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl_usd": 0.0,
                "total_pnl_r": 0.0,
            }

        wins = sum(1 for t in filtered if t.is_win)
        losses = total_trades - wins
        win_rate = round((wins / total_trades) * 100.0, 1)
        total_pnl_usd = round(sum(t.pnl_usd for t in filtered), 2)
        total_pnl_r = round(sum(t.pnl_r for t in filtered), 2)

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl_usd": total_pnl_usd,
            "total_pnl_r": total_pnl_r,
        }

    def get_recent_trades(self, limit: int = 15) -> List[Dict[str, Any]]:
        return [
            {
                "id": t.id,
                "mode": t.mode,
                "symbol": t.symbol,
                "side": t.side,
                "qty": t.qty,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl_usd": t.pnl_usd,
                "pnl_r": t.pnl_r,
                "is_win": t.is_win,
                "timestamp": t.timestamp,
            }
            for t in self.trades[:limit]
        ]
