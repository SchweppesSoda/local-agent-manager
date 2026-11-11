"""T3 Code draft/layout rewrites with admission for known desktop stores."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile

from . import office_leveldb as level
from .paseo_indexeddb import _copy, install_family, require_no_sealed
from .t3code_store import ID, fail
from . import frozen_files

SCHEMA = "larj.t3code-local-storage.v1"
RELATIVE = "Local Storage/leveldb"


def _require_qualified_desktop(root, environment_id, ids, indexeddb=None):
    if indexeddb is not None:
        from .t3code_desktop_cleanup import require_bound_state
        require_bound_state(indexeddb, root, environment_id, ids)
        return
    from .t3code_desktop_cleanup import require_absent
    require_absent(root, environment_id, ids)


def _call(path, evidence, *, apply=False):
    request = {"schema_version": level.PROTOCOL, "operation": "apply" if apply else "inspect-copy",
        "path": str(path), "chat_ids": evidence["thread_ids"], "sub_chat_ids": [],
        "origins": ["t3code://app"], "t3code_environment_id": evidence["environment_id"]}
    if apply:
        observed = evidence["observation"]
        edits = [{"key": i["key_sha256"], "after": i["after_sha256"], "meta": i["after_metadata_sha256"]}
                 for i in observed["edits"]]
        request.update(authorized_before_sha256=observed["fingerprint"],
                       authorized_after_sha256=hashlib.sha256(level._json(edits)).hexdigest())
    return level._call(request)


def _validate_scope(evidence):
    ids = evidence.get("thread_ids")
    if (evidence.get("schema_version") != SCHEMA or evidence.get("relative") != RELATIVE
            or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or ids != sorted(set(ids)) or not isinstance(evidence.get("environment_id"), str) or not ID.fullmatch(evidence["environment_id"])):
        fail("local_storage_scope_invalid")
    frozen_files.checked_path(Path(evidence["root"]), RELATIVE)
    if evidence.get('indexeddb') is not None:
        from .t3code_desktop_cleanup import validate_plan
        plan = evidence['indexeddb']
        root = validate_plan(plan)
        if (root.resolve() != Path(evidence['root']).resolve()
                or plan['evidence']['environment'] != evidence['environment_id']
                or plan['evidence']['ids'] != ids or plan['schema_version'] != 'lam.t3-desktop-full.v1'):
            fail('desktop_full_scope_changed')
    return Path(evidence["root"])


def _validate(evidence):
    root = _validate_scope(evidence)
    _require_qualified_desktop(root, evidence['environment_id'], evidence['thread_ids'], evidence.get('indexeddb'))
    return root


def freeze(root, environment_id, ids, *, indexeddb=None):
    root = Path(root)
    _require_qualified_desktop(root, environment_id, ids, indexeddb)
    require_no_sealed(root)
    if (not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
            or not isinstance(environment_id, str) or not ID.fullmatch(environment_id)):
        fail("local_storage_selection_invalid")
    evidence = {"schema_version": SCHEMA, "root": str(root), "relative": RELATIVE,
        "thread_ids": sorted(set(ids)), "environment_id": environment_id, "files": level._snapshot(root, RELATIVE),
        "runtime": None, "observation": None}
    if indexeddb is not None:
        evidence['indexeddb'] = indexeddb
    if evidence["files"]:
        evidence["runtime"] = level.runtime_fingerprint()
        with tempfile.TemporaryDirectory(prefix="larj-t3code-local-") as raw:
            stage = Path(raw).resolve(strict=True) / "stage"
            _copy(root, evidence["files"], stage, relative=RELATIVE)
            evidence["observation"] = _call(stage / RELATIVE, evidence)
        if level.runtime_fingerprint() != evidence["runtime"]:
            fail("local_storage_runtime_changed")
    return evidence


@contextmanager
def prepared(evidence):
    root = _validate(evidence)
    require_no_sealed(root)
    if level._snapshot(root, RELATIVE) != evidence["files"]:
        if remaining(evidence) == 0:
            yield None
            return
        fail("local_storage_source_changed")
    if evidence["files"] is None:
        yield None
        return
    if level.runtime_fingerprint() != evidence["runtime"]:
        fail("local_storage_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-t3code-local-apply-") as raw:
        temporary = Path(raw).resolve(strict=True)
        stage = temporary / "stage"
        _copy(root, evidence["files"], stage, relative=RELATIVE)
        observed = _call(stage / RELATIVE, evidence)
        if observed != evidence["observation"]:
            fail("local_storage_frozen_state_changed")
        changes = observed["count"]["removed_pairs"]
        if changes:
            _call(stage / RELATIVE, evidence, apply=True)
        snapshot = level._snapshot(stage, RELATIVE)
        cold = temporary / "cold"
        _copy(stage, snapshot, cold, relative=RELATIVE)
        result = _call(cold / RELATIVE, evidence)
        if (result["logical_before_sha256"] != observed["logical_after_sha256"]
                or result["count"]["removed_pairs"]):
            fail("local_storage_cold_after_unverified")
        if level.runtime_fingerprint() != evidence["runtime"]:
            fail("local_storage_runtime_changed")
        yield (stage, snapshot) if changes else None


def install(evidence, family, *, phase_callback):
    root = _validate(evidence)
    if family is None:
        if remaining(evidence):
            fail("local_storage_after_unverified")
        return
    install_family(root, RELATIVE, evidence["files"], family, phase_callback=phase_callback,
                   verify_after=lambda: _verify_installed(evidence))


def _verify_installed(evidence):
    root = Path(evidence["root"])
    with tempfile.TemporaryDirectory(prefix="larj-t3code-local-installed-") as raw:
        stage = Path(raw).resolve(strict=True) / "stage"
        _copy(root, level._snapshot(root, RELATIVE), stage, relative=RELATIVE)
        value = _call(stage / RELATIVE, evidence)
    if (value["logical_before_sha256"] != evidence["observation"]["logical_after_sha256"]
            or value["count"]["removed_pairs"]):
        fail("local_storage_installed_after_unverified")


def remaining(evidence):
    root = _validate(evidence)
    require_no_sealed(root)
    current = level._snapshot(root, RELATIVE)
    if current is None:
        if evidence["files"] is not None:
            fail("local_storage_shared_family_missing")
        return 0
    if evidence["files"] is None:
        fail("local_storage_unexpected_family")
    if level.runtime_fingerprint() != evidence["runtime"]:
        fail("local_storage_runtime_changed")
    with tempfile.TemporaryDirectory(prefix="larj-t3code-local-verify-") as raw:
        stage = Path(raw).resolve(strict=True) / "stage"
        _copy(root, current, stage, relative=RELATIVE)
        value = _call(stage / RELATIVE, evidence)
    expected = evidence["observation"]
    if value["logical_before_sha256"] == expected["logical_after_sha256"] and not value["count"]["removed_pairs"]:
        return 0
    if current == evidence["files"] and value == expected:
        return value["count"]["removed_pairs"]
    from .joint_cli_recovery import desktop_evolution
    if desktop_evolution(evidence, value):
        return 0
    fail("local_storage_state_unknown")
