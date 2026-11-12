from contextlib import closing
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from local_agent_manager.cli import main
from local_agent_manager.t3code_bound_adapter import T3CodeBoundAdapter, history_references
from local_agent_manager.t3code_discovery import BindingDiscoveryError, discover_bindings
from tests.orca_native_support import create_native_schema, add_native_record
from tests.t3code_support import create_store, add_thread, insert, V2

THREAD = "11111111-1111-4111-8111-111111111111"
NATIVE = "33333333-3333-4333-8333-333333333333"


class T3DiscoveryTests(unittest.TestCase):
    def claude_threads(self):
        home = self.base/'.claude'
        project = home/'projects/project'
        project.mkdir(parents=True)
        transcript = project/(NATIVE+'.jsonl')
        transcript.write_text(json.dumps({'sessionId': NATIVE, 'private': 'PRIVATE_BODY'})+'\n')
        self.settings['providerInstances']['claudeAgent'] = {
            'driver': 'claudeAgent', 'config': {'homePath': str(home)}}
        self.write_settings()
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            for i in range(3):
                add_thread(db, 'claude-'+str(i), native=NATIVE, driver='claudeAgent')
        return transcript

    def test_failed_reference_catalog_is_grouped_with_original_source_metadata(self):
        self.claude_threads()
        database = self.base/'appdata/CindyGlobal/cindy-local-v1.db'
        database.parent.mkdir(parents=True)
        # An actual present database with an unqualified reference schema.
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE unrelated (private TEXT)')
            db.execute("INSERT INTO unrelated VALUES ('PRIVATE_BODY')")
            db.commit()
        with self.assertRaises(BindingDiscoveryError) as caught:
            discover_bindings(self.root)
        failures = caught.exception.blockers
        self.assertEqual(len(failures), 1, failures)
        self.assertEqual(failures[0]['affected_thread_ids'], ['claude-0', 'claude-1', 'claude-2'])
        error = failures[0]['details']['errors'][0]
        self.assertEqual(error['path'], str(database))
        self.assertEqual(error['error_type'], 'CindyReferenceError')
        self.assertEqual(error['source'], 'claude-references')
        self.assertTrue(error['details']['causes'][0]['stack'])
        self.assertIn('sessions', error['details']['causes'][0]['reason'])
        self.assertNotIn('PRIVATE_BODY', json.dumps(failures))

    @unittest.skipUnless(os.name == 'nt', 'Windows native sharing violation')
    def test_real_transcript_handle_preserves_sharing_violation_cause(self):
        transcript = self.claude_threads()
        from local_agent_manager.windows_held_files import HeldFiles
        from local_agent_manager.frozen_files import read_file
        proof = read_file(transcript.parent, transcript.name)[0]
        with HeldFiles() as held:
            held.acquire(transcript.parent, proof)
            with self.assertRaises(BindingDiscoveryError) as caught:
                discover_bindings(self.root)
            errors = caught.exception.blockers[0]['details']['errors']
            causes = [cause for error in errors for cause in error.get('details', {}).get('causes', ())]
            # Python's CRT open reports EACCES without retaining WinError 32.
            self.assertIn(13, [cause['errno'] for cause in causes])
            self.assertNotIn('PRIVATE_BODY', json.dumps(errors))
        self.assertTrue(discover_bindings(self.root))

    @unittest.skipUnless(os.name == 'nt', 'Windows profile junction')
    def test_profile_discovery_exception_keeps_path_stage_and_stable_code(self):
        self.claude_threads()
        roaming = self.base/'appdata'
        roaming.mkdir()
        physical = self.base/'other-profile'
        physical.mkdir()
        alias = roaming/'CindyGlobal'
        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(physical)],
                       check=True, capture_output=True)
        self.addCleanup(alias.rmdir)
        with self.assertRaises(BindingDiscoveryError) as caught:
            discover_bindings(self.root)
        failures = caught.exception.blockers
        self.assertEqual(len(failures), 1)
        cause = failures[0]['details']['errors'][0]['details']['causes'][0]
        self.assertEqual(cause['path'], str(alias))
        self.assertEqual(cause['stage'], 'cindy_profile')
        self.assertEqual(cause['code'], 'cindy_profile_link_or_reparse')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "t3"
        self.home = self.base / "native"
        self.home.mkdir()
        create_native_schema(self.home)
        add_native_record(self.home, NATIVE)
        with closing(create_store(self.root)) as db:
            add_thread(db, THREAD, native=NATIVE)
        (self.root / "userdata/environment-id").write_text("env-fixture")
        self.install = self.base / "install"
        self.install.mkdir()
        self.binary = self.install / "T3 Code (Nightly).exe"
        self.binary.write_bytes(b"fixture, never executed")
        self.settings = {"providerInstances": {"codex": {"driver": "codex", "config": {
            "homePath": str(self.home), "binaryPath": str(self.binary)}, "environment": [
            {"name": "TEST_CREDENTIAL", "value": "PRIVATE_SECRET", "sensitive": True}]}}}
        self.write_settings()
        for item in (patch("local_agent_manager.t3code_server_delete._bundled_install", return_value=self.install),
                     patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "T3CODE_HOME": str(self.root)})):
            item.start()
            self.addCleanup(item.stop)

    def write_settings(self):
        (self.root / "userdata/settings.json").write_text(json.dumps(self.settings), encoding="utf-8")

    def test_configured_custom_instance_native_ids_and_desktop_are_discovered(self):
        desktop = self.base / "appdata/t3code-v2"
        desktop.mkdir(parents=True)
        self.settings["providerInstances"]["custom-codex"] = self.settings["providerInstances"].pop("codex")
        self.write_settings()
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            for table in (V2 + "threads", V2 + "provider_threads"):
                db.execute(f'UPDATE {table} SET provider_instance_id=? WHERE thread_id=?', ("custom-codex", THREAD))
            db.execute(f'UPDATE {V2}threads SET default_provider=? WHERE thread_id=?', ("custom-codex", THREAD))
            db.commit()
        result = discover_bindings(self.root)
        self.assertEqual(result["native_stores"], [{"thread_id": THREAD, "engine": "codex",
            "root": str(self.home), "codex_binary": str(self.binary)}])
        self.assertEqual(result["desktop_profiles"][0]["root"], str(desktop))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        self.assertFalse(T3CodeBoundAdapter(result).snapshot_references().errors)

    def test_prepare_cli_can_save_binding_without_mutating_records(self):
        out, err = StringIO(), StringIO()
        destination = self.base / "bindings.json"
        before = (self.home / "state_5.sqlite").read_bytes()
        code = main(["environment", "t3-cleanup", "prepare", "--out", str(destination), "--json"],
                    stdout=out, stderr=err)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["status"], "ready")
        self.assertEqual(json.loads(destination.read_text())["native_stores"][0]["thread_id"], THREAD)
        self.assertEqual(before, (self.home / "state_5.sqlite").read_bytes())
        self.assertNotIn("PRIVATE_SECRET", out.getvalue())

    def test_installer_directory_alias_resolves_to_the_physical_runtime(self):
        alias = self.base / 'installer-current'
        if os.name == 'nt':
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(self.install)],
                           check=True, capture_output=True)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(self.install, target_is_directory=True)
        self.settings['providerInstances']['codex']['config']['binaryPath'] = str(alias / self.binary.name)
        self.write_settings()
        manifest = discover_bindings(self.root)
        self.assertEqual(manifest['native_stores'][0]['codex_binary'], str(self.binary))

    def test_custom_instance_names_in_run_history_use_the_persisted_driver(self):
        self.settings['providerInstances']['custom-codex'] = self.settings['providerInstances'].pop('codex')
        self.write_settings()
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            for table in (V2+'threads', V2+'provider_threads'):
                db.execute(f'UPDATE {table} SET provider_instance_id=? WHERE thread_id=?', ('custom-codex', THREAD))
            db.execute(f'UPDATE {V2}threads SET default_provider=? WHERE thread_id=?', ('custom-codex', THREAD))
            insert(db, V2+'runs', run_id='run-custom', thread_id=THREAD, provider='custom-codex',
                   provider_instance_id='custom-codex', status='completed', payload_json='{}')
            insert(db, V2+'run_attempts', attempt_id='attempt-custom', run_id='run-custom', thread_id=THREAD,
                   provider='custom-codex', provider_instance_id='custom-codex', status='completed', payload_json='{}')
            db.commit()
        manifest = discover_bindings(self.root)
        self.assertFalse(T3CodeBoundAdapter(manifest).snapshot_references().errors)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}runs SET provider_instance_id=?', ('unproven-other-instance',))
            db.commit()
        with self.assertRaisesRegex(ValueError, 'provider_cleanup_unsupported'):
            T3CodeBoundAdapter(manifest).snapshot_references()

    def test_subagent_provider_is_attributed_to_the_child_thread(self):
        child = 'child-claude'
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db, child, native='native-claude', driver='claudeAgent')
            insert(db, V2+'subagents', subagent_id='task-claude', thread_id=THREAD, child_thread_id=child,
                   provider='claudeAgent', provider_instance_id='claudeAgent', status='completed',
                   payload_json=json.dumps({'childThreadId': child}))
            insert(db, 'orchestration_events', event_id='provider-mirror', aggregate_kind='thread',
                   stream_id=child, event_type='provider-thread.updated', metadata_json='{}',
                   payload_json=json.dumps({'providerInstanceId': 'claudeAgent',
                       'nativeThreadRef': {'driver': 'claudeAgent', 'nativeId': 'native-claude', 'strength': 'strong'}}))
            db.commit()
        refs = history_references(self.root, {THREAD: {'engine': 'codex'}, child: {'engine': 'claude'}})
        self.assertTrue(any(row['thread_id'] == child and row['native_id'] == 'native-claude' for row in refs))

    def test_known_import_cursors_are_read_only_protection_sources(self):
        entry = {'birthtimeMs': 1, 'device': 1, 'filePath': str(self.home/'sessions'/('rollout-2026-11-10-'+NATIVE+'.jsonl')),
                 'inode': '12345678901234567890', 'mtimeMs': 1, 'provider': 'codex',
                 'providerInstanceId': 'codex', 'providerSessionId': NATIVE, 'size': 1}
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db, 'provider_session_runtime', thread_id=THREAD, provider_name='codex',
                   provider_instance_id='codex', adapter_key='import', status='stopped',
                   resume_cursor_json=json.dumps({'threadId': NATIVE}),
                   runtime_payload_json=json.dumps({'cwd': str(self.base), 'importedTranscripts': [entry]}))
            db.commit()
        manifest = discover_bindings(self.root)
        adapter = T3CodeBoundAdapter(manifest)
        self.assertFalse(adapter.snapshot_references().errors)
        self.assertEqual(adapter.legacy_import_ids, {THREAD})
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute('UPDATE provider_session_runtime SET resume_cursor_json=?', ('{}',))
            db.commit()
        with self.assertRaisesRegex(ValueError, 'legacy_restore_closure_unqualified'):
            T3CodeBoundAdapter(manifest).snapshot_references()

    def test_imported_claude_uses_recorded_source_after_instance_removal(self):
        source = self.base / 'old-claude'
        source.mkdir()
        path = source / 'projects' / 'fixture' / (NATIVE + '.jsonl')
        entry = {'birthtimeMs': 1, 'device': 1, 'filePath': str(path), 'inode': '123',
                 'mtimeMs': 1, 'provider': 'claudeAgent', 'providerInstanceId': 'retired-claude',
                 'providerSessionId': NATIVE, 'size': 1}
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db, 'imported', native=NATIVE, driver='claudeAgent')
            for table in (V2+'threads', V2+'provider_threads'):
                db.execute(f'UPDATE {table} SET provider_instance_id=? WHERE thread_id=?', ('retired-claude', 'imported'))
            insert(db, 'provider_session_runtime', thread_id='imported', provider_name='claudeAgent',
                   provider_instance_id='retired-claude', adapter_key='import', status='stopped',
                   resume_cursor_json=json.dumps({'threadId': 'imported', 'resume': NATIVE}),
                   runtime_payload_json=json.dumps({'cwd': str(self.base), 'importedTranscripts': [entry]}))
            db.commit()
        manifest = discover_bindings(self.root)
        bound = next(b for b in manifest['native_stores'] if b['thread_id'] == 'imported')
        self.assertEqual(bound, {'thread_id': 'imported', 'engine': 'claude', 'root': str(source)})
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}provider_threads SET payload_json=json_set(payload_json,'$.nativeThreadRef.nativeId',?) WHERE thread_id=?",
                       ('44444444-4444-4444-8444-444444444444', 'imported'))
            db.commit()
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings(self.root)
        self.assertEqual(raised.exception.blockers[0]['blocker_code'], 'import_provider_binding_unproven')
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}provider_threads SET payload_json=json_set(payload_json,'$.nativeThreadRef.nativeId',?) WHERE thread_id=?",
                       (NATIVE, 'imported'))
            db.commit()
        # An existing but unrecognized original cannot be treated as absent.
        path.parent.mkdir(parents=True)
        path.write_text('unrecognized original')
        with self.assertRaises(BindingDiscoveryError):
            discover_bindings(self.root)

    def test_model_switch_mirror_is_supported_but_cannot_hide_restore_handles(self):
        payload = {'type': 'user_message', 'modelSwitch': {
            'from': {'model': 'old'}, 'to': {'model': 'new'}}}
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            insert(db, 'orchestration_events', event_id='switch', aggregate_kind='thread',
                   stream_id=THREAD, event_type='turn-item.updated', metadata_json='{}',
                   payload_json=json.dumps(payload))
            db.commit()
        self.assertTrue(discover_bindings(self.root))
        payload['modelSwitch']['unexpectedRestore'] = {'nativeThreadId': 'other-native'}
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute('UPDATE orchestration_events SET payload_json=? WHERE event_id=?',
                       (json.dumps(payload), 'switch'))
            db.commit()
        with self.assertRaisesRegex(ValueError, 'legacy_restore_closure_unqualified'):
            discover_bindings(self.root)

    def test_read_tool_native_handles_and_invalid_shapes_stay_audit_content_in_both_mirrors(self):
        for native_ref in ({'driver': 'codex', 'nativeId': 'unrelated', 'strength': 'strong'}, ['invalid-shape']):
            with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
                item = {'type': 'dynamic_tool', 'toolName': 't3-code.t3_thread_read',
                        'output': {'nativeThreadRef': native_ref, 'nativeThreadId': ['also-audit']}}
                insert(db, V2+'turn_items', turn_item_id='audit', thread_id=THREAD, payload_json=json.dumps(item))
                insert(db, 'orchestration_v2_events', event_id='audit-v2', thread_id=THREAD,
                       event_type='turn-item.updated', payload_json=json.dumps(item))
                insert(db, 'orchestration_events', event_id='audit-old', stream_id=THREAD, aggregate_kind='thread',
                       event_type='turn-item.updated', payload_json=json.dumps(item), metadata_json='{}')
                db.commit()
                refs = history_references(self.root, {THREAD: {'engine': 'codex'}})
                self.assertEqual({row['native_id'] for row in refs}, {NATIVE})
                for table in (V2+'turn_items', 'orchestration_v2_events', 'orchestration_events'):
                    db.execute(f'DELETE FROM {table} WHERE payload_json=?', (json.dumps(item),))
                db.commit()

    def test_unknown_legacy_restore_event_and_nested_cursor_block_full_cleanup(self):
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            for event_type, payload in (
                ('unknown.restore', {'resumeCursor': {'threadId': NATIVE}}),
                ('provider-thread.updated', {'nativeMetadata': {'resumeCursor': {'threadId': NATIVE}}}),
                ('provider-thread.updated', {'unexpected': {'threadId': NATIVE}}),
            ):
                insert(db, 'orchestration_events', event_id='unknown', stream_id=THREAD, aggregate_kind='thread',
                       event_type=event_type, payload_json=json.dumps(payload), metadata_json='{}')
                db.commit()
                with self.assertRaisesRegex(ValueError, 'legacy_restore_closure_unqualified'):
                    history_references(self.root, {THREAD: {'engine': 'codex'}})
                db.execute("DELETE FROM orchestration_events WHERE event_id='unknown'")
                db.commit()

    def test_v2_subagent_event_native_handle_belongs_to_child(self):
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            child = 'child-claude'
            add_thread(db, child, native='native-claude', driver='claudeAgent')
            payload = {'childThreadId': child, 'providerInstanceId': 'claudeAgent',
                       'nativeThreadRef': {'driver': 'claudeAgent', 'nativeId': 'native-claude', 'strength': 'strong'}}
            insert(db, 'orchestration_v2_events', event_id='child-event', thread_id=THREAD,
                   event_type='subagent.updated', payload_json=json.dumps(payload))
            db.commit()
        refs = history_references(self.root, {THREAD: {'engine': 'codex'}, child: {'engine': 'claude'}})
        self.assertFalse(any(row['thread_id'] == THREAD and row['native_id'] == 'native-claude' for row in refs))
        self.assertTrue(any(row['thread_id'] == child and row['native_id'] == 'native-claude' for row in refs))

    def test_unknown_native_home_is_a_blocker_instead_of_assuming_default(self):
        other = self.base / "wrong-home"
        other.mkdir()
        create_native_schema(other)
        self.settings["providerInstances"]["codex"]["config"]["homePath"] = str(other)
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings(self.root)
        self.assertEqual(raised.exception.blockers, [{"blocker_code": "native_ids_not_in_configured_home", "thread_id": THREAD}])

    def test_unreadable_database_keeps_prepare_output_machine_readable(self):
        (self.root / "userdata/statev2.sqlite").write_bytes(b"invalid database")
        output, error = StringIO(), StringIO()
        code = main(["environment", "t3-cleanup", "prepare", "--json"], stdout=output, stderr=error)
        self.assertNotEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "blocked")
        self.assertNotIn("PRIVATE_SECRET", output.getvalue())

    def test_environment_root_without_home_path_is_checked_by_native_id(self):
        instance = self.settings["providerInstances"]["codex"]
        del instance["config"]["homePath"]
        instance["environment"].append({"name": "CODEX_HOME", "value": str(self.home)})
        self.write_settings()
        self.assertEqual(discover_bindings()["native_stores"][0]["root"], str(self.home))
        instance["environment"].append({"name": "CODEX_HOME", "value": str(self.home)})
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError):
            discover_bindings()

    def test_auto_delete_planning_uses_discovered_bound_adapter(self):
        from local_agent_manager.adapter_factory import create_default_adapters
        from local_agent_manager.cli import build_parser
        args = build_parser().parse_args(["delete", "plan", "--client", "t3code", "--record-id", THREAD])
        adapters = create_default_adapters(args)
        self.assertTrue(any(isinstance(adapter, T3CodeBoundAdapter) for adapter in adapters))

    def test_t3_guard_on_another_clients_operation_remains_read_only(self):
        from local_agent_manager.adapter_factory import create_default_adapters
        from local_agent_manager.cli import build_parser
        args = build_parser().parse_args(["delete", "plan", "--client", "native", "--record-id", NATIVE,
                                          "--t3code-root", str(self.root)])
        with patch("local_agent_manager.t3code_discovery.discover_bindings", side_effect=AssertionError("guard must stay read-only")):
            adapters = create_default_adapters(args)
        self.assertFalse(any(isinstance(adapter, T3CodeBoundAdapter) for adapter in adapters))

    @unittest.skipUnless(os.name == "nt", "Full T3 writer qualification uses the Windows lifecycle")
    def test_normal_plan_entry_discovers_native_descendants_without_binding_file(self):
        child = "44444444-4444-4444-8444-444444444444"
        add_native_record(self.home, child, parent=NATIVE)
        (self.root / "userdata/server-owner.sqlite").touch()
        output, error = StringIO(), StringIO()
        code = main(["delete", "plan", "--client", "t3code", "--record-id", THREAD,
                     "--out", str(self.base / "auto.plan.json"), "--json"], stdout=output, stderr=error)
        plan = json.loads(output.getvalue())
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan["goal_status"], "ready", plan)
        self.assertIn(child, json.dumps(plan))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(plan))

    @unittest.skipUnless(os.name == 'nt', 'Full T3 writer qualification uses the Windows lifecycle')
    def test_parent_selection_expands_the_registered_subagent_child(self):
        child_thread = 'child-thread'
        child_native = '44444444-4444-4444-8444-444444444444'
        add_native_record(self.home, child_native)
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            add_thread(db, child_thread, native=child_native)
            insert(db, V2+'subagents', subagent_id='child-task', thread_id=THREAD, child_thread_id=child_thread,
                   provider='codex', provider_instance_id='codex', status='completed',
                   payload_json=json.dumps({'childThreadId': child_thread}))
            db.commit()
        (self.root/'userdata/server-owner.sqlite').touch()
        output, error = StringIO(), StringIO()
        code = main(['delete', 'plan', '--client', 't3code', '--record-id', THREAD,
                     '--out', str(self.base/'child.plan.json'), '--json'], stdout=output, stderr=error)
        plan = json.loads(output.getvalue())
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.assertIn(child_thread, json.dumps(plan))
        self.assertIn(child_native, json.dumps(plan))

    def test_unproven_antigravity_cli_runtime_is_reported_without_claiming_binding(self):
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            add_thread(db, "antigravity-thread", driver="antigravity", native="antigravity-native")
        self.settings["providerInstances"]["antigravity"] = {"driver": "antigravity", "config": {
            "binaryPath": str(self.base / "agy.exe")}}
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings()
        self.assertEqual(raised.exception.blockers, [{"blocker_code": "native_binding_unverified",
                                                      "thread_id": "antigravity-thread"}])

    def test_pi_environment_session_root_uses_exact_path_and_header_identity(self):
        agent_dir = self.base / "pi"
        agent_dir.mkdir()
        root = self.base / "custom-sessions"
        root.mkdir()
        path = root / "session.jsonl"
        path.write_text(json.dumps({"type": "session", "version": 3, "id": NATIVE,
            "timestamp": "2026-11-10T00:00:00Z", "cwd": str(self.base)}) + "\n")
        self.settings["providerInstances"]["pi"] = {"driver": "pi", "config": {}, "environment": {
            "PI_CODING_AGENT_DIR": str(agent_dir), "PI_CODING_AGENT_SESSION_DIR": str(root)}}
        self.write_settings()
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            add_thread(db, "pi-thread", driver="pi", native=str(path))
        manifest = discover_bindings(self.root)
        refs = T3CodeBoundAdapter(manifest).snapshot_references().references
        ref = next(ref for ref in refs if ref.frontend_id == "pi-thread")
        self.assertEqual(ref.native_id, NATIVE)
        self.assertEqual(ref.opaque_native_locator, str(path))
        self.assertEqual(ref.native_record.store.canonical_path, str(root))
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id='pi-thread'",
                (json.dumps({"nativeThreadRef": {"driver": "pi", "nativeId": None, "strength": "none"}}),))
            db.commit()
        empty_refs = T3CodeBoundAdapter(discover_bindings(self.root)).snapshot_references().references
        self.assertFalse(any(ref.frontend_id == "pi-thread" for ref in empty_refs))
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id='pi-thread'",
                (json.dumps({"nativeThreadRef": {"driver": "pi", "nativeId": str(path), "strength": "strong"}}),))
            db.commit()
        self.settings["providerInstances"]["pi"]["environment"]["PI_CODING_AGENT_SESSION_DIR"] = str(agent_dir)
        self.write_settings()
        with self.assertRaises(BindingDiscoveryError):
            discover_bindings(self.root)

    def test_deleted_imported_transcript_and_acp_audit_tools_do_not_block_cleanup(self):
        from local_agent_manager import t3code_database

        imported_thread = "import:codex:99999999-9999-4999-8999-999999999999"
        missing_native = "99999999-9999-4999-8999-999999999999"
        observer_thread = "22222222-2222-4222-8222-222222222222"
        observer_native = "55555555-5555-4555-8555-555555555555"
        add_native_record(self.home, observer_native)
        deleted_transcript = self.home / 'sessions/2026/11/01' / ('rollout-2026-11-01-' + missing_native + '.jsonl')
        entry = {
            "birthtimeMs": 1,
            "device": 1,
            "filePath": str(deleted_transcript),
            "inode": "1234567890",
            "mtimeMs": 1,
            "provider": "codex",
            "providerInstanceId": "codex",
            "providerSessionId": missing_native,
            "size": 1,
        }
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            add_thread(db, imported_thread, native=missing_native)
            insert(
                db,
                "provider_session_runtime",
                thread_id=imported_thread,
                provider_name="codex",
                provider_instance_id="codex",
                adapter_key="import",
                status="stopped",
                resume_cursor_json=json.dumps({"threadId": missing_native}),
                runtime_payload_json=json.dumps({"cwd": str(self.base), "importedTranscripts": [entry]}),
            )
            add_thread(db, observer_thread, native=observer_native)
            acp_read = {
                "type": "dynamic_tool",
                "toolName": "Run mcp_t3-code_t3_thread_read",
                "input": {"threadId": THREAD},
                "output": {"threadId": THREAD},
            }
            search_hit = {
                "type": "dynamic_tool",
                "toolName": "t3-code.t3_thread_search",
                "input": {"query": "fixture"},
                "output": {"matches": [{"threadId": THREAD}]},
            }
            insert(db, V2 + "turn_items", turn_item_id="acp-read", thread_id=observer_thread, payload_json=json.dumps(acp_read))
            insert(db, V2 + "turn_items", turn_item_id="search-hit", thread_id=observer_thread, payload_json=json.dumps(search_hit))
            db.commit()

        manifest = discover_bindings(self.root)
        adapter = T3CodeBoundAdapter(manifest)
        snapshot = adapter.snapshot_references(refresh=True)
        self.assertFalse(snapshot.errors)
        self.assertIn(imported_thread, adapter.legacy_import_ids)
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.row_factory = sqlite3.Row
            owned, _ = t3code_database.closure(db, [THREAD], native_ids=[NATIVE])
        self.assertEqual(owned["counts"][V2 + "threads"], 1)

        # Non-imported missing native IDs must still block discovery.
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            add_thread(db, "non-imported-missing", native="88888888-8888-4888-8888-888888888888")
            db.commit()
        with self.assertRaises(BindingDiscoveryError) as raised:
            discover_bindings(self.root)
        self.assertEqual(
            raised.exception.blockers,
            [{"blocker_code": "native_ids_not_in_configured_home", "thread_id": "non-imported-missing"}],
        )


if __name__ == "__main__":
    unittest.main()
