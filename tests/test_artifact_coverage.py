"""Coverage regressions use disposable stores, never a user's live profile."""
import json
import os
from pathlib import Path
import struct
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

    def test_rotated_t3_spans_qualify_current_and_transcripts_do_not(self):
        obj = {'type': 'effect-span', 'kind': 'internal', 'name': 'test', 'traceId': 'a' * 32,
               'spanId': 'b' * 16, 'startTimeUnixNano': '100', 'endTimeUnixNano': '200',
               'sampled': True, 'attributes': {}, 'events': [], 'links': [], 'exit': {}}
        body = json.dumps(obj).encode() + b'\n'
        self.file('userdata/logs/server.trace.ndjson.1', body)
        self.file('userdata/logs/server.trace.ndjson', body)
        self.file('userdata/logs/server.trace.ndjson.2', b'{"type":"message"}\n')
        self.file('userdata/logs/terminals/session.log', body)
        self.assertEqual(self.selected(self.inspect('t3code', ['logs'])), ['userdata/logs/server.trace.ndjson.1'])

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
        profile = self.root / 'CindyGlobal'
        account = profile / 'codex-accounts' / ('a' * 64) / 'openai-deadbeef'
        account.mkdir(parents=True)
        (account / 'state_5.sqlite').write_bytes(b'presence only')
        (profile / 'codex-home').mkdir()
        with patch.object(rules, 'default_appdata', return_value=self.root), patch.object(rules, 'default_codex_home', return_value=profile / 'codex-home'):
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
