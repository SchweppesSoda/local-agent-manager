import copy
from contextlib import closing, contextmanager, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import t3code_cleanup as cleanup, t3code_database as database
from local_agent_record_janitor import t3code_cleanup_files as files, t3code_lifecycle as lifecycle
from local_agent_record_janitor.t3code_bound_adapter import DATABASE_ONLY_SCHEMA, DATABASE_RESET_SCHEMA, T3CodeBoundAdapter, SCHEMA
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
        from local_agent_manager import t3code_desktop_cleanup as d, office_leveldb as level
        self.installed_runtime = d.default_runtime()
        self.leveldb_runtime = level._runtime_directory()
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

    def test_full_plan_freezes_chromium_before_service_deletion(self):
        from local_agent_manager import t3code_desktop_cleanup as d
        from tests.test_t3code_desktop_writer import fixture, ENV, IDS
        installed = self.installed_runtime
        if not (installed/'T3 Code (Nightly).exe').is_file():
            self.skipTest('existing qualified Electron required')
        profile = self.base/'desktop'
        profile.mkdir()
        seed = json.loads(json.dumps(fixture()).replace(ENV, 'env_test').replace(IDS[0], THREAD))
        stack = ExitStack()
        self.addCleanup(stack.close)
        runtime = stack.enter_context(d.isolated_runtime(installed))
        d.observe(runtime, profile, 'env_test', [THREAD], native=True, mode='seed', seed=seed)
        (runtime/'electron.exe').rename(runtime/'T3 Code (Nightly).exe')
        installed = runtime
        self.manifest['runtime_binaries'] = [str(installed/'T3 Code (Nightly).exe')]
        self.manifest['desktop_profiles'] = [{'root':str(profile), 'runtime_binaries':self.manifest['runtime_binaries']}]
        self.adapter = T3CodeBoundAdapter(self.manifest)
        with patch.object(d, 'default_runtime', return_value=installed), patch.dict(os.environ, {
                'LARJ_LEVELDB_RUNTIME':str(self.leveldb_runtime)}):
            running = [dict(pid=987654, parent=1, name='T3 Code (Nightly).exe',
                            executable=str(installed/'T3 Code (Nightly).exe'), command='synthetic desktop')]
            before = d.source_files(profile)
            with patch('local_agent_manager.office_runtime.processes', return_value=running):
                plan = self.plan()
                item = cleanup.evidence_from_document(plan)['desktop'][0]['localstorage']['indexeddb']
                with self.assertRaisesRegex(ValueError, 'writer_running'):
                    d.apply_plan(item, item['plan_sha256'], clients_closed=True)
                self.assertFalse((d.journal_root(profile)/item['operation_id']).exists())
            self.assertEqual(before, d.source_files(profile))
            item = cleanup.evidence_from_document(plan)['desktop'][0]['localstorage']['indexeddb']
            self.assertEqual(item['schema_version'], 'lam.t3-desktop-full.v1')
            self.assertEqual(item['evidence']['native']['counts']['thread'], 1)
            self.assertGreater(d.remaining_bound(item), 0)
            result = self.apply(plan)
            self.assertEqual(result['goal_status'], 'complete', result)
            self.assertEqual(d.status_plan(item)['attempts'], 1)
            self.assertEqual(d.remaining_bound(item), 0)
            result = self.coordinator().verify_operation(operation_id=plan['operation_id'], plan_path=self.plan_path)
            self.assertEqual(result['goal_status'], 'complete', result)

    def retired_adapter(self, db):
        db.execute(f"DELETE FROM {V2}provider_threads WHERE thread_id=?", (THREAD,))
        db.execute(f"UPDATE {V2}threads SET default_provider='antigravity', provider_instance_id='antigravity',"
                   " deleted_at='retired', payload_json=json_set(payload_json,'$.deletedAt','retired') WHERE thread_id=?", (THREAD,))
        (self.root / "userdata/settings.json").write_text(json.dumps({"providerInstances": {
            "antigravity": {"driver": "antigravity", "enabled": False}}}), encoding="utf-8")
        return T3CodeBoundAdapter({**self.manifest, "schema_version": DATABASE_ONLY_SCHEMA,
            "native_stores": [], "provider_instance_id": "antigravity"})

    def test_online_desktop_plan_rejects_source_change_during_copy_rehearsal(self):
        from local_agent_manager import t3code_desktop_cleanup as d

        installed = self.base / 'synthetic-runtime'
        installed.mkdir()
        binary = installed / 'T3 Code (Nightly).exe'
        binary.write_bytes(b'synthetic runtime identity, never executed')
        profile = self.base/'desktop'
        log_path = profile / d.LEVEL / 'LOG'
        log_path.parent.mkdir(parents=True)
        log_path.write_bytes(b'original fixture\n')
        self.manifest['runtime_binaries'] = [str(binary)]
        self.manifest['desktop_profiles'] = [{'root':str(profile), 'runtime_binaries':self.manifest['runtime_binaries']}]
        frozen = files.freeze(self.root, [THREAD])['database']

        @contextmanager
        def concurrent_writer(evidence, _runtime):
            copied = self.base / 'rehearsal'
            d.copy_source(profile, copied, evidence['files'])
            with log_path.open('ab') as log:
                log.write(b'concurrent fixture writer\n')
            yield

        # Stub Chromium's interpretation of the copy while exercising the
        # real file snapshots, rehearsal copy, and post-rehearsal drift check.
        with (patch.object(d, 'default_runtime', return_value=installed),
              patch.object(d, 'observe', return_value={'counts': {'thread': 1}}),
              patch.object(d, 'prepared', side_effect=concurrent_writer),
              self.assertRaisesRegex(ValueError, 'desktop_planning_source_changed')):
            d.new_full_plan(self.manifest, frozen, profile)
        self.assertEqual((self.base / 'rehearsal' / d.LEVEL / 'LOG').read_bytes(), b'original fixture\n')
        self.assertFalse(d.journal_root(profile).exists())
        self.assertGreater(database.remaining(frozen), 0)

    def test_retired_database_only_purges_legacy_rows_and_truncates_wal(self):
        path = self.root / "userdata/statev2.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            adapter = self.retired_adapter(db)
            for tid in (THREAD, KEEP):
                insert(db, "orchestration_events", event_id="legacy-" + tid, stream_id=tid,
                       aggregate_kind="thread", payload_json=json.dumps({"threadId": tid, "text": PRIVATE}))
            db.commit()
        plan = self.coordinator().plan_operation(client="t3code", record_ids=(THREAD,), adapters=(adapter,),
            plan_path=self.plan_path, operation_id="t3-retired-database")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertNotIn(PRIVATE, json.dumps(plan))
        evidence = cleanup.evidence_from_document(plan)
        self.assertEqual(evidence["files"]["database"]["counts"]["orchestration_events"], 1)
        self.assertEqual(evidence["native_targets"], [])
        self.assertEqual(evidence["desktop"], [])
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        database.require_wal_truncated(evidence["files"]["database"])
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT stream_id FROM orchestration_events").fetchall(), [(KEEP,)])
            self.assertEqual(db.execute(f"SELECT thread_id FROM {V2}messages").fetchall(), [(KEEP,)])
        result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(result["goal_status"], "complete", result)

    def test_retired_database_only_rejects_other_threads_and_reappeared_runtime(self):
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            adapter = self.retired_adapter(db)
            db.commit()
        result = self.coordinator().plan_operation(client="t3code", record_ids=(KEEP,), adapters=(adapter,),
            plan_path=self.plan_path)
        self.assertEqual(result["blockers"][0]["blocker_code"], "t3code_database_only_thread_unqualified", result)
        plan = self.coordinator().plan_operation(client="t3code", record_ids=(THREAD,), adapters=(adapter,),
            plan_path=self.plan_path)
        self.assertEqual(plan["goal_status"], "ready", plan)
        (self.root / "tools/antigravity-acp").mkdir(parents=True)
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["mutation_started"], result)

    def test_database_reset_requires_every_thread_and_preserves_native_history(self):
        native = add_native_record(self.home, NATIVE)
        path = self.root / "userdata/statev2.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            projects = db.execute("SELECT * FROM projection_projects").fetchall()
        adapter = T3CodeBoundAdapter({**self.manifest, "schema_version": DATABASE_RESET_SCHEMA, "native_stores": []})
        result = self.coordinator().plan_operation(client="t3code", record_ids=(THREAD,), adapters=(adapter,),
            plan_path=self.plan_path)
        self.assertEqual(result["blockers"][0]["blocker_code"], "t3code_database_reset_requires_all_thread_ids", result)
        plan = self.coordinator().plan_operation(client="t3code", record_ids=(THREAD, KEEP), adapters=(adapter,),
            plan_path=self.plan_path, operation_id="t3-database-reset")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual([a["kind"] for a in plan["actions"]], [cleanup.KIND])
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(native.is_file())
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {V2}threads").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT * FROM projection_projects").fetchall(), projects)
            self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)
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

    def test_long_native_title_persisted_fingerprint_is_stable_but_real_drift_blocks(self):
        from local_agent_record_janitor.office_cleanup import digest
        add_native_record(self.home, NATIVE)
        title = 'Synthetic title\n  ' + 'long display metadata ' * 60
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db:
            db.execute('UPDATE threads SET title=? WHERE id=?', (title, NATIVE))
            db.commit()
        plan = self.plan()
        evidence = cleanup.evidence_from_document(plan)
        frontend = next(a for a in plan['actions'] if a['kind'] == cleanup.KIND)
        self.assertEqual(digest(evidence), frontend['binding']['snapshot_fingerprint'])
        # A new process reconstructs from the persisted evidence, with no
        # original long display value or in-memory planner cache available.
        cold = self.coordinator()
        context = cleanup.build_context(cold, (self.adapter,), (self.adapter,),
                                        [THREAD], ['codex'], document=plan)[0]
        actions, blockers = cold._bind_fresh_candidates(plan, context)
        self.assertFalse(blockers, blockers)
        self.assertEqual(len(actions), 2)
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db:
            # Even a suffix beyond the displayed 240 chars remains protected
            # by native metadata evidence, independently of display clipping.
            db.execute('UPDATE threads SET title=? WHERE id=?', (title + ' changed', NATIVE))
            db.commit()
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])

    def test_unavailable_native_peer_blocks_plan_before_any_batch(self):
        child='55555555-5555-4555-8555-555555555555'
        broken='66666666-6666-4666-8666-666666666666'
        healthy_path=add_native_record(self.home,NATIVE)
        add_native_record(self.home,broken)
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(healthy_path),broken));db.commit()
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db,child,native=broken)
            db.execute(f"UPDATE {V2}threads SET payload_json=json_set(payload_json,'$.lineage.parentThreadId',?) WHERE thread_id=?",(THREAD,child));db.commit()
        self.manifest['native_stores'].append({'thread_id':child,'engine':'codex','root':str(self.home)})
        self.adapter=T3CodeBoundAdapter(self.manifest)
        plan=self.coordinator().plan_operation(client='t3code',record_ids=(THREAD,),adapters=(self.adapter,),plan_path=self.plan_path)
        self.assertEqual(plan['goal_status'],'blocked',plan)
        self.assertFalse(plan['mutation_started'])
        self.assertIn('native_writer_missing',str(plan['blockers']))

    def test_absent_and_partial_child_journals_have_distinct_cold_status(self):
        from local_agent_record_janitor.operation_store import OperationStore
        plan = self.plan()
        cold = self.coordinator()
        result = cold.status_operation(operation_id=plan['operation_id'], plan_path=self.plan_path)
        self.assertEqual(result['goal_status'], 'ready', result)
        self.assertEqual([b['status'] for b in result['batches']], ['pending'])
        raw = plan['child_batches'][0]
        store = OperationStore(self.root, raw['child_operation_id'])
        self.assertFalse(store.directory.exists())
        # A partial journal is not absence and must never acquire pending
        # semantics, even when no mutation checkpoint survived.
        store.directory.mkdir(parents=True)
        store.plan_path.write_text('{}', encoding='utf-8')
        result = cold.status_operation(operation_id=plan['operation_id'], plan_path=self.plan_path)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertTrue(result['blockers'])
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)

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

    def test_unselected_upload_and_extracted_sidecar_are_preserved(self):
        attachment_id = 'keeper-77777777-7777-4777-8777-777777777777-pptx'
        payload = {'role': 'user', 'attachments': [{'type': 'file', 'id': attachment_id}]}
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db, 'keeper')
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?', (json.dumps(payload), 'keeper'))
            db.commit()
        directory = self.root/'userdata/attachments'
        directory.mkdir(parents=True)
        for name in (attachment_id+'.pptx', attachment_id+'.pptx.txt'):
            (directory/name).write_bytes(b'unselected upload')
        evidence = files.freeze(self.root, [THREAD])
        files.apply(evidence, phase_callback=lambda _: None)
        self.assertEqual(files.remaining(evidence), 0)
        self.assertEqual(len(list(directory.iterdir())), 2)
        (directory/(attachment_id+'.pptx')).write_bytes(b'drift')
        with self.assertRaisesRegex(ValueError, 'unselected_file_changed'):
            files.remaining(evidence)

    def test_selected_or_unproven_attachment_files_still_block(self):
        attachment_id = 'keeper-77777777-7777-4777-8777-777777777777-pptx'
        payload = {'attachments': [{'type': 'file', 'id': attachment_id}]}
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db, 'keeper')
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id IN (?,?)',
                       (json.dumps(payload), 'keeper', THREAD))
            db.commit()
        directory = self.root/'userdata/attachments'
        directory.mkdir(parents=True)
        path = directory/(attachment_id+'.pptx')
        path.write_bytes(b'shared with selected')
        with self.assertRaisesRegex(ValueError, 'attachment_closure_unqualified'):
            files.freeze(self.root, [THREAD])
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?', ('{}', THREAD))
            db.commit()
        path.rename(path.with_suffix('.unqualified'))
        with self.assertRaisesRegex(ValueError, 'attachment_closure_unqualified'):
            files.freeze(self.root, [THREAD])

    def test_retained_html_render_is_preserved_with_exact_tool_and_owner(self):
        attachment_id = KEEP+'-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa-html'
        payload = {'id': 'html-item', 'threadId': KEEP, 'type': 'dynamic_tool',
                   'status': 'completed', 'toolName': 't3-code.html_render',
                   'output': {'htmlRender': {'attachmentId': attachment_id, 'title': 'render', 'height': 250}}}
        directory = self.root/'userdata/attachments'
        directory.mkdir(parents=True)
        path = directory/(attachment_id+'.html')
        path.write_bytes(b'<html>retained render</html>')
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db, V2+'turn_items', turn_item_id='html-item', thread_id=KEEP,
                   type='dynamic_tool', status='completed', payload_json=json.dumps(payload))
            db.commit()
        files.freeze(self.root, [THREAD])
        invalid = [
            {**payload, 'threadId': OTHER},
            {**payload, 'toolName': 'unknown.html_render'},
            {**payload, 'status': 'running'},
            {**payload, 'output': {**payload['output'], 'isError': True}},
            {**payload, 'output': {'htmlRender': {**payload['output']['htmlRender'], 'restore': 'unknown'}}},
        ]
        for index, item in enumerate(invalid):
            with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
                db.execute(f'UPDATE {V2}turn_items SET payload_json=? WHERE turn_item_id=?',
                           (json.dumps(item), 'html-item'))
                db.commit()
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'attachment_closure_unqualified'):
                files.freeze(self.root, [THREAD])
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}turn_items SET payload_json=? WHERE turn_item_id=?',
                       (json.dumps(payload), 'html-item'))
            db.commit()
        evidence = files.freeze(self.root, [THREAD])
        files.apply(evidence, phase_callback=lambda _: None)
        self.assertEqual(files.remaining(evidence), 0)
        self.assertEqual(path.read_bytes(), b'<html>retained render</html>')

    def test_image_attachment_preservation_rejects_unknown_siblings_and_text_sidecars(self):
        directory = self.root/'userdata/attachments'
        directory.mkdir(parents=True)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db, 'keeper')
        for extension in ('', '-png'):
            attachment_id = 'keeper-77777777-7777-4777-8777-777777777777' + extension
            payload = {'attachments': [{'type': 'image', 'id': attachment_id, 'name': 'photo.png'}]}
            with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
                db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?', (json.dumps(payload), 'keeper'))
                db.commit()
            primary = directory/(attachment_id+'.png')
            primary.write_bytes(b'image')
            files.freeze(self.root, [THREAD])
            for suffix in ('.sql', '.png.txt'):
                unknown = directory/(attachment_id+suffix)
                unknown.write_bytes(b'unknown')
                with self.assertRaisesRegex(ValueError, 'attachment_closure_unqualified'):
                    files.freeze(self.root, [THREAD])
                unknown.unlink()
            primary.unlink()

    def test_retained_session_referencing_native_descendant_blocks_complete_plan(self):
        child = '55555555-5555-4555-8555-555555555555'
        add_native_record(self.home, NATIVE)
        add_native_record(self.home, child, parent=NATIVE)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db, V2+'provider_sessions', provider_session_id='keeper-session', thread_id=KEEP,
                   provider='codex', status='stopped', payload_json=json.dumps({'unexpectedRestore': child}))
            db.commit()
        result = self.coordinator().plan_operation(client='t3code', record_ids=(THREAD,),
            adapters=(self.adapter,), plan_path=self.plan_path, operation_id='native-descendant-protection')
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertIn('t3code_shared_provider_session_restore_reference',
                      {item['blocker_code'] for item in result['blockers']})

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

    def test_server_empty_desktop_replica_blocks_cold_status_and_verify(self):
        profile = self.base / "desktop"
        profile.mkdir()
        self.manifest["desktop_profiles"] = [
            {"root": str(profile), "runtime_binaries": [str(self.binary)]}
        ]
        self.adapter = T3CodeBoundAdapter(self.manifest)
        plan = self.plan()
        operation = self.coordinator()
        result = operation.apply_operation(operation_id=plan["operation_id"], plan_path=self.plan_path,
            plan_sha256=plan["plan_sha256"], clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            self.assertEqual(db.execute(f"SELECT count(*) FROM {V2}threads WHERE thread_id=?", (THREAD,)).fetchone()[0], 0)
            self.assertEqual(db.execute(f"SELECT count(*) FROM {V2}threads WHERE thread_id=?", (KEEP,)).fetchone()[0], 1)
        # An omitted persistent family appears after the journal completed.
        # Historical complete receipts must not bypass cold discovery.
        family = profile / "IndexedDB/t3code_app_0.indexeddb.leveldb"
        family.mkdir(parents=True)
        retained = family / "retained"
        retained.write_bytes(b"unselected offline list, body and draft")
        before = retained.read_bytes()
        plan_before = self.plan_path.read_bytes()
        result = operation.status_operation(operation_id=plan["operation_id"])
        self.assertFalse(result["goal_satisfied"], result)
        self.assertIn("desktop_indexeddb_unqualified", str(result))
        for method in ("status_operation", "verify_operation"):
            with self.subTest(method=method):
                result = getattr(self.coordinator(), method)(operation_id=plan["operation_id"], plan_path=self.plan_path)
                self.assertFalse(result["goal_satisfied"], result)
                self.assertNotEqual(result["goal_status"], "complete", result)
                self.assertIn("desktop_indexeddb_unqualified", str(result))
        self.assertEqual(retained.read_bytes(), before)
        self.assertEqual(self.plan_path.read_bytes(), plan_before)

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

    def test_two_claude_actions_share_one_batch_context_and_preserve_keeper(self):
        child='55555555-5555-4555-8555-555555555555'
        child_native='66666666-6666-4666-8666-666666666666'
        claude=self.base/'claude';project=claude/'projects/synthetic';project.mkdir(parents=True)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db,child,native=child_native,driver='claudeAgent')
            db.execute(f"UPDATE {V2}threads SET payload_json=json_set(payload_json,'$.lineage.parentThreadId',?) WHERE thread_id=?",(THREAD,child))
            for tid,native in ((THREAD,NATIVE),(KEEP,OTHER)):
                db.execute(f"UPDATE {V2}threads SET default_provider='claudeAgent',provider_instance_id='claudeAgent' WHERE thread_id=?",(tid,))
                db.execute(f"UPDATE {V2}provider_threads SET driver='claudeAgent',provider='claudeAgent',provider_instance_id='claudeAgent',payload_json=? WHERE thread_id=?",
                    (json.dumps({'nativeThreadRef':{'driver':'claudeAgent','nativeId':native,'strength':'strong'}}),tid))
            db.commit()
        for native in (NATIVE,child_native,OTHER):
            (project/(native+'.jsonl')).write_text(json.dumps({'sessionId':native,'message':PRIVATE})+'\n')
        for bound in self.manifest['native_stores']:bound.update(engine='claude',root=str(claude))
        self.manifest['native_stores'].append({'thread_id':child,'engine':'claude','root':str(claude)})
        self.adapter=T3CodeBoundAdapter(self.manifest)
        plan=self.coordinator().plan_operation(client='t3code',record_ids=(THREAD,),adapters=(self.adapter,),
            plan_path=self.plan_path,operation_id='t3-two-claude')
        self.assertEqual(plan['goal_status'],'ready',plan)
        batch=next(b for b in plan['child_batches'] if b['mutation_family']=='delete_claude_session')
        self.assertEqual(batch['action_count'],2)
        result=self.apply(plan)
        self.assertEqual(result['goal_status'],'complete',result)
        self.assertFalse((project/(NATIVE+'.jsonl')).exists())
        self.assertFalse((project/(child_native+'.jsonl')).exists())
        self.assertTrue((project/(OTHER+'.jsonl')).is_file())
        cold=self.coordinator().verify_operation(operation_id=plan['operation_id'],plan_path=self.plan_path)
        self.assertEqual(cold['goal_status'],'complete',cold)

    def test_pi_exact_path_survives_interrupted_native_and_frontend_cleanup(self):
        agent_dir = self.base / "pi"
        sessions = agent_dir / "sessions"
        transcript = sessions / "project" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        header = {"type": "session", "version": 3, "id": NATIVE, "timestamp": "2026-11-10T00:00:00Z", "cwd": str(self.base)}
        transcript.write_text(json.dumps(header) + "\n", encoding="utf-8")
        keeper = transcript.with_name("same-id-other-file.jsonl")
        keeper.write_bytes(transcript.read_bytes())
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}threads SET default_provider='pi' WHERE thread_id=?", (THREAD,))
            db.execute(f"UPDATE {V2}provider_threads SET driver='pi',provider='pi',payload_json=? WHERE thread_id=?",
                (json.dumps({"nativeThreadRef": {"driver": "pi", "nativeId": str(transcript), "strength": "strong"}}), THREAD))
            db.commit()
        self.manifest["native_stores"][0].update(engine="pi", root=str(sessions), agent_dir=str(agent_dir))
        self.adapter = T3CodeBoundAdapter(self.manifest)
        plan = self.coordinator().plan_operation(client="t3code", record_ids=(THREAD,),
            adapters=(self.adapter,), plan_path=self.plan_path, operation_id="t3-pi-fullflow")
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertEqual({a["kind"] for a in plan["actions"]}, {"delete_pi_session", cleanup.KIND})
        with patch.object(cleanup, "execute", side_effect=RuntimeError("stop before frontend")):
            result = self.apply(plan)
        self.assertFalse(transcript.exists(), result)
        self.assertTrue(keeper.exists())
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(keeper.exists())


if __name__ == '__main__':
    unittest.main()
