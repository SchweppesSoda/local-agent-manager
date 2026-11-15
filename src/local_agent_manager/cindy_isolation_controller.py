"""Fixed two-stage privileged protocol for the owner's one Cindy legacy split.

No shell/command/path requests. The elevated process can only import this spec,
wait a finite time for its ordinary user's acceptance, then run the manager's
new source-only native plan. All product UI and lifecycle work stays ordinary.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time

from .cindy_isolation_plan import IsolationPlanError, fingerprint, directory_identity, physical_descendant
from .cindy_isolation_recovery import digest
from .operation_store import atomic_write_json, write_new_json as _write_json, utc_now

TASK = "cindy-legacy-isolation-20261115"
ROLES = {"root", "long_history", "side_history", "automations", "new_default_task", "cold_start"}


def fail(code): raise IsolationPlanError(code)


def write_new_json(path, value):
    """Publish one complete no-replace protocol packet, never an empty prefix.

    The ordinary and elevated peers poll these paths concurrently. The shared
    immutable-plan writer alone would expose creation before its fsync.
    Windows rename is atomic and refuses an existing destination.
    """
    from .legacy_index import _fsync_directory
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name('.controller-packet-' + secrets.token_hex(16) + '.tmp')
    try:
        _write_json(stage, value)
        if os.name == 'nt': os.rename(stage, path)
        else:
            os.link(stage, path); stage.unlink()
        _fsync_directory(path.parent)
    finally:
        if stage.exists(): stage.unlink()


def signed(value, key):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"body": value, "hmac_sha256": hmac.new(key, body, hashlib.sha256).hexdigest()}


def authenticated(value, key):
    if not isinstance(value, dict) or set(value) != {"body", "hmac_sha256"}:
        fail("handshake_shape_invalid")
    expected = signed(value["body"], key)["hmac_sha256"]
    if not hmac.compare_digest(expected, str(value["hmac_sha256"])): fail("handshake_authentication_failed")
    return value["body"]


class FixedChannel:
    def __init__(self, run, spec_hash, nonce, key):
        self.run, self.spec_hash, self.nonce, self.key = Path(run), spec_hash, nonce, key
        self.sequence = 0

    def envelope(self, role, sequence, kind, payload):
        return signed({"spec_sha256": self.spec_hash, "nonce": self.nonce, "role": role,
                       "sequence": sequence, "kind": kind, "payload": payload}, self.key)

    def decode(self, path, role, sequence):
        from . import frozen_files as ff
        ff._plain(path.lstat())
        body = authenticated(json.loads(path.read_text(encoding="utf8")), self.key)
        if (body.get("spec_sha256"), body.get("nonce"), body.get("role"), body.get("sequence")) != (
                self.spec_hash, self.nonce, role, sequence): fail("handshake_run_or_sequence_mismatch")
        return body

    def send_request(self, kind, payload):
        self.sequence += 1
        path = self.run / ("request-%06d.json" % self.sequence)
        write_new_json(path, self.envelope("ordinary", self.sequence, kind, payload))
        return self.sequence

    def receive_request(self, sequence):
        path = self.run / ("request-%06d.json" % sequence)
        return self.decode(path, "ordinary", sequence) if path.exists() else None

    def reply(self, sequence, kind, payload):
        write_new_json(self.run / ("response-%06d.json" % sequence), self.envelope("supervisor", sequence, kind, payload))

    def receive_reply(self, sequence):
        path = self.run / ("response-%06d.json" % sequence)
        return self.decode(path, "supervisor", sequence) if path.exists() else None


def verify_bundle(bundle, expected_manifest_hash, *, readonly_runtime=False):
    from . import frozen_files as ff
    bundle = Path(bundle)
    manifest_path = bundle / "execution-manifest.json"
    ff._plain(manifest_path.lstat())
    if digest(manifest_path) != expected_manifest_hash: fail("execution_manifest_hash_mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    spec = json.loads((bundle / "execution-spec.json").read_text(encoding="utf8"))
    startup={os.path.normcase(str(Path(e['root'])/e['file']['path'])) for e in spec['controller']['startup_files']}
    for entry in manifest["files"]:
        path = Path(entry["path"])
        if readonly_runtime and os.path.normcase(str(path)) in startup:continue
        ff._plain(path.lstat())
        if digest(path) != entry["sha256"]: fail("reviewed_execution_file_changed")
    value = dict(spec); approved = value.pop("plan_sha256")
    if fingerprint(value) != approved or approved != manifest["spec_sha256"]: fail("execution_spec_hash_mismatch")
    if spec["controller"]["task"] != TASK: fail("wrong_task_spec")
    if os.name=='nt' and spec['controller'].get('executor_sid'):
        from .cindy_isolation_windows import current_user_sid
        if current_user_sid()!=spec['controller']['executor_sid']:fail('execution_windows_account_changed')
    for key in ("profile", "source", "target"):
        if directory_identity(Path(spec[key]["path"])) != spec[key]: fail("execution_physical_scope_changed")
    draft = json.loads((bundle / "source-draft.json").read_text(encoding="utf8"))
    if fingerprint(draft) != spec["controller"]["draft_content_sha256"]: fail("source_draft_hash_changed")
    return spec, draft


@contextmanager
def product_fence(spec):
    from .cindy_isolation_windows import no_cindy
    from .windows_held_files import HeldFiles
    no_cindy()
    with HeldFiles() as held:
        for file in spec["controller"]["startup_files"]:
            held.acquire(Path(file["root"]), file["file"])
        no_cindy()
        yield


def supported_account(spec, *, home=None):
    from .codex_app_server import CodexAppServer
    env = {k: v for k, v in os.environ.items() if k.upper() not in {
        "CODEX_HOME", "CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENAI_FEDERATION_RULE_ID", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_WORKLOAD_IDENTITY_CONTEXT"}}
    with CodexAppServer(codex_home=Path(home or spec["target"]["path"]),
                        codex_binary=Path(spec["controller"]["native_binary"]), timeout=30, environment=env) as server:
        account = server.request("account/read", {"refreshToken": False}).get("account") or {}
    identity = {"type": account.get("type"), "email": str(account.get("email", "")).strip().casefold()}
    if identity["type"] != "chatgpt" or not identity["email"]: fail("existing_account_not_authenticated")
    return {"identity_sha256": fingerprint(identity), "authenticated": True, "refreshToken": False}


def validate_source_plan(plan, spec):
    """Reject every cross-home or frontend write; do not trim a wider plan."""
    from .operation_store import plan_sha256
    roots, ids = set(spec["controller"]["source_root_ids"]), {f["native_id"] for f in spec["native_files"]}
    if plan.get("goal_status") != "ready" or plan.get("blockers"): fail("native_source_plan_blocked")
    if plan_sha256(plan) != plan.get("plan_sha256"): fail("native_source_plan_hash_invalid")
    scope = plan["scope"]
    if scope["client"] not in {"native", "codex-native"} or set(scope.get("record_ids", [])) != roots or scope.get("all_projects") or scope.get("projects"):
        fail("native_source_scope_changed")
    source = Path(spec["source"]["path"])
    storages = {s["storage_id"]: Path(s["path"]) for s in plan["storages"]}
    if not storages or any(not os.path.samefile(path, source) for path in storages.values()): fail("native_source_cross_home_plan")
    affected = set()
    for action in plan["actions"]:
        if action["kind"] not in {"delete_conversation", "remove_desktop_state", "remove_broken_relation"}: fail("native_source_unapproved_mutation_family")
        if action["target"]["storage_id"] not in storages: fail("native_source_unknown_storage")
        impact = action.get("impact", {})
        selected = set(action.get("affected_thread_ids", [])) | set(impact.get("affected_thread_ids", []))
        selected |= set(impact.get("descendant_thread_ids", []))
        if action["target"].get("thread_id"): selected.add(action["target"]["thread_id"])
        if not selected or not selected <= ids: fail("native_source_native_closure_expanded")
        if impact.get("frontend_reference_evidence") or impact.get("frontend_session_evidence") or impact.get("frontend_reference_count"):
            fail("native_source_would_clear_cindy_target_reference")
        affected.update(selected)
    if affected != ids: fail("native_source_incomplete_closure")
    return {"plan_sha256": plan["plan_sha256"], "source_only": True, "native_ids": sorted(affected)}


def cleanup_source(spec, run, *, coordinator=None, adapters=None, app_server_factory=None, binary_resolver=None, before_apply=None):
    from .operation_coordinator import OperationCoordinator
    from .cleanup_service import CleanupService
    from .codex_desktop_state import running_related_clients
    from .cindy_isolation_windows import verify_retained_target, verify_runtime_switch
    coordinator = coordinator or OperationCoordinator(CleanupService())
    source = Path(spec["source"]["path"]); run = Path(run)
    plan_path = run / "source-cleanup-plan.json"
    if plan_path.exists(): fail("prior_source_cleanup_use_status_verify_never_reapply")
    if app_server_factory is None and running_related_clients(source, owner_client="native"):
        fail("official_source_client_still_running")
    verify_retained_target(spec); verify_runtime_switch(spec)
    plan = coordinator.plan_operation(client="native", record_ids=tuple(spec["controller"]["source_root_ids"]),
        codex_home=source, codex_bin=Path(spec["controller"]["native_binary"]), plan_path=plan_path, adapters=adapters)
    validation = validate_source_plan(plan, spec)
    write_new_json(run / "source-cleanup-plan-review.json", validation)
    if before_apply is not None: before_apply(plan)
    result = coordinator.apply_operation(plan_path=plan_path, codex_home=source,
        codex_bin=Path(spec["controller"]["native_binary"]), plan_sha256=plan["plan_sha256"],
        clients_closed=True, adapters=adapters, app_server_factory=app_server_factory, binary_resolver=binary_resolver)
    write_new_json(run / "source-cleanup-apply.json", result)
    status = coordinator.status_operation(plan_path=plan_path, codex_home=source)
    write_new_json(run / "source-cleanup-status.json", status)
    verified = coordinator.verify_operation(plan_path=plan_path, codex_home=source, adapters=adapters)
    write_new_json(run / "source-cleanup-verify.json", verified)
    if any(value.get("goal_status") != "complete" or value.get("goal_satisfied") is not True for value in (result, status, verified)):
        fail("native_source_cleanup_not_verified_no_replay")
    verify_retained_target(spec); verify_runtime_switch(spec)
    return {"goal_status": "complete", "goal_satisfied": True, "source_only": True,
            "operation_id": plan["operation_id"], "plan_sha256": plan["plan_sha256"]}


def validate_acceptance(spec, run, envelope, key, nonce):
    body = authenticated(envelope, key)
    if body.get("spec_sha256") != spec["plan_sha256"] or body.get("nonce") != nonce: fail("acceptance_wrong_run")
    if set(body.get("roles", {})) != ROLES: fail("acceptance_roles_incomplete")
    if body.get("account", {}).get("identity_sha256") != spec["controller"]["account_identity_sha256"]: fail("acceptance_wrong_existing_account")
    if body.get("lifecycle_closed_for_cleanup") is not True: fail("acceptance_cindy_not_closed")
    frames = body.get("frames", {})
    for role, proof in body["roles"].items():
        frame = frames.get(proof.get("frame"))
        if not frame: fail("acceptance_role_without_product_frame")
    for name, frame in frames.items():
        if Path(name).name != name or not name.endswith(".png"): fail("acceptance_frame_path_invalid")
        if digest(Path(run) / name) != frame["sha256"]: fail("acceptance_frame_changed")
    for role in ("root", "long_history"):
        proof = body["roles"][role]
        expected = spec["controller"]["representatives"][role]
        if proof.get("task_id") != expected["task_id"] or proof.get("native_id") != expected["native_id"]:
            fail("acceptance_original_task_mismatch")
        if not proof.get("native_probe_verified"): fail("acceptance_no_real_continuation")
    if not body["roles"]["new_default_task"].get("native_probe_verified"): fail("acceptance_default_task_not_proven")
    if not body["roles"]["cold_start"].get("independent_auth"): fail("acceptance_cold_auth_not_proven")
    cold = body["roles"]["cold_start"]
    if not cold.get("native_probe_verified") or cold.get("native_probe_nonce") != body["roles"]["root"].get("nonce"):
        fail("cold_start_requires_a_real_post_restart_probe")
    before_window = cold.get("before", {}).get("window_identity")
    after_window = cold.get("after", {}).get("window_identity")
    if not before_window or not after_window or before_window == after_window or frames[cold["frame"]].get("window_identity") != after_window:
        fail("cold_start_process_identity_not_changed_and_observed")
    return body


def protected_snapshot(spec):
    """Freeze configuration/ownership, not transient run status or chat text."""
    from .cindy_isolation_plan import readonly_sqlite
    from .cindy_isolation_recovery import _hash_rows, q
    result = {}
    with readonly_sqlite(Path(spec["controller"]["cindy_database"])) as c:
        result["task_bindings"] = [list(r) for r in c.execute("select id,agent_kind,sdk_session_id from sessions order by id")]
        names = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        for table in ("schedules", "project_automation_consents"):
            if table not in names: continue
            columns = [r[1] for r in c.execute("pragma table_info(" + q(table) + ")") if r[1] not in {
                "updated_at", "last_fired_at", "next_fire_at", "last_finished_at"}]
            result[table] = {"columns": columns, "rows_sha256": _hash_rows(c.execute("select " + ",".join(map(q, columns)) + " from " + q(table)))}
    second = spec["controller"].get("protected_second_home")
    if second:
        with readonly_sqlite(Path(second["path"]) / "state_5.sqlite") as c:
            result["second_ids"] = [r[0] for r in c.execute("select id from threads order by id")]
            files = [r[0] for r in c.execute("select rollout_path from threads order by id")]
        from . import frozen_files as ff
        result['second_files'] = {}
        for path in files:
            info = Path(path).lstat(); ff._plain(info)
            result['second_files'][path] = {'sha256': digest(path), 'size': info.st_size,
                'device': info.st_dev, 'inode': info.st_ino}
    return result


def verify_protected_state(spec):
    expected = spec["controller"]["protected_snapshot"]
    actual = protected_snapshot(spec)
    current = {row[0]: row for row in actual["task_bindings"]}
    for row in expected["task_bindings"]:
        if current.get(row[0]) != row: fail("existing_cindy_task_native_binding_changed")
    for key in ("schedules", "project_automation_consents"):
        if actual.get(key) != expected.get(key): fail("protected_second_account_or_automation_changed")
    if not set(expected.get('second_ids',[]))<=set(actual.get('second_ids',[])):
        fail('protected_second_original_native_missing')
    for path, frozen in expected.get('second_files',{}).items():
        current = actual.get('second_files', {}).get(path)
        if not current or current['device'] != frozen['device'] or current['inode'] != frozen['inode'] or current['size'] < frozen['size']:
            fail('protected_second_original_rollout_changed')
        hasher = hashlib.sha256()
        with Path(path).open('rb') as stream:
            remaining = frozen['size']
            while remaining:
                data = stream.read(min(1024*1024, remaining))
                if not data: fail('protected_second_original_rollout_truncated')
                remaining -= len(data); hasher.update(data)
        if hasher.hexdigest() != frozen['sha256']: fail('protected_second_original_rollout_prefix_changed')
    return {"existing_tasks_preserved": True, "automation_config_preserved": True, "second_account_preserved": True}


def verify_accepted_runtime(spec, run, envelope, key, nonce):
    from .cindy_isolation_plan import readonly_sqlite
    from .cindy_isolation_ui import probe_in_rollout, probe_home
    from .cindy_isolation_windows import verify_retained_target, verify_runtime_switch
    from . import frozen_files as ff
    body = validate_acceptance(spec, run, envelope, key, nonce)
    verify_retained_target(spec); verify_runtime_switch(spec); verify_protected_state(spec)
    for role in ("root", "long_history", "new_default_task"):
        proof = body["roles"][role]
        with readonly_sqlite(Path(spec["controller"]["cindy_database"])) as c:
            row = c.execute("select sdk_session_id,agent_kind from sessions where id=?", (proof["task_id"],)).fetchone()
        if row is None or row[1] != "codex" or row[0] != proof["native_id"]: fail("acceptance_task_native_binding_not_current")
        home,account=probe_home(spec,proof['native_id'],role)
        if proof.get('account_id')!=account or proof.get('actual_home')!=str(home): fail('acceptance_new_account_home_attribution_changed')
        with readonly_sqlite(home / "state_5.sqlite") as c:
            path = c.execute("select rollout_path from threads where id=?", (proof["native_id"],)).fetchone()
        if path is None: fail("acceptance_target_native_missing")
        path = physical_descendant(Path(path[0]),home)
        token = proof["nonce"]
        if len(token) != 20 or any(ch not in "0123456789abcdef" for ch in token): fail("acceptance_probe_nonce_invalid")
        prompt = "隔离验收测试 " + token + "：请只回复 ISOLATION_OK " + token + "，不调用工具、不修改文件。"
        original = next((f for f in spec["native_files"] if f["native_id"] == proof["native_id"]), None)
        if not probe_in_rollout(path, offset=original["size"] if original else 0, prompt=prompt, answer="ISOLATION_OK " + token):
            fail("acceptance_real_probe_not_found")
        if role == "new_default_task":
            if original is not None or proof["task_id"] in {r[0] for r in spec["controller"]["protected_snapshot"]["task_bindings"]}:
                fail("acceptance_default_test_not_new")
            with readonly_sqlite(Path(spec["source"]["path"]) / "state_5.sqlite") as c:
                if c.execute("select count(*) from threads where id=?", (proof["native_id"],)).fetchone()[0]:
                    fail("acceptance_default_test_present_in_official_home")
            expected_account=spec['controller'].get('allowed_test_accounts',{}).get(account)
            if expected_account and supported_account(spec,home=home)['identity_sha256']!=expected_account:
                fail('new_task_existing_account_identity_changed')
    if body["roles"]["side_history"].get("native_id") not in spec["controller"]["side_native_ids"]:
        fail("acceptance_side_thread_outside_original_closure")
    return body


class FixedSupervisor:
    """Finite privileged state machine, injected host actions only in tests."""
    def __init__(self, spec, run, channel, *, offline, verify_accepted, cleanup,
                 peer_alive=lambda: True, clock=time.monotonic, wait=time.sleep):
        self.spec, self.run, self.channel = spec, Path(run), channel
        self.offline, self.verify_accepted, self.cleanup = offline, verify_accepted, cleanup
        self.peer_alive, self.clock, self.wait = peer_alive, clock, wait

    def execute(self):
        deadline = self.clock() + self.spec["controller"]["supervisor_max_seconds"]
        phase, sequence, mutated = "awaiting_offline", 1, False
        try:
            self.channel.reply(0, "ready", {"phase": phase, "deadline_seconds": self.spec["controller"]["supervisor_max_seconds"]})
            while self.clock() < deadline:
                if not self.peer_alive(): fail("ordinary_controller_exited_source_retained")
                request = self.channel.receive_request(sequence)
                if request is None:
                    self.wait(0.1); continue
                kind, payload = request["kind"], request["payload"]
                if kind == "cancel":
                    self.channel.reply(sequence, "cancelled", {"source_cleanup_dispatched_this_run": False}); return 3
                if kind == "offline" and phase == "awaiting_offline":
                    if set(payload) != {"lifecycle_receipt_sha256"}: fail("offline_request_extra_fields")
                    mutated = True; result = self.offline(); phase = "awaiting_acceptance"
                    self.channel.reply(sequence, "offline_verified", result)
                elif kind == "acceptance" and phase == "awaiting_acceptance":
                    if set(payload) != {"acceptance_sha256"}: fail("acceptance_request_extra_fields")
                    path = self.run / "acceptance.json"
                    if digest(path) != payload["acceptance_sha256"]: fail("acceptance_packet_changed")
                    accepted = self.verify_accepted(json.loads(path.read_text(encoding="utf8")))
                    write_new_json(self.run / "acceptance-verified.json", {"spec_sha256": self.spec["plan_sha256"], "verified": True,
                                   "acceptance_sha256": payload["acceptance_sha256"]})
                    # No message can nominate a path, command, extra ID or step.
                    if self.clock() >= deadline or not self.peer_alive():
                        fail('acceptance_expired_or_ordinary_peer_exited_source_retained')
                    result = self.cleanup()
                    phase='complete'
                    self.channel.reply(sequence, "complete", result)
                    return 0
                else: fail("supervisor_unexpected_phase_or_request")
                sequence += 1
            fail("supervisor_deadline_source_retained")
        except BaseException as error:
            # Never echo arbitrary native/RPC exception bodies or credentials.
            atomic_write_json(self.run / "supervisor-failure.json", {"at": utc_now(), "phase": phase,
                "error_type": type(error).__name__, "source_cleanup_may_have_started": phase == "awaiting_acceptance" and (self.run / "source-cleanup-plan.json").exists(),
                "offline_may_have_mutated": mutated, "goal_satisfied": False, "replay_permitted": False})
            try: self.channel.reply(sequence, "failed", {"phase": phase, "error_type": type(error).__name__, "goal_satisfied": False})
            except Exception: pass
            return 1
        finally:
            atomic_write_json(self.run / "supervisor-exit.json", {"at": utc_now(), "bounded_exit": True,
                              "phase": phase, "pid": os.getpid()})
