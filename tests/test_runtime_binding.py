from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_work_skills_v3_e2e import ClientRegistryFixture, create_real_directory_link, manager
from work_skills_clients import load_client_model
from work_skills_clients.model import RuntimeBinding
from work_skills_clients.runtime_binding import (
    RuntimeBindingError, inspect_runtime_binding, validate_binding_evidence,
)


class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "claude.exe"
        self.binary.write_bytes(b"fixture executable v1")
        self.frontend = self.root / "package.json"
        self.frontend.write_text('{"version":"t3-v1"}', encoding="utf-8")
        self.home = self.root / "profile"
        self.settings = self.root / "settings.json"
        self.instance = {"driver": "claudeAgent", "config": {"binaryPath": str(self.binary), "homePath": str(self.home)}}
        self.write_settings()
        self.policy = RuntimeBinding("t3-provider", "skills", settings_path=self.settings,
                                     instance_id="claudeAgent", driver="claudeAgent", frontend_path=self.frontend)

    def write_settings(self):
        self.settings.write_text(json.dumps({"providerInstances": {"claudeAgent": self.instance}, "unrelatedSecret": "DO-NOT-PRINT"}), encoding="utf-8")

    def inspect(self):
        return inspect_runtime_binding(self.policy, self.home / "skills")

    def test_absolute_frontend_binding_does_not_require_a_path_command(self):
        snapshot = self.inspect()
        self.assertEqual(snapshot["binary"]["path"], str(self.binary))
        self.assertEqual(snapshot["frontend"]["version"], "t3-v1")
        self.assertNotIn("DO-NOT-PRINT", json.dumps(snapshot))
        self.assertFalse(self.home.exists())

    def test_executable_or_frontend_change_invalidates_evidence(self):
        original = self.inspect()
        evidence = {"adapterId": "t3-claude", "capturedAt": datetime.now(timezone.utc).isoformat(), "binding": original}
        validate_binding_evidence(evidence, "t3-claude", self.inspect())
        self.binary.write_bytes(b"fixture executable v2")
        with self.assertRaises(RuntimeBindingError):
            validate_binding_evidence(evidence, "t3-claude", self.inspect())
        self.frontend.write_text('{"version":"t3-v2"}', encoding="utf-8")
        self.assertNotEqual(original["frontend"], self.inspect()["frontend"])

    def test_disabled_wrong_driver_missing_binary_and_home_mismatch_fail_closed(self):
        variants = [
            {**self.instance, "enabled": False},
            {**self.instance, "driver": "another-driver"},
            {**self.instance, "config": {"binaryPath": str(self.root / "missing"), "homePath": str(self.home)}},
            {**self.instance, "config": {"binaryPath": str(self.binary), "homePath": str(self.root / "other-home")}},
        ]
        for instance in variants:
            with self.subTest(instance=instance):
                self.instance = instance
                self.write_settings()
                with self.assertRaises(RuntimeBindingError):
                    self.inspect()

    def test_malformed_settings_do_not_expose_content(self):
        self.settings.write_text("DO-NOT-PRINT invalid json", encoding="utf-8")
        with self.assertRaises(RuntimeBindingError) as caught:
            self.inspect()
        self.assertNotIn("DO-NOT-PRINT", str(caught.exception))

    def test_contexts_cannot_share_evidence(self):
        snapshot = self.inspect()
        evidence = {"adapterId": "claude", "capturedAt": datetime.now(timezone.utc).isoformat(), "binding": snapshot}
        with self.assertRaises(RuntimeBindingError):
            validate_binding_evidence(evidence, "t3-claude", snapshot)


