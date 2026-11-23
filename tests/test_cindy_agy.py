"""Exercise the public operation protocol with independent temporary stores."""
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing, ExitStack
from unittest.mock import patch

from tests import bootstrap
from tests.test_antigravity_cli_cleanup import fixture, TARGET, KEEP, CHILD
from local_agent_manager import antigravity_cli_cleanup as native
from local_agent_manager.cindy_references import AGY_PROVIDER_ID
from local_agent_manager.cli import main

FRONT = '42345678-1234-4123-8123-123456789abc'


class CindyAgyTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.base = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.root, self.binary = fixture(self.base, child=True)
        roaming = self.base/'roaming'
        stack.enter_context(patch.dict(os.environ, {'APPDATA': str(roaming), 'T3CODE_HOME': str(self.base/'t3'),
                                                   'PASEO_HOME': str(self.base/'paseo')}))
        stack.enter_context(patch('pathlib.Path.home', return_value=self.base))
        stack.enter_context(patch('local_agent_manager.office_runtime.processes', return_value=[]))
        self.db = roaming/'CindyGlobal/cindy-one.db'
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                CREATE TABLE sessions(id TEXT PRIMARY KEY, agent_kind TEXT, provider_id TEXT,
                    sdk_session_id TEXT, status TEXT, source TEXT, agent_device_id TEXT, remote_host_id TEXT);
                CREATE TABLE messages(id TEXT PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,
                    created_at INTEGER,rewind_at INTEGER);
                CREATE TABLE messages_fts(message_id TEXT, session_id TEXT);
            """)
            for sid, handle in ((FRONT, TARGET), ('keep', KEEP)):
                db.execute("INSERT INTO sessions VALUES(?,'acp',?,?,'active','desktop',NULL,NULL)",
                           (sid, AGY_PROVIDER_ID, handle))
                db.execute("INSERT INTO messages VALUES(?,?,'user','test transcript',1,NULL)", ('m'+sid, sid))
                db.execute('INSERT INTO messages_fts VALUES(?,?)', ('m'+sid, sid))
            db.commit()
        self.path = self.base/'plan.json'

    def cli(self, *args):
        out = io.StringIO()
        main([*map(str, args), '--json'], stdout=out, stderr=io.StringIO())
        return json.loads(out.getvalue())

    def plan(self):
        return self.cli('delete','plan','--client','cindy','--record-id',FRONT,'--engine','antigravity',
                        '--antigravity-root',self.root,'--antigravity-bin',self.binary,'--out',self.path)

    def apply(self, plan, digest=None):
        return self.cli('delete','apply','--operation-id',plan['operation_id'],'--plan',self.path,
                        '--plan-sha256',digest or plan['plan_sha256'],'--clients-closed')

    def test_plan_apply_verify(self):
        from local_agent_manager.t3code_store import CONTRACT, SCHEMA_CONTRACTS, qualify
        from tests.t3code_support import create_store
        # The newer T3 schema is qualified as a read-only reference source.
        variant = {row[1]: row for row in (*SCHEMA_CONTRACTS[-1], *CONTRACT['reference_guard_variant']['objects'])}
        with closing(create_store(self.base/'t3', objects=variant.values())) as t3:
            with self.assertRaisesRegex(ValueError, 'schema_unverified'):
                qualify(t3)
        with closing(sqlite3.connect(self.db)) as source, closing(sqlite3.connect(self.db.with_name('cindy-two.db'))) as db:
            source.backup(db)
            db.execute("DELETE FROM sessions WHERE id=?", (FRONT,))
            db.commit()
        inventory = self.cli('records','--client','cindy','--engine','antigravity','--cindy-db',self.db)
        record = next(r for r in inventory['records'] if r['native_thread_id'] == TARGET)
        self.assertEqual(record['record_id'], FRONT)
        self.assertEqual(record['classification'], 'unverified')
        with closing(sqlite3.connect(self.root/native.SUMMARY)) as db:
            db.execute(native.PARENTS_SQL)
            db.execute('INSERT INTO conversation_parents VALUES(?,?)', (CHILD, TARGET))
            db.commit()
        (self.root/'persistent_terminals').mkdir()
        plan = self.plan()
        self.assertEqual(plan['goal_status'], 'ready', plan)
        self.assertEqual(set(plan['selected_ids']), {TARGET, CHILD})
        self.assertNotIn('test transcript', self.path.read_text())
        self.assertFalse(self.apply(plan,'0'*64)['mutation_started'])
        self.assertTrue((self.root/'conversations'/(TARGET+'.db')).exists())
        result = self.apply(plan)
        if os.name != 'nt':
            self.assertEqual(result['goal_status'], 'blocked', result)
            self.assertEqual(result['blockers'][0]['blocker_code'], 'antigravity_cli_file_lease_platform_unqualified')
            self.assertFalse(result['mutation_started'])
            self.assertTrue((self.root/'conversations'/(TARGET+'.db')).exists())
            with closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute('SELECT sdk_session_id FROM sessions WHERE id=?', (FRONT,)).fetchone(), (TARGET,))
            return
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertEqual(result['counts']['verified_native_sessions'], 2)
        self.assertEqual(result['counts']['verified_cindy_sessions'], 1)
        self.assertEqual(self.cli('operation','verify','--operation-id',plan['operation_id'],
                                  '--plan',self.path)['goal_status'], 'complete')
        with closing(sqlite3.connect(self.db)) as db:
            for table, column in [('sessions','id'), ('messages','session_id'), ('messages_fts','session_id')]:
                self.assertEqual(db.execute(f'SELECT {column} FROM {table}').fetchall(), [('keep',)])
        with closing(sqlite3.connect(self.root/native.SUMMARY)) as db:
            self.assertEqual(db.execute('SELECT conversation_id FROM conversation_summaries').fetchall(), [(KEEP,)])
            self.assertEqual(db.execute('SELECT * FROM conversation_parents').fetchall(), [])

    def test_other_provider_remote_or_mixed_history_cannot_authorize(self):
        for field, value in [('provider_id','another:agy'), ('agent_device_id','other'), ('remote_host_id','remote')]:
            with self.subTest(field=field), closing(sqlite3.connect(self.db)) as db:
                db.execute(f'UPDATE sessions SET {field}=? WHERE id=?', (value, FRONT))
                db.commit()
                self.assertEqual(self.plan()['goal_status'], 'blocked')
                db.execute(f'UPDATE sessions SET {field}=? WHERE id=?', (AGY_PROVIDER_ID if field=='provider_id' else None, FRONT))
                db.commit()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("INSERT INTO messages VALUES('switch',?,'agent_switch',?,2,NULL)",
                       (FRONT, json.dumps({'fromAgentKind':'pi','fromSdkSessionId':'older'})))
            db.commit()
        self.assertEqual(self.plan()['goal_status'], 'blocked')

    def test_new_native_layout_refuses_conflicting_parent_or_nonempty_terminals(self):
        with closing(sqlite3.connect(self.root/native.SUMMARY)) as db:
            db.execute(native.PARENTS_SQL)
            db.execute('INSERT INTO conversation_parents VALUES(?,?)', (CHILD, KEEP))
            db.commit()
        self.assertEqual(self.plan()['blockers'][0]['blocker_code'], 'antigravity_cli_summary_parent_unresolved')
        terminal = self.root/'persistent_terminals'
        terminal.mkdir()
        (terminal/'unknown').touch()
        self.assertEqual(self.plan()['blockers'][0]['blocker_code'], 'antigravity_cli_persistent_terminal_ownership_unqualified')

    def test_metadata_agent_mode_preserves_other_session_and_rejects_unknown_format(self):
        from local_agent_manager.antigravity_cli_restore import metadata_after
        data = {}
        for sid in (TARGET, KEEP):
            summary = dict(ID=sid, Title='', Preview='', NumSteps=2, Loaded=True, UpdatedAt='',
                           WorkspaceURIs=[], AppDataDir='antigravity-cli', ProjectID='', AgentName='',
                           GroupID='', Internal=False, Cloud=False, CloudProject={}, PermissionPreset=0, AgentMode=0)
            data[sid] = dict(summary=summary, is_internal=False, last_modified_time='')
        def transform():
            return json.loads(metadata_after(json.dumps({'conversations':data}).encode(), [TARGET], data))
        self.assertEqual(transform()['conversations'], {KEEP:data[KEEP]})
        data[TARGET]['summary']['AgentMode'] = True
        with self.assertRaisesRegex(ValueError, 'metadata_unqualified'):
            transform()

    @unittest.skipUnless(os.name == 'nt', 'Windows mandatory file leases')
    def test_run_and_verify_detects_reappearing_index_entry(self):
        result = self.cli('delete','run','--client','cindy','--record-id',FRONT,
                          '--antigravity-root',self.root,'--antigravity-bin',self.binary,'--out',self.path,
                          '--clients-closed','--yes')
        self.assertEqual(result['goal_status'], 'complete', result)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('INSERT INTO messages_fts VALUES(?,?)', ('m'+FRONT, FRONT))
            db.commit()
        verified = self.cli('operation','verify','--operation-id',result['operation_id'],'--plan',self.path)
        self.assertEqual(verified['goal_status'], 'unknown')

    def test_sibling_historical_child_reference_after_plan_blocks_both_stores(self):
        plan = self.plan()
        with closing(sqlite3.connect(self.db)) as source, closing(sqlite3.connect(self.db.with_name('cindy-two.db'))) as db:
            source.backup(db)
            db.execute('DELETE FROM sessions WHERE id=?', (FRONT,))
            db.execute("INSERT INTO messages VALUES('old','keep','agent_switch',?,2,NULL)",
                       (json.dumps({'fromAgentKind':'acp','fromSdkSessionId':CHILD}),))
            db.commit()
        self.assertFalse(self.apply(plan)['mutation_started'])
        self.assertEqual(self.plan()['blockers'][0]['blocker_code'], 'antigravity_cli_cindy_native_reference')
        self.assertTrue((self.root/'conversations'/(TARGET+'.db')).exists())

    def test_standalone_native_delete_is_blocked_by_cindy(self):
        result = self.cli('delete','plan','--client','antigravity','--record-id',TARGET,
                          '--antigravity-root',self.root,'--antigravity-bin',self.binary)
        self.assertEqual(result['blockers'][0]['blocker_code'], 'antigravity_cli_cindy_native_reference')
        self.assertFalse(result['mutation_started'])

    @unittest.skipUnless(os.name == 'nt', 'Windows mandatory file leases')
    def test_apply_repeated_selection_in_different_order(self):
        second = '52345678-1234-4123-8123-123456789abc'
        with closing(sqlite3.connect(self.db)) as db:
            for table, key in [('sessions','id'),('messages','session_id'),('messages_fts','session_id')]:
                db.execute(f'UPDATE {table} SET {key}=? WHERE {key}=?', (second,'keep'))
            db.commit()
        plan = self.cli('delete','plan','--client','cindy','--record-id',second,'--record-id',FRONT,
                        '--antigravity-root',self.root,'--antigravity-bin',self.binary,'--out',self.path)
        self.assertEqual(plan['goal_status'], 'ready', plan)
        result = self.cli('delete','apply','--client','cindy','--record-id',second,'--record-id',FRONT,
                          '--plan',self.path,'--operation-id',plan['operation_id'],
                          '--plan-sha256',plan['plan_sha256'],'--clients-closed')
        self.assertEqual(result['goal_status'], 'complete', result)
        self.assertEqual(result['counts']['verified_cindy_sessions'], 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows mandatory file leases')
    def test_native_failure_preserves_frontend_and_partial_completion_survives_retry(self):
        plan = self.plan()
        with patch.object(native, 'apply', side_effect=native.CleanupError('writer_running')):
            self.assertFalse(self.apply(plan)['mutation_started'])
        with patch('local_agent_manager.cindy_agy.remove', side_effect=OSError):
            result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        for result in (result, self.apply(plan), self.cli('operation','verify','--plan',self.path,
                                                          '--operation-id',plan['operation_id'])):
            self.assertEqual(result['counts']['verified_native_sessions'], 2)
            self.assertEqual(result['counts']['verified_cindy_sessions'], 0)
            self.assertTrue(result['mutation_started'])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT sdk_session_id FROM sessions WHERE id=?',(FRONT,)).fetchone()[0], TARGET)
