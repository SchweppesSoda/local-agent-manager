"""Qualified T3-managed ACP local session files, never CLI profiles.

The native runtime does not advertise session/delete. Removal is subordinate
to the T3 full-cleanup ticket and held startup/runtime exclusion. Unknown
layouts and opaque parent/subtrajectory relationships fail closed.
"""
from contextlib import closing
import copy
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from . import frozen_files
from .environment_config import antigravity_cli_command, _unique_object
from .record_identity import canonical_path
from .sqlite_utils import connect_readonly
from .t3code_cleanup_files import _names
from .t3code_store import fail, local_root

SCHEMA = 'larj.antigravity-acp-files.v2'
CONTRACT = json.loads(files(__package__).joinpath('antigravity_acp_schema.json').read_text())
RUNTIMES = ('agy_acp_server.exe', 'localharness_external.exe')
SUFFIXES = ('.db', '.db-wal', '.db-shm', '.db-journal', '.meta')


def _sql_tokens(sql):
    if sql is None:
        return None
    # Keep quoted values, identifiers and comments intact. Whitespace between
    # SQL tokens does not change the stored table/constraint contract.
    return re.findall(r"--[^\r\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|\w+|[^\s]", sql)


def schema_compatible(objects, contract):
    """Keep required tables, constraints and indexes while allowing new indexes.

    Additional tables, views or triggers can carry restore relationships.
    Their format needs explicit support before native cleanup can include it.
    """
    expected = {(row[0], row[1]): row for row in contract['objects']}
    actual = {(row[0], row[1]): row for row in objects}
    return (all(key in actual and actual[key][:3] == row[:3]
                and _sql_tokens(actual[key][3]) == _sql_tokens(row[3]) for key, row in expected.items())
            and all(row[0] == 'index' or (row[0], row[1]) in expected for row in objects))


def _root_entries(root):
    result = []
    for path in root.iterdir():
        if path.name in {'.local-agent-record-janitor', '.local-agent-manager'}:
            # The coordinator creates its resource lock/journal after plan.
            frozen_files._plain(path.lstat(), directory=True)
        else:
            result.append(path.name)
    return sorted(result)


def _skills(root):
    """Preserve linked skill namespaces without traversing their external targets."""
    result=[]
    def visit(relative, depth=0):
        if depth > 128 or len(result) >= frozen_files.MAX_ENTRIES:
            fail('antigravity_skills_budget_exceeded')
        path=frozen_files.checked_path(root,relative)
        try:info=path.lstat()
        except FileNotFoundError:
            result.append({'path':relative,'absent':True});return
        proof={'path':relative,'identity':frozen_files._identity(info)}
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
            proof['link']=os.readlink(path)
            result.append(proof)
        elif stat.S_ISDIR(info.st_mode):
            frozen_files._plain(info,directory=True)
            proof['entries']=sorted(p.name for p in path.iterdir());result.append(proof)
            for name in proof['entries']:visit(relative+'/'+name,depth+1)
        else:
            proof,_=frozen_files.read_file(root,relative);result.append(proof)
    visit('skills')
    return result


def native_id(value):
    try:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        fail('antigravity_native_id_unqualified')
    return value


def check_binding(manifest, binding):
    instance = binding.get('provider_instance_id')
    if not isinstance(instance, str) or not instance:
        fail('antigravity_instance_required')
    expected = Path(manifest['profile_root']) / 'userdata/providers/antigravity' / hashlib.sha256(instance.encode()).hexdigest() / 'antigravity-acp'
    if canonical_path(binding['root']) != canonical_path(expected):
        fail('antigravity_instance_root_mismatch')
    binaries = binding.get('acp_runtime_binaries')
    if (not isinstance(binaries, list) or len(binaries) != 2
            or {Path(p).name for p in binaries} != set(RUNTIMES)
            or len({str(Path(p).parent) for p in binaries}) != 1
            or not set(binaries) <= set(manifest['runtime_binaries'])):
        fail('antigravity_runtime_binding_required')
    for path in binaries:
        local_root(path)
    return expected


