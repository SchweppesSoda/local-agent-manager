"""Bind a controller's structured chat projections to its paired Orca runtime."""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
from urllib.parse import quote

from . import frozen_files, frozen_sqlite, orca_frontend as frontend, orca_journal_cleanup as journal
from . import remote_records as remote
from .operation_store import strict_json_load
from .orca_metadata import canonical_execution_host, parse_orca_record
from .sqlite_utils import connect_readonly


def _key(value):
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise ValueError("orca_pairing_public_key_invalid")
    return hashlib.sha256(raw).hexdigest()


def handle_chain(sid, raw):
    record = parse_orca_record(sid, raw)
    lease = raw["lease"]
    if (record.unsupported_recovery or record.wsl_distro is not None
            or record.lease.claim_status != "released" or record.lease.unreconciled or record.lease.owner_pid is not None
            or any(lease.get(k) is not None for k in ("handoffStage", "handoffOperationId", "reservedSpawnToken"))):
        raise ValueError("orca_remote_lease_unsettled")
    identity = [sid, raw["createdAt"], raw["location"]["workspaceKind"], raw["location"]["workspaceId"],
                raw["accountHome"], record.provider, raw["providerHandleChain"]]
    if record.provider == "claude":
        identity.extend([lease["runtimeFence"], raw.get("launchDirectory"), raw.get("launchedAccount"), raw.get("acquiredAccount"),
                         raw.get("forkedFrom"), raw.get("providerContextBoundary")])
    return remote._digest(identity)


def authoritative_chain(root, sid):
    with closing(connect_readonly(frozen_sqlite.path_for(root, journal.writer_name(root)))) as db:
        db.execute("BEGIN")
        journal.qualify(db)
        row = db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError("orca_authoritative_journal_record_missing")
        raw = journal.decode(row[0])
        if raw["location"]["executionHostId"] != "local":
            raise ValueError("orca_authoritative_journal_record_required")
        return handle_chain(sid, raw)


def owner_scope(doc, root, sid):
    from .orca_cleanup import frontend_evidence
    from .orca_target_safety import evidence_for_actions
    closures = list(frontend_evidence(doc).values())
    if (len(closures) != 1 or Path(closures[0]["root"]) != root or closures[0]["session_ids"] != [sid]
            or "remote_owner" in closures[0] or len(closures[0]["records"]) != 1):
        raise ValueError("orca_authoritative_session_required")
    record, = closures[0]["records"]
    _, raw = frozen_files.read_file(root, "orca-e2ee-keypair.json", content=True, limit=8192)
    fingerprint = _key(journal.decode(raw)["publicKeyB64"])
    scope = {"completion_scope": "orca_host_session", "frontend_ids": [sid], "authority": fingerprint,
             "workspace": record["workspace_id"], "native_ids": record["native_ids"], "provider_home": record["home"]}
    if record.get("provider") == "claude":
        from .orca_claude import validate_actions
        validate_actions(doc, closures[0])
        history = closures[0]["claude_history"]
        return {**scope, "provider": "claude", "deleted_native_ids": history["delete_ids"],
                "preserved_native": history["preserved"]}
    native = evidence_for_actions(doc, doc["actions"])
    covered = set()
    for item in native:
        frozen = item["frozen"]
        if (not item["preflight_complete"] or not item["native_delete"] or Path(frozen["profile_root"]) != root
                or Path(frozen["home"]) != Path(record["home"])):
            raise ValueError("orca_authoritative_native_binding_invalid")
        covered.update(frozen["affected_thread_ids"])
    if not covered or covered != set(record["native_ids"]):
        raise ValueError("orca_complete_native_handle_chain_required")
    return scope


def _environments(root):
    _, raw = frozen_files.read_file(root, "orca-environments.json", content=True, limit=1024*1024)
    store = journal.decode(raw)
    if not isinstance(store, dict) or type(store.get("version")) is not int or store["version"] != 1 or not isinstance(store.get("environments"), list):
        raise ValueError("orca_pairing_store_invalid")
    result = {}
    for env in store["environments"]:
        identifier = env["id"]
        if not journal.text(identifier) or identifier in result or not isinstance(env.get("endpoints"), list):
            raise ValueError("orca_pairing_environment_invalid")
        keys = {_key(endpoint["publicKeyB64"]) for endpoint in env["endpoints"]}
        result[identifier] = (keys, env.get("source"))
    return result


