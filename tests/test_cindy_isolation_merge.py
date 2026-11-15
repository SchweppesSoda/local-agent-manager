from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from local_agent_manager.cindy_isolation_merge import merge_selected_rows, HISTORY_TRIGGERS
from local_agent_manager.cindy_isolation_plan import IsolationPlanError


class CindyIsolationMergeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.target = self.root/'source.db', self.root/'target.db'
        schema = ["create table threads(id text primary key,rollout_path text not null,title text)",
                  "create table thread_items(thread_id text,item_id text,item_json text,primary key(thread_id,item_id))",
                  "create table thread_history_projection_state(thread_id text primary key,next_rollout_byte_offset int,next_rollout_ordinal int)"]
        for db in (self.source,self.target):
            with closing(sqlite3.connect(db)) as c:
                c.execute('pragma journal_mode=wal')
                for sql in schema:c.execute(sql)
                c.commit()
        with closing(sqlite3.connect(self.source)) as c:
            c.executemany('insert into threads values(?,?,?)',[('selected','/source/selected.jsonl','synthetic selected'),('unselected','/source/other.jsonl','protected')])
            c.execute("insert into thread_items values('selected','item','{\"opaque\":\"synthetic\"}')")
            c.execute("insert into thread_history_projection_state values('selected',918273,42)")
            c.commit()
        with closing(sqlite3.connect(self.target)) as c:
            c.execute("insert into threads values('existing-t3','/target/t3.jsonl','keep')");c.commit()

    def connection(self):
        c=sqlite3.connect(self.target.as_uri()+'?mode=rw',uri=True)
        self.addCleanup(c.close)
        c.execute('attach database ? as incoming',(self.source.as_uri()+'?mode=ro',))
        c.execute('begin immediate')
        return c

    def merge(self,c,**kwargs):
        return merge_selected_rows(c,native_ids=['selected'],tables=kwargs.pop('tables',['threads','thread_items','thread_history_projection_state']),rollout_rewrites=kwargs.pop('rollout_rewrites',{'/source/selected.jsonl':'/target/selected.jsonl'}),**kwargs)

    def test_merge_preserves_existing_target_and_pagination_and_opaque_payload(self):
        c=self.connection();r=self.merge(c)
        self.assertEqual(r['rows_inserted'],{'threads':1,'thread_items':1,'thread_history_projection_state':1})
        self.assertEqual(c.execute('select next_rollout_byte_offset,next_rollout_ordinal from thread_history_projection_state').fetchone(),(918273,42))
        self.assertEqual(c.execute("select item_json from thread_items").fetchone()[0],'{"opaque":"synthetic"}')
        self.assertEqual(c.execute("select rollout_path from threads where id='selected'").fetchone()[0],'/target/selected.jsonl')
        c.commit()
        self.assertEqual(c.execute('select id from threads order by id').fetchall(),[('existing-t3',),('selected',)])
        self.assertEqual(c.execute('select count(*) from incoming.threads').fetchone()[0],2)

    def test_conflict_in_later_table_leaves_all_tables_unchanged(self):
        c=self.connection();c.execute("insert into thread_items values('selected','item','target increment')")
        with self.assertRaisesRegex(IsolationPlanError,'conflict'):self.merge(c)
        self.assertEqual(c.execute('select count(*) from main.threads').fetchone()[0],1)
        self.assertEqual(c.execute('select item_json from main.thread_items').fetchone()[0],'target increment')

    def test_missing_path_mapping_cannot_insert_any_rows(self):
        c=self.connection()
        with self.assertRaisesRegex(IsolationPlanError,'rewrite_missing'):self.merge(c,rollout_rewrites={})
        self.assertEqual(c.execute('select count(*) from main.threads').fetchone()[0],1)

    def test_unqualified_schema_or_trigger_cannot_affect_unselected_target(self):
        for sql in ('alter table thread_items add column changed text',
                    "create trigger unexpected after insert on thread_items begin delete from threads;end"):
            c=self.connection();c.execute(sql)
            with self.assertRaises(IsolationPlanError):self.merge(c)
            self.assertEqual(c.execute('select count(*) from main.threads').fetchone()[0],1)
            c.rollback();c.close()

    def test_interrupted_caller_transaction_rolls_back_without_switching(self):
        c=self.connection();self.merge(c);c.rollback()
        self.assertEqual(c.execute('select id from main.threads').fetchall(),[('existing-t3',)])
        self.assertEqual(c.execute('select count(*) from main.thread_items').fetchone()[0],0)

    def test_pinned_projection_cleanup_trigger_is_qualified_without_deleting_items(self):
        sql = 'create table thread_realtime_items(thread_id text,item_id text,primary key(thread_id,item_id))'
        for db in (self.source,self.target):
            with closing(sqlite3.connect(db)) as c:
                c.execute(sql)
                c.execute(next(iter(HISTORY_TRIGGERS.values())))
                c.execute("insert into thread_realtime_items values('existing-t3','keep')")
                c.commit()
        c=self.connection();self.merge(c);c.commit()
        self.assertEqual(c.execute('select item_id from main.thread_realtime_items').fetchall(),[('keep',)])

    def test_same_named_but_changed_projection_trigger_is_rejected(self):
        for db in (self.source,self.target):
            with closing(sqlite3.connect(db)) as c:
                c.execute('create table thread_realtime_items(thread_id text)')
                c.execute('CREATE TRIGGER thread_realtime_items_projection_cleanup AFTER DELETE ON thread_history_projection_state BEGIN DELETE FROM thread_realtime_items; END')
                c.commit()
        c=self.connection()
        with self.assertRaisesRegex(IsolationPlanError,'trigger'):self.merge(c)
        self.assertEqual(c.execute('select count(*) from main.threads').fetchone()[0],1)


if __name__=='__main__':unittest.main()
