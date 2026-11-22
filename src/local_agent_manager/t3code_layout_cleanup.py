"""Journaled Local Storage companion to an explicit desktop cache-only plan."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from . import t3code_desktop_cleanup as desktop, t3code_localstorage as local
from . import frozen_files as ff, office_leveldb as level
from .mutation_guard import MutationScope, mutation_guard
from .operation_store import atomic_write_json, write_new_json, strict_json_load, plan_sha256, validate_operation_id
from .paseo_indexeddb import _copy
from .t3code_store import fail

SCHEMA = 'lam.t3-layout-residual.v1'


def helpers():
    result = desktop.helpers_fingerprint()
    for name in ('t3code_layout_cleanup.py', 't3code_localstorage.py', 'paseo_indexeddb.py'):
        result[name] = hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
    return result


def new_plan(cache_plan):
    root = desktop.validate_plan(cache_plan)
    if cache_plan['schema_version'] != desktop.CACHE_SCHEMA:
        fail('layout_cache_plan_required')
    with mutation_guard([MutationScope(root, None)]), desktop.held_lifecycle(cache_plan['lifecycle']):
        desktop.admission(root)
        desktop.service_gate(cache_plan)
        selection = cache_plan['evidence']
        evidence = local.freeze(root, selection['environment'], selection['ids'], indexeddb=cache_plan)
        with local.prepared(evidence):
            pass
    value = {'schema_version': SCHEMA, 'operation_id': 'layout-' + uuid.uuid4().hex,
             'evidence': evidence, 'helpers': helpers()}
    value['plan_sha256'] = plan_sha256(value)
    return value


def validate(plan, authorized=None):
    if (plan.get('schema_version') != SCHEMA or plan.get('plan_sha256') != plan_sha256(plan)
            or plan.get('helpers') != helpers()
            or authorized is not None and authorized != plan['plan_sha256']):
        fail('layout_plan_or_helpers_changed')
    validate_operation_id(plan['operation_id'])
    root = local._validate_scope(plan['evidence'])
    if plan['evidence']['indexeddb']['schema_version'] != desktop.CACHE_SCHEMA:
        fail('layout_cache_plan_required')
    return root


def directory(plan):
    return desktop.journal_root(Path(plan['evidence']['root'])) / plan['operation_id']


def require_cache_complete(plan):
    cache = plan['evidence']['indexeddb']
    if not desktop.status_plan(cache)['goal_satisfied']:
        fail('layout_indexeddb_completion_required')
    desktop.service_gate(cache)


def content(snapshot):
    return [(f['path'], f['size'], f['sha256']) for f in snapshot['files']]


def finish(plan, state):
    evidence = plan['evidence']
    if local.remaining(evidence):
        fail('layout_after_unverified')
    path = directory(plan)
    seal = path / 'after'
    if seal.exists():
        if level._snapshot(seal, local.RELATIVE) != state['after_files']:
            fail('layout_sealed_after_changed')
        ff.apply_remove(path, ff.freeze_remove(path, 'after'))
    result = {'goal_status': 'complete', 'goal_satisfied': True, 'operation_id': plan['operation_id'],
              'removed_pairs': evidence['observation']['count']['removed_pairs'],
              'logical_after_sha256': evidence['observation']['logical_after_sha256'],
              'control_directory': str(path)}
    state.update(phase='complete', result=result)
    atomic_write_json(path / 'state.json', state)
    return result


def apply_plan(plan, authorized, *, clients_closed=False, phase_callback=lambda phase: None):
    if not clients_closed:
        fail('layout_clients_closed_required')
    root = validate(plan, authorized)
    with mutation_guard([MutationScope(root, None)]), desktop.held_lifecycle(plan['evidence']['indexeddb']['lifecycle']):
        desktop.admission(root)
        path = directory(plan)
        if path.exists():
            fail('layout_attempt_not_replayable')
        require_cache_complete(plan)
        evidence = plan['evidence']
        with local.prepared(evidence) as family:
            path.mkdir(parents=True)
            write_new_json(path / 'plan.json', plan)
            state = {'plan_sha256': plan['plan_sha256'], 'phase': 'sealing', 'attempts': 1}
            atomic_write_json(path / 'state.json', state)
            if family is not None:
                stage, snapshot = family
                seal = path / 'after'
                _copy(stage, snapshot, seal, relative=local.RELATIVE)
                after = level._snapshot(seal, local.RELATIVE)
                if content(after) != content(snapshot):
                    fail('layout_sealed_after_changed')
                state.update(phase='installing', after_files=after)
                atomic_write_json(path / 'state.json', state)
                if level._snapshot(root, local.RELATIVE) != evidence['files']:
                    fail('layout_source_changed')
                phase_callback('before_install')
                with ff._parent_fence(root, local.RELATIVE + '/CURRENT'):
                    for item in evidence['files']['files']:
                        ff._delete_frozen(root, item)
                    _copy(seal, after, root, relative=local.RELATIVE)
                    if content(level._snapshot(root, local.RELATIVE)) != content(after):
                        fail('layout_install_unverified')
                phase_callback('installed')
            return finish(plan, state)


def status_plan(plan):
    validate(plan)
    path = directory(plan)
    if not path.exists():
        return {'goal_status': 'ready', 'goal_satisfied': False, 'attempts': 0}
    state = strict_json_load(path / 'state.json')
    if state['plan_sha256'] != plan['plan_sha256']:
        fail('layout_journal_changed')
    complete = state['phase'] == 'complete' and local.remaining(plan['evidence']) == 0
    return {'goal_status': 'complete' if complete else 'unknown', 'goal_satisfied': complete, 'attempts': state['attempts']}


def verify_plan(plan, *, clients_closed=False):
    if not clients_closed:
        fail('layout_clients_closed_required')
    root = validate(plan)
    with mutation_guard([MutationScope(root, None)]), desktop.held_lifecycle(plan['evidence']['indexeddb']['lifecycle']):
        desktop.admission(root, own=plan['operation_id'])
        require_cache_complete(plan)
        state = strict_json_load(directory(plan) / 'state.json')
        if state['plan_sha256'] != plan['plan_sha256']:
            fail('layout_journal_changed')
        return finish(plan, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply', 'status', 'verify'))
    parser.add_argument('--plan', required=True)
    parser.add_argument('--cache-plan')
    parser.add_argument('--authorized-plan-sha256')
    parser.add_argument('--clients-closed', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            plan = new_plan(strict_json_load(Path(args.cache_plan)))
            write_new_json(Path(args.plan), plan)
            result = {'goal_status': 'ready', 'goal_satisfied': False, 'operation_id': plan['operation_id'],
                      'plan_sha256': plan['plan_sha256'], 'counts': plan['evidence']['observation']['count']}
        else:
            plan = strict_json_load(Path(args.plan))
            if args.command == 'apply':
                if not args.authorized_plan_sha256:
                    fail('layout_authorized_hash_required')
                result = apply_plan(plan, args.authorized_plan_sha256, clients_closed=args.clients_closed)
            elif args.command == 'status':
                result = status_plan(plan)
            else:
                result = verify_plan(plan, clients_closed=args.clients_closed)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({'goal_status': 'blocked' if args.command == 'plan' else 'unknown',
                          'goal_satisfied': False, 'blocker_code': getattr(exc, 'kind', 'layout_operation_failed')}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
