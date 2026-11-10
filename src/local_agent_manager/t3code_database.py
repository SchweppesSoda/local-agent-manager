"""Exact T3 Code database-history transaction, subordinate to a local closure.

This module never deletes provider stores or guesses ownership from an ID.
Its caller must qualify those stores, files, UI caches and writer exclusion.
Evidence contains IDs, counts and digests; private cells stay inside SQLite.
"""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from . import frozen_files
from .office_database import logical_hash
from .sqlite_utils import connect_readonly
from .t3code_store import CONTRACT, RELATIVE, V2, T3CodeError, database_path, fail, identifier, native_handle, qualify, rows

SCHEMA = "larj.t3code-database.v1"
THREAD_TABLES = (
    "mcp_app_model_context", "projection_thread_messages", "projection_thread_pull_requests",
    "projection_thread_sessions", "provider_session_runtime", "projection_threads",
    "orchestration_v2_command_receipts", "orchestration_v2_effect_outbox", "orchestration_v2_events",
    "orchestration_v2_legacy_imports", "orchestration_v2_thread_launch_workflows", "orchestration_v2_turn_item_positions",
    *(V2 + name for name in ("checkpoint_scopes", "checkpoints", "context_handoffs", "messages", "nodes", "plans",
        "provider_session_bindings", "provider_sessions", "provider_threads", "provider_turns", "run_attempts", "runs",
        "runtime_requests", "subagents", "threads", "turn_items")),
)


def _where(column, ids):
    return column + " IN (" + ",".join("?" for _ in ids) + ")"


def _check(db):
    qualify(db)
    if [tuple(r) for r in db.execute("PRAGMA quick_check")] != [("ok",)]:
        fail("database_corrupt")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        fail("relations_inconsistent")


