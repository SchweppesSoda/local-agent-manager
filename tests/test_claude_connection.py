from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_agent_record_janitor import claude_connection, frozen_files
from local_agent_record_janitor.claude_connection import (
    PLAN_SCHEMA,
    RESULT_SCHEMA,
    ClaudeConnectionError,
    apply_connection,
    prepare_connection,
)
from local_agent_record_janitor.operation_store import plan_sha256


class ClaudeConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name).resolve()
        self.binary = self.root / "CindyGlobal/claude-code/2.1.312/claude.exe"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"fixture only")
        self.home = self.root / "native history"
        self.home.mkdir()
        self.paseo = self.root / "paseo/config.json"
        self.paseo.parent.mkdir()
        self.t3 = self.root / "t3/userdata/settings.json"
        self.t3.parent.mkdir(parents=True)

    def save(self, config: Path, value: dict) -> bytes:
        raw = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        config.write_bytes(raw)
        return raw

    def prepare(self, target: str = "paseo", **options) -> dict:
        fields = {
            "target": target,
            "config_path": self.paseo if target == "paseo" else self.t3,
            "claude_binary": self.binary,
            "claude_home": self.home,
        }
        fields.update(options)
        return prepare_connection(**fields)

    def apply(self, plan: dict, **options) -> dict:
        values = {"approved_sha256": plan["plan_sha256"], "clients_closed": True}
        values.update(options)
        return apply_connection(plan, **values)

    def assert_code(self, code: str, call, *args, **kwargs) -> None:
        with self.assertRaises(ClaudeConnectionError) as exc:
            call(*args, **kwargs)
        self.assertEqual(exc.exception.code, code)

    def test_paseo_replaces_only_reviewed_fields_and_redacts_old_values(self) -> None:
        original = {
            "version": 1,
            "agents": {"providers": {
                "codex": {"command": ["codex"]},
                "claude-cindy": {
                    "extends": "claude", "label": "custom label",
                    "command": ["PRIVATE_OLD_COMMAND", "--model", "PRIVATE_ARGUMENT"],
                    "env": {"CLAUDE_CONFIG_DIR": "PRIVATE_OLD_HOME", "SOME_KEY": "PRIVATE_SECRET"},
                    "enabled": False, "description": "preserved",
                },
            }},
            "other": {"value": "PRIVATE_OTHER"},
        }
        original_bytes = self.save(self.paseo, original)
        self.binary.parent.joinpath("auth.json").write_bytes(b"never read")
        (self.home / "history.jsonl").write_bytes(b"never read")
        with patch.object(Path, "home", side_effect=AssertionError("host home")), patch(
            "subprocess.Popen", side_effect=AssertionError("launched executable"),
        ), patch.object(frozen_files, "read_file", wraps=frozen_files.read_file) as read:
            plan = self.prepare()
            self.assertEqual(self.paseo.read_bytes(), original_bytes)
            self.assertEqual(plan["schema_version"], PLAN_SCHEMA)
            self.assertEqual(plan["field_changes"], {
                "provider_created": False, "provider_enabled": False,
                "command_executable": "different", "retained_argument_count": 2,
                "CLAUDE_CONFIG_DIR": "different",
            })
            result = self.apply(json.loads(json.dumps(plan)))
        self.assertEqual({call.args[0] for call in read.call_args_list}, {self.paseo.parent})
        self.assertNotIn("PRIVATE_", json.dumps((plan, result)))
        self.assertEqual(result["schema_version"], RESULT_SCHEMA)
        self.assertTrue(result["modified"])
        self.assertFalse(result["provider_enabled"])
        original["agents"]["providers"]["claude-cindy"]["command"][0] = str(self.binary)
        original["agents"]["providers"]["claude-cindy"]["env"]["CLAUDE_CONFIG_DIR"] = str(self.home)
        self.assertEqual(json.loads(self.paseo.read_bytes()), original)

    def test_create_paseo_slot_only_in_existing_v1_config(self) -> None:
        self.save(self.paseo, {"version": 1, "existing": {"keep": True}})
        plan = self.prepare(provider_id="claude-custom")
        self.assertTrue(plan["field_changes"]["provider_created"])
        self.apply(plan)
        config = json.loads(self.paseo.read_bytes())
        self.assertEqual(config["existing"], {"keep": True})
        self.assertEqual(config["agents"]["providers"], {
            "claude-custom": {
                "extends": "claude", "label": "Claude (Cindy)",
                "command": [str(self.binary)], "env": {"CLAUDE_CONFIG_DIR": str(self.home)},
            }
        })
        self.assert_code("invalid_provider", self.prepare, provider_id="codex")

    def test_t3_upserts_default_and_preserves_the_rest_of_the_document(self) -> None:
        original = {
            "providerInstances": {"codex": {"driver": "codex", "config": {"binaryPath": "untouched"}}},
            "other": "value",
        }
        self.save(self.t3, original)
        plan = self.prepare("t3code")
        self.assertEqual(plan["provider_id"], "claudeAgent")
        self.assertTrue(plan["field_changes"]["provider_created"])
        self.apply(plan)
        original["providerInstances"]["claudeAgent"] = {
            "driver": "claudeAgent", "config": {"binaryPath": str(self.binary), "homePath": str(self.home)},
        }
        self.assertEqual(json.loads(self.t3.read_bytes()), original)

    def test_empty_and_current_t3_settings_can_create_the_default_instance(self) -> None:
        for original in ({}, {"enableProviderUpdateChecks": False}):
            with self.subTest(original=original):
                self.save(self.t3, original)
                plan = self.prepare("t3code")
                self.assertTrue(plan["field_changes"]["provider_created"])
                self.apply(plan)
                updated = json.loads(self.t3.read_bytes())
                self.assertEqual(updated.pop("providerInstances")["claudeAgent"]["driver"], "claudeAgent")
                self.assertEqual(updated, original)

    def test_already_configured_t3_is_an_exact_byte_noop(self) -> None:
        self.save(self.t3, {"providerInstances": {"claudeAgent": {
            "driver": "claudeAgent", "config": {
                "binaryPath": str(self.binary), "homePath": str(self.home), "enabled": False,
            },
        }}})
        raw = self.t3.read_bytes()
        plan = self.prepare("t3code")
        self.assertEqual(plan["status"], "already_configured")
        with patch.object(frozen_files, "apply_rewrite", side_effect=AssertionError("wrote no-op")):
            result = self.apply(plan)
        self.assertFalse(result["modified"])
        self.assertEqual(result["status"], "already_configured")
        self.assertFalse(result["provider_enabled"])
        self.assertEqual(self.t3.read_bytes(), raw)

    def test_approval_and_closed_confirmation_are_required_before_reading(self) -> None:
        self.save(self.paseo, {"version": 1})
        plan = self.prepare()
        tampered = {**plan, "claude_binary": str(self.root / "elsewhere/claude.exe")}
        with patch.object(frozen_files, "read_file", side_effect=AssertionError("read before approval")):
            self.assert_code("clients_not_closed", self.apply, plan, clients_closed=False)
            self.assert_code("clients_not_closed", self.apply, plan, clients_closed=1)
            self.assert_code("approval_mismatch", self.apply, plan, approved_sha256="a" * 64)
            self.assert_code("approval_mismatch", self.apply, tampered)

    def test_rehashed_forged_change_and_config_drift_are_blocked(self) -> None:
        self.save(self.paseo, {"version": 1})
        plan = self.prepare()
        forged = {**plan, "field_changes": {}}
        forged["plan_sha256"] = plan_sha256(forged)
        self.assert_code("stale_plan", self.apply, forged)
        new = self.save(self.paseo, {"version": 1, "unrelated": True})
        self.assert_code("stale_plan", self.apply, plan)
        self.assertEqual(self.paseo.read_bytes(), new)

    def test_same_path_replaced_binary_invalidates_approved_config(self) -> None:
        unchanged = self.save(self.paseo, {"version": 1})
        plan = self.prepare()
        self.binary.write_bytes(b"different fixture size")
        self.assert_code("stale_plan", self.apply, plan)
        self.assertEqual(self.paseo.read_bytes(), unchanged)

    def test_invalid_json_and_unknown_provider_schema_are_rejected(self) -> None:
        for body in (
            b'{"version":1,"agents":{"PRIVATE_DUPLICATE":1,"PRIVATE_DUPLICATE":2}}',
            b'{"version":1,"x":1e999}',
            b'{"version":1,"x":NaN}',
            b'{"version":1,"x":"unterminated}',
        ):
            with self.subTest(body=body):
                self.paseo.write_bytes(body)
                with self.assertRaises(ClaudeConnectionError) as error:
                    self.prepare()
                self.assertEqual(error.exception.code, "invalid_config")
                self.assertNotIn("PRIVATE_", str(error.exception))
                self.assertEqual(self.paseo.read_bytes(), body)
        self.save(self.paseo, {"version": 1, "agents": {"providers": {"claude-cindy": {"extends": "codex"}}}})
        self.assert_code("provider_conflict", self.prepare)
        self.save(self.paseo, {"version": 1, "agents": {"providers": {"claude-cindy": {"extends": "claude"}}}})
        self.assert_code("unknown_config_schema", self.prepare)
        self.save(self.t3, {"unrelated": "PRIVATE_UNRELATED"})
        self.assert_code("unknown_config_schema", self.prepare, "t3code")
        self.save(self.t3, {"providers": {}})
        self.assert_code("unknown_config_schema", self.prepare, "t3code")
        self.save(self.t3, {"providerInstances": {"claudeAgent": {"driver": "codex"}}})
        self.assert_code("provider_conflict", self.prepare, "t3code")

    def test_missing_relative_remote_and_wrong_config_paths_are_rejected(self) -> None:
        self.assert_code("config_unavailable", self.prepare)
        self.assert_code("wrong_config_path", self.prepare, "t3code", config_path=self.root / "settings.json")
        self.assert_code("invalid_path", self.prepare, config_path=Path("relative/config.json"))
        remote = r"\\example\share\config.json" if os.name == "nt" else "//example/share/config.json"
        self.assert_code("invalid_path", self.prepare, config_path=Path(remote))

    def test_refuses_reparse_and_hardlinked_config(self) -> None:
        self.save(self.paseo, {"version": 1})
        original = Path.lstat

        def redirected(path):
            if path == self.paseo.parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            if self.paseo.parent in path.parents:
                raise AssertionError("descended into reparse directory")
            return original(path)

        with patch.object(Path, "lstat", redirected):
            self.assert_code("config_unavailable", self.prepare)
        hardlink = self.root / "unrelated.json"
        try:
            os.link(self.paseo, hardlink)
        except OSError:
            self.skipTest("hardlinks unavailable in this test environment")
        self.assert_code("config_unsafe", self.prepare)



    def test_concurrent_plan_cannot_pass_the_same_config_write_guard(self) -> None:
        self.save(self.t3, {})
        second_home = self.root / "different native history"
        second_home.mkdir()
        first_plan = self.prepare("t3code")
        second_plan = self.prepare("t3code", claude_home=second_home)
        at_replace = Event()
        allow_replace = Event()
        responses = []
        errors = []
        replace = claude_connection._replace_config_file

        def pause_replace(temporary: Path, destination: Path) -> None:
            at_replace.set()
            if not allow_replace.wait(5):
                raise TimeoutError("Fixture did not finish second apply")
            replace(temporary, destination)

        def first():
            try:
                responses.append(self.apply(first_plan))
            except Exception as exc:
                errors.append(exc)

        with patch.object(claude_connection, "_replace_config_file", pause_replace):
            thread = Thread(target=first)
            thread.start()
            try:
                self.assertTrue(at_replace.wait(5))
                self.assert_code("config_locked", self.apply, second_plan)
            finally:
                allow_replace.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([row["status"] for row in responses], ["applied"])
        self.assert_code("stale_plan", self.apply, second_plan)
        self.assertFalse((self.t3.parent / claude_connection._LOCK_NAME).exists())

    def test_unverified_lock_is_retained_and_never_removed_by_next_apply(self) -> None:
        self.save(self.paseo, {"version": 1})
        plan = self.prepare()
        lock = self.paseo.parent / claude_connection._LOCK_NAME
        original_delete = frozen_files._delete_frozen

        def fail_lock_delete(root, evidence, **kwargs):
            if evidence["path"] == claude_connection._LOCK_NAME:
                raise PermissionError("fixture lock cleanup failure")
            return original_delete(root, evidence, **kwargs)

        with patch.object(frozen_files, "_delete_frozen", fail_lock_delete):
            self.assert_code("config_lock_cleanup_unverified", self.apply, plan)
        self.assertTrue(lock.is_file())
        self.assertIn("claude-cindy", json.loads(self.paseo.read_bytes())["agents"]["providers"])
        fresh = self.prepare()
        self.assert_code("config_locked", self.apply, fresh)
        self.assertTrue(lock.is_file())

    @unittest.skipUnless(os.name == "nt", "NTFS ACL fixture")
    def test_windows_preserves_restricted_dacl_in_staging_and_final_file(self) -> None:
        self.save(self.paseo, {"version": 1, "unrelated": "PRIVATE_FIXTURE"})
        try:
            identity = subprocess.check_output(["whoami"], text=True).strip()
            applied_acl = subprocess.run(
                ["icacls", str(self.paseo), "/inheritance:r", "/grant:r", identity + ":(F)"],
                capture_output=True, check=False,
            )
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("Windows ACL tools unavailable")
        if applied_acl.returncode != 0:
            self.skipTest("Could not create a restricted fixture DACL")

        def descriptor(path: Path) -> str:
            # icacls prints the filename before the first ACE. Normalize that
            # prefix so this checks the permissions of both fixture files.
            value = subprocess.check_output(["icacls", str(path)], text=True, errors="replace")
            return value.replace(str(path), "<fixture>", 1)
        before = descriptor(self.paseo)
        protect = claude_connection._prepare_config_file
        observations = []

        def observe_protect(temporary, destination):
            self.assertEqual(temporary.stat().st_size, 0)
            protect(temporary, destination)
            observations.append(descriptor(temporary))

        with patch.object(claude_connection, "_prepare_config_file", observe_protect):
            self.assertEqual(self.apply(self.prepare())["status"], "applied")
        self.assertEqual(observations, [before])
        self.assertEqual(descriptor(self.paseo), before)

if __name__ == "__main__":
    unittest.main()
