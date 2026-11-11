"""Independent, offline T3 desktop replica cleanup. Native opens are copy-only.

Run with python -m local_agent_manager.t3code_desktop_cleanup --help.
A persistent attempt admits one installation only. Recovery installs its sealed
cold after image and never replays the native transaction or original operation.
"""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from . import frozen_files as ff, t3code_lifecycle as lifecycle
from .office_leveldb import _environment
from .operation_store import atomic_write_json, write_new_json, strict_json_load, plan_sha256
from .mutation_guard import mutation_roots
from .t3code_store import ID, fail

SCHEMA = 'lam.t3-desktop-residual.v1'
LEVEL = 'IndexedDB/t3code_app_0.indexeddb.leveldb'
BLOB = 'IndexedDB/t3code_app_0.indexeddb.blob'
FAMILIES = (LEVEL, BLOB)
CODE = Path(__file__).with_name('leveldb_runtime')
HELPERS = ('t3code_browser.cjs', 't3code_cache.cjs', 't3code_indexeddb.cjs', 'idb_audit.cjs', 'idb_keys.cjs', 'physical.cjs')


@contextmanager
def held_lifecycle(evidence):
    current = lifecycle.current_boundary(evidence['root'])
    if current is not None:
        if (current['lifecycle'] != evidence['lifecycle']
                or current['binding_manifest'] != evidence['binding_manifest']):
            fail('desktop_lifecycle_binding_changed')
        yield
    else:
        with lifecycle.hold(evidence):
            yield


def source_files(root):
    root = Path(root)
    paths = []
    for relative in FAMILIES:
        folder = ff.checked_path(root, relative)
        if not folder.exists():
            continue
        ff._plain(folder.lstat(), directory=True)
        for path in sorted(folder.rglob('*')):
            ff._plain(path.lstat(), directory=path.is_dir())
            if path.is_file():
                if path.relative_to(root).as_posix() == LEVEL + '/LOCK':
                    if path.stat().st_size:
                        fail('desktop_lock_schema_unqualified')
                else:
                    paths.append(path.relative_to(root).as_posix())
    return [ff.read_file(root, name)[0] for name in paths]


def contents(files):
    return [(f['path'], f['size'], f['sha256']) for f in files]


