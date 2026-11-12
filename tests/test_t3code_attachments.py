from contextlib import closing
import json
import os
import sqlite3
import unittest

from local_agent_manager import t3code_cleanup_files as files
from tests import test_t3code_fullflow as fixture
from tests.t3code_attachment_support import seed_attachments
from tests.t3code_support import V2, add_thread


@unittest.skipUnless(os.name == 'nt', 'Windows full local deletion')
class AttachmentDeletionTests(unittest.TestCase):
    setUp = fixture.T3CodeFullflowTests.setUp
    coordinator = fixture.T3CodeFullflowTests.coordinator
    plan = fixture.T3CodeFullflowTests.plan
    apply = fixture.T3CodeFullflowTests.apply

    def test_upload_sidecar_html_and_mirrors_delete_with_cold_verification(self):
        paths, _, _, _ = seed_attachments(self.root,fixture.THREAD)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            kept = db.execute(f'SELECT payload_json FROM {V2}messages WHERE thread_id=?',(fixture.KEEP,)).fetchall()
        plan = self.plan()
        result = self.apply(plan)
        self.assertEqual(result['goal_status'],'complete',result)
        self.assertTrue(all(not p.exists() for p in paths))
        result = self.coordinator().verify_operation(operation_id=plan['operation_id'],plan_path=self.plan_path)
        self.assertTrue(result['goal_satisfied'],result)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            self.assertEqual(db.execute(f'SELECT payload_json FROM {V2}messages WHERE thread_id=?',(fixture.KEEP,)).fetchall(),kept)

    def test_sanitized_attachment_handle_in_retained_row_blocks_before_deletion(self):
        owner = 'thread:selected-attachment'
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db,owner)
        paths, attachment, _, _ = seed_attachments(self.root,owner)
        self.assertNotIn(owner,attachment['id'])
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?',
                (json.dumps({'restore':{'attachmentId':attachment['id']}}),fixture.KEEP));db.commit()
        with self.assertRaisesRegex(ValueError,'unselected_restore_reference'):
            files.freeze(self.root,[owner])
        self.assertTrue(all(p.exists() for p in paths))

    def test_unknown_context_source_and_file_drift_block(self):
        paths, attachment, message, _ = seed_attachments(self.root,fixture.THREAD)
        changed=json.loads(json.dumps(message));changed['context']['records'][0]['externalPath']='outside.pptx'
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?',(json.dumps(changed),fixture.THREAD));db.commit()
        with self.assertRaisesRegex(ValueError,'attachment_closure_unqualified'):
            files.freeze(self.root,[fixture.THREAD])
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?',(json.dumps(message),fixture.THREAD));db.commit()
        paths[0].write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'attachment_size_changed'):
            files.freeze(self.root,[fixture.THREAD])

    def test_approved_file_change_and_unknown_sibling_refuse_install(self):
        paths, _, _, _ = seed_attachments(self.root,fixture.THREAD)
        evidence=files.freeze(self.root,[fixture.THREAD])
        paths[2].write_bytes(b'changed html')
        with self.assertRaisesRegex(ValueError,'file_state_unknown'):
            files.apply(evidence,phase_callback=lambda _:self.fail('writer dispatched'))
        paths[2].write_bytes(b'synthetic attachment')
        sibling=paths[0].with_suffix('.sql');sibling.write_bytes(b'unknown sibling')
        with self.assertRaisesRegex(ValueError,'attachment_closure_unqualified'):
            files.freeze(self.root,[fixture.THREAD])
