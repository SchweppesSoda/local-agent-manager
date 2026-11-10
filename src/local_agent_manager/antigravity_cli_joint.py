"""Embed one exact native CLI closure in an existing frontend operation.

The frontend owns authorization, action journal, before/after evidence and
startup exclusion. Other frontend profiles remain immutable guard sources.
"""
from contextlib import contextmanager, ExitStack, nullcontext
from contextvars import ContextVar
from pathlib import Path

from . import antigravity_cli_cleanup as native, antigravity_cli_guards as guards
from . import antigravity_cli_binding as binding
from .office_cleanup import digest
from .record_identity import canonical_path
from .windows_held_files import HeldFiles

_execution = ContextVar('antigravity_cli_joint_execution', default=None)


def targets(evidence):
    return [t for t in evidence['native_targets'] if t['engine'] == 'antigravity' and binding.is_cli(t['binding'])]


def document_has_targets(document):
    return any(targets(a.get('impact', {}).get('external_action_payload', {}).get(client+'_evidence', {'native_targets': []}))
               for a in document.get('actions', ()) for client in ('t3code', 'paseo'))


def reservation(document, evidence, target):
    """Leave durable root occupancy where standalone native writers look.

    The frontend journal alone cannot protect shared summary/cache files
    from a later native operation after a crash in another profile.
    """
    from .operation_store import OperationStore, plan_sha256
    operation_id = 'joint-cli-'+digest([document['operation_id'], evidence['root'], target['root']])[:40]
    action = {'kind': 'delete_antigravity_cli_sessions', 'action_id': 'delete_antigravity_cli_sessions:'+operation_id,
              'thread_id': target['ids'][0], 'affected_thread_ids': target['ids']}
    plan = {'schema_version': 'larj.child-operation-plan.v1', 'document_type': 'operation_plan',
        'writer': 'antigravity-cli-joint', 'operation_id': operation_id,
        'parent_operation_id': document['operation_id'], 'parent_plan_sha256': document['plan_sha256'],
        'frontend_root': evidence['root'], 'frontend_evidence_sha256': digest(evidence),
        'scope': {'client': 'antigravity', 'record_ids': target['native_files']['requested_ids']},
        'target': {'codex_home': target['root']}, 'mutation_family': action['kind'],
        'actions': [action], 'antigravity_cli_evidence': target['native_files']}
    plan['plan_sha256'] = plan_sha256(plan)
    return OperationStore(Path(target['root']), operation_id), plan


def finish_reservation(store, plan, *, complete):
    from .antigravity_cli_operations import _result
    state = store.read_state()
    goal = 'complete' if complete else 'unknown'
    if not complete and not state['mutation_started']:
        try:
            native.discard_recovery(plan['antigravity_cli_evidence'], store.directory/'native-recovery')
            goal = 'blocked'
            state['current_action_state'] = 'not_started'
        except (ValueError, RuntimeError, OSError):
            pass
    result = _result(plan, state, goal, None if complete else 'antigravity_cli_joint_recovery_required')
    state.update(phase=result['phase'], goal_status=goal, goal_satisfied=complete)
    if complete:
        state['current_action_state'] = 'verified'
    store.append_event({'event': 'operation_finished', 'goal_status': goal}, state_updates=state)
    store.write_result(result)


def complete_document(document):
    """After full read-only verification, retire root occupancy and images."""
    from .mutation_guard import mutation_roots
    for action in document.get('actions', ()):
        payload = action.get('impact', {}).get('external_action_payload', {})
        for client in ('t3code', 'paseo'):
            evidence = payload.get(client+'_evidence')
            if evidence is None:
                continue
            for target in targets(evidence):
                store, plan = reservation(document, evidence, target)
                if not store.directory.exists():
                    continue
                from .joint_cli_recovery import completion_entry, publish_completion
                recovery = completion_entry(store, plan)
                with mutation_roots((store.codex_home,)), (nullcontext() if recovery else store.mutation_lock()):
                    if store.receipt_path.exists():
                        result = store.read_result(maintain=False)
                        if result.get('plan_sha256') != plan['plan_sha256'] or result.get('goal_status') != 'complete':
                            native.fail('joint_reservation_changed')
                        continue
                    if store.read_plan() != plan:
                        native.fail('joint_reservation_changed')
                    if recovery:
                        store.append_event({'event': 'readonly_recovery_verified',
                            'parent_plan_sha256': document['plan_sha256'],
                            'original_lock_sha256': recovery['proof']['sha256']})
                    native.discard_recovery(target['native_files'], store.directory/'native-recovery')
                    finish_reservation(store, plan, complete=True)
                    if recovery:
                        publish_completion(recovery)


