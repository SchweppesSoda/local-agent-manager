"""Explicit, verification-only recovery of in-process joint CLI reservations.

Not a stale-lock utility. Other writers (notably app-server/Job journals) are
never admitted here. The original lock remains leased until a full verified
result has been published. Failure or another crash leaves occupancy intact.
"""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import json
import os
from datetime import datetime
from pathlib import Path

from .operation_store import plan_sha256, utc_now, strict_json_load
from .windows_held_files import HeldFiles
from . import frozen_files as ff

_active = ContextVar('joint_cli_readonly_recovery', default=None)
_settings = ContextVar('joint_cli_recovery_settings', default=None)


def held_settings(root, relative):
    ticket = _settings.get()
    key = (str(Path(root)), relative)
    if ticket is None or key not in ticket[1]:
        return None
    proof, raw = ticket[0].read(Path(root), relative, content=True, limit=1024*1024)
    if proof != ticket[1][key]:
        raise RuntimeError('joint_recovery_provider_selection_changed')
    return proof, raw


def require_process_absent(pid, created_at=None):
    """Reject access denial, a living writer AND PID reuse; no age heuristic."""
    if os.name != 'nt' or type(pid) is not int or pid <= 0:
        raise RuntimeError('joint_recovery_process_unproven')
    if created_at is not None:
        timestamp = datetime.fromisoformat(created_at)
        if timestamp.tzinfo is None:
            raise RuntimeError('joint_recovery_lock_time_untrusted')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),)*4)
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, 0) != 0 or created_at is None:
                raise RuntimeError('joint_recovery_writer_alive_or_pid_reused')
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise RuntimeError('joint_recovery_process_unproven')
            started = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)/10_000_000-11644473600
            lock_time = datetime.fromisoformat(created_at)
            if lock_time.tzinfo is None or started > lock_time.timestamp():
                raise RuntimeError('joint_recovery_writer_alive_or_pid_reused')
            # Windows can retain an exited process object after wait(); its
            # signalled kernel handle, unlike a missing census row, proves exit.
            return {'pid': pid, 'proof': 'signalled_process_handle', 'started_unix': started}
        finally:
            kernel.CloseHandle(handle)
    if ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: PID absent
        raise RuntimeError('joint_recovery_process_unproven')
    return {'pid': pid, 'proof': 'kernel_pid_absent'}


def _children(document, coordinator):
    from .operation_store import OperationStore
    roots = {s['storage_id']: Path(s['path']) for s in document['storages']}
    proofs = []
    if document.get('schema_version') not in {'larj.operation-plan.v1', 'larj.operation-plan.v2'}:
        raise RuntimeError('joint_recovery_parent_format_unsupported')
    # Reconstruct the exact child plan using the same binding as admission,
    # without creating/accepting any plan or dispatching an action.
    for batch in document['child_batches']:
        store = OperationStore(roots[batch['storage_id']], batch['child_operation_id'])
        if store.lock_exists():
            raise RuntimeError('joint_recovery_child_locked')
        result = store.read_result(maintain=False)
        if (not result or result.get('goal_status') != 'complete'
                or result.get('goal_satisfied') is not True):
            raise RuntimeError('joint_recovery_children_not_complete')
        # The receipt hash is checked by OperationStore; parent binding below
        # uses the production child-plan builder's pure document projection.
        expected = coordinator._child_document(document, batch)
        if result.get('plan_sha256') != expected['plan_sha256']:
            raise RuntimeError('joint_recovery_child_binding_changed')
        if not coordinator._workbuddy_terminal_verified(document, str(roots[batch['storage_id']]), batch['mutation_family']):
            raise RuntimeError('joint_recovery_child_after_state_unproven')
        proofs.append({'operation_id': store.operation_id, 'plan_sha256': result['plan_sha256']})
    if not proofs:
        raise RuntimeError('joint_recovery_children_missing')
    return proofs


