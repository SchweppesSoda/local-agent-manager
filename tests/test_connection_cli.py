from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor.cli import main


class ConnectionCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="connect-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.binary = self.root / "claude.exe"
        self.binary.write_bytes(b"synthetic binary; never execute")
        self.home = self.root / "claude-home"
        self.home.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"version": 1, "secret": "DO-NOT-REPORT"}), encoding="utf-8")

    def run_cli(self, *arguments):
        output, errors = StringIO(), StringIO()
        with patch("local_agent_record_janitor.cli.create_default_adapters") as adapters:
            code = main(["environment", *arguments, "--json"], stdout=output, stderr=errors)
            adapters.assert_not_called()
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("DO-NOT-REPORT", output.getvalue())
        return code, json.loads(output.getvalue())

    def prepare(self, out):
        return self.run_cli("claude-connect", "prepare", "--target", "paseo",
            "--config", str(self.config), "--claude-binary", str(self.binary),
            "--claude-home", str(self.home), "--out", str(out))

    def test_config_prepare_apply_and_no_mutation_without_closed_assertion(self):
        out = self.root / "plan.json"
        before = self.config.read_bytes()
        code, plan = self.prepare(out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_bytes()), plan)
        self.assertEqual(self.config.read_bytes(), before)
        code, blocked = self.run_cli("claude-connect", "apply", "--plan", str(out),
                                      "--approve", plan["plan_sha256"])
        self.assertEqual((code, blocked["code"]), (1, "clients_not_closed"))
        self.assertEqual(self.config.read_bytes(), before)
        code, applied = self.run_cli("claude-connect", "apply", "--plan", str(out),
            "--approve", plan["plan_sha256"], "--clients-closed")
        self.assertEqual((code, applied["status"]), (0, "applied"))
        current = json.loads(self.config.read_bytes())
        self.assertEqual(current["secret"], "DO-NOT-REPORT")
        self.assertEqual(current["agents"]["providers"]["claude-cindy"]["command"], [str(self.binary)])

    def test_plan_output_never_overwrites_and_invalid_input_is_redacted(self):
        out = self.root / "plan.json"
        out.write_text("DO-NOT-REPORT", encoding="utf-8")
        code, report = self.prepare(out)
        self.assertEqual((code, report["status"]), (1, "blocked"))
        self.assertEqual(out.read_text(encoding="utf-8"), "DO-NOT-REPORT")
        out.write_text('{"invalid": "DO-NOT-REPORT"', encoding="utf-8")
        code, report = self.run_cli("claude-connect", "apply", "--plan", str(out),
            "--approve", "0" * 64, "--clients-closed")
        self.assertEqual(code, 1)

    def test_memory_inspect_and_selected_export_do_not_activate_loader(self):
        project = self.root / "项目 space"
        project.mkdir()
        maker = self.root / "maker-memory"
        store = maker / re.sub(r"[/\\:]", "-", str(project)).lstrip("-")
        store.mkdir(parents=True)
        (store / "meta.json").write_text(json.dumps({"absPath": str(project)}), encoding="utf-8")
        (store / "project_a.md").write_text("DO-NOT-REPORT\n@private-other.md", encoding="utf-8")
        (store / "feedback_b.md").write_text("UNSELECTED", encoding="utf-8")
        options = ["--maker-memory-root", str(maker), "--project", str(project)]
        code, report = self.run_cli("cindy-memory", "inspect", *options)
        self.assertEqual(code, 0)
        self.assertEqual(len(report["shards"]), 2)
        snapshot = self.root / "selected.md"
        code, report = self.run_cli("cindy-memory", "export", *options,
            "--shard", "project_a.md", "--out", str(snapshot))
        self.assertEqual((code, report["status"]), (0, "exported"))
        self.assertIn("DO-NOT-REPORT", snapshot.read_text(encoding="utf-8"))
        self.assertNotIn("UNSELECTED", snapshot.read_text(encoding="utf-8"))
        self.assertFalse((project / "CLAUDE.local.md").exists())

    def test_t3_exact_session_request_does_not_broaden_to_project_import(self):
        with patch("local_agent_record_janitor.claude_session_import.prepare_t3code_import") as prepare:
            code, report = self.run_cli("claude-import", "prepare", "--target", "t3code",
                "--executable", str(self.binary), "--profile", str(self.home),
                "--project", str(self.root), "--project-id", "project", "--native-id", "one-session",
                "--out", str(self.root / "import.json"))
            self.assertEqual(code, 1)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["code"], "t3code_import_exact_session_unsupported")
            prepare.assert_not_called()

    def test_import_apply_forwards_explicit_project_scope_and_hash(self):
        out = self.root / "import.json"
        plan = {"status": "ready", "plan_sha256": "1" * 64}
        out.write_text(json.dumps(plan), encoding="utf-8")
        with patch("local_agent_record_janitor.claude_session_import.run_session_import",
                   return_value={"status": "imported", "target": "t3code"}) as apply:
            code, report = self.run_cli("claude-import", "apply", "--plan", str(out),
                "--approve", "1" * 64, "--accept-project-history")
            self.assertEqual(code, 0)
            apply.assert_called_once_with(plan, approved_sha256="1" * 64, accept_project_history=True)

    def test_post_replace_verification_failure_is_unknown_not_blocked(self):
        out = self.root / "plan.json"
        _, plan = self.prepare(out)
        with patch("local_agent_record_janitor.frozen_files.satisfied", return_value=False):
            code, result = self.run_cli("claude-connect", "apply", "--plan", str(out),
                "--approve", plan["plan_sha256"], "--clients-closed")
        self.assertEqual((code, result["status"]), (1, "unknown"))
        self.assertEqual(result["code"], "config_write_unverified")
        current = json.loads(self.config.read_bytes())
        self.assertEqual(current["agents"]["providers"]["claude-cindy"]["command"], [str(self.binary)])

    def test_paseo_prepare_forwards_explicit_transcript_without_inventory(self):
        transcript = self.root / "chosen.jsonl"
        plan = {"status": "ready", "plan_sha256": "2" * 64}
        with patch("local_agent_record_janitor.claude_session_import.prepare_paseo_import",
                   return_value=plan) as prepare:
            code, result = self.run_cli("claude-import", "prepare", "--target", "paseo",
                "--executable", str(self.binary), "--profile", str(self.home),
                "--project", str(self.root), "--native-id", "selected-id",
                "--native-transcript", str(transcript), "--out", str(self.root / "import.json"))
            self.assertEqual((code, result), (0, plan))
            prepare.assert_called_once_with(executable=self.binary, profile=self.home, cwd=self.root,
                native_id="selected-id", native_transcript=transcript, provider="claude-cindy")

    def test_failed_session_revocation_reports_unknown_side_effect(self):
        from local_agent_record_janitor.claude_session_import import SessionImportError
        out = self.root / "import.json"
        out.write_text('{"status":"ready"}', encoding="utf-8")
        with patch("local_agent_record_janitor.claude_session_import.run_session_import",
                   side_effect=SessionImportError("t3code_import_preflight_and_session_revoke_unverified")):
            code, report = self.run_cli("claude-import", "apply", "--plan", str(out),
                "--approve", "0" * 64, "--accept-project-history")
            self.assertEqual((code, report["status"]), (1, "unknown"))


if __name__ == "__main__":
    unittest.main()
