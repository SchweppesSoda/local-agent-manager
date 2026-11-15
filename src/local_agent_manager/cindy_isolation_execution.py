"""Synchronous stages of the task-specific legacy import (never source deletion).

Not a public CLI: the task controller supplies the held product/startup boundary,
source transactions and credential/atomic-file adapters. No --clients-closed
boolean or on-disk gate receipt can substitute for that live boundary.
"""
from contextlib import ExitStack, closing
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .cindy_isolation_merge import merge_selected_rows
from .cindy_isolation_plan import IsolationPlanError, directory_identity, fingerprint
from .cindy_isolation_recovery import (
    compare_packet, database_preservation, digest, file_state,
)
from .mutation_guard import MutationScope, mutation_guard
from .operation_store import plan_sha256
from . import frozen_files
from .legacy_index import _fsync_directory


def publish_new_file(source, target, expected, *, checkpoint):
    """Publish a complete sibling by no-replace hard link, never truncate target.

    An interruption leaves only a uniquely named stage file, or the complete
    target. Both paths are journalled *before* creation and retained on failure.
    A new recovery plan may inspect these exact paths; it must not recopy over
    an existing target, even when the original completion checkpoint is absent.
    """
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    directory_identity(target.parent)
    with frozen_files._parent_fence(target.parent, target.name):
        _publish_in_fenced_parent(source, target, expected, checkpoint=checkpoint)


