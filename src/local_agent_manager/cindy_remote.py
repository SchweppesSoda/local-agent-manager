"""Qualify one Cindy remote task against its execution host's native operation."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import ntpath

from . import remote_records as remote
from . import cindy_hosted
from .cindy_references import backend_for_agent_kind, build_cindy_reference_catalog, execution_host
from .discovery import resolve_cindy_profiles
from .operation_store import strict_json_load
from .record_identity import canonical_path, project_selector_matches
from .sqlite_utils import connect_readonly

CONTROLLER_ONLY = "cindy_controller_only"


def _controller_identity(database, session_id, profile_root):
    database = Path(database)
    profile = Path(profile_root) if profile_root is not None else database.parent
    return {"database": str(database), "database_id": remote._owner("cindy", database)["store_id"],
            "profile_root": str(profile), "profile_id": remote.directory_identity(profile), "session_id": session_id}


def controller_proof(database, session_id, profile_root=None):
    """Freeze only the controller's own task and locally attributable checkpoint."""
    proof = _controller_identity(database, session_id, profile_root)
    with closing(connect_readonly(Path(database))) as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None or not execution_host(row).startswith(("device:", "share:", "ssh:")):
            raise ValueError("cindy_controller_only_exact_remote_task_required")
        proof.update(completion_scope=CONTROLLER_ONLY, execution_host=execution_host(row),
                     engine=backend_for_agent_kind(row["agent_kind"]), native_id=row["sdk_session_id"])
        guard_row(db, row, proof)
    backup = cindy_hosted.backup(proof)
    if backup:
        proof["backup"] = backup
    return proof


def selected_nonlocal_tasks(rows, scope):
    """Match the same task selectors without resolving an SSH cwd on this computer."""
    selected = set()
    for row in rows:
        if scope["engines"] and row.backend not in scope["engines"]:
            continue
        sid = row.platform_session_id
        if scope["record_ids"]:
            matches = any(sid.startswith(value) or ("cindy:" + sid).startswith(value) for value in scope["record_ids"])
        elif row.status == "deleted":
            cwd = row.details.get("working_dir")
            host = row.details.get("execution_host", "local")
            if host == "unknown" or host.startswith("ssh:"):
                matches = any(cwd and (s == cwd or "/" not in s and "\\" not in s
                              and ntpath.basename(cwd.rstrip("/\\")).casefold().startswith(s.casefold()))
                              for s in scope["projects"])
            else:
                matches = any(cwd and project_selector_matches(s, cwd) for s in scope["projects"])
            matches = scope["all_projects"] or matches
        else:
            matches = False
        if matches:
            selected.add((str(row.database), sid))
    return sorted(selected)


def from_remote_plan(path, database, session_id, profile_root=None):
    plan = strict_json_load(Path(path))
    owner = plan["owner"]
    if (plan.get("protocol") != remote.PROTOCOL or plan.get("completion_scope") != "provider_native"
            or owner.get("client") not in {"native", "claude", "pi"} or plan.get("goal_status") != "ready"):
        raise ValueError("cindy_ssh_native_plan_required")
    request = {"protocol": remote.PROTOCOL, "verb": "verify", "destination": plan["destination"],
               "client": owner["client"], "root": owner["root"], "owner": owner["store_id"],
               "operation_id": plan["operation_id"], "approval": plan["approval"]}
    if owner["client"] == "pi":
        request["pi_agent_dir"] = owner["agent_dir"]
    remote._validate(request)
    proof = _controller_identity(database, session_id, profile_root)
    proof.update(native_id=plan["native_locator"], verify_request=request)
    if "cindy_hosted" in plan:
        hosted = plan["cindy_hosted"]
        if hosted["controller_device"] != cindy_hosted.device_id(Path(proof["profile_root"])) or hosted["task_id"] != session_id:
            raise ValueError("cindy_hosted_controller_mismatch")
        proof.update(hosted=hosted, native_record_id=plan["record_id"])
        if hosted["execution_host"].startswith("device:") and "account_scope" in hosted:
            digest = cindy_hosted.controller_account(proof)
            expected = digest if owner["client"] == "native" else digest[:20]
            if hosted["account_scope"] != expected:
                raise ValueError("cindy_controller_executor_accounts_differ")
        backup = cindy_hosted.backup(proof)
        if backup:
            proof["backup"] = backup
    return proof


