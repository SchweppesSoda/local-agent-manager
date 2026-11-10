"""Opt-in installed-runtime tests. All stores, credentials and worktrees are fixtures."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_agent_record_janitor import t3code_server_delete as t3, antigravity_acp_cleanup as acp
from local_agent_record_janitor import claude_session_import
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.t3code_bound_adapter import T3CodeBoundAdapter, SCHEMA
from local_agent_record_janitor.t3code_store import V2
from tests.t3code_support import create_store, add_thread

TARGET='11111111-1111-4111-8111-111111111111'
KEEP='22222222-2222-4222-8222-222222222222'
NATIVE='33333333-3333-4333-8333-333333333333'


def isolated_env(base):
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','PATHEXT','COMSPEC'}}
    env.update(USERPROFILE=str(base),HOME=str(base),APPDATA=str(base/'appdata'),LOCALAPPDATA=str(base/'local'),
               TEMP=str(base),TMP=str(base),CODEX_HOME=str(base/'codex'),CLAUDE_CONFIG_DIR=str(base/'claude'),DO_NOT_TRACK='1')
    return env


@contextmanager
def process(arguments,base,env,*,piped=False):
    launcher=Path(t3.__file__).with_name('orca_runtime_launcher.py')
    with (base/'fixture-runtime.log').open('ab') as log:
        p=subprocess.Popen([sys.executable,'-I',str(launcher),*map(str,arguments)],env=env,cwd=base,
            stdin=subprocess.PIPE if piped else subprocess.DEVNULL,stdout=subprocess.PIPE if piped else log,
            stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            yield p
        finally:
            if p.poll() is None:p.kill()
            p.wait(timeout=10)
            for stream in (p.stdin,p.stdout):
                if stream:stream.close()


class AcpClient:
    def __init__(self,p):
        self.p=p;self.counter=0;self.queue=queue.Queue()
        def read():
            for line in iter(p.stdout.readline,b''):self.queue.put(json.loads(line))
        threading.Thread(target=read,daemon=True).start()

    def call(self,method,params):
        self.counter+=1
        self.p.stdin.write((json.dumps({'jsonrpc':'2.0','id':self.counter,'method':method,'params':params})+'\n').encode())
        self.p.stdin.flush()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            item=self.queue.get(timeout=max(.1,deadline-time.monotonic()))
            if item.get('id')==self.counter:
                if 'error' in item:raise AssertionError('ACP fixture request failed: '+method)
                return item['result']
        raise TimeoutError(method)


@unittest.skipUnless(os.name=='nt' and os.environ.get('LARJ_T3_BUNDLE_ACCEPTANCE')=='1',
                     'Opt in with installed qualified T3 and managed ACP; temporary profiles only')
class T3RuntimeAcceptanceTests(unittest.TestCase):
    def test_project_history_import_and_reimport_preserve_sources(self):
        install = t3._bundled_install()
        with tempfile.TemporaryDirectory(prefix='i-') as directory:
            root = Path(directory).resolve()
            profile = root / 't3'
            (profile / 'userdata').mkdir(parents=True)
            workspace = root / 'p q'
            other_workspace = root / 'other'
            workspace.mkdir()
            other_workspace.mkdir()
            claude_home = root / 'claude'
            codex_home = root / 'codex'
            fixture_binary = root / 'fixture-provider.exe'
            fixture_binary.write_bytes(b'synthetic provider, never used for generation')
            timestamp = datetime.now(timezone.utc).isoformat()
            sentinel = 'PRIVATE_SYNTHETIC_IMPORT_BODY'
            sources = {}
            for native_id, cwd in ((NATIVE, workspace), (KEEP, other_workspace)):
                transcript = claude_home / 'projects' / 'fixture' / (native_id + '.jsonl')
                transcript.parent.mkdir(parents=True, exist_ok=True)
                records = [
                    {'type': 'user', 'sessionId': native_id, 'cwd': str(cwd), 'timestamp': timestamp,
                     'message': {'role': 'user', 'content': sentinel}},
                    {'type': 'assistant', 'sessionId': native_id, 'cwd': str(cwd), 'timestamp': timestamp,
                     'message': {'role': 'assistant', 'content': 'synthetic response'}},
                ]
                transcript.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
                sources[transcript] = transcript.read_bytes()
            session_date = datetime.now(timezone.utc).strftime('%Y/%m/%d')
            transcript = codex_home / 'sessions' / session_date / ('rollout-' + TARGET + '.jsonl')
            transcript.parent.mkdir(parents=True)
            records = [
                {'type': 'session_meta', 'timestamp': timestamp,
                 'payload': {'id': TARGET, 'cwd': str(workspace)}},
                {'type': 'event_msg', 'timestamp': timestamp,
                 'payload': {'type': 'user_message', 'message': sentinel}},
                {'type': 'response_item', 'timestamp': timestamp,
                 'payload': {'type': 'message', 'role': 'assistant',
                             'content': [{'type': 'output_text', 'text': 'synthetic response'}]}},
            ]
            transcript.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
            sources[transcript] = transcript.read_bytes()
            pi_sessions = root / 'pi' / 'sessions'
            pi_sessions.mkdir(parents=True)
            pi_transcript = pi_sessions / 'fixture.jsonl'
            records = [
                {'type': 'session', 'version': 3, 'id': '44444444-4444-4444-8444-444444444444',
                 'cwd': str(workspace), 'timestamp': timestamp},
                {'type': 'message', 'id': 'user', 'parentId': None, 'timestamp': timestamp,
                 'message': {'role': 'user', 'content': [{'type': 'text', 'text': sentinel}]}},
                {'type': 'message', 'id': 'assistant', 'parentId': 'user', 'timestamp': timestamp,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'synthetic response'}]}},
            ]
            pi_transcript.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
            sources[pi_transcript] = pi_transcript.read_bytes()
            settings = {'enableProviderUpdateChecks': False, 'providerInstances': {
                'claudeAgent': {'driver': 'claudeAgent', 'enabled': True, 'config': {
                    'binaryPath': str(fixture_binary), 'homePath': str(claude_home)}},
                'codex': {'driver': 'codex', 'enabled': True, 'config': {
                    'binaryPath': str(fixture_binary), 'homePath': str(codex_home)}},
                'pi': {'driver': 'pi', 'enabled': True, 'config': {}, 'environment': [
                    {'name': 'PI_CODING_AGENT_SESSION_DIR', 'value': str(pi_sessions), 'sensitive': False}]},
                'antigravity': {'driver': 'antigravity', 'enabled': False, 'config': {}},
            }}
            (profile / 'userdata/settings.json').write_text(json.dumps(settings), encoding='utf-8')
            env = isolated_env(root)
            env.update(ELECTRON_RUN_AS_NODE='1', T3CODE_HOME=str(profile),
                       HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9',
                       NO_PROXY='127.0.0.1,localhost')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            arguments = [install / t3._RUNTIME_FILES[0], install / t3._ENTRY, 'serve',
                         '--base-dir', profile, '--host', '127.0.0.1', '--port', str(port),
                         '--mode', 'desktop', '--no-browser']
            with process(arguments, root, env) as proc, patch.dict(os.environ, env, clear=True):
                for _ in range(90):
                    if (profile / 'userdata/server-runtime.json').exists():
                        break
                    self.assertIsNone(proc.poll(), 'Fixture T3 server exited during startup')
                    time.sleep(.5)
                # Seed the existing-project fixture, then use the real product importer.
                with closing(sqlite3.connect(profile / 'userdata/statev2.sqlite')) as db:
                    db.execute('''INSERT INTO projection_projects
                        (project_id,title,workspace_root,scripts_json,created_at,updated_at)
                        VALUES('fixture','fixture',?,'[]',?,?)''', (str(workspace), timestamp, timestamp))
                    db.commit()
                plan = claude_session_import.prepare_t3code_import(
                    executable=install / t3._RUNTIME_FILES[0], profile=profile,
                    cwd=workspace, project_id='fixture')
                version = json.loads((profile / 'userdata/server-runtime.json').read_text())['serverVersion']
                self.assertEqual(plan['version'], version)
                self.assertEqual(claude_session_import.verify_session_import(plan)['status'], 'not_observed')
                expected = {('claudeAgent', NATIVE), ('codex', TARGET), ('pi', str(pi_transcript))}
                for attempt, (status, new_count) in enumerate((('imported', 3), ('observed', 0)), start=1):
                    result = claude_session_import.run_session_import(
                        plan, approved_sha256=plan['plan_sha256'], accept_project_history=True)
                    self.assertEqual(result['status'], status, result)
                    self.assertEqual(result['skipped_count'], 0)
                    self.assertEqual(result['imported_count'], 3)
                    self.assertEqual(len(result['new_mappings']), new_count)
                    self.assertEqual({(row['driver'], row['native_id']) for row in result['mappings']}, expected)
                    self.assertNotIn(sentinel, json.dumps([plan, result]))
                    with closing(sqlite3.connect(profile / 'userdata/statev2.sqlite')) as db:
                        self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {V2}threads').fetchone()[0], 3)
                        token_counts = db.execute("""SELECT COUNT(*), SUM(revoked_at IS NOT NULL)
                            FROM auth_sessions WHERE client_label='janitor session import'""").fetchone()
                        self.assertEqual(token_counts, (attempt, attempt))
            verified = claude_session_import.verify_session_import(plan)
            self.assertEqual(verified['status'], 'observed', verified)
            self.assertEqual({(row['driver'], row['native_id']) for row in verified['mappings']}, expected)
            for path, original in sources.items():
                self.assertEqual(path.read_bytes(), original)

    def test_builtin_delete_mixed_providers_keeps_clean_git_worktree(self):
        install=t3._bundled_install()
        with tempfile.TemporaryDirectory(prefix='t3-') as directory:
            root=Path(directory).resolve();(root/'userdata').mkdir()
            repo=root/'repo';repo.mkdir();worktree=root/'worktrees'/'fixture'
            def git(*args):
                subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
            git('init');(repo/'keep.txt').write_text('committed worktree sentinel')
            git('add','keep.txt');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture')
            git('worktree','add','-b','fixture',str(worktree))
            settings={'worktreesDirectory':str(worktree.parent),'enableProviderUpdateChecks':False,
                'worktreeCleanup':{'mode':'custom','rules':{'worktreeKeepWhen':'uncommitted-changes',
                    'worktreeAfterDays':None,'worktreeSettledAfterDays':None,'worktreeOnDelete':True,'worktreeOnMerge':False,'worktreeUnchanged':False}}}
            (root/'userdata/settings.json').write_text(json.dumps(settings))
            native=root/'codex'/'sessions'/(NATIVE+'.jsonl');native.parent.mkdir(parents=True);native.write_bytes(b'native sentinel')
            env=isolated_env(root);env.update(ELECTRON_RUN_AS_NODE='1',T3CODE_HOME=str(root))
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            args=[install/t3._RUNTIME_FILES[0],install/t3._ENTRY,'serve','--base-dir',root,'--host','127.0.0.1',
                  '--port',str(port),'--mode','desktop','--no-browser']
            with process(args,root,env) as proc, patch.object(t3,'_env',return_value=env):
                for _ in range(90):
                    if (root/'userdata/server-runtime.json').exists():break
                    self.assertIsNone(proc.poll());time.sleep(.5)
                evidence={'root':str(root),'install':str(install),'server':t3._locator(root)}
                with closing(sqlite3.connect(root/'userdata/statev2.sqlite')) as db:
                    db.execute("INSERT INTO projection_projects(project_id,title,workspace_root,scripts_json,created_at,updated_at) VALUES('fixture','fixture',?,'[]',?,?)",
                        (str(repo),'2026-11-08T00:00:00Z','2026-11-08T00:00:00Z'));db.commit()
                unselected = '44444444-4444-4444-8444-444444444444'
                for tid,driver in ((TARGET,'codex'),(KEEP,'antigravity'),(unselected,'codex')):
                    t3._rpc(evidence,[{'type':'thread.create','commandId':'fixture-'+tid,'threadId':tid,'projectId':'fixture',
                        'title':'fixture','createdBy':'user','creationSource':'web','modelSelection':{'instanceId':driver,'model':'fixture'},
                        'runtimeMode':'full-access','interactionMode':'default','branch':'fixture' if tid==TARGET else None,
                        'worktreePath':str(worktree) if tid==TARGET else None,
                        'importedNativeThread':{'ref':{'driver':driver,'nativeId':NATIVE,'strength':'strong'}}}],30)
                keeper=t3._snapshot(root,unselected)
                coordinator=OperationCoordinator(CleanupService())
                from local_agent_manager.t3code_workflow import run_server_batch
                result=run_server_batch(coordinator, record_ids=(TARGET,KEEP), roots=(root,),
                    authorized=True, out=root/'batch.json')
                self.assertEqual(result['goal_status'],'complete',result)
                self.assertEqual(len(result['plans']),2)
                self.assertEqual(native.read_bytes(),b'native sentinel')
                self.assertEqual((worktree/'keep.txt').read_text(),'committed worktree sentinel')
                self.assertEqual(t3._snapshot(root,unselected),keeper)
                git('worktree','list','--porcelain')
                self.assertIn(str(worktree).replace('\\','/'),subprocess.run(['git','-C',str(repo),'worktree','list','--porcelain'],capture_output=True,text=True,check=True).stdout)

    def test_real_acp_files_full_delete_and_reopened_list(self):
        # Installation metadata only; all sessions and auth below are synthetic.
        platform=Path.home()/'.t3/tools/antigravity-acp/win32-x64'
        if not (platform/'active.json').exists():
            self.skipTest('Legacy managed ACP is not installed; do not reinstall for tests')
        active=json.loads((platform/'active.json').read_text())
        binaries=[platform/'versions'/active['releaseId']/name for name in acp.RUNTIMES]
        with tempfile.TemporaryDirectory(prefix='a-') as directory:
            root=Path(directory).resolve();instance='antigravity'
            gemini=root/'userdata/providers/antigravity'/hashlib.sha256(instance.encode()).hexdigest()
            native=gemini/'antigravity-acp';native.mkdir(parents=True)
            (native/'settings.json').write_text(json.dumps({'auth':{'type':'gemini-api-key'}}))
            worktree=root/'worktree';worktree.mkdir();(worktree/'keep.txt').write_text('workspace sentinel')
            env=isolated_env(root);env.update(GEMINI_HOME=str(gemini),AGY_ACP_FORCE_FILE_STORAGE='1',
                GEMINI_API_KEY='fixture-not-a-valid-key',HTTP_PROXY='http://127.0.0.1:9',HTTPS_PROXY='http://127.0.0.1:9',
                BROWSER='false',PYTHONUNBUFFERED='1')
            def initialize(p):
                client=AcpClient(p)
                info=client.call('initialize',{'protocolVersion':1,'clientCapabilities':{},'clientInfo':{'name':'janitor-fixture','version':'1'}})
                self.assertEqual(info['agentInfo']['version'],'1.4.5')
                return client
            with process([binaries[0]],root,env,piped=True) as p:
                client=initialize(p)
                ids=[client.call('session/new',{'cwd':str(worktree),'mcpServers':[]})['sessionId'] for _ in range(2)]
                self.assertEqual({s['sessionId'] for s in client.call('session/list',{})['sessions']},set(ids))
            (native/'acp_token.json').write_text('fixture credential retained')
            artifact=native/'brain'/ids[0]/'artifact.md';artifact.parent.mkdir(parents=True);artifact.write_text('fixture artifact')
            with closing(create_store(root)) as db:
                add_thread(db,TARGET,native=ids[0],driver=instance)
                add_thread(db,KEEP,native=ids[1],driver=instance)
            (root/'userdata/environment-id').write_text('fixture-environment');(root/'userdata/server-owner.sqlite').touch()
            (root/'userdata/settings.json').write_text(json.dumps({'providerInstances':{
                instance:{'driver':instance,'enabled':True,'config':{'binaryPath':str(binaries[0])}}}}))
            t3_binary=root/'fixture-t3.exe';t3_binary.write_bytes(b'never executed')
            manifest={'schema_version':SCHEMA,'profile_root':str(root),'environment_id':'fixture-environment',
                'runtime_binaries':[str(t3_binary),*map(str,binaries)],'desktop_profiles':[],
                'native_stores':[{'thread_id':tid,'engine':instance,'root':str(native),'provider_instance_id':instance,
                    'acp_runtime_binaries':list(map(str,binaries))} for tid in (TARGET,KEEP)]}
            coordinator=OperationCoordinator(CleanupService(client_inspector=lambda *_a,**_kw:()))
            with patch('local_agent_record_janitor.office_runtime.processes',return_value=[]), \
                    patch('pathlib.Path.home',return_value=root), \
                    patch.dict(os.environ,{**isolated_env(root),'T3CODE_HOME':str(root)},clear=True):
                plan=coordinator.plan_operation(client='t3code',record_ids=[TARGET],adapters=[T3CodeBoundAdapter(manifest)],plan_path=root/'plan.json')
                self.assertEqual(plan['goal_status'],'ready',plan)
                result=coordinator.apply_operation(operation_id=plan['operation_id'],plan_path=root/'plan.json',plan_sha256=plan['plan_sha256'],clients_closed=True)
                self.assertEqual(result['goal_status'],'complete',result)
                self.assertEqual(coordinator.verify_operation(operation_id=plan['operation_id'],plan_path=root/'plan.json')['goal_status'],'complete')
            self.assertFalse(artifact.exists());self.assertTrue((worktree/'keep.txt').exists())
            self.assertEqual((native/'acp_token.json').read_text(),'fixture credential retained')
            # Listing needs no authentication; remove only our fake credential so
            # this deliberate invalid JSON cannot distract the fixture runtime.
            (native/'acp_token.json').unlink()
            with process([binaries[0]],root,env,piped=True) as p:
                client=initialize(p)
                self.assertEqual([s['sessionId'] for s in client.call('session/list',{})['sessions']],[ids[1]])


@unittest.skipUnless(os.name=='nt' and os.environ.get('LARJ_AGY_CLI_ACCEPTANCE')=='1',
                     'Opt in with an explicit agy CLI path; temporary profiles only')
class AgyCliAcceptanceTests(unittest.TestCase):
    def test_cli_153_uses_user_profile_despite_t3_gemini_home(self):
        binary=Path(os.environ['LARJ_AGY_CLI_BINARY'])
        self.assertTrue(binary.is_absolute())
        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(),
                         '47b1bec8c4a6d4afa1587177530e329cee4f5c6e76573a12ac9ab47b846e50e6',
                         'Requalify another CLI build before changing storage claims')
        with tempfile.TemporaryDirectory(prefix='agy-') as directory:
            root=Path(directory).resolve();gemini=root/'t3-instance'
            native=gemini/'antigravity-acp';native.mkdir(parents=True)
            (native/'settings.json').write_text(json.dumps({'auth':{'type':'gemini-api-key'}}))
            env=isolated_env(root);env.update(GEMINI_HOME=str(gemini),AGY_ACP_FORCE_FILE_STORAGE='1',
                GEMINI_API_KEY='fixture-not-a-valid-key',HTTP_PROXY='http://127.0.0.1:9',
                HTTPS_PROXY='http://127.0.0.1:9',BROWSER='false',PYTHONUNBUFFERED='1')
            with process([binary,'acp'],root,env,piped=True) as p:
                client=AcpClient(p)
                info=client.call('initialize',{'protocolVersion':1,'clientCapabilities':{}})
                self.assertEqual(info['agentInfo']['version'],'1.5.3')
                self.assertNotIn('delete',info['agentCapabilities']['sessionCapabilities'])
                with self.assertRaisesRegex(AssertionError,'ACP fixture request failed: session/new'):
                    client.call('session/new',{'cwd':str(root),'mcpServers':[]})
            self.assertTrue((root/'.gemini/antigravity-cli/conversation_summaries.db').is_file())
            self.assertEqual({p.name for p in native.iterdir()},{'settings.json'})


if __name__=='__main__':unittest.main()
