"""Installed V2 context shapes in projections and both persisted mirrors."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from local_agent_manager import t3code_database as database
from local_agent_manager.t3code_bound_adapter import SCHEMA, T3CodeBoundAdapter
from tests.t3code_support import add_thread, create_store, insert, V2


class ContextReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = create_store(self.root)
        self.addCleanup(self.db.close)
        add_thread(self.db, 'selected', native='selected-native')
        add_thread(self.db, 'keeper', native='keeper-native')
        (self.root / 'userdata/environment-id').write_text('env-test')
        self.manifest = {'schema_version': SCHEMA, 'profile_root': str(self.root),
            'environment_id': 'env-test', 'runtime_binaries': [str(self.root / 'unused.exe')],
            'desktop_profiles': [], 'native_stores': [
                {'thread_id': tid, 'engine': 'codex', 'root': str(self.root)}
                for tid in ('selected', 'keeper')]}

    def handoff(self):
        return {'id': 'handoff', 'transferId': None, 'threadId': 'keeper', 'targetRunId': 'run',
            'fromProviderThreadIds': ['provider-keeper'], 'toProviderThreadId': 'provider-keeper',
            'coveredRunOrdinals': {'from': 1, 'to': 2}, 'strategy': 'full_thread_summary',
            'status': 'ready', 'summaryMessageId': None, 'summaryText': 'selected',
            'history': {'messages': [{'role': 'user', 'text': 'selected-native', 'threadId': 'keeper',
                'runId': None, 'itemId': 'item', 'providerThreadId': None, 'status': 'completed',
                'kind': 'user_message'}], 'coverage': 'all', 'omittedItems': 0},
            'delivery': {'nativeThreadId': 'keeper-native', 'status': 'injected', 'itemIds': []},
            'createdByProviderInstanceId': None, 'createdAt': '2026-11-10T00:00:00Z',
            'updatedAt': '2026-11-10T00:00:00Z'}

    def test_single_scan_keeps_escaped_reference_keys_and_shape_guard(self):
        from local_agent_manager.t3code_bound_adapter import history_references
        payload = json.dumps({'nativeThreadRef': {'driver':'codex','nativeId':'escaped-native','strength':'strong'}})
        payload = payload.replace('nativeThreadRef', 'native\\u0054hreadRef')
        self.db.execute(f'UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id=?', (payload,'selected'))
        self.db.commit()
        refs=history_references(self.root,{b['thread_id']:b for b in self.manifest['native_stores']})
        self.assertTrue(any(r['native_id']=='escaped-native' for r in refs))
        self.db.execute(f'UPDATE {V2}provider_threads SET payload_json=? WHERE thread_id=?',
                        ('{"nativeThreadId":12}', 'selected'))
        self.db.commit()
        with self.assertRaisesRegex(ValueError,'native_reference_shape_unqualified'):
            history_references(self.root,{b['thread_id']:b for b in self.manifest['native_stores']})

    def transfer(self):
        return {'id': 'transfer', 'type': 'provider_handoff', 'sourceThreadId': 'keeper',
            'targetThreadId': 'keeper', 'sourcePoint': {'threadId': 'keeper',
                'providerThreadRef': {'driver': 'codex', 'nativeId': 'keeper-native', 'strength': 'strong'},
                'providerTurnRef': {'driver': 'codex', 'nativeId': 'a-turn-not-a-thread', 'strength': 'strong'}},
            'basePoint': None, 'sourceProviderInstanceId': 'codex', 'targetProviderInstanceId': 'codex',
            'sourceTokenUsage': {'usedTokens': 5, 'updatedAt': '2026-11-10T00:00:00Z'},
            'targetRunId': 'run', 'status': 'consumed', 'resolution': {'strategy': 'native_fork',
                'providerThreadRef': {'driver': 'codex', 'nativeId': 'fork-native', 'strength': 'strong'}},
            'createdBy': 'system', 'error': None, 'createdAt': '2026-11-10T00:00:00Z',
            'updatedAt': '2026-11-10T00:00:00Z', 'consumedAt': '2026-11-10T00:00:00Z'}

    def save(self, storage, kind, payload):
        raw = json.dumps(payload)
        if storage == 'legacy':
            insert(self.db, 'orchestration_events', event_id='context', stream_id='keeper',
                   aggregate_kind='thread', event_type=kind, payload_json=raw, metadata_json='{}')
        elif storage == 'v2':
            insert(self.db, 'orchestration_v2_events', event_id='context', thread_id='keeper',
                   event_type=kind, payload_json=raw)
        elif kind == 'context-handoff.updated':
            insert(self.db, V2+'context_handoffs', context_handoff_id='handoff', thread_id='keeper',
                   target_run_id='run', to_provider_thread_id='provider-keeper', strategy=payload['strategy'],
                   status=payload['status'], payload_json=raw)
        else:
            insert(self.db, V2+'context_transfers', context_transfer_id='transfer',
                   source_thread_id=payload['sourceThreadId'], target_thread_id=payload['targetThreadId'],
                   type=payload['type'], status=payload['status'], payload_json=raw)
        self.db.commit()

    def clear_context(self):
        for table in ('orchestration_events', 'orchestration_v2_events',
                      V2+'context_handoffs', V2+'context_transfers'):
            predicate = "event_id='context'" if 'events' in table else '1'
            self.db.execute(f'DELETE FROM {table} WHERE {predicate}')
        self.db.commit()

    def test_handoff_content_preserved_and_same_thread_delivery_qualified(self):
        for storage in ('projection', 'v2', 'legacy'):
            with self.subTest(storage=storage):
                payload = self.handoff()
                self.save(storage, 'context-handoff.updated', payload)
                self.assertFalse(T3CodeBoundAdapter(self.manifest).snapshot_references().errors)
                evidence = database.freeze(self.root, ['selected'])
                database.apply(evidence, phase_callback=lambda _: None)
                with closing(database.connect_readonly(self.root / database.RELATIVE)) as db:
                    table = {'projection': V2+'context_handoffs', 'v2': 'orchestration_v2_events',
                             'legacy': 'orchestration_events'}[storage]
                    self.assertTrue(db.execute(f'SELECT 1 FROM {table} WHERE payload_json=?',
                                               (json.dumps(payload),)).fetchone())
                add_thread(self.db, 'selected', native='selected-native')
                self.clear_context()

    def test_retained_handoff_delivery_target_remains_a_blocker_in_all_copies(self):
        for storage in ('projection', 'v2', 'legacy'):
            with self.subTest(storage=storage):
                payload = self.handoff()
                payload['delivery']['nativeThreadId'] = 'selected-native'
                self.save(storage, 'context-handoff.updated', payload)
                with self.assertRaisesRegex(ValueError, 'historical_native_reference_unbound'):
                    T3CodeBoundAdapter(self.manifest).snapshot_references()
                with self.assertRaisesRegex(ValueError, 'unselected_restore_reference'):
                    database.freeze(self.root, ['selected'], native_ids=['selected-native'])
                self.clear_context()

    def test_transfer_source_base_and_fork_result_qualified_in_all_copies(self):
        for storage in ('projection', 'v2', 'legacy'):
            with self.subTest(storage=storage):
                payload = self.transfer()
                payload['basePoint'] = {'threadId': 'keeper', 'providerThreadRef': {
                    'driver': 'codex', 'nativeId': 'base-native', 'strength': 'strong'}}
                self.save(storage, 'context-transfer.updated', payload)
                refs = T3CodeBoundAdapter(self.manifest).snapshot_references().references
                self.assertEqual({r.native_id for r in refs if r.frontend_id == 'keeper'},
                                 {'keeper-native', 'base-native', 'fork-native'})
                evidence = database.freeze(self.root, ['keeper'])
                self.assertEqual({r['native_id'] for r in evidence['native_references']},
                                 {'keeper-native', 'base-native', 'fork-native'})
                database.freeze(self.root, ['selected'], native_ids=['selected-native'])
                self.clear_context()

    def test_retained_transfer_native_target_blocks_even_without_a_projection(self):
        for storage in ('projection', 'v2', 'legacy'):
            for point in ('sourcePoint', 'basePoint', 'resolution'):
                with self.subTest(storage=storage, point=point):
                    payload = self.transfer()
                    if point == 'basePoint':
                        payload[point] = {'threadId': 'keeper'}
                    payload[point]['providerThreadRef'] = {
                        'driver': 'codex', 'nativeId': 'selected-native', 'strength': 'strong'}
                    self.save(storage, 'context-transfer.updated', payload)
                    refs = T3CodeBoundAdapter(self.manifest).snapshot_references().references
                    self.assertTrue(any(r.frontend_id == 'keeper' and r.native_id == 'selected-native' for r in refs))
                    with self.assertRaisesRegex(ValueError, 'unselected_restore_reference'):
                        database.freeze(self.root, ['selected'], native_ids=['selected-native'])
                    self.clear_context()

    def test_cross_thread_transfer_and_unknown_restoration_remain_blockers(self):
        payload = self.transfer()
        payload['sourceThreadId'] = 'selected'
        self.save('projection', 'context-transfer.updated', payload)
        with self.assertRaisesRegex(ValueError, 'cross_thread_transfer'):
            database.freeze(self.root, ['selected'])
        self.clear_context()
        for payload in ({**self.handoff(), 'unknownRecovery': {}},
                        {**self.handoff(), 'delivery': {'resumeCursor': 'selected-native'}},
                        {**self.transfer(), 'resolution': {'restoreCursor': 'selected-native'}}):
            kind = 'context-handoff.updated' if payload['id'] == 'handoff' else 'context-transfer.updated'
            self.save('legacy', kind, payload)
            with self.assertRaisesRegex(ValueError, 'legacy_restore_closure_unqualified'):
                T3CodeBoundAdapter(self.manifest).snapshot_references()
            self.clear_context()

    def test_current_interrupt_message_fields_are_qualified(self):
        for kind in ('run_interrupt_request', 'run_interrupt_result'):
            self.save('legacy', 'turn-item.updated', {'id': 'interrupt', 'threadId': 'keeper',
                'type': kind, 'message': 'Synthetic stopped', 'unresponsiveAt': None})
            self.assertFalse(T3CodeBoundAdapter(self.manifest).snapshot_references().errors)
            self.clear_context()

    def test_transfer_unknown_shapes_and_weak_handles_do_not_grant_authority(self):
        for storage in ('projection', 'v2', 'legacy'):
            for ref, error in (('not-an-object', 'native_reference_shape_unqualified'),
                               ({'driver': 'codex', 'nativeId': 'fork-native', 'strength': 'weak'},
                                'native_reference_strength_unqualified')):
                with self.subTest(storage=storage, reference=ref):
                    payload = self.transfer()
                    payload['resolution']['providerThreadRef'] = ref
                    self.save(storage, 'context-transfer.updated', payload)
                    with self.assertRaisesRegex(ValueError, error):
                        T3CodeBoundAdapter(self.manifest).snapshot_references()
                    self.clear_context()


if __name__ == '__main__':
    unittest.main()
