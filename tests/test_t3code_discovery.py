from contextlib import closing
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager.cli import main
from local_agent_manager.t3code_bound_adapter import T3CodeBoundAdapter
from local_agent_manager.t3code_discovery import BindingDiscoveryError, discover_bindings
from tests.orca_native_support import create_native_schema, add_native_record
from tests.t3code_support import create_store, add_thread, V2

THREAD = "11111111-1111-4111-8111-111111111111"
NATIVE = "33333333-3333-4333-8333-333333333333"


class T3DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "t3"
        self.home = self.base / "native"
        self.home.mkdir()
        create_native_schema(self.home)
        add_native_record(self.home, NATIVE)
        with closing(create_store(self.root)) as db:
            add_thread(db, THREAD, native=NATIVE)
        (self.root / "userdata/environment-id").write_text("env-fixture")
        self.install = self.base / "install"
        self.install.mkdir()
        self.binary = self.install / "T3 Code (Nightly).exe"
        self.binary.write_bytes(b"fixture, never executed")
        self.settings = {"providerInstances": {"codex": {"driver": "codex", "config": {
            "homePath": str(self.home), "binaryPath": str(self.binary)}, "environment": [
            {"name": "TEST_CREDENTIAL", "value": "PRIVATE_SECRET", "sensitive": True}]}}}
        self.write_settings()
        for item in (patch("local_agent_manager.t3code_server_delete._bundled_install", return_value=self.install),
                     patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "T3CODE_HOME": str(self.root)})):
            item.start()
            self.addCleanup(item.stop)

    def write_settings(self):
        (self.root / "userdata/settings.json").write_text(json.dumps(self.settings), encoding="utf-8")

    def test_configured_custom_instance_native_ids_and_desktop_are_discovered(self):
        desktop = self.base / "appdata/t3code-v2"
        desktop.mkdir(parents=True)
        self.settings["providerInstances"]["custom-codex"] = self.settings["providerInstances"].pop("codex")
        self.write_settings()
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            for table in (V2 + "threads", V2 + "provider_threads"):
                db.execute(f'UPDATE {table} SET provider_instance_id=? WHERE thread_id=?', ("custom-codex", THREAD))
            db.execute(f'UPDATE {V2}threads SET default_provider=? WHERE thread_id=?', ("custom-codex", THREAD))
            db.commit()
        result = discover_bindings(self.root)
        self.assertEqual(result["native_stores"], [{"thread_id": THREAD, "engine": "codex",
            "root": str(self.home), "codex_binary": str(self.binary)}])
        self.assertEqual(result["desktop_profiles"][0]["root"], str(desktop))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        self.assertFalse(T3CodeBoundAdapter(result).snapshot_references().errors)

    def test_prepare_cli_can_save_binding_without_mutating_records(self):
        out, err = StringIO(), StringIO()
        destination = self.base / "bindings.json"
        before = (self.home / "state_5.sqlite").read_bytes()
        code = main(["environment", "t3-cleanup", "prepare", "--out", str(destination), "--json"],
                    stdout=out, stderr=err)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["status"], "ready")
        self.assertEqual(json.loads(destination.read_text())["native_stores"][0]["thread_id"], THREAD)
        self.assertEqual(before, (self.home / "state_5.sqlite").read_bytes())
        self.assertNotIn("PRIVATE_SECRET", out.getvalue())

    def test_unknown_native_home_is_a_blocker_instead_of_assuming_default(self):
        other = self.base / "wrong-home"
        other.mkdir()
        create_native_schema(other)
        self.settings["providerInstances"]["codex"]["config"]["homePath"] = str(other)
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings(self.root)
        self.assertEqual(raised.exception.blockers, [{"blocker_code": "native_ids_not_in_configured_home", "thread_id": THREAD}])

    def test_unreadable_database_keeps_prepare_output_machine_readable(self):
        (self.root / "userdata/statev2.sqlite").write_bytes(b"invalid database")
        output, error = StringIO(), StringIO()
        code = main(["environment", "t3-cleanup", "prepare", "--json"], stdout=output, stderr=error)
        self.assertNotEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "blocked")
        self.assertNotIn("PRIVATE_SECRET", output.getvalue())

    def test_environment_root_without_home_path_is_checked_by_native_id(self):
        instance = self.settings["providerInstances"]["codex"]
        del instance["config"]["homePath"]
        instance["environment"].append({"name": "CODEX_HOME", "value": str(self.home)})
        self.write_settings()
        self.assertEqual(discover_bindings()["native_stores"][0]["root"], str(self.home))
        instance["environment"].append({"name": "CODEX_HOME", "value": str(self.home)})
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError):
            discover_bindings()

    def test_auto_delete_planning_uses_discovered_bound_adapter(self):
        from local_agent_manager.adapter_factory import create_default_adapters
        from local_agent_manager.cli import build_parser
        args = build_parser().parse_args(["delete", "plan", "--client", "t3code", "--record-id", THREAD])
        adapters = create_default_adapters(args)
        self.assertTrue(any(isinstance(adapter, T3CodeBoundAdapter) for adapter in adapters))

    def test_t3_guard_on_another_clients_operation_remains_read_only(self):
        from local_agent_manager.adapter_factory import create_default_adapters
        from local_agent_manager.cli import build_parser
        args = build_parser().parse_args(["delete", "plan", "--client", "native", "--record-id", NATIVE,
                                          "--t3code-root", str(self.root)])
        with patch("local_agent_manager.t3code_discovery.discover_bindings", side_effect=AssertionError("guard must stay read-only")):
            adapters = create_default_adapters(args)
        self.assertFalse(any(isinstance(adapter, T3CodeBoundAdapter) for adapter in adapters))

    @unittest.skipUnless(os.name == "nt", "Full T3 writer qualification uses the Windows lifecycle")
    def test_normal_plan_entry_discovers_native_descendants_without_binding_file(self):
        child = "44444444-4444-4444-8444-444444444444"
        add_native_record(self.home, child, parent=NATIVE)
        (self.root / "userdata/server-owner.sqlite").touch()
        output, error = StringIO(), StringIO()
        code = main(["delete", "plan", "--client", "t3code", "--record-id", THREAD,
                     "--out", str(self.base / "auto.plan.json"), "--json"], stdout=output, stderr=error)
        plan = json.loads(output.getvalue())
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertIn(child, json.dumps(plan))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(plan))

    def test_standalone_antigravity_is_reported_as_unsupported_native_cleanup(self):
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            add_thread(db, "antigravity-thread", driver="antigravity", native="antigravity-native")
        self.settings["providerInstances"]["antigravity"] = {"driver": "antigravity", "config": {
            "binaryPath": str(self.base / "agy.exe")}}
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings()
        self.assertEqual(raised.exception.blockers, [{"blocker_code": "antigravity_cli_acp_cleanup_unsupported",
                                                      "thread_id": "antigravity-thread"}])

    def test_pi_environment_session_root_uses_exact_path_and_header_identity(self):
        agent_dir = self.base / "pi"
        agent_dir.mkdir()
        root = self.base / "custom-sessions"
        root.mkdir()
        path = root / "session.jsonl"
        path.write_text(json.dumps({"type": "session", "version": 3, "id": NATIVE,
            "timestamp": "2026-11-10T00:00:00Z", "cwd": str(self.base)}) + "\n")
        self.settings["providerInstances"]["pi"] = {"driver": "pi", "config": {}, "environment": {
            "PI_CODING_AGENT_DIR": str(agent_dir), "PI_CODING_AGENT_SESSION_DIR": str(root)}}
        self.write_settings()
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            add_thread(db, "pi-thread", driver="pi", native=str(path))
        manifest = discover_bindings(self.root)
        refs = T3CodeBoundAdapter(manifest).snapshot_references().references
        ref = next(ref for ref in refs if ref.frontend_id == "pi-thread")
        self.assertEqual(ref.native_id, NATIVE)
        self.assertEqual(ref.opaque_native_locator, str(path))
        self.assertEqual(ref.native_record.store.canonical_path, str(root))
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id='pi-thread'",
                (json.dumps({"nativeThreadRef": {"driver": "pi", "nativeId": None, "strength": "none"}}),))
            db.commit()
        empty_refs = T3CodeBoundAdapter(discover_bindings(self.root)).snapshot_references().references
        self.assertFalse(any(ref.frontend_id == "pi-thread" for ref in empty_refs))
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id='pi-thread'",
                (json.dumps({"nativeThreadRef": {"driver": "pi", "nativeId": str(path), "strength": "strong"}}),))
            db.commit()
        self.settings["providerInstances"]["pi"]["environment"]["PI_CODING_AGENT_SESSION_DIR"] = str(agent_dir)
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError):
            discover_bindings(self.root)


if __name__ == "__main__":
    unittest.main()
