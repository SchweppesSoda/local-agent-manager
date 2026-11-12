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
    plan_sha256, validate_operation_id,
)
from .state_paths import (
    LAYOUT_MARKER, LEGACY_CONTROL, MANAGER_CONTROL, ROUTED_LAYOUT, control_directories, control_directory,
    operation_entries, read_layout_marker, routed_control, validate_external_directory,
)

_DETAIL_FILES = {"plan.json", "events.jsonl", "state.json", "result.json"}
_JOURNAL_FILES = _DETAIL_FILES | {"receipt.json", "apply.lock"}
_ROLLBACK_DIRECTORIES = {"legacy-index-backups", "desktop-state-backups"}


@contextmanager
def _existing_index_lock(root: Path):
    """Exclude older index writers without changing their lock's payload."""
    path = root / LEGACY_CONTROL / "legacy-index.lock"
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
            if path.name == "legacy-index.lock" and location == root / LEGACY_CONTROL:
                _validate_regular_file(path, _required_lstat(path))
                continue
            extras.append(path.name)
            if path.name in _ROLLBACK_DIRECTORIES:
                _validate_directory(path, _required_lstat(path))
                if location == control and not any(path.iterdir()):
                    empty_directories.append(path.name)
    return {"schema_version": "lam.state-inventory.v1", "root": str(root),
            "control_directory": str(control), "coordination_lock": str(
                root / LEGACY_CONTROL / "operations" / ".mutation.lock"),
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


def _validate_migration_contents(root: Path, plan: dict) -> list[tuple[Path, Path]]:
    source = root / LEGACY_CONTROL / "operations"
    destination = root / MANAGER_CONTROL / "operations"
    expected = {item["operation_id"] for item in plan["operations"]}
    if {path.name for path in (root / LEGACY_CONTROL).iterdir()} - {"operations", "legacy-index.lock"}:
        raise OperationStoreError("Legacy rollback or unrecognized state must be resolved before migration")
    if {path.name for path in source.iterdir()} - expected - {".mutation.lock", LAYOUT_MARKER}:
        raise OperationStoreError("Legacy operations changed during migration")
    if _optional_lstat(root / MANAGER_CONTROL) is not None:
        _validate_directory(root / MANAGER_CONTROL, _required_lstat(root / MANAGER_CONTROL))
        if {path.name for path in (root / MANAGER_CONTROL).iterdir()} - {"operations"}:
            raise OperationStoreError("Migration destination contains unexpected state")
    if _optional_lstat(destination) is not None:
        _validate_directory(destination, _required_lstat(destination))
        if {path.name for path in destination.iterdir()} - expected:
            raise OperationStoreError("Migration destination contains conflicting operations")
    moves = []
    for row in plan["operations"]:
        operation_id = validate_operation_id(row["operation_id"])
        old, new = source / operation_id, destination / operation_id
        old_exists, new_exists = _optional_lstat(old) is not None, _optional_lstat(new) is not None
        if old_exists == new_exists:
            raise OperationStoreError(f"Migration requires exactly one copy of {operation_id}")
        current = old if old_exists else new
        if _journal_snapshot(current) != row["files"]:
            raise OperationStoreError(f"Migration evidence changed for {operation_id}")
        if old_exists:
            moves.append((old, new))
    return moves


def _migrate_locked(root: Path, *, apply: bool = False, approved_hash: str | None = None) -> dict:
    marker = read_layout_marker(root)
    if marker is not None:
        if marker.get("schema_version") == ROUTED_LAYOUT:
            raise OperationStoreError("This store already uses an external journal location")
        if marker["phase"] == "complete":
            control_directory(root)
            return {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": "migrate",
                    "plan_sha256": marker["migration_plan_sha256"], "modified": False, "phase": "complete"}
        plan = marker["plan"]
    else:
        plan = _maintenance_plan(root, "migration")
    blockers = [row["operation_id"] for row in plan["operations"] if not row.get("trusted_terminal")]
    if plan["unmanaged_entries"]:
        blockers.extend(plan["unmanaged_entries"])
    result = {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": "migrate",
              "plan": plan, "plan_sha256": plan["plan_sha256"], "modified": False,
              "phase": "moving" if marker else "planned", "blockers": blockers,
              "retained_legacy_entries": ["operations/.mutation.lock", "operations/" + LAYOUT_MARKER]}
    if _optional_lstat(root / LEGACY_CONTROL / "legacy-index.lock") is not None:
        result["retained_legacy_entries"].append("legacy-index.lock")
    if blockers:
        result["phase"] = "blocked"
        return result
    # Validate both sides before publishing an intent or moving the first journal.
    moves = _validate_migration_contents(root, plan)
    if not apply:
        return result
    _require_approval(plan, approved_hash)
    marker_path = root / LEGACY_CONTROL / "operations" / LAYOUT_MARKER
    if marker is None:
        marker = {"schema_version": "lam.state-layout.v1", "phase": "moving", "plan": plan}
        atomic_write_json(marker_path, marker)
    _ensure_plain_directory(root / MANAGER_CONTROL)
    _ensure_plain_directory(root / MANAGER_CONTROL / "operations")
    for old, new in moves:
        os.rename(old, new)
        _fsync_directory(old.parent)
        _fsync_directory(new.parent)
    if _validate_migration_contents(root, plan):
        raise OperationStoreError("Migration verification found unmoved journals")
    # Retain only layout identity after success. Historical operation IDs and
    # per-file snapshots are needed for interrupted moves, not permanent layout.
    marker = {"schema_version": "lam.state-layout.v1", "phase": "complete", "root": str(root),
              "destination": MANAGER_CONTROL, "migration_plan_sha256": plan["plan_sha256"]}
    marker["plan_sha256"] = plan_sha256(marker)
    atomic_write_json(marker_path, marker)
    control_directory(root)
    result.update(modified=True, phase="complete")
    return result


def _route_locked(root: Path, directory: Path, *, apply: bool, approved_hash: str | None) -> dict:
    validate_external_directory(root, directory)
    source = control_directory(root)
    target = routed_control(root, directory)
    marker = read_layout_marker(root)
    result = {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": "route",
              "phase": "planned", "modified": False, "root": str(root), "destination": str(target)}
    if marker and marker.get("schema_version") == ROUTED_LAYOUT:
        if source != target:
            raise OperationStoreError("The store is already routed to a different directory")
        return {**result, "phase": "complete"}
    base_info = _required_lstat(directory)
    base_identity = [base_info.st_dev, base_info.st_ino]
    plan = {"schema_version": "lam.state-route-plan.v1", "root": str(root), "source": source.name,
            "base_identity": base_identity,
            "base": str(directory), "destination": str(target), "previous_layout_sha256": marker and marker["plan_sha256"]}
    for path in (target.parent, target, target / "operations"):
        info = _optional_lstat(path)
        if info is not None:
            _validate_directory(path, info)
            permitted = {target.name} if path == target.parent else {"operations"} if path == target else set()
            if {entry.name for entry in path.iterdir()} - permitted:
                raise OperationStoreError("The new journal location already contains state")
    result.update(plan=plan, plan_sha256=plan_sha256(plan))
    if not apply:
        return result
    _require_approval(result, approved_hash)
    for path in (target.parent, target, target / "operations"):
        _ensure_plain_directory(path)
    validate_external_directory(root, target)
    current_base = _required_lstat(directory)
    if [current_base.st_dev, current_base.st_ino] != base_identity:
        raise OperationStoreError("External journal base directory changed")
    info = _required_lstat(target)
    routing = {"schema_version": ROUTED_LAYOUT, "root": str(root), "source": source.name,
               "base": str(directory), "destination": str(target), "identity": [info.st_dev, info.st_ino]}
    routing["plan_sha256"] = plan_sha256(routing)
    atomic_write_json(root / LEGACY_CONTROL / "operations" / LAYOUT_MARKER, routing)
    if control_directory(root) != target:
        raise OperationStoreError("External journal routing could not be verified")
    return {**result, "phase": "complete", "modified": True}


def run_state_command(command: str, root: Path, *, apply: bool = False,
                      approved_hash: str | None = None, directory: Path | None = None) -> dict:
    # Validate the explicit physical home before creating any coordination file.
    root = Path(canonical_existing_path_key(root.expanduser().resolve(strict=True)))
    _validate_directory(root, _required_lstat(root))
    exists = any(_optional_lstat(root / name) is not None for name in (LEGACY_CONTROL, MANAGER_CONTROL))
    if not exists and command == "migrate":
        raise OperationStoreError("No legacy state exists to migrate")
    with mutation_roots((root,)) if exists or (command == "route" and apply) else nullcontext(), _existing_index_lock(root):
        if command == "inventory":
            marker = read_layout_marker(root)
            if marker is not None and marker.get("phase") == "moving":
                return {"schema_version": "lam.state-inventory.v1", "root": str(root),
                        "phase": "moving", "plan_sha256": marker["plan"]["plan_sha256"],
                        "message": "Resume state migrate using this original hash"}
            return _inspect_locked(root)
        if command == "cleanup":
            return _cleanup_locked(root, apply=apply, approved_hash=approved_hash)
        if command == "migrate":
            return _migrate_locked(root, apply=apply, approved_hash=approved_hash)
        if command == "route" and directory is not None:
            return _route_locked(root, directory.expanduser().absolute(), apply=apply, approved_hash=approved_hash)
    raise ValueError("Unknown state command")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent-manager state", description="Inspect and maintain manager recovery metadata")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "cleanup", "migrate", "route"):
        child = commands.add_parser(command)
        child.add_argument("--root", type=Path, required=True, help="Exact native or frontend store root")
        child.add_argument("--json", action="store_true")
        if command != "inventory":
            child.add_argument("--apply", action="store_true", help="Apply the reviewed maintenance plan")
            child.add_argument("--plan-sha256", help="Hash returned by this command's preview")
        if command == "route":
            child.add_argument("--directory", type=Path, required=True, help="Existing external state directory")
    args = parser.parse_args(argv)
    try:
        result = run_state_command(args.command, args.root, apply=getattr(args, "apply", False),
                                   approved_hash=getattr(args, "plan_sha256", None), directory=getattr(args, "directory", None))
        code = 3 if result.get("phase") in {"blocked", "moving"} else 0
    except (OSError, ValueError, OperationStoreError) as exc:
        result = {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": args.command,
                  "phase": "blocked", "modified": None, "error": str(exc),
                  "message": "Inspect state inventory and re-plan; an interrupted migration must resume with its original hash"}
        code = 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
