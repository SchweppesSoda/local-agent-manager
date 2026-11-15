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
import re
import stat

from .cindy_isolation_merge import merge_selected_rows
from .cindy_isolation_plan import IsolationPlanError, directory_identity, fingerprint
from .cindy_isolation_recovery import (
    compare_packet, database_preservation, digest, file_state,
)
from .mutation_guard import MutationScope, mutation_guard
from .operation_store import plan_sha256
from . import frozen_files
from .legacy_index import _fsync_directory


def config_stage_path(spec, destination):
    path = Path(destination)
    allowed = {spec['registry_path'], *(c['file'] for c in spec['location_changes'])}
    if str(path) not in allowed: raise IsolationPlanError('unapproved_config_stage_destination')
    return path.with_name('.cindy-switch-' + spec['plan_sha256'][:16] + '-' + fingerprint(str(path))[:16] + '.tmp')


def frozen_staging_candidates(spec, mutations):
    """Only names durably owned by this import; never the original packets."""
    candidates = []
    for mutation in mutations:
        if mutation.get("kind") == "file_stage":
            path, target = Path(mutation["path"]), Path(mutation["target"])
            if (str(target) not in {f["target_path"] for f in spec["native_files"]}
                    or path.parent != target.parent or not re.fullmatch(r"\.cindy-import-[0-9a-f]{32}\.tmp", path.name)):
                raise IsolationPlanError("recovery_stage_outside_frozen_scope")
        elif mutation.get("kind") == "independent_auth_snapshot":
            path = Path(mutation.get("stage_path") or Path(spec["auth"]["target"]["path"]).with_name(
                ".auth-isolation-" + spec["plan_sha256"][:16] + ".tmp"))
            if path.parent != Path(spec["auth"]["target"]["path"]).parent or not re.fullmatch(r"\.auth-isolation-[0-9a-f]{16}\.tmp", path.name):
                raise IsolationPlanError("recovery_auth_stage_outside_scope")
        elif mutation.get('kind') in {'registry_switch', 'location_switch'}:
            destination = spec['registry_path'] if mutation['kind'] == 'registry_switch' else mutation['path']
            path = Path(mutation['stage_path'])
            # The original spec hash can differ after several qualified
            # remaining operations. Destination and the exact journal-owned
            # temporary name remain frozen, with no arbitrary path channel.
            if (str(destination) not in {spec['registry_path'], *(c['file'] for c in spec['location_changes'])}
                    or path.parent != Path(destination).parent
                    or not re.fullmatch(r'\.cindy-switch-[0-9a-f]{16}-' + fingerprint(str(Path(destination)))[:16] + r'\.tmp', path.name)):
                raise IsolationPlanError('recovery_config_stage_outside_scope')
        else: continue
        if not path.exists(): continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 1024:
            raise IsolationPlanError("recovery_stage_not_regular")
        row = {"path": str(path), "device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
               "mtime_ns": info.st_mtime_ns, "sha256": digest(path)}
        if row not in candidates: candidates.append(row)
    return candidates


def cleanup_frozen_stages(spec, checkpoint):
    """Delete exact owned temporary entries through their Windows handles."""
    for before in spec.get("stage_cleanup", []):
        path = Path(before["path"])
        root = Path(spec['target']['path']) if path.is_relative_to(Path(spec['target']['path'])) else Path(spec['profile']['path'])
        relative = path.relative_to(root).as_posix()
        frozen_files.checked_path(root, relative)
        with frozen_files._parent_fence(root, relative):
            info = path.lstat()
            if any(getattr(info, attr) != before[key] for attr, key in (("st_dev", "device"), ("st_ino", "inode"), ("st_size", "size"), ("st_mtime_ns", "mtime_ns"))) or digest(path) != before["sha256"]:
                raise IsolationPlanError("recovery_stage_changed")
            checkpoint("owned_stage_cleanup_intent", mutation={"kind": "owned_stage_cleanup", "path": str(path)})
            if os.name == "nt":
                from .windows_held_files import exclusive_fd
                import ctypes, msvcrt
                fd = exclusive_fd(path, delete=True)
                try:
                    if frozen_files._identity(os.fstat(fd)) != [before["device"], before["inode"]]: raise IsolationPlanError("recovery_stage_replaced")
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.SetFileInformationByHandle.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)
                    kernel.SetFileInformationByHandle.restype = ctypes.c_int
                    value = ctypes.c_int(1)
                    if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 4, ctypes.byref(value), ctypes.sizeof(value)):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally: os.close(fd)
            else: path.unlink()
            _fsync_directory(path.parent)
        checkpoint("owned_stage_cleanup_verified", batch="stage:" + str(path))


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
            if not component_pending(spec, "database:" + name):
                continue
            path = Path(spec["target"]["path"]) / name
            stack.enter_context(frozen_files._parent_fence(path.parent, path.name))
            frozen_files._plain(path.lstat())
            if os.name == 'nt':
                pinned = frozen_files._windows_fd(path)
                stack.callback(os.close, pinned)
                expected = spec.get('controller', {}).get('target_database_identities', {}).get(name)
                if expected and frozen_files._identity(os.fstat(pinned)) != [expected['device'], expected['inode']]:
                    raise IsolationPlanError('target_database_physical_identity_changed')
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
        if not component_pending(spec, "file:" + item["native_id"]):
            continue
        boundary.require_held()
        if file_state(item["target_path"], item["sha256"]) != "before":
            raise IsolationPlanError("target_file_already_present")
        publish_new_file(item["path"], item["target_path"], item["sha256"], checkpoint=checkpoint)
    merge_databases(spec, ids, checkpoint=checkpoint, boundary=boundary)
    cleanup_frozen_stages(spec, checkpoint)
    boundary.validate_source()
    boundary.verify_import()
    if component_pending(spec, "auth"):
        checkpoint("auth_replace_intent", mutation={"kind": "independent_auth_snapshot", "stage_path": str(
            Path(spec["auth"]["target"]["path"]).with_name(".auth-isolation-" + spec["plan_sha256"][:16] + ".tmp"))})
        boundary.snapshot_auth()
        checkpoint("auth_verified", batch="auth-file")
    if component_pending(spec, "registry"):
        checkpoint("registry_replace_intent", mutation={"kind": "registry_switch", 'stage_path': str(config_stage_path(spec, spec['registry_path']))})
        boundary.switch_registry()
        checkpoint("registry_verified", batch="registry")
    for change in spec["location_changes"]:
        if not component_pending(spec, "location:" + change["file"]):
            continue
        boundary.require_held()
        checkpoint("location_replace_intent", mutation={"kind": "location_switch", "path": change["file"],
                   'stage_path': str(config_stage_path(spec, change['file']))})
        boundary.switch_location(change)
        checkpoint("location_verified", batch="location:" + change["file"])
    boundary.verify_switch()
    # This is a stage result, deliberately not an operation completion result.
    return {"offline_import_verified": True, "goal_satisfied": False,
            "runtime_acceptance_pending": True, "source_deleted": False}


