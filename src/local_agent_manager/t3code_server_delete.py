"""Journaled, exact-ID T3 deletion through the product's local server.

This is T3's soft deletion, not a purge. No provider-native delete is called.
Worktree detachment is a durable product command before deletion, so later
worktreeOnDelete sweeps cannot remove the selected thread's former checkout.
"""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from urllib.parse import urlsplit

from . import frozen_files
from .cleaner import ScanReport
from .office_cleanup import digest
from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, ScanStatus, StorageLocation, TargetRef, storage_id_for_path
from .record_identity import canonical_path
from .sqlite_utils import connect_readonly
from .t3code_store import V2, database_path, engine, identifier, local_root, qualify, rows

KIND = 'delete_t3code_server_thread'
SCHEMA = 'larj.t3code-server-delete.v1'
VERSIONS = (
    '0.1.8-nightly.20261107.4301',
    '0.1.8-nightly.20261108.4353',
    '0.1.8-nightly.20261108.4380',
    '0.1.8-nightly.20261109.4399',
)
_ENTRY = 'resources/server.asar/apps/server/dist/bin.mjs'
_RUNTIME_FILES = ('T3 Code (Nightly).exe', 'resources/app.asar', 'resources/server.asar')


class T3ServerError(RuntimeError):
    kind = 't3code_server_delete_unverified'


def _bundled_install():
    if os.name != 'nt':
        raise T3ServerError('t3code_windows_bundle_required')
    return Path.home() / 'AppData/Local/Programs/t3code'


def _identity(root):
    root = local_root(root)
    for parent in (*reversed(root.parents), root):
        frozen_files._plain(parent.lstat(), directory=True)
    info = root.stat()
    return [info.st_dev, info.st_ino]


def _runtime(install):
    return [frozen_files.read_file(install, part)[0] for part in _RUNTIME_FILES]


def _environment_id(root):
    _, raw = frozen_files.read_file(root, 'userdata/environment-id', content=True, limit=256)
    return identifier(raw.decode('utf-8').strip())


def _locator(root):
    _, raw = frozen_files.read_file(root, 'userdata/server-runtime.json', content=True, limit=16384)
    try:
        state = json.loads(raw)
        url = urlsplit(state['origin'])
        if (state['version'] != 1 or type(state['pid']) is not int or state['pid'] <= 0
                or state.get('serverVersion') not in VERSIONS or not state.get('ownerId')
                or url.scheme != 'http' or url.hostname != '127.0.0.1'
                or url.port != state['port'] or not 1 <= url.port <= 65535
                or url.path not in ('', '/') or url.query or url.fragment or url.username or url.password
                or state.get('devUrl') or state.get('managedPolicy')):
            raise ValueError
        return {k: state[k] for k in ('pid', 'origin', 'port', 'startedAt', 'serverVersion', 'ownerId')}
    except (KeyError, ValueError, TypeError):
        raise T3ServerError('t3code_local_server_unproven') from None


def _snapshot(root, thread_id):
    with closing(connect_readonly(database_path(root))) as db:
        db.execute('BEGIN')
        qualify(db)
        record = rows(db, f'''SELECT thread_id,project_id,default_provider,provider_instance_id,deleted_at,
            json_extract(payload_json,'$.worktreePath') AS worktree_path,
            json_extract(payload_json,'$.branch') AS branch FROM {V2}threads WHERE thread_id=?''', (thread_id,))
        if len(record) != 1:
            raise T3ServerError('t3code_exact_thread_not_found')
        row = record[0]
        # Hash private state in-process; never place titles, messages or tokens in evidence.
        payload = db.execute(f'SELECT payload_json FROM {V2}threads WHERE thread_id=?', (thread_id,)).fetchone()[0]
        if json.loads(payload).get('deletedAt') != row['deleted_at']:
            raise T3ServerError('t3code_deletion_projection_inconsistent')
        row['fingerprint'] = hashlib.sha256(payload.encode()).hexdigest()
        row['active'] = bool(db.execute(f"SELECT 1 FROM {V2}runs WHERE thread_id=? AND status NOT IN ('completed','interrupted','failed','cancelled','rolled_back') LIMIT 1", (thread_id,)).fetchone())
        session_ids = f"SELECT provider_session_id FROM {V2}provider_sessions WHERE thread_id=? UNION SELECT provider_session_id FROM {V2}provider_session_bindings WHERE thread_id=?"
        stopped_session_ids = f"SELECT provider_session_id FROM {V2}provider_sessions WHERE status IN ('stopped','error')"
        row['active'] |= bool(db.execute(f"SELECT 1 FROM {V2}provider_sessions WHERE provider_session_id IN ({session_ids}) AND status NOT IN ('ready','stopped','error') LIMIT 1", (thread_id,thread_id)).fetchone())
        row['active'] |= bool(db.execute(f"SELECT 1 FROM {V2}provider_session_bindings WHERE provider_session_id IN ({session_ids}) AND provider_session_id NOT IN ({stopped_session_ids}) AND thread_id!=? LIMIT 1", (thread_id,thread_id,thread_id)).fetchone())
        row['live_sessions'] = bool(db.execute(f"SELECT 1 FROM {V2}provider_sessions WHERE provider_session_id IN ({session_ids}) AND status NOT IN ('stopped','error') LIMIT 1", (thread_id,thread_id)).fetchone())
        row['effects'] = rows(db, 'SELECT effect_id,status FROM orchestration_v2_effect_outbox WHERE thread_id=? ORDER BY effect_id', (thread_id,))
        row['native_refs'] = digest(rows(db, f"SELECT provider_thread_id,provider,driver,provider_instance_id,json_extract(payload_json,'$.nativeThreadRef') AS ref FROM {V2}provider_threads WHERE thread_id=? ORDER BY provider_thread_id", (thread_id,)))
        return {k:v for k,v in row.items() if v is not None}


