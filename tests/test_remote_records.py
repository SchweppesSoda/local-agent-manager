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
import hashlib
import base64
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
from local_agent_manager import cindy_hosted


def orca_worker_fixture():
    """The SSH child uses a synthetic app-server and only the test's managed home."""
    from contextlib import ExitStack
    from types import SimpleNamespace
    from uuid import uuid4
    root = Path(sys.argv[1])
    home = root / "codex-accounts/account-1/home"
    def server(_runtime, *, codex_home, timeout, job_ready_callback):
        assert Path(codex_home) == home
        job_ready_callback("Local\\larj-orca-" + uuid4().hex)
        def delete_thread(identifier):
            assert identifier in {orca_support.CURRENT_ID, orca_support.HISTORY_ID}
            with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
                path, = db.execute("SELECT rollout_path FROM threads WHERE id=?", (identifier,)).fetchone()
                assert Path(path).is_relative_to(home / "sessions")
                db.execute("DELETE FROM threads WHERE id=?", (identifier,))
                db.commit()
            Path(path).unlink()
        return nullcontext(SimpleNamespace(delete_thread=delete_thread))
    with ExitStack() as stack:
        for target, value in (
            ("orca_runtime_probe.probe_contract", lambda *args: None),
            ("orca_target_safety._windows_process_snapshot", lambda: ()),
            ("orca_target_safety._system_configuration_root", lambda: root.parent / "system-config"),
            ("office_runtime.processes", lambda: ()),
            ("orca_runtime.IsolatedCodexRuntime.server", server),
        ):
            stack.enter_context(patch("local_agent_manager." + target, value))
        return remote.worker(sys.stdin.buffer, sys.stdout.buffer)


