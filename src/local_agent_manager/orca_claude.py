"""Bind a released Orca chat to the Claude transcript its last owner published."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from . import frozen_files, frozen_sqlite, orca_frontend as frontend, orca_journal_cleanup as journal
from .orca_metadata import DOMAIN_JOURNAL, parse_orca_record
from .path_identity import is_local_absolute_locator
from .record_identity import canonical_path
from .sqlite_utils import connect_readonly


def metadata(sid, raw):
    from .orca_remote import handle_chain
    from .remote_records import directory_identity
    chain = handle_chain(sid, raw)
    record = parse_orca_record(sid, raw)
    if not record.handles or record.host != "local":
        journal.fail("claude_local_handle_required")
    home = record.launched_home
    ids = sorted({h.native_id for h in record.handles})
    native = str(UUID(bytes=hashlib.sha256(("orca-claude:" + sid).encode()).digest()[:16], version=4))
    reason = None
    if home is None or not is_local_absolute_locator(home):
        reason = "launch_account_unproven"
    elif (ids != [native] or record.handles[0].origin != "created" or raw.get("forkedFrom")
            or raw.get("providerContextBoundary")):
        reason = "fork_adoption_or_context_change"
    elif record.handles[-1].leaf_uuid is None:
        reason = "head_cursor_missing"
    info = {"session_id": sid, "provider": "claude", "workspace_id": raw["location"]["workspaceId"],
            "native_ids": ids, "home": home or record.home_locator, "chain": chain,
            "cursors": [h.leaf_uuid for h in record.handles if h.leaf_uuid is not None]}
    if "launchDirectory" in raw:
        info["launch_directory"] = raw["launchDirectory"]
    if not reason:
        try:
            info["home_id"] = directory_identity(Path(home))
        except FileNotFoundError:
            reason = "launch_home_absent"
    if reason:
        info["preserve_reason"] = reason
    return info


def references_native(raw, record, sid, native_ids):
    return (bool(sid and (raw.get("forkedFrom") or {}).get("sessionId") == sid)
            or any(h.native_id in native_ids for h in record.handles) or bool(raw.get("rewind")))


def journal_may_restore_native(db, native_ids):
    if db.execute("PRAGMA user_version").fetchone()[0] != 2:
        return False
    # A retired original can contain an earlier launch record. History origins
    # retain two provider handles without independent host columns.
    if db.execute("SELECT 1 FROM agent_session_store_meta WHERE key GLOB 'journal.legacy-original:*' LIMIT 1").fetchone():
        return True
    for raw, in journal.rows(db, "SELECT history_origin_json FROM journal_chat_state WHERE history_origin_json IS NOT NULL"):
        origin = journal.decode(raw)
        if not isinstance(origin, dict):
            return True
        for part in (origin, origin.get("raw")):
            handle = part.get("providerHandle") if isinstance(part, dict) else None
            if not isinstance(handle, dict):
                return True
            if handle.get("kind") == "codex" and isinstance(handle.get("threadId"), str):
                continue
            if handle.get("kind") != "claude" or not isinstance(handle.get("sessionId"), str) or handle["sessionId"] in native_ids:
                return True
    return False


def _shared(root, sid, native):
    def conflict(other, raw):
        if other == sid:
            return False
        record = parse_orca_record(other, raw)
        if record.host != "local" or record.wsl_distro is not None or record.provider != "claude":
            return False
        return references_native(raw, record, sid, {native})

    from .orca_empty_guard import proves_no_sessions
    if sid is None and proves_no_sessions(root):
        return False
    with closing(connect_readonly(frozen_sqlite.path_for(root, journal.writer_name(root)))) as db:
        db.execute("BEGIN")
        journal.qualify(db)
        if any(conflict(other, journal.decode(raw)) for other, raw in journal.rows(db,
               "SELECT session_id,record_json FROM agent_session_records")):
            return True
        if journal_may_restore_native(db, {native}):
            return True
    for relative, family in frontend.sources(root)[0]:
        if family == "legacy_records":
            _, raw = frozen_files.read_file(root, relative, content=True)
            legacy = journal.decode(raw)
            for key in ("records", "unusableRecords"):
                if any(conflict(other, value["raw"] if key == "unusableRecords" else value)
                       for other, value in legacy[key].items()):
                    return True
    return False


def _transcript(record, cursors, launch_directory):
    """A cursor identifies an entry, and cannot authorize another branch or an appended tail."""
    if len(record.transcript_paths) != 1 or len(record.manifest) != 1 or record.frontend_references:
        return None
    entry, = record.manifest
    parts = Path(entry.relative_path).parts
    if len(parts) != 3 or parts[0] != "projects" or parts[-1] != record.session_id + ".jsonl":
        return None
    observed, data = frozen_files.read_file(record.config_dir, entry.relative_path, content=True,
                                           limit=frozen_files.MAX_REWRITE_BYTES)
    if observed["sha256"] != entry.sha256 or not data.endswith(b"\n"):
        return None
    order, metadata_cursors, previous = {}, set(), None
    for line in data.splitlines():
        if not line.strip():
            continue
        row = journal.decode(line)
        if not isinstance(row, dict):
            return None
        uid = row.get("uuid")
        kind = row.get("type")
        if row.get("sessionId", record.session_id) != record.session_id or row.get("isSidechain") or row.get("parent_tool_use_id") is not None:
            return None
        if launch_directory is not None and "cwd" in row and row["cwd"] != launch_directory:
            return None
        if uid is None:
            if kind == "file-history-snapshot" and isinstance(row.get("snapshot"), dict):
                cursor = row.get("messageId")
                if row["snapshot"].get("messageId") != cursor:
                    return None
            elif kind == "last-prompt" and row.get("sessionId") == record.session_id:
                cursor = row.get("leafUuid")
            elif kind == "agent-name" and row.get("sessionId") == record.session_id:
                continue
            else:
                return None
            if not isinstance(cursor, str) or not cursor:
                return None
            metadata_cursors.add(cursor)
            continue
        parent = row.get("parentUuid")
        if (not isinstance(uid, str) or not uid or uid in order or row.get("sessionId") != record.session_id
                or (parent if isinstance(parent, str) else row.get("logicalParentUuid")) != previous
                or kind not in {"user", "assistant", "system", "attachment"}
                or previous is None and kind != "user"):
            return None
        order[uid] = len(order)
        previous = uid
    if not cursors or cursors[-1] != previous or not metadata_cursors <= order.keys() or any(c not in order for c in cursors):
        return None
    positions = [order[c] for c in cursors]
    return observed if positions == sorted(positions) else None


def _catalog(home):
    from .session_catalog_factory import build_claude_catalog
    catalog = build_claude_catalog(SimpleNamespace(claude_config_dir=Path(home)))
    if catalog.errors:
        journal.fail("claude_native_catalog_incomplete")
    return catalog


def remaining(history):
    if not history["delete_ids"]:
        return False
    from .herdr_cleanup import native_remaining
    from .remote_records import directory_identity
    if directory_identity(Path(history["home"])) != history["home_id"]:
        journal.fail("claude_home_changed")
    binding = {"engine": "claude", "root": history["home"]}
    return native_remaining({**binding, "binding": binding, "ids": history["delete_ids"]})


def validate_actions(doc, closure):
    from .orca_cleanup import KIND
    history = closure["claude_history"]
    record, = closure["records"]
    sid, = closure["session_ids"]
    actions = [a for a in doc["actions"] if a["kind"] != KIND]
    scope = doc["scope"]
    if (scope["client"] != "orca" or scope["record_ids"] != [sid] or scope["projects"] or scope["all_projects"]
            or scope["engines"] not in ([], ["claude"]) or record["session_id"] != sid or record.get("provider") != "claude"
            or history["home"] != record["home"] or len(doc["actions"]) != 1 + len(actions)
            or sorted([*history["delete_ids"], *history["preserved"]]) != record["native_ids"]
            or sorted(a["target"]["thread_id"] for a in actions) != history["delete_ids"]):
        journal.fail("claude_action_scope_changed")
    for action in actions:
        native = action["target"]["thread_id"]
        impact = action["impact"]
        transcript = history["transcript_file"]
        path = Path(history["home"]) / transcript["path"]
        payload = impact["external_action_payload"]
        entry, = payload["manifest"]
        storage, = [s for s in doc["storages"] if s["storage_id"] == action["target"]["storage_id"]]
        if (action["kind"] != "delete_claude_session" or native != path.stem or record.get("preserve_reason")
                or Path(storage["path"]) != Path(history["home"]) or payload["action_id"] != action["action_id"]
                or impact["affected_thread_ids"] != [native] or impact.get("descendant_thread_ids")
                or impact["external_engine"] != "claude" or payload["session_id"] != native
                or Path(impact["external_storage_root"]) != Path(history["home"])
                or Path(payload["config_dir"]) != Path(history["home"]) or payload["frontend_reference_snapshot"]
                or list(map(Path, impact["external_artifact_paths"])) != [path]
                or list(map(Path, payload["transcript_paths"])) != [path]
                or entry["relative_path"] != transcript["path"] or Path(entry["path"]) != path or entry["node_type"] != "file"
                or [entry["stat"]["dev"], entry["stat"]["ino"]] != transcript["identity"]
                or entry["sha256"] != transcript["sha256"] or entry["stat"]["size"] != transcript["size"]
                or history["home_id"] != record.get("home_id")):
            journal.fail("claude_native_action_changed")
    return [a["action_id"] for a in actions]


def build_context(coordinator, adapter, guards, sid, *, document):
    from .cleaner import ScanReport
    from .office_cleanup import digest
    from .orca_authorization import coordinator_scope
    from .orca_cleanup import action_id, frontend_evidence, KIND
    from .orca_remote import authoritative_chain
    from .client_contracts import describe_adapter
    from .planning import CandidateAction, ActionKind, ActionImpact, TargetRef, StorageLocation, ScanStatus, RiskLevel, storage_id_for_path
    root = adapter.profile_root
    if journal.writer_name(root) != DOMAIN_JOURNAL:
        journal.fail("claude_authoritative_domain_journal_required")
    old = frontend_evidence(document or {}).get(canonical_path(root))
    closure = frontend.freeze(root, [sid], timestamp=old["timestamp"] if old else None)
    if len(closure["records"]) != 1 or closure["records"][0].get("chain") != authoritative_chain(root, sid):
        journal.fail("claude_journal_copies_disagree")
    record, = closure["records"]
    ids, home = record["native_ids"], record["home"]
    reason = record.get("preserve_reason")
    actions, bindings = [], {}
    base = coordinator.service.prepare_report(ScanReport(), active_adapters=(), platforms=("orca",))
    history = {"home": home, "delete_ids": [], "preserved": {}}
    if not reason:
        native, = ids
        other_roots = sorted({str(a.profile_root) for a in guards
            if describe_adapter(a).client == "orca" and a.profile_root != root and a.profile_root.exists()})
        if _shared(root, sid, native) or any(_shared(Path(p), None, native) for p in other_roots):
            reason = "shared_native_history"
        else:
            catalog = _catalog(home)
            matches = [r for r in catalog.records if r.session_id == native and r.config_dir == Path(home) and r.manifest]
            transcript = _transcript(matches[0], record["cursors"], record.get("launch_directory")) if len(matches) == 1 else None
            if transcript is not None:
                context = coordinator.service.prepare_session_catalog("claude", catalog, catalog_builder=lambda: _catalog(home),
                                                                      target_root=Path(home))
                action, = [a for a in context.plan.actions if a.target.thread_id == native]
                actions.append(action)
                bindings[str(action.action_id)] = context
                base = coordinator._merge_cleanup_contexts(base, (context,))
                history.update(delete_ids=[native], transcript_file=transcript, home_id=record["home_id"],
                               other_orca_roots=other_roots)
            elif old and old["claude_history"]["delete_ids"] == [native] and not remaining(old["claude_history"]):
                history = old["claude_history"]
            else:
                reason = "transcript_or_cursor_not_exclusive"
    if reason:
        history["preserved"] = dict.fromkeys(ids, reason)
    before = closure
    closure = frontend.freeze(root, [sid], timestamp=before["timestamp"], claude_history=history)
    if any(closure[key] != value for key, value in before.items() if key != "covered_errors") or old and old != closure:
        journal.fail("claude_approved_session_changed")
    storage = StorageLocation(storage_id_for_path(root), "Orca structured Claude chat", root, scan_status=ScanStatus.OK)
    deps = (validate_actions(document, closure) if old else sorted(bindings))
    payload = {"frontend_session_id": sid, "orca_frontend_evidence": closure, "requires_action_ids": deps}
    actions.append(CandidateAction(action_id(root, sid), ActionKind(KIND), TargetRef(storage.storage_id, sid),
        RiskLevel.HIGH, True, None, ActionImpact(owner_client="orca", owner_process_root=str(root),
        external_engine="claude", external_storage_root=str(root), resource_path=str(root), affected_thread_ids=(sid,),
        external_action_payload=payload, frontend_references_preserved=False), digest(closure),
        resource_kind="orca_session", requires_explicit_selection=True))
    plan = replace(base.plan, actions=tuple(actions), storages=(*base.plan.storages, storage), plan_fingerprint=digest(closure))
    context = replace(base, plan=plan, actions=coordinator.service.typed_actions(plan))
    with coordinator_scope((), frontend_closures=(closure,)):
        context, bindings = coordinator._restrict_context_bindings(context, bindings, guards)
    return context, context.active_adapters, None, None, {}, bindings


def recheck_before_native(closure):
    root = Path(closure["root"])
    sid, = closure["session_ids"]
    history = closure["claude_history"]
    native, = history["delete_ids"]
    current = frontend.freeze(root, [sid], timestamp=closure["timestamp"], claude_history=history)
    if (current != closure or _shared(root, sid, native)
            or any(_shared(Path(p), None, native) for p in history["other_orca_roots"])):
        journal.fail("claude_approved_session_changed")
    observed, _ = frozen_files.read_file(Path(history["home"]), history["transcript_file"]["path"])
    if observed != history["transcript_file"]:
        journal.fail("claude_transcript_changed")
