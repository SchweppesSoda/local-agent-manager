from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager.cli import main
from local_agent_manager.paseo_discovery import BindingDiscoveryError, discover_bindings
from tests.orca_native_support import add_native_record, create_native_schema
from tests.test_paseo_fullflow import AGENT, KEEP, NATIVE, OTHER, agent, write


class PaseoDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "paseo"
        self.home = self.base / "codex"
        self.home.mkdir()
        create_native_schema(self.home)
        add_native_record(self.home, NATIVE)
        value = agent(AGENT, NATIVE, self.base)
        value["launchEnv"] = {"CODEX_HOME": str(self.home), "PRIVATE_KEY": "PRIVATE_SECRET"}
        write(self.root / "agents" / (AGENT + ".json"), value)
        (self.root / "server-id").write_text("srv_fixture")
        self.install = self.base / "install"
        self.install.mkdir()
        (self.install / "Paseo.exe").write_bytes(b"fixture, never executed")
        for item in (patch("local_agent_manager.paseo_daemon_delete._bundled_install", return_value=self.install),
                     patch("pathlib.Path.home", return_value=self.base),
                     patch("local_agent_manager.office_runtime.processes", return_value=[]),
                     patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"),
                         "LOCALAPPDATA": str(self.base / "localappdata"), "PASEO_HOME": str(self.root),
                         "T3CODE_HOME": str(self.base / "absent-t3")})):
            item.start()
            self.addCleanup(item.stop)

    def invoke(self, *arguments):
        output, error = StringIO(), StringIO()
        code = main([*arguments, "--json"], stdout=output, stderr=error)
        return code, json.loads(output.getvalue())

    def test_prepare_discovers_launch_overlay_and_does_not_expose_secrets(self):
        wrong = self.base / "wrong-home"
        write(self.root / "config.json", {"version": 1, "agents": {"providers": {
            "codex": {"env": {"CODEX_HOME": str(wrong), "PRIVATE_KEY": "PRIVATE_SECRET"}}}}})
        output = self.base / "bindings.json"
        before = (self.home / "state_5.sqlite").read_bytes()
        code, result = self.invoke("environment", "paseo-cleanup", "prepare", "--out", str(output))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["binding_manifest"]["native_stores"], [
            {"agent_id": AGENT, "engine": "codex", "root": str(self.home)}])
        self.assertEqual(json.loads(output.read_text()), result["binding_manifest"])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        self.assertEqual(before, (self.home / "state_5.sqlite").read_bytes())

    def test_wrong_home_and_unqualified_provider_block_without_leaking_config(self):
        value = agent(AGENT, NATIVE, self.base)
        value["launchEnv"] = {"CODEX_HOME": str(self.home)}
        value["persistence"]["sessionId"] = OTHER
        write(self.root / "agents" / (AGENT + ".json"), value)
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings(self.root)
        self.assertEqual(raised.exception.blockers[0]["blocker_code"], "native_handle_not_in_configured_home")
        value.update(provider="antigravity", persistence={"provider": "antigravity", "sessionId": NATIVE})
        write(self.root / "agents" / (AGENT + ".json"), value)
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings(self.root)
        self.assertEqual(raised.exception.blockers[0]["blocker_code"], "antigravity_handle_unqualified")

    def test_other_clients_keep_paseo_as_readonly_guard(self):
        from local_agent_manager.adapter_factory import create_default_adapters
        from local_agent_manager.cli import build_parser
        args = build_parser().parse_args(["delete", "plan", "--client", "native", "--record-id", NATIVE,
                                           "--paseo-root", str(self.root)])
        with patch("local_agent_manager.paseo_discovery.discover_bindings", side_effect=AssertionError("readonly guard")):
            create_default_adapters(args)

    @unittest.skipUnless(os.name == "nt", "Paseo full cleanup uses the Windows lifecycle")
    def test_automatic_claude_and_pi_full_cleanup_through_cli(self):
        for backend in ("claude", "pi"):
            with self.subTest(backend=backend):
                home = self.base / backend
                if backend == "claude":
                    transcript = home / "projects" / "fixture" / (NATIVE + ".jsonl")
                    keeper = transcript.with_name(OTHER + ".jsonl")
                    write(transcript, {"sessionId": NATIVE, "message": "fixture"})
                    write(keeper, {"sessionId": OTHER, "message": "keep"})
                    environment = {"CLAUDE_CONFIG_DIR": str(home)}
                else:
                    transcript = home / "sessions" / "fixture" / "session.jsonl"
                    keeper = transcript.with_name("same-id-copy.jsonl")
                    write(transcript, {"type": "session", "version": 3, "id": NATIVE,
                        "timestamp": "2026-11-10T00:00:00Z", "cwd": str(self.base)})
                    keeper.write_bytes(transcript.read_bytes())
                    environment = {"PI_CODING_AGENT_DIR": str(home)}
                value = agent(AGENT, NATIVE, self.base)
                value.update(provider=backend, launchEnv=environment,
                    persistence={"provider": backend, "sessionId": NATIVE},
                    runtimeInfo={"provider": backend, "sessionId": None})
                if backend == "pi":
                    value["persistence"]["nativeHandle"] = str(transcript)
                write(self.root / "agents" / (AGENT + ".json"), value)
                code, plan = self.invoke("delete", "plan", "--client", "paseo", "--record-id", AGENT,
                    "--out", str(self.base / (backend + ".plan.json")))
                self.assertEqual(code, 0, plan)
                code, result = self.invoke("delete", "apply", "--operation-id", plan["operation_id"],
                    "--plan", plan["plan_path"], "--plan-sha256", plan["plan_sha256"], "--clients-closed")
                self.assertEqual(code, 0, result)
                self.assertTrue(result["goal_satisfied"])
                self.assertFalse(transcript.exists())
                self.assertTrue(keeper.exists())
                self.assertFalse((self.root / "agents" / (AGENT + ".json")).exists())
                code, result = self.invoke("operation", "verify", "--operation-id", plan["operation_id"],
                    "--plan", plan["plan_path"])
                self.assertEqual(code, 0, result)
                self.assertTrue(result["goal_satisfied"])


if __name__ == "__main__":
    unittest.main()
