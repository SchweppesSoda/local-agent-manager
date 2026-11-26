"""Plan one native deletion from a record owned by a T3 server or Paseo daemon."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from pathlib import Path
import sqlite3

from . import frozen_files
from .daemon_records import settings_file
from .record_identity import canonical_path


# The native plan commits to the daemon proof. Direct or differently scoped
# apply must not bypass the second inventory under the daemon startup fence.
active_proof = ContextVar("daemon_provider_approval", default=None)


def _modules(client):
    if client == "paseo":
        from . import paseo_lifecycle as lifecycle
        from .paseo_bound_adapter import PaseoBoundAdapter as adapter, load_manifest
        from .paseo_discovery import discover_bindings
    else:
        from . import t3code_lifecycle as lifecycle
        from .t3code_bound_adapter import T3CodeBoundAdapter as adapter, load_manifest
        from .t3code_discovery import discover_bindings
    return adapter, load_manifest, discover_bindings, lifecycle


def _settings(owner):
    try:
        proof, _ = frozen_files.read_file(Path(owner["root"]), settings_file(owner["client"]), limit=1024 * 1024)
        return proof
    except FileNotFoundError:
        return None


def _target(owner, sid, adapter):
    from .remote_records import _digest, _owner
    if adapter.profile_root != Path(owner["root"]) or adapter.manifest.get(
            "server_id" if owner["client"] == "paseo" else "environment_id") != owner["instance_id"]:
        raise ValueError("daemon_provider_profile_mismatch")
    if owner["client"] == "t3code":
        snapshot = adapter.snapshot_provider_references()
    else:
        snapshot = adapter.snapshot_references(refresh=True)
        if any(row["opaque_session_handle_present"] for row in adapter.observations):
            raise ValueError("daemon_provider_session_handle_unverified")
    if snapshot.errors:
        raise ValueError("daemon_provider_reference_inventory_incomplete")
    binding = adapter.bindings.get(sid)
    refs = [r for r in snapshot.references if r.frontend_id == sid]
    if not binding or binding["engine"] not in {"codex", "claude", "pi"}:
        raise ValueError("daemon_provider_native_writer_unqualified")
    if not refs or any(not r.evidence_complete or r.native_record is None or r.host != "local"
                       or r.path_namespace != "local" for r in refs):
        raise ValueError("daemon_provider_strong_reference_required")
    native = refs[0].native_record
    if any(r.native_record.value != native.value or r.native_record.canonical_path != native.canonical_path for r in refs):
        raise ValueError("daemon_provider_single_native_record_required")
    if owner["client"] == "t3code":
        adapter.check_import_roots([sid])
    imports = getattr(adapter, "legacy_import_ids", ())
    if any(r.frontend_id != sid and r.engine == binding["engine"] and r.native_id == native.record_id
           and (r.native_record is None or r.native_record.store.canonical_path == native.store.canonical_path
                or r.frontend_id in imports) for r in snapshot.references):
        raise ValueError("daemon_provider_native_record_shared")
    client = "native" if binding["engine"] == "codex" else binding["engine"]
    return {"owner": _owner(client, Path(binding["root"]), binding.get("agent_dir"), cindy_ssh_layout=False),
            "id": native.record_id,
            "path": str(native.path) if native.path else None,
            "reference_sha256": _digest(sorted(_digest(r.to_dict()) for r in refs))}


def prepare(owner, request):
    from .daemon_records import require_native_route
    from .remote_records import _owner, _records
    adapter_type, load, discover, lifecycle = _modules(owner["client"])
    root = Path(owner["root"])
    settings = _settings(owner)
    sid = request["record_id"]
    selected = [r for r in _records(owner["client"], root) if r["record_id"] == sid]
    if not selected:
        raise ValueError("remote_exact_record_not_found")
    for record in selected:
        require_native_route(record)
    if request["bindings"]:
        manifest = load(request["bindings"])
    elif owner["client"] == "paseo":
        manifest = discover(root, provider_only=True)
    else:
        manifest = discover(root)
    adapter = adapter_type(manifest)
    target = _target(owner, sid, adapter)
    if settings != _settings(owner):
        raise ValueError("daemon_provider_settings_changed")
    binary = request.get("codex_bin")
    if target["owner"]["client"] == "native":
        configured = adapter.bindings[sid].get("codex_binary")
        if configured and binary and canonical_path(configured) != canonical_path(binary):
            raise ValueError("daemon_provider_binary_mismatch")
        binary = configured or binary
        if not binary:
            raise ValueError("execution_host_codex_binary_required")
    elif binary:
        raise ValueError("daemon_provider_unexpected_codex_binary")
    boundary = lifecycle.freeze(manifest)
    if _owner(owner["client"], root) != owner:
        raise ValueError("remote_owner_changed")
    return {"owner": owner, "record_id": sid, "target": target, "codex_bin": binary, "settings": settings,
            "binding_manifest": manifest, "automatic_bindings": request["bindings"] is None, "lifecycle": boundary}


def check_plan(coverage, proof, *, planning):
    target = proof["target"]
    if (coverage["completion_scope"] != "provider_native" or coverage["native_ids"] != [target["id"]]
            or planning and target["path"] and not Path(coverage["native_locator"]).samefile(target["path"])):
        raise ValueError("daemon_provider_native_plan_exceeds_reference")


@contextmanager
def _hold_source(proof):
    from .remote_records import _owner
    owner = proof["owner"]
    root = Path(owner["root"])
    if _owner(owner["client"], root) != owner:
        raise ValueError("remote_owner_changed")
    adapter_type, _, _, lifecycle = _modules(owner["client"])
    evidence = {**proof, "root": str(root)}
    with ExitStack() as stack:
        stack.enter_context(lifecycle.hold(evidence))
        if _owner(owner["client"], root) != owner:
            raise ValueError("remote_owner_changed")
        if owner["client"] == "t3code":
            from . import t3code_provider_settings
            from .t3code_reference_batch import hold_references
            if proof["automatic_bindings"] and t3code_provider_settings.has_provider_policy():
                raise ValueError("t3code_managed_provider_requires_explicit_bindings")
            stack.enter_context(hold_references(evidence))
        if (_settings(owner) != proof["settings"]
                or _target(owner, proof["record_id"], adapter_type(proof["binding_manifest"])) != proof["target"]):
            raise ValueError("daemon_provider_binding_changed")
        yield


def _apply(inner, proof, document):
    from .mutation_guard import frozen_operation_roots, mutation_roots
    from .remote_records import dispatch
    if not inner["clients_closed"] or "controller_approval" in inner:
        raise ValueError("daemon_provider_closed_owner_required")
    lookup = {k: v for k, v in inner.items() if k != "clients_closed"}
    with mutation_roots((*frozen_operation_roots(document), Path(proof["owner"]["root"]))):
        code, status = dispatch({**lookup, "verb": "status"}, provider_source=proof)
        prior = status.get("result", {})
        if prior.get("plan_sha256") != document["plan_sha256"]:
            raise ValueError("daemon_provider_native_status_unverified")
        if prior.get("mutation_started"):
            # A retry, including one after a lost reply, can only verify.
            return dispatch({**lookup, "verb": "verify"}, provider_source=proof)
        if prior.get("mutation_started") is not False or prior.get("goal_status") not in {"ready", "blocked"}:
            return code or 1, status
        with _hold_source(proof):
            return dispatch(inner, provider_source=proof)


def route(request, proof, *, document=None):
    from .remote_records import _digest, _operation, dispatch
    owner = proof["owner"]
    target = proof["target"]
    native_owner = target["owner"]
    inner = {**request, "client": native_owner["client"], "root": native_owner["root"],
             "owner": native_owner["store_id"]}
    if native_owner.get("agent_dir"):
        inner["pi_agent_dir"] = native_owner["agent_dir"]
    if request["verb"] == "plan":
        inner.pop("provider_only")
        inner.update(record_id=target["id"], bindings=None, codex_bin=proof["codex_bin"])
    token = active_proof.set(_digest(proof))
    try:
        if inner["verb"] == "apply":
            code, result = _apply(inner, proof, document)
        else:
            code, result = dispatch(inner, provider_source=proof)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if document is None:
            raise
        # Owner or inventory changes can prevent a fresh verification. Keep
        # any recorded native mutations visible without attesting absence.
        _, prior = _operation(native_owner["client"], Path(native_owner["root"]),
            ["operation", "status", "--operation-id", document["operation_id"]],
            agent_dir=native_owner.get("agent_dir"))
        if prior.get("plan_sha256") != document["plan_sha256"] or not prior.get("mutation_started"):
            raise
        code, result = 1, {"protocol": request["protocol"], "destination": request["destination"],
            "operation_id": document["operation_id"], "approval": request["approval"], "goal_status": "unknown",
            "error": str(exc), "result": {**prior, "goal_status": "unknown", "goal_satisfied": False}}
    finally:
        active_proof.reset(token)
    return code, {**result, "owner": owner, "provider_owner": native_owner, "daemon_record_id": proof["record_id"],
                  "completion_scope": "bound_provider_only", "daemon_record_action": "none"}
