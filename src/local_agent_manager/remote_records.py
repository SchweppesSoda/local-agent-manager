"""Run a bounded record request in the authoritative checkout on the owning host."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import socket
import subprocess
import tempfile
from types import SimpleNamespace

from . import frozen_files
from .operation_store import canonical_json_bytes, plan_sha256, strict_json_load, validate_operation_id, write_new_json
from .path_identity import is_local_absolute_locator
from .providers.t3 import strict_json
from .state_paths import PLANS_DIRECTORY, manager_state_base

PROTOCOL = "lam.remote-records.v1"
CLIENTS = ("cindy", "t3code", "paseo", "orca", "native", "pi", "claude")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", re.ASCII)
HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
VERBS = ("records", "plan", "apply", "status", "verify")
REPLY_LIMIT = 16 * 1024 * 1024
LOCAL_OWNER = "@local"


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def directory_identity(root):
    if not is_local_absolute_locator(str(root)) or root != Path(os.path.abspath(root)):
        raise ValueError("absolute_owner_directory_required")
    for part in (*reversed(root.parents), root):
        info = part.lstat()
        frozen_files._plain(info, directory=True)
    return _digest([os.path.normcase(str(root)), info.st_dev, info.st_ino])


def _owner(client, root, pi_agent_dir=None, *, cindy_ssh_layout=True):
    """Resolve a path on this host only. File IDs distinguish replaced profiles."""
    if not is_local_absolute_locator(str(root)) or root != Path(os.path.abspath(root)):
        raise ValueError("remote_owner_absolute_path_required")
    for parent in reversed(root.parents):
        frozen_files._plain(parent.lstat(), directory=True)
    info = root.lstat()
    frozen_files._plain(info, directory=client != "cindy")
    physical = [info.st_dev, info.st_ino]
    if client == "t3code":
        from .t3code_server_delete import _environment_id
        from .t3code_store import database_path
        instance = _environment_id(root)
        database = database_path(root).stat()
        physical.extend((database.st_dev, database.st_ino))
    elif client == "paseo":
        from .paseo_daemon_delete import _server_id
        instance = _server_id(root)
    elif client == "orca":
        from .orca_metadata import journal_name
        journal = frozen_files.checked_path(root, journal_name(root)).lstat()
        frozen_files._plain(journal)
        physical.extend((journal.st_dev, journal.st_ino))
        instance = None
    else:
        instance = None
    identity = {"client": client, "root": str(root), "instance_id": instance,
                "store_id": _digest([socket.gethostname(), client, os.path.normcase(str(root)), physical, instance])}
    # Cindy's SSH launchers explicitly set these two homes on the executor.
    # Only this execution mode has this layout. Device/share providers may differ.
    cindy_ssh = {"native": "codex-home", "claude": "claude-home"}
    if cindy_ssh_layout and client in cindy_ssh and root == Path.home() / ".xdt-server" / "v1" / cindy_ssh[client]:
        identity["cindy_ssh_provider"] = "codex" if client == "native" else client
    if client == "pi":
        from .pi_sessions import resolve_pi_paths
        if pi_agent_dir and not is_local_absolute_locator(pi_agent_dir):
            raise ValueError("remote_pi_absolute_agent_directory_required")
        agent = resolve_pi_paths(agent_dir=pi_agent_dir, session_root=root).agent_dir
        for part in (*reversed(agent.parents), agent):
            frozen_files._plain(part.lstat(), directory=True)
        info = agent.stat()
        identity["agent_dir"] = str(agent)
        identity["store_id"] = _digest([identity["store_id"], os.path.normcase(str(agent)), info.st_dev, info.st_ino])
    return identity


def _records(client, root, *, agent_dir=None, hosted_id=None, hosted_locators=(), all_workspaces=False):
    if client == "orca":
        from .orca_metadata import journal_name, read_orca_journal
        journal = frozen_files.checked_path(root, journal_name(root))
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                frozen_files._plain(Path(str(journal) + suffix).lstat())
            except FileNotFoundError:
                if not suffix:
                    raise
        records, errors = read_orca_journal(journal, root)
        if errors:
            raise ValueError("orca_journal_inventory_incomplete")
        return [{"record_id": r.session_id, "engine": r.provider, "execution_host": r.host, "wsl_distro": r.wsl_distro,
                 "provider_home": r.home_locator, "native_ids": [h.native_id for h in r.handles],
                 **({"launched_home": r.launched_home, "leaf_uuids": [h.leaf_uuid for h in r.handles]}
                    if r.provider == "claude" else {})} for r in records]
    if client == "cindy":
        from .cindy_references import build_cindy_reference_catalog
        catalog = build_cindy_reference_catalog(root)
        if catalog.failures:
            raise ValueError("cindy_reference_inventory_incomplete")
        return [{"record_id": ref.cindy_session_id, "engine": ref.backend, "kind": ref.reference_kind,
                 "execution_host": ref.execution_host, "native_locator": ref.native_session_id or ref.remote_native_locator,
                 "status": ref.session_status, "boundary_id": ref.boundary_id,
                 "rewind_at": ref.boundary_rewind_at_ms} for ref in catalog.references]
    if client == "t3code":
        from .daemon_records import t3_records
        return t3_records(root)
    if client == "paseo":
        from .daemon_records import paseo_records
        return paseo_records(root)
    from .session_catalog_factory import build_pi_catalog, build_claude_catalog
    if client == "native":
        from .adapters import NativeIntegrityAdapter
        from .inventory import build_session_catalog
        catalog = build_session_catalog((NativeIntegrityAdapter(codex_home=root),))
    else:
        catalog = (build_pi_catalog(SimpleNamespace(pi_session_dir=root, pi_agent_dir=agent_dir)) if client == "pi"
                   else build_claude_catalog(SimpleNamespace(claude_config_dir=root)))
    if catalog.errors:
        raise ValueError("remote_native_inventory_incomplete")
    from .cindy_hosted import locator_key
    locators = set(map(locator_key, hosted_locators))
    def need_workspace(identifier, locator):
        return all_workspaces or hosted_id == identifier or locator_key(locator) in locators
    if client == "native":
        return [{"record_id": item.thread_id, **({"cwds": [r.cwd for r in item.rollouts], "rollouts": [str(r.path) for r in item.rollouts]}
                 if need_workspace(item.thread_id, item.thread_id) else {})} for item in catalog.records
                if item.codex_home == root and (item.rollouts or item.indexed or item.artifact_present)]
    if client == "pi":
        return [{"record_id": item.session_id, "path": str(item.path),
                 **({"cwds": [item.cwd]} if need_workspace(item.session_id, str(item.path)) else {})} for item in catalog.records
                if item.session_root == root]
    records = []
    for item in catalog.records:
        if item.config_dir != root or not item.manifest:
            continue
        record = {"record_id": item.session_id}
        if need_workspace(item.session_id, item.session_id):
            from .claude_sessions import _read_transcript
            cwds = []
            for path in item.transcript_paths:
                frozen_files._plain(frozen_files.checked_path(root, path.relative_to(root).as_posix()).lstat())
                values = set()
                _read_transcript(path, working_dirs=values)
                if not values and not all_workspaces:
                    raise ValueError("cindy_claude_workspace_missing")
                cwds.extend(values or [None])
            record["cwds"] = cwds
        records.append(record)
    return records


def _scope(client, root, record_id, frontend_only):
    if frontend_only and client not in {"paseo", "t3code"}:
        raise ValueError("remote_frontend_mode_unsupported")
    flags = [f"--client={client}", f"--record-id={record_id}"]
    if client in {"paseo", "t3code"}:
        flags.append(f"--{client}-root={root}")
        if frontend_only:
            flags.append(f"--{client}-only")
    elif client == "orca":
        flags.append(f"--orca-root={root}")
    elif client == "native":
        flags.append(f"--codex-home={root}")
    return flags


def _operation(client, root, args, *, agent_dir=None, runtime=None):
    """Use the same CLI, including its normal discovery and cross-client guards."""
    from .cli import main
    from .cleanup_service import CleanupService
    from .operation_coordinator import OperationCoordinator
    paths = SimpleNamespace(pi_session_dir=root, pi_agent_dir=agent_dir) if client == "pi" else SimpleNamespace(claude_config_dir=root)
    coordinator = OperationCoordinator(CleanupService(), session_catalog_args=paths) if client in {"pi", "claude"} else None
    output = io.StringIO()
    def bound_binary(_hint):
        # The standalone adapter may suggest its ambient PATH binary during a
        # cold rebind. This single-store plan explicitly selected another binary.
        path = Path(runtime["path"])
        if frozen_files.read_file(path.parent, path.name)[0] != runtime["identity"]:
            raise ValueError("remote_codex_runtime_changed")
        return path
    options = {"binary_resolver": bound_binary} if runtime and client == "native" else {}
    code = main([*args, "--json"], operation_coordinator=coordinator, stdout=output, stderr=io.StringIO(), **options)
    return code, strict_json(output.getvalue())


def _codex_runtime(owner, explicit):
    from .discovery import choose_codex_binary
    if explicit is not None and not is_local_absolute_locator(explicit):
        raise ValueError("remote_codex_absolute_binary_required")
    if owner.get("cindy_ssh_provider") == "codex":
        bundled = Path(owner["root"]) / "packages" / "standalone" / "current" / "codex"
        if explicit and Path(explicit).resolve(strict=True) != bundled.resolve(strict=True):
            raise ValueError("cindy_ssh_runtime_mismatch")
        binary = bundled
    else:
        binary = choose_codex_binary(Path(explicit) if explicit else None)
    if binary is None:
        raise ValueError("remote_codex_binary_required")
    path = binary.resolve(strict=True)
    if os.name == "nt" and path.suffix.casefold() != ".exe" or not os.access(path, os.X_OK):
        raise ValueError("remote_native_executable_required")
    identity, _ = frozen_files.read_file(path.parent, path.name)
    return {"path": str(path), "identity": identity}


def _physical_scope(doc, owner):
    """Return the semantics proved by this plan, on every cold request."""
    client, root, instance = owner["client"], Path(owner["root"]), owner["instance_id"]
    scope = doc["scope"]
    record_id, = scope["record_ids"]
    if client in {"t3code", "paseo"}:
        if scope.get(client + "_only"):
            from . import t3code_server_delete, paseo_daemon_delete
            module = t3code_server_delete if client == "t3code" else paseo_daemon_delete
            e = module.approved_evidence(doc)
            key = "environment_id" if client == "t3code" else "server_id"
            if Path(e["root"]) != root or e[key] != instance:
                raise ValueError("remote_frontend_owner_mismatch")
            provider = e["before"]["default_provider"] if client == "t3code" else e["provider"]
            history = "preserved" if provider in {"codex", "claude", "claudeAgent", "pi", "antigravity"} else "outside_scope"
            return {"completion_scope": "t3_soft_delete" if client == "t3code" else "paseo_daemon_delete",
                    "provider_history": history, "frontend_ids": [record_id]}
        from . import t3code_cleanup, paseo_cleanup
        module = t3code_cleanup if client == "t3code" else paseo_cleanup
        e = module.evidence_from_document(doc)
        if not e:
            raise ValueError("remote_full_cleanup_binding_required")
        module.validate(e)
        ids = e["selected_thread_ids"] if client == "t3code" else e["selected_agent_ids"]
        key = "environment_id" if client == "t3code" else "server_id"
        if (Path(e["root"]) != root or e["binding_manifest"][key] != instance or record_id not in ids
                or not e["native_targets"]):
            raise ValueError("remote_full_cleanup_binding_required")
        return {"completion_scope": "bound_frontend_and_native", "frontend_ids": ids,
                "native_targets": [{k: t[k] for k in ("engine", "root", "ids", "paths")} for t in e["native_targets"]],
                "desktop_profiles_on_owner": [p["root"] for p in e["binding_manifest"]["desktop_profiles"]]}
    if client == "orca":
        from .orca_remote import owner_scope
        return owner_scope(doc, root, record_id)
    kinds = {"pi": "delete_pi_session", "claude": "delete_claude_session", "native": "delete_conversation"}
    action, = doc["actions"]
    impact = action["impact"]
    payload = impact.get("external_action_payload") or {}
    if (client not in kinds or action["kind"] != kinds[client] or action["target"]["thread_id"] != record_id
            or impact.get("frontend_reference_evidence") or impact.get("frontend_session_evidence")):
        raise ValueError("remote_exact_native_action_required")
    storage, = [s for s in doc["storages"] if s["storage_id"] == action["target"]["storage_id"]]
    location = payload.get("session_root") if client == "pi" else storage["path"]
    if not location or Path(location) != root:
        raise ValueError("remote_native_root_mismatch")
    if client == "pi" and Path(payload["pi_root"]) != Path(owner["agent_dir"]):
        raise ValueError("remote_pi_agent_directory_mismatch")
    native_ids = sorted({record_id, *impact.get("affected_thread_ids", ()), *impact.get("descendant_thread_ids", ())})
    return {"completion_scope": "provider_native", "native_locator": payload["path"] if client == "pi" else record_id,
            "record_id": record_id, "native_ids": native_ids}


def _validate(q, *, executor_only=False):
    verb = q.get("verb")
    required = {"protocol", "verb", "destination", "client", "root"}
    if "cindy_hosted" in q:
        required.add("cindy_hosted")
        h = q["cindy_hosted"]
        selector = "host_session_id" if executor_only and isinstance(h, dict) and "host_session_id" in h else "task_id"
        if (verb != "plan" or q.get("client") not in {"pi", "claude", "native"}
                or not isinstance(h, dict) or set(h) != {selector, "profile", "controller_device"}
                or not isinstance(h["profile"], str) or any(ord(c) < 32 for c in h["profile"])
                or not isinstance(h[selector], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", h[selector])
                or not isinstance(h["controller_device"], str) or not HASH.fullmatch(h["controller_device"])):
            raise ValueError("cindy_hosted_request_invalid")
    if "pi_agent_dir" in q:
        required.add("pi_agent_dir")
        if (q.get("client") != "pi" or not isinstance(q["pi_agent_dir"], str)
                or not q["pi_agent_dir"] or any(ord(c) < 32 for c in q["pi_agent_dir"])):
            raise ValueError("remote_pi_agent_directory_invalid")
    if verb != "records":
        required.add("owner")
    if verb == "plan":
        required.update(("record_id", "frontend_only", "bindings", "codex_bin"))
        if "provider_only" in q:
            if q["provider_only"] is not True or q["client"] not in {"t3code", "paseo"} or q.get("frontend_only"):
                raise ValueError("remote_provider_only_scope_invalid")
            required.add("provider_only")
    if verb in {"apply", "status", "verify"}:
        required.update(("operation_id", "approval"))
    if verb == "apply":
        required.add("clients_closed")
        if "controller_approval" in q:
            required.add("controller_approval")
    local = (q.get("destination") == LOCAL_OWNER and q.get("client") in {"t3code", "paseo", "native", "pi", "claude"}
             and not {"cindy_hosted", "controller_approval"}.intersection(q))
    if (set(q) != required or q["protocol"] != PROTOCOL or verb not in VERBS or q["client"] not in CLIENTS
            or not isinstance(q["destination"], str) or not (local or HOST.fullmatch(q["destination"]))
            or not isinstance(q["root"], str) or not 0 < len(q["root"]) <= 32767
            or any(ord(char) < 32 for char in q["root"])):
        raise ValueError("remote_request_invalid")
    for key in ("owner", "approval", "controller_approval"):
        if key in q and (not isinstance(q[key], str) or not HASH.fullmatch(q[key])):
            raise ValueError("remote_binding_invalid")
    for key in ("clients_closed", "frontend_only"):
        if key in q and type(q[key]) is not bool:
            raise ValueError("remote_scope_invalid")
    if "operation_id" in q:
        validate_operation_id(q["operation_id"])
    if verb != "records" and q["client"] == "cindy":
        raise ValueError("select_the_provider_on_its_execution_host")
    if verb == "plan" and q["bindings"] is not None:
        if (q["client"] not in {"paseo", "t3code"} or q["frontend_only"]
                or not isinstance(q["bindings"], str) or not q["bindings"]
                or any(ord(c) < 32 for c in q["bindings"])):
            raise ValueError("remote_binding_manifest_invalid")
    if verb == "plan" and q["codex_bin"] is not None and (
            q["client"] not in {"native", "orca"} and not q.get("provider_only")
            or not isinstance(q["codex_bin"], str) or any(ord(c) < 32 for c in q["codex_bin"])):
        raise ValueError("remote_codex_binary_invalid")
    if verb == "plan" and (not isinstance(q["record_id"], str) or not 0 < len(q["record_id"]) <= 512
                           or q["record_id"] != q["record_id"].strip() or any(ord(c) < 32 for c in q["record_id"])):
        raise ValueError("remote_exact_record_id_required")


def completed_batches(status, digest):
    batches = status.get("batches")
    return (status.get("plan_sha256") == digest and status.get("goal_status") == "complete"
            and status.get("modified") is True and status.get("mutation_started") is True
            and bool(batches) and all(b.get("status") == "complete" for b in batches))


def _recheck_hosted(stored, owner, coverage, verb, receipt, approval):
    from . import cindy_hosted
    hosted = stored["cindy_hosted"]
    client = owner["client"]
    locator = coverage["native_locator"]
    if client == "pi":
        if Path(stored["pi_locator"]) != Path(locator):
            raise ValueError("cindy_hosted_native_file_mismatch")
        locator = coverage["native_locator"] = stored["pi_locator"]
    selector = "task_id" if "task_id" in hosted else "host_session_id"
    claim = {k: hosted[k] for k in ("profile", selector, "controller_device")}
    record = None
    executor_only = stored.get("cindy_executor_only") is True
    if verb == "apply" or executor_only and verb == "verify":
        selected, = coverage["native_ids"]
        rows = _records(client, Path(owner["root"]), agent_dir=owner.get("agent_dir"),
                        hosted_id=selected, all_workspaces=executor_only)
        matches = [r for r in rows if r["record_id"] == selected]
        if executor_only:
            from .cindy_executor import records_in_shadow
            if matches != records_in_shadow(rows, Path(hosted["workspace_root"])):
                raise ValueError("cindy_executor_task_changed")
        if matches:
            record, = matches
    if executor_only:
        from .cindy_executor import guard_local_references
        guard_local_references(hosted, owner, coverage["record_id"], locator)
    retired = None
    if "host_index" in stored:
        change = stored["host_index"]
        if cindy_hosted.index_settled(receipt, approval, change, hosted, locator):
            retired = hosted
        elif (cindy_hosted.index_phase(receipt, approval) == "complete"
                or cindy_hosted.index_change(hosted, locator) != change):
            raise ValueError("cindy_host_index_changed_or_interrupted")
    if cindy_hosted.observe(claim, owner, locator, record=record, approved=hosted, retired=retired) != hosted:
        raise ValueError("cindy_hosted_session_binding_changed")


def dispatch(q, *, executor_only=False, provider_source=None):
    _validate(q, executor_only=executor_only)
    verb, client, root = q["verb"], q["client"], Path(q["root"])
    if executor_only and (client not in {"native", "claude", "pi"} or "controller_approval" in q
                          or verb == "plan" and "cindy_hosted" not in q):
        raise ValueError("cindy_executor_only_scope_invalid")
    if client in {"t3code", "paseo"} and verb in {"apply", "status", "verify"}:
        path = manager_state_base() / PLANS_DIRECTORY / (validate_operation_id(q["operation_id"]) + ".json")
        binding = strict_json_load(path.parent / "remote-bindings" / path.name)
        if "daemon_provider" in binding:
            from .daemon_provider import route
            proof = binding["daemon_provider"]
            owner = proof["owner"]
            doc = strict_json_load(path)
            if (q["approval"] != _digest(binding) or q["owner"] != owner["store_id"] or client != owner["client"]
                    or root != Path(owner["root"]) or q["destination"] != binding["destination"]
                    or q["operation_id"] != binding["operation_id"] or binding["plan_sha256"] != plan_sha256(doc)
                    or doc.get("daemon_provider_sha256") != _digest(proof)):
                raise ValueError("remote_provider_approval_or_owner_mismatch")
            # Receipts belong to the native owner. A cold status/verify can
            # read them even when the original daemon profile is offline.
            return route(q, proof, document=doc)
    cindy_ssh_layout = provider_source is None and q["destination"] != LOCAL_OWNER
    owner = _owner(client, root, q.get("pi_agent_dir"), cindy_ssh_layout=cindy_ssh_layout)
    agent_dir = Path(owner["agent_dir"]) if client == "pi" else None
    common = {"protocol": PROTOCOL, "destination": q["destination"], "owner": owner}
    if verb == "records":
        records = _records(client, root, agent_dir=agent_dir)
        if owner != _owner(client, root, q.get("pi_agent_dir"), cindy_ssh_layout=cindy_ssh_layout):
            raise ValueError("remote_owner_changed")
        return 0, {**common, "goal_status": "complete", "records": records}
    if q["owner"] != owner["store_id"]:
        raise ValueError("remote_owner_changed")
    if verb == "plan":
        record_id, frontend_only = q["record_id"], q["frontend_only"]
        if q.get("bindings") is not None and not is_local_absolute_locator(q["bindings"]):
            raise ValueError("remote_bindings_absolute_path_required")
        if q.get("provider_only"):
            from .daemon_provider import prepare, route
            return route(q, prepare(owner, q))
        matches = [r for r in _records(client, root, agent_dir=agent_dir,
                   hosted_id=record_id if "cindy_hosted" in q else None) if r["record_id"] == record_id]
        if not matches or client in {"pi", "claude", "native"} and len(matches) != 1:
            raise ValueError("remote_exact_record_not_found")
        if client in {"t3code", "paseo"} and not frontend_only:
            from .daemon_records import require_native_route
            for record in matches:
                require_native_route(record)
        args = _scope(client, root, record_id, frontend_only)
        orca_codex = client == "orca" and matches[0]["engine"] == "codex"
        if (orca_codex or client == "native" and "cindy_hosted" in q) and not q["codex_bin"]:
            raise ValueError("execution_host_codex_binary_required")
        if client == "orca" and not orca_codex and q["codex_bin"]:
            raise ValueError("orca_claude_does_not_use_codex_binary")
        runtime = _codex_runtime(owner, q["codex_bin"]) if client == "native" or orca_codex else None
        if runtime:
            args.append("--codex-bin=" + runtime["path"])
        if q["bindings"] is not None:
            args.append(f"--{client}-bindings={q['bindings']}")
        code, result = _operation(client, root, ["delete", "plan", *args], agent_dir=agent_dir)
        if code or result.get("goal_status") != "ready":
            return code or 1, {**common, "goal_status": result.get("goal_status", "blocked"), "result": result}
        operation = result["operation_id"]
    else:
        operation = q["operation_id"]
        matches, runtime = (), None
    path = manager_state_base() / PLANS_DIRECTORY / (validate_operation_id(operation) + ".json")
    doc = strict_json_load(path)
    digest = plan_sha256(doc)
    scope = doc["scope"]
    if (doc.get("plan_sha256") != digest or doc["operation_id"] != operation or scope.get("client") != client
            or len(scope["record_ids"]) != 1 or scope.get("all_projects") or scope.get("projects")
            or scope.get("engines") or verb == "plan" and scope["record_ids"] != [record_id]
            or doc.get("goal_status") != "ready" or doc.get("blockers") or not doc.get("actions")):
        raise ValueError("remote_frozen_scope_invalid")
    coverage = _physical_scope(doc, owner)
    envelope = {"protocol": PROTOCOL, "destination": q["destination"], "owner": owner,
                "operation_id": operation, "plan_sha256": digest}
    if provider_source is not None:
        from .daemon_provider import check_plan
        if doc.get("daemon_provider_sha256") != _digest(provider_source) or provider_source["target"]["owner"] != owner:
            raise ValueError("daemon_provider_native_owner_mismatch")
        # The plan hash keeps the exact Pi path after its long/short aliases
        # stop resolving to a file. Prove their physical identity at admission.
        check_plan(coverage, provider_source, planning=verb == "plan")
        envelope["daemon_provider"] = provider_source
    elif "daemon_provider_sha256" in doc:
        raise ValueError("daemon_provider_approved_operation_required")
    if executor_only:
        envelope["cindy_executor_only"] = True
    if client == "orca":
        envelope["authority"] = coverage["authority"]
    envelope_path = path.parent / "remote-bindings" / path.name
    stored = None if verb == "plan" else strict_json_load(envelope_path)
    lock = nullcontext()
    if stored is not None:
        if q["approval"] != _digest(stored):
            raise ValueError("remote_plan_approval_mismatch")
        if bool(stored.get("cindy_executor_only")) != executor_only or any(stored.get(k) != v for k, v in envelope.items()):
            raise ValueError("remote_operation_owner_mismatch")
        if verb == "apply" and "cindy_hosted" in stored:
            # Two tasks may share the same index. Serialize its before-state
            # check through native completion and the durable index receipt.
            from .mutation_guard import frozen_operation_roots, mutation_roots
            profile = Path(stored["cindy_hosted"]["profile"])
            if directory_identity(profile) != stored["cindy_hosted"]["profile_id"]:
                raise ValueError("cindy_executor_profile_changed")
            lock = mutation_roots((*frozen_operation_roots(doc), profile))
    with lock:
        return _bound_result(q, envelope, stored, path, doc, coverage, matches, runtime)


def _bound_result(q, envelope, stored, path, doc, coverage, matches, runtime):
    owner = envelope["owner"]
    client, root, verb = owner["client"], Path(owner["root"]), q["verb"]
    operation, digest, scope = doc["operation_id"], doc["plan_sha256"], doc["scope"]
    record_id, = scope["record_ids"]
    agent_dir = Path(owner["agent_dir"]) if client == "pi" else None
    common = {"protocol": PROTOCOL, "destination": q["destination"], "owner": owner}
    executor_only = envelope.get("cindy_executor_only") is True
    envelope_path = path.parent / "remote-bindings" / path.name
    index_receipt = envelope_path.parent / "host-index" / path.name
    frozen_args = ["--operation-id", operation, "--plan", str(path)]
    if owner != _owner(client, root, q.get("pi_agent_dir"), cindy_ssh_layout=(
            "daemon_provider" not in envelope and q["destination"] != LOCAL_OWNER)):
        raise ValueError("remote_owner_changed")
    if verb == "plan":
        envelope["codex_runtime"] = runtime
        if client == "orca":
            from .orca_remote import authoritative_chain
            envelope["handle_chain_sha256"] = authoritative_chain(root, record_id)
        if "cindy_hosted" in q:
            from .cindy_hosted import index_change, observe
            if coverage.get("native_ids") != [record_id]:
                raise ValueError("cindy_hosted_native_cascade_unverified")
            if client == "pi":
                native_path = matches[0]["path"]
                if not Path(native_path).samefile(coverage["native_locator"]):
                    raise ValueError("cindy_hosted_native_file_mismatch")
                envelope["pi_locator"] = coverage["native_locator"] = native_path
            hosted = observe(q["cindy_hosted"], owner, coverage["native_locator"], record=matches[0])
            if executor_only:
                from .cindy_executor import guard_local_references
                guard_local_references(hosted, owner, record_id, coverage["native_locator"])
            envelope["cindy_hosted"] = hosted
            if "guest_key" in hosted or "thread_location" in hosted:
                envelope["host_index"] = index_change(hosted, coverage["native_locator"])
        write_new_json(envelope_path, envelope)
    else:
        runtime = stored.get("codex_runtime")
        envelope["codex_runtime"] = runtime
        try:
            if client == "orca":
                h = stored["handle_chain_sha256"]
                if verb == "apply" and any(r["record_id"] == scope["record_ids"][0] for r in _records(client, root)):
                    from .orca_remote import authoritative_chain
                    if authoritative_chain(root, scope["record_ids"][0]) != h:
                        raise ValueError("orca_remote_handle_chain_changed")
                envelope["handle_chain_sha256"] = h
            if "cindy_hosted" in stored:
                _recheck_hosted(stored, owner, coverage, verb, index_receipt, q["approval"])
                envelope.update({k: stored[k] for k in ("cindy_hosted", "pi_locator", "host_index") if k in stored})
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError, sqlite3.Error) as exc:
            # A changed reference does not invalidate a completed child receipt.
            _, prior = _operation(client, root, ["operation", "status", *frozen_args], agent_dir=agent_dir)
            if prior.get("plan_sha256") != digest or not prior.get("mutation_started"):
                raise
            goal = "completed_with_residuals" if completed_batches(prior, digest) else "unknown"
            return 1, {**common, "operation_id": operation, "approval": q["approval"], "goal_status": goal,
                       **({"completion_scope": "cindy_executor_only"} if executor_only else {}),
                       "error": str(exc), "result": {**prior, "goal_status": goal, "goal_satisfied": False}}
        if stored != envelope:
            raise ValueError("remote_operation_owner_mismatch")
    approval = _digest(envelope)
    binding = {**common, "operation_id": operation, "approval": approval,
               "plan_sha256": digest, "scope": scope, "plan_path_on_owner": str(path), **coverage,
               **({"handle_chain_sha256": envelope["handle_chain_sha256"]} if client == "orca" else {}),
               **({"cindy_hosted": envelope["cindy_hosted"]} if "cindy_hosted" in envelope else {}),
               **({"codex_runtime": runtime} if runtime else {})}
    if executor_only:
        binding["completion_scope"] = "cindy_executor_only"
    if "host_index" in envelope:
        change = envelope["host_index"]
        if change["kind"] == "remove":
            binding["thread_location_to_remove"] = change["path"]
        else:
            binding["guest_index"] = {"path": change["before"]["path"], "remove_host_session_id": envelope["cindy_hosted"]["host_session_id"],
                                      "remove_native_locator": coverage["native_locator"]}
    if verb == "plan":
        actions = [{"kind": a["kind"], "target": a["target"],
                    "affected_thread_ids": a["impact"].get("affected_thread_ids", []),
                    "descendant_thread_ids": a["impact"].get("descendant_thread_ids", []),
                    "artifact_paths": a["impact"].get("external_artifact_paths", []),
                    "resource_path": a["impact"].get("resource_path")} for a in doc["actions"]]
        return 0, {**binding, "goal_status": "ready", "actions": actions, "storages": doc["storages"]}
    if q["approval"] != approval:
        raise ValueError("remote_plan_approval_mismatch")
    if verb == "apply":
        if client == "orca" and coverage.get("provider") == "claude" and not q.get("controller_approval"):
            raise ValueError("orca_paired_controller_plan_required")
        if "cindy_hosted" in envelope:
            from .codex_desktop_state import running_related_clients
            from .frontend_session_cleanup import _require_client_closed
            if not q["clients_closed"]:
                raise ValueError("cindy_executor_clients_closed_required")
            if not executor_only and not q.get("controller_approval"):
                raise ValueError("cindy_hosted_controller_plan_required")
            _require_client_closed(Path(envelope["cindy_hosted"]["profile"]), running_related_clients, owner_client="cindy")
        mode = bool(scope.get("paseo_only") or scope.get("t3code_only"))
        args = ["delete", "apply", *_scope(client, root, scope["record_ids"][0], mode), "--plan-sha256", digest]
        if client == "native" or client == "orca" and coverage.get("provider") != "claude":
            if not runtime or _codex_runtime(owner, runtime["path"]) != runtime:
                raise ValueError("remote_codex_runtime_changed")
            args.append("--codex-bin=" + runtime["path"])
        if q["clients_closed"]:
            args.append("--clients-closed")
    else:
        args = ["operation", verb]
    # An interrupted host index change can finish from a verified native
    # receipt. It must never send that provider another deletion request.
    resume_index = False
    if verb == "apply" and "host_index" in envelope:
        status_code, prior = _operation(client, root, ["operation", "status", *frozen_args], agent_dir=agent_dir)
        resume_index = not status_code and completed_batches(prior, digest)
        if resume_index:
            args = ["operation", "verify"]
    status = None
    if (verb == "verify" or resume_index) and coverage["completion_scope"] in {"provider_native", "orca_host_session"}:
        # An ordinary verify can settle a journal. Read the execution receipts
        # first so a missing transcript alone cannot establish that we deleted it.
        status_code, status = _operation(client, root, ["operation", "status", *frozen_args], agent_dir=agent_dir)
        if status.get("plan_sha256") != digest or not status.get("batches") or not status.get("mutation_started"):
            return 1, {**binding, "goal_status": "unknown", "error": "native_receipt_unverified", "result": status}
        ids = coverage["frontend_ids"] if client == "orca" else coverage["native_ids"]
        remaining = {r["record_id"] for r in _records(client, root, agent_dir=agent_dir)}.intersection(ids)
        if remaining:
            goal = "completed_with_residuals" if status.get("goal_status") == "complete" else "unknown"
            return 1, {**binding, "goal_status": goal, "error": "native_record_present",
                       "remaining_native_ids": sorted(remaining),
                       "result": {**status, "goal_status": goal, "goal_satisfied": False}}
    code, result = _operation(client, root, [*args, *frozen_args], agent_dir=agent_dir, runtime=runtime if verb == "apply" else None)
    if status is not None:
        if status_code or status.get("goal_status") != "complete":
            # Verification may have reconciled an interrupted child without
            # replaying its mutation. Only its durable outcome can release Cindy.
            status_code, status = _operation(client, root, ["operation", "status", *frozen_args], agent_dir=agent_dir)
        if status_code or not completed_batches(status, digest):
            return 1, {**binding, "goal_status": "unknown", "error": "native_receipt_unverified", "result": status}
        result.update(batches=status["batches"], modified=status["modified"], mutation_started=status["mutation_started"])
        result.update(scope=scope, counts={**result["counts"], "batch_count": len(status["batches"]),
                                          "action_count": len(doc["actions"])})
    if "host_index" in envelope and not code and result["goal_status"] == "complete":
        from .cindy_hosted import finish_index, index_settled
        change, hosted, locator = envelope["host_index"], envelope["cindy_hosted"], coverage["native_locator"]
        try:
            if verb == "apply":
                receipt_code, receipt = _operation(client, root, ["operation", "status", *frozen_args], agent_dir=agent_dir)
                if (receipt_code or not completed_batches(receipt, digest)
                        or any(r["record_id"] in coverage["native_ids"] for r in _records(client, root, agent_dir=agent_dir))):
                    raise ValueError("cindy_hosted_native_completion_unverified")
                finish_index(index_receipt, approval, change, hosted, locator)
            if not index_settled(index_receipt, approval, change, hosted, locator):
                raise ValueError("cindy_host_index_pending")
        except (OSError, ValueError, RuntimeError) as exc:
            return 1, {**binding, "goal_status": "completed_with_residuals", "error": str(exc),
                       "result": {**result, "goal_satisfied": False, "goal_status": "completed_with_residuals"}}
    return code, {**binding, "goal_status": result["goal_status"], "result": result}


def worker(stdin, stdout, *, executor_only=False, local_owner=False):
    request = None
    try:
        raw = stdin.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("remote_request_too_large")
        request = strict_json(raw.decode("utf-8"))
        if (request.get("destination") == LOCAL_OWNER) != local_owner:
            raise ValueError("remote_local_owner_transport_mismatch")
        code, reply = dispatch(request, executor_only=executor_only)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError, AttributeError, RecursionError) as exc:
        code = 1
        reply = {"protocol": PROTOCOL, "error": str(exc),
                 "goal_status": "unknown" if isinstance(request, dict) and request.get("verb") == "apply" else "blocked"}
    if isinstance(request, dict):
        reply.update(request_sha256=_digest(request), destination=request.get("destination"))
        if "operation_id" in request:
            reply.setdefault("operation_id", request["operation_id"])
    encoded = canonical_json_bytes(reply)
    if len(encoded) > REPLY_LIMIT:
        code = 1
        encoded = canonical_json_bytes({"protocol": PROTOCOL, "request_sha256": _digest(request),
            "goal_status": "unknown" if request["verb"] == "apply" else "blocked", "error": "remote_reply_too_large",
            "destination": request["destination"], "operation_id": request.get("operation_id")})
    stdout.write(encoded + b"\n")
    return code


def add_arguments(parser):
    subs = parser.add_subparsers(dest="remote_verb", required=True)
    for verb in VERBS:
        sub = subs.add_parser(verb)
        sub.add_argument("--progress", action="store_true", help="在标准错误输出请求阶段及结果计数")
        destination = sub.add_mutually_exclusive_group(required=True)
        destination.add_argument("--ssh-host", help="已信任的 OpenSSH 主机别名")
        destination.add_argument("--local", action="store_true", help="在当前电脑操作 T3、Paseo 或独立原生存储")
        destination.add_argument("--cindy-executor-only", action="store_true",
                                 help="直接在本机清理 Cindy 执行端记录，控制端可离线")
        clients = CLIENTS if verb == "records" else tuple(c for c in CLIENTS if c != "cindy")
        sub.add_argument("--client", required=True, choices=clients)
        sub.add_argument("--pi-agent-dir", help="执行端独立配置的 Pi Agent 目录")
        sub.add_argument("--root", required=True, help="执行端的绝对路径，Cindy 填数据库文件，其他客户端填存储目录")
        if verb != "records":
            sub.add_argument("--owner", required=True, help="remote records 返回的 owner.store_id")
        if verb == "plan":
            sub.add_argument("--record-id", help="完整记录 ID，Cindy 托管任务也可用 --cindy-task 定位")
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument("--frontend-only", action="store_true", help="仅执行 T3 或 Paseo 自带的前端删除")
            mode.add_argument("--provider-only", action="store_true", help="仅删除当前 thread 或 agent 独占并已绑定的原生会话")
            sub.add_argument("--bindings", help="执行端 T3 或 Paseo 原生存储绑定清单的绝对路径")
            sub.add_argument("--codex-bin", help="执行端实际使用的 Codex 程序绝对路径")
            sub.add_argument("--cindy-task", help="同账号设备或共享 provider 的完整 Cindy 任务 ID")
            sub.add_argument("--cindy-db", type=Path, help="读取任务原生定位符的控制机 Cindy 数据库")
            sub.add_argument("--cindy-host-profile", help="执行机 Cindy userData 的绝对路径")
            sub.add_argument("--cindy-controller-profile", type=Path, help="控制机 Cindy userData 的绝对路径")
            sub.add_argument("--cindy-controller-device", help="仅执行端模式下已知的控制设备 ID，同账号任务用此 ID 核对工作目录")
        if verb in {"apply", "status", "verify"}:
            sub.add_argument("--operation-id", required=True)
            sub.add_argument("--approval", required=True, help="remote plan 返回的主机与计划批准摘要")
        if verb == "apply":
            sub.add_argument("--clients-closed", action="store_true")
            sub.add_argument("--controller-plan", type=Path, help="在发出执行端删除前重查已批准的控制端会话")


def _ssh(host, payload):
    # Paths and locators never enter a shell command on either host.
    command = ["ssh", "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
               "-oClearAllForwardings=yes", "-oForwardAgent=no", "-oConnectTimeout=15",
               "-oServerAliveInterval=15", "-oServerAliveCountMax=3",
               "--", host, "local-agent-manager remote-worker"]
    with tempfile.TemporaryFile() as stream:
        # A healthy, large database operation can run for minutes. SSH
        # keepalives detect a lost connection without interrupting that writer.
        result = subprocess.run(command, input=payload, stdout=stream, stderr=subprocess.DEVNULL, check=False)
        if stream.tell() > REPLY_LIMIT + 1:
            raise ValueError("remote_response_too_large")
        stream.seek(0)
        return result.returncode, stream.read()


def exchange(request, *, executor_only=False):
    verb = request["verb"]
    try:
        _validate(request, executor_only=executor_only)
    except (ValueError, RuntimeError, TypeError, KeyError) as exc:
        return 1, {"goal_status": "blocked", "error": str(exc), "destination": request.get("destination")}
    try:
        payload = canonical_json_bytes(request)
        if executor_only or request["destination"] == LOCAL_OWNER:
            stream = io.BytesIO()
            code = worker(io.BytesIO(payload), stream, executor_only=executor_only,
                          local_owner=request["destination"] == LOCAL_OWNER)
            raw = stream.getvalue()
        else:
            code, raw = _ssh(request["destination"], payload)
        reply = strict_json(raw.decode("utf-8"))
        if (not isinstance(reply, dict) or reply.get("protocol") != PROTOCOL
                or reply.get("request_sha256") != _digest(request) or reply.get("destination") != request["destination"]
                or request.get("operation_id") and reply.get("operation_id") != request["operation_id"]):
            raise ValueError("remote_response_unverified")
        if code == 0:
            identity = reply["owner"]
            if (identity["client"] != request["client"] or not HASH.fullmatch(identity["store_id"])
                    or request.get("owner", identity["store_id"]) != identity["store_id"]
                    or request.get("approval", reply.get("approval")) != reply.get("approval")):
                raise ValueError("remote_response_owner_mismatch")
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError):
        code = 1
        reply = {"goal_status": "unknown" if verb == "apply" else "blocked",
                 "error": "remote_request_or_connection_unverified",
                 "destination": request["destination"], "operation_id": request.get("operation_id")}
    return code, reply


def _check_controller_plan(path, request):
    from . import cindy_remote, orca_remote
    doc = strict_json_load(path)
    registered = manager_state_base() / PLANS_DIRECTORY / (validate_operation_id(doc["operation_id"]) + ".json")
    digest = plan_sha256(doc)
    if (doc.get("plan_sha256") != digest or doc.get("goal_status") != "ready"
            or strict_json_load(registered) != doc or not request["clients_closed"]):
        raise ValueError("approved_controller_plan_and_closed_clients_required")
    if doc["scope"]["client"] == "cindy":
        proof = cindy_remote.frozen(doc)
        if not proof or "verify_request" not in proof:
            raise ValueError("controller_remote_binding_required")
        expected = proof["verify_request"]
    else:
        proof = orca_remote.frozen(doc)
        if not proof:
            raise ValueError("controller_remote_binding_required")
        expected = proof["remote_owner"]["request"]
    if any(expected[k] != request[k] for k in ("client", "destination", "root", "owner", "operation_id", "approval")):
        raise ValueError("controller_remote_operation_mismatch")
    if doc["scope"]["client"] == "cindy":
        cindy_remote.check_before_native(doc, proof)
    else:
        orca_remote.check_before_native(proof)
    return digest


def run(args, output, errors):
    executor_only = args.cindy_executor_only
    if args.ssh_host == LOCAL_OWNER:
        raise ValueError("remote_ssh_alias_invalid")
    destination = LOCAL_OWNER if args.local else "local-executor" if executor_only else args.ssh_host
    request = {"protocol": PROTOCOL, "destination": destination, "verb": args.remote_verb,
               "client": args.client, "root": args.root}
    if getattr(args, "provider_only", False):
        request["provider_only"] = True
    for name in ("owner", "record_id", "frontend_only", "bindings", "codex_bin", "operation_id", "approval", "clients_closed"):
        if hasattr(args, name):
            request[name] = getattr(args, name)
    if args.pi_agent_dir is not None:
        request["pi_agent_dir"] = args.pi_agent_dir
    if executor_only and (args.client not in {"native", "claude", "pi"} or getattr(args, "controller_plan", None)
                          or getattr(args, "cindy_controller_profile", None) or getattr(args, "cindy_db", None)):
        raise ValueError("cindy_executor_only_uses_local_provider_and_profile")
    if not executor_only and getattr(args, "cindy_controller_device", None):
        raise ValueError("cindy_controller_device_requires_executor_only")
    if getattr(args, "controller_plan", None):
        request["controller_approval"] = _check_controller_plan(args.controller_plan, request)
    task = None
    if executor_only and args.remote_verb == "plan":
        from .cindy_executor import select
        if not args.cindy_host_profile or args.frontend_only or args.bindings:
            raise ValueError("cindy_executor_local_host_profile_required")
        owner = _owner(args.client, Path(args.root), args.pi_agent_dir)
        if args.owner != owner["store_id"]:
            raise ValueError("remote_owner_changed")
        request["record_id"], request["cindy_hosted"] = select(Path(args.cindy_host_profile), owner,
            task_id=args.cindy_task, record_id=args.record_id, controller_device=args.cindy_controller_device)
    elif any(getattr(args, key, None) for key in ("cindy_task", "cindy_host_profile", "cindy_controller_profile", "cindy_db")):
        from .cindy_hosted import device_id
        if not all((args.cindy_task, args.cindy_host_profile, args.cindy_controller_profile)):
            raise ValueError("cindy_hosted_task_and_both_profiles_required")
        request["cindy_hosted"] = {"task_id": args.cindy_task, "profile": args.cindy_host_profile,
                                   "controller_device": device_id(args.cindy_controller_profile)}
        if args.cindy_db:
            from .cindy_remote import hosted_task_reference
            task = hosted_task_reference(args.cindy_db, args.cindy_controller_profile, args.cindy_task, args.client)
            if args.client == "pi" and not args.record_id:
                from .cindy_hosted import locator_matches
                listing = {k: request[k] for k in ("protocol", "destination", "client", "root", "pi_agent_dir") if k in request}
                code, reply = exchange({**listing, "verb": "records"})
                if code or reply["owner"]["store_id"] != args.owner:
                    raise ValueError("cindy_pi_executor_inventory_unverified")
                matches = [r["record_id"] for r in reply["records"] if locator_matches(r["path"], task.remote_native_locator)]
                if len(matches) != 1:
                    raise ValueError("cindy_pi_exact_task_file_required")
                request["record_id"], = matches
            elif not args.record_id:
                request["record_id"] = task.remote_native_locator
    from .progress import ProgressReporter
    progress = ProgressReporter(errors) if args.progress else None
    stage = "inventory" if args.remote_verb == "records" else args.remote_verb
    if progress:
        progress(stage=stage, status="started", operation_id=request.get("operation_id"))
    code, reply = exchange(request, executor_only=executor_only)
    if task is not None and not code:
        from .cindy_hosted import locator_matches
        if (reply["cindy_hosted"]["execution_host"] != task.execution_host
                or not locator_matches(reply["native_locator"], task.remote_native_locator)):
            raise ValueError("cindy_hosted_task_executor_mismatch")
    if progress:
        counts = dict((reply.get("result") or {}).get("counts") or {})
        if "records" in reply:
            counts["record_count"] = len(reply["records"])
        if "actions" in reply:
            counts["action_count"] = len(reply["actions"])
        progress(stage=stage, status="failed" if code else "completed",
                 operation_id=reply.get("operation_id"), goal_status=reply.get("goal_status"), counts=counts)
    output.write(json.dumps(reply, ensure_ascii=False, indent=2) + "\n")
    return code