def component_pending(spec, key):
    return "remaining_components" not in spec or key in spec["remaining_components"]


def finish_import_journals(entries, states, *, mutations, batches, event="operation_finished"):
    """Known offline phase completion; overall source cleanup stays pending."""
    for (store, plan), state in zip(entries, states):
        state.update(phase="finished", goal_status="completed_with_residuals", goal_satisfied=False,
                     cumulative_mutations=list(mutations), completed_batches=list(batches))
        store.append_event({"event": event, "goal_status": "completed_with_residuals",
                            "cumulative_mutations": list(mutations), "completed_batches": list(batches)}, state_updates=state)
        store.write_result({"schema_version": "larj.agent-result.v1", "document_type": "operation_result",
            "command": "agent", "mode": "agent", "operation_id": store.operation_id,
            "plan_sha256": plan["plan_sha256"], "goal_status": "completed_with_residuals", "goal_satisfied": False,
            "modified": bool(state["modified"]), "mutation_started": bool(state["mutation_started"]),
            "blockers": [{"blocker_code": "runtime_acceptance_and_native_source_cleanup_pending"}],
            "counts": {"completed_batches": len(batches)}, "cumulative_mutations": list(mutations),
            "completed_batches": list(batches)})


def admitted_import(spec, ids, entries, *, boundary_factory):
    """Admit all three roots/locks before the first stage, never replay a run.

    All three journals/locks precede boundary acquisition. The held native
    auth protocol can create only its two empty, persistent coordination files;
    no credential or history write precedes the durable boundary intent.
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
    states = []
    mutations = list(spec.get("inherited_cumulative_mutations", []))
    batches = list(spec.get("inherited_completed_batches", []))
    with mutation_guard(tuple(MutationScope(root, None) for root in roots)):
        for key in ("profile", "source", "target"):
            if directory_identity(Path(spec[key]["path"])) != spec[key]:
                raise IsolationPlanError("physical_root_changed:" + key)
        if any(store.directory.exists() for store, _ in entries):
            raise IsolationPlanError("prior_journal_status_verify_only")
        with ExitStack() as stack:
            for store, plan in entries:
                store.accept_plan(plan)
                state = {"schema_version": "larj.agent-state.v1", "operation_id": store.operation_id,
                         "plan_sha256": plan["plan_sha256"], "phase": "preflight",
                         "goal_status": "blocked", "goal_satisfied": False, "modified": False,
                         "mutation_started": False, "cumulative_mutations": [], "completed_batches": []}
                if os.name == "nt":
                    from .cindy_isolation_windows import process_evidence
                    actual_writer = process_evidence()
                    bound_writer = plan.get("writer_evidence", actual_writer)
                    if any(bound_writer.get(k) != actual_writer.get(k) for k in ("pid", "started_unix", "executable", "machine", "windows_session", "model")):
                        raise IsolationPlanError("accepted_import_writer_identity_changed")
                    state["writer"] = bound_writer
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
                checkpoint('native_auth_boundary_intent', mutation={'kind': 'native_auth_coordination',
                    'paths': [str(Path(spec[key]['path']) / 'auth.lock') for key in ('source', 'target')]})
                boundary = stack.enter_context(boundary_factory())
                boundary.require_held(); boundary.validate_source()
                result = execute_stages(spec, ids, boundary=boundary, checkpoint=checkpoint)
                checkpoint("offline_stages_verified_runtime_acceptance_pending")
                finish_import_journals(entries, states, mutations=mutations, batches=batches)
                return {**result, "goal_status": "completed_with_residuals", "cumulative_mutations": mutations,
                        "completed_batches": batches, "journal_resolution_required": False}
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
    offline_known = all((r.get('journal_state') or {}).get('goal_status') == 'completed_with_residuals'
                        and not r.get('apply_lock') and r['state'] == 'observed_unsettled' for r in roots)
    return {"roots": roots, "goal_status": 'completed_with_residuals' if offline_known else
            "unknown" if any_journal or any(r["state"] == "unknown" for r in roots) else "not_started",
            'offline_import_journals_settled': offline_known,
            "goal_satisfied": False, "replay_permitted": False,
            "cumulative_mutations": mutations, "completed_batches": batches}
