from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

import hashlib
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_agent_manager.cindy_isolation_plan import (
    IsolationPlanError, binding_changes, closure_batches,
    recovery_action, recovery_report, registry_patch, readonly_sqlite, legacy_destination,
)
from local_agent_manager.mutation_guard import MutationScope, mutation_guard, UnknownMutationError
from local_agent_manager.operation_store import OperationStore, plan_sha256


class CindyIsolationPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.owner = "fixture-owner"
        self.account = "cindy-" + hashlib.sha256(self.owner.encode()).hexdigest()[:12] + "-11111111-2222-4333-8444-555555555555"
        self.home = self.root / (".codex-" + self.account)
        self.home.mkdir()
        self.source = self.root / ".codex"
        self.source.mkdir()
        self.registry = {"pools": {"openai": {"accounts": [{"id": "default"}, {"id": self.account}]}},
                         "bindings": {"openai:task:task": {"providerId": "openai", "accountId": "default"}}}

    def test_existing_legacy_default_requires_real_home_and_preserves_other_accounts(self):
        profile = self.root / "profile"
        legacy = profile / "codex-home"
        legacy.mkdir(parents=True)
        before = json.loads(json.dumps(self.registry))
        result = legacy_destination(self.registry, profile_root=profile, home=legacy, official_home=self.source)
        self.assertEqual(result["account_id"], "default")
        self.assertEqual(result["route_change"], {"before_present": False, "before": None, "after": True})
        self.assertFalse(result["new_login_required"])
        self.assertEqual(self.registry, before)
        changes = binding_changes(self.registry, task_ids=["task"], native_ids=["native"],
                                  account_id="default", legacy_default=True, protected_keys=set())
        self.assertTrue(all(c["after"]["accountId"] == "default" for c in changes))
        with self.assertRaises(IsolationPlanError):
            legacy_destination(self.registry, profile_root=profile, home=self.home, official_home=self.source)

    def test_transitive_shared_children_and_cycle_are_never_split(self):
        roots = {"r1": ["t1"], "r2": ["t2"], "r3": ["t3"], "r4": ["t4"]}
        batches = closure_batches(roots, [("r1", "x"), ("r2", "y"), ("r3", "x"),
                                         ("r3", "y"), ("x", "r1")], protected_ids=set(), max_roots=1)
        self.assertEqual(batches[0]["root_ids"], ["r1", "r2", "r3"])
        self.assertEqual(batches[1]["native_ids"], ["r4"])
        self.assertEqual(len(set(batches[0]["native_ids"]) & set(batches[1]["native_ids"])), 0)

    def test_protected_descendant_blocks_even_if_root_is_selected(self):
        with self.assertRaisesRegex(IsolationPlanError, "protected_native"):
            closure_batches({"root": ["task"]}, [("root", "child")], protected_ids={"child"})

    def changes(self):
        return binding_changes(self.registry, task_ids=["task"], native_ids=["native"],
                               account_id=self.account, protected_keys=set())

    def test_field_rollback_keeps_new_tasks_accounts_and_configuration(self):
        changes = self.changes()
        switched = registry_patch(self.registry, changes)
        switched["bindings"]["openai:task:new"] = {"providerId": "openai", "accountId": self.account}
        switched["pools"]["openai"]["strategy"] = "round-robin"
        after = registry_patch(switched, changes, rollback=True)
        self.assertEqual(after["bindings"]["openai:task:task"]["accountId"], "default")
        self.assertNotIn("openai:native:native", after["bindings"])
        self.assertIn("openai:task:new", after["bindings"])
        self.assertEqual(after["pools"]["openai"]["strategy"], "round-robin")
        self.assertNotIn("openai:native:native", self.registry["bindings"])

    def test_conflicting_selected_binding_blocks_entire_patch(self):
        changes = self.changes()
        current = json.loads(json.dumps(self.registry))
        current["bindings"]["openai:task:task"]["accountId"] = "other-account"
        before = json.loads(json.dumps(current))
        with self.assertRaises(IsolationPlanError):
            registry_patch(current, changes)
        self.assertEqual(current, before)

    def test_legacy_binding_is_frozen_and_protected_bindings_block(self):
        self.registry["bindings"]["task:task"] = {"providerId": "openai", "accountId": "default"}
        self.assertIn("task:task", [c["key"] for c in self.changes()])
        with self.assertRaises(IsolationPlanError):
            binding_changes(self.registry, task_ids=["task"], native_ids=["native"],
                            account_id=self.account, protected_keys={"openai:native:native"})

    def recovery(self, **kwargs):
        return recovery_action(phase=kwargs.pop("phase", "switched"), copied_verified=True,
                               switched_verified=True, acceptance_verified=kwargs.pop("acceptance_verified", False),
                               target_at_switch={"thread": "before"}, target_now=kwargs.pop("target_now", {"thread": "before"}), **kwargs)

    def test_unknown_write_never_replays_and_preserves_previous_batches(self):
        for phase in ("copying", "switching"):
            self.assertEqual(self.recovery(phase=phase)["action"], "status_verify_no_replay")
        self.assertEqual(self.recovery()["action"], "exact_binding_rollback_candidate")

    def test_new_target_history_prevents_rollback_and_cleanup(self):
        result = self.recovery(phase="accepted", acceptance_verified=True, target_now={"thread": "new message"})
        self.assertEqual(result["action"], "preserve_target_increment_reconcile")
        self.assertFalse(result["source_cleanup_allowed"])
        self.assertFalse(self.recovery(phase="accepted")["source_cleanup_allowed"])
        self.assertTrue(self.recovery(phase="accepted", acceptance_verified=True)["source_cleanup_allowed"])

    def test_wal_snapshot_reads_committed_rows_without_checkpoint_or_write(self):
        path = self.root / "fixture.sqlite"
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("pragma journal_mode=wal")
            writer.execute("create table threads(id text primary key)")
            writer.commit()
            writer.execute("insert into threads values('committed-wal')")
            writer.commit()
            wal = Path(str(path) + "-wal")
            before = wal.read_bytes()
            with readonly_sqlite(path) as reader:
                self.assertEqual(reader.execute("select id from threads").fetchone()[0], "committed-wal")
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("delete from threads")
            self.assertEqual(wal.read_bytes(), before)

    def test_nonempty_wal_without_shm_is_never_read_as_immutable(self):
        path = self.root / "fixture.sqlite"
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("create table threads(id text)")
            writer.commit()
        Path(str(path) + "-wal").write_bytes(b"unqualified WAL")
        with self.assertRaisesRegex(IsolationPlanError, "sidecars"):
            with readonly_sqlite(path):
                self.fail("must not open")

    def test_interrupted_batch_report_retains_completed_siblings_and_mutations(self):
        state = {"phase": "accepted", "copied_verified": True, "switched_verified": True,
                 "acceptance_verified": True, "target_at_switch": {"one": "hash"}, "target_now": {"one": "hash"}}
        result = recovery_report([
            {"batch_id": "one", "state": state, "mutations": [{"kind": "copy", "count": 3}]},
            {"batch_id": "two", "state": {**state, "phase": "switching"}, "mutations": [{"kind": "binding", "count": 1}]},
        ])
        self.assertEqual(result["completed_batches"], ["one"])
        self.assertEqual([m["count"] for m in result["cumulative_mutations"]], [3, 1])
        self.assertEqual(result["batches"][1]["action"], "status_verify_no_replay")

    def test_unknown_import_journals_block_each_involved_physical_root(self):
        for name in ('profile','source','target'):
            root=self.root/name;root.mkdir()
            store=OperationStore(root,'fixture-cindy-isolation')
            plan={'schema_version':'larj.agent-plan.v1','operation_id':store.operation_id,
                  'target':{'codex_home':str(root)},
                  'authorization':{'root_actions':[{'kind':'cindy_legacy_home_import'}]}}
            plan['plan_sha256']=plan_sha256(plan)
            store.accept_plan(plan)
            store.write_state({'schema_version':'larj.agent-state.v1','operation_id':store.operation_id,
                               'plan_sha256':plan['plan_sha256'],'phase':'executing','goal_status':'unknown',
                               'goal_satisfied':False,'modified':True,'mutation_started':True})
            store.append_event({'event':'mutation_started','goal_status':'unknown'})
            with self.subTest(root=name), self.assertRaises(UnknownMutationError):
                with mutation_guard((MutationScope(root,frozenset({'other-record'})),)):
                    self.fail('unknown import must not admit a sibling')


if __name__ == "__main__":
    unittest.main()