def copy_source(root, target, files):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for item in files:
        proof, body = ff.read_file(Path(root), item['path'], content=True)
        if proof != item:
            fail('desktop_source_changed')
        path = ff.checked_path(target, item['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    if source_files(root) != files or contents(source_files(target)) != contents(files):
        fail('desktop_source_changed')
    level = target / LEVEL
    if level.exists():
        (level / 'LOCK').touch(exist_ok=False)


def runtime_files(root, service=None):
    root = Path(root)
    paths = sorted([p for p in root.iterdir() if p.is_file() and
                    (p.suffix in ('.dll', '.pak', '.bin', '.dat') or p.name == 'T3 Code (Nightly).exe')]
                   + list((root / 'locales').glob('*.pak')))
    if not any(p.name == 'T3 Code (Nightly).exe' for p in paths):
        fail('desktop_runtime_unqualified')
    result = []
    for path in paths:
        proof = (lifecycle.read_binary(service, path) if service and path.suffix == '.exe'
                 else ff.read_file(path.parent, path.name)[0])
        result.append({'path': path.relative_to(root).as_posix(), 'sha256': proof['sha256'], 'size': proof['size']})
    return result


@contextmanager
def isolated_runtime(installed, *, expected=None, service=None):
    """Copy only Electron distribution files. Never copy any installed app code."""
    before = runtime_files(installed, service)
    if expected is not None and before != expected:
        fail('desktop_runtime_changed')
    with tempfile.TemporaryDirectory(prefix='lam-t3-electron-') as raw:
        root = Path(raw)
        for item in before:
            source = Path(installed) / item['path']
            target = root / ('electron.exe' if source.suffix == '.exe' else item['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            if service and source.suffix == '.exe' and lifecycle.current_boundary(service):
                _, body = lifecycle.read_binary(service, source, content=True)
                target.write_bytes(body)
            else:
                shutil.copyfile(source, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != item['sha256']:
                fail('desktop_runtime_changed')
        app = root / 'resources/app'
        app.mkdir(parents=True)
        for name in HELPERS:
            shutil.copyfile(CODE / name, app / name)
        (app / 'package.json').write_text(json.dumps({'name': 'lam-isolated-idb', 'version': '1.0.0', 'main': 't3code_browser.cjs'}))
        if runtime_files(installed, service) != before:
            fail('desktop_runtime_changed')
        yield root


def run_helper(runtime, request, *, native=False):
    with tempfile.TemporaryDirectory(prefix='lam-t3-control-') as raw:
        control = Path(raw)
        request = {**request, 'controlPath': str(control / 'userData')}
        path = control / 'request.json'
        path.write_text(json.dumps(request), encoding='utf8')
        env = _environment()
        command = [str(runtime / 'electron.exe')]
        if not native:
            env['ELECTRON_RUN_AS_NODE'] = '1'
            command.append(str(runtime / 'resources/app/t3code_indexeddb.cjs'))
        command.append(str(path))
        # A timed-out helper must not outlive its disposable source. Windows
        # Job ownership makes teardown independent of cooperative app shutdown.
        from .orca_runtime import _WindowsJob
        from . import orca_runtime_launcher
        job = _WindowsJob('Local\\lam-t3-copy-' + uuid.uuid4().hex)
        env['LARJ_JOB_HANDLE'] = str(int(job.handle))
        for name in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
            folder = control / name.lower()
            folder.mkdir()
            env[name] = str(folder)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': [int(job.handle)]}
        try:
            process = subprocess.Popen([sys.executable, '-I', orca_runtime_launcher.__file__, *command],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=control,
                startupinfo=startup, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                stdout, _ = process.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                job.close()
                process.communicate(timeout=10)
                fail('desktop_helper_timeout')
        finally:
            job.close()
        lines = [line for line in stdout.splitlines() if line.startswith(b'{')]
        if len(lines) != 1:
            fail('desktop_helper_response_invalid')
        result = json.loads(lines[0])
        if process.returncode or result.get('status') != 'verified':
            fail('desktop_' + result.get('code', 'helper_failed'))
        return result


def observe(runtime, session, environment, ids, *, native=False, **options):
    request = {'environment': environment, 'ids': ids, **options}
    if native:
        request.update(sessionPath=str(session), mode=options.pop('mode', 'inspect'))
    else:
        request.update(leveldb=str(session / LEVEL), blob=str(session / BLOB))
    return run_helper(runtime, request, native=native)


def freeze(profile, runtime, environment, ids):
    if (not ID.fullmatch(environment) or not ids or any(not ID.fullmatch(i) for i in ids)
            or len(ids) != len(set(ids))):
        fail('desktop_scope_invalid')
    before = source_files(profile)
    if not before or not (Path(profile) / LEVEL).exists():
        fail('desktop_family_missing')
    with tempfile.TemporaryDirectory(prefix='lam-t3-freeze-') as raw:
        stage = Path(raw) / 'session'
        copy_source(profile, stage, before)
        physical = observe(runtime, stage, environment, ids)
        native = observe(runtime, stage, environment, ids, native=True)
        if physical['counts'] != native['counts']:
            fail('desktop_native_projection_mismatch')
    return {'root': str(Path(profile).resolve()), 'environment': environment, 'ids': sorted(ids),
            'files': before, 'physical': physical, 'native': native}


@contextmanager
def prepared(evidence, runtime):
    with tempfile.TemporaryDirectory(prefix='lam-t3-prepared-') as raw:
        root = Path(raw)
        stage = root / 'transaction'
        copy_source(Path(evidence['root']), stage, evidence['files'])
        before = observe(runtime, stage, evidence['environment'], evidence['ids'], native=True)
        if before != evidence['native']:
            fail('desktop_frozen_state_changed')
        observe(runtime, stage, evidence['environment'], evidence['ids'], native=True, mode='apply',
                beforeSha256=before['beforeSha256'], afterSha256=before['expectedAfterSha256'])
        # Native transaction owns all external references. Compact the complete
        # cold logical owner graph to a new family, including only live blobs.
        compact = root / 'compact'
        (compact / 'IndexedDB').mkdir(parents=True)
        proof = observe(runtime, stage, evidence['environment'], evidence['ids'],
            output=str(compact / LEVEL), outputBlob=str(compact / BLOB))
        if any(proof['counts'].values()):
            fail('desktop_residual_after_transaction')
        cache = next(db['db'] for db in proof['databaseNames'] if db['name'] == 't3code:connection-runtime')
        store_ids = {s['name']: s['id'] for s in proof['stores'] if s['db'] == cache}
        changed = {(cache, store_ids[a['store']], a['key']) for a in before['actions']}
        def retained(rows):
            return [r for r in rows if (r['db'], r['store'], r['key']) not in changed]
        if retained(proof['dataRows']) != retained(evidence['physical']['dataRows']):
            fail('desktop_retained_raw_records_changed')
        after_files = source_files(compact)
        cold = root / 'cold'
        copy_source(compact, cold, after_files)
        recovered = observe(runtime, cold, evidence['environment'], evidence['ids'], native=True)
        if (recovered['beforeSha256'] != before['expectedAfterSha256']
                or recovered['beforeSha256'] != recovered['afterSha256'] or any(recovered['counts'].values())):
            fail('desktop_cold_recovery_unverified')
        yield compact, after_files, proof


def default_runtime():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'Programs/t3code'


def require_absent(profile, environment, ids):
    """Exact gate for historical plans. This cannot add any mutation to them."""
    try:
        admission(profile)
        files = source_files(profile)
        if not files:
            if any((Path(profile) / rel).exists() for rel in FAMILIES):
                fail('desktop_indexeddb_unqualified')
            return
        if not (Path(profile) / LEVEL / 'CURRENT').is_file():
            fail('desktop_indexeddb_unqualified')
        boundary = lifecycle._active.get()
        with isolated_runtime(default_runtime(), service=boundary['root'] if boundary else None) as runtime, tempfile.TemporaryDirectory(prefix='lam-t3-gate-') as raw:
            stage = Path(raw)
            copy_source(profile, stage, files)
            value = observe(runtime, stage, environment, ids)
            native = observe(runtime, stage, environment, ids, native=True)
        if any(value['counts'].values()) or any(native['counts'].values()):
            fail('desktop_indexeddb_residual')
    except Exception as exc:
        if getattr(exc, 'kind', '') == 't3code_desktop_indexeddb_residual':
            raise
        fail('desktop_indexeddb_unqualified')


def helpers_fingerprint():
    paths = [CODE / name for name in HELPERS]
    paths.extend(CODE.parent / name for name in (
        't3code_desktop_cleanup.py', 't3code_lifecycle.py', 'frozen_files.py',
        'windows_held_files.py', 'mutation_guard.py', 'operation_store.py',
        'orca_runtime.py', 'orca_runtime_launcher.py', 'office_leveldb.py'))
    return {str(path.relative_to(CODE.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def journal_root(profile):
    return ff.checked_path(Path(profile), '.local-agent-record-janitor/desktop-cleanups')


def admission(profile, *, own=None):
    directory = journal_root(profile)
    if not directory.exists():
        return
    ff._plain(directory.lstat(), directory=True)
    for child in directory.iterdir():
        ff._plain(child.lstat(), directory=True)
        if child.name == own:
            continue
        state = strict_json_load(ff.checked_path(child, 'state.json'))
        if state.get('phase') not in ('complete', 'not_applied'):
            fail('desktop_sibling_unknown')


def service_absence(service, ids):
    """Lease existing SQLite files, never create WAL/SHM during observation."""
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    path = Path(service) / 'userdata/statev2.sqlite'
    wal, shm = Path(str(path) + '-wal'), Path(str(path) + '-shm')
    has_wal = wal.exists()
    if has_wal != shm.exists():
        fail('desktop_sqlite_sidecar_state_unknown')
    handles = []
    try:
        for member in ([path, wal, shm] if has_wal else [path]):
            ff._plain(member.lstat())
            # Without WAL, deny new writes for the whole immutable read.
            handle = kernel.CreateFileW(str(member), 0x80000000, 3 if has_wal else 1, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                fail('desktop_sqlite_lease_unavailable')
            handles.append(handle)
        uri = path.as_uri() + ('?mode=ro' if has_wal else '?mode=ro&immutable=1')
        with sqlite3.connect(uri, uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            schema = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            counts = {}
            for table, _ in schema:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = {r[1] for r in db.execute(f'PRAGMA table_info({quoted})')}
                column = 'thread_id' if 'thread_id' in columns else 'id' if table == 'projection_threads' else None
                if column:
                    counts[table] = [db.execute(f'SELECT count(*) FROM {quoted} WHERE "{column}"=?', (id,)).fetchone()[0] for id in ids]
            if 'projection_threads' not in counts or len(counts) < 20 or any(n for rows in counts.values() for n in rows):
                fail('desktop_service_records_present_or_unqualified')
        return {'schemaSha256': hashlib.sha256(json.dumps(schema).encode()).hexdigest(), 'counts': counts}
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def new_plan(source_path, profile, installed):
    original = strict_json_load(Path(source_path))
    from .t3code_cleanup import evidence_from_document
    old = evidence_from_document(original)
    if old is None or original.get('plan_sha256') != plan_sha256(original):
        fail('desktop_source_plan_invalid')
    from .record_identity import canonical_path
    matches = [d for d in old['binding_manifest']['desktop_profiles'] if canonical_path(d['root']) == canonical_path(profile)]
    if len(matches) != 1:
        fail('desktop_profile_not_bound')
    manifest = {**old['binding_manifest'], 'desktop_profiles': matches}
    lifecycle_evidence = {'root': old['root'], 'binding_manifest': manifest, 'lifecycle': lifecycle.freeze(manifest)}
    # Freeze only at the offline boundary. A new hash is reviewed before apply.
    with lifecycle.hold(lifecycle_evidence):
        admission(profile)
        absent = service_absence(old['root'], old['selected_thread_ids'])
        with isolated_runtime(installed, service=old['root']) as runtime:
            evidence = freeze(profile, runtime, manifest['environment_id'], old['selected_thread_ids'])
        runtime_proof = runtime_files(installed, old['root'])
    value = {'schema_version': SCHEMA, 'operation_id': 'desktop-' + uuid.uuid4().hex,
             'source_operation': original['operation_id'], 'source_plan_sha256': original['plan_sha256'],
             'source_file_sha256': hashlib.sha256(Path(source_path).read_bytes()).hexdigest(),
             'lifecycle': lifecycle_evidence, 'service_absence': absent, 'evidence': evidence,
             'runtime_root': str(Path(installed).resolve()), 'runtime_files': runtime_proof,
             'helpers': helpers_fingerprint()}
    value['plan_sha256'] = plan_sha256(value)
    return value


def new_full_plan(manifest, database, profile):
    """Freeze IDB before any native mutation, as part of a NEW full plan.

    Historical residual plans still require service absence. This distinct
    schema binds the exact service before/after transaction, not an old receipt.
    """
    from .record_identity import canonical_path
    if not any(canonical_path(p['root']) == canonical_path(profile) for p in manifest['desktop_profiles']):
        fail('desktop_profile_not_bound')
    life = {'root': manifest['profile_root'], 'binding_manifest': manifest,
            'lifecycle': lifecycle.freeze(manifest)}
    installed = default_runtime()
    with lifecycle.hold(life):
        admission(profile)
        from .t3code_database import remaining
        if not remaining(database):
            fail('desktop_full_service_before_required')
        with isolated_runtime(installed, service=life['root']) as runtime:
            evidence = freeze(profile, runtime, manifest['environment_id'], database['thread_ids'])
            # Qualify the complete write + cold image now, before any original
            # family/native write. The eventual installer repeats on its copy.
            with prepared(evidence, runtime):
                pass
        proof = runtime_files(installed, life['root'])
    value = {'schema_version': 'lam.t3-desktop-full.v1', 'operation_id': 'desktop-' + uuid.uuid4().hex,
             'lifecycle': life, 'service_transaction': database, 'evidence': evidence,
             'runtime_root': str(installed.resolve()), 'runtime_files': proof, 'helpers': helpers_fingerprint()}
    value['plan_sha256'] = plan_sha256(value)
    return value


def service_gate(plan):
    if plan['schema_version'] == 'lam.t3-desktop-full.v1':
        from .t3code_database import remaining
        database = plan['service_transaction']
        if (database['root'] != plan['lifecycle']['root']
                or database['thread_ids'] != plan['evidence']['ids']):
            fail('desktop_service_scope_changed')
        remaining(database)  # exact frozen before, or verified after; never mutate
    elif service_absence(plan['lifecycle']['root'], plan['evidence']['ids']) != plan['service_absence']:
        fail('desktop_service_schema_changed')


def require_bound_state(plan, profile, environment, ids):
    """Allow only a frozen before or cold-verified after for a NEW full plan."""
    root = validate_plan(plan)
    if (root.resolve() != Path(profile).resolve() or plan['evidence']['environment'] != environment
            or plan['evidence']['ids'] != sorted(ids) or plan['schema_version'] != 'lam.t3-desktop-full.v1'):
        fail('desktop_full_scope_changed')
    admission(profile, own=plan['operation_id'])
    current = source_files(profile)
    if current == plan['evidence']['files']:
        return
    state_path = journal_root(profile) / plan['operation_id'] / 'state.json'
    if state_path.exists():
        state = strict_json_load(state_path)
        result = state.get('result', {})
        if (state.get('phase') == 'complete' and state.get('plan_sha256') == plan['plan_sha256']
                and result.get('logical_after_sha256') == plan['evidence']['native']['expectedAfterSha256']
                and contents(current) == [tuple(i) for i in result.get('files', ())]):
            # This is exact receipt-bound state classification, not a new cold
            # verification. verify_plan still reopens the complete copy natively.
            return
    boundary = lifecycle.current_boundary(plan['lifecycle']['root'])
    with isolated_runtime(plan['runtime_root'], expected=plan['runtime_files'],
                          service=boundary['root'] if boundary else None) as runtime:
        check_after(plan, runtime)


def remaining_bound(plan):
    evidence = plan['evidence']
    require_bound_state(plan, evidence['root'], evidence['environment'], evidence['ids'])
    if source_files(evidence['root']) == evidence['files']:
        return sum(evidence['native']['counts'].values())
    return 0


def validate_plan(plan, authorized=None):
    if (plan.get('schema_version') not in {SCHEMA, 'lam.t3-desktop-full.v1'} or plan.get('plan_sha256') != plan_sha256(plan)
            or authorized is not None and authorized != plan['plan_sha256']
            or helpers_fingerprint() != plan['helpers']):
        fail('desktop_plan_or_helpers_changed')
    from .operation_store import validate_operation_id
    validate_operation_id(plan['operation_id'])
    evidence = plan['evidence']
    if (evidence['environment'] != plan['lifecycle']['binding_manifest']['environment_id']
            or evidence['ids'] != sorted(set(evidence['ids']))
            or any(not ID.fullmatch(i) for i in evidence['ids'])):
        fail('desktop_plan_scope_invalid')
    return Path(evidence['root'])


def check_after(plan, runtime, *, root=None):
    evidence = plan['evidence']
    profile = Path(root or evidence['root'])
    files = source_files(profile)
    with tempfile.TemporaryDirectory(prefix='lam-t3-verify-') as raw:
        stage = Path(raw)
        copy_source(profile, stage, files)
        physical = observe(runtime, stage, evidence['environment'], evidence['ids'])
        native = observe(runtime, stage, evidence['environment'], evidence['ids'], native=True)
    if any(physical['counts'].values()) or any(native['counts'].values()) or native['beforeSha256'] != evidence['native']['expectedAfterSha256']:
        fail('desktop_after_unverified')
    return {'logical_after_sha256': native['beforeSha256'], 'counts': physical['counts'],
            'owners': physical['owners'], 'files': contents(files)}


def install_image(profile, seal, after_files, *, before_files, phase_callback=lambda phase: None):
    """Whole-family replacement, including owner-qualified blob projection.

    Each unlink uses frozen_files' checked handle. No individual blob deletion
    is inferred from a target ID. This function never opens a live IDB engine.
    """
    if source_files(profile) != before_files or contents(source_files(seal)) != contents(after_files):
        fail('desktop_install_source_changed')
    targets = [ff.freeze_remove(profile, rel) for rel in FAMILIES if (profile / rel).exists()]
    for target in targets:
        ff.apply_remove(profile, target)
        phase_callback('family_removed')
    copy_source(seal, profile, source_files(seal))
    phase_callback('family_installed')


def apply_plan(plan, authorized, *, clients_closed=False, phase_callback=lambda phase: None):
    if not clients_closed:
        fail('desktop_clients_closed_required')
    profile = validate_plan(plan, authorized)
    directory = journal_root(profile) / plan['operation_id']
    with mutation_roots([profile]), held_lifecycle(plan['lifecycle']):
        admission(profile)
        if directory.exists():
            fail('desktop_attempt_not_replayable')
        service_gate(plan)
        with isolated_runtime(plan['runtime_root'], expected=plan['runtime_files'], service=plan['lifecycle']['root']) as runtime:
            # All transaction/rewrite failures are confined to disposable data.
            with prepared(plan['evidence'], runtime) as (stage, files, proof):
                directory.mkdir(parents=True)
                write_new_json(directory / 'plan.json', plan)
                state = {'schema_version': SCHEMA, 'plan_sha256': plan['plan_sha256'], 'phase': 'sealing',
                         'pid': os.getpid(), 'attempts': 1, 'staged_expected': contents(files)}
                atomic_write_json(directory / 'state.json', state)
                seal = directory / 'after'
                copy_source(stage, seal, files)
                after_files = source_files(seal)
                # Durable after-image ownership precedes the first original write.
                state.update(phase='installing', after_files=after_files, after_logical_sha256=proof['logicalSha256'])
                atomic_write_json(directory / 'state.json', state)
                phase_callback('before_install')
                install_image(profile, seal, after_files, before_files=plan['evidence']['files'], phase_callback=phase_callback)
                verified = check_after(plan, runtime)
                state.update(phase='verified', result=verified)
                atomic_write_json(directory / 'state.json', state)
                ff.apply_remove(directory, ff.freeze_remove(directory, 'after'))
                state['phase'] = 'complete'
                atomic_write_json(directory / 'state.json', state)
                return {'goal_status': 'complete', 'goal_satisfied': True, 'operation_id': plan['operation_id'], 'result': verified}


def status_plan(plan):
    profile = validate_plan(plan)
    directory = journal_root(profile) / plan['operation_id']
    if not directory.exists():
        return {'goal_status': 'ready', 'goal_satisfied': False, 'operation_id': plan['operation_id'], 'attempts': 0}
    state = strict_json_load(directory / 'state.json')
    if state.get('plan_sha256') != plan['plan_sha256']:
        fail('desktop_journal_binding_changed')
    current_matches = (state['phase'] == 'complete' and
                       contents(source_files(profile)) == [tuple(i) for i in state['result']['files']])
    return {'goal_status': 'complete' if current_matches else 'blocked' if state['phase'] == 'not_applied' else 'unknown',
            'goal_satisfied': current_matches, 'operation_id': plan['operation_id'],
            'phase': state['phase'], 'attempts': state['attempts']}


def verify_plan(plan, *, clients_closed=False, recover=False):
    if not clients_closed:
        fail('desktop_clients_closed_required')
    profile = validate_plan(plan)
    directory = journal_root(profile) / plan['operation_id']
    with mutation_roots([profile]), held_lifecycle(plan['lifecycle']):
        admission(profile, own=plan['operation_id'])
        state = strict_json_load(directory / 'state.json')
        if state.get('plan_sha256') != plan['plan_sha256']:
            fail('desktop_journal_binding_changed')
        service_gate(plan)
        with isolated_runtime(plan['runtime_root'], expected=plan['runtime_files'], service=plan['lifecycle']['root']) as runtime:
            seal = directory / 'after'
            if state['phase'] == 'sealing':
                if source_files(profile) != plan['evidence']['files']:
                    fail('desktop_preinstall_state_changed')
                if seal.exists():
                    expected = {tuple(item) for item in state['staged_expected']}
                    if any(item not in expected for item in contents(source_files(seal))):
                        fail('desktop_partial_seal_changed')
                    ff.apply_remove(directory, ff.freeze_remove(directory, 'after'))
                state['phase'] = 'not_applied'
                atomic_write_json(directory / 'state.json', state)
                return {'goal_status': 'blocked', 'goal_satisfied': False,
                        'operation_id': plan['operation_id'], 'mutation_started': False}
            try:
                verified = check_after(plan, runtime)
            except (ValueError, OSError):
                if not recover or state['phase'] != 'installing':
                    raise
                after = state['after_files']
                if source_files(seal) != after:
                    fail('desktop_sealed_after_changed')
                # Only an exact before/after subset may be replaced during
                # recovery. The native transaction is never called here.
                current = source_files(profile)
                allowed = {(i['path'], i['size'], i['sha256']) for i in plan['evidence']['files'] + after}
                for item in current:
                    signature = (item['path'], item['size'], item['sha256'])
                    if signature in allowed:
                        continue
                    target = next((f for f in after if f['path'] == item['path']), None)
                    if target is None or item['size'] >= target['size']:
                        fail('desktop_recovery_source_changed')
                    _, body = ff.read_file(seal, item['path'], content=True)
                    # A crash inside the one admitted file copy may leave an
                    # exact prefix of the sealed after member, never new bytes.
                    if hashlib.sha256(body[:item['size']]).hexdigest() != item['sha256']:
                        fail('desktop_recovery_source_changed')
                check_after(plan, runtime, root=seal)
                install_image(profile, seal, after, before_files=current)
                verified = check_after(plan, runtime)
            state.update(phase='verified', result=verified)
            atomic_write_json(directory / 'state.json', state)
            if seal.exists():
                allowed = set(contents(state['after_files']))
                if any(item not in allowed for item in contents(source_files(seal))):
                    fail('desktop_sealed_after_changed')
                ff.apply_remove(directory, ff.freeze_remove(directory, 'after'))
            state['phase'] = 'complete'
            atomic_write_json(directory / 'state.json', state)
            return {'goal_status': 'complete', 'goal_satisfied': True, 'operation_id': plan['operation_id'], 'result': verified}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--source-plan', required=True)
    p.add_argument('--profile', required=True)
    p.add_argument('--runtime-root', default=str(default_runtime()))
    p.add_argument('--plan', required=True)
    p.add_argument('--clients-closed', action='store_true', required=True)
    for command in ('apply', 'status', 'verify', 'recover'):
        p = sub.add_parser(command)
        p.add_argument('--plan', required=True)
        if command in ('apply', 'recover'):
            p.add_argument('--authorized-plan-sha256', required=True)
        if command != 'status':
            p.add_argument('--clients-closed', action='store_true', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            plan = new_plan(args.source_plan, args.profile, args.runtime_root)
            write_new_json(Path(args.plan), plan)
            result = {'goal_status': 'ready', 'goal_satisfied': False, 'plan_sha256': plan['plan_sha256'],
                      'operation_id': plan['operation_id'], 'counts': plan['evidence']['native']['counts']}
        else:
            plan = strict_json_load(Path(args.plan))
            if args.command == 'status':
                result = status_plan(plan)
            elif args.command == 'apply':
                result = apply_plan(plan, args.authorized_plan_sha256, clients_closed=args.clients_closed)
            else:
                if args.command == 'recover':
                    validate_plan(plan, args.authorized_plan_sha256)
                result = verify_plan(plan, clients_closed=args.clients_closed, recover=args.command == 'recover')
        print(json.dumps(result))
        return 0
    except Exception as exc:
        code = getattr(exc, 'kind', 'desktop_operation_failed')
        print(json.dumps({'goal_status': 'unknown' if args.command != 'plan' else 'blocked',
                          'goal_satisfied': False, 'blocker_code': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
