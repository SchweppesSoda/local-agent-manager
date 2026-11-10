"""Client artifact maintenance using shared journals and mutation admission."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
import sys

from local_agent_manager import frozen_files as ff
from local_agent_manager import client_artifacts as rules
from local_agent_manager import runtime_artifacts
from local_agent_manager.mutation_guard import MutationScope, mutation_guard, mutation_roots
from local_agent_manager.operation_store import OperationStore, OperationStoreError, plan_sha256, strict_json_load, write_new_json
from local_agent_manager.windows_held_files import HeldFiles

WRITER = 'client-artifact-maintenance'
KIND = 'cleanup_client_artifacts'


def _error_code(error):
    if isinstance(error, (ValueError, ff.FrozenFilesError)) and re.fullmatch('[a-zA-Z0-9_]{1,128}', str(error)):
        return str(error)
    if isinstance(error, OSError) and getattr(error, 'winerror', None) in {5, 32, 33}:
        return 'maintenance_file_in_use_or_access_denied'
    return getattr(error, 'kind', 'maintenance_' + type(error).__name__)


def _implementation():
    from local_agent_manager import windows_held_files
    modules = (sys.modules[__name__], rules, runtime_artifacts, ff, windows_held_files)
    return {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in modules}


def _state(document):
    return {'schema_version': 'larj.agent-state.v1', 'operation_id': document['operation_id'],
            'plan_sha256': document['plan_sha256'], 'phase': 'preflight', 'goal_status': 'unknown',
            'goal_satisfied': False, 'mutation_started': False, 'modified': False,
            'current_action_state': 'not_started'}


def _result(document, state, goal, *, code=None, remaining=None):
    complete = goal == 'complete'
    return {'schema_version': 'larj.agent-result.v1', 'document_type': 'operation_result',
            'command': 'agent', 'mode': 'agent', 'subcommand': 'maintenance',
            'operation_id': document['operation_id'], 'plan_sha256': document['plan_sha256'],
            'goal_status': goal, 'goal_satisfied': complete,
            'phase': 'finished' if goal in {'complete', 'completed_with_residuals'} else 'recovery_required' if state['mutation_started'] else 'blocked',
            'mutation_started': state['mutation_started'], 'modified': state['modified'],
            'scope': document['scope'], 'blockers': [] if not code else [{'blocker_code': code}],
            'counts': {'approved_file_count': len(_files(document)), 'residual_file_count': len(remaining) if remaining is not None else None,
                       'planned_logical_bytes': sum(item['size'] for item in _files(document)),
                       'observed_reclaimed_bytes': None},
            'residual_paths': list(remaining) if remaining is not None else None, 'retained': document['retained'],
            'verification': {'all_satisfied': complete, 'verified_action_ids': [KIND + ':' + document['operation_id']] if complete else []},
            'final_scope_verification': {'all_satisfied': complete}}


def _public(result):
    return {**{key: value for key, value in result.items() if key not in {'command', 'mode'}},
            'schema_version': 'lam.maintenance-result.v1', 'command': 'maintenance'}


def _files(document):
    return [proof for change in document['artifact_evidence']['changes'] for proof in change['evidence']['files']]


def _dependencies(document):
    evidence = document['artifact_evidence']
    root = Path(evidence['root'])
    dependencies = [(root, proof) for proof in evidence['dependencies']]
    if evidence.get('runtime_evidence'):
        dependencies.extend(runtime_artifacts.dependencies(root, evidence['runtime_evidence']))
    unique = {}
    for source_root, proof in dependencies:
        key = os.path.normcase(str(source_root / proof['path']))
        if key in unique and unique[key][1] != proof:
            raise ValueError('maintenance_dependency_changed')
        unique[key] = (source_root, proof)
    return list(unique.values())


def _selected_root(client, root, *, categories):
    if root is not None:
        return rules.root_path(root)
    observed = {}
    for profile in rules.default_profiles((client,), include_runtime_roots='runtimes' in categories):
        try:
            path = rules.root_path(profile['root'])
        except FileNotFoundError:
            continue
        observed.setdefault(tuple(ff._identity(path.lstat())), path)
    if len(observed) != 1:
        raise ValueError('maintenance_multiple_or_unobserved_profiles_use_inventory_and_root')
    return next(iter(observed.values()))


def plan(client, root=None, *, categories, older_than_days=14, plan_path=None, operation_id=None, runtime_review=None):
    if not 0 <= older_than_days <= 36500:
        raise ValueError('maintenance_retention_invalid')
    cutoff = time.time_ns() - older_than_days * 86400 * 1000000000
    evidence = rules.inspect_profile(client, _selected_root(client, root, categories=categories), categories=categories,
                                    cutoff_ns=cutoff, freeze=True, runtime_review=runtime_review)
    paths = [proof['path'] for change in evidence['changes'] for proof in change['evidence']['files']]
    if len(paths) != len(set(paths)) or set(paths) & {p['path'] for p in evidence['dependencies']}:
        raise ValueError('maintenance_overlapping_scope')
    operation_id = operation_id or 'maintenance-' + uuid.uuid4().hex
    store = OperationStore(Path(evidence['root']), operation_id)
    destination = Path(plan_path) if plan_path else store.plan_path
    retained = [item for item in evidence['observations'] if item['decision'] != 'eligible_for_plan']
    document = {'schema_version': 'larj.child-operation-plan.v1', 'document_type': 'operation_plan',
                'writer': WRITER, 'mutation_family': KIND, 'operation_id': operation_id,
                'target': {'codex_home': evidence['root']},
                'scope': {'client': client, 'root': evidence['root'], 'categories': sorted(categories),
                          'older_than_days': older_than_days, 'cutoff_ns': cutoff},
                'actions': [{'kind': KIND, 'action_id': KIND + ':' + operation_id}],
                'authorization': {'blockers': []}, 'artifact_evidence': evidence,
                'retained': retained, 'implementation': _implementation(), 'plan_path': str(destination)}
    document['plan_sha256'] = plan_sha256(document)
    removed = {os.path.normcase(str(Path(evidence['root']) / path)) for path in paths}
    if removed & {os.path.normcase(str(source_root / proof['path'])) for source_root, proof in _dependencies(document)}:
        raise ValueError('maintenance_overlapping_scope')
    if destination != store.plan_path:
        write_new_json(destination, document)
    store.accept_plan(document)
    with mutation_roots((store.codex_home,)):
        if store.read_state() is None:
            store.write_state(_state(document))
            store.append_event({'event': 'plan_accepted'})
    return {'schema_version': 'lam.maintenance-result.v1', 'command': 'maintenance', 'goal_status': 'ready',
            'goal_satisfied': False, 'modified': False, 'mutation_started': False,
            'operation_id': operation_id, 'plan_sha256': document['plan_sha256'], 'plan_path': str(destination),
            'scope': document['scope'], 'observations': evidence['observations'],
            'counts': {'approved_file_count': len(paths), 'planned_logical_bytes': sum(p['size'] for p in _files(document)),
                       'observed_reclaimed_bytes': None}}


def _load(path):
    document = strict_json_load(Path(path))
    if (not isinstance(document, dict) or document.get('writer') != WRITER
            or document.get('schema_version') != 'larj.child-operation-plan.v1'
            or document.get('plan_sha256') != plan_sha256(document)):
        raise ValueError('maintenance_plan_invalid')
    evidence, scope = document['artifact_evidence'], document['scope']
    root = rules.root_path(scope['root'])
    if (evidence['root'] != str(root) or document['target']['codex_home'] != str(root)
            or evidence['client'] != scope['client'] or evidence['categories'] != scope['categories']
            or evidence['cutoff_ns'] != scope['cutoff_ns']):
        raise ValueError('maintenance_scope_changed')
    store = OperationStore(root, document['operation_id'])
    if store.read_plan() != document:
        raise ValueError('maintenance_accepted_plan_changed')
    return document, store


def _check_dependencies(document):
    evidence = document['artifact_evidence']
    root = rules.root_path(evidence['root'])
    if ff._identity(root.lstat()) != evidence['root_identity']:
        raise ff.FrozenFilesError('maintenance_root_changed')
    for proof in evidence['dependencies']:
        if ff.read_file(root, proof['path'])[0] != proof:
            raise ff.FrozenFilesError('maintenance_dependency_changed')
    if evidence.get('runtime_evidence'):
        runtime_artifacts.verify(root, evidence['runtime_evidence'])
    _check_absences(document)
    return root


def _check_absences(document):
    evidence = document['artifact_evidence']
    for relative in evidence['absent_paths']:
        path = ff.checked_path(Path(evidence['root']), relative)
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise ff.FrozenFilesError('maintenance_previously_absent_sidecar_appeared')
    if evidence.get('runtime_evidence'):
        runtime_artifacts.check_absences(evidence['runtime_evidence'])


def _publish(document, store, state, goal, *, code=None, remaining=None):
    result = _result(document, state, goal, code=code, remaining=remaining)
    state.update(phase=result['phase'], goal_status=goal, goal_satisfied=result['goal_satisfied'])
    store.append_event({'event': 'operation_finished', 'goal_status': goal}, state_updates=state)
    store.write_result(result)
    return _public(result)


def apply(path, *, approved_hash, clients_closed=False):
    try:
        document, store = _load(path)
    except OperationStoreError:
        historical = query(path)
        if not historical.get('compacted'):
            raise
        if historical.get('plan_sha256') != approved_hash:
            raise ValueError('maintenance_approved_hash_required')
        return historical
    if approved_hash != document['plan_sha256']:
        raise ValueError('maintenance_approved_hash_required')
    if not clients_closed:
        raise ValueError('maintenance_clients_closed_ack_required')
    if os.name != 'nt':
        raise ValueError('maintenance_windows_exclusion_required')
    if document['implementation'] != _implementation():
        raise ValueError('maintenance_implementation_changed_replan')
    state = store.read_state() or _state(document)
    if state['goal_status'] == 'complete':
        return query(path, verify=True)
    if state['mutation_started']:
        return query(path)
    with mutation_guard((MutationScope(store.codex_home, None),), store=store), store.mutation_lock():
        try:
            root = _check_dependencies(document)
            fresh = rules.inspect_profile(document['scope']['client'], root,
                categories=document['scope']['categories'], cutoff_ns=document['scope']['cutoff_ns'], freeze=True,
                runtime_review=(document['artifact_evidence'].get('runtime_evidence') or {}).get('review'))
            if fresh != document['artifact_evidence']:
                raise ff.FrozenFilesError('maintenance_plan_drift')
            with HeldFiles() as held:
                runtime_evidence = document['artifact_evidence'].get('runtime_evidence')
                if runtime_evidence:
                    held.stack.enter_context(runtime_artifacts.hold_pointers(runtime_evidence))
                for source_root, proof in _dependencies(document):
                    held.acquire(source_root, proof)
                for proof in _files(document):
                    held.acquire(root, proof, delete=True)
                # Validate the complete member list after all leases are held.
                # Held reads replace pathname opens, which exclusion now denies.
                for change in document['artifact_evidence']['changes']:
                    for directory in change['evidence']['directories']:
                        if (ff._identity(ff.checked_path(root, directory['path']).lstat()) != directory['identity']
                                or rules._names(root, directory['path']) != directory['entries']):
                            raise ff.FrozenFilesError('maintenance_directory_changed')
                _check_absences(document)
                if runtime_evidence:
                    runtime_artifacts.check_directories(root, runtime_evidence['preserved'])
                    runtime_artifacts.check_containers(root, runtime_evidence['containers'])
                    runtime_artifacts.check_writers(runtime_evidence)
                if _files(document):
                    state.update(phase='executing', mutation_started=True, modified=True, current_action_state='started')
                    store.append_event({'event': 'mutation_started'}, state_updates=state)
                for proof in _files(document):
                    held.dispose(root, proof['path'], before=proof)
            remaining = _remaining(document)
            if remaining:
                raise ff.FrozenFilesError('maintenance_frozen_artifacts_reappeared')
            return _publish(document, store, state, 'complete', remaining=remaining)
        except Exception as exc:
            return _publish(document, store, state, 'unknown' if state['mutation_started'] else 'blocked',
                            code=_error_code(exc))


def _remaining(document):
    root = _check_dependencies(document)
    if document['artifact_evidence'].get('runtime_evidence'):
        runtime_artifacts.check_removed_directories(root,
            [change['evidence'] for change in document['artifact_evidence']['changes'] if change['category'] == 'runtimes'])
    remaining = []
    for proof in _files(document):
        try:
            current, _ = ff.read_file(root, proof['path'])
        except FileNotFoundError:
            continue
        if current != proof:
            raise ff.FrozenFilesError('maintenance_residual_changed')
        remaining.append(proof['path'])
    return remaining


def query(path=None, *, root=None, operation_id=None, verify=False):
    document, expected_hash = None, None
    if path is not None:
        path = Path(path)
        try:
            path.lstat()
        except FileNotFoundError:
            if (not path.is_absolute() or len(path.parents) < 4 or path.name != 'plan.json'
                    or path.parents[1].name != 'operations'
                    or path.parents[2].name not in {'.local-agent-record-janitor', '.local-agent-manager'}):
                raise ValueError('maintenance_missing_external_plan')
            store = OperationStore(rules.root_path(path.parents[3]), path.parent.name)
        else:
            document = strict_json_load(path)
            if (not isinstance(document, dict) or document.get('writer') != WRITER
                    or document.get('schema_version') != 'larj.child-operation-plan.v1'
                    or document.get('plan_sha256') != plan_sha256(document)):
                raise ValueError('maintenance_plan_invalid')
            expected_hash = document['plan_sha256']
            store = OperationStore(rules.root_path(document['target']['codex_home']), document['operation_id'])
    else:
        if root is None or operation_id is None:
            raise ValueError('maintenance_plan_or_root_and_operation_id_required')
        store = OperationStore(rules.root_path(root), operation_id)
    with mutation_roots((store.codex_home,)):
        if store.receipt_path.exists():
            result = store.read_result(maintain=False)
            if not result or not result.get('compacted') or not result.get('action_ids') or any(
                    not action.startswith(KIND + ':') for action in result['action_ids']) or (
                    expected_hash is not None and result['plan_sha256'] != expected_hash):
                raise ValueError('maintenance_receipt_binding_invalid')
            if store.lock_exists():
                result = {**result, 'goal_status': 'unknown', 'goal_satisfied': False, 'phase': 'recovery_required',
                          'blockers': [{'blocker_code': 'maintenance_apply_lock_present'}],
                          'counts': {**result.get('counts', {}), 'residual_file_count': None},
                          'verification': {'all_satisfied': False}, 'final_scope_verification': {'all_satisfied': False}}
            return _public({**result, 'verification_basis': 'retained_completion_receipt'})
        document, store = _load(path if document is not None else store.plan_path)
        state = store.read_state() or _state(document)
        store.read_events()
        if store.lock_exists():
            return _public(_result(document, state, 'unknown', code='maintenance_apply_lock_present'))
        previous = store.read_result(maintain=False)
        if not verify:
            return _public(previous or _result(document, state, 'unknown',
                code='maintenance_recovery_required' if state['mutation_started'] else 'maintenance_not_applied'))
        try:
            remaining = _remaining(document)
            goal = 'completed_with_residuals' if remaining and state['mutation_started'] else 'blocked' if remaining else 'complete'
            return _publish(document, store, state, goal, remaining=remaining,
                            code='maintenance_frozen_residuals_replan_required' if remaining else None)
        except (OSError, ValueError, RuntimeError) as exc:
            return _publish(document, store, state, 'unknown' if state['mutation_started'] else 'blocked',
                            code=_error_code(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='local-agent-manager maintenance', description='Inspect and remove qualified client artifacts')
    commands = parser.add_subparsers(dest='command', required=True)
    for verb in ('inventory', 'plan', 'run', 'apply', 'status', 'verify'):
        child = commands.add_parser(verb)
        child.add_argument('--json', action='store_true')
        if verb in {'inventory', 'plan', 'run'}:
            child.add_argument('--client', choices=rules.CLIENTS, required=verb != 'inventory')
            child.add_argument('--root', type=Path)
            child.add_argument('--category', choices=rules.CATEGORIES, action='append', required=verb != 'inventory')
            child.add_argument('--older-than-days', type=int, default=14)
        if verb in {'plan', 'run'}:
            child.add_argument('--runtime-review', type=Path,
                               help='AI-reviewed exact runtime decisions and dependency files')
        if verb in {'plan', 'run', 'apply', 'status', 'verify'}:
            child.add_argument('--plan', type=Path, required=verb == 'apply')
        if verb in {'status', 'verify'}:
            child.add_argument('--root', type=Path)
            child.add_argument('--operation-id')
        if verb in {'apply', 'run'}:
            child.add_argument('--clients-closed', action='store_true')
        if verb == 'apply':
            child.add_argument('--plan-sha256', required=True)
    args = parser.parse_args(argv)
    result = {}
    dispatching = False
    try:
        if args.command == 'inventory':
            if args.root and not args.client:
                raise ValueError('maintenance_explicit_root_requires_client')
            if not 0 <= args.older_than_days <= 36500:
                raise ValueError('maintenance_retention_invalid')
            profiles = [{'client': args.client, 'root': str(args.root)}] if args.root else rules.default_profiles(
                (args.client,) if args.client else rules.CLIENTS,
                include_runtime_roots=not args.category or 'runtimes' in args.category)
            reports = []
            cutoff = time.time_ns() - args.older_than_days * 86400 * 1000000000
            for profile in profiles:
                try:
                    report = rules.inspect_profile(profile['client'], profile['root'],
                        categories=args.category or rules.CATEGORIES, cutoff_ns=cutoff)
                    report.pop('changes')
                    report.pop('dependencies')
                    reports.append(report)
                except (OSError, ValueError, RuntimeError) as exc:
                    reports.append({**profile, 'status': 'not_observed_at_candidate' if isinstance(exc, FileNotFoundError) else 'unqualified',
                                    'reason': type(exc).__name__})
            result = {'schema_version': 'lam.maintenance-inventory.v1', 'profiles': reports,
                      'coverage': 'known_defaults_and_environment_or_explicit_profile', 'modified': False}
        elif args.command in {'plan', 'run'}:
            if args.command == 'run' and not args.clients_closed:
                raise ValueError('maintenance_clients_closed_ack_required')
            result = plan(args.client, args.root, categories=sorted(set(args.category)),
                          older_than_days=args.older_than_days, plan_path=args.plan,
                          runtime_review=strict_json_load(args.runtime_review) if args.runtime_review else None)
            if args.command == 'run':
                dispatching = True
                result = apply(result['plan_path'], approved_hash=result['plan_sha256'], clients_closed=True)
        elif args.command == 'apply':
            dispatching = True
            result = apply(args.plan, approved_hash=args.plan_sha256, clients_closed=args.clients_closed)
        else:
            if args.plan and (args.root or args.operation_id):
                raise ValueError('maintenance_ambiguous_recovery_locator')
            result = query(args.plan, root=args.root, operation_id=args.operation_id, verify=args.command == 'verify')
        code = 3 if result.get('goal_status') in {'blocked', 'unknown', 'completed_with_residuals'} else 0
    except (OSError, ValueError, RuntimeError) as exc:
        recovering = args.command in {'status', 'verify'}
        state = {'modified': None, 'mutation_started': None} if dispatching or recovering else {'modified': False, 'mutation_started': False}
        recovery_path = getattr(args, 'plan', None) or result.get('plan_path')
        if (dispatching or recovering) and recovery_path:
            try:
                _document, store = _load(recovery_path)
                state = store.read_state() or state
            except (OSError, ValueError, RuntimeError):
                pass
        result = {'schema_version': 'lam.maintenance-result.v1', 'command': 'maintenance',
                  'goal_status': 'blocked' if state['mutation_started'] is False else 'unknown',
                  'goal_satisfied': False, 'modified': state['modified'], 'mutation_started': state['mutation_started'],
                  'blockers': [{'blocker_code': _error_code(exc)}]}
        code = 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
