"""Qualified verification/remaining-plan execution for synchronous Cindy imports.

This owns only its exact import journals. It is not joint-CLI recovery, does
not reclaim other apply locks, and never replays the original import.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path

from . import frozen_files as ff
from .cindy_isolation_execution import (admitted_import, finish_import_journals, journal_projection)
from .cindy_isolation_execution import frozen_staging_candidates
from .cindy_isolation_plan import IsolationPlanError, fingerprint
from .mutation_guard import MutationScope, mutation_roots, _check_siblings
from .operation_store import OperationStore, plan_sha256, write_new_json, utc_now
from .windows_held_files import HeldFiles


def import_entries(spec, *, writer=None):
    operation = "cindy-legacy-import-" + spec["plan_sha256"][:20]
    entries = []
    for key in ("profile", "source", "target"):
        root = Path(spec[key]["path"])
        plan = {"schema_version": "larj.agent-plan.v1", "operation_id": operation,
                "target": {"codex_home": str(root)}, "migration_spec": spec,
                "authorization": {"root_actions": [{"kind": "cindy_legacy_home_import",
                    "impact": {"approved_spec_sha256": spec["plan_sha256"]}}]}}
        if writer is not None: plan["writer_evidence"] = writer
        plan["plan_sha256"] = plan_sha256(plan)
        entries.append((OperationStore(root, operation), plan))
    return entries


def existing_entries(spec):
    """Restore immutable runtime-bound journal plans; never manufacture writer."""
    result = []
    for store, proposed in import_entries(spec):
        if store.directory.exists():
            frozen = store.read_plan()
            if frozen.get("migration_spec") != spec: raise IsolationPlanError("recovery_spec_not_original")
            result.append((store, frozen))
        else:
            result.append((store, proposed))
    return result


def latest_import_spec(bundle, original):
    """Follow only scope-preserving remaining operations with actual journals.

    A second crash belongs to its remaining operation, not to the already
    settled original. Never bypass that sibling by starting from the old hash.
    Files alone are not admission evidence and ambiguous branches block.
    """
    known = {original['plan_sha256']: original}
    pending = []
    for path in sorted((Path(bundle) / 'runs').glob('run-*/remaining-spec-*.json')):
        ff._plain(path.lstat())
        value = json.loads(path.read_text(encoding='utf8'))
        check = dict(value); approved = check.pop('plan_sha256', None)
        if not approved or fingerprint(check) != approved:
            raise IsolationPlanError('remaining_spec_hash_changed')
        pending.append(value)
    children = {}
    while pending:
        progressed = False
        for value in list(pending):
            parent = known.get(value.get('resume_of'))
            if parent is None: continue
            fixed, expected = deepcopy(value), deepcopy(parent)
            for key in ('plan_sha256', 'resume_of', 'recovery_audit_sha256', 'remaining_components',
                        'allow_target_progress', 'inherited_cumulative_mutations',
                        'inherited_completed_batches', 'stage_cleanup'):
                fixed.pop(key, None); expected.pop(key, None)
            # Registry refresh can only retain extra unrelated bindings, as
            # independently checked by the held recovery observer.
            from .cindy_isolation_recovery import registry_state
            if registry_state(value['controller']['registry_before'], parent['controller']['registry_before'], parent) != 'before':
                raise IsolationPlanError('remaining_registry_scope_changed')
            fixed['controller']['registry_before'] = expected['controller']['registry_before']
            fixed['registry_before_sha256'] = expected['registry_before_sha256']
            if fixed != expected:
                raise IsolationPlanError('remaining_operation_expanded_frozen_actions')
            if 'remaining_components' in parent and not set(value['remaining_components']) <= set(parent['remaining_components']):
                raise IsolationPlanError('remaining_operation_regressed_scope')
            proof = journal_projection(existing_entries(parent))
            if (value['inherited_cumulative_mutations'] != proof['cumulative_mutations'] or
                    value['inherited_completed_batches'] != proof['completed_batches']):
                raise IsolationPlanError('remaining_operation_not_bound_to_parent_journal')
            owned = {m.get('path') for m in proof['cumulative_mutations'] if m.get('kind') == 'file_stage'}
            owned.update(m.get('stage_path') for m in proof['cumulative_mutations'] if m.get('kind') == 'independent_auth_snapshot')
            owned.update(m.get('stage_path') for m in proof['cumulative_mutations'] if m.get('kind') in {'registry_switch', 'location_switch'})
            if any(row['path'] not in owned for row in value.get('stage_cleanup', [])):
                raise IsolationPlanError('remaining_stage_not_owned_by_parent')
            if any(store.directory.exists() for store, _ in import_entries(value)):
                if value['resume_of'] in children and children[value['resume_of']] != value['plan_sha256']:
                    raise IsolationPlanError('ambiguous_remaining_operation_branches')
                children[value['resume_of']] = value['plan_sha256']
                known[value['plan_sha256']] = value
            pending.remove(value); progressed = True
        if not progressed:
            raise IsolationPlanError('unbound_remaining_operation_chain')
    current = original
    while current['plan_sha256'] in children:
        current = known[children[current['plan_sha256']]]
    return current


def original_writer_exited(writer):
    from .cindy_isolation_windows import process_evidence
    from .joint_cli_recovery import require_process_absent
    current = process_evidence()
    if (not writer or writer.get("model") != "synchronous_in_process_no_delegated_writer"
            or writer.get("machine") != current["machine"]
            or writer.get("windows_session") != current["windows_session"]
            or writer.get('user_sid')!=current.get('user_sid')):
        raise IsolationPlanError("original_import_writer_scope_unproven")
    # The existing kernel helper rejects live writers, PID reuse and access
    # denial. Its only use here is the read-only process-exit primitive.
    return require_process_absent(writer["pid"], writer["admitted_at"])


def resolve_import(spec, draft, *, boundary_factory, evidence_directory,
                   prove_exit=original_writer_exited, lock_files_factory=HeldFiles):
    """Settle only independently known before/after stages under held boundary.

    A diverged target is retained and refuses settlement. This never rewrites a
    product store. It publishes a new audit and retires only the exact original
    apply.lock via its leased Windows handle after full proof/publication.
    """
    entries = existing_entries(spec)
    evidence_directory = Path(evidence_directory)
    with mutation_roots(store.codex_home for store, _ in entries):
        states, writers, proofs = [], [], []
        for store, plan in entries:
            _check_siblings(MutationScope(store.codex_home, None), store)
            if not store.directory.exists():
                states.append(None); continue
            state = store.read_state()
            events = store.read_events()
            writer = plan.get("writer_evidence")
            reported = (state or {}).get("writer")
            if not writer or reported and any(reported.get(k) != writer.get(k) for k in ("pid", "started_unix", "executable", "machine", "windows_session", "model")):
                raise IsolationPlanError("original_import_writer_binding_missing_or_changed")
            if writers and any(writer.get(k) != writers[0].get(k) for k in ("pid", "started_unix", "executable", "machine", "windows_session", "model")):
                raise IsolationPlanError("import_replicas_do_not_have_one_synchronous_writer")
            if writer not in writers:
                proofs.append(prove_exit(writer)); writers.append(writer)
            states.append(state)
        if not writers: raise IsolationPlanError("no_original_import_journals")
        with ExitStack() as stack:
            held = stack.enter_context(lock_files_factory())
            original_locks = []
            for store, _ in entries:
                if store.directory.exists() and store.lock_exists():
                    evidence, raw = ff.read_file(store.directory, "apply.lock", content=True)
                    lock = json.loads(raw)
                    if lock.get("pid") not in {w["pid"] for w in writers}: raise IsolationPlanError("import_lock_writer_mismatch")
                    held.acquire(store.directory, evidence, delete=True)
                    original_locks.append((store, evidence))
            boundary = stack.enter_context(boundary_factory())
            boundary.require_held(); boundary.validate_source()
            observation = boundary.observation()
            from .cindy_isolation_recovery import remaining_plan
            journal = journal_projection(entries)
            remaining = remaining_plan(observation, original_sha256=spec["plan_sha256"],
                cumulative_mutations=journal["cumulative_mutations"], completed_batches=journal["completed_batches"])
            if remaining["blockers"]: raise IsolationPlanError("import_recovery_diverged_keep_source:" + ",".join(remaining["blockers"]))
            audit = {"schema_version": "larj.cindy-import-recovery.v1", "original_spec_sha256": spec["plan_sha256"],
                     "writer_exit": proofs, "physical_scope": {k: spec[k] for k in ("profile", "source", "target")},
                     "original_locks": [e for _, e in original_locks], "observation": observation,
                     "cumulative_mutations": journal["cumulative_mutations"], "completed_batches": journal["completed_batches"],
                     "remaining": remaining, "product_mutation_dispatched": False}
            audit["audit_sha256"] = fingerprint(audit)
            audit_path = evidence_directory / ("recovery-" + audit["audit_sha256"][:20] + ".json")
            if not audit_path.exists(): write_new_json(audit_path, audit)
            else:
                if json.loads(audit_path.read_text(encoding="utf8")) != audit: raise IsolationPlanError("recovery_audit_changed")
            # state may lag durable events, or admission may have stopped after
            # accepting a plan. Only this qualified verifier advances sequence.
            admitted_entries, resolved_states = [], []
            for (store, plan), state in zip(entries, states):
                if not store.directory.exists(): continue
                state = dict(state or {"schema_version": "larj.agent-state.v1", "operation_id": store.operation_id,
                    "plan_sha256": plan["plan_sha256"], "phase": "preflight", "goal_status": "blocked",
                    "goal_satisfied": False, "modified": False, "mutation_started": False})
                state["next_event_sequence"] = len(store.read_events()) + 1
                state["cumulative_mutations"] = journal["cumulative_mutations"]
                state["completed_batches"] = journal["completed_batches"]
                store.write_state(state)
                admitted_entries.append((store, plan)); resolved_states.append(state)
            finish_import_journals(admitted_entries, resolved_states, mutations=journal["cumulative_mutations"],
                batches=journal["completed_batches"], event="verification_finished")
            for store, evidence in original_locks:
                held.dispose(store.directory, "apply.lock", before=evidence)
            return audit


def new_remaining_spec(spec, audit):
    remaining = audit["remaining"]
    if remaining["blockers"]: raise IsolationPlanError("remaining_import_has_blockers")
    value = deepcopy(spec); value.pop("plan_sha256")
    value.update(resume_of=spec["plan_sha256"], recovery_audit_sha256=audit["audit_sha256"],
                 remaining_components=remaining["writes"],
                 allow_target_progress=True,
                 inherited_cumulative_mutations=remaining["cumulative_mutations"],
                 inherited_completed_batches=remaining["completed_batches"])
    value["stage_cleanup"] = frozen_staging_candidates(spec, remaining["cumulative_mutations"])
    if "registry" in remaining["writes"]:
        value["controller"]["registry_before"] = audit["observation"]["registry_current"]
        registry = Path(value["registry_path"])
        from .cindy_isolation_recovery import digest
        value["registry_before_sha256"] = digest(registry)
    value["plan_sha256"] = fingerprint(value)
    return value


def execute_remaining(spec, draft, audit, *, boundary_type, evidence_directory, writer=None):
    value = new_remaining_spec(spec, audit)
    path = Path(evidence_directory) / ("remaining-spec-" + value["plan_sha256"][:20] + ".json")
    if path.exists():
        if json.loads(path.read_text(encoding="utf8")) != value: raise IsolationPlanError("remaining_spec_changed")
    else: write_new_json(path, value)
    # A distinct spec/operation hash owns every remaining mutation. Normal
    # sibling admission is now possible because original journals were settled.
    return value, admitted_import(value, [f["native_id"] for f in value["native_files"]],
        import_entries(value, writer=writer), boundary_factory=lambda: boundary_type(value, draft))
