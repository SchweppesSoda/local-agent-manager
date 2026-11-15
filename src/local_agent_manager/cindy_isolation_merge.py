"""Exact native-row merge primitive for a frozen Cindy home split.

The caller owns admission, lifecycle exclusion, immutable snapshots, per-database
write-ahead checkpoints and the transaction. This never commits, deletes source
rows, changes credentials or switches frontend bindings. Opaque payloads stay
inside SQLite; verification compares complete rows without returning chat text.
"""
from __future__ import annotations

import sqlite3

from .cindy_isolation_plan import IsolationPlanError


TABLES = {
    "threads": ("id",), "thread_dynamic_tools": ("thread_id",),
    "thread_spawn_edges": ("parent_thread_id", "child_thread_id"),
    "thread_attachments": ("thread_id",), "guardian_review_feedback": ("thread_id",),
    "thread_read_receipts": ("thread_id",), "thread_turns": ("thread_id",),
    "thread_items": ("thread_id",), "thread_history_projection_state": ("thread_id",),
    "thread_realtime_items": ("thread_id",), "thread_goals": ("thread_id",),
    "thread_goal_continuation_deferrals": ("thread_id",), "queued_items": ("thread_id",),
    "queued_thread_revisions": ("thread_id",), "stage1_outputs": ("thread_id",),
}

# Codex state_5 timestamp/recency triggers. Each UPDATE is constrained to NEW.id;
# complete-row verification below still rejects any inserted-value change.
THREAD_TRIGGERS = {
    "threads_created_at_ms_after_insert": "CREATE TRIGGER threads_created_at_ms_after_insert AFTER INSERT ON threads WHEN NEW.created_at_ms IS NULL BEGIN UPDATE threads SET created_at_ms = NEW.created_at * 1000 WHERE id = NEW.id; END",
    "threads_updated_at_ms_after_insert": "CREATE TRIGGER threads_updated_at_ms_after_insert AFTER INSERT ON threads WHEN NEW.updated_at_ms IS NULL BEGIN UPDATE threads SET updated_at_ms = NEW.updated_at * 1000 WHERE id = NEW.id; END",
    "threads_created_at_ms_after_update": "CREATE TRIGGER threads_created_at_ms_after_update AFTER UPDATE OF created_at ON threads WHEN NEW.created_at != OLD.created_at AND NEW.created_at_ms IS OLD.created_at_ms BEGIN UPDATE threads SET created_at_ms = NEW.created_at * 1000 WHERE id = NEW.id; END",
    "threads_updated_at_ms_after_update": "CREATE TRIGGER threads_updated_at_ms_after_update AFTER UPDATE OF updated_at ON threads WHEN NEW.updated_at != OLD.updated_at AND NEW.updated_at_ms IS OLD.updated_at_ms BEGIN UPDATE threads SET updated_at_ms = NEW.updated_at * 1000 WHERE id = NEW.id; END",
    "threads_recency_at_after_insert": "CREATE TRIGGER threads_recency_at_after_insert AFTER INSERT ON threads WHEN NEW.recency_at_ms = 0 BEGIN UPDATE threads SET recency_at = NEW.updated_at, recency_at_ms = COALESCE(NEW.updated_at_ms, NEW.updated_at * 1000) WHERE id = NEW.id; END",
}
HISTORY_TRIGGERS = {
    "thread_realtime_items_projection_cleanup": "CREATE TRIGGER thread_realtime_items_projection_cleanup AFTER DELETE ON thread_history_projection_state BEGIN DELETE FROM thread_realtime_items WHERE thread_id = OLD.thread_id; END",
}


def _trigger_definitions(connection, schema, table):
    return {name: " ".join(sql.split()) for name, sql in connection.execute(
        "select name,sql from " + schema + ".sqlite_master where type='trigger' and tbl_name=?", (table,))}


def _q(value):
    return '"' + value.replace('"', '""') + '"'


