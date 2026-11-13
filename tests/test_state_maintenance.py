from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

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

from local_agent_manager.state_paths import manager_state_base
from local_agent_manager.agent_operations import result_document
from local_agent_manager.entrypoint import main
from local_agent_manager.mutation_guard import (
    MutationRootLockedError, MutationScope, UnknownMutationError, mutation_guard, mutation_roots,
)
from local_agent_manager.operation_coordinator import _find_operation_plan
from local_agent_manager.operation_store import OperationStore, OperationStoreError, _file_identity, _receipt_sha256
from local_agent_manager.state_maintenance import run_state_command
from local_agent_manager.state_paths import control_directory
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

    def test_state_lives_outside_the_store(self):
        store = self.terminal()
        base = manager_state_base()
        self.assertTrue(store.directory.is_relative_to(base))
        self.assertEqual(store.directory.parents[1], control_directory(self.home))
        self.assertTrue((control_directory(self.home) / "operations" / ".mutation.lock").is_file())
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(run_state_command("inventory", self.home)["historical_controls"], [])

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

    def test_os_lock_and_thread_lock_prevent_maintenance(self):
        self.terminal()
        lock = control_directory(self.home) / "operations" / ".mutation.lock"
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
            for command in ("inventory", "cleanup"):
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
        control = control_directory(self.home)
        for name in ("desktop-state-backups", "legacy-index-backups"):
            (control / name).mkdir()
        index_lock = control / "legacy-index.lock"
        index_lock.write_bytes(b"owner metadata")
        identity = index_lock.stat().st_ino
        before = index_lock.read_bytes()
        result = self.apply("cleanup")
        self.assertEqual(set(result["removed_empty_directories"]), {"desktop-state-backups", "legacy-index-backups"})
        self.assertEqual(index_lock.stat().st_ino, identity)
        self.assertEqual(index_lock.read_bytes(), before)

    def test_current_cli_entry_and_removed_surfaces(self):
        self.terminal()
        process = subprocess.run([sys.executable, "-m", "local_agent_manager", "state", "inventory", "--root", str(self.home), "--json"],
                                 text=True, capture_output=True, timeout=20, check=True)
        self.assertEqual(json.loads(process.stdout)["operation_count"], 1)
        process = subprocess.run([sys.executable, "-m", "local_agent_record_janitor", "--version"], text=True, capture_output=True, timeout=20)
        self.assertNotEqual(process.returncode, 0)
        for command in ("route", "migrate", "relocate"):
            with self.assertRaises(ValueError):
                run_state_command(command, self.home)
        output = StringIO()
        with redirect_stdout(output):
            code = main(["state", "cleanup", "--root", str(self.home), "--apply", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["phase"], "blocked")


if __name__ == "__main__":
    unittest.main()
