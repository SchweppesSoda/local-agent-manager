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
from local_agent_manager.record_identity import canonical_path
from local_agent_manager.operation_store import strict_json_load, write_new_json, plan_sha256
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
        (self.install / 'resources').mkdir()
        for name in ('app.asar', 'server.asar'):
            (self.install / 'resources' / name).write_bytes(b'fixture archive')
        (self.root / 'userdata/server-owner.sqlite').touch()
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
            patch('pathlib.Path.home', return_value=self.base / 'profile'),
            patch('local_agent_manager.office_runtime.processes', return_value=[]),
            patch("local_agent_manager.t3code_server_delete._bundled_install", return_value=self.install),
            patch("local_agent_manager.t3code_handoff._bundled_install", return_value=self.install),
            patch.dict(
                os.environ,
                {
                    "USERPROFILE": str(self.base / "profile"),
                    "HOME": str(self.base / "profile"),
                    "CODEX_HOME": str(self.home),
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
            ["delete", "run", "--client", "t3code", "--record-id", TARGET_THREAD, "--handoff-app-pid", "41000", "--json"],
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

    def test_full_plan_blocker_is_reported_before_confirmation_or_close(self):
        with closing(sqlite3.connect(self.root / 'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?',
                (json.dumps({'savedResume': 'conversation://' + TARGET_NATIVE}), KEEP_THREAD))
            db.commit()
        with (patch('local_agent_manager.t3code_handoff.windows_confirmation') as prompt,
              patch('local_agent_manager.t3code_handoff._qualify_lifecycle_targets') as census,
              patch('local_agent_manager.t3code_handoff._spawn_detached_worker') as spawn):
            output = StringIO()
            code = main(['delete', 'run', '--client', 't3code', '--record-id', TARGET_THREAD,
                         '--handoff', '--json'], stdout=output, stderr=StringIO())
        self.assertEqual(code, 3, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['blockers'][0]['blocker_code'], 'preflight_plan_blocked')
        prompt.assert_not_called()
        census.assert_not_called()
        spawn.assert_not_called()
        with closing(sqlite3.connect(self.root / 'userdata/statev2.sqlite')) as db:
            self.assertEqual(db.execute(f'SELECT count(*) FROM {V2}threads').fetchone()[0], 3)

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

    def test_kernel_image_fallback_groups_cindy_children_and_rejects_unproven_identity(self):
        from local_agent_manager import t3code_handoff as handoff
        from local_agent_manager.record_identity import canonical_path
        cindy = self.base/'Cindy.exe'
        cindy.write_bytes(b'fixture never launched')
        home = self.base/'CindyGlobal/codex-home'
        home.mkdir(parents=True)
        census = [dict(pid=40010, parent=40000, name='Cindy.exe', executable=None, command=None),
                  dict(pid=40011, parent=40010, name='Cindy.exe', executable=None, command=None),
                  dict(pid=40000, parent=0, name='explorer.exe', executable=None, command=None)]
        def qualify():
            return handoff._qualify_lifecycle_targets(t3_root=self.root,
                manifest={'runtime_binaries': [str(cindy)]},
                target_stores=[('codex', canonical_path(home), str(home))],
                record_ids=[TARGET_THREAD], expanded_thread_ids=[TARGET_THREAD], native_record_count=1,
                occupancy_resources={str(home): [str(home/'state_5.sqlite')]})
        with (patch.object(handoff, 'list_processes', return_value=census),
              patch.object(handoff, 'file_owners', return_value=[dict(pid=40011, created_unix=100.0,
                  source='windows_restart_manager', resources=[str(home/'state_5.sqlite')])]),
              patch.object(handoff, '_kernel_process_start_unix', return_value=100.0),
              patch.object(handoff, '_kernel_process_image', return_value=(str(cindy), 100.0)) as image):
            targets = qualify()
            self.assertEqual([(t['pid'], t['executable']) for t in targets], [(40010, str(cindy))])
            self.assertIsNone(census[0]['executable'])
            for identity in (None, (str(self.t3_exe), 100.0), (str(cindy), 99.0)):
                image.return_value = identity
                with self.subTest(identity=identity), self.assertRaises(handoff.HandoffError):
                    qualify()

    def test_kernel_image_probe_reads_this_process_without_wmi(self):
        import sys
        from local_agent_manager.t3code_handoff import _kernel_process_image, _kernel_process_start_unix
        image, started = _kernel_process_image(os.getpid())
        self.assertTrue(os.path.samefile(image, sys.executable))
        self.assertEqual(started, _kernel_process_start_unix(os.getpid()))

    def test_extra_pid_and_matching_names_do_not_authorize_closure_without_file_occupancy(self):
        from local_agent_manager import t3code_handoff as handoff
        magpie = self.base/'magpie.exe'
        magpie.write_bytes(b'fixture never launched')
        rows = [dict(pid=41000, parent=40000, name='magpie.exe', executable=str(magpie), command='desktop'),
                dict(pid=41001, parent=41000, name='magpie-bun.exe', executable=str(self.base/'bun.exe'), command='server'),
                dict(pid=40000, parent=0, name='uTools.exe', executable=str(self.base/'uTools.exe'), command='launcher')]
        with (patch.object(handoff, 'list_processes', return_value=rows),
              patch.object(handoff, 'file_owners', return_value=[]) as owners,
              patch.object(handoff, '_kernel_process_image', return_value=(str(magpie), 100.0)),
              patch.object(handoff, '_kernel_process_start_unix', return_value=100.0)):
            def qualify(pids):
                return handoff._qualify_lifecycle_targets(t3_root=self.root,
                    manifest={'runtime_binaries': []}, target_stores=[], record_ids=[TARGET_THREAD],
                    expanded_thread_ids=[TARGET_THREAD], native_record_count=1, extra_app_pids=pids,
                    occupancy_resources={str(self.root): [str(self.root/'userdata/statev2.sqlite')]})
            self.assertEqual(qualify([]), [])
            with self.assertRaisesRegex(handoff.HandoffError, '没有目标数据文件'):
                qualify([41000])
            owners.return_value = [dict(pid=41001, created_unix=100.0, source='windows_restart_manager',
                                       resources=[str(self.root/'userdata/statev2.sqlite')])]
            targets = qualify([41000, 41000])
            self.assertEqual([(t['pid'],t['software'],t['reopen']) for t in targets], [(41000,'Magpie',True)])
            self.assertEqual(targets[0]['lifecycle_target'].pid, 41000)
            self.assertIn('Windows 文件占用证据', targets[0]['affected_tasks'][0])
            self.assertEqual(targets[0]['occupancy'][0]['pid'], 41001)
            owners.return_value[0]['created_unix'] = 99.0
            with self.assertRaises(handoff.HandoffError):
                qualify([])
            for invalid in (41001,40000,99999,-1):
                with self.subTest(pid=invalid), self.assertRaises(handoff.HandoffError):
                    qualify([invalid])

    def test_desktop_boundary_does_not_include_application_launcher(self):
        from local_agent_manager import t3code_handoff as handoff
        launcher = self.base/'uTools.exe'
        launcher.write_bytes(b'fixture launcher never executed')
        census = [
            dict(pid=40100, parent=0, name='uTools.exe', executable=str(launcher), command='launcher'),
            dict(pid=40200, parent=40100, name=self.t3_exe.name, executable=str(self.t3_exe), command='desktop'),
            dict(pid=40201, parent=40200, name=self.t3_exe.name, executable=str(self.t3_exe), command='--type=renderer'),
            dict(pid=40300, parent=40201, name='agy.exe', executable=str(self.base/'agy.exe'), command='acp'),
            dict(pid=40400, parent=40100, name='agy.exe', executable=str(self.base/'agy.exe'), command='acp'),
            dict(pid=40500, parent=40100, name='magpie-bun.exe', executable=str(self.base/'magpie-bun.exe'), command='server'),
            dict(pid=40600, parent=40500, name='agy.exe', executable=str(self.base/'agy.exe'), command='acp'),
        ]
        by_pid = {r['pid']: r for r in census}
        for pid in (40200, 40201, 40300):
            self.assertEqual(handoff._climb_desktop_owner(pid, by_pid)['pid'], 40200)
        self.assertEqual(handoff._climb_desktop_owner(40400, by_pid)['pid'], 40400)
        self.assertEqual(handoff._climb_desktop_owner(40600, by_pid)['pid'], 40600)
        with (patch.object(handoff, 'list_processes', return_value=census[:4]),
              patch.object(handoff, '_kernel_process_image', return_value=(str(self.t3_exe), 100.0)),
              patch.object(handoff, 'file_owners', return_value=[dict(pid=40300, created_unix=100.0,
                  source='windows_restart_manager', resources=[str(self.root/'userdata/statev2.sqlite')])]),
              patch.object(handoff, '_kernel_process_start_unix', return_value=100.0)):
            targets = handoff._qualify_lifecycle_targets(t3_root=self.root,
                manifest={'runtime_binaries': [str(self.t3_exe)]}, target_stores=[],
                record_ids=[TARGET_THREAD], expanded_thread_ids=[TARGET_THREAD], native_record_count=1,
                occupancy_resources={str(self.root): [str(self.root/'userdata/statev2.sqlite')]})
        self.assertEqual([(t['pid'], t['software']) for t in targets], [(40200, 'T3 Code Desktop')])

    def test_confirmed_background_launcher_cannot_restart_until_hold_released(self):
        import shutil
        import subprocess
        from local_agent_manager.t3code_handoff import _hold_background_launchers
        exe = self.base/'magpie.exe'
        shutil.copy2(Path(os.environ['SystemRoot'])/'System32/whoami.exe',exe)
        target = dict(executable=str(exe),hold_background_launcher=True)
        with _hold_background_launchers([target]):
            with self.assertRaises(OSError):
                subprocess.run([str(exe)],capture_output=True,timeout=5)
        result = subprocess.run([str(exe)],capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0)

    def test_released_occupancy_never_closes_confirmed_application(self):
        from local_agent_manager import t3code_handoff as h
        target = dict(pid=41000, created_unix=100., software='Magpie', executable=str(self.t3_exe), occupancy=[dict(
            pid=41001, created_unix=100., resources=[str(self.root/'userdata/statev2.sqlite')])])
        with (patch.object(h, '_capture_descendant_tree', return_value={41000:100., 41001:100.}),
              patch.object(h, '_kernel_process_image', return_value=(str(self.t3_exe), 100.0)),
              patch.object(h, 'file_owners', return_value=[]) as owners,
              patch.object(h, '_request_graceful_close') as close):
            result = h._close_confirmed_target(target)
            self.assertEqual(result['skip_reason'], 'target_file_occupancy_released')
            self.assertFalse(result['was_running'])
            close.assert_not_called()
            # PID reuse or a new occupant cannot enlarge previous consent.
            owners.return_value = [dict(pid=41001, created_unix=101.)]
            self.assertFalse(h._close_confirmed_target(target)['was_running'])
            target['occupancy'] = []
            with self.assertRaises(h.HandoffError):
                h._close_confirmed_target(target)
            close.assert_not_called()

    def test_current_exact_occupant_can_close_but_changed_image_cannot(self):
        from local_agent_manager import t3code_handoff as h
        proof = dict(pid=41000, created_unix=100., resources=[str(self.root/'userdata/statev2.sqlite')])
        target = dict(pid=41000, created_unix=100., software='T3', executable=str(self.t3_exe), occupancy=[proof])
        with (patch.object(h, '_capture_descendant_tree', return_value={41000:100.}),
              patch.object(h, '_kernel_process_image', return_value=(str(self.codex_exe),100.)) as identity,
              patch.object(h, '_kernel_process_start_unix', return_value=None),
              patch.object(h, 'file_owners', return_value=[proof]),
              patch.object(h, 'list_processes', return_value=[]),
              patch.object(h, '_request_graceful_close') as close):
            with self.assertRaises(h.HandoffError):
                h._close_confirmed_target(target)
            close.assert_not_called()
            identity.return_value = str(self.t3_exe),100.
            self.assertTrue(h._close_confirmed_target(target)['was_running'])
            close.assert_called_once_with(41000)

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
                "cleanup_scope": {"root": canonical_path(self.root), "thread_ids": sorted([TARGET_THREAD, CHILD_THREAD]), "native_targets": []},
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
                document = {
                    "operation_id": "op-clean-1", "plan_path": str(plan_out),
                    "goal_status": "ready", "blockers": [],
                    "actions": [{"kind": "delete_t3code_frontend", "impact": {
                        "external_action_payload": {"t3code_evidence": {
                            "schema_version": "larj.t3code-cleanup.v1", "root": str(self.root),
                            "native_targets": [], "selected_thread_ids": expanded}}}}],
                }
                document['plan_sha256'] = plan_sha256(document)
                write_new_json(plan_out, document)
                return 0, document
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

    def test_real_spawned_worker_boots_and_writes_receipt_without_mocks(self) -> None:
        from local_agent_manager.t3code_handoff import _follow_worker_events, _spawn_detached_worker

        handoff_dir = self.localappdata / "local-agent-manager/handoffs/real-spawn-test"
        handoff_dir.mkdir(parents=True)
        receipt_path = handoff_dir / "receipt.json"
        spec_path = handoff_dir / "spec.json"
        write_new_json(
            spec_path,
            {
                "schema_version": HANDOFF_SPEC_SCHEMA,
                "cleanup_scope": {"root": canonical_path(self.root), "thread_ids": sorted([TARGET_THREAD, CHILD_THREAD]), "native_targets": []},
                "handoff_id": "real-spawn-test",
                "created_at": "2026-11-11T08:00:00+00:00",
                "t3code_root": str(self.root),
                "t3code_install": str(self.install),
                "record_ids": [TARGET_THREAD],
                "expanded_thread_ids": [TARGET_THREAD, CHILD_THREAD],
                "native_record_count": 2,
                "notify_thread_id": None,
                "timeout": 5.0,
                "quiesce_timeout": 2.0,
                "handoff_dir": str(handoff_dir),
                "receipt_path": str(receipt_path),
                "cli_forwards": ["--t3code-bindings", str(handoff_dir / "missing-bindings.json")],
                "recoverable_preflight_children": [],
                "confirmed_targets": [],
            },
        )
        err = StringIO()
        with patch.dict(os.environ, {"PYTHONPATH": ""}, clear=False):
            worker_pid = _spawn_detached_worker(spec_path)
            final = _follow_worker_events(
                events_path=handoff_dir / "events.jsonl",
                receipt_path=receipt_path,
                worker_pid=worker_pid,
                stderr=err,
                timeout_seconds=20.0,
            )
        self.assertIsNotNone(final)
        self.assertTrue(receipt_path.is_file(), (handoff_dir / "worker.log").read_text(encoding="utf-8", errors="replace"))
        self.assertIn("[执行 1/7]", err.getvalue())
        self.assertIn("[执行 2/7]", err.getvalue())


if __name__ == "__main__":
    unittest.main()