def closure(db, ids):
    ids = tuple(sorted({identifier(i) for i in ids}))
    if not ids:
        fail("empty_selection")
    where = _where("thread_id", ids)
    found = {r[0] for r in db.execute(f"SELECT thread_id FROM {V2}threads WHERE {where}", ids)}
    found.update(r[0] for r in db.execute(f"SELECT thread_id FROM projection_threads WHERE {where}", ids))
    if found != set(ids):
        fail("selected_thread_missing")
    if db.execute(f"SELECT 1 FROM scheduled_tasks WHERE {where} LIMIT 1", ids).fetchone():
        fail("scheduled_task_reference")
    if db.execute(f"SELECT 1 FROM {V2}context_transfers WHERE ({_where('source_thread_id',ids)}) != ({_where('target_thread_id',ids)}) LIMIT 1", ids + ids).fetchone():
        fail("cross_thread_transfer")
    if db.execute(f"SELECT 1 FROM {V2}subagents WHERE ({where}) != ({_where('child_thread_id',ids)}) AND child_thread_id IS NOT NULL LIMIT 1", ids + ids).fetchone():
        fail("cross_thread_subagent")
    # Provider sessions may be attached to more than one app thread.
    bindings = rows(db, f"SELECT provider_session_id,thread_id FROM {V2}provider_session_bindings")
    sessions = {r['provider_session_id'] for r in bindings if r['thread_id'] in ids}
    sessions.update(r[0] for r in db.execute(f"SELECT provider_session_id FROM {V2}provider_sessions WHERE {where}", ids))
    if any(r['provider_session_id'] in sessions and r['thread_id'] not in ids for r in bindings):
        fail("shared_provider_session")
    if sessions:
        session_ids = tuple(sorted(sessions))
        if db.execute(f"SELECT 1 FROM {V2}provider_sessions WHERE {_where('provider_session_id',session_ids)} AND thread_id IS NOT NULL AND NOT ({where}) LIMIT 1", session_ids + ids).fetchone():
            fail("shared_provider_session")
        if db.execute(f"SELECT 1 FROM {V2}provider_threads WHERE {_where('provider_session_id',session_ids)} AND (thread_id IS NULL OR NOT ({where})) LIMIT 1", session_ids + ids).fetchone():
            fail("shared_provider_session")
    # Unknown non-thread event ownership cannot be absorbed by an ID match.
    for table, column in (("orchestration_events", "stream_id"), ("orchestration_command_receipts", "aggregate_id")):
        if db.execute(f"SELECT 1 FROM {table} WHERE {_where(column,ids)} AND aggregate_kind != 'thread' LIMIT 1", ids).fetchone():
            fail("legacy_aggregate_ownership_unverified")
    statements = [(table, where, ids) for table in THREAD_TABLES if table != V2 + "provider_sessions"]
    if sessions:
        statements.append((V2 + "provider_sessions", _where("provider_session_id", tuple(sorted(sessions))), tuple(sorted(sessions))))
    statements.extend((
        (V2 + "context_transfers", _where("source_thread_id", ids), ids),
        ("orchestration_events", "aggregate_kind='thread' AND " + _where("stream_id", ids), ids),
        ("orchestration_command_receipts", "aggregate_kind='thread' AND " + _where("aggregate_id", ids), ids),
    ))
    counts = {}
    native_refs = set()
    for table, predicate, args in statements:
        counts[table] = db.execute(f'SELECT count(*) FROM "{table}" WHERE {predicate}', args).fetchone()[0]
        # An unselected event/projection that mentions an exact selected ID
        # may replay a relationship. Conservatively block instead of editing it.
        columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
        for column in (c for c in columns if c.endswith("_json")):
            if db.execute(f'''SELECT 1 FROM "{table}" AS t, json_tree(t."{column}") AS j
                WHERE NOT ({predicate}) AND j.type='text' AND {_where('j.value',ids)} LIMIT 1''', args + ids).fetchone():
                fail("unselected_restore_reference")
        # All historical nativeThreadRef values are included, not just the
        # current projection. This also catches previous provider switches.
        if "payload_json" in columns:
            for row in rows(db, f'''SELECT DISTINCT json_extract(j.value,'$.driver') AS driver,
                    json_extract(j.value,'$.nativeId') AS native_id
                FROM "{table}" AS t,json_tree(t.payload_json) AS j
                WHERE ({predicate}) AND j.key='nativeThreadRef' AND j.type='object' ''', args):
                if row["native_id"] is not None:
                    native_refs.add((identifier(row["driver"]), native_handle(row["driver"], row["native_id"])))
    active = db.execute(f"SELECT 1 FROM orchestration_v2_effect_outbox WHERE {where} AND status IN ('pending','running') LIMIT 1", ids).fetchone()
    if active:
        fail("pending_effects")
    return {"counts": counts, "provider_session_ids": sorted(sessions),
        "native_references": [{"driver": d, "native_id": i} for d, i in sorted(native_refs)]}, statements


def _transform(db, ids):
    owned, statements = closure(db, ids)
    for table, predicate, args in statements:
        changed = db.execute(f'DELETE FROM "{table}" WHERE {predicate}', args).rowcount
        if changed != owned["counts"][table]:
            fail("affected_row_count_changed")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        fail("relations_inconsistent")


def freeze(root, ids):
    ids = sorted({identifier(i) for i in ids})
    path = database_path(root)
    identity = list(frozen_files._identity(path.stat()))
    with closing(connect_readonly(path)) as source:
        source.execute("BEGIN")
        deadline = time.monotonic() + 20
        source.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        _check(source)
        owned, _ = closure(source, ids)
        before = logical_hash(source)
        with tempfile.TemporaryDirectory(prefix="larj-t3code-simulation-") as directory:
            with closing(sqlite3.connect(Path(directory) / "state.sqlite")) as simulation:
                source.backup(simulation)
                simulation.row_factory = sqlite3.Row
                simulation.execute("PRAGMA foreign_keys=ON")
                simulation.execute("PRAGMA secure_delete=ON")
                _transform(simulation, ids)
                staged = logical_hash(simulation)
                simulation.commit()
                after = logical_hash(simulation)
    if list(frozen_files._identity(database_path(root).stat())) != identity:
        fail("database_replaced")
    return {"schema_version": SCHEMA, "root": str(root), "relative": RELATIVE, "thread_ids": ids,
        "identity": identity, "before_sha256": before, "staged_sha256": staged, "after_sha256": after, **owned}