def hosted_task_reference(database, profile, sid, client):
    """Resolve the user's exact controller task before looking up its executor ID."""
    remote._owner("cindy", database)
    remote.directory_identity(profile)
    catalog = build_cindy_reference_catalog(database, profile_root=profile)
    refs = [r for r in catalog.references if r.cindy_session_id == sid]
    if catalog.failures or len(refs) != 1:
        raise ValueError("cindy_hosted_single_task_binding_required")
    ref, = refs
    if (ref.reference_kind != "current" or not ref.remote_native_locator
            or not ref.execution_host.startswith(("device:", "share:"))
            or ref.backend != ("codex" if client == "native" else client)):
        raise ValueError("cindy_hosted_provider_mismatch")
    return ref


def verify_native(proof, *, planning=False):
    request = proof["verify_request"]
    if request["verb"] != "verify" or request["client"] not in {"native", "claude", "pi"}:
        raise ValueError("cindy_ssh_invalid_native_receipt")
    code, reply = remote.exchange({**request, "verb": "status"} if planning else request)
    engine = "codex" if request["client"] == "native" else request["client"]
    hosted = proof.get("hosted")
    result = reply.get("result") or {}
    if (code or reply.get("completion_scope") != "provider_native"
            or (reply.get("cindy_hosted") != hosted if hosted else reply["owner"].get("cindy_ssh_provider") != engine)
            or reply.get("native_locator") != proof["native_id"]
            or reply.get("native_ids") != [proof.get("native_record_id", proof["native_id"])]):
        raise ValueError("cindy_ssh_native_binding_unverified")
    if planning and reply.get("goal_status") in {"ready", "complete"}:
        return
    if (reply.get("goal_status") != "complete" or result.get("goal_satisfied") is not True or result.get("modified") is not True
            or not result.get("batches") or any(b.get("status") != "complete" for b in result["batches"])):
        raise ValueError("cindy_ssh_native_completion_unverified")


def _require_settled_turn(row):
    # Cindy retains the start timestamp even after a normal stop. The last
    # completed turn or history clear can settle an earlier start.
    started, ended, cleared = (row[k] if k in row.keys() else None for k in
                               ("active_turn_started_at", "last_turn_ended_at", "cleared_at"))
    if any(t is not None and (type(t) is not int or t < 0) for t in (started, ended, cleared)):
        raise ValueError("cindy_ssh_turn_state_unverified")
    if started is not None and started > max(ended or 0, cleared or 0):
        raise ValueError("cindy_ssh_turn_unfinished")


def guard_row(db, row, proof):
    """Checked again in the SQLite write transaction, including newly parked history."""
    sid = row["id"]
    if proof.get("completion_scope") == CONTROLLER_ONLY:
        host, engine = proof["execution_host"], proof["engine"]
        if not host.startswith(("device:", "share:", "ssh:")) or "verify_request" in proof or "hosted" in proof:
            raise ValueError("cindy_controller_only_scope_invalid")
        if "parent_session_id" in row.keys() and db.execute(
                "SELECT 1 FROM sessions WHERE parent_session_id=? AND id<>? LIMIT 1", (sid, sid)).fetchone():
            raise ValueError("cindy_controller_task_has_children")
    else:
        request, hosted = proof["verify_request"], proof.get("hosted")
        host = hosted["execution_host"] if hosted else "ssh:" + request["destination"]
        engine = {"native": "codex", "claude": "claude", "pi": "pi"}.get(request["client"])
        if hosted and (sid != hosted["task_id"] or cindy_hosted.device_id(Path(proof["profile_root"])) != hosted["controller_device"]
                or host != "device:" + hosted["host_device"] and not host.startswith("share:")):
            raise ValueError("cindy_hosted_controller_mismatch")
        if not row["sdk_session_id"] or db.execute(
                "SELECT 1 FROM messages WHERE session_id=? AND role='agent_switch' LIMIT 1", (sid,)).fetchone():
            raise ValueError("cindy_ssh_binding_unverified")
    database = Path(db.execute("PRAGMA database_list").fetchone()[2])
    _require_settled_turn(row)
    if (sid != proof["session_id"] or remote.directory_identity(Path(proof["profile_root"])) != proof["profile_id"]
            or remote._owner("cindy", database)["store_id"] != proof["database_id"]
            or database != Path(proof["database"]) or execution_host(row) != host
            or not cindy_hosted.locator_matches(row["sdk_session_id"], proof["native_id"])
            or backend_for_agent_kind(row["agent_kind"]) != engine):
        raise ValueError("cindy_ssh_binding_unverified")


