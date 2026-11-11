"""A server-empty result cannot certify an unqualified desktop replica."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from local_agent_manager import t3code_localstorage as storage
from local_agent_manager.t3code_store import T3CodeError


LEVELDB = "IndexedDB/t3code_app_0.indexeddb.leveldb"
BLOB = "IndexedDB/t3code_app_0.indexeddb.blob"


class DesktopAdmissionTests(unittest.TestCase):
    def test_unqualified_replica_blocks_all_storage_entry_points(self):
        for relative in (LEVELDB, BLOB):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                evidence = storage.freeze(root, "env-1", ["thread-1"])
                family = root / relative
                family.mkdir(parents=True)
                retained = family / "retained"
                retained.write_bytes(b"unselected thread and another environment")
                before = hashlib.sha256(retained.read_bytes()).hexdigest()
                with self.assertRaisesRegex(T3CodeError, "desktop_indexeddb_unqualified"):
                    storage.freeze(root, "env-1", ["thread-1"])
                with self.assertRaisesRegex(T3CodeError, "desktop_indexeddb_unqualified"):
                    storage.remaining(evidence)
                with self.assertRaisesRegex(T3CodeError, "desktop_indexeddb_unqualified"):
                    with storage.prepared(evidence):
                        self.fail("An unqualified replica was admitted")
                with self.assertRaisesRegex(T3CodeError, "desktop_indexeddb_unqualified"):
                    storage.install(evidence, None, phase_callback=self.fail)
                self.assertEqual(hashlib.sha256(retained.read_bytes()).hexdigest(), before)