def controller_route(root, authority):
    try:
        _, local = frozen_files.read_file(root, "orca-e2ee-keypair.json", content=True, limit=8192)
    except FileNotFoundError:
        pass
    else:
        if _key(journal.decode(local)["publicKeyB64"]) == authority:
            raise ValueError("orca_controller_and_executor_key_not_distinct")
    hosts = []
    for identifier, (keys, source) in _environments(root).items():
        if authority in keys:
            if keys != {authority} or source == "ephemeral-vm" or identifier == "unresolved-owner":
                raise ValueError("orca_pairing_environment_ambiguous")
            hosts.append("runtime:" + quote(identifier, safe="-_.!~*'()"))
    if len(hosts) != 1:
        raise ValueError("orca_paired_host_unverified")
    return hosts[0]


def controller_only(root, sid, selection):
    """Bind a controller's own copies without requesting or authorizing anything on its peer."""
    if not isinstance(selection, (list, tuple)) or len(selection) != 2 or not all(journal.text(v) for v in selection):
        raise ValueError("orca_controller_environment_and_workspace_required")
    environment, workspace = selection
    profile_id = remote.directory_identity(root)
    keys, _ = _environments(root).get(environment, (set(), None))
    if len(keys) != 1:
        raise ValueError("orca_controller_environment_and_workspace_required")
    authority, = keys
    host = controller_route(root, authority)
    if host != "runtime:" + quote(environment, safe="-_.!~*'()"):
        raise ValueError("orca_paired_host_unverified")
    pair = {"controller_only": True, "environment": environment, "root": str(root),
            "profile_id": profile_id, "authority": authority, "host": host, "workspace": workspace,
            "sid": sid, "provider": None, "provider_home": None, "native_ids": [], "deleted_native_ids": []}

    def observe(raw):
        record = parse_orca_record(sid, raw)
        if canonical_execution_host(record.host) != host:
            return
        identity = {"provider": record.provider, "provider_home": record.launched_home or record.home_locator,
                    "native_ids": sorted({h.native_id for h in record.handles}), "handle_chain_sha256": handle_chain(sid, raw)}
        if record.workspace_id != workspace or pair["provider"] and any(pair[k] != v for k, v in identity.items()):
            raise ValueError("orca_remote_journal_owner_mismatch")
        pair.update(identity)

    relative = journal.writer_name(root)
    if frontend._exists(root, relative):
        with closing(connect_readonly(frozen_sqlite.path_for(root, relative))) as db:
            db.execute("BEGIN")
            journal.qualify(db)
            for raw, in db.execute("SELECT record_json FROM agent_session_records WHERE session_id=?", (sid,)):
                observe(journal.decode(raw))
    for relative, family in frontend.sources(root)[0]:
        if family == "legacy_records":
            _, raw = frozen_files.read_file(root, relative, content=True)
            value = journal.decode(raw)
            for name in ("records", "unusableRecords"):
                if sid in value.get(name, {}):
                    item = value[name][sid]
                    observe(item if name == "records" else item["raw"])
    return pair


def from_plan(path, root, sid):
    plan = strict_json_load(path)
    owner = plan["owner"]
    if (plan.get("protocol") != remote.PROTOCOL or plan.get("goal_status") != "ready"
            or plan.get("completion_scope") != "orca_host_session" or plan.get("frontend_ids") != [sid]
            or owner.get("client") != "orca" or not plan.get("native_ids")):
        raise ValueError("orca_authoritative_session_plan_required")
    request = {"protocol": remote.PROTOCOL, "verb": "verify", "client": "orca", "destination": plan["destination"],
               "root": owner["root"], "owner": owner["store_id"], "operation_id": plan["operation_id"], "approval": plan["approval"]}
    remote._validate(request)
    return {"request": request, "root": str(root), "profile_id": remote.directory_identity(root),
            "sid": sid, "host": controller_route(root, plan["authority"]),
            **{k: plan[k] for k in ("authority", "workspace", "native_ids", "provider_home", "handle_chain_sha256")},
            **({k: plan[k] for k in ("provider", "deleted_native_ids", "preserved_native")} if "provider" in plan else {})}


def check_pairing(pair):
    root = Path(pair["root"])
    if (remote.directory_identity(root) != pair["profile_id"]
            or controller_route(root, pair["authority"]) != pair["host"]):
        raise ValueError("orca_controller_pairing_changed")


