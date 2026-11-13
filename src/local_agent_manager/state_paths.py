"""One installed checkout owns all manager state for this Python installation."""
from __future__ import annotations

import os
from hashlib import sha256
from importlib import metadata
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
import json
import re

from .path_identity import canonical_existing_path_key, is_local_absolute_locator

MANAGER_CONTROL = ".local-agent-manager"
CHECKOUT_STATE = ".agent-manager-state"
PLANS_DIRECTORY = "plans"
HANDOFFS_DIRECTORY = "handoffs"
STORE_RECORD = "store.json"
STORE_RECORD_SCHEMA = "lam.state-store.v1"


@lru_cache(maxsize=1)
def authoritative_checkout() -> Path:
    """Resolve this Python installation's registered editable checkout.

    Python's normal distribution lookup selects the installation. Duplicate
    registrations within that installation are rejected. Other source checkouts
    must use this same interpreter; separate Python installations are separate
    administrative installations and are not a cross-interpreter registry.
    """
    from .operation_store import OperationStoreError

    try:
        distribution = metadata.distribution("local-agent-manager")
        # A checkout's setuptools build metadata is not an installation. It
        # can appear before site-packages when another checkout is on sys.path.
        # Ignore only that source format, never a malformed installed dist-info.
        if str(getattr(distribution, "_path", "")).endswith(".egg-info"):
            distribution = next((item for item in metadata.distributions(name="local-agent-manager")
                if str(getattr(item, "_path", "")).endswith(".dist-info")), None)
            if distribution is None:
                raise ValueError("an installed editable dist-info registration is required")
        location = Path(distribution.locate_file("")).resolve(strict=True)
        matches = [item for item in metadata.distributions(path=[str(location)])
                   if re.sub(r"[-_.]+", "-", item.metadata.get("Name", "")).lower() == "local-agent-manager"]
        if len(matches) != 1:
            raise ValueError("exactly one local-agent-manager registration is required in the selected installation")
        value = json.loads(distribution.read_text("direct_url.json") or "null")
        if not isinstance(value, dict) or value.get("dir_info", {}).get("editable") is not True:
            raise ValueError("an editable installation is required")
        url = urlsplit(value.get("url", ""))
        if url.scheme != "file" or url.netloc not in ("", "localhost") or url.query or url.fragment:
            raise ValueError("the editable checkout must be a local directory")
        root = Path(url2pathname(url.path)).resolve(strict=True)
        project = (root / "pyproject.toml").read_text(encoding="utf-8")
        if not re.search(r'^name\s*=\s*"local-agent-manager"\s*$', project, re.MULTILINE):
            raise ValueError("the registered directory is not local-agent-manager")
        return root
    except (OSError, ValueError, TypeError, AttributeError, metadata.PackageNotFoundError) as exc:
        raise OperationStoreError(f"Cannot locate the authoritative manager checkout: {exc}") from exc


def manager_state_base() -> Path:
    """All journals, locks, plans and handoffs share the registered checkout."""
    return authoritative_checkout() / CHECKOUT_STATE


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


def _validate_store_record(root: Path, control: Path) -> None:
    from .operation_store import OperationStoreError, _required_lstat, _validate_regular_file, strict_json_load

    record = control.parent / STORE_RECORD
    _validate_regular_file(record, _required_lstat(record))
    value = strict_json_load(record)
    expected = {"schema_version": STORE_RECORD_SCHEMA, "root": str(root)}
    if value != expected or routed_control(root, control.parent.parent) != control:
        raise OperationStoreError("Manager state store binding does not match the physical store")


def record_store_root(root: Path, control: Path) -> None:
    """Publish a complete binding under the caller's permanent per-store gate.

    Existing bindings are validated and never replaced. The same-directory
    atomic writer fsyncs its complete temporary payload before os.replace;
    process loss before or after publication leaves the next owner able to
    establish or validate a complete, single-link binding.
    """
    from .operation_store import OperationStoreError, _optional_lstat, atomic_write_json

    record = control.parent / STORE_RECORD
    if _optional_lstat(record) is not None:
        _validate_store_record(root, control)
        return
    try:
        atomic_write_json(record, {"schema_version": STORE_RECORD_SCHEMA, "root": str(root)})
    except OperationStoreError as exc:
        raise OperationStoreError(f"Could not publish manager state store binding: {exc}") from exc
    _validate_store_record(root, control)


def store_root_for_control(control: Path) -> Path | None:
    from .operation_store import _optional_lstat, strict_json_load

    record = control.parent / STORE_RECORD
    if _optional_lstat(record) is None:
        return None
    value = strict_json_load(record)
    if not isinstance(value, dict) or not isinstance(value.get("root"), str):
        from .operation_store import OperationStoreError
        raise OperationStoreError("Invalid manager state store binding")
    root = Path(value["root"])
    _validate_store_record(root, control)
    return root if root.is_absolute() and root.is_dir() else None


def journal_store_root(control: Path) -> Path:
    from .operation_store import OperationStoreError
    root = store_root_for_control(control)
    if root is None:
        raise OperationStoreError("Manager journal is missing its store binding")
    return root


def ensure_state_base() -> Path:
    from .operation_store import _required_lstat, _validate_directory
    base = manager_state_base()
    base.mkdir(parents=True, exist_ok=True)
    _validate_directory(base, _required_lstat(base))
    return base


def control_directory(root: Path) -> Path:
    return routed_control(root, manager_state_base())


def control_directories(root: Path) -> tuple[Path, ...]:
    return (control_directory(root),)


def operation_control(root: Path, operation_id: str) -> Path:
    from .operation_store import validate_operation_id
    validate_operation_id(operation_id)
    return control_directory(root)


def operation_entries(root: Path):
    from .operation_store import _optional_lstat, _validate_directory
    directory = control_directory(root) / "operations"
    info = _optional_lstat(directory)
    if info is None:
        return ()
    _validate_directory(directory, info)
    return tuple(path for path in sorted(directory.iterdir()) if path.name != ".mutation.lock")
