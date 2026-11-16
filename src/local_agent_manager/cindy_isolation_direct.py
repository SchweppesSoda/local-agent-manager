"""Owner-requested direct data acceptance; no product UI or synthetic UI attest."""
import json
import os
from pathlib import Path
import secrets
import time

from .cindy_isolation_controller import (authenticated, cleanup_source, fail, product_fence,
    signed, supported_account, verify_protected_state, write_new_json)
from .cindy_isolation_execution import admitted_import
from .cindy_isolation_plan import legacy_destination, readonly_sqlite
from .cindy_isolation_resume import existing_entries, import_entries, resolve_import, execute_remaining, latest_import_spec
from .cindy_isolation_windows import (WindowsImportBoundary, no_cindy, process_evidence,
    verify_retained_target, verify_runtime_switch)


def verify_direct(spec, *, server_factory=None, account_reader=supported_account):
    from .codex_app_server import CodexAppServer
    from .cindy_isolation_ui import clean_product_environment
    verify_retained_target(spec); verify_runtime_switch(spec); protected=verify_protected_state(spec)
    account=account_reader(spec)
    if account['identity_sha256']!=spec['controller']['account_identity_sha256']: fail('direct_existing_account_changed')
    registry=json.loads(Path(spec['registry_path']).read_text(encoding='utf-8-sig'))
    route=legacy_destination(registry,profile_root=Path(spec['profile']['path']),home=Path(spec['target']['path']),official_home=Path(spec['source']['path']))
    factory=server_factory or CodexAppServer
    reads=[];history={}
    with factory(codex_home=Path(spec['target']['path']),codex_binary=Path(spec['controller']['native_binary']),
                 environment=clean_product_environment(),timeout=30) as server:
        for item in spec['native_files']:
            ident=item['native_id']
            thread=server.request('thread/read',{'threadId':ident,'includeTurns':False})['thread']
            if thread.get('id')!=ident: fail('direct_native_read_wrong_id')
            reads.append(ident)
        for role,rep in spec['controller']['representatives'].items():
            thread=server.request('thread/read',{'threadId':rep['native_id'],'includeTurns':True})['thread']
            if thread.get('id')!=rep['native_id']: fail('direct_history_wrong_id')
            turns=thread.get('turns',[])
            if rep.get('frozen_turn_count',0)>0 and not turns: fail('direct_long_history_not_returned')
            history[role]={'task_id':rep['task_id'],'native_id':rep['native_id'],'native_turns_returned':len(turns),
                           'frozen_turn_rows':rep.get('frozen_turn_count'),'includeTurns':True}
    verify_retained_target(spec);verify_runtime_switch(spec);verify_protected_state(spec)
    return {'mode':'direct-native-data','native_ids_read':reads,'history':history,'account_identity_sha256':account['identity_sha256'],
            'resolved_legacy_home':route['home'],'protected':protected,'ui_performed':False,'cold_ui_performed':False,
            'new_chat_probe_sent':False,'native_validation':'thread/read for every ID; full history for original representative roots',
            'pagination_storage':'complete original thread_history rows and projection state retained; rendered paging untested',
            'product_route_basis':'reviewed installed package hash, persistent legacyCodexDefault, actual binding/location resolver',
            'goal_satisfied':True}


