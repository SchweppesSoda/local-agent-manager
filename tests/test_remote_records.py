"""Exercise both public command boundaries with isolated stores and no SSH server."""
from __future__ import annotations

from tests import bootstrap
from tests import test_paseo_daemon_delete as paseo_fixture
from tests import test_t3code_server_delete as t3_fixture
from tests import test_frontend_session_cleanup as cindy_fixture
from tests import test_paseo_fullflow, test_t3code_fullflow, orca_support
from tests.t3code_support import create_store, add_thread, insert, V2
from contextlib import closing, nullcontext
from contextvars import Context
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import remote_records as remote
from local_agent_manager.cli import main


class RemoteRecordsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / "owner"
        self.root.mkdir()
        isolated = self.base / "user"
        isolated.mkdir()
        env = {k: str(isolated / k.lower()) for k in (
            "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "CODEX_HOME", "PI_CODING_AGENT_DIR", "CLAUDE_CONFIG_DIR")}
        env.update(USERPROFILE=str(isolated), HOME=str(isolated), PASEO_HOME=str(isolated / "paseo"))
        self.enterContext(patch.dict(os.environ, env))
        Path(env["PI_CODING_AGENT_DIR"]).mkdir()
        self.client = "pi"
        self.host = "lab_owner"
        self.drop_after_dispatch = False
        self.transport = self.enterContext(patch.object(remote, "_ssh", side_effect=self.ssh))

    def ssh(self, host, payload):
        self.assertEqual(host, self.host)
        output = io.BytesIO()
        code = Context().run(remote.worker, io.BytesIO(payload), output)
        self.last_worker_reply = json.loads(output.getvalue())
        if self.drop_after_dispatch:
            self.drop_after_dispatch = False
            raise subprocess.TimeoutExpired("ssh", 1)
        return code, output.getvalue()

    def invoke(self, verb, *extra, ok=True):
        output = io.StringIO()
        code = main(["remote", verb, "--ssh-host", self.host, "--client", self.client,
                     "--root", str(self.root), *extra], stdout=output, stderr=io.StringIO())
        value = json.loads(output.getvalue())
        self.assertEqual(code == 0, ok, value)
        return value

    def plan(self, identifier, *extra):
        inventory = self.invoke("records")
        self.assertTrue(any(row["record_id"] == identifier for row in inventory["records"]))
        owner = inventory["owner"]["store_id"]
        plan = self.invoke("plan", "--owner", owner, "--record-id", identifier, *extra)
        return plan, ("--owner", owner, "--operation-id", plan["operation_id"], "--approval", plan["approval"])

    def pi_file(self, root, identifier):
        path = root / "project" / (identifier + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session", "id": identifier, "version": 3,
            "cwd": str(self.base), "timestamp": "2026-09-01T00:00:00Z"}) + "\n", encoding="utf-8")
        return path

    def test_pi_exact_store_approval_and_lost_apply_reply(self):
        first = self.pi_file(self.root, "pi-session")
        sibling = self.base / "other"
        second = self.pi_file(sibling, "pi-session")
        plan, args = self.plan("pi-session")
        self.assertEqual(Path(plan["native_locator"]), first)
        self.invoke("apply", *args[:-1], "a" * 64, "--clients-closed", ok=False)
        self.root = sibling
        self.invoke("apply", *args, "--clients-closed", ok=False)
        self.root = first.parents[1]
        self.assertTrue(first.exists())
        self.drop_after_dispatch = True
        lost = self.invoke("apply", *args, "--clients-closed", ok=False)
        self.assertEqual((lost["goal_status"], lost["operation_id"]), ("unknown", plan["operation_id"]))
        self.assertFalse(first.exists(), self.last_worker_reply)
        self.assertTrue(second.exists())
        self.assertEqual(self.invoke("status", *args)["goal_status"], "complete")
        self.assertEqual(self.invoke("verify", *args)["goal_status"], "complete")
        self.pi_file(self.root, "pi-session")
        self.assertEqual(self.invoke("verify", *args, ok=False)["goal_status"], "completed_with_residuals")

    def test_codex_uses_the_approved_executable_and_native_store(self):
        from tests.support import create_thread_index, write_rollout
        self.client = "native"
        tid = "33333333-3333-4333-8333-333333333333"
        rollout = write_rollout(self.root, tid, originator="codex")
        index = create_thread_index(self.root, [{"id": tid, "rollout_path": str(rollout)}])
        binary = self.base / "codex.exe"
        binary.write_bytes(b"test runtime")
        binary.chmod(0o700)
        def delete_thread(identifier):
            self.assertEqual(identifier, tid)
            with closing(sqlite3.connect(index)) as db:
                db.execute("DELETE FROM threads WHERE id=?", (identifier,))
                db.commit()
            rollout.unlink()
        from types import SimpleNamespace
        def server(*, codex_home, codex_binary, **_):
            self.assertEqual((Path(codex_home), Path(codex_binary)), (self.root, binary))
            return nullcontext(SimpleNamespace(delete_thread=delete_thread))
        self.enterContext(patch("local_agent_manager.cli.main",
            side_effect=lambda argv, **kwargs: main(argv, **kwargs, app_server_factory=server)))
        plan, args = self.plan(tid, "--codex-bin", str(binary))
        self.assertEqual(plan["native_ids"], [tid])
        binary.write_bytes(b"replaced runtime")
        self.invoke("apply", *args, "--clients-closed", ok=False)
        self.assertTrue(rollout.exists())
        plan, args = self.plan(tid, "--codex-bin", str(binary))
        self.assertEqual(self.invoke("apply", *args, "--clients-closed")["goal_status"], "complete")
        self.assertFalse(rollout.exists())
        self.assertEqual(self.invoke("verify", *args)["goal_status"], "complete")

    def test_t3_and_paseo_product_requests_are_qualified_by_instance(self):
        cases = [(t3_fixture.T3ServerDeleteTests, "t3code", t3_fixture.THREAD, "root", "t3_soft_delete"),
                 (paseo_fixture.PaseoDaemonDeleteTests, "paseo", paseo_fixture.AGENT, "profile", "paseo_daemon_delete")]
        for factory, self.client, identifier, root_attr, semantics in cases:
            with self.subTest(client=self.client):
                fixture = factory()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                self.root = getattr(fixture, root_attr)
                if self.client == "t3code":
                    row = next(r for r in self.invoke("records")["records"] if r["record_id"] == identifier)
                    self.assertEqual(row["provider_references"][0]["locator"], "native-codex")
                plan, args = self.plan(identifier, "--frontend-only")
                self.assertEqual(plan["completion_scope"], semantics)
                self.assertNotIn("PRIVATE", json.dumps(plan))
                self.assertEqual(self.invoke("apply", *args)["goal_status"], "complete")
                self.assertEqual(self.invoke("verify", *args)["goal_status"], "complete")
                self.assertTrue(fixture.native.is_file())

    def test_worker_rejects_unknown_fields_and_host_options(self):
        request = {"protocol": remote.PROTOCOL, "destination": "-oProxyCommand=bad", "verb": "records",
                   "client": "pi", "root": str(self.root)}
        with self.assertRaises(ValueError):
            remote.dispatch(request)
        with self.assertRaises(ValueError):
            remote.dispatch({**request, "destination": "valid", "shell": "bad"})

    def test_remote_inventory_reports_progress_separately_from_its_result(self):
        self.pi_file(self.root, "sample")
        output, progress = io.StringIO(), io.StringIO()
        code = main(["remote", "records", "--client", "pi", "--root", str(self.root),
                     "--ssh-host", self.host, "--progress"], stdout=output, stderr=progress)
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["goal_status"], "complete")
        started, completed = map(json.loads, progress.getvalue().splitlines())
        self.assertEqual((started["stage"], started["status"]), ("inventory", "started"))
        self.assertEqual((completed["status"], completed["counts"]["record_count"]), ("completed", 1))

    def test_latest_t3_schema_preserves_unselected_feedback_and_guards_forked_runs(self):
        from local_agent_manager import t3code_database as database, t3code_store as store
        with closing(create_store(self.root, objects=store.SCHEMA_CONTRACTS[-1])) as db:
            for tid in ("selected", "keeper"):
                add_thread(db, tid, native="native-" + tid)
                insert(db, V2 + "runs", thread_id=tid, run_id="run-" + tid)
                insert(db, V2 + "turn_items", turn_item_id="it-" + tid, thread_id=tid,
                       run_id="run-" + tid, payload_json="{}")
            insert(db, "agent_feedback", id="keep-feedback", thread_id="keeper", run_id="run-keeper")
            insert(db, "workflow_steps", run_id="wf", name="agent", thread_id="selected", environment_id="peer")
            db.commit()
            with self.assertRaisesRegex(ValueError, "workflow_or_feedback_reference"):
                database.freeze(self.root, ["selected"])
            db.execute("UPDATE workflow_steps SET thread_id=NULL, turn_id='run-selected'")
            db.commit()
            with self.assertRaisesRegex(ValueError, "workflow_or_feedback_reference"):
                database.freeze(self.root, ["selected"])
            db.execute("DELETE FROM workflow_steps")
            db.execute(f"UPDATE {V2}threads SET fork_run_ids_json=? WHERE thread_id='keeper'", ('["run-selected"]',))
            db.commit()
            with self.assertRaisesRegex(ValueError, "shared_fork_history"):
                database.freeze(self.root, ["selected"])
            db.execute(f"UPDATE {V2}threads SET fork_run_ids_json=NULL")
            db.commit()
            evidence = database.freeze(self.root, ["selected"])
            self.assertEqual(evidence["linked_run_ids"], ["run-selected"])
            database.apply(evidence, phase_callback=lambda _: None)
            self.assertEqual(database.remaining(evidence), 0)
            self.assertEqual(db.execute("SELECT id FROM agent_feedback").fetchall(), [("keep-feedback",)])
            self.assertEqual(db.execute(f"SELECT thread_id FROM {V2}thread_item_counts").fetchall(), [("keeper",)])
            db.execute(f"UPDATE {V2}turn_items SET run_id='run-selected' WHERE thread_id='keeper'")
            db.commit()
            with self.assertRaisesRegex(ValueError, "shared_fork_history"):
                database.remaining(evidence)

    def test_orca_reads_the_journal_owners_not_the_ssh_alias(self):
        self.client = "orca"
        self.root = self.base / "orca"
        orca_support.create_profile(self.root, accounts=0)
        row = orca_support.make_record(Path("/home/example/.codex"))
        row["location"]["executionHostId"] = "ssh:connection-from-orca"
        orca_support.replace_record(self.root, row)
        result = self.invoke("records")
        record, = result["records"]
        self.assertEqual(record["execution_host"], "ssh:connection-from-orca")
        self.assertEqual(record["native_ids"], [orca_support.HISTORY_ID, orca_support.CURRENT_ID])
        self.assertNotIn(orca_support.SENTINEL, json.dumps(result))

    @unittest.skipUnless(os.name == "nt", "Full local lifecycle adapters require Windows")
    def test_full_owner_plans_delete_the_selected_native_transcript_and_frontend(self):
        for self.client, fixture, key in (("t3code", test_t3code_fullflow.T3CodeFullflowTests(), "thread_id"),
                                         ("paseo", test_paseo_fullflow.PaseoFullflowTests(), "agent_id")):
            with self.subTest(client=self.client):
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                self.root = fixture.root
                binding = fixture.manifest["native_stores"][0]
                identifier = binding[key]
                native_id = test_paseo_fullflow.NATIVE
                home = fixture.base / "claude"
                transcript = home / "projects" / "fixture" / (native_id + ".jsonl")
                transcript.parent.mkdir(parents=True)
                transcript.write_text(json.dumps({"sessionId": native_id, "type": "user", "message": "PRIVATE"}) + "\n")
                binding.update(engine="claude", root=str(home))
                if self.client == "paseo":
                    source = self.root / "agents" / (identifier + ".json")
                    row = json.loads(source.read_bytes())
                    for entry in (row, row["persistence"], row["runtimeInfo"]):
                        entry["provider"] = "claude"
                    test_paseo_fullflow.write(source, row)
                else:
                    module = test_t3code_fullflow
                    # Exercise the current release migrations through the public full writer.
                    from local_agent_manager.t3code_store import SCHEMA_CONTRACTS
                    database_path = self.root / "userdata/statev2.sqlite"
                    database_path.unlink()
                    with closing(create_store(self.root, objects=SCHEMA_CONTRACTS[-1])) as db:
                        module.add_thread(db, module.KEEP, native=module.OTHER)
                        module.add_thread(db, identifier, native=native_id, driver="claudeAgent")
                path = fixture.base / "bindings.json"
                path.write_text(json.dumps(fixture.manifest), encoding="utf-8")
                plan, args = self.plan(identifier, "--bindings", str(path))
                self.assertEqual(plan["completion_scope"], "bound_frontend_and_native")
                self.assertNotIn("PRIVATE", json.dumps(plan))
                self.assertEqual(self.invoke("apply", *args, "--clients-closed")["goal_status"], "complete")
                self.assertFalse(transcript.exists())
                self.assertEqual(self.invoke("verify", *args)["goal_status"], "complete")
                self.assertNotIn(identifier, [r["record_id"] for r in self.invoke("records")["records"]])

    def test_cindy_ssh_claude_requires_owner_completion_before_controller_cleanup(self):
        def separate_process(host, payload):
            self.assertEqual(host, self.host)
            worker = subprocess.run([sys.executable, "-m", "local_agent_manager", "remote-worker"], input=payload,
                                    capture_output=True, timeout=30)
            self.last_worker_reply = json.loads(worker.stdout)
            return worker.returncode, worker.stdout
        self.transport.side_effect = separate_process
        self.client = "claude"
        self.root = Path.home() / ".xdt-server" / "v1" / "claude-home"
        identifier = "11111111-1111-4111-8111-111111111111"
        transcript = Path("projects") / "project" / (identifier + ".jsonl")
        def create(home):
            path = home / transcript
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"sessionId": identifier, "type": "user", "message": "PRIVATE"}) + "\n")
            return path
        original = create(self.root)
        unrelated = create(self.base / "controller-native")
        profile = self.base / "controller"
        profile.mkdir()
        database, (sid,) = cindy_fixture.FrontendSessionCleanupTests()._database(profile, 1)
        with closing(sqlite3.connect(database)) as db:
            db.execute("ALTER TABLE sessions ADD COLUMN remote_host_id TEXT")
            db.execute("ALTER TABLE sessions ADD COLUMN agent_device_id TEXT")
            db.execute("ALTER TABLE sessions ADD COLUMN active_turn_started_at INTEGER")
            db.execute("ALTER TABLE sessions ADD COLUMN last_turn_ended_at INTEGER")
            db.execute("UPDATE sessions SET agent_kind='cc', remote_host_id=?, sdk_session_id=? WHERE id=?",
                       (self.host, identifier, sid))
            db.commit()
        plan, args = self.plan(identifier)
        saved = self.base / "native-plan.json"
        saved.write_text(json.dumps(plan), encoding="utf-8")

        def local(*argv, ok=True):
            output = io.StringIO()
            code = main([*argv, "--json"], stdout=output, stderr=io.StringIO(), client_inspector=lambda *_a, **_k: ())
            result = json.loads(output.getvalue())
            self.assertEqual(code == 0, ok, (result, self.last_worker_reply))
            return result

        select = ("--client", "cindy", "--cindy-db", str(database), "--record-id", sid)
        with_ssh = (*select, "--cindy-remote-plan", str(saved))
        record, = local("records", *select)["records"]
        reference, = record["references"]
        self.assertEqual((reference["host"], reference["opaque_native_locator"], reference.get("native_record")),
                         ("ssh:" + self.host, identifier, None))
        self.assertEqual(record["record_metadata"]["working_dir"], "C:/deleted")
        self.assertFalse(record["cleanup_eligible"])
        for selector in (("--record-id", sid), ("--record-id", "cindy:" + sid[:2]),
                         ("--project", "C:/deleted"), ("--all-projects",)):
            result = local("delete", "plan", *select[:4], *selector, ok=False)
            self.assertIn("cindy_execution_host_required", {b["blocker_code"] for b in result["blockers"]})
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET remote_host_id='different' WHERE id=?", (sid,))
            db.commit()
            local("delete", "plan", *with_ssh, ok=False)
            db.execute("UPDATE sessions SET remote_host_id=?, active_turn_started_at=100 WHERE id=?", (self.host, sid))
            db.commit()
            self.assertIn("cindy_ssh_turn_unfinished", json.dumps(local("delete", "plan", *with_ssh, ok=False)))
            db.execute("UPDATE sessions SET last_turn_ended_at=101 WHERE id=?", (sid,))
            db.commit()
        from tests.test_cindy_references import create_database
        sibling = profile / "cindy-other.db"
        create_database(sibling, [("other", identifier, "active", "cc")])
        with closing(sqlite3.connect(sibling)) as db:
            db.execute("ALTER TABLE sessions ADD COLUMN remote_host_id TEXT")
            db.execute("UPDATE sessions SET remote_host_id=?", (self.host,))
            db.commit()
            refused = local("delete", "plan", *with_ssh, ok=False)
            self.assertIn("cindy_ssh_shared_native_handle", json.dumps(refused))
            db.execute("UPDATE sessions SET remote_host_id='independent'")
            db.commit()
        controller = local("delete", "plan", *with_ssh)
        operation = ("--operation-id", controller["operation_id"], "--plan", controller["plan_path"])
        apply = ("delete", "apply", *operation, "--plan-sha256", controller["plan_sha256"], "--clients-closed")
        self.assertFalse(local(*apply, ok=False)["mutation_started"])
        self.assertEqual(self.invoke("apply", *args, "--clients-closed")["goal_status"], "complete")
        self.assertFalse(original.exists())
        local("delete", "apply", *operation, "--clients-closed", ok=False)
        local(*apply, "--cindy-root", str(self.base), ok=False)
        create(self.root)
        refused = local(*apply, ok=False)
        self.assertFalse(refused["mutation_started"], refused)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT sdk_session_id FROM sessions WHERE id=?", (sid,)).fetchone(), (identifier,))
        original.unlink()  # Simulate the owner retiring the independently recreated fixture.
        self.assertEqual(local(*apply)["goal_status"], "complete")
        self.assertEqual(local("operation", "verify", *operation)["goal_status"], "complete")
        self.assertTrue(unrelated.exists())
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT id FROM sessions").fetchall(), [("active",)])
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE session_id=?", (sid,)).fetchone())