def merge_selected_rows(connection: sqlite3.Connection, *, native_ids: list[str],
                        tables: list[str], rollout_rewrites: dict[str, str]) -> dict:
    """Merge from attached ``incoming`` into ``main`` within caller transaction.

    Every selected row must have an exact stable primary key and matching table
    schema. Conflicting target rows are never replaced, even if text looks equal.
    Global jobs/projects require separate qualified dependency planning; a
    nonempty dependency outside these per-thread tables cannot use this entry.
    """
    if not connection.in_transaction or not native_ids or not tables:
        raise IsolationPlanError("frozen_scope_and_transaction_required")
    if len(tables) != len(set(tables)) or any(t not in TABLES for t in tables):
        raise IsolationPlanError("unqualified_table_scope")
    connection.execute("savepoint cindy_native_merge")
    try:
        connection.execute("create temp table _cindy_merge_ids(id text primary key)")
        connection.executemany("insert into _cindy_merge_ids values(?)", [(i,) for i in sorted(set(native_ids))])
        connection.execute("create temp table _cindy_merge_paths(source text primary key, target text not null unique)")
        connection.executemany("insert into _cindy_merge_paths values(?,?)", sorted(rollout_rewrites.items()))
        selections = {}
        for table in tables:
            columns = list(connection.execute("pragma main.table_info(" + _q(table) + ")"))
            other = list(connection.execute("pragma incoming.table_info(" + _q(table) + ")"))
            if not columns or columns != other:
                raise IsolationPlanError("native_table_schema_mismatch:" + table)
            source_triggers = _trigger_definitions(connection, "incoming", table)
            target_triggers = _trigger_definitions(connection, "main", table)
            allowed_triggers = {"threads": THREAD_TRIGGERS,
                                "thread_history_projection_state": HISTORY_TRIGGERS}.get(table)
            if (source_triggers != target_triggers or source_triggers
                    and source_triggers != allowed_triggers):
                raise IsolationPlanError("unqualified_native_trigger:" + table)
            definitions = [connection.execute("select sql from " + schema + ".sqlite_master where type='table' and name=?", (table,)).fetchone()[0] for schema in ("main", "incoming")]
            if definitions[0] != definitions[1]:
                raise IsolationPlanError("native_table_definition_mismatch:" + table)
            names = [r[1] for r in columns]
            keys = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
            if not keys or any(k not in names for k in TABLES[table]):
                raise IsolationPlanError("native_primary_key_unqualified:" + table)
            predicate = " or ".join("s." + _q(k) + " in (select id from temp._cindy_merge_ids)" for k in TABLES[table])
            selected = "select s.* from incoming." + _q(table) + " s where " + predicate
            collision = "select exists(select 1 from main." + _q(table) + " d join (" + selected + ") s on " + " and ".join("d." + _q(k) + " is s." + _q(k) for k in keys) + ")"
            if connection.execute(collision).fetchone()[0]:
                raise IsolationPlanError("target_primary_key_conflict:" + table)
            expressions = ["s." + _q(k) for k in names]
            if table == "threads":
                missing = "select count(*) from (" + selected + ") s where not exists(select 1 from temp._cindy_merge_paths p where p.source=s.rollout_path)"
                if connection.execute(missing).fetchone()[0]:
                    raise IsolationPlanError("rollout_rewrite_missing")
                expressions[names.index("rollout_path")] = "(select target from temp._cindy_merge_paths p where p.source=s.rollout_path)"
            expected = "select " + ",".join(expressions) + " from incoming." + _q(table) + " s where " + predicate
            selections[table] = (names, keys, expected)
        counts = {}
        for table, (names, keys, expected) in selections.items():
            count = connection.execute("select count(*) from (" + expected + ")").fetchone()[0]
            connection.execute("insert into main." + _q(table) + "(" + ",".join(map(_q, names)) + ") " + expected)
            actual = "select " + ",".join("d." + _q(k) for k in names) + " from main." + _q(table) + " d join (" + expected + ") s on " + " and ".join("d." + _q(k) + " is s." + _q(k) for k in keys)
            if (connection.execute("select exists(select * from (" + expected + " except " + actual + "))").fetchone()[0]
                    or connection.execute("select count(*) from (" + actual + ")").fetchone()[0] != count):
                raise IsolationPlanError("native_full_row_verification_failed:" + table)
            counts[table] = count
        connection.execute("drop table temp._cindy_merge_paths")
        connection.execute("drop table temp._cindy_merge_ids")
        connection.execute("release cindy_native_merge")
        return {"rows_inserted": counts, "full_rows_verified": True, "committed": False}
    except BaseException:
        connection.execute("rollback to cindy_native_merge")
        connection.execute("release cindy_native_merge")
        raise