def verify(pair, *, planning=False):
    check_pairing(pair)
    if pair.get("controller_only") is True:
        if "request" in pair or pair["deleted_native_ids"] != []:
            raise ValueError("orca_controller_only_scope_invalid")
        return
    request = pair["request"]
    if request["verb"] != "verify" or request["client"] != "orca":
        raise ValueError("orca_owner_request_invalid")
    code, reply = remote.exchange({**request, "verb": "status"} if planning else request)
    if (code or reply.get("completion_scope") != "orca_host_session" or reply.get("frontend_ids") != [pair["sid"]]
            or any(reply.get(k) != pair[k] for k in ("authority", "workspace", "native_ids", "provider_home", "handle_chain_sha256"))):
        raise ValueError("orca_executor_session_unverified")
    if any(reply.get(k) != pair.get(k) for k in ("provider", "deleted_native_ids", "preserved_native")):
        raise ValueError("orca_executor_native_scope_unverified")
    if planning and reply.get("goal_status") in {"ready", "complete"}:
        return
    result = reply.get("result") or {}
    if (reply.get("goal_status") != "complete" or result.get("goal_satisfied") is not True
            or result.get("modified") is not True or not result.get("batches")
            or any(b.get("status") != "complete" for b in result["batches"])):
        raise ValueError("orca_executor_completion_unverified")


def controller_metadata(root, pair, sources):
    """An executor's local journal and an unrelated same-ID local chat may coexist."""
    selected = pair["sid"]
    copies, tabs = [], {}
    foreign_replacement = False
    def observe(sid, raw):
        nonlocal foreign_replacement
        record = parse_orca_record(sid, raw)
        owned = canonical_execution_host(record.host) == pair["host"]
        ids = {h.native_id for h in record.handles}
        if sid != selected:
            if owned:
                journal.guard_fork_source(raw, {selected}, record.host)
            elif raw.get("conversationCommand"):
                foreign_replacement |= journal.record_references(raw, {selected}) != raw
            if owned and record.provider == pair.get("provider", "codex"):
                deleted = set(pair.get("deleted_native_ids", pair["native_ids"]))
                if record.provider == "claude":
                    from .orca_claude import references_native
                    shared = deleted and references_native(raw, record, selected, deleted)
                else:
                    shared = ids & deleted
                if shared:
                    raise ValueError("orca_remote_native_handle_shared")
            return
        home = record.home_locator
        if owned and record.provider == "claude":
            home = record.launched_home or home
        if owned and (record.provider != pair.get("provider", "codex") or record.wsl_distro is not None or record.unsupported_recovery
                or home != pair["provider_home"] or raw["location"]["workspaceId"] != pair["workspace"]
                or ids != set(pair["native_ids"]) or handle_chain(sid, raw) != pair["handle_chain_sha256"]):
            raise ValueError("orca_remote_journal_owner_mismatch")
        copies.append(owned)
    relative = journal.writer_name(root)
    if frontend._exists(root, relative):
        with closing(connect_readonly(frozen_sqlite.path_for(root, relative))) as db:
            db.execute("BEGIN")
            journal.qualify(db)
            for sid, raw in journal.rows(db, "SELECT session_id,record_json FROM agent_session_records"):
                observe(sid, journal.decode(raw))
            if pair.get("deleted_native_ids"):
                from .orca_claude import journal_may_restore_native
                if journal_may_restore_native(db, set(pair["deleted_native_ids"])):
                    raise ValueError("orca_remote_native_handle_shared")
            if not copies and journal.count_remaining(db, {selected}):
                raise ValueError("orca_controller_journal_owner_unproven")
            for tab, in journal.rows(db, "SELECT tab_id FROM agent_session_tabs WHERE session_id=?", (selected,)):
                tabs[tab] = [selected]
    elif any(frontend._exists(root, relative + suffix) for suffix in ("-shm", "-wal", "-journal")):
        raise ValueError("orca_controller_journal_sidecar_unverified")
    for relative, family in sources:
        if family == "legacy_records":
            _, raw = frozen_files.read_file(root, relative, content=True)
            value = journal.decode(raw)
            for key in ("records", "unusableRecords"):
                for sid, item in value[key].items():
                    observe(sid, item["raw"] if key == "unusableRecords" else item)
            for row in value.get("sessionTabs", ()):
                if row["sessionId"] == selected:
                    tabs[row["tabId"]] = [selected]
    if True in copies and (False in copies or foreign_replacement):
        raise ValueError("orca_controller_journal_namespace_conflict")
    if True not in copies:
        if not copies and frontend._legacy_journals(root, [{"session_id": selected, "workspace_id": pair["workspace"]}]):
            raise ValueError("orca_controller_journal_owner_unproven")
        return [], {}
    return [{"session_id": selected, "workspace_id": pair["workspace"], "home": pair["provider_home"],
             "native_ids": pair["native_ids"]}], tabs