def frozen(document):
    proofs = [e["remote_native"] for a in document.get("actions", ())
              for e in a.get("impact", {}).get("frontend_session_evidence", ()) if e.get("remote_native")]
    if not proofs:
        if document.get("scope", {}).get("cindy_controller_only") and document.get("goal_status") == "ready":
            raise ValueError("cindy_controller_only_evidence_required")
        return None
    proof, = proofs
    action, = document["actions"]
    scope = document["scope"]
    local = proof.get("completion_scope") == CONTROLLER_ONLY
    if local != (scope.get("cindy_controller_only") is True):
        raise ValueError("cindy_controller_only_scope_mismatch")
    if local:
        if ("verify_request" in proof or "hosted" in proof or scope.get("paseo_only") or scope.get("t3code_only")
                or action["impact"]["external_action_payload"].get("completion_scope") != CONTROLLER_ONLY):
            raise ValueError("cindy_controller_only_scope_invalid")
    else:
        request = proof["verify_request"]
        remote._validate(request)
        if request["verb"] != "verify" or request["client"] not in {"native", "claude", "pi"}:
            raise ValueError("cindy_ssh_invalid_native_receipt")
    if (scope["client"] != "cindy" or scope["record_ids"] != [proof["session_id"]] or scope["engines"]
            or scope["projects"] or scope["all_projects"] or action["kind"] != "delete_frontend_session"
            or action["target"]["thread_id"] != proof["session_id"]
            or action["impact"]["owner_client"] != "cindy" or action["impact"]["owner_process_root"] != proof["profile_root"]
            or action["impact"]["frontend_session_database_paths"] != [proof["database"]]):
        raise ValueError("cindy_ssh_exact_frontend_scope_required")
    return proof


def guard_references(proof, *, selected_absent=False):
    database, profile, sid = Path(proof["database"]), Path(proof["profile_root"]), proof["session_id"]
    client = proof["verify_request"]["client"]
    engine = "codex" if client == "native" else client
    hosted = proof.get("hosted")
    host = hosted["execution_host"] if hosted else "ssh:" + proof["verify_request"]["destination"]
    bindings = (hosted or {}).get("guest_bindings", {})
    seen = set()
    if bindings and not selected_absent:
        with closing(connect_readonly(database)) as db:
            for row in db.execute("SELECT * FROM sessions WHERE agent_device_id=?", (host,)):
                _require_settled_turn(row)
    for namespace in resolve_cindy_profiles(root=profile, database=database):
        catalog = build_cindy_reference_catalog(namespace.database, profile_root=profile)
        if catalog.failures:
            raise ValueError("cindy_ssh_reference_inventory_incomplete")
        for r in catalog.references:
            if bindings and not selected_absent:
                task = cindy_hosted.host_session_id(hosted["guest_peer"], r.cindy_session_id)
                if r.execution_host == host or task in bindings and r.database == database:
                    if (r.database != database or task in seen or task not in bindings or r.execution_host != host or r.reference_kind != "current"
                            or r.backend != engine or r.session_details.get("parent_session_id")
                            or not cindy_hosted.locator_matches(r.remote_native_locator, bindings[task])):
                        raise ValueError("cindy_guest_controller_tasks_unverified")
                    seen.add(task)
                if r.database == database and r.session_details.get("parent_session_id") == sid:
                    raise ValueError("cindy_guest_controller_fork_reference")
            if r.database == database and r.cindy_session_id == sid or r.backend != engine:
                continue
            if hosted and r.execution_host.startswith("device:") and r.execution_host != "device:" + hosted["host_device"]:
                continue
            if ((cindy_hosted.possible_reference(r.remote_native_locator, proof["native_id"])
                    or r.remote_native_locator == proof.get("native_record_id", proof["native_id"]))
                    and (hosted or r.execution_host in {host, "unknown"})):
                raise ValueError("cindy_ssh_shared_native_handle")
    if bindings and not selected_absent and seen != bindings.keys():
        raise ValueError("cindy_guest_controller_tasks_unverified")


