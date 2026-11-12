"""Settle the specific completed-IDB / unstarted-CLI T3 interruption.

This is verification, not replay. The original error and journals are retained;
only a fresh, independently qualified plan can delete the remaining records.
No other partial native writer, desktop installation, or error is admitted.
"""
from contextlib import ExitStack
from pathlib import Path

from . import frozen_files as ff, t3code_cleanup as cleanup, antigravity_cli_joint as joint
from .operation_store import OperationStore, strict_json_load, plan_sha256, write_new_json, utc_now
from .record_identity import canonical_path

ERROR = 'antigravity_cli_profile_layout_changed'


def fail(code):
    raise RuntimeError('t3_partial_' + code)


def inspect(document, coordinator, *, approved_hash=None):
    if (document.get('scope', {}).get('client') != 't3code'
            or document.get('plan_sha256') != plan_sha256(document)
            or approved_hash is not None and approved_hash != document['plan_sha256']):
        fail('parent_binding_invalid')
    evidence = cleanup.evidence_from_document(document)
    if evidence is None or not joint.targets(evidence):
        fail('unsupported_scope')
    # The reopened frontend may rebuild IndexedDB from its still-present
    # service rows. Qualify the old immutable scope and completed checkpoint;
    # a fresh full plan must independently qualify today's desktop bytes.
    cleanup.validate_scope(evidence)
    roots = {s['storage_id']: Path(s['path']) for s in document['storages']}
    frontend = None
    completed = []
    for batch in document['child_batches']:
        store = OperationStore(roots[batch['storage_id']], batch['child_operation_id'])
        expected = coordinator._child_document(document, batch)
        if batch['mutation_family'] != cleanup.KIND:
            result = store.read_result(maintain=False)
            if result is None:
                result = store.read_state()
                events = store.read_events()
                if (store.read_plan() != expected or not events
                        or events[-1].get('event') != 'batch_finished'
                        or events[-1].get('goal_status') != 'complete'
                        or {e.get('action_id') for e in events if e['event'] == 'action_verified'}
                           != set(batch['action_ids'])):
                    fail('native_child_completion_unproven')
            if (store.lock_exists() or not result or result.get('goal_status') != 'complete'
                    or result.get('plan_sha256') != expected['plan_sha256']):
                fail('native_child_not_complete')
            completed.append(store.operation_id)
            continue
        if frontend is not None or store.read_plan() != expected:
            fail('frontend_binding_invalid')
        frontend = store
    if frontend is None:
        fail('frontend_missing')
    state, events = frontend.read_state(), frontend.read_events()
    if not state or state.get('next_event_sequence') != len(events)+1 or not events:
        fail('checkpoint_invalid')
    audits = list(frontend.directory.glob('partial-recovery-*.json'))
    settled = state.get('goal_status') == 'completed_with_residuals'
    if settled:
        if len(audits) != 1:
            fail('audit_missing')
        audit = strict_json_load(audits[0])
        if (audit.get('parent_plan_sha256') != document['plan_sha256']
                or audit.get('child_plan_sha256') != frontend.read_plan()['plan_sha256']
                or audit.get('schema_version') != 'lam.t3-partial-recovery.v1'
                or events[-1].get('event') != 'verification_finished'
                or events[-1].get('recovery_protocol') != 't3_partial'
                or events[-1].get('audit_path') != str(audits[0])):
            fail('audit_binding_invalid')
    else:
        if (state.get('goal_status') != 'unknown' or state.get('phase') != 'recovery_required'
                or not state.get('mutation_started') or events[-1].get('event') != 'batch_finished'
                or events[-1].get('error', {}).get('message') != ERROR
                or any(e['event'] not in {'plan_accepted', 'action_guard_started', 'mutation_started',
                                         'batch_finished'} for e in events)):
            fail('checkpoint_unsupported')
    # The CLI writer is ordered before Local Storage, terminal files and the
    # service database. Its durable unstarted reservation proves those later
    # stages were not dispatched by this failed attempt.
    for target in joint.targets(evidence):
        store, expected = joint.reservation(document, evidence, target)
        if store.read_plan() != expected or store.lock_exists():
            fail('native_reservation_changed')
        native_state, native_events = store.read_state(), store.read_events()
        if (not native_state or native_state.get('mutation_started') or native_state.get('modified')
                or native_state.get('goal_status') != 'blocked'
                or native_state.get('next_event_sequence') != len(native_events)+1
                or [e['event'] for e in native_events] != ['plan_accepted', 'operation_finished']
                or (store.directory/'native-recovery').exists()):
            fail('native_writer_started_or_unknown')
    desktops, writers = [], set()
    from . import t3code_desktop_cleanup as idb
    for desktop in evidence['desktop']:
        plan = desktop['localstorage'].get('indexeddb')
        if plan is None:
            continue
        profile = idb.validate_plan(plan)
        directory = idb.journal_root(profile)/plan['operation_id']
        if strict_json_load(directory/'plan.json') != plan:
            fail('desktop_binding_changed')
        checkpoint = strict_json_load(directory/'state.json')
        if (checkpoint.get('phase') != 'complete' or checkpoint.get('plan_sha256') != plan['plan_sha256']
                or checkpoint.get('attempts') != 1 or (directory/'after').exists()
                or checkpoint.get('result', {}).get('logical_after_sha256') != plan['evidence']['native']['expectedAfterSha256']):
            fail('desktop_install_incomplete')
        desktops.append(plan['operation_id'])
        if type(checkpoint.get('pid')) is not int or checkpoint['pid'] <= 0:
            fail('desktop_writer_unproven')
        writers.add(checkpoint['pid'])
    if not desktops:
        fail('desktop_checkpoint_missing')
    failures = [e for e in events if e.get('event') == 'batch_finished'
                and e.get('error', {}).get('message') == ERROR]
    if len(failures) != 1:
        fail('failure_event_unproven')
    writer_before = failures[0]['recorded_at']
    from .joint_cli_recovery import require_process_absent
    for pid in writers:
        require_process_absent(pid, writer_before)
    if frontend.lock_exists():
        lock = strict_json_load(frontend.lock_path)
        if set(lock) != {'pid', 'created_at'}:
            fail('lock_untrusted')
        require_process_absent(lock['pid'], lock['created_at'])
    return dict(evidence=evidence, store=frontend, completed_children=completed,
                desktop_operations=desktops, writer_pids=sorted(writers), writer_before=writer_before, settled=settled)