@contextmanager
def recovery_scope(document, coordinator, approved_hash):
    from . import antigravity_cli_joint as joint, antigravity_cli_guards as guards
    from . import antigravity_cli_cleanup as native, t3code_lifecycle, paseo_lifecycle
    from .office_runtime import processes
    if not joint.document_has_targets(document) or approved_hash != document['plan_sha256'] or plan_sha256(document) != approved_hash:
        raise RuntimeError('joint_recovery_approved_parent_required')
    children = _children(document, coordinator)
    entries = {}
    frontends = []
    with ExitStack() as stack:
        held = stack.enter_context(HeldFiles())
        settings = {}
        selection_proofs = {}
        for action in document['actions']:
            payload = action.get('impact', {}).get('external_action_payload', {})
            for client in ('t3code', 'paseo'):
                evidence = payload.get(client+'_evidence')
                if not evidence or not joint.targets(evidence):
                    continue
                # Holds the actual startup boundary and binaries; an online
                # frontend is a blocker, never a request to close the host.
                lifecycle = t3code_lifecycle if client == 't3code' else paseo_lifecycle
                # Verify selection before admission, then keep the exact
                # settings leased for every subsequent discovery/recheck.
                from .antigravity_cli_binding import selection
                for target in joint.targets(evidence):
                    current = selection(evidence['binding_manifest'], target['binding'], client)
                    original = target['cli_selection']
                    # Reopened frontends atomically rewrite identical settings.
                    # Accept only byte/mode-identical selection, not arbitrary
                    # settings evolution. Freeze the current physical identity
                    # before leasing it; never rewrite the original plan proof.
                    if (current['runtime'] != original['runtime'] or any(
                            current['settings'][k] != original['settings'][k]
                            for k in ('path', 'size', 'sha256', 'mode'))):
                        raise RuntimeError('joint_recovery_provider_selection_changed')
                    proof = current['settings']
                    selection_proofs[evidence['root']] = {'original': original['settings'], 'current': proof,
                        'basis': 'byte_identical_selection_under_current_file_lease'}
                    key = (str(Path(evidence['root'])), proof['path'])
                    if key not in settings:
                        held.acquire(Path(evidence['root']), proof)
                        settings[key] = proof
                stack.enter_context(lifecycle.hold(evidence))
                guards.require_closed(evidence['cli_guards'])
                frontends.append((evidence, client))
                for target in joint.targets(evidence):
                    native.require_closed(target['native_files'])
                    runtime = target['native_files']['runtime']
                    key = (str(Path(runtime['root'])), runtime['file']['path'])
                    runtime_path = Path(runtime['root'])/runtime['file']['path']
                    from .record_identity import canonical_path
                    lifecycle_paths = {canonical_path(Path(b['root'])/b['file']['path']) for b in evidence['lifecycle']['binaries']}
                    if key not in held.handles and canonical_path(runtime_path) not in lifecycle_paths:
                        held.acquire(Path(runtime['root']), runtime['file'])
                    store, plan = joint.reservation(document, evidence, target)
                    if not store.lock_exists():
                        # Idempotent re-verification after successful recovery.
                        # Only a bound retained recovery audit can re-establish
                        # the post-terminal scope ticket; it never reclaims a lock.
                        prior = store.read_result(maintain=False)
                        audits = list(store.directory.glob('recovery-*.json'))
                        if prior and prior.get('goal_status') == 'complete' and prior.get('plan_sha256') == plan['plan_sha256'] and len(audits) == 1:
                            ff._plain(audits[0].lstat())
                            audit = strict_json_load(audits[0])
                            if (audit.get('schema_version') != 'lam.joint-cli-recovery.v1'
                                    or audit.get('parent_plan_sha256') != approved_hash
                                    or audit.get('parent_operation_id') != document['operation_id']
                                    or audit.get('reservation_plan_sha256') != plan['plan_sha256']
                                    or audit.get('children') != children or audit.get('goal_satisfied') is not True):
                                raise RuntimeError('joint_recovery_retained_audit_untrusted')
                            entries[str(store.directory)] = {'store': store, 'plan': plan,
                                'frontend': evidence, 'client': client, 'audit': audit,
                                'proof': None, 'completed': False}
                        continue
                    if store.read_plan() != plan:
                        raise RuntimeError('joint_recovery_reservation_changed')
                    state, events = store.read_state(), store.read_events()
                    if (not state or not state['mutation_started'] or not state['modified']
                            or not events or state.get('next_event_sequence') != len(events)+1
                            or not any(e['event'] == 'native_files_removed' for e in events)):
                        raise RuntimeError('joint_recovery_checkpoint_unproven')
                    proof, _ = ff.read_file(store.directory, 'apply.lock')
                    held.acquire(store.directory, proof, delete=True)
                    _, raw = held.read(store.directory, 'apply.lock', content=True)
                    from .environment_config import _unique_object
                    lock = json.loads(raw, object_pairs_hook=_unique_object)
                    if (not isinstance(lock, dict) or set(lock) != {'pid', 'created_at'} or type(lock['pid']) is not int
                            or not isinstance(lock['created_at'], str)):
                        raise RuntimeError('joint_recovery_lock_untrusted')
                    process_proof = require_process_absent(lock['pid'], lock['created_at'])
                    rows = processes()
                    if any(r['pid'] == lock['pid'] or r['parent'] == lock['pid'] for r in rows):
                        raise RuntimeError('joint_recovery_writer_descendant_present')
                    # This qualified writer only mutates synchronously inside
                    # Python. It has no native Job or delegated storage writer.
                    # Other child startup Jobs remain their own receipt contract.
                    entries[str(store.directory)] = {
                        'store': store, 'plan': plan, 'proof': proof, 'held': held,
                        'frontend': evidence, 'client': client,
                        'audit': {'schema_version': 'lam.joint-cli-recovery.v1',
                            'parent_operation_id': document['operation_id'], 'parent_plan_sha256': approved_hash,
                            'reservation_plan_sha256': plan['plan_sha256'], 'original_lock': lock,
                            'current_provider_settings': selection_proofs[evidence['root']],
                            'original_lock_sha256': proof['sha256'], 'children': children,
                            'writer_exit': process_proof,
                            'writer_model': 'synchronous_in_process_no_native_job',
                            'journal_sha256': {p.name: ff.read_file(store.directory, p.name)[0]['sha256']
                                for p in (store.plan_path, store.state_path, store.events_path)},
                            'verified_at': utc_now()}, 'completed': False}
        token = _active.set(entries)
        settings_token = _settings.set((held, settings))
        try:
            # The receipt/process/handle ticket is installed before the guard
            # refresh, which itself checks the owning frontend database.
            for evidence, client in frontends:
                joint.recheck(evidence, client)
                guards.acquire(evidence['cli_guards'], held)
                stack.enter_context(guards.leased_reads(evidence['cli_guards'], held))
            yield entries
        finally:
            _settings.reset(settings_token)
            _active.reset(token)
            for entry in entries.values():
                if entry['completed']:
                    # Atomic handle disposition: never close/reopen/unlink a
                    # pathname based only on a PID or terminal receipt.
                    held.dispose(entry['store'].directory, 'apply.lock', before=entry['proof'])