def reservation_blockers(document):
    """Child receipts alone do not retire a separate shared-root occupancy."""
    result = []
    for action in document.get('actions', ()):
        payload = action.get('impact', {}).get('external_action_payload', {})
        for client in ('t3code', 'paseo'):
            evidence = payload.get(client+'_evidence')
            if not evidence:
                continue
            for target in targets(evidence):
                store, plan = reservation(document, evidence, target)
                try:
                    receipt = store.read_result(maintain=False)
                    if (store.lock_exists() or not receipt or receipt.get('goal_status') != 'complete'
                            or receipt.get('plan_sha256') != plan['plan_sha256']):
                        raise RuntimeError('joint_reservation_requires_verification')
                except (OSError, ValueError, RuntimeError) as exc:
                    result.append({'blocker_code': 'joint_reservation_recovery_required',
                        'scope': store.operation_id, 'message': str(exc)})
    return result


def freeze_target(manifest, bound, ids, client):
    selected = binding.selection(manifest, bound, client)
    evidence = native.freeze(bound['root'], ids, binary=bound['cli_binary'])
    if evidence['runtime'] != selected['runtime']:
        native.fail('runtime_changed')
    return evidence, selected


def validate_target(target, manifest):
    binding.check_binding(manifest, target['binding'])
    evidence = target['native_files']
    native.validate(evidence)
    if (canonical_path(evidence['root']) != target['root'] or evidence['ids'] != target['ids']
            or target['paths'] or target['action_ids']
            or evidence['runtime'] != target['cli_selection']['runtime']
            or target['cli_selection']['settings']['path'] != ('userdata/settings.json' if 'environment_id' in manifest else 'config.json')
            or canonical_path(Path(evidence['runtime']['root'])/evidence['runtime']['file']['path'])
                != canonical_path(target['binding']['cli_binary'])):
        native.fail('joint_target_scope_changed')
    # Child closure must be derivable from the frozen summary relationships.
    rows = {sid: {'parent_conversation_id': parent} for sid, parent in evidence['parents'].items()}
    if native._closure(rows, evidence['requested_ids']) != evidence['ids']:
        native.fail('joint_child_scope_changed')


def protection(evidence, client):
    return {'client': client, 'files': evidence['files']}


def freeze_guards(evidence, client, adapters):
    if not targets(evidence):
        return
    roots = {c: [] for c in ('t3code', 'paseo')}
    roots[client].append(evidence['root'])
    for adapter in adapters:
        # Include explicitly supplied profiles as well as default discovery.
        from .client_contracts import describe_adapter
        descriptor = describe_adapter(adapter)
        if descriptor.client in roots and descriptor.profile_root:
            roots[descriptor.client].append(descriptor.profile_root)
    sources = guards.locations(t3code_roots=roots['t3code'], paseo_roots=roots['paseo'])
    ids = sorted({sid for t in targets(evidence) for sid in t['ids']})
    evidence['cli_guards'] = guards.freeze(sources, ids, joint=protection(evidence, client))


def recheck(evidence, client):
    selected = targets(evidence)
    if selected:
        if 'cli_guards' not in evidence:
            native.fail('joint_guards_required')
        owned = [s for s in evidence['cli_guards'] if s['client'] == client
                 and canonical_path(s['root']) == canonical_path(evidence['root'])]
        if len(owned) != 1 or owned[0].get('joint_files_sha256') != digest(evidence['files']):
            native.fail('joint_guard_scope_changed')
        guards.recheck(evidence['cli_guards'], sorted({sid for t in selected for sid in t['ids']}),
                       joint=protection(evidence, client))


