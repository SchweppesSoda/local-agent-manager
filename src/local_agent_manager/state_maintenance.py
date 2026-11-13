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
    _required_lstat, _validate_directory, _validate_regular_file, plan_sha256,
)
from .state_paths import control_directories, control_directory, operation_entries


_DETAIL_FILES = {"plan.json", "events.jsonl", "state.json", "result.json"}
_JOURNAL_FILES = _DETAIL_FILES | {"receipt.json", "apply.lock"}
_ROLLBACK_DIRECTORIES = {"legacy-index-backups", "desktop-state-backups"}


@contextmanager
def _existing_index_lock(root: Path):
    with _held_index_lock(control_directory(root) / "legacy-index.lock"):
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
            # A current child may retain only a checkpoint and events.
            # Compaction requires the stronger result/verification contract.
            row.update(trusted_terminal=True, reason="terminal_result_missing",
                       message="Retain checkpoint details until operation verify publishes a validated result")
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


def run_state_command(command: str, root: Path, *, apply: bool = False,
                      approved_hash: str | None = None) -> dict:
    if command not in {"inventory", "cleanup"}:
        raise ValueError("Unknown state command")
    root = Path(canonical_existing_path_key(root.expanduser().resolve(strict=True)))
    _validate_directory(root, _required_lstat(root))
    exists = _optional_lstat(control_directory(root)) is not None
    with (mutation_roots((root,)) if exists else nullcontext(), _existing_index_lock(root)):
        if command == "inventory":
            return _inspect_locked(root)
        return _cleanup_locked(root, apply=apply, approved_hash=approved_hash)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-agent-manager state", description="Inspect and maintain manager recovery metadata")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "cleanup"):
        child = commands.add_parser(command)
        child.add_argument("--root", type=Path, required=True, help="Exact native or frontend store root")
        child.add_argument("--json", action="store_true")
        if command != "inventory":
            child.add_argument("--apply", action="store_true", help="Apply the reviewed maintenance plan")
            child.add_argument("--plan-sha256", help="Hash returned by this command's preview")
    args = parser.parse_args(argv)
    try:
        result = run_state_command(args.command, args.root, apply=getattr(args, "apply", False),
                                   approved_hash=getattr(args, "plan_sha256", None))
        code = 3 if result.get("phase") in {"blocked", "moving"} else 0
    except (OSError, ValueError, OperationStoreError) as exc:
        result = {"schema_version": "lam.state-result.v1", "command": "state", "subcommand": args.command,
                  "phase": "blocked", "modified": None, "error": str(exc),
                  "message": "Inspect state inventory and re-plan"}
        code = 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
