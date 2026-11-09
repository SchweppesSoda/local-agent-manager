"""Compatibility exports for the Local Agent Manager package."""
from importlib import import_module as _import_module

_implementation = _import_module('local_agent_manager.adapters')
__all__ = _implementation.__all__
globals().update({name: getattr(_implementation, name) for name in __all__})
def __getattr__(name):
    return getattr(_implementation, name)