def _publish_in_fenced_parent(source, target, expected, *, checkpoint):
    temporary = target.with_name(".cindy-import-" + uuid.uuid4().hex + ".tmp")
    checkpoint("file_stage_intent", mutation={"kind": "file_stage", "path": str(temporary),
                                             "target": str(target)})
    with source.open("rb") as src, temporary.open("xb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    if digest(temporary) != expected:
        raise IsolationPlanError("staged_native_digest_mismatch")
    checkpoint("file_publish_intent", mutation={"kind": "file_publish", "path": str(target)})
    os.link(temporary, target)  # atomic CREATE_NEW, including Windows/NTFS
    _fsync_directory(target.parent)
    checkpoint("file_published")
    if file_state(target, expected) != "after":
        raise IsolationPlanError("published_native_digest_mismatch")
    temporary.unlink()
    _fsync_directory(target.parent)
    checkpoint("file_verified", batch="file:" + str(target))


def merge_databases(spec, ids, *, checkpoint, boundary):
    """Preflight both DBs before either commit; preserve unselected full rows.

    Separate WAL databases cannot be committed atomically. Every dispatch is
    journalled before commit, so a crash between commits remains observable as
    one after/one before. The caller never retries this function for that run.
    """
    boundary.require_held()
    with ExitStack() as stack:
        prepared = []
        for packet in spec["database_packets"]:
            name = Path(packet["packet"]).name
            path = Path(spec["target"]["path"]) / name
            connection = stack.enter_context(closing(sqlite3.connect(
                path.as_uri() + "?mode=rw", uri=True, timeout=0)))
            connection.execute("attach database ? as incoming", (Path(packet["packet"]).as_uri() + "?mode=ro",))
            connection.execute("begin immediate")
            stack.callback(connection.rollback)
            tables = list(packet["rows_inserted"])
            if digest(packet["packet"]) != packet["sha256"]:
                raise IsolationPlanError("packet_digest_changed")
            status = compare_packet(connection, ids=ids, tables=tables)
            if status["state"] != "before":
                raise IsolationPlanError("target_not_empty_for_frozen_import")
            before = database_preservation(connection, ids, tables)
            prepared.append((name, connection, packet, before))
        for name, connection, packet, before in prepared:
            boundary.require_held()
            checkpoint("database_merge_intent", mutation={"kind": "database_merge", "database": name})
            paths = dict(connection.execute("select rollout_path,rollout_path from incoming.threads")) if "threads" in packet["rows_inserted"] else {}
            merge_selected_rows(connection, native_ids=ids, tables=list(packet["rows_inserted"]), rollout_rewrites=paths)
            if compare_packet(connection, ids=ids, tables=list(packet["rows_inserted"]))["state"] != "after":
                raise IsolationPlanError("merged_selected_rows_not_exact")
            if database_preservation(connection, ids, list(packet["rows_inserted"])) != before:
                raise IsolationPlanError("unselected_database_rows_changed")
            checkpoint("database_commit_intent", mutation={"kind": "database_commit", "database": name,
                        "protected_rows_sha256": before})
            connection.commit()
            checkpoint("database_committed", batch="database:" + name)


def execute_stages(spec, ids, *, boundary, checkpoint):
    """Only a fresh admitted run may enter; failure is status/verify only.

    Adapter methods own actual atomic replace and ACL checks under a live
    namespace/product fence. This layer cannot create that proof from JSON.
    """
    boundary.require_held()
    boundary.validate_source()  # source closure/full rows AFTER leases/BEGIN
    for item in spec["native_files"]:
        boundary.require_held()
        if file_state(item["target_path"], item["sha256"]) != "before":
            raise IsolationPlanError("target_file_already_present")
        publish_new_file(item["path"], item["target_path"], item["sha256"], checkpoint=checkpoint)
    merge_databases(spec, ids, checkpoint=checkpoint, boundary=boundary)
    boundary.validate_source()
    boundary.verify_import()
    checkpoint("auth_replace_intent", mutation={"kind": "independent_auth_snapshot"})
    boundary.snapshot_auth()
    checkpoint("auth_verified", batch="auth-file")
    checkpoint("registry_replace_intent", mutation={"kind": "registry_switch"})
    boundary.switch_registry()
    checkpoint("registry_verified", batch="registry")
    for change in spec["location_changes"]:
        boundary.require_held()
        checkpoint("location_replace_intent", mutation={"kind": "location_switch", "path": change["file"]})
        boundary.switch_location(change)
        checkpoint("location_verified", batch="location:" + change["file"])
    boundary.verify_switch()
    # This is a stage result, deliberately not an operation completion result.
    return {"offline_import_verified": True, "goal_satisfied": False,
            "runtime_acceptance_pending": True, "source_deleted": False}


def admitted_import(spec, ids, entries, *, boundary_factory):
    """Admit all three roots/locks before the first stage, never replay a run.

    Boundary acquisition and validation must be read-only on product stores.
    Failed journal admission is retained. A new run cannot clear the partial
    journals or bypass the manager's pre-existing unknown-outcome occupancy.
    """
    frozen = dict(spec)
    approved = frozen.pop("plan_sha256", None)
    if approved is None or fingerprint(frozen) != approved:
        raise IsolationPlanError("frozen_spec_hash_invalid")
    if sorted(ids) != sorted(f["native_id"] for f in spec["native_files"]) or len(ids) != len(set(ids)):
        raise IsolationPlanError("native_scope_mismatch")
    destinations = [f["target_path"] for f in spec["native_files"]]
    if len(destinations) != len(set(destinations)):
        raise IsolationPlanError("duplicate_native_destinations")
    for destination in destinations:
        try:
            root = Path(spec["target"]["path"])
            relative = Path(destination).relative_to(root)
            frozen_files.checked_path(root, relative.as_posix())
        except ValueError as error:
            raise IsolationPlanError("native_destination_outside_target") from error
    roots = [store.codex_home for store, _ in entries]
    if len(entries) != 3 or len(set(roots)) != 3:
        raise IsolationPlanError("exact_three_root_journals_required")
    if {str(root) for root in roots} != {spec[k]["path"] for k in ("source", "target", "profile")}:
        raise IsolationPlanError("journal_physical_scope_mismatch")
    for _, plan in entries:
        if plan.get("migration_spec") != spec or plan_sha256(plan) != plan.get("plan_sha256"):
            raise IsolationPlanError("journal_spec_binding_mismatch")
    states, mutations, batches = [], [], []
    with mutation_guard(tuple(MutationScope(root, None) for root in roots)):
        for key in ("profile", "source", "target"):
            if directory_identity(Path(spec[key]["path"])) != spec[key]:
                raise IsolationPlanError("physical_root_changed:" + key)
        if any(store.directory.exists() for store, _ in entries):
            raise IsolationPlanError("prior_journal_status_verify_only")
        with ExitStack() as stack:
            boundary = stack.enter_context(boundary_factory())
            boundary.require_held()
            boundary.validate_source()
            for store, plan in entries:
                store.accept_plan(plan)
                state = {"schema_version": "larj.agent-state.v1", "operation_id": store.operation_id,
                         "plan_sha256": plan["plan_sha256"], "phase": "preflight",
                         "goal_status": "blocked", "goal_satisfied": False, "modified": False,
                         "mutation_started": False, "cumulative_mutations": [], "completed_batches": []}
                store.write_state(state); states.append(state)
            for store, _ in entries:
                stack.enter_context(store.mutation_lock())

            def checkpoint(event, mutation=None, batch=None):
                if mutation is not None: mutations.append(mutation)
                if batch is not None and batch not in batches: batches.append(batch)
                # Each event contains cumulative evidence, even if a process
                # crashes before append_event can replace its state file.
                for (store, _), state in zip(entries, states):
                    state.update(phase="executing", goal_status="unknown", mutation_started=True,
                                 modified=bool(batches), cumulative_mutations=list(mutations),
                                 completed_batches=list(batches))
                    store.append_event({"event": event, "goal_status": "unknown", "mutation": mutation,
                                        "batch": batch, "cumulative_mutations": list(mutations),
                                        "completed_batches": list(batches)}, state_updates=state)
            try:
                checkpoint("mutation_started")
                result = execute_stages(spec, ids, boundary=boundary, checkpoint=checkpoint)
                checkpoint("offline_stages_verified_runtime_acceptance_pending")
                return {**result, "goal_status": "unknown", "cumulative_mutations": mutations,
                        "completed_batches": batches, "journal_resolution_required": True}
            except BaseException:
                # Do not let publication failure conceal the original failure,
                # or remove evidence from replicas already written.
                for (store, _), state in zip(entries, states):
                    try:
                        state.update(phase="recovery_required", goal_status="unknown")
                        store.append_event({"event": "recovery_required", "goal_status": "unknown",
                                            "cumulative_mutations": list(mutations),
                                            "completed_batches": list(batches)}, state_updates=state)
                    except Exception:
                        pass
                raise


def journal_projection(entries):
    """Read-only status resilient to partial cross-root journal publication.

    Union cumulative evidence across replicas/events. A missing, unreadable or
    partially admitted replica is unknown, even if another has a terminal state.
    Never compact receipts, acquire/alter permanent locks or mark completion.
    """
    roots, mutations, batches = [], [], []
    seen = set()
    any_journal = False
    for store, plan in entries:
        row = {"root": str(store.codex_home), "state": "not_started"}
        try:
            if store.directory.exists():
                any_journal = True
                if store.read_plan() != plan:
                    raise IsolationPlanError("journal_plan_mismatch")
                state = store.read_state()
                row.update(state="observed_unsettled", journal_state=state, apply_lock=store.lock_exists())
                if not state:
                    row["state"] = "partial_admission"
                def retain(item):
                    for mutation in item.get("cumulative_mutations", []):
                        key = repr(sorted(mutation.items()))
                        if key not in seen:
                            seen.add(key); mutations.append(mutation)
                    for batch in item.get("completed_batches", []):
                        if batch not in batches:
                            batches.append(batch)
                retain(state or {})
                events = store.read_events()
                for event in events:
                    retain(event)
                    mutation = event.get("mutation")
                    if mutation is not None and repr(sorted(mutation.items())) not in seen:
                        seen.add(repr(sorted(mutation.items()))); mutations.append(mutation)
                    if event.get("batch") and event["batch"] not in batches:
                        batches.append(event["batch"])
        except (OSError, ValueError, RuntimeError) as error:
            row.update(state="unknown", error_type=type(error).__name__)
        roots.append(row)
    return {"roots": roots, "goal_status": "unknown" if any_journal or any(r["state"] == "unknown" for r in roots) else "not_started",
            "goal_satisfied": False, "replay_permitted": False,
            "cumulative_mutations": mutations, "completed_batches": batches}