def validate(evidence):
    if not isinstance(evidence, dict) or evidence.get("schema_version") != SCHEMA or evidence.get("relative") != RELATIVE:
        fail("database_evidence_invalid")
    ids = evidence.get("thread_ids")
    if not isinstance(ids, list) or not ids or ids != sorted({identifier(i) for i in ids}):
        fail("database_evidence_invalid")
    for key in ("before_sha256", "staged_sha256", "after_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get(key, ""))):
            fail("database_evidence_invalid")
    root = Path(evidence["root"])
    path = database_path(root)
    if list(frozen_files._identity(path.stat())) != evidence.get("identity"):
        fail("database_replaced")
    return root, path


@contextmanager
def _fence(root, path, identity):
    with frozen_files._parent_fence(root, RELATIVE):
        fd = frozen_files._windows_fd(path) if os.name == "nt" else os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if list(frozen_files._identity(os.fstat(fd))) != identity:
                fail("database_replaced")
            yield
            if list(frozen_files._identity(path.lstat())) != identity:
                fail("database_replaced")
        finally:
            os.close(fd)


def require_wal_truncated(evidence):
    _, path = validate(evidence)
    try:
        wal = Path(str(path) + "-wal").lstat()
    except FileNotFoundError:
        return
    if wal.st_size:
        fail("database_wal_not_truncated")


def apply(evidence, *, phase_callback, truncate_wal=False):
    root, path = validate(evidence)
    started = False
    try:
        with _fence(root, path, evidence["identity"]), closing(sqlite3.connect(path, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            _check(db)
            if logical_hash(db) != evidence["before_sha256"]:
                fail("database_changed")
            owned, _ = closure(db, evidence["thread_ids"])
            if owned != {key: evidence[key] for key in owned}:
                fail("database_closure_changed")
            phase_callback("mutation_started")
            started = True
            _transform(db, evidence["thread_ids"])
            if logical_hash(db) != evidence["staged_sha256"]:
                fail("database_after_state_unverified")
            db.commit()
            if truncate_wal and db.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                if tuple(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) != (0, 0, 0):
                    fail("database_wal_not_truncated")
        if remaining(evidence):
            fail("database_targets_remain")
        if truncate_wal:
            require_wal_truncated(evidence)
    except Exception as exc:
        raise T3CodeError(getattr(exc, "kind", "t3code_database_transaction_failed"), unknown=started) from exc


def remaining(evidence, *, terminal_verified=False):
    _, path = validate(evidence)
    ids = tuple(evidence["thread_ids"])
    with closing(connect_readonly(path)) as db:
        db.execute("BEGIN")
        _check(db)
        remaining_count = 0
        for table in THREAD_TABLES:
            remaining_count += db.execute(f'SELECT count(*) FROM "{table}" WHERE {_where("thread_id",ids)}', ids).fetchone()[0]
        for table, column in (("orchestration_events", "stream_id"), ("orchestration_command_receipts", "aggregate_id"),
                              (V2 + "context_transfers", "source_thread_id"), (V2 + "context_transfers", "target_thread_id")):
            remaining_count += db.execute(f'SELECT count(*) FROM "{table}" WHERE {_where(column,ids)}', ids).fetchone()[0]
        sessions = tuple(evidence["provider_session_ids"])
        if sessions:
            remaining_count += db.execute(f'SELECT count(*) FROM {V2}provider_sessions WHERE {_where("provider_session_id",sessions)}', sessions).fetchone()[0]
        if remaining_count and logical_hash(db) != evidence["before_sha256"]:
            fail("database_changed")
        if not remaining_count and not terminal_verified and logical_hash(db) != evidence["after_sha256"]:
            fail("database_after_state_unverified")
        return remaining_count
