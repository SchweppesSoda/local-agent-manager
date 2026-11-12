"""Qualified Chromium IndexedDB cleanup on verified private copies.

The original is never opened by Chromium. A durable mutation checkpoint precedes
installation of the cold-verified family. Interrupted installation is unknown;
only an exact frozen logical after state can subsequently verify completion.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import frozen_files
from .office_leveldb import _environment, _node, _snapshot

SCHEMA = "larj.paseo-indexeddb.v1"
RELATIVE = "IndexedDB/paseo_app_0.indexeddb.leveldb"
BLOBS = RELATIVE.replace(".leveldb", ".blob")
CODE = Path(__file__).with_name("leveldb_runtime")


class PaseoIndexedDBError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.kind = code


def fail(code):
    raise PaseoIndexedDBError("paseo_" + code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def runtime(root):
    """Only the complete official Electron 44.2.0 Windows distribution."""
    if os.name != "nt":
        fail("browser_platform_unqualified")
    root = Path(root)
    expected = json.loads((CODE / "paseo_electron.json").read_text(encoding="utf-8"))
    found = frozen_files.freeze_remove(root.parent, root.name)
    expected_names = {item["path"] for item in expected["files"]}
    if {item["path"][len(root.name) + 1:] for item in found["files"]} != expected_names:
        fail("browser_runtime_layout_changed")
    by_name = {item["path"][len(root.name) + 1:]: item for item in found["files"]}
    for item in expected["files"]:
        actual = by_name[item["path"]]
        if (actual["sha256"], actual["size"]) != (item["sha256"], item["size"]):
            fail("browser_runtime_unverified")
    helpers = [frozen_files.read_file(CODE, name)[0] for name in (
        "paseo_browser.cjs", "physical.cjs", "idb_keys.cjs", "idb_audit.cjs")]
    node = _node().resolve(strict=True)
    return {"root": str(root), "distribution": found, "helpers": helpers,
            "node": str(node), "node_file": frozen_files.read_file(node.parent, node.name)[0]}


def _run(command, *, cwd=None, timeout=60):
    try:
        result = subprocess.run(list(map(str, command)), capture_output=True, timeout=timeout,
            cwd=cwd, env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PaseoIndexedDBError("paseo_browser_helper_failed") from exc
    if len(result.stdout) > 1024 * 1024:
        fail("browser_output_budget_exceeded")
    try:
        lines = [line for line in result.stdout.splitlines() if line.startswith(b"{")]
        if len(lines) != 1:
            raise ValueError()
        value = json.loads(lines[0])
    except (ValueError, UnicodeError) as exc:
        raise PaseoIndexedDBError("paseo_browser_response_invalid") from exc
    if result.returncode or value.get("status") in {"blocked", "unknown"}:
        fail("browser_copy_unverified")
    return value


def audit(path, state, scope=None):
    command = [state["node"], CODE / "idb_audit.cjs", path]
    if scope is not None:
        command.append(json.dumps(scope, separators=(",", ":")))
    value = _run(command)
    if value.get("schema_version") != "larj.paseo-idb-audit.v1":
        fail("browser_physical_audit_invalid")
    return value


def _browser(session, temporary, state, server_id, ids, *, before=None, after=None, managed_names=None):
    control = Path(tempfile.mkdtemp(prefix="control-", dir=temporary))
    request = {"mode": "inspect" if before is None else "apply", "sessionPath": str(session),
               "controlPath": str(control), "serverId": server_id, "agentIds": ids,
               "beforeSha256": before, "afterSha256": after}
    if managed_names is not None:
        request["managedNames"] = managed_names
    path = control / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    value = _run([Path(state["root"]) / "electron.exe", CODE / "paseo_browser.cjs", path], cwd=control)
    if (value.get("status") != "verified" or value.get("origin") != "paseo://app"
            or value.get("valueCodec") != "json-safe.v1"
            or value.get("runtime") != {"electron": "44.2.0", "chrome": "152.0.7977.76", "node": "24.20.0"}):
        fail("browser_logical_audit_invalid")
    for key in ("beforeSha256", "expectedAfterSha256", "afterSha256"):
        import re
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]):
            fail("browser_logical_audit_invalid")
    return value


def _copy(root, snapshot, stage, *, relative=RELATIVE):
    destination = stage / relative
    stage.mkdir(parents=True, exist_ok=True)
    with ExitStack() as fences:
        fences.enter_context(frozen_files._parent_fence(stage, ".larj-parent-fence"))
        prefix = ""
        for component in relative.split("/"):
            prefix = prefix + "/" + component if prefix else component
            path = frozen_files.checked_path(stage, prefix)
            path.mkdir(exist_ok=True)
            fences.enter_context(frozen_files._parent_fence(stage, prefix + "/.larj-parent-fence"))
        for item in snapshot["files"]:
            evidence, body = frozen_files.read_file(root, item["path"], content=True)
            if evidence != item:
                fail("browser_source_changed")
            with (destination / Path(item["path"]).name).open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
    if _snapshot(root, relative) != snapshot:
        fail("browser_source_changed")


def _blob_snapshot(root):
    path = frozen_files.checked_path(root, BLOBS)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    snapshot = frozen_files.freeze_remove(root, BLOBS)
    # Chromium's decimal database / low-byte bucket / hexadecimal blob number.
    import re
    for directory in snapshot["directories"]:
        relative = Path(directory["path"]).relative_to(BLOBS).parts
        if (len(relative) > 2 or relative and not re.fullmatch(r"[1-9][0-9]*", relative[0])
                or len(relative) == 2 and not re.fullmatch(r"[0-9a-f]{2}", relative[1])):
            fail("browser_blob_layout_unqualified")
    for item in snapshot["files"]:
        parts = Path(item["path"]).relative_to(BLOBS).parts
        if (len(parts) != 3 or not re.fullmatch(r"[1-9][0-9]*", parts[0])
                or not re.fullmatch(r"[0-9a-f]{2}", parts[1]) or not re.fullmatch(r"[1-9a-f][0-9a-f]*", parts[2])
                or f"{(int(parts[2], 16) >> 8) & 255:02x}" != parts[1]):
            fail("browser_blob_layout_unqualified")
    return snapshot


def _copy_blobs(root, snapshot, stage):
    if snapshot is None:
        return
    if _blob_snapshot(root) != snapshot:
        fail("browser_blob_source_changed")
    for directory in sorted(snapshot["directories"], key=lambda item: len(item["path"])):
        path = frozen_files.checked_path(stage, directory["path"])
        with frozen_files._parent_fence(stage, directory["path"]):
            path.mkdir(parents=True, exist_ok=True)
    for item in snapshot["files"]:
        proof, body = frozen_files.read_file(root, item["path"], content=True)
        if proof != item:
            fail("browser_blob_source_changed")
        with frozen_files._parent_fence(stage, item["path"]) as path:
            with path.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
    if _blob_snapshot(root) != snapshot:
        fail("browser_blob_source_changed")


def _observe(stage, temporary, state, server_id, ids, blobs=None, managed_names=None):
    scope = {"serverId": server_id, "agentIds": ids} if blobs is not None else None
    raw = audit(stage / RELATIVE, state, scope)
    expected = sorted((item["path"], item["size"]) for item in raw.get("wrappedFiles", ()))
    actual = sorted((str(Path(item["path"]).relative_to(BLOBS)).replace("\\", "/"), item["size"])
                    for item in (blobs or {}).get("files", ()))
    if actual != expected:
        fail("browser_blob_closure_unverified")
    value = _browser(stage, temporary, state, server_id, ids, managed_names=managed_names)
    opened = audit(stage / RELATIVE, state, scope)
    if (opened["preservationSha256"] != raw["preservationSha256"]
            or opened["dataRecords"] != raw["dataRecords"]
            or value["totalRecords"] != raw["dataRecords"]
            or value["beforeSha256"] != value["afterSha256"]):
        fail("browser_native_open_changed_records")
    observation = {"before_sha256": value["beforeSha256"], "after_sha256": value["expectedAfterSha256"],
            "selected": value["selectedBefore"], "raw_preservation_sha256": raw["preservationSha256"],
            "data_records": raw["dataRecords"]}
    if managed_names is not None:
        observation["protected_managed_names"] = value["protectedManagedNames"]
    return observation


def require_no_blob(root):
    blob = frozen_files.checked_path(root, RELATIVE.replace(".leveldb", ".blob"))
    try:
        blob.lstat()
    except FileNotFoundError:
        return
    fail("browser_blob_family_unqualified")


def _discard_selected_blobs(stage, before, state):
    # A native transaction must have removed every external reference and
    # settled its deletion journals before any selected payload is discarded.
    audit(stage / RELATIVE, state)
    current = _blob_snapshot(stage)
    if current is None:
        return
    approved = {(f["path"], f["size"], f["sha256"]) for f in before["files"]}
    if any((f["path"], f["size"], f["sha256"]) not in approved for f in current["files"]):
        fail("browser_blob_state_unknown")
    frozen_files.apply_remove(stage, current)


def freeze(root, electron_root, server_id, ids, *, managed_names=None):
    root = Path(root)
    require_no_sealed(root)
    from .paseo_cleanup_files import ID
    if (not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or not isinstance(server_id, str) or not ID.fullmatch(server_id)):
        fail("browser_selection_invalid")
    ids = sorted(set(ids))
    snapshot = _snapshot(root, RELATIVE)
    blobs = _blob_snapshot(root)
    if blobs is not None and snapshot is None:
        fail("browser_blob_closure_unverified")
    state = runtime(electron_root) if snapshot else None
    observation = None
    if snapshot:
        with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-") as raw:
            temporary = Path(raw).resolve(strict=True)
            stage = temporary / "session"
            _copy(root, snapshot, stage)
            _copy_blobs(root, blobs, stage)
            observation = _observe(stage, temporary, state, server_id, ids, blobs, managed_names)
        if runtime(electron_root) != state:
            fail("browser_runtime_changed")
    return {"schema_version": SCHEMA, "root": str(root), "relative": RELATIVE, "server_id": server_id,
            "agent_ids": ids, "files": snapshot, "runtime": state, "observation": observation, "blobs": blobs,
            "managed_names": managed_names}


def _validate(evidence):
    from .paseo_cleanup_files import ID
    ids = evidence.get("agent_ids")
    if (evidence.get("schema_version") != SCHEMA or evidence.get("relative") != RELATIVE
            or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or ids != sorted(set(ids)) or not isinstance(evidence.get("server_id"), str) or not ID.fullmatch(evidence["server_id"])):
        fail("browser_frozen_scope_invalid")
    frozen_files.checked_path(Path(evidence["root"]), RELATIVE)
    return Path(evidence["root"])


@contextmanager
def prepared(evidence):
    """All native opens and after checks complete before original mutation."""
    root = _validate(evidence)
    require_no_sealed(root)
    if _snapshot(root, RELATIVE) != evidence["files"]:
        if remaining(evidence) == 0:
            yield None
            return
        fail("browser_source_changed")
    blobs = evidence.get("blobs")
    if _blob_snapshot(root) != blobs:
        fail("browser_blob_source_changed")
    if evidence["files"] is None:
        yield None
        return
    state = evidence["runtime"]
    if runtime(state["root"]) != state:
        fail("browser_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-apply-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "stage"
        _copy(root, evidence["files"], stage)
        _copy_blobs(root, blobs, stage)
        observation = _observe(stage, temporary, state, evidence["server_id"], evidence["agent_ids"], blobs, evidence.get("managed_names"))
        if observation != evidence["observation"]:
            fail("browser_frozen_state_changed")
        if observation["selected"]:
            result = _browser(stage, temporary, state, evidence["server_id"], evidence["agent_ids"],
                before=observation["before_sha256"], after=observation["after_sha256"])
            if result["afterSha256"] != observation["after_sha256"] or result["selectedAfter"]:
                fail("browser_after_unverified")
            if blobs is not None:
                _discard_selected_blobs(stage, blobs, state)
        # The exact staged family is reopened only on another private copy.
        snapshot = _snapshot(stage, RELATIVE)
        cold = temporary / "cold"
        _copy(stage, snapshot, cold)
        observed = _observe(cold, temporary, state, evidence["server_id"], evidence["agent_ids"])
        if observed["before_sha256"] != observation["after_sha256"] or observed["selected"]:
            fail("browser_cold_after_unverified")
        if runtime(state["root"]) != state:
            fail("browser_runtime_changed")
        yield (stage, snapshot) if observation["selected"] else None


def install(evidence, prepared_family, *, phase_callback):
    """Caller holds the product lifecycle; no implicit retries or backups."""
    root = _validate(evidence)
    if prepared_family is None:
        if remaining(evidence):
            fail("browser_after_unverified")
        return
    install_family(root, RELATIVE, evidence["files"], prepared_family, phase_callback=phase_callback,
                   verify_after=lambda: _verify_installed(evidence), blobs=evidence.get("blobs"))


def require_no_sealed(root):
    for path in Path(root).iterdir():
        if path.name.casefold().startswith(".larj-paseo-sealed-"):
            fail("cache_install_recovery_required")


def install_family(root, relative, before, prepared_family, *, phase_callback, verify_after, blobs=None):
    if relative not in {RELATIVE, "Local Storage/leveldb"}:
        fail("browser_family_unqualified")
    if relative == RELATIVE:
        if _blob_snapshot(root) != blobs:
            fail("browser_blob_source_changed")
    if _snapshot(root, relative) != before:
        fail("browser_source_changed")
    if prepared_family is None:
        return
    require_no_sealed(root)
    stage, snapshot = prepared_family
    if _snapshot(stage, relative) != snapshot:
        fail("browser_preparation_changed")
    sealed_relative = ".larj-paseo-sealed-" + _digest([str(root), relative, before])[:32]
    sealed = frozen_files.checked_path(root, sealed_relative)
    # A complete durable after copy survives any later interrupted install.
    # No cleanup in finally: partial/unknown seals are recovery evidence.
    phase_callback("mutation_started")
    with frozen_files._parent_fence(root, sealed_relative):
        sealed.mkdir(mode=0o700)
        with frozen_files._parent_fence(root, sealed_relative + "/owner.json"):
            _copy(stage, snapshot, sealed, relative=relative)
            with frozen_files._parent_fence(sealed, relative + "/CURRENT"):
                sealed_snapshot = _snapshot(sealed, relative)
                if [(i["path"], i["size"], i["sha256"]) for i in sealed_snapshot["files"]] != [(i["path"], i["size"], i["sha256"]) for i in snapshot["files"]]:
                    fail("browser_sealed_copy_changed")
                manifest = {"schema_version": "larj.paseo-sealed-after.v1", "root": str(root), "relative": relative,
                            "before_sha256": _digest(before), "files": sealed_snapshot}
                with (sealed / "owner.json").open("x", encoding="utf-8") as stream:
                    json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                owned_seal = frozen_files.freeze_remove(root, sealed_relative)
    # Keep the directory itself pinned while replacing only frozen members.
    # Every removal uses a verified handle; a newly appeared member survives
    # and makes the operation unknown rather than being recursively removed.
    with frozen_files._parent_fence(root, relative + "/CURRENT"):
        if relative == RELATIVE:
            if _blob_snapshot(root) != blobs:
                fail("browser_blob_source_changed")
        if _snapshot(root, relative) != before or frozen_files.freeze_remove(root, sealed_relative) != owned_seal:
            fail("browser_source_changed")
        for item in before["files"]:
            frozen_files._delete_frozen(root, item)
        for item in sorted(snapshot["files"], key=lambda i: Path(i["path"]).name == "CURRENT"):
            proof, body = frozen_files.read_file(stage, item["path"], content=True)
            if proof != item:
                fail("browser_preparation_changed")
            path = frozen_files.checked_path(root, item["path"])
            with path.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        current = _snapshot(root, relative)
        def contents(value):
            return [(i["path"], i["sha256"], i["size"]) for i in value["files"]]
        if contents(current) != contents(snapshot):
            fail("browser_install_unverified")
        if blobs is not None:
            frozen_files.apply_remove(root, blobs)
        verify_after()
        frozen_files.apply_remove(root, owned_seal)


def _verify_installed(evidence):
    root, state = Path(evidence["root"]), evidence["runtime"]
    require_no_blob(root)
    with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-installed-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "session"
        _copy(root, _snapshot(root, RELATIVE), stage)
        value = _observe(stage, temporary, state, evidence["server_id"], evidence["agent_ids"])
    if value["before_sha256"] != evidence["observation"]["after_sha256"] or value["selected"]:
        fail("browser_installed_after_unverified")


def remaining(evidence):
    root = _validate(evidence)
    require_no_sealed(root)
    blobs = _blob_snapshot(root)
    if blobs is not None and blobs != evidence.get("blobs"):
        fail("browser_blob_state_unknown")
    snapshot = _snapshot(root, RELATIVE)
    if snapshot is None:
        if evidence["files"] is not None:
            fail("browser_shared_family_missing")
        return 0
    if evidence["files"] is None:
        fail("browser_unexpected_family")
    state = evidence["runtime"]
    if runtime(state["root"]) != state:
        fail("browser_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-paseo-idb-verify-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "session"
        _copy(root, snapshot, stage)
        _copy_blobs(root, blobs, stage)
        value = _observe(stage, temporary, state, evidence["server_id"], evidence["agent_ids"], blobs, evidence.get("managed_names"))
    expected = evidence["observation"]
    if value["before_sha256"] == expected["after_sha256"] and not value["selected"] and blobs is None:
        return 0
    if snapshot == evidence["files"] and value == expected:
        return value["selected"]
    fail("browser_state_unknown")
