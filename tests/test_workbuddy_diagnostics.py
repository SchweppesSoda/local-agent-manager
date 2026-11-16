import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from local_agent_manager import client_artifacts as rules, artifact_maintenance as maintenance
from local_agent_manager.workbuddy_runtime import WorkBuddyRuntimeError


class WorkBuddyDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='lam-workbuddy-diagnostics-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = time.time_ns() - 60 * 86400 * 10**9
        self.cutoff = time.time_ns() - 14 * 86400 * 10**9

    def file(self, path, body):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode() if isinstance(body, str) else body)
        os.utime(p, ns=(self.old, self.old))
        return p

    def inspect(self):
        return rules.inspect_profile('workbuddy', self.root, categories=['logs'], cutoff_ns=self.cutoff, freeze=True)

    def selected(self):
        return [p['path'] for c in self.inspect()['changes'] for p in c['evidence']['files']]

    def sdk(self, body, name='operation.log'):
        return self.file('logs/2020-01-01/sdk/conversations/' + name, body)

    def logger(self, kind, completed=True):
        relative = f'logs/{kind}/{kind}-20200101.log'
        text = '\n' + '=' * 80 + '\n  Session started at 2020-01-01T00:00:00.000Z\n' + '=' * 80
        text += '\n[2020-01-01T00:00:00.000Z] [INFO ] Logger initialized at ' + str(self.root / relative).replace('/', '\\') + '\n'
        if completed:
            text += '[2020-01-01T00:00:01.000Z] [INFO ] === Migration Completed ===\n[2020-01-01T00:00:01.000Z] [INFO ] Status: SUCCESS\n'
        return self.file(relative, text)

    def crash(self, name='crash-report-daemon-123-20200101T000000.json'):
        obj = {'launchedAt': '2020-01-01T00:00:00Z', 'pid': 123, 'processName': 'daemon',
               'versions': {'node': '22.0.0'}, 'platform': 'win32', 'arch': 'x64', 'appVersion': '5.7.6',
               'entries': [{'timestamp': '2020-01-01T00:00:01Z', 'type': 'child_process_crash',
                            'errorName': 'ChildProcessCrash', 'errorMessage': 'process exited'}], 'droppedCount': 0}
        return self.file('logs/Crash-Log/' + name, json.dumps(obj))

    def test_sdk_metadata_is_distinct_from_native_conversations_and_embedded_payload(self):
        p = self.sdk('2020-01-01T00:00:00Z list {"filter":{"transport":"local","page":1}}\n')
        native = self.file('projects/example/session.jsonl', '{"role":"user","content":"preserve"}')
        self.assertEqual(self.selected(), [p.relative_to(self.root).as_posix()])
        p.write_text(p.read_text() + '2020-01-01T00:00:01Z list {"filter":{"messages":["preserve"]}}\n')
        os.utime(p, ns=(self.old, self.old))
        self.assertEqual(self.selected(), [])
        self.assertIn('preserve', native.read_text())

    def test_conversation_named_diagnostic_validates_every_nested_and_encoded_row(self):
        name = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.log'
        p = self.sdk('2020-01-01T00:00:00Z LOCAL_HTTP_CONNECTION >> session/prompt blocks '
                     '{"blockCount":1,"blocks":[{"type":"text","missing":[]}]}\n', name)
        self.assertEqual(len(self.selected()), 1)
        self.sdk('2020-01-01T00:00:00Z LOCAL_HTTP_CONNECTION >> highfreq ' + json.dumps({
            'line': '[LocalHttpConnection] request ok {"content":"preserve"}'}) + '\n', name)
        self.assertEqual(self.selected(), [])
        self.sdk('2020-01-01T00:00:00Z unknown-new-event {"state":"done"}\n', name)
        self.assertEqual(self.selected(), [])

    def test_recent_timestamps_invalid_dates_and_unknown_subtrees_stay(self):
        self.sdk('2099-01-01T00:00:00Z list {"filter":{}}\n')
        self.file('logs/2020-99-99/sdk/conversations/operation.log', '2020-01-01T00:00:00Z list {"filter":{}}\n')
        self.file('logs/2020-01-01/sdk/messages/history.log', '2020-01-01T00:00:00Z list {"filter":{}}\n')
        self.assertEqual(self.selected(), [])

    def test_completed_migration_and_update_diagnostics_do_not_select_recovery_or_installers(self):
        migration = self.logger('migration')
        update = self.logger('update', completed=False)
        native = self.file('.workbuddy-sqlite-migrations/migration.json', '{"pending":true}')
        installer = self.file('logs/update/pending-installer.exe', b'MZinstaller')
        self.assertCountEqual(self.selected(), [p.relative_to(self.root).as_posix() for p in (migration, update)])
        self.logger('migration', completed=False)
        self.assertEqual(self.selected(), [update.relative_to(self.root).as_posix()])
        self.assertTrue(native.exists() and installer.exists())

    def test_startup_and_performance_marks_reject_session_payload(self):
        path = 'logs/perf/worker-startup-20200101-000000.jsonl'
        obj = {'_type': 'mark', 'phase': 'worker_started', 'source': 'daemon', 'proc': 'daemon', 'data': {'blockCount': 2}}
        self.file(path, '[2020-01-01T00:00:00Z] ' + json.dumps(obj) + '\n')
        self.assertEqual(self.selected(), [path])
        obj['data']['content'] = 'preserve'
        self.file(path, '[2020-01-01T00:00:00Z] ' + json.dumps(obj) + '\n')
        self.assertEqual(self.selected(), [])

    def test_zero_logging_buffers_and_ansi_editor_logs(self):
        path = 'logs/sandbox/20200101/sandbox_center_123.mmap3'
        self.file(path, bytes(150 * 1024))
        editor = 'logs/editor_sdk/editor_sdk.log'
        self.file(editor, '\x1b[32m2020-01-01 00:00:00.001 [123] I/SDK:GET /health\x1b[39m\n')
        self.assertCountEqual(self.selected(), [path, editor])
        self.file(path, b'X' + bytes(150 * 1024 - 1))
        self.assertEqual(self.selected(), [editor])

    def test_xlog_framing_rejects_truncation_and_unpaired_counters(self):
        path = 'logs/weixinpay/weixinpay_20200101.xlog'
        raw = bytes([7]) + struct.pack('<HBBI', 1, 12, 12, 8) + bytes(64) + b'payload!' + b'\0'
        self.file(path, raw)
        self.file(path[:-5] + '.seq', b'2')
        self.assertEqual(len(self.selected()), 2)
        self.file(path, raw[:-1])
        self.assertEqual(self.selected(), [])

    def test_pending_crash_entries_and_cursor_preserved_processed_report_qualifies(self):
        p = self.crash()
        cursor = self.file('logs/Crash-Log/.processed-crashes.json', json.dumps({'files': {p.name: 0}}))
        self.assertEqual(self.selected(), [])
        reasons = {x['reason'] for x in self.inspect()['observations']}
        self.assertIn('crash_entries_pending_native_processing', reasons)
        self.file('logs/Crash-Log/.processed-crashes.json', json.dumps({'files': {p.name: 1}}))
        report = self.inspect()
        self.assertEqual(len(report['changes']), 1)
        self.assertEqual([x['path'] for x in report['dependencies']], [cursor.relative_to(self.root).as_posix()])
        self.file('logs/Crash-Log/.processed-crashes.json', '{"files": []}')
        self.assertEqual(self.selected(), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusion and operation protocol')
    def test_live_writer_blocks_before_mutation_and_fresh_closed_apply_preserves_store(self):
        p = self.sdk('2020-01-01T00:00:00Z list {"filter":{}}\n')
        native = self.file('projects/example/record.jsonl', '{"role":"user","content":"keep"}')
        plan = maintenance.plan('workbuddy', self.root, categories=['logs'])
        with patch('local_agent_manager.workbuddy_runtime.require_closed', side_effect=WorkBuddyRuntimeError('workbuddy_writer_running')):
            result = maintenance.apply(plan['plan_path'], approved_hash=plan['plan_sha256'], clients_closed=True)
        self.assertFalse(result['mutation_started'])
        self.assertTrue(p.exists())
        with patch('local_agent_manager.workbuddy_runtime.require_closed') as closed:
            result = maintenance.apply(plan['plan_path'], approved_hash=plan['plan_sha256'], clients_closed=True)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertEqual(closed.call_count, 2)
        self.assertFalse(p.exists())
        self.assertIn('keep', native.read_text())

    @unittest.skipUnless(os.name == 'nt', 'Windows frozen dependency checks')
    def test_crash_cursor_change_invalidates_plan(self):
        p = self.crash()
        cursor = self.file('logs/Crash-Log/.processed-crashes.json', json.dumps({'files': {p.name: 1}}))
        plan = maintenance.plan('workbuddy', self.root, categories=['logs'])
        cursor.write_text(json.dumps({'files': {p.name: 0}}))
        with patch('local_agent_manager.workbuddy_runtime.require_closed'):
            result = maintenance.apply(plan['plan_path'], approved_hash=plan['plan_sha256'], clients_closed=True)
        self.assertFalse(result['mutation_started'])
        self.assertTrue(p.exists())
