from tests import bootstrap
from contextlib import closing
from pathlib import Path
import os
import unittest
from local_agent_manager.cindy_isolation_direct import verify_direct
from local_agent_manager.cindy_isolation_execution import admitted_import
from local_agent_manager.cindy_isolation_resume import import_entries
from local_agent_manager.cindy_isolation_windows import WindowsImportBoundary,process_evidence
from tests.test_cindy_isolation_controller import CindyControllerTests

@unittest.skipUnless(os.name=='nt','Windows isolated direct mode')
class DirectTests(unittest.TestCase):
    def setUp(self):
        self.case=CindyControllerTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)
        s=self.case.spec
        admitted_import(s,['selected'],import_entries(s,writer=process_evidence()),boundary_factory=lambda:WindowsImportBoundary(s,self.case.draft))
    def factory(self,wrong=False):
        case=self.case
        class Server:
            def __init__(self,**kwargs):case.assertEqual(Path(kwargs['codex_home']),Path(case.spec['target']['path']))
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def request(self,method,params):
                case.assertEqual(method,'thread/read')
                return {'thread':{'id':'wrong' if wrong else params['threadId'],'turns':[{'opaque':'synthetic'}] if params['includeTurns'] else []}}
        return Server
    def test_direct_acceptance_and_formal_cleanup_preserve_same_id_target_without_ui(self):
        c=self.case;s=c.spec
        proof=verify_direct(s,server_factory=self.factory(),account_reader=lambda _: {'identity_sha256':'fixture-account'})
        self.assertFalse(proof['ui_performed']);self.assertEqual(proof['native_ids_read'],['selected'])
        before=Path(s['native_files'][0]['target_path']).read_bytes();db=c.database.read_bytes()
        result=c.cleanup();self.assertTrue(result['goal_satisfied'])
        self.assertFalse((Path(s['source']['path'])/'sessions/rollout-selected.jsonl').exists())
        self.assertEqual(Path(s['native_files'][0]['target_path']).read_bytes(),before);self.assertEqual(c.database.read_bytes(),db)
    def test_wrong_native_id_never_qualifies_for_source_cleanup(self):
        with self.assertRaises(ValueError):verify_direct(self.case.spec,server_factory=self.factory(True),account_reader=lambda _: {'identity_sha256':'fixture-account'})
        self.assertTrue((Path(self.case.spec['source']['path'])/'sessions/rollout-selected.jsonl').exists())

if __name__=='__main__':unittest.main()
