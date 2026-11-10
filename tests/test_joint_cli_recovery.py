"""Real process loss after child publication; never use a live user store."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from local_agent_manager import antigravity_cli_joint as joint, antigravity_cli_cleanup as native
from local_agent_manager import joint_cli_recovery as recovery, t3code_cleanup
from local_agent_manager.operation_store import strict_json_load
from tests import test_antigravity_cli_joint as fixtures
from tests.t3code_support import add_thread, V2
from local_agent_manager.t3code_store import database_path
import sqlite3

CRASH = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from local_agent_manager.operation_coordinator import OperationCoordinator
from local_agent_manager.cleanup_service import CleanupService
from local_agent_manager import antigravity_cli_joint as joint
from local_agent_manager.operation_store import strict_json_load
plan = strict_json_load(Path(sys.argv[1]))
original = joint.finish_reservation
def finish(*args, **kwargs):
    if kwargs['complete']:
        # Same supported publication performed by the first cold verify in
        # the incident: freeze real verified child receipts before the loss.
        coordinator._persist_verified_child_journals(plan, [])
        os._exit(73) # child receipts durable; all native/frontend files verified
    return original(*args, **kwargs)
with patch('pathlib.Path.home', return_value=Path(sys.argv[2])), patch('local_agent_manager.office_runtime.processes', return_value=[]), patch.object(joint, 'finish_reservation', finish):
    coordinator = OperationCoordinator(CleanupService(client_inspector=lambda *a, **k: ()))
    result = coordinator.apply_operation(
        operation_id=plan['operation_id'], plan_path=Path(sys.argv[1]),
        plan_sha256=plan['plan_sha256'], clients_closed=True)
    print(result)
    raise SystemExit(74)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows process/handle recovery')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JointTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.adapter = self.fixture.setup_client('t3code')
        if self._testMethodName == 'test_reopened_desktop_scope_is_verified_and_target_reappearance_blocks':
            from tests.test_t3code_localstorage import local_storage
            from local_agent_manager.t3code_bound_adapter import T3CodeBoundAdapter
            self.desktop = self.fixture.base/'desktop'
            local_storage(self.desktop, thread_id=fixtures.FRONT)
            (self.fixture.t3/'userdata/environment-id').write_text('env-1')
            manifest = copy.deepcopy(self.adapter.manifest)
            manifest['environment_id'] = 'env-1'
            manifest['desktop_profiles'] = [{'root':str(self.desktop), 'runtime_binaries':[str(self.fixture.binary)]}]
            self.adapter = T3CodeBoundAdapter(manifest)
        self.plan = self.fixture.plan('t3code', self.adapter)
        self.assertEqual(self.plan['goal_status'], 'ready', self.plan)
        self.document = strict_json_load(self.fixture.plan_path)
        self.evidence = t3code_cleanup.evidence_from_document(self.document)
        self.store, self.reservation = joint.reservation(self.document, self.evidence, joint.targets(self.evidence)[0])

    def crash_after_children(self):
        completed = subprocess.run([sys.executable, '-c', CRASH, str(self.fixture.plan_path), str(self.fixture.user)],
            capture_output=True, timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(completed.returncode, 73, completed.stderr.decode(errors='replace')+completed.stdout.decode(errors='replace'))
        self.assertTrue(self.store.lock_exists())
        self.assertTrue(recovery._children(self.document, self.fixture.coordinator()))
        self.assertEqual(self.store.read_state()['goal_status'], 'unknown')

    def verify(self, recover=True, **kwargs):
        return self.fixture.coordinator().verify_operation(operation_id=self.plan['operation_id'],
            plan_path=self.fixture.plan_path, recover_joint_reservations=recover,
            authorized_plan_sha256=kwargs.get('hash', self.plan['plan_sha256']))

    def test_real_child_exit_cold_verify_and_retained_audit(self):
        self.crash_after_children()
        original_lock = self.store.lock_path.read_bytes()
        status = self.fixture.coordinator().status_operation(operation_id=self.plan['operation_id'], plan_path=self.fixture.plan_path)
        self.assertEqual(status['goal_status'], 'unknown')
        with patch.object(native, 'apply', side_effect=AssertionError('never replay')):
            self.assertEqual(self.fixture.apply(self.plan)['goal_status'], 'unknown')
            unknown = self.verify(False)
            self.assertEqual(unknown['goal_status'], 'unknown', unknown)
            self.assertTrue(unknown['mutation_started'])
            self.assertEqual(self.store.lock_path.read_bytes(), original_lock)
            result = self.verify()
            self.assertEqual(result['goal_status'], 'complete', result)
            self.assertTrue(result['goal_satisfied'])
            self.assertFalse(self.store.lock_exists())
            audits = list(self.store.directory.glob('recovery-*.json'))
            self.assertEqual(len(audits), 1)
            audit = strict_json_load(audits[0])
            self.assertEqual(audit['original_lock'], json.loads(original_lock))
            self.assertEqual(audit['parent_plan_sha256'], self.plan['plan_sha256'])
            self.assertTrue(audit['goal_satisfied'])
            self.assertEqual(self.verify(False)['goal_status'], 'complete')

    def test_cold_cli_parser_and_recovery_publication_crash_are_resumable(self):
        self.crash_after_children()
        from local_agent_manager.windows_held_files import HeldFiles
        with patch.object(HeldFiles, 'dispose', side_effect=RuntimeError('lost_after_publication')):
            failed = self.verify()
        self.assertEqual(failed['goal_status'], 'unknown', failed)
        self.assertTrue(self.store.lock_exists())
        audit_path = next(self.store.directory.glob('recovery-*.json'))
        audit_before = audit_path.read_bytes()
        command = r'''
import sys
from pathlib import Path
from unittest.mock import patch
from local_agent_manager.entrypoint import main
with patch('pathlib.Path.home',return_value=Path(sys.argv[2])),patch('local_agent_manager.office_runtime.processes',return_value=[]):
    raise SystemExit(main(['operation','verify','--operation-id','joint-test','--plan',sys.argv[1],
        '--recover-joint-reservations','--authorized-plan-sha256',sys.argv[3],'--json','--progress']))
'''
        result = subprocess.run([sys.executable, '-c', command, str(self.fixture.plan_path), str(self.fixture.user), self.plan['plan_sha256']],
            capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))
        payload = json.loads(result.stdout)
        self.assertTrue(payload['goal_satisfied'], payload)
        self.assertTrue(payload['recovery'][0]['verification_only'])
        self.assertFalse(self.store.lock_exists())
        self.assertEqual(audit_path.read_bytes(), audit_before)

    def test_missing_unselected_thread_still_blocks_post_terminal_recovery(self):
        self.crash_after_children()
        with closing(sqlite3.connect(database_path(self.fixture.t3))) as db:
            db.execute(f'DELETE FROM {V2}threads WHERE thread_id=?', (fixtures.OTHER,))
            db.commit()
        self.assertEqual(self.verify()['goal_status'], 'unknown')
        self.assertTrue(self.store.lock_exists())
        self.assertFalse(list(self.store.directory.glob('recovery-*.json')))

    def test_active_writer_pid_reuse_access_denial_and_descendant_rejected(self):
        self.crash_after_children()
        before = self.store.lock_path.read_bytes()
        for code in ('joint_recovery_writer_alive_or_pid_reused', 'joint_recovery_process_unproven'):
            with self.subTest(code=code), patch.object(recovery, 'require_process_absent', side_effect=RuntimeError(code)):
                result = self.verify()
                self.assertEqual(result['goal_status'], 'unknown', result)
        pid = json.loads(before)['pid']
        with patch('local_agent_manager.office_runtime.processes', return_value=[{
                'pid': pid+1, 'parent': pid, 'name': 'python.exe', 'executable': sys.executable, 'command': 'fixture'}]):
            self.assertIn('descendant', self.verify()['blockers'][0]['message'])
        self.assertEqual(self.store.lock_path.read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, 'alive_or_pid_reused'):
            recovery.require_process_absent(os.getpid())

    def test_untrusted_checkpoint_bad_hash_or_changed_after_state_keeps_lock(self):
        self.crash_after_children()
        before = self.store.lock_path.read_bytes()
        self.assertEqual(self.verify(hash='0'*64)['goal_status'], 'unknown')
        with patch.object(native, 'remaining', side_effect=RuntimeError('preserved_file_changed')):
            result = self.verify()
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertEqual(self.store.lock_path.read_bytes(), before)
        with patch.object(native, 'remaining', return_value=1), patch.object(native, 'apply', side_effect=AssertionError('no replay')):
            self.assertEqual(self.verify()['goal_status'], 'unknown')
        self.assertEqual(self.store.lock_path.read_bytes(), before)
        # Synthetic journal corruption must not get a recovery receipt.
        with self.store.events_path.open('a') as stream:
            stream.write('{"sequence":999}\n')
        self.assertEqual(self.verify()['goal_status'], 'unknown')
        self.assertFalse(list(self.store.directory.glob('recovery-*.json')))
        with patch.object(native, 'apply', side_effect=AssertionError('unknown never replayed')):
            self.assertNotEqual(self.fixture.apply(self.plan)['goal_status'], 'ready')

    def test_actual_live_pid_and_exclusive_handle_block_recovery(self):
        self.crash_after_children()
        original = self.store.lock_path.read_bytes()
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            lock = json.loads(original)
            lock['pid'] = child.pid
            self.store.lock_path.write_text(json.dumps(lock)) # isolated synthetic store only
            result = self.verify()
            self.assertEqual(result['goal_status'], 'unknown')
            self.assertIn('alive_or_pid_reused', result['blockers'][0]['message'])
        finally:
            child.terminate(); child.wait(timeout=10)
        self.store.lock_path.write_bytes(original)
        from local_agent_manager.windows_held_files import exclusive_fd
        fd = exclusive_fd(self.store.lock_path)
        try:
            self.assertEqual(self.verify()['goal_status'], 'unknown')
        finally:
            os.close(fd)
        self.assertEqual(self.store.lock_path.read_bytes(), original)

    def test_post_terminal_changes_require_current_preservation(self):
        self.crash_after_children()
        with closing(sqlite3.connect(database_path(self.fixture.t3))) as db:
            db.execute(f"UPDATE {V2}threads SET title='Continued after reopening' WHERE thread_id=?", (fixtures.OTHER,))
            db.commit()
        self.assertEqual(self.verify(False)['goal_status'], 'unknown')
        result = self.verify()
        self.assertEqual(result['goal_status'], 'complete', result)
        audit = strict_json_load(next(self.store.directory.glob('recovery-*.json')))
        self.assertNotEqual(audit['current_database']['logical_sha256'], audit['current_database']['original_verified_after_sha256'])
        self.assertIn(fixtures.OTHER, audit['current_database']['preserved_thread_ids'])
        self.assertEqual(self.verify()['goal_status'], 'complete')

    def test_reopened_desktop_scope_is_verified_and_target_reappearance_blocks(self):
        self.crash_after_children()
        from local_agent_manager import t3code_localstorage as storage
        from tests.test_t3code_localstorage import local_storage
        level = storage.level
        node, dependency, helper = level.runtime()
        script = r'''
const path=require('node:path');const h=require(process.argv[1]);
const req=require('node:module').createRequire(path.join(process.env.LARJ_LEVELDB_RUNTIME,'package.json'));
const db=new(req('classic-level').ClassicLevel)(process.argv[2],{keyEncoding:'buffer',valueEncoding:'buffer'});
(async()=>{await db.open();await db.put(Buffer.concat([Buffer.from('_t3code://app\0'),h.encodeString('opaque-settings')]),Buffer.from('UPDATED_AFTER_REOPEN'));await db.close()})().catch(e=>{console.error(e);process.exitCode=1});
'''
        done = subprocess.run([str(node), '-e', script, str(helper), str(self.desktop/storage.RELATIVE)],
            cwd=dependency, env=level._environment(dependency), capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.verify(False)['goal_status'], 'unknown')
        result = self.verify()
        self.assertEqual(result['goal_status'], 'complete', result)
        audit = strict_json_load(next(self.store.directory.glob('recovery-*.json')))
        self.assertTrue(audit['current_desktop'])
        local_storage(self.desktop, thread_id=fixtures.FRONT) # resurrect a selected draft in this fixture only
        with patch.object(native, 'apply', side_effect=AssertionError('no replay')):
            self.assertEqual(self.verify()['goal_status'], 'unknown')

    def test_partial_children_never_recovered(self):
        # A new operation with no executed child cannot recover or dispatch.
        with patch.object(native, 'apply', side_effect=AssertionError('no dispatch')):
            result = self.verify()
        self.assertNotEqual(result['goal_status'], 'complete', result)
        self.assertFalse(self.store.directory.exists())

    def test_new_unselected_bindings_are_guards_only_and_discovery_failure_blocks(self):
        new_id = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
        with closing(sqlite3.connect(database_path(self.fixture.t3))) as db:
            add_thread(db, new_id, native=fixtures.KEEP, driver='antigravity')
        fresh = copy.deepcopy(self.evidence['binding_manifest'])
        added = self.fixture.bound(new_id, 't3code')
        fresh['native_stores'].append(added)
        with patch('local_agent_manager.t3code_discovery.discover_bindings', return_value=fresh):
            adapters = t3code_cleanup.bound_adapters(self.document, readonly_recovery=True)
        self.assertEqual(adapters[0].bindings[new_id], added)
        self.assertEqual(adapters[0].manifest, self.evidence['binding_manifest'])
        self.assertNotIn(new_id, self.evidence['selected_thread_ids'])
        with patch('local_agent_manager.t3code_discovery.discover_bindings', side_effect=RuntimeError('unproven home')):
            with self.assertRaisesRegex(RuntimeError, 'unproven'):
                t3code_cleanup.bound_adapters(self.document, readonly_recovery=True)


if __name__ == '__main__':
    unittest.main()