@contextmanager
def execution_scope(document, evidence, client, census=None):
    selected = targets(evidence)
    if not selected:
        yield
        return
    from . import t3code_lifecycle, paseo_lifecycle
    lifecycle = t3code_lifecycle if client == 't3code' else paseo_lifecycle
    if lifecycle.current_boundary(evidence['root']) != evidence:
        native.fail('joint_lifecycle_required')
    recheck(evidence, client)
    guards.require_closed(evidence['cli_guards'], census)
    with ExitStack() as stack:
        held = stack.enter_context(HeldFiles())
        guards.acquire(evidence['cli_guards'], held)
        stack.enter_context(guards.leased_reads(evidence['cli_guards'], held))
        settings_paths = set()
        for target in selected:
            proof = target['cli_selection']['settings']
            if proof['path'] in settings_paths:
                continue
            settings_paths.add(proof['path'])
            current = native.ff.read_file(Path(evidence['root']), proof['path'])[0]
            if current != proof:
                native.fail('provider_selection_changed')
            held.acquire(Path(evidence['root']), proof)
        guards.require_closed(evidence['cli_guards'], census)
        reservations = []
        token = _execution.set((evidence, document, census, held, stack, reservations))
        try:
            yield
        finally:
            _execution.reset(token)
            # The child dispatcher can return an unknown result without
            # raising. Only a complete native AND frontend after-state
            # releases shared-root occupancy.
            complete = False
            try:
                from . import t3code_cleanup, paseo_cleanup
                module = t3code_cleanup if client == 't3code' else paseo_cleanup
                complete = not module.files.remaining(evidence['files']) and all(
                    not native.remaining(t['native_files']) for t in selected)
                for desktop in evidence['desktop']:
                    if client == 't3code':
                        complete = complete and not module.t3code_localstorage.remaining(desktop['localstorage'])
                    else:
                        complete = complete and not (module.paseo_localstorage.remaining(desktop['localstorage'])
                            + module.paseo_indexeddb.remaining(desktop['indexeddb']))
            except (OSError, ValueError, RuntimeError):
                pass
            guard_error = None
            try:
                recheck(evidence, client)
            except (OSError, ValueError, RuntimeError) as exc:
                complete, guard_error = False, exc
            for store, plan in reservations:
                finish_reservation(store, plan, complete=complete)
            if guard_error:
                raise guard_error


def held_settings(root, relative):
    ticket = _execution.get()
    if ticket and canonical_path(root) == canonical_path(ticket[0]['root']):
        if (str(Path(root)), relative) in ticket[3].handles:
            return ticket[3].read(Path(root), relative, content=True)
    from .joint_cli_recovery import held_settings as recovery_settings
    return recovery_settings(root, relative)


def runtime_is_held(evidence):
    ticket = _execution.get()
    if not ticket:
        return False
    owner = ticket[0]
    if not any(t['native_files'] == evidence for t in targets(owner)):
        return False
    from . import t3code_lifecycle, paseo_lifecycle
    boundary = (t3code_lifecycle.current_boundary(owner['root'])
                or paseo_lifecycle.current_boundary(owner['root']))
    return bool(boundary == owner and evidence['runtime'] in boundary['lifecycle']['binaries'])


def execute(target, *, phase_callback):
    ticket = _execution.get()
    if not ticket or target not in targets(ticket[0]) or not runtime_is_held(target['native_files']):
        native.fail('joint_execution_required')
    from .antigravity_cli_operations import _state
    from .mutation_guard import _journal_unknown
    store, plan = reservation(ticket[1], ticket[0], target)
    store.accept_plan(plan)
    if store.read_state() is None:
        store.write_state(_state(plan))
        store.append_event({'event': 'plan_accepted'})
    else:
        unknown, started = _journal_unknown(store)
        if unknown or started:
            native.fail('joint_reservation_requires_recovery')
    ticket[4].enter_context(store.mutation_lock())
    ticket[5].append((store, plan))
    def phase(event):
        state = store.read_state()
        if event == 'mutation_started':
            state.update(phase='executing', mutation_started=True, modified=True, current_action_state='started')
        store.append_event({'event': event}, state_updates=state)
        phase_callback(event)
    with native.execution_scope(target['native_files']):
        native.apply(target['native_files'], phase_callback=phase, recovery_dir=store.directory/'native-recovery', census=ticket[2])
