"""Select an explicitly migrated journal layout without changing its OS lock.

The legacy lock remains at its original inode and pathname. A regular marker
inside its operations directory makes older sibling scanners fail closed.
"""
from __future__ import annotations

import re
from pathlib import Path

from .path_identity import canonical_existing_path_key

LEGACY_CONTROL = ".local-agent-record-janitor"
MANAGER_CONTROL = ".local-agent-manager"
LAYOUT_MARKER = ".manager-layout.json"


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


def relocated_plan_path(path: Path) -> Path:
    """Resolve an old exact journal locator through the explicit layout marker."""
    path = path.expanduser().absolute()
    if (path.name == "plan.json" and path.parent.parent.name == "operations"
            and path.parent.parent.parent.name == LEGACY_CONTROL):
        root = path.parent.parent.parent.parent.resolve(strict=True)
        control = control_directory(root)
        if control.name == MANAGER_CONTROL:
            return control / "operations" / path.parent.name / "plan.json"
    return path
