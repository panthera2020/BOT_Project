"""The strategies the engine can run. Add one here and it appears in the dashboard's list."""
from typing import Dict, List, Type

from src.engine.strategies.base import Entry, Strategy
from src.engine.strategies.breakout_retest import BreakoutRetest
from src.engine.strategies.range_reversion import RangeReversion
from src.engine.strategies.session_breakout import SessionBreakout
from src.engine.strategies.sweep_reversal import SweepReversal, SweepReversalLoose
from src.engine.strategies.trend_pullback import TrendPullback

DEFAULT_STRATEGY = SweepReversal.id

# Order = order shown in the dashboard.
STRATEGY_CLASSES: List[Type[Strategy]] = [
    SweepReversal, SweepReversalLoose, TrendPullback, BreakoutRetest, SessionBreakout, RangeReversion,
]
REGISTRY: Dict[str, Type[Strategy]] = {cls.id: cls for cls in STRATEGY_CLASSES}


def strategy_catalog() -> List[Dict[str, object]]:
    return [{"id": c.id, "name": c.name, "summary": c.summary, "rules": list(c.rules)} for c in STRATEGY_CLASSES]


__all__ = ["DEFAULT_STRATEGY", "REGISTRY", "STRATEGY_CLASSES", "Entry", "Strategy", "strategy_catalog"]
