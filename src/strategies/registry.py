"""Strategy auto-discovery — scan the strategies directory and register all scan/on functions."""

import importlib
import inspect
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class StrategyEntry:
    strategy_id: str
    module_name: str
    scan_function: Callable
    description: str = ""
    tier: str = "swing"
    signal_sources: list[str] = field(default_factory=list)


STRATEGY_REGISTRY: dict[str, StrategyEntry] = {}
_discovered = False
_discovery_lock = threading.Lock()

_SKIP_FILES = {"__init__.py", "registry.py"}
_SCAN_PATTERN = re.compile(r"^(scan_|on_)")


def discover_strategies(strategies_dir: Optional[str] = None) -> dict[str, StrategyEntry]:
    """Scan the strategies directory and register all scan_*/on_* functions.

    Each strategy module can optionally define a STRATEGY_META dict for richer metadata.
    Without it, functions are registered using their name as the strategy ID.
    """
    global _discovered
    STRATEGY_REGISTRY.clear()

    if strategies_dir is None:
        strategies_dir = str(Path(__file__).parent)

    strat_path = Path(strategies_dir)

    for py_file in sorted(strat_path.glob("*.py")):
        if py_file.name in _SKIP_FILES:
            continue

        module_name = f"src.strategies.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception as err:
            import logging
            logging.getLogger(__name__).warning("Failed to import strategy %s: %s", module_name, err)
            continue

        meta = getattr(module, "STRATEGY_META", None)

        if meta and "strategies" in meta:
            for entry_meta in meta["strategies"]:
                func_name = entry_meta.get("function", "")
                func = getattr(module, func_name, None)
                if func and callable(func):
                    sid = entry_meta["id"]
                    if sid in STRATEGY_REGISTRY:
                        raise ValueError(
                            f"Duplicate strategy ID '{sid}' in {module_name} "
                            f"(already registered from {STRATEGY_REGISTRY[sid].module_name})"
                        )
                    STRATEGY_REGISTRY[sid] = StrategyEntry(
                        strategy_id=sid,
                        module_name=module_name,
                        scan_function=func,
                        description=entry_meta.get("description", ""),
                        tier=entry_meta.get("tier", "swing"),
                        signal_sources=meta.get("signal_sources", []),
                    )
        else:
            # Fallback: discover by function name convention
            for name, obj in inspect.getmembers(module, inspect.isfunction):
                if _SCAN_PATTERN.match(name) and obj.__module__ == module.__name__:
                    sid = _SCAN_PATTERN.sub("", name, count=1)
                    if sid in STRATEGY_REGISTRY:
                        raise ValueError(
                            f"Duplicate strategy ID '{sid}' in {module_name} "
                            f"(already registered from {STRATEGY_REGISTRY[sid].module_name})"
                        )
                    STRATEGY_REGISTRY[sid] = StrategyEntry(
                        strategy_id=sid,
                        module_name=module_name,
                        scan_function=obj,
                        description=f"Auto-discovered from {py_file.name}:{name}",
                    )

    _discovered = True
    return STRATEGY_REGISTRY


def get_registry() -> dict[str, StrategyEntry]:
    if not _discovered:
        with _discovery_lock:
            if not _discovered:  # double-checked locking
                discover_strategies()
    return STRATEGY_REGISTRY


def get_scan_functions() -> list[Callable]:
    return [entry.scan_function for entry in get_registry().values()]
