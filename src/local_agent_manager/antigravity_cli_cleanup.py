"""Frozen Antigravity CLI local files; no ACP soft-delete or managed-home alias.

Qualification is by on-disk schema and identities, not a product version list.
Opaque trajectory relationships are blockers. Windows file leases, not an
advisory sidecar lock or a closed-client acknowledgement, exclude writers.
"""
from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
from importlib.resources import files
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from . import frozen_files as ff
from .antigravity_acp_cleanup import CONTRACT as SESSION_CONTRACT
from .environment_config import _unique_object
from .office_database import logical_hash
from .path_identity import is_local_absolute_locator
from .record_identity import canonical_path
from .t3code_cleanup_files import _names
from .windows_held_files import HeldFiles

SCHEMA = 'lam.antigravity-cli-files.v1'
SUMMARY = 'conversation_summaries.db'
CACHE = 'cache/last_conversations.json'
CONTRACT = json.loads(files(__package__).joinpath('antigravity_cli_schema.json').read_text(encoding='utf-8'))
SIDECARS = ('', '-wal', '-shm', '-journal')
# These are preserved, never authorized by a conversation ID. Unknown top-level
# storage (including legacy .pb histories) requires a new format qualification.
PRESERVED = {'installation_id', 'jetski_state.pbtxt', 'settings.json', 'config.json',
             'builtin', 'skills', 'implicit', 'log', 'crashes', 'cache', 'bin', 'mcp', 'updater'}
CONTROL = {'.local-agent-record-janitor', '.local-agent-manager'}
_ticket = ContextVar('antigravity_cli_writer_ticket', default=None)


@contextmanager
def execution_scope(evidence):
    """The coordinator enters only after its journal admission and approval."""
    token = _ticket.set(hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest())
    try:
        yield
    finally:
        _ticket.reset(token)


class CleanupError(ValueError):
    def __init__(self, code):
        self.kind = 'antigravity_cli_' + code
        super().__init__(self.kind)


def fail(code):
    raise CleanupError(code)


def native_id(value):
    try:
        parsed = uuid.UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        fail('session_id_invalid')
    return value


def root_path(value):
    path = Path(value).expanduser()
    if not is_local_absolute_locator(str(path)) or path != Path(os.path.abspath(path)):
        fail('root_invalid')
    for parent in (*reversed(path.parents), path):
        ff._plain(parent.lstat(), directory=True)
    return path


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: fail('json_invalid'))
    except (ValueError, UnicodeError, RecursionError):
        fail('json_invalid')


