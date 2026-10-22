"""
What every strategy provides, and what the engine does for all of them.

A strategy only answers two questions: "is there a trade now?" (side, and where the stop
goes) and, through its own saved state, "what am I waiting for?". Everything after that is
shared and identical for every strategy, so their results are comparable and the same gates
apply: capital / leverage tiers, position sizing, net reward-to-risk >= 2, the margin limit,
the stop and take-profits placed on Bybit, breakeven, the early exit, the kill switch, and the
24h + 20 clean trades demo gate (tracked per strategy).
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# How the engine sets the risk for the trade:
#   "table"  the PRD trend table as written: a Range on 1h or 15m stands down, half risk when
#            the two disagree, and the side must still be the one the table allows (strategy 1)
#   "half_unaligned"  full risk when the 1h and 15m trends agree, half risk otherwise
#            (including a Range on the 1h); the strategy chose the side itself
#   "full"   the strategy decides when to trade; the trade gets the full base risk
RISK_TABLE = "table"
RISK_HALF_UNALIGNED = "half_unaligned"
RISK_FULL = "full"


@dataclass
class Entry:
    side: str                  # "Buy" or "Sell"
    stop_price: float          # raw stop level; the engine rounds it to the tick
    trigger_price: float       # price used for sizing (the fill price is read back from Bybit)
    risk_mode: str = RISK_FULL


class Strategy:
    id: str = ""
    order: int = 100          # position in the dashboard list (lower first); ties by id
    name: str = ""
    summary: str = ""
    rules: List[str] = []

    def __init__(self, engine: Any):
        self.engine = engine

    @property
    def slot(self) -> Dict[str, Any]:
        """This strategy's own saved state (survives restarts with the rest of the state)."""
        return self.engine.es.setdefault("strategies", {}).setdefault(self.id, {})

    async def scan(self, now: Any, equity: float, c15: list) -> Optional[Entry]:
        """Called each tick while flat. `c15` = closed 15m candles, oldest first. Returns an
        Entry to trade, or None. Sets engine.status["text"] to say what it is waiting for."""
        raise NotImplementedError

    def new_candle(self, c15: list) -> bool:
        """True once per newly closed 15m candle (and remembers it)."""
        latest = c15[-1].timestamp
        if latest > self.slot.get("last_15m_eval_ts", 0):
            self.slot["last_15m_eval_ts"] = latest
            return True
        return False
