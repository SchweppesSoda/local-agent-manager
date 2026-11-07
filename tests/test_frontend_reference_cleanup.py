from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path

from local_agent_record_janitor.adapters.cindy import CindyAdapter
from local_agent_record_janitor.frontend_reference_cleanup import (
    FrontendReferenceError,
    execute_frontend_reference_cleanup,
    remove_top_level_json_field,
)
from local_agent_record_janitor.cli import main
from local_agent_record_janitor.cleaner import scan_adapters
from local_agent_record_janitor.planning import build_cleanup_plan

from tests.support import create_cindy_database


class FrontendReferenceCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve(strict=True)
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.binary = self.root / "codex.exe"


    def cindy_adapter(self, database: Path) -> CindyAdapter:
        return CindyAdapter(
            database=database,
            codex_home=self.codex_home,
            cindy_root=self.root,
            codex_bin_hint=self.binary,
        )

    def clean(self, evidence: list[dict[str, object]]) -> object:
        return execute_frontend_reference_cleanup(
            self.root,
            evidence,
            client_inspector=lambda _home: (),
            owner_client="cindy",
        )


    def test_cindy_current_cleanup_changes_only_sdk_session_id(self) -> None:
        database = self.root / "cindy-current.db"
        create_cindy_database(
            database,
            [
                {
                    "id": "deleted-session",
                    "sdk_session_id": "native-id",
                    "status": "deleted",
                    "source": "desktop",
                    "created_at": 1,
                    "updated_at": 2,
                    "parent_session_id": "parent",
                    "agent_kind": "codex",
                }
            ],
        )
        evidence = self.cindy_adapter(database).scan()[0].details[
            "frontend_reference"
        ]
        with closing(sqlite3.connect(database)) as connection:
            before = connection.execute("SELECT * FROM sessions").fetchone()

        result = self.clean([evidence])

        with closing(sqlite3.connect(database)) as connection:
            after = connection.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(after[0], before[0])
        self.assertIsNone(after[1])
        self.assertEqual(after[2:], before[2:])
        self.assertEqual(result.cleared_cindy_current_references, 1)

    def test_cindy_action_uses_exact_owner_identity_once(self) -> None:
        database = self.root / "cindy-owner.db"
        create_cindy_database(
            database,
            [
                {
                    "id": "deleted-session",
                    "sdk_session_id": "native-id",
                    "status": "deleted",
                    "agent_kind": "codex",
                }
            ],
        )
        adapter = self.cindy_adapter(database)
        preview = StringIO()
        self.assertEqual(
            main(
                ["clean", "--platform", "cindy", "--json"],
                adapters=[adapter],
                stdout=preview,
                stderr=StringIO(),
                client_inspector=lambda _root: (),
            ),
            0,
        )
        plan = json.loads(preview.getvalue())
        action = next(
            item
            for item in plan["actions"]
            if item["kind"] == "remove_frontend_reference"
        )
        self.assertEqual(action["impact"]["owner_client"], "cindy")
        self.assertEqual(
            Path(action["impact"]["owner_process_root"]),
            self.root,
        )
        inspections: list[tuple[Path, str | None]] = []

        def inspect(
            root: Path,
            *,
            owner_client: str | None = None,
        ) -> tuple[str, ...]:
            inspections.append((root, owner_client))
            return () if root == self.root and owner_client == "cindy" else (
                "ChatGPT.exe",
                "codex.exe",
            )

        output = StringIO()
        self.assertEqual(
            main(
                [
                    "clean",
                    "--platform",
                    "cindy",
                    "--action-id",
                    action["action_id"],
                    "--plan-fingerprint",
                    plan["plan_fingerprint"],
                    "--clients-closed",
                    "--yes",
                    "--json",
                ],
                adapters=[adapter],
                stdout=output,
                stderr=StringIO(),
                client_inspector=inspect,
            ),
            0,
        )
        self.assertEqual(inspections, [(self.root, "cindy")])
        self.assertEqual(
            json.loads(output.getvalue())["result"][
                "cleared_cindy_current_references"
            ],
            1,
        )

    def test_cindy_history_removes_only_json_field_and_preserves_row(self) -> None:
        database = self.root / "cindy-history.db"
        create_cindy_database(
            database,
            [
                {
                    "id": "deleted-session",
                    "sdk_session_id": None,
                    "status": "deleted",
                    "agent_kind": "pi",
                }
            ],
        )
        original_content = (
            '{"fromAgentKind":"codex", "fromSdkSessionId":"native-old", '
            '"note":{"keep":true}, "toAgentKind":"pi"}'
        )
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER,
                    rewind_at INTEGER,
                    untouched TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "switch-1",
                    "deleted-session",
                    "agent_switch",
                    original_content,
                    10,
                    11,
                    "preserve-me",
                ),
            )
            connection.commit()
        finding = next(
            item
            for item in self.cindy_adapter(database).scan()
            if item.thread_id == "native-old"
        )
        evidence = finding.details["frontend_reference"]
        with closing(sqlite3.connect(database)) as connection:
            before = connection.execute("SELECT * FROM messages").fetchone()

        result = self.clean([evidence])

        with closing(sqlite3.connect(database)) as connection:
            after = connection.execute("SELECT * FROM messages").fetchone()
        self.assertEqual(after[:3], before[:3])
        self.assertEqual(after[4:], before[4:])
        self.assertNotIn("fromSdkSessionId", json.loads(after[3]))
        self.assertEqual(json.loads(after[3])["note"], {"keep": True})
        self.assertEqual(result.cleaned_cindy_historical_references, 1)
        self.assertFalse(
            any(
                item.thread_id == "native-old"
                for item in self.cindy_adapter(database).scan()
            )
        )

    def test_cindy_batch_validates_every_row_before_first_write(self) -> None:
        database = self.root / "cindy-transaction.db"
        create_cindy_database(
            database,
            [
                {
                    "id": "one",
                    "sdk_session_id": "native-one",
                    "status": "deleted",
                    "agent_kind": "codex",
                },
                {
                    "id": "two",
                    "sdk_session_id": "native-two",
                    "status": "deleted",
                    "agent_kind": "codex",
                },
            ],
        )
        evidence = [
            item.details["frontend_reference"]
            for item in self.cindy_adapter(database).scan()
        ]
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE sessions SET source = 'drift' WHERE id = 'two'"
            )
            connection.commit()

        with self.assertRaises(FrontendReferenceError):
            self.clean(evidence)

        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                "SELECT id, sdk_session_id, source FROM sessions ORDER BY id"
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("one", "native-one", None),
                ("two", "native-two", "drift"),
            ],
        )

    def test_schema_change_and_running_client_block_cleanup(self) -> None:
        database = self.root / "cindy-schema.db"
        create_cindy_database(
            database,
            [
                {
                    "id": "one",
                    "sdk_session_id": "native-one",
                    "status": "deleted",
                    "agent_kind": "codex",
                }
            ],
        )
        evidence = self.cindy_adapter(database).scan()[0].details[
            "frontend_reference"
        ]
        with self.assertRaisesRegex(
            FrontendReferenceError,
            "still running",
        ):
            execute_frontend_reference_cleanup(
                self.root,
                [evidence],
                client_inspector=lambda _home: ("Cindy.exe",),
                owner_client="cindy",
            )
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("ALTER TABLE sessions ADD COLUMN added TEXT")
            connection.commit()
        with self.assertRaisesRegex(
            FrontendReferenceError,
            "schema changed",
        ):
            self.clean([evidence])

    def test_json_field_removal_preserves_other_member_text(self) -> None:
        original = '{ "a" : 1, "fromSdkSessionId" : "x", "b" : [2, 3] }'
        cleaned = remove_top_level_json_field(
            original,
            "fromSdkSessionId",
            expected_value="x",
        )
        self.assertEqual(cleaned, '{ "a" : 1, "b" : [2, 3] }')
        self.assertEqual(json.loads(cleaned), {"a": 1, "b": [2, 3]})

    def test_human_cli_executes_mapping_only_frontend_action(self) -> None:
        database = self.root / "cindy-cli.db"
        create_cindy_database(
            database,
            [
                {
                    'id': 'deleted',
                    'sdk_session_id': 'mapping-only',
                    'status': 'deleted',
                    'agent_kind': 'codex',
                },
            ],
        )
        adapter = self.cindy_adapter(database)
        preview = StringIO()
        code = main(
            ["clean", "--platform", "cindy", "--json"],
            adapters=[adapter],
            stdout=preview,
            stderr=StringIO(),
            client_inspector=lambda _home: (),
        )
        self.assertEqual(code, 0)
        document = json.loads(preview.getvalue())
        action = next(
            value
            for value in document["actions"]
            if value["kind"] == "remove_frontend_reference"
        )
        self.assertTrue(action["available"])

        output = StringIO()
        code = main(
            [
                "clean",
                "--platform",
                "cindy",
                "--action-id",
                action["action_id"],
                "--plan-fingerprint",
                document["plan_fingerprint"],
                "--clients-closed",
                "--yes",
                "--json",
            ],
            adapters=[adapter],
            stdout=output,
            stderr=StringIO(),
            app_server_factory=lambda **_kwargs: self.fail(
                "frontend cleanup must not start app-server"
            ),
            client_inspector=lambda _home: (),
        )

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["mutation_kind"], "remove_frontend_reference")
        self.assertEqual(result["result"]["removed_reference_count"], 1)

    def test_agent_plan_apply_verifies_mapping_only_frontend_action(self) -> None:
        database = self.root / "cindy-agent.db"
        create_cindy_database(
            database,
            [
                {
                    'id': 'deleted',
                    'sdk_session_id': 'agent-mapping-only',
                    'status': 'deleted',
                    'agent_kind': 'codex',
                },
            ],
        )
        adapter = self.cindy_adapter(database)
        plan_path = self.root / "frontend-plan.json"
        plan_output = StringIO()
        code = main(
            [
                "agent",
                "plan",
                "--operation",
                "purge",
                "--platform",
                "cindy",
                "--codex-home",
                str(self.codex_home),
                "--out",
                str(plan_path),
            ],
            adapters=[adapter],
            stdout=plan_output,
            stderr=StringIO(),
            client_inspector=lambda _home: (),
        )
        self.assertEqual(code, 0)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(
            plan["authorization"]["mutation_kind"],
            "remove_frontend_reference",
        )
        self.assertEqual(
            plan["authorization"]["root_actions"][0]["impact"]
            ["frontend_reference_evidence"][0]["platform"],
            "cindy",
        )

        output = StringIO()
        code = main(
            [
                "agent",
                "apply",
                "--plan",
                str(plan_path),
                "--authorized-plan-sha256",
                plan["plan_sha256"],
                "--clients-closed",
                "--verify-timeout",
                "0",
            ],
            adapters=[adapter],
            stdout=output,
            stderr=StringIO(),
            app_server_factory=lambda **_kwargs: self.fail(
                "frontend cleanup must not start app-server"
            ),
            client_inspector=lambda _home: (),
        )

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["goal_status"], "complete")
        self.assertTrue(result["mutation_started"])
        self.assertEqual(
            result["execution_result"]["result"]
            ["removed_reference_count"],
            1,
        )

    def test_same_native_id_in_two_frontend_databases_is_two_actions(self) -> None:
        databases = [self.root / "one.db", self.root / "two.db"]
        for database in databases:
            create_cindy_database(
                database,
                [
                    {
                        'id': f'deleted-{database.stem}',
                        'sdk_session_id': 'shared-native-id',
                        'status': 'deleted',
                        'agent_kind': 'codex',
                    },
                ],
            )
        report = scan_adapters(
            [self.cindy_adapter(database) for database in databases]
        )
        plan = build_cleanup_plan(report)
        actions = [
            action
            for action in plan.actions
            if str(action.kind.value) == "remove_frontend_reference"
        ]

        self.assertEqual(len(actions), 2)
        self.assertEqual(len({action.action_id for action in actions}), 2)
        self.assertEqual(
            {
                tuple(action.impact.frontend_database_paths)
                for action in actions
            },
            {(str(database.resolve()),) for database in databases},
        )


if __name__ == "__main__":
    unittest.main()
