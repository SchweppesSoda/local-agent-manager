import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from local_agent_record_janitor import claude_session_import

from local_agent_record_janitor.claude_session_import import (
    PASEO_VERSION, T3_VERSION, SessionImportError,
    prepare_paseo_import, prepare_t3code_import, run_session_import, verify_session_import,
)
from tests.t3code_support import add_thread, create_store, insert


NATIVE = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
PRIVATE = "CLAUDE_IMPORT_PRIVATE_FIXTURE_SENTINEL"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def reply(stdout, value):
    stdout.write(json.dumps(value).encode("utf-8"))
    return SimpleNamespace(returncode=0)


class ClaudeSessionImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="i")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.cwd = self.root / "p q"
        self.cwd.mkdir()
        self.claude_home = self.root / "c"
        self.claude_home.mkdir()
        self.claude = self.root / "Cindy Claude.exe"
        self.claude.write_bytes(b"fixture, never executed")

    def paseo_fixture(self):
        self.profile = self.root / "paseo"
        (self.profile / "agents").mkdir(parents=True)
        self.executable = self.root / "paseo-cli.exe"
        self.executable.touch()
        (self.profile / "server-id").write_text("srv_fixture", encoding="utf-8")
        write_json(self.profile / "paseo.pid", {"serverId": "srv_fixture", "pid": 9,
                                              "startedAt": "fixture", "listen": "127.0.0.1:6767"})
        self.paseo_config = {
            "version": 1,
            "agents": {"providers": {"claude-cindy": {
                "extends": "claude", "command": [str(self.claude)],
                "env": {"CLAUDE_CONFIG_DIR": str(self.claude_home), "PRIVATE": PRIVATE},
            }}},
        }
        write_json(self.profile / "config.json", self.paseo_config)
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(self.cwd.resolve(strict=True)))
        self.transcript = self.claude_home / "projects" / encoded / f"{NATIVE}.jsonl"
        write_json(self.transcript, {"type": "user", "sessionId": NATIVE, "cwd": str(self.cwd),
                                     "message": {"role": "user", "content": PRIVATE}})
        return self.paseo_plan()

    def paseo_plan(self):
        return prepare_paseo_import(executable=self.executable, profile=self.profile,
                                    cwd=self.cwd, native_id=NATIVE, native_transcript=self.transcript)

    def paseo_record(self, *, archived=False):
        write_json(self.profile / "agents" / (AGENT + ".json"), {
            "id": AGENT, "provider": "claude-cindy", "cwd": str(self.cwd),
            "createdAt": "2026-11-07T00:00:00Z", "updatedAt": "2026-11-07T00:00:00Z",
            "archivedAt": "2026-11-08T00:00:00Z" if archived else None,
            "persistence": {"provider": "claude-cindy", "sessionId": NATIVE, "nativeHandle": NATIVE},
            "runtimeInfo": {"provider": "claude-cindy", "sessionId": NATIVE},
            "title": PRIVATE,
        })

    def paseo_runner(self, *, timeout=False):
        def run(argv, *, stdout, **kwargs):
            if argv[1:] == ["--version"]:
                stdout.write(PASEO_VERSION.encode())
                return SimpleNamespace(returncode=0)
            self.assertEqual(argv[1:], ["import", NATIVE, "--provider", "claude-cindy", "--cwd",
                                       str(self.cwd), "--home", str(self.profile), "--json"])
            self.paseo_record()
            if timeout:
                raise subprocess.TimeoutExpired(argv, 1)
            return reply(stdout, {"agentId": AGENT, "provider": "claude-cindy",
                                  "cwd": str(self.cwd), "title": PRIVATE})
        return Mock(side_effect=run)

    def test_paseo_exact_import_checks_registry_and_redacts_product_reply(self):
        plan = self.paseo_fixture()
        self.assertEqual(verify_session_import(plan)["status"], "not_observed")
        runner = self.paseo_runner()
        result = run_session_import(plan, approved_sha256=plan["plan_sha256"], runner=runner)
        self.assertEqual(result["status"], "imported", result)
        self.assertEqual(result["mappings"][0]["native_id"], NATIVE)
        self.assertEqual(result["mappings"][0]["agent_id"], AGENT)
        self.assertNotIn(PRIVATE, json.dumps([plan, result]))
        observed = verify_session_import(plan)
        self.assertEqual(observed["status"], "observed", observed)
        self.assertEqual(observed["mappings"], result["mappings"])
        self.assertEqual(runner.call_count, 2)
        repeated = run_session_import(plan, approved_sha256=plan["plan_sha256"], runner=runner)
        self.assertEqual(repeated["status"], "already_imported", repeated)
        self.assertEqual(runner.call_count, 3)  # Only the version check repeats.

    def test_paseo_timeout_does_not_retry_a_possible_import(self):
        plan = self.paseo_fixture()
        runner = self.paseo_runner(timeout=True)
        result = run_session_import(plan, approved_sha256=plan["plan_sha256"], runner=runner)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["mappings"][0]["native_id"], NATIVE)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(verify_session_import(plan)["status"], "observed")
        self.assertEqual(runner.call_count, 2)

    def test_paseo_transcript_must_match_source_identity_and_frozen_bytes(self):
        plan = self.paseo_fixture()
        runner = self.paseo_runner()
        value = {"type": "user", "sessionId": NATIVE, "cwd": str(self.cwd),
                 "message": {"role": "user", "content": "changed"}}
        write_json(self.transcript, value)
        with self.assertRaisesRegex(SessionImportError, "import_preflight_changed"):
            run_session_import(plan, approved_sha256=plan["plan_sha256"], runner=runner)
        runner.assert_not_called()
        for change in ({"sessionId": OTHER}, {"cwd": str(self.root)}):
            with self.subTest(change=tuple(change)):
                write_json(self.transcript, {**value, **change})
                with self.assertRaisesRegex(SessionImportError, "native_transcript_identity_conflict"):
                    self.paseo_plan()
        self.transcript = self.claude_home / "projects" / "other-encoded-cwd" / f"{NATIVE}.jsonl"
        write_json(self.transcript, value)
        with self.assertRaisesRegex(SessionImportError, "native_transcript_path_unverified"):
            self.paseo_plan()

    def test_paseo_approval_archive_and_home_override_fail_closed(self):
        plan = self.paseo_fixture()
        runner = self.paseo_runner()
        changed = {**plan, "native_id": OTHER}
        with self.assertRaisesRegex(SessionImportError, "import_plan_digest_mismatch"):
            run_session_import(changed, approved_sha256=plan["plan_sha256"], runner=runner)
        runner.assert_not_called()
        self.paseo_record(archived=True)
        with self.assertRaisesRegex(SessionImportError, "archived_restore_requires_separate_scope"):
            run_session_import(plan, approved_sha256=plan["plan_sha256"], runner=runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(verify_session_import(plan)["status"], "archived")

        provider = self.paseo_config["agents"]["providers"]["claude-cindy"]
        original = copy.deepcopy(provider)
        for override in ({"env": {**provider["env"], "claude_config_dir": str(self.root)}},
                         {"command": [str(self.claude), "--config-dir", str(self.root)]}):
            with self.subTest(override=tuple(override)):
                self.paseo_config["agents"]["providers"]["claude-cindy"] = {**original, **override}
                write_json(self.profile / "config.json", self.paseo_config)
                with self.assertRaisesRegex(SessionImportError, "claude_provider_unverified"):
                    self.paseo_plan()

    def test_mapped_or_unverified_drive_is_rejected_before_file_reads(self):
        self.paseo_fixture()
        with (
            patch("local_agent_record_janitor.environment_paths._is_local_drive", return_value=False),
            patch.object(claude_session_import.frozen_files, "read_file") as read_file,
        ):
            with self.assertRaisesRegex(SessionImportError, "import_local_path_unverified"):
                self.paseo_plan()
        read_file.assert_not_called()

    def t3_fixture(self):
        self.profile = self.root / "t3"
        self.executable = self.root / "T3 Code (Nightly).exe"
        self.executable.touch()
        archive = self.root / "resources" / "server.asar"
        archive.parent.mkdir()
        archive.touch()
        self.db = create_store(self.profile)
        self.addCleanup(self.db.close)
        insert(self.db, "projection_projects", project_id="project-1", title=PRIVATE,
               workspace_root=str(self.cwd), scripts_json="[]")
        self.db.commit()
        (self.profile / "userdata/environment-id").write_text("env_fixture", encoding="utf-8")
        write_json(self.profile / "userdata/server-runtime.json", {
            "version": 1, "pid": 9, "origin": "http://127.0.0.1:7733", "port": 7733,
            "serverVersion": T3_VERSION, "startedAt": "fixture", "ownerId": "fixture",
        })
        self.t3_settings = {"providerInstances": {"claudeAgent": {
            "driver": "claudeAgent", "enabled": True, "config": {
                "binaryPath": str(self.claude), "homePath": str(self.claude_home), "private": PRIVATE,
            },
        }}}
        write_json(self.profile / "userdata/settings.json", self.t3_settings)
        return prepare_t3code_import(executable=self.executable, profile=self.profile,
                                     cwd=self.cwd, project_id="project-1")

    def t3_runner(self, *, truncated=False, revoke_fail=False, timeout=False):
        tags = []

        def run(argv, *, stdout, input, **kwargs):
            if "issue" in argv:
                tags.append("issue")
                self.assertIn("orchestration:read", argv)
                self.assertIn("orchestration:operate", argv)
                self.assertNotIn(PRIVATE, " ".join(argv))
                return reply(stdout, {"sessionId": "fixture-session", "token": PRIVATE})
            if "revoke" in argv:
                tags.append("revoke")
                self.assertIn("fixture-session", argv)
                return SimpleNamespace(returncode=1 if revoke_fail else 0)
            self.assertEqual(Path(argv[1]).name, "t3code_session_import_rpc.cjs")
            req = json.loads(input)
            self.assertEqual(req["token"], PRIVATE)
            tags.append(req["tag"])
            if req["tag"] == "agentSessions.scan":
                self.assertEqual(req["payload"], {})
                return reply(stdout, {"truncated": truncated, "candidates": [{
                    "path": str(self.cwd), "projectId": "project-1", "threadCount": 3,
                    "sources": ["claudeAgent", "codex", "pi"], "title": PRIVATE,
                }]})
            self.assertEqual(req["tag"], "agentSessions.import")
            self.assertEqual(req["payload"], {"projectId": "project-1", "expectedWorkspaceRoot": str(self.cwd)})
            if tags.count("agentSessions.import") == 1:
                add_thread(self.db, f"import:claudeAgent:{NATIVE}", native=NATIVE, driver="claudeAgent")
                add_thread(self.db, f"import:codex:{OTHER}", native=OTHER)
                add_thread(self.db, "import:pi:provider-session-id",
                           native=str(self.root / "pi" / "synthetic.jsonl"), driver="pi")
            if timeout:
                raise subprocess.TimeoutExpired(argv, 1)
            return reply(stdout, {"importedCount": 3, "skippedCount": 0})

        return Mock(side_effect=run), tags

    def test_t3_scope_projection_and_reimport_count(self):
        plan = self.t3_fixture()
        self.assertTrue(plan["may_include_other_engines"])
        runner, tags = self.t3_runner()
        with self.assertRaisesRegex(SessionImportError, "project_history_approval_required"):
            run_session_import(plan, approved_sha256=plan["plan_sha256"], runner=runner)
        runner.assert_not_called()
        for status, new_count in (("imported", 3), ("observed", 0)):
            result = run_session_import(plan, approved_sha256=plan["plan_sha256"],
                                        accept_project_history=True, runner=runner)
            self.assertEqual(result["status"], status, result)
            self.assertEqual(result["verification_scope"], "project_projection")
            self.assertEqual(result["imported_count"], 3)
            self.assertEqual(len(result["new_mappings"]), new_count)
            self.assertEqual({item["native_id"] for item in result["mappings"]},
                             {NATIVE, OTHER, str(self.root / "pi" / "synthetic.jsonl")})
            self.assertNotIn(PRIVATE, json.dumps([plan, result]))
        self.assertEqual(tags, ["issue", "agentSessions.scan", "agentSessions.import", "revoke"] * 2)
        self.assertEqual(verify_session_import(plan)["status"], "observed")
        self.t3_settings["unrelated"] = True
        write_json(self.profile / "userdata/settings.json", self.t3_settings)
        self.assertEqual(verify_session_import(plan)["status"], "unknown")

    def test_t3_truncated_scan_and_revoke_failure_are_reported(self):
        plan = self.t3_fixture()
        runner, tags = self.t3_runner(truncated=True, revoke_fail=True)
        with self.assertRaisesRegex(SessionImportError, "preflight_and_session_revoke_unverified"):
            run_session_import(plan, approved_sha256=plan["plan_sha256"],
                               accept_project_history=True, runner=runner)
        self.assertEqual(tags, ["issue", "agentSessions.scan", "revoke"])
        self.assertEqual(verify_session_import(plan)["status"], "not_observed")

    def test_t3_timeout_reads_projection_without_resending(self):
        plan = self.t3_fixture()
        runner, tags = self.t3_runner(timeout=True)
        result = run_session_import(plan, approved_sha256=plan["plan_sha256"],
                                    accept_project_history=True, runner=runner)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual({item["native_id"] for item in result["mappings"]},
                         {NATIVE, OTHER, str(self.root / "pi" / "synthetic.jsonl")})
        self.assertEqual(tags, ["issue", "agentSessions.scan", "agentSessions.import", "revoke"])

    def test_t3_temporary_session_issuance_timeout_is_unknown(self):
        plan = self.t3_fixture()
        runner = Mock(side_effect=subprocess.TimeoutExpired("fixture-issue", 1))
        result = run_session_import(plan, approved_sha256=plan["plan_sha256"],
                                    accept_project_history=True, runner=runner)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "t3code_import_session_issuance_unverified")
        self.assertEqual(result["requested_session_ttl_seconds"], 120)
        runner.assert_called_once()

    def test_t3_js_projects_wire_replies_and_rejects_other_tags(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node runtime is unavailable")
        helper = Path(claude_session_import.__file__).with_name("t3code_session_import_rpc.cjs")
        stub = self.root / "wire-stub.cjs"
        stub.write_text(r"""
const assert = require('node:assert/strict');
global.fetch = async (url, options) => {
  assert.equal(String(url), 'http://127.0.0.1:7733/api/auth/websocket-ticket');
  assert.equal(options.method, 'POST');
  assert.equal(options.headers.authorization, 'Bearer fixture-token');
  assert.equal(options.redirect, 'error');
  return new Response(JSON.stringify({ticket: 'fixture-ticket'}));
};
global.WebSocket = class extends EventTarget {
  constructor(url) {
    super();
    assert.equal(String(url), 'ws://127.0.0.1:7733/ws?orchestrationProtocol=2&wsTicket=fixture-ticket');
    queueMicrotask(() => this.dispatchEvent(new Event('open')));
  }
  send(raw) {
    const request = JSON.parse(raw);
    assert.equal(request._tag, 'Request');
    assert.deepEqual(request.headers, []);
    let value;
    if (request.tag === 'agentSessions.scan') {
      assert.deepEqual(request.payload, {});
      value = {candidates: [{path: '/synthetic', sources: ['claudeAgent'], threadCount: 1,
                            title: 'private title', secret: 'private raw field'}]};
    } else {
      assert.equal(request.tag, 'agentSessions.import');
      assert.deepEqual(request.payload, {projectId: 'fixture', expectedWorkspaceRoot: '/synthetic'});
      value = {importedCount: 1, skippedCount: 0, secret: 'private raw field'};
    }
    const data = JSON.stringify([{_tag: 'Exit', requestId: request.id, exit: {_tag: 'Success', value}}]);
    queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', {data})));
  }
  close() {}
};
""", encoding="utf-8")
        for tag, payload, expected in (
            ("agentSessions.scan", {}, {"candidates": [{"path": "/synthetic",
                "sources": ["claudeAgent"], "threadCount": 1}]}),
            ("agentSessions.import", {"projectId": "fixture", "expectedWorkspaceRoot": "/synthetic"},
             {"importedCount": 1, "skippedCount": 0}),
            ("orchestration.dispatchCommand", {}, None),
        ):
            with self.subTest(tag=tag):
                result = subprocess.run(
                    [node, "--require", str(stub), str(helper)], capture_output=True,
                    input=json.dumps({"origin": "http://127.0.0.1:7733", "token": "fixture-token",
                                      "tag": tag, "payload": payload}).encode(),
                    timeout=5, check=False,
                )
                if expected is None:
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, b"")
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), expected)
                self.assertNotIn(b"fixture-token", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
