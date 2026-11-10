from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import t3code_server_delete as t3
from local_agent_record_janitor.cli import main
from tests.t3code_support import create_store, add_thread, insert, V2, PRIVATE

THREAD = '11111111-1111-4111-8111-111111111111'
KEEP = '22222222-2222-4222-8222-222222222222'


class T3ServerDeleteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base/'t3'
        self.worktree = self.base/'worktree'
        self.worktree.mkdir()
        (self.worktree/'keep.txt').write_text('worktree data')
        self.native = self.base/'native-session.db'
        self.native.write_bytes(b'retained provider history')
        db = create_store(self.root)
        add_thread(db,THREAD,native='native-codex')
        add_thread(db,KEEP,native='native-antigravity',driver='antigravity')
        payload = json.loads(db.execute(f'SELECT payload_json FROM {V2}threads WHERE thread_id=?',(THREAD,)).fetchone()[0])
        payload.update(worktreePath=str(self.worktree),branch='test')
        db.execute(f'UPDATE {V2}threads SET payload_json=? WHERE thread_id=?',(json.dumps(payload),THREAD))
        db.commit();db.close()
        (self.root/'userdata/environment-id').write_text('env-fixture')
        self.server = {'version':1,'pid':123,'origin':'http://127.0.0.1:6767','port':6767,'startedAt':'fixture',
                       'serverVersion':'0.1.9-nightly.20261109.4443','ownerId':'owner-fixture'}
        (self.root/'userdata/server-runtime.json').write_text(json.dumps(self.server))
        install = self.base/'install'
        for part in t3._RUNTIME_FILES:
            p = install/part;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
        for item in (patch.object(t3,'_bundled_install',return_value=install),patch.object(t3,'_rpc',side_effect=self.rpc),
                     patch.dict(os.environ, {'LOCALAPPDATA': str(self.base/'state'), 'XDG_STATE_HOME': str(self.base/'state')})):
            item.start();self.addCleanup(item.stop)
        self.calls=[]
        self.plan_path=self.base/'plan.json'
        self.ack_only=False
        self.timeout_after=False
        self.detach_noop=False

    def invoke(self,*args):
        output=io.StringIO();err=io.StringIO()
        code=main([*args,'--json'],stdin=io.StringIO(),stdout=output,stderr=err)
        return code,json.loads(output.getvalue())

    def plan(self,tid=THREAD,*extra):
        return self.invoke('delete','plan','--client','t3code','--t3code-only','--t3code-root',str(self.root),
                           '--record-id',tid,'--out',str(self.plan_path),*extra)

    def apply(self,plan,*extra,approved=True):
        approval=('--plan-sha256',plan['plan_sha256']) if approved else ()
        return self.invoke('delete','apply','--t3code-only','--operation-id',plan['operation_id'],
                           '--plan',str(self.plan_path),*approval,*extra)

    def verify(self,plan):
        return self.invoke('operation','verify','--operation-id',plan['operation_id'],'--plan',str(self.plan_path))

    def rpc(self,evidence,commands,timeout):
        for c in commands:
            self.calls.append(c)
            with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
                payload=json.loads(db.execute(f'SELECT payload_json FROM {V2}threads WHERE thread_id=?',(c['threadId'],)).fetchone()[0])
                if c['type']=='thread.metadata.update' and not self.detach_noop:
                    payload['worktreePath']=None
                    db.execute(f'UPDATE {V2}threads SET payload_json=? WHERE thread_id=?',(json.dumps(payload),c['threadId']))
                elif c['type']=='thread.delete' and not self.ack_only:
                    # Emulate the dangerous builtin worktreeOnDelete setting.
                    if payload.get('worktreePath'):
                        self.fail('delete reached server before worktree detachment')
                    db.execute(f'UPDATE {V2}threads SET deleted_at=? WHERE thread_id=?',('2026-11-08',c['threadId']))
                    payload['deletedAt']='2026-11-08'
                    db.execute(f'UPDATE {V2}threads SET payload_json=? WHERE thread_id=?',(json.dumps(payload),c['threadId']))
                    db.execute(f"UPDATE {V2}provider_sessions SET status='stopped' WHERE thread_id=?",(c['threadId'],))
                db.commit()
            if self.timeout_after and c['type']=='thread.delete':
                raise t3.T3ServerError('fixture timeout')

    def test_mixed_profile_preserves_provider_native_worktree_and_keeper(self):
        keeper=t3._snapshot(self.root,KEEP)
        code,plan=self.plan();self.assertEqual(code,0,plan)
        self.assertEqual([a['kind'] for a in plan['actions']],[t3.KIND])
        self.assertNotIn(PRIVATE,json.dumps(plan))
        code,result=self.apply(plan);self.assertEqual(code,0,result)
        self.assertTrue(result['goal_satisfied'])
        self.assertEqual(t3._snapshot(self.root,KEEP),keeper)
        self.assertEqual(self.native.read_bytes(),b'retained provider history')
        self.assertEqual((self.worktree/'keep.txt').read_text(),'worktree data')
        self.assertEqual([c['type'] for c in self.calls],['thread.metadata.update','thread.delete'])
        self.assertEqual(self.verify(plan)[1]['goal_status'],'complete')

    def test_antigravity_only_requires_no_native_binding(self):
        code,plan=self.plan(KEEP);self.assertEqual(code,0,plan)
        code,result=self.apply(plan);self.assertEqual(code,0,result)
        self.assertEqual([c['type'] for c in self.calls],['thread.delete'])
        self.assertEqual(self.native.read_bytes(),b'retained provider history')

    def test_batch_run_plans_and_verifies_both_ids_without_manual_hashes(self):
        output = self.base / 'batch.json'
        code, result = self.invoke('delete', 'run', '--client', 't3code', '--t3code-only',
            '--t3code-root', str(self.root), '--record-id', THREAD, '--record-id', KEEP,
            '--yes', '--out', str(output))
        self.assertEqual(code, 0, result)
        self.assertTrue(result['goal_satisfied'])
        self.assertEqual(len(result['plans']), 2)
        manifest = json.loads(output.read_text())
        self.assertEqual(len(manifest['plans']), 2)
        self.assertEqual([c['type'] for c in self.calls], ['thread.metadata.update', 'thread.delete', 'thread.delete'])
        for plan in result['plans']:
            code, checked = self.invoke('operation', 'verify', '--operation-id', plan['operation_id'], '--plan', plan['plan_path'])
            self.assertEqual(checked['goal_status'], 'complete', checked)

    def test_batch_preflight_checks_every_target_before_any_mutation(self):
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db, V2+'provider_sessions', provider_session_id='busy', thread_id=KEEP, status='running')
            db.commit()
        code, result = self.invoke('delete', 'run', '--client', 't3code', '--t3code-only',
            '--t3code-root', str(self.root), '--record-id', THREAD, '--record-id', KEEP, '--yes')
        self.assertEqual(code, 3, result)
        self.assertFalse(result['mutation_started'])
        self.assertEqual(self.calls, [])

    def test_batch_requires_authorization_and_unknown_is_not_replayed(self):
        arguments = ('delete', 'run', '--client', 't3code', '--t3code-only',
            '--t3code-root', str(self.root), '--record-id', THREAD, '--record-id', KEEP)
        self.assertNotEqual(self.invoke(*arguments)[0], 0)
        self.assertEqual(self.calls, [])
        self.ack_only = True
        code, result = self.invoke(*arguments, '--yes')
        self.assertNotEqual(code, 0)
        self.assertFalse(result['goal_satisfied'])
        self.assertEqual(sum(c['type'] == 'thread.delete' for c in self.calls), 1)
        self.assertIsNone(t3._snapshot(self.root, KEEP).get('deleted_at'))

    def test_prefix_wrong_profile_missing_approval_and_drift_block(self):
        self.assertNotEqual(self.plan(THREAD[:8])[0],0)
        code,plan=self.plan();self.assertEqual(code,0,plan)
        self.assertFalse(self.apply(plan,approved=False)[1]['mutation_started'])
        self.assertFalse(self.apply(plan,'--t3code-root',str(self.base))[1]['mutation_started'])
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}threads SET payload_json=json_set(payload_json,'$.title','changed') WHERE thread_id=?",(THREAD,))
            db.commit()
        _,result=self.apply(plan);self.assertEqual(result['goal_status'],'blocked',result)
        self.assertFalse(result['mutation_started']);self.assertEqual(self.calls,[])

    def test_detach_noop_never_dispatches_delete(self):
        _,plan=self.plan();self.detach_noop=True
        _,result=self.apply(plan);self.assertEqual(result['goal_status'],'unknown',result)
        self.assertEqual([c['type'] for c in self.calls],['thread.metadata.update'])
        self.assertEqual(self.apply(plan)[1]['goal_status'],'unknown')

    def test_ack_is_not_completion_and_unknown_cannot_retry(self):
        _,plan=self.plan();self.ack_only=True
        with patch.object(t3,'require_absent',side_effect=t3.T3ServerError('residual')):
            _,result=self.apply(plan)
        self.assertEqual(result['goal_status'],'unknown',result)
        self.assertEqual(self.verify(plan)[1]['goal_status'],'unknown')
        self.assertEqual(self.apply(plan)[1]['goal_status'],'unknown')
        self.assertEqual(sum(c['type']=='thread.delete' for c in self.calls),1)

    def test_timeout_after_deletion_recovers_by_cold_verification(self):
        _,plan=self.plan();self.timeout_after=True
        _,result=self.apply(plan);self.assertEqual(result['goal_status'],'unknown',result)
        self.assertEqual(self.apply(plan)[1]['goal_status'],'unknown')
        self.assertEqual(self.verify(plan)[1]['goal_status'],'complete')
        self.assertEqual(sum(c['type']=='thread.delete' for c in self.calls),1)

    def test_remote_endpoint_and_conflicting_mode_are_rejected(self):
        self.server['origin']='http://192.0.2.1:6767'
        (self.root/'userdata/server-runtime.json').write_text(json.dumps(self.server))
        self.assertNotEqual(self.plan()[0],0)
        self.assertNotEqual(self.plan(THREAD,'--paseo-only')[0],0)
        self.assertEqual(self.calls,[])

    def test_new_versions_and_additive_schema_changes_use_existing_contract(self):
        self.server['serverVersion'] = '0.1.9-nightly.20261109.4443'
        (self.root / 'userdata/server-runtime.json').write_text(json.dumps(self.server))
        with closing(sqlite3.connect(self.root / 'userdata/statev2.sqlite')) as db:
            db.execute(f'ALTER TABLE {V2}threads ADD COLUMN future_metadata TEXT')
            db.execute('CREATE TABLE future_feature(id TEXT PRIMARY KEY)')
            db.commit()
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        code, result = self.apply(plan)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.verify(plan)[1]['goal_status'], 'complete')

    def test_incompatible_projection_or_locator_blocks_before_rpc(self):
        with closing(sqlite3.connect(self.root / 'userdata/statev2.sqlite')) as db:
            db.execute(f'ALTER TABLE {V2}threads RENAME COLUMN deleted_at TO incompatible_delete')
            db.commit()
        self.assertNotEqual(self.plan()[0], 0)
        self.assertEqual(self.calls, [])

    def test_busy_or_shared_provider_session_blocks_and_ready_session_can_stop(self):
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db,V2+'provider_sessions',provider_session_id='provider-session',thread_id=THREAD,status='running')
            db.commit()
        self.assertNotEqual(self.plan()[0],0)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}provider_sessions SET status='ready'")
            insert(db,V2+'provider_session_bindings',provider_session_id='provider-session',thread_id=KEEP)
            db.commit()
        self.assertNotEqual(self.plan()[0],0)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db,V2+'provider_session_bindings',provider_session_id='provider-session',thread_id=THREAD)
            db.execute(f'DELETE FROM {V2}provider_sessions')
            db.commit()
        self.assertNotEqual(self.plan()[0],0)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db,V2+'provider_sessions',provider_session_id='provider-session',thread_id=THREAD,status='stopped')
            db.commit()
        code,plan=self.plan();self.assertEqual(code,0,plan)
        self.plan_path.unlink()
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}provider_sessions SET status='ready'")
            db.execute(f'DELETE FROM {V2}provider_session_bindings');db.commit()
        code,plan=self.plan();self.assertEqual(code,0,plan)
        code,result=self.apply(plan);self.assertEqual(code,0,result)

    def test_pending_effect_and_inconsistent_tombstone_do_not_verify(self):
        _,plan=self.plan()
        _,result=self.apply(plan);self.assertEqual(result['goal_status'],'complete',result)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db,'orchestration_v2_effect_outbox',effect_id='pending-cleanup',thread_id=THREAD,status='pending')
            db.commit()
        self.assertFalse(self.verify(plan)[1]['goal_satisfied'])
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute('DELETE FROM orchestration_v2_effect_outbox')
            db.execute(f"UPDATE {V2}threads SET payload_json=json_set(payload_json,'$.deletedAt',NULL) WHERE thread_id=?",(THREAD,))
            db.commit()
        self.assertFalse(self.verify(plan)[1]['goal_satisfied'])


if __name__=='__main__':unittest.main()