def frozen(document):
    from .orca_cleanup import frontend_evidence
    found = [v for v in frontend_evidence(document).values() if "remote_owner" in v]
    if not found:
        if "orca_controller_only" in document.get("scope", {}) and document.get("goal_status") == "ready":
            raise ValueError("orca_controller_only_evidence_required")
        return None
    e, = found
    action, = document["actions"]
    pair, scope = e["remote_owner"], document["scope"]
    if pair.get("controller_only"):
        if (pair["controller_only"] is not True or "request" in pair or pair.get("deleted_native_ids") != []
                or scope.get("orca_controller_only") != [pair["environment"], pair["workspace"]]
                or pair["host"] != "runtime:" + quote(pair["environment"], safe="-_.!~*'()")):
            raise ValueError("orca_controller_only_scope_invalid")
    elif "orca_controller_only" in scope:
        raise ValueError("orca_controller_only_scope_invalid")
    if (scope["client"] != "orca" or scope["record_ids"] != [pair["sid"]] or scope["engines"] or scope["projects"]
            or scope["all_projects"] or e["session_ids"] != [pair["sid"]] or e["root"] != pair["root"]
            or action["target"]["thread_id"] != pair["sid"] or document.get("target_safety_evidence")):
        raise ValueError("orca_remote_exact_frontend_scope_required")
    return e


def check_before_native(evidence):
    from .orca_runtime_guard import require_closed
    pair = evidence["remote_owner"]
    check_pairing(pair)
    require_closed(evidence)
    current = frontend.freeze(Path(pair["root"]), [pair["sid"]], timestamp=evidence["timestamp"], remote_owner=pair)
    if current != evidence:
        raise ValueError("orca_controller_session_changed")


def context_for(coordinator, pair, approved):
    from .adapters.orca import OrcaAdapter
    from .cleaner import ScanReport
    from .orca_cleanup import action_id
    from .office_cleanup import digest
    from .planning import CandidateAction, ActionKind, TargetRef, RiskLevel, ActionImpact, StorageLocation, ScanStatus, storage_id_for_path
    root, sid = Path(pair["root"]), pair["sid"]
    verify(pair, planning=approved is None)
    closure = frontend.freeze(root, [sid], timestamp=approved["timestamp"] if approved else None, remote_owner=pair)
    changed = (bool(closure["records"]) or any(c["evidence"]["before"]["sha256"] != c["evidence"]["after_sha256"]
        for c in closure["json_files"]) or any(c["before_sha256"] != c["after_sha256"] for c in closure["profile_databases"]))
    if not changed and not approved:
        raise ValueError("orca_controller_session_not_found")
    if approved and changed and closure != approved:
        raise ValueError("orca_controller_session_changed")
    storage = StorageLocation(storage_id_for_path(root), "Orca paired session", root, scan_status=ScanStatus.OK)
    actions = ()
    if changed:
        engine = pair.get("provider", "codex")
        if engine not in (None, "codex", "claude"):
            engine = "unsupported:" + engine
        impact = ActionImpact(owner_client="orca", owner_process_root=str(root), external_engine=engine or "orca",
            external_storage_root=str(root), resource_path=str(root), affected_thread_ids=(sid,),
            external_action_payload={"frontend_session_id": sid, "orca_frontend_evidence": closure, "requires_action_ids": []},
            frontend_references_preserved=False)
        actions = (CandidateAction(action_id(root, sid), ActionKind.DELETE_ORCA_FRONTEND, TargetRef(storage.storage_id, sid),
            RiskLevel.HIGH, True, None, impact, digest(closure), resource_kind="orca_session", requires_explicit_selection=True),)
    guards = (OrcaAdapter(profile_root=root),)
    base = coordinator.service.prepare_report(ScanReport(), active_adapters=guards, platforms=("orca",))
    plan = replace(base.plan, actions=actions, storages=(storage,), plan_fingerprint=digest(closure))
    return replace(base, plan=plan, actions=coordinator.service.typed_actions(plan)), guards, None, None, {}, {}
