import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import t3code_cleanup as cleanup, t3code_database as database
from local_agent_record_janitor import t3code_cleanup_files as files, t3code_lifecycle as lifecycle
from local_agent_record_janitor.t3code_bound_adapter import T3CodeBoundAdapter, SCHEMA
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from tests.orca_native_support import create_native_schema, add_native_record
from tests.t3code_support import create_store, add_thread, insert, V2, PRIVATE

THREAD = "11111111-1111-4111-8111-111111111111"
KEEP = "22222222-2222-4222-8222-222222222222"
NATIVE = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"


@unittest.skipUnless(os.name == "nt", "Qualified T3 startup lock is Windows-only")
class T3CodeFullflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve(strict=True)
        self.root, self.home = self.base / "t3", self.base / "codex"
        self.home.mkdir()
        with closing(create_store(self.root)) as db:
            add_thread(db, THREAD, native=NATIVE)
            add_thread(db, KEEP, native=OTHER)
        (self.root / "userdata/environment-id").write_text("env_test\n", encoding="utf-8")
        (self.root / lifecycle.OWNER).touch()
        create_native_schema(self.home)
        self.binary = self.base / "synthetic-t3.exe"
        self.binary.write_bytes(b"synthetic identity gate, never executed")
        self.manifest = {"schema_version": SCHEMA, "profile_root": str(self.root), "environment_id": "env_test",
            "runtime_binaries": [str(self.binary)], "desktop_profiles": [], "native_stores": [
                {"thread_id": THREAD, "engine": "codex", "root": str(self.home)},
                {"thread_id": KEEP, "engine": "codex", "root": str(self.home)}]}
        self.adapter = T3CodeBoundAdapter(self.manifest)
        self.plan_path = self.base / "plan.json"
        for item in (patch("local_agent_record_janitor.office_runtime.processes", return_value=[]),
                     patch("pathlib.Path.home", return_value=self.base),
                     patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "LOCALAPPDATA": str(self.base / "localappdata"),
                         "PASEO_HOME": str(self.base / "absent-paseo"), "T3CODE_HOME": str(self.root)})):
            item.start(); self.addCleanup(item.stop)

    def coordinator(self):
        return OperationCoordinator(CleanupService(client_inspector=lambda *_args, **_kwargs: ()))

    def plan(self):
        result = self.coordinator().plan_operation(client="t3code", record_ids=(THREAD,), engines=("codex",),
            adapters=(self.adapter,), plan_path=self.plan_path, operation_id="t3-fullflow")
        self.assertEqual(result.get("goal_status"), "ready", result)
        self.assertNotIn(PRIVATE, json.dumps(result))
        return result

    def apply(self, plan):
        return self.coordinator().apply_operation(operation_id=plan["operation_id"], plan_path=self.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)

    def test_frontend_only_cold_apply_verify_and_terminal_preservation(self):
        terminal = self.root / files.TERMINALS
        terminal.mkdir(parents=True)
        (terminal / (THREAD + ".log")).write_text(PRIVATE)
        (terminal / (KEEP + ".log")).write_text("keeper")
        plan = self.plan()
        self.assertEqual([a["kind"] for a in plan["actions"]], [cleanup.KIND])
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertFalse((terminal / (THREAD + ".log")).exists())
        self.assertEqual((terminal / (KEEP + ".log")).read_text(), "keeper")
        self.assertTrue((self.root / lifecycle.OWNER).exists())
        self.assertEqual(database.remaining(cleanup.evidence_from_document(plan)["files"]["database"]), 0)
        result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(result["goal_status"], "complete", result)

    def test_native_dependency_and_exact_ticket(self):
        add_native_record(self.home, NATIVE)
        plan = self.plan()
        self.assertEqual([a["kind"] for a in plan["actions"]], ["delete_conversation", cleanup.KIND])
        evidence = cleanup.evidence_from_document(plan)
        reference = next(r for r in self.adapter.snapshot_references().references if r.frontend_id == THREAD)
        self.assertFalse(cleanup.permits_reference(reference))
        with cleanup.planning_scope(evidence):
            self.assertTrue(cleanup.permits_reference(reference))
            self.assertFalse(cleanup.permits_reference(reference, execution=True))
        with self.assertRaisesRegex(ValueError, "execution_ticket_required"):
            cleanup.execute(evidence, phase_callback=lambda _: self.fail("standalone denied"))

    def test_existing_owner_and_database_drift_block_before_mutation(self):
        plan = self.plan()
        with closing(sqlite3.connect(self.root / lifecycle.OWNER, timeout=0)) as owner:
            owner.execute("BEGIN EXCLUSIVE")
            result = self.apply(plan)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["modified"])

    def test_held_startup_boundary_and_binary_gate(self):
        evidence = cleanup.evidence_from_document(self.plan())
        with lifecycle.hold(evidence, census=lambda: []):
            with closing(sqlite3.connect(self.root / lifecycle.OWNER, timeout=0)) as contender:
                with self.assertRaises(sqlite3.OperationalError):
                    contender.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(OSError):
                self.binary.write_bytes(b"replacement")
        self.assertIsNone(lifecycle.current_boundary(self.root))

    def test_historical_provider_and_unselected_native_owner_block(self):
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            insert(db, "orchestration_v2_events", event_id="historic", thread_id=THREAD, event_type="provider.thread",
                payload_json=json.dumps({"nativeThreadRef": {"driver": "codex", "nativeId": OTHER, "strength": "strong"}}))
            db.commit()
        with self.assertRaisesRegex(ValueError, "unselected_thread_shares_native_record"):
            cleanup.build_context(self.coordinator(), (self.adapter,), (self.adapter,), [THREAD], ["codex"])

    def test_weak_or_unbound_bare_history_never_grants_native_authority(self):
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            for payload in ({"nativeThreadId": "unbound-native"},
                            {"nativeThreadRef": {"driver": "codex", "nativeId": NATIVE, "strength": "weak"}},
                            {"nativeThreadRef": "invalid"}):
                insert(db, "orchestration_v2_events", event_id="unqualified", thread_id=THREAD,
                    event_type="provider.thread", payload_json=json.dumps(payload))
                db.commit()
                with self.assertRaisesRegex(ValueError, "(historical_native_reference_unbound|native_reference_strength_unqualified|native_reference_shape_unqualified)"):
                    self.adapter.snapshot_references(refresh=True)
                db.execute("DELETE FROM orchestration_v2_events WHERE event_id='unqualified'")
                db.commit()

    def test_unqualified_recovery_files_block(self):
        paths = (self.root / "userdata/state.sqlite", self.root / "userdata/attachments/file",
                 self.root / files.TERMINALS / "unknown.log")
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic unknown recovery")
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                files.freeze(self.root, [THREAD])
            path.unlink()

    def test_database_and_bound_desktop_apply_as_one_operation(self):
        from local_agent_record_janitor import office_leveldb as level
        from tests.test_t3code_localstorage import local_storage
        try:
            level.runtime()
        except level.OfficeLevelDBError:
            if os.environ.get("LARJ_REQUIRE_LEVELDB_TESTS") == "1":
                raise
            self.skipTest("Pinned LevelDB runtime not configured")
        profile = self.base / "desktop"
        local_storage(profile)
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            add_thread(db, "thread-1")
        (self.root / "userdata/environment-id").write_text("env-1\n", encoding="utf-8")
        self.manifest["environment_id"] = "env-1"
        self.manifest["native_stores"].append({"thread_id": "thread-1", "engine": "codex", "root": str(self.home)})
        self.manifest["desktop_profiles"] = [{"root": str(profile), "runtime_binaries": [str(self.binary)]}]
        plan = self.coordinator().plan_operation(client="t3code", record_ids=("thread-1",),
            adapters=(T3CodeBoundAdapter(self.manifest),), plan_path=self.plan_path, operation_id="t3-desktop-fullflow")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertNotIn("T3_PRIVATE", json.dumps(plan))
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        local_storage(profile, inspect=True)
        result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(result["goal_status"], "complete", result)

    def test_frozen_native_scope_cannot_be_expanded(self):
        add_native_record(self.home, NATIVE)
        evidence = cleanup.evidence_from_document(self.plan())
        extra = copy.deepcopy(evidence["native_actions"][0])
        extra.update(action_id="unowned", thread_id=OTHER, affected_thread_ids=[OTHER])
        evidence["native_actions"].append(extra)
        evidence["native_targets"][0]["action_ids"].append("unowned")
        evidence["native_targets"][0]["ids"].append(OTHER)
        with self.assertRaisesRegex(ValueError, "native_action_owner_unproven"):
            cleanup.validate(evidence)

    def test_database_drift_is_blocked_before_native_deletion(self):
        native = add_native_record(self.home, NATIVE)
        plan = self.plan()
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}threads SET title='changed' WHERE thread_id=?", (KEEP,))
            db.commit()
        with patch.object(OperationCoordinator, "_execute_manual_batch", side_effect=AssertionError("native writer must not run")):
            result = self.apply(plan)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["modified"])
        self.assertTrue(native.exists())

    def test_unknown_frontend_result_is_not_retried_and_cold_verify_recovers(self):
        plan = self.plan()
        original = files.apply
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("synthetic interruption after database commit")
        with patch.object(files, "apply", side_effect=interrupted):
            result = self.apply(plan)
        self.assertEqual(result["goal_status"], "unknown", result)
        with patch.object(files, "apply", side_effect=AssertionError("must not repeat started child")):
            result = self.apply(plan)
            self.assertEqual(result["goal_status"], "unknown", result)
            result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(result["goal_status"], "complete", result)

    def test_claude_writer_finishes_before_frontend_and_is_not_repeated(self):
        claude = self.base / "claude"
        transcript = claude / "projects" / "synthetic" / (NATIVE + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"sessionId": NATIVE, "message": PRIVATE}) + "\n", encoding="utf-8")
        keeper = transcript.with_name(OTHER + ".jsonl")
        keeper.write_text(json.dumps({"sessionId": OTHER, "message": "keep"}) + "\n", encoding="utf-8")
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}threads SET default_provider='claudeAgent' WHERE thread_id=?", (THREAD,))
            db.execute(f"UPDATE {V2}provider_threads SET driver='claudeAgent',provider='claudeAgent',payload_json=? WHERE thread_id=?",
                (json.dumps({"nativeThreadRef": {"driver": "claudeAgent", "nativeId": NATIVE, "strength": "strong"}}), THREAD))
            db.commit()
        self.manifest["native_stores"][0].update(engine="claude", root=str(claude))
        self.adapter = T3CodeBoundAdapter(self.manifest)
        plan = self.coordinator().plan_operation(client="t3code", record_ids=(THREAD,),
            adapters=(self.adapter,), plan_path=self.plan_path, operation_id="t3-claude-fullflow")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual({a["kind"] for a in plan["actions"]}, {"delete_claude_session", cleanup.KIND})
        with patch.object(cleanup, "execute", side_effect=RuntimeError("synthetic stop before frontend")):
            result = self.apply(plan)
        self.assertFalse(transcript.exists(), result)
        self.assertTrue(keeper.exists())
        original = CleanupService.execute
        def only_frontend(service, context, actions, **kwargs):
            self.assertTrue(all(str(a.kind.value) == cleanup.KIND for a in actions))
            return original(service, context, actions, **kwargs)
        with patch.object(CleanupService, "execute", only_frontend):
            result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(keeper.exists())


if __name__ == '__main__':
    unittest.main()
