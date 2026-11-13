"""Locate manager-owned operation state outside every Agent data directory.

Each physical store gets its own directory below one manager state base.
Older in-store layouts (``.local-agent-record-janitor``/``.local-agent-manager``)
and routing markers are read only as history until ``state relocate`` moves
their journals out and removes them.
"""
from __future__ import annotations

import re
import os
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path

from .path_identity import canonical_existing_path_key, is_local_absolute_locator

LEGACY_CONTROL = ".local-agent-record-janitor"
MANAGER_CONTROL = ".local-agent-manager"
LAYOUT_MARKER = ".manager-layout.json"
ROUTED_LAYOUT = "lam.state-routing.v1"
RELOCATION_DIRECTORY = ".relocate"
RELOCATION_MANIFEST = ".plan.json"
_relocation_root: ContextVar[Path | None] = ContextVar("journal_relocation_root", default=None)


@contextmanager
def relocation_recovery(root: Path):
    token = _relocation_root.set(root)
    try:
        yield
    finally:
        _relocation_root.reset(token)


def routed_control(root: Path, base: Path) -> Path:
    key = sha256(os.path.normcase(canonical_existing_path_key(root)).encode("utf-8")).hexdigest()
    return base / key[:32] / MANAGER_CONTROL


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


STATE_ENVIRONMENT = "LAM_STATE_DIR"
CHECKOUT_STATE = ".agent-manager-state"
USER_STATE = "local-agent-manager"
LEGACY_USER_STATE = "local-agent-record-janitor"
PLANS_DIRECTORY = "plans"
HANDOFFS_DIRECTORY = "handoffs"
_HISTORICAL_CONTROLS = (LEGACY_CONTROL, MANAGER_CONTROL)


def user_state_root() -> Path:
    """The per-user application state root (LOCALAPPDATA / XDG_STATE_HOME)."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"])
    if os.environ.get("XDG_STATE_HOME"):
        return Path(os.environ["XDG_STATE_HOME"])
    return Path.home() / ("AppData/Local" if os.name == "nt" else ".local/state")


def source_checkout() -> Path | None:
    """The local-agent-manager checkout this package runs from, if any."""
    package = Path(__file__).resolve().parent
    root = package.parent.parent
    project = root / "pyproject.toml"
    if package.parent.name != "src" or not project.is_file():
        return None
    try:
        text = project.read_text(encoding="utf-8")
    except OSError:
        return None
    return root if re.search(r'^name\s*=\s*"local-agent-manager"\s*$', text, re.MULTILINE) else None


def manager_state_base() -> Path:
    """One manager-owned directory for all operation state.

    Journals, locks, rollback copies, plans, run receipts and handoff evidence
    live here, partitioned per physical store. Nothing is written inside an
    Agent's own data directory. ``LAM_STATE_DIR`` selects the directory
    explicitly; a source checkout uses its ignored ``.agent-manager-state``;
    an installed package uses the per-user ``local-agent-manager/state``.
    """
    configured = os.environ.get(STATE_ENVIRONMENT)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            from .operation_store import OperationStoreError
            raise OperationStoreError(f"{STATE_ENVIRONMENT} must be an absolute path")
        return path.absolute()
    checkout = source_checkout()
    if checkout is not None:
        return checkout / CHECKOUT_STATE
    return user_state_root() / USER_STATE / "state"


STORE_RECORD = "store.json"
STORE_RECORD_SCHEMA = "lam.state-store.v1"


def record_store_root(root: Path, control: Path) -> None:
    """Name the physical store a per-store state directory belongs to."""
    from .operation_store import OperationStoreError, _optional_lstat, write_new_json

    record = control.parent / STORE_RECORD
    if _optional_lstat(record) is None:
        try:
            # Exclusive create: the record is written once and never replaced.
            write_new_json(record, {"schema_version": STORE_RECORD_SCHEMA, "root": str(root)})
        except OperationStoreError:
            if _optional_lstat(record) is None:
                raise


def store_root_for_control(control: Path) -> Path | None:
    """Map a manager-state control directory back to its store root."""
    from .operation_store import _optional_lstat, strict_json_load

    record = control.parent / STORE_RECORD
    if _optional_lstat(record) is None:
        return None
    value = strict_json_load(record)
    if not isinstance(value, dict) or value.get("schema_version") != STORE_RECORD_SCHEMA:
        return None
    root = Path(str(value.get("root", "")))
    if not root.is_absolute() or not root.is_dir():
        return None
    try:
        if os.path.normcase(str(routed_control(root, control.parent.parent))) != os.path.normcase(str(control)):
            return None
    except OSError:
        return None
    return root


def journal_store_root(control: Path) -> Path:
    """The store root for a journal control directory, new or historical."""
    return store_root_for_control(control) or control.parent


def ensure_state_base() -> Path:
    from .operation_store import _required_lstat, _validate_directory

    base = manager_state_base()
    base.mkdir(parents=True, exist_ok=True)
    _validate_directory(base, _required_lstat(base))
    return base


def _historical_marker(root: Path) -> dict | None:
    """Validate an old in-store layout marker; unfinished moves fail closed."""
    from .operation_store import OperationStoreError, _optional_lstat

    marker = read_layout_marker(root)
    if marker is None:
        return None
    if marker.get("schema_version") == ROUTED_LAYOUT:
        destination = Path(marker["destination"])
        if (_relocation_root.get() != root
                and _optional_lstat(destination / RELOCATION_DIRECTORY / RELOCATION_MANIFEST) is not None):
            raise OperationStoreError("Journal relocation is incomplete; resume state relocate with its original hash")
        return marker
    if marker.get("phase") != "complete":
        raise OperationStoreError("An old in-store state migration is incomplete; preserve it for recovery")
    return marker


def control_directory(root: Path) -> Path:
    """The manager-owned control directory for one physical store."""
    from .operation_store import OperationStoreError, _optional_lstat

    _historical_marker(root)
    target = routed_control(root, manager_state_base())
    if _relocation_root.get() != root and _optional_lstat(target / RELOCATION_DIRECTORY / RELOCATION_MANIFEST) is not None:
        raise OperationStoreError("Journal relocation is incomplete; resume state relocate with its original hash")
    return target


def historical_controls(root: Path) -> tuple[Path, ...]:
    """Old journal locations that may still hold history for this store."""
    from .operation_store import _optional_lstat

    current = control_directory(root)
    found: list[Path] = []
    marker = _historical_marker(root)
    if marker is not None and marker.get("schema_version") == ROUTED_LAYOUT:
        destination = Path(marker["destination"])
        if os.path.normcase(str(destination)) != os.path.normcase(str(current)) and _optional_lstat(destination) is not None:
            found.append(destination)
    for name in _HISTORICAL_CONTROLS:
        if _optional_lstat(root / name) is not None:
            found.append(root / name)
    return tuple(found)


def control_directories(root: Path) -> tuple[Path, ...]:
    """The current write location first, then any historical journal locations."""
    return (control_directory(root), *historical_controls(root))


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
                if path.name == ".mutation.lock" or (path.name == LAYOUT_MARKER and control.parent == root):
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
        root = journal_store_root(path.parent.parent.parent).resolve(strict=True)
        control = operation_control(root, path.parent.name)
        return control / "operations" / path.parent.name / "plan.json"
    return path