def qualify_remaining(original, current):
    """A new full plan qualifies today's bytes; old authority only bounds scope."""
    if current.get('plan_sha256') != plan_sha256(current) or current.get('goal_status') != 'ready':
        fail('remaining_plan_invalid')
    old, fresh = cleanup.evidence_from_document(original), cleanup.evidence_from_document(current)
    cleanup._validate_current(fresh)
    if (canonical_path(old['root']) != canonical_path(fresh['root'])
            or old['selected_thread_ids'] != fresh['selected_thread_ids']
            or current['scope'] != original['scope']):
        fail('remaining_scope_changed')
    from .t3code_store import read_database
    present = set(read_database(Path(old['root']))['threads'])
    if not {b['thread_id'] for b in old['binding_manifest']['native_stores']} <= present:
        fail('original_thread_missing')
    old_targets = {(t['engine'], t['root']): t for t in old['native_targets']}
    if {(t['engine'], t['root']) for t in fresh['native_targets']} != set(old_targets):
        fail('native_roots_changed')
    for target in fresh['native_targets']:
        previous = old_targets[target['engine'], target['root']]
        if not set(target['ids']) <= set(previous['ids']):
            fail('native_scope_expanded')
        if target['engine'] != 'antigravity':
            if target['action_ids'] or cleanup.native_remaining(previous):
                fail('completed_native_target_reappeared')
        else:
            if target['ids'] != previous['ids']:
                fail('unstarted_native_scope_changed')
            before, now = previous['native_files'], target['native_files']
            # Reopened clients may update unrelated summary metadata. The new
            # full plan binds every current row and preserves its unselected
            # rows and current restore maps. Selected session bytes and parent
            # relationships cannot drift; shared rewrite paths cannot expand.
            if (now.get('summary_schema_sha256') != before.get('summary_schema_sha256')
                    or any(now['parents'].get(sid) != before['parents'].get(sid) for sid in target['ids'])
                    or now['changes'] != before['changes']
                    or {c['before']['path'] for c in now['rewrites']}
                       != {c['before']['path'] for c in before['rewrites']}
                    or (now['cache'] or {}).get('before', {}).get('path')
                       != (before['cache'] or {}).get('before', {}).get('path')):
                fail('unstarted_native_bytes_changed')
    for change in old['files']['changes']:
        if ff.freeze_remove(Path(old['root']), change['path']) != change:
            fail('service_files_changed')
    for proof in old['files']['preserved']:
        if ff.read_file(Path(old['root']), proof['path'])[0] != proof:
            fail('preserved_file_changed')
    return fresh