def cache_after(raw, ids):
    value = _json(raw)
    if not isinstance(value, dict) or len(value) > ff.MAX_ENTRIES:
        fail('resume_cache_unqualified')
    for workspace, sid in value.items():
        if not is_local_absolute_locator(workspace):
            fail('resume_cache_workspace_unqualified')
        native_id(sid)
    return (json.dumps({k: v for k, v in value.items() if v not in ids},
                       ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _objects(db):
    return [list(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]


def _check(db, contract):
    deadline = time.monotonic() + 20
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    if _objects(db) != contract['objects']:
        fail('database_schema_unqualified')
    if [tuple(r) for r in db.execute('PRAGMA quick_check')] != [('ok',)]:
        fail('database_corrupt')


@contextmanager
def private_database(root, relative):
    """Never let a metadata read initialize SHM in the user's WAL database."""
    proofs = []
    with tempfile.TemporaryDirectory(prefix='lam-agy-db-') as directory:
        target = Path(directory) / 'copy.db'
        total = 0
        for suffix in SIDECARS:
            path = ff.checked_path(root, relative + suffix)
            if not path.exists() and suffix:
                continue
            proof, raw = ff.read_file(root, relative + suffix, content=True, limit=ff.MAX_BYTES-total)
            total += len(raw)
            proofs.append(proof)
            if suffix == '-journal' and raw:
                fail('database_recovery_pending')
            if suffix != '-shm':
                Path(str(target)+suffix).write_bytes(raw)
        with closing(sqlite3.connect(target)) as db:
            db.row_factory = sqlite3.Row
            yield db, proofs, target
        if any(ff.read_file(root, p['path'])[0] != p for p in proofs):
            fail('database_changed')


def _rows(db):
    result = list(db.execute('SELECT * FROM conversation_summaries ORDER BY conversation_id').fetchmany(ff.MAX_ENTRIES+1))
    if len(result) > ff.MAX_ENTRIES:
        fail('summary_budget_exceeded')
    rows = {}
    for row in result:
        sid = native_id(row['conversation_id'])
        for field in ('parent_conversation_id', 'winning_conversation_id'):
            if row[field]:
                native_id(row[field])
        if type(row['nesting_depth']) is not int or row['nesting_depth'] < 0:
            fail('summary_relationship_unqualified')
        if row['app_data_dir'] not in ('', 'antigravity-cli'):
            fail('summary_external_store')
        if row['battle_id'] or row['winning_conversation_id'] or row['group_id']:
            fail('summary_group_relationship_unqualified')
        rows[sid] = row
    # A parent column is a known relationship, never inferred from titles,
    # matching workspaces, or substring matches in a transcript.
    for sid, row in rows.items():
        seen = {sid}
        parent = row['parent_conversation_id']
        depth = row['nesting_depth']
        while parent:
            if parent in seen or parent not in rows or depth < 1:
                fail('summary_parent_unresolved')
            seen.add(parent)
            if rows[parent]['nesting_depth'] != depth - 1:
                fail('summary_parent_unresolved')
            parent, depth = rows[parent]['parent_conversation_id'], depth-1
        if depth:
            fail('summary_parent_unresolved')
    return rows


def _closure(rows, requested):
    selected = set(requested)
    if not selected or not selected <= rows.keys():
        fail('selected_session_missing')
    while True:
        expanded = selected | {sid for sid, r in rows.items() if r['parent_conversation_id'] in selected}
        if expanded == selected:
            break
        selected = expanded
    if any(rows[sid]['parent_conversation_id'] and rows[sid]['parent_conversation_id'] not in selected for sid in selected):
        fail('unselected_parent_reference')
    return sorted(selected)


def _summary_transform(db, ids):
    db.execute('PRAGMA secure_delete=ON')
    db.executemany('DELETE FROM conversation_summaries WHERE conversation_id=?', [(sid,) for sid in ids])
    db.commit()
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('VACUUM')
    return logical_hash(db)


def _root_names(root):
    names = []
    for path in root.iterdir():
        if path.name in CONTROL:
            ff._plain(path.lstat(), directory=True)
        else:
            names.append(path.name)
    if len(names) > ff.MAX_ENTRIES:
        fail('profile_budget_exceeded')
    return sorted(names)


def _protect_reference(root, proof, ids):
    for entry in proof['files']:
        current, raw = ff.read_file(root, entry['path'], content=True)
        if current != entry:
            fail('preserved_file_changed')
        if any(s.encode() in raw or s in entry['path'] for s in ids):
            fail('unqualified_profile_restore_reference')


def _log_link(root):
    """Preserve only the CLI's relative diagnostic-log symlink, without following it."""
    path = root/'cli.log'
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not path.is_symlink():
        fail('log_link_unqualified')
    target = os.readlink(path)
    normalized = target.replace('\\', '/')
    if (not normalized.startswith('log/') or len(normalized.split('/')) != 2
            or normalized.split('/')[1] in ('', '.', '..') or ':' in normalized
            or any(ord(c) < 32 for c in normalized)):
        fail('log_link_unqualified')
    if ff._revision(path.lstat()) != ff._revision(before):
        fail('log_link_changed')
    return {'identity': ff._identity(before), 'mtime_ns': before.st_mtime_ns, 'target': target}


def freeze(root, ids, *, binary):
    from . import antigravity_cli_restore as restore
    root = root_path(root)
    requested = sorted({native_id(s) for s in ids})
    binary = Path(binary)
    root_path(binary.parent)
    runtime = {'root': str(binary.parent), 'file': ff.read_file(binary.parent, binary.name)[0]}
    names = _root_names(root)
    if set(names) - PRESERVED - {'conversations', 'brain', 'annotations', 'presence', 'cli.log',
                               restore.PROTO, *(SUMMARY+s for s in SIDECARS)}:
        fail('profile_layout_unqualified')
    log_link = _log_link(root)
    with private_database(root, SUMMARY) as (db, summary_files, _):
        _check(db, CONTRACT)
        rows = _rows(db)
        selected = _closure(rows, requested)
        # Raw protobuf is not used to infer ownership. A known other session
        # embedded in a remaining summary is conservatively protected.
        for sid, row in rows.items():
            if sid not in selected and any(s.encode() in (row['raw_summary'] or b'') for s in selected):
                fail('unselected_summary_restore_reference')
        summary_before = logical_hash(db)
        summary_after = _summary_transform(db, selected)
        parents = {sid: row['parent_conversation_id'] for sid, row in rows.items()}
        raw_hashes = {sid: hashlib.sha256(row['raw_summary'] or b'').hexdigest() for sid, row in rows.items()}
    directories = [_names(root, name) for name in ('conversations', 'brain', 'cache', 'annotations', 'presence')]
    sessions = set()
    for name in directories[0].get('entries', ()):
        sid = native_id(name[:36])
        if name[36:] not in tuple('.db'+s for s in SIDECARS):
            fail('conversation_layout_unqualified')
        sessions.add(sid)
    if sessions != set(rows):
        fail('summary_session_set_mismatch')
    for name in directories[1].get('entries', ()):
        if native_id(name) not in sessions:
            fail('artifact_owner_unproven')
    # Observed CLI annotations contain only a protobuf-text title field. Do
    # not treat unknown annotation fields or nonempty presence files as owned
    # disposable state. A matching SQL ID and exact native filename are both
    # required; no workspace/title matching grants ownership.
    for directory, suffix in ((directories[3], '.pbtxt'), (directories[4], '.lock')):
        for name in directory.get('entries', ()):
            if not name.endswith(suffix) or native_id(name[:-len(suffix)]) not in sessions:
                fail('auxiliary_session_owner_unproven')
            _, raw = ff.read_file(root, directory['path']+'/'+name, content=True, limit=ff.MAX_REWRITE_BYTES)
            if directory['path'] == 'annotations':
                try:
                    text = raw.decode('utf-8')
                except UnicodeError:
                    fail('annotation_format_unqualified')
                if not re.fullmatch(r'\s*title\s*:\s*"(?:[^"\\\r\n]|\\[^\r\n])*"\s*', text):
                    fail('annotation_format_unqualified')
            elif raw:
                fail('presence_format_unqualified')
    for sid in sorted(sessions):
        with private_database(root, 'conversations/'+sid+'.db') as (db, _, _):
            _check(db, SESSION_CONTRACT)
            identities = [tuple(r) for r in db.execute('SELECT * FROM trajectory_meta')]
            if (len(identities) != 1 or identities[0][1] != sid or identities[0][2] != 4
                    or type(identities[0][3]) is not int):
                fail('session_identity_mismatch')
            native_id(identities[0][0])
            # Protobuf parent and subtrajectory payloads have not been
            # qualified. A summary link alone cannot grant their deletion.
            if (db.execute('SELECT 1 FROM parent_references LIMIT 1').fetchone()
                    or db.execute('SELECT 1 FROM steps WHERE has_subtrajectory IS NOT 0 LIMIT 1').fetchone()):
                fail('opaque_trajectory_relationship')
            if sid not in selected:
                # Search privately across typed BLOB/TEXT cells. This is only
                # a blocker, never a positive ownership relationship.
                for table in ('steps', 'trajectory_metadata_blob', 'executor_metadata', 'gen_metadata', 'battle_mode_infos'):
                    for row in db.execute('SELECT * FROM '+table):
                        for cell in row:
                            raw = cell.encode() if isinstance(cell, str) else cell if isinstance(cell, bytes) else b''
                            if any(s.encode() in raw for s in selected):
                                fail('unselected_trajectory_restore_reference')
    changes, preserved = [], []
    for d in [*directories[:2], *directories[3:]]:
        for name in d.get('entries', ()):
            proof = ff.freeze_remove(root, d['path']+'/'+name)
            if d['path'] in {'brain', 'annotations'} and name[:36] not in selected:
                _protect_reference(root, proof, selected)
            (changes if name[:36] in selected else preserved).append(proof)
    cache, rewrites = None, []
    if restore.PROTO in names:
        rewrites.append(ff.freeze_rewrite(root, restore.PROTO, lambda raw: restore.proto_after(raw, selected, raw_hashes)))
    for name in directories[2].get('entries', ()):
        relative = 'cache/'+name
        if name == 'last_conversations.json':
            cache = ff.freeze_rewrite(root, relative, lambda raw: cache_after(raw, selected))
        elif relative == restore.METADATA:
            rewrites.append(ff.freeze_rewrite(root, relative, lambda raw: restore.metadata_after(raw, selected, raw_hashes)))
        else:
            if 'conversation' in name.casefold() or 'resume' in name.casefold():
                fail('resume_cache_layout_unqualified')
            proof = ff.freeze_remove(root, relative)
            _protect_reference(root, proof, selected)
            preserved.append(proof)
    for name in names:
        if name not in {'conversations', 'brain', 'cache', 'annotations', 'presence', 'cli.log',
                        restore.PROTO, *(SUMMARY+s for s in SIDECARS)}:
            proof = ff.freeze_remove(root, name)
            # Logs are diagnostics, not restoration sources; retained in full.
            if name not in {'log', 'crashes', 'builtin', 'skills'}:
                _protect_reference(root, proof, selected)
            preserved.append(proof)
    evidence = {'schema_version': SCHEMA, 'root': str(root), 'root_identity': ff._identity(root.stat()),
                'root_entries': names, 'requested_ids': requested, 'ids': selected,
                'parents': parents, 'runtime': runtime, 'summary_files': summary_files,
                'summary_before': summary_before, 'summary_after': summary_after,
                'summary_raw_hashes': raw_hashes, 'rewrites': rewrites,
                'log_link': log_link,
                'cache': cache, 'directories': directories, 'changes': changes, 'preserved': preserved}
    remaining(evidence)
    return evidence


def validate(evidence):
    from . import antigravity_cli_restore as restore
    if evidence.get('schema_version') != SCHEMA:
        fail('evidence_schema_invalid')
    root = root_path(evidence['root'])
    ids = sorted({native_id(s) for s in evidence['ids']})
    if ids != evidence['ids'] or not set(evidence['requested_ids']) <= set(ids):
        fail('scope_invalid')
    allowed = ({f'conversations/{s}.db{x}' for s in ids for x in SIDECARS} | {'brain/'+s for s in ids}
               | {f'annotations/{s}.pbtxt' for s in ids} | {f'presence/{s}.lock' for s in ids})
    paths = [c['path'] for c in evidence['changes']]
    if len(paths) != len(set(paths)) or set(paths) - allowed:
        fail('scope_invalid')
    for change in evidence['changes']:
        base = change['path']
        if change['kind'] != 'remove' or any(p['path'] != base and not p['path'].startswith(base+'/')
                                           for p in (*change['files'], *change['directories'])):
            fail('scope_invalid')
    if (any(p['path'] not in {SUMMARY+s for s in SIDECARS} for p in evidence['summary_files'])
            or not any(p['path'] == SUMMARY for p in evidence['summary_files'])
            or evidence['cache'] and evidence['cache']['before']['path'] != CACHE):
        fail('shared_scope_invalid')
    paths = [c['before']['path'] for c in evidence['rewrites']]
    if len(paths) != len(set(paths)) or set(paths) - {restore.PROTO, restore.METADATA}:
        fail('shared_scope_invalid')
    return root


def remaining(evidence):
    root = validate(evidence)
    if ff._identity(root.stat()) != evidence['root_identity']:
        fail('root_replaced')
    if _log_link(root) != evidence['log_link']:
        fail('log_link_changed')
    count = 0
    removed = set()
    for change in evidence['changes']:
        path = ff.checked_path(root, change['path'])
        if not path.exists():
            removed.add(change['path'])
        elif ff.freeze_remove(root, change['path']) != change:
            fail('session_state_unknown')
        else:
            count += 1
    with private_database(root, SUMMARY) as (db, _, _):
        _check(db, CONTRACT)
        actual = logical_hash(db)
        if actual not in (evidence['summary_before'], evidence['summary_after']):
            fail('summary_state_unknown')
        count += actual != evidence['summary_after']
        # No newly added child or summary may evade the frozen closure.
        if actual == evidence['summary_after'] and any(r['conversation_id'] in evidence['ids'] or
                r['parent_conversation_id'] in evidence['ids'] for r in db.execute('SELECT * FROM conversation_summaries')):
            fail('summary_delete_unverified')
    for change in [*evidence['rewrites'], *([evidence['cache']] if evidence['cache'] else [])]:
        current, _ = ff.read_file(root, change['before']['path'])
        if current['sha256'] != change['after_sha256'] or current['size'] != change['after_size']:
            if current != change['before']:
                fail('resume_cache_state_unknown')
            count += 1
    expected_names = [n for n in evidence['root_entries'] if n not in {p['path'] for p in evidence['summary_files']
                      if p['path'] != SUMMARY and not (root/p['path']).exists()}]
    if _root_names(root) != expected_names:
        fail('profile_layout_changed')
    for directory in evidence['directories']:
        expected = dict(directory)
        if 'entries' in expected:
            expected['entries'] = [n for n in expected['entries'] if directory['path']+'/'+n not in removed]
        if _names(root, directory['path']) != expected:
            fail('session_set_changed')
    for proof in evidence['preserved']:
        if ff.freeze_remove(root, proof['path']) != proof:
            fail('preserved_file_changed')
    if not count:
        for suffix in SIDECARS[1:]:
            if (root/(SUMMARY+suffix)).exists():
                fail('summary_sidecar_residual')
    return count


def require_closed(evidence, census=None):
    from .office_runtime import processes
    rows = (census or processes)()
    if not isinstance(rows, (tuple, list)) or len(rows) > 10000:
        fail('writer_census_unverified')
    binary = canonical_path(Path(evidence['runtime']['root'])/evidence['runtime']['file']['path'])
    for row in rows:
        if not isinstance(row, dict) or type(row.get('pid')) is not int:
            fail('writer_census_unverified')
        if row['pid'] == os.getpid():
            continue
        name = str(row.get('name') or '').casefold()
        if not name:
            fail('writer_census_unverified')
        executable = row.get('executable')
        if name in {'agy', 'agy.exe', 'antigravity', 'antigravity.exe', 'agy_acp_server.exe', 'localharness_external.exe'} or (
                executable and canonical_path(executable) == binary):
            fail('writer_running')


def discard_recovery(evidence, directory):
    """Remove only proved before images after exact native verification."""
    directory = Path(directory)
    if not directory.exists():
        return
    ff._plain(directory.lstat(), directory=True)
    proofs = [evidence['summary_files'][0], *([evidence['cache']['before']] if evidence['cache'] else []),
              *(change['before'] for change in evidence['rewrites'])]
    expected = {str(i)+'.before': proof for i, proof in enumerate(proofs)}
    present = {p.name for p in directory.iterdir()}
    if not present <= set(expected):
        fail('recovery_images_unqualified')
    for name, proof in expected.items():
        if name not in present:
            continue
        current, _ = ff.read_file(directory, name)
        if current['sha256'] != proof['sha256'] or current['size'] != proof['size']:
            fail('recovery_image_changed')
    for name in present:
        ff.apply_remove(directory, ff.freeze_remove(directory, name))
    directory.rmdir()


def apply(evidence, *, phase_callback, recovery_dir, census=None):
    """Execute once under a coordinator mutation ticket and exclusive handles.

    Shared-file before images survive uncertain failures. Recovery inspection
    never blindly retries an attempted writer or overwrites a changed store.
    """
    from .mutation_guard import mutation_guard, MutationScope
    from . import antigravity_cli_restore as restore
    root = validate(evidence)
    if _ticket.get() != hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest():
        fail('execution_ticket_required')
    if os.name != 'nt':
        fail('file_lease_platform_unqualified')
    with mutation_guard((MutationScope(root, None),)):
        require_closed(evidence, census)
        if not remaining(evidence):
            fail('already_changed_requires_verification')
        runtime = evidence['runtime']
        # Generate a complete replacement privately, including all unselected
        # typed cells. Do not open the native SQLite database for mutation.
        with private_database(root, SUMMARY) as (db, proofs, copy_path):
            if proofs != evidence['summary_files'] or logical_hash(db) != evidence['summary_before']:
                fail('summary_changed')
            if _summary_transform(db, evidence['ids']) != evidence['summary_after']:
                fail('summary_transform_changed')
            replacement = copy_path.read_bytes()
        shared = [(evidence['summary_files'][0], replacement)]
        if evidence['cache']:
            proof, raw = ff.read_file(root, CACHE, content=True)
            if proof != evidence['cache']['before']:
                fail('resume_cache_changed')
            shared.append((proof, cache_after(raw, evidence['ids'])))
        for change in evidence['rewrites']:
            proof, raw = ff.read_file(root, change['before']['path'], content=True)
            body = restore.transform(proof['path'], raw, evidence)
            if proof != change['before'] or hashlib.sha256(body).hexdigest() != change['after_sha256']:
                fail('restore_mirror_changed')
            shared.append((proof, body))
        recovery_dir = Path(recovery_dir)
        # The operation journal owns this private recovery directory. It is
        # not a discoverable conversation or a normal long-lived backup.
        shared_started = False
        with ExitStack() as stack:
            held = stack.enter_context(HeldFiles())
            from .antigravity_cli_joint import runtime_is_held
            if not runtime_is_held(evidence):
                held.acquire(Path(runtime['root']), runtime['file'])
            for proof, body in shared:
                held.acquire(root, proof, writable=True)
            removed = stack.enter_context(HeldFiles())
            for proof in evidence['summary_files'][1:]:
                removed.acquire(root, proof, delete=True)
            for change in evidence['changes']:
                for proof in change['files']:
                    removed.acquire(root, proof, delete=True)
            require_closed(evidence, census)
            # Leased files cannot be reopened, so verify preservation and
            # directory membership here using the frozen namespace only.
            if _root_names(root) != evidence['root_entries'] or any(_names(root, d['path']) != d for d in evidence['directories']):
                fail('profile_changed_before_mutation')
            if _log_link(root) != evidence['log_link']:
                fail('log_link_changed')
            for proof in evidence['preserved']:
                if ff.freeze_remove(root, proof['path']) != proof:
                    fail('preserved_file_changed')
            recovery_dir.mkdir(mode=0o700, exist_ok=False)
            originals = []
            for index, (proof, body) in enumerate(shared):
                _, original = held.read(root, proof['path'], content=True)
                backup = recovery_dir / (str(index)+'.before')
                with backup.open('xb') as handle:
                    handle.write(original); handle.flush(); os.fsync(handle.fileno())
                originals.append((proof, original))
            from .legacy_index import _fsync_directory
            _fsync_directory(recovery_dir)
            phase_callback('recovery_images_durable')
            try:
                for proof, body in shared:
                    phase_callback('mutation_started')
                    shared_started = True
                    held.replace(root, proof['path'], body, before=proof,
                                 after_sha256=hashlib.sha256(body).hexdigest())
                phase_callback('shared_files_written')
            except Exception:
                # A caught write failure has an unambiguous before image and
                # still-held handles. Restore without releasing exclusion.
                if shared_started:
                    for proof, original in originals:
                        current, _ = held.read(root, proof['path'])
                        held.replace(root, proof['path'], original, before=current, after_sha256=proof['sha256'])
                raise
            for proof in evidence['summary_files'][1:]:
                phase_callback('mutation_started')
                removed.dispose(root, proof['path'], before=proof)
            for change in evidence['changes']:
                for proof in change['files']:
                    phase_callback('mutation_started')
                    removed.dispose(root, proof['path'], before=proof)
            # Close pending-delete files while keeping the summary, cache and
            # runtime leases. Only frozen empty artifact directories follow.
            removed.__exit__(None, None, None)
            for change in evidence['changes']:
                for proof in reversed(change['directories']):
                    phase_callback('mutation_started')
                    ff._delete_frozen(root, proof, directory=True)
            for proof, body in shared:
                current, _ = held.read(root, proof['path'])
                if current['sha256'] != hashlib.sha256(body).hexdigest():
                    fail('shared_write_unverified')
            phase_callback('native_files_removed')
        if remaining(evidence):
            fail('delete_unverified')
        discard_recovery(evidence, recovery_dir)