class BindingManagerTests(unittest.TestCase):
    def prepare(self, base):
        fixture = ClientRegistryFixture(base / "repo")
        fixture.add_skill()
        binary = base / "runtime.exe"
        binary.write_bytes(b"v1")
        fixture.mapping["adapters"]["cindy"]["runtimeBinding"] = {
            "resolver": "explicit", "binaryPath": str(binary),
            "homePath": str(fixture.mounts["agents-shared"].parent), "skillsSubdir": "agents-shared",
        }
        fixture.write_config()
        return fixture, binary

    def test_plan_identity_changes_and_apply_rejects_runtime_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, binary = self.prepare(Path(tmp))
            options = fixture.options(["cindy"], yes=True)
            with fixture.context():
                plan = manager.build_plan(options)
                binary.write_bytes(b"v2")
                updated = manager.build_plan(options)
                self.assertNotEqual(plan.plan_id, updated.plan_id)
                with self.assertRaises(manager.BlockedError):
                    manager.apply_plan(plan, options)
                self.assertFalse(fixture.mounts["agents-shared"].exists())

    def test_legacy_aggregate_record_cannot_bypass_binding_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _ = self.prepare(Path(tmp))
            with fixture.context():
                target = manager.build_plan(fixture.options(["cindy"])).targets[0]
                state = {"nativeVerification": {
                    "status": "verified", "adapterIds": list(target.adapter_ids),
                    "consumerIds": list(target.consumer_ids), "mountPointId": target.mount_point_id,
                    "root": str(target.root), "evidence": {"method": "manual-native-check", "verifiedVersion": "v1", "verifiedOn": "2026-08-03"},
                }}
                self.assertFalse(manager.native_verification_is_current(state, target))

    def test_apply_rechecks_resolved_identity_even_if_file_preconditions_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, binary = self.prepare(Path(tmp))
            options = fixture.options(["cindy"], yes=True)
            with fixture.context():
                plan = manager.build_plan(options)
                binary.write_bytes(b"changed target behind an unchanged launcher link")
                with mock.patch.object(manager, "verify_plan_fingerprints"):
                    with self.assertRaisesRegex(manager.BlockedError, "Runtime identity changed"):
                        manager.apply_plan(plan, options)
            self.assertFalse(fixture.mounts["agents-shared"].exists())

    def test_change_between_identity_capture_and_seal_cannot_be_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, binary = self.prepare(Path(tmp))
            options = fixture.options(["cindy"], yes=True)
            original_seal = manager.seal_plan
            def change_then_seal(plan):
                binary.write_bytes(b"new runtime before sealing file metadata")
                original_seal(plan)
            with fixture.context():
                with mock.patch.object(manager, "seal_plan", side_effect=change_then_seal):
                    plan = manager.build_plan(options)
                manager.verify_plan_fingerprints(plan)
                with self.assertRaisesRegex(manager.BlockedError, "Runtime identity changed"):
                    manager.apply_plan(plan, options)
            self.assertFalse(fixture.mounts["agents-shared"].exists())

    def test_verified_record_expires_after_runtime_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, binary = self.prepare(Path(tmp))
            with fixture.context(), redirect_stdout(io.StringIO()):
                options = fixture.options(["cindy"], yes=True)
                plan = manager.build_plan(options)
                manager.apply_plan(plan, options)
                evidence_path = Path(tmp) / "binding.json"
                snapshot = plan.targets[0].runtime_bindings["cindy"]
                evidence_path.write_text(json.dumps({"adapterId": "cindy", "capturedAt": datetime.now(timezone.utc).isoformat(), "binding": snapshot}), encoding="utf-8")
                manager.run_clients_verify(options, "v1", True, binding_evidence_path=str(evidence_path))
                self.assertIn("OK_LINK", [a.kind for a in manager.build_plan(options).actions])
                binary.write_bytes(b"v2")
                self.assertIn("CONSUMER_NATIVE_UNVERIFIED", [a.kind for a in manager.build_plan(options).actions])
                with self.assertRaises(manager.BlockedError):
                    manager.run_clients_verify(options, "v2", True, binding_evidence_path=str(evidence_path))

    def test_shared_native_and_frontend_adapters_create_one_physical_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _ = self.prepare(Path(tmp))
            with fixture.context():
                plan = manager.build_plan(fixture.options(["cindy", "pi"]))
            links = [action for action in plan.actions if action.kind == "CREATE_LINK"]
            self.assertEqual(len(links), 1)

    def test_native_manifest_creation_conflicts_and_identity_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _ = self.prepare(Path(tmp))
            manifest_path = fixture.mounts["agents-shared"].with_suffix(".json")
            fixture.mapping["mountPoints"]["agents-shared"]["discoveryManifest"] = str(manifest_path)
            fixture.write_config()
            options = fixture.options(["cindy"], yes=True)
            with fixture.context(), redirect_stdout(io.StringIO()):
                plan = manager.build_plan(options)
                self.assertEqual(sum(a.kind == "CREATE_DISCOVERY_MANIFEST" for a in plan.actions), 1)
                manager.apply_plan(plan, options)
                expected = {"entries": [{"path": str(fixture.skills).replace("\\", "/")}]}
                self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), expected)
                updated = manager.build_plan(options)
                self.assertIn("OK_DISCOVERY_MANIFEST", [a.kind for a in updated.actions])
                self.assertNotEqual(plan.targets[0].runtime_bindings, updated.targets[0].runtime_bindings)
                manifest_path.write_text('{"entries":[{"path":"external-owner"}]}', encoding="utf-8")
                conflict = manager.build_plan(options)
                self.assertIn("DISCOVERY_MANIFEST_CONFLICT", [a.kind for a in conflict.blockers])
                with self.assertRaises(manager.BlockedError):
                    manager.apply_plan(conflict, options)
                self.assertIn("external-owner", manifest_path.read_text(encoding="utf-8"))

    def test_create_only_rejects_onboarding_but_allows_new_skill_after_root_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _ = self.prepare(Path(tmp))
            options = fixture.options(["cindy"], yes=True, create_only=True)
            with fixture.context(), redirect_stdout(io.StringIO()):
                with self.assertRaises(manager.BlockedError):
                    manager.apply_plan(manager.build_plan(options), options)
                fixture.mounts["agents-shared"].mkdir(parents=True)
                manager.apply_plan(manager.build_plan(options), options)
                self.assertTrue((fixture.mounts["agents-shared"] / "alpha" / "SKILL.md").exists())

    def test_replacement_journal_contains_recovery_before_the_link_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _ = self.prepare(Path(tmp))
            middle = Path(tmp) / "middle"
            create_real_directory_link(fixture.skills / "alpha", middle)
            link = fixture.mounts["agents-shared"] / "alpha"
            create_real_directory_link(middle, link)
            previous = manager.snapshot_link(link)
            options = fixture.options(["cindy"], yes=True, replace_wrong_links=True)
            original_remove = manager.remove_link
            def inspect_journal_then_remove(path):
                journal = next((fixture.state / "transactions").glob("*.json"))
                saved = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual(saved["replacedLinks"][0]["rawTarget"], previous.raw_target)
                self.assertEqual(saved["replacedLinks"][0]["kind"], previous.kind)
                self.assertIn("originalManifest", saved)
                self.assertFalse(saved["manifestExisted"])
                original_remove(path)
            with fixture.context(), redirect_stdout(io.StringIO()):
                plan = manager.build_plan(options)
                with mock.patch.object(manager, "remove_link", side_effect=inspect_journal_then_remove):
                    manager.apply_plan(plan, options)
            self.assertTrue(manager.direct_link_to(link, fixture.skills / "alpha"))


if __name__ == "__main__":
    unittest.main()
