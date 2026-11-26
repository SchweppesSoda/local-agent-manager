"""Exercise daemon and provider scopes through the public CLI and temporary stores."""
from contextlib import closing, nullcontext
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import bootstrap, test_paseo_fullflow as paseo
from tests.t3code_support import V2, add_thread, create_store, insert
from local_agent_manager import remote_records
from local_agent_manager.cli import main
from local_agent_manager.t3code_store import SCHEMA_CONTRACTS


class DaemonProviderTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.base / "provider"
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {key: str(self.base / key) for key in
            ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
             "PASEO_HOME", "T3CODE_HOME", "PI_CODING_AGENT_DIR")}))
        self.enterContext(patch("local_agent_manager.office_runtime.processes", return_value=[]))
        self.policy = self.enterContext(patch("local_agent_manager.t3code_provider_settings.has_provider_policy",
                                              return_value=False))
        self.ssh = self.enterContext(patch.object(remote_records, "_ssh", side_effect=self.worker))
        self.destination = ["--local"]

    @staticmethod
    def worker(host, payload):
        stream = io.BytesIO()
        code = remote_records.worker(io.BytesIO(payload), stream)
        return code, stream.getvalue()

    def call(self, *args, ok=True):
        out = io.StringIO()
        code = main(list(map(str, args)), stdout=out, stderr=io.StringIO())
        value = json.loads(out.getvalue())
        self.assertEqual(code == 0, ok, value)
        return value

    def remote(self, verb, *extra, ok=True):
        return self.call("remote", verb, *self.destination, "--client", self.client,
                         "--root", self.root, *extra, ok=ok)

    def fixture(self, client, engine="claude"):
        self.client, self.root = client, self.base / client
        self.root.mkdir()
        binary = self.root / "synthetic-desktop.exe"
        binary.write_bytes(b"runtime fence fixture")
        pairs = [(paseo.AGENT, paseo.NATIVE), (paseo.KEEP, paseo.OTHER)]
        files = {}
        for _, native in pairs:
            if engine == "pi":
                path = self.home / "sessions/project" / (native + ".jsonl")
                record = {"type": "session", "id": native, "version": 3, "cwd": str(self.base),
                          "timestamp": "2026-11-25T00:00:00Z"}
            elif engine == "claude":
                path = self.home / "projects/project" / (native + ".jsonl")
                record = {"sessionId": native, "type": "user", "message": "PRIVATE"}
            else:
                from tests.support import write_rollout
                files[native] = write_rollout(self.home, native, originator="codex")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record) + "\n")
            files[native] = path
        if engine == "codex":
            from tests.support import create_thread_index
            create_thread_index(self.home, [{"id": key, "rollout_path": str(path)} for key, path in files.items()])
        if client == "t3code":
            from local_agent_manager.t3code_bound_adapter import SCHEMA
            key = "thread_id"
            identity = {"environment_id": "env-owner"}
            driver = "claudeAgent" if engine == "claude" else engine
            with closing(create_store(self.root, objects=SCHEMA_CONTRACTS[-1])) as db:
                for sid, native in pairs:
                    add_thread(db, sid, native=str(files[native]) if engine == "pi" else native, driver=driver)
            (self.root / "userdata/environment-id").write_text(identity["environment_id"])
            (self.root / "userdata/server-owner.sqlite").touch()
        else:
            from local_agent_manager.paseo_bound_adapter import SCHEMA
            key = "agent_id"
            identity = {"server_id": "srv_owner"}
            (self.root / "server-id").write_text(identity["server_id"])
            for sid, native in pairs:
                row = paseo.agent(sid, native, self.base)
                for item in (row, row["persistence"], row["runtimeInfo"]):
                    item["provider"] = engine
                if engine == "pi":
                    row["persistence"]["nativeHandle"] = str(files[native])
                paseo.write(self.root / "agents" / (sid + ".json"), row)
        store = {"root": str(self.home)}
        if engine == "pi":
            store = {"agent_dir": str(self.home), "root": str(self.home / "sessions")}
        self.manifest = {"schema_version": SCHEMA, "profile_root": str(self.root), **identity,
                         "runtime_binaries": [str(binary)], "desktop_profiles": [],
                         "native_stores": [{key: sid, "engine": engine, **store} for sid, _ in pairs]}
        self.bindings = self.base / "bindings.json"
        self.bindings.write_text(json.dumps(self.manifest))
        return files

    def plan(self, *extra, automatic=False, ok=True):
        owner = self.remote("records")["owner"]["store_id"]
        return self.remote("plan", "--owner", owner, "--record-id", paseo.AGENT, "--provider-only",
                           *([] if automatic else ["--bindings", self.bindings]), *extra, ok=ok)

    @staticmethod
    def operation(plan):
        return ["--operation-id", plan["operation_id"], "--approval", plan["approval"], "--owner", plan["owner"]["store_id"]]

    def exercise(self, files, engine):
        original = self.remote("records")["records"]
        plan = self.plan()
        self.assertEqual(plan["completion_scope"], "bound_provider_only")
        self.assertEqual(plan["daemon_record_id"], paseo.AGENT)
        self.assertEqual(plan["native_ids"], [paseo.NATIVE])
        self.assertEqual([a["kind"] for a in plan["actions"]], ["delete_" + engine + "_session"])
        self.assertNotIn("PRIVATE", json.dumps(plan))
        args = self.operation(plan)
        self.assertEqual(self.remote("verify", *args, ok=False)["goal_status"], "unknown")
        self.remote("apply", *args, ok=False)
        self.call("delete", "apply", "--operation-id", plan["operation_id"], "--plan", plan["plan_path_on_owner"],
                  "--plan-sha256", plan["plan_sha256"], "--clients-closed", "--json", ok=False)
        self.assertTrue(files[paseo.NATIVE].is_file())
        self.assertEqual(self.remote("apply", *args, "--clients-closed")["goal_status"], "complete")
        self.assertFalse(files[paseo.NATIVE].exists())
        self.assertTrue(files[paseo.OTHER].is_file())
        self.assertEqual(self.remote("records")["records"], original)
        for verb, extra in (("status", []), ("verify", []), ("apply", ["--clients-closed"])):
            result = self.remote(verb, *args, *extra)
            self.assertEqual(result["goal_status"], "complete")
            self.assertTrue(result["result"]["mutation_started"])
        files[paseo.NATIVE].write_bytes(files[paseo.OTHER].read_bytes().replace(
            paseo.OTHER.encode(), paseo.NATIVE.encode()))
        self.assertEqual(self.remote("verify", *args, ok=False)["goal_status"], "completed_with_residuals")

    @unittest.skipUnless(os.name == "nt", "Windows lifecycle fence")
    def test_t3_claude_only_over_ssh(self):
        self.destination = ["--ssh-host", "test-owner"]
        files = self.fixture("t3code")
        peer = self.base / "peer"
        with closing(create_store(peer, objects=SCHEMA_CONTRACTS[-1])) as db:
            add_thread(db, paseo.AGENT, native=paseo.NATIVE, driver="claudeAgent")
        db_path = peer / "userdata/statev2.sqlite"
        before = db_path.read_bytes()
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            insert(db, "peer_environment_links", environment_id="env-remote", secret_name="PRIVATE",
                   urls_json='["https://PRIVATE"]', label="PRIVATE", access="full-access")
            child = {"environmentId": "env-remote", "threadId": paseo.AGENT, "label": "PRIVATE"}
            payload = {"id": "remote-task", "driver": "remote", "remoteChild": child, "childThreadId": None}
            insert(db, V2 + "subagents", subagent_id="remote-task", thread_id=paseo.AGENT, provider="remote",
                   payload_json=json.dumps(payload))
            events = (("subagent.updated", payload), ("turn-item.updated", {**payload, "type": "subagent"}),
                    ("thread.created", {"linkOrigin": {"sessionId": paseo.NATIVE, "label": "PRIVATE"},
                     "delegatedFrom": {"environmentId": "env-remote", "threadId": paseo.AGENT, "title": "PRIVATE"}}))
            for version, (event, fields) in enumerate(events, start=1):
                insert(db, "orchestration_events", stream_id=paseo.AGENT, event_id=event, event_type=event,
                       stream_version=version, aggregate_kind="thread", payload_json=json.dumps(fields), metadata_json="{}")
            db.commit()
        self.exercise(files, "claude")
        self.assertEqual(db_path.read_bytes(), before)
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            self.assertEqual(db.execute("SELECT environment_id FROM peer_environment_links").fetchone(), ("env-remote",))

    def test_unpairing_does_not_expand_joint_cleanup_to_delegated_threads(self):
        self.fixture("t3code")
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"""UPDATE {V2}threads SET payload_json=json_set(payload_json,'$.delegatedFrom',json(?))
                WHERE thread_id=?""", (json.dumps({"environmentId": "other", "threadId": paseo.KEEP,
                "title": "PRIVATE"}), paseo.AGENT))
            db.commit()
        owner = self.remote("records")["owner"]["store_id"]
        result = self.remote("plan", "--owner", owner, "--record-id", paseo.AGENT, "--bindings", self.bindings, ok=False)
        self.assertIn("peer_thread_closure_unqualified", json.dumps(result))
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}threads SET payload_json=json_remove(payload_json,'$.delegatedFrom') WHERE thread_id=?",
                       (paseo.AGENT,))
            insert(db, "orchestration_v2_events", thread_id=paseo.AGENT, event_id="remote-task-history",
                   event_type="subagent.updated", payload_json=json.dumps({"remoteChild": {
                       "environmentId": "other", "threadId": paseo.KEEP}}))
            db.commit()
        result = self.remote("plan", "--owner", owner, "--record-id", paseo.AGENT, "--bindings", self.bindings, ok=False)
        self.assertIn("peer_thread_closure_unqualified", json.dumps(result))

    @unittest.skipUnless(os.name == "nt", "Windows lifecycle fence")
    def test_paseo_pi_only_with_offline_desktop(self):
        self.exercise(self.fixture("paseo", "pi"), "pi")
        self.ssh.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows lifecycle fence")
    def test_apply_rechecks_unselected_agent_and_owner(self):
        files = self.fixture("paseo")
        plan = self.plan()
        path = self.root / "agents" / (paseo.KEEP + ".json")
        original = path.read_bytes()
        other = json.loads(original)
        other["persistence"] = {"provider": "custom", "sessionId": '{"resume":"PRIVATE"}'}
        paseo.write(path, other)
        self.assertIn("handle_unverified", self.plan(ok=False)["error"])
        path.write_bytes(original.replace(paseo.OTHER.encode(), paseo.NATIVE.encode()))
        self.assertIn("shared", self.plan(ok=False)["error"])
        args = self.operation(plan)
        self.assertIn("shared", self.remote("apply", *args, "--clients-closed", ok=False)["error"])
        (self.root / "server-id").write_text("srv_other")
        self.assertIn("owner_changed", self.remote("apply", *args, "--clients-closed", ok=False)["error"])
        self.assertTrue(files[paseo.NATIVE].is_file())

    @unittest.skipUnless(os.name == "nt", "Windows lifecycle fence")
    def test_automatic_paseo_binding_and_cold_receipt_after_lost_reply(self):
        self.destination = ["--ssh-host", "worker"]
        files = self.fixture("paseo")
        (self.root / "Paseo.exe").write_bytes(b"fixture")
        self.enterContext(patch("local_agent_manager.paseo_daemon_delete._bundled_install", return_value=self.root))
        settings = self.root / "config.json"
        paseo.write(settings, {"version": 1, "agents": {"providers": {"claude": {"env": {
            "CLAUDE_CONFIG_DIR": str(self.home), "ANTHROPIC_BASE_URL": "https://PRIVATE"}}}}})
        (Path(os.environ["APPDATA"]) / "Paseo/IndexedDB/paseo_app_0.indexeddb.leveldb").mkdir(parents=True)
        plan = self.plan(automatic=True)
        self.assertNotIn("PRIVATE", json.dumps(plan))
        self.assertEqual(Path(plan["provider_owner"]["root"]), self.home)
        args = self.operation(plan)
        settings.write_bytes(settings.read_bytes() + b"\n")
        self.assertIn("binding_changed", self.remote("apply", *args, "--clients-closed", ok=False)["error"])
        self.assertTrue(files[paseo.NATIVE].is_file())
        plan = self.plan(automatic=True)
        args = self.operation(plan)
        def dropped(host, payload):
            code, reply = self.worker(host, payload)
            self.assertEqual(code, 0, reply)
            return 255, b""
        self.ssh.side_effect = dropped
        self.assertEqual(self.remote("apply", *args, "--clients-closed", ok=False)["goal_status"], "unknown")
        self.ssh.side_effect = self.worker
        self.assertFalse(files[paseo.NATIVE].exists())
        self.assertTrue(files[paseo.OTHER].exists())
        # The approved provider receipt can be checked without the daemon.
        self.root.rename(self.base / "parked-daemon")
        for verb in ("status", "verify"):
            result = self.remote(verb, *args)
            self.assertTrue(result["result"]["mutation_started"])
            self.assertEqual(result["goal_status"], "complete")
            self.assertEqual(result["daemon_record_action"], "none")
        self.assertIn("approval", self.remote("status", *self.operation({**plan, "approval": "f" * 64}),
                                             ok=False)["error"])
        self.home.rename(self.base / "parked-provider")
        self.home.mkdir()
        unverified = self.remote("verify", *args, ok=False)
        self.assertEqual(unverified["goal_status"], "unknown")
        self.assertTrue(unverified["result"]["mutation_started"])
        self.assertTrue(unverified["result"]["batches"])

    @unittest.skipUnless(os.name == "nt", "Windows lifecycle fence")
    def test_t3_builtin_instance_uses_native_evidence_without_settings_entry(self):
        self.home = Path(os.environ["USERPROFILE"]) / ".claude"
        self.home.mkdir(parents=True)
        self.fixture("t3code")
        binary = self.root / "T3 Code (Nightly).exe"
        binary.write_bytes(b"fixture")
        self.enterContext(patch("local_agent_manager.t3code_server_delete._bundled_install", return_value=self.root))
        plan = self.plan(automatic=True)
        self.assertEqual(Path(plan["provider_owner"]["root"]), self.home)
        self.policy.return_value = True
        self.assertIn("managed_provider", self.remote("apply", *self.operation(plan), "--clients-closed", ok=False)["error"])
        self.assertIn("managed_provider", self.plan(automatic=True, ok=False)["error"])
        self.assertEqual(self.plan()["native_ids"], [paseo.NATIVE])
        self.policy.return_value = False
        settings = self.root / "userdata/settings.json"
        settings.write_text(json.dumps({"providers": {"claudeAgent": {"homePath": str(self.base / "wrong")}}}))
        self.assertNotEqual(self.plan(automatic=True, ok=False)["goal_status"], "ready")
        settings.write_text(json.dumps({"providers": {"claudeAgent": {"homePath": str(self.base / "wrong")}},
                                        "providerInstances": {"claudeAgent": {"driver": "claudeAgent",
                                                            "config": {"homePath": str(self.home)}}}}))
        self.assertEqual(Path(self.plan(automatic=True)["provider_owner"]["root"]), self.home)
        settings.write_text('{"providerInstances":{"claudeAgent":null}}')
        self.assertIn("provider_instance_unverified", self.plan(automatic=True, ok=False)["error"])

    @unittest.skipUnless(os.name == "nt", "Windows lifecycle fence")
    def test_bound_codex_rejects_cascade_and_changed_runtime(self):
        self.destination = ["--ssh-host", "owner"]
        # Paseo may select the same physical home as Cindy with a different BIN.
        self.home = Path.home() / ".xdt-server/v1/codex-home"
        self.home.mkdir(parents=True)
        files = self.fixture("paseo", "codex")
        binary = self.base / "codex.exe"
        binary.write_bytes(b"approved Codex")
        self.assertIn("binary_required", self.plan(ok=False)["error"])
        plan = self.plan("--codex-bin", binary)
        self.assertNotIn("cindy_ssh_provider", plan["provider_owner"])
        args = self.operation(plan)
        binary.write_bytes(b"different Codex")
        self.assertIn("runtime_changed", self.remote("apply", *args, "--clients-closed", ok=False)["error"])
        db_path = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (paseo.NATIVE, paseo.OTHER, "closed"))
            db.commit()
        refused = self.plan("--codex-bin", binary, ok=False)
        self.assertIn("exceeds_reference", refused["error"])
        self.assertTrue(all(path.is_file() for path in files.values()))
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("DELETE FROM thread_spawn_edges")
            db.commit()
        plan = self.plan("--codex-bin", binary)
        args = self.operation(plan)
        def delete(identifier):
            self.assertEqual(identifier, paseo.NATIVE)
            with closing(sqlite3.connect(db_path)) as db:
                db.execute("DELETE FROM threads WHERE id=?", (identifier,))
                db.commit()
            files[identifier].unlink()
        def server(*, codex_home, codex_binary, **_):
            self.assertEqual((Path(codex_home), Path(codex_binary)), (self.home, binary))
            return nullcontext(SimpleNamespace(delete_thread=delete))
        self.enterContext(patch("local_agent_manager.cli.main",
            side_effect=lambda argv, **kw: main(argv, app_server_factory=server, **kw)))
        self.assertEqual(self.remote("apply", *args, "--clients-closed")["goal_status"], "complete")
        self.assertEqual(self.remote("verify", *args)["native_ids"], [paseo.NATIVE])
        self.assertTrue(files[paseo.OTHER].exists())

    def test_workflow_subject_in_119_protects_thread_and_unrelated_row_survives(self):
        from local_agent_manager import t3code_database as database
        self.fixture("t3code")
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            insert(db, "workflow_runs", run_id="wf-test", subject_kind="thread", subject_key="elsewhere",
                   subject_json=json.dumps({"threadId": paseo.AGENT}))
            db.commit()
            with self.assertRaisesRegex(ValueError, "workflow_or_feedback_reference"):
                database.freeze(self.root, [paseo.AGENT])
            db.execute("UPDATE workflow_runs SET subject_json=NULL, subject_key=?", (paseo.AGENT,))
            db.commit()
            with self.assertRaisesRegex(ValueError, "workflow_or_feedback_reference"):
                database.freeze(self.root, [paseo.AGENT])
            db.execute("UPDATE workflow_runs SET subject_key=?", (paseo.KEEP,))
            db.commit()
            evidence = database.freeze(self.root, [paseo.AGENT])
            database.apply(evidence, phase_callback=lambda _: None)
            self.assertEqual(db.execute("SELECT subject_key FROM workflow_runs").fetchone(), (paseo.KEEP,))

    def test_t3_peer_and_external_provider_are_separate(self):
        self.fixture("t3code")
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"""UPDATE {V2}threads SET default_provider='custom',provider_instance_id='custom',
                payload_json=json_set(payload_json,'$.delegatedFrom',json(?),'$.linkOrigin',json(?)) WHERE thread_id=?""",
                (json.dumps({"environmentId": "env-other", "threadId": paseo.KEEP, "title": "PRIVATE"}),
                 json.dumps({"sessionId": "PRIVATE", "label": "PRIVATE"}), paseo.AGENT))
            insert(db, V2 + "subagents", subagent_id="task-7", thread_id=paseo.AGENT, payload_json=json.dumps({
                "remoteChild": {"environmentId": "env-third", "threadId": paseo.KEEP, "label": "PRIVATE"}}))
            db.commit()
        (self.root / "userdata/settings.json").write_text(json.dumps({"providerInstances": {
            "custom": {"driver": "opencode", "config": {"serverUrl": "http://localhost:1234/?PRIVATE",
                                                        "serverPassword": "PRIVATE"}}}}))
        row = next(r for r in self.remote("records")["records"] if r["record_id"] == paseo.AGENT)
        self.assertEqual(row["delegated_from"], {"environment_id": "env-other", "thread_id": paseo.KEEP})
        self.assertTrue(row["peer_link_origin_present"])
        self.assertEqual(row["remote_children"], [{"task_id": "task-7", "environment_id": "env-third", "thread_id": paseo.KEEP}])
        self.assertEqual(row["opencode_configured_route"], "configured_server")
        self.assertNotIn("PRIVATE", json.dumps(row))
        self.assertEqual(self.plan(ok=False)["error"], "external_opencode_native_store_unverified")

    def test_t3_legacy_opencode_and_machine_policy_sources(self):
        from local_agent_manager import daemon_records, t3code_provider_settings
        root = self.base / "t3code"
        (root / "userdata").mkdir(parents=True)
        path = root / "userdata/settings.json"
        path.write_text(json.dumps({"providers": {"opencode": {"enabled": True, "serverUrl": "http://PRIVATE"}}}))
        def route():
            settings = daemon_records._provider_settings("t3code", root)
            return daemon_records._opencode_route("t3code", settings, "opencode", "opencode")
        self.assertEqual(route(), "configured_server")
        path.write_text(json.dumps({"providers": {"opencode": {"serverUrl": "http://PRIVATE"}},
                                    "providerInstances": {"opencode": {"driver": "opencode", "config": {"serverUrl": None}}}}))
        self.assertEqual(route(), "spawn_on_environment")
        policy = self.base / "policy.json"
        policy.write_text('{"settings":{"theme":{"locked":true,"value":"dark"}}}')
        self.assertFalse(t3code_provider_settings._policy_file(policy))
        policy.write_text('{"limits":{"providers":{"instances":{"opencode":{"locked":true,"binaryPath":"PRIVATE"}}}}}')
        self.policy.return_value = t3code_provider_settings._policy_file(policy)
        self.assertEqual(route(), "unverified")
        self.assertTrue(t3code_provider_settings._provider_policy({"settings": {"providerInstances": {}}}))
        self.assertTrue(t3code_provider_settings._provider_policy({"limits": {"maxRuntimeMode": "full-access"}}))

    def test_paseo_hub_parent_and_external_service_are_separate(self):
        self.fixture("paseo")
        path = self.root / "agents" / (paseo.AGENT + ".json")
        agent = json.loads(path.read_bytes())
        agent.update(provider="opencode", owner={"kind": "daemon", "daemonId": "hub-daemon", "executionId": "run-1"},
                     labels={"paseo.parent-agent-id": paseo.KEEP, "paseo.parent-server-id": "srv_parent"},
                     launchEnv={"PASEO_OPENCODE_SERVER_URL": "http://per-agent-PRIVATE"})
        for item in (agent["persistence"], agent["runtimeInfo"]):
            item["provider"] = "opencode"
        paseo.write(path, agent)
        def selected():
            return next(r for r in self.remote("records")["records"] if r["record_id"] == paseo.AGENT)
        row = selected()
        self.assertEqual(row["hub_execution"], agent["owner"])
        self.assertEqual(row["parent_agent"], {"agent_id": paseo.KEEP, "server_id": "srv_parent"})
        self.assertEqual(row["opencode_configured_route"], "daemon_runtime_selection")
        paseo.write(self.root / "config.json", {"version": 1, "agents": {"providers": {"opencode": {
            "env": {"PASEO_OPENCODE_SERVER_URL": "https://PRIVATE", "PASEO_OPENCODE_SERVER_PASSWORD": "PRIVATE"}}}}})
        self.assertEqual(selected()["opencode_configured_route"], "configured_server")
        self.assertNotIn("PRIVATE", json.dumps(selected()))
        self.assertEqual(self.plan(ok=False)["error"], "external_opencode_native_store_unverified")
        agent["provider"] = "team-opencode"
        for item in (agent["persistence"], agent["runtimeInfo"]):
            item["provider"] = "team-opencode"
        paseo.write(path, agent)
        config = {"version": 1, "agents": {"providers": {"opencode": {"env": {"PASEO_OPENCODE_SERVER_URL": "https://PRIVATE"}},
                  "team-opencode": {"extends": "opencode", "label": "Team"}}}}
        paseo.write(self.root / "config.json", config)
        self.assertEqual(selected()["opencode_configured_route"], "configured_server")
        config["agents"]["providers"]["team-opencode"]["env"] = {"PASEO_OPENCODE_SERVER_URL": ""}
        paseo.write(self.root / "config.json", config)
        self.assertEqual(selected()["opencode_configured_route"], "daemon_runtime_selection")

    def test_explicit_local_destination_cannot_be_replayed_over_ssh(self):
        self.fixture("paseo")
        request = {"protocol": remote_records.PROTOCOL, "verb": "records", "destination": remote_records.LOCAL_OWNER,
                   "client": self.client, "root": str(self.root)}
        code, reply = self.worker("worker", json.dumps(request).encode())
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(reply)["error"], "remote_local_owner_transport_mismatch")

    def test_external_opencode_keeps_product_delete_available(self):
        from tests.test_paseo_daemon_delete import PaseoDaemonDeleteTests
        from tests.test_t3code_server_delete import T3ServerDeleteTests, THREAD
        for client, product, sid in (("t3code", T3ServerDeleteTests(), THREAD),
                                     ("paseo", PaseoDaemonDeleteTests(), paseo.AGENT)):
            with self.subTest(client=client):
                product.setUp()
                self.addCleanup(product.doCleanups)
                self.client = client
                if client == "paseo":
                    self.root = product.profile
                    row = json.loads(product.target.read_bytes())
                    for item in (row, row["persistence"], row["runtimeInfo"]):
                        item["provider"] = "opencode"
                    paseo.write(product.target, row)
                    paseo.write(self.root / "config.json", {"version": 1, "agents": {"providers": {"opencode": {
                        "env": {"PASEO_OPENCODE_SERVER_URL": "http://127.0.0.1:4096"}}}}})
                else:
                    self.root = product.root
                    (self.root / "userdata/settings.json").write_text(json.dumps({"providerInstances": {
                        "opencode": {"driver": "opencode", "config": {"serverUrl": "https://PRIVATE"}}}}))
                    with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
                        db.execute(f"UPDATE {V2}threads SET default_provider='opencode',provider_instance_id='opencode' WHERE thread_id=?", (sid,))
                        db.commit()
                owner = self.remote("records")["owner"]["store_id"]
                args = ["--owner", owner, "--record-id", sid]
                self.assertEqual(self.remote("plan", *args, ok=False)["error"], "external_opencode_native_store_unverified")
                plan = self.remote("plan", *args, "--frontend-only")
                self.assertEqual(plan["provider_history"], "outside_scope")
                self.assertEqual(self.remote("apply", *self.operation(plan))["goal_status"], "complete")
                self.assertEqual(self.remote("verify", *self.operation(plan))["frontend_ids"], [sid])
                self.assertTrue(product.native.exists())
