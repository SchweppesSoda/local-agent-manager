"""Reviewable, root-locked maintenance of manager journals, never chat stores."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from .legacy_index import _fsync_directory
from .mutation_guard import (
    MutationRootLockedError, _journal_unknown, _os_lock, mutation_roots, scopes_for_frozen_plan,
)
from .path_identity import canonical_existing_path_key
from .operation_store import (
    OperationStore, OperationStoreError, _file_identity, _optional_lstat, _parse_utc_timestamp,
    _required_lstat, _validate_directory, _validate_regular_file, atomic_write_json,
    plan_sha256, strict_json_load, validate_operation_id,
)
from .state_paths import (
    LAYOUT_MARKER, LEGACY_CONTROL, MANAGER_CONTROL, RELOCATION_DIRECTORY, RELOCATION_MANIFEST,
    control_directories, control_directory, manager_state_base, operation_entries, relocation_recovery,
)

_DETAIL_FILES = {"plan.json", "events.jsonl", "state.json", "result.json"}
_JOURNAL_FILES = _DETAIL_FILES | {"receipt.json", "apply.lock"}
_ROLLBACK_DIRECTORIES = {"legacy-index-backups", "desktop-state-backups"}
_RELOCATION = "lam.state-relocation-plan.v1"


@contextmanager
def _existing_index_lock(root: Path, *, include_store: bool = True):
    """Exclude current and older index writers without changing lock payloads.

    Relocation removes the old in-store lock itself and proves it is free
    immediately before unlinking, so it does not hold that file open.
    """
    from contextlib import ExitStack

    paths = [control_directory(root) / "legacy-index.lock"]
    if include_store:
        paths.append(root / LEGACY_CONTROL / "legacy-index.lock")
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(_held_index_lock(path))
        yield


@contextmanager
def _held_index_lock(path: Path):
    before = _optional_lstat(path)
    if before is None:
        yield
        return
    _validate_regular_file(path, before)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    locked = False
    try:
        opened = os.fstat(fd)
        _validate_regular_file(path, opened)
        if _file_identity(before) != _file_identity(opened):
            raise OperationStoreError("Legacy index lock changed while opening")
        try:
            _os_lock(fd)
        except OSError as exc:
            raise MutationRootLockedError("An older index writer owns this state root") from exc
        locked = True
        _validate_regular_file(path, _required_lstat(path))
        if _file_identity(_required_lstat(path)) != _file_identity(opened):
            raise OperationStoreError("Legacy index lock identity changed")
        yield
    finally:
        if locked:
            _os_lock(fd, unlock=True)
        os.close(fd)


def _file_snapshot(path: Path) -> dict:
    before = _required_lstat(path)
    _validate_regular_file(path, before)
    payload = path.read_bytes()
    after = _required_lstat(path)
    _validate_regular_file(path, after)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise OperationStoreError(f"State file changed while reading: {path}")
    return {"name": path.name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _journal_snapshot(directory: Path) -> list[dict]:
    _validate_directory(directory, _required_lstat(directory))
    files = sorted(directory.iterdir())
    if any(path.name not in _JOURNAL_FILES for path in files):
        raise OperationStoreError("Journal contains unexpected files; preserve for recovery")
    return [_file_snapshot(path) for path in files]


def _inspect_operation(root: Path, operation_id: str) -> dict:
    row = {"operation_id": operation_id, "action": "retain"}
    try:
        store = OperationStore(root, operation_id)
        files = _journal_snapshot(store.directory)
        row.update(files=files, bytes=sum(item["size"] for item in files))
        if store.lock_exists():
            raise OperationStoreError("Existing apply.lock must be preserved; status/verify is required")
        receipt = store._read_receipt()
        if receipt is not None:
            expires = _parse_utc_timestamp(receipt["receipt_expires_at"])
            expired = expires <= datetime.now(timezone.utc)
            details = bool({item["name"] for item in files} & _DETAIL_FILES)
            action = "retain"
            if expired:
                action = "expire"
            elif details:
                action = "compact"
            row.update(goal_status=receipt["goal_status"], trusted_terminal=True,
                       action=action,
                       reason="expired_receipt" if expired else "retained_receipt",
                       receipt_expires_at=receipt["receipt_expires_at"])
            return row
        scopes_for_frozen_plan(store.read_plan())
        unknown, _ = _journal_unknown(store)
        result = store.read_result(maintain=False)
        state = store.read_state()
        if state is not None:
            row["goal_status"] = state["goal_status"]
        if unknown or state is None or state["goal_status"] == "unknown":
            raise OperationStoreError("Unresolved journal requires operation status/verify before maintenance")
        if result is None:
            events = store.read_events()
            if (not events or events[0]["event"] != "plan_accepted"
                    or events[-1].get("goal_status") != state["goal_status"]
                    or events[-1]["event"] not in {"batch_finished", "verification_finished", "operation_finished"}):
                raise OperationStoreError("Terminal checkpoint has no trustworthy final event")
            # Historical child writers kept a checkpoint and events without a
            # result document. Relocation preserves that recovery evidence.
            # Compaction requires the stronger result/verification contract.
            row.update(trusted_terminal=True, reason="terminal_result_missing",
                       message="Known checkpoint can migrate intact; retain its details until operation verify publishes a result")
            return row
        row.update(goal_status=result["goal_status"], trusted_terminal=True,
                   action="compact", reason="validated_terminal_journal")
    except (OSError, ValueError, OperationStoreError) as exc:
        row.update(trusted_terminal=False, reason="recovery_required", message=str(exc))
    return row


def _inspect_locked(root: Path) -> dict:
    control = control_directory(root)
    operations = control / "operations"
    if _optional_lstat(control) is not None:
        _validate_directory(control, _required_lstat(control))
    if _optional_lstat(operations) is not None:
        _validate_directory(operations, _required_lstat(operations))
        for path in sorted(operations.iterdir()):
            if path.name == ".mutation.lock":
                _validate_regular_file(path, _required_lstat(path))
                continue
    rows = [_inspect_operation(root, path.name) for path in operation_entries(root)]
    extras = []
    empty_directories = []
    for location in control_directories(root):
        if not location.exists():
            continue
        for path in sorted(location.iterdir()):
            if path.name == "operations":
                continue
            if path.name == "legacy-index.lock":
                _validate_regular_file(path, _required_lstat(path))
                continue
            if location != control and path.name == "desktop-cleanups":
                continue
            extras.append(path.name)
            if path.name in _ROLLBACK_DIRECTORIES:
                _validate_directory(path, _required_lstat(path))
                if location == control and not any(path.iterdir()):
                    empty_directories.append(path.name)
    historical = [str(path) for path in control_directories(root)[1:] if path.exists()]
    return {"schema_version": "lam.state-inventory.v1", "root": str(root),
            "control_directory": str(control), "coordination_lock": str(
                control / "operations" / ".mutation.lock"),
            "historical_controls": historical,
            "operations": rows, "unmanaged_entries": extras, "empty_directories": empty_directories,
            "operation_count": len(rows), "bytes": sum(row.get("bytes", 0) for row in rows)}


def _maintenance_plan(root: Path, command: str) -> dict:
    report = _inspect_locked(root)
    rows = report["operations"]
    plan = {"schema_version": f"lam.state-{command}-plan.v1", "root": str(root),
            "source": Path(report["control_directory"]).name,
            "operations": rows, "unmanaged_entries": report["unmanaged_entries"],
            "empty_directories": report["empty_directories"]}
    if command == "migration":
        plan["destination"] = MANAGER_CONTROL
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def _require_approval(plan: dict, approved_hash: str | None) -> None:
    if not approved_hash or approved_hash != plan["plan_sha256"]:
        raise OperationStoreError("State plan changed or approval is missing; inspect the current plan and use its hash")


def _cleanup_locked(root: Path, *, apply: bool = False, approved_hash: str | None = None) -> dict:
    plan = _maintenance_plan(root, "cleanup")
    result = {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": "cleanup",
              "plan": plan, "plan_sha256": plan["plan_sha256"], "modified": False,
              "phase": "planned",
              "removed_empty_directories": [],
              "completed": [], "retained": [row["operation_id"] for row in plan["operations"]
                                             if row["action"] == "retain"]}
    if not apply:
        return result
    _require_approval(plan, approved_hash)
    for row in plan["operations"]:
        if row["action"] == "retain":
            continue
        store = OperationStore(root, row["operation_id"])
        if _journal_snapshot(store.directory) != row["files"]:
            raise OperationStoreError("Journal changed after the approved snapshot")
        receipt = store._read_receipt()
        if receipt is None:
            durable = store.read_result(maintain=False)
            if durable is None:
                raise OperationStoreError("Terminal journal lost its result")
            store.compact_completed(durable)
        else:
            store._discard_detailed_files()
        if row["action"] == "expire" and not store._purge_expired_receipt_if_needed():
            raise OperationStoreError("Approved receipt expiry could not be verified")
        result["completed"].append(row["operation_id"])
        result["modified"] = True
    for name in plan["empty_directories"]:
        path = control_directory(root) / name
        _validate_directory(path, _required_lstat(path))
        path.rmdir()
        _fsync_directory(path.parent)
        result["removed_empty_directories"].append(name)
        result["modified"] = True
    result["after"] = _inspect_locked(root)
    result["phase"] = "complete"
    return result


def _ensure_plain_directory(path: Path) -> None:
    if _optional_lstat(path) is None:
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    _validate_directory(path, _required_lstat(path))


def _publish_journal(old: Path, staged: Path, destination: Path, expected: list[dict]) -> None:
    """Publish a verified destination before removing any original file."""
    if _optional_lstat(destination) is None:
        if _journal_snapshot(old) != expected:
            raise OperationStoreError("Original journal changed before relocation")
        _ensure_plain_directory(staged)
        files = {item["name"]: item for item in expected}
        for entry in staged.iterdir():
            if entry.name not in files:
                raise OperationStoreError("Unexpected staged journal file")
            _validate_regular_file(entry, _required_lstat(entry))
        for name, proof in files.items():
            source = old / name
            target = staged / name
            _validate_regular_file(source, _required_lstat(source))
            payload = source.read_bytes()
            if len(payload) != proof["size"] or hashlib.sha256(payload).hexdigest() != proof["sha256"]:
                raise OperationStoreError("Original journal file changed while copying")
            if _optional_lstat(target) is not None:
                existing = target.read_bytes()
                if not payload.startswith(existing):
                    raise OperationStoreError("Staged journal bytes do not match the original")
                mode = "r+b"
            else:
                mode = "xb"
            with target.open(mode) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        if _journal_snapshot(staged) != expected or _journal_snapshot(old) != expected:
            raise OperationStoreError("Journal copies could not be verified before publication")
        _fsync_directory(staged)
        os.rename(staged, destination)
        _fsync_directory(destination.parent)
        _fsync_directory(staged.parent)
    if _optional_lstat(staged) is not None or _journal_snapshot(destination) != expected:
        raise OperationStoreError("Published journal does not match the frozen manifest")
    if _optional_lstat(old) is not None:
        remaining = _journal_snapshot(old)
        if any(item not in expected for item in remaining):
            raise OperationStoreError("Original journal no longer matches the frozen manifest")
        for item in remaining:
            path = old / item["name"]
            if _file_snapshot(path) != item:
                raise OperationStoreError("Original journal changed before unlink")
            path.unlink()
        old.rmdir()
        _fsync_directory(old.parent)


_REMOVABLE_FILES = {("operations", ".mutation.lock"), ("operations", LAYOUT_MARKER), ("legacy-index.lock",)}
_REMOVABLE_DIRECTORIES = {("operations",), ("desktop-cleanups",), *((name,) for name in _ROLLBACK_DIRECTORIES)}


def _store_control_manifest(control: Path, moving: set[str]) -> list[dict] | None:
    """Entries of an old in-store control directory that may be removed.

    Only coordination files (lock, layout marker, legacy index lock) and empty
    containers qualify; journals planned to move are excluded. Anything else
    keeps the directory in place.
    """
    entries: list[dict] = []
    for current, directories, files in os.walk(control, followlinks=False):
        relative = Path(current).relative_to(control).parts
        for name in list(directories):
            path = Path(current) / name
            _validate_directory(path, _required_lstat(path))
            parts = (*relative, name)
            if parts[:1] == ("operations",) and len(parts) == 2 and name in moving:
                directories.remove(name)
                continue
            if parts not in _REMOVABLE_DIRECTORIES:
                return None
            entries.append({"path": "/".join(parts), "kind": "directory"})
        for name in files:
            path = Path(current) / name
            info = _required_lstat(path)
            _validate_regular_file(path, info)
            parts = (*relative, name)
            if parts not in _REMOVABLE_FILES:
                return None
            entries.append({"path": "/".join(parts), "kind": "file", "size": info.st_size})
    return sorted(entries, key=lambda item: item["path"])


def _remove_store_control(control: Path, manifest: list[dict]) -> None:
    if _store_control_manifest(control, set()) != manifest:
        raise OperationStoreError(f"Old in-store state changed after the approved preview: {control}")
    for item in manifest:
        if item["kind"] != "file":
            continue
        path = control / item["path"]
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        try:
            try:
                _os_lock(fd)
            except OSError as exc:
                raise MutationRootLockedError(f"An older manager process still holds {path}") from exc
            _os_lock(fd, unlock=True)
        finally:
            os.close(fd)
        path.unlink()
    for item in sorted((item for item in manifest if item["kind"] == "directory"),
                       key=lambda item: -item["path"].count("/")):
        (control / item["path"]).rmdir()
    control.rmdir()
    _fsync_directory(control.parent)


def _relocate_locked(root: Path, *, apply: bool, approved_hash: str | None) -> dict:
    """Move old journals into manager state and remove old in-store directories."""
    destination, *sources = control_directories(root)
    staging = destination / RELOCATION_DIRECTORY
    manifest = staging / RELOCATION_MANIFEST
    if _optional_lstat(staging) is not None:
        _validate_directory(staging, _required_lstat(staging))
    binding = {"schema_version": _RELOCATION, "root": str(root), "destination": str(destination),
               "sources": [str(source) for source in sources]}
    resumed = _optional_lstat(manifest) is not None
    if resumed:
        _validate_regular_file(manifest, _required_lstat(manifest))
        plan = strict_json_load(manifest)
        if (not isinstance(plan, dict) or any(plan.get(key) != value for key, value in binding.items())
                or not isinstance(plan.get("operations"), list) or not isinstance(plan.get("retained"), list)
                or not isinstance(plan.get("remove_controls"), list)
                or plan.get("plan_sha256") != plan_sha256(plan)):
            raise OperationStoreError("Relocation manifest binding or hash is invalid")
    else:
        rows = []
        for row in _inspect_locked(root)["operations"]:
            source = next((item for item in sources
                           if _optional_lstat(item / "operations" / row["operation_id"]) is not None), None)
            if source is not None:
                rows.append({**row, "source": str(source)})
        moving = {row["operation_id"] for row in rows if row["trusted_terminal"]}
        removable = []
        for source in sources:
            if source.parent != root:
                continue
            entries = _store_control_manifest(source, moving)
            if entries is not None:
                removable.append({"path": str(source), "entries": entries})
        plan = {**binding,
                "operations": [{"operation_id": row["operation_id"], "source": row["source"], "files": row["files"]}
                               for row in rows if row["trusted_terminal"]],
                "retained": [row["operation_id"] for row in rows if not row["trusted_terminal"]],
                "remove_controls": removable,
                "retained_controls": [str(source) for source in sources
                                      if str(source) not in {item["path"] for item in removable}]}
        plan["plan_sha256"] = plan_sha256(plan)
    entries = {}
    for row in plan["operations"]:
        if (not isinstance(row, dict) or not isinstance(row.get("operation_id"), str)
                or not isinstance(row.get("files"), list) or row.get("source") not in binding["sources"]):
            raise OperationStoreError("Relocation manifest contains an invalid journal entry")
        operation_id = validate_operation_id(row["operation_id"])
        files = row["files"]
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in files):
            raise OperationStoreError("Relocation manifest contains an invalid file entry")
        names = {item["name"] for item in files}
        if (operation_id in entries or not names or len(names) != len(files)
                or names - (_JOURNAL_FILES - {"apply.lock"})):
            raise OperationStoreError("Relocation manifest contains unsupported journal members")
        entries[operation_id] = (Path(row["source"]), files)
    if _optional_lstat(staging) is not None and {p.name for p in staging.iterdir()} - {RELOCATION_MANIFEST, *entries}:
        raise OperationStoreError("Relocation staging directory contains unexpected state")
    result = {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": "relocate",
              "phase": "moving" if resumed else "planned", "modified": False, "plan": plan,
              "plan_sha256": plan["plan_sha256"], "retained": plan["retained"], "completed": [],
              "removed_controls": []}
    if not apply:
        return result
    _require_approval(result, approved_hash)
    if entries:
        for path in (destination.parent, destination, destination / "operations"):
            _ensure_plain_directory(path)
        _ensure_plain_directory(staging)
        if not resumed:
            atomic_write_json(manifest, plan)
        for operation_id, (source, files) in entries.items():
            old = source / "operations" / operation_id
            new = destination / "operations" / operation_id
            _publish_journal(old, staging / operation_id, new, files)
            if not _inspect_operation(root, operation_id).get("trusted_terminal"):
                raise OperationStoreError("Relocated journal could not be reopened")
            result["completed"].append(operation_id)
        manifest.unlink()
        staging.rmdir()
        _fsync_directory(destination)
    for item in plan["remove_controls"]:
        control = Path(item["path"])
        if control.parent != root or control.name not in (LEGACY_CONTROL, MANAGER_CONTROL):
            raise OperationStoreError("Relocation may only remove old in-store control directories")
        if _optional_lstat(control) is None:
            continue
        _remove_store_control(control, item["entries"])
        result["removed_controls"].append(str(control))
    modified = bool(entries) or bool(result["removed_controls"])
    return {**result, "phase": "complete", "modified": modified}


def run_state_command(command: str, root: Path, *, apply: bool = False,
                      approved_hash: str | None = None, directory: Path | None = None) -> dict:
    if command in {"migrate", "route"}:
        raise OperationStoreError(
            f"state {command} is retired: manager state lives in {manager_state_base()}; "
            "use state relocate to move old in-store history there")
    # Validate the explicit physical home before creating any coordination file.
    root = Path(canonical_existing_path_key(root.expanduser().resolve(strict=True)))
    _validate_directory(root, _required_lstat(root))
    # Inventory and relocate must be able to see an interrupted relocation.
    recovering = relocation_recovery(root) if command in {"inventory", "relocate"} else nullcontext()
    with recovering:
        exists = any(_optional_lstat(path) is not None for path in control_directories(root))
        return _run_state_locked(command, root, exists, apply=apply, approved_hash=approved_hash)


def _run_state_locked(command: str, root: Path, exists: bool, *, apply: bool, approved_hash: str | None) -> dict:
    with (mutation_roots((root,)) if exists else nullcontext(),
          _existing_index_lock(root, include_store=command != "relocate")):
        if command == "inventory":
            current = control_directory(root)
            if _optional_lstat(current / RELOCATION_DIRECTORY / RELOCATION_MANIFEST) is not None:
                with relocation_recovery(root):
                    report = _relocate_locked(root, apply=False, approved_hash=None)
                return {"schema_version": "lam.state-inventory.v1", "root": str(root),
                        "phase": "moving", "plan_sha256": report["plan_sha256"],
                        "message": "Resume state relocate using this original hash"}
            return _inspect_locked(root)
        if command == "cleanup":
            return _cleanup_locked(root, apply=apply, approved_hash=approved_hash)
        if command == "relocate":
            with relocation_recovery(root):
                return _relocate_locked(root, apply=apply, approved_hash=approved_hash)
    raise ValueError("Unknown state command")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent-manager state", description="Inspect and maintain manager recovery metadata")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "cleanup", "relocate", "migrate", "route"):
        child = commands.add_parser(command, help=("retired" if command in {"migrate", "route"} else None))
        child.add_argument("--root", type=Path, required=True, help="Exact native or frontend store root")
        child.add_argument("--json", action="store_true")
        if command != "inventory":
            child.add_argument("--apply", action="store_true", help="Apply the reviewed maintenance plan")
            child.add_argument("--plan-sha256", help="Hash returned by this command's preview")
        if command == "route":
            child.add_argument("--directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = run_state_command(args.command, args.root, apply=getattr(args, "apply", False),
                                   approved_hash=getattr(args, "plan_sha256", None))
        code = 3 if result.get("phase") in {"blocked", "moving"} else 0
    except (OSError, ValueError, OperationStoreError) as exc:
        result = {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": args.command,
                  "phase": "blocked", "modified": None, "error": str(exc),
                  "message": "Inspect state inventory and re-plan; an interrupted relocation must resume with its original hash"}
        code = 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
