import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from local_agent_record_janitor import antigravity_acp_cleanup as acp, t3code_cleanup as cleanup
from local_agent_record_janitor.t3code_bound_adapter import T3CodeBoundAdapter
from tests import test_t3code_fullflow as fullflow
from tests.test_t3code_fullflow import THREAD, KEEP, NATIVE, OTHER
from tests.t3code_support import V2, insert


def native_session(root, sid, workspace):
    folder = root/'conversations'
    folder.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(folder/(sid+'.db'))) as db:
        for kind, _, _, sql in sorted(acp.CONTRACT['objects'], key=lambda r: r[0] != 'table'):
            db.execute(sql)
        db.execute('INSERT INTO trajectory_meta VALUES(?,?,4,0)', (sid,sid))
        db.commit()
    (folder/(sid+'.meta')).write_text(json.dumps({'cwd':str(workspace)}))
    brain = root/'brain'/sid
    brain.mkdir(parents=True)
    (brain/'artifact.md').write_text('PRIVATE_NATIVE_ARTIFACT')


@unittest.skipUnless(os.name == 'nt', 'Qualified T3 runtime exclusion is Windows-only')
class AntigravityCleanupTests(unittest.TestCase):
    coordinator = fullflow.T3CodeFullflowTests.coordinator
    apply = fullflow.T3CodeFullflowTests.apply

    def setUp(self):
        fullflow.T3CodeFullflowTests.setUp(self)
        instance='antigravity'
        self.acp_root=self.root/'userdata/providers/antigravity'/hashlib.sha256(instance.encode()).hexdigest()/'antigravity-acp'
        binaries=[];hashes={}
        for name in acp.RUNTIMES:
            p=self.base/'runtime'/name;p.parent.mkdir(exist_ok=True);p.write_bytes(('fixture '+name).encode())
            binaries.append(str(p));hashes[name]=hashlib.sha256(p.read_bytes()).hexdigest()
        patcher=patch.object(acp,'RUNTIMES',hashes);patcher.start();self.addCleanup(patcher.stop)
        self.manifest['runtime_binaries'].extend(binaries)
        self.manifest['native_stores'][0]={'thread_id':THREAD,'engine':'antigravity','root':str(self.acp_root),
            'provider_instance_id':instance,'acp_runtime_binaries':binaries}
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}threads SET default_provider='antigravity',provider_instance_id='antigravity' WHERE thread_id=?",(THREAD,))
            db.execute(f"UPDATE {V2}provider_threads SET provider='antigravity',driver='antigravity',provider_instance_id='antigravity',payload_json=? WHERE thread_id=?",
                (json.dumps({'nativeThreadRef':{'driver':'antigravity','nativeId':NATIVE,'strength':'strong'}}),THREAD))
            db.commit()
        native_session(self.acp_root,NATIVE,self.base)
        native_session(self.acp_root,OTHER,self.base)
        (self.acp_root/'settings.json').write_text('{"fixture":"retained"}')
        (self.acp_root/'acp_token.json').write_text('PRIVATE_AUTH_RETAINED')
        skill=self.acp_root/'skills'/'fixture'/'SKILL.md';skill.parent.mkdir(parents=True);skill.write_text('retained skill')
        self.adapter=T3CodeBoundAdapter(self.manifest)

    def plan(self):
        return self.coordinator().plan_operation(client='t3code',record_ids=(THREAD,),adapters=(self.adapter,),
            plan_path=self.plan_path,operation_id='acp-fullflow')

    def test_full_mixed_profile_deletes_exact_native_and_t3_preserves_auth_worktree_keeper(self):
        keeper=acp.frozen_files.freeze_remove(self.acp_root,'conversations/'+OTHER+'.db')
        plan=self.plan();self.assertEqual(plan['goal_status'],'ready',plan)
        self.assertEqual([a['kind'] for a in plan['actions']],[cleanup.KIND])
        self.assertNotIn('PRIVATE_AUTH_RETAINED',json.dumps(plan))
        self.assertNotIn('PRIVATE_NATIVE_ARTIFACT',json.dumps(plan))
        result=self.apply(plan);self.assertEqual(result['goal_status'],'complete',result)
        self.assertFalse((self.acp_root/'conversations'/(NATIVE+'.db')).exists())
        self.assertFalse((self.acp_root/'conversations'/(NATIVE+'.meta')).exists())
        self.assertFalse((self.acp_root/'brain'/NATIVE).exists())
        self.assertEqual(acp.frozen_files.freeze_remove(self.acp_root,keeper['path']),keeper)
        self.assertEqual((self.acp_root/'acp_token.json').read_text(),'PRIVATE_AUTH_RETAINED')
        self.assertEqual((self.acp_root/'skills'/'fixture'/'SKILL.md').read_text(),'retained skill')
        self.assertTrue(self.base.exists())
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            self.assertEqual(db.execute(f'SELECT thread_id FROM {V2}threads').fetchall(),[(KEEP,)])
        self.assertEqual(self.coordinator().verify_operation(operation_id=plan['operation_id'],plan_path=self.plan_path)['goal_status'],'complete')

    def test_schema_parent_reference_and_unknown_files_block(self):
        with closing(sqlite3.connect(self.acp_root/'conversations'/(OTHER+'.db'))) as db:
            db.execute("INSERT INTO parent_references VALUES(1,x'00')");db.commit()
        self.assertEqual(self.plan()['goal_status'],'blocked')
        with closing(sqlite3.connect(self.acp_root/'conversations'/(OTHER+'.db'))) as db:
            db.execute('DELETE FROM parent_references');db.commit()
        (self.acp_root/'conversations'/(NATIVE+'.db.bak')).write_text('unknown restore copy')
        self.assertEqual(self.plan()['goal_status'],'blocked')
        (self.acp_root/'conversations'/(NATIVE+'.db.bak')).unlink()
        with closing(sqlite3.connect(self.acp_root/'conversations'/(OTHER+'.db'))) as db:
            db.execute('CREATE TABLE unknown_restore(data BLOB)');db.commit()
        self.assertEqual(self.plan()['goal_status'],'blocked')
        with closing(sqlite3.connect(self.acp_root/'conversations'/(OTHER+'.db'))) as db:
            db.execute('DROP TABLE unknown_restore')
            db.execute('INSERT INTO steps(idx,has_subtrajectory) VALUES(1,1)');db.commit()
        self.assertEqual(self.plan()['goal_status'],'blocked')

    def test_historical_instance_and_forged_file_scope_rejected(self):
        plan=self.plan();self.assertEqual(plan['goal_status'],'ready',plan)
        evidence=copy.deepcopy(cleanup.evidence_from_document(plan))
        evidence['native_targets'][0]['native_files']['changes'][0]['path']='acp_token.json'
        with self.assertRaisesRegex(ValueError,'deletion_scope_changed'):
            cleanup.validate(evidence)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db,'orchestration_v2_events',event_id='old-instance',thread_id=THREAD,event_type='provider.thread',
                provider_instance_id='different-instance',payload_json=json.dumps({'nativeThreadRef':{'driver':'antigravity','nativeId':NATIVE,'strength':'strong'}}))
            db.commit()
        self.assertEqual(self.plan()['goal_status'],'blocked')

    def test_instance_and_runtime_mismatch_block(self):
        bad=copy.deepcopy(self.manifest)
        bad['native_stores'][0]['provider_instance_id']='other-instance'
        with self.assertRaisesRegex(ValueError,'instance_root_mismatch'):
            T3CodeBoundAdapter(bad)
        Path(self.manifest['native_stores'][0]['acp_runtime_binaries'][0]).write_bytes(b'changed')
        self.assertEqual(self.plan()['goal_status'],'blocked')

    def test_warm_apply_deduplicates_identical_bound_profile(self):
        coordinator=self.coordinator()
        plan=coordinator.plan_operation(client='t3code',record_ids=[THREAD],adapters=[self.adapter],plan_path=self.plan_path)
        self.assertEqual(plan['goal_status'],'ready',plan)
        result=coordinator.apply_operation(operation_id=plan['operation_id'],plan_path=self.plan_path,
            plan_sha256=plan['plan_sha256'],clients_closed=True)
        self.assertEqual(result['goal_status'],'complete',result)

    def test_drift_blocks_before_any_mutation_and_standalone_writer_denied(self):
        plan=self.plan();self.assertEqual(plan['goal_status'],'ready',plan)
        evidence=cleanup.evidence_from_document(plan)['native_targets'][0]['native_files']
        with self.assertRaisesRegex(ValueError,'execution_ticket_required'):
            acp.apply(evidence,phase_callback=lambda _:self.fail('unauthorized'))
        (self.acp_root/'brain'/OTHER/'artifact.md').write_text('unselected changed')
        result=self.apply(plan);self.assertEqual(result['goal_status'],'blocked',result)
        self.assertFalse(result['mutation_started'])
        self.assertTrue((self.acp_root/'conversations'/(NATIVE+'.db')).exists())

    def test_interrupted_after_native_delete_cannot_repeat(self):
        plan=self.plan();original=acp.apply
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise OSError('fixture interruption')
        with patch.object(acp,'apply',side_effect=interrupted):
            result=self.apply(plan)
        self.assertEqual(result['goal_status'],'unknown',result)
        with patch.object(acp,'apply',side_effect=AssertionError('no retry')):
            self.assertEqual(self.apply(plan)['goal_status'],'unknown')
        verified=self.coordinator().verify_operation(operation_id=plan['operation_id'],plan_path=self.plan_path)
        self.assertEqual(verified['goal_status'],'completed_with_residuals')
        self.assertFalse(verified['goal_satisfied'])
        with patch.object(acp,'apply',side_effect=AssertionError('no retry after residual verification')):
            self.assertFalse(self.apply(plan)['goal_satisfied'])


if __name__=='__main__':unittest.main()
