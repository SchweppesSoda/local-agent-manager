"""Read-only absence proof for a profile with no persisted agent sessions.

This is deliberately narrower than an Orca reference reader. A missing journal
alone proves nothing. Every registered profile/recovery copy must qualify, and
any session-bearing state keeps the original guard errors. No writer capability
or runtime-closure acknowledgement is granted by this proof.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from . import frozen_files, frozen_sqlite, orca_frontend as frontend, orca_profile_cleanup as profile
from .orca_frontend_json import mapping, sequence, workspace, legacy_state
from .orca_journal_cleanup import decode, fail, rows, OrcaFrontendError
from .sqlite_utils import connect_readonly


COVERED_ERRORS = frozenset({"journal_unavailable", "agent_hooks_namespaces_not_covered",
                            "legacy_or_profile_restore_source_not_read"})
# Known profile JSON v1 domains / SQLite v3 projections. Unknown domains are
# not interpreted as harmless settings, even when their values look empty.
DOMAINS = frozenset("""
schemaVersion repos projects projectHostSetups projectGroups folderWorkspaces
sparsePresetsByRepo retiredWorktreeNamesByRepo retiredWorktreeNamesByNamespace
worktreeMeta worktreeLineageById workspaceLineageByChildKey settings ui
workspaceSession workspaceSessionsByHostId sshTargets sshTargetGenerationCounter
deletedSshConfigAliases sshRemotePtyLeases sshPtyConsumerRecoveries
claudeLivePtySessionIds migrationUnsupportedPtyEntries legacyPaneKeyAliasEntries
automations automationRuns onboarding featureInteractionTelemetryBuckets
codexResetCreditAttemptLedger worktreeMetaByIdentity worktreeIdentityAliases
mobileClientTabSelectionsByDeviceId
""".split())
EMPTY_DOMAINS = frozenset("""
sshRemotePtyLeases sshPtyConsumerRecoveries claudeLivePtySessionIds
migrationUnsupportedPtyEntries legacyPaneKeyAliasEntries automations automationRuns
mobileClientTabSelectionsByDeviceId
""".split())
TERMINAL_FIELDS = frozenset("""
id worktreeId title customTitle defaultTitle color createdAt sortOrder ptyId shellOverride
""".split())
UNIFIED_FIELDS = frozenset("""
id entityId groupId worktreeId contentType label customLabel color sortOrder createdAt
isPreview lastFocusedAt executionHostId
""".split())


def _empty(value):
    if value is not None and value != {} and value != []:
        fail("guard_session_state_present")


def _workspace(value):
    workspace(value, set(), (), ())  # Enforce the registered workspace fields.
    for key in ("sleepingAgentSessionsByPaneKey", "remoteSessionIdsByTabId",
                "localOnlyScrollbackByTabId", "terminalSurfaceTombstonesByPaneKey",
                "closedTerminalTabTombstonesByTabId"):
        _empty(value.get(key))
    terminals = set()
    for owner, tabs in mapping(value.get("tabsByWorktree", {})).items():
        for tab in sequence(tabs):
            tab = mapping(tab)
            if (set(tab) - TERMINAL_FIELDS or tab.get("worktreeId") != owner
                    or not isinstance(tab.get("id"), str) or not tab["id"]
                    or tab["id"] in terminals
                    or tab.get("shellOverride") not in (None, "", "powershell.exe", "pwsh.exe", "cmd.exe")):
                fail("guard_terminal_restore_unverified")
            terminals.add(tab["id"])
    for tab_id, layout in mapping(value.get("terminalLayoutsByTabId", {})).items():
        layout = mapping(layout)
        leaf = mapping(layout.get("root"))
        if (tab_id not in terminals or set(layout) - {"root", "activeLeafId", "expandedLeafId", "ptyIdsByLeafId"}
                or set(leaf) != {"type", "leafId"} or leaf["type"] != "leaf"
                or not isinstance(leaf["leafId"], str) or not leaf["leafId"]
                or layout.get("activeLeafId") != leaf["leafId"]
                or layout.get("expandedLeafId") not in (None, leaf["leafId"])
                or set(mapping(layout.get("ptyIdsByLeafId", {}))) - {leaf["leafId"]}):
            fail("guard_terminal_layout_unverified")
        if any(not isinstance(value, str) for value in layout.get("ptyIdsByLeafId", {}).values()):
            fail("guard_terminal_layout_unverified")
    for tabs in mapping(value.get("unifiedTabs", {})).values():
        for tab in sequence(tabs):
            if (set(tab) - UNIFIED_FIELDS or tab.get("contentType") != "terminal"
                    or tab.get("entityId") not in terminals
                    or tab.get("executionHostId", "local") != "local"):
                fail("guard_agent_tab_present")


def _no_binding_objects(value):
    # Reject explicit bindings even in otherwise passive domains. Values are
    # never returned/logged; do not scan arbitrary native files or transcripts.
    pending = [value]
    binding_keys = {"providerSession", "providerHandleChain", "accountHome", "aiVaultTitle",
                    "sessionId", "session_id", "sdkSessionId", "sdk_session_id", "resumeSessionId"}
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if binding_keys & item.keys():
                fail("guard_binding_object_present")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _profile(value):
    value = mapping(value)
    if set(value) - DOMAINS or value.get("schemaVersion") != 1 or "workspaceSession" not in value:
        fail("guard_profile_domains_unverified")
    for key in EMPTY_DOMAINS:
        _empty(value.get(key))
    _workspace(mapping(value["workspaceSession"]))
    for state in mapping(value.get("workspaceSessionsByHostId", {})).values():
        _workspace(mapping(state))
    settings = mapping(value.get("settings", {}))
    _empty(settings.get("nativeChatSessionOptions"))
    ui = mapping(value.get("ui", {}))
    for key in ("acknowledgedAgentsByPaneKey", "activityClearedAtByPaneKey", "manuallyUnreadTurnsByPaneKey"):
        _empty(ui.get(key))
    _no_binding_objects(value)


def _observe(root):
    # Missing/unreadable journals are different. This proof accepts absence
    # only, including absence of sidecars, restore copies and old journal dirs.
    for name in frontend._names(root):
        if name.startswith("agent-session-journal"):
            fail("guard_journal_present")
    json_sources, databases = frontend.sources(root)
    indexes = frontend._indexes(root)
    current_index = next(item for item in indexes if item["path"] == "orca-profile-index.json")
    if current_index.get("absent"):
        fail("guard_profile_index_required")
    _, raw = frozen_files.read_file(root, "orca-profile-index.json", content=True, limit=16 * 1024 * 1024)
    indexed = {entry["id"] for entry in decode(raw)["profiles"]}
    directories = set(frontend._names(root, "profiles"))
    if indexed != directories or not indexed:
        fail("guard_profile_membership_unverified")
    # All index backups must refer to the same complete observed directories.
    for item in indexes:
        if not item.get("absent"):
            _, raw = frozen_files.read_file(root, item["path"], content=True, limit=16 * 1024 * 1024)
            if {entry["id"] for entry in decode(raw)["profiles"]} != directories:
                fail("guard_profile_membership_unverified")
    for identifier in indexed:
        if f"profiles/{identifier}/profile-state.db" not in databases:
            fail("guard_profile_database_required")
    observations = [indexes, frontend._hooks(root, (), require_empty=True)]
    for relative, family in json_sources:
        observation, raw = frozen_files.read_file(root, relative, content=True, limit=16 * 1024 * 1024)
        value = decode(raw)
        if family == "profile_json":
            _profile(value)
        else:
            legacy_state(value, set())
            for key in ("records", "unusableRecords", "operations", "sessionTabs", "visibleSessionIds", "retiredClaimKeys"):
                _empty(value.get(key))
        observations.append(observation)
    for relative in databases:
        path = frozen_sqlite.path_for(root, relative)
        before = frozen_files._revision(path.lstat())
        with closing(connect_readonly(path)) as db:
            db.execute("BEGIN")
            profile.qualify(db)
            if profile.meta(db)[0]["profile_id"] != relative.split("/")[1]:
                fail("guard_profile_identity_unverified")
            if db.execute("SELECT 1 FROM profile_state_automation_runs LIMIT 1").fetchone():
                fail("guard_automation_runs_present")
            _profile({domain: decode(payload) for domain, payload in rows(db, "SELECT domain,payload FROM profile_state_documents")})
            digest = frozen_sqlite.logical_hash(db)
        if before != frozen_files._revision(path.lstat()):
            fail("guard_database_changed")
        observations.append({"path": relative, "identity": before, "sha256": digest})
    return json_sources, databases, observations


def proves_no_sessions(root: Path) -> bool:
    """Fresh, bounded proof; a cached successful result must never be reused."""
    try:
        first = _observe(root)
        return first == _observe(root)
    except (OSError, ValueError, sqlite3.Error, OrcaFrontendError,
            frozen_files.FrozenFilesError, frozen_sqlite.FrozenSQLiteError):
        return False