def hosted_codex_worker_fixture(root, binary):
    from types import SimpleNamespace
    def server(*, codex_home, codex_binary, **_):
        assert (Path(codex_home), Path(codex_binary)) == (root, binary)
        def delete_thread(identifier):
            assert identifier == "11111111-1111-4111-8111-111111111111"
            with closing(sqlite3.connect(root / "state_5.sqlite")) as db:
                rollout, = db.execute("SELECT rollout_path FROM threads WHERE id=?", (identifier,)).fetchone()
                assert Path(rollout).is_relative_to(root / "sessions")
                db.execute("DELETE FROM threads WHERE id=?", (identifier,))
                db.commit()
            Path(rollout).unlink()
        return nullcontext(SimpleNamespace(delete_thread=delete_thread))
    with patch("local_agent_manager.cli.main", side_effect=lambda argv, **kw: main(argv, **kw, app_server_factory=server)):
        return remote.worker(sys.stdin.buffer, sys.stdout.buffer)


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

    def controller(self, *args, ok=True):
        out = io.StringIO()
        code = main([*args, "--json"], stdout=out, stderr=io.StringIO(), client_inspector=lambda *a, **k: ())
        value = json.loads(out.getvalue())
        self.assertEqual(code == 0, ok, value)
        return value

    def hosted_devices(self, share=False):
        controller = self.base / "controller"
        host = self.base / "executor"
        controller.mkdir()
        host.mkdir()
        self.enterContext(patch.object(cindy_hosted, "_hardware_id", return_value="c" * 64))
        self.enterContext(patch("local_agent_manager.codex_desktop_state.running_related_clients", return_value=()))
        def separate(hostname, payload):
            self.assertEqual(hostname, self.host)
            fail_index = getattr(self, "interrupt_index", False) and json.loads(payload)["verb"] == "apply"
            if fail_index:
                self.interrupt_index = False
            script = ("import sys; from local_agent_manager import cindy_hosted as h, codex_desktop_state as d, remote_records as r;"
                      "h._hardware_id=lambda:'e'*64; d.running_related_clients=lambda *a,**k:();")
            if fail_index:
                script += "from unittest.mock import Mock; h.frozen_files.apply_rewrite=Mock(side_effect=OSError('interrupted'));"
            if self.client == "native":
                script += ("from tests.test_remote_records import hosted_codex_worker_fixture, Path;"
                           "sys.exit(hosted_codex_worker_fixture(Path(sys.argv[1]),Path(sys.argv[2])))")
            else:
                script += "sys.exit(r.worker(sys.stdin.buffer,sys.stdout.buffer))"
            child = subprocess.run([sys.executable, "-c", script, str(self.root), str(self.base / "codex.exe")],
                                   input=payload, capture_output=True, timeout=60)
            return child.returncode, child.stdout
        self.transport.side_effect = separate
        old, (sid,) = cindy_fixture.FrontendSessionCleanupTests()._database(controller, 1)
        database = old.rename(controller / "cindy-owner.db")
        controller_id = "c" * 64
        peer = json.dumps(["provider-share", "share-one", "guest", "member", controller_id], separators=(",", ":")).ljust(129, "~") if share else controller_id
        peer_dir = hashlib.sha256(peer.encode()).hexdigest()[:16]
        host_sid = cindy_hosted.host_session_id(peer, sid)
        fs = host / "remote-agent" / "workspaces" / peer_dir / host_sid / "fs"
        with closing(sqlite3.connect(database)) as db:
            for field, typ in (("agent_device_id", "TEXT"), ("remote_host_id", "TEXT"),
                               ("active_turn_started_at", "INTEGER"), ("last_turn_ended_at", "INTEGER")):
                db.execute(f"ALTER TABLE sessions ADD COLUMN {field} {typ}")
            db.execute("UPDATE sessions SET status='active',last_turn_ended_at=5,agent_device_id=? WHERE id=?",
                       ("share:share-one" if share else "e"*64, sid))
            db.commit()
        options = ("--cindy-task", sid, "--cindy-host-profile", str(host), "--cindy-controller-profile", str(controller),
                   "--cindy-db", str(database))
        return sid, database, host, peer, fs, options

    def hosted_pi(self, share=False):
        sid, database, host, peer, fs, options = self.hosted_devices(share)
        peer_dir = hashlib.sha256(peer.encode()).hexdigest()[:16]
        agent = host / "pi-agent-home"
        agent.mkdir()
        self.enterContext(patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(agent)}))
        self.root = (host / "remote-agent/guest-homes" / peer_dir / "pi-sessions" if share else agent / "sessions")
        identifier = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        path = self.pi_file(self.root, identifier)
        original = json.loads(path.read_text())
        original["cwd"] = str(fs / "work")
        path.write_text(json.dumps(original) + "\n")
        self.pi_file(self.root, "keeper")
        index = host / "remote-agent/guest-sessions.json"
        if share:
            index.parent.mkdir(exist_ok=True)
            owners = {hashlib.sha256(peer.encode()).hexdigest()[:32]:
                      {"controller": peer, "hostSessionIds": [fs.parent.name], "nativeIds": [str(path)]}}
            sibling = "another-device"
            owners[hashlib.sha256(sibling.encode()).hexdigest()[:32]] = {
                "controller": sibling, "hostSessionIds": ["aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"], "nativeIds": ["other-native"]}
            index.write_text(json.dumps(owners))
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET agent_kind='pi',sdk_session_id=? WHERE id=?", (str(path), sid))
            db.commit()
        return sid, database, path, identifier, options, index

    def approve_cindy_controller(self, sid, database, plan):
        saved = self.base / "host-plan.json"
        saved.write_text(json.dumps(plan), encoding="utf-8")
        local = self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(database),
                                "--record-id", sid, "--cindy-remote-plan", str(saved))
        return "--operation-id", local["operation_id"], "--plan", local["plan_path"], "--plan-sha256", local["plan_sha256"]

    def test_hosted_codex_uses_recorded_rollout_and_removes_exact_thread_location(self):
        from tests.support import create_thread_index, write_rollout
        sid, database, host, _, fs, options = self.hosted_devices()
        self.client = "native"
        tid = "11111111-1111-4111-8111-111111111111"
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET agent_kind='codex',sdk_session_id=? WHERE id=?", (tid, sid))
            db.commit()
        rollout = write_rollout(self.root, tid, originator="codex")
        events = [json.loads(line) for line in rollout.read_text().splitlines()]
        create_thread_index(self.root, [{"id": tid, "rollout_path": str(rollout)}])
        locations = host / "codex-accounts" / hashlib.sha256(b"owner").hexdigest() / "thread-index/locations"
        locations.mkdir(parents=True)
        location = locations / (tid + ".json")
        unrelated = locations / "unrelated.json"
        unrelated.write_text("{}")
        original = {"threadId": tid, "path": str(rollout), "sqliteHome": str(self.root)}
        location.write_text(json.dumps(original))
        binary = self.base / "codex.exe"
        binary.write_bytes(b"fixture")
        binary.chmod(0o700)
        owner = self.invoke("records")["owner"]["store_id"]
        args = ("--owner", owner, "--codex-bin", str(binary), *options)
        self.invoke("plan", *args, ok=False)
        events[0]["payload"]["cwd"] = str(fs / "workspace")
        rollout.write_text("".join(json.dumps(event) + "\n" for event in events))
        location.write_text(json.dumps({**original, "sqliteHome": str(host)}))
        self.invoke("plan", *args, ok=False)
        location.write_text(json.dumps(original))
        plan = self.invoke("plan", *args)
        self.assertEqual(plan["thread_location_to_remove"], location.relative_to(host).as_posix())
        controller_op = self.approve_cindy_controller(sid, database, plan)
        other_db = self.base / "different-controller" / "cindy-another-user.db"
        other_db.parent.mkdir()
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(other_db)) as copy:
            source.backup(copy)
        rejected = self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(other_db), "--record-id", sid,
                                   "--cindy-remote-plan", str(self.base / "host-plan.json"), ok=False)
        self.assertIn("cindy_controller_executor_accounts_differ", json.dumps(rejected))
        op = ("--owner", owner, "--operation-id", plan["operation_id"], "--approval", plan["approval"])
        self.invoke("apply", *op, "--clients-closed", "--controller-plan", controller_op[3])
        self.assertFalse(rollout.exists())
        self.assertFalse(location.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.controller("delete", "apply", *controller_op, "--clients-closed")["goal_status"], "complete")
        location.write_text(json.dumps(original))
        self.assertTrue(self.invoke("verify", *op, ok=False)["result"]["modified"])

    def test_shared_claude_requires_bound_account_home_and_shadow(self):
        sid, database, host, peer, fs, options = self.hosted_devices(share=True)
        self.client = "claude"
        tid = "22222222-2222-4222-8222-222222222222"
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET agent_kind='cc',sdk_session_id=? WHERE id=?", (tid, sid))
            db.commit()
        digest = hashlib.sha256(b"executor-owner").hexdigest()
        account = "cindy-" + digest[:12] + "-test"
        pool = host / "owners" / digest[:20] / "subscription-accounts.json"
        pool.parent.mkdir(parents=True)
        pool.write_text(json.dumps({"bindings": {"anthropic:native:" + tid: {"providerId":"anthropic", "accountId":account}}}))
        index = host / "remote-agent/guest-sessions.json"
        index.parent.mkdir()
        index.write_text(json.dumps({hashlib.sha256(peer.encode()).hexdigest()[:32]: {
            "controller": peer, "nativeIds": [tid], "hostSessionIds": [fs.parent.name]}}))
        def transcript(root, cwd):
            path = root / "projects" / "example" / (tid + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type":"user", "sessionId":tid, "cwd":str(cwd), "message":"PRIVATE"}) + "\n")
            return path
        self.root = Path.home() / ".claude"
        copy = transcript(self.root, fs / "workspace")
        def plan_args():
            return "--owner", self.invoke("records")["owner"]["store_id"], "--record-id", tid, *options
        self.invoke("plan", *plan_args(), ok=False)
        self.root = Path.home() / (".claude-" + account)
        path = transcript(self.root, self.base)
        self.invoke("plan", *plan_args(), ok=False)
        transcript(self.root, fs / "workspace")
        plan, args = self.plan(tid, *options)
        controller_op = self.approve_cindy_controller(sid, database, plan)
        self.invoke("apply", *args, "--clients-closed", "--controller-plan", controller_op[3])
        self.assertEqual(self.controller("delete", "apply", *controller_op, "--clients-closed")["goal_status"], "complete")
        self.assertTrue(copy.exists())
        self.assertFalse(path.exists())

    def test_hosted_device_pi_pairs_checkpoint_and_checks_both_owners(self):
        sid, database, path, identifier, options, _ = self.hosted_pi()
        backup_dir = database.parent / "owners" / hashlib.sha256(b"owner").hexdigest()[:20] / "native-history-backups"
        backup_dir.mkdir(parents=True)
        backup = backup_dir / (hashlib.sha256(sid.encode()).hexdigest() + ".backup")
        body = b'PRIVATE CHECKPOINT'
        header = {"version": 1, "sessionId": sid, "anchor": "assistant", "savedAt": 12,
                  "revision": {"nativeId": str(path), "fromDevice": "e"*64}, "manifest": {"kind": "pi", "nativeId": str(path),
                  "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}}
        checkpoint = json.dumps(header).encode() + b"\n" + body
        backup.write_bytes(checkpoint)
        other = backup_dir / ("a"*64 + ".backup")
        other.write_bytes(b"keeper")
        owner = self.invoke("records")["owner"]["store_id"]
        plan = self.invoke("plan", "--owner", owner, *options)
        self.assertEqual(plan["record_id"], identifier)
        args = ("--owner", owner, "--operation-id", plan["operation_id"], "--approval", plan["approval"])
        saved = self.base / "host-plan.json"
        saved.write_text(json.dumps(plan))
        select = ("delete", "plan", "--client", "cindy", "--cindy-db", str(database), "--record-id", sid, "--cindy-remote-plan", str(saved))
        backup.write_bytes(checkpoint[:-1])
        self.controller(*select, ok=False)
        backup.write_bytes(checkpoint)
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET agent_device_id='another-owner' WHERE id=?", (sid,))
            db.commit()
            self.controller(*select, ok=False)
            db.execute("UPDATE sessions SET agent_device_id=? WHERE id=?", ("e"*64, sid))
            db.commit()
        local = self.controller(*select)
        self.assertNotIn("PRIVATE", json.dumps(local))
        op = ("--operation-id", local["operation_id"], "--plan", local["plan_path"])
        apply = ("delete", "apply", *op, "--plan-sha256", local["plan_sha256"], "--clients-closed")
        self.assertFalse(self.controller(*apply, ok=False)["mutation_started"])
        self.assertTrue(backup.exists())
        self.invoke("apply", *args, "--clients-closed", ok=False)
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET active_turn_started_at=7 WHERE id=?", (sid,))
            db.commit()
            self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
            self.assertTrue(path.exists())
            db.execute("UPDATE sessions SET active_turn_started_at=NULL WHERE id=?", (sid,))
            db.commit()
        self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"])
        self.assertFalse(path.exists())
        self.assertEqual(self.controller(*apply)["goal_status"], "complete")
        self.assertFalse(backup.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.controller("operation", "verify", *op)["goal_status"], "complete")
        backup.write_bytes(checkpoint)
        self.controller("operation", "verify", *op, ok=False)

    def test_shared_pi_never_pairs_guest_arrays_by_position(self):
        sid, database, path, identifier, options, index = self.hosted_pi(share=True)
        stored = json.loads(index.read_bytes())
        row = next(iter(stored.values()))
        row["hostSessionIds"].append("aaaaaaaa-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        index.write_text(json.dumps(stored))
        owner = self.invoke("records")["owner"]["store_id"]
        self.invoke("plan", "--owner", owner, "--record-id", identifier, *options, ok=False)
        row["hostSessionIds"].pop()
        index.write_text(json.dumps(stored))
        wrong = list(options)
        wrong[1] = "different-task"
        self.invoke("plan", "--owner", owner, "--record-id", identifier, *wrong, ok=False)
        plan, args = self.plan(identifier, *options)
        saved = self.base / "host-plan.json"
        saved.write_text(json.dumps(plan))
        local = self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(database),
                                "--record-id", sid, "--cindy-remote-plan", str(saved))
        row["relayDevices"] = ["third-host"]
        index.write_text(json.dumps(stored))
        self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
        self.assertTrue(path.exists())
        del row["relayDevices"]
        index.write_text(json.dumps(stored))
        plan, args = self.plan(identifier, *options)
        saved.write_text(json.dumps(plan))
        local = self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(database),
                                "--record-id", sid, "--cindy-remote-plan", str(saved))
        self.interrupt_index = True
        mixed = self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
        self.assertEqual(mixed["goal_status"], "completed_with_residuals")
        self.assertTrue(mixed["result"]["mutation_started"])
        self.assertEqual(mixed["result"]["batches"][0]["status"], "complete")
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(index.read_bytes()), stored)
        self.invoke("verify", *args, ok=False)
        frozen_stat = index.stat()
        row["relayDevices"] = ["third-host"]
        index.write_text(json.dumps(stored))
        conflict = self.invoke("status", *args, ok=False)
        self.assertTrue(conflict["result"]["modified"])
        self.assertEqual(conflict["result"]["batches"][0]["status"], "complete")
        del row["relayDevices"]
        # Restore the frozen file version, including its original mtime.
        index.write_text(json.dumps(stored))
        os.utime(index, ns=(frozen_stat.st_atime_ns, frozen_stat.st_mtime_ns))
        with closing(sqlite3.connect(database)) as db:
            self.assertTrue(db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone())
        self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"])
        self.assertEqual(self.controller("delete", "apply", "--operation-id", local["operation_id"], "--plan", local["plan_path"],
            "--plan-sha256", local["plan_sha256"], "--clients-closed")["goal_status"], "complete")
        row.update(hostSessionIds=[], nativeIds=[])
        self.assertEqual(json.loads(index.read_bytes()), stored)
        with closing(sqlite3.connect(database)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone())
        self.assertTrue((path.parent / "keeper.jsonl").is_file())

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

    @unittest.skipUnless(os.name == "nt", "The executor requires the qualified Orca Codex runtime")
    def test_orca_paired_runtime_cleans_host_and_only_its_controller_tabs(self):
        from tests.test_orca_fullflow import OrcaFullflowTests
        fixture = OrcaFullflowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.client, self.root = "orca", fixture.root
        key = base64.b64encode(bytes(range(32))).decode()
        (self.root / "orca-e2ee-keypair.json").write_text(json.dumps({"publicKeyB64": key, "secretKeyB64": "PRIVATE"}))
        def separate(host, payload):
            self.assertEqual(host, self.host)
            script = "from tests.test_remote_records import orca_worker_fixture; raise SystemExit(orca_worker_fixture())"
            result = subprocess.run([sys.executable, "-c", script, str(self.root)], input=payload, capture_output=True, timeout=90)
            self.last_worker_reply = json.loads(result.stdout)
            return result.returncode, result.stdout
        self.transport.side_effect = separate
        sid = "orca_fixture_01"
        plan, owner_args = self.plan(sid, "--codex-bin", str(fixture.binary))
        self.assertEqual((plan["completion_scope"], plan["native_ids"]), ("orca_host_session", sorted([orca_support.HISTORY_ID, orca_support.CURRENT_ID])))
        self.assertNotIn("PRIVATE", json.dumps(plan))
        saved = self.base / "remote-plan.json"
        saved.write_text(json.dumps(plan))
        root = self.base / "controller"
        root.mkdir()
        envs = root / "orca-environments.json"
        def pair(public):
            envs.write_text(json.dumps({"version": 1, "environments": [{"id": "environment", "endpoints": [
                {"publicKeyB64": public, "deviceToken": "PRIVATE"}]}]}))
        pair(base64.b64encode(b"x"*32).decode())
        def tabs(tab):
            return {"unifiedTabs": {"workspace-fixture": [{"id": tab, "entityId": sid, "contentType": "agent-session",
                "worktreeId": "workspace-fixture", "groupId": "g"}]}}
        state = {"schemaVersion": 1, "workspaceSessionsByHostId": {"runtime:environment": tabs("target"),
                 "runtime:other": tabs("keeper"), "local": tabs("local")}}
        ui = root / "orca-data.json"
        ui.write_text(json.dumps(state))
        select = ("delete", "plan", "--client", "orca", "--orca-root", str(root), "--record-id", sid, "--orca-remote-plan", str(saved))
        self.controller(*select, ok=False)
        pair(key)
        own_key = root / "orca-e2ee-keypair.json"
        own_key.write_text(json.dumps({"publicKeyB64": key}))
        self.controller(*select, ok=False)
        own_key.unlink()
        tab_root = self.base / "controller-with-tabs-only"
        tab_root.mkdir()
        (tab_root / "orca-environments.json").write_bytes(envs.read_bytes())
        (tab_root / "orca-data.json").write_bytes(ui.read_bytes())
        projection_only = self.controller(*[str(tab_root) if arg == str(root) else arg for arg in select])
        self.assertTrue(projection_only["actions"][0]["impact"]["external_action_payload"]["orca_frontend_evidence"]["journal"]["absent"])
        with closing(sqlite3.connect(root / "agent-session-journal.db")) as db:
            db.executescript(orca_support.DDL)
            db.execute("INSERT INTO agent_session_store_meta VALUES ('session_tabs_recorded', '1')")
            db.execute("INSERT INTO journal_sessions VALUES (?, 'workspace-fixture', 'epoch')", (sid,))
            db.commit()
        self.controller(*select, ok=False)
        with closing(sqlite3.connect(root / "agent-session-journal.db")) as db:
            db.execute("DELETE FROM journal_sessions WHERE session_id=?", (sid,))
            db.commit()
        matching = orca_support.make_record(fixture.home, sid)
        matching["location"]["executionHostId"] = "runtime:environment"
        matching["providerHandleChain"][0]["linkId"] = "foreign-link"
        orca_support.replace_record(root, matching)
        self.controller(*select, ok=False)
        matching["providerHandleChain"][0]["linkId"] = "link_old"
        orca_support.replace_record(root, matching)
        sibling = orca_support.make_record(fixture.home, "other_orca_session")
        sibling["location"]["executionHostId"] = "runtime:environment"
        orca_support.replace_record(root, sibling)
        self.controller(*select, ok=False)
        sibling["location"]["executionHostId"] = "runtime:another-host"
        orca_support.replace_record(root, sibling)
        local = self.controller(*select)
        op = ("--operation-id", local["operation_id"], "--plan", local["plan_path"])
        apply = ("delete", "apply", *op, "--plan-sha256", local["plan_sha256"], "--clients-closed")
        self.assertTrue(local["capabilities"]["codex"]["frontend_session_delete"])
        self.assertFalse(self.controller(*apply, ok=False)["mutation_started"])
        self.assertEqual(json.loads(ui.read_bytes()), state)
        self.enterContext(patch("local_agent_manager.office_runtime.processes", return_value=()))
        self.enterContext(patch("local_agent_manager.orca_target_safety._windows_process_snapshot", return_value=()))
        pair(base64.b64encode(b"x"*32).decode())
        self.invoke("apply", *owner_args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
        self.assertTrue(all(p.is_file() for p in fixture.rollouts))
        pair(key)
        self.invoke("apply", *owner_args, "--clients-closed", "--controller-plan", local["plan_path"])
        self.assertEqual(self.invoke("verify", *owner_args)["goal_status"], "complete")
        self.assertTrue(all(not p.exists() for p in fixture.rollouts))
        pair(base64.b64encode(b"x"*32).decode())
        self.assertFalse(self.controller(*apply, ok=False)["mutation_started"])
        pair(key)
        self.assertEqual(self.controller(*apply)["goal_status"], "complete")
        after = json.loads(ui.read_bytes())["workspaceSessionsByHostId"]
        self.assertEqual(after["runtime:environment"]["unifiedTabs"]["workspace-fixture"], [])
        for host in ("runtime:other", "local"):
            self.assertEqual(after[host], state["workspaceSessionsByHostId"][host])
        with closing(sqlite3.connect(root / "agent-session-journal.db")) as db:
            self.assertEqual(db.execute("SELECT session_id FROM agent_session_records").fetchall(), [("other_orca_session",)])
        self.assertEqual(self.controller("operation", "verify", *op)["goal_status"], "complete")
        tab_op = ("--operation-id", projection_only["operation_id"], "--plan", projection_only["plan_path"])
        self.assertEqual(self.controller("delete", "apply", *tab_op, "--plan-sha256", projection_only["plan_sha256"],
                                         "--clients-closed")["goal_status"], "complete")
        self.assertFalse((tab_root / "agent-session-journal.db").exists())
        self.assertEqual(json.loads((tab_root / "orca-data.json").read_bytes())["workspaceSessionsByHostId"], after)
        orca_support.replace_record(self.root, orca_support.make_record(fixture.home, sid))
        self.controller("operation", "verify", *op, ok=False)

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
