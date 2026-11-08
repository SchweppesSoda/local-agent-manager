"""Journaled deletion through the local, version-qualified Paseo daemon."""

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from . import frozen_files
from .cleaner import ScanReport
from .office_cleanup import digest
from .paseo_store import PaseoAdapter, _engine, _plain, local_root
from .planning import ActionImpact, ActionKind, CandidateAction, RiskLevel, ScanStatus, StorageLocation, TargetRef, storage_id_for_path
from .record_identity import canonical_path

KIND = "delete_paseo_daemon_agent"
VERSION = "0.15.3"
SCHEMA = "larj.paseo-daemon-delete.v1"
_RUNNER = "resources/app.asar.unpacked/dist/daemon/node-entrypoint-runner.js"
_ARCHIVE = "resources/app.asar"
_RUNTIME_FILES = ("Paseo.exe", _ARCHIVE, _RUNNER)
_SERVER_ID = re.compile(r"srv_[A-Za-z0-9_-]{1,128}")
_LOOPBACK = re.compile(r"(?:127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})", re.ASCII)
_LOCAL_PIPE = re.compile(r"(?:pipe://)?\\\\\.\\pipe\\(?:LOCAL\\)?[A-Za-z0-9_.-]{1,180}", re.IGNORECASE | re.ASCII)


class PaseoDaemonError(RuntimeError):
    kind = "paseo_daemon_delete_unverified"


def _full_id(value):
    try:
        return str(uuid.UUID(value)) == value
    except (AttributeError, ValueError, TypeError):
        return False


def _bundled_install():
    if os.name != "nt":
        raise PaseoDaemonError("paseo_windows_bundle_required")
    return Path.home() / "AppData/Local/Programs/Paseo"


def _runtime(install):
    return [frozen_files.read_file(install, part)[0] for part in _RUNTIME_FILES]


def _identity(root):
    info = _plain(root, directory=True)
    return [info.st_dev, info.st_ino]


def _server_id(root):
    _, raw = frozen_files.read_file(root, "server-id", content=True, limit=256)
    value = raw.decode("utf-8").strip()
    if not _SERVER_ID.fullmatch(value):
        raise PaseoDaemonError("paseo_server_id_invalid")
    return value


def _snapshot(root, agent_id):
    # This shipped version uses the file registry and in-memory timeline.
    # Legacy timeline data is outside this daemon's qualified delete path.
    legacy = frozen_files.checked_path(root, "agent-timelines")
    if os.path.lexists(legacy):
        raise PaseoDaemonError("paseo_legacy_timeline_unqualified")
    adapter = PaseoAdapter(profile_root=root)
    snapshot = adapter.snapshot_references()
    if any(error.blocks_inventory for error in snapshot.errors):
        raise PaseoDaemonError("paseo_registry_inventory_incomplete")
    return next((row for row in adapter.observations if row["id"] == agent_id), None)


def _daemon_locator(root, server_id):
    _, raw = frozen_files.read_file(root, "paseo.pid", content=True, limit=4096)
    try:
        value = json.loads(raw)
        endpoint = value["listen"]
        port = _LOOPBACK.fullmatch(endpoint)
        if (value["serverId"] != server_id or type(value["pid"]) is not int or value["pid"] <= 0
                or not isinstance(value["startedAt"], str)
                or not (_LOCAL_PIPE.fullmatch(endpoint) or port and int(port[1]) <= 65535)):
            raise ValueError
        return endpoint
    except (KeyError, TypeError, ValueError):
        raise PaseoDaemonError("paseo_local_daemon_unproven") from None