def check_before_native(document, proof):
    from .codex_desktop_state import running_related_clients
    from .frontend_session_cleanup import _require_client_closed, build_cindy_session_delete_evidence
    _require_client_closed(Path(proof["profile_root"]), running_related_clients, owner_client="cindy")
    expected, = document["actions"][0]["impact"]["frontend_session_evidence"]
    current, = build_cindy_session_delete_evidence([expected])
    if current.to_dict() != expected or cindy_hosted.backup(proof) != proof.get("backup"):
        raise ValueError("cindy_controller_session_changed")
    guard_references(proof)


def context_for(coordinator, proof, *, allow_absent):
    from .cleaner import ScanReport
    from .frontend_session_cleanup import build_cindy_session_delete_evidence
    from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, ScanStatus, StorageLocation, TargetRef, storage_id_for_path

    database, sid = Path(proof["database"]), proof["session_id"]
    profile = Path(proof["profile_root"])
    if remote.directory_identity(profile) != proof["profile_id"]:
        raise ValueError("cindy_controller_profile_changed")
    if remote._owner("cindy", database)["store_id"] != proof["database_id"]:
        raise ValueError("cindy_controller_database_changed")
    with closing(connect_readonly(database)) as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is not None:
            guard_row(db, row, proof)
        elif not allow_absent:
            raise ValueError("cindy_ssh_task_missing")
    # Check all DB namespaces of this userData root before handing the owner
    # its native operation. A second current or parked binding can still resume it.
    local = proof.get("completion_scope") == CONTROLLER_ONLY
    if not local:
        guard_references(proof, selected_absent=row is None)
    if "backup" in proof:
        current = cindy_hosted.backup(proof)
        if row is not None and current != proof["backup"] or row is None and not current.get("absent"):
            raise ValueError("cindy_native_history_backup_changed_or_remaining")
    if not local:
        verify_native(proof, planning=not allow_absent)
    root = database.parent
    storage = StorageLocation(storage_id_for_path(root), "Cindy remote task database", root, scan_status=ScanStatus.OK)
    base = coordinator.service.prepare_report(ScanReport(), active_adapters=(), platforms=("cindy",))
    actions = ()
    if row is not None:
        evidence, = build_cindy_session_delete_evidence([{"database": str(database), "session_id": sid,
            "expected_status": row["status"], "explicitly_selected": True, "remote_native": proof}])
        digest = remote._digest(evidence.to_dict())
        payload = {"frontend_session_id": sid}
        if local:
            payload["completion_scope"] = CONTROLLER_ONLY
        else:
            payload["remote_operation_id"] = proof["verify_request"]["operation_id"]
        impact = ActionImpact(frontend_session_database_paths=(str(database),), frontend_session_evidence=(evidence.to_dict(),),
            owner_client="cindy", owner_process_root=str(profile), resource_path=str(database),
            external_engine=backend_for_agent_kind(row["agent_kind"]), external_storage_root=str(root),
            external_action_payload=payload)
        actions = (CandidateAction("delete_frontend_session:" + digest[:32], ActionKind.DELETE_FRONTEND_SESSION,
            TargetRef(storage.storage_id, sid), RiskLevel.HIGH, True, None, impact, digest,
            requires_explicit_selection=True, resource_kind="frontend_session"),)
    plan = replace(base.plan, actions=actions, storages=(storage,), plan_fingerprint=remote._digest(proof))
    context = replace(base, plan=plan, actions=coordinator.service.typed_actions(plan),
                      frontend_scan_coverage=((canonical_path(database), "sessions"),))
    return context, (), None, None, {}, {}
