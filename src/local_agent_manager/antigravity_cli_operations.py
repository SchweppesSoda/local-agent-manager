"""Antigravity CLI plan/apply/run/status/verify on the shared operation store."""
from functools import wraps
import inspect
from pathlib import Path
import uuid

from . import antigravity_cli_cleanup as native, antigravity_cli_guards as guards
from .mutation_guard import MutationScope, mutation_guard, mutation_roots
from .operation_store import OperationStore, plan_sha256, strict_json_load, write_new_json
from .record_identity import canonical_path
from .windows_held_files import HeldFiles

WRITER = 'antigravity-cli'
KIND = 'delete_antigravity_cli_sessions'


def _progress(options, stage, status, **values):
    callback = options.get('progress_callback')
    if callable(callback):
        try:
            callback({'stage': stage, 'status': status, **values})
        except Exception:
            pass


def _error(code, operation_id=None):
    return {'goal_status': 'blocked', 'goal_satisfied': False, 'modified': False,
            'mutation_started': False, 'operation_id': operation_id,
            'blockers': [{'blocker_code': code}], 'counts': {}}


def _public_result(result):
    # The journal retains its established child-result encoding. Callers of
    # the coordinator receive the same public protocol as the other clients.
    payload = {key: value for key, value in result.items() if key not in {'command', 'mode'}}
    payload.update(schema_version='larj.operation-result.v1', document_type='operation_result')
    return payload


def _receipt(store, expected_hash=None):
    if not store.receipt_path.exists():
        return None
    result = store.read_result(maintain=False)
    if (not result or not result.get('compacted') or not result.get('action_ids')
            or any(not aid.startswith(KIND+':') for aid in result['action_ids'])
            or expected_hash and result['plan_sha256'] != expected_hash):
        native.fail('receipt_binding_changed')
    return {**result, 'verification_basis': 'retained_completion_receipt'}


def _scope(options):
    source = options.get('scope') or options
    if (source.get('client') != 'antigravity' or not source.get('record_ids')
            or source.get('projects') or source.get('all_projects')
            or set(source.get('engines', ())) - {'antigravity'}
            or source.get('paseo_only') or source.get('t3code_only')):
        native.fail('exact_session_scope_required')
    return {'client': 'antigravity', 'record_ids': sorted({native.native_id(s) for s in source['record_ids']}),
            'projects': [], 'all_projects': False, 'engines': ['antigravity']}


def records(root, ids=()):
    root = native.root_path(root or Path.home()/'.gemini/antigravity-cli')
    wanted = {native.native_id(s) for s in ids}
    with native.private_database(root, native.SUMMARY) as (db, _, _):
        native._check(db, native.CONTRACT)
        rows = native._rows(db)
        result = [{'record_id': sid, 'engine': 'antigravity', 'native_root': str(root),
                   'parent_record_id': row['parent_conversation_id'] or None,
                   'step_count': row['step_count'], 'native_cleanup_status': 'requires_frozen_plan'}
                  for sid, row in rows.items() if not wanted or sid in wanted]
    return {'schema_version': 'lam.antigravity-cli-records.v1', 'client': 'antigravity',
            'records': result, 'counts': {'record_count': len(result)}, 'goal_status': 'complete',
            'goal_satisfied': True, 'modified': False, 'mutation_started': False}


def plan(options):
    scope = _scope(options)
    _progress(options, 'inventory', 'started')
    root = native.root_path(options.get('antigravity_root') or Path.home()/'.gemini/antigravity-cli')
    binary = options.get('antigravity_bin')
    if binary is None:
        native.fail('explicit_runtime_required')
    if options.get('operation_home') and canonical_path(options['operation_home']) != canonical_path(root):
        native.fail('journal_requires_native_root')
    evidence = native.freeze(root, scope['record_ids'], binary=binary)
    sources = guards.locations(t3code_roots=options.get('t3code_roots', ()), paseo_roots=options.get('paseo_roots', ()))
    protection = guards.freeze(sources, evidence['ids'])
    _progress(options, 'inventory', 'completed', counts={'selected_session_count': len(evidence['ids'])})
    operation_id = options.get('operation_id') or 'antigravity-'+uuid.uuid4().hex
    store = OperationStore(root, operation_id)
    path = Path(options['plan_path']) if options.get('plan_path') else store.plan_path
    action = {'kind': KIND, 'action_id': KIND+':'+operation_id,
              'thread_id': evidence['requested_ids'][0], 'affected_thread_ids': evidence['ids']}
    document = {'schema_version': 'larj.child-operation-plan.v1', 'document_type': 'operation_plan',
                'writer': WRITER, 'operation_id': operation_id, 'scope': scope,
                'mutation_family': KIND,
                'target': {'codex_home': str(root)}, 'actions': [action],
                'authorization': {'blockers': []}, 'antigravity_cli_evidence': evidence,
                'guard_sources': protection, 'plan_path': str(path)}
    document['plan_sha256'] = plan_sha256(document)
    if path != store.plan_path:
        write_new_json(path, document)
    store.accept_plan(document)
    with mutation_roots((root,)):
        if store.read_state() is None:
            store.write_state(_state(document))
            store.append_event({'event': 'plan_accepted'})
    return {'goal_status': 'ready', 'goal_satisfied': False, 'modified': False, 'mutation_started': False,
            'operation_id': operation_id, 'plan_sha256': document['plan_sha256'], 'plan_path': str(path),
            'scope': scope, 'selected_ids': evidence['ids'], 'actions': [action], 'blockers': [],
            'counts': {'selected_session_count': len(evidence['ids'])},
            'verification_scope': 'qualified_local_cli_store_and_known_frontend_references'}


