"""Read Orca's shared journal metadata projection.

Only agent_session_records/tabs/store_meta are queried. Journal/chat rows,
operations, credentials and launch arguments are never returned or executed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from urllib.parse import quote, unquote
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client_contracts import SourceFailure
from .sqlite_utils import connect_readonly

UPSTREAM_REVISION = "1c61fb73e5ddbacbe1465d2e0506483f57017c5f"
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_LINK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_NAMESPACE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_HOME_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PROFILE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)
_MAX_RECORD_BYTES = 1024 * 1024
DOMAIN_JOURNAL = "agent-session-domain.db"
LEGACY_JOURNAL = "agent-session-journal.db"


def journal_name(root: Path) -> str:
    """The host opens the domain store first. Never fall back past an orphan WAL."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            (root / (DOMAIN_JOURNAL + suffix)).lstat()
        except FileNotFoundError:
            continue
        return DOMAIN_JOURNAL
    return LEGACY_JOURNAL


class OrcaMetadataError(ValueError):
    """Fixed diagnostic codes, never raw persisted JSON or parser excerpts."""


@dataclass(frozen=True)
class OrcaHandle:
    link_id: str
    provider: str
    native_id: str
    leaf_uuid: str | None
    origin: str
    transport: str


@dataclass(frozen=True)
class OrcaLease:
    """Minimum lifecycle/owner metadata; no spawn token or launch options."""

    claim_status: str
    unreconciled: bool
    runtime_kind: str
    handoff_stage: str | None
    owner_host: str | None
    owner_pid: int | None
    owner_start_time_ms: int | None
    death_kind: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"claim_status": self.claim_status, "unreconciled": self.unreconciled,
                "runtime_kind": self.runtime_kind, "handoff_stage": self.handoff_stage,
                "owner_host": self.owner_host, "owner_pid": self.owner_pid,
                "owner_start_time_ms": self.owner_start_time_ms, "death_kind": self.death_kind}


@dataclass(frozen=True)
class OrcaRecord:
    session_id: str
    provider: str
    host: str
    workspace_id: str
    wsl_distro: str | None
    home_variable: str | None
    home_locator: str | None
    handles: tuple[OrcaHandle, ...]
    unsupported_recovery: bool
    lease: OrcaLease
    launched_home: str | None = None
    account_locator: dict[str, str] | None = None

    @property
    def execution_route(self) -> str:
        if self.host == "local":
            return "wsl" if self.wsl_distro is not None else "local"
        return (canonical_execution_host(self.host) or "unknown").partition(":")[0]


def canonical_execution_host(host):
    if host == "local":
        return host
    if not isinstance(host, str):
        return None
    kind, _, encoded = host.partition(":")
    if kind not in {"runtime", "ssh"} or not encoded or "|" in encoded or re.search(r"%(?![0-9a-fA-F]{2})", encoded):
        return None
    try:
        identifier = unquote(encoded, errors="strict")
        return kind + ":" + quote(identifier, safe="-_.!~*'()")
    except UnicodeError:
        return None


