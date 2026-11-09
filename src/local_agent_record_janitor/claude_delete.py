"""Compatibility module. Implementation lives in local_agent_manager."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module('local_agent_manager.claude_delete')
