from contextlib import closing, redirect_stdout
import io
import base64
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from local_agent_manager import client_artifacts as rules, artifact_maintenance as maintenance
from local_agent_manager.operation_store import OperationStore
from local_agent_manager.windows_held_files import HeldFiles


class ArtifactMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='lam-artifact-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old = time.time_ns() - 60 * 86400 * 1000000000

    def cache(self):
        cache = self.root / 'Cache/Cache_Data'
        cache.mkdir(parents=True)
        (cache / 'index').write_bytes(struct.pack('<II', 0xC103CAC3, 0x30000))
        (cache / 'data_0').write_bytes(b'rebuildable cache')
        return cache

    def make_plan(self, categories=('caches',)):
        return maintenance.plan('cindy', self.root, categories=list(categories))

    def execute(self, result):
        return maintenance.apply(result['plan_path'], approved_hash=result['plan_sha256'], clients_closed=True)

    def test_common_cache_rules_are_available_to_desktop_clients_without_inventory_writes(self):
        self.cache()
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*'))
        for client in rules.DESKTOP:
            result = rules.inspect_profile(client, self.root, categories=['caches'])
            self.assertEqual(result['observations'][0]['decision'], 'eligible_for_plan', client)
            self.assertEqual(result['changes'], [])
        self.assertEqual(before, sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*')))

    def test_unknown_cache_member_and_hardlink_are_never_approved(self):
        cache = self.cache()
        (cache / 'notes.txt').write_text('user data')
        result = rules.inspect_profile('cindy', self.root, categories=['caches'], freeze=True)
        self.assertEqual(result['changes'], [])
        (cache / 'notes.txt').unlink()
        os.link(cache / 'data_0', self.root / 'shared')
        result = rules.inspect_profile('cindy', self.root, categories=['caches'], freeze=True)
        self.assertEqual(result['changes'], [])
        self.assertTrue((self.root / 'shared').exists())

    def test_codex_msix_discovers_nested_cache_and_preserves_browser_state(self):
        profile = self.root / 'Roaming/Codex/web/Codex/Default'
        cache = profile / 'Cache/Cache_Data'
        cache.mkdir(parents=True)
        (cache / 'index').write_bytes(struct.pack('<II', 0xC103CAC3, 0x30000))
        (cache / 'data_0').write_bytes(b'cache')
        (profile / 'Cookies').write_bytes(b'preserve')
        result = rules.inspect_profile('codex-desktop', self.root, categories=['caches'], freeze=True)
        self.assertEqual([c['evidence']['path'] for c in result['changes']],
                         ['Roaming/Codex/web/Codex/Default/Cache/Cache_Data'])
        self.assertTrue((profile / 'Cookies').exists())
        (cache / 'notes.txt').write_bytes(b'user file')
        self.assertEqual(rules.inspect_profile('codex-desktop', self.root,
                         categories=['caches'], freeze=True)['changes'], [])

    def test_codex_cache_access_denial_is_not_reported_as_unknown_format(self):
        self.cache()
        with patch.object(rules, '_cache', side_effect=PermissionError('locked')):
            result = rules.inspect_profile('codex-desktop', self.root, categories=['caches'], freeze=True)
        self.assertEqual(result['changes'], [])
        self.assertEqual(result['observations'][0]['reason'], 'file_in_use_or_access_denied')

    @unittest.skipUnless(os.name == 'nt', 'Windows package layout')
    def test_codex_package_discovery_uses_common_localcache_root(self):
        package = self.root / 'Packages/OpenAI.Codex_test/LocalCache'
        package.mkdir(parents=True)
        with patch.object(rules, 'default_local_appdata', return_value=self.root):
            profiles = rules.default_profiles(['codex-desktop'])
        self.assertIn(str(package), [p['root'] for p in profiles])
        self.assertNotIn(str(package / 'Roaming/Codex'), [p['root'] for p in profiles])

    def codex_log(self, day='2020/01/01', body=b'2020-01-01T04:38:39.050Z warning [git] diagnostic\n'):
        path = self.root / 'Local/Codex/Logs' / day / 'codex-desktop-5b705aa4-9643-4437-ac08-c4c294e5c2cc-40704-t0-i1-043833-0.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        os.utime(path, ns=(self.old, self.old))
        return path

    def test_codex_dated_diagnostics_require_date_header_name_and_retention(self):
        old = self.codex_log()
        recent = self.codex_log('2099/01/01', b'2099-01-01T00:00:00.000Z info [app] diagnostic\n')
        invalid = self.codex_log('2020/99/01')
        mismatch = self.codex_log('2020/01/02')
        transcript = old.parent / 'session.jsonl'
        transcript.write_bytes(b'preserve')
        arbitrary = old.parent / '2020-01-01.log'
        arbitrary.write_bytes(b'2020-01-01 00:00:00 INFO transcript\n')
        os.utime(arbitrary, ns=(self.old, self.old))
        result = rules.inspect_profile('codex-desktop', self.root, categories=['logs'],
                                       cutoff_ns=time.time_ns(), freeze=True)
        self.assertEqual([c['evidence']['path'] for c in result['changes']], [old.relative_to(self.root).as_posix()])
        self.assertTrue(all(p.exists() for p in (recent, invalid, mismatch, transcript, arbitrary)))

    def test_codex_dated_logs_preserve_recent_mtime_and_unrecognized_headers(self):
        old = self.codex_log()
        os.utime(old, None)
        bad = self.codex_log('2020/01/02', b'conversation contents\n')
        result = rules.inspect_profile('codex-desktop', self.root, categories=['logs'],
                                       cutoff_ns=time.time_ns() - 14 * 86400 * 10**9, freeze=True)
        self.assertEqual(result['changes'], [])
        self.assertTrue(bad.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_codex_nested_artifact_apply_verifies_only_qualified_files_removed(self):
        old = self.codex_log()
        kept = old.parent / 'session.jsonl'
        kept.write_bytes(b'preserve')
        plan = maintenance.plan('codex-desktop', self.root, categories=['logs'])
        result = self.execute(plan)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertFalse(old.exists())
        self.assertEqual(kept.read_bytes(), b'preserve')

    def test_antigravity_resume_cache_is_retained(self):
        cache = self.root / 'cache'
        cache.mkdir()
        (cache / 'conversation_metadata.json').write_text('{"session":"retained"}')
        result = rules.inspect_profile('antigravity-cli', self.root, categories=['caches'], freeze=True)
        self.assertEqual(result['changes'], [])
        self.assertTrue((cache / 'conversation_metadata.json').exists())

    def test_log_retention_preserves_current_files_and_terminal_transcripts(self):
        logs = self.root / 'logs'
        logs.mkdir()
        for name in ('agent-2020-01-01.ndjson', 'main.log', 'agent-2099-01-01.ndjson', 'agent-2020-99-99.ndjson'):
            path = logs / name
            path.write_text(json.dumps({'level': 'info', 'msg': 'diagnostic', 'scope': 'test', 'seq': 1,
                'sessionId': 'test', 'source': 'main', 'ts': 1577836800000, 'tz': 0}) + '\n')
            os.utime(path, ns=(self.old, self.old))
        (logs / 'terminals').mkdir()
        (logs / 'terminals/session.log').write_text('conversation terminal')
        result = rules.inspect_profile('cindy', self.root, categories=['logs'],
            cutoff_ns=time.time_ns(), freeze=True)
        self.assertEqual([item['evidence']['path'] for item in result['changes']], ['logs/agent-2020-01-01.ndjson'])

    def backups(self):
        paths = [self.root / 'cindy-test.db']
        paths.extend(self.root / ('cindy-test.db.bak.2020-01-0' + str(day) + 'T00-00-00-000Z') for day in range(1, 5))
        for path in paths:
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE records (id TEXT PRIMARY KEY)')
                db.execute('INSERT INTO records VALUES (?)', ('preserved',))
            os.utime(path, ns=(self.old, self.old))
        (self.root / 'cindy-test.db.migration-runtime.json').write_text(json.dumps({
            'version': 1, 'legacyBaselineVersion': -1,
            'migrations': [{'fileName': '0001.sql', 'scriptHash': None, 'seq': 1, 'sqlHash': 'b' * 64}]}))
        return paths

    def test_database_backups_require_healthy_replacement_and_keep_two_newest(self):
        paths = self.backups()
        result = rules.inspect_profile('cindy', self.root, categories=['backups'], cutoff_ns=time.time_ns(), freeze=True)
        selected = [item['evidence']['path'] for item in result['changes']]
        self.assertCountEqual(selected, [path.name for path in paths[1:3]])
        self.assertIn('cindy-test.db', {item['path'] for item in result['dependencies']})
        paths[0].write_bytes(b'corrupt current database')
        result = rules.inspect_profile('cindy', self.root, categories=['backups'], cutoff_ns=time.time_ns(), freeze=True)
        self.assertEqual(result['changes'], [])

    def test_corrupt_newer_generations_do_not_justify_deleting_healthy_backups(self):
        paths = self.backups()
        for path in paths[-2:]:
            path.write_bytes(b'corrupt newer backup')
        result = rules.inspect_profile('cindy', self.root, categories=['backups'], cutoff_ns=time.time_ns(), freeze=True)
        self.assertEqual(result['changes'], [])
        self.assertTrue(all(path.exists() for path in paths))

    def test_retention_requires_generation_time_as_well_as_mtime(self):
        paths = self.backups()
        for index, path in enumerate(paths[1:], 1):
            stamp = (datetime.now(timezone.utc) - timedelta(days=index)).strftime('%Y-%m-%dT00-00-00-000Z')
            path.rename(self.root / ('cindy-test.db.bak.' + stamp))
        result = rules.inspect_profile('cindy', self.root, categories=['backups'],
            cutoff_ns=time.time_ns() - 14 * 86400 * 1000000000, freeze=True)
        self.assertEqual(result['changes'], [])
        for name in ('.codex-global-state.json', '.codex-global-state.json.bak'):
            (self.root / name).write_text('{}')
        generation = (time.time_ns() - 2 * 86400 * 1000000000) // 1000000
        temporary = self.root / ('..codex-global-state.json.bak.tmp-' + str(generation) + '-' + str(uuid.uuid4()))
        temporary.write_text('{}')
        os.utime(temporary, ns=(self.old, self.old))
        result = rules.inspect_profile('codex', self.root, categories=['backups'],
            cutoff_ns=time.time_ns() - 14 * 86400 * 1000000000, freeze=True)
        self.assertEqual(result['changes'], [])

    def test_runtime_and_updater_artifacts_retain_their_selection_roles(self):
        for name in ('pending', 'versions', 'tools'):
            (self.root / name).mkdir()
            (self.root / name / 'current').write_text('active runtime')
        result = rules.inspect_profile('t3code', self.root, categories=['downloads', 'runtimes'], freeze=True)
        self.assertEqual(result['changes'], [])
        self.assertEqual({item['decision'] for item in result['observations']}, {'retain'})

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_updater_removes_only_old_unselected_temp_download_and_preserves_pending_and_base(self):
        pending = self.root / 'pending'
        pending.mkdir()
        selected = pending / 'Setup-1.2.3.exe'
        selected.write_bytes(b'selected installer')
        base = self.root / 'installer.exe'
        base.write_bytes(b'differential base')
        (pending / 'update-info.json').write_text(json.dumps({'fileName': selected.name,
            'sha512': base64.b64encode(b'x' * 64).decode(), 'installOnNextLaunch': True}))
        old = pending / ('temp-' + selected.name)
        old.write_bytes(b'incomplete download')
        os.utime(old, ns=(self.old, self.old))
        recent = pending / ('0-temp-' + selected.name)
        recent.write_bytes(b'recent download')
        result = self.execute(self.make_plan(('downloads',)))
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertFalse(old.exists())
        self.assertEqual(selected.read_bytes(), b'selected installer')
        self.assertEqual(base.read_bytes(), b'differential base')
        self.assertTrue(recent.exists())

    def test_orphan_codex_fallback_temp_requires_both_valid_current_slots(self):
        current = self.root / '.codex-global-state.json'
        fallback = self.root / '.codex-global-state.json.bak'
        temporary = self.root / ('..codex-global-state.json.bak.tmp-1787876805406-' + str(uuid.uuid4()))
        for path in (current, fallback, temporary):
            path.write_text('{}')
            os.utime(path, ns=(self.old, self.old))
        result = rules.inspect_profile('codex', self.root, categories=['backups'],
            cutoff_ns=time.time_ns(), freeze=True)
        self.assertEqual([item['evidence']['path'] for item in result['changes']], [temporary.name])
        self.assertEqual({proof['path'] for proof in result['dependencies']}, {current.name, fallback.name})
        fallback.write_text('corrupt fallback')
        result = rules.inspect_profile('codex', self.root, categories=['backups'],
            cutoff_ns=time.time_ns(), freeze=True)
        self.assertEqual(result['changes'], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_unresolved_sibling_journal_excludes_a_new_maintenance_mutation(self):
        from local_agent_manager.mutation_guard import UnknownMutationError
        cache = self.cache()
        first = self.make_plan()
        store = OperationStore(self.root, first['operation_id'])
        state = store.read_state()
        state.update(phase='executing', mutation_started=True, modified=True, current_action_state='started')
        store.append_event({'event': 'mutation_started'}, state_updates=state)
        second = self.make_plan()
        with self.assertRaises(UnknownMutationError):
            self.execute(second)
        self.assertTrue((cache / 'data_0').exists())
        self.assertEqual(maintenance.query(first['plan_path'], verify=True)['goal_status'], 'completed_with_residuals')
        self.assertEqual(self.execute(second)['goal_status'], 'complete')

    def test_cli_inventory_reports_missing_profiles_without_claiming_absent_installations(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = maintenance.main(['inventory', '--client', 'cindy', '--root', str(self.root / 'missing'), '--json'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['profiles'][0]['status'], 'not_observed_at_candidate')
        self.assertFalse((self.root / 'missing').exists())

    def test_plan_automatically_uses_a_unique_observed_profile_and_refuses_ambiguity(self):
        self.cache()
        profiles = [{'client': 'cindy', 'root': str(self.root)},
                    {'client': 'cindy', 'root': str(self.root / 'missing')}]
        with patch.object(rules, 'default_profiles', return_value=profiles):
            result = maintenance.plan('cindy', None, categories=['caches'])
        self.assertEqual(Path(result['scope']['root']), self.root)
        other = self.root / 'another-profile'
        other.mkdir()
        profiles.append({'client': 'cindy', 'root': str(other)})
        with patch.object(rules, 'default_profiles', return_value=profiles):
            with self.assertRaisesRegex(ValueError, 'multiple_or_unobserved_profiles'):
                maintenance.plan('cindy', None, categories=['caches'])
        self.assertEqual(list(other.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_run_removes_only_approved_cache_files_and_preserves_other_state(self):
        cache = self.cache()
        (self.root / 'sessions').mkdir()
        record = self.root / 'sessions/production.jsonl'
        record.write_text('preserve conversation')
        result = self.execute(self.make_plan())
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertEqual(list(cache.iterdir()), [])
        self.assertEqual(record.read_text(), 'preserve conversation')
        self.assertEqual(result['counts']['observed_reclaimed_bytes'], None)

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_plan_drift_is_blocked_before_any_deletion(self):
        cache = self.cache()
        plan = self.make_plan()
        (cache / 'f_000001').write_text('new cache file')
        result = self.execute(plan)
        self.assertEqual(result['goal_status'], 'blocked')
        self.assertFalse(result['mutation_started'])
        self.assertTrue((cache / 'index').exists())
        self.assertTrue((cache / 'f_000001').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_cli_rejects_wrong_hash_without_overstating_an_unknown_mutation(self):
        cache = self.cache()
        plan = self.make_plan()
        output = io.StringIO()
        with redirect_stdout(output):
            code = maintenance.main(['apply', '--plan', plan['plan_path'],
                '--plan-sha256', '0' * 64, '--clients-closed', '--json'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result['goal_status'], 'blocked')
        self.assertFalse(result['mutation_started'])
        self.assertTrue((cache / 'index').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_fallback_slot_replacement_with_equal_bytes_invalidates_approval(self):
        current = self.root / '.codex-global-state.json'
        fallback = self.root / '.codex-global-state.json.bak'
        temporary = self.root / ('..codex-global-state.json.bak.tmp-1787876805406-' + str(uuid.uuid4()))
        for path in (current, fallback, temporary):
            path.write_text('{}')
            os.utime(path, ns=(self.old, self.old))
        plan = maintenance.plan('codex', self.root, categories=['backups'])
        fallback.write_text('{}')
        os.utime(fallback, ns=(self.old + 1000000000, self.old + 1000000000))
        result = self.execute(plan)
        self.assertEqual(result['goal_status'], 'blocked')
        self.assertFalse(result['mutation_started'])
        self.assertTrue(temporary.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_open_writer_prevents_the_whole_batch_from_starting(self):
        cache = self.cache()
        plan = self.make_plan()
        with (cache / 'index').open('rb'):
            result = self.execute(plan)
        self.assertEqual(result['goal_status'], 'blocked')
        self.assertFalse(result['mutation_started'])
        self.assertTrue((cache / 'data_0').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_interruption_is_not_replayed_and_verify_resolves_exact_residuals(self):
        self.cache()
        plan = self.make_plan()
        dispose = HeldFiles.dispose
        def interrupt(held, *args, **kwargs):
            dispose(held, *args, **kwargs)
            raise OSError('simulated crash after first irreversible disposal')
        with patch.object(HeldFiles, 'dispose', interrupt):
            result = self.execute(plan)
        self.assertEqual(result['goal_status'], 'unknown')
        self.assertIsNone(result['counts']['residual_file_count'])
        with patch.object(HeldFiles, 'dispose', side_effect=AssertionError('must never replay')):
            self.assertEqual(self.execute(plan)['goal_status'], 'unknown')
            verified = maintenance.query(plan['plan_path'], verify=True)
            self.assertEqual(verified['goal_status'], 'completed_with_residuals')
            self.assertEqual(self.execute(plan)['goal_status'], 'completed_with_residuals')
        final = self.execute(self.make_plan())
        self.assertEqual(final['goal_status'], 'complete', final)

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_changed_current_database_blocks_backup_removal(self):
        paths = self.backups()
        plan = self.make_plan(('backups',))
        with closing(sqlite3.connect(paths[0])) as db, db:
            db.execute('INSERT INTO records VALUES (?)', ('new user record',))
        result = self.execute(plan)
        self.assertEqual(result['goal_status'], 'blocked')
        self.assertFalse(result['mutation_started'])
        self.assertTrue(all(path.exists() for path in paths))

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_new_sidecar_after_qualification_blocks_before_irreversible_disposal(self):
        paths = self.backups()
        plan = self.make_plan(('backups',))
        original = HeldFiles.acquire
        injected = []
        def acquire(held, root, proof, **kwargs):
            original(held, root, proof, **kwargs)
            if not injected:
                (self.root / 'cindy-test.db-wal').write_bytes(b'new unapproved WAL generation')
                injected.append(True)
        with patch.object(HeldFiles, 'acquire', acquire):
            result = self.execute(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])
        self.assertTrue(all(path.exists() for path in paths))

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_corrupt_recovery_journal_does_not_assert_no_prior_modification(self):
        self.cache()
        plan = self.make_plan()
        original = HeldFiles.dispose
        def interrupt(held, *args, **kwargs):
            original(held, *args, **kwargs)
            raise OSError('interrupted disposal')
        with patch.object(HeldFiles, 'dispose', interrupt):
            self.assertEqual(self.execute(plan)['goal_status'], 'unknown')
        store = OperationStore(self.root, plan['operation_id'])
        with store.events_path.open('ab') as stream:
            stream.write(b'not-json\n')
        output = io.StringIO()
        with redirect_stdout(output):
            code = maintenance.main(['verify', '--plan', plan['plan_path'], '--json'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result['goal_status'], 'unknown')
        self.assertIsNot(result['mutation_started'], False)
        self.assertIsNot(result['modified'], False)

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_receipt_recovery_uses_the_shared_operation_store(self):
        self.cache()
        plan = self.make_plan()
        self.assertEqual(self.execute(plan)['goal_status'], 'complete')
        store = OperationStore(self.root, plan['operation_id'])
        external = self.root / 'external-plan.json'
        external.write_bytes(Path(plan['plan_path']).read_bytes())
        store.compact_completed(store.read_result(maintain=False))
        result = maintenance.query(root=self.root, operation_id=plan['operation_id'], verify=True)
        self.assertEqual(result['goal_status'], 'complete')
        self.assertEqual(result['verification_basis'], 'retained_completion_receipt')
        self.assertEqual(maintenance.query(plan['plan_path'], verify=True)['goal_status'], 'complete')
        self.assertEqual(maintenance.query(external, verify=True)['goal_status'], 'complete')
        store.lock_path.touch()
        for locator in (None, external, plan['plan_path']):
            result = (maintenance.query(locator, verify=True) if locator else
                maintenance.query(root=self.root, operation_id=plan['operation_id'], verify=True))
            self.assertEqual(result['goal_status'], 'unknown', result)
            self.assertEqual(result['blockers'][0]['blocker_code'], 'maintenance_apply_lock_present')


if __name__ == '__main__':
    unittest.main()
