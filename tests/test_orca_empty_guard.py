from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import orca_empty_guard as guard, orca_profile_cleanup as profile
from local_agent_record_janitor.adapters import CindyAdapter, OrcaAdapter
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.operation_guard_sources import guard_sources_for, required_source_errors
from tests.support import create_cindy_database


BACKUP = "profile-state.db.backup.1700000000000-11111111-1111-4111-8111-111111111111.db"


def create_empty_profile(root):
    directory = root / "profiles/local-default"
    directory.mkdir(parents=True)
    (root / "orca-profile-index.json").write_text(json.dumps({
        "schemaVersion": 1, "activeProfileId": "local-default", "profiles": [{"id": "local-default"}]}))
    state = {"schemaVersion": 1, "workspaceSession": {
        "tabsByWorktree": {"worktree": [{"id": "terminal", "worktreeId": "worktree", "shellOverride": "powershell.exe"}]},
        "unifiedTabs": {"worktree": [{"id": "tab", "entityId": "terminal", "worktreeId": "worktree", "groupId": "g", "contentType": "terminal"}]}},
        "workspaceSessionsByHostId": {}, "automations": [], "automationRuns": [], "settings": {"theme": "dark"}}
    for name in ("orca-data.json", "orca-data.json.bak.0", "orca-data.json.sqlite-export.7.json"):
        (directory / name).write_text(json.dumps(state))
    for name in ("profile-state.db", BACKUP):
        with closing(sqlite3.connect(directory / name)) as db:
            db.executescript("PRAGMA user_version=3;" + profile.DDL)
            db.executemany("INSERT INTO profile_state_meta VALUES (?,?)", [("profile_id", "local-default"), ("revision", "1")])
            db.execute("INSERT INTO profile_state_automation_runs_meta VALUES ('automationRuns','array',1,1,10,?)", (profile.sha("[]"),))
            for key, value in state.items():
                payload = json.dumps(value)
                db.execute("INSERT INTO profile_state_documents VALUES (?,?,1,1,10,?)", (key, payload, profile.sha(payload)))
            db.commit()
    hooks = root / "agent-hooks"
    hooks.mkdir()
    (hooks / "last-status.json").write_text('{"version":2,"entries":{},"authorityCommitments":{}}')
    return state


class OrcaEmptyGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "orca"
        self.state = create_empty_profile(self.root)
        self.directory = self.root / "profiles/local-default"
        environment = patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "ORCA_USER_DATA_PATH": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def write_state(self, value, filename="orca-data.json"):
        (self.directory / filename).write_text(json.dumps(value))

    def document(self, adapter):
        return {"schema_version": "larj.operation-plan.v2", "guard_sources": guard_sources_for((adapter,))}

    def test_registered_empty_profile_proves_absence_without_mutating_sources(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        adapter = OrcaAdapter(profile_root=self.root)
        self.assertTrue(guard.proves_no_sessions(self.root))
        self.assertEqual(required_source_errors(self.document(adapter), (adapter,)), ())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertTrue(adapter.snapshot_references().errors)  # Static inventory capability stays closed.

    def test_missing_journal_or_missing_profile_alone_is_not_absence_proof(self):
        empty = self.base / "unknown"
        empty.mkdir()
        self.assertFalse(guard.proves_no_sessions(empty))
        (self.directory / "profile-state.db").unlink()
        self.assertFalse(guard.proves_no_sessions(self.root))

    def test_each_json_restore_copy_blocks_on_session_state(self):
        for filename in ("orca-data.json", "orca-data.json.bak.0", "orca-data.json.sqlite-export.7.json"):
            with self.subTest(filename=filename):
                value = deepcopy(self.state)
                value["workspaceSession"]["sleepingAgentSessionsByPaneKey"] = {"pane": {"providerSession": {"id": "target"}}}
                self.write_state(value, filename)
                self.assertFalse(guard.proves_no_sessions(self.root))
                self.write_state(self.state, filename)

    def test_current_and_backup_database_payloads_and_schema_are_checked(self):
        for name in ("profile-state.db", BACKUP):
            with self.subTest(name=name), closing(sqlite3.connect(self.directory / name)) as db:
                payload = json.dumps([{"sessionId": "target"}])
                db.execute("UPDATE profile_state_documents SET payload=?,content_hash=? WHERE domain='automationRuns'", (payload, profile.sha(payload)))
                db.commit()
                self.assertFalse(guard.proves_no_sessions(self.root))
                db.execute("UPDATE profile_state_documents SET payload='[]',content_hash=? WHERE domain='automationRuns'", (profile.sha("[]"),))
                db.commit()
        with closing(sqlite3.connect(self.directory / BACKUP)) as db:
            db.execute("CREATE TABLE unknown_restore(id TEXT)")
            db.commit()
        self.assertFalse(guard.proves_no_sessions(self.root))

    def test_wal_committed_restore_state_is_not_hidden(self):
        with closing(sqlite3.connect(self.directory / "profile-state.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            payload = '["target"]'
            db.execute("INSERT INTO profile_state_documents VALUES ('claudeLivePtySessionIds',?,1,1,10,?)", (payload, profile.sha(payload)))
            db.commit()
            self.assertFalse(guard.proves_no_sessions(self.root))

    def test_unknown_domains_bindings_and_recovery_state_fail_closed(self):
        variants = [
            {"futureRecovery": {}}, {"automations": [{"id": "automation"}]},
            {"sshPtyConsumerRecoveries": [{"id": "target"}]},
            {"settings": {"nativeChatSessionOptions": {"target": {}}}},
            {"ui": {"future": {"providerSession": {"id": "target"}}}},
        ]
        for fields in variants:
            with self.subTest(fields=fields):
                self.write_state({**self.state, **fields})
                self.assertFalse(guard.proves_no_sessions(self.root))

    def test_nonterminal_and_unregistered_terminal_fields_fail_closed(self):
        for change in ({"contentType": "agent-session"}, {"aiVaultTitle": {"sessionId": "target"}}, {"resumeArgs": "target"}):
            value = deepcopy(self.state)
            value["workspaceSession"]["unifiedTabs"]["worktree"][0].update(change)
            self.write_state(value)
            self.assertFalse(guard.proves_no_sessions(self.root))

    def test_hook_namespace_status_authority_spool_and_unknown_files_block(self):
        namespace = self.root / "agent-hooks/com.stablyai.orca"
        namespace.mkdir()
        for name, value in (
            ("last-status.json", '{"version":2,"entries":{"pane":{}},"authorityCommitments":{}}'),
            ("last-status.json", '{"version":2,"entries":{},"authorityCommitments":{"pane":{}}}'),
            ("spool/event.jsonl", '{"paneKey":"pane"}\n'),
            ("future-state.json", '{}'),
        ):
            path = namespace / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(value)
            self.assertFalse(guard.proves_no_sessions(self.root))
            path.unlink()

    def test_journal_families_unknown_backups_and_profile_index_drift_block(self):
        for relative in ("agent-session-journal.db", "agent-session-journal.db-wal", "agent-session-journal/old", "profiles/local-default/orca-data.json.bak.5", "profiles/local-default/profile-state-corrupt-old.db"):
            path = self.root / relative
            path.parent.mkdir(exist_ok=True)
            path.touch()
            self.assertFalse(guard.proves_no_sessions(self.root))
            path.unlink()
            if relative.endswith("/old"):
                path.parent.rmdir()
        (self.root / "profiles/unindexed").mkdir()
        self.assertFalse(guard.proves_no_sessions(self.root))

    def test_source_replacement_and_changes_between_reads_block(self):
        original = guard._observe
        calls = 0
        def observe(root):
            nonlocal calls
            result = original(root)
            calls += 1
            if calls == 1:
                self.write_state({**self.state, "settings": {"theme": "light"}})
            return result
        with patch.object(guard, "_observe", side_effect=observe):
            self.assertFalse(guard.proves_no_sessions(self.root))
    def test_hardlinked_source_is_rejected(self):
        try:
            os.link(self.directory / "orca-data.json", self.base / "alias.json")
        except OSError as exc:
            self.skipTest(f"Hardlink creation unavailable: {type(exc).__name__}")
        self.assertFalse(guard.proves_no_sessions(self.root))

    def test_cached_snapshot_cannot_hide_new_recovery_source_or_runtime_error(self):
        adapter = OrcaAdapter(profile_root=self.root)
        document = self.document(adapter)
        self.assertEqual(required_source_errors(document, (adapter,)), ())
        self.write_state({**self.state, "automations": [{"id": "target"}]}, "orca-data.json.bak.1")
        self.assertTrue(required_source_errors(document, (adapter,)))
        (self.directory / "orca-data.json.bak.1").unlink()
        (self.root / "orca-runtime.json").write_text('{}')
        adapter.snapshot_references(refresh=True)
        self.assertIn("runtime_verification_not_covered", required_source_errors(document, (adapter,)))

    def test_cindy_operation_rechecks_frozen_guard_before_dispatch(self):
        database = self.base / "cindy.db"
        create_cindy_database(database, [{"id": "selected", "sdk_session_id": None, "status": "active", "agent_kind": "codex"}])
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, created_at INTEGER, rewind_at INTEGER)")
        adapter = CindyAdapter(database=database, codex_home=self.base / "codex-home", cindy_root=self.base)
        service = CleanupService(client_inspector=lambda *_: ())
        coordinator = OperationCoordinator(service)
        plan_path = self.base / "plan.json"
        plan = coordinator.plan_operation(client="cindy", record_ids=("selected",), adapters=(adapter,), orca_roots=(self.root,), plan_path=plan_path)
        self.assertEqual(plan["goal_status"], "ready", plan)
        frozen = plan_path.read_bytes()
        self.write_state({**self.state, "automations": [{"id": "target"}]}, "orca-data.json.bak.1")
        before = database.read_bytes()
        for runner in (coordinator, OperationCoordinator(service)):
            result = runner.apply_operation(operation_id=plan["operation_id"], plan_path=plan_path, clients_closed=True, adapters=(adapter,))
            self.assertEqual(result["goal_status"], "blocked", result)
            self.assertFalse(result["mutation_started"])
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(plan_path.read_bytes(), frozen)
        (self.directory / "orca-data.json.bak.1").unlink()
        result = OperationCoordinator(service).apply_operation(operation_id=plan["operation_id"], plan_path=plan_path, clients_closed=True, adapters=(adapter,))
        self.assertEqual(result["goal_status"], "complete", result)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone(), (0,))

    def test_multiple_pi_records_keep_one_context_after_guard_refresh(self):
        from tests.test_multi_engine_cli import write_pi_session
        database = self.base / "cindy.db"
        create_cindy_database(database, [])
        identifiers = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
        paths = [write_pi_session(self.base / "pi-agent-home/sessions", identifier, "PRIVATE") for identifier in identifiers]
        adapter = CindyAdapter(database=database, codex_home=self.base / "codex-home", cindy_root=self.base)
        service = CleanupService(client_inspector=lambda *_: ())
        plan_path = self.base / "pi-plan.json"
        plan = OperationCoordinator(service).plan_operation(client="cindy", record_ids=identifiers,
            adapters=(adapter,), orca_roots=(self.root,), plan_path=plan_path)
        self.assertEqual(plan["goal_status"], "ready", plan.get("blockers"))
        result = OperationCoordinator(service).apply_operation(operation_id=plan["operation_id"], plan_path=plan_path,
            clients_closed=True, adapters=(adapter,))
        self.assertEqual(result["goal_status"], "complete", result.get("blockers"))
        self.assertFalse(any(path.exists() for path in paths))
