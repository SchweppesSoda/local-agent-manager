import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import antigravity_cli_binding as binding, antigravity_cli_cleanup as native
from local_agent_manager import paseo_cleanup, t3code_cleanup, paseo_store, t3code_lifecycle
from local_agent_manager.paseo_bound_adapter import PaseoBoundAdapter, SCHEMA as PASEO
from local_agent_manager.t3code_bound_adapter import T3CodeBoundAdapter, SCHEMA as T3
from local_agent_manager.cleanup_service import CleanupService
from local_agent_manager.operation_coordinator import OperationCoordinator
from tests.test_antigravity_cli_cleanup import fixture, TARGET, KEEP, CHILD, SECRET
from tests.t3code_support import create_store, add_thread, V2, insert
from tests.test_paseo_fullflow import agent, write

FRONT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


def paseo_agent(identifier, sid, cwd):
    value = agent(identifier, sid, cwd)
    persistence = {'version': 1, 'data': {'conversationId': sid}}
    handle = 'plugin:'+json.dumps(persistence, separators=(',', ':'))
    value.update(provider='antigravity', persistence={'provider': 'antigravity', 'sessionId': handle,
        'metadata': {'pluginProviderPersistence': persistence}},
        runtimeInfo={'provider': 'antigravity', 'sessionId': handle})
    return value


class PersistenceTests(unittest.TestCase):
    def test_real_plugin_envelope_projects_three_agreeing_native_references(self):
        row = paseo_store._project(paseo_agent(FRONT, TARGET, Path.cwd()))
        self.assertTrue(row['antigravity_handle_qualified'])
        self.assertEqual({h['value'] for h in row['references']}, {TARGET})
        self.assertEqual(len(row['references']), 3)

    def test_core_metadata_cwd_qualifies_without_becoming_native_identity(self):
        value = paseo_agent(FRONT, TARGET, Path.cwd())
        value['persistence']['metadata']['cwd'] = str(Path.cwd())
        row = paseo_store._project(value)
        self.assertTrue(row['antigravity_handle_qualified'])
        self.assertEqual({h['value'] for h in row['references']}, {TARGET})
        self.assertEqual(len(row['references']), 3)

    def test_unknown_metadata_or_conflicting_cwd_remain_opaque(self):
        for cwd in (None, 123, 'relative', str(Path.cwd()/'other-workspace')):
            with self.subTest(cwd=cwd):
                value = paseo_agent(FRONT, TARGET, Path.cwd())
                value['persistence']['metadata']['cwd'] = cwd
                self.assertFalse(paseo_store._project(value)['antigravity_handle_qualified'])
        value = paseo_agent(FRONT, TARGET, Path.cwd())
        value['persistence']['metadata']['cwd'] = str(Path.cwd())
        value['persistence']['metadata']['resume'] = KEEP
        self.assertFalse(paseo_store._project(value)['antigravity_handle_qualified'])

    def test_custom_acp_raw_uuid_and_conflicting_metadata_remain_opaque(self):
        for shape in ('scalar', 'object', 'metadata', 'runtime', 'runtime_extra', 'nativeHandle', 'version', 'duplicate'):
            with self.subTest(shape=shape):
                value = paseo_agent(FRONT, TARGET, Path.cwd())
                p = value['persistence']
                if shape == 'scalar': p['sessionId'] = TARGET
                if shape == 'object': p['sessionId'] = {'conversationId': TARGET}
                if shape == 'metadata': p['metadata']['pluginProviderPersistence']['data']['conversationId'] = KEEP
                if shape == 'runtime': value['runtimeInfo']['sessionId'] = 'plugin:'+json.dumps({'version': 1, 'data': {'conversationId': KEEP}})
                if shape == 'runtime_extra': value['runtimeInfo']['extra'] = {'resume': KEEP}
                if shape == 'nativeHandle': p['nativeHandle'] = TARGET
                if shape == 'version': p['metadata']['pluginProviderPersistence']['version'] = 2
                if shape == 'duplicate': p['sessionId'] = 'plugin:{"version":1,"version":1,"data":{"conversationId":"'+TARGET+'"}}'
                try:
                    row = paseo_store._project(value)
                except ValueError:
                    continue
                self.assertFalse(row['antigravity_handle_qualified'])
                self.assertEqual(row['references'], [])


