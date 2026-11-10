"""Synthetic CLI stores; no live profile, credentials or process termination."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import antigravity_cli_cleanup as agy
from local_agent_manager import antigravity_cli_guards as guards
from local_agent_manager import antigravity_cli_operations as operations
from local_agent_manager.cleanup_service import CleanupService
from local_agent_manager.operation_coordinator import OperationCoordinator
from local_agent_manager.operation_store import OperationStore
from local_agent_manager.windows_held_files import HeldFiles

TARGET = '11111111-1111-4111-8111-111111111111'
KEEP = '22222222-2222-4222-8222-222222222222'
CHILD = '33333333-3333-4333-8333-333333333333'
SECRET = 'PRIVATE_SYNTHETIC_SESSION_BODY'


def fixture(base, *, child=False):
    root = base/'profile/.gemini/antigravity-cli'
    (root/'conversations').mkdir(parents=True)
    (root/'cache').mkdir()
    binary = base/'agy.exe'; binary.write_bytes(b'fixture executable never launched')
    ids = [TARGET, KEEP, *([CHILD] if child else [])]
    with closing(sqlite3.connect(root/agy.SUMMARY)) as db:
        for obj in agy.CONTRACT['objects']:
            if obj[0] == 'table': db.execute(obj[3])
        for obj in agy.CONTRACT['objects']:
            if obj[0] == 'index': db.execute(obj[3])
        for sid in ids:
            db.execute('''INSERT INTO conversation_summaries
                (conversation_id,title,preview,last_modified_time,workspace_uris,last_user_input_time,
                 parent_conversation_id,nesting_depth,raw_summary) VALUES(?,?,?,?,?,?,?,?,?)''',
                (sid, SECRET, SECRET, '2026-11-10T00:00:00Z', '[]', '2026-11-10T00:00:00Z',
                 TARGET if sid == CHILD else '', 1 if sid == CHILD else 0, b'synthetic summary'))
        db.commit()
    for sid in ids:
        with closing(sqlite3.connect(root/'conversations'/(sid+'.db'))) as db:
            for obj in agy.SESSION_CONTRACT['objects']:
                if obj[0] == 'table': db.execute(obj[3])
            for obj in agy.SESSION_CONTRACT['objects']:
                if obj[0] == 'index': db.execute(obj[3])
            db.execute('INSERT INTO trajectory_meta VALUES(?,?,4,17)', (sid, sid))
            db.execute('INSERT INTO trajectory_metadata_blob VALUES(?,?)', ('main', SECRET.encode()))
            db.commit()
        artifact = root/'brain'/sid/'artifact.md'
        artifact.parent.mkdir(parents=True); artifact.write_text(SECRET)
        (root/'annotations').mkdir(exist_ok=True)
        (root/'annotations'/(sid+'.pbtxt')).write_text('title: "synthetic title"\n')
        (root/'presence').mkdir(exist_ok=True)
        (root/'presence'/(sid+'.lock')).touch()
    (root/agy.CACHE).write_text(json.dumps({str(base/'work'): TARGET, str(base/'keep'): KEEP}))
    (root/'settings.json').write_text('{"fixture":"preserve"}')
    return root, binary


class CliFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lam-agy-test-')
        self.base = Path(self.temp.name).resolve()
        self.root, self.binary = fixture(self.base)
        self.patch = patch.dict(os.environ, {'LOCALAPPDATA': str(self.base/'local'),
            'XDG_STATE_HOME': str(self.base/'state'), 'T3CODE_HOME': str(self.base/'no-t3'),
            'PASEO_HOME': str(self.base/'no-paseo')})
        self.patch.start()
        self.home = patch('pathlib.Path.home', return_value=self.base)
        self.home.start()

    def tearDown(self):
        self.home.stop(); self.patch.stop(); self.temp.cleanup()

    def freeze(self):
        return agy.freeze(self.root, [TARGET], binary=self.binary)

    def coordinator(self):
        return OperationCoordinator(CleanupService())

    def plan(self, coordinator=None):
        return (coordinator or self.coordinator()).plan_operation(client='antigravity', record_ids=[TARGET],
            antigravity_root=self.root, antigravity_bin=self.binary, plan_path=self.base/'plan.json')

    def test_frozen_evidence_excludes_bodies_and_credentials(self):
        evidence = self.freeze()
        self.assertNotIn(SECRET, json.dumps(evidence))
        self.assertEqual(evidence['ids'], [TARGET])
        self.assertGreater(agy.remaining(evidence), 0)

    def test_unknown_database_schema_blocks(self):
        with closing(sqlite3.connect(self.root/agy.SUMMARY)) as db:
            db.execute('CREATE TABLE extra (id text)'); db.commit()
        with self.assertRaisesRegex(agy.CleanupError, 'schema_unqualified'): self.freeze()

    def test_opaque_parent_blocks(self):
        with closing(sqlite3.connect(self.root/'conversations'/(KEEP+'.db'))) as db:
            db.execute('INSERT INTO parent_references VALUES(1,?)', (b'opaque',)); db.commit()
        with self.assertRaisesRegex(agy.CleanupError, 'opaque_trajectory'): self.freeze()

    def test_known_children_expand_and_child_only_blocks(self):
        other = self.base/'other'; other.mkdir()
        root, binary = fixture(other, child=True)
        evidence = agy.freeze(root, [TARGET], binary=binary)
        self.assertEqual(evidence['ids'], sorted([TARGET, CHILD]))
        with self.assertRaisesRegex(agy.CleanupError, 'unselected_parent'):
            agy.freeze(root, [CHILD], binary=binary)

    def test_unselected_embedded_restore_reference_blocks(self):
        with closing(sqlite3.connect(self.root/'conversations'/(KEEP+'.db'))) as db:
            db.execute('UPDATE trajectory_metadata_blob SET data=?', (TARGET.encode(),)); db.commit()
        with self.assertRaisesRegex(agy.CleanupError, 'restore_reference'): self.freeze()

    def test_unknown_cache_and_implicit_restore_block(self):
        (self.root/'cache/last_conversations.json.bak').write_text('{}')
        with self.assertRaisesRegex(agy.CleanupError, 'cache_layout'): self.freeze()
        (self.root/'cache/last_conversations.json.bak').unlink()
        (self.root/'implicit').mkdir(); (self.root/'implicit/ref.pb').write_bytes(TARGET.encode())
        with self.assertRaisesRegex(agy.CleanupError, 'profile_restore'): self.freeze()

    def test_preserved_drift_and_new_session_block_verification(self):
        evidence = self.freeze()
        (self.root/'brain'/KEEP/'artifact.md').write_text('changed')
        with self.assertRaisesRegex(agy.CleanupError, 'preserved'): agy.remaining(evidence)

    def test_other_cache_and_unselected_artifact_references_block(self):
        for path in (self.root/'cache/opaque.bin', self.root/'brain'/KEEP/'ref.json'):
            with self.subTest(path=path.name):
                path.write_bytes(TARGET.encode())
                with self.assertRaisesRegex(agy.CleanupError, 'profile_restore_reference'):
                    self.freeze()
                path.unlink()

    def test_annotation_presence_and_diagnostic_link_contract(self):
        annotation = self.root/'annotations'/(TARGET+'.pbtxt')
        before = annotation.read_bytes()
        annotation.write_text('title: "fixture"\nunknown: true\n')
        with self.assertRaisesRegex(agy.CleanupError, 'annotation_format'):
            self.freeze()
        annotation.write_bytes(before)
        presence = self.root/'presence'/(TARGET+'.lock')
        presence.write_bytes(b'unknown state')
        with self.assertRaisesRegex(agy.CleanupError, 'presence_format'):
            self.freeze()
        presence.write_bytes(b'')
        (self.root/'log').mkdir()
        (self.root/'log/cli-fixture.log').write_text(SECRET)
        link = self.root/'cli.log'
        try:
            link.symlink_to('log/cli-fixture.log')
        except OSError:
            self.skipTest('Symbolic links require host support')
        evidence = self.freeze()
        self.assertEqual(evidence['log_link']['target'], 'log/cli-fixture.log')
        link.unlink(); link.symlink_to('../outside.log')
        with self.assertRaisesRegex(agy.CleanupError, 'log_link_unqualified'):
            agy.remaining(evidence)

    def test_scope_requires_exact_ids_and_explicit_runtime(self):
        result = self.coordinator().plan_operation(client='antigravity', all_projects=True)
        self.assertEqual(result['goal_status'], 'blocked')
        result = self.coordinator().plan_operation(client='antigravity', record_ids=[TARGET], antigravity_root=self.root)
        self.assertEqual(result['blockers'][0]['blocker_code'], 'antigravity_cli_explicit_runtime_required')

    def test_t3_history_and_paseo_restore_guards(self):
        from tests.t3code_support import create_store, add_thread
        t3 = self.base/'t3'
        with closing(create_store(t3)) as db:
            add_thread(db, KEEP, native=TARGET, driver='antigravity')
        with self.assertRaisesRegex(agy.CleanupError, 't3code_native_or_restore'):
            guards.freeze([{'client': 't3code', 'root': str(t3)}], [TARGET])
        paseo = self.base/'paseo'; (paseo/'agents').mkdir(parents=True)
        row = {'id': KEEP, 'provider': 'antigravity', 'cwd': str(self.base),
               'createdAt': '2026-11-10T00:00:00Z', 'updatedAt': '2026-11-10T00:00:00Z',
               'persistence': {'provider': 'antigravity', 'sessionId': TARGET}}
        (paseo/'agents'/(KEEP+'.json')).write_text(json.dumps(row))
        with self.assertRaisesRegex(agy.CleanupError, 'paseo_native_or_restore'):
            guards.freeze([{'client': 'paseo', 'root': str(paseo)}], [TARGET])

    def test_compatibility_aliases(self):
        from local_agent_record_janitor import antigravity_cli_cleanup, antigravity_cli_operations, antigravity_cli_guards
        self.assertIs(antigravity_cli_cleanup, agy)
        self.assertIs(antigravity_cli_operations, operations)
        self.assertIs(antigravity_cli_guards, guards)

    def test_t3_backup_sources_block_even_without_main_database_reference(self):
        from tests.t3code_support import create_store
        t3 = self.base/'t3'
        with closing(create_store(t3)):
            pass
        sources = [{'client': 't3code', 'root': str(t3)}]
        guards.freeze(sources, [TARGET])
        for relative in ('userdata/statev2.sqlite.bak', 'userdata/backups/copy.bin', 'userdata/snapshots/copy.bin'):
            path = t3/relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'opaque recovery fixture')
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, 'recovery_'):
                guards.freeze(sources, [TARGET])
            path.unlink()

    def test_public_records_are_metadata_only(self):
        from local_agent_manager.cli import main
        out = io.StringIO()
        code = main(['records', '--client', 'antigravity', '--antigravity-root', str(self.root),
                     '--record-id', TARGET, '--json'], stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0, out.getvalue())
        value = json.loads(out.getvalue())
        self.assertEqual([r['record_id'] for r in value['records']], [TARGET])
        self.assertNotIn(SECRET, out.getvalue())
        human = io.StringIO()
        self.assertEqual(main(['records', '--client', 'antigravity', '--antigravity-root', str(self.root),
                              '--record-id', TARGET], stdout=human, stderr=io.StringIO()), 0)
        self.assertIn(TARGET, human.getvalue())
        self.assertNotIn(KEEP, human.getvalue())
        self.assertNotIn(SECRET, human.getvalue())

    def test_restore_map_removes_exact_entry_and_preserves_bytes(self):
        from local_agent_manager.antigravity_cli_restore import proto_after, proto_entries
        def field(number, raw):
            self.assertLess(len(raw), 128)
            return bytes([number*8+2, len(raw)])+raw
        entries = {sid: field(1, field(1, sid.encode())+field(2, b'summary-'+sid.encode())) for sid in (TARGET, KEEP)}
        raw = entries[TARGET]+entries[KEEP]
        summaries = {sid: hashlib.sha256(b'summary-'+sid.encode()).hexdigest() for sid in (TARGET, KEEP)}
        self.assertEqual(proto_after(raw, [TARGET], summaries), entries[KEEP])
        self.assertEqual(set(proto_entries(raw)), {TARGET, KEEP})
        with self.assertRaisesRegex(agy.CleanupError, 'duplicate'):
            proto_after(raw+entries[TARGET], [TARGET], summaries)
        with self.assertRaisesRegex(agy.CleanupError, 'mirror_mismatch'):
            proto_after(raw, [TARGET], {TARGET: 'wrong', KEEP: summaries[KEEP]})

    def test_guard_default_cannot_be_omitted_and_new_profile_blocks(self):
        sources = guards.locations()
        frozen = guards.freeze(sources, [TARGET])
        (self.base/'.paseo').mkdir()
        with self.assertRaisesRegex(agy.CleanupError, 'guard_source_changed'):
            guards.recheck(frozen, [TARGET])


@unittest.skipUnless(os.name == 'nt', 'Windows mandatory file leases')
class CliWriterTests(CliFormatTests):
    def apply(self, coordinator, plan, **kwargs):
        return coordinator.apply_operation(operation_id=plan['operation_id'], plan_path=self.base/'plan.json',
            plan_sha256=plan['plan_sha256'], clients_closed=True, census=lambda: [], **kwargs)

    def test_full_cleanup_fresh_coordinator_status_verify_keep(self):
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        keep = {p: p.read_bytes() for p in (self.root/'conversations'/(KEEP+'.db'), self.root/'brain'/KEEP/'artifact.md',
                self.root/'settings.json', self.root/'annotations'/(KEEP+'.pbtxt'), self.root/'presence'/(KEEP+'.lock'))}
        result = self.apply(coordinator, plan)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertFalse((self.root/'conversations'/(TARGET+'.db')).exists())
        self.assertFalse((self.root/'brain'/TARGET).exists())
        self.assertFalse((self.root/'annotations'/(TARGET+'.pbtxt')).exists())
        self.assertFalse((self.root/'presence'/(TARGET+'.lock')).exists())
        with closing(sqlite3.connect(self.root/agy.SUMMARY)) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT conversation_id FROM conversation_summaries')], [KEEP])
        self.assertEqual(json.loads((self.root/agy.CACHE).read_text()), {str(self.base/'keep'): KEEP})
        self.assertEqual({p: p.read_bytes() for p in keep}, keep)
        for method in ('status_operation', 'verify_operation'):
            result = getattr(self.coordinator(), method)(operation_id=plan['operation_id'], plan_path=self.base/'plan.json')
            self.assertEqual(result['goal_status'], 'complete', result)
        self.assertNotIn(SECRET, (self.base/'plan.json').read_text())
        self.assertFalse((OperationStore(self.root, plan['operation_id']).directory/'native-recovery').exists())

    def test_existing_writer_handle_blocks_before_mutation(self):
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        with (self.root/'conversations'/(TARGET+'.db')).open('rb'):
            result = self.apply(coordinator, plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])
        self.assertTrue((self.root/'conversations'/(TARGET+'.db')).exists())
        self.assertEqual(self.apply(self.coordinator(), plan)['goal_status'], 'complete')

    def test_held_handles_reject_competing_write_and_recreation(self):
        evidence = self.freeze()
        observed = []
        def phase(event):
            if event == 'shared_files_written':
                for path in (self.root/agy.SUMMARY, self.root/'conversations'/(TARGET+'.db')):
                    with self.assertRaises(OSError): path.open('wb')
                observed.append(event)
        with agy.execution_scope(evidence):
            agy.apply(evidence, phase_callback=phase, recovery_dir=self.base/'recovery', census=lambda: [])
        self.assertEqual(observed, ['shared_files_written'])

    def test_mid_delete_failure_is_unknown_and_not_replayed(self):
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        original = HeldFiles.dispose
        counter = [0]
        def crash(held, *args, **kwargs):
            counter[0] += 1
            if counter[0] == 2: raise OSError('synthetic crash')
            return original(held, *args, **kwargs)
        with patch.object(HeldFiles, 'dispose', crash): result = self.apply(coordinator, plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertTrue(result['mutation_started'])
        self.assertEqual(self.apply(self.coordinator(), plan)['goal_status'], 'unknown')
        result = self.coordinator().verify_operation(operation_id=plan['operation_id'], plan_path=self.base/'plan.json')
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertTrue((OperationStore(self.root, plan['operation_id']).directory/'native-recovery').exists())

    def test_shared_write_failure_restores_before_images(self):
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        before = (self.root/agy.SUMMARY).read_bytes()
        original = HeldFiles.replace
        count = [0]
        def fail_once(held, *args, **kwargs):
            count[0] += 1
            if count[0] == 2: raise OSError('synthetic shared write failure')
            return original(held, *args, **kwargs)
        with patch.object(HeldFiles, 'replace', fail_once): result = self.apply(coordinator, plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertEqual((self.root/agy.SUMMARY).read_bytes(), before)
        self.assertTrue((self.root/'conversations'/(TARGET+'.db')).exists())

    def test_runtime_census_and_plan_hash_block(self):
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        result = coordinator.apply_operation(operation_id=plan['operation_id'], plan_path=self.base/'plan.json',
            clients_closed=True, plan_sha256='bad')
        self.assertEqual(result['goal_status'], 'blocked')
        result = coordinator.apply_operation(operation_id=plan['operation_id'], plan_path=self.base/'plan.json',
            clients_closed=True, plan_sha256=plan['plan_sha256'], census=lambda: [{'pid': -1, 'name': 'agy.exe'}])
        self.assertEqual(result['goal_status'], 'blocked')
        self.assertFalse(result['mutation_started'])

    def test_state_maintenance_retains_recovery_and_compacts_completed(self):
        from local_agent_manager.state_maintenance import run_state_command
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        self.assertEqual(self.apply(coordinator, plan)['goal_status'], 'complete')
        inventory = run_state_command('inventory', self.root)
        self.assertEqual(inventory['operations'][0]['action'], 'compact', inventory)
        cleanup = run_state_command('cleanup', self.root)
        run_state_command('cleanup', self.root, apply=True, approved_hash=cleanup['plan_sha256'])
        for location in ({'plan_path': self.base/'plan.json'}, {'operation_home': self.root}):
            result = self.coordinator().status_operation(operation_id=plan['operation_id'], **location)
            self.assertEqual(result['goal_status'], 'complete', result)
            self.assertEqual(result['verification_basis'], 'retained_completion_receipt')

    def test_direct_writer_needs_ticket(self):
        with self.assertRaisesRegex(agy.CleanupError, 'execution_ticket'):
            agy.apply(self.freeze(), phase_callback=lambda _: None, recovery_dir=self.base/'recovery', census=lambda: [])

    def test_verify_discards_only_proved_recovery_images(self):
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        original = agy.discard_recovery
        with patch.object(agy, 'discard_recovery', side_effect=OSError('fixture interruption after native deletion')):
            result = self.apply(coordinator, plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        result = self.coordinator().verify_operation(operation_id=plan['operation_id'], plan_path=self.base/'plan.json')
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertFalse((OperationStore(self.root, plan['operation_id']).directory/'native-recovery').exists())

    def test_unknown_sibling_blocks_other_ids(self):
        coordinator = self.coordinator(); plan = self.plan(coordinator)
        with patch.object(HeldFiles, 'dispose', side_effect=OSError('fixture interruption')):
            result = self.apply(coordinator, plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        # Root-wide shared summaries prevent a second operation, even when
        # its selected IDs differ from the unresolved operation's IDs.
        from local_agent_manager.mutation_guard import mutation_guard, MutationScope, UnknownMutationError
        with self.assertRaises(UnknownMutationError):
            with mutation_guard((MutationScope(self.root, frozenset({KEEP})),)):
                self.fail('Unknown shared writer admitted a sibling')

    def test_public_cli_plan_and_run(self):
        from local_agent_manager.cli import main
        out = io.StringIO()
        with patch('local_agent_manager.office_runtime.processes', return_value=[]):
            code = main(['delete', 'run', '--client', 'antigravity', '--record-id', TARGET,
                         '--antigravity-root', str(self.root), '--antigravity-bin', str(self.binary),
                         '--out', str(self.base/'cli.json'), '--clients-closed', '--yes', '--json'],
                        stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['goal_status'], 'complete')


if __name__ == '__main__': unittest.main()
