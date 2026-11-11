"""Real Chromium persistence and exact-copy recovery regression tests."""
from contextlib import nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import string
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import t3code_desktop_cleanup as d
from local_agent_manager.operation_store import plan_sha256
from local_agent_manager.t3code_store import T3CodeError

ENV = 'env-selected'
OTHER = 'env-other'
IDS = ['root-inline', 'thread:delegated-task:command%3Amcp%3Aencoded']


def fixture():
    def row(key, value):
        return {'key': key, 'value': value}
    def store(name, records):
        return {'name': name, 'keyPath': None, 'autoIncrement': False,
                'indexes': [{'name': 'environment-updated', 'keyPath': ['environmentId', 'updatedAt', 'bytes'],
                             'unique': False, 'multiEntry': False}] if name == 'query' else [], 'records': records}
    rng = random.Random(319)
    external = ''.join(rng.choices(string.ascii_letters + string.digits, k=750000))
    def thread(environment, id, body):
        return row(f'{environment}:{id}', json.dumps({'schemaVersion': 5, 'environmentId': environment,
            'threadId': id, 'snapshot': {'snapshotSequence': 81, 'projection': {'thread': {'id': id},
            'messages': [{'text': body}], 'turnItems': [], 'visibleTurnItems': []}}}))
    shell = {'schemaVersion': 3, 'environmentId': ENV, 'snapshot': {'snapshotSequence': 81,
             'projects': [{'id': 'retained-project'}], 'threads': [{'id': id} for id in [*IDS, 'keep']],
             'archivedThreads': []}, 'origin': {'preserve': 'origin-fields'}, 'retainedField': [1, 2]}
    payload = json.dumps({'safe': 'target-owned query'})
    queries = [row(ENV + ':batch', {'rows': 8, 'bytes': 72})]
    for env, id in [(ENV, IDS[0]), (ENV, 'keep'), (OTHER, IDS[0])]:
        key = env + ':v1:' + json.dumps(['thread.query', {'threadId': id}], separators=(',', ':'))
        queries.append(row(key, {'environmentId': env, 'payload': payload, 'updatedAt': 1,
                                 'bytes': len(payload.encode())}))
    return [{'name': 't3code:connection-runtime', 'version': 5, 'stores': [
        store('catalog', [row('document', '{"retained":"catalog"}')]), store('query', queries),
        store('server-config', [row(ENV, '{"retained":"config"}')]), store('shell', [row(ENV, json.dumps(shell))]),
        store('thread', [thread(ENV, IDS[0], 'compressed-inline ' * 200), thread(ENV, IDS[1], external),
                         thread(ENV, 'keep', external[::-1]), thread(OTHER, IDS[0], 'other env body')]),
        store('vcs-refs', [row(ENV + ':cwd', '{"retained":"refs"}')])]},
        {'name': 't3code:cloud-auth', 'version': 1, 'stores': [store('keys', [row('key', {'opaque': 'synthetic auth'})])]},
        {'name': 't3code:project-favicons', 'version': 2, 'stores': [store('images', [row('icon', 'synthetic favicon')])]}]


@unittest.skipUnless(os.name == 'nt' and (d.default_runtime() / 'T3 Code (Nightly).exe').is_file(),
                     'requires existing pinned Electron distribution; installs nothing')
class DesktopWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime_context = d.isolated_runtime(d.default_runtime())
        cls.runtime = cls.runtime_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.runtime_context.__exit__(None, None, None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lam-t3-regression-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        d.observe(self.runtime, self.profile, ENV, IDS, native=True, mode='seed', seed=fixture())
        for name in ['draft', 'attachment-1', 'attachment-2']:
            (self.profile / name).write_bytes(('retained-' + name).encode())
        self.evidence = d.freeze(self.profile, self.runtime, ENV, IDS)

    def plan(self):
        plan = {'schema_version': d.SCHEMA, 'operation_id': 'test-residual', 'source_operation': 'old-history',
                'source_plan_sha256': 'f' * 64, 'lifecycle': {'root': str(self.root)}, 'service_absence': {},
                'runtime_root': str(d.default_runtime()), 'runtime_files': [], 'helpers': d.helpers_fingerprint(),
                'evidence': self.evidence}
        plan['lifecycle']['binding_manifest'] = {'environment_id': ENV}
        plan['plan_sha256'] = plan_sha256(plan)
        return plan

    def patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(d.lifecycle, 'hold', return_value=nullcontext()))
        stack.enter_context(patch.object(d, 'service_absence', return_value={}))
        stack.enter_context(patch.object(d, 'isolated_runtime', side_effect=lambda *a, **kw: nullcontext(self.runtime)))
        return stack

    def test_native_cold_writer_preserves_mixed_scope_and_opaque_records(self):
        self.assertEqual(self.evidence['native']['counts'], {'thread': 2, 'shell': 2, 'query': 1})
        self.assertGreaterEqual(len(self.evidence['physical']['owners']), 2)
        before = d.source_files(self.profile)
        with d.prepared(self.evidence, self.runtime) as (stage, files, proof):
            self.assertEqual(proof['counts'], {'thread': 0, 'shell': 0, 'query': 0})
            self.assertEqual(len(proof['owners']), len(self.evidence['physical']['owners']) - 1)
        self.assertEqual(before, d.source_files(self.profile))
        plan = self.plan()
        with self.patches():
            result = d.apply_plan(plan, plan['plan_sha256'], clients_closed=True)
            self.assertTrue(result['goal_satisfied'])
            self.assertEqual(d.status_plan(plan)['attempts'], 1)
            self.assertTrue(d.verify_plan(plan, clients_closed=True)['goal_satisfied'])
            d.require_absent(self.profile, ENV, IDS)
            with self.assertRaises(T3CodeError):
                d.apply_plan(plan, plan['plan_sha256'], clients_closed=True)
        for name in ['draft', 'attachment-1', 'attachment-2']:
            self.assertEqual((self.profile / name).read_bytes(), ('retained-' + name).encode())
        self.assertFalse((d.journal_root(self.profile) / plan['operation_id'] / 'after').exists())

    def test_native_transaction_abort_changes_no_logical_values(self):
        before = self.evidence['native']
        with self.assertRaisesRegex(T3CodeError, 'transaction_aborted'):
            d.observe(self.runtime, self.profile, ENV, IDS, native=True, mode='apply', abortTransaction=True,
                      beforeSha256=before['beforeSha256'], afterSha256=before['expectedAfterSha256'])
        after = d.observe(self.runtime, self.profile, ENV, IDS, native=True)
        self.assertEqual(after['beforeSha256'], before['beforeSha256'])

    def test_changed_source_rejected_before_install(self):
        plan = self.plan()
        target = self.profile / d.LEVEL / 'LOG'
        with target.open('ab') as stream:
            stream.write(b'changed')
        with self.patches(), self.assertRaisesRegex(T3CodeError, 'source_changed'):
            d.apply_plan(plan, plan['plan_sha256'], clients_closed=True)
        self.assertFalse((d.journal_root(self.profile) / plan['operation_id']).exists())

    def test_interrupted_install_is_unknown_not_replayed_then_recovers_seal(self):
        plan = self.plan()
        def interrupt(phase):
            if phase == 'family_removed':
                raise RuntimeError('synthetic interruption')
        with self.patches():
            with self.assertRaises(RuntimeError):
                d.apply_plan(plan, plan['plan_sha256'], clients_closed=True, phase_callback=interrupt)
            self.assertEqual(d.status_plan(plan)['goal_status'], 'unknown')
            with self.assertRaises(T3CodeError):
                d.apply_plan(plan, plan['plan_sha256'], clients_closed=True)
            with self.assertRaises(T3CodeError):
                d.admission(self.profile)
            with self.assertRaises(T3CodeError):
                d.verify_plan(plan, clients_closed=True)
            result = d.verify_plan(plan, clients_closed=True, recover=True)
            self.assertTrue(result['goal_satisfied'])
            self.assertEqual(d.status_plan(plan)['attempts'], 1)

    def test_preinstall_interruption_is_proved_not_applied(self):
        plan = self.plan()
        original_copy = d.copy_source
        def interrupt_copy(root, target, files):
            if Path(target).name == 'after':
                Path(target).mkdir()
                raise RuntimeError('synthetic seal interruption')
            return original_copy(root, target, files)
        with self.patches(), patch.object(d, 'copy_source', side_effect=interrupt_copy):
            with self.assertRaises(RuntimeError):
                d.apply_plan(plan, plan['plan_sha256'], clients_closed=True)
        with self.patches():
            result = d.verify_plan(plan, clients_closed=True)
            self.assertFalse(result['mutation_started'])
            self.assertEqual(d.status_plan(plan)['goal_status'], 'blocked')
            d.admission(self.profile)
            with self.assertRaises(T3CodeError):
                d.apply_plan(plan, plan['plan_sha256'], clients_closed=True)

    def test_recovery_refuses_new_unowned_file(self):
        plan = self.plan()
        def interrupt(phase):
            if phase == 'family_removed':
                raise RuntimeError('synthetic interruption')
        with self.patches():
            with self.assertRaises(RuntimeError):
                d.apply_plan(plan, plan['plan_sha256'], clients_closed=True, phase_callback=interrupt)
            folder = self.profile / d.LEVEL
            folder.mkdir()
            (folder / 'new-unowned').write_bytes(b'keep')
            with self.assertRaisesRegex(T3CodeError, 'recovery_source_changed'):
                d.verify_plan(plan, clients_closed=True, recover=True)
            self.assertEqual((folder / 'new-unowned').read_bytes(), b'keep')
            self.assertEqual(d.status_plan(plan)['goal_status'], 'unknown')

    def test_interrupted_after_file_copy_recovers_known_prefix(self):
        plan = self.plan()
        original_copy = d.copy_source
        def partial_copy(root, target, files):
            if Path(target) == self.profile and Path(root).name == 'after':
                item = files[0]
                _, body = d.ff.read_file(Path(root), item['path'], content=True)
                path = Path(target) / item['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body[:max(1, len(body) // 2)])
                raise RuntimeError('synthetic partial file copy')
            return original_copy(root, target, files)
        with self.patches(), patch.object(d, 'copy_source', side_effect=partial_copy):
            with self.assertRaises(RuntimeError):
                d.apply_plan(plan, plan['plan_sha256'], clients_closed=True)
        with self.patches():
            self.assertEqual(d.status_plan(plan)['goal_status'], 'unknown')
            self.assertTrue(d.verify_plan(plan, clients_closed=True, recover=True)['goal_satisfied'])
            self.assertEqual(d.status_plan(plan)['attempts'], 1)

    def test_fresh_plan_uses_bound_profile_and_new_hash(self):
        original = {'operation_id': 'original', 'actions': [{'kind': 'delete_t3code_frontend',
            'impact': {'external_action_payload': {'t3code_evidence': {
                'root': str(self.root), 'selected_thread_ids': IDS,
                'binding_manifest': {'profile_root': str(self.root), 'environment_id': ENV,
                    'runtime_binaries': [], 'desktop_profiles': [{'root': str(self.profile), 'runtime_binaries': []}]}
            }}}}]}
        original['plan_sha256'] = plan_sha256(original)
        path = self.root / 'original.json'
        path.write_text(json.dumps(original))
        with self.patches(), patch.object(d.lifecycle, 'freeze', return_value={}), patch.object(d, 'runtime_files', return_value=[]):
            plan = d.new_plan(path, self.profile, d.default_runtime())
        self.assertEqual(plan['evidence']['ids'], sorted(IDS))
        self.assertNotEqual(plan['plan_sha256'], original['plan_sha256'])
        self.assertEqual(json.loads(path.read_text()), original)
        with self.assertRaises(T3CodeError):
            d.validate_plan(plan, '0' * 64)

    def test_shared_query_reference_and_unknown_schema_fail_closed(self):
        for kind in ('shared', 'schema'):
            profile = self.root / kind
            profile.mkdir()
            seed = fixture()
            cache = seed[0]
            if kind == 'schema':
                cache['version'] = 6
            else:
                query = next(s for s in cache['stores'] if s['name'] == 'query')
                value = json.dumps({'threads': [IDS[0], 'keep']})
                query['records'].append({'key': ENV + ':v1:["search",{}]', 'value': {
                    'environmentId': ENV, 'payload': value, 'updatedAt': 1, 'bytes': len(value.encode())}})
            with self.assertRaises(T3CodeError):
                d.observe(self.runtime, profile, ENV, IDS, native=True, mode='seed', seed=seed)
            with self.assertRaises(T3CodeError):
                d.freeze(profile, self.runtime, ENV, IDS)


if __name__ == '__main__':
    unittest.main()