def settle(document, current, coordinator, approved_hash):
    from .mutation_guard import mutation_roots
    from .joint_cli_recovery import require_process_absent
    from .windows_held_files import HeldFiles
    from .office_runtime import processes
    from . import t3code_lifecycle
    if approved_hash != document.get('plan_sha256'):
        fail('approved_parent_required')
    info = inspect(document, coordinator, approved_hash=approved_hash)
    store = info['store']
    fresh = qualify_remaining(document, current)
    roots = [Path(t['root']) for t in fresh['native_targets']] + [Path(fresh['root'])]
    with mutation_roots(roots), ExitStack() as stack:
        stack.enter_context(t3code_lifecycle.hold(fresh))
        held = stack.enter_context(HeldFiles())
        from . import antigravity_cli_guards as guards, antigravity_cli_cleanup as native
        joint.recheck(fresh, 't3code')
        guards.require_closed(fresh['cli_guards'])
        guards.acquire(fresh['cli_guards'], held)
        stack.enter_context(guards.leased_reads(fresh['cli_guards'], held))
        for target in joint.targets(fresh):
            native.require_closed(target['native_files'])
            runtime = target['native_files']['runtime']
            runtime_path = canonical_path(Path(runtime['root'])/runtime['file']['path'])
            if runtime_path not in {canonical_path(Path(b['root'])/b['file']['path'])
                                    for b in fresh['lifecycle']['binaries']}:
                held.acquire(Path(runtime['root']), runtime['file'])
            proof = target['cli_selection']['settings']
            if (str(Path(fresh['root'])), proof['path']) not in held.handles:
                held.acquire(Path(fresh['root']), proof)
        info = inspect(document, coordinator, approved_hash=approved_hash)
        original_lock = store.lock_exists()
        lock, proof = None, None
        writers = set(info['writer_pids'])
        exited = []
        if original_lock:
            proof, _ = ff.read_file(store.directory, 'apply.lock')
            held.acquire(store.directory, proof, delete=True)
            import json
            _, raw = held.read(store.directory, 'apply.lock', content=True)
            lock = json.loads(raw)
            if set(lock) != {'pid', 'created_at'}:
                fail('lock_untrusted')
            writers.add(lock['pid'])
            exited.append(require_process_absent(lock['pid'], lock['created_at']))
        else:
            stack.enter_context(store.mutation_lock())
        for pid in info['writer_pids']:
            exited.append(require_process_absent(pid, info['writer_before']))
        if any(r['pid'] in writers or r['parent'] in writers for r in processes()):
            fail('writer_descendant_present')
        qualify_remaining(document, current)
        cleanup.files.remaining(fresh['files'])
        for target in fresh['native_targets']:
            cleanup.native_remaining(target)
        for desktop in fresh['desktop']:
            cleanup.t3code_localstorage.remaining(desktop['localstorage'])
        journal_hashes = {p.name: ff.read_file(store.directory,p.name)[0]['sha256']
                          for p in (store.plan_path,store.events_path,store.state_path)}
        from .office_cleanup import digest
        proof_hash = proof['sha256'] if proof else digest(journal_hashes)
        audit = dict(schema_version='lam.t3-partial-recovery.v1', parent_plan_sha256=document['plan_sha256'],
            child_plan_sha256=store.read_plan()['plan_sha256'], original_lock_sha256=proof_hash,
            original_lock=lock, writer_exit=exited, completed_children=info['completed_children'],
            desktop_operations=info['desktop_operations'], remaining_plan_sha256=current['plan_sha256'],
            goal_status='completed_with_residuals', goal_satisfied=False, verified_at=utc_now(),
            mutation_replayed=False, original_failure=ERROR,
            journal_sha256=journal_hashes)
        path = (next(store.directory.glob('partial-recovery-*.json')) if info['settled'] else
                store.directory/('partial-recovery-'+proof_hash+'.json'))
        if not path.exists():
            write_new_json(path, audit)
        elif not info['settled']:
            prior = strict_json_load(path)
            for key in ('schema_version','parent_plan_sha256','child_plan_sha256','original_lock_sha256'):
                if prior.get(key) != audit[key]:
                    fail('audit_changed')
        state = store.read_state()
        state.update(phase='finished', goal_status='completed_with_residuals', goal_satisfied=False,
                     modified=True, current_action_state='verified')
        if not info['settled']:
            store.append_event({'event':'verification_finished', 'goal_status':'completed_with_residuals',
                                'recovery_protocol':'t3_partial', 'audit_path':str(path)}, state_updates=state)
        result = dict(schema_version='larj.agent-result.v1', document_type='operation_result', command='agent',
            mode='agent', subcommand='verify', operation_id=store.operation_id,
            plan_sha256=store.read_plan()['plan_sha256'], phase='finished', goal_status='completed_with_residuals',
            goal_satisfied=False, modified=True, mutation_started=True,
            blockers=[dict(blocker_code='residual_records',scope='child',message='Fresh cleanup plan required')],
            counts={'action_count':1,'residual_count':1},
            residual_action_ids=[a['action_id'] for a in store.read_plan()['actions']])
        store.write_result(result)
        if original_lock:
            held.dispose(store.directory,'apply.lock',before=proof)
    return dict(goal_status='completed_with_residuals',goal_satisfied=False,partial_recovery_settled=True,
                operation_id=document['operation_id'],audit_path=str(path),mutation_replayed=False)
