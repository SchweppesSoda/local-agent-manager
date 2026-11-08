from io import StringIO
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_agent_record_janitor.cli import main
from local_agent_record_janitor import t3code_store as store, t3code_database as database
from tests.t3code_support import create_store, add_thread, insert, PRIVATE, V2


class T3CodeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / ".t3"
        self.db = create_store(self.home)
        self.addCleanup(self.db.close)
        environment = {"T3CODE_HOME": str(self.home), "APPDATA": str(self.root / "appdata"),
            "ORCA_USER_DATA_PATH": "", "HERDR_CONFIG_DIR": str(self.root / "absent-herdr"),
            "CODEX_HOME": str(self.root / "absent-codex"), "PASEO_HOME": str(self.root / "absent-paseo")}
        for p in (patch("pathlib.Path.home", return_value=self.root), patch.dict(os.environ, environment)):
            p.start()
            self.addCleanup(p.stop)
        self.writer = Mock(side_effect=AssertionError("Unexpected native writer"))

    def invoke(self, *args):
        out, err = StringIO(), StringIO()
        code = main(list(args), stdout=out, stderr=err, app_server_factory=self.writer, binary_resolver=self.writer)
        self.assertNotIn(PRIVATE, out.getvalue() + err.getvalue())
        return code, json.loads(out.getvalue())

    def test_inventory_v1_v2_provider_identity_and_private_fields(self):
        add_thread(self.db, "codex-thread", native="native-1")
        add_thread(self.db, "claude-thread", native="native-2", driver="claudeAgent")
        add_thread(self.db, "legacy", legacy=True)
        code, value = self.invoke("records", "--client", "t3code", "--inspect-clients", "--json")
        self.assertEqual(code, 0, value)
        self.assertEqual(value["inventory_scope"], "database_metadata")
        self.assertEqual(value["count"], 3)
        self.assertEqual({t["engine"] for t in value["targets"]}, {"codex", "claude", "unknown"})
        references = [r for t in value["targets"] for r in t["references"]]
        self.assertEqual({r["native_id"] for r in references}, {"native-1", "native-2"})
        self.assertTrue(all(r.get("native_record") is None for r in references))
        code, value = self.invoke("records", "--client", "t3code", "--engine", "claude", "--json")
        self.assertEqual(code, 0, value)
        self.assertEqual(value["count"], 1)
        self.writer.assert_not_called()

    def test_unknown_schema_missing_and_empty_are_distinct(self):
        code, value = self.invoke("records", "--client", "t3code", "--json")
        self.assertEqual(code, 0, value)
        self.assertEqual(value["count"], 0)
        code, value = self.invoke("records", "--client", "t3code", "--t3code-root", str(self.root / "missing"), "--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(value["goal_status"], "blocked")
        self.db.execute("CREATE TABLE future_history(body TEXT)")
        self.db.commit()
        code, value = self.invoke("records", "--client", "t3code", "--json")
        self.assertNotEqual(code, 0)
        self.assertIn("t3code_schema_unverified", {e["message"] for e in value["errors"]})

    def test_antigravity_plan_run_and_forged_bindings_preserve_mixed_profile(self):
        add_thread(self.db, "selected", native="selected-native")
        add_thread(self.db, "keeper", native="shared-id")
        insert(self.db, V2 + "provider_threads", provider_thread_id="antigravity-no-handle",
            thread_id="selected", provider="antigravity", driver="antigravity",
            provider_instance_id="antigravity", status="closed", payload_json="{}")
        self.db.commit()
        code, inventory = self.invoke("records", "--client", "t3code", "--json")
        self.assertEqual(code, 0, inventory)
        selected = {row["engine"]: row for row in inventory["targets"] if row["record_id"] == "selected"}
        self.assertEqual(set(selected), {"unsupported:antigravity", "codex"})
        antigravity = selected["unsupported:antigravity"]
        self.assertEqual(antigravity["record_metadata"]["provider_backend_family_hint"], "antigravity-acp")
        self.assertEqual(antigravity["record_metadata"]["native_cleanup_status"], "requires_t3code_binding")
        self.assertNotIn("provider_backend_family_hint", selected["codex"]["record_metadata"])
        self.assertFalse(antigravity["record_metadata"]["coverage"]["native_root_verified"])
        self.assertFalse(antigravity["record_metadata"]["coverage"]["full_record_closure"])
        self.assertEqual(antigravity["capability"]["mode"], "inventory_only")
        self.assertFalse(antigravity["capability"]["native_delete"])
        self.assertFalse(antigravity["capability"]["frontend_session_delete"])
        self.assertFalse(antigravity["capability"]["verify"])
        self.assertTrue(all(ref.get("native_record") is None for ref in antigravity["references"]))

        native_root = self.home / "userdata" / "providers" / "antigravity" / "fixture"
        native_root.mkdir(parents=True)
        native = native_root / "session-data.bin"
        native.write_text(PRIVATE, encoding="utf-8")
        environment_id = self.home / "userdata" / "environment-id"
        environment_id.write_text("env_fixture", encoding="utf-8")
        runtime = self.root / "synthetic-t3.exe"
        runtime.touch()
        before = {path: path.read_bytes() for path in (self.home / store.RELATIVE, native, environment_id)}
        base = ("--client", "t3code", "--t3code-root", str(self.home), "--record-id", "selected", "--json")

        with patch("local_agent_record_janitor.cleanup_service.CleanupService.execute",
                   side_effect=AssertionError("Unexpected cleanup writer")) as execute:
            for verb in ("plan", "run"):
                options = ("--clients-closed",) if verb == "run" else ()
                code, result = self.invoke("delete", verb, *base, *options, "--engine", "unsupported:antigravity",
                    "--out", str(self.root / f"{verb}.json"))
                self.assertNotEqual(code, 0, result)
                self.assertEqual(result["goal_status"], "blocked", result)
                self.assertEqual(result["actions"], [])
                self.assertFalse(result.get("mutation_started", False))

            manifest = {"schema_version": "larj.t3code-bindings.v1", "profile_root": str(self.home),
                "environment_id": "env_fixture", "runtime_binaries": [str(runtime)], "desktop_profiles": [],
                "native_stores": [{"thread_id": "keeper", "engine": "codex", "root": str(native_root)}]}
            for engine, message in (
                ("antigravity", "t3code_antigravity_instance_required"),
                ("codex", "t3code_provider_binding_conflict"),
            ):
                with self.subTest(forged_engine=engine):
                    forged = {**manifest, "native_stores": [*manifest["native_stores"],
                        {"thread_id": "selected", "engine": engine, "root": str(native_root)}]}
                    bindings = self.root / f"forged-{engine}.json"
                    bindings.write_text(json.dumps(forged), encoding="utf-8")
                    code, result = self.invoke("delete", "run", *base, "--clients-closed",
                        "--engine", "codex", "--t3code-bindings", str(bindings))
                    self.assertNotEqual(code, 0, result)
                    self.assertEqual(result["goal_status"], "blocked", result)
                    self.assertFalse(result["mutation_started"])
                    self.assertIn(message, {row["message"] for row in result["blockers"]})
            self.assertEqual({path: path.read_bytes() for path in before}, before)

            # The provider can survive only in historical JSON, with a typed
            # driver and no native ID in its handle.
            self.db.execute(f"DELETE FROM {V2}provider_threads WHERE provider_thread_id='antigravity-no-handle'")
            insert(self.db, "orchestration_v2_events", event_id="historical-antigravity", thread_id="selected",
                event_type="provider.thread", payload_json=json.dumps({
                    "nativeThreadRef": {"driver": "antigravity", "nativeId": None, "strength": "none"},
                }))
            self.db.commit()
            before = {path: path.read_bytes() for path in before}
            code, result = self.invoke("delete", "plan", *base, "--engine", "codex",
                "--t3code-bindings", str(self.root / "forged-codex.json"))
            self.assertNotEqual(code, 0, result)
            self.assertEqual(result["goal_status"], "blocked", result)
            self.assertFalse(result["mutation_started"])
            self.assertIn("t3code_historical_provider_binding_unqualified",
                {row["message"] for row in result["blockers"]})
            execute.assert_not_called()
        self.writer.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_profiles_are_independent_and_default_is_selected_only(self):
        add_thread(self.db)
        other = self.root / "other"
        with closing(create_store(other)) as db:
            add_thread(db)
        code, value = self.invoke("records", "--client", "t3code", "--t3code-root", str(self.home), "--t3code-root", str(other), "--json")
        self.assertEqual(code, 0, value)
        self.assertEqual(value["count"], 2)
        self.assertEqual(len({t["record_key"]["value"] for t in value["targets"]}), 2)
        with patch.object(store.T3CodeAdapter, "snapshot_references", side_effect=AssertionError("unselected")):
            self.invoke("records", "--client", "native", "--json")

    def test_database_transaction_removes_events_search_and_legacy_preserving_other_data(self):
        add_thread(self.db, "selected")
        add_thread(self.db, "selected", legacy=True)
        add_thread(self.db, "keeper")
        before = self.db.execute(f"SELECT payload_json FROM {V2}messages WHERE thread_id='keeper'").fetchone()[0]
        evidence = database.freeze(self.home, ["selected"])
        self.assertNotIn(PRIVATE, json.dumps(evidence))
        phases = []
        database.apply(evidence, phase_callback=phases.append)
        self.assertEqual(phases, ["mutation_started"])
        self.assertEqual(database.remaining(evidence), 0)
        self.assertEqual(self.db.execute(f"SELECT payload_json FROM {V2}messages WHERE thread_id='keeper'").fetchone()[0], before)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orchestration_v2_message_search").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM projection_projects").fetchone()[0], 1)

    def test_native_history_and_cross_thread_dependencies_are_frozen(self):
        add_thread(self.db, "selected", native="native-1")
        add_thread(self.db, "keeper")
        evidence = database.freeze(self.home, ["selected"])
        self.assertEqual(evidence["native_references"], [{"driver": "codex", "native_id": "native-1"}])
        insert(self.db, V2 + "context_transfers", context_transfer_id="transfer", source_thread_id="selected",
            target_thread_id="keeper", type="fork", status="consumed", payload_json="{}")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "cross_thread_transfer"):
            database.freeze(self.home, ["selected"])
        with self.assertRaisesRegex(ValueError, "database_changed"):
            database.remaining(evidence)

    def test_drift_and_unselected_replay_prevent_mutation(self):
        add_thread(self.db, "selected")
        add_thread(self.db, "keeper")
        evidence = database.freeze(self.home, ["selected"])
        self.db.execute(f"UPDATE {V2}threads SET title='changed' WHERE thread_id='keeper'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "database_changed"):
            database.apply(evidence, phase_callback=lambda _: self.fail("drift must stop before mutation"))
        self.db.execute(f"UPDATE {V2}threads SET payload_json=? WHERE thread_id='keeper'", (json.dumps({"lineage": {"parentThreadId": "selected"}}),))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "unselected_restore_reference"):
            database.freeze(self.home, ["selected"])

    def test_unbound_delete_cannot_become_verified_or_mutate(self):
        add_thread(self.db, "selected", native="native-1")
        path = self.root / "plan.json"
        code, plan = self.invoke("delete", "plan", "--client", "t3code", "--record-id", "selected", "--out", str(path), "--json")
        self.assertNotEqual(code, 0)
        self.assertEqual(plan["goal_status"], "blocked", plan)
        self.assertEqual(plan["actions"], [])
        for verb in ("status", "verify"):
            _, value = self.invoke("operation", verb, "--operation-id", plan["operation_id"], "--plan", str(path), "--json")
            self.assertEqual(value["goal_status"], "blocked", value)
        _, result = self.invoke("delete", "apply", "--operation-id", plan["operation_id"], "--plan", str(path),
            "--authorized-plan-sha256", plan["plan_sha256"], "--clients-closed", "--json")
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["mutation_started"])
        self.writer.assert_not_called()
        self.assertEqual(self.db.execute(f"SELECT count(*) FROM {V2}threads").fetchone()[0], 1)

    def test_shared_provider_session_blocks_and_null_owner_is_cleaned_by_binding(self):
        add_thread(self.db, "selected")
        add_thread(self.db, "keeper")
        insert(self.db, V2 + "provider_sessions", provider_session_id="session-1", thread_id=None,
            provider="codex", status="stopped", payload_json="{}")
        insert(self.db, V2 + "provider_session_bindings", provider_session_id="session-1", thread_id="selected")
        insert(self.db, V2 + "provider_session_bindings", provider_session_id="session-1", thread_id="keeper")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "shared_provider_session"):
            database.freeze(self.home, ["selected"])
        self.db.execute(f"DELETE FROM {V2}provider_session_bindings WHERE thread_id='keeper'")
        self.db.commit()
        evidence = database.freeze(self.home, ["selected"])
        self.assertEqual(evidence["provider_session_ids"], ["session-1"])
        database.apply(evidence, phase_callback=lambda _: None)
        self.assertEqual(database.remaining(evidence), 0)
        self.assertEqual(self.db.execute(f"SELECT count(*) FROM {V2}provider_sessions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
