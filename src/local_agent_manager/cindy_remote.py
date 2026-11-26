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
    database = Path(database)
    profile = Path(profile_root) if profile_root is not None else database.parent
    # Distinguish the process owner (Electron userData) from an external custom DB.
    profile_id = remote.directory_identity(profile)
    proof = {"database": str(database), "database_id": remote._owner("cindy", database)["store_id"],
            "profile_root": str(profile), "profile_id": profile_id,
            "session_id": session_id, "native_id": plan["native_locator"], "verify_request": request}
    if "cindy_hosted" in plan:
        hosted = plan["cindy_hosted"]
        if hosted["controller_device"] != cindy_hosted.device_id(profile) or hosted["task_id"] != session_id:
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


def guard_row(db, row, proof):
    """Checked again in the SQLite write transaction, including newly parked history."""
    sid, request = row["id"], proof["verify_request"]
    hosted = proof.get("hosted")
    host = hosted["execution_host"] if hosted else "ssh:" + request["destination"]
    if hosted and (sid != hosted["task_id"] or cindy_hosted.device_id(Path(proof["profile_root"])) != hosted["controller_device"]
            or host != "device:" + hosted["host_device"] and not host.startswith("share:")):
        raise ValueError("cindy_hosted_controller_mismatch")
    database = Path(db.execute("PRAGMA database_list").fetchone()[2])
    # Cindy retains the start timestamp even after a normal stop. The last
    # completed turn or history clear can settle an earlier start.
    started, ended, cleared = (row[k] if k in row.keys() else None for k in
                               ("active_turn_started_at", "last_turn_ended_at", "cleared_at"))
    if any(t is not None and (type(t) is not int or t < 0) for t in (started, ended, cleared)):
        raise ValueError("cindy_ssh_turn_state_unverified")
    if started is not None and started > max(ended or 0, cleared or 0):
        raise ValueError("cindy_ssh_turn_unfinished")
    if (sid != proof["session_id"] or remote.directory_identity(Path(proof["profile_root"])) != proof["profile_id"]
            or remote._owner("cindy", database)["store_id"] != proof["database_id"]
            or database != Path(proof["database"]) or execution_host(row) != host
            or not cindy_hosted.locator_matches(row["sdk_session_id"], proof["native_id"]) or not row["sdk_session_id"]
            or backend_for_agent_kind(row["agent_kind"]) != {"native": "codex", "claude": "claude", "pi": "pi"}.get(request["client"])
            or db.execute("SELECT 1 FROM messages WHERE session_id=? AND role='agent_switch' LIMIT 1", (sid,)).fetchone()):
        raise ValueError("cindy_ssh_binding_unverified")


def frozen(document):
    proofs = [e["remote_native"] for a in document.get("actions", ())
              for e in a.get("impact", {}).get("frontend_session_evidence", ()) if e.get("remote_native")]
    if not proofs:
        return None
    proof, = proofs
    action, = document["actions"]
    scope = document["scope"]
    request = proof["verify_request"]
    remote._validate(request)
    if (scope["client"] != "cindy" or scope["record_ids"] != [proof["session_id"]] or scope["engines"]
            or scope["projects"] or scope["all_projects"] or action["kind"] != "delete_frontend_session"
            or action["target"]["thread_id"] != proof["session_id"]
            or request["verb"] != "verify" or request["client"] not in {"native", "claude", "pi"}
            or action["impact"]["owner_client"] != "cindy" or action["impact"]["owner_process_root"] != proof["profile_root"]
            or action["impact"]["frontend_session_database_paths"] != [proof["database"]]):
        raise ValueError("cindy_ssh_exact_frontend_scope_required")
    return proof


def guard_references(proof):
    database, profile, sid = Path(proof["database"]), Path(proof["profile_root"]), proof["session_id"]
    client = proof["verify_request"]["client"]
    engine = "codex" if client == "native" else client
    hosted = proof.get("hosted")
    host = hosted["execution_host"] if hosted else "ssh:" + proof["verify_request"]["destination"]
    for namespace in resolve_cindy_profiles(root=profile, database=database):
        catalog = build_cindy_reference_catalog(namespace.database, profile_root=profile)
        if catalog.failures:
            raise ValueError("cindy_ssh_reference_inventory_incomplete")
        for r in catalog.references:
            if r.database == database and r.cindy_session_id == sid or r.backend != engine:
                continue
            if ((cindy_hosted.possible_reference(r.remote_native_locator, proof["native_id"])
                    or r.remote_native_locator == proof.get("native_record_id", proof["native_id"]))
                    and (hosted or r.execution_host in {host, "unknown"})):
                raise ValueError("cindy_ssh_shared_native_handle")


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
    guard_references(proof)
    if "backup" in proof:
        current = cindy_hosted.backup(proof)
        if row is not None and current != proof["backup"] or row is None and not current.get("absent"):
            raise ValueError("cindy_native_history_backup_changed_or_remaining")
    verify_native(proof, planning=not allow_absent)
    root = database.parent
    storage = StorageLocation(storage_id_for_path(root), "Cindy remote task database", root, scan_status=ScanStatus.OK)
    base = coordinator.service.prepare_report(ScanReport(), active_adapters=(), platforms=("cindy",))
    actions = ()
    if row is not None:
        evidence, = build_cindy_session_delete_evidence([{"database": str(database), "session_id": sid,
            "expected_status": row["status"], "explicitly_selected": True, "remote_native": proof}])
        digest = remote._digest(evidence.to_dict())
        impact = ActionImpact(frontend_session_database_paths=(str(database),), frontend_session_evidence=(evidence.to_dict(),),
            owner_client="cindy", owner_process_root=str(profile), resource_path=str(database),
            external_engine=backend_for_agent_kind(row["agent_kind"]), external_storage_root=str(root),
            external_action_payload={"frontend_session_id": sid, "remote_operation_id": proof["verify_request"]["operation_id"]})
        actions = (CandidateAction("delete_frontend_session:" + digest[:32], ActionKind.DELETE_FRONTEND_SESSION,
            TargetRef(storage.storage_id, sid), RiskLevel.HIGH, True, None, impact, digest,
            requires_explicit_selection=True, resource_kind="frontend_session"),)
    plan = replace(base.plan, actions=actions, storages=(storage,), plan_fingerprint=remote._digest(proof))
    context = replace(base, plan=plan, actions=coordinator.service.typed_actions(plan),
                      frontend_scan_coverage=((canonical_path(database), "sessions"),))
    return context, (), None, None, {}, {}
