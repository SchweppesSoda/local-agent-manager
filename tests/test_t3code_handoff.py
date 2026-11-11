from __future__ import annotations

from contextlib import closing
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager.cli import main
from local_agent_manager.operation_store import strict_json_load, write_new_json
from local_agent_manager.t3code_handoff import (
    HANDOFF_RECEIPT_SCHEMA,
    HANDOFF_SPEC_SCHEMA,
    run_handoff_worker,
)
from tests.orca_native_support import add_native_record, create_native_schema
from tests.t3code_support import V2, add_thread, create_store, insert

TARGET_THREAD = "11111111-1111-4111-8111-111111111111"
CHILD_THREAD = "22222222-2222-4222-8222-222222222222"
KEEP_THREAD = "33333333-3333-4333-8333-333333333333"
TARGET_NATIVE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CHILD_NATIVE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
KEEP_NATIVE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


@unittest.skipUnless(os.name == "nt", "Windows popup handoff workflow")
class T3CodeHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "t3"
        self.home = self.base / "codex-home"
        self.home.mkdir()
        self.localappdata = self.base / "localappdata"
        self.localappdata.mkdir()
        self.appdata = self.base / "appdata"
        self.appdata.mkdir()

        create_native_schema(self.home)
        add_native_record(self.home, TARGET_NATIVE)
        add_native_record(self.home, CHILD_NATIVE)
        add_native_record(self.home, KEEP_NATIVE)

        with closing(create_store(self.root)) as db:
            add_thread(db, TARGET_THREAD, native=TARGET_NATIVE)
            add_thread(db, CHILD_THREAD, native=CHILD_NATIVE)
            add_thread(db, KEEP_THREAD, native=KEEP_NATIVE)
            insert(
                db,
                V2 + "subagents",
                subagent_id="sub-1",
                thread_id=TARGET_THREAD,
                child_thread_id=CHILD_THREAD,
                provider="codex",
                provider_instance_id="codex",
                status="completed",
                payload_json=json.dumps({"childThreadId": CHILD_THREAD}),
            )
            db.commit()

        (self.root / "userdata/environment-id").write_text("env-handoff-test", encoding="utf-8")
        self.install = self.base / "t3-install"
        self.install.mkdir()
        self.t3_exe = self.install / "T3 Code (Nightly).exe"
        self.t3_exe.write_bytes(b"fixture-t3-exe")
        self.codex_exe = self.base / "codex.exe"
        self.codex_exe.write_bytes(b"fixture-codex-exe")
        settings = {
            "providerInstances": {
                "codex": {
                    "driver": "codex",
                    "config": {"homePath": str(self.home), "binaryPath": str(self.codex_exe)},
                }
            }
        }
        (self.root / "userdata/settings.json").write_text(json.dumps(settings), encoding="utf-8")

        for patcher in (
            patch("local_agent_manager.t3code_server_delete._bundled_install", return_value=self.install),
            patch("local_agent_manager.t3code_handoff._bundled_install", return_value=self.install),
            patch.dict(
                os.environ,
                {
                    "APPDATA": str(self.appdata),
                    "LOCALAPPDATA": str(self.localappdata),
                    "T3CODE_HOME": str(self.root),
                },
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cli_validation_rejects_invalid_handoff_combinations(self) -> None:
        cases = [
            ["delete", "run", "--client", "native", "--record-id", TARGET_NATIVE, "--handoff", "--json"],
            ["delete", "run", "--client", "t3code", "--t3code-only", "--record-id", TARGET_THREAD, "--handoff", "--json"],
            ["delete", "run", "--client", "t3code", "--all-projects", "--handoff", "--json"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                out, err = StringIO(), StringIO()
                code = main(argv, stdout=out, stderr=err)
                self.assertNotEqual(code, 0)

    def test_notify_thread_inside_expanded_cascade_is_rejected(self) -> None:
        out, err = StringIO(), StringIO()
        code = main(
            [
                "delete",
                "run",
                "--client",
                "t3code",
                "--record-id",
                TARGET_THREAD,
                "--handoff",
                "--notify-thread-id",
                CHILD_THREAD,
                "--json",
            ],
            stdout=out,
            stderr=err,
        )
        self.assertEqual(code, 3)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["blockers"][0]["blocker_code"], "notify_thread_in_deletion_scope")

    def test_isolated_desktop_returns_structured_blocker_without_hanging(self) -> None:
        with (
            patch("local_agent_manager.t3code_handoff._qualify_lifecycle_targets", return_value=[]),
            patch(
                "local_agent_manager.t3code_handoff.windows_confirmation",
                side_effect=RuntimeError("interactive Windows desktop unavailable (WinSta0\\exebox-test)"),
            ),
        ):
            out, err = StringIO(), StringIO()
            code = main(
                [
                    "delete",
                    "run",
                    "--client",
                    "t3code",
                    "--record-id",
                    TARGET_THREAD,
                    "--handoff",
                    "--json",
                ],
                stdout=out,
                stderr=err,
            )
        self.assertEqual(code, 3)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["blockers"][0]["blocker_code"], "interactive_desktop_unavailable")

    def test_cancel_declines_without_creating_handoff_files_and_confirm_starts_worker(self) -> None:
        fake_targets = [
            {
                "software": "T3 Code Desktop",
                "pid": 40001,
                "created": "2026-11-11T08:00:00+00:00",
                "created_unix": 1794384000.0,
                "executable": str(self.t3_exe),
                "home": str(self.root),
                "affected_tasks": ("清理测试会话",),
                "reopen": True,
                "hold_executable": False,
                "lifecycle_target": __import__(
                    "local_agent_manager.lifecycle_confirmation", fromlist=["LifecycleTarget"]
                ).LifecycleTarget(
                    software="T3 Code Desktop",
                    pid=40001,
                    created="2026-11-11T08:00:00+00:00",
                    executable=str(self.t3_exe),
                    home=str(self.root),
                    affected_tasks=("清理测试会话",),
                ),
            }
        ]
        seen_prompts: list[str] = []

        with (
            patch("local_agent_manager.t3code_handoff._qualify_lifecycle_targets", return_value=fake_targets),
            patch("local_agent_manager.t3code_handoff.windows_confirmation", side_effect=lambda msg: (seen_prompts.append(msg), False)[1]),
        ):
            out, err = StringIO(), StringIO()
            code = main(
                [
                    "delete",
                    "run",
                    "--client",
                    "t3code",
                    "--record-id",
                    TARGET_THREAD,
                    "--handoff",
                    "--notify-thread-id",
                    KEEP_THREAD,
                    "--json",
                ],
                stdout=out,
                stderr=err,
            )
        self.assertEqual(code, 3)
        declined = json.loads(out.getvalue())
        self.assertEqual(declined["blockers"][0]["blocker_code"], "user_confirmation_declined")
        self.assertFalse((self.localappdata / "local-agent-manager/handoffs").exists())
        self.assertEqual(len(seen_prompts), 1)
        self.assertIn("T3 Code Desktop: PID 40001", seen_prompts[0])

        spawned_specs: list[Path] = []
        with (
            patch("local_agent_manager.t3code_handoff._qualify_lifecycle_targets", return_value=fake_targets),
            patch("local_agent_manager.t3code_handoff.windows_confirmation", return_value=True),
            patch("local_agent_manager.t3code_handoff._spawn_detached_worker", side_effect=lambda p: (spawned_specs.append(p), 54321)[1]),
        ):
            out2, err2 = StringIO(), StringIO()
            code2 = main(
                [
                    "delete",
                    "run",
                    "--client",
                    "t3code",
                    "--record-id",
                    TARGET_THREAD,
                    "--handoff",
                    "--notify-thread-id",
                    KEEP_THREAD,
                    "--json",
                ],
                stdout=out2,
                stderr=err2,
            )
        self.assertEqual(code2, 0, out2.getvalue())
        started = json.loads(out2.getvalue())
        self.assertEqual(started["status"], "handoff_started")
        self.assertEqual(started["worker_pid"], 54321)
        self.assertEqual(started["expanded_thread_count"], 2)
        self.assertEqual(len(spawned_specs), 1)
        spec = strict_json_load(spawned_specs[0])
        self.assertEqual(spec["schema_version"], HANDOFF_SPEC_SCHEMA)
        self.assertEqual(spec["expanded_thread_ids"], sorted([TARGET_THREAD, CHILD_THREAD]))

    def test_worker_orchestrates_cleanup_reopen_and_single_notification(self) -> None:
        handoff_dir = self.localappdata / "local-agent-manager/handoffs/test-worker-1"
        handoff_dir.mkdir(parents=True)
        receipt_path = handoff_dir / "receipt.json"
        cindy_exe = self.base / "Cindy.exe"
        cindy_exe.write_bytes(b"fixture-cindy-exe")
        spec_path = handoff_dir / "spec.json"
        expanded = sorted([TARGET_THREAD, CHILD_THREAD])
        write_new_json(
            spec_path,
            {
                "schema_version": HANDOFF_SPEC_SCHEMA,
                "handoff_id": "test-worker-1",
                "created_at": "2026-11-11T08:00:00+00:00",
                "t3code_root": str(self.root),
                "t3code_install": str(self.install),
                "record_ids": [TARGET_THREAD],
                "expanded_thread_ids": expanded,
                "native_record_count": 2,
                "notify_thread_id": KEEP_THREAD,
                "timeout": 30.0,
                "quiesce_timeout": 10.0,
                "handoff_dir": str(handoff_dir),
                "receipt_path": str(receipt_path),
                "cli_forwards": [],
                "recoverable_preflight_children": [
                    {"operation_id": "child-lock-1", "codex_home": str(self.home)}
                ],
                "confirmed_targets": [
                    {
                        "software": "T3 Code Desktop",
                        "pid": 40001,
                        "created": "2026-11-11T08:00:00+00:00",
                        "created_unix": 1794384000.0,
                        "executable": str(self.t3_exe),
                        "home": str(self.root),
                        "affected_tasks": ["t3"],
                        "reopen": True,
                        "hold_executable": False,
                    },
                    {
                        "software": "Cindy Desktop",
                        "pid": 40002,
                        "created": "2026-11-11T08:00:00+00:00",
                        "created_unix": 1794384000.0,
                        "executable": str(cindy_exe),
                        "home": str(self.home),
                        "affected_tasks": ["cindy"],
                        "reopen": True,
                        "hold_executable": True,
                    },
                ],
            },
        )

        call_order: list[str] = []

        def fake_cli(argv, *, coordinator=None):
            del coordinator
            if argv[:2] == ["operation", "verify"] and "child-lock-1" in argv:
                call_order.append("settle_child")
                return 3, {"operation_id": "child-lock-1", "goal_status": "completed_with_residuals"}
            if argv[:2] == ["delete", "plan"]:
                call_order.append("plan")
                plan_out = Path(argv[argv.index("--out") + 1])
                write_new_json(
                    plan_out,
                    {
                        "actions": [
                            {
                                "kind": "delete_t3code_frontend",
                                "impact": {
                                    "external_action_payload": {
                                        "t3code_evidence": {
                                            "schema_version": "larj.t3code-cleanup.v1",
                                            "selected_thread_ids": expanded,
                                        }
                                    }
                                },
                            }
                        ]
                    },
                )
                return 0, {
                    "operation_id": "op-clean-1",
                    "plan_path": str(plan_out),
                    "plan_sha256": "a" * 64,
                    "goal_status": "ready",
                }
            if argv[:2] == ["delete", "apply"]:
                call_order.append("apply")
                return 0, {"operation_id": "op-clean-1", "goal_status": "complete", "goal_satisfied": True}
            if argv[:2] == ["operation", "verify"]:
                call_order.append("cold_verify")
                return 0, {"operation_id": "op-clean-1", "goal_status": "complete", "goal_satisfied": True}
            raise AssertionError(f"Unexpected CLI call: {argv}")

        with (
            patch("local_agent_manager.t3code_handoff._wait_for_t3_quiescent", side_effect=lambda *_: call_order.append("quiesce")),
            patch(
                "local_agent_manager.t3code_handoff._close_confirmed_target",
                side_effect=lambda t: (
                    call_order.append(f"close:{t['software']}"),
                    {"software": t["software"], "pid": t["pid"], "was_running": True, "closed_pids": [t["pid"]]},
                )[1],
            ),
            patch("local_agent_manager.t3code_handoff._run_cli_json", side_effect=fake_cli),
            patch(
                "local_agent_manager.t3code_handoff._reopen_desktop_target",
                side_effect=lambda t: (
                    call_order.append(f"reopen:{t['software']}"),
                    {"software": t["software"], "executable": t["executable"], "reopened": True},
                )[1],
            ),
            patch(
                "local_agent_manager.t3code_handoff._send_t3_completion_notification",
                side_effect=lambda **_: (call_order.append("notify"), {"sent": True, "returncode": 0})[1],
            ),
        ):
            rc = run_handoff_worker(spec_path)

        self.assertEqual(rc, 0)
        receipt = strict_json_load(receipt_path)
        self.assertEqual(receipt["schema_version"], HANDOFF_RECEIPT_SCHEMA)
        self.assertEqual(receipt["goal_status"], "complete")
        self.assertTrue(receipt["goal_satisfied"])
        self.assertEqual(
            call_order,
            [
                "quiesce",
                "close:Cindy Desktop",
                "close:T3 Code Desktop",
                "settle_child",
                "plan",
                "apply",
                "cold_verify",
                "reopen:T3 Code Desktop",
                "reopen:Cindy Desktop",
                "notify",
            ],
        )

    def test_human_mode_streams_preflight_and_worker_progress_to_terminal(self) -> None:
        def fake_spawn(spec_path: Path) -> int:
            spec = strict_json_load(spec_path)
            events_path = Path(spec["handoff_dir"]) / "events.jsonl"
            lines = [
                json.dumps({"event": ev})
                for ev in (
                    "worker_started",
                    "t3_quiescent",
                    "targets_closed",
                    "plan_ready",
                    "apply_finished",
                    "cold_verify_finished",
                    "targets_reopened",
                    "worker_finished",
                )
            ]
            events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            write_new_json(
                Path(spec["receipt_path"]),
                {
                    "schema_version": HANDOFF_RECEIPT_SCHEMA,
                    "handoff_id": spec["handoff_id"],
                    "goal_status": "complete",
                    "goal_satisfied": True,
                    "errors": [],
                },
            )
            return 65432

        with (
            patch("local_agent_manager.t3code_handoff._qualify_lifecycle_targets", return_value=[]),
            patch("local_agent_manager.t3code_handoff.windows_confirmation", return_value=True),
            patch("local_agent_manager.t3code_handoff._spawn_detached_worker", side_effect=fake_spawn),
        ):
            out, err = StringIO(), StringIO()
            code = main(
                ["delete", "run", "--client", "t3code", "--record-id", TARGET_THREAD, "--handoff"],
                stdout=out,
                stderr=err,
            )
        self.assertEqual(code, 0, err.getvalue())
        stderr_text = err.getvalue()
        self.assertIn("[预检 1/4]", stderr_text)
        self.assertIn("[预检 4/4]", stderr_text)
        self.assertIn("[执行 1/7]", stderr_text)
        self.assertIn("[执行 7/7]", stderr_text)
        self.assertIn("[完成] 最终状态：complete", stderr_text)
        self.assertIn("complete", out.getvalue())


if __name__ == "__main__":
    unittest.main()
