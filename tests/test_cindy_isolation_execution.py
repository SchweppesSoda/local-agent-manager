from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
import os
import shutil
import subprocess

from local_agent_manager.cindy_isolation_execution import admitted_import, execute_stages, journal_projection, publish_new_file
from local_agent_manager.cindy_isolation_plan import directory_identity, fingerprint, IsolationPlanError
from local_agent_manager.cindy_isolation_recovery import (
    compare_packet, database_preservation, digest, file_state, observe_database,
    remaining_plan, auth_state,
)
from local_agent_manager.operation_store import OperationStore, plan_sha256, atomic_write_json
from local_agent_manager.mutation_guard import UnknownMutationError


class FixtureBoundary:
    """Only temporary stores: not accepted by the live controller."""
    def __init__(self, case):
        self.case, self.held, self.source_checks = case, True, 0

    def require_held(self):
        if not self.held:
            raise IsolationPlanError("boundary_not_held")

    def validate_source(self):
        self.require_held(); self.source_checks += 1

    def verify_import(self):
        for packet in self.case.spec["database_packets"]:
            self.case.assertEqual(observe_database(self.case.target / Path(packet["packet"]).name,
                                                  packet, ["selected"])["state"], "after")

    def snapshot_auth(self):
        self.case.auth.write_text("synthetic independent credential")

    def switch_registry(self):
        atomic_write_json(self.case.registry, {"route": "legacy", "unselected": 1})

    def switch_location(self, change):
        atomic_write_json(Path(change["file"]), change["after"])

    def verify_switch(self):
        self.case.assertEqual(json.loads(self.case.registry.read_text())["route"], "legacy")


class CindyIsolationExecutionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.target = self.root / "target"; self.target.mkdir()
        self.packet_dir = self.root / "packets"; self.packet_dir.mkdir()
        self.packet_source = self.root / "input.jsonl"; self.packet_source.write_bytes(b"synthetic rollout\n")
        self.dest = self.target / "selected.jsonl"
        self.registry = self.root / "registry.json"; self.registry.write_text('{"route":"official","unselected":1}')
        self.auth = self.root / "auth"; self.auth.write_text("synthetic shared credential")
        self.spec = {"target": {"path": str(self.target)}, "native_files": [
            {"native_id": "selected", "path": str(self.packet_source), "target_path": str(self.dest), "sha256": digest(self.packet_source)}],
            "database_packets": [], "location_changes": []}
        for name, table, schema, rows in [
            ("state.sqlite", "threads", "id text primary key,rollout_path text,title text",
             [("selected", str(self.dest), "opaque title")]),
            ("history.sqlite", "thread_items", "thread_id text,item_id text,item_json blob,primary key(thread_id,item_id)",
             [("selected", "one", b"\x00opaque\x10")])]:
            packet, target = self.packet_dir / name, self.target / name
            for path in (packet, target):
                with closing(sqlite3.connect(path)) as c:
                    c.execute("pragma journal_mode=wal")
                    c.execute("create table " + table + "(" + schema + ")")
                    c.execute("create table unrelated(id text primary key,value text)")
                    c.execute("insert into unrelated values('keep','unchanged')")
                    c.commit()
            with closing(sqlite3.connect(packet)) as c:
                c.executemany("insert into " + table + " values(?,?,?)", rows); c.commit()
            self.spec["database_packets"].append({"packet": str(packet), "sha256": digest(packet), "rows_inserted": {table: 1}})
        for i in range(2):
            path = self.root / ("location" + str(i) + ".json"); path.write_text('{"home":"official"}')
            self.spec["location_changes"].append({"file": str(path), "before": {"home": "official"}, "after": {"home": "legacy"}})
        self.events = []
        self.boundary = FixtureBoundary(self)

    def checkpoint(self, event, mutation=None, batch=None):
        self.events.append({"event": event, "mutation": mutation, "batch": batch})

    def run_stages(self, callback=None):
        return execute_stages(self.spec, ["selected"], boundary=self.boundary, checkpoint=callback or self.checkpoint)

    def db_states(self):
        return [observe_database(self.target / Path(p["packet"]).name, p, ["selected"])["state"] for p in self.spec["database_packets"]]

    def test_full_stages_are_not_runtime_acceptance_or_source_deletion(self):
        result = self.run_stages()
        self.assertTrue(result["offline_import_verified"])
        self.assertFalse(result["goal_satisfied"])
        self.assertFalse(result["source_deleted"])
        self.assertTrue(result["runtime_acceptance_pending"])
        self.assertEqual(self.boundary.source_checks, 2)
        self.assertEqual(self.db_states(), ["after", "after"])
        with self.assertRaisesRegex(IsolationPlanError, "already_present"):
            self.run_stages()

    def test_absent_boundary_cannot_write(self):
        self.boundary.held = False
        with self.assertRaisesRegex(IsolationPlanError, "boundary"):
            self.run_stages()
        self.assertFalse(self.dest.exists()); self.assertEqual(self.events, [])

    def test_incomplete_stage_never_publishes_a_half_target(self):
        def crash(event, **kw):
            self.checkpoint(event, **kw)
            if event == "file_publish_intent": raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_stages(crash)
        self.assertFalse(self.dest.exists())
        temporary = Path(self.events[0]["mutation"]["path"])
        self.assertTrue(temporary.exists())
        self.assertEqual(digest(temporary), digest(self.packet_source))

    def test_crash_after_publish_retains_complete_file_and_no_replay(self):
        def crash(event, **kw):
            self.checkpoint(event, **kw)
            if event == "file_published": raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_stages(crash)
        self.assertEqual(file_state(self.dest, digest(self.packet_source)), "after")
        self.assertTrue(Path(self.events[0]["mutation"]["path"]).exists())
        with self.assertRaisesRegex(IsolationPlanError, "already_present"): self.run_stages()

    def test_no_replace_publish_retains_existing_target(self):
        self.dest.write_bytes(b"target increment")
        with self.assertRaises(FileExistsError):
            publish_new_file(self.packet_source, self.dest, digest(self.packet_source), checkpoint=self.checkpoint)
        self.assertEqual(self.dest.read_bytes(), b"target increment")

    def test_first_db_committed_second_rolled_back_is_detectable(self):
        def crash(event, **kw):
            self.checkpoint(event, **kw)
            if event == "database_committed": raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_stages(crash)
        self.assertEqual(self.db_states(), ["after", "before"])
        self.assertEqual(json.loads(self.registry.read_text())["route"], "official")
        self.assertIn("database:state.sqlite", [e["batch"] for e in self.events])

    def test_commit_without_completion_checkpoint_is_observed_from_db(self):
        def crash(event, **kw):
            if event == "database_committed": raise SystemExit()
            self.checkpoint(event, **kw)
        with self.assertRaises(SystemExit): self.run_stages(crash)
        self.assertEqual(self.db_states(), ["after", "before"])
        self.assertFalse(any(e["batch"] == "database:state.sqlite" for e in self.events))

    def test_late_schema_failure_rolls_back_all_prepared_transactions(self):
        with closing(sqlite3.connect(self.target / "history.sqlite")) as c:
            c.execute("alter table thread_items add column changed text"); c.commit()
        with self.assertRaisesRegex(IsolationPlanError, "schema_mismatch"): self.run_stages()
        self.assertEqual(observe_database(self.target / "state.sqlite", self.spec["database_packets"][0], ["selected"])["state"], "before")

    def test_registry_switched_location_partial_remains_explicit(self):
        def crash(event, **kw):
            self.checkpoint(event, **kw)
            if event == "location_verified": raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_stages(crash)
        self.assertEqual(self.db_states(), ["after", "after"])
        self.assertEqual(json.loads(self.registry.read_text())["route"], "legacy")
        self.assertEqual([json.loads(Path(x["file"]).read_text())["home"] for x in self.spec["location_changes"]], ["legacy", "official"])

    def test_post_import_target_increment_is_retained_and_not_called_exact(self):
        self.run_stages()
        with closing(sqlite3.connect(self.target / "history.sqlite")) as c:
            c.execute("insert into thread_items values('selected','new',?)", (b"new target content",)); c.commit()
        result = observe_database(self.target / "history.sqlite", self.spec["database_packets"][1], ["selected"])
        self.assertEqual(result["state"], "diverged")
        self.assertEqual(result["tables"]["thread_items"]["additional_or_changed"], 1)
        self.assertNotIn("new target content", json.dumps(result))

    def test_unselected_nullable_rows_and_shared_tables_are_protected(self):
        path = self.target / "history.sqlite"
        with closing(sqlite3.connect(path)) as c:
            c.execute("insert into thread_items values(NULL,'nullable','keep')"); c.commit()
            before = database_preservation(c, ["selected"], ["thread_items"])
            c.execute("update unrelated set value='changed'")
            self.assertNotEqual(database_preservation(c, ["selected"], ["thread_items"]), before)
            c.rollback()
            c.execute("delete from thread_items where thread_id is NULL")
            self.assertNotEqual(database_preservation(c, ["selected"], ["thread_items"]), before)

    def test_recovery_never_overwrites_increment_or_replays_original(self):
        observation = {"spec_sha256": "original", "physical_roots": {"source": True},
                       "files": {"one": "after", "half": "diverged"},
                       "databases": {"state": {"target": {"state": "after"}, "source": {"state": "after"}},
                                     "history": {"target": {"state": "before"}, "source": {"state": "after"}}},
                       "locations": {"loc": "before"}, "registry": "after", "auth": {"state": "after"}}
        result = remaining_plan(observation, original_sha256="original", cumulative_mutations=[{"kind": "commit"}], completed_batches=["database:state"])
        self.assertIn("file:half", result["blockers"])
        self.assertNotIn("file:one", result["writes"])
        self.assertIn("database:history", result["writes"])
        self.assertEqual(result["completed_batches"], ["database:state"])
        self.assertFalse(result["target_restore_or_delete"])
        self.assertFalse(result["apply_permitted"])

    def test_auth_observation_distinguishes_entity_and_generation_without_secrets(self):
        source = self.root / "official-auth"; target = self.root / "legacy-auth"
        source.write_text("SYNTHETIC_SECRET_NEVER_PRINT")
        os.link(source, target)
        stat = source.stat()
        spec = {"auth": {"source": {"path": str(source), "device": stat.st_dev, "inode": stat.st_ino,
                                    "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                         "target": {"path": str(target)}}}
        self.assertEqual(auth_state(spec)["state"], "before")
        temporary = self.root / "auth-stage"; temporary.write_bytes(source.read_bytes())
        os.replace(temporary, target)
        result = auth_state(spec)
        self.assertEqual(result["state"], "after")
        self.assertFalse(result["permissions_verified"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        source.write_text("NEW_SYNTHETIC_GENERATION")
        result = auth_state(spec)
        self.assertEqual(result["state"], "diverged")
        self.assertFalse(result["source_generation_matches_freeze"])

    def test_partial_journal_replica_retains_cumulative_evidence(self):
        entries = []
        for name in ("source", "target", "profile"):
            root = self.root / name; root.mkdir(exist_ok=True)
            store = OperationStore(root, "cindy-fixture")
            plan = {"schema_version": "larj.agent-plan.v1", "operation_id": store.operation_id,
                    "target": {"codex_home": str(root)}, "authorization": {"root_actions": []}}
            plan["plan_sha256"] = plan_sha256(plan)
            entries.append((store, plan))
        entries[0][0].accept_plan(entries[0][1])
        entries[0][0].write_state({"schema_version": "larj.agent-state.v1", "operation_id": "cindy-fixture",
                                 "plan_sha256": entries[0][1]["plan_sha256"], "phase": "executing",
                                 "goal_status": "unknown", "goal_satisfied": False,
                                 "modified": True, "mutation_started": True,
                                 "cumulative_mutations": [{"kind": "database_commit"}], "completed_batches": ["state"]})
        entries[1][0].accept_plan(entries[1][1])  # no state: partial admission
        result = journal_projection(entries)
        self.assertEqual(result["goal_status"], "unknown")
        self.assertEqual(result["completed_batches"], ["state"])
        self.assertEqual(result["cumulative_mutations"], [{"kind": "database_commit"}])
        self.assertEqual(result["roots"][1]["state"], "partial_admission")
        self.assertFalse(entries[2][0].directory.exists())

    def admission_entries(self):
        entries = []
        for name in ("source", "target", "profile"):
            root = self.root / name; root.mkdir(exist_ok=True)
            self.spec[name] = directory_identity(root)
            store = OperationStore(root, "cindy-admission-fixture")
            plan = {"schema_version": "larj.agent-plan.v1", "operation_id": store.operation_id,
                    "target": {"codex_home": str(root)}, "authorization": {"root_actions": []}}
            entries.append((store, plan))
        self.spec["plan_sha256"] = fingerprint(self.spec)
        for _, plan in entries:
            plan["migration_spec"] = self.spec
            plan["plan_sha256"] = plan_sha256(plan)
        return entries

    @contextmanager
    def boundary_factory(self):
        yield self.boundary

    def test_later_root_lock_failure_has_no_product_mutation(self):
        entries = self.admission_entries()
        original = OperationStore.mutation_lock
        @contextmanager
        def failing_lock(store):
            if store is entries[1][0]: raise PermissionError("synthetic lock denied")
            with original(store): yield
        with patch.object(OperationStore, "mutation_lock", failing_lock):
            with self.assertRaises(PermissionError):
                admitted_import(self.spec, ["selected"], entries, boundary_factory=self.boundary_factory)
        self.assertFalse(self.dest.exists()); self.assertEqual(self.db_states(), ["before", "before"])
        self.assertTrue(all(store.directory.exists() for store, _ in entries))
        self.assertFalse(any(store.lock_exists() for store, _ in entries))
        self.assertEqual(journal_projection(entries)["goal_status"], "unknown")
        with self.assertRaises(UnknownMutationError):
            admitted_import(self.spec, ["selected"], entries, boundary_factory=self.boundary_factory)

    def test_partial_plan_admission_has_no_product_mutation(self):
        entries = self.admission_entries()
        original = OperationStore.accept_plan
        def fail(store, plan):
            if store is entries[1][0]: raise PermissionError("synthetic admission denied")
            original(store, plan)
        with patch.object(OperationStore, "accept_plan", fail):
            with self.assertRaises(PermissionError):
                admitted_import(self.spec, ["selected"], entries, boundary_factory=self.boundary_factory)
        self.assertFalse(self.dest.exists()); self.assertFalse(entries[2][0].directory.exists())
        self.assertEqual(journal_projection(entries)["goal_status"], "unknown")

    def test_journal_status_retains_state_if_events_are_unreadable(self):
        entries = self.admission_entries()
        store, plan = entries[0]; store.accept_plan(plan)
        store.write_state({"schema_version": "larj.agent-state.v1", "operation_id": store.operation_id,
                           "plan_sha256": plan["plan_sha256"], "phase": "executing", "goal_status": "unknown",
                           "goal_satisfied": False, "modified": True, "mutation_started": True,
                           "cumulative_mutations": [{"kind": "commit"}], "completed_batches": ["state"]})
        with patch.object(OperationStore, "read_events", side_effect=PermissionError("denied")):
            result = journal_projection(entries)
        self.assertEqual(result["completed_batches"], ["state"])
        self.assertEqual(result["roots"][0]["state"], "unknown")

    def test_frozen_empty_table_cannot_hide_new_selected_dependency(self):
        packet = self.spec["database_packets"][0]
        for path in (Path(packet["packet"]), self.target / "state.sqlite"):
            with closing(sqlite3.connect(path)) as c:
                c.execute("create table thread_attachments(thread_id text,attachment_id text,primary key(thread_id,attachment_id))")
                c.commit()
        with closing(sqlite3.connect(self.target / "state.sqlite")) as c:
            c.execute("insert into thread_attachments values('selected','new')"); c.commit()
        result = observe_database(self.target / "state.sqlite", packet, ["selected"])
        self.assertEqual(result["tables"]["thread_attachments"]["state"], "diverged")

    @unittest.skipUnless(os.name == "nt", "Windows sharing-mode qualification")
    def test_windows_executable_lease_blocks_launch_and_atomic_target_replace(self):
        from local_agent_manager.windows_held_files import exclusive_fd
        image = self.root / "fixture-cmd.exe"
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", image)
        with closing(os.fdopen(exclusive_fd(image), "rb")):
            with self.assertRaises(OSError):
                subprocess.run([str(image), "/d", "/c", "exit", "0"], check=True,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        # Also qualify why a held exclusive file handle cannot be kept through
        # pathname os.replace: that is an additional required boundary, not CAS.
        self.dest.write_bytes(b"old")
        staged = self.root / "staged"; staged.write_bytes(b"new")
        with closing(os.fdopen(exclusive_fd(self.dest), "rb")):
            with self.assertRaises(OSError): os.replace(staged, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"old")


if __name__ == "__main__": unittest.main()
