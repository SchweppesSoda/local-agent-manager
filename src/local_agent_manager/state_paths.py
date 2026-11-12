"""Select an explicitly configured journal layout without changing its OS lock.

The legacy lock remains at its original inode and pathname. A regular marker
inside its operations directory makes older sibling scanners fail closed.
"""
from __future__ import annotations

import re
import os
from hashlib import sha256
from pathlib import Path

from .path_identity import canonical_existing_path_key, is_local_absolute_locator

LEGACY_CONTROL = ".local-agent-record-janitor"
MANAGER_CONTROL = ".local-agent-manager"
LAYOUT_MARKER = ".manager-layout.json"
ROUTED_LAYOUT = "lam.state-routing.v1"


def routed_control(root: Path, base: Path) -> Path:
    key = sha256(os.path.normcase(canonical_existing_path_key(root)).encode("utf-8")).hexdigest()
    return base / key / MANAGER_CONTROL


def validate_external_directory(root: Path, directory: Path) -> None:
    from .operation_store import OperationStoreError, _required_lstat, _validate_directory

    if (not is_local_absolute_locator(str(directory)) or directory == root or directory.is_relative_to(root)
            or root.is_relative_to(directory) or directory.resolve(strict=True) != directory):
        raise OperationStoreError("External journal location is not a distinct physical directory")
    for path in (directory, *directory.parents):
        _validate_directory(path, _required_lstat(path))


def read_layout_marker(root: Path) -> dict | None:
    from .operation_store import (
        OperationStoreError, _optional_lstat, _required_lstat,
        _validate_directory, _validate_regular_file, plan_sha256, strict_json_load,
        validate_operation_id,
    )

    _validate_directory(root, _required_lstat(root))
    directory = root / LEGACY_CONTROL
    for path in (directory, directory / "operations"):
        info = _optional_lstat(path)
        if info is None:
            return None
        _validate_directory(path, info)
    path = directory / "operations" / LAYOUT_MARKER
    info = _optional_lstat(path)
    if info is None:
        return None
    _validate_regular_file(path, info)
    marker = strict_json_load(path)
    if not isinstance(marker, dict):
        raise OperationStoreError("Invalid manager layout marker")
    if marker.get("schema_version") == ROUTED_LAYOUT:
        if (set(marker) != {"schema_version", "root", "source", "base", "destination", "identity", "plan_sha256"}
                or not isinstance(marker.get("root"), str)
                or not Path(marker["root"]).is_absolute()
                or Path(canonical_existing_path_key(marker["root"])) != Path(canonical_existing_path_key(root))
                or marker.get("source") not in (LEGACY_CONTROL, MANAGER_CONTROL)
                or not isinstance(marker.get("base"), str)
                or not isinstance(marker.get("destination"), str)
                or marker.get("plan_sha256") != plan_sha256(marker)):
            raise OperationStoreError("External journal routing marker is invalid")
        destination = Path(marker["destination"])
        if destination != routed_control(root, Path(marker["base"])):
            raise OperationStoreError("External journal destination does not match the store identity")
        validate_external_directory(root, destination)
        info = _required_lstat(destination)
        if marker.get("identity") != [info.st_dev, info.st_ino]:
            raise OperationStoreError("External journal directory identity changed")
        _validate_directory(destination / "operations", _required_lstat(destination / "operations"))
        return marker
    if marker.get("phase") == "complete":
        if (marker.get("schema_version") != "lam.state-layout.v1"
                or not isinstance(marker.get("root"), str)
                or not Path(marker["root"]).is_absolute()
                or canonical_existing_path_key(marker["root"]) != canonical_existing_path_key(root)
                or marker.get("destination") != MANAGER_CONTROL
                or not re.fullmatch(r"[0-9a-f]{64}", str(marker.get("migration_plan_sha256", "")))
                or marker.get("plan_sha256") != plan_sha256(marker)):
            raise OperationStoreError("Completed layout marker binding or hash is invalid")
        return marker
    plan = marker.get("plan")
    if (marker.get("schema_version") != "lam.state-layout.v1"
            or marker.get("phase") != "moving"
            or not isinstance(plan, dict)
            or plan.get("schema_version") != "lam.state-migration-plan.v1"
            or not isinstance(plan.get("root"), str)
            or not Path(plan["root"]).is_absolute()
            or canonical_existing_path_key(plan["root"]) != canonical_existing_path_key(root)
            or plan.get("source") != LEGACY_CONTROL
            or plan.get("destination") != MANAGER_CONTROL
            or not isinstance(plan.get("operations"), list)
            or not isinstance(plan.get("unmanaged_entries"), list)
            or not isinstance(plan.get("empty_directories"), list)
            or plan.get("plan_sha256") != plan_sha256(plan)):
        raise OperationStoreError("Manager layout marker binding or hash is invalid")
    ids = []
    for item in plan["operations"]:
        if not isinstance(item, dict) or not isinstance(item.get("operation_id"), str):
            raise OperationStoreError("Invalid migration operation entry")
        if not isinstance(item.get("files"), list):
            raise OperationStoreError("Migration operation is missing its file manifest")
        ids.append(validate_operation_id(item["operation_id"]))
    if len(ids) != len(set(ids)):
        raise OperationStoreError("Duplicate migration operation IDs")
    return marker


