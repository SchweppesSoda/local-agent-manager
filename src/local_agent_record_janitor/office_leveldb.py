"""Compatibility module. Implementation lives in local_agent_manager."""
from importlib import import_module as _import_module
import sys as _sys

if __name__ == "__main__":
    import runpy
    runpy.run_module('local_agent_manager.office_leveldb', run_name="__main__")
else:
    _sys.modules[__name__] = _import_module('local_agent_manager.office_leveldb')