def _text(value: Any, maximum: int = 512) -> bool:
    # Upstream bounds JavaScript UTF-16 code units, not Python code points.
    return (isinstance(value, str) and 0 < len(value.encode("utf-16-le", errors="surrogatepass")) // 2 <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _integer(value: Any, minimum: int | None = None) -> bool:
    return type(value) is int and abs(value) <= 2**53 - 1 and (minimum is None or value >= minimum)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise OrcaMetadataError(code)


def _handle(value: Any) -> tuple[OrcaHandle, str, str, str | None]:
    _require(isinstance(value, dict), "provider_handle_invalid")
    handle = value.get("handle")
    _require(isinstance(handle, dict), "provider_handle_invalid")
    provider = handle.get("provider")
    if "provider" in handle:
        _require(provider in ("codex", "claude") and not ({"agent", "transport", "nativeId", "resumeCursor"} & handle.keys()),
                 "provider_handle_invalid")
        transport = "codex-app-server" if provider == "codex" else "claude-sdk"
        native = handle.get("threadId") if provider == "codex" else handle.get("sessionId")
        cursor = handle.get("leafUuid") if provider == "claude" else None
        _require(provider != "claude" or "leafUuid" in handle and (cursor is None or _text(cursor) and cursor == cursor.strip()),
                 "provider_handle_invalid")
        namespace = provider
    else:
        provider, transport, native = handle.get("agent"), handle.get("transport"), handle.get("nativeId")
        _require(all(isinstance(v, str) and _NAMESPACE.fullmatch(v) for v in (provider, transport))
                 and provider not in ("codex", "claude"), "provider_handle_invalid")
        cursor = handle.get("resumeCursor")
        _require("resumeCursor" not in handle or isinstance(cursor, str)
                 and 0 < len(cursor.encode("utf-16-le", errors="surrogatepass")) // 2 <= 4096, "provider_handle_invalid")
        namespace = f"{transport}/{provider}"
    _require(_text(native) and native == native.strip(), "provider_handle_invalid")
    _require(isinstance(value.get("linkId"), str) and bool(_LINK_ID.fullmatch(value["linkId"])), "provider_link_invalid")
    _require(value.get("origin") in {"created", "adopted", "resumed", "forked"}, "provider_link_invalid")
    _require(_integer(value.get("mintedAtFence"), 0) and _integer(value.get("observedAt")), "provider_link_invalid")
    root = namespace + ":" + json.dumps(native, ensure_ascii=False)
    key = "claude:" + json.dumps([native, cursor], ensure_ascii=False, separators=(",", ":")) if provider == "claude" else root
    # Opaque resume cursors participate in retry detection but never leave this parser.
    return OrcaHandle(value["linkId"], provider, native, cursor if provider == "claude" else None,
                      value["origin"], transport), key, root, cursor


def _account_home(home, provider):
    _require(isinstance(home, dict), "record_account_home_invalid")
    if "kind" in home:
        locator = home.get("locator")
        _require(provider not in ("codex", "claude") and home["kind"] == "opencode" and isinstance(locator, dict),
                 "record_account_home_invalid")
        if locator.get("kind") == "unmanaged":
            return None, None, {"kind": "unmanaged"}
        identifier = locator.get("managedProfileId")
        _require(locator.get("kind") == "managed" and isinstance(identifier, str) and _PROFILE_ID.fullmatch(identifier),
                 "record_account_home_invalid")
        return None, None, {"kind": "managed", "managedProfileId": identifier}
    variable, path = home.get("variable"), home.get("path")
    _require(isinstance(variable, str) and _HOME_VARIABLE.fullmatch(variable) and _text(path, 4096)
             and variable == {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}.get(provider, variable),
             "record_account_home_invalid")
    return variable, path, None


def launch_home(raw):
    """A founding home can differ from the home reported by the acquired SDK."""
    launch = raw.get("launchedAccount")
    chain, lease = raw["providerHandleChain"], raw["lease"]
    if not isinstance(launch, dict) or not chain:
        return None
    fence, home = launch.get("fence"), launch.get("accountHome")
    if (type(fence) is not int or not 0 < fence <= lease["runtimeFence"] or fence != chain[-1]["mintedAtFence"]
            or not isinstance(home, dict) or "kind" in home or home.get("variable") != "CLAUDE_CONFIG_DIR"
            or not _text(home.get("path"), 4096)):
        return None
    if "acquiredAccount" in raw:
        receipt = raw["acquiredAccount"]
        location = raw["location"]
        # A mirrored record can retain the executing host's original local scope.
        scope_suffix = "\0" + (location["wslDistro"] or "") + "\0" + location["workspaceId"]
        boundary = raw.get("providerContextBoundary") or {}
        if (not isinstance(receipt, dict) or set(receipt) != {"version", "hostId", "sessionId", "scope", "context",
                "fence", "observedThroughFence", "generation", "spawnToken", "recipe"}
                or type(receipt.get("version")) is not int or receipt["version"] != 1
                or any(not isinstance(receipt[k], str) or not 0 < len(receipt[k]) <= 512 for k in ("hostId", "generation", "spawnToken"))
                or receipt["hostId"] not in ("local", location["executionHostId"])
                or receipt.get("scope") != receipt["hostId"] + scope_suffix or receipt.get("sessionId") != raw["sessionId"]
                or type(receipt.get("fence")) is not int or receipt["fence"] != fence
                or type(receipt.get("observedThroughFence")) is not int or receipt["observedThroughFence"] != lease["runtimeFence"]
                or not isinstance(boundary, dict) or receipt.get("context") != boundary.get("operationId")
                or not isinstance(receipt.get("recipe"), dict) or receipt["recipe"].get("home") != home["path"]):
            return None
        recipe = receipt["recipe"]
        if recipe.get("route") == "system":
            if set(recipe) != {"route", "home", "configDir"} or recipe["configDir"] not in (None, home["path"]):
                return None
        elif (recipe.get("route") != "managed" or set(recipe) != {"route", "home", "accountId"}
              or not isinstance(recipe["accountId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", recipe["accountId"])):
            return None
    return home["path"]


def parse_orca_record(row_id: Any, value: Any) -> OrcaRecord:
    _require(isinstance(value, dict) and type(value.get("schemaVersion")) is int and value["schemaVersion"] == 2, "record_schema_unsupported")
    session_id = value.get("sessionId")
    _require(isinstance(session_id, str) and bool(_SESSION_ID.fullmatch(session_id)) and row_id == session_id, "record_identity_invalid")
    location, home, lease = (value.get(key) for key in ("location", "accountHome", "lease"))
    _require(isinstance(location, dict) and _text(location.get("executionHostId"))
        and "wslDistro" in location and (location["wslDistro"] is None or _text(location["wslDistro"]))
        and _text(location.get("workspaceId")) and location.get("workspaceKind") in {"folder", "git-worktree"}, "record_location_invalid")
    provider = value.get("provider")
    _require(isinstance(provider, str) and bool(_NAMESPACE.fullmatch(provider)), "record_provider_unsupported")
    home_variable, home_locator, account_locator = _account_home(home, provider)
    chain = value.get("providerHandleChain")
    _require(isinstance(chain, list) and len(chain) <= 256, "provider_chain_invalid")
    handles: list[OrcaHandle] = []
    link_ids: set[str] = set()
    previous_key = previous_root = previous_cursor = None
    key_limit = 512 if provider in ("codex", "claude") else 8192
    previous_fence = -1
    for link in chain:
        handle, key, root, cursor = _handle(link)
        fence = link["mintedAtFence"]
        _require(handle.provider == provider and handle.link_id not in link_ids and fence >= previous_fence, "provider_chain_invalid")
        if handles:
            _require(handle.transport == handles[-1].transport, "provider_chain_invalid")
        if "replaces" in link:
            replacement = link["replaces"]
            _require(bool(handles) and key_limit == 8192 and handle.origin == "created" and root != previous_root
                     and isinstance(replacement, dict) and replacement.get("key") == previous_key
                     and _text(replacement.get("reason"), 64) and _integer(replacement.get("replacedAt"), 0), "provider_chain_invalid")
        elif not handles:
            _require(handle.origin in {"created", "adopted"}, "provider_chain_invalid")
        elif handle.origin == "resumed":
            _require(root == previous_root and (cursor != previous_cursor or fence != previous_fence), "provider_chain_resume_invalid")
        elif handle.origin == "forked":
            fork_key = link.get("forkedFromKey")
            _require(root != previous_root and _text(fork_key, key_limit) and fork_key == fork_key.strip()
                     and fork_key == previous_key, "provider_chain_fork_invalid")
        else:
            raise OrcaMetadataError("provider_chain_invalid")
        _require(handle.origin == "forked" or "forkedFromKey" not in link, "provider_chain_invalid")
        _require("supersedesKey" not in link or handle.origin == "created" and _text(link["supersedesKey"], key_limit), "provider_chain_invalid")
        handles.append(handle)
        link_ids.add(handle.link_id)
        previous_key, previous_root, previous_fence, previous_cursor = key, root, fence, cursor
    _require(isinstance(lease, dict) and lease.get("sessionId") == session_id, "record_lease_invalid")
    _require(lease.get("runtimeKind") in {"native", "tui"} and _integer(lease.get("runtimeFence"), 0)
        and "handoffStage" in lease and lease["handoffStage"] in {None, "preparing", "old-owner-stopped", "new-owner-proving", "recovering", "manual-recovery"}, "record_lease_invalid")
    for key, bound in (("provenHandleLinkId", 128), ("reservedSpawnToken", 512), ("handoffOperationId", 512)):
        _require(key in lease and (lease[key] is None or _text(lease[key], bound)), "record_lease_invalid")
    _require(_integer(lease.get("leaseDeadlineAt")) and _integer(lease.get("lastRenewedAt"))
        and _text(lease.get("claimKeyId")) and lease.get("claimStatus") in {"reserved", "live", "conflicted", "released"}
        and type(lease.get("unreconciled")) is bool, "record_lease_invalid")
    owner = lease.get("ownerProcess")
    _require("ownerProcess" in lease and (owner is None or isinstance(owner, dict)
        and _text(owner.get("hostId")) and _integer(owner.get("pid"), 1)
        and "processStartTimeMs" in owner and (owner["processStartTimeMs"] is None or _integer(owner["processStartTimeMs"], 0))
        and _text(owner.get("spawnToken"))), "record_owner_invalid")
    checkpoint = lease.get("journalCheckpoint")
    _require(checkpoint is None or isinstance(checkpoint, dict)
        and _integer(checkpoint.get("epoch"), 0) and _integer(checkpoint.get("sequence"), 0), "record_lease_invalid")
    death = lease.get("deathEvidence")
    _require("deathEvidence" in lease and (death is None or isinstance(death, dict)
        and death.get("kind") in {"exit-observed", "kill-delivered", "pid-absent", "identity-mismatch"} and _text(death.get("detail"))
        and _integer(death.get("observedAt"), 0)
        and ("ownerFence" not in death or _integer(death["ownerFence"], 0))
        and ("lastProvenAliveAt" not in death or _integer(death["lastProvenAliveAt"], 0)
             and death["lastProvenAliveAt"] <= death["observedAt"])), "record_lease_invalid")
    if lease["claimStatus"] == "live":
        _require(owner is not None and (lease["provenHandleLinkId"] is None or bool(handles)
            and lease["provenHandleLinkId"] == handles[-1].link_id and lease["runtimeFence"] == previous_fence), "record_live_lease_invalid")
    _require(_integer(value.get("createdAt")) and _integer(value.get("updatedAt")) and "launchEnv" not in value, "record_shape_invalid")
    if "options" in value:
        options = value["options"]
        _require(isinstance(options, dict) and len(options) <= 32
            and all(_text(k) and _text(v) for k, v in options.items()), "record_options_invalid")
    if "conversationName" in value:
        name = value["conversationName"]
        _require(_text(name, 200) and name == " ".join(name.split())
                 and not all(c in " \u200c\u200d" for c in name)
                 and not any(unicodedata.category(c) in {"Cc", "Zl", "Zp", "Cf"} and c not in "\u200c\u200d" for c in name),
                 "record_conversation_name_invalid")
    if "launchArgs" in value:
        args = value["launchArgs"]
        _require(isinstance(args, list) and len(args) <= 256 and all(isinstance(arg, str) and "\x00" not in arg for arg in args),
                 "record_launch_args_invalid")
        _require(len(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 16384,
                 "record_launch_args_invalid")
    recovery = (lease["runtimeKind"] != "native" or lease["handoffStage"] in {"preparing", "old-owner-stopped", "manual-recovery"}
                or any(key in value for key in ("rewind", "conversationCommand"))
                or any("supersedesKey" in link or "replaces" in link for link in chain))
    launched = value.get("launchedAccount")
    if provider == "codex" and "launchedAccount" in value:
        _require(isinstance(launched, dict) and _integer(launched.get("fence"), 1), "record_account_home_invalid")
        _, actual_home, _ = _account_home(launched.get("accountHome"), provider)
        # Codex can copy history across accounts on resume. The founding home alone no longer covers this session.
        recovery |= actual_home != home_locator or launched["fence"] > lease["runtimeFence"]
    else:
        actual_home = launch_home(value) if provider == "claude" else None
    return OrcaRecord(session_id, provider, location["executionHostId"], location["workspaceId"], location["wslDistro"],
                      home_variable, home_locator, tuple(handles), recovery,
                      OrcaLease(lease["claimStatus"], lease["unreconciled"], lease["runtimeKind"],
                                lease["handoffStage"], owner["hostId"] if owner else None,
                                owner["pid"] if owner else None,
                                owner["processStartTimeMs"] if owner else None,
                                death["kind"] if death else None),
                      actual_home, account_locator)


def read_orca_journal(path: Path, profile_root: Path) -> tuple[tuple[OrcaRecord, ...], tuple[SourceFailure, ...]]:
    records: list[OrcaRecord] = []
    errors: list[SourceFailure] = []
    def failure(code: str) -> SourceFailure:
        return SourceFailure(str(path), code, profile_root=profile_root, database=path, error_type="OrcaInventoryIncomplete")
    try:
        with closing(connect_readonly(path)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] != (2 if path.name == DOMAIN_JOURNAL else 4):
                raise OrcaMetadataError("journal_schema_unsupported")
            for table, columns in {
                "agent_session_records": {"session_id": ("TEXT", True), "record_json": ("TEXT", False)},
                "agent_session_tabs": {"tab_id": ("TEXT", True), "session_id": ("TEXT", False), "position": ("INTEGER", False)},
                "agent_session_store_meta": {"key": ("TEXT", True), "value": ("TEXT", False)},
            }.items():
                schema = connection.execute("SELECT type, sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
                if schema is None or schema["type"] != "table" or str(schema["sql"]).lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                    raise OrcaMetadataError("journal_metadata_schema_incomplete")
                actual = {row["name"]: row for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if set(actual) != set(columns) or any(
                    str(actual[name]["type"]).upper() != kind
                    or (actual[name]["pk"] != 1 if primary else actual[name]["pk"] != 0 or actual[name]["notnull"] != 1)
                    for name, (kind, primary) in columns.items()
                ):
                    raise OrcaMetadataError("journal_metadata_schema_incomplete")
            for row in connection.execute("SELECT session_id, record_json FROM agent_session_records ORDER BY rowid"):
                try:
                    raw = row["record_json"]
                    _require(isinstance(raw, str) and len(raw.encode("utf-8")) <= _MAX_RECORD_BYTES, "record_size_unsupported")
                    records.append(parse_orca_record(row["session_id"], json.loads(raw)))
                except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
                    errors.append(failure(str(exc) if isinstance(exc, OrcaMetadataError) else "record_json_invalid"))
            ids = {record.session_id for record in records}
            tab_ids: set[str] = set()
            tab_sessions: set[str] = set()
            for row in connection.execute("SELECT tab_id, session_id, position FROM agent_session_tabs ORDER BY position"):
                tab, session = row["tab_id"], row["session_id"]
                if (not _text(tab) or ":" in tab or tab.startswith("web-terminal-") or session not in ids
                    or tab in tab_ids or session in tab_sessions or not _integer(row["position"], 0)):
                    errors.append(failure("session_tab_metadata_invalid"))
                    continue
                tab_ids.add(tab)
                tab_sessions.add(session)
            if connection.execute("SELECT 1 FROM agent_session_store_meta WHERE key = ?", ("session_tabs_recorded",)).fetchone() is None:
                errors.append(failure("session_tabs_coverage_unknown"))
    except (OSError, sqlite3.Error, OrcaMetadataError) as exc:
        errors.append(failure(str(exc) if isinstance(exc, OrcaMetadataError) else "journal_read_failed"))
    return tuple(records), tuple(dict.fromkeys(errors))
