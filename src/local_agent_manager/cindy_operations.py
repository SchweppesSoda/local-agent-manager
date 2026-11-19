"""Cindy frontend preflight shared by scoped operation planning.

One collection guard per database validates the frozen native/reference closure.
It does not open app-servers, scan another catalog or mutate a live store.
"""

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .claude_delete import CINDY_APPROVED_CURRENT_REFERENCE
from .claude_sessions import ClaudeMultiRootCatalog
from .frontend_reference_cleanup import guard_frontend_reference_closure
from .frontend_session_cleanup import _require_client_closed, guard_cindy_session_rows
from .record_identity import canonical_path


def qualify_claude_current_references(
    catalog: Any, frontend_actions: Sequence[Any], explicit_ids: Sequence[str],
) -> Any:
    """Permit one root-qualified current reference only with both exact IDs selected."""
    if not isinstance(catalog, ClaudeMultiRootCatalog) or not explicit_ids:
        return catalog
    selected = set(explicit_ids)
    approved = {
        (canonical_path(evidence["database"]), evidence["session_id"]): (action.action_id, evidence)
        for action in frontend_actions
        if action.kind == "delete_frontend_session" and action.available
        and action.impact.external_engine == "claude"
        for evidence in action.impact.frontend_session_evidence
        if evidence["explicitly_selected"] and evidence["expected_status"] == "active"
        and evidence["session_id"] in selected and evidence["expected_sdk_session_id"] in selected
    }
    if not approved:
        return catalog
    native_id_counts = Counter(record.session_id for record in catalog.records)

    def qualify(record: Any) -> Any:
        if (record.session_id not in selected or native_id_counts[record.session_id] != 1
                or record.classification != "live_current_reference"
                or record.blockers or not record.manifest or len(record.frontend_references) != 1
                or len(record.frontend_reference_snapshot) != 1):
            return record
        raw, = record.frontend_references
        snapshot, = record.frontend_reference_snapshot
        if (not isinstance(raw, Mapping) or not raw.get("database")
                or raw.get("session_status") != "active" or raw.get("reference_kind") != "current"
                or raw.get("backend") != "claude" or raw.get("native_session_id") != record.session_id
                or snapshot.get("root_qualified") is not True):
            return record
        binding = approved.get((canonical_path(raw["database"]), raw.get("cindy_session_id")))
        if binding is None:
            return record
        action_id, evidence = binding
        if (evidence["expected_sdk_session_id"] != record.session_id
                or raw.get("session_row_fingerprint") != evidence["session_row_fingerprint"]):
            return record
        return replace(record, classification=CINDY_APPROVED_CURRENT_REFERENCE, deletable=True,
                       frontend_reference_snapshot=({**snapshot, "cindy_frontend_action_id": action_id},))

    return replace(catalog, catalogs=tuple(
        replace(root, records=tuple(qualify(record) for record in root.records))
        for root in catalog.catalogs
    ))


def claude_frontend_action_id(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None or payload.get("classification") != CINDY_APPROVED_CURRENT_REFERENCE:
        return None
    reference, = payload["frontend_reference_snapshot"]
    return str(reference["cindy_frontend_action_id"])


def guard_paired_claude_frontend(
    actions: Sequence[Any], frozen_actions: Sequence[Mapping[str, Any]], client_inspector: Any,
) -> bool:
    """Recheck the exact Cindy row and owning client before native mutation."""
    frontend = {action["action_id"]: action for action in frozen_actions if action["kind"] == "delete_frontend_session"}
    paired = False
    for native in actions:
        action_id = claude_frontend_action_id(native.impact.external_action_payload)
        if action_id is None:
            continue
        if client_inspector is None:
            raise ValueError("Paired Claude deletion requires Cindy process inspection")
        action = frontend[action_id]
        impact = action["impact"]
        _require_client_closed(Path(impact["owner_process_root"]), client_inspector, owner_client="cindy")
        guard_cindy_session_rows(impact["frontend_session_evidence"])
        paired = True
    return paired


def preflight_frontend_actions(actions: Sequence[Any]) -> list[dict[str, Any]]:
    databases: dict[str, dict[str, Mapping[str, Any]]] = {}
    action_ids: dict[str, list[str]] = {}
    for action in actions:
        for evidence in getattr(action.impact, "frontend_reference_evidence", ()):
            if evidence.get("platform") != "cindy":
                continue
            database = canonical_path(str(evidence.get("database") or ""))
            # Identical descendant reference actions may occur in two selected
            # root closures. Preserve each exact row once without widening it.
            key = json.dumps(evidence, sort_keys=True)
            databases.setdefault(database, {})[key] = evidence
            action_ids.setdefault(database, []).append(str(action.action_id))
    blockers = []
    for database, evidence in databases.items():
        try:
            guard_frontend_reference_closure(tuple(evidence.values()))
        except Exception as exc:
            blockers.append({
                "blocker_code": "frontend_preflight_blocked",
                "scope": "frontend:" + database,
                "severity": "error",
                "retryable": True,
                "action_ids": sorted(set(action_ids[database])),
                "message": str(exc),
            })
    return blockers
