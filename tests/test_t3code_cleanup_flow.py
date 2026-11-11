"""Handoff authorization cannot expand between online and offline planning."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from local_agent_manager.operation_store import plan_sha256, write_new_json
from local_agent_manager.t3code_cleanup_flow import (
    CleanupFlowError, cleanup_scope, execute_cleanup, prepare_cleanup, PreparedCleanup,
)


class CleanupFlowTests(unittest.TestCase):
    def test_offline_native_scope_drift_blocks_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence = dict(schema_version='larj.t3code-cleanup.v1', root=str(root),
                selected_thread_ids=['selected'], native_targets=[dict(engine='codex', root=str(root),
                    ids=['original-native'], paths=[])])
            expected = cleanup_scope(evidence)
            changed = deepcopy(evidence)
            changed['native_targets'][0]['ids'] = ['new-native']
            document = dict(operation_id='offline', plan_path=str(root/'plan.json'), goal_status='ready',
                actions=[dict(kind='delete_t3code_frontend', impact=dict(external_action_payload=dict(t3code_evidence=changed)))])
            document['plan_sha256'] = plan_sha256(document)
            calls = []
            def run(argv, **kwargs):
                calls.append(argv[:2])
                write_new_json(root/'plan.json', document)
                return 0, document
            with self.assertRaises(CleanupFlowError) as error:
                prepare_cleanup(['selected'], [], root/'plan.json', run_cli=run, expected_scope=expected, stage='offline')
            self.assertEqual(error.exception.code, 'offline_scope_drift')
            self.assertEqual(calls, [['delete', 'plan']])

    def test_unknown_apply_uses_status_then_cold_verify_without_replay(self):
        plan = dict(operation_id='op', plan_path='plan.json', plan_sha256='a'*64)
        receipt, calls = {}, []
        def run(argv, **kwargs):
            calls.append(argv[:2])
            if argv[:2] == ['delete', 'apply']:
                return 1, dict(goal_status='unknown', mutation_started=True)
            if argv[:2] == ['operation', 'status']:
                return 1, dict(goal_status='unknown')
            return 0, dict(goal_status='complete', goal_satisfied=True)
        execute_cleanup(PreparedCleanup(plan, {}), [], timeout=30, run_cli=run, coordinator=None,
                        receipt=receipt, event=lambda *a, **k: None)
        self.assertEqual(calls, [['delete','apply'], ['operation','status'], ['operation','verify']])
        self.assertTrue(receipt['goal_satisfied'])

    def test_dispatch_exception_is_never_reported_as_unstarted(self):
        plan = dict(operation_id='op', plan_path='plan.json', plan_sha256='a'*64)
        receipt = {}
        def lost(*args, **kwargs):
            raise OSError('result lost')
        with self.assertRaises(OSError):
            execute_cleanup(PreparedCleanup(plan, {}), [], timeout=30, run_cli=lost, coordinator=None,
                            receipt=receipt, event=lambda *a, **k: None)
        self.assertTrue(receipt['mutation_started'])
        self.assertEqual(receipt['plan']['operation_id'], 'op')
