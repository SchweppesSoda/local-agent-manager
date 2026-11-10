"""Compatibility alias for the canonical implementation."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("local_agent_manager.antigravity_cli_joint")