def completion_entry(store, plan):
    entry = (_active.get() or {}).get(str(store.directory))
    if entry and entry['plan'] != plan:
        raise RuntimeError('joint_recovery_ticket_changed')
    return entry if entry and entry['proof'] is not None else None


def database_evolution(evidence, db, current_hash):
    """Post-terminal scope proof, never an initial after-state exemption.

    All child action receipts have already proved their exact original after
    hashes. A reopened frontend may append messages/runs. Re-prove target
    absence and preservation membership under the offline lifecycle instead
    of pretending the old whole-database hash can remain unchanged forever.
    """
    from .t3code_store import V2
    for entry in (_active.get() or {}).values():
        frontend = entry['frontend']
        if entry['client'] != 't3code' or frontend['files']['database'] != evidence:
            continue
        original_ids = {b['thread_id'] for b in frontend['binding_manifest']['native_stores']}
        preserved = original_ids - set(frontend['selected_thread_ids'])
        present = {row[0] for row in db.execute(f'SELECT thread_id FROM {V2}threads')}
        if not preserved <= present or present & set(frontend['selected_thread_ids']):
            raise RuntimeError('joint_recovery_unselected_thread_missing_or_target_present')
        entry['audit']['current_database'] = {'logical_sha256': current_hash,
            'original_verified_after_sha256': evidence['after_sha256'],
            'preserved_thread_ids': sorted(preserved), 'new_thread_ids': sorted(present-original_ids),
            'basis': 'exact_original_child_completion_and_current_scope_preservation'}
        return True
    return False


def desktop_evolution(evidence, value):
    for entry in (_active.get() or {}).values():
        if (entry['client'] == 't3code' and any(d['localstorage'] == evidence for d in entry['frontend']['desktop'])
                and not value['count']['removed_pairs']):
            entry['audit'].setdefault('current_desktop', []).append({
                'root': evidence['root'], 'logical_sha256': value['logical_before_sha256'],
                'basis': 'exact_original_child_completion_and_current_target_absence'})
            return True
    return False


def publish_completion(entry):
    # Retained alongside the journal/receipt; does not overwrite the executor
    # failure, frozen plan, original events or an earlier recovery attempt.
    audit = {**entry['audit'], 'goal_status': 'complete', 'goal_satisfied': True}
    path = entry['store'].directory / ('recovery-'+audit['original_lock_sha256']+'.json')
    if path.exists():
        # A crash after publication but before handle disposition is safe to
        # finish, after repeating all guards/after-state checks. Retain the
        # first audit and its original pre-recovery journal hashes.
        ff._plain(path.lstat())
        prior = strict_json_load(path)
        for key in ('schema_version', 'parent_operation_id', 'parent_plan_sha256',
                    'reservation_plan_sha256', 'original_lock', 'original_lock_sha256',
                    'children', 'writer_model', 'goal_status', 'goal_satisfied'):
            if prior.get(key) != audit[key]:
                raise RuntimeError('joint_recovery_evidence_changed')
    else:
        from .operation_store import write_new_json
        write_new_json(path, audit)
    entry['completed'] = True
