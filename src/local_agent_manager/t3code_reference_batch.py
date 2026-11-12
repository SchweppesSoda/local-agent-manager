"""Reuse T3 reference scans only while a native batch excludes T3 writers.

The SQLite reserved transaction prevents other connections from changing any
reference table. Namespace fences and the environment lease bind the database
and native roots. The context ends before the frontend writer runs; cold
verification and every subsequent batch read fresh evidence.
"""
from contextlib import ExitStack, closing, contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sqlite3

from . import frozen_files

_batch = ContextVar('t3_reference_batch', default=None)


@contextmanager
def hold(document, mutation_family):
    from .t3code_cleanup import evidence_from_document
    from .t3code_lifecycle import current_boundary
    from .t3code_store import RELATIVE
    from .windows_held_files import HeldFiles
    evidence = evidence_from_document(document)
    if evidence is None or str(mutation_family) == 'delete_t3code_frontend':
        yield
        return
    root = Path(evidence['root'])
    if current_boundary(root) is None or _batch.get() is not None:
        raise frozen_files.FrozenFilesError('t3_reference_batch_boundary_missing')
    with ExitStack() as stack:
        path = stack.enter_context(frozen_files._parent_fence(root, RELATIVE))
        fd = frozen_files._windows_fd(path)
        stack.callback(os.close, fd)
        if frozen_files._identity(os.fstat(fd)) != frozen_files._identity(path.lstat()):
            raise frozen_files.FrozenFilesError('t3_reference_database_replaced')
        for native_root in sorted({b['root'] for b in evidence['binding_manifest']['native_stores']}):
            stack.enter_context(frozen_files._parent_fence(Path(native_root), '.reference-boundary'))
        held = stack.enter_context(HeldFiles())
        env, _ = frozen_files.read_file(root, 'userdata/environment-id')
        held.acquire(root, env)
        db = stack.enter_context(closing(sqlite3.connect(path, timeout=0)))
        db.execute('BEGIN IMMEDIATE')
        manifest = evidence['binding_manifest']
        token = _batch.set({'root': root, 'cache': {}, 'held': held, 'manifest': manifest,
                           'bindings': {b['thread_id']: b for b in manifest['native_stores']}})
        try:
            yield
        finally:
            _batch.reset(token)
            db.rollback()


def read_environment(root):
    batch = _batch.get()
    reader = batch['held'].read if batch is not None and batch['root'] == root else frozen_files.read_file
    return reader(root, 'userdata/environment-id', content=True, limit=256)


def read(adapter, loader):
    batch = _batch.get()
    # Pi references also depend on mutable native headers. Keep their existing
    # fresh inspection until a corresponding native-file lease is available.
    if (batch is None or batch['root'] != adapter.profile_root
            or batch['manifest'] != adapter.manifest or batch['bindings'] != adapter.bindings
            or any(b['engine'] == 'pi' for b in adapter.bindings.values())):
        return loader()
    key = json.dumps([adapter.manifest, adapter.bindings, adapter.frozen_references], sort_keys=True)
    cache = batch['cache']
    if key not in cache:
        snapshot = loader()
        if snapshot.errors:
            return snapshot
        cache[key] = (snapshot, adapter.observation, adapter.observations, adapter.legacy_import_ids)
    snapshot, adapter.observation, adapter.observations, adapter.legacy_import_ids = cache[key]
    return snapshot
