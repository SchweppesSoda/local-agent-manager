"""Opt-in installed-runtime tests. All stores, credentials and worktrees are fixtures."""
from contextlib import closing, contextmanager
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
from local_agent_record_janitor.cleanup_service import CleanupService
from local_agent_record_janitor.operation_coordinator import OperationCoordinator
from local_agent_record_janitor.t3code_bound_adapter import T3CodeBoundAdapter, SCHEMA
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
                for tid,driver in ((TARGET,'codex'),(KEEP,'antigravity')):
                    t3._rpc(evidence,[{'type':'thread.create','commandId':'fixture-'+tid,'threadId':tid,'projectId':'fixture',
                        'title':'fixture','createdBy':'user','creationSource':'web','modelSelection':{'instanceId':driver,'model':'fixture'},
                        'runtimeMode':'full-access','interactionMode':'default','branch':'fixture' if tid==TARGET else None,
                        'worktreePath':str(worktree) if tid==TARGET else None,
                        'importedNativeThread':{'ref':{'driver':driver,'nativeId':NATIVE,'strength':'strong'}}}],30)
                keeper=t3._snapshot(root,KEEP)
                coordinator=OperationCoordinator(CleanupService())
                for tid in (TARGET,KEEP):
                    plan_path=root/(tid+'.plan.json')
                    plan=coordinator.plan_operation(scope={'client':'t3code','t3code_only':True,'record_ids':[tid]},
                        t3code_roots=[root],plan_path=plan_path)
                    self.assertEqual(plan['goal_status'],'ready',plan)
                    result=coordinator.apply_operation(scope={'t3code_only':True},operation_id=plan['operation_id'],
                        plan_path=plan_path,plan_sha256=plan['plan_sha256'])
                    self.assertEqual(result['goal_status'],'complete',result)
                    self.assertEqual(coordinator.verify_operation(operation_id=plan['operation_id'],plan_path=plan_path)['goal_status'],'complete')
                    self.assertEqual(native.read_bytes(),b'native sentinel')
                    self.assertEqual((worktree/'keep.txt').read_text(),'committed worktree sentinel')
                    if tid==TARGET:self.assertEqual(t3._snapshot(root,KEEP),keeper)
                git('worktree','list','--porcelain')
                self.assertIn(str(worktree).replace('\\','/'),subprocess.run(['git','-C',str(repo),'worktree','list','--porcelain'],capture_output=True,text=True,check=True).stdout)

    def test_real_acp_files_full_delete_and_reopened_list(self):
        # Installation metadata only; all sessions and auth below are synthetic.
        platform=Path.home()/'.t3/tools/antigravity-acp/win32-x64'
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


if __name__=='__main__':unittest.main()
