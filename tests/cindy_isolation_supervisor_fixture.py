"""Child host-fact injection for isolated ordinary/supervisor protocol tests.

There is deliberately no production fixture flag or privilege bypass endpoint.
"""
from tests import bootstrap
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

from local_agent_manager import cindy_isolation_entry as entry
from local_agent_manager.cindy_isolation_controller import cleanup_source
from local_agent_manager.adapters import CindyAdapter, NativeIntegrityAdapter
from local_agent_manager.cleanup_service import CleanupService
from local_agent_manager.operation_coordinator import OperationCoordinator


def main():
    bundle, run_name = Path(sys.argv[1]), sys.argv[2]
    spec = json.loads((bundle / 'spec.json').read_text(encoding='utf8'))
    draft = json.loads((bundle / 'draft.json').read_text(encoding='utf8'))
    source, target = (Path(spec[k]['path']) for k in ('source', 'target'))
    database = Path(spec['controller']['cindy_database'])
    adapters = (NativeIntegrityAdapter(codex_home=source), CindyAdapter(database=database, codex_home=target, cindy_root=target.parent))
    class NativeServer:
        def __init__(self, **kwargs):
            assert Path(kwargs['codex_home']) == source
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def delete_thread(self, ident):
            assert ident == 'selected'
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as c:
                c.execute('delete from threads where id=?', (ident,)); c.commit()
            (source / 'sessions/rollout-selected.jsonl').unlink()
    def cleanup(*args, **kwargs):
        return cleanup_source(*args, **kwargs, adapters=adapters, app_server_factory=NativeServer,
            binary_resolver=lambda _: bundle / 'synthetic-codex',
            coordinator=OperationCoordinator(CleanupService(client_inspector=lambda *_: ())))
    with patch.object(entry, 'ctypes_is_admin', return_value=True), \
         patch('local_agent_manager.cindy_isolation_windows.no_cindy'), \
         patch.object(entry, 'cleanup_source', cleanup):
        return entry.elevated(bundle, spec, draft, 'fixture-manifest', run_name)


if __name__ == '__main__': raise SystemExit(main())
