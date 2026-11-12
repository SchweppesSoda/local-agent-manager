"""Magpie companion actions within the ordinary immutable operation protocol.

One action/child per physical config or cache store. Native ownership is
derived from selected native actions, never from a frontend ID or a BIN path.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
import os

from . import frozen_files as ff, magpie_store as files
from .record_identity import canonical_path

KIND = "delete_magpie_records"
_ticket = ContextVar("magpie_execution", default=None)
SUPPORTED_CLIENTS = {"native", "cindy", "t3code", "paseo", "pi", "claude"}


def native_bindings(document):
    roots = {s["storage_id"]: s["path"] for s in document.get("storages", ())}
    result = {}
    for action in document.get("actions", ()):
        engine = {"delete_conversation": "codex", "delete_pi_session": "pi",
                  "delete_claude_session": "claude"}.get(action["kind"])
        if engine is None:
            continue
        impact, target = action["impact"], action["target"]
        payload = impact.get("external_action_payload", {})
        root = (payload.get("session_root") if engine == "pi" else impact.get("external_storage_root"))
        root = root or roots.get(target["storage_id"])
        root = canonical_path(files.path(root))
        paths = list(impact.get("rollout_paths", ())) + list(impact.get("external_artifact_paths", ()))
        paths.extend(payload.get("transcript_paths", ()))
        if payload.get("path"):
            paths.append(payload["path"])
        paths.extend(p["path"] for p in payload.get("manifest", ()) if isinstance(p, dict) and "path" in p)
        paths = sorted({str(files.path(p)) for p in paths if files._within(p, Path(root))})
        ids = {target["thread_id"], *impact.get("affected_thread_ids", ()), *impact.get("descendant_thread_ids", ())}
        for record_id in ids:
            key = engine, root, record_id
            if key in result:
                files.fail("duplicate_native_action")
            result[key] = {"action_id": action["action_id"], "paths": paths}
    return result


def _key(record):
    return record["engine"], canonical_path(record["root"]), record["id"]


def _bind(manifest, document):
    if document.get("scope", {}).get("client") not in SUPPORTED_CLIENTS or any(
            document.get("scope", {}).get(k) for k in ("t3code_only", "paseo_only")):
        files.fail("native_cleanup_scope_required")
    for action in document.get("actions", ()):
        payload = action.get("impact", {}).get("external_action_payload", {})
        for owner in ("t3code", "paseo"):
            if any(t["engine"] not in {"codex", "claude", "pi"}
                   for t in payload.get(owner + "_evidence", {}).get("native_targets", ())):
                files.fail("native_engine_scope_unqualified")
    native = native_bindings(document)
    selected = {_key(r) for p in manifest["profiles"] for r in p["records"]}
    if not native or selected != set(native):
        files.fail("native_binding_scope_mismatch")
    return native


def _runtime(profile):
    binaries = sorted(set(profile["runtime_binaries"]))
    # Windows packages have a console launcher and GUI executable. Binding
    # just the console shim would leave the desktop free to restart.
    for binary in binaries:
        p = files.path(binary)
        if p.suffix.lower() not in {".exe", ".com"}:
            files.fail("runtime_format_unqualified")
        for suffix in (".exe", ".com"):
            sibling = p.with_suffix(suffix)
            if sibling.exists() and canonical_path(sibling) not in set(map(canonical_path, binaries)):
                files.fail("runtime_sibling_unbound")
    return [{"root": str(Path(b).parent), "file": ff.read_file(Path(b).parent, Path(b).name)[0]} for b in binaries]


def action_id(evidence, index):
    return KIND + ":" + files.digest([evidence["profile"], index])[:32]


def actions_for(evidence):
    from .planning import CandidateAction, ActionKind, ActionImpact, RiskLevel, TargetRef, StorageLocation
    from .planning import storage_id_for_path
    actions, storages = [], []
    for index, store in enumerate(evidence["stores"]):
        root = Path(store["root"])
        sid = storage_id_for_path(root)
        aid = action_id(evidence, index)
        payload = {"magpie_evidence": evidence, "store_index": index,
                   "requires_action_ids": evidence["dependencies"]}
        actions.append(CandidateAction(aid, ActionKind(KIND), TargetRef(sid, aid), RiskLevel.HIGH,
            True, None, ActionImpact(owner_client="magpie", owner_process_root=str(root),
                external_storage_root=str(root), external_action_payload=payload,
                resource_path=str(root)), files.digest([evidence, index]), resource_kind="magpie_records"))
        storages.append(StorageLocation(sid, "Magpie " + ("config" if index == 0 else "cache"), root))
    return tuple(actions), tuple(storages)


def add_context(coordinator, context, evidences):
    actions, storages = [], []
    for evidence in evidences:
        a, s = actions_for(evidence)
        actions.extend(a); storages.extend(s)
    existing = {s.storage_id for s in context.plan.storages}
    if existing & {s.storage_id for s in storages}:
        files.fail("native_magpie_store_overlap")
    plan = replace(context.plan, actions=(*context.plan.actions, *actions),
        storages=(*context.plan.storages, *storages),
        plan_fingerprint="magpie:v1:" + files.digest([context.plan.plan_fingerprint, evidences]))
    return replace(context, plan=plan, actions=coordinator.service.typed_actions(plan)), tuple(actions)


def augment(coordinator, document, context, candidates, binding_path):
    p = files.path(str(Path(binding_path).absolute()))
    manifest = files.validate_manifest(files.decode(ff.read_file(p.parent, p.name, content=True,
                                            limit=files.MAX_BYTES)[1]))
    native = _bind(manifest, document)
    roots = [Path(p[k]) for p in manifest["profiles"] for k in ("config_root", "cache_root")]
    for root in roots:
        if any(files._within(str(root), Path(key[1])) or files._within(key[1], root) for key in native):
            files.fail("native_magpie_store_overlap")
    dependencies = sorted(a["action_id"] for a in document["actions"])
    evidences = []
    for profile in manifest["profiles"]:
        paths = sorted({p for r in profile["records"] for p in native[_key(r)]["paths"]})
        evidence = files.freeze(profile, paths)
        # Keep Magpie's exact spelling too: shard names hash the original
        # locator bytes, and a deleted path no longer has a filesystem alias.
        evidence = files.freeze(profile, sorted(set(paths) | set(evidence["paths"])))
        evidence.update(runtime=_runtime(profile), dependencies=dependencies,
                        native_bindings=[{"engine": k[0], "root": k[1], "id": k[2], **v}
                            for k, v in sorted(native.items()) if k in {_key(r) for r in profile["records"]}])
        evidences.append(evidence)
    updated, additions = add_context(coordinator, context, evidences)
    return updated, (*candidates, *additions)


def evidences(document):
    result = []
    for a in document.get("actions", ()):
        if a.get("kind") == KIND:
            e = a["impact"]["external_action_payload"]["magpie_evidence"]
            if e not in result:
                result.append(e)
    return result


def validate_document(document):
    values = evidences(document)
    if not values:
        return
    manifest = {"schema_version": files.SCHEMA, "profiles": [e["profile"] for e in values]}
    files.validate_manifest(manifest, live=False)
    native = _bind(manifest, document)
    dependencies = sorted(a["action_id"] for a in document["actions"] if a["kind"] != KIND)
    expected = []
    for e in values:
        if (e.get("schema_version") != files.STORE_SCHEMA or e.get("dependencies") != dependencies
                or e.get("native_bindings") != [{"engine": k[0], "root": k[1], "id": k[2], **v}
                    for k, v in sorted(native.items()) if k in {_key(r) for r in e["profile"]["records"]}]):
            files.fail("frozen_binding_changed")
        if ([s["root"] for s in e["stores"]] != [e["profile"]["config_root"], e["profile"]["cache_root"]]
                or {str(Path(r["root"]) / r["file"]["path"]) for r in e["runtime"]}
                    != set(e["profile"]["runtime_binaries"])
                or e["source_paths"] != sorted(set(e["paths"]) | {p for b in e["native_bindings"] for p in b["paths"]})):
            files.fail("frozen_store_scope_changed")
        expected.extend(a.to_dict() for a in actions_for(e)[0])
    actual = [a for a in document["actions"] if a["kind"] == KIND]
    if len(actual) != len(expected):
        files.fail("frozen_action_scope_changed")
    for a, b in zip(actual, expected):
        from .operation_coordinator import OperationCoordinator
        b = OperationCoordinator._metadata(b)
        if any(a.get(k) != b.get(k) for k in ("action_id", "kind", "target", "snapshot_fingerprint", "impact")):
            files.fail("frozen_action_scope_changed")


def check_repeated_bindings(document, binding_path):
    if binding_path is None:
        return
    p = Path(binding_path).absolute()
    value = files.decode(ff.read_file(p.parent, p.name, content=True, limit=files.MAX_BYTES)[1])
    expected = {"schema_version": files.SCHEMA, "profiles": [e["profile"] for e in evidences(document)]}
    if value != expected:
        files.fail("repeated_bindings_changed")


def restore_context(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        document = kwargs.get("approved_document") or {}
        if evidences(document):
            validate_document(document)
            context, _ = add_context(self, result[0], evidences(document))
            result = (context, *result[1:])
        return result
    return wrapped


def with_residuals(method):
    @wraps(method)
    def wrapped(cls, document, context):
        with execution_scope(document, clients_closed=True, verify=True):
            result = method(cls, document, context)
            for e in evidences(document):
                fresh = files.freeze(e["profile"], e["source_paths"])
                for i, store in enumerate(e["stores"]):
                    if (any(not ff.satisfied(Path(store["root"]), c) for c in store["changes"])
                            or fresh["stores"][i]["changes"]):
                        result.append(action_id(e, i))
        return list(dict.fromkeys(result))
    return wrapped


def _file_evidence(e):
    return {k: v for k, v in e.items() if k not in {"runtime", "dependencies", "native_bindings"}}


def require_closed(values):
    from .office_runtime import processes
    binaries = {canonical_path(Path(r["root"]) / r["file"]["path"]) for e in values for r in e["runtime"]}
    for row in processes():
        name = str(row.get("name") or "").lower()
        if not name:
            files.fail("process_census_incomplete")
        if name.startswith("magpie") or row.get("executable") and canonical_path(row["executable"]) in binaries:
            files.fail("writer_running")


@contextmanager
def execution_scope(document, *, clients_closed, verify=False):
    values = evidences(document)
    if not values:
        yield
        return
    validate_document(document)
    if verify and _ticket.get() == values:
        yield
        return
    if not clients_closed:
        files.fail("clients_closed_ack_required")
    if os.name != "nt":
        files.fail("startup_exclusion_unavailable")
    from .windows_held_files import HeldFiles
    require_closed(values)
    with HeldFiles() as held:
        acquired = set()
        for e in values:
            for runtime in e["runtime"]:
                key = runtime["root"], runtime["file"]["path"]
                if key not in acquired:
                    held.acquire(Path(runtime["root"]), runtime["file"])
                    acquired.add(key)
            if not verify:
                files.check_before(_file_evidence(e))
        require_closed(values)
        token = _ticket.set(values)
        try:
            yield
        finally:
            _ticket.reset(token)


@dataclass(frozen=True)
class MagpieResult:
    deleted_ids: tuple[str, ...]
    status: str = "deleted"

    def to_dict(self):
        return {"deleted_ids": list(self.deleted_ids), "status": self.status}


def execute(evidence, index, *, phase_callback=None):
    if evidence not in (_ticket.get() or ()):
        files.fail("execution_boundary_required")
    store = evidence["stores"][index]
    root = Path(store["root"])
    # The other store may have completed. Recheck this entire store against
    # its own frozen before-state, including unrelated shared-file bytes.
    fresh = files.freeze(evidence["profile"], evidence["source_paths"])
    if fresh["stores"][index] != store:
        files.fail("store_changed")
    if phase_callback and store["changes"]:
        phase_callback("mutation_started")
    for change in store["changes"]:
        if change["kind"] == "rewrite":
            ff.apply_rewrite(root, change, lambda raw: files.transform(evidence, change, raw))
        else:
            ff.apply_remove(root, change)
    if any(not ff.satisfied(root, c) for c in store["changes"]):
        files.fail("after_state_mismatch")
    if phase_callback:
        phase_callback("verified")
    return MagpieResult((action_id(evidence, index),) if store["changes"] else ())
