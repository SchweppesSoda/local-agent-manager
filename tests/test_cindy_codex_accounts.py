from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from local_agent_manager.adapters import CindyAdapter, NativeIntegrityAdapter
from local_agent_manager.adapters.base import AdapterScanError
from local_agent_manager.cindy_codex_accounts import codex_account_bindings
from local_agent_manager.client_inventory import build_client_inventory
from local_agent_manager.inventory import build_session_catalog
from local_agent_manager.targeted_guard import TargetedReferenceGuard, TargetedGuardError
from local_agent_manager.frontend_reference_cleanup import FrontendReferenceError
from local_agent_manager.cleaner import _apply_live_reference_protection
from local_agent_manager.cleanup_service import CleanupService
from local_agent_manager.operation_coordinator import OperationCoordinator
from tests.support import create_cindy_database, create_thread_index, write_rollout


class CindyCodexAccountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.profile = self.root / "CindyGlobal"
        self.profile.mkdir()
        self.legacy = self.profile / "codex-home"
        self.legacy.mkdir()
        self.user = self.root / "user"
        self.user.mkdir()
        self.owner = "owner-fixture"
        self.digest = hashlib.sha256(self.owner.encode()).hexdigest()
        self.database = self.profile / f"cindy-{self.owner}.db"
        self.config = self.profile / "owners" / self.digest[:20] / "subscription-accounts.json"
        self.config.parent.mkdir(parents=True)
        self.account_id = f"cindy-{self.digest[:12]}-account-fixture"
        patcher = patch("local_agent_manager.cindy_codex_accounts.Path.home", return_value=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_bindings(self, bindings):
        self.config.write_text(json.dumps({"pools": {}, "bindings": bindings}), encoding="utf-8")

    def binding(self, account_id):
        return {"providerId": "openai", "accountId": account_id}

    def adapter(self):
        return CindyAdapter(database=self.database, codex_home=self.legacy, cindy_root=self.profile)

    def test_exact_native_binding_overrides_current_task_account(self):
        self.write_bindings({
            "openai:task:task": self.binding(self.account_id),
            "openai:native:historical": self.binding("default"),
        })
        self.assertEqual(codex_account_bindings(self.database, self.profile),
                         {"historical": self.user / ".codex"})

    def test_legacy_default_flag_preserves_original_cindy_home(self):
        self.config.write_text(json.dumps({
            "pools": {"openai": {"legacyCodexDefault": True}},
            "bindings": {"openai:native:thread": self.binding("default")},
        }), encoding="utf-8")
        self.assertEqual(codex_account_bindings(self.database, self.profile), {"thread": self.legacy})

    def test_registered_accounts_keep_distinct_native_homes(self):
        self.write_bindings({"openai:native:system": self.binding("default"),
                             "openai:native:independent": self.binding(self.account_id)})
        create_cindy_database(self.database, [
            {"id": "one", "sdk_session_id": "system", "agent_kind": "codex", "status": "active"},
            {"id": "two", "sdk_session_id": "independent", "agent_kind": "codex", "status": "active"},
        ])
        rows = self.adapter().list_sessions()
        self.assertEqual({row.thread_id: row.codex_home for row in rows},
                         {"system": self.user / ".codex",
                          "independent": self.user / f".codex-{self.account_id}"})

    def test_account_bound_parent_keeps_its_complete_native_cascade(self):
        self.write_bindings({"openai:native:parent": self.binding(self.account_id)})
        create_cindy_database(self.database, [
            {"id": "task", "sdk_session_id": "parent", "agent_kind": "codex", "status": "active"},
        ])
        home = self.user / f".codex-{self.account_id}"
        rows = []
        for thread_id, parent in (("parent", None), ("child", "parent"),
                                  ("grandchild", "child"), ("unrelated", None)):
            source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "app-server"
            path = write_rollout(home, thread_id, originator="cindy", source=source)
            rows.append({"id": thread_id, "rollout_path": str(path), "source": json.dumps(source)})
        create_thread_index(home, rows)

        adapter = self.adapter()
        inventory = build_client_inventory((adapter,), client="cindy", engines=("codex",))
        self.assertEqual({record.thread_id for record in inventory.records}, {"parent", "child", "grandchild"})
        plan = OperationCoordinator(CleanupService()).plan_operation(
            client="cindy", engines=("codex",), record_ids=("parent",),
            adapters=(adapter,), plan_path=self.root / "cascade-plan.json",
        )
        self.assertEqual(plan["goal_status"], "ready", plan)
        native, reference = plan["actions"]
        self.assertEqual(native["kind"], "delete_conversation")
        self.assertEqual(set(native["impact"]["affected_thread_ids"]), {"parent", "child", "grandchild"})
        self.assertEqual(reference["kind"], "remove_frontend_reference")

    def test_off_home_guard_protects_official_native_thread(self):
        self.write_bindings({"openai:native:thread": self.binding("default")})
        create_cindy_database(self.database, [
            {"id": "task", "sdk_session_id": "thread", "agent_kind": "codex", "status": "active"},
        ])
        home = self.user / ".codex"
        rollout = write_rollout(home, "thread", originator="cindy")
        create_thread_index(home, [{"id": "thread", "rollout_path": str(rollout)}])
        catalog = build_session_catalog([NativeIntegrityAdapter(codex_home=home)],
                                        guard_adapters=(self.adapter(),))
        self.assertEqual(len(catalog.records), 1)
        record = catalog.records[0]
        self.assertEqual(record.codex_home, home)
        self.assertEqual([row.platform_session_id for row in record.frontend_sessions], ["task"])
        self.assertIn("live_frontend_reference", record.blocker_codes)
        self.assertFalse(record.deletable)
        self.assertNotIn(self.legacy, catalog.scanned_native_homes)
        plan = OperationCoordinator(CleanupService()).plan_operation(
            client="native", record_ids=("thread",), codex_home=home,
            adapters=(NativeIntegrityAdapter(codex_home=home), self.adapter()),
            plan_path=self.root / "native-plan.json")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual({action["kind"] for action in plan["actions"]},
                         {"delete_conversation", "remove_frontend_reference"})
        self.legacy.rmdir()
        self.assertEqual(self.adapter().scan(), [])

    def test_execution_guard_uses_actual_account_home_and_exact_closure(self):
        self.write_bindings({"openai:native:thread": self.binding("default")})
        create_cindy_database(self.database, [
            {"id": "task", "sdk_session_id": "thread", "agent_kind": "codex", "status": "active"},
        ])
        adapter = self.adapter()
        home = self.user / ".codex"
        home.mkdir()
        guard = TargetedReferenceGuard((adapter,), {})
        with self.assertRaises(TargetedGuardError):
            guard._check_home(home, frozenset({"thread"}))
        approved = (adapter.list_sessions()[0].details["frontend_reference"],)
        guard._check_home(home, frozenset({"thread"}), approved_references=approved)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO sessions (id,sdk_session_id,agent_kind,status) VALUES ('new','thread','codex','active')")
            connection.commit()
        with self.assertRaises((TargetedGuardError, FrontendReferenceError)):
            guard._check_home(home, frozenset({"thread"}), approved_references=approved)

    def test_guard_returns_structured_failure_for_broken_account_registry(self):
        self.config.write_text("invalid", encoding="utf-8")
        create_cindy_database(self.database, [])
        findings, failures = _apply_live_reference_protection([], [self.adapter()])
        self.assertEqual(findings, [])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].platform, "live-reference-guard")

    def test_native_source_plan_keeps_same_id_reference_in_legacy_home(self):
        self.config.write_text(json.dumps({
            "pools": {"openai": {"legacyCodexDefault": True}},
            "bindings": {"openai:native:thread": self.binding("default")},
        }), encoding="utf-8")
        create_cindy_database(self.database, [
            {"id": "retained-task", "sdk_session_id": "thread", "agent_kind": "codex", "status": "active"},
        ])
        official = self.user / ".codex"
        paths = {}
        for home in (official, self.legacy):
            rollout = write_rollout(home, "thread", originator="cindy")
            create_thread_index(home, [{"id": "thread", "rollout_path": str(rollout)}])
            paths[home] = rollout
        preserved = {p: p.read_bytes() for p in (paths[self.legacy], self.legacy / "state_5.sqlite", self.database, self.config)}
        adapter = self.adapter()
        service = CleanupService(client_inspector=lambda *_: ())
        plan = OperationCoordinator(service).plan_operation(
            client="native", record_ids=("thread",), codex_home=official,
            adapters=(NativeIntegrityAdapter(codex_home=official), adapter),
            plan_path=self.root / "source-only-plan.json")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual({action["kind"] for action in plan["actions"]}, {"delete_conversation"})
        self.assertEqual(adapter.list_sessions()[0].codex_home, self.legacy)
        # The execution-time guard also qualifies physical home, not ID alone.
        TargetedReferenceGuard((adapter,), {})._check_home(official, frozenset({"thread"}))
        with self.assertRaises(TargetedGuardError):
            TargetedReferenceGuard((adapter,), {})._check_home(self.legacy, frozenset({"thread"}))
        calls = []
        def server(**kwargs):
            self.assertEqual(Path(kwargs["codex_home"]), official)
            class Server:
                def __enter__(inner): return inner
                def __exit__(inner, *_): pass
                def delete_thread(inner, ident):
                    calls.append(ident)
                    with closing(sqlite3.connect(official / "state_5.sqlite")) as c:
                        c.execute("delete from threads where id=?", (ident,)); c.commit()
                    paths[official].unlink()
            return Server()
        result = OperationCoordinator(service).apply_operation(
            plan_path=Path(plan["plan_path"]), adapters=(NativeIntegrityAdapter(codex_home=official), adapter),
            clients_closed=True, app_server_factory=server, binary_resolver=lambda _: self.root / "fixture-codex")
        self.assertEqual(result["goal_status"], "complete", result)
        cold = OperationCoordinator(service)
        status = cold.status_operation(plan_path=Path(plan["plan_path"]))
        self.assertEqual(status["goal_status"], "complete", status)
        verified = cold.verify_operation(plan_path=Path(plan["plan_path"]),
            adapters=(NativeIntegrityAdapter(codex_home=official), adapter))
        self.assertEqual(verified["goal_status"], "complete", verified)
        self.assertEqual(calls, ["thread"])
        self.assertFalse(paths[official].exists())
        for path, before in preserved.items(): self.assertEqual(path.read_bytes(), before, str(path))

    def test_missing_registry_preserves_dedicated_home(self):
        self.assertEqual(codex_account_bindings(self.database, self.profile), {})

    def test_task_binding_does_not_reassign_historical_native_id(self):
        self.write_bindings({"openai:task:task": self.binding("default")})
        self.assertEqual(codex_account_bindings(self.database, self.profile), {})

    def test_foreign_owner_account_and_invalid_registry_fail_closed(self):
        create_cindy_database(self.database, [])
        self.write_bindings({"openai:native:thread": self.binding("cindy-another-owner-account")})
        with self.assertRaises(AdapterScanError):
            self.adapter().list_sessions()
        self.config.write_text('{"bindings": {}, "bindings": {}}', encoding="utf-8")
        with self.assertRaises(AdapterScanError):
            self.adapter().describe_client()


if __name__ == "__main__":
    unittest.main()