def control_directory(root: Path) -> Path:
    from .operation_store import (
        OperationStoreError, _optional_lstat, _required_lstat, _validate_directory,
    )

    marker = read_layout_marker(root)
    if marker is None:
        if _optional_lstat(root / MANAGER_CONTROL) is not None:
            raise OperationStoreError("Unmarked manager state directory requires state migrate inspection")
        return root / LEGACY_CONTROL
    if marker.get("schema_version") == ROUTED_LAYOUT:
        if marker["source"] == MANAGER_CONTROL:
            _validate_directory(root / MANAGER_CONTROL, _required_lstat(root / MANAGER_CONTROL))
            legacy = root / LEGACY_CONTROL
            if {path.name for path in (legacy / "operations").iterdir()} != {".mutation.lock", LAYOUT_MARKER}:
                raise OperationStoreError("Legacy state changed after migration; preserve it for recovery")
            if {path.name for path in legacy.iterdir()} - {"operations", "legacy-index.lock"}:
                raise OperationStoreError("Legacy rollback state appeared after migration; preserve it for recovery")
        elif _optional_lstat(root / MANAGER_CONTROL) is not None:
            raise OperationStoreError("Unmarked manager state directory appeared after routing")
        return Path(marker["destination"])
    if marker["phase"] != "complete":
        raise OperationStoreError("State migration is incomplete; resume state migrate with its original hash")
    legacy = root / LEGACY_CONTROL / "operations"
    if {path.name for path in legacy.iterdir()} != {".mutation.lock", LAYOUT_MARKER}:
        raise OperationStoreError("Legacy state changed after migration; preserve it for recovery")
    if {path.name for path in legacy.parent.iterdir()} - {"operations", "legacy-index.lock"}:
        raise OperationStoreError("Legacy rollback state appeared after migration; preserve it for recovery")
    destination = root / MANAGER_CONTROL
    for path in (destination, destination / "operations"):
        _validate_directory(path, _required_lstat(path))
    return destination


def control_directories(root: Path) -> tuple[Path, ...]:
    """Find both the configured write location and any in-place historical journals."""
    current = control_directory(root)
    marker = read_layout_marker(root)
    if marker is not None and marker.get("schema_version") == ROUTED_LAYOUT:
        return current, root / marker["source"]
    return (current,)


def operation_control(root: Path, operation_id: str) -> Path:
    from .operation_store import OperationStoreError, _optional_lstat, _validate_directory, validate_operation_id

    validate_operation_id(operation_id)
    controls = control_directories(root)
    present = []
    for control in controls:
        path = control / "operations"
        info = _optional_lstat(path)
        if info is not None:
            _validate_directory(path, info)
            if _optional_lstat(path / operation_id) is not None:
                present.append(control)
    if len(present) > 1:
        raise OperationStoreError("Operation ID exists in both routed and historical journals")
    return present[0] if present else controls[0]


def operation_entries(root: Path):
    from .operation_store import OperationStoreError, _optional_lstat, _validate_directory

    entries = {}
    for control in control_directories(root):
        directory = control / "operations"
        info = _optional_lstat(directory)
        if info is not None:
            _validate_directory(directory, info)
            for path in sorted(directory.iterdir()):
                if path.name == ".mutation.lock" or (path.name == LAYOUT_MARKER and control == root / LEGACY_CONTROL):
                    continue
                key = os.path.normcase(path.name)
                if key in entries:
                    raise OperationStoreError("Duplicate operation ID across journal locations")
                entries[key] = path
    return tuple(entries.values())


def relocated_plan_path(path: Path) -> Path:
    """Resolve an old exact journal locator through the explicit layout marker."""
    path = path.expanduser().absolute()
    if (path.name == "plan.json" and path.parent.parent.name == "operations"
            and path.parent.parent.parent.name in {LEGACY_CONTROL, MANAGER_CONTROL}
            and not path.exists()):
        root = path.parent.parent.parent.parent.resolve(strict=True)
        control = operation_control(root, path.parent.name)
        return control / "operations" / path.parent.name / "plan.json"
    return path