def _worktree(path):
    if path is None:
        return None
    root = local_root(path)
    return {'path': str(root), 'identity': _identity(root)}


def _env(root):
    env = {k:v for k,v in os.environ.items() if not k.upper().startswith(('NODE_', 'ELECTRON_', 'T3CODE_'))}
    env.update(ELECTRON_RUN_AS_NODE='1', T3CODE_HOME=str(root))
    return env


def _cli(evidence, args, timeout):
    install, root = Path(evidence['install']), Path(evidence['root'])
    return subprocess.run([str(install / _RUNTIME_FILES[0]), str(install / _ENTRY), *args,
                           '--base-dir', str(root)], env=_env(root), cwd=root, capture_output=True,
                          timeout=timeout, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _rpc(evidence, commands, timeout):
    # Issue/revoke via T3's own CLI, never read provider credentials or edit auth tables.
    session = None
    try:
        session = json.loads(_cli(evidence, ['auth','session','issue','--ttl','2m','--label','janitor deletion','--json'], timeout).stdout)
        request = {'origin': evidence['server']['origin'], 'token': session['token'], 'commands': commands}
        result = subprocess.run([str(Path(evidence['install']) / _RUNTIME_FILES[0]),
                                 str(Path(__file__).with_name('t3code_server_rpc.cjs'))],
                                input=json.dumps(request).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=_env(evidence['root']), timeout=timeout, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if json.loads(result.stdout) is not True:
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        raise T3ServerError('t3code_server_rpc_unverified') from None
    finally:
        if session and isinstance(session.get('sessionId'), str):
            try:
                _cli(evidence, ['auth','session','revoke',session['sessionId']], timeout)
            except (OSError, subprocess.SubprocessError):
                raise T3ServerError('t3code_temporary_session_revoke_unverified') from None


def _check_identity(evidence):
    root = Path(evidence['root'])
    if (_identity(root) != evidence['root_identity'] or _environment_id(root) != evidence['environment_id']
            or _runtime(Path(evidence['install'])) != evidence['runtime']):
        raise T3ServerError('t3code_frozen_identity_changed')
    if evidence.get('worktree') != _worktree(evidence.get('worktree')['path'] if evidence.get('worktree') else None):
        raise T3ServerError('t3code_preserved_worktree_changed')


def freeze(root, thread_id):
    root = local_root(root)
    identifier(thread_id)
    row = _snapshot(root, thread_id)  # equality lookup, never prefix/title matching
    if row.get('deleted_at') is not None or row['active'] or any(e['status'] not in ('succeeded','cancelled') for e in row['effects']):
        raise T3ServerError('t3code_thread_not_quiescent_or_already_deleted')
    install = _bundled_install()
    return {'schema_version': SCHEMA, 'root': str(root), 'root_identity': _identity(root),
            'environment_id': _environment_id(root), 'thread_id': thread_id, 'before': row,
            **({'worktree': _worktree(row['worktree_path'])} if row.get('worktree_path') else {}), 'server': _locator(root),
            'install': str(install), 'runtime': _runtime(install), 'command_id': 'larj-delete-' + str(uuid.uuid4()),
            'provider_history': 'preserved', 'worktree_policy': 'detach_and_preserve', 'deletion_semantics': 't3_soft_delete'}


def _candidate(evidence):
    root = Path(evidence['root'])
    tid = evidence['thread_id']
    return CandidateAction(f'{KIND}:{digest([canonical_path(root),tid])[:32]}', ActionKind(KIND),
        TargetRef(storage_id_for_path(root),tid), RiskLevel.HIGH, True, None,
        ActionImpact(owner_client='t3code',owner_process_root=str(root),external_engine=engine(evidence['before']['default_provider']),
                     external_storage_root=str(root),affected_thread_ids=(tid,),resource_path=str(root),
                     external_action_payload={'t3code_server_evidence':evidence}), digest(evidence),
        requires_explicit_selection=True,resource_kind='t3code_thread')


def context_for(coordinator, evidence):
    base = coordinator.service.prepare_report(ScanReport(),active_adapters=(),platforms=('t3code',))
    action = _candidate(evidence)
    storage = StorageLocation(action.target.storage_id,'T3 local server',Path(evidence['root']),scan_status=ScanStatus.OK)
    plan = replace(base.plan,actions=(action,),storages=(storage,),plan_fingerprint=digest(evidence))
    return replace(base,plan=plan,actions=coordinator.service.typed_actions(plan)),(),None,None,{},{}


def approved_evidence(document):
    scope, actions = document['scope'], document.get('actions',())
    if (scope.get('client') != 't3code' or scope.get('t3code_only') is not True or scope.get('paseo_only')
            or scope.get('engines') or scope.get('projects') or scope.get('all_projects')
            or len(actions) != 1 or actions[0].get('kind') != KIND):
        raise T3ServerError('t3code_server_plan_scope_invalid')
    action = actions[0]
    e = action['impact']['external_action_payload']['t3code_server_evidence']
    local_root(e['root']); identifier(e['thread_id'])
    if (e['schema_version'] != SCHEMA or list(scope['record_ids']) != [e['thread_id']]
            or e['install'] != str(_bundled_install()) or e['server']['serverVersion'] not in VERSIONS
            or e['before']['thread_id'] != e['thread_id'] or e['before'].get('deleted_at') is not None
            or e['provider_history'] != 'preserved' or e['worktree_policy'] != 'detach_and_preserve'
            or e['deletion_semantics'] != 't3_soft_delete'):
        raise T3ServerError('t3code_server_plan_evidence_invalid')
    try:
        if not e['command_id'].startswith('larj-delete-') or str(uuid.UUID(e['command_id'][12:])) != e['command_id'][12:]:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise T3ServerError('t3code_server_command_identity_invalid') from None
    candidate = _candidate(e)
    if (action['action_id'] != candidate.action_id or action['target'] != candidate.target.to_dict()
            or [(s['storage_id'],s['path']) for s in document['storages']] != [(candidate.target.storage_id,e['root'])]
            or len(document['child_batches']) != 1 or document['child_batches'][0]['mutation_family'] != KIND):
        raise T3ServerError('t3code_server_plan_target_invalid')
    return e


def require_absent(evidence, timeout=0):
    # Cold local evidence is authoritative even if the server has since stopped.
    _check_identity(evidence)
    deadline = time.monotonic() + timeout
    while True:
        row = _snapshot(evidence['root'],evidence['thread_id'])
        if (row.get('deleted_at') is not None and row.get('worktree_path') is None and not row['active'] and not row['live_sessions']
                and row['native_refs'] == evidence['before']['native_refs']
                and all(x['status'] in ('succeeded','cancelled') for x in row['effects'])):
            return
        if time.monotonic() >= deadline:
            raise T3ServerError('t3code_server_delete_after_unverified')
        time.sleep(.1)


def execute(document, action, *, checkpoint, timeout, plan):
    from .execution import ExecutionOutcome
    from .t3code_cleanup import T3CodeCleanupResult
    evidence = approved_evidence(document)
    checkpoint('guard_started',action,None)
    _check_identity(evidence)
    if _locator(evidence['root']) != evidence['server'] or _snapshot(evidence['root'],evidence['thread_id']) != evidence['before']:
        raise T3ServerError('t3code_approved_thread_or_server_changed')
    tid, cid = evidence['thread_id'], evidence['command_id']
    checkpoint('mutation_started',action,None)
    if evidence.get('worktree'):
        _rpc(evidence,[{'type':'thread.metadata.update','threadId':tid,'commandId':cid+'-preserve-worktree',
                         'worktreePath':None,'expectedWorktreePath':evidence['before'].get('worktree_path'),
                         'expectedBranch':evidence['before'].get('branch')}],timeout)
        detached = _snapshot(evidence['root'],tid)
        if detached.get('worktree_path') is not None or detached['active'] or detached['native_refs'] != evidence['before']['native_refs']:
            raise T3ServerError('t3code_worktree_detach_unverified')
        _check_identity(evidence)
    _rpc(evidence,[{'type':'thread.delete','threadId':tid,'commandId':cid}],timeout)
    require_absent(evidence,timeout)
    result = T3CodeCleanupResult((tid,))
    checkpoint('verified',action,result)
    return ExecutionOutcome(KIND,(action,),plan,t3code_cleanup=result)
