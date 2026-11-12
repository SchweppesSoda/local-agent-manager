from contextlib import closing
import os
import sqlite3
import unittest
from unittest.mock import patch

from local_agent_manager import t3code_bound_adapter as bound, t3code_reference_batch as batch
from tests import test_t3code_fullflow as fixture
from tests.test_t3code_fullflow import THREAD, OTHER, V2


@unittest.skipUnless(os.name == 'nt', 'Windows namespace leases')
class ReferenceBatchTests(unittest.TestCase):
    setUp = fixture.T3CodeFullflowTests.setUp

    def document(self):
        return {'actions': [{'kind': 'delete_t3code_frontend', 'impact': {'external_action_payload':
            {'t3code_evidence': {'root': str(self.root), 'binding_manifest': self.manifest}}}}]}

    def test_shared_scan_excludes_writers_and_new_batch_sees_committed_changes(self):
        document = self.document()
        second = bound.T3CodeBoundAdapter(self.manifest)
        with (patch('local_agent_manager.t3code_lifecycle.current_boundary', return_value=object()),
              patch.object(bound, 'history_references', wraps=bound.history_references) as scan):
            with batch.hold(document, 'delete_conversation'):
                snapshot = self.adapter.snapshot_references(refresh=True)
                for _ in range(12):
                    self.assertEqual(second.snapshot_references(refresh=True), snapshot)
                self.assertEqual(scan.call_count, 1)
                with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite', timeout=0)) as writer:
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                        writer.execute(f'UPDATE {V2}threads SET title=?', ('concurrent change',))
                with self.assertRaises(OSError):
                    (self.root/'userdata/environment-id').write_text('changed')
                with self.assertRaises(OSError):
                    self.home.rename(self.base/'replaced-home')
            with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as writer:
                writer.execute(f"UPDATE {V2}provider_threads SET payload_json=json_set(payload_json,'$.nativeThreadRef.nativeId',?) WHERE thread_id=?", (OTHER, THREAD))
                writer.commit()
            with batch.hold(document, 'delete_conversation'):
                changed = second.snapshot_references(refresh=True)
                self.assertEqual(scan.call_count, 2)
                self.assertEqual({r.native_id for r in changed.references if r.frontend_id == THREAD}, {OTHER})
            self.adapter.snapshot_references(refresh=True)
            self.assertEqual(scan.call_count, 3)

    def test_frontend_and_cold_reads_never_reuse_native_batch_snapshot(self):
        with patch.object(bound, 'history_references', wraps=bound.history_references) as scan:
            with batch.hold(self.document(), 'delete_t3code_frontend'):
                self.adapter.snapshot_references(refresh=True)
                self.adapter.snapshot_references(refresh=True)
            self.assertEqual(scan.call_count, 2)
        with self.assertRaisesRegex(Exception, 'boundary_missing'):
            with batch.hold(self.document(), 'delete_conversation'):
                self.fail('unapproved cache entered')

    def test_exception_releases_transaction_and_cache(self):
        with patch('local_agent_manager.t3code_lifecycle.current_boundary', return_value=object()):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                with batch.hold(self.document(), 'delete_conversation'):
                    self.adapter.snapshot_references(refresh=True)
                    raise RuntimeError('interrupted')
        self.assertIsNone(batch._batch.get())
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite', timeout=0)) as writer:
            writer.execute('BEGIN IMMEDIATE')