def _load(path, options):
    document = strict_json_load(Path(path))
    if (not isinstance(document, dict) or document.get('writer') != WRITER
            or document.get('schema_version') != 'larj.child-operation-plan.v1'
            or document.get('plan_sha256') != plan_sha256(document)):
        native.fail('plan_invalid')
    if options.get('operation_id') != document['operation_id']:
        native.fail('operation_id_mismatch')
    root = native.validate(document['antigravity_cli_evidence'])
    if canonical_path(document['target']['codex_home']) != canonical_path(root):
        native.fail('journal_binding_changed')
    if _scope(document) != document['scope'] or document['scope']['record_ids'] != document['antigravity_cli_evidence']['requested_ids']:
        native.fail('scope_changed')
    if options.get('antigravity_root') and canonical_path(options['antigravity_root']) != canonical_path(root):
        native.fail('root_changed')
    supplied_scope = options.get('scope') or {}
    for key, value in supplied_scope.items():
        value = list(value) if isinstance(value, tuple) else value
        if value and value != document['scope'].get(key):
            native.fail('scope_changed')
    store = OperationStore(root, document['operation_id'])
    if options.get('operation_home'):
        from .operation_coordinator import _operation_plan_path
        if canonical_path(_operation_plan_path(Path(options['operation_home']), document['operation_id'])) != canonical_path(store.plan_path):
            native.fail('journal_binding_changed')
    if options.get('codex_home') and canonical_path(options['codex_home']) != canonical_path(root):
        native.fail('journal_binding_changed')
    if options.get('antigravity_bin') and canonical_path(options['antigravity_bin']) != canonical_path(
            Path(document['antigravity_cli_evidence']['runtime']['root'])/document['antigravity_cli_evidence']['runtime']['file']['path']):
        native.fail('runtime_binding_changed')
    for client in ('t3code', 'paseo'):
        known = {canonical_path(s['root']) for s in document['guard_sources'] if s['client'] == client}
        if any(canonical_path(p) not in known for p in options.get(client+'_roots', ())):
            native.fail('guard_scope_changed')
    if _receipt(store, document['plan_sha256']) is None and store.read_plan() != document:
        native.fail('accepted_plan_changed')
    return document, store


def _state(document, *, phase='preflight', goal='unknown', started=False, modified=False):
    return {'schema_version': 'larj.agent-state.v1', 'operation_id': document['operation_id'],
            'plan_sha256': document['plan_sha256'], 'phase': phase, 'goal_status': goal,
            'goal_satisfied': goal == 'complete', 'mutation_started': started, 'modified': modified,
            'current_action_state': 'started' if started else 'not_started'}


def _result(document, state, goal, code=None):
    complete = goal == 'complete'
    return {'schema_version': 'larj.agent-result.v1', 'document_type': 'operation_result',
            'command': 'agent', 'mode': 'agent', 'operation_id': document['operation_id'],
            'plan_sha256': document['plan_sha256'], 'phase': 'finished' if complete else 'recovery_required' if state.get('mutation_started') else 'blocked',
            'goal_status': goal, 'goal_satisfied': complete, 'modified': bool(state.get('modified')),
            'mutation_started': bool(state.get('mutation_started')), 'scope': document['scope'],
            'blockers': [] if code is None else [{'blocker_code': code}],
            'counts': {'selected_session_count': len(document['antigravity_cli_evidence']['ids'])},
            'verification': {'all_satisfied': complete, 'verified_action_ids': [a['action_id'] for a in document['actions']] if complete else []},
            'final_scope_verification': {'all_satisfied': complete},
            'verification_scope': 'qualified_local_cli_store_and_known_frontend_references'}