def runtime(binding, profile_root):
    from .t3code_lifecycle import current_boundary, read_binary
    boundary = current_boundary(profile_root)
    result = []
    for name in sorted(RUNTIMES):
        binary = Path(next(p for p in binding['acp_runtime_binaries'] if Path(p).name == name))
        if boundary:
            # Recheck through the lifecycle's original exclusive handle.
            item = next((i for i in boundary['lifecycle']['binaries']
                         if canonical_path(Path(i['root']) / i['file']['path']) == canonical_path(binary)), None)
            if item is None:
                fail('antigravity_runtime_not_held')
            proof = read_binary(profile_root, binary)
        else:
            proof, _ = frozen_files.read_file(binary.parent, name)
        if not proof['size']:
            fail('antigravity_runtime_file_unqualified')
        result.append({'root': str(binary.parent), 'file': proof})
    return result


def provider_selection(manifest, binding):
    """Freeze the selected instance; an installed old binary is not selection."""
    root = Path(manifest['profile_root'])
    proof, raw = frozen_files.read_file(root, 'userdata/settings.json', content=True, limit=256 * 1024)
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        instance = data['providerInstances'][binding['provider_instance_id']]
        config = instance['config']
        if instance['driver'] != 'antigravity' or not isinstance(config, dict):
            raise ValueError
        binary = config.get('binaryPath', '')
        if binary is None:
            binary = ''
        elif not isinstance(binary, str):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        fail('antigravity_provider_selection_unverified')
    binary = binary.strip()
    if antigravity_cli_command(binary):
        fail('antigravity_cli_acp_cleanup_unsupported')
    expected = next(Path(p) for p in binding['acp_runtime_binaries'] if Path(p).name == 'agy_acp_server.exe')
    result = {'settings': proof}
    if binary:
        if canonical_path(local_root(binary)) != canonical_path(expected):
            fail('antigravity_selected_runtime_mismatch')
    else:
        pointer, raw = frozen_files.read_file(root, 'tools/antigravity-acp/win32-x64/active.json',
                                             content=True, limit=16384)
        try:
            release = json.loads(raw, object_pairs_hook=_unique_object)['releaseId']
            if not isinstance(release, str) or len(release) != 64 or any(c not in '0123456789abcdef' for c in release):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            fail('antigravity_provider_selection_unverified')
        selected = root/'tools/antigravity-acp/win32-x64/versions'/release/'agy_acp_server.exe'
        if canonical_path(selected) != canonical_path(expected):
            fail('antigravity_selected_runtime_mismatch')
        result['managed_pointer'] = pointer
    return result


def _database(root, sid):
    # Even a read-only WAL open may create/update shared memory. Qualify an
    # exact private copy, including WAL, and never open the native database.
    proofs=[];total=0
    with tempfile.TemporaryDirectory(prefix='larj-acp-schema-') as directory:
        for suffix in SUFFIXES[:-1]:
            relative='conversations/'+sid+suffix
            path=frozen_files.checked_path(root,relative)
            if not path.exists() and suffix != '.db':
                continue
            proof,raw=frozen_files.read_file(root,relative,content=True,limit=frozen_files.MAX_BYTES-total)
            total+=len(raw);proofs.append(proof)
            (Path(directory)/(sid+suffix)).write_bytes(raw)
        with closing(connect_readonly(Path(directory)/(sid+'.db'))) as db:
            deadline=time.monotonic()+20
            db.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            db.execute('BEGIN')
            objects = [list(r) for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
            if not schema_compatible(objects, CONTRACT) or [tuple(r) for r in db.execute('PRAGMA quick_check')] != [('ok',)]:
                fail('antigravity_database_unqualified')
            if [tuple(r) for r in db.execute('SELECT trajectory_id,cascade_id,trajectory_type,source FROM trajectory_meta')] != [(sid, sid, 4, 0)]:
                fail('antigravity_database_identity_mismatch')
            # Opaque protobuf parent/child references require separate closure.
            if (db.execute('SELECT 1 FROM parent_references LIMIT 1').fetchone()
                    or db.execute('SELECT 1 FROM steps WHERE has_subtrajectory IS NOT 0 LIMIT 1').fetchone()):
                fail('antigravity_related_trajectory_unqualified')
    if any(frozen_files.read_file(root,p['path'])[0] != p for p in proofs):
        fail('antigravity_database_changed')


def freeze(manifest, binding, ids):
    root = check_binding(manifest, binding)
    selection = provider_selection(manifest, binding)
    ids = sorted({native_id(s) for s in ids})
    runtime_proof = runtime(binding, manifest['profile_root'])
    directories = [_names(root, name) for name in ('conversations', 'brain')]
    names = _root_entries(root)
    if set(names) - {'conversations', 'brain', 'settings.json', 'acp_token.json', 'skills'}:
        fail('antigravity_profile_layout_unqualified')
    sessions = set()
    for name in directories[0].get('entries', ()):
        sid = name[:36]
        native_id(sid)
        if name[36:] not in SUFFIXES:
            fail('antigravity_conversation_file_unqualified')
        sessions.add(sid)
    for sid in sessions:
        _, raw = frozen_files.read_file(root, 'conversations/' + sid + '.meta', content=True, limit=16384)
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'cwd'}:
            fail('antigravity_metadata_unqualified')
        local_root(value['cwd'])  # metadata confirms a local workspace; it is never deleted
        _database(root, sid)
    for name in directories[1].get('entries', ()):
        if native_id(name) not in sessions:
            fail('antigravity_artifact_owner_unproven')
    changes, preserved = [], []
    for directory in directories:
        for name in directory.get('entries', ()):
            relative = directory['path'] + '/' + name
            proof = frozen_files.freeze_remove(root, relative)
            (changes if name[:36] in ids else preserved).append(proof)
    for name in names:
        if name not in ('conversations', 'brain', 'skills'):
            frozen_files._plain((root/name).lstat())
            preserved.append(frozen_files.freeze_remove(root, name))
    result = {'schema_version': SCHEMA, 'root': str(root), 'ids': ids, 'runtime': runtime_proof,
              'provider_selection': selection,
              'root_identity': frozen_files._identity(root.stat()), 'root_entries': names,
              'directories': directories, 'changes': changes, 'preserved': preserved, 'skills': _skills(root)}
    remaining(result)
    return result


