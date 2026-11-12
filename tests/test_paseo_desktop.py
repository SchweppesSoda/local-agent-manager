"""Real Electron/LevelDB on fresh synthetic profiles, never the Paseo app."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import paseo_indexeddb as idb, paseo_cleanup, paseo_cleanup_files as files
from local_agent_record_janitor.office_leveldb import _environment
from tests.paseo_desktop_support import local_storage
from tests.test_paseo_fullflow import AGENT, KEEP


@unittest.skipUnless(os.name == "nt", "Pinned Chromium distribution is Windows-only")
class PaseoDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = os.environ.get("LARJ_PASEO_ELECTRON_RUNTIME")
        if not raw:
            if os.environ.get("LARJ_REQUIRE_PASEO_BROWSER_TESTS") == "1":
                raise AssertionError("LARJ_PASEO_ELECTRON_RUNTIME is required")
            raise unittest.SkipTest("Explicit pinned Electron fixture runtime not configured")
        cls.runtime = Path(raw).resolve(strict=True)
        idb.runtime(cls.runtime)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="larj-paseo-fixture-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve(strict=True)
        self.profile = self.base / "session"

    def fixture(self, *, mode="seed", variant="normal"):
        request = self.base / ("request-" + mode + ".json")
        request.write_text(json.dumps({"schema_version": "larj.paseo-native-fixture.v1", "fixtureRoot": str(self.base),
            "mode": mode, "variant": variant, "serverId": "srv_test", "selectedId": AGENT, "keepId": KEEP}), encoding="utf-8")
        result = subprocess.run([str(self.runtime / "electron.exe"), str(Path(__file__).parent / "fixtures/paseo_native_seed.cjs"), str(request)],
            cwd=self.base, env=_environment(), capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, "Synthetic Electron fixture failed")
        values = [json.loads(line) for line in result.stdout.splitlines() if line.startswith(b'{')]
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["status"], "verified", values)
        return values[0]

    def assert_preserved(self, before, after):
        self.assertEqual(before["selectedRecords"], 2)
        self.assertEqual(after["selectedRecords"], 0)
        for key in ("unselectedRowsSha256", "otherDatabaseSha256", "schemaSha256", "remoteSameIdRecords"):
            self.assertEqual(before[key], after[key], key)

    def test_real_native_deletion_preservation_and_cold_completed_family(self):
        before = self.fixture()
        evidence = idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        self.assertNotIn("TEMP_SELECTED", json.dumps(evidence))
        with idb.prepared(evidence) as family:
            idb.install(evidence, family, phase_callback=lambda _: None)
        self.assertEqual(idb.remaining(evidence), 0)
        with idb.prepared(evidence) as family:
            self.assertIsNone(family)
            idb.install(evidence, family, phase_callback=lambda _: self.fail("completed install repeated"))
        self.assert_preserved(before, self.fixture(mode="inspect"))

    def test_selected_external_json_payload_is_deleted_with_independent_cold_verification(self):
        before = self.fixture(variant="wrapped-selected")
        evidence = idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        self.assertTrue(evidence["blobs"]["files"])
        self.assertEqual(idb.remaining(evidence), 2)
        with idb.prepared(evidence) as family:
            idb.install(evidence, family, phase_callback=lambda _: None)
        self.assertFalse((self.profile / idb.BLOBS).exists())
        self.assertEqual(idb.remaining(evidence), 0)
        self.assert_preserved(before, self.fixture(mode="inspect", variant="wrapped-selected"))

    def test_unselected_external_files_block_without_modification(self):
        self.fixture(variant="wrapped-keep")
        before = idb._snapshot(self.profile, idb.RELATIVE)
        blobs = idb._blob_snapshot(self.profile)
        with self.assertRaises(idb.PaseoIndexedDBError):
            idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        self.assertEqual(idb._snapshot(self.profile, idb.RELATIVE), before)
        self.assertEqual(idb._blob_snapshot(self.profile), blobs)

    def test_orphaned_or_missing_external_file_blocks_without_changing_database(self):
        self.fixture(variant="wrapped-selected")
        before = idb._snapshot(self.profile, idb.RELATIVE)
        blobs = idb._blob_snapshot(self.profile)
        source = self.profile / blobs["files"][0]["path"]
        orphan = source.with_name(source.name + "0")
        orphan.parent.mkdir(parents=True, exist_ok=True)
        # Use a canonical bucket so refusal is specifically the graph closure.
        orphan = orphan.parent.parent / f"{(int(orphan.name, 16) >> 8) & 255:02x}" / orphan.name
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(b"SYNTHETIC_UNOWNED")
        with self.assertRaises(idb.PaseoIndexedDBError):
            idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        self.assertEqual(idb._snapshot(self.profile, idb.RELATIVE), before)
        orphan.unlink()
        source.unlink()
        with self.assertRaises(idb.PaseoIndexedDBError):
            idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        self.assertEqual(idb._snapshot(self.profile, idb.RELATIVE), before)

    def test_new_or_changed_blob_after_plan_blocks_before_mutation(self):
        self.fixture(variant="wrapped-selected")
        evidence = idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        source = self.profile / evidence["blobs"]["files"][0]["path"]
        source.write_bytes(source.read_bytes() + b"SYNTHETIC_CHANGED")
        with self.assertRaisesRegex(idb.PaseoIndexedDBError, "blob_source_changed"):
            with idb.prepared(evidence):
                self.fail("changed external payload reached preparation")

    def test_interrupted_blob_disposal_retains_seal_and_requires_recovery(self):
        self.fixture(variant="wrapped-selected")
        evidence = idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        original = idb.frozen_files.apply_remove
        def interrupted(root, proof):
            if Path(root) == self.profile:
                raise OSError("synthetic external-file disposal interruption")
            return original(root, proof)
        with idb.prepared(evidence) as family:
            with patch.object(idb.frozen_files, "apply_remove", interrupted), self.assertRaises(OSError):
                idb.install(evidence, family, phase_callback=lambda _: None)
        self.assertEqual(idb._blob_snapshot(self.profile), evidence["blobs"])
        self.assertEqual(len(list(self.profile.glob('.larj-paseo-sealed-*'))), 1)
        with self.assertRaisesRegex(idb.PaseoIndexedDBError, "cache_install_recovery_required"):
            idb.remaining(evidence)

    def test_interrupted_install_retains_cold_verified_seal(self):
        self.fixture()
        evidence = idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
        original = Path.open
        def interrupted(path, mode="r", *args, **kwargs):
            if path.parent == self.profile / idb.RELATIVE and mode == "xb":
                raise OSError("synthetic install interruption")
            return original(path, mode, *args, **kwargs)
        with idb.prepared(evidence) as family:
            with patch.object(Path, "open", interrupted), self.assertRaises(OSError):
                idb.install(evidence, family, phase_callback=lambda _: None)
        seals = list(self.profile.glob('.larj-paseo-sealed-*'))
        self.assertEqual(len(seals), 1)
        with self.assertRaisesRegex(idb.PaseoIndexedDBError, "cache_install_recovery_required"):
            idb.remaining(evidence)
        snapshot = idb._snapshot(seals[0], idb.RELATIVE)
        cold = self.base / "sealed-cold"
        idb._copy(seals[0], snapshot, cold)
        observed = idb._observe(cold, self.base, evidence["runtime"], "srv_test", [AGENT])
        self.assertEqual(observed["before_sha256"], evidence["observation"]["after_sha256"])
        self.assertEqual(observed["selected"], 0)

    def test_unqualified_blob_legacy_and_key_generator_fail_before_mutation(self):
        for variant in ("blob", "legacy", "auto-increment"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(prefix="larj-paseo-fixture-") as raw:
                self.base = Path(raw).resolve(strict=True)
                self.profile = self.base / "session"
                self.fixture(variant=variant)
                before = idb._snapshot(self.profile, idb.RELATIVE)
                with self.assertRaises(idb.PaseoIndexedDBError):
                    idb.freeze(self.profile, self.runtime, "srv_test", [AGENT])
                self.assertEqual(idb._snapshot(self.profile, idb.RELATIVE), before)

    def test_bound_desktop_coordinator_plan_apply_cold_verify(self):
        from tests.test_paseo_fullflow import PaseoFullflowTests
        before = self.fixture()
        local_storage(self.profile, "srv_test", AGENT, KEEP)
        server = PaseoFullflowTests()
        server.setUp()
        self.addCleanup(server.doCleanups)
        server.manifest["desktop_profiles"] = [{"root": str(self.profile), "electron_runtime": str(self.runtime),
                                               "runtime_binaries": [str(server.binary)]}]
        from local_agent_record_janitor.paseo_bound_adapter import PaseoBoundAdapter
        server.adapter = PaseoBoundAdapter(server.manifest)
        plan = server.plan()
        result = server.coordinator().apply_operation(operation_id=plan["operation_id"], plan_path=server.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        verified = server.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=server.plan_path)
        self.assertEqual(verified["goal_status"], "complete", verified)
        self.assert_preserved(before, self.fixture(mode="inspect"))
        local_storage(self.profile, "srv_test", AGENT, KEEP, inspect=True)

    def managed_image_server(self):
        from tests.test_paseo_fullflow import PaseoFullflowTests
        from tests.test_paseo_managed_attachments import IMAGE, PNG
        from local_agent_record_janitor.paseo_bound_adapter import PaseoBoundAdapter
        server = PaseoFullflowTests()
        server.setUp()
        self.addCleanup(server.doCleanups)
        server.manifest["desktop_profiles"] = [{"root": str(self.profile), "electron_runtime": str(self.runtime),
                                               "runtime_binaries": [str(server.binary)]}]
        server.adapter = PaseoBoundAdapter(server.manifest)
        images = server.root / "desktop-attachments"
        images.mkdir()
        (images / IMAGE).write_bytes(PNG)
        return server

    def test_full_profile_cold_cleanup_removes_managed_images_and_wrapped_payload(self):
        self.fixture(variant="wrapped-selected")
        local_storage(self.profile, "srv_test", AGENT, KEEP)
        server = self.managed_image_server()
        plan = server.coordinator().plan_operation(client="paseo", record_ids=(AGENT, KEEP),
            adapters=(server.adapter,), plan_path=server.plan_path)
        self.assertEqual(plan["goal_status"], "ready", plan)
        result = server.coordinator().apply_operation(operation_id=plan["operation_id"], plan_path=server.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        cold = server.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=server.plan_path)
        self.assertEqual(cold["goal_status"], "complete", cold)
        self.assertFalse((server.root / "desktop-attachments").exists())
        self.assertFalse((self.profile / idb.BLOBS).exists())

    def test_remote_cached_attachment_reference_blocks_full_profile_image_disposal(self):
        self.fixture(variant="managed-reference")
        server = self.managed_image_server()
        plan = server.coordinator().plan_operation(client="paseo", record_ids=(AGENT, KEEP),
            adapters=(server.adapter,), plan_path=server.plan_path)
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertIn("managed_attachment_preserved_reference", str(plan))
        self.assertTrue((server.root / "agents" / (AGENT + ".json")).exists())

    def test_unselected_modal_draft_protects_its_managed_image(self):
        self.fixture()
        local_storage(self.profile, "srv_test", AGENT, KEEP, managed_reference=True)
        server = self.managed_image_server()
        plan = server.coordinator().plan_operation(client="paseo", record_ids=(AGENT, KEEP),
            adapters=(server.adapter,), plan_path=server.plan_path)
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertIn("managed_attachment_preserved_reference", str(plan))
        self.assertTrue((server.root / "desktop-attachments").exists())


if __name__ == "__main__":
    unittest.main()
