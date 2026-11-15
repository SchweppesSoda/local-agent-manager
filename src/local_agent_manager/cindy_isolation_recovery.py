"""Body-free observation and recovery planning for the legacy Cindy import.

This is deliberately not mutation authority. Observations made while products
are running are provisional. An executor must repeat them under its admitted
offline boundary and SQLite transactions. Unknown writes are never replayed;
recovery freezes a *new* remaining plan and never restores a whole target DB.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .cindy_isolation_merge import TABLES
from .cindy_isolation_plan import (
    IsolationPlanError, directory_identity, fingerprint, readonly_sqlite,
    registry_patch,
)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def q(name):
    return '"' + name.replace('"', '""') + '"'


def _predicate(table, ids, alias=""):
    if table not in TABLES or not ids:
        raise IsolationPlanError("unqualified_selection")
    marks = ",".join("?" for _ in ids)
    return (" or ".join(alias + q(k) + " in (" + marks + ")" for k in TABLES[table]),
            list(ids) * len(TABLES[table]))


def _hash_rows(rows):
    # Type tags preserve NULL, blobs, floats and strings. No values escape.
    hashes = []
    for row in rows:
        values = [(type(v).__name__, v.hex() if isinstance(v, bytes) else v) for v in row]
        hashes.append(fingerprint(values))
    return fingerprint(sorted(hashes))


def database_preservation(connection, ids, tables):
    """Fingerprint every non-selected row, including non-imported tables.

    The before/after comparison belongs inside the same write transaction.
    Runtime increments after that transaction are not corruption evidence.
    """
    result = {}
    for (table,) in connection.execute(
            "select name from main.sqlite_master where type='table' order by name"):
        if table.startswith("sqlite_"):
            continue
        if table in tables:
            predicate, params = _predicate(table, ids)
            # Nullable ownership columns must remain in the protected set.
            sql = "select * from main." + q(table) + " where coalesce((" + predicate + "),0)=0"
        else:
            sql, params = "select * from main." + q(table), []
        result[table] = _hash_rows(connection.execute(sql, params))
    return result


def compare_packet(connection, *, ids, tables, source_paths=None):
    """Compare complete rows to attached ``incoming``, without exposing bodies.

    An exact selected set is required for an offline merge/switch. Additional
    runtime rows or changes are explicitly retained but never called complete.
    Packet rollout paths already name the target; source_paths maps source to
    target only for a source comparison. Empty selected tables are checked too.
    """
    result = {}
    # A table empty at freeze may acquire a selected attachment/tool/realtime
    # row later. Nonzero rows_inserted alone is not a complete dependency scope.
    available = {r[0] for r in connection.execute("select name from incoming.sqlite_master where type='table'")}
    tables = sorted(set(tables) | (set(TABLES) & available))
    for table in tables:
        columns = [tuple(r) for r in connection.execute("pragma main.table_info(" + q(table) + ")")]
        other = [tuple(r) for r in connection.execute("pragma incoming.table_info(" + q(table) + ")")]
        schema = [list(connection.execute(
            "select type,name,sql from " + name + ".sqlite_master where tbl_name=? and sql is not null order by type,name",
            (table,))) for name in ("main", "incoming")]
        if not columns or columns != other or [tuple(r) for r in schema[0]] != [tuple(r) for r in schema[1]]:
            raise IsolationPlanError("packet_schema_mismatch:" + table)
        names = [r[1] for r in columns]
        predicate, params = _predicate(table, ids, "s.")
        expr = ["s." + q(n) for n in names]
        if table == "threads" and source_paths is not None:
            connection.create_function("_cindy_frozen_path", 1, source_paths.get)
            expr[names.index("rollout_path")] = "_cindy_frozen_path(s.rollout_path)"
        actual = "select " + ",".join(expr) + " from main." + q(table) + " s where " + predicate
        expected = "select * from incoming." + q(table)
        count = connection.execute("select count(*) from (" + actual + ")", params).fetchone()[0]
        wanted = connection.execute("select count(*) from (" + expected + ")").fetchone()[0]
        missing = connection.execute("select count(*) from (" + expected + " except " + actual + ")", params).fetchone()[0]
        extra = connection.execute("select count(*) from (" + actual + " except " + expected + ")", params).fetchone()[0]
        result[table] = {"rows": count, "expected": wanted, "missing_or_changed": missing,
                         "additional_or_changed": extra,
                         "state": "after" if not missing and not extra and count == wanted else
                         "before" if not count and wanted else "diverged"}
    states = {r["state"] for r in result.values() if r["expected"] or r["rows"]}
    return {"tables": result, "state": next(iter(states)) if len(states) == 1 else
            "after" if not states else "mixed", "full_rows_compared": True}


def observe_empty_dependencies(draft, ids):
    """Recheck previously empty goals/queue/jobs/projects, not just 71 IDs."""
    group = draft["groups"]["current_shared_candidates"]
    with readonly_sqlite(Path(group["source_home"]["path"]) / "state_5.sqlite") as c:
        marks = ",".join("?" for _ in ids)
        rows = c.execute("select thread_section_id,project_id from threads where id in (" + marks + ")", ids).fetchall()
    sections, projects = ({r[i] for r in rows if r[i] is not None} for i in (0, 1))
    result = {}
    for dependency in group["dependencies"]:
        with readonly_sqlite(Path(dependency["identity"]["path"])) as c:
            for table, evidence in dependency["tables"].items():
                if evidence.get("class") != "copy_selected_rows" or evidence["selected_rows"]:
                    continue
                values = sorted(sections if table == "thread_sections" else projects if table in {
                    "projects", "project_roots", "project_idempotency_keys"} else ids)
                selectors = evidence["selectors"]
                if not values:
                    count = 0
                else:
                    marks = ",".join("?" for _ in values)
                    predicate = " or ".join(q(k) + " in (" + marks + ")" for k in selectors)
                    count = c.execute("select count(*) from " + q(table) + " where " + predicate,
                                      values * len(selectors)).fetchone()[0]
                result[Path(dependency["identity"]["path"]).name + ":" + table] = {
                    "rows": count, "state": "after" if not count else "diverged"}
    return result


def observe_database(path, packet, ids, *, source_paths=None):
    with readonly_sqlite(Path(path)) as connection:
        connection.execute("attach database ? as incoming", (Path(packet["packet"]).as_uri() + "?mode=ro",))
        if connection.execute("pragma main.quick_check(1)").fetchone()[0] != "ok":
            raise IsolationPlanError("database_integrity_failed")
        return compare_packet(connection, ids=ids, tables=list(packet["rows_inserted"]), source_paths=source_paths)


def file_state(path, expected):
    path = Path(path)
    if not os.path.lexists(path):
        return "before"
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 1024 or not path.is_file():
        return "diverged"
    return "after" if digest(path) == expected else "diverged"


def projected_registry(before, spec):
    after = registry_patch(before, spec["binding_changes"])
    pool = after["pools"]["openai"]
    route = spec["route_change"]
    if (("legacyCodexDefault" in pool) != route["before_present"]
            or pool.get("legacyCodexDefault") != route["before"]):
        raise IsolationPlanError("default_route_changed_since_freeze")
    pool["legacyCodexDefault"] = True
    return after


def registry_state(current, before, spec):
    after = projected_registry(before, spec)
    # New unrelated bindings are positive protection sources, never overwritten.
    for state, expected in (("before", before), ("after", after)):
        projected = deepcopy(current)
        for key in set(projected.get("bindings", {})) - set(expected.get("bindings", {})):
            projected["bindings"].pop(key)
        if projected == expected: return state
    return "diverged"


def auth_state(spec):
    source, target = (Path(spec["auth"][k]["path"]) for k in ("source", "target"))
    try:
        source_info = source.stat()
        source_generation = all(getattr(source_info, attr) == spec["auth"]["source"][key] for attr, key in (
            ("st_dev", "device"), ("st_ino", "inode"), ("st_size", "size"), ("st_mtime_ns", "mtime_ns")))
        if os.path.samefile(source, target):
            return {"state": "before", "independent_regular_file": False,
                    "source_generation_matches_freeze": source_generation}
        info = target.lstat()
        independent = (target.is_file() and not target.is_symlink() and info.st_nlink == 1
                       and not getattr(info, "st_file_attributes", 0) & 1024)
        # Credential bodies and digests are deliberately absent from output.
        same_bytes = independent and digest(source) == digest(target)
        return {"state": "after" if same_bytes else "diverged",
                "independent_regular_file": bool(independent),
                "source_generation_matches_freeze": source_generation,
                "same_current_generation": bool(same_bytes), "permissions_verified": False}
    except OSError as error:
        return {"state": "unknown", "error_type": type(error).__name__}


def observe(spec, draft, *, registry_before=None):
    """Live read-only observation. Does not acquire or bypass mutation locks."""
    group = draft["groups"]["current_shared_candidates"]
    ids = sorted({i for batch in group["batches"] for i in batch["native_ids"]})
    files_by_id = {f["native_id"]: f for f in group["native_files"]}
    paths = {}
    # state_5 may record an extended Windows spelling (\\\\?\\C:\\...).
    # Qualify the physical file, never silently normalize an unknown locator.
    with readonly_sqlite(Path(spec["source"]["path"]) / "state_5.sqlite") as connection:
        for ident, path in connection.execute("select id,rollout_path from threads"):
            if ident in files_by_id:
                frozen = files_by_id[ident]
                if not os.path.samefile(path, frozen["source"]["path"]):
                    raise IsolationPlanError("source_rollout_physical_path_changed")
                paths[path] = frozen["target_path"]
    result = {"spec_sha256": spec["plan_sha256"], "snapshot_consistency": "provisional_live_reads",
              "replay_permitted": False, "source_cleanup_permitted": False,
              "goal_satisfied": False, "physical_roots": {}, "files": {}, "source_files": {}, "databases": {}}
    for key in ("profile", "source", "target"):
        result["physical_roots"][key] = directory_identity(Path(spec[key]["path"])) == spec[key]
    for entry in spec["native_files"]:
        result["files"][entry["native_id"]] = file_state(entry["target_path"], entry["sha256"])
        frozen = files_by_id[entry["native_id"]]["source"]
        path = Path(frozen["path"])
        info = path.lstat()
        plain = not path.is_symlink() and not getattr(info, "st_file_attributes", 0) & 1024
        identity = all(getattr(info, attr) == frozen[key] for attr, key in (
            ("st_dev", "device"), ("st_ino", "inode"), ("st_size", "size"), ("st_mtime_ns", "mtime_ns")))
        result["source_files"][entry["native_id"]] = {
            "plain_file_and_frozen_identity": bool(plain and identity),
            "frozen_bytes": digest(path) == entry["sha256"]}
    for packet in spec["database_packets"]:
        name = Path(packet["packet"]).name
        if digest(packet["packet"]) != packet["sha256"]:
            raise IsolationPlanError("packet_digest_changed")
        result["databases"][name] = {
            "source": observe_database(Path(spec["source"]["path"]) / name, packet, ids, source_paths=paths),
            "target": observe_database(Path(spec["target"]["path"]) / name, packet, ids)}
    current = json.loads(Path(spec["registry_path"]).read_text(encoding="utf-8-sig"))
    result["registry_current"] = current
    result["registry"] = (registry_state(current, registry_before, spec) if registry_before is not None else
                          "before" if digest(spec["registry_path"]) == spec["registry_before_sha256"] else "unqualified")
    result["bindings"] = {change["key"]: (
        "after" if current.get("bindings", {}).get(change["key"]) == change["after"] else
        "before" if ((change["key"] in current.get("bindings", {})) == change["before_present"]
                     and current.get("bindings", {}).get(change["key"]) == change["before"]) else "diverged")
        for change in spec["binding_changes"]}
    result["locations"] = {}
    for change in spec["location_changes"]:
        path = Path(change["file"])
        value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None
        result["locations"][str(path)] = ("after" if value == change["after"] else
                                            "before" if value == change["before"] else "diverged")
    result["auth"] = auth_state(spec)
    result["empty_dependencies"] = observe_empty_dependencies(draft, ids)
    return result


def remaining_plan(observation, *, original_sha256, cumulative_mutations, completed_batches):
    """New draft, never permission to clear a journal or reuse original apply.

    Frozen after-state components are omitted from writes. Diverged data,
    including newly continued target threads, stays intact and blocks a blind
    switch. A separate qualified offline verifier must resolve original journals.
    """
    if observation["spec_sha256"] != original_sha256:
        raise IsolationPlanError("recovery_spec_mismatch")
    states = {"file:" + k: v for k, v in observation["files"].items()}
    states.update({"database:" + k: v["target"]["state"] for k, v in observation["databases"].items()})
    states.update({"location:" + k: v for k, v in observation["locations"].items()})
    states.update(registry=observation["registry"], auth=observation["auth"]["state"])
    blockers = [key for key, value in states.items() if value not in ("before", "after", "after_with_increments")]
    blockers += ["source:" + key for key, value in observation["databases"].items() if value["source"]["state"] != "after"]
    blockers += ["identity:" + key for key, value in observation["physical_roots"].items() if value is not True]
    blockers += ["dependency:" + key for key, value in observation.get("empty_dependencies", {}).items()
                 if value["state"] != "after"]
    blockers += ["source_file:" + key for key, value in observation.get("source_files", {}).items()
                 if not all(value.values())]
    closure = observation.get("closure")
    if closure and any(closure.get(key) for key in ("added", "missing", "header_failures", "selected_rollout_drift")):
        blockers.append("source_closure_changed_or_unqualified")
    if observation["auth"].get("source_generation_matches_freeze") is False:
        blockers.append("auth_source_generation_changed")
    if observation["auth"]["state"] == "after" and not observation["auth"].get("permissions_verified"):
        blockers.append("auth_permissions_unverified")
    batch_components = {"auth-file": "auth", "registry": "registry"}
    for batch in completed_batches:
        component = batch_components.get(batch, batch)
        if component.startswith("file:"):
            # Early execution versions named file batches by absolute path.
            component = observation.get("file_batch_components", {}).get(component, component)
        if states.get(component) == "before": blockers.append("previously_verified_component_reverted:" + component)
    result = {"kind": "cindy_legacy_remaining_draft", "original_spec_sha256": original_sha256,
              "writes": sorted(k for k, v in states.items() if v == "before"),
              "already_after": sorted(k for k, v in states.items() if v in ("after", "after_with_increments")),
              "blockers": sorted(blockers), "cumulative_mutations": deepcopy(cumulative_mutations),
              "completed_batches": list(completed_batches), "target_restore_or_delete": False,
              "apply_permitted": False, "source_cleanup_permitted": False,
              "requires": ["fresh_offline_freeze", "original_writer_exit_proof",
                           "original_journal_resolution", "normal_mutation_admission"]}
    result["draft_sha256"] = fingerprint(result)
    return result
