"""Cold recovery with actual discovery, independent runtimes and desktop stores.

Only host locations/census are injected. All leases, SQLite/LevelDB readers,
binding discovery, CLI and verification are production implementations.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import t3code_cleanup, antigravity_cli_joint as joint
from local_agent_manager.operation_store import strict_json_load
from local_agent_manager.t3code_bound_adapter import T3CodeBoundAdapter
from local_agent_manager.t3code_discovery import discover_bindings
from tests.test_joint_cli_recovery import CRASH
from tests import test_antigravity_cli_joint as joint_fixture
from tests.test_antigravity_cli_joint import FRONT, OTHER, KEEP, TARGET
from tests.orca_native_support import create_native_schema, add_native_record
from tests.t3code_support import add_thread, V2, create_store
from tests.test_t3code_localstorage import local_storage
from tests.native_recovery_support import initial_sources, reopen, NEW_NATIVE, LATER_NATIVE

NEW = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
CODEX_THREAD = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
CODEX_NATIVE = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'

COLD = r'''
import json, sys, traceback
from pathlib import Path
from unittest.mock import patch
from local_agent_manager.entrypoint import main
from local_agent_manager.operation_coordinator import OperationCoordinator
# Preserve the exact failure stack before the public API turns it into a
# structured unknown. This observer neither mocks nor changes verification.
original = OperationCoordinator._verify_operation_locked
def traced(self, **kwargs):
    try: return original(self, **kwargs)
    except BaseException:
        traceback.print_exc()
        raise
with patch('pathlib.Path.home',return_value=Path(sys.argv[2])), patch('local_agent_manager.office_runtime.processes',return_value=json.loads(sys.argv[5])), patch('local_agent_manager.t3code_server_delete._bundled_install',return_value=Path(sys.argv[4])), patch.object(OperationCoordinator,'_verify_operation_locked',traced):
    operation_id = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['operation_id']
    raise SystemExit(main(['operation','verify','--operation-id',operation_id,'--plan',sys.argv[1],
        '--recover-joint-reservations','--authorized-plan-sha256',sys.argv[3],'--json','--progress']))
'''


@unittest.skipUnless(os.name == 'nt', 'Windows held lifecycle')
class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.f = joint_fixture.JointTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.setup_client('t3code')
        # Existing non-owning frontend guard sources must also remain readable
        # through their original leases on every full verification pass.
        with closing(create_store(self.f.user/'.t3')) as db:
            add_thread(db, 'foreign-guard', native=KEEP, driver='antigravity')
        from tests.test_antigravity_cli_joint import paseo_agent
        paseo = self.f.user/'.paseo/agents'
        paseo.mkdir(parents=True)
        (paseo/'guard.json').write_text(json.dumps(paseo_agent('guard', KEEP, self.f.base)))
        # Current CLI summary layout includes the search schema and FTS index.
        from tests.test_antigravity_cli_cleanup import CliFormatTests
        summary = CliFormatTests('runTest')
        summary.root = self.f.native
        summary.search_index()
        initial_sources(self.f.native)
        self.install = self.f.base/'install'
        self.install.mkdir()
        self.binary = self.install/'T3 Code (Nightly).exe'
        self.binary.write_bytes(b'independent frontend, never executed')
        self.codex = self.f.base/'codex-home'
        self.codex.mkdir()
        create_native_schema(self.codex)
        add_native_record(self.codex, CODEX_NATIVE)
        self.codex_binary = self.f.base/'codex.exe'
        self.codex_binary.write_bytes(b'independent native runtime, never executed')
        self.desktop = self.f.base/'appdata/t3code-v2'
        local_storage(self.desktop, thread_id=FRONT)
        (self.f.t3/'userdata/environment-id').write_text('env-1')
        settings = self.f.t3/'userdata/settings.json'
        config = json.loads(settings.read_text())
        config['providerInstances']['codex'] = {'driver':'codex', 'config':{
            'homePath':str(self.codex), 'binaryPath':str(self.codex_binary)}}
        settings.write_text(json.dumps(config))
        with closing(sqlite3.connect(self.f.t3/'userdata/statev2.sqlite')) as db:
            add_thread(db, CODEX_THREAD, native=CODEX_NATIVE)
        host = patch('local_agent_manager.t3code_server_delete._bundled_install', return_value=self.install)
        host.start(); self.addCleanup(host.stop)
        self.adapter = T3CodeBoundAdapter(discover_bindings(self.f.t3))
        self.plan = self.f.plan('t3code', self.adapter)
        self.assertEqual(self.plan['goal_status'], 'ready', self.plan)
        self.document = strict_json_load(self.f.plan_path)
        self.evidence = t3code_cleanup.evidence_from_document(self.document)
        self.store, _ = joint.reservation(self.document, self.evidence, joint.targets(self.evidence)[0])
        result = subprocess.run([sys.executable, '-c', CRASH, str(self.f.plan_path), str(self.f.user)],
            capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 73, result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))
        self.assertTrue(self.store.lock_exists())
        reopen(self.f.native, KEEP)
        settings_copy = self.f.t3/'userdata/settings-reopened.tmp'
        settings_copy.write_bytes((self.f.t3/'userdata/settings.json').read_bytes())
        settings_copy.replace(self.f.t3/'userdata/settings.json')
        # A newly opened supervision thread, absent from frozen authorization,
        # must be discovered for protection in both cold verification processes.
        with closing(sqlite3.connect(self.f.t3/'userdata/statev2.sqlite')) as db:
            add_thread(db, NEW, native=CODEX_NATIVE)
        from local_agent_manager import t3code_localstorage as storage
        node, dependency, helper = storage.level.runtime()
        script = r'''
const path=require('node:path');const h=require(process.argv[1]);
const req=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const db=new(req('classic-level').ClassicLevel)(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer'});
(async()=>{await db.open();await db.put(Buffer.concat([Buffer.from('_t3code://app\0'),h.encodeString('opaque-settings')]),Buffer.from('REOPENED'));await db.close()})().catch(e=>{console.error(e);process.exitCode=1});
'''
        subprocess.run([str(node), '-e', script, str(helper), str(self.desktop/storage.RELATIVE)],
            cwd=dependency, env=storage.level._environment(dependency), check=True, capture_output=True, timeout=30)
        self.original_lock = self.store.lock_path.read_bytes()

    def cold(self, label, rows=()):
        result = subprocess.run([sys.executable, '-c', COLD, str(self.f.plan_path), str(self.f.user),
            self.plan['plan_sha256'], str(self.install), json.dumps(rows)], capture_output=True, timeout=120)
        # Optional external diagnostics keep receipts, never the temporary stores.
        if os.environ.get('LAM_TOPOLOGY_EVIDENCE'):
            directory = Path(os.environ['LAM_TOPOLOGY_EVIDENCE'])
            directory.mkdir(parents=True, exist_ok=True)
            (directory/(label+'.json')).write_bytes(result.stdout)
            (directory/(label+'.stderr')).write_bytes(result.stderr)
        return result, json.loads(result.stdout)

    def assert_blocked(self, label, rows=()):
        result, payload = self.cold(label, rows)
        self.assertNotEqual(result.returncode, 0, payload)
        self.assertEqual(payload['goal_status'], 'unknown', payload)
        self.assertEqual(self.store.lock_path.read_bytes(), self.original_lock)
        self.assertFalse(list(self.store.directory.glob('recovery-*.json')))
        return payload

    def test_complete_chain_and_second_cold_verify(self):
        result, payload = self.cold('first-cold')
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')+str(payload))
        self.assertEqual(payload['goal_status'], 'complete', payload)
        self.assertTrue(payload['goal_satisfied'])
        self.assertFalse(self.store.lock_exists())
        audit_path = next(self.store.directory.glob('recovery-*.json'))
        audit_raw = audit_path.read_bytes()
        audit = strict_json_load(audit_path)
        self.assertTrue(audit['goal_satisfied'])
        self.assertIn(NEW, audit['current_database']['new_thread_ids'])
        self.assertTrue(audit['current_desktop'])
        self.assertEqual(audit['current_native']['new_summary_ids'], [NEW_NATIVE])
        self.assertFalse(audit['current_native']['blockers'])
        with self.assertRaisesRegex(ValueError, 'log_link_changed'):
            native = __import__('local_agent_manager.antigravity_cli_cleanup', fromlist=['remaining'])
            native.remaining(joint.targets(self.evidence)[0]['native_files'])
        self.assertNotEqual(audit['current_provider_settings']['original']['identity'],
                            audit['current_provider_settings']['current']['identity'])
        self.assertEqual(audit['current_provider_settings']['original']['sha256'],
                         audit['current_provider_settings']['current']['sha256'])
        self.assertNotIn(NEW, self.evidence['selected_thread_ids'])
        # The ordinary strict old whole-DB hash is deliberately no longer
        # equal; only the admitted recovery scope can prove this evolved DB.
        self.assertFalse((self.f.native/'conversations'/(TARGET+'.db')).exists())
        self.assertTrue((self.f.native/'conversations'/(KEEP+'.db')).exists())
        with closing(sqlite3.connect(self.f.t3/'userdata/statev2.sqlite')) as db:
            self.assertEqual({r[0] for r in db.execute(f'SELECT thread_id FROM {V2}threads')}, {OTHER, CODEX_THREAD, NEW})
        reopen(self.f.native, KEEP, LATER_NATIVE)
        result, payload = self.cold('second-cold')
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')+str(payload))
        self.assertTrue(payload['goal_satisfied'])
        self.assertEqual(audit_path.read_bytes(), audit_raw)
        (self.f.native/'cache/unknown-resume.json').write_text('{}')
        result, payload = self.cold('danger-after-second')
        self.assertNotEqual(result.returncode, 0, payload)
        self.assertFalse(payload['goal_satisfied'])
        self.assertEqual(audit_path.read_bytes(), audit_raw)
        if os.environ.get('LAM_TOPOLOGY_EVIDENCE'):
            out = Path(os.environ['LAM_TOPOLOGY_EVIDENCE'])
            (out/'guarded-audit.json').write_bytes(audit_raw)
            (out/'guarded-preservation.json').write_text(json.dumps({
                'target_frontend_remaining':0, 'target_native_remaining':0,
                'unselected_missing':[], 'new_binding_is_protection_only':True,
                'foreign_t3_and_paseo_guards_leased':True, 'atomic_identical_settings_verified':True,
                'reservation_lock_released':True, 'two_cold_verifications_complete':True},indent=2))

    def test_changed_program_is_rejected(self):
        self.binary.write_bytes(b'changed real program')
        self.assert_blocked('changed-program')

    def test_changed_native_runtime_is_rejected(self):
        self.f.agy.write_bytes(b'changed native runtime')
        self.assert_blocked('changed-native-runtime')

    def test_active_frontend_is_rejected(self):
        self.assert_blocked('active-frontend', [{'pid':12345,'parent':0,'name':self.binary.name,
            'executable':str(self.binary),'command':'fixture'}])

    def test_third_party_exclusive_handle_is_rejected(self):
        from local_agent_manager.windows_held_files import exclusive_fd
        fd = exclusive_fd(self.binary)
        try: self.assert_blocked('third-party-exclusive')
        finally: os.close(fd)

    def test_guard_source_third_party_exclusive_handle_is_rejected(self):
        from local_agent_manager.windows_held_files import exclusive_fd
        fd = exclusive_fd(self.f.user/'.t3/userdata/statev2.sqlite')
        try: self.assert_blocked('guard-third-party-exclusive')
        finally: os.close(fd)

    def test_settings_change_is_rejected(self):
        with (self.f.t3/'userdata/settings.json').open('a') as stream: stream.write(' ')
        self.assert_blocked('changed-settings')

    def test_native_target_parent_fts_and_restore_references_block(self):
        from local_agent_manager import antigravity_cli_cleanup as native
        mutations = (
            ('summary-parent', 'UPDATE conversation_summaries SET parent_conversation_id=?,nesting_depth=1 WHERE conversation_id=?'),
            ('summary-reference', 'UPDATE conversation_summaries SET raw_summary=? WHERE conversation_id=?'),
            ('fts-reference', 'UPDATE conversations_fts SET user_body=? WHERE conversation_id=?'),
        )
        for label, sql in mutations:
            path = self.f.native/native.SUMMARY
            before = path.read_bytes()
            with closing(sqlite3.connect(path)) as db:
                db.execute(sql, (TARGET, NEW_NATIVE)); db.commit()
            self.assert_blocked(label)
            path.write_bytes(before)

    def test_native_target_file_reappearance_blocks(self):
        (self.f.native/'conversations'/(TARGET+'.db')).write_bytes(b'reappeared target')
        self.assert_blocked('native-target-file')

    def test_unknown_restore_source_and_opaque_implicit_churn_block(self):
        cases = ('cache/saved-resume.json', 'implicit/new.pb', 'unknown.pb')
        for relative in cases:
            path = self.f.native/relative
            path.write_bytes(b'opaque unknown restoration source')
            self.assert_blocked(relative.replace('/', '-'))
            path.unlink()
        (self.f.native/'implicit/original.pb').unlink()
        self.assert_blocked('implicit-missing-original')

    def test_unknown_mirror_fields_and_mismatch_block(self):
        from local_agent_manager import antigravity_cli_restore as restore
        path = self.f.native/restore.METADATA
        before = path.read_bytes()
        value = json.loads(before)
        value['conversations'][NEW_NATIVE]['summary']['CloudProject']['unknownResume'] = 'opaque'
        path.write_text(json.dumps(value))
        self.assert_blocked('unknown-metadata-nested-field')
        path.write_bytes(before)
        proto = self.f.native/restore.PROTO
        before = proto.read_bytes()
        proto.write_bytes(before+b'\x12\x00')
        self.assert_blocked('unknown-proto-field')
        proto.write_bytes(before.replace(b'synthetic summary', b'different summary', 1))
        self.assert_blocked('proto-summary-mismatch')

    def test_missing_or_corrupt_unselected_native_blocks(self):
        path = self.f.native/'conversations'/(KEEP+'.db')
        before = path.read_bytes()
        path.unlink()
        self.assert_blocked('native-missing-unselected')
        path.write_bytes(b'broken SQLite database')
        self.assert_blocked('native-corrupt-unselected')
        path.write_bytes(before)
        brain = self.f.native/'brain'/KEEP/'artifact.md'
        brain.unlink()
        self.assert_blocked('native-missing-unselected-artifact')

    def test_dangerous_diagnostic_link_and_unknown_onboarding_block(self):
        link = self.f.native/'cli.log'
        for target in ('../outside.log', 'log/../outside.log', 'log/cli.log:stream', 'log/CON', 'log/missing.log'):
            link.unlink(); link.symlink_to(target)
            self.assert_blocked('bad-diagnostic-link')
        link.unlink(); link.symlink_to('log/cli-'+NEW_NATIVE+'.log')
        path = self.f.native/'cache/onboarding.json'
        path.write_text(json.dumps({'onboardingComplete': True, 'savedResume': TARGET}))
        self.assert_blocked('unknown-onboarding-field')

    def test_current_native_file_handle_blocks_and_keeps_exclusion(self):
        from local_agent_manager.windows_held_files import exclusive_fd
        from local_agent_manager.joint_cli_recovery import recovery_scope, native_evolution
        path = self.f.native/'conversations'/(NEW_NATIVE+'.db')
        fd = exclusive_fd(path)
        try: self.assert_blocked('native-third-party-handle')
        finally: os.close(fd)
        with recovery_scope(self.document, self.f.coordinator(), self.plan['plan_sha256']):
            self.assertTrue(native_evolution(joint.targets(self.evidence)[0]['native_files']))
            with self.assertRaises(PermissionError): path.open('ab')
            with self.assertRaises(PermissionError): path.read_bytes()
            with self.assertRaises(PermissionError): (self.f.native/'cli.log').unlink()

    def test_large_current_diagnostic_namespace_keeps_all_leases(self):
        from local_agent_manager.joint_cli_recovery import recovery_scope, native_evolution
        from local_agent_manager.windows_held_files import ensure_descriptor_capacity
        with self.assertRaisesRegex(RuntimeError, 'lease_budget_exceeded'):
            ensure_descriptor_capacity(8193)
        # More files than the Windows CRT's initial 512-descriptor table, with
        # shared ancestors. Every diagnostic and restore source remains held.
        for index in range(650):
            (self.f.native/'log'/('cli-large-'+str(index)+'.log')).write_text('diagnostic')
        with recovery_scope(self.document, self.f.coordinator(), self.plan['plan_sha256']) as entries:
            self.assertTrue(native_evolution(joint.targets(self.evidence)[0]['native_files']))
            current = next(iter(entries.values()))['native_current']
            self.assertGreater(len(current.held.handles), 650)
            self.assertLess(len(current.held.parents), 100)
            with self.assertRaises(PermissionError): (self.f.native/'log/cli-large-649.log').open('ab')
            with self.assertRaises(PermissionError): (self.f.native/'log').rename(self.f.native/'moved-log')
        result, payload = self.cold('large-diagnostic-cold')
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')+str(payload))
        self.assertTrue(payload['goal_satisfied'])

    def test_restore_reference_is_rejected(self):
        with closing(sqlite3.connect(self.f.t3/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id=?',
                (json.dumps({'nativeThreadRef':{'driver':'codex','nativeId':CODEX_NATIVE,'strength':'strong'},
                             'savedResume':TARGET}), NEW))
            db.commit()
        self.assert_blocked('restore-reference')

    def test_held_reads_keep_binary_and_startup_exclusion(self):
        from local_agent_manager import t3code_lifecycle as lifecycle
        from local_agent_manager.joint_cli_recovery import recovery_scope
        with recovery_scope(self.document, self.f.coordinator(), self.plan['plan_sha256']):
            self.assertEqual(lifecycle.read_binary(self.f.t3, self.binary),
                next(b['file'] for b in self.evidence['lifecycle']['binaries'] if b['file']['path']==self.binary.name))
            # Discovery is real and still cannot authorize the new thread.
            fresh = discover_bindings(self.f.t3)
            self.assertIn(NEW, {b['thread_id'] for b in fresh['native_stores']})
            with self.assertRaisesRegex(ValueError,'runtime_not_held'):
                lifecycle.read_binary(self.f.t3, self.codex_binary)
            for binary in (self.binary, self.f.agy):
                with self.assertRaises(PermissionError): binary.read_bytes()
                with self.assertRaises(PermissionError): binary.open('ab')
            for path in (self.f.t3/'userdata/settings.json',
                         self.f.user/'.t3/userdata/statev2.sqlite', self.f.user/'.paseo/agents/guard.json'):
                with self.assertRaises(PermissionError): path.read_bytes()
            with closing(sqlite3.connect(self.f.t3/lifecycle.OWNER, timeout=0)) as db:
                with self.assertRaises(sqlite3.OperationalError): db.execute('BEGIN EXCLUSIVE')


@unittest.skipUnless(os.name == 'nt' and os.environ.get('LAM_REHEARSAL_CODEX_BINARY'),
                     'opt-in copied installed Codex runtime; all stores synthetic')
class MixedTopologyTests(unittest.TestCase):
    def test_completed_three_children_unknown_reservation_and_new_bindings(self):
        from tests.test_t3code_cli_rehearsal import ColdCliRehearsal, ROOTS, SELECTED, CHILDREN, CODEX
        from local_agent_manager import antigravity_cli_cleanup as native
        # Only copy the installed executable. Never lease a production binary
        # or connect this app-server to a production home.
        runtime_temp = tempfile.TemporaryDirectory(prefix='lam-runtime-copy-')
        self.addCleanup(runtime_temp.cleanup)
        runtime = Path(runtime_temp.name)/'codex.exe'
        shutil.copy2(os.environ['LAM_REHEARSAL_CODEX_BINARY'], runtime)
        host = patch.dict(os.environ, {'LAM_REHEARSAL_CODEX_BINARY':str(runtime)})
        host.start(); self.addCleanup(host.stop)
        f = ColdCliRehearsal('runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        from tests.test_antigravity_cli_cleanup import KEEP as AGY_KEEP
        initial_sources(f.agyroot)
        install = f.base/'install'; install.mkdir()
        frontend = install/'T3 Code (Nightly).exe'
        f.frontend.rename(frontend)
        manifest = strict_json_load(f.bindings)
        manifest['runtime_binaries'][0] = str(frontend)
        desktop = f.base/'appdata/t3code-v2'
        desktop.parent.mkdir(parents=True)
        f.desktop.rename(desktop)
        manifest['desktop_profiles'] = [{'root':str(desktop), 'runtime_binaries':[str(frontend)]}]
        f.bindings.write_text(json.dumps(manifest))
        settings_path = f.root/'userdata/settings.json'
        settings = strict_json_load(settings_path)
        settings['providerInstances'].update(codex={'driver':'codex','config':{
            'homePath':str(f.home),'binaryPath':str(runtime)}}, claudeAgent={'driver':'claudeAgent',
                'config':{'homePath':str(f.claude)}})
        settings_path.write_text(json.dumps(settings))
        with patch.dict(os.environ, f.env), patch('pathlib.Path.home', return_value=f.base/'profile'), patch(
                'local_agent_manager.t3code_server_delete._bundled_install', return_value=install):
            manifest = discover_bindings(f.root)
        f.bindings.write_text(json.dumps(manifest))
        plan = f.plan()
        self.assertEqual(len(plan['child_batches']), 3)
        result = subprocess.run([sys.executable, '-c', CRASH, str(f.plan_path), str(f.base/'profile')],
            cwd=Path(__file__).resolve().parents[1], env=f.env, capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 73, result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))
        evidence = t3code_cleanup.evidence_from_document(plan)
        store, _ = joint.reservation(plan, evidence, joint.targets(evidence)[0])
        self.assertTrue(store.lock_exists())
        reopen(f.agyroot, AGY_KEEP)
        with closing(sqlite3.connect(f.root/'userdata/statev2.sqlite')) as db:
            # A genuinely new AGY binding and a new Codex binding, both only
            # protection sources; no frozen target or child hash is changed.
            add_thread(db, NEW, native=KEEP, driver='antigravity')
            add_thread(db, CODEX_THREAD, native='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
        out = Path(os.environ['LAM_TOPOLOGY_EVIDENCE'])/'mixed' if os.environ.get('LAM_TOPOLOGY_EVIDENCE') else f.base/'recovery-evidence'
        out.mkdir(parents=True)
        (out/'frozen-plan.json').write_bytes(f.plan_path.read_bytes())
        audit_raw = None
        for label in ('first-cold', 'second-cold'):
            if label == 'second-cold':
                reopen(f.agyroot, AGY_KEEP, LATER_NATIVE)
            result = subprocess.run([sys.executable, '-c', COLD, str(f.plan_path), str(f.base/'profile'),
                plan['plan_sha256'], str(install), '[]'], cwd=Path(__file__).resolve().parents[1],
                env=f.env, capture_output=True, timeout=180)
            (out/(label+'.json')).write_bytes(result.stdout)
            (out/(label+'.stderr')).write_bytes(result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace')+str(payload))
            self.assertTrue(payload['goal_satisfied'])
            self.assertEqual(payload['goal_status'], 'complete')
            self.assertFalse(store.lock_exists())
            audit_path = next(store.directory.glob('recovery-*.json'))
            if audit_raw is not None: self.assertEqual(audit_path.read_bytes(), audit_raw)
            audit_raw = audit_path.read_bytes()
        (out/'audit.json').write_bytes(audit_raw)
        self.assertEqual(len(json.loads(audit_raw)['children']), 3)
        self.assertEqual({str(p):__import__('hashlib').sha256(p.read_bytes()).hexdigest() for p in f.preserved}, f.before)
        self.assertFalse(any(p.exists() for p in f.native_paths[:-1]))
        with patch.dict(os.environ, f.env), patch('pathlib.Path.home', return_value=f.base/'profile'):
            self.assertTrue(all(not t3code_cleanup.native_remaining(t) for t in evidence['native_targets']
                                if t['engine'] != 'antigravity'))
            with self.assertRaisesRegex(ValueError, 'log_link_changed'):
                native.remaining(joint.targets(evidence)[0]['native_files'])
        self.assertEqual(payload['recovery_current'][0]['current_native']['new_summary_ids'],
                         sorted([NEW_NATIVE, LATER_NATIVE]))
        with closing(sqlite3.connect(f.root/'userdata/statev2.sqlite')) as db:
            present = {r[0] for r in db.execute(f'SELECT thread_id FROM {V2}threads')}
            self.assertEqual(present, {'keeper', NEW, CODEX_THREAD})
            self.assertFalse(present & set(SELECTED))
        (out/'preservation.json').write_text(json.dumps({'target_t3_remaining':0,'native_targets_remaining':0,
            'unchanged_files':len(f.preserved), 'new_bindings_are_protection_only':True,
            'three_children_complete':True,'reservation_lock_released':True,'two_cold_verifications_complete':True,
            'codex_transport':'copied_installed_app_server','production_writes':0},indent=2))


if __name__ == '__main__': unittest.main()
