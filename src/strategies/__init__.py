"""Strategy registry — auto-discovered from this directory."""

from src.strategies.registry import discover_strategies, get_registry, get_scan_functions

# Trigger discovery on import
_registry = discover_strategies()

# Backward compatibility: expose scan functions at module level
for _entry in _registry.values():
    globals()[_entry.scan_function.__name__] = _entry.scan_function

__all__ = [entry.scan_function.__name__ for entry in _registry.values()]
