"""Cold public CLI rehearsal on synthetic stores, never host record stores.

Only the host process census is injected (the real desktop remains open).
Codex uses the CLI's supported transport seam by default; an opt-in absolute
LAM_REHEARSAL_CODEX_BINARY runs the installed app-server instead. No prompts,
credentials, model requests or external network are needed.
"""
from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.t3code_support import create_store, add_thread, insert, V2
from tests.orca_native_support import create_native_schema, add_native_record
from tests.test_antigravity_cli_cleanup import fixture, TARGET, CHILD, KEEP as AGY_KEEP
from tests.test_t3code_localstorage import local_storage
from local_agent_manager import antigravity_cli_cleanup as agy, t3code_cleanup as cleanup
from local_agent_manager import t3code_lifecycle as lifecycle, office_leveldb as level
from local_agent_manager.operation_store import OperationStore

ROOTS = ['ce0c93df-0b50-4604-8dc2-00c5ae842ac1', '952b4a77-3066-490c-bc97-0d073c53a41b']
CHILDREN = ['thread:delegated-task:command%3Amcp%3A0cd9d9a2-a87c-4766-9bf5-2b45fb226bdc'
            '%3Adelegate-task%3A' + str(i) * 36 for i in range(6)]
SELECTED = ROOTS + CHILDREN
KEEP = 'keeper'
CODEX = ['01a1beed-505e-7781-85ff-a128c4dbc87f', '01a1befd-3635-7871-abe1-54616221e4e3',
         '01a1bf09-5a69-7a31-ac53-265f0df8c0a1', '01a1bf0a-5a69-7a31-ac53-265f0df8c0a1']
CLAUDE = '99999999-9999-4999-8999-999999999999'
THIRD = '66666666-6666-4666-8666-666666666666'
SID = 'provider-session:provider-instance:codex:shared'


