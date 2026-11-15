from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch
from contextlib import closing

from local_agent_manager.cindy_isolation_execution import admitted_import
from local_agent_manager.cindy_isolation_resume import import_entries, resolve_import, execute_remaining
from local_agent_manager.cindy_isolation_windows import WindowsImportBoundary, process_evidence
from tests.cindy_isolation_fixture import make_fixture


@unittest.skipUnless(os.name == "nt", "real Windows task boundary")
class WindowsCindyImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.spec, self.draft = make_fixture(self.root)
        patcher = patch("local_agent_manager.cindy_isolation_windows.no_cindy")
        patcher.start(); self.addCleanup(patcher.stop)

    def boundary(self, spec=None): return WindowsImportBoundary(spec or self.spec, self.draft)

    def recover(self, spec=None, directory=None):
        spec = spec or self.spec
        audit = resolve_import(spec, self.draft, boundary_factory=lambda: self.boundary(spec),
            evidence_directory=directory or self.root, prove_exit=lambda _: {'proof': 'injected_exit_host_fact'})
        return execute_remaining(spec, self.draft, audit, boundary_type=WindowsImportBoundary,
            evidence_directory=directory or self.root, writer=process_evidence())

    def interrupt_at(self, event):
        from local_agent_manager import cindy_isolation_execution as execution
        original = execution.execute_stages
        def stages(*args, checkpoint, **kwargs):
            def interrupt(name, **options):
                checkpoint(name, **options)
                if name == event: raise KeyboardInterrupt('synthetic phase interruption')
            return original(*args, checkpoint=interrupt, **kwargs)
        return patch.object(execution, 'execute_stages', stages)

    def test_real_windows_boundary_imports_atomically_and_retains_unselected_t3(self):
        result = admitted_import(self.spec, ["selected"], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
        self.assertEqual(result["goal_status"], "completed_with_residuals")
        self.assertFalse(result["source_deleted"])
        source, target = (Path(self.spec[k]["path"]) for k in ("source", "target"))
        self.assertTrue((source / "sessions/rollout-selected.jsonl").exists())
        self.assertFalse(os.path.samefile(source / "auth.json", target / "auth.json"))
        for name in ("state_5.sqlite", "thread_history_1.sqlite"):
            with closing(sqlite3.connect(target / name)) as c:
                self.assertEqual(c.execute("select opaque from protected").fetchone()[0], b"protected synthetic")

    def test_source_transaction_and_read_leases_prevent_reentry_and_replacement(self):
        with self.boundary():
            with closing(sqlite3.connect(Path(self.spec["source"]["path"]) / "state_5.sqlite", timeout=0)) as c:
                with self.assertRaises(sqlite3.OperationalError): c.execute("begin immediate")
            with self.assertRaises(OSError):
                Path(self.draft["groups"]["current_shared_candidates"]["native_files"][0]["source"]["path"]).write_bytes(b"wrong")
            import subprocess
            with self.assertRaises(OSError):
                subprocess.run([str(self.root / "fixture-cmd.exe"), "/d", "/c", "exit", "0"], timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            database = Path(self.spec['source']['path']) / 'state_5.sqlite'
            replacement = database.with_suffix('.replacement'); replacement.write_bytes(b'synthetic')
            with self.assertRaises(OSError): os.replace(replacement, database)

    def test_orphan_published_hardlink_recovery_removes_only_owned_stage(self):
        with self.interrupt_at('file_published'), self.assertRaises(KeyboardInterrupt):
            admitted_import(self.spec, ['selected'], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
        target = Path(self.spec['native_files'][0]['target_path'])
        self.assertEqual(target.stat().st_nlink, 2)
        unrelated = target.parent / '.cindy-import-unowned.tmp'; unrelated.write_bytes(b'keep')
        _, result = self.recover()
        self.assertTrue(result['offline_import_verified']); self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(unrelated.read_bytes(), b'keep')

    def test_target_writer_contention_commits_neither_database_and_can_recover(self):
        with closing(sqlite3.connect(Path(self.spec['target']['path'])/'thread_history_1.sqlite')) as held:
            held.execute('begin immediate')
            with self.assertRaises(sqlite3.OperationalError):
                admitted_import(self.spec, ['selected'], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
            with closing(sqlite3.connect(Path(self.spec['target']['path'])/'state_5.sqlite')) as c:
                self.assertEqual(c.execute('select count(*) from threads').fetchone()[0], 0)
            held.rollback()
        _, result = self.recover()
        self.assertTrue(result['offline_import_verified'])

    def test_half_native_file_and_half_auth_stage_are_recoverable(self):
        import shutil
        original = shutil.copyfileobj
        for role in ('native', 'auth'):
            with self.subTest(role=role):
                if role == 'auth':
                    # Independent stores for the second scenario.
                    self.spec, self.draft = make_fixture(self.root / 'auth-scenario')
                def half(src, dst, *args, **kwargs):
                    if (role == 'native' and '.cindy-import-' in str(dst.name)) or (role == 'auth' and '.auth-isolation-' in str(dst.name)):
                        dst.write(src.read(7)); dst.flush(); os.fsync(dst.fileno()); raise KeyboardInterrupt()
                    return original(src, dst, *args, **kwargs)
                with patch.object(shutil, 'copyfileobj', half), self.assertRaises(KeyboardInterrupt):
                    admitted_import(self.spec, ['selected'], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
                _, result = self.recover()
                self.assertTrue(result['offline_import_verified'])
                self.assertFalse(list(Path(self.spec['target']['path']).rglob('.cindy-import-*.tmp')))
                self.assertFalse(list(Path(self.spec['target']['path']).glob('.auth-isolation-*.tmp')))

    def test_registry_switched_location_remaining_preserves_target_increments(self):
        with self.interrupt_at('registry_verified'), self.assertRaises(KeyboardInterrupt):
            admitted_import(self.spec, ['selected'], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
        target = Path(self.spec['target']['path'])
        with closing(sqlite3.connect(target / 'state_5.sqlite')) as c:
            c.execute("update threads set updated_at=8 where id='selected'")
            c.execute("insert into protected values('t3-new',?)", (b't3 increment',)); c.commit()
        path = Path(self.spec['native_files'][0]['target_path']); path.write_bytes(path.read_bytes() + b'{"type":"new_synthetic_turn"}\n')
        before = path.read_bytes()
        _, result = self.recover()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(result['offline_import_verified'])
        with closing(sqlite3.connect(target / 'state_5.sqlite')) as c:
            self.assertEqual(c.execute("select updated_at from threads where id='selected'").fetchone()[0], 8)
            self.assertEqual(c.execute("select opaque from protected where id='t3-new'").fetchone()[0], b't3 increment')

    def test_half_registry_stage_is_owned_recovered_and_never_replaces_before_verification(self):
        from local_agent_manager.cindy_isolation_execution import config_stage_path
        before = Path(self.spec['registry_path']).read_bytes()
        def half(boundary, path, value):
            stage = config_stage_path(boundary.spec, path)
            with stage.open('xb') as stream:
                stream.write(b'{"half"'); stream.flush(); os.fsync(stream.fileno())
            raise KeyboardInterrupt()
        with patch.object(WindowsImportBoundary, 'atomic_config', half), self.assertRaises(KeyboardInterrupt):
            admitted_import(self.spec, ['selected'], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
        self.assertEqual(Path(self.spec['registry_path']).read_bytes(), before)
        old_stage = config_stage_path(self.spec, self.spec['registry_path'])
        self.assertTrue(old_stage.exists())
        _, result = self.recover()
        self.assertTrue(result['offline_import_verified']); self.assertFalse(old_stage.exists())

    def test_second_interruption_uses_latest_remaining_journal(self):
        from local_agent_manager.cindy_isolation_resume import latest_import_spec
        bundle = self.root / 'bundle'; directory = bundle / 'runs' / ('run-' + 'a' * 24); directory.mkdir(parents=True)
        with self.interrupt_at('database_committed'), self.assertRaises(KeyboardInterrupt):
            admitted_import(self.spec, ['selected'], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
        with self.interrupt_at('registry_verified'), self.assertRaises(KeyboardInterrupt): self.recover(directory=directory)
        active = latest_import_spec(bundle, self.spec)
        self.assertNotEqual(active['plan_sha256'], self.spec['plan_sha256'])
        next_spec, result = self.recover(active, directory)
        self.assertNotEqual(next_spec['plan_sha256'], active['plan_sha256'])
        self.assertTrue(result['offline_import_verified'])

    def test_interrupted_first_db_has_qualified_new_remaining_operation(self):
        original = WindowsImportBoundary.snapshot_auth
        with patch.object(WindowsImportBoundary, "snapshot_auth", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                admitted_import(self.spec, ["selected"], import_entries(self.spec, writer=process_evidence()), boundary_factory=self.boundary)
        # Exit proof injection is a host fact fixture, never exposed by CLI.
        audit = resolve_import(self.spec, self.draft, boundary_factory=self.boundary,
                               evidence_directory=self.root, prove_exit=lambda writer: {"proof": "fixture_exited"})
        self.assertNotIn("database:state_5.sqlite", audit["remaining"]["writes"])
        next_spec, result = execute_remaining(self.spec, self.draft, audit, boundary_type=WindowsImportBoundary,
                                              evidence_directory=self.root, writer=process_evidence())
        self.assertNotEqual(next_spec["plan_sha256"], self.spec["plan_sha256"])
        self.assertFalse(result["source_deleted"])
        self.assertGreater(len(result["cumulative_mutations"]), len(audit["cumulative_mutations"]))
        self.assertEqual(result["goal_status"], "completed_with_residuals")

    def test_real_dead_writer_lock_is_retired_only_after_qualified_recovery(self):
        spec_path, draft_path = self.root/'spec.json', self.root/'draft.json'
        spec_path.write_text(json.dumps(self.spec));draft_path.write_text(json.dumps(self.draft))
        script = '''
from tests import bootstrap
import json,os,sys
from pathlib import Path
from local_agent_manager.cindy_isolation_execution import admitted_import
from local_agent_manager.cindy_isolation_resume import import_entries
from local_agent_manager.cindy_isolation_windows import WindowsImportBoundary,process_evidence
from local_agent_manager.operation_store import OperationStore
import local_agent_manager.cindy_isolation_windows as windows
windows.no_cindy=lambda:None
s=json.loads(Path(sys.argv[1]).read_text());d=json.loads(Path(sys.argv[2]).read_text())
original=OperationStore.append_event
def checkpoint(store,event,**kw):
 if event['event']==sys.argv[3]:os._exit(17)
 return original(store,event,**kw)
OperationStore.append_event=checkpoint
admitted_import(s,['selected'],import_entries(s,writer=process_evidence()),boundary_factory=lambda:WindowsImportBoundary(s,d))
'''
        # Real kernel exit proof, real orphaned apply locks and WAL commits;
        # this test never patches the writer-exit or lock-disposition primitive.
        child=subprocess.run([sys.executable,'-c',script,str(spec_path),str(draft_path),'database_committed'],
                             capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(child.returncode,17,child.stderr.decode(errors='replace'))
        from local_agent_manager.cindy_isolation_resume import existing_entries
        self.assertTrue(all(store.lock_exists() for store,_ in existing_entries(self.spec)))
        audit=resolve_import(self.spec,self.draft,boundary_factory=self.boundary,evidence_directory=self.root)
        self.assertTrue(audit['writer_exit'])
        self.assertTrue(all(not store.lock_exists() for store,_ in existing_entries(self.spec)))
        self.assertEqual(audit['observation']['databases']['state_5.sqlite']['target']['state'],'after')
        self.assertEqual(audit['observation']['databases']['thread_history_1.sqlite']['target']['state'],'before')
        _,result=execute_remaining(self.spec,self.draft,audit,boundary_type=WindowsImportBoundary,
                                    evidence_directory=self.root,writer=process_evidence())
        self.assertEqual(result['goal_status'],'completed_with_residuals')
        self.assertTrue((Path(self.spec['source']['path'])/'sessions/rollout-selected.jsonl').exists())


if __name__ == "__main__": unittest.main()
