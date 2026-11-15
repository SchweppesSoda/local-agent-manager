from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

import base64
from contextlib import redirect_stdout
from contextlib import closing
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_agent_manager.cindy_isolation_controller import (FixedChannel, FixedSupervisor, authenticated,
    cleanup_source, protected_snapshot, signed, validate_acceptance, verify_accepted_runtime, verify_protected_state, write_new_json)
from local_agent_manager.cindy_isolation_execution import admitted_import
from local_agent_manager.cindy_isolation_plan import fingerprint
from local_agent_manager.cindy_isolation_resume import import_entries
from local_agent_manager.cindy_isolation_windows import WindowsImportBoundary, process_evidence, verify_runtime_switch
from local_agent_manager.cindy_isolation_ui import ProductAcceptance
from local_agent_manager.adapters import CindyAdapter, NativeIntegrityAdapter
from local_agent_manager.cleanup_service import CleanupService
from local_agent_manager.operation_coordinator import OperationCoordinator
from tests.cindy_isolation_fixture import make_fixture
from tests.support import create_cindy_database


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


class FixtureUI:
    def __init__(self, case): self.case, self.driver, self.text = case, None, None
    def perform(self, action, frame_name, **options):
        (self.case.run / frame_name).write_bytes(PNG)
        if action == "type_probe": self.text = options["text"]
        if action == "submit_probe":
            task = self.driver.current_task
            native = task["sdk_session_id"] or "new-native"
            path = Path(self.case.spec['native_files'][0]['target_path']) if native == 'selected' else Path(self.case.spec['target']['path'])/'sessions/rollout-new-native.jsonl'
            if not path.exists():
                path.write_text(json.dumps({"type":"session_meta","payload":{"id":native}})+"\n")
                with closing(sqlite3.connect(Path(self.case.spec["target"]["path"]) / "state_5.sqlite")) as c:
                    c.execute("insert into threads(id,rollout_path) values(?,?)", (native,str(path))); c.commit()
            token = self.driver.probes[self.driver.current_view]["nonce"]
            with path.open("a", encoding="utf8") as stream:
                for role,text in (("user",self.text),("assistant","ISOLATION_OK "+token)):
                    stream.write(json.dumps({"type":"response_item","payload":{"type":"message","role":role,"content":[{"type":"input_text" if role=="user" else "output_text","text":text}]}},ensure_ascii=False)+"\n")
            with closing(sqlite3.connect(self.case.database)) as c:
                c.execute("update sessions set sdk_session_id=? where id=?",(native,task["id"]));c.commit()
            if native=='new-native':
                registry=Path(self.case.spec['registry_path']);value=json.loads(registry.read_text())
                value['bindings']['openai:native:'+native]={'providerId':'openai','accountId':'default'}
                registry.write_text(json.dumps(value))
            with closing(sqlite3.connect(Path(self.case.spec["target"]["path"]) / "state_5.sqlite")) as c:
                c.execute("update threads set updated_at=2 where id=?",(native,)); c.commit()
        generation = getattr(self.case, 'generation', 0)
        if self.driver.current_view == 'lifecycle_settings' and action == 'click':
            phase = json.loads((self.case.run / 'ordinary-phase.json').read_text(encoding='utf8'))['phase']
            (Path(self.case.spec['profile']['path']) / 'window-behavior-settings.json').write_text(json.dumps(
                {'windowsCloseBehavior': 'tray' if phase == 'restore_window_behavior' else 'quit'}))
        return {"pid":123+generation,"created":"fixture"+str(generation),"hwnd":456+generation,"width":800,"height":600,"left":0,"top":0,
                "controls":[{"name":"fixture product view"}]}