def worker(argv):
    from local_agent_manager.cli import main
    from local_agent_manager.codex_app_server import CodexAppServer
    # Fixture-only process census, not a patch to scanners, fingerprints,
    # journals, filesystem writers, verification or the CLI dispatcher.
    class NativeTransport:
        def __init__(self, *, codex_home, **kwargs): self.home = Path(codex_home)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def delete_thread(self, record_id):
            with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
                path = Path(db.execute('SELECT rollout_path FROM threads WHERE id=?', (record_id,)).fetchone()[0])
                db.execute('DELETE FROM threads WHERE id=?', (record_id,)); db.commit()
            path.unlink()
    transport = CodexAppServer if os.environ.get('LAM_REHEARSAL_CODEX_BINARY') else NativeTransport
    with ExitStack() as stack:
        stack.enter_context(patch('local_agent_manager.office_runtime.processes', return_value=[]))
        if os.environ.get('LAM_REHEARSAL_HANDOFF'):
            from local_agent_manager import t3code_handoff as handoff
            # Inject only host facts, consent and process launch. The child
            # executes the real worker/CLI/writers on isolated physical stores.
            stack.enter_context(patch.object(handoff, 'list_processes', return_value=[]))
            stack.enter_context(patch.object(handoff, '_running_related_process_records', return_value=[]))
            stack.enter_context(patch.object(handoff, '_bundled_install', return_value=Path(os.environ['T3CODE_HOME']).parent))
            stack.enter_context(patch.object(handoff, 'windows_confirmation', return_value=True))
            def launch(spec):
                log = (spec.parent / 'fixture-worker.log').open('wb')
                try:
                    child = subprocess.Popen([sys.executable, '-B', '-m', 'tests.test_t3code_cli_rehearsal', '--worker',
                        '--handoff-worker', str(spec)], cwd=Path(__file__).resolve().parents[1],
                        env=dict(os.environ), stdout=log, stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    stack.callback(lambda: child.wait(timeout=180))
                    return child.pid
                finally:
                    log.close()
            stack.enter_context(patch.object(handoff, '_spawn_detached_worker', side_effect=launch))
            if argv and argv[0] == '--handoff-worker':
                return handoff.run_handoff_worker(Path(argv[1]))
        if os.environ.get('LAM_REHEARSAL_FAULT') == 'lost-frontend-result':
            original = cleanup.execute
            def lose_result(*args, **kwargs):
                original(*args, **kwargs)
                error = RuntimeError('synthetic lost frontend response after mutation')
                error.unknown = True
                raise error
            with patch.object(cleanup, 'execute', lose_result):
                return main(argv, app_server_factory=transport, client_inspector=lambda *a, **k: ())
        return main(argv, app_server_factory=transport, client_inspector=lambda *a, **k: ())


@unittest.skipUnless(os.name == 'nt', 'Windows lifecycle and LevelDB')
class ColdCliRehearsal(unittest.TestCase):
    def setUp(self):
        level.runtime()  # Required, never silently skipped in this rehearsal.
        evidence = os.environ.get('LAM_REHEARSAL_EVIDENCE')
        self.temp = tempfile.TemporaryDirectory(prefix='lam-r-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.evidence = Path(evidence)/self._testMethodName if evidence else self.base/'evidence'
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.root, self.home = self.base/'t3', self.base/'codex'
        self.home.mkdir(); create_native_schema(self.home)
        self.agyroot, binary = fixture(self.base, child=True)
        shutil.copyfile(self.agyroot/'conversations'/(CHILD+'.db'), self.agyroot/'conversations'/(THIRD+'.db'))
        with closing(sqlite3.connect(self.agyroot/'conversations'/(THIRD+'.db'))) as db:
            db.execute('UPDATE trajectory_meta SET trajectory_id=?,cascade_id=?', (THIRD, THIRD)); db.commit()
        with closing(sqlite3.connect(self.agyroot/agy.SUMMARY)) as db:
            db.execute('INSERT INTO conversation_summaries (conversation_id,title,preview,last_modified_time,workspace_uris,last_user_input_time,parent_conversation_id,nesting_depth,raw_summary) SELECT ?,title,preview,last_modified_time,workspace_uris,last_user_input_time,parent_conversation_id,nesting_depth,raw_summary FROM conversation_summaries WHERE conversation_id=?', (THIRD, CHILD)); db.commit()
        shutil.copytree(self.agyroot/'brain'/CHILD, self.agyroot/'brain'/THIRD)
        self.frontend = self.base/'frontend.exe'; self.frontend.write_bytes(b'fixture, never executed')
        self.codexbin = Path(os.environ.get('LAM_REHEARSAL_CODEX_BINARY', str(self.frontend)))
        if os.environ.get('LAM_REHEARSAL_CODEX_BINARY'):
            copied = self.base / 'runtime' / 'codex.exe'
            copied.parent.mkdir()
            shutil.copy2(self.codexbin, copied)
            self.codexbin = copied
            # Initialize this synthetic home with the selected installed
            # runtime before planning, including its current schema migrations.
            from local_agent_manager.codex_app_server import CodexAppServer
            from tests.test_t3code_runtime_acceptance import isolated_env
            environment = isolated_env(self.base)
            environment.update(HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9')
            with CodexAppServer(codex_home=self.home, codex_binary=self.codexbin,
                                environment=environment, working_directory=self.base) as server:
                server.request('thread/list', {'limit': 1})
        self.claude = self.base/'claude'
        bindings = []
        self.native_paths = []
        for native in [*CODEX, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa']:
            self.native_paths.append(add_native_record(self.home, native))
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            db.execute('UPDATE threads SET title=?', ('Synthetic title\n  ' + 'display metadata ' * 100,)); db.commit()
        with closing(create_store(self.root)) as db:
            mapping = [(ROOTS[0], CODEX[0], 'codex'), (ROOTS[1], TARGET, 'antigravity'),
                       *[(CHILDREN[i], CODEX[i+1], 'codex') for i in range(3)],
                       (CHILDREN[3], CLAUDE, 'claudeAgent'), (CHILDREN[4], CHILD, 'antigravity'),
                       (CHILDREN[5], THIRD, 'antigravity'), (KEEP, AGY_KEEP, 'antigravity')]
            for tid, native, driver in mapping:
                add_thread(db, tid, native=native, driver=driver)
                engine = {'claudeAgent': 'claude'}.get(driver, driver)
                bound = {'thread_id': tid, 'engine': engine, 'root': str({'codex': self.home, 'claude': self.claude, 'antigravity': self.agyroot}[engine])}
                if engine == 'codex': bound['codex_binary'] = str(self.codexbin)
                if engine == 'antigravity':
                    bound.update(provider_instance_id='antigravity', native_layout='cli', cli_binary=str(binary), cli_user_home=str(self.base/'profile'))
                bindings.append(bound)
                if tid in CHILDREN:
                    payload = json.loads(db.execute(f'SELECT payload_json FROM {V2}threads WHERE thread_id=?', (tid,)).fetchone()[0])
                    payload['lineage'].update(parentThreadId=ROOTS[0], rootThreadId=ROOTS[0], relationshipToParent='subagent')
                    db.execute(f'UPDATE {V2}threads SET payload_json=? WHERE thread_id=?', (json.dumps(payload), tid))
            payload = json.dumps({'id': SID, 'providerInstanceId': 'codex', 'driver': 'codex', 'status': 'stopped'})
            insert(db, V2+'provider_sessions', provider_session_id=SID, thread_id=KEEP, provider='codex', provider_instance_id='codex', status='stopped', payload_json=payload)
            for tid in [*SELECTED, KEEP]: insert(db, V2+'provider_session_bindings', provider_session_id=SID, thread_id=tid)
            attachment = 'keeper-55555555-5555-4555-8555-555555555555-pptx'
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?',
                (json.dumps({'role': 'user', 'text': 'Keep mention '+ROOTS[0], 'attachments': [{'type': 'file', 'id': attachment}]}), KEEP))
            insert(db, V2+'turn_items', turn_item_id='audit', thread_id=KEEP,
                   payload_json=json.dumps({'type': 'dynamic_tool', 'toolName': 't3-code.t3_thread_read', 'input': {'threadId': ROOTS[0]}, 'output': {'threadId': ROOTS[0]}}))
            db.commit()
        with closing(sqlite3.connect(self.agyroot/agy.SUMMARY)) as db:
            db.execute(agy.SEARCH_VERSION_SQL); db.execute('INSERT INTO search_schema_version VALUES(3)'); db.execute(agy.SEARCH_SQL)
            for sid in [TARGET, CHILD, THIRD, AGY_KEEP]:
                db.execute('INSERT INTO conversations_fts(conversation_id,chunk_index,title,user_body,agent_body) VALUES(?,0,?,?,?)', (sid, 'Synthetic', 'deletedsearchterm' if sid != AGY_KEEP else 'preservedsearchterm', 'fixture'))
            db.commit()
        transcript = self.claude/'projects/synthetic'/(CLAUDE+'.jsonl')
        transcript.parent.mkdir(parents=True); transcript.write_text(json.dumps({'sessionId': CLAUDE, 'message': 'fixture'})+'\n')
        keeper = transcript.with_name('88888888-8888-4888-8888-888888888888.jsonl')
        keeper.write_text(json.dumps({'sessionId': keeper.stem, 'message': 'keep'})+'\n')
        (self.root/'userdata/environment-id').write_text('env-1'); (self.root/lifecycle.OWNER).touch()
        (self.root/'userdata/settings.json').write_text(json.dumps({'providerInstances': {'antigravity': {'driver': 'antigravity', 'enabled': True, 'config': {'binaryPath': str(binary)}}}}))
        self.desktop = self.base/'desktop'; local_storage(self.desktop, thread_id=CHILDREN[0])
        attachments = self.root/'userdata/attachments'; attachments.mkdir()
        for suffix in ('.pptx', '.pptx.txt'): (attachments/(attachment+suffix)).write_bytes(b'unselected upload')
        self.preserved = [*attachments.iterdir(), keeper, self.agyroot/'conversations'/(AGY_KEEP+'.db'), self.native_paths[-1]]
        self.before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.preserved}
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            self.kept_native_row = db.execute('SELECT * FROM threads WHERE id=?', ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',)).fetchone()
        manifest = {'schema_version': 'larj.t3code-bindings.v1', 'profile_root': str(self.root), 'environment_id': 'env-1',
                    'runtime_binaries': [str(self.frontend), str(binary)], 'native_stores': bindings,
                    'desktop_profiles': [{'root': str(self.desktop), 'runtime_binaries': [str(self.frontend)]}]}
        self.bindings = self.base/'bindings.json'; self.bindings.write_text(json.dumps(manifest))
        env = {k: v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','PATHEXT','COMSPEC','LARJ_LEVELDB_RUNTIME','LARJ_NODE','LAM_REHEARSAL_CODEX_BINARY'}}
        self.env = {**env, 'TEMP': str(self.base), 'TMP': str(self.base), 'PYTHONUTF8': '1', 'USERPROFILE': str(self.base/'profile'), 'HOME': str(self.base/'profile'),
                    'APPDATA': str(self.base/'appdata'), 'LOCALAPPDATA': str(self.base/'local'), 'CODEX_HOME': str(self.home),
                    'CLAUDE_CONFIG_DIR': str(self.claude), 'T3CODE_HOME': str(self.root), 'PASEO_HOME': str(self.base/'no-paseo'),
                    'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9', 'DO_NOT_TRACK': '1'}
        self.plan_path = self.base/'plan.json'

    def call(self, label, args):
        completed = subprocess.run([sys.executable, '-m', 'tests.test_t3code_cli_rehearsal', '--worker', *args, '--json', '--progress'],
                                   cwd=Path(__file__).resolve().parents[1], env=self.env, capture_output=True, timeout=180)
        (self.evidence/(label+'.json')).write_bytes(completed.stdout)
        (self.evidence/(label+'.jsonl')).write_bytes(completed.stderr)
        self.assertTrue(completed.stdout, completed.stderr.decode(errors='replace'))
        result = json.loads(completed.stdout)
        self.assertNotIn('T3_PRIVATE_MESSAGE_TITLE_TOKEN_SENTINEL', completed.stdout.decode())
        return completed.returncode, result

    def plan(self):
        args = ['delete', 'plan', '--client', 't3code', '--t3code-bindings', str(self.bindings), '--out', str(self.plan_path)]
        for tid in ROOTS: args += ['--record-id', tid]
        code, plan = self.call('plan', args)
        self.assertEqual(code, 0, plan); self.assertEqual(plan['goal_status'], 'ready', plan)
        # stdout is a metadata presentation (it may omit nulls). Only --out
        # is the immutable approval document consumed by apply/status/verify.
        frozen = json.loads(self.plan_path.read_text(encoding='utf-8'))
        from local_agent_manager.operation_store import plan_sha256
        from local_agent_manager.office_cleanup import digest
        self.assertEqual(frozen['plan_sha256'], plan['plan_sha256'])
        self.assertEqual(plan_sha256(frozen), frozen['plan_sha256'])
        evidence = cleanup.evidence_from_document(frozen)
        frontend = next(a for a in frozen['actions'] if a['kind'] == cleanup.KIND)
        self.assertEqual(frontend['binding']['snapshot_fingerprint'], digest(evidence))
        (self.evidence/'plan-file.json').write_bytes(self.plan_path.read_bytes())
        plan = frozen
        self.common = ['--operation-id', plan['operation_id'], '--plan', str(self.plan_path)]
        evidence = cleanup.evidence_from_document(plan)
        self.assertEqual(set(evidence['selected_thread_ids']), set(SELECTED))
        self.assertEqual({t['engine']: len(t['ids']) for t in evidence['native_targets']},
                         {'codex': 4, 'claude': 1, 'antigravity': 3})
        return plan

    def apply(self, plan):
        return self.call('apply', ['delete', 'apply', *self.common, '--clients-closed', '--authorized-plan-sha256', plan['plan_sha256']])

    def test_cold_mixed_cli_chain(self):
        plan = self.plan()
        self.assertEqual(self.call('status-before', ['operation','status', *self.common])[1]['goal_status'], 'ready')
        code, result = self.apply(plan)
        self.assertEqual(code, 0, result); self.assertEqual(result['goal_status'], 'complete', result)
        for phase in ('status', 'verify', 'verify-cold-reopen'):
            command = 'status' if phase == 'status' else 'verify'
            code, result = self.call(phase, ['operation', command, *self.common])
            self.assertEqual(code, 0, result); self.assertTrue(result['goal_satisfied'], result)
        local_storage(self.desktop, inspect=True, thread_id=CHILDREN[0])
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            self.assertEqual(db.execute(f'SELECT thread_id FROM {V2}threads').fetchall(), [(KEEP,)])
            self.assertEqual(db.execute(f'SELECT thread_id FROM {V2}provider_session_bindings').fetchall(), [(KEEP,)])
            self.assertEqual(db.execute(f'SELECT provider_session_id FROM {V2}provider_sessions').fetchall(), [(SID,)])
            self.assertEqual(db.execute(f'SELECT count(*) FROM {V2}turn_items WHERE turn_item_id="audit"').fetchone()[0], 1)
        with closing(sqlite3.connect(self.agyroot/agy.SUMMARY)) as db:
            self.assertEqual(db.execute('SELECT conversation_id FROM conversation_summaries').fetchall(), [(AGY_KEEP,)])
            self.assertEqual(db.execute("SELECT count(*) FROM conversations_fts WHERE conversations_fts MATCH 'deletedsearchterm'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM conversations_fts WHERE conversations_fts MATCH 'preservedsearchterm'").fetchone()[0], 1)
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.preserved}
        self.assertEqual(after, self.before)
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            self.assertEqual(db.execute('SELECT id FROM threads').fetchall(), [('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',)])
            self.assertEqual(db.execute('SELECT * FROM threads').fetchone(), self.kept_native_row)
        self.assertFalse((self.claude/'projects/synthetic'/(CLAUDE+'.jsonl')).exists())
        self.assertFalse(any(p.exists() for p in self.native_paths[:-1]))
        self.assertEqual(self.call('apply-completed-no-replay', ['delete','apply', *self.common, '--clients-closed', '--authorized-plan-sha256', plan['plan_sha256']])[1]['goal_status'], 'complete')
        (self.evidence/'preservation.json').write_text(json.dumps({'target_t3_remaining': 0, 'native_targets_remaining': 0,
             'unchanged_files': len(after), 'shared_session_and_keeper_binding_preserved': True, 'audit_preserved': True,
             'fts_cold_reopen_verified': True, 'leveldb_cold_reopen_verified': True,
             'codex_transport': 'installed_app_server' if os.environ.get('LAM_REHEARSAL_CODEX_BINARY') else 'injected_test_transport'}, indent=2))

    @unittest.skipUnless(os.environ.get('LAM_REHEARSAL_CODEX_BINARY'), 'requires copied real Codex app-server')
    def test_handoff_17_frontend_18_native_records_with_retained_diagnostics(self):
        from tests.test_antigravity_cli_references import field, step
        selected = list(SELECTED)
        manifest = json.loads(self.bindings.read_text())
        with closing(sqlite3.connect(self.root / 'userdata/statev2.sqlite')) as db:
            for i in range(9):
                tid, nid = f'additional-child-{i}', f'bbbbbbbb-bbbb-4bbb-8bbb-{i:012d}'
                selected.append(tid)
                self.native_paths.insert(-1, add_native_record(self.home, nid))
                add_thread(db, tid, native=nid)
                manifest['native_stores'].append(dict(thread_id=tid, engine='codex',
                    root=str(self.home), codex_binary=str(self.codexbin)))
            for tid in selected[1:]:
                db.execute(f"UPDATE {V2}threads SET payload_json=json_set(payload_json,"
                    "'$.lineage.parentThreadId',?,'$.lineage.rootThreadId',?,"
                    "'$.lineage.relationshipToParent','subagent') WHERE thread_id=?", (ROOTS[0], ROOTS[0], tid))
            for tid in ('keeper-two', 'keeper-deleted'):
                add_thread(db, tid, native=AGY_KEEP, driver='antigravity')
                original = next(b for b in manifest['native_stores'] if b['thread_id'] == KEEP)
                manifest['native_stores'].append({**original, 'thread_id': tid})
            db.execute(f"UPDATE {V2}threads SET deleted_at='2026-11-09T00:00:00Z' WHERE thread_id='keeper-deleted'")
            db.execute(f"UPDATE {V2}messages SET payload_json=json_set(payload_json,'$.context',json(?)) WHERE thread_id=?",
                (json.dumps({'version': 1, 'records': [{'version': 1, 'kind': 'thread', 'threadId': ROOTS[0],
                    'contextId': 'thread_' + ROOTS[0], 'label': 'mention', 'title': 'retained mention'}]}), KEEP))
            audit = [dict(type='reasoning', text='Diagnose ' + TARGET),
                     dict(type='file_change', fileName='repair.py', diffStr='+ inspect ' + TARGET),
                     dict(type='command_execution', input='inspect ' + TARGET, output=TARGET),
                     dict(type='dynamic_tool', toolName='cua_repl.js',
                          output=[{'type': 'text', 'text': 'Displayed ' + TARGET}]),
                     dict(type='dynamic_tool', toolName='t3-code.t3_thread_send',
                          input={'threadId': KEEP, 'message': 'Diagnose ' + TARGET}, output={'threadId': KEEP})]
            for i, payload in enumerate(audit):
                insert(db, V2 + 'turn_items', turn_item_id=f'retained-{i}', thread_id=KEEP,
                       type=payload['type'], payload_json=json.dumps(payload))
                for table in ('orchestration_events', 'orchestration_v2_events'):
                    owner = dict(stream_id=KEEP, aggregate_kind='thread', stream_version=i+1) if table == 'orchestration_events' else dict(thread_id=KEEP)
                    insert(db, table, event_id=f'audit-{i}', event_type='turn-item.updated',
                           payload_json=json.dumps(payload), **owner)
            attachment = KEEP + '-44444444-4444-4444-8444-444444444444-html'
            render = dict(id='html', threadId=KEEP, type='dynamic_tool', status='completed',
                          toolName='t3-code.html_render', output={'htmlRender': {
                              'attachmentId': attachment, 'title': 'kept', 'height': 250}})
            insert(db, V2+'turn_items', turn_item_id='html', thread_id=KEEP, type='dynamic_tool',
                   status='completed', payload_json=json.dumps(render))
            db.commit()
            preserved_rows = {table: db.execute(f'SELECT * FROM {V2}{table} WHERE thread_id IN (?,?,?) ORDER BY rowid',
                (KEEP, 'keeper-two', 'keeper-deleted')).fetchall() for table in ('threads', 'messages', 'turn_items')}
        self.native_paths.insert(-1, add_native_record(self.home, 'cccccccc-cccc-4ccc-8ccc-000000000001', parent=CODEX[0]))
        html = self.root / 'userdata/attachments' / (attachment + '.html')
        html.write_bytes(b'<html>keep this artifact</html>')
        diagnostic = self.agyroot / 'brain' / AGY_KEEP / '.system_generated/steps/1/output.txt'
        diagnostic.parent.mkdir(parents=True)
        diagnostic.write_text(TARGET)
        row = step(15, field(20, field(3, TARGET.encode())))
        with closing(sqlite3.connect(self.agyroot / 'conversations' / (AGY_KEEP + '.db'))) as db:
            db.execute('INSERT INTO steps(idx,step_type,step_format,step_payload) VALUES(1,15,0,?)', (row['step_payload'],))
            db.execute('INSERT INTO gen_metadata(idx,data) VALUES(1,?)', (field(1, field(2, TARGET.encode())),))
            db.commit()
        self.preserved.extend((html, diagnostic))
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.preserved}
        self.bindings.write_text(json.dumps(manifest))
        self.env['LAM_REHEARSAL_HANDOFF'] = '1'
        receipt_path = self.base / 'handoff-receipt.json'
        code, started = self.call('handoff-start', ['delete', 'run', '--client', 't3code',
            '--t3code-bindings', str(self.bindings), '--record-id', ROOTS[0], '--handoff', '--out', str(receipt_path)])
        self.assertEqual(code, 0, started)
        self.assertEqual(started['expanded_thread_count'], 17)
        self.assertEqual(started['native_record_count'], 18)
        receipt = json.loads(receipt_path.read_text())
        self.assertTrue(receipt['goal_satisfied'], receipt)
        self.assertEqual(receipt['apply']['goal_status'], 'complete', receipt)
        self.assertEqual(receipt['cold_verify']['goal_status'], 'complete', receipt)
        self.assertEqual(receipt['closed_targets'], [])
        (self.evidence / 'handoff-receipt.json').write_bytes(receipt_path.read_bytes())
        plan = receipt['plan']
        for phase in ('status', 'verify'):
            code, result = self.call('handoff-cold-' + phase, ['operation', phase,
                '--operation-id', plan['operation_id'], '--plan', plan['plan_path']])
            self.assertEqual(code, 0, result)
            self.assertTrue(result['goal_satisfied'], result)
        with closing(sqlite3.connect(self.root / 'userdata/statev2.sqlite')) as db:
            self.assertEqual({r[0] for r in db.execute(f'SELECT thread_id FROM {V2}threads')},
                             {KEEP, 'keeper-two', 'keeper-deleted'})
            for table, expected in preserved_rows.items():
                self.assertEqual(db.execute(f'SELECT * FROM {V2}{table} ORDER BY rowid').fetchall(), expected)
        self.assertEqual({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.preserved}, before)
        self.assertFalse(any(p.exists() for p in self.native_paths[:-1]))
        self.assertFalse((self.claude/'projects/synthetic'/(CLAUDE+'.jsonl')).exists())
        with closing(sqlite3.connect(self.agyroot/agy.SUMMARY)) as db:
            self.assertEqual(db.execute('SELECT conversation_id FROM conversation_summaries').fetchall(), [(AGY_KEEP,)])
        local_storage(self.desktop, inspect=True, thread_id=CHILDREN[0])
        (self.evidence / 'preservation.json').write_text(json.dumps(dict(
            target_t3_remaining=0, native_targets_remaining=0, kept_threads=3,
            kept_rows_unchanged=True, kept_files_unchanged=True, real_codex_app_server=True,
            detached_worker=True, cold_status_and_verify=True, production_writes=0), indent=2))

    def test_real_target_drift_blocks_without_child_journals(self):
        plan = self.plan()
        with closing(sqlite3.connect(self.root/'userdata/statev2.sqlite')) as db:
            db.execute(f'UPDATE {V2}messages SET payload_json=? WHERE thread_id=?', ('{"text":"changed"}', ROOTS[0])); db.commit()
        code, result = self.apply(plan)
        self.assertEqual(code, 3, result); self.assertFalse(result['modified']); self.assertFalse(result['mutation_started'])
        status = self.call('status', ['operation','status', *self.common])[1]
        self.assertEqual(status['goal_status'], 'ready', status)
        self.assertEqual({b['status'] for b in status['batches']}, {'pending'})
        for batch in plan['child_batches']:
            root = Path(next(s['path'] for s in plan['storages'] if s['storage_id']==batch['storage_id']))
            self.assertFalse(OperationStore(root, batch['child_operation_id']).directory.exists())

    def test_new_cross_frontend_reference_blocks(self):
        plan = self.plan()
        other = self.base/'no-paseo'; (other/'agents').mkdir(parents=True)
        from tests.test_antigravity_cli_joint import paseo_agent
        (other/'agents/other.json').write_text(json.dumps(paseo_agent('other', TARGET, self.base)))
        code, result = self.apply(plan)
        self.assertEqual(code, 3, result); self.assertFalse(result['mutation_started']); self.assertFalse(result['modified'])

    def test_lost_result_unknown_is_not_replayed_and_cold_verify_recovers(self):
        plan = self.plan()
        self.env['LAM_REHEARSAL_FAULT'] = 'lost-frontend-result'
        code, result = self.apply(plan)
        self.assertEqual(result['goal_status'], 'unknown', result)
        self.assertTrue(result['mutation_started'], result)
        self.env.pop('LAM_REHEARSAL_FAULT')
        self.assertEqual(self.call('status-unknown', ['operation','status', *self.common])[1]['goal_status'], 'unknown')
        self.assertEqual(self.call('apply-no-replay', ['delete','apply', *self.common, '--clients-closed', '--authorized-plan-sha256', plan['plan_sha256']])[1]['goal_status'], 'unknown')
        self.assertEqual(self.call('verify-recovery', ['operation','verify', *self.common])[1]['goal_status'], 'complete')
        self.assertEqual(self.call('status-recovered', ['operation','status', *self.common])[1]['goal_status'], 'complete')


if __name__ == '__main__':
    if '--worker' in sys.argv:
        raise SystemExit(worker(sys.argv[sys.argv.index('--worker')+1:]))
    unittest.main()
