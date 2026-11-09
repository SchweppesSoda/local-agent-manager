from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from local_agent_manager.skills import core
from local_agent_manager.skills.coordination import shared_resource_locks, SharedResourceBusyError
from local_agent_manager.providers.t3 import provider_instance, strict_json
from tests.test_work_skills_v3_e2e import ClientRegistryFixture, manager


class ManagerCompatibilityTests(unittest.TestCase):
    def test_legacy_submodules_share_types_and_patch_targets(self):
        for name in ("cli", "models", "operation_store", "environment_config", "adapters.base", "adapters.cindy"):
            old = importlib.import_module("local_agent_record_janitor." + name)
            current = importlib.import_module("local_agent_manager." + name)
            self.assertIs(old, current)
        with mock.patch("local_agent_record_janitor.environment_config.MAX_CONFIG_BYTES", 11):
            self.assertEqual(importlib.import_module("local_agent_manager.environment_config").MAX_CONFIG_BYTES, 11)

    def test_both_import_orders_in_new_processes(self):
        for first, second in (("local_agent_manager", "local_agent_record_janitor"), ("local_agent_record_janitor", "local_agent_manager")):
            script = f"import importlib; a=importlib.import_module('{first}.adapters.cindy'); b=importlib.import_module('{second}.adapters.cindy'); assert a is b"
            subprocess.run([sys.executable, "-B", "-c", script], check=True, capture_output=True)

    def test_new_and_old_module_entry_points(self):
        for name in ("local_agent_manager", "local_agent_record_janitor"):
            result = subprocess.run([sys.executable, "-B", "-m", name, "--help"], text=True, encoding="utf-8", capture_output=True, check=True)
            self.assertIn("skills", result.stdout)
            self.assertIn("environment", result.stdout)

    def test_strict_provider_projection(self):
        with self.assertRaises(ValueError):
            strict_json('{"binaryPath":"first","binaryPath":"second"}')
        settings = {"providerInstances": {"custom": {"driver": "claudeAgent", "config": {"enabled": False}}}}
        self.assertFalse(provider_instance(settings, "custom", "claudeAgent")["enabled"])
        settings["providerInstances"]["custom"]["config"]["enabled"] = "false"
        with self.assertRaises(ValueError):
            provider_instance(settings, "custom", "claudeAgent")

    def test_configuration_resolves_source_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ClientRegistryFixture(Path(temporary))
            fixture.mapping["repo"]["root"] = str(fixture.root)
            fixture.write_config()
            self.assertEqual(core.repo_root_from_args(None, str(fixture.config)), fixture.root.resolve())
            other = fixture.root / "other"
            with self.assertRaises(core.ConfigError):
                core.main(["clients", "detect", "--repo-root", str(other), "--config", str(fixture.config)])

    def test_relative_config_and_repo_root_use_one_directory_basis(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = ClientRegistryFixture(base / "source")
            fixture.mapping["repo"]["root"] = ".."
            fixture.write_config()
            output = io.StringIO()
            with mock.patch("os.getcwd", return_value=str(base)), redirect_stdout(output):
                result = core.main(["clients", "detect", "--config", "source/config/skill-targets.yaml", "--format", "json"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["version"], 3)

    def test_workspace_resource_rules_are_explicit_source_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ClientRegistryFixture(Path(temporary))
            marker = fixture.add_skill() / "SKILL.md"
            marker.write_text(marker.read_text(encoding="utf-8") + "\nInspect `C:/Users/example/Desktop/diagnostic.txt`.\n", encoding="utf-8")
            self.assertEqual(core.run_content_audit(fixture.root, fixture.config).errors, [])
            fixture.mapping["sourceAudit"] = {"forbidDesktopResourcePaths": True}
            fixture.write_config()
            errors = core.run_content_audit(fixture.root, fixture.config).errors
            self.assertEqual([item.code for item in errors], ["DESKTOP_RESOURCE_PATH"])


class MultiSourceManifestTests(unittest.TestCase):
    def fixture(self, root, manifest):
        fixture = ClientRegistryFixture(root)
        target_root = manifest.with_suffix("")
        fixture.mounts["agents-shared"] = target_root
        fixture.mapping["mountPoints"]["agents-shared"]["root"] = str(target_root)
        fixture.mapping["targets"]["agents-shared"]["root"] = str(target_root)
        fixture.mapping["mountPoints"]["agents-shared"]["discoveryManifest"] = str(manifest)
        fixture.write_config()
        fixture.add_skill()
        return fixture

    def test_additive_registration_is_idempotent_and_retains_unknown_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "other"
            other.mkdir()
            manifest = root / "skills.json"
            original = {"custom": {"retained": True}, "entries": [{"path": str(other), "label": "external"}, {"path": str(other)}]}
            manifest.write_text(json.dumps(original), encoding="utf-8")
            fixture = self.fixture(root / "repo", manifest)
            with fixture.context(), redirect_stdout(io.StringIO()):
                plan = manager.build_plan(fixture.options(yes=True))
                self.assertFalse(plan.blockers)
                self.assertEqual(sum(a.kind == "UPDATE_DISCOVERY_MANIFEST" for a in plan.actions), 1)
                manager.apply_plan(plan, fixture.options(yes=True))
                actual = json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(actual["entries"][:-1], original["entries"])
                self.assertEqual(actual["custom"], original["custom"])
                before = manifest.stat().st_mtime_ns
                plan = manager.build_plan(fixture.options(yes=True))
                self.assertFalse(any(a.kind == "UPDATE_DISCOVERY_MANIFEST" for a in plan.actions))
                manager.apply_plan(plan, fixture.options(yes=True))
                self.assertEqual(before, manifest.stat().st_mtime_ns)

    def test_other_source_same_name_is_blocked_without_target_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "other/alpha"
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text("---\nname: alpha\ndescription: Other source\n---\n", encoding="utf-8")
            manifest = root / "skills.json"
            manifest.write_text(json.dumps({"entries": [{"path": str(other.parent)}]}), encoding="utf-8")
            fixture = self.fixture(root / "repo", manifest)
            with fixture.context():
                plan = manager.build_plan(fixture.options())
            self.assertIn("DISCOVERY_SKILL_NAME_CONFLICT", [a.kind for a in plan.blockers])

    def test_duplicate_self_registration_and_create_only_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "skills.json"
            fixture = self.fixture(root / "repo", manifest)
            manifest.write_text(json.dumps({"entries": [{"path": str(fixture.skills)}] * 2}), encoding="utf-8")
            with fixture.context():
                self.assertTrue(manager.build_plan(fixture.options()).blockers)
                manifest.write_text('{"entries":[]}', encoding="utf-8")
                options = fixture.options(yes=True, create_only=True)
                with self.assertRaises(manager.BlockedError):
                    manager.apply_plan(manager.build_plan(options), options)

    def test_rollback_does_not_overwrite_external_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "skills.json"
            path.write_bytes(b"ours")
            core.restore_discovery_manifest(path, b"before", b"ours")
            self.assertEqual(path.read_bytes(), b"before")
            path.write_bytes(b"external")
            with self.assertRaises(core.TransactionError):
                core.restore_discovery_manifest(path, b"before", b"ours")
            self.assertEqual(path.read_bytes(), b"external")

    def test_resource_locks_coordinate_separate_sources(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"LOCAL_AGENT_MANAGER_LOCK_DIR": temporary}):
            path = Path(temporary) / "target"
            script = ("import sys; from pathlib import Path; from local_agent_manager.skills.coordination import shared_resource_locks; "
                      "lock=shared_resource_locks([Path(sys.argv[1])]); lock.__enter__(); print('ready',flush=True); sys.stdin.read(1); lock.__exit__(None,None,None)")
            process = subprocess.Popen([sys.executable, "-B", "-c", script, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaises(SharedResourceBusyError), shared_resource_locks([path]):
                    pass
            finally:
                process.communicate("x", timeout=10)
            self.assertEqual(process.returncode, 0)


class StandaloneProbeTests(unittest.TestCase):
    def test_probe_runs_without_manager_or_site_packages(self):
        interpreter = getattr(sys, "_base_executable", sys.executable)
        with tempfile.TemporaryDirectory(prefix="manager probe ") as temporary:
            destination = Path(temporary) / "evidence.json"
            context = Path(temporary) / "context"
            environment = os.environ.copy()
            environment["MANAGER_TEST_CONTEXT"] = str(context)
            environment["WORK_SKILLS_SYSTEM_PYTHON"] = interpreter
            script = Path(core.__file__).with_name("probes") / "capture_runtime_evidence.py"
            result = subprocess.run([interpreter, "-I", "-S", str(script), "--adapter", "fixture", "--consumer", "fixture", "--context-id", "fixture", "--context-environment", "MANAGER_TEST_CONTEXT", "--expected-context", str(context), "--required-version", f"{sys.version_info.major}.{sys.version_info.minor}", "--output", str(destination)], env=environment, text=True, encoding="utf-8", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(evidence["adapterId"], "fixture")
            self.assertEqual(evidence["method"], "client-native-python-probe")