@unittest.skipUnless(os.name == 'nt', 'Windows held frontend and CLI boundaries')
class JointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lam-cli-joint-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.native, self.agy = fixture(self.base, child=True)
        self.user = self.base/'profile'
        self.t3, self.paseo = self.base/'t3', self.base/'paseo'
        self.binary = self.base/'frontend.exe'
        self.binary.write_bytes(b'fixture not executed')
        for item in (patch('pathlib.Path.home', return_value=self.user),
                     patch('local_agent_manager.office_runtime.processes', return_value=[]),
                     patch.dict(os.environ, {'USERPROFILE': str(self.user), 'HOME': str(self.user),
                         'APPDATA': str(self.base/'appdata'), 'LOCALAPPDATA': str(self.base/'local'),
                         'T3CODE_HOME': str(self.t3), 'PASEO_HOME': str(self.paseo)})):
            item.start(); self.addCleanup(item.stop)
        self.plan_path = self.base/'plan.json'

    def coordinator(self):
        return OperationCoordinator(CleanupService(client_inspector=lambda *a, **k: ()))

    def bound(self, identifier, client):
        value = {('thread_id' if client == 't3code' else 'agent_id'): identifier,
                 'engine': 'antigravity', 'root': str(self.native), 'native_layout': 'cli',
                 'cli_binary': str(self.agy), 'cli_user_home': str(self.user)}
        if client == 't3code': value['provider_instance_id'] = 'antigravity'
        return value

    def setup_client(self, client):
        root = self.t3 if client == 't3code' else self.paseo
        if client == 't3code':
            with closing(create_store(root)) as db:
                add_thread(db, FRONT, native=TARGET, driver='antigravity')
                add_thread(db, OTHER, native=KEEP, driver='antigravity')
            (root/'userdata/environment-id').write_text('env_test')
            (root/t3code_lifecycle.OWNER).touch()
            (root/'userdata/settings.json').write_text(json.dumps({'providerInstances': {'antigravity': {
                'driver': 'antigravity', 'enabled': True, 'config': {'binaryPath': str(self.agy)},
                'environment': {'GEMINI_HOME': str(root/'ignored-managed-home')}}}}))
            identity = {'environment_id': 'env_test'}
        else:
            write(root/'agents'/f'{FRONT}.json', paseo_agent(FRONT, TARGET, self.base))
            write(root/'agents'/f'{OTHER}.json', paseo_agent(OTHER, KEEP, self.base))
            (root/'server-id').write_text('srv_test')
            (root/'config.json').write_text(json.dumps({'version': 1, 'agents': {'providers': {
                'antigravity': {'command': [str(self.agy)]}}}}))
            identity = {'server_id': 'srv_test'}
        manifest = {'schema_version': T3 if client == 't3code' else PASEO,
            'profile_root': str(root), **identity, 'runtime_binaries': [str(self.binary), str(self.agy)],
            'desktop_profiles': [], 'native_stores': [self.bound(FRONT, client), self.bound(OTHER, client)]}
        return (T3CodeBoundAdapter if client == 't3code' else PaseoBoundAdapter)(manifest)

    def plan(self, client, adapter):
        return self.coordinator().plan_operation(client=client, record_ids=[FRONT], adapters=(adapter,),
            plan_path=self.plan_path, operation_id='joint-test')

    def apply(self, plan):
        return self.coordinator().apply_operation(operation_id=plan['operation_id'], plan_path=self.plan_path,
            plan_sha256=plan['plan_sha256'], clients_closed=True)

    def assert_fullflow(self, client):
        adapter = self.setup_client(client)
        keeper = (self.native/'conversations'/f'{KEEP}.db').read_bytes()
        plan = self.plan(client, adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        module = t3code_cleanup if client == 't3code' else paseo_cleanup
        evidence = module.evidence_from_document(plan)
        self.assertEqual(evidence['native_targets'][0]['ids'], sorted([TARGET, CHILD]))
        self.assertNotIn(SECRET, json.dumps(plan))
        applied = self.apply(plan)
        self.assertEqual(applied['goal_status'], 'complete', applied)
        self.assertFalse((self.native/'conversations'/f'{TARGET}.db').exists())
        self.assertFalse((self.native/'brain'/CHILD).exists())
        self.assertEqual((self.native/'conversations'/f'{KEEP}.db').read_bytes(), keeper)
        self.assertEqual(module.files.remaining(evidence['files']), 0)
        verified = self.coordinator().verify_operation(operation_id=plan['operation_id'], plan_path=self.plan_path)
        self.assertEqual(verified['goal_status'], 'complete', verified)
        with patch.object(native, 'apply', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.apply(plan)['goal_status'], 'complete')

    def test_t3_fullflow_cold_apply_child_closure_and_no_replay(self):
        self.assert_fullflow('t3code')

    def test_t3_joint_cleanup_preserves_known_audit_text(self):
        adapter = self.setup_client('t3code')
        payloads = [
            (V2+'messages', {'role': 'user', 'text': 'Mention '+TARGET}, None),
            (V2+'turn_items', {'type': 'command_execution', 'input': 'inspect '+TARGET, 'output': TARGET}, 'turn-item.updated'),
            (V2+'turn_items', {'type': 'dynamic_tool', 'toolName': 't3-code.delegate_task',
                              'input': {'task': 'Review '+TARGET}}, 'turn-item.updated'),
            (V2+'turn_items', {'type': 'subagent', 'prompt': 'Review '+TARGET}, 'turn-item.updated'),
            (V2+'subagents', {'prompt': 'Review '+TARGET}, 'subagent.updated'),
            (V2+'turn_items', {'type': 'reasoning', 'text': 'Diagnose '+TARGET}, 'turn-item.updated'),
            (V2+'turn_items', {'type': 'file_change', 'fileName': 'repair.py',
                'diffStr': '+ inspect '+TARGET, 'oldStr': TARGET, 'newStr': 'audit '+TARGET}, 'turn-item.updated'),
        ]
        with closing(sqlite3.connect(self.t3/'userdata/statev2.sqlite')) as db:
            for index, (table, payload, event_type) in enumerate(payloads):
                if table == V2+'messages':
                    db.execute(f'UPDATE {table} SET payload_json=? WHERE thread_id=?', (json.dumps(payload), OTHER))
                elif table == V2+'turn_items':
                    insert(db, table, turn_item_id='audit-'+str(index), thread_id=OTHER, payload_json=json.dumps(payload))
                else:
                    insert(db, table, subagent_id='audit-task', thread_id=OTHER, child_thread_id=None,
                           provider='antigravity', provider_instance_id='antigravity', status='completed', payload_json=json.dumps(payload))
                insert(db, 'orchestration_events', event_id='mirror-'+str(index), aggregate_kind='thread', stream_id=OTHER,
                       stream_version=index+1, event_type=event_type or 'message.updated',
                       payload_json=json.dumps(payload), metadata_json='{}')
                insert(db, 'orchestration_v2_events', event_id='mirror-v2-'+str(index), thread_id=OTHER,
                       event_type=event_type or 'message.updated', payload_json=json.dumps(payload))
            db.commit()
        plan = self.plan('t3code', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'complete', result)
        with closing(sqlite3.connect(self.t3/'userdata/statev2.sqlite')) as db:
            self.assertEqual(json.loads(db.execute(f'SELECT payload_json FROM {V2}messages WHERE thread_id=?', (OTHER,)).fetchone()[0]), payloads[0][1])

    def test_paseo_fullflow_cold_apply_child_closure_and_no_replay(self):
        self.assert_fullflow('paseo')

    def test_paseo_core_metadata_cwd_full_plan_apply_and_cold_verify(self):
        adapter = self.setup_client('paseo')
        path = self.paseo/'agents'/f'{FRONT}.json'
        value = json.loads(path.read_bytes())
        value['persistence']['metadata']['cwd'] = value['cwd']
        write(path, value)
        plan = self.plan('paseo', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.assertTrue(self.apply(plan)['goal_satisfied'])
        self.assertFalse(path.exists())
        self.assertFalse((self.native/'conversations'/f'{TARGET}.db').exists())
        self.assertTrue((self.native/'conversations'/f'{KEEP}.db').exists())
        self.assertTrue(self.coordinator().verify_operation(operation_id=plan['operation_id'],
            plan_path=self.plan_path)['goal_satisfied'])

    def test_t3_diagnostic_hardlinks_survive_joint_apply_and_cold_verify(self):
        adapter = self.setup_client('t3code')
        directory = self.native/'log'
        directory.mkdir()
        target = directory/'cli-fixture.log'
        target.write_text('preserved diagnostic log')
        link = self.native/'cli.log'
        os.link(target, link)
        plan = self.plan('t3code', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.assertTrue(self.apply(plan)['goal_satisfied'])
        self.assertEqual(target.read_text(), 'preserved diagnostic log')
        self.assertTrue(os.path.samefile(link, target))
        self.assertTrue(self.coordinator().verify_operation(operation_id=plan['operation_id'],
            plan_path=self.plan_path)['goal_satisfied'])

    def test_t3_missing_native_cleans_exact_cache_and_frontend_preserving_native_peers(self):
        from tests.t3code_support import V2
        missing = '44444444-4444-4444-8444-444444444444'
        adapter = self.setup_client('t3code')
        with closing(sqlite3.connect(self.t3/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}provider_threads SET payload_json=json_set(payload_json,'$.nativeThreadRef.nativeId',?) WHERE thread_id=?", (missing, FRONT))
            db.commit()
        cache_path = self.native/native.CACHE
        cache = json.loads(cache_path.read_text())
        workspace = str(self.base/'missing-workspace')
        cache[workspace] = missing
        cache_path.write_text(json.dumps(cache))
        keeper = (self.native/'conversations'/f'{TARGET}.db').read_bytes()
        plan = self.plan('t3code', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        evidence = t3code_cleanup.evidence_from_document(plan)
        self.assertEqual(evidence['native_targets'][0]['native_files']['absent_ids'], [missing])
        self.assertTrue(self.apply(plan)['goal_satisfied'])
        self.assertEqual((self.native/'conversations'/f'{TARGET}.db').read_bytes(), keeper)
        self.assertNotIn(workspace, json.loads(cache_path.read_text()))
        self.assertTrue(self.coordinator().verify_operation(operation_id=plan['operation_id'],
            plan_path=self.plan_path)['goal_satisfied'])

    def test_t3_linked_skills_are_preserved_without_following_shared_target(self):
        adapter = self.setup_client('t3code')
        shared = self.base/'shared-skill'
        shared.mkdir()
        marker = shared/'SKILL.md'
        marker.write_text('shared skill ' + TARGET)
        directory = self.native/'skills'
        directory.mkdir()
        link = directory/'fixture'
        if os.name == 'nt':
            subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(shared)],
                           check=True,capture_output=True)
        else:
            link.symlink_to(shared, target_is_directory=True)
        self.addCleanup(link.rmdir if os.name == 'nt' else link.unlink)
        plan = self.plan('t3code', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.assertTrue(self.apply(plan)['goal_satisfied'])
        self.assertEqual(marker.read_text(), 'shared skill ' + TARGET)
        self.assertTrue(link.exists())
        self.assertTrue(self.coordinator().verify_operation(operation_id=plan['operation_id'],
            plan_path=self.plan_path)['goal_satisfied'])

    def test_t3_unselected_native_child_and_historical_restore_block(self):
        adapter = self.setup_client('t3code')
        with closing(sqlite3.connect(self.t3/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id=?',
                (json.dumps({'nativeThreadRef': {'driver': 'antigravity', 'nativeId': CHILD, 'strength': 'strong'}}), OTHER))
            db.commit()
        self.assertEqual(self.plan('t3code', adapter)['goal_status'], 'blocked')
        with closing(sqlite3.connect(self.t3/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id=?',
                (json.dumps({'nativeThreadRef': {'driver': 'antigravity', 'nativeId': KEEP, 'strength': 'strong'},
                             'savedResume': TARGET}), OTHER))
            db.commit()
        self.assertEqual(self.plan('t3code', adapter)['goal_status'], 'blocked')

    def test_paseo_unselected_child_consumer_and_atomic_restore_block(self):
        adapter = self.setup_client('paseo')
        write(self.paseo/'agents'/f'{OTHER}.json', paseo_agent(OTHER, CHILD, self.base))
        self.assertEqual(self.plan('paseo', adapter)['goal_status'], 'blocked')
        write(self.paseo/'agents'/f'{OTHER}.json', paseo_agent(OTHER, KEEP, self.base))
        atomic = self.paseo/'agents'/f'.{OTHER}.json.123.456.{CHILD}.tmp'
        write(atomic, paseo_agent(OTHER, TARGET, self.base))
        self.assertEqual(self.plan('paseo', adapter)['goal_status'], 'blocked')

    def test_other_client_native_reference_is_not_approved_by_selected_profile(self):
        adapter = self.setup_client('t3code')
        self.setup_client('paseo')
        plan = self.plan('t3code', adapter)
        self.assertEqual(plan['goal_status'], 'blocked', plan)
        self.assertTrue((self.native/'conversations'/f'{TARGET}.db').exists())

    def test_other_t3_profile_remains_protected_even_if_deleted(self):
        adapter = self.setup_client('paseo')
        self.setup_client('t3code')
        with closing(sqlite3.connect(self.t3/'userdata/statev2.sqlite')) as db:
            db.execute(f"UPDATE {V2}threads SET deleted_at='deleted' WHERE thread_id=?", (FRONT,))
            db.commit()
        self.assertEqual(self.plan('paseo', adapter)['goal_status'], 'blocked')

    def test_settings_and_runtime_drift_block_before_native_write(self):
        adapter = self.setup_client('paseo')
        plan = self.plan('paseo', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        with (self.paseo/'config.json').open('a') as stream: stream.write(' ')
        with patch.object(native, 'apply', side_effect=AssertionError('writer not authorized')):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])

    def test_wrong_native_root_is_refused_and_compatible_new_build_can_plan(self):
        adapter = self.setup_client('t3code')
        bad = copy.deepcopy(adapter.manifest)
        bad['native_stores'][0]['root'] = str(self.t3/'userdata/providers/antigravity/old')
        with self.assertRaisesRegex(ValueError, 'root_or_runtime_unproven'):
            T3CodeBoundAdapter(bad)
        self.agy.write_bytes(b'different build')
        self.assertEqual(self.plan('t3code', adapter)['goal_status'], 'ready')

    def assert_upgrade_after_approval(self, client):
        adapter = self.setup_client(client)
        plan = self.coordinator().plan_operation(client=client, record_ids=[FRONT], adapters=[adapter],
            plan_path=self.base/'before-upgrade.json', operation_id='before-upgrade')
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.agy.write_bytes(b'compatible future CLI build')
        blocked = self.coordinator().apply_operation(operation_id=plan['operation_id'],
            plan_path=Path(plan['plan_path']), plan_sha256=plan['plan_sha256'], clients_closed=True)
        self.assertEqual(blocked['goal_status'], 'blocked', blocked)
        self.assertFalse(blocked['mutation_started'])
        fresh = self.coordinator().plan_operation(client=client, record_ids=[FRONT], adapters=[adapter],
            plan_path=self.base/'after-upgrade.json', operation_id='after-upgrade')
        self.assertEqual(fresh['goal_status'], 'ready', fresh)
        evidence = (t3code_cleanup if client == 't3code' else paseo_cleanup).evidence_from_document(fresh)
        actual = evidence['native_targets'][0]['native_files']['runtime']['file']['sha256']
        self.assertEqual(actual, hashlib.sha256(self.agy.read_bytes()).hexdigest())
        applied = self.coordinator().apply_operation(operation_id=fresh['operation_id'],
            plan_path=Path(fresh['plan_path']), plan_sha256=fresh['plan_sha256'], clients_closed=True)
        self.assertEqual(applied['goal_status'], 'complete', applied)

    def test_t3_upgrade_after_approval_blocks_old_plan_and_fresh_plan_succeeds(self):
        self.assert_upgrade_after_approval('t3code')

    def test_paseo_upgrade_after_approval_blocks_old_plan_and_fresh_plan_succeeds(self):
        self.assert_upgrade_after_approval('paseo')

    def test_partial_native_failure_is_unknown_with_before_images_and_no_replay(self):
        from local_agent_manager.windows_held_files import HeldFiles
        adapter = self.setup_client('t3code')
        plan = self.plan('t3code', adapter)
        original = HeldFiles.dispose
        calls = []
        def interrupted(held, root, relative, **kwargs):
            calls.append(relative)
            original(held, root, relative, **kwargs)
            raise OSError('synthetic interruption after first native removal')
        with patch.object(HeldFiles, 'dispose', interrupted):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertTrue(result['mutation_started'])
        self.assertEqual(len(calls), 1)
        # Before-images are manager state: kept outside the native store.
        from local_agent_manager.state_paths import control_directory
        self.assertTrue(list(control_directory(self.native).rglob('*.before')))
        self.assertFalse(list(self.native.rglob('*.before')))
        from local_agent_manager.mutation_guard import MutationScope, mutation_guard, UnknownMutationError
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((MutationScope(self.native, None),)):
                self.fail('Unknown joint cleanup must reserve the shared native root')
        with patch.object(native, 'apply', side_effect=AssertionError('never replay')):
            again = self.apply(plan)
            verified = self.coordinator().verify_operation(operation_id=plan['operation_id'], plan_path=self.plan_path)
        self.assertEqual(again['goal_status'], 'unknown', again)
        self.assertEqual(verified['goal_status'], 'unknown', verified)

    def test_frontend_failure_after_native_success_is_unknown_and_not_replayed(self):
        adapter = self.setup_client('paseo')
        plan = self.plan('paseo', adapter)
        with patch.object(paseo_cleanup.files, 'apply', side_effect=OSError('synthetic frontend failure')):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertFalse((self.native/'conversations'/f'{TARGET}.db').exists())
        self.assertTrue((self.paseo/'agents'/f'{FRONT}.json').exists())
        with patch.object(native, 'apply', side_effect=AssertionError('never replay')):
            self.assertEqual(self.apply(plan)['goal_status'], 'unknown')

    def test_native_preflight_refusal_does_not_strand_root_reservation(self):
        adapter = self.setup_client('paseo')
        plan = self.plan('paseo', adapter)
        with patch.object(native, 'require_closed', side_effect=native.CleanupError('writer_running')):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])
        self.assertEqual(self.apply(plan)['goal_status'], 'complete')

    def test_cli_discovery_uses_real_home_not_t3_gemini_home(self):
        from local_agent_manager.t3code_discovery import _acp_binding
        adapter = self.setup_client('t3code')
        instance = json.loads((self.t3/'userdata/settings.json').read_bytes())['providerInstances']['antigravity']
        binaries = set()
        found = _acp_binding(self.t3, {'thread_id': FRONT, 'provider_instance_id': 'antigravity'}, instance, binaries)
        self.assertEqual(found, adapter.bindings[FRONT])
        self.assertEqual(binaries, {str(self.agy)})

    def test_paseo_discovery_qualifies_opaque_plugin_handle_and_native_uuid(self):
        from local_agent_manager.paseo_discovery import _binding
        adapter = self.setup_client('paseo')
        entries, _ = paseo_cleanup.files.rows(self.paseo)
        entry = next(e for e in entries if e['row']['id'] == FRONT)
        found = _binding(entry, {'antigravity': {'command': [str(self.agy)]}}, {})
        self.assertEqual(found, adapter.bindings[FRONT])
        with self.assertRaisesRegex(ValueError, 'runtime_resolution_unproven'):
            _binding(entry, {}, {})

    def test_selected_frontend_descendant_is_visible_in_same_frozen_plan(self):
        adapter = self.setup_client('t3code')
        child_thread = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
        with closing(sqlite3.connect(self.t3/'userdata/statev2.sqlite')) as db:
            add_thread(db, child_thread, native=CHILD, driver='antigravity')
            db.execute(f"UPDATE {V2}threads SET payload_json=json_set(payload_json,'$.lineage.parentThreadId',?) WHERE thread_id=?", (FRONT, child_thread))
            db.commit()
        adapter.manifest['native_stores'].append(self.bound(child_thread, 't3code'))
        adapter = T3CodeBoundAdapter(adapter.manifest)
        coordinator = self.coordinator()
        plan = coordinator.plan_operation(client='t3code', record_ids=[FRONT], adapters=[adapter],
            plan_path=self.plan_path, operation_id='joint-children')
        self.assertEqual(plan['goal_status'], 'ready', plan)
        evidence = t3code_cleanup.evidence_from_document(plan)
        self.assertEqual(evidence['selected_thread_ids'], sorted([FRONT, child_thread]))
        applied = coordinator.apply_operation(operation_id=plan['operation_id'], plan_path=self.plan_path,
            plan_sha256=plan['plan_sha256'], clients_closed=True)
        self.assertEqual(applied['goal_status'], 'complete', applied)

    def test_paseo_warm_apply_preserves_unselected_profile(self):
        adapter = self.setup_client('paseo')
        coordinator = self.coordinator()
        plan = coordinator.plan_operation(client='paseo', record_ids=[FRONT], adapters=[adapter],
            plan_path=self.plan_path, operation_id='joint-warm')
        self.assertEqual(plan['goal_status'], 'ready', plan)
        applied = coordinator.apply_operation(operation_id=plan['operation_id'], plan_path=self.plan_path,
            plan_sha256=plan['plan_sha256'], clients_closed=True)
        self.assertEqual(applied['goal_status'], 'complete', applied)

    def test_new_guard_profile_and_native_writer_after_plan_block(self):
        adapter = self.setup_client('t3code')
        plan = self.plan('t3code', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.setup_client('paseo')
        with patch.object(native, 'apply', side_effect=AssertionError('must not write')):
            self.assertEqual(self.apply(plan)['goal_status'], 'blocked')

    def test_cli_guard_cannot_drop_selected_profile_or_widen_frozen_frontend(self):
        from local_agent_manager import antigravity_cli_joint as joint
        adapter = self.setup_client('t3code')
        plan = self.plan('t3code', adapter)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        evidence = copy.deepcopy(t3code_cleanup.evidence_from_document(plan))
        evidence['cli_guards'] = [s for s in evidence['cli_guards'] if s['client'] != 't3code']
        with self.assertRaisesRegex(ValueError, 'joint_guard_scope_changed'):
            joint.recheck(evidence, 't3code')
        evidence = copy.deepcopy(t3code_cleanup.evidence_from_document(plan))
        evidence['selected_thread_ids'].append(OTHER)
        with self.assertRaisesRegex(ValueError, 'closure_root_changed'):
            t3code_cleanup.validate(evidence)

    def test_runtime_and_frontend_startup_are_held_during_native_mutation(self):
        adapter = self.setup_client('t3code')
        plan = self.plan('t3code', adapter)
        original = native.apply
        observed = []
        def inspect(*args, **kwargs):
            with self.assertRaises(OSError): self.agy.open('rb')
            self.assertIsNotNone(t3code_lifecycle.current_boundary(self.t3))
            with closing(sqlite3.connect(self.t3/t3code_lifecycle.OWNER, timeout=0)) as db:
                with self.assertRaises(sqlite3.OperationalError): db.execute('BEGIN EXCLUSIVE')
            observed.append(True)
            return original(*args, **kwargs)
        with patch.object(native, 'apply', inspect):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertEqual(observed, [True])
