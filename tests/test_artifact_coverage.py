
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap
"""Coverage regressions use disposable stores, never a user's live profile."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
import zlib
from unittest.mock import patch

from local_agent_manager import client_artifacts as rules, runtime_artifacts as runtimes
from local_agent_manager import artifact_maintenance as maintenance


class ArtifactCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lam-coverage-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = time.time_ns() - 60 * 86400 * 10**9
        self.cutoff = time.time_ns() - 14 * 86400 * 10**9
        census = patch('local_agent_manager.office_runtime.processes', return_value=[])
        census.start()
        self.addCleanup(census.stop)

    def file(self, name, body=b'', old=True):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        if old:
            os.utime(p, ns=(self.old, self.old))
        return p

    def inspect(self, client, categories):
        return rules.inspect_profile(client, self.root, categories=categories,
                                     cutoff_ns=self.cutoff, freeze=True)

    def selected(self, report):
        return [c['evidence']['path'] for c in report['changes']]

    def simple(self, prefix='Code Cache/js'):
        self.file(prefix + '/index', struct.pack('<QIIII', 0xfcfb6d1ba7725c30, 9, 0, 0, 0))
        payload = struct.pack('<QIQQI', 0x656e74657220796f, 9, 1, 100, 2)
        self.file(prefix + '/index-dir/the-real-index', struct.pack('<II', len(payload), zlib.crc32(payload)) + payload)
        body = struct.pack('<QIIII', 0xfcfb6d1ba7725c30, 5, 3, 0, 0) + b'keypayload'
        body += struct.pack('<QIIII', 0xf4fa6f45970d41d8, 0, 0, 7, 0)
        return self.file(prefix + '/0123456789abcdef_0', body)

    def block(self, prefix):
        self.file(prefix + '/index', struct.pack('<II', 0xc103cac3, 0x30000))
        self.file(prefix + '/data_0', b'cache')

    def test_simple_cache_qualifies_but_unexpected_data_blocks_whole_directory(self):
        self.simple()
        self.assertEqual(self.selected(self.inspect('t3code', ['caches'])), ['Code Cache/js'])
        self.file('Code Cache/js/notes.txt', b'user data')
        self.assertEqual(self.selected(self.inspect('t3code', ['caches'])), [])

    def test_simple_cache_rejects_bad_footer_index_and_hardlinks(self):
        entry = self.simple()
        valid = entry.read_bytes()
        entry.write_bytes(valid[:-24] + b'x' * 24)
        self.assertEqual(self.selected(self.inspect('cindy', ['caches'])), [])
        entry.write_bytes(valid)
        index = self.root / 'Code Cache/js/index-dir/the-real-index'
        original = index.read_bytes()
        index.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        self.assertEqual(self.selected(self.inspect('cindy', ['caches'])), [])
        index.write_bytes(original)
        os.link(entry, self.root / 'shared')
        self.assertEqual(self.selected(self.inspect('cindy', ['caches'])), [])

    def test_partition_and_named_browser_cache_preserves_cookies_and_indexeddb(self):
        base = 'Roaming/Codex/web/Codex/'
        paths = [base + 'Default/Partitions/web/Cache/Cache_Data',
                 base + 'codex-browser-app/Cache/Cache_Data']
        for path in paths:
            self.block(path)
        cookie = self.file(base + 'Default/Partitions/web/Network/Cookies', b'login')
        self.file(base + 'Default/Partitions/web/IndexedDB/records', b'drafts')
        self.assertCountEqual(self.selected(self.inspect('codex-desktop', ['caches'])), paths)
        self.assertEqual(cookie.read_bytes(), b'login')

    def test_cindy_webview_and_partition_code_cache(self):
        path = 'Partitions/xdmaker-browser-app/Code Cache/js'
        self.simple(path)
        self.block('EBWebView/Default/Cache/Cache_Data')
        self.assertCountEqual(self.selected(self.inspect('cindy', ['caches'])),
                              [path, 'EBWebView/Default/Cache/Cache_Data'])

    def test_crash_and_component_observations_work_without_cache_category(self):
        self.file('Crashpad/reports/problem.dmp', b'MDMP')
        self.file('component_crx_cache/archive', b'component')
        r = self.inspect('t3code', ['downloads', 'logs'])
        self.assertEqual(self.selected(r), [])
        self.assertTrue({'Crashpad', 'component_crx_cache'} <= {x['path'] for x in r['observations']})

    def test_catalogs_require_registered_namespace_schema_and_retention(self):
        name = 'cache/remote_plugin_catalog/' + 'a' * 16 + '.json'
        self.file(name, json.dumps({'schema_version': 1, 'fetched_at': '2020-01-01', 'plugins': []}).encode())
        self.file('cache/bundled_plugin_exclusions/settings.json', b'{"disabled-bundled-plugin-ids":[]}')
        self.assertEqual(self.selected(self.inspect('codex', ['caches'])), [name])
        os.utime(self.root / name, None)
        self.assertEqual(self.selected(self.inspect('codex', ['caches'])), [])
        self.file(name, b'{"schema_version":99,"plugins":[],"fetched_at":"old"}')
        self.assertEqual(self.selected(self.inspect('codex', ['caches'])), [])

    def test_usage_cache_preserves_unknown_version_and_real_session_data(self):
        obj = {'version': 11, 'models': [], 'sessions': [], 'sources': {}, 'files': {}}
        path = 'userdata/usage-scan-cache-v11.json'
        self.file(path, json.dumps(obj).encode())
        self.file('userdata/statev2.sqlite', b'live history')
        self.file('userdata/usage-scan-cache-v12.json', json.dumps({**obj, 'version': 12}).encode())
        self.assertEqual(self.selected(self.inspect('t3code', ['caches'])), [path])

    def test_sandbox_logs_require_header_date_and_preserve_secrets(self):
        path = '.sandbox/sandbox.2020-01-01.log'
        self.file(path, b'[2020-01-01 08:25:57.273 codex.exe] START: diagnostic\n')
        self.file('.sandbox/sandbox.2020-01-02.log', b'[2020-01-01 08:25:57.273 codex.exe] START: mismatch\n')
        self.file('.sandbox/session.jsonl', b'conversation')
        self.assertEqual(self.selected(self.inspect('codex', ['logs'])), [path])

    def test_browser_skill_dated_diagnostics_preserve_lock_recent_and_unknown_files(self):
        body = json.dumps({'timestamp': '2020-01-01T12:00:00Z', 'level': 'INFO',
                           'fields': {'message': 'diagnostic'}, 'target': 'bsk::daemon::start'}).encode()
        self.file('daemon.log.2020-01-01', body)
        self.file('daemon.log.2020-01-02', body)
        self.file('daemon.log', body)
        self.file('daemon.log.2020-01-03', b'')
        self.file('daemon.lock', b'')
        self.file('session.json', body)
        self.assertEqual(self.selected(self.inspect('browser-skill', ['logs'])), ['daemon.log.2020-01-01', 'daemon.log.2020-01-03'])
        os.utime(self.root / 'daemon.log.2020-01-01', ns=(self.cutoff + 1, self.cutoff + 1))
        self.assertEqual(self.selected(self.inspect('browser-skill', ['logs'])), ['daemon.log.2020-01-03'])

    def test_workbuddy_stale_root_diagnostics_require_the_known_envelope(self):
        self.file('logs/main.log', b'{"timestamp":"2020-01-01T00:00:00Z","level":"info","scope":"window","message":[]}\n')
        self.file('logs/daemon.log', b'{"role":"user","content":"keep"}\n')
        self.file('logs/perf-metrics-cli-pids.json', b'{"pids":[]}')
        self.file('logs/2020-01-01/daemon-memory-diag.log', b'[2020-01-01T00:00:00Z] rss=3.1MB heap=1/2MB\n')
        self.assertCountEqual(self.selected(self.inspect('workbuddy', ['logs'])),
            ['logs/main.log', 'logs/2020-01-01/daemon-memory-diag.log'])

    def test_workbuddy_only_known_dated_logger_envelopes_qualify(self):
        self.file('logs/2020-01-03', b'a date-named file is not a directory')
        path = 'logs/2020-01-01/workbuddyMainThread__' + 'a' * 32 + '.log'
        body = b'Workspace Path: example\n[2020/1/1 08:00:00.000] [Info] [LoggerUploadService] diagnostic\n'
        self.file(path, body)
        self.file('logs/2020-01-01/edge-sync.log', b'[2020-01-01T08:00:00Z] [pid=123] [BOOTSTRAP_ENTER] diagnostic\n')
        self.file('logs/2020-01-01/session.log', body)
        self.file('logs/2020-01-01/nested/transcript.log', body)
        self.file('logs/main.log', body)
        self.assertCountEqual(self.selected(self.inspect('workbuddy', ['logs'])),
                              [path, 'logs/2020-01-01/edge-sync.log'])
        (self.root / path).write_bytes(b'user transcript\n')
        self.assertEqual(self.selected(self.inspect('workbuddy', ['logs'])), ['logs/2020-01-01/edge-sync.log'])

    def test_closed_t3_trace_sinks_qualify_but_transcripts_do_not(self):
        obj = {'type': 'effect-span', 'kind': 'internal', 'name': 'test', 'traceId': 'a' * 32,
               'spanId': 'b' * 16, 'startTimeUnixNano': '100', 'endTimeUnixNano': '200',
               'sampled': True, 'attributes': {}, 'events': [], 'links': [], 'exit': {}}
        body = json.dumps(obj).encode() + b'\n'
        self.file('userdata/logs/server.trace.ndjson.1', body)
        self.file('userdata/logs/desktop.trace.ndjson.1', body)
        otlp = {k: v for k, v in obj.items() if k != 'exit'}
        otlp.update(type='otlp-span', resourceAttributes={}, scope={}, status={})
        self.file('userdata/logs/server.trace.ndjson.3', json.dumps(otlp).encode())
        self.file('userdata/logs/server.trace.ndjson', body)
        self.file('userdata/logs/server.trace.ndjson.2', b'{"type":"message"}\n')
        self.file('userdata/logs/terminals/session.log', body)
        self.assertEqual(self.selected(self.inspect('t3code', ['logs'])),
                         ['userdata/logs/desktop.trace.ndjson.1', 'userdata/logs/server.trace.ndjson', 'userdata/logs/server.trace.ndjson.1',
                          'userdata/logs/server.trace.ndjson.3'])

    def test_t3_diagnostics_validate_every_row_and_preserve_native_payloads(self):
        path = 'userdata/logs/event-loop-stalls.ndjson'
        obj = dict(time='2020-01-01T00:00:00Z', delayMaxMs=12, utilization=0.5,
                   cpuUserMs=2, cpuSystemMs=1, majorPageFaults=0, minorPageFaults=0,
                   involuntaryContextSwitches=1, rssMb=20)
        body = json.dumps(obj).encode() + b'\n'
        self.file(path, body)
        self.file('userdata/logs/provider/events.example.log', body)
        self.assertEqual(self.selected(self.inspect('t3code', ['logs'])), [path])
        self.file(path, body + b'{"role":"user","content":"keep"}\n')
        self.assertEqual(self.selected(self.inspect('t3code', ['logs'])), [])
        self.file(path, json.dumps({**obj, 'message': 'user content'}).encode())
        self.assertEqual(self.selected(self.inspect('t3code', ['logs'])), [])

    def test_t3_runtime_census_blocks_live_or_unreadable_server(self):
        from local_agent_manager.t3code_diagnostics import require_closed
        from local_agent_manager.t3code_store import T3CodeError
        require_closed(self.root, census=lambda: [])
        for row in [dict(pid=999999, parent=1, name='t3code.exe', executable='C:/T3/t3code.exe', command=''),
                    dict(pid=999999, parent=1, name='node.exe', executable='C:/node.exe', command='node C:/t3code/server.mjs'),
                    dict(pid=999999, parent=1, name='node.exe', executable=None, command=None)]:
            with self.assertRaises(T3CodeError):
                require_closed(self.root, census=lambda: [row])

    def test_t3_renderer_and_service_sinks_reject_unexpected_values(self):
        renderer = dict(version=1, mainPid=1, mainSessionStartedAtUnixMs=1, timestampUnixMs=2,
                        event='sample', webContentsId=1, surface='main', rendererPid=2,
                        rendererPidSource='current', rendererCreationTimeMs=1,
                        memory=dict(kind='sample', sampledAtUnixMs=2, workingSetBytes=4, peakWorkingSetBytes=8))
        service = dict(message='diagnostic', level='INFO', timestamp='2020-01-01T00:00:00Z',
                       annotations={}, spans={}, fiberId='1')
        paths = ['userdata/logs/renderer-history.ndjson', 'userdata/logs/server-child.log']
        for path, row in zip(paths, (renderer, service)):
            self.file(path, json.dumps(row).encode())
        self.assertCountEqual(self.selected(self.inspect('t3code', ['logs'])), paths)
        for key, bad in [('event', []), ('mainPid', 10**400), ('version', 99), ('content', 'session')]:
            self.file(paths[0], json.dumps({**renderer, key: bad}).encode())
            self.assertEqual(self.selected(self.inspect('t3code', ['logs'])), [paths[1]])
        self.file(paths[1], json.dumps({**service, 'level': {}}).encode())
        self.assertEqual(self.selected(self.inspect('t3code', ['logs'])), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows exact exclusions')
    def test_t3_apply_rechecks_writers_after_leases_before_deletion(self):
        from local_agent_manager.t3code_store import T3CodeError
        self.simple()
        plan = maintenance.plan('t3code', self.root, categories=['caches'])
        with patch('local_agent_manager.t3code_diagnostics.require_closed',
                   side_effect=[None, T3CodeError('t3code_writer_running')]) as check:
            result = maintenance.apply(plan['plan_path'], approved_hash=plan['plan_sha256'], clients_closed=True)
        self.assertEqual(check.call_count, 2)
        self.assertFalse(result['mutation_started'])
        self.assertEqual(result['goal_status'], 'blocked')
        self.assertTrue((self.root / 'Code Cache/js/0123456789abcdef_0').exists())

    def test_atomic_temps_need_valid_current_fallback_and_keep_stable_slots(self):
        self.file('.codex-global-state.json', b'{}')
        self.file('.codex-global-state.json.bak', b'{}')
        path = '..codex-global-state.json.tmp-1577836800000-12345678-1234-1234-1234-123456789012'
        self.file(path)
        self.assertEqual(self.selected(self.inspect('codex', ['backups'])), [path])
        (self.root / '.codex-global-state.json.bak').write_bytes(b'bad')
        self.assertEqual(self.selected(self.inspect('codex', ['backups'])), [])

    def test_platform_releases_daemon_and_sandbox_helpers_are_visible_not_auto_deleted(self):
        names = ['packages/standalone/releases/0.999.0-x86_64-pc-windows-msvc/bin/codex.exe',
                 'packages/app-server-daemon/releases/0.999.0-aarch64-apple-darwin/bin/codex',
                 '.sandbox-bin/codex-command-runner-0.998.0-alpha.2.exe']
        for name in names:
            self.file(name, b'runtime')
        self.file('packages/standalone/releases/user_notes/readme', b'keep')
        r = runtimes.inventory('codex', self.root)
        self.assertEqual(len(r['candidates']), 3)
        self.assertIn('runtime_unit_name_unrecognized', {x['reason'] for x in r['unqualified']})
        self.assertEqual(self.selected(self.inspect('codex', ['runtimes'])), [])

    def test_cindy_account_discovery_tags_native_client_and_deduplicates_home(self):
        roaming = self.root / 'Library/Application Support' if sys.platform == 'darwin' else self.root
        profile = roaming / 'CindyGlobal'
        account = profile / 'codex-accounts' / ('a' * 64) / 'openai-deadbeef'
        account.mkdir(parents=True)
        (account / 'state_5.sqlite').write_bytes(b'presence only')
        (profile / 'codex-home').mkdir()
        with (patch.object(Path, 'home', return_value=self.root),
              patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root)}),
              patch.object(rules, 'default_appdata', return_value=roaming),
              patch.object(rules, 'default_codex_home', return_value=profile / 'codex-home')):
            profiles = rules.default_profiles(['cindy', 'codex'])
        native = [p['root'] for p in profiles if p['client'] == 'codex']
        self.assertIn(str(account), native)
        self.assertEqual(native.count(str(profile / 'codex-home')), 1)

    def test_paseo_exposes_but_preserves_recovery_records(self):
        for name in ('agents', 'schedules', 'agent-requests', 'creations'):
            self.file(name + '/record.json', b'{}')
        self.simple()
        r = self.inspect('paseo', ['backups', 'caches'])
        self.assertEqual(self.selected(r), ['Code Cache/js'])
        retained = [x for x in r['observations'] if x['reason'] == 'paseo_record_and_recovery_state_use_record_workflow']
        self.assertEqual(len(retained), 4)

    @unittest.skipUnless(os.name == 'nt', 'Windows held-file writer')
    def test_simple_cache_apply_preserves_other_browser_state(self):
        entry = self.simple('Partitions/web/Code Cache/js')
        cookie = self.file('Partitions/web/Network/Cookies', b'keep')
        p = maintenance.plan('t3code', self.root, categories=['caches'])
        result = maintenance.apply(p['plan_path'], approved_hash=p['plan_sha256'], clients_closed=True)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertFalse(entry.exists())
        self.assertEqual(cookie.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