def execute(document, store, options):
    if not options.get('clients_closed'):
        native.fail('clients_closed_ack_required')
    if options.get('plan_sha256') != document['plan_sha256']:
        native.fail('approved_plan_hash_required')
    receipt = _receipt(store, document['plan_sha256'])
    if receipt is not None:
        return receipt
    evidence = document['antigravity_cli_evidence']
    root = Path(evidence['root'])
    with mutation_guard((MutationScope(root, None),), store=store), store.mutation_lock():
        state = _state(document)
        store.write_state(state)
        def phase(event):
            nonlocal state
            if event == 'mutation_started':
                state.update(phase='executing', mutation_started=True, modified=True, current_action_state='started')
            store.append_event({'event': event}, state_updates=state)
            _progress(options, event, 'completed', operation_id=document['operation_id'])
        try:
            guards.recheck(document['guard_sources'], evidence['ids'])
            guards.require_closed(document['guard_sources'], options.get('census'))
            with HeldFiles() as held, native.execution_scope(evidence):
                guards.acquire(document['guard_sources'], held)
                guards.require_closed(document['guard_sources'], options.get('census'))
                native.apply(evidence, phase_callback=phase,
                             recovery_dir=store.directory/'native-recovery', census=options.get('census'))
            guards.recheck(document['guard_sources'], evidence['ids'])
            goal, code = 'complete', None
        except Exception as exc:
            goal = 'unknown' if state['mutation_started'] else 'blocked'
            code = getattr(exc, 'kind', 'antigravity_cli_'+type(exc).__name__)
        result = _result(document, state, goal, code)
        state.update(phase=result['phase'], goal_status=goal, goal_satisfied=goal == 'complete')
        # A preflight refusal is retryable. An attempted operation is never
        # silently dispatched again, including after a successful rollback.
        store.append_event({'event': 'operation_finished', 'goal_status': goal}, state_updates=state)
        store.write_result(result)
        return result


def query(document, store, *, verify):
    with mutation_roots((store.codex_home,)):
        receipt = _receipt(store, document['plan_sha256'])
        if receipt is not None:
            return receipt
        state = store.read_state() or _state(document)
        if store.lock_exists():
            return _result(document, state, 'unknown', 'antigravity_cli_apply_lock_present')
        store.read_events()
        prior = store.read_result(maintain=False)
        if not verify:
            return prior or _result(document, state, 'unknown', 'antigravity_cli_not_applied')
        evidence = document['antigravity_cli_evidence']
        try:
            guards.recheck(document['guard_sources'], evidence['ids'])
            count = native.remaining(evidence)
            if not count:
                native.discard_recovery(evidence, store.directory/'native-recovery')
            goal = 'complete' if not count else 'unknown' if state['mutation_started'] else 'blocked'
            code = None if not count else 'antigravity_cli_frozen_residuals_require_recovery'
        except Exception as exc:
            goal, code = 'unknown', getattr(exc, 'kind', 'antigravity_cli_'+type(exc).__name__)
        result = _result(document, state, goal, code)
        state.update(phase=result['phase'], goal_status=goal, goal_satisfied=goal == 'complete')
        store.write_state(state)
        store.append_event({'event': 'verification_finished', 'goal_status': goal}, state_updates=state)
        store.write_result(result)
        return result


def route(verb):
    """Keep the public coordinator API and its existing non-CLI paths intact."""
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def routed(self, *args, **kwargs):
            options = dict(signature.bind(self, *args, **kwargs).arguments)
            options.update(options.pop('_unused', {}))
            options.pop('self', None)
            selected = (options.get('scope') or {}).get('client') or options.get('client')
            path = options.get('plan_path')
            if verb in {'apply', 'status', 'verify'}:
                if not path:
                    from .operation_coordinator import _find_operation_plan
                    path = _find_operation_plan(options.get('operation_id'), operation_home=options.get('operation_home'), codex_home=options.get('codex_home'))
                if not path and verb in {'status', 'verify'} and options.get('operation_id'):
                    root = options.get('operation_home') or options.get('codex_home')
                    if root:
                        try:
                            receipt = _receipt(OperationStore(Path(root), options['operation_id']))
                            if receipt is not None:
                                return _public_result(receipt)
                        except (ValueError, RuntimeError, OSError):
                            pass
                if path and Path(path).is_file():
                    try:
                        document = strict_json_load(Path(path))
                        if isinstance(document, dict) and document.get('writer') == WRITER:
                            selected = 'antigravity'
                    except (ValueError, OSError, RuntimeError):
                        pass
            if selected != 'antigravity':
                return function(self, *args, **kwargs)
            store = None
            try:
                if verb in {'plan', 'run'}:
                    if verb == 'run' and not options.get('clients_closed'):
                        native.fail('clients_closed_ack_required')
                    result = plan(options)
                    if verb == 'plan':
                        return _public_result(result)
                    options.update(operation_id=result['operation_id'], plan_sha256=result['plan_sha256'])
                    path = result['plan_path']
                document, store = _load(path, options)
                if verb in {'apply', 'run'}:
                    return _public_result(execute(document, store, options))
                return _public_result(query(document, store, verify=verb == 'verify'))
            except Exception as exc:
                if store is not None:
                    try:
                        state = store.read_state()
                        if state and state['mutation_started']:
                            return _public_result(_result(document, state, 'unknown',
                                getattr(exc, 'kind', 'antigravity_cli_'+type(exc).__name__)))
                    except (ValueError, RuntimeError, OSError):
                        pass
                return _public_result(_error(getattr(exc, 'kind', 'antigravity_cli_'+type(exc).__name__), options.get('operation_id')))
        return routed
    return decorate
