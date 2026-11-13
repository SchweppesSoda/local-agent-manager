
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap
import json
import os
from pathlib import Path
import tempfile
import unittest

from local_agent_manager import paseo_localstorage as storage, office_leveldb as level
from tests.paseo_desktop_support import local_storage
from tests.test_paseo_fullflow import AGENT, KEEP


class PaseoLocalStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            level.runtime()
        except level.OfficeLevelDBError:
            if os.environ.get("LARJ_REQUIRE_LEVELDB_TESTS") == "1" or os.environ.get("LARJ_LEVELDB_RUNTIME") or __import__("local_agent_manager.office_leveldb", fromlist=["_runtime_directory"])._runtime_directory().exists():
                raise
            raise unittest.SkipTest("Pinned LevelDB runtime not configured")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)

    def test_native_rewrite_foreign_origin_preservation_and_cold_completed_family(self):
        local_storage(self.root, "srv_test", AGENT, KEEP)
        evidence = storage.freeze(self.root, "srv_test", [AGENT])
        self.assertNotIn("PRIVATE_PASEO", json.dumps(evidence))
        self.assertGreater(storage.remaining(evidence), 0)
        phases = []
        with storage.prepared(evidence) as family:
            storage.install(evidence, family, phase_callback=phases.append)
        self.assertEqual(storage.remaining(evidence), 0)
        self.assertEqual(phases, ["mutation_started"])
        self.assertFalse(list(self.root.glob('.larj-paseo-sealed-*')))
        with storage.prepared(evidence) as family:
            self.assertIsNone(family)
            storage.install(evidence, family, phase_callback=lambda _: self.fail("completed family must not repeat"))
        local_storage(self.root, "srv_test", AGENT, KEEP, inspect=True)

    def test_external_attachment_is_blocked_before_mutation(self):
        local_storage(self.root, "srv_test", AGENT, KEEP, attachment=True)
        before = level._snapshot(self.root, storage.RELATIVE)
        with self.assertRaisesRegex(level.OfficeLevelDBError, "attachment_closure_unverified"):
            storage.freeze(self.root, "srv_test", [AGENT])
        self.assertEqual(level._snapshot(self.root, storage.RELATIVE), before)