def _rpc(evidence, verb, timeout):
    root = local_root(evidence["root"])
    install = local_root(evidence["install"])
    if (_identity(root) != evidence["root_identity"] or _server_id(root) != evidence["server_id"]
            or _runtime(install) != evidence["runtime"]):
        raise PaseoDaemonError("paseo_frozen_identity_changed")
    endpoint = _daemon_locator(root, evidence["server_id"])
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "PASEO_"))}
    env.update(ELECTRON_RUN_AS_NODE="1", PASEO_NODE_ENV="production")
    request = {k: evidence[k] for k in ("root", "server_id", "agent_id", "provider")}
    request.update(version=VERSION, verb=verb, endpoint=endpoint)
    try:
        result = subprocess.run(
            [str(install / "Paseo.exe"), "--disable-warning=DEP0040", str(install / _RUNNER),
             "node-script", str(Path(__file__).with_name("paseo_daemon_rpc.cjs")), str(install / _ARCHIVE)],
            input=json.dumps(request).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, timeout=timeout, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        response = json.loads(result.stdout)
        if type(response) is not bool:
            raise ValueError
        return response
    except (OSError, subprocess.SubprocessError, ValueError):
        raise PaseoDaemonError("paseo_daemon_rpc_unverified") from None


def freeze(root, agent_id):
    root = local_root(root)
    if not _full_id(agent_id):
        raise PaseoDaemonError("paseo_full_uuid_required")
    row = _snapshot(root, agent_id)
    if row is None:
        raise PaseoDaemonError("paseo_exact_agent_not_found")
    source = Path(row["source"]).relative_to(root)
    if source.name != f"{agent_id}.json":
        raise PaseoDaemonError("paseo_registry_filename_mismatch")
    install = _bundled_install()
    evidence = {
        "schema_version": SCHEMA, "root": str(root), "root_identity": _identity(root),
        "server_id": _server_id(root), "agent_id": agent_id, "provider": row["provider"],
        "record_path": source.as_posix(), "record_sha256": row["fingerprint"],
        "install": str(install), "runtime": _runtime(install), "version": VERSION,
    }
    if not _rpc(evidence, "probe", 15):
        raise PaseoDaemonError("paseo_daemon_agent_not_found")
    return evidence


def _candidate(evidence):
    root = Path(evidence["root"])
    agent_id = evidence["agent_id"]
    storage_id = storage_id_for_path(root)
    return CandidateAction(
        f"{KIND}:{digest([canonical_path(root), agent_id])[:32]}", ActionKind(KIND),
        TargetRef(storage_id, agent_id), RiskLevel.HIGH, True, None,
        ActionImpact(owner_client="paseo", owner_process_root=str(root),
                     external_engine=_engine(evidence["provider"]), external_storage_root=str(root),
                     affected_thread_ids=(agent_id,), resource_path=str(root), frontend_references_preserved=False,
                     external_action_payload={"paseo_daemon_evidence": evidence}),
        digest(evidence), requires_explicit_selection=True, resource_kind="paseo_agent",
    )


def context_for(coordinator, evidence):
    base = coordinator.service.prepare_report(ScanReport(), active_adapters=(), platforms=("paseo",))
    action = _candidate(evidence)
    storage = StorageLocation(action.target.storage_id, "Paseo daemon", Path(evidence["root"]), scan_status=ScanStatus.OK)
    plan = replace(base.plan, actions=(action,), storages=(storage,), plan_fingerprint=digest(evidence))
    return replace(base, plan=plan, actions=coordinator.service.typed_actions(plan)), (), None, None, {}, {}


def approved_evidence(document):
    scope = document["scope"]
    actions = document.get("actions", ())
    if (document.get("schema_version") != "larj.operation-plan.v1"
            or scope.get("client") != "paseo" or scope.get("paseo_only") is not True or scope.get("engines")
            or scope.get("projects") or scope.get("all_projects") or len(actions) != 1
            or actions[0].get("kind") != KIND):
        raise PaseoDaemonError("paseo_daemon_plan_scope_invalid")
    action = actions[0]
    evidence = action["impact"]["external_action_payload"]["paseo_daemon_evidence"]
    root = local_root(evidence["root"])
    agent_id = evidence["agent_id"]
    record = Path(evidence["record_path"])
    if (evidence["schema_version"] != SCHEMA or evidence["version"] != VERSION or not _full_id(agent_id)
            or not _SERVER_ID.fullmatch(evidence["server_id"]) or list(scope["record_ids"]) != [agent_id]
            or record.parts[:1] != ("agents",) or len(record.parts) not in {2, 3}
            or record.name != f"{agent_id}.json" or evidence["install"] != str(_bundled_install())):
        raise PaseoDaemonError("paseo_daemon_plan_evidence_invalid")
    candidate = _candidate(evidence)
    if (action["action_id"] != candidate.action_id or action["target"] != candidate.target.to_dict()
            or [(item["storage_id"], item["path"]) for item in document["storages"]]
               != [(candidate.target.storage_id, str(root))]
            or len(document["child_batches"]) != 1
            or document["child_batches"][0]["mutation_family"] != KIND
            or document["child_batches"][0]["storage_id"] != candidate.target.storage_id
            or document["child_batches"][0]["action_ids"] != [candidate.action_id]):
        raise PaseoDaemonError("paseo_daemon_plan_target_invalid")
    return evidence


def require_absent(evidence, timeout=15):
    root = Path(evidence["root"])
    if (_rpc(evidence, "probe", timeout)
            or _snapshot(root, evidence["agent_id"]) is not None
            or os.path.lexists(frozen_files.checked_path(root, evidence["record_path"]))):
        raise PaseoDaemonError("paseo_daemon_delete_after_unverified")


def execute(document, action, *, checkpoint, timeout, plan):
    from .execution import ExecutionOutcome
    from .paseo_cleanup import PaseoCleanupResult

    evidence = approved_evidence(document)
    root = Path(evidence["root"])
    agent_id = evidence["agent_id"]
    checkpoint("guard_started", action, None)
    row = _snapshot(root, agent_id)
    if (row is None or row["fingerprint"] != evidence["record_sha256"]
            or Path(row["source"]).relative_to(root).as_posix() != evidence["record_path"]
            or not _rpc(evidence, "probe", timeout)):
        raise PaseoDaemonError("paseo_approved_agent_changed")
    checkpoint("mutation_started", action, None)
    if _rpc(evidence, "delete", timeout):
        raise PaseoDaemonError("paseo_daemon_agent_still_present")
    require_absent(evidence, timeout)
    result = PaseoCleanupResult((agent_id,))
    checkpoint("verified", action, result)
    return ExecutionOutcome(KIND, (action,), plan, paseo_cleanup=result)
