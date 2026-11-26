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
from copy import deepcopy
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
        if "--interrupt-after-native" in sys.argv:
            from local_agent_manager.operation_store import OperationStore
            append = OperationStore.append_event
            def stop_after_receipt(store, event, **kwargs):
                result = append(store, event, **kwargs)
                if event.get("event") == "batch_finished" and event.get("goal_status") == "complete":
                    os._exit(74)
                return result
            stack.enter_context(patch.object(OperationStore, "append_event", stop_after_receipt))
        for target, value in (
            ("orca_runtime_probe.probe_contract", lambda *args: None),
            ("orca_target_safety._windows_process_snapshot", lambda: ()),
            ("orca_target_safety._system_configuration_root", lambda: root.parent / "system-config"),
            ("office_runtime.processes", lambda: ()),
            ("orca_runtime.IsolatedCodexRuntime.server", server),
        ):
            stack.enter_context(patch("local_agent_manager." + target, value))
        return remote.worker(sys.stdin.buffer, sys.stdout.buffer)


def hosted_codex_server(root, binary):
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
    return server


def hosted_codex_worker_fixture(root, binary):
    server = hosted_codex_server(root, binary)
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

    def orca_transport(self, host, payload):
        self.assertEqual(host, self.host)
        script = "from tests.test_remote_records import orca_worker_fixture; raise SystemExit(orca_worker_fixture())"
        extra = []
        if getattr(self, "interrupt_orca", False) and json.loads(payload)["verb"] == "apply":
            self.interrupt_orca = False
            extra.append("--interrupt-after-native")
        result = subprocess.run([sys.executable, "-c", script, str(self.root), *extra], input=payload, capture_output=True, timeout=90)
        if result.returncode == 74:
            raise subprocess.TimeoutExpired("interrupted-test-worker", 1)
        self.last_worker_reply = json.loads(result.stdout)
        if self.drop_after_dispatch:
            self.drop_after_dispatch = False
            raise subprocess.TimeoutExpired("test-worker", 1)
        return result.returncode, result.stdout

    def invoke(self, verb, *extra, ok=True, executor_only=False):
        output = io.StringIO()
        destination = ["--cindy-executor-only"] if executor_only else ["--ssh-host", self.host]
        code = main(["remote", verb, *destination, "--client", self.client,
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

    def shared_pi_pair(self):
        sid, database, path, native, options, index = self.hosted_pi(share=True)
        data = json.loads(index.read_bytes())
        row = next(iter(data.values()))
        sibling = "second-guest-task"
        other_host = cindy_hosted.host_session_id(row["controller"], sibling)
        other = self.pi_file(self.root, "sibling")
        record = json.loads(other.read_bytes())
        workspaces = Path(json.loads(path.read_bytes())["cwd"]).parents[2]
        record["cwd"] = str(workspaces / other_host / "fs" / "work")
        other.write_text(json.dumps(record) + "\n")
        row["hostSessionIds"].append(other_host)
        row["nativeIds"].insert(0, str(other))
        row["attachmentRunIds"] = ["aaaaaaaa-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]
        index.write_text(json.dumps(data))
        with closing(sqlite3.connect(database)) as db:
            db.execute("INSERT INTO sessions(id,status,agent_kind,sdk_session_id,agent_device_id) VALUES(?,?,?,?,?)",
                       (sibling, "active", "pi", str(other), "share:share-one"))
            db.execute("INSERT INTO messages(id,session_id,role,content) VALUES(?,?,?,?)", ("keep", sibling, "user", "KEEP"))
            db.commit()
        return sid, database, path, native, index, sibling, other, options

    def sharing_files(self, host, peer):
        owner_dir = host / "owners" / hashlib.sha256(b"executor-owner").hexdigest()[:20]
        owner_dir.mkdir(parents=True)
        files = {
            host / "device-link-settings.json": {"remoteControlEnabled": True, "revokedControllers": []},
            owner_dir / "provider-share-availability.json": {"shares": {"share-one": "pi"}},
            owner_dir / "provider-share-standby-members.json": {"members": {peer: {"joinedAt": 1730000000000}}},
        }
        for path, content in files.items():
            path.write_text(json.dumps(content, indent=2), encoding="utf-8")
        return {path: path.read_bytes() for path in files}

    def clean_on_executor_by_native(self, host, database, native, *options):
        controller_bytes = database.read_bytes()
        offline = database.parent.rename(self.base / "offline-controller")
        self.enterContext(patch.object(cindy_hosted, "_hardware_id", return_value="e" * 64))
        with patch.object(remote, "_ssh", side_effect=AssertionError("unexpected SSH")):
            owner = self.invoke("records", executor_only=True)["owner"]["store_id"]
            plan = self.invoke("plan", "--owner", owner, "--record-id", native, "--cindy-host-profile", str(host),
                               *options, executor_only=True)
            args = ("--owner", owner, "--operation-id", plan["operation_id"], "--approval", plan["approval"])
            self.assertEqual(self.invoke("apply", *args, "--clients-closed", executor_only=True)["goal_status"], "complete")
            self.assertEqual(self.invoke("verify", *args, executor_only=True)["goal_status"], "complete")
        self.assertEqual((offline / database.name).read_bytes(), controller_bytes)

    def test_controller_only_deletes_exact_rows_with_executor_offline(self):
        sid, database, path, _, index, sibling, other, _ = self.shared_pi_pair()
        host = index.parent.parent
        peer = next(iter(json.loads(index.read_bytes()).values()))["controller"]
        grants = self.sharing_files(host, peer)
        before = [f.read_bytes() for f in (path, other, index)]
        offline = host.rename(self.base / "offline-executor")
        self.transport.side_effect = AssertionError("A contacted the offline executor")
        select = ("--client", "cindy", "--cindy-db", str(database), "--record-id", sid)
        self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(database),
                        "--record-id", sid[:-1], "--cindy-controller-only", ok=False)
        self.controller("delete", "plan", *select, ok=False)
        plan = self.controller("delete", "plan", *select, "--cindy-controller-only")
        self.assertTrue(plan["scope"]["cindy_controller_only"])
        self.assertEqual([a["kind"] for a in plan["actions"]], ["delete_frontend_session"])
        op = ("--operation-id", plan["operation_id"], "--plan", plan["plan_path"])
        apply = ("delete", "apply", *op, "--plan-sha256", plan["plan_sha256"], "--clients-closed")
        self.controller("delete", "apply", *op, "--clients-closed", ok=False)
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET parent_session_id=? WHERE id=?", (sid, sibling))
            db.commit()
            self.assertFalse(self.controller(*apply, ok=False)["mutation_started"])
            db.execute("UPDATE sessions SET parent_session_id=NULL WHERE id=?", (sibling,))
            db.commit()
        self.assertEqual(self.controller(*apply)["goal_status"], "complete")
        self.assertEqual(self.controller("operation", "verify", *op)["goal_status"], "complete")
        with closing(sqlite3.connect(database)) as db:
            self.assertIsNone(db.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone())
            self.assertEqual(db.execute("SELECT id FROM messages WHERE session_id=?", (sid,)).fetchall(), [])
            self.assertEqual(db.execute("SELECT sdk_session_id FROM sessions WHERE id=?", (sibling,)).fetchone(), (str(other),))
            self.assertEqual(db.execute("SELECT content FROM messages WHERE session_id=?", (sibling,)).fetchone(), ("KEEP",))
        offline.rename(host)
        self.assertEqual([f.read_bytes() for f in (path, other, index)], before)
        self.assertEqual({p: p.read_bytes() for p in grants}, grants)
        self.transport.assert_not_called()

    def test_executor_only_deletes_one_direct_guest_and_recovers_without_controller(self):
        sid, database, path, native, index, _, other, _ = self.shared_pi_pair()
        host = index.parent.parent
        expected_index = json.loads(index.read_bytes())
        guest = next(iter(expected_index.values()))
        guest["hostSessionIds"].remove(cindy_hosted.host_session_id(guest["controller"], sid))
        guest["nativeIds"].remove(str(path))
        kept = other.read_bytes()
        grants = self.sharing_files(host, guest["controller"])
        controller_bytes = database.read_bytes()
        offline = database.parent.rename(self.base / "offline-controller")
        self.transport.side_effect = AssertionError("B contacted the offline controller")
        self.enterContext(patch.object(cindy_hosted, "_hardware_id", return_value="e" * 64))
        def local(verb, *args, ok=True):
            return self.invoke(verb, *args, executor_only=True, ok=ok)
        owner = local("records")["owner"]["store_id"]
        options = ("--owner", owner, "--cindy-host-profile", str(host))
        local("plan", *options, "--cindy-task", sid, "--record-id", "sibling", ok=False)
        local("plan", *options, "--cindy-task", sid, "--cindy-db", str(database), ok=False)
        alias = self.pi_file(self.root, "unindexed")
        duplicate = {**json.loads(path.read_bytes()), "id": "unindexed"}
        alias.write_text(json.dumps(duplicate) + "\n")
        local("plan", *options, "--record-id", native, ok=False)
        alias.unlink()
        selected = local("plan", *options, "--record-id", native)
        self.assertEqual(selected["cindy_hosted"]["host_session_id"], cindy_hosted.host_session_id(guest["controller"], sid))
        plan = local("plan", *options, "--cindy-task", sid)
        self.assertEqual(plan["record_id"], native)
        self.assertEqual(plan["completion_scope"], "cindy_executor_only")
        op = ("--owner", owner, "--operation-id", plan["operation_id"], "--approval", plan["approval"])
        local("apply", *op, ok=False)
        self.assertTrue(path.exists())
        original = index.read_bytes(), index.stat()
        changed = json.loads(original[0])
        next(iter(changed.values()))["forkWorkspaces"] = {str(other): [selected["cindy_hosted"]["host_session_id"]]}
        index.write_text(json.dumps(changed))
        local("apply", *op, "--clients-closed", ok=False)
        self.assertTrue(path.exists())
        index.write_bytes(original[0])
        os.utime(index, ns=(original[1].st_atime_ns, original[1].st_mtime_ns))
        with patch.object(cindy_hosted.frozen_files, "apply_rewrite", side_effect=OSError("interrupted before index")):
            partial = local("apply", *op, "--clients-closed", ok=False)
        self.assertEqual(partial["goal_status"], "completed_with_residuals")
        self.assertEqual(partial["result"]["batches"][0]["status"], "complete")
        self.assertFalse(path.exists())
        self.assertEqual(index.read_bytes(), original[0])
        local("verify", *op, ok=False)
        alias.write_text(json.dumps(duplicate) + "\n")
        conflict = local("apply", *op, "--clients-closed", ok=False)
        self.assertEqual(conflict["result"]["batches"][0]["status"], "complete")
        self.assertEqual(conflict["completion_scope"], "cindy_executor_only")
        self.assertEqual(index.read_bytes(), original[0])
        alias.unlink()
        self.assertEqual(local("apply", *op, "--clients-closed")["goal_status"], "complete")
        self.assertEqual(local("verify", *op)["goal_status"], "complete")
        self.assertEqual(json.loads(index.read_bytes()), expected_index)
        self.assertEqual(other.read_bytes(), kept)
        self.assertEqual({p: p.read_bytes() for p in grants}, grants)
        self.assertEqual((offline / database.name).read_bytes(), controller_bytes)
        self.transport.assert_not_called()

    def test_last_guest_task_on_executor_after_controller_only_cleanup(self):
        sid, database, path, native, _, index = self.hosted_pi(share=True)
        host = index.parent.parent
        expected = json.loads(index.read_bytes())
        guest = next(iter(expected.values()))
        grants = self.sharing_files(host, guest["controller"])
        guest.update(hostSessionIds=[], nativeIds=[])
        offline = host.rename(self.base / "offline-executor")
        with patch.object(remote, "_ssh", side_effect=AssertionError("unexpected SSH")):
            plan = self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(database),
                                   "--record-id", sid, "--cindy-controller-only")
            op = ("--operation-id", plan["operation_id"], "--plan", plan["plan_path"])
            self.controller("delete", "apply", *op, "--plan-sha256", plan["plan_sha256"], "--clients-closed")
            self.controller("operation", "verify", *op)
        offline.rename(host)
        self.clean_on_executor_by_native(host, database, native)
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(index.read_bytes()), expected)
        self.assertEqual({p: p.read_bytes() for p in grants}, grants)

    def test_single_end_approvals_cannot_be_used_for_joint_cleanup(self):
        sid, database, path, native, options, index = self.hosted_pi(share=True)
        self.host = "local-executor"  # Deliberately collide with the local destination label.
        joint, joint_args = self.plan(native, *options)
        select = ("delete", "plan", "--client", "cindy", "--cindy-db", str(database), "--record-id", sid)
        controller = self.controller(*select, "--cindy-controller-only")
        with patch.object(cindy_hosted, "_hardware_id", return_value="e" * 64):
            standalone = self.invoke("plan", "--owner", joint["owner"]["store_id"], "--cindy-host-profile",
                                     str(index.parent.parent), "--cindy-task", sid, executor_only=True)
        provider_plan = self.base / "executor-only.json"
        provider_plan.write_text(json.dumps(standalone), encoding="utf-8")
        self.controller(*select, "--cindy-remote-plan", str(provider_plan), ok=False)
        self.invoke("apply", *joint_args, "--controller-plan", controller["plan_path"], "--clients-closed", ok=False)
        self.invoke("apply", *joint_args, "--clients-closed", executor_only=True, ok=False)
        offline_args = ("--owner", standalone["owner"]["store_id"], "--operation-id", standalone["operation_id"],
                        "--approval", standalone["approval"])
        self.invoke("apply", *offline_args, "--clients-closed", ok=False)
        self.assertTrue(path.is_file())
        with closing(sqlite3.connect(database)) as db:
            self.assertTrue(db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone())

    def test_executor_only_rechecks_local_and_parked_native_references(self):
        sid, database, path, _, _, index = self.hosted_pi(share=True)
        database.parent.rename(self.base / "offline-controller")
        host = index.parent.parent
        local_db, (local_sid,) = cindy_fixture.FrontendSessionCleanupTests()._database(host, 1)
        local_db = local_db.rename(host / "cindy-owner.db")
        self.enterContext(patch.object(cindy_hosted, "_hardware_id", return_value="e" * 64))
        owner = self.invoke("records", executor_only=True)["owner"]["store_id"]
        options = ("plan", "--owner", owner, "--cindy-host-profile", str(host), "--cindy-task", sid)
        with closing(sqlite3.connect(local_db)) as db:
            db.execute("ALTER TABLE sessions ADD COLUMN agent_device_id TEXT")
            db.execute("UPDATE sessions SET agent_kind='pi',sdk_session_id=? WHERE id=?", (str(path), local_sid))
            db.commit()
            self.invoke(*options, executor_only=True, ok=False)
            db.execute("UPDATE sessions SET agent_device_id=? WHERE id=?", ("d" * 64, local_sid))
            db.commit()
            plan = self.invoke(*options, executor_only=True)
            op = ("--owner", owner, "--operation-id", plan["operation_id"], "--approval", plan["approval"])
            switch = {"fromAgentKind": "pi", "fromAgentDeviceId": None, "fromSdkSessionId": str(path)}
            db.execute("INSERT INTO messages(id,session_id,role,content) VALUES(?,?,?,?)",
                       ("switch", local_sid, "agent_switch", json.dumps(switch)))
            db.commit()
            self.invoke("apply", *op, "--clients-closed", executor_only=True, ok=False)
        self.assertTrue(path.is_file())
        self.transport.assert_not_called()

    def test_executor_only_recognizes_cached_same_account_device_with_controller_offline(self):
        sid, database, path, _, _, _ = self.hosted_pi()
        host = self.root.parents[1]
        database.parent.rename(self.base / "offline-controller")
        cached = host / "device-link-settings.json"
        cached.write_text(json.dumps({"lastKnownDeviceNames": {"c" * 64: "A", "f" * 64: "other PC"}}))
        self.enterContext(patch.object(cindy_hosted, "_hardware_id", return_value="e" * 64))
        self.transport.side_effect = AssertionError("unexpected SSH")
        owner = self.invoke("records", executor_only=True)["owner"]["store_id"]
        plan = self.invoke("plan", "--owner", owner, "--cindy-host-profile", str(host), "--cindy-task", sid, executor_only=True)
        self.assertEqual(plan["cindy_hosted"]["controller_device"], "c" * 64)
        op = ("--owner", owner, "--operation-id", plan["operation_id"], "--approval", plan["approval"])
        self.invoke("apply", *op, "--clients-closed", executor_only=True)
        self.assertEqual(self.invoke("verify", *op, executor_only=True)["goal_status"], "complete")
        self.assertFalse(path.exists())
        self.assertTrue((self.root / "project/keeper.jsonl").is_file())
        self.transport.assert_not_called()

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

    def test_shared_codex_deletes_only_one_guest_rollout(self):
        self.run_shared_codex(executor_only=False)

    def test_executor_only_shared_codex_by_native_id(self):
        self.run_shared_codex(executor_only=True)

    def run_shared_codex(self, *, executor_only):
        from tests.support import create_thread_index, write_rollout
        sid, database, host, peer, fs, options = self.hosted_devices(share=True)
        self.client = "native"
        self.root = host / "remote-agent/guest-homes" / fs.parents[1].name / "codex"
        native_ids = ["11111111-1111-4111-8111-111111111111", "77777777-7777-4777-8777-777777777777"]
        tasks = [sid, "second-codex-task"]
        hosts = [cindy_hosted.host_session_id(peer, task) for task in tasks]
        rollouts = []
        for native, task_host in zip(native_ids, hosts):
            path = write_rollout(self.root, native, originator="codex")
            events = [json.loads(line) for line in path.read_bytes().splitlines()]
            events[0]["payload"]["cwd"] = str(fs.parents[1] / task_host / "fs")
            path.write_text("".join(json.dumps(e) + "\n" for e in events))
            rollouts.append(path)
        create_thread_index(self.root, [dict(id=native, rollout_path=str(path)) for native, path in zip(native_ids, rollouts)])
        guest_path = host / "remote-agent/guest-sessions.json"
        guest_key = hashlib.sha256(peer.encode()).hexdigest()[:32]
        guest = {"controller": peer, "hostSessionIds": hosts, "nativeIds": native_ids[::-1], "cleanupEpoch": 4}
        guest_path.write_text(json.dumps({guest_key: guest}))
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET agent_kind='codex',sdk_session_id=? WHERE id=?", (native_ids[0], sid))
            db.execute("INSERT INTO sessions(id,status,agent_kind,sdk_session_id,agent_device_id) VALUES(?,?,?,?,?)",
                       (tasks[1], "active", "codex", native_ids[1], "share:share-one"))
            db.commit()
        binary = self.base / "codex.exe"
        binary.write_bytes(b"fixture")
        binary.chmod(0o700)
        if executor_only:
            server = hosted_codex_server(self.root, binary)
            with patch("local_agent_manager.cli.main", side_effect=lambda argv, **kw: main(argv, **kw, app_server_factory=server)):
                self.clean_on_executor_by_native(host, database, native_ids[0], "--codex-bin", str(binary))
        else:
            plan, op = self.plan(native_ids[0], "--codex-bin", str(binary), *options)
            local = self.approve_cindy_controller(sid, database, plan)
            self.invoke("apply", *op, "--controller-plan", local[3], "--clients-closed")
            self.assertEqual(self.invoke("verify", *op)["goal_status"], "complete")
            self.assertEqual(self.controller("delete", "apply", *local, "--clients-closed")["goal_status"], "complete")
        self.assertFalse(rollouts[0].exists())
        self.assertTrue(rollouts[1].is_file())
        self.assertEqual(json.loads(guest_path.read_bytes())[guest_key],
                         dict(guest, nativeIds=[native_ids[1]], hostSessionIds=[hosts[1]]))

    def test_shared_claude_requires_bound_account_home_and_shadow(self):
        self.run_shared_claude(executor_only=False)

    def test_executor_only_shared_claude_by_native_id(self):
        self.run_shared_claude(executor_only=True)

    def run_shared_claude(self, *, executor_only):
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
        def transcript(root, cwd, native=tid):
            path = root / "projects" / "example" / (native + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type":"user", "sessionId":native, "cwd":str(cwd), "message":"PRIVATE"}) + "\n")
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
        sibling = "another-claude-task"
        native = "33333333-3333-4333-8333-333333333333"
        other_host = cindy_hosted.host_session_id(peer, sibling)
        other_file = transcript(self.root, fs.parents[1] / other_host / "fs", native)
        with closing(sqlite3.connect(database)) as db:
            db.execute("INSERT INTO sessions(id,status,agent_kind,sdk_session_id,agent_device_id) VALUES(?,?,?,?,?)",
                       (sibling, "active", "cc", native, "share:share-one"))
            db.commit()
        guest = json.loads(index.read_bytes())
        row, = guest.values()
        row["hostSessionIds"].append(other_host)
        row["nativeIds"].insert(0, native)
        index.write_text(json.dumps(guest))
        self.invoke("plan", *plan_args(), ok=False)
        owners = json.loads(pool.read_bytes())
        owners["bindings"]["anthropic:native:" + native] = {"providerId": "anthropic", "accountId": account}
        pool.write_text(json.dumps(owners))
        if executor_only:
            self.clean_on_executor_by_native(host, database, tid)
        else:
            plan, args = self.plan(tid, *options)
            controller_op = self.approve_cindy_controller(sid, database, plan)
            self.invoke("apply", *args, "--clients-closed", "--controller-plan", controller_op[3])
            self.assertEqual(self.controller("delete", "apply", *controller_op, "--clients-closed")["goal_status"], "complete")
        self.assertTrue(copy.exists())
        self.assertFalse(path.exists())
        self.assertTrue(other_file.is_file())
        self.assertEqual(next(iter(json.loads(index.read_bytes()).values()))["nativeIds"], [native])

    def controller_pi_checkpoint(self, sid, database, path):
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
        return backup, checkpoint, other

    def test_controller_only_checks_checkpoint_without_executor(self):
        sid, database, path, _, _, index = self.hosted_pi()
        backup, checkpoint, other = self.controller_pi_checkpoint(sid, database, path)
        host = index.parent.parent
        offline = host.rename(self.base / "offline-executor")
        self.transport.side_effect = AssertionError("unexpected SSH")
        plan = self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(database),
                               "--record-id", sid, "--cindy-controller-only")
        self.assertNotIn("PRIVATE", json.dumps(plan))
        op = ("--operation-id", plan["operation_id"], "--plan", plan["plan_path"])
        apply = ("delete", "apply", *op, "--plan-sha256", plan["plan_sha256"], "--clients-closed")
        original = backup.stat()
        backup.write_bytes(checkpoint[:-1])
        self.assertFalse(self.controller(*apply, ok=False)["mutation_started"])
        backup.write_bytes(checkpoint)
        os.utime(backup, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(self.controller(*apply)["goal_status"], "complete")
        self.assertEqual(self.controller("operation", "verify", *op)["goal_status"], "complete")
        self.assertFalse(backup.exists())
        self.assertEqual(other.read_bytes(), b"keeper")
        self.assertTrue((offline / path.relative_to(host)).is_file())
        self.transport.assert_not_called()

    def test_hosted_device_pi_pairs_checkpoint_and_checks_both_owners(self):
        sid, database, path, identifier, options, _ = self.hosted_pi()
        backup, checkpoint, other = self.controller_pi_checkpoint(sid, database, path)
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
        sid, database, path, identifier, index, sibling, other, options = self.shared_pi_pair()
        stored = json.loads(index.read_bytes())
        row = next(iter(stored.values()))
        row["hostSessionIds"].append("aaaaaaaa-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        index.write_text(json.dumps(stored))
        owner = self.invoke("records")["owner"]["store_id"]
        self.invoke("plan", "--owner", owner, "--record-id", identifier, *options, ok=False)
        row["hostSessionIds"].pop()
        sibling_host = cindy_hosted.host_session_id(row["controller"], sibling)
        selected_host = row["hostSessionIds"][0]
        row["forkWorkspaces"] = {str(other): [selected_host]}
        index.write_text(json.dumps(stored))
        self.invoke("plan", "--owner", owner, *options, ok=False)
        del row["forkWorkspaces"]
        with closing(sqlite3.connect(database)) as db:
            db.execute("INSERT INTO sessions(id,status,agent_kind,sdk_session_id,agent_device_id) VALUES(?,?,?,?,?)",
                       ("other-device", "active", "pi", str(path), "d"*64))
            db.commit()
        index.write_text(json.dumps(stored))
        wrong = list(options)
        wrong[1] = "different-task"
        self.invoke("plan", "--owner", owner, "--record-id", identifier, *wrong, ok=False)
        plan, args = self.plan(identifier, *options)
        saved = self.base / "host-plan.json"
        saved.write_text(json.dumps(plan))
        local = self.controller("delete", "plan", "--client", "cindy", "--cindy-db", str(database),
                                "--record-id", sid, "--cindy-remote-plan", str(saved))
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE sessions SET sdk_session_id=? WHERE id=?", (str(path), sibling))
            db.commit()
            self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
            self.assertTrue(path.exists())
            db.execute("UPDATE sessions SET sdk_session_id=? WHERE id=?", (str(other), sibling))
            db.execute("UPDATE sessions SET active_turn_started_at=8,last_turn_ended_at=2 WHERE id=?", (sibling,))
            db.commit()
            self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
            self.assertTrue(path.exists())
            db.execute("UPDATE sessions SET last_turn_ended_at=9 WHERE id=?", (sibling,))
            db.commit()
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
        self.assertEqual(mixed["goal_status"], "completed_with_residuals", mixed)
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
        row.update(hostSessionIds=[sibling_host], nativeIds=[str(other)])
        self.assertEqual(json.loads(index.read_bytes()), stored)
        self.assertTrue(other.is_file())
        self.assertEqual(self.invoke("verify", *args)["goal_status"], "complete")
        self.assertEqual(self.controller("operation", "verify", "--operation-id", local["operation_id"],
                                        "--plan", local["plan_path"])["goal_status"], "complete")
        with closing(sqlite3.connect(database)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone())
            self.assertEqual(db.execute("SELECT sdk_session_id FROM sessions WHERE id=?", (sibling,)).fetchone(), (str(other),))
            self.assertEqual(db.execute("SELECT sdk_session_id FROM sessions WHERE id='other-device'").fetchone(), (str(path),))
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
        output = io.StringIO()
        self.assertEqual(main(["remote", "records", "--local", "--client", "orca", "--root", str(self.root)], stdout=output), 0)
        self.assertEqual(json.loads(output.getvalue())["records"], result["records"])
        self.assertEqual((record["execution_route"], record["provider_transport"]), ("ssh", "codex-app-server"))
        denied = self.invoke("plan", "--owner", result["owner"]["store_id"], "--record-id", record["record_id"], ok=False)
        self.assertIn("orca_execution_host_session_required", str(denied))
        self.assertEqual(record["native_ids"], [orca_support.HISTORY_ID, orca_support.CURRENT_ID])
        self.assertNotIn(orca_support.SENTINEL, json.dumps(result))

    def test_orca_offline_controller_cleans_only_selected_environment_and_workspace(self):
        sid, workspace, environment = "orca_fixture_01", "workspace-fixture", "office/B"
        host = "runtime:office%2fB"  # Orca canonicalizes alternate percent encodings to the same environment.
        root = self.base / "controller"
        other_host_home, = orca_support.create_profile(self.base / "offline-host", accounts=1)
        native = other_host_home / "sessions" / "history.jsonl"
        native.write_bytes(b"retained on peer")
        self.transport.side_effect = AssertionError("offline peer contacted")
        self.enterContext(patch("local_agent_manager.office_runtime.processes", return_value=()))
        self.enterContext(patch("local_agent_manager.orca_target_safety._windows_process_snapshot", return_value=()))
        orca_support.create_profile(root, accounts=0)
        pairing = root / "orca-environments.json"
        def register(key):
            pairing.write_text(json.dumps({"version": 1, "environments": [{"id": environment, "endpoints": [
                {"publicKeyB64": base64.b64encode(key).decode(), "deviceToken": "PRIVATE-TOKEN"}]}]}))
        register(b"B" * 32)
        local = orca_support.make_record(other_host_home, sid)
        orca_support.replace_record(root, local)
        def tabs(tab):
            return {"unifiedTabs": {workspace: [{"id": tab, "entityId": sid, "contentType": "agent-session",
                "worktreeId": workspace, "groupId": "g", "agentSessionAgent": "opencode"}]}}
        state = {"schemaVersion": 1, "workspaceSessionsByHostId": {host: tabs("target"), "local": tabs("local"),
            "runtime:other": tabs("keeper")}, "remoteLayoutSnapshots": {environment: {"workspaces": {workspace: {
                "worktree": workspace, "tabs": [{"id": "saved", "type": "agent-session", "agent": "opencode", "sessionId": sid}]}}}}}
        ui = root / "orca-data.json"
        ui.write_text(json.dumps(state))
        select = ("delete", "plan", "--client", "orca", "--orca-root", str(root), "--record-id", sid, "--orca-controller-only", environment)
        self.controller(*select, "wrong-workspace", ok=False)
        self.controller(*select, workspace, "--orca-remote-plan", "other.json", ok=False)
        plan = self.controller(*select, workspace)
        self.assertEqual(plan["scope"]["orca_controller_only"], [environment, workspace])
        self.assertFalse(plan["capabilities"]["orca"]["native_delete"])
        self.assertNotIn("PRIVATE-TOKEN", json.dumps(plan))
        op = ("--operation-id", plan["operation_id"], "--plan", plan["plan_path"])
        apply = ("delete", "apply", *op, "--plan-sha256", plan["plan_sha256"], "--clients-closed")
        self.controller(*apply, "--orca-controller-only", environment, "wrong-workspace", ok=False)
        with self.assertRaisesRegex(ValueError, "controller_remote_binding_required"):
            remote._check_controller_plan(Path(plan["plan_path"]), {"clients_closed": True})
        register(b"C" * 32)
        self.controller(*apply, ok=False)
        self.assertEqual(json.loads(ui.read_bytes()), state)
        register(b"B" * 32)
        self.assertEqual(self.controller(*apply)["goal_status"], "complete")
        self.assertEqual(self.controller("operation", "verify", *op)["goal_status"], "complete")
        after = json.loads(ui.read_bytes())
        self.assertEqual(after["workspaceSessionsByHostId"][host]["unifiedTabs"][workspace], [])
        for owner in ("local", "runtime:other"):
            self.assertEqual(after["workspaceSessionsByHostId"][owner], state["workspaceSessionsByHostId"][owner])
        self.assertEqual(after["remoteLayoutSnapshots"][environment]["workspaces"][workspace]["tabs"], [])
        with closing(sqlite3.connect(root / "agent-session-journal.db")) as db:
            raw, = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone()
        self.assertEqual(json.loads(raw), local)
        self.assertEqual(native.read_bytes(), b"retained on peer")
        self.transport.assert_not_called()

    def test_orca_registered_provider_can_clean_a_paired_mirror_without_an_engine_writer(self):
        self.client, self.root = "orca", self.base / "registered-controller"
        orca_support.create_profile(self.root, accounts=0)
        sid = "orca_registry_chat"
        row = orca_support.make_record(self.base / "unused", sid, empty=True)
        row.update(provider="cc", accountHome={"kind": "opencode", "locator": {"kind": "unmanaged"}})
        row["providerHandleChain"] = [{"linkId": "link", "origin": "created", "mintedAtFence": 1, "observedAt": 1,
            "handle": {"agent": "cc", "transport": "acp", "nativeId": "opaque", "resumeCursor": "PRIVATE-CURSOR"}}]
        orca_support.replace_record(self.root, row)
        inventory = self.invoke("records")
        record, = inventory["records"]
        self.assertEqual((record["engine"], record["provider_transport"], record["account_locator"]), ("cc", "acp", {"kind": "unmanaged"}))
        self.assertNotIn("PRIVATE-CURSOR", json.dumps(inventory))
        self.assertIn("orca_provider_native_binding_unverified", str(self.invoke("plan", "--owner",
            inventory["owner"]["store_id"], "--record-id", sid, ok=False)))
        (self.root / "orca-environments.json").write_text(json.dumps({"version": 1, "environments": [
            {"id": "peer", "endpoints": [{"publicKeyB64": base64.b64encode(b"B" * 32).decode()}]}]}))
        row["location"]["executionHostId"] = "runtime:peer"
        orca_support.replace_record(self.root, row)
        self.transport.side_effect = AssertionError("offline peer contacted")
        self.enterContext(patch("local_agent_manager.office_runtime.processes", return_value=()))
        self.enterContext(patch("local_agent_manager.orca_target_safety._windows_process_snapshot", return_value=()))
        plan = self.controller("delete", "plan", "--client", "orca", "--orca-root", str(self.root), "--record-id", sid,
                               "--orca-controller-only", "peer", record["workspace_id"])
        self.assertFalse(plan["capabilities"]["unsupported:cc"]["native_delete"])
        self.assertNotIn("PRIVATE-CURSOR", json.dumps(plan))
        op = ("--operation-id", plan["operation_id"], "--plan", plan["plan_path"])
        self.assertEqual(self.controller("delete", "apply", *op, "--plan-sha256", plan["plan_sha256"],
                                         "--clients-closed")["goal_status"], "complete")
        self.assertEqual(self.controller("operation", "verify", *op)["goal_status"], "complete")
        with closing(sqlite3.connect(self.root / "agent-session-journal.db")) as db:
            self.assertIsNone(db.execute("SELECT session_id FROM agent_session_records WHERE session_id=?", (sid,)).fetchone())

    @unittest.skipUnless(os.name == "nt", "The executor requires the qualified Orca Codex runtime")
    def test_orca_paired_runtime_cleans_host_and_only_its_controller_tabs(self):
        from tests.test_orca_fullflow import OrcaFullflowTests
        fixture = OrcaFullflowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.client, self.root = "orca", fixture.root
        key = base64.b64encode(bytes(range(32))).decode()
        (self.root / "orca-e2ee-keypair.json").write_text(json.dumps({"publicKeyB64": key, "secretKeyB64": "PRIVATE"}))
        self.transport.side_effect = self.orca_transport
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
        self.controller(*apply, "--orca-controller-only", "environment", "workspace-fixture", ok=False)
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

    def claude_pair(self, *, shared=False):
        self.client = "orca"
        self.transport.side_effect = self.orca_transport
        self.enterContext(patch("local_agent_manager.office_runtime.processes", return_value=()))
        self.enterContext(patch("local_agent_manager.orca_target_safety._windows_process_snapshot", return_value=()))
        sid, native = "orca_claude_selected", "bd75030a-9dfa-4123-84e7-b8eb94dc151f"
        first, last = orca_support.HISTORY_ID, orca_support.CURRENT_ID
        root = self.base / "controller"
        root.mkdir()
        public = base64.b64encode(b"E"*32).decode()
        (self.root / "orca-e2ee-keypair.json").write_text(json.dumps({"publicKeyB64": public}))
        (root / "orca-environments.json").write_text(json.dumps({"version": 1, "environments": [
            {"id": "paired", "endpoints": [{"publicKeyB64": public}]}]}))
        home = self.base / "actual-launch-home"
        row = orca_support.make_record(self.base / "founding-home", sid)
        row["provider"] = "claude"
        row["launchDirectory"] = str(self.base / "repo")
        row["accountHome"]["variable"] = "CLAUDE_CONFIG_DIR"
        row["launchedAccount"] = {"fence": 2, "accountHome": {"variable": "CLAUDE_CONFIG_DIR", "path": str(home)}}
        del row["lease"]["journalCheckpoint"]
        row["acquiredAccount"] = {"version": 1, "hostId": "local", "sessionId": sid, "scope": "local\0\0workspace-fixture",
            "context": None, "fence": 2, "observedThroughFence": 2, "generation": "g", "spawnToken": "PRIVATE",
            "recipe": {"route": "system", "home": str(home), "configDir": str(home)}}
        for i, link in enumerate(row["providerHandleChain"]):
            link.update(handle={"provider": "claude", "sessionId": native, "leafUuid": (first, last)[i]},
                        origin=("created", "resumed")[i])
            link.pop("forkedFromKey", None)
        rows = [row]
        other = "orca_claude_sibling"
        sibling = deepcopy(row)
        sibling["sessionId"] = sibling["lease"]["sessionId"] = other
        sibling.pop("acquiredAccount")
        sibling["providerHandleChain"] = [dict(sibling["providerHandleChain"][0], origin="adopted")]
        if not shared:
            sibling["providerHandleChain"][0]["handle"]["sessionId"] = first
        rows.append(sibling)
        schema = (Path(__file__).parent / "fixtures/orca-domain-v2.sql").read_text(encoding="utf-8")
        for profile, host in ((self.root, "local"), (root, "runtime:paired")):
            with closing(sqlite3.connect(profile / "agent-session-domain.db")) as db:
                db.executescript("BEGIN;\n" + schema)
                db.execute("INSERT INTO agent_session_store_meta VALUES ('session_tabs_recorded','1')")
                for original in rows:
                    value = deepcopy(original)
                    value["location"]["executionHostId"] = host
                    identifier = value["sessionId"]
                    db.execute("INSERT INTO agent_session_records(session_id,record_json) VALUES (?,?)",
                               (identifier, json.dumps(value)))
                    db.execute("INSERT INTO agent_session_tabs VALUES (?,?,0)", ("chat-tab-" + identifier, identifier))
                    db.execute("INSERT INTO journal_chat_state(session_id,workspace_id,epoch) VALUES (?,'workspace-fixture','1')", (identifier,))
                    db.execute("INSERT INTO journal_events VALUES (?,'1',1,1700000000000,'[]')", (identifier,))
                db.commit()
        def tabs(ids):
            return {"unifiedTabs": {"workspace-fixture": [{"id": "chat-tab-"+s, "entityId": s, "contentType": "agent-session",
                "agentSessionAgent": "claude", "worktreeId": "workspace-fixture", "groupId": "g"} for s in ids]}}
        ui = root / "orca-data.json"
        state = {"schemaVersion": 1, "workspaceSessionsByHostId": {"runtime:paired": tabs([sid, other]),
            "runtime:else": tabs([sid]), "local": tabs([sid])}}
        def snapshot(ids):
            rows = [{"type": "agent-session", "id": "chat-tab-" + s, "sessionId": s, "agent": "claude",
                     "title": "PRIVATE", "isActive": i == 0} for i, s in enumerate(ids)]
            return {"workspaces": {"workspace-fixture": {"worktree": "workspace-fixture", "tabs": rows,
                "publicationEpoch": "epoch", "snapshotVersion": 2, "activeGroupId": "g",
                "agentSenderAssociations": {"associations": [], "attachments": [], "principals": [],
                                            "revision": {"epoch": "epoch", "revision": 1}},
                "activeTabId": rows[0]["id"], "activeTabType": "agent-session", "tabGroups": [
                    {"id": "g", "activeTabId": rows[0]["id"], "tabOrder": [r["id"] for r in rows]}]}}}
        state["remoteLayoutSnapshots"] = {"paired": snapshot([sid, other]), "else": snapshot([sid])}
        ui.write_text(json.dumps(state))
        from local_agent_manager import orca_profile_cleanup as profile_db
        profile_path = root / "profiles/default/profile-state.db"
        profile_path.parent.mkdir(parents=True)
        profile_path.with_suffix(".db.authority").write_bytes(b"sqlite\n")
        with closing(sqlite3.connect(profile_path)) as db:
            db.executescript("PRAGMA user_version=3;" + profile_db.DDL + profile_db.PARTS_DDL)
            db.executemany("INSERT INTO profile_state_meta VALUES(?,?)", [("revision", "2"), ("profile_id", "default")])
            db.execute("INSERT INTO profile_state_automation_runs_meta VALUES('automationRuns','document',1,0,0,'')")
            for domain in ("workspaceSessionsByHostId", "remoteLayoutSnapshots"):
                payload = json.dumps(state[domain], separators=(",", ":"))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                db.execute("INSERT INTO profile_state_documents VALUES(?,?,1,2,10,?)", (domain, payload, digest))
                db.execute("INSERT INTO profile_state_document_parts_meta VALUES(?,1,2,10,?,2)", (domain, digest))
                for i, (host, data) in enumerate(state[domain].items()):
                    field, group = next(iter(data.items()))
                    for key, items in group.items():
                        part = json.dumps(items, separators=(",", ":"))
                        path = json.dumps([host, field, key], separators=(",", ":"))
                        db.execute("INSERT INTO profile_state_document_parts VALUES(?,?,?,?,?)",
                                   (domain, path, i * 64, part, hashlib.sha256(part.encode()).hexdigest()))
            db.commit()
        project = home / "projects/repo"
        project.mkdir(parents=True)
        transcript = project / (native + ".jsonl")
        lines = [{"sessionId": native, "uuid": first, "parentUuid": None, "type": "user", "message": "PRIVATE"},
                 {"type": "file-history-snapshot", "messageId": first, "snapshot": {"messageId": first}},
                 {"sessionId": native, "uuid": last, "parentUuid": first, "type": "assistant", "message": "PRIVATE"}]
        lines[0]["cwd"] = row["launchDirectory"]
        lines.append({"type": "last-prompt", "leafUuid": last, "sessionId": native})
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return sid, native, root, transcript, state

    def run_claude_pair(self, *, shared, fork=False, interrupt=False, foreign_controller=False):
        sid, native, root, transcript, state = self.claude_pair(shared=shared)
        original = transcript.read_bytes()
        preserved = {native: "shared_native_history"} if shared else {}
        if fork:
            fork_id = "aaaaaaaa-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            fork_path = transcript.with_name(fork_id + ".jsonl")
            fork_bytes = original.replace(native.encode(), fork_id.encode())
            fork_path.write_bytes(fork_bytes)
            for profile in (self.root, root):
                with closing(sqlite3.connect(profile / "agent-session-domain.db")) as db:
                    raw, = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone()
                    record = json.loads(raw)
                    first, last = record["providerHandleChain"]
                    record["forkedFrom"] = {"sessionId": "orca_claude_sibling", "itemId": "source-item",
                                            "childBoundary": {"kind": "item", "itemId": "child-item"}}
                    last.update(origin="forked", forkedFromKey="claude:" + json.dumps(
                        [native, first["handle"]["leafUuid"]], separators=(",", ":")))
                    last["handle"]["sessionId"] = fork_id
                    db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (json.dumps(record), sid))
                    db.commit()
            owner = self.invoke("records")["owner"]["store_id"]
            blocked = self.invoke("plan", "--owner", owner, "--record-id", "orca_claude_sibling", ok=False)
            self.assertIn("orca_claude_fork_source_still_required", json.dumps(blocked))
            preserved = dict.fromkeys([native, fork_id], "fork_adoption_or_context_change")
        plan, args = self.plan(sid)
        self.assertEqual(plan["deleted_native_ids"], [] if preserved else [native])
        self.assertEqual(plan["preserved_native"], preserved)
        self.assertNotIn("PRIVATE", json.dumps(plan))
        saved = self.base / "owner.json"
        saved.write_text(json.dumps(plan))
        if foreign_controller:
            with closing(sqlite3.connect(root / "agent-session-domain.db")) as db:
                raw, = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone()
                foreign = json.loads(raw)
                foreign["location"]["executionHostId"] = "runtime:else"
                db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (json.dumps(foreign), sid))
                db.commit()
        tabs_only = None
        if not (shared or interrupt):
            tabs_only_root = self.base / "tabs-only"
            tabs_only_root.mkdir()
            for name in ("orca-environments.json", "orca-data.json"):
                (tabs_only_root / name).write_bytes((root / name).read_bytes())
            tabs_only = self.controller("delete", "plan", "--client", "orca", "--orca-root", str(tabs_only_root),
                                        "--record-id", sid, "--orca-remote-plan", str(saved))
            self.assertTrue(tabs_only["capabilities"]["claude"]["frontend_session_delete"])
        local = self.controller("delete", "plan", "--client", "orca", "--orca-root", str(root),
                                "--record-id", sid, "--orca-remote-plan", str(saved))
        self.assertNotIn("PRIVATE", json.dumps(local))
        op = ("--operation-id", local["operation_id"], "--plan", local["plan_path"])
        apply = ("delete", "apply", *op, "--plan-sha256", local["plan_sha256"], "--clients-closed")
        self.assertFalse(self.controller(*apply, ok=False)["mutation_started"])
        self.invoke("apply", *args, "--clients-closed", ok=False)
        self.assertTrue(transcript.exists())
        if interrupt:
            self.interrupt_orca = True
            lost = self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
            self.assertEqual(lost["goal_status"], "unknown")
            self.assertFalse(transcript.exists())
            status = self.invoke("status", *args)["result"]
            self.assertTrue(status["modified"] and status["mutation_started"], status)
            self.assertEqual(status["batches"][0]["status"], "complete")
            self.assertFalse(self.controller(*apply, ok=False)["mutation_started"])
            self.assertEqual(json.loads((root / "orca-data.json").read_bytes()), state)
            with closing(sqlite3.connect(self.root / "agent-session-domain.db")) as db:
                raw, = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone()
                changed = json.loads(raw)
                changed["providerHandleChain"][-1]["handle"]["leafUuid"] = orca_support.HISTORY_ID
                db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (json.dumps(changed), sid))
                db.commit()
                changed_result = self.invoke("apply", *args, "--controller-plan", local["plan_path"], "--clients-closed", ok=False)
                self.assertTrue(changed_result["result"]["modified"])
                self.assertEqual(changed_result["result"]["batches"][0]["status"], "complete")
                db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (raw, sid))
                db.commit()
        self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"])
        self.assertEqual(self.invoke("verify", *args)["goal_status"], "complete")
        self.assertEqual(self.controller(*apply)["goal_status"], "complete")
        self.assertEqual(self.controller("operation", "verify", *op)["goal_status"], "complete")
        if preserved:
            self.assertEqual(transcript.read_bytes(), original)
            if fork:
                self.assertEqual(fork_path.read_bytes(), fork_bytes)
        else:
            self.assertFalse(transcript.exists())
        for profile in (self.root, root):
            with closing(sqlite3.connect(profile / "agent-session-domain.db")) as db:
                expected = ["orca_claude_sibling"]
                if profile == root and foreign_controller:
                    expected.append(sid)
                    self.assertEqual(db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone(),
                                     (json.dumps(foreign),))
                for table in ("agent_session_records", "agent_session_tabs", "journal_events", "journal_chat_state"):
                    self.assertEqual(sorted(r[0] for r in db.execute(f"SELECT session_id FROM {table}")), sorted(expected))
        if tabs_only:
            tab_op = ("--operation-id", tabs_only["operation_id"], "--plan", tabs_only["plan_path"])
            self.assertEqual(self.controller("delete", "apply", *tab_op, "--plan-sha256", tabs_only["plan_sha256"],
                                             "--clients-closed")["goal_status"], "complete")
            self.assertEqual(self.controller("operation", "verify", *tab_op)["goal_status"], "complete")
            self.assertFalse((tabs_only_root / "agent-session-domain.db").exists())
        after = json.loads((root / "orca-data.json").read_bytes())["workspaceSessionsByHostId"]
        for host in ("runtime:else", "local"):
            self.assertEqual(after[host], state["workspaceSessionsByHostId"][host])
        self.assertEqual([t["entityId"] for t in after["runtime:paired"]["unifiedTabs"]["workspace-fixture"]], ["orca_claude_sibling"])
        offline = json.loads((root / "orca-data.json").read_bytes())["remoteLayoutSnapshots"]
        self.assertEqual(offline["else"], state["remoteLayoutSnapshots"]["else"])
        self.assertEqual([t["sessionId"] for t in offline["paired"]["workspaces"]["workspace-fixture"]["tabs"]], ["orca_claude_sibling"])
        profile_path = root / "profiles/default/profile-state.db"
        self.assertEqual(profile_path.with_suffix(".db.authority").read_bytes(), b"sqlite\n")
        with closing(sqlite3.connect(profile_path)) as db:
            for domain, expected in (("workspaceSessionsByHostId", after), ("remoteLayoutSnapshots", offline)):
                payload, digest = db.execute("SELECT payload,content_hash FROM profile_state_documents WHERE domain=?", (domain,)).fetchone()
                self.assertEqual(json.loads(payload), expected)
                self.assertEqual(db.execute("SELECT revision,legacy_revision,content_hash FROM profile_state_document_parts_meta WHERE domain=?",
                                            (domain,)).fetchone(), (3, 3, digest))
                for key, part in db.execute("SELECT part_key,payload FROM profile_state_document_parts WHERE domain=?", (domain,)):
                    host, field, worktree = json.loads(key)
                    self.assertEqual(json.loads(part), expected[host][field][worktree])

    @unittest.skipUnless(os.name == "nt", "Orca closed-process verification requires Windows")
    def test_orca_claude_paired_exclusive_linear_cursors(self):
        self.run_claude_pair(shared=False)

    @unittest.skipUnless(os.name == "nt", "Orca closed-process verification requires Windows")
    def test_orca_claude_paired_shared_transcript(self):
        self.run_claude_pair(shared=True, foreign_controller=True)

    @unittest.skipUnless(os.name == "nt", "Orca closed-process verification requires Windows")
    def test_orca_claude_fork_preserves_both_native_histories(self):
        self.run_claude_pair(shared=True, fork=True)

    @unittest.skipUnless(os.name == "nt", "Orca closed-process verification requires Windows")
    def test_orca_claude_interrupted_between_native_and_frontend(self):
        self.run_claude_pair(shared=False, interrupt=True)

    @unittest.skipUnless(os.name == "nt", "Orca closed-process verification requires Windows")
    def test_orca_claude_controller_restore_sources_guard_the_executor(self):
        sid, native, root, transcript, _ = self.claude_pair()
        plan, args = self.plan(sid)
        saved = self.base / "owner.json"
        saved.write_text(json.dumps(plan))
        select = ("delete", "plan", "--client", "orca", "--orca-root", str(root),
                  "--record-id", sid, "--orca-remote-plan", str(saved))
        local = self.controller(*select)
        apply = ("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"])
        with closing(sqlite3.connect(root / "agent-session-domain.db")) as db:
            other = "orca_claude_sibling"
            raw, = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (other,)).fetchone()
            sibling = json.loads(raw)
            sibling["providerHandleChain"][0]["handle"]["sessionId"] = native
            db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (json.dumps(sibling), other))
            db.commit()
            self.invoke(*apply, ok=False)
            db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (raw, other))
            original = json.dumps({"version": 1, "record": json.dumps(sibling), "queued": []})
            key = "journal.legacy-original:" + other + ":digest"
            db.execute("INSERT INTO agent_session_store_meta VALUES(?,?)", (key, original))
            db.commit()
            self.controller(*select, ok=False)
            self.invoke(*apply, ok=False)
            db.execute("DELETE FROM agent_session_store_meta WHERE key=?", (key,))
            db.commit()
        with closing(sqlite3.connect(root / "profiles/default/profile-state.db")) as db:
            db.execute("UPDATE profile_state_document_parts_meta SET legacy_revision=1 WHERE domain='remoteLayoutSnapshots'")
            db.commit()
            self.controller(*select, ok=False)
            self.invoke(*apply, ok=False)
        self.assertTrue(transcript.is_file())
        self.assertFalse(self.invoke("status", *args)["result"]["mutation_started"])

    @unittest.skipUnless(os.name == "nt", "Orca closed-process verification requires Windows")
    def test_orca_claude_requires_matching_cursors_and_exclusive_transcript(self):
        sid, native, controller, transcript, _ = self.claude_pair()
        plan, args = self.plan(sid)
        saved = self.base / "owner.json"
        saved.write_text(json.dumps(plan))
        select = ("delete", "plan", "--client", "orca", "--orca-root", str(controller),
                  "--record-id", sid, "--orca-remote-plan", str(saved))
        local = self.controller(*select)
        with closing(sqlite3.connect(controller / "agent-session-domain.db")) as db:
            raw, = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone()
            changed = json.loads(raw)
            changed["providerHandleChain"][-1]["handle"]["leafUuid"] = orca_support.HISTORY_ID
            db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (json.dumps(changed), sid))
            db.commit()
            self.controller(*select, ok=False)
            self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
            self.assertTrue(transcript.is_file())
            db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (raw, sid))
            db.commit()
        original = transcript.read_bytes()
        branch = {"sessionId": native, "type": "assistant", "uuid": "side", "parentUuid": orca_support.HISTORY_ID}
        transcript.write_bytes(original + json.dumps(branch).encode() + b"\n")
        self.invoke("apply", *args, "--clients-closed", "--controller-plan", local["plan_path"], ok=False)
        self.assertEqual(self.plan(sid)[0]["preserved_native"], {native: "transcript_or_cursor_not_exclusive"})
        transcript.write_bytes(original)
        with closing(sqlite3.connect(self.root / "agent-session-domain.db")) as db:
            raw, = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone()
            row = json.loads(raw)
            row["acquiredAccount"]["observedThroughFence"] = 1
            db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (json.dumps(row), sid))
            db.commit()
            self.assertEqual(self.plan(sid)[0]["preserved_native"], {native: "launch_account_unproven"})
            row["acquiredAccount"].update(observedThroughFence=2, hostId="runtime:else")
            db.execute("UPDATE agent_session_records SET record_json=? WHERE session_id=?", (json.dumps(row), sid))
            db.commit()
            self.assertEqual(self.plan(sid)[0]["preserved_native"], {native: "launch_account_unproven"})
            db.execute("INSERT INTO agent_session_store_meta VALUES(?,?)", ("journal.legacy-original:" + sid + ":digest", raw))
            db.commit()
            owner = self.invoke("records")["owner"]["store_id"]
            self.invoke("plan", "--owner", owner, "--record-id", sid, ok=False)
        self.assertEqual(transcript.read_bytes(), original)

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
