
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import shutil
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import artifact_maintenance as maintenance
from local_agent_manager import frozen_files as ff
from local_agent_manager import runtime_artifacts as runtimes
from local_agent_manager.operation_store import strict_json_load
from local_agent_manager.windows_held_files import HeldFiles


class RuntimeArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'cindy'
        self.root.mkdir()
        for version in ('2.1.10', '2.1.20'):
            unit = self.root / 'claude-code' / version
            unit.mkdir(parents=True)
            (unit / 'claude.exe').write_bytes(('runtime ' + version).encode())
            (unit / 'support.dat').write_bytes(b'runtime dependency')
        self.frontend = Path(self.temporary.name) / 't3'
        self.frontend.mkdir()
        self.config = self.frontend / 'settings.json'
        self.config.write_text(json.dumps({'binaryPath': str(self.root / 'claude-code/2.1.20/claude.exe')}))
        self.writer = self.frontend / 'lam-test-settings-writer.exe'
        header = bytearray(64)
        header[:2] = b'MZ'
        struct.pack_into('<I', header, 60, 64)
        self.writer.write_bytes(header + b'PE\x00\x00')
        self.processes = patch.object(runtimes, '_windows_process_snapshot', return_value=())
        self.processes.start()
        self.addCleanup(self.processes.stop)

    def review(self):
        candidates = runtimes.inventory('cindy', self.root)['candidates']
        decisions = [{'path': item['path'], 'snapshot_sha256': item['snapshot_sha256'],
                      'reason': 'Inspected version and consumer selection'} for item in candidates]
        return {'schema_version': 'lam.runtime-review.v1', 'client': 'cindy', 'root': str(self.root),
                'assessment': {'ownership': 'Cindy versioned Claude installation',
                               'selection': 'Cindy and T3 select 2.1.20',
                               'consumers': 'Reviewed the host PATH and all known frontend launch settings',
                               'rollback': 'Retain 2.1.20; old runtime is reinstallable'},
                'remove': decisions[:1], 'preserve': decisions[1:],
                'references': [{'root': str(self.frontend), 'path': 'settings.json',
                                'reason': 'T3 selects the retained Cindy runtime'},
                               {'root': str(self.frontend), 'path': 'override.json',
                                'reason': 'No overriding selection exists'},
                               {'root': str(self.frontend), 'path': self.writer.name, 'writer': True,
                                'reason': 'The inspected native executable owns settings and override writes'}]}

    def plan(self, review=None):
        return maintenance.plan('cindy', self.root, categories=['runtimes'],
                                runtime_review=self.review() if review is None else review)

    def apply(self, plan):
        return maintenance.apply(plan['plan_path'], approved_hash=plan['plan_sha256'], clients_closed=True)

    def test_inventory_is_metadata_only_and_never_implies_selection(self):
        with patch.object(ff, 'read_file', side_effect=AssertionError('no runtime content reads')):
            report = runtimes.inventory('cindy', self.root)
        self.assertEqual(len(report['candidates']), 2)
        self.assertEqual(report['selection_and_consumers'], 'requires_reviewer_assessment')
        self.assertEqual(report['unqualified'], [])
        self.assertFalse((self.root / '.local-agent-record-janitor').exists())

    def test_without_review_runtimes_are_retained(self):
        result = maintenance.plan('cindy', self.root, categories=['runtimes'])
        self.assertEqual(result['counts']['approved_file_count'], 0)

    def helper_plan(self):
        sandbox = self.root / '.sandbox-bin'
        sandbox.mkdir()
        for version in ('0.998.0', '0.999.0'):
            (sandbox / ('codex-command-runner-' + version + '.exe')).write_bytes(version.encode())
        candidates = runtimes.inventory('codex', self.root)['candidates']
        review = self.review()
        review['client'] = 'codex'
        review['assessment'] = {key: 'Synthetic versioned helper retirement fixture'
                                for key in ('ownership', 'selection', 'consumers', 'rollback')}
        decisions = [{'path': row['path'], 'snapshot_sha256': row['snapshot_sha256'],
                      'reason': 'Retire the old helper, preserve the current helper'} for row in candidates]
        review.update(remove=decisions[:1], preserve=decisions[1:])
        return maintenance.plan('codex', self.root, categories=['runtimes'], runtime_review=review)

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_single_file_runtime_retirement_and_repeat_verification_complete(self):
        plan = self.helper_plan()
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertEqual(result['counts']['residual_file_count'], 0)
        verified = maintenance.query(plan['plan_path'], verify=True)
        self.assertEqual(verified['goal_status'], 'complete', verified)
        self.assertTrue(verified['mutation_started'])
        self.assertTrue((self.root / '.sandbox-bin/codex-command-runner-0.999.0.exe').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_single_file_runtime_recovery_preserves_mutation_and_detects_new_members(self):
        plan = self.helper_plan()
        with patch.object(maintenance, '_remaining', side_effect=RuntimeError('interrupted after deletion')):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        added = self.root / '.sandbox-bin/new-unapproved.exe'
        added.write_bytes(b'new unreviewed member')
        unknown = maintenance.query(plan['plan_path'], verify=True)
        self.assertEqual(unknown['goal_status'], 'unknown', unknown)
        self.assertTrue(unknown['mutation_started'])
        added.unlink()
        verified = maintenance.query(plan['plan_path'], verify=True)
        self.assertEqual(verified['goal_status'], 'complete', verified)
        self.assertTrue(verified['modified'])

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_single_file_runtime_missing_before_apply_is_not_approved_as_complete(self):
        plan = self.helper_plan()
        (self.root / '.sandbox-bin/codex-command-runner-0.998.0.exe').unlink()
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])

    def test_unqualified_runtime_bytes_remain_visible_without_content_reads_or_approval(self):
        root = Path(self.temporary.name) / 'codex'
        sandbox = root / '.sandbox-bin'
        sandbox.mkdir(parents=True)
        (sandbox / 'unknown.exe').write_bytes(b'unversioned executable')
        unknown = root / 'packages/standalone/releases/unrecognized-build'
        unknown.mkdir(parents=True)
        (unknown / 'payload').write_bytes(b'unknown package contents')
        with patch.object(ff, 'read_file', side_effect=AssertionError('metadata only')):
            report = maintenance.rules.inspect_profile('codex', root, categories=['runtimes'])
        self.assertEqual(report['changes'], [])
        self.assertEqual(report['runtime_inventory']['candidates'], [])
        rows = {item['path']: item for item in report['observations']}
        for relative, size in (('.sandbox-bin/unknown.exe', 22),
                               ('packages/standalone/releases/unrecognized-build', 24)):
            self.assertEqual(rows[relative]['logical_bytes'], size)
            self.assertEqual(rows[relative]['file_count'], 1)
            self.assertIsNone(rows[relative]['allocated_bytes'])
            self.assertEqual(rows[relative]['decision'], 'unqualified')
            self.assertEqual(rows[relative]['reason'], 'runtime_unit_name_unrecognized')

    def test_unmeasurable_runtime_is_unknown_size_not_zero(self):
        root = Path(self.temporary.name) / 'codex'
        sandbox = root / '.sandbox-bin'
        sandbox.mkdir(parents=True)
        executable = sandbox / 'codex.exe'
        executable.write_bytes(b'shared executable')
        os.link(executable, root / 'shared.exe')
        report = runtimes.inventory('codex', root)
        row = report['unqualified'][0]
        self.assertIsNone(row['logical_bytes'])
        self.assertIsNone(row['file_count'])
        self.assertEqual(row['reason'], 'runtime_unit_unreadable_linked_or_unknown')
        self.assertEqual(row['measurement_reason'], 'unreadable_linked_or_budgeted')
        self.assertEqual(report['candidates'], [])

    def test_cli_accepts_review_file_and_freezes_assessment_and_external_evidence(self):
        review = self.review()
        review_path = Path(self.temporary.name) / 'review.json'
        review_path.write_text(json.dumps(review))
        output = io.StringIO()
        with redirect_stdout(output):
            code = maintenance.main(['plan', '--client', 'cindy', '--root', str(self.root),
                '--category', 'runtimes', '--runtime-review', str(review_path), '--json'])
        self.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        document = strict_json_load(Path(result['plan_path']))
        evidence = document['artifact_evidence']['runtime_evidence']
        self.assertEqual(evidence['review'], review)
        self.assertEqual(len(evidence['references']), 3)
        self.assertIsNone(evidence['references'][1]['file'])
        self.assertEqual(result['counts']['approved_file_count'], 2)

    def test_review_requires_specific_assessments_complete_decisions_and_replacement(self):
        original = self.review()
        invalid = []
        review = copy.deepcopy(original)
        review['assessment']['consumers'] = ''
        invalid.append(review)
        review = copy.deepcopy(original)
        review['preserve'] = []
        invalid.append(review)
        review = copy.deepcopy(original)
        review['remove'].extend(review['preserve'])
        review['preserve'] = []
        invalid.append(review)
        review = copy.deepcopy(original)
        review['references'] = []
        invalid.append(review)
        review = copy.deepcopy(original)
        review['remove'][0]['path'] = '../sessions'
        invalid.append(review)
        review = copy.deepcopy(original)
        review['client'] = 'codex'
        invalid.append(review)
        for review in invalid:
            with self.subTest(review=review), self.assertRaises(ValueError):
                self.plan(review)
        self.assertFalse((self.root / '.local-agent-record-janitor').exists())

    def test_absent_reference_requires_a_native_writer_exclusion_binding(self):
        review = self.review()
        review['references'] = review['references'][:2]
        with self.assertRaisesRegex(ValueError, 'absent_reference_requires_writer_exclusion'):
            self.plan(review)
        review['references'].append({'root': str(self.frontend), 'path': 'settings.json',
                                    'writer': True, 'reason': 'Metadata file provides no image exclusion'})
        with self.assertRaises(ValueError):
            self.plan(review)

    def test_review_snapshot_change_requires_renewed_assessment(self):
        review = self.review()
        (self.root / 'claude-code/2.1.10/new.dat').write_text('new unreviewed runtime data')
        with self.assertRaisesRegex(ValueError, 'candidate_changed'):
            self.plan(review)

    def test_runtime_hashing_uses_its_own_budget_for_compiled_distributions(self):
        with patch.object(ff, 'freeze_remove', side_effect=AssertionError('runtime uses its separate tree budget')):
            result = self.plan()
        self.assertEqual(result['counts']['approved_file_count'], 2)
        with patch.object(runtimes, 'MAX_RUNTIME_BYTES', 1):
            report = runtimes.inventory('cindy', self.root)
        self.assertEqual(report['candidates'], [])
        self.assertEqual(len(report['unqualified']), 2)

    def test_hardlinks_and_reparse_units_cannot_be_selected(self):
        os.link(self.root / 'claude-code/2.1.10/support.dat', self.root / 'shared.dat')
        report = runtimes.inventory('cindy', self.root)
        self.assertEqual([item['path'] for item in report['candidates']], ['claude-code/2.1.20'])
        self.assertEqual(report['unqualified'][0]['path'], 'claude-code/2.1.10')

    def test_recognized_install_layouts_need_no_fixed_product_build(self):
        cases = [('cindy', 'codex-package/0.999.0/bin/codex.exe', 'codex-package/0.999.0'),
                 ('codex', 'packages/standalone/releases/0.999.0/bin/codex.exe', 'packages/standalone/releases/0.999.0'),
                 ('codex', 'packages/app-server/releases/0.999.0/codex.exe', 'packages/app-server/releases/0.999.0'),
                 ('claude', 'versions/9.8.7', 'versions/9.8.7'),
                 ('claude', 'share/claude/versions/9.8.7', 'share/claude/versions/9.8.7'),
                 ('claude', 'bin/claude.exe.old.1794789050948.42676', 'bin/claude.exe.old.1794789050948.42676'),
                 ('claude', 'bin/claude.exe', 'bin/claude.exe'),
                 ('codex', '.sandbox-bin/codex.exe', '.sandbox-bin/codex.exe'),
                 ('codex', '.sandbox-bin/codex-command-runner.exe', '.sandbox-bin/codex-command-runner.exe'),
                 ('codex-desktop', 'bin/' + 'a' * 16 + '/codex.exe', 'bin/' + 'a' * 16),
                 ('t3code', 'tools/antigravity/win32-x64/versions/' + 'b' * 64 + '/agy_acp_server.exe',
                  'tools/antigravity/win32-x64/versions/' + 'b' * 64)]
        for index, (client, filename, unit) in enumerate(cases):
            with self.subTest(client=client, filename=filename):
                root = Path(self.temporary.name) / ('layout-' + str(index))
                path = root / filename
                path.parent.mkdir(parents=True)
                path.write_bytes(b'synthetic runtime')
                report = runtimes.inventory(client, root)
                self.assertEqual(report['candidates'][0]['path'], unit, report)
                self.assertEqual(report['unqualified'], [])

    def test_claude_launcher_discovery_excludes_other_programs_and_unknown_backup_names(self):
        directory = self.root / 'bin'
        directory.mkdir()
        for name in ('uv.exe', '1.2.3', 'claude.exe.old.notes', 'claude.exe.old.123.42'):
            (directory / name).write_bytes(b'not an approved Claude updater layout')
        self.assertEqual(runtimes.inventory('claude', self.root)['candidates'], [])

    def daemon_review(self):
        role = self.root / runtimes.DAEMON
        old = role / 'releases/0.998.0/bin/codex.exe'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'old standalone daemon')
        replacement = self.root / 'packages/standalone/releases/0.999.0/bin/codex.exe'
        replacement.parent.mkdir(parents=True)
        replacement.write_bytes(b'current standalone')
        for name in ('auto-update-version', 'selection-history', 'install.lock'):
            (role / name).write_text('0.998.0' if name != 'install.lock' else '')
        pointer = role / 'current'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(pointer), str(old.parent.parent)],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        review = self.review()
        review['client'] = 'codex'
        rows = runtimes.inventory('codex', self.root)['candidates']
        for key, condition in (('remove', True), ('preserve', False)):
            review[key] = [{'path': row['path'], 'snapshot_sha256': row['snapshot_sha256'],
                            'reason': 'Retire stopped daemon; separate frontend installation remains outside scope'}
                           for row in rows if row['path'].startswith(runtimes.DAEMON + '/') == condition]
        for name in ('current', 'auto-update-version', 'selection-history', 'install.lock'):
            review['references'].append({'root': str(self.root), 'path': runtimes.DAEMON + '/' + name,
                'reason': 'Retire daemon selectors; retain and exclude the installer lock', 'retire': name != 'install.lock'})
        return review

    @unittest.skipUnless(os.name == 'nt', 'Windows junction retirement')
    def test_daemon_retirement_removes_selectors_and_preserves_separate_same_version(self):
        review = self.daemon_review()
        separate = self.frontend / '0.998.0/codex.exe'
        separate.parent.mkdir()
        separate.write_bytes(b'old standalone daemon')
        plan = maintenance.plan('codex', self.root, categories=['runtimes'], runtime_review=review)
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'complete', result)
        role = self.root / runtimes.DAEMON
        self.assertFalse((role / 'current').exists())
        self.assertFalse((role / 'auto-update-version').exists())
        self.assertFalse((role / 'selection-history').exists())
        self.assertTrue((role / 'install.lock').exists())
        self.assertEqual(separate.read_bytes(), b'old standalone daemon')
        self.assertEqual(maintenance.query(plan['plan_path'], verify=True)['goal_status'], 'complete')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction retirement')
    def test_daemon_retirement_requires_complete_exact_selectors_and_release_scope(self):
        original = self.daemon_review()
        for case in ('no-retirement', 'missing-metadata', 'lock-retirement', 'extra-unit', 'unknown-member'):
            with self.subTest(case=case):
                review = copy.deepcopy(original)
                extra = None
                if case == 'no-retirement':
                    for ref in review['references']: ref.pop('retire', None)
                elif case == 'missing-metadata':
                    review['references'] = [ref for ref in review['references'] if not ref['path'].endswith('selection-history')]
                elif case == 'lock-retirement':
                    review['references'][-1]['retire'] = True
                else:
                    extra = self.root / runtimes.DAEMON / ('releases/unknown-unit' if case == 'extra-unit' else 'unknown-selection')
                    extra.write_text('unknown')
                try:
                    with self.assertRaises(ValueError):
                        maintenance.plan('codex', self.root, categories=['runtimes'], runtime_review=review)
                finally:
                    if extra: extra.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction retirement')
    def test_daemon_retirement_pointer_drift_blocks_before_mutation(self):
        review = self.daemon_review()
        plan = maintenance.plan('codex', self.root, categories=['runtimes'], runtime_review=review)
        pointer = self.root / runtimes.DAEMON / 'current'
        pointer.rmdir()
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])
        self.assertTrue((self.root / runtimes.DAEMON / 'releases/0.998.0/bin/codex.exe').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction retirement')
    def test_daemon_interruption_reports_remaining_pointer_and_never_replays(self):
        review = self.daemon_review()
        plan = maintenance.plan('codex', self.root, categories=['runtimes'], runtime_review=review)
        with patch.object(runtimes, 'dispose_pointers', side_effect=RuntimeError('interrupted')):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertTrue(result['modified'])
        with patch.object(HeldFiles, 'dispose', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.apply(plan)['goal_status'], 'unknown')
            result = maintenance.query(plan['plan_path'], verify=True)
        self.assertEqual(result['goal_status'], 'completed_with_residuals', result)
        self.assertEqual(result['residual_paths'], [runtimes.DAEMON + '/current'])
        (self.root / runtimes.DAEMON / 'current').rmdir()
        result = maintenance.query(plan['plan_path'], verify=True)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertTrue(result['mutation_started'])

    def junction(self, name, target):
        if os.name != 'nt':
            self.skipTest('Windows junction selector')
        path = self.frontend / name
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(path), str(target)],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: path.rmdir() if path.exists() else None)
        return path

    @unittest.skipUnless(os.name == 'nt', 'Windows junction selector')
    def test_current_junction_is_frozen_and_cannot_point_at_removed_version(self):
        current = self.junction('current', self.root / 'claude-code/2.1.20')
        review = self.review()
        review['references'].append({'root': str(self.frontend), 'path': 'current',
                                     'reason': 'Installer current points at retained version'})
        plan = self.plan(review)
        self.assertEqual(self.apply(plan)['goal_status'], 'complete')
        self.assertTrue((current / 'claude.exe').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction selector')
    def test_selected_junction_target_cannot_be_retired(self):
        self.junction('current', self.root / 'claude-code/2.1.10')
        review = self.review()
        review['references'].append({'root': str(self.frontend), 'path': 'current',
                                     'reason': 'Conflicting current selector must block'})
        with self.assertRaisesRegex(ValueError, 'selected_pointer_target'):
            self.plan(review)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction selector')
    def test_installer_alias_chain_requires_and_leases_every_pointer(self):
        retained = self.root / 'claude-code/2.1.20'
        (retained / 'bin').mkdir()
        (retained / 'bin/runner.exe').write_bytes(b'retained runner')
        current = self.junction('current', retained)
        alias = self.junction('bin', current / 'bin')
        review = self.review()
        review['references'].append({'root': str(self.frontend), 'path': 'bin',
                                     'reason': 'Launcher alias to the installer current bin'})
        with self.assertRaisesRegex(ValueError, 'chain_unbound_or_cyclic'):
            self.plan(review)
        review['references'].append({'root': str(self.frontend), 'path': 'current',
                                     'reason': 'Explicit intermediate installer selector'})
        plan = self.plan(review)
        dispose = HeldFiles.dispose
        def check_chain_leases(held, *args, **kwargs):
            for pointer in (current, alias):
                with self.assertRaises(OSError):
                    pointer.rmdir()
            dispose(held, *args, **kwargs)
        with patch.object(HeldFiles, 'dispose', check_chain_leases):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertTrue((alias / 'runner.exe').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction selector')
    def test_chained_selector_cannot_target_removed_version(self):
        current = self.junction('current', self.root / 'claude-code/2.1.10')
        self.junction('alias', current)
        review = self.review()
        for name in ('alias', 'current'):
            review['references'].append({'root': str(self.frontend), 'path': name,
                                         'reason': 'Explicit selector chain'})
        with self.assertRaisesRegex(ValueError, 'selected_pointer_target'):
            self.plan(review)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction selector')
    def test_intermediate_selector_drift_blocks_before_any_deletion(self):
        current = self.junction('current', self.root / 'claude-code/2.1.20')
        self.junction('alias', current)
        review = self.review()
        for name in ('alias', 'current'):
            review['references'].append({'root': str(self.frontend), 'path': name,
                                         'reason': 'Explicit selector chain'})
        plan = self.plan(review)
        current.rmdir()
        self.junction('current', self.root / 'claude-code/2.1.10')
        result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])
        self.assertTrue((self.root / 'claude-code/2.1.10/claude.exe').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction selector')
    def test_selector_cycle_is_rejected(self):
        first = self.junction('first', self.root / 'claude-code/2.1.20')
        second = self.junction('second', first)
        first.rmdir()
        self.junction('first', second)
        self.addCleanup(first.rmdir)
        review = self.review()
        for name in ('first', 'second'):
            review['references'].append({'root': str(self.frontend), 'path': name,
                                         'reason': 'Cyclic selector must remain blocked'})
        with self.assertRaisesRegex(ValueError, 'chain_unbound_or_cyclic'):
            self.plan(review)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction selector')
    def test_pointer_namespace_is_excluded_until_deletion_finishes(self):
        current = self.junction('current', self.root / 'claude-code/2.1.20')
        review = self.review()
        review['references'].append({'root': str(self.frontend), 'path': 'current',
                                     'reason': 'Installer current selector'})
        plan = self.plan(review)
        dispose = HeldFiles.dispose
        def check_lease(held, *args, **kwargs):
            with self.assertRaises(OSError):
                current.rmdir()
            dispose(held, *args, **kwargs)
        with patch.object(HeldFiles, 'dispose', check_lease):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'complete', result)

    @unittest.skipUnless(os.name == 'nt', 'Windows directory symlink selector')
    def test_directory_symlink_uses_a_directory_handle(self):
        pointer = self.frontend / 'symlink-current'
        try:
            pointer.symlink_to(self.root / 'claude-code/2.1.20', target_is_directory=True)
        except OSError as exc:
            if exc.winerror == 1314:
                self.skipTest('Directory symlink privilege unavailable')
            raise
        self.addCleanup(pointer.unlink)
        review = self.review()
        review['references'].append({'root': str(self.frontend), 'path': pointer.name,
                                    'reason': 'Native installer selects this directory'})
        result = self.apply(self.plan(review))
        self.assertEqual(result['goal_status'], 'complete', result)

    def test_reference_inside_removed_unit_is_rejected(self):
        review = self.review()
        review['references'] = [{'root': str(self.root), 'path': 'claude-code/2.1.10/claude.exe',
                                 'reason': 'Cannot discard evidence that must be preserved'}]
        with self.assertRaisesRegex(ValueError, 'reference_overlaps_removal'):
            self.plan(review)

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_success_retains_shared_selected_runtime_and_external_settings(self):
        review_path = self.frontend / 'review.json'
        review_path.write_text(json.dumps(self.review()))
        output = io.StringIO()
        with redirect_stdout(output):
            code = maintenance.main(['run', '--client', 'cindy', '--root', str(self.root),
                '--category', 'runtimes', '--runtime-review', str(review_path), '--clients-closed', '--json'])
        self.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertEqual(list((self.root / 'claude-code/2.1.10').iterdir()), [])
        self.assertTrue((self.root / 'claude-code/2.1.20/claude.exe').is_file())
        self.assertTrue(self.config.is_file())

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_selection_change_new_reference_and_retained_version_change_block_before_mutation(self):
        for change in ('config', 'absent', 'preserved', 'new-version'):
            with self.subTest(change=change):
                plan = self.plan()
                original = self.config.read_bytes()
                if change == 'config':
                    self.config.write_text('{"binaryPath":"old runtime now selected"}')
                elif change == 'absent':
                    (self.frontend / 'override.json').write_text('{}')
                elif change == 'preserved':
                    (self.root / 'claude-code/2.1.20/new.dat').write_text('new retained dependency')
                else:
                    version = self.root / 'claude-code/2.1.30'
                    version.mkdir()
                    (version / 'claude.exe').write_bytes(b'newly installed executable')
                result = self.apply(plan)
                self.assertEqual(result['goal_status'], 'blocked', result)
                self.assertFalse(result['mutation_started'], result)
                self.assertTrue((self.root / 'claude-code/2.1.10/claude.exe').is_file())
                self.config.write_bytes(original)
                for path in (self.frontend / 'override.json', self.root / 'claude-code/2.1.20/new.dat'):
                    if path.exists():
                        path.unlink()
                if change == 'new-version':
                    (version / 'claude.exe').unlink()
                    version.rmdir()

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_external_consumer_settings_are_leased_before_disposal(self):
        plan = self.plan()
        with self.config.open('rb'):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_absent_reference_is_rechecked_after_all_leases(self):
        plan = self.plan()
        acquire = HeldFiles.acquire
        def introduce_override(held, *args, **kwargs):
            acquire(held, *args, **kwargs)
            (self.frontend / 'override.json').write_text('{}')
        with patch.object(HeldFiles, 'acquire', introduce_override):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])

    @unittest.skipUnless(os.name == 'nt', 'Windows writer exclusion')
    def test_reference_writer_process_blocks_and_image_lease_excludes_new_opens(self):
        plan = self.plan()
        with patch.object(runtimes, '_windows_process_snapshot', return_value=({'name': self.writer.name},)):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])
        dispose = HeldFiles.dispose
        def check_writer_lease(held, *args, **kwargs):
            with self.assertRaises(OSError):
                with self.writer.open('rb'):
                    pass
            dispose(held, *args, **kwargs)
        with patch.object(HeldFiles, 'dispose', check_writer_lease):
            result = self.apply(self.plan())
        self.assertEqual(result['goal_status'], 'complete', result)

    @unittest.skipUnless(os.name == 'nt', 'Windows native image lifecycle exclusion')
    def test_real_temporary_native_writer_is_blocked_while_running_and_cannot_restart_during_apply(self):
        self.processes.stop()
        # A private copy runs only console builtins and waits on our stdin.
        shutil.copyfile(os.environ['COMSPEC'], self.writer)
        plan = self.plan()
        process = subprocess.Popen([str(self.writer), '/d', '/q', '/c', 'set /p lam_wait='],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            result = self.apply(plan)
            self.assertEqual(result['goal_status'], 'blocked', result)
            self.assertFalse(result['mutation_started'])
            self.assertTrue((self.root / 'claude-code/2.1.10/claude.exe').exists())
        finally:
            try:
                process.communicate(input=b'finish\r\n', timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        dispose = HeldFiles.dispose
        def attempt_restart(held, *args, **kwargs):
            with self.assertRaises(OSError) as raised:
                subprocess.run([str(self.writer), '/d', '/c', 'exit 0'], check=False,
                    capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertIn(raised.exception.winerror, {5, 32, 33})
            dispose(held, *args, **kwargs)
        with patch.object(HeldFiles, 'dispose', attempt_restart):
            result = self.apply(self.plan())
        self.assertEqual(result['goal_status'], 'complete', result)

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_new_version_during_lease_acquisition_blocks_before_mutation(self):
        plan = self.plan()
        acquire = HeldFiles.acquire
        def install_version(held, *args, **kwargs):
            acquire(held, *args, **kwargs)
            version = self.root / 'claude-code/2.1.30'
            version.mkdir(exist_ok=True)
            (version / 'claude.exe').write_bytes(b'new version')
        with patch.object(HeldFiles, 'acquire', install_version):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'blocked', result)
        self.assertFalse(result['mutation_started'])

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_new_runtime_member_after_mutation_keeps_verification_unknown(self):
        plan = self.plan()
        dispose = HeldFiles.dispose
        def recreate_member(held, *args, **kwargs):
            dispose(held, *args, **kwargs)
            (self.root / 'claude-code/2.1.10/new.dat').write_bytes(b'unapproved new member')
        with patch.object(HeldFiles, 'dispose', recreate_member):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertEqual(maintenance.query(plan['plan_path'], verify=True)['goal_status'], 'unknown')

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_new_version_after_mutation_keeps_verification_unknown(self):
        plan = self.plan()
        dispose = HeldFiles.dispose
        def install_version(held, *args, **kwargs):
            dispose(held, *args, **kwargs)
            version = self.root / 'claude-code/2.1.30'
            version.mkdir(exist_ok=True)
            (version / 'claude.exe').write_bytes(b'new version')
        with patch.object(HeldFiles, 'dispose', install_version):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertEqual(maintenance.query(plan['plan_path'], verify=True)['goal_status'], 'unknown')

    @unittest.skipUnless(os.name == 'nt', 'Windows file exclusion contract')
    def test_partial_removal_is_not_replayed_and_retained_drift_keeps_recovery_unknown(self):
        plan = self.plan()
        dispose = HeldFiles.dispose
        def interrupt(held, *args, **kwargs):
            dispose(held, *args, **kwargs)
            raise OSError('interruption after disposal')
        with patch.object(HeldFiles, 'dispose', interrupt):
            self.assertEqual(self.apply(plan)['goal_status'], 'unknown')
        with patch.object(HeldFiles, 'dispose', side_effect=AssertionError('must not replay')):
            self.assertEqual(self.apply(plan)['goal_status'], 'unknown')
        self.assertEqual(maintenance.query(plan['plan_path'], verify=True)['goal_status'], 'completed_with_residuals')
        (self.root / 'claude-code/2.1.20/support.dat').write_bytes(b'changed retained dependency')
        self.assertEqual(maintenance.query(plan['plan_path'], verify=True)['goal_status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
