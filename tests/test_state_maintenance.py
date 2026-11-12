from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_manager.agent_operations import result_document
from local_agent_manager.entrypoint import main
from local_agent_manager.mutation_guard import (
    MutationRootLockedError, MutationScope, UnknownMutationError, mutation_guard, mutation_roots,
)
from local_agent_manager.operation_coordinator import _find_operation_plan
from local_agent_manager.operation_store import OperationStore, OperationStoreError, _file_identity, _receipt_sha256
from local_agent_manager.state_maintenance import run_state_command
from local_agent_manager.state_paths import LAYOUT_MARKER, LEGACY_CONTROL, MANAGER_CONTROL
from tests.test_mutation_guard import basic_action, write_journal


class StateMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.home = self.root / "home"
        self.home.mkdir()

    def terminal(self, operation="terminal", goal="complete"):
        store = write_journal(self.home, operation, basic_action(self.home))
        store.append_event({"event": "verification_finished", "goal_status": goal},
            state_updates={"phase": "finished", "goal_status": goal, "goal_satisfied": goal == "complete"})
        result = result_document(subcommand="verify", operation_id=operation,
            plan_sha=store.read_plan()["plan_sha256"], goal_status=goal, modified=True, mutation_started=True,
            details={"verification": {"all_satisfied": goal == "complete", "verified_action_ids": ["synthetic-action"]},
                     "final_scope_verification": {"all_satisfied": goal == "complete", "scan_complete": True}})
        store.write_result(result)
        return store

    def snapshot(self, directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def apply(self, command):
        plan = run_state_command(command, self.home)
        return run_state_command(command, self.home, apply=True, approved_hash=plan["plan_sha256"])

    def expire(self, store):
        receipt = json.loads(store.receipt_path.read_text(encoding="utf-8"))
        receipt.update(completed_at=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
                       receipt_expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        receipt["receipt_sha256"] = _receipt_sha256(receipt)
        store.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    def test_inventory_and_preview_preserve_journals_and_expired_receipts(self):
        store = self.terminal()
        self.apply("cleanup")
        self.expire(store)
        before = self.snapshot(self.home)
        inventory = run_state_command("inventory", self.home)
        self.assertEqual(inventory["operations"][0]["action"], "expire")
        run_state_command("cleanup", self.home)
        self.assertEqual(before, self.snapshot(self.home))

    def test_empty_inventory_does_not_create_state(self):
        self.assertEqual(run_state_command("inventory", self.home)["operation_count"], 0)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_external_route_keeps_lock_and_reads_historical_journals(self):
        old = self.terminal()
        lock = old.directory.parent / ".mutation.lock"
        identity = _file_identity(lock.stat())
        base = self.root / "external"
        base.mkdir()
        preview = run_state_command("route", self.home, directory=base)
        self.assertEqual(list(base.iterdir()), [])
        result = run_state_command("route", self.home, directory=base, apply=True, approved_hash=preview["plan_sha256"])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(OperationStore(self.home, old.operation_id).plan_path, old.plan_path)
        fresh = self.terminal("new")
        self.assertTrue(fresh.directory.is_relative_to(base))
        self.assertEqual(_find_operation_plan("new", operation_home=self.home), fresh.plan_path)
        inventory = run_state_command("inventory", self.home)
        self.assertEqual(inventory["operation_count"], 2)
        self.assertTrue(Path(inventory["control_directory"]).is_relative_to(base))
        self.assertEqual(self.apply("cleanup")["completed"], [fresh.operation_id, old.operation_id])
        self.assertTrue(old.receipt_path.is_file())
        self.assertTrue(fresh.receipt_path.is_file())
        self.assertEqual(_file_identity(lock.stat()), identity)

    def test_routed_store_still_fails_closed_on_unknown_legacy_journal(self):
        old = write_journal(self.home, "old-unknown", basic_action(self.home))
        old.lock_path.write_text("existing lock", encoding="utf-8")
        base = self.root / "external"
        base.mkdir()
        preview = run_state_command("route", self.home, directory=base)
        run_state_command("route", self.home, directory=base, apply=True, approved_hash=preview["plan_sha256"])
        with self.assertRaises(UnknownMutationError):
            with mutation_guard([MutationScope(self.home, None)]):
                self.fail("an old unknown operation must still block the store")
        self.assertEqual(old.lock_path.read_text(encoding="utf-8"), "existing lock")
        self.assertEqual(OperationStore(self.home, old.operation_id).plan_path, old.plan_path)

    def test_routed_desktop_journal_keeps_historical_lookup(self):
        from local_agent_manager.t3code_desktop_cleanup import admission, journal_root

        previous = journal_root(self.home) / "desktop-fixture"
        previous.mkdir(parents=True)
        (previous / "state.json").write_text('{"phase":"complete"}', encoding="utf-8")
        base = self.root / "external"
        base.mkdir()
        preview = run_state_command("route", self.home, directory=base)
        run_state_command("route", self.home, directory=base, apply=True, approved_hash=preview["plan_sha256"])
        self.assertEqual(journal_root(self.home, previous.name), previous.parent)
        self.assertTrue(journal_root(self.home, "new").is_relative_to(base))
        admission(self.home)

    def test_routing_preserves_previously_migrated_plan_lookup(self):
        old = self.terminal()
        self.apply("migrate")
        migrated = OperationStore(self.home, old.operation_id)
        base = self.root / "repository-state"
        base.mkdir()
        preview = run_state_command("route", self.home, directory=base)
        run_state_command("route", self.home, directory=base, apply=True, approved_hash=preview["plan_sha256"])
        self.assertEqual(_find_operation_plan(old.operation_id, operation_home=self.home), migrated.plan_path)
        self.assertEqual(_find_operation_plan(old.operation_id, operation_home=old.directory), migrated.plan_path)
        self.assertEqual(run_state_command("inventory", self.home)["operation_count"], 1)

    def test_relocate_preserves_receipt_and_historical_checkpoint_bytes(self):
        compact = self.terminal("receipt")
        self.apply("cleanup")
        checkpoint = self.terminal("checkpoint")
        checkpoint.result_path.unlink()
        locked = self.terminal("locked")
        locked.lock_path.write_text("existing lock", encoding="utf-8")
        snapshots = {s.operation_id: self.snapshot(s.directory) for s in (compact, checkpoint, locked)}
        lock = compact.directory.parent / ".mutation.lock"
        identity = _file_identity(lock.stat())
        base = self.root / "external"
        base.mkdir()
        route = run_state_command("route", self.home, directory=base)
        run_state_command("route", self.home, directory=base, apply=True, approved_hash=route["plan_sha256"])
        result = self.apply("relocate")
        self.assertEqual(set(result["completed"]), {"receipt", "checkpoint"})
        self.assertEqual(result["retained"], ["locked"])
        for previous in (compact, checkpoint):
            moved = OperationStore(self.home, previous.operation_id)
            self.assertTrue(moved.directory.is_relative_to(base))
            self.assertFalse(previous.directory.exists())
            self.assertEqual(self.snapshot(moved.directory), snapshots[previous.operation_id])
        self.assertEqual(self.snapshot(locked.directory), snapshots["locked"])
        self.assertEqual(_file_identity(lock.stat()), identity)
        self.assertEqual(_find_operation_plan("checkpoint", operation_home=checkpoint.directory),
                         OperationStore(self.home, "checkpoint").plan_path)
        self.assertTrue(OperationStore(self.home, "receipt").read_result(maintain=False)["compacted"])

    def test_relocation_resumes_after_partial_source_unlink(self):
        store = self.terminal()
        original = self.snapshot(store.directory)
        base = self.root / "external"
        base.mkdir()
        route = run_state_command("route", self.home, directory=base)
        run_state_command("route", self.home, directory=base, apply=True, approved_hash=route["plan_sha256"])
        plan = run_state_command("relocate", self.home)
        unlink = Path.unlink

        def interrupt(path, *args, **kwargs):
            if path == store.state_path:
                raise OSError("interrupted after publication")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupt):
            with self.assertRaisesRegex(OSError, "interrupted after publication"):
                run_state_command("relocate", self.home, apply=True, approved_hash=plan["plan_sha256"])
        self.assertFalse(store.plan_path.exists())
        self.assertTrue(store.state_path.exists())
        self.assertEqual(run_state_command("inventory", self.home)["phase"], "moving")
        with self.assertRaisesRegex(OperationStoreError, "incomplete"):
            OperationStore(self.home, store.operation_id).read_plan()
        with self.assertRaisesRegex(OperationStoreError, "approval"):
            run_state_command("relocate", self.home, apply=True, approved_hash="0" * 64)
        done = run_state_command("relocate", self.home, apply=True, approved_hash=plan["plan_sha256"])
        self.assertEqual(done["phase"], "complete")
        self.assertEqual(self.snapshot(OperationStore(self.home, store.operation_id).directory), original)

    def test_windows_path_and_handle_identity_use_the_same_creation_time(self):
        path_stat = SimpleNamespace(st_dev=1, st_ino=2, st_ctime=3, st_ctime_ns=30, st_birthtime_ns=20)
        handle_stat = SimpleNamespace(st_dev=1, st_ino=2, st_ctime=4, st_ctime_ns=40, st_birthtime_ns=20)
        with patch("local_agent_manager.operation_store.os.name", "nt"):
            self.assertEqual(_file_identity(path_stat), _file_identity(handle_stat))
            handle_stat.st_birthtime_ns = 21
            self.assertNotEqual(_file_identity(path_stat), _file_identity(handle_stat))

    def test_cleanup_compacts_known_result_then_expires_and_is_idempotent(self):
        store = self.terminal()
        lock = store.directory.parent / ".mutation.lock"
        identity = lock.stat().st_ino
        result = self.apply("cleanup")
        self.assertEqual(result["completed"], [store.operation_id])
        self.assertEqual({p.name for p in store.directory.iterdir()}, {"receipt.json"})
        self.assertFalse(self.apply("cleanup")["modified"])
        self.expire(store)
        self.apply("cleanup")
        self.assertFalse(store.directory.exists())
        self.assertEqual(lock.stat().st_ino, identity)
        self.assertFalse(self.apply("cleanup")["modified"])

    def test_unknown_malformed_unexpected_and_locked_journals_are_preserved(self):
        unknown = write_journal(self.home, "unknown", basic_action(self.home))
        malformed = self.terminal("malformed")
        malformed.state_path.write_text("{broken", encoding="utf-8")
        unexpected = self.terminal("unexpected")
        (unexpected.directory / "keep.bin").write_bytes(b"recovery")
        locked = self.terminal("locked")
        locked.lock_path.write_text("stale lock", encoding="utf-8")
        before = self.snapshot(self.home)
        result = self.apply("cleanup")
        self.assertEqual(set(result["retained"]), {"unknown", "malformed", "unexpected", "locked"})
        self.assertEqual(before, self.snapshot(self.home))
        self.assertEqual(self.apply("migrate")["phase"], "blocked")
        self.assertEqual(before, self.snapshot(self.home))
        self.assertTrue(unknown.plan_path.exists())

    def test_cleanup_requires_current_exact_hash(self):
        store = self.terminal()
        plan = run_state_command("cleanup", self.home)
        with self.assertRaisesRegex(OperationStoreError, "approval"):
            run_state_command("cleanup", self.home, apply=True)
        store.append_event({"event": "verification_finished", "goal_status": "complete"})
        before = self.snapshot(self.home)
        with self.assertRaisesRegex(OperationStoreError, "changed"):
            run_state_command("cleanup", self.home, apply=True, approved_hash=plan["plan_sha256"])
        self.assertEqual(before, self.snapshot(self.home))

    def test_interrupted_compaction_finishes_from_durable_receipt(self):
        store = self.terminal()
        plan = run_state_command("cleanup", self.home)
        with patch.object(OperationStore, "_discard_detailed_files", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                run_state_command("cleanup", self.home, apply=True, approved_hash=plan["plan_sha256"])
        self.assertTrue(store.plan_path.exists())
        self.assertTrue(store.receipt_path.exists())
        self.apply("cleanup")
        self.assertEqual({p.name for p in store.directory.iterdir()}, {"receipt.json"})

    def test_migrate_preserves_all_journal_bytes_original_lock_and_recovery_locators(self):
        store = self.terminal()
        original_path = store.plan_path
        before = self.snapshot(store.directory)
        lock = store.directory.parent / ".mutation.lock"
        identity = lock.stat().st_ino
        preview = run_state_command("migrate", self.home)
        self.assertFalse((self.home / MANAGER_CONTROL).exists())
        result = self.apply("migrate")
        self.assertEqual(result["phase"], "complete")
        new = OperationStore(self.home, store.operation_id)
        self.assertEqual(self.snapshot(new.directory), before)
        self.assertEqual(new.directory.parents[1].name, MANAGER_CONTROL)
        self.assertEqual(lock.stat().st_ino, identity)
        for root in (self.home, original_path.parent, original_path.parent.parent,
                     original_path.parent.parent.parent):
            self.assertEqual(_find_operation_plan(store.operation_id, operation_home=root), new.plan_path)
        with self.assertRaisesRegex(OperationStoreError, "layout changed"):
            store.read_plan()
        self.assertFalse(self.apply("migrate")["modified"])
        self.assertEqual(preview["plan_sha256"], result["plan_sha256"])
        # Same lock still excludes cooperating writers after the journal move.
        with mutation_roots((self.home,)):
            script = "from pathlib import Path; from local_agent_manager.mutation_guard import mutation_roots; import sys\nwith mutation_roots((Path(sys.argv[1]),)): pass"
            process = subprocess.run([sys.executable, "-c", script, str(self.home)], capture_output=True, timeout=20)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"Another process owns", process.stderr)
        # The previous implementation rejects a regular marker during sibling scanning.
        from local_agent_manager.operation_store import _validate_directory
        marker = lock.parent / LAYOUT_MARKER
        with self.assertRaises(OperationStoreError):
            _validate_directory(marker, marker.lstat())

    def test_migrated_new_unknown_still_blocks_native_mutation(self):
        self.terminal()
        self.apply("migrate")
        unknown = write_journal(self.home, "new-unknown", basic_action(self.home))
        self.assertEqual(unknown.directory.parents[1].name, MANAGER_CONTROL)
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((MutationScope(self.home, frozenset(("synthetic-thread",))),)):
                self.fail("New layout must retain sibling admission")
        result = self.apply("cleanup")
        self.assertIn("new-unknown", result["retained"])
        self.assertTrue(unknown.plan_path.exists())

    def test_migration_interruption_blocks_writers_and_resumes_only_original_plan(self):
        stores = [self.terminal("one"), self.terminal("two")]
        plan = run_state_command("migrate", self.home)
        original = os.rename
        def interrupt(source, destination):
            if Path(source).name == "two":
                raise OSError("interrupted")
            original(source, destination)
        with patch("local_agent_manager.state_maintenance.os.rename", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "interrupted"):
                run_state_command("migrate", self.home, apply=True, approved_hash=plan["plan_sha256"])
        self.assertTrue((self.home / MANAGER_CONTROL / "operations" / "one" / "plan.json").exists())
        self.assertTrue(stores[1].plan_path.exists())
        self.assertEqual(run_state_command("inventory", self.home)["phase"], "moving")
        with self.assertRaisesRegex(OperationStoreError, "incomplete"):
            OperationStore(self.home, "new-writer").read_plan()
        with self.assertRaisesRegex(OperationStoreError, "approval"):
            run_state_command("migrate", self.home, apply=True, approved_hash="0" * 64)
        result = run_state_command("migrate", self.home, apply=True, approved_hash=plan["plan_sha256"])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(run_state_command("inventory", self.home)["operation_count"], 2)

    def test_migration_conflicting_destination_and_backup_evidence_are_preserved(self):
        store = self.terminal()
        rollback = self.home / LEGACY_CONTROL / "desktop-state-backups"
        rollback.mkdir()
        (rollback / "keep.db").write_bytes(b"recovery")
        self.assertEqual(self.apply("migrate")["phase"], "blocked")
        (rollback / "keep.db").unlink()
        rollback.rmdir()
        destination = self.home / MANAGER_CONTROL
        destination.mkdir()
        before = self.snapshot(store.directory)
        with self.assertRaisesRegex(OperationStoreError, "Unmarked"):
            self.apply("migrate")
        self.assertEqual(before, self.snapshot(store.directory))

    def test_historical_terminal_checkpoint_without_result_migrates_without_compaction(self):
        store = self.terminal()
        store.result_path.unlink()
        before = self.snapshot(store.directory)
        result = self.apply("cleanup")
        self.assertEqual(result["retained"], [store.operation_id])
        self.assertEqual(result["after"]["operations"][0]["reason"], "terminal_result_missing")
        self.assertEqual(self.apply("migrate")["phase"], "complete")
        migrated = OperationStore(self.home, store.operation_id)
        self.assertEqual(self.snapshot(migrated.directory), before)

    def test_missing_or_changed_moved_evidence_does_not_complete_migration(self):
        store = self.terminal()
        plan = run_state_command("migrate", self.home)
        with patch("local_agent_manager.state_maintenance.os.rename", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                run_state_command("migrate", self.home, apply=True, approved_hash=plan["plan_sha256"])
        store.events_path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(OperationStoreError, "evidence changed"):
            run_state_command("migrate", self.home, apply=True, approved_hash=plan["plan_sha256"])
        self.assertEqual(run_state_command("inventory", self.home)["phase"], "moving")

    def test_late_legacy_journal_is_not_ignored(self):
        self.terminal()
        self.apply("migrate")
        late = self.home / LEGACY_CONTROL / "operations" / "old-writer"
        late.mkdir()
        with self.assertRaisesRegex(OperationStoreError, "Legacy state changed"):
            OperationStore(self.home, "new-writer").read_plan()

    def test_os_lock_and_thread_lock_prevent_maintenance(self):
        self.terminal()
        lock = self.home / LEGACY_CONTROL / "operations" / ".mutation.lock"
        script = """import os, sys
from pathlib import Path
from local_agent_manager.mutation_guard import _os_lock
fd = os.open(Path(sys.argv[1]), os.O_RDWR)
_os_lock(fd)
print('locked', flush=True)
sys.stdin.readline()
os.close(fd)
"""
        process = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            for command in ("inventory", "cleanup", "migrate"):
                with self.assertRaises(MutationRootLockedError):
                    run_state_command(command, self.home)
        finally:
            process.communicate("done\n", timeout=20)

    def test_linked_journal_file_is_rejected_without_touching_target(self):
        store = self.terminal()
        target = self.root / "outside"
        target.write_bytes(store.result_path.read_bytes())
        store.result_path.unlink()
        os.link(target, store.result_path)
        before = target.read_bytes()
        self.assertIn(store.operation_id, self.apply("cleanup")["retained"])
        self.assertEqual(target.read_bytes(), before)

    def test_empty_rollback_containers_are_removable_and_index_lock_is_preserved(self):
        self.terminal()
        control = self.home / LEGACY_CONTROL
        for name in ("desktop-state-backups", "legacy-index-backups"):
            (control / name).mkdir()
        index_lock = control / "legacy-index.lock"
        index_lock.write_bytes(b"old owner metadata")
        identity = index_lock.stat().st_ino
        before = index_lock.read_bytes()
        result = self.apply("cleanup")
        self.assertEqual(set(result["removed_empty_directories"]), {"desktop-state-backups", "legacy-index-backups"})
        self.assertEqual(self.apply("migrate")["phase"], "complete")
        self.assertEqual(index_lock.stat().st_ino, identity)
        self.assertEqual(index_lock.read_bytes(), before)

    def test_old_index_writer_excludes_state_maintenance(self):
        self.terminal()
        index_lock = self.home / LEGACY_CONTROL / "legacy-index.lock"
        index_lock.write_bytes(b"old index lock")
        script = """import os, sys
from local_agent_manager.mutation_guard import _os_lock
fd = os.open(sys.argv[1], os.O_RDWR)
_os_lock(fd)
print('locked', flush=True)
sys.stdin.readline()
os.close(fd)
"""
        process = subprocess.Popen([sys.executable, "-c", script, str(index_lock)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(MutationRootLockedError, "index writer"):
                run_state_command("migrate", self.home)
        finally:
            process.communicate("done\n", timeout=20)

    def test_cli_and_compatibility_entry_points(self):
        self.terminal()
        for module in ("local_agent_manager", "local_agent_record_janitor"):
            process = subprocess.run([sys.executable, "-m", module, "state", "inventory", "--root", str(self.home), "--json"],
                                     text=True, capture_output=True, timeout=20, check=True)
            self.assertEqual(json.loads(process.stdout)["operation_count"], 1)
        output = StringIO()
        with redirect_stdout(output):
            code = main(["state", "cleanup", "--root", str(self.home), "--apply", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["phase"], "blocked")
        for name in ("state_paths", "state_maintenance"):
            self.assertIs(importlib.import_module("local_agent_manager." + name),
                          importlib.import_module("local_agent_record_janitor." + name))


if __name__ == "__main__":
    unittest.main()