def validate(evidence, manifest, binding, ids):
    root = check_binding(manifest, binding)
    if (evidence.get('schema_version') != SCHEMA or evidence.get('root') != str(root)
            or evidence.get('ids') != sorted({native_id(s) for s in ids})
            or evidence.get('provider_selection') != provider_selection(manifest, binding)
            or evidence.get('runtime') != runtime(binding, manifest['profile_root'])):
        fail('antigravity_evidence_binding_changed')
    allowed = {f'conversations/{sid}{suffix}' for sid in ids for suffix in SUFFIXES}
    allowed.update('brain/' + sid for sid in ids)
    paths = [c['path'] for c in evidence['changes']]
    expected = {d['path']+'/'+name for d in evidence['directories'] for name in d.get('entries',()) if name[:36] in ids}
    if len(paths) != len(set(paths)) or set(paths) - allowed or set(paths) != expected:
        fail('antigravity_deletion_scope_changed')
    for change in evidence['changes']:
        base = change['path']
        if (change['kind'] != 'remove' or any(p['path'] != base and not p['path'].startswith(base + '/')
                for p in (*change['files'], *change['directories']))):
            fail('antigravity_deletion_scope_changed')


def remaining(evidence):
    root = Path(evidence['root'])
    for parent in (*reversed(root.parents), root):
        frozen_files._plain(parent.lstat(), directory=True)
    if frozen_files._identity(root.stat()) != evidence['root_identity'] or _root_entries(root) != evidence['root_entries']:
        fail('antigravity_profile_changed')
    if _skills(root) != evidence['skills']:
        fail('antigravity_preserved_skills_changed')
    missing, count = set(), 0
    for change in evidence['changes']:
        if frozen_files.satisfied(root, change):
            missing.add(change['path'])
        else:
            if frozen_files.freeze_remove(root, change['path']) != change:
                fail('antigravity_file_state_unknown')
            count += 1
    expected = copy.deepcopy(evidence['directories'])
    for directory in expected:
        if 'entries' in directory:
            directory['entries'] = [n for n in directory['entries'] if directory['path'] + '/' + n not in missing]
    if [_names(root, d['path']) for d in expected] != expected:
        fail('antigravity_session_closure_changed')
    for change in evidence['preserved']:
        if frozen_files.freeze_remove(root, change['path']) != change:
            fail('antigravity_preserved_file_changed')
    return count


def apply(evidence, *, phase_callback):
    from .t3code_cleanup import _ticket
    from .t3code_lifecycle import current_boundary
    ticket = _ticket.get()
    if (not ticket or not ticket[1] or current_boundary(ticket[0]['root']) is None
            or evidence not in [t.get('native_files') for t in ticket[0]['native_targets']]):
        fail('antigravity_execution_ticket_required')
    remaining(evidence)
    for change in evidence['changes']:
        phase_callback('mutation_started')
        frozen_files.apply_remove(Path(evidence['root']), change)
    if remaining(evidence):
        fail('antigravity_delete_unverified')
