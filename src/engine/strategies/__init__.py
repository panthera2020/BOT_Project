"""
The strategies the engine can run: plug and play.

Every module in this folder that defines a `Strategy` subclass with an `id` is picked up
automatically and appears in the dashboard list. To remove a strategy, delete its file (or list
its id in the DISABLED_STRATEGIES environment variable); nothing else needs to change. To add
one, drop in a new file. A file that fails to import is skipped with a warning, so one broken
strategy never takes the others (or the console) down. What the console saved for a strategy
that is gone (results, progress) is kept, so putting the file back restores it.
"""
import importlib
import inspect
import logging
import os
import pkgutil
from typing import Dict, Iterable, List, Optional, Type

from src.engine.strategies.base import Entry, Strategy

logger = logging.getLogger("Strategies")

DEFAULT_STRATEGY = "sweep_reversal"   # the original; its saved progress uses the long-standing state fields

# Filled by reload_strategies(); mutated in place so every importer sees changes.
REGISTRY: Dict[str, Type[Strategy]] = {}
STRATEGY_CLASSES: List[Type[Strategy]] = []


def _disabled_from_env() -> List[str]:
    return [x.strip() for x in os.environ.get("DISABLED_STRATEGIES", "").split(",") if x.strip()]


def discover(disabled: Optional[Iterable[str]] = None) -> List[Type[Strategy]]:
    """Strategy classes found in this folder, in dashboard order."""
    skip = set(disabled if disabled is not None else _disabled_from_env())
    found: Dict[str, Type[Strategy]] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # a broken strategy file must not take the console down
            logger.warning("Strategy file %s.py could not be loaded and is skipped: %s", info.name, exc)
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls is Strategy or not issubclass(cls, Strategy) or cls.__module__ != module.__name__ or not cls.id:
                continue
            if cls.id in skip:
                continue
            if cls.id in found:
                logger.warning("Two strategies use the id %r; keeping the first.", cls.id)
                continue
            found[cls.id] = cls
    return sorted(found.values(), key=lambda c: (c.order, c.id))


def reload_strategies(disabled: Optional[Iterable[str]] = None) -> None:
    classes = discover(disabled)
    STRATEGY_CLASSES[:] = classes
    REGISTRY.clear()
    REGISTRY.update({c.id: c for c in classes})


def ordered() -> List[Type[Strategy]]:
    """The installed strategies in dashboard order (read from REGISTRY, so it follows changes)."""
    return sorted(REGISTRY.values(), key=lambda c: (c.order, c.id))


def default_strategy_id() -> Optional[str]:
    """The original if installed, otherwise the first installed one, otherwise None."""
    if DEFAULT_STRATEGY in REGISTRY:
        return DEFAULT_STRATEGY
    classes = ordered()
    return classes[0].id if classes else None


def strategy_catalog() -> List[Dict[str, object]]:
    return [{"id": c.id, "name": c.name, "summary": c.summary, "rules": list(c.rules)} for c in ordered()]


reload_strategies()

__all__ = ["DEFAULT_STRATEGY", "REGISTRY", "STRATEGY_CLASSES", "Entry", "Strategy", "default_strategy_id",
           "discover", "ordered", "reload_strategies", "strategy_catalog"]
