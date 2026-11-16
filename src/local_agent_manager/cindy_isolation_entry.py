"""Executable, task-bundle-bound ordinary/elevated endpoints. No free paths."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from .cindy_isolation_controller import (TASK, FixedChannel, FixedSupervisor, authenticated, cleanup_source,
    fail, product_fence, signed, supported_account,
    verify_accepted_runtime, verify_bundle, verify_protected_state, write_new_json)
from .cindy_isolation_controller import validate_source_plan
from .cindy_isolation_execution import admitted_import, journal_projection
from .cindy_isolation_lifecycle import CindyLifecycle, windows_identity
from .cindy_isolation_resume import (existing_entries, import_entries, resolve_import, execute_remaining, latest_import_spec)
from .cindy_isolation_windows import (WindowsImportBoundary, process_evidence, verify_retained_target, verify_runtime_switch)
from .cindy_isolation_ui import ProductAcceptance, ctypes_is_admin, launch_cindy, window_behavior
from .cindy_isolation_plan import readonly_sqlite
from .cindy_isolation_recovery import digest
from .operation_store import atomic_write_json, utc_now


def _run_path(bundle, name):
    if not re.fullmatch(r"run-[0-9a-f]{24}", name): fail("fixed_run_name_required")
    path = Path(bundle) / "runs" / name
    if path.is_symlink(): fail("run_namespace_link")
    return path


def private_run_directory(path):
    """Only a new task run dir, never old stores/locks or their ACLs."""
    from ctypes import wintypes
    from . import frozen_files as ff
    from .cindy_isolation_windows import current_user_sid
    path.parent.mkdir(parents=True, exist_ok=True)
    ff._plain(path.parent.lstat(), directory=True)
    class SA(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    sid = current_user_sid()
    sddl = 'O:' + sid + 'G:' + sid + 'D:P(A;OICI;FA;;;' + sid + ')(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SA)); kernel.CreateDirectoryW.restype = wintypes.BOOL
        sa = SA(ctypes.sizeof(SA), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(sa)): raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree.argtypes = (ctypes.c_void_p,); kernel.LocalFree(descriptor)


def _launch_elevated(entry, manifest_hash, run_name, *, role='internal-supervisor'):
    """One reviewed Python entry, one fixed role, exact hash/run name only."""
    from ctypes import wintypes
    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = (ctypes.POINTER(SHELLEXECUTEINFO),); shell.ShellExecuteExW.restype = wintypes.BOOL
    info = SHELLEXECUTEINFO(); info.cbSize = ctypes.sizeof(info); info.fMask = 0x40 | 0x400
    info.lpVerb, info.lpFile = "runas", sys.executable
    if role not in {'internal-supervisor', 'internal-direct'}: fail('fixed_elevated_role_required')
    info.lpParameters = subprocess.list2cmdline(["-I", str(entry), role, "--manifest-sha256", manifest_hash, "--run", run_name])
    info.lpDirectory, info.nShow = str(Path(entry).parent), 0
    if not shell.ShellExecuteExW(ctypes.byref(info)): raise ctypes.WinError(ctypes.get_last_error())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessId.argtypes = (wintypes.HANDLE,); kernel.GetProcessId.restype = wintypes.DWORD
    pid = kernel.GetProcessId(info.hProcess)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,); kernel.CloseHandle(info.hProcess)
    return pid


def _peer_alive(stamp):
    current = windows_identity(stamp["pid"])
    return current is not None and current[0] == stamp["executable"] and current[1] == stamp["started_unix"]


def _load_run(bundle, spec, name):
    run = _run_path(bundle, name)
    key = (run / "session-key.bin").read_bytes()
    if len(key) != 32: fail("session_key_invalid")
    launch = authenticated(json.loads((run / "ordinary-launch.json").read_text(encoding="utf8")), key)
    if launch["spec_sha256"] != spec["plan_sha256"] or launch["run"] != name: fail("run_launch_spec_mismatch")
    return run, key, launch, FixedChannel(run, spec["plan_sha256"], launch["nonce"], key)


def _lifecycle(spec, run_name, suffix):
    with readonly_sqlite(Path(spec["controller"]["cindy_database"])) as c:
        ids = [r[0] for r in c.execute("select id from sessions where status is null or status!='deleted'")]
    return CindyLifecycle(run_id=run_name + suffix, executable=spec["controller"]["executable"],
                          profile=spec["profile"]["path"], affected_tasks=ids)


def _wait_reply(channel, sequence, deadline, peer=None):
    while time.monotonic() < deadline:
        reply = channel.receive_reply(sequence)
        if reply: return reply
        if peer is not None and not _peer_alive(peer): fail("supervisor_exited_before_reply")
        time.sleep(0.1)
    fail("fixed_protocol_reply_timeout")


def _restart(spec, cycle):
    return cycle.restart(expected_executable_sha256=spec["controller"]["executable_sha256"],
                         launch=lambda: launch_cindy(spec))


def _ordinary_settings_phase(spec, run, driver, key, nonce, handled, deadline, *, restore=False):
    """Real product settings UI, never a raw file/IPC write or forced exit.

    This machine uses close-to-tray. WM_CLOSE needs the product's own temporary
    Quit setting. All other preferences must remain equal; restore at the end.
    """
    expected = dict(spec["controller"]["window_behavior_before"])
    if not restore: expected["windowsCloseBehavior"] = "quit"
    if window_behavior(spec) == expected:
        if restore:
            atomic_write_json(run / 'window-behavior-pending-restore.json', {'original': expected, 'restored': True})
        return
    if restore:
        cycle = _lifecycle(spec, run.name, '-restore-settings')
        if not cycle.observe().targets:
            _restart(spec, cycle)  # A failed cleanup may have left Cindy closed.
    driver.current_view, driver.current_task = "lifecycle_settings", None
    driver.launch(spec, "cindy://settings/providers")
    for attempt in range(30):
        try: driver.capture(); break
        except Exception:
            if attempt == 29: raise
            time.sleep(0.5)
    phase = "restore_window_behavior" if restore else "prepare_wm_close_quit"
    atomic_write_json(run / "ordinary-phase.json", {"phase": phase, "expected_windowsCloseBehavior": expected["windowsCloseBehavior"]})
    atomic_write_json(run / "window-behavior-pending-restore.json", {"original": spec["controller"]["window_behavior_before"], "restored": False})
    print(json.dumps({"phase": phase, "run": run.name, "frame_path": driver.state()["frame_path"],
        "next": "navigate the ordinary product settings with observe/click/scroll; choose the fixed close behavior; use settings_ready after reviewing the resulting frame",
        "windowsCloseBehavior": expected["windowsCloseBehavior"]}), flush=True)
    while time.monotonic() < deadline:
        for request_path in sorted(run.glob("ui-request-*.json")):
            if request_path.name in handled: continue
            handled.add(request_path.name)
            response = run / request_path.name.replace("ui-request-", "ui-response-")
            try:
                value = authenticated(json.loads(request_path.read_text(encoding="utf8")), key)
                if value.get("nonce") != nonce: fail("ordinary_settings_wrong_run")
                request = value["request"]
                if request["action"] == "settings_ready":
                    driver._fresh(request.get("frame"))
                    if window_behavior(spec) != expected: fail("product_close_preference_not_expected_or_other_setting_changed")
                    write_new_json(response, signed({"ok": True, "phase": phase, "product_setting_verified": True}, key))
                    if restore:
                        atomic_write_json(run / "window-behavior-pending-restore.json", {"original": expected, "restored": True})
                    return
                if request["action"] not in {"observe", "click", "scroll"}: fail("only_ordinary_settings_navigation_before_offline")
                output = driver.command(request)
                write_new_json(response, signed({"ok": True, "result": output}, key))
            except Exception as error:
                write_new_json(response, signed({"ok": False, "error_type": type(error).__name__,
                    "policy_reason": str(error) if isinstance(error, ValueError) else "settings_navigation_unverified"}, key))
        time.sleep(.1)
    fail("ordinary_settings_phase_timed_out")


def elevated(bundle, spec, draft, manifest_hash, run_name):
    if not ctypes_is_admin(): fail("supervisor_requires_actual_elevation")
    run, key, launch, channel = _load_run(bundle, spec, run_name)
    if launch["manifest_sha256"] != manifest_hash: fail("supervisor_manifest_not_ordinary_review")
    if not _peer_alive(launch["writer"]): fail("ordinary_peer_not_current")
    stamp = process_evidence()
    write_new_json(run / "supervisor-identity.json", signed(stamp, key))
    native_recovered = {}

    def verify_prior_native_cleanup():
        from .operation_coordinator import OperationCoordinator
        from . import frozen_files as ff
        for prior in sorted((Path(bundle) / "runs").glob("run-*/source-cleanup-plan.json")):
            if prior.parent == run: continue
            ff._plain(prior.lstat())
            plan = json.loads(prior.read_text(encoding="utf8")); validate_source_plan(plan, spec)
            coordinator = OperationCoordinator()
            status = coordinator.status_operation(plan_path=prior, codex_home=Path(spec["source"]["path"]))
            verified = coordinator.verify_operation(plan_path=prior, codex_home=Path(spec["source"]["path"]))
            write_new_json(run / ("prior-native-" + plan["operation_id"] + "-status.json"), status)
            write_new_json(run / ("prior-native-" + plan["operation_id"] + "-verify.json"), verified)
            if verified.get("goal_status") != "complete" or verified.get("goal_satisfied") is not True:
                fail("prior_native_cleanup_unresolved_formal_verify_required_no_replay")
            native_recovered.update(operation_id=plan["operation_id"], plan_sha256=plan["plan_sha256"],
                                    goal_status="complete", goal_satisfied=True, source_only=True,
                                    basis="formal_status_verify_of_original_native_plan_no_apply_replay")
        return bool(native_recovered)

    def offline():
        with product_fence(spec):
            if verify_prior_native_cleanup():
                verify_retained_target(spec); verify_runtime_switch(spec); verify_protected_state(spec)
                result = {"offline_import_verified": True, "source_deleted": True,
                          "source_already_verified_clean": True, "goal_satisfied": False}
                write_new_json(run / "offline-result.json", result)
                return result
        active = latest_import_spec(bundle, spec)
        entries = existing_entries(active)
        if any(store.directory.exists() for store, _ in entries):
            audit = resolve_import(active, draft, boundary_factory=lambda: WindowsImportBoundary(active, draft), evidence_directory=run)
            current_spec, result = execute_remaining(active, draft, audit, boundary_type=WindowsImportBoundary,
                                                    evidence_directory=run, writer=stamp)
            write_new_json(run / "active-import-spec.json", current_spec)
        else:
            result = admitted_import(spec, [f["native_id"] for f in spec["native_files"]], import_entries(spec, writer=stamp),
                boundary_factory=lambda: WindowsImportBoundary(spec, draft))
        write_new_json(run / "offline-result.json", result)
        return {"offline_import_verified": result["offline_import_verified"], "source_deleted": False,
                "goal_satisfied": False, "supervisor_waiting_for_ordinary_acceptance": True}

    def accept(envelope):
        # The ordinary controller has closed Cindy after the final cold-start
        # proof. Keep the real product writer out while rechecking evidence.
        with product_fence(spec):
            return verify_accepted_runtime(spec, run, envelope, key, launch["nonce"])

    def cleanup():
        from .mutation_guard import mutation_roots
        def publish(result):
            names = ['source-cleanup-apply.json', 'source-cleanup-status.json', 'source-cleanup-verify.json']
            names += [p.name for p in run.glob('prior-native-*-status.json')]
            names += [p.name for p in run.glob('prior-native-*-verify.json')]
            evidence = {name: digest(run / name) for name in names if (run / name).exists()}
            if not evidence: fail('formal_native_completion_evidence_missing')
            write_new_json(run / 'controller-completion.json', signed({**result, 'spec_sha256': spec['plan_sha256'],
                'nonce': launch['nonce'], 'accepted_evidence_sha256': digest(run / 'acceptance.json'),
                'native_evidence': evidence}, key))
        if native_recovered:
            with mutation_roots(Path(spec[k]["path"]) for k in ("profile", "source", "target")), product_fence(spec):
                verify_retained_target(spec); verify_runtime_switch(spec); verify_protected_state(spec)
                result = dict(native_recovered)
                publish(result)
                return result
        with mutation_roots(Path(spec[k]["path"]) for k in ("profile", "source", "target")), WindowsImportBoundary(spec, draft) as boundary:
            verify_protected_state(spec)
            # Source may have evolved while the normal user tested Cindy. Its
            # complete selected rows/closure/bytes must still match the copied
            # packet under leases, both before planning and before dispatch.
            result = cleanup_source(spec, run, before_apply=lambda _: boundary.release_source())
            publish(result)
            return result
    supervisor = FixedSupervisor(spec, run, channel, offline=offline, verify_accepted=accept,
                                 cleanup=cleanup, peer_alive=lambda: _peer_alive(launch["writer"]))
    return supervisor.execute()


def ordinary_execute(bundle, spec, draft, manifest_hash, *, uac_authorized):
    if not uac_authorized: fail("explicit_new_uac_authorization_required")
    if ctypes_is_admin(): fail("execute_must_start_as_ordinary_user")
    run_name = "run-" + secrets.token_hex(12); run = _run_path(bundle, run_name)
    private_run_directory(run)
    key, nonce = secrets.token_bytes(32), secrets.token_hex(24)
    with (run / "session-key.bin").open("xb") as stream: stream.write(key)
    stamp = process_evidence()
    write_new_json(run / "ordinary-launch.json", signed({"spec_sha256": spec["plan_sha256"], "nonce": nonce,
        "manifest_sha256": manifest_hash, "run": run_name, "writer": stamp, "at": utc_now()}, key))
    driver = ProductAcceptance(spec, run)
    handled = set()
    # Pure ordinary UI preparation happens before UAC. It sends no chat and
    # closes nothing. A pre-existing closed Cindy needs no policy change yet.
    if _lifecycle(spec, run_name, "-observe-only").observe().targets:
        _ordinary_settings_phase(spec, run, driver, key, nonce, handled, time.monotonic() + 900)
    # This is the only UAC call. Cindy remains open if UAC is declined or the
    # reviewed supervisor fails admission/handshake.
    try:
        pid = _launch_elevated(Path(bundle) / "entry.py", manifest_hash, run_name)
    except OSError as error:
        write_new_json(run / "uac-declined-or-failed.json", {"error_code": getattr(error, "winerror", None), "attempts_this_run": 1})
        _ordinary_settings_phase(spec, run, driver, key, nonce, handled, time.monotonic() + 600, restore=True)
        return 3
    write_new_json(run / "uac-launch.json", {"pid": pid, "attempts_this_run": 1})
    channel = FixedChannel(run, spec["plan_sha256"], nonce, key)
    deadline = time.monotonic() + spec["controller"]["supervisor_max_seconds"]
    try:
        ready = _wait_reply(channel, 0, min(deadline, time.monotonic() + 120))
        if ready["kind"] != "ready": fail("supervisor_not_ready")
        supervisor_identity = authenticated(json.loads((run / 'supervisor-identity.json').read_text(encoding='utf8')), key)
        if supervisor_identity['pid'] != pid or not _peer_alive(supervisor_identity): fail('supervisor_identity_not_current')
        cycle = _lifecycle(spec, run_name, "-offline")
        receipt = cycle.close()
    except BaseException:
        # No offline request has been dispatched. Cancel the finite peer and
        # restore through the still-open product UI when closure was declined.
        channel.send_request('cancel', {})
        if _lifecycle(spec, run_name, '-cancel-observe').observe().targets:
            _ordinary_settings_phase(spec, run, driver, key, nonce, handled, time.monotonic() + 600, restore=True)
        raise
    write_new_json(run / "ordinary-offline-lifecycle.json", receipt)
    number = channel.send_request("offline", {"lifecycle_receipt_sha256": digest(run / "ordinary-offline-lifecycle.json")})
    reply = _wait_reply(channel, number, deadline, peer=supervisor_identity)
    if reply["kind"] != "offline_verified": fail("offline_failed_cindy_retained_closed_status_verify_required")
    _restart(spec, cycle)
    _ordinary_settings_phase(spec, run, driver, key, nonce, handled, deadline)
    # Allow first paint without sending any test. Ordinary UI requests each
    # perform one observed action and a fresh capture; no stored coordinates.
    for attempt in range(30):
        try: driver.capture(); break
        except Exception:
            if attempt == 29: raise
            time.sleep(0.5)
    print(json.dumps({"phase": "ordinary_product_acceptance", "run": run_name, "frame_path": driver.state()["frame_path"],
                      "next": "use the ui endpoint with observe/open/confirm_task/focus_composer/type_probe/submit_probe/attest; inspect every returned frame"}), flush=True)
    atomic_write_json(run / "ordinary-phase.json", {"phase": "ordinary_product_acceptance"})
    while time.monotonic() < deadline:
        if (run / "supervisor-exit.json").exists():
            _ordinary_settings_phase(spec, run, driver, key, nonce, handled, time.monotonic() + 600, restore=True)
            fail("supervisor_exited_before_acceptance")
        for request_path in sorted(run.glob("ui-request-*.json")):
            if request_path.name in handled: continue
            handled.add(request_path.name)
            response = run / request_path.name.replace("ui-request-", "ui-response-")
            try:
                request = authenticated(json.loads(request_path.read_text(encoding="utf8")), key)
                if request.get("nonce") != nonce: fail("ordinary_ui_wrong_run")
                action = request["request"]["action"]
                if action == "cold_restart":
                    from .cindy_isolation_controller import ROLES
                    if set(driver.roles) != ROLES - {"cold_start"}: fail("cold_start_requires_all_other_acceptance")
                    before = verify_runtime_switch(spec)
                    before["window_identity"] = {key: driver.frames[driver.current_frame]["window"][key] for key in ("pid", "created", "hwnd")}
                    cold_cycle = _lifecycle(spec, run_name, "-cold")
                    cold_receipt = cold_cycle.close(); write_new_json(run / "ordinary-cold-lifecycle.json", cold_receipt)
                    _restart(spec, cold_cycle)
                    time.sleep(1)
                    after = verify_runtime_switch(spec); account = supported_account(spec)
                    driver.capture()
                    driver.pending_cold = {"before": before, "after": after, "previous_root_nonce": driver.roles["root"]["nonce"]}
                    output = driver.state()
                    output["next"] = "open the original root and run its fixed probe again after this cold restart; attest the resulting reply"
                elif action == "finish":
                    account = supported_account(spec)
                    if set(driver.roles) != set(__import__("local_agent_manager.cindy_isolation_controller", fromlist=["ROLES"]).ROLES): fail("acceptance_not_complete")
                    final_cycle = _lifecycle(spec, run_name, "-source-cleanup")
                    final_receipt = final_cycle.close(); write_new_json(run / "ordinary-final-lifecycle.json", final_receipt)
                    packet = driver.acceptance_packet(key, nonce, account)
                    write_new_json(run / "acceptance.json", packet)
                    number = channel.send_request("acceptance", {"acceptance_sha256": digest(run / "acceptance.json")})
                    result = _wait_reply(channel, number, deadline, peer=supervisor_identity)
                    if result["kind"] != "complete": fail("source_cleanup_not_complete_use_formal_status_verify")
                    _restart(spec, final_cycle)
                    output = result["payload"]
                    write_new_json(response, signed({"ok": True, "result": output,
                        "phase": "source_cleanup_complete_restore_window_behavior_next"}, key))
                    _ordinary_settings_phase(spec, run, driver, key, nonce, handled,
                                             max(deadline, time.monotonic() + 600), restore=True)
                    write_new_json(run / "ordinary-completion.json", signed({"spec_sha256": spec["plan_sha256"],
                        "nonce": nonce, "window_behavior_restored": True, "goal_satisfied": True}, key))
                    print(json.dumps({"phase": "complete", "run": run_name, **output}), flush=True)
                    return 0
                elif action == "cancel":
                    channel.send_request("cancel", {})
                    write_new_json(response, signed({"ok": True, "source_deleted": False,
                        'phase': 'cancelled_restore_window_behavior_next'}, key))
                    _ordinary_settings_phase(spec, run, driver, key, nonce, handled, time.monotonic() + 600, restore=True)
                    return 3
                else:
                    output = driver.command(request["request"])
                    pending = getattr(driver, "pending_cold", None)
                    if pending and action == "attest" and driver.current_view == "root" and driver.roles["root"]["nonce"] != pending["previous_root_nonce"]:
                        after = verify_runtime_switch(spec)
                        after["window_identity"] = {field: driver.frames[driver.current_frame]["window"][field] for field in ("pid", "created", "hwnd")}
                        driver.record_cold_start(supported_account(spec), pending["before"], after)
                        driver.pending_cold = None; output = driver.state()
                write_new_json(response, signed({"ok": True, "result": output}, key))
            except Exception as error:
                write_new_json(response, signed({"ok": False, "error_type": type(error).__name__,
                    "policy_reason": str(error) if isinstance(error, ValueError) else "product_action_or_evidence_unverified"}, key))
        time.sleep(0.1)
    channel.send_request('cancel', {})
    if _lifecycle(spec, run_name, '-timeout-observe').observe().targets:
        _ordinary_settings_phase(spec, run, driver, key, nonce, handled, time.monotonic() + 600, restore=True)
    fail("ordinary_acceptance_deadline_source_retained")


def read_status(bundle, spec, draft, run, *, verify=False):
    """Read-only projection, including incomplete imports; never settle locks."""
    from .cindy_isolation_recovery import observe
    result = {'read_only': True, 'snapshot_consistency': 'provisional_live_reads',
              'replay_permitted': False, 'source_cleanup_permitted': False}
    try:
        active = latest_import_spec(bundle, spec)
        result['import_spec_sha256'] = active['plan_sha256']
        result['import_journals'] = journal_projection(existing_entries(active))
        if verify and not (run / 'controller-completion.json').exists():
            observation = observe(active, draft, registry_before=active['controller']['registry_before'])
            observation.pop('registry_current', None)
            result['observation'] = observation
    except Exception as error:
        result['observation_error'] = {'error_type': type(error).__name__,
            'policy_reason': str(error) if isinstance(error, ValueError) else 'ordinary_read_unavailable_keep_evidence',
            'offline_elevated_verification_may_be_required': isinstance(error, PermissionError)}
    return result


def ui_request(bundle, spec, run_name, request):
    run, key, launch, _ = _load_run(bundle, spec, run_name)
    if not _peer_alive(launch["writer"]): fail("ordinary_controller_not_running")
    token = secrets.token_hex(12)
    path = run / ("ui-request-" + token + ".json")
    write_new_json(path, signed({"nonce": launch["nonce"], "request": request}, key))
    response = run / ("ui-response-" + token + ".json")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if response.exists():
            result = authenticated(json.loads(response.read_text(encoding="utf8")), key)
            print(json.dumps(result, ensure_ascii=False)); return 0 if result.get("ok") else 3
        if not _peer_alive(launch["writer"]): fail("ordinary_controller_exited_request_outcome_unknown")
        time.sleep(0.1)
    fail("ui_request_timeout_observe_before_retry")


def main(bundle, expected_manifest_hash, argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["execute", "internal-supervisor", "direct-execute", "internal-direct", "ui", "status", "verify"])
    parser.add_argument("--manifest-sha256", default=expected_manifest_hash)
    parser.add_argument("--run")
    parser.add_argument("--uac-authorized", action="store_true")
    parser.add_argument("--request")
    args = parser.parse_args(argv)
    if args.manifest_sha256 != expected_manifest_hash: fail("entry_manifest_argument_changed")
    spec, draft = verify_bundle(bundle, expected_manifest_hash,readonly_runtime=args.mode in {'ui','status','verify'})
    if args.mode in {'direct-execute', 'internal-direct'}:
        from .cindy_isolation_direct import ordinary_direct, elevated_direct
        if spec['controller'].get('acceptance_mode') != 'direct-native-data': fail('direct_mode_requires_new_frozen_spec')
        if args.mode == 'direct-execute': return ordinary_direct(bundle,spec,draft,expected_manifest_hash,uac_authorized=args.uac_authorized)
        return elevated_direct(bundle,spec,draft,expected_manifest_hash,args.run)
    if args.mode == "execute": return ordinary_execute(bundle, spec, draft, expected_manifest_hash, uac_authorized=args.uac_authorized)
    if args.mode == "internal-supervisor": return elevated(bundle, spec, draft, expected_manifest_hash, args.run)
    if args.mode == "ui": return ui_request(bundle, spec, args.run, json.loads(args.request))
    if args.run is None:
        result = read_status(bundle, spec, draft, Path(bundle), verify=args.mode == 'verify')
        result.update(phase='prepared_no_run_selected', goal_satisfied=False, spec_sha256=spec['plan_sha256'],
            frozen_roots=len(spec['controller']['source_root_ids']), frozen_native=len(spec['native_files']))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if args.mode == 'status' else 3
    run, key, launch, channel = _load_run(bundle, spec, args.run)
    result = {"run": args.run, "spec_sha256": spec["plan_sha256"], "ordinary_peer_alive": _peer_alive(launch["writer"]),
              'reviewed_product_files_rehashed':False,
              "ordinary_phase": json.loads((run / "ordinary-phase.json").read_text(encoding="utf8")) if (run / "ordinary-phase.json").exists() else None,
              "supervisor_exit": json.loads((run / "supervisor-exit.json").read_text()) if (run / "supervisor-exit.json").exists() else None,
              "goal_satisfied": False}
    result.update(read_status(bundle, spec, draft, run, verify=args.mode == 'verify'))
    completion = run / "controller-completion.json"
    if completion.exists():
        result["completion"] = authenticated(json.loads(completion.read_text(encoding="utf8")), key)
        if args.mode == "verify":
            completed = result['completion']
            if (completed.get('spec_sha256') != spec['plan_sha256'] or completed.get('nonce') != launch['nonce']
                    or not completed.get('goal_satisfied') or completed.get('goal_status') != 'complete'):
                fail('completion_not_bound_to_exact_run')
            evidence = completed.get('native_evidence', {})
            if not evidence or not any(name.endswith('-verify.json') for name in evidence): fail('native_verification_evidence_missing')
            for name, sha in evidence.items():
                if Path(name).name != name or digest(run / name) != sha: fail('formal_native_evidence_changed')
                proof = json.loads((run / name).read_text(encoding='utf8'))
                if proof.get('goal_status') != 'complete' or proof.get('goal_satisfied') is not True: fail('formal_native_evidence_not_complete')
            if digest(run / 'acceptance.json') != completed['accepted_evidence_sha256']: fail('accepted_evidence_changed')
            verify_accepted_runtime(spec, run, json.loads((run / 'acceptance.json').read_text(encoding='utf8')), key, launch['nonce'])
            if window_behavior(spec) != spec["controller"]["window_behavior_before"]: fail("original_window_behavior_not_restored")
            ordinary = authenticated(json.loads((run / "ordinary-completion.json").read_text(encoding="utf8")), key)
            if ordinary.get("spec_sha256") != spec["plan_sha256"] or ordinary.get("nonce") != launch["nonce"] or not ordinary.get("window_behavior_restored") or ordinary.get('goal_satisfied') is not True:
                fail("ordinary_completion_not_bound_to_this_run")
            for item in draft['groups']['current_shared_candidates']['native_files']:
                if os.path.lexists(item['source']['path']): fail('official_source_rollout_reappeared')
            with readonly_sqlite(Path(spec["source"]["path"]) / "state_5.sqlite") as c:
                ids = [f["native_id"] for f in spec["native_files"]]
                if c.execute("select count(*) from threads where id in (" + ",".join("?" for _ in ids) + ")", ids).fetchone()[0]: fail("official_source_records_reappeared")
            result.update(goal_satisfied=True, goal_status="complete")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if args.mode == "status" or result["goal_satisfied"] else 3