@unittest.skipUnless(os.name == "nt", "real Windows IO, isolated product/service fixtures")
class CindyControllerTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve(); self.run=self.root/'run';self.run.mkdir()
        self.spec,self.draft=make_fixture(self.root)
        self.database=Path(self.spec['profile']['path'])/'cindy-owner.db'
        create_cindy_database(self.database,[{'id':'task-root','sdk_session_id':'selected','agent_kind':'codex','status':'active','title':'Root'},
                                             {'id':'task-long','sdk_session_id':'selected','agent_kind':'codex','status':'active','title':'Long'}])
        with closing(sqlite3.connect(self.database)) as c:
            c.execute('alter table sessions add column title text')
            c.execute("update sessions set title=case id when 'task-root' then 'Root' else 'Long' end")
            c.execute('create table schedules(id text primary key,name text,updated_at int)');c.commit()
        controller=self.spec['controller'];controller.update(task='cindy-legacy-isolation-20261115',cindy_database=str(self.database),
            native_binary=str(self.root/'fixture-cmd.exe'),source_root_ids=['selected'],side_native_ids=['selected'],
            supervisor_max_seconds=60,account_identity_sha256='fixture-account',test_workdir=str(self.root),
            representatives={'root':{'task_id':'task-root','native_id':'selected'},'long_history':{'task_id':'task-long','native_id':'selected'}})
        controller['allowed_test_homes']={'default':self.spec['target']}
        controller['protected_snapshot']=protected_snapshot(self.spec)
        self.spec.pop('plan_sha256');self.spec['plan_sha256']=fingerprint(self.spec)
        p=patch('local_agent_manager.cindy_isolation_windows.no_cindy');p.start();self.addCleanup(p.stop)
        p=patch('local_agent_manager.cindy_isolation_windows.auth_protocol_census', return_value=[]);p.start();self.addCleanup(p.stop)
        self.key=secrets.token_bytes(32);self.nonce='fixture-nonce'

    def source_server(self,**kwargs):
        self.assertEqual(Path(kwargs['codex_home']),Path(self.spec['source']['path']))
        case=self
        class Server:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def delete_thread(self,ident):
                with closing(sqlite3.connect(Path(case.spec['source']['path'])/'state_5.sqlite')) as c:
                    c.execute('delete from threads where id=?',(ident,));c.commit()
                (Path(case.spec['source']['path'])/'sessions/rollout-selected.jsonl').unlink()
        return Server()

    def cleanup(self):
        target=Path(self.spec['target']['path'])
        adapters=(NativeIntegrityAdapter(codex_home=Path(self.spec['source']['path'])),CindyAdapter(database=self.database,codex_home=target,cindy_root=target.parent))
        return cleanup_source(self.spec,self.run,coordinator=OperationCoordinator(CleanupService(client_inspector=lambda *_:())),
            adapters=adapters,app_server_factory=self.source_server,binary_resolver=lambda _:self.root/'fixture-codex')

    def make_acceptance(self):
        ui=FixtureUI(self)
        def launch(spec,*args):
            if args and args[0]=='--open-folder':
                with closing(sqlite3.connect(self.database)) as c:
                    c.execute("insert into sessions(id,sdk_session_id,agent_kind,status,title) values('task-new',NULL,'codex','active','New')");c.commit()
        driver=ProductAcceptance(self.spec,self.run,ui=ui,launch=launch);ui.driver=driver
        def action(kind,**kw):return driver.command({'action':kind,'frame':driver.current_frame,**kw})
        for role,title in (('root','Root'),('long_history','Long'),('new_default_task','New')):
            action('open',role=role)
            action('confirm_task',observed_title=title,**({'task_id':'task-new'} if role=='new_default_task' else {}))
            action('focus_composer',x=200,y=400);action('type_probe');action('submit_probe',x=700,y=500)
            if role=='long_history':
                action('scroll',x=200,y=200,delta=120);action('scroll',x=200,y=200,delta=120)
            action('attest')
        for role in ('side_history','automations'):
            action('open',role=role);action('click',x=200,y=200)
            action('attest',**({'observed_native_id':'selected'} if role=='side_history' else {}))
        proof=verify_runtime_switch(self.spec)
        driver.capture()
        before={**proof,'window_identity':{'pid':122,'created':'old-fixture','hwnd':455}}
        after={**proof,'window_identity':{'pid':123,'created':'fixture0','hwnd':456}}
        driver.record_cold_start({'identity_sha256':'fixture-account','authenticated':True},before,after)
        return driver.acceptance_packet(self.key,self.nonce,{'identity_sha256':'fixture-account','authenticated':True})

    def test_end_to_end_fixed_supervisor_real_windows_import_ui_protocol_and_formal_source_cleanup(self):
        normal=FixedChannel(self.run,self.spec['plan_sha256'],self.nonce,self.key)
        elevated=FixedChannel(self.run,self.spec['plan_sha256'],self.nonce,self.key)
        def offline():
            return admitted_import(self.spec,['selected'],import_entries(self.spec,writer=process_evidence()),
                boundary_factory=lambda:WindowsImportBoundary(self.spec,self.draft))
        errors=[]
        def diagnose(callback):
            def call(*args):
                try:return callback(*args)
                except Exception:
                    import traceback
                    errors.append(traceback.format_exc());raise
            return call
        supervisor=FixedSupervisor(self.spec,self.run,elevated,offline=offline,
            verify_accepted=diagnose(lambda envelope:verify_accepted_runtime(self.spec,self.run,envelope,self.key,self.nonce)),cleanup=diagnose(self.cleanup))
        outcomes=[]; thread=threading.Thread(target=lambda:outcomes.append(supervisor.execute()));thread.start()
        def stop():
            if thread.is_alive(): normal.send_request('cancel',{})
            thread.join(5)
        self.addCleanup(stop)
        def wait(number):
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                reply=normal.receive_reply(number)
                if reply:return reply
                time.sleep(.05)
            self.fail('fixture supervisor reply missing')
        self.assertEqual(wait(0)['kind'],'ready')
        normal.send_request('offline',{'lifecycle_receipt_sha256':'fixture-gate-receipt'})
        self.assertEqual(wait(1)['kind'],'offline_verified')
        packet=self.make_acceptance();write_new_json(self.run/'acceptance.json',packet)
        target_before=Path(self.spec['native_files'][0]['target_path']).read_bytes()
        cindy_before=self.database.read_bytes()
        from local_agent_manager.cindy_isolation_recovery import digest
        normal.send_request('acceptance',{'acceptance_sha256':digest(self.run/'acceptance.json')})
        reply=wait(2)
        if reply['kind']!='complete':
            for name in ('source-cleanup-apply.json','source-cleanup-status.json','source-cleanup-verify.json'):
                if (self.run/name).exists():errors.append(name+': '+(self.run/name).read_text(encoding='utf8'))
        self.assertEqual(reply['kind'],'complete',str(reply)+'\n'+'\n'.join(errors))
        thread.join(5);self.assertEqual(outcomes,[0])
        self.assertFalse((Path(self.spec['source']['path'])/'sessions/rollout-selected.jsonl').exists())
        self.assertEqual(Path(self.spec['native_files'][0]['target_path']).read_bytes(),target_before)
        self.assertEqual(self.database.read_bytes(),cindy_before)
        for name in ('source-cleanup-plan.json','source-cleanup-apply.json','source-cleanup-status.json','source-cleanup-verify.json','supervisor-exit.json'):
            self.assertTrue((self.run/name).exists(),name)

    def test_handshake_replay_other_run_and_arbitrary_command_are_rejected(self):
        channel=FixedChannel(self.run,self.spec['plan_sha256'],self.nonce,self.key)
        write_new_json(self.run/'request-000001.json',signed({'spec_sha256':self.spec['plan_sha256'],'nonce':'other-run','role':'ordinary','sequence':1,'kind':'offline','payload':{}},self.key))
        with self.assertRaises(ValueError):channel.receive_request(1)
        value=signed({'anything':1},self.key);value['body']['anything']=2
        with self.assertRaises(ValueError):authenticated(value,self.key)

    def test_protocol_packet_is_invisible_until_complete_and_refuses_overwrite(self):
        from local_agent_manager import cindy_isolation_controller as controller
        staged, release = threading.Event(), threading.Event()
        original = controller._write_json
        def pause(path, value):
            original(path, value); staged.set(); self.assertTrue(release.wait(5))
        channel = FixedChannel(self.run,self.spec['plan_sha256'],self.nonce,self.key)
        with patch.object(controller, '_write_json', pause):
            worker = threading.Thread(target=lambda: channel.send_request('cancel', {})); worker.start()
            self.assertTrue(staged.wait(5)); self.assertIsNone(channel.receive_request(1))
            release.set(); worker.join(5)
        self.assertEqual(channel.receive_request(1)['kind'], 'cancel')
        before = (self.run/'request-000001.json').read_bytes()
        with self.assertRaises(OSError): write_new_json(self.run/'request-000001.json', {'different':True})
        self.assertEqual((self.run/'request-000001.json').read_bytes(), before)

    def test_deadline_and_parent_exit_never_call_cleanup(self):
        called=[]
        clock=[0]
        channel=FixedChannel(self.run,self.spec['plan_sha256'],self.nonce,self.key)
        supervisor=FixedSupervisor(self.spec,self.run,channel,offline=lambda:called.append('import'),
            verify_accepted=lambda e:True,cleanup=lambda:called.append('delete'),clock=lambda:clock[0],wait=lambda _:clock.__setitem__(0,100))
        self.assertEqual(supervisor.execute(),1);self.assertEqual(called,[])
        self.assertTrue((self.run/'supervisor-exit.json').exists())

    def test_existing_second_home_accepts_append_but_retains_original_prefix(self):
        from local_agent_manager.cindy_isolation_plan import directory_identity
        home = self.root/'second'; home.mkdir(); path = home/'synthetic.jsonl'; path.write_bytes(b'original history\n')
        with closing(sqlite3.connect(home/'state_5.sqlite')) as c:
            c.execute('create table threads(id text primary key,rollout_path text)')
            c.execute('insert into threads values(?,?)', ('second-original',str(path))); c.commit()
        self.spec['controller']['protected_second_home'] = directory_identity(home)
        self.spec['controller']['protected_snapshot'] = protected_snapshot(self.spec)
        path.write_bytes(path.read_bytes()+b'new T3 turn\n')
        self.assertTrue(verify_protected_state(self.spec)['second_account_preserved'])
        path.write_bytes(b'changed history\nnew T3 turn\n')
        with self.assertRaises(ValueError): verify_protected_state(self.spec)

    def test_ordinary_execute_real_child_supervisor_fixed_ui_and_final_restore(self):
        from local_agent_manager import cindy_isolation_entry as entry
        from local_agent_manager.cindy_isolation_ui import window_behavior
        from local_agent_manager.cindy_isolation_recovery import digest
        bundle = self.root / 'bundle'; bundle.mkdir()
        preference = Path(self.spec['profile']['path']) / 'window-behavior-settings.json'
        preference.write_text('{"windowsCloseBehavior":"tray"}')
        self.spec['controller'].update(window_behavior_before=window_behavior(self.spec),
            executable=str(self.root/'fixture-cmd.exe'), executable_sha256=digest(self.root/'fixture-cmd.exe'))
        self.spec.pop('plan_sha256'); self.spec['plan_sha256'] = fingerprint(self.spec)
        (bundle/'spec.json').write_text(json.dumps(self.spec), encoding='utf8')
        (bundle/'draft.json').write_text(json.dumps(self.draft), encoding='utf8')
        self.generation = 0
        child, errors, gates = [], [], []
        class Lifecycle:
            def observe(_):
                from types import SimpleNamespace
                return SimpleNamespace(targets=('synthetic-cindy',))
            def close(_):
                gates.append('fixture fresh close consent')
                return {'gate_displayed': True, 'fixture': True}
            def restart(_, **kwargs):
                gates.append('fixture fresh restart consent'); self.generation += 1
        def start(entry_path, manifest, run_name):
            process = subprocess.Popen([sys.executable, '-m', 'tests.cindy_isolation_supervisor_fixture', str(bundle), run_name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            child.append(process); return process.pid
        def launch(spec, *args):
            if args and args[0] == '--open-folder':
                with closing(sqlite3.connect(self.database)) as c:
                    c.execute("insert into sessions(id,sdk_session_id,agent_kind,status,title) values('task-new',NULL,'codex','active','New')"); c.commit()
        def driver(spec, run):
            self.run = run; ui = FixtureUI(self)
            result = ProductAcceptance(spec, run, ui=ui, launch=launch); ui.driver = result
            return result
        def actor():
            try:
                deadline = time.monotonic() + 60
                while not list((bundle/'runs').glob('run-*')):
                    if time.monotonic() > deadline: raise AssertionError('ordinary run missing')
                    time.sleep(.02)
                run = next((bundle/'runs').glob('run-*'))
                while not (run/'ordinary-launch.json').exists(): time.sleep(.02)
                key = (run/'session-key.bin').read_bytes()
                nonce = authenticated(json.loads((run/'ordinary-launch.json').read_text(encoding='utf8')), key)['nonce']
                def send(action, **options):
                    state = json.loads((run/'ordinary-ui-state.json').read_text(encoding='utf8'))
                    token = secrets.token_hex(12); name = 'ui-request-' + token + '.json'
                    write_new_json(run/name, signed({'nonce': nonce, 'request': {'action': action, 'frame': state['current_frame'], **options}}, key))
                    response = run/('ui-response-' + token + '.json')
                    while not response.exists():
                        if time.monotonic() > deadline: raise AssertionError('ordinary UI request timed out: ' + action)
                        time.sleep(.02)
                    value = authenticated(json.loads(response.read_text(encoding='utf8')), key)
                    if not value['ok']: raise AssertionError(str(value))
                    return value
                seen = set()
                while True:
                    path = run/'ordinary-phase.json'
                    phase = json.loads(path.read_text(encoding='utf8'))['phase'] if path.exists() else None
                    if phase in ('prepare_wm_close_quit', 'restore_window_behavior') and phase not in seen:
                        seen.add(phase); send('click', x=100, y=100); send('settings_ready')
                        if phase == 'restore_window_behavior': return
                    elif phase == 'ordinary_product_acceptance' and phase not in seen:
                        seen.add(phase)
                        def probe(role, title):
                            send('open', role=role)
                            send('confirm_task', observed_title=title, **({'task_id':'task-new'} if role=='new_default_task' else {}))
                            send('focus_composer', x=100, y=400); send('type_probe'); send('submit_probe', x=700, y=500)
                            if role == 'long_history':
                                send('scroll', x=200, y=200, delta=120); send('scroll', x=200, y=200, delta=120)
                            send('attest')
                        probe('root', 'Root'); probe('long_history', 'Long'); probe('new_default_task', 'New')
                        for role in ('side_history','automations'):
                            send('open', role=role); send('click', x=100, y=200)
                            send('attest', **({'observed_native_id':'selected'} if role=='side_history' else {}))
                        # Source is still retained until the final six-role packet.
                        assert (Path(self.spec['source']['path'])/'sessions/rollout-selected.jsonl').exists()
                        send('cold_restart'); probe('root','Root'); send('finish')
                    if time.monotonic() > deadline: raise AssertionError('ordinary fixture phase timeout')
                    time.sleep(.02)
            except BaseException:
                import traceback
                errors.append(traceback.format_exc())
        worker = threading.Thread(target=actor, daemon=True); worker.start()
        with patch.object(entry, '_launch_elevated', start), patch.object(entry, '_lifecycle', lambda *_: Lifecycle()), \
             patch.object(entry, 'ProductAcceptance', driver), patch.object(entry, 'supported_account', return_value={'authenticated':True,'identity_sha256':'fixture-account'}), \
             patch.object(entry, 'verify_bundle', return_value=(self.spec, self.draft)), redirect_stdout(io.StringIO()):
            result = entry.main(bundle, 'fixture-manifest', ['execute', '--uac-authorized'])
        worker.join(5)
        self.assertFalse(errors, '\n'.join(errors)); self.assertEqual(result, 0)
        stdout, stderr = child[0].communicate(timeout=10)
        self.assertEqual(child[0].returncode, 0, stderr.decode(errors='replace'))
        self.assertEqual(len(gates), 6)
        self.assertEqual(window_behavior(self.spec), self.spec['controller']['window_behavior_before'])
        self.assertTrue((self.run/'ordinary-completion.json').exists())
        self.assertTrue((self.run/'controller-completion.json').exists())
        self.assertFalse((Path(self.spec['source']['path'])/'sessions/rollout-selected.jsonl').exists())
        self.assertTrue(Path(self.spec['native_files'][0]['target_path']).exists())
        with patch.object(entry, 'verify_bundle', return_value=(self.spec, self.draft)), redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(bundle, 'fixture-manifest', ['verify', '--run', self.run.name]), 0)


if __name__=='__main__':unittest.main()