def elevated_direct(bundle,spec,draft,manifest_hash,run_name):
    from .cindy_isolation_entry import _load_run,_peer_alive,ctypes_is_admin
    from .mutation_guard import mutation_roots
    if not ctypes_is_admin():fail('direct_supervisor_requires_actual_elevation')
    run,key,launch,_=_load_run(bundle,spec,run_name)
    if launch['manifest_sha256']!=manifest_hash or not _peer_alive(launch['writer']): fail('direct_ordinary_peer_unproven')
    stamp=process_evidence();write_new_json(run/'supervisor-identity.json',signed(stamp,key))
    phase='offline_import'
    try:
        active=latest_import_spec(bundle,spec)
        if any(store.directory.exists() for store,_ in existing_entries(active)):
            audit=resolve_import(active,draft,boundary_factory=lambda:WindowsImportBoundary(active,draft),evidence_directory=run)
            active,result=execute_remaining(active,draft,audit,boundary_type=WindowsImportBoundary,evidence_directory=run,writer=stamp)
        else:
            result=admitted_import(spec,[f['native_id'] for f in spec['native_files']],import_entries(spec,writer=stamp),
                                   boundary_factory=lambda:WindowsImportBoundary(spec,draft))
        write_new_json(run/'direct-import-result.json',result)
        phase='direct_native_validation'
        with product_fence(spec): proof=verify_direct(spec)
        write_new_json(run/'direct-acceptance.json',proof)
        if not _peer_alive(launch['writer']):fail('direct_ordinary_peer_exited_keep_source')
        phase='formal_source_cleanup'
        with mutation_roots(Path(spec[k]['path']) for k in ('profile','source','target')), WindowsImportBoundary(spec,draft) as boundary:
            verify_protected_state(spec)
            cleaned=cleanup_source(spec,run,before_apply=lambda _:boundary.release_source())
        verify_retained_target(spec);verify_runtime_switch(spec);verify_protected_state(spec)
        write_new_json(run/'direct-result.json',signed({**cleaned,'mode':'direct-native-data','spec_sha256':spec['plan_sha256'],
            'nonce':launch['nonce'],'migrated_native':len(spec['native_files']),'migrated_roots':len(spec['controller']['source_root_ids']),
            'binding_changes':len(spec['binding_changes']),'location_changes':len(spec['location_changes']),
            'native_read_count':len(proof['native_ids_read']),'product_ui_performed':False,'source_deleted':True},key))
        return 0
    except BaseException as error:
        write_new_json(run/'direct-result.json',signed({'mode':'direct-native-data','phase':phase,'goal_satisfied':False,
            'error_type':type(error).__name__,'policy_reason':str(error) if isinstance(error,ValueError) else 'inspect_original_journals_no_replay',
            'source_cleanup_may_have_started':(run/'source-cleanup-plan.json').exists(),'spec_sha256':spec['plan_sha256'],
            'nonce':launch['nonce'],'replay_permitted':False},key))
        return 1
    finally:write_new_json(run/'supervisor-exit.json',{'pid':os.getpid(),'phase':phase,'bounded_exit':True})


def ordinary_direct(bundle,spec,draft,manifest_hash,*,uac_authorized):
    from .cindy_isolation_entry import _run_path,private_run_directory,_launch_elevated,ctypes_is_admin
    from .codex_desktop_state import running_related_clients
    if not uac_authorized:fail('explicit_new_uac_authorization_required')
    if ctypes_is_admin():fail('direct_controller_requires_ordinary_user')
    no_cindy()
    if running_related_clients(Path(spec['source']['path']),owner_client='native'):fail('official_source_client_still_running_before_uac')
    run_name='run-'+secrets.token_hex(12);run=_run_path(bundle,run_name);private_run_directory(run)
    key=secrets.token_bytes(32);nonce=secrets.token_hex(24)
    with (run/'session-key.bin').open('xb') as f:f.write(key)
    write_new_json(run/'ordinary-launch.json',signed({'spec_sha256':spec['plan_sha256'],'manifest_sha256':manifest_hash,
        'run':run_name,'nonce':nonce,'writer':process_evidence(),'acceptance_mode':'direct-native-data'},key))
    print(json.dumps({'phase':'requesting_one_uac','run':run_name,'product_ui':False}),flush=True)
    try:pid=_launch_elevated(Path(bundle)/'entry.py',manifest_hash,run_name,role='internal-direct')
    except OSError as error:
        write_new_json(run/'uac-declined-or-failed.json',{'winerror':getattr(error,'winerror',None),'attempts_this_run':1});return 3
    write_new_json(run/'uac-launch.json',{'pid':pid,'attempts_this_run':1})
    deadline=time.monotonic()+spec['controller']['supervisor_max_seconds']
    while time.monotonic()<deadline:
        if (run/'direct-result.json').exists():
            result=authenticated(json.loads((run/'direct-result.json').read_text(encoding='utf8')),key)
            print(json.dumps({'run':run_name,**result},ensure_ascii=True),flush=True)
            return 0 if result.get('goal_satisfied') else 3
        if (run/'supervisor-exit.json').exists():fail('direct_supervisor_exited_without_result_no_replay')
        time.sleep(.2)
    fail('direct_controller_deadline_keep_evidence_no_replay')
