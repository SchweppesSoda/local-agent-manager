"""Read-only preparation for a Cindy Codex home split.

These functions do not grant mutation, login or lifecycle authority. Plans remain
drafts until an executor freezes cold stores under the normal admission gate.
Recovery returns field patches, never an old whole-store replacement.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import stat
import sqlite3
from typing import Mapping


class IsolationPlanError(ValueError):
    pass


@contextmanager
def readonly_sqlite(path: Path):
    """Read committed WAL state, refusing immutable mode on a pending WAL."""
    path = path.absolute()
    if not path.is_file():
        raise IsolationPlanError("sqlite_source_missing")
    before = path.stat()
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    immutable = not shm.exists()
    if Path(str(path) + "-journal").exists() or (immutable and wal.exists() and wal.stat().st_size):
        raise IsolationPlanError("sqlite_sidecars_unqualified")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma query_only=on")
    connection.execute("begin")
    try:
        yield connection
    finally:
        connection.close()
        after = path.stat()
        if immutable and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise IsolationPlanError("sqlite_source_changed")


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def directory_identity(path: Path) -> dict:
    path = path.absolute()
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise IsolationPlanError("reparse_directory")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise IsolationPlanError("not_a_directory")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def legacy_destination(registry: Mapping, *, profile_root: Path,
                       home: Path, official_home: Path) -> dict:
    """Prepare the existing product's legacy default route, without a login.

    legacyCodexDefault is the product's persisted compatibility discriminator.
    Its proposed change is explicit; callers must freeze and write it offline.
    """
    pool = registry.get("pools", {}).get("openai", {})
    if sum(a.get("id") == "default" for a in pool.get("accounts", [])) != 1:
        raise IsolationPlanError("existing_default_account_required")
    if home.absolute() != (profile_root / "codex-home").absolute():
        raise IsolationPlanError("legacy_product_home_mismatch")
    target = directory_identity(home)
    source = directory_identity(official_home)
    if (target["device"], target["inode"]) == (source["device"], source["inode"]):
        raise IsolationPlanError("shared_source_directory")
    before = pool.get("legacyCodexDefault")
    if before is not None and not isinstance(before, bool):
        raise IsolationPlanError("legacy_default_flag_unqualified")
    return {"account_id": "default", "home": target, "mode": "existing_legacy_default",
            "route_change": {"before_present": "legacyCodexDefault" in pool,
                             "before": before, "after": True},
            "authentication_verified": False, "new_login_required": False}


def closure_batches(roots: Mapping[str, list[str]], edges: list[tuple[str, str]],
                    *, protected_ids: set[str], max_roots: int = 25) -> list[dict]:
    """Coalesce overlapping descendants before packing bounded root batches.

    A component larger than max_roots remains intact; max_roots is a packing
    target, never permission to split a shared child between operations.
    """
    if not roots or max_roots < 1 or any(not ids for ids in roots.values()):
        raise IsolationPlanError("empty_scope")
    children: dict[str, set[str]] = {}
    for parent, child in edges:
        children.setdefault(parent, set()).add(child)
    components = []
    for root, task_ids in sorted(roots.items()):
        descendants, pending = set(), [root]
        while pending:
            current = pending.pop()
            if current not in descendants:
                descendants.add(current)
                pending.extend(children.get(current, ()))
        if descendants & protected_ids:
            raise IsolationPlanError("protected_native_in_closure")
        component = {"root_ids": {root}, "task_ids": set(task_ids), "native_ids": descendants}
        rest = []
        for old in components:
            if old["native_ids"] & component["native_ids"]:
                for key in component:
                    component[key].update(old[key])
            else:
                rest.append(old)
        # A newly merged component can connect an earlier disjoint component.
        while any(c["native_ids"] & component["native_ids"] for c in rest):
            remaining = []
            for old in rest:
                if old["native_ids"] & component["native_ids"]:
                    for key in component:
                        component[key].update(old[key])
                else:
                    remaining.append(old)
            rest = remaining
        components = rest + [component]
    batches = []
    for component in sorted(components, key=lambda c: min(c["root_ids"])):
        if not batches or len(batches[-1]["root_ids"]) + len(component["root_ids"]) > max_roots:
            batches.append({key: set() for key in component})
        for key in component:
            batches[-1][key].update(component[key])
    return [{key: sorted(values) for key, values in batch.items()} for batch in batches]


def binding_changes(registry: Mapping, *, task_ids: list[str], native_ids: list[str],
                    account_id: str, protected_keys: set[str], legacy_default: bool = False) -> list[dict]:
    """Freeze exact keys, including absent keys, without replacing the registry."""
    if not task_ids or not native_ids or not account_id or (account_id == "default" and not legacy_default):
        raise IsolationPlanError("empty_binding_scope_or_destination")
    keys = {"openai:task:" + i for i in task_ids} | {"openai:native:" + i for i in native_ids}
    if keys & protected_keys:
        raise IsolationPlanError("protected_binding")
    bindings = registry.get("bindings", {})
    changes = []
    for key in sorted(keys):
        # The product also reads old unprefixed binding keys. Never leave a
        # contradictory fallback value for one of the selected identities.
        legacy = key.removeprefix("openai:")
        selected = [key, *([legacy] if legacy in bindings else [])]
        if set(selected) & protected_keys:
            raise IsolationPlanError("protected_binding")
        for name in selected:
            before = bindings.get(name)
            if before is not None and (not isinstance(before, dict) or before.get("providerId") != "openai"):
                raise IsolationPlanError("binding_provider_conflict")
            changes.append({"key": name, "before_present": name in bindings,
                            "before": deepcopy(before), "after": {"providerId": "openai", "accountId": account_id}})
    return changes


def registry_patch(current: Mapping, changes: list[dict], *, rollback: bool = False) -> dict:
    """Compute a compare/exchange patch while retaining unrelated/new bindings.

    The caller must still hold the root mutation lock and atomic-file lease.
    This is not a live writer. Any conflicting key blocks the entire patch.
    """
    result = deepcopy(current)
    bindings = result.setdefault("bindings", {})
    seen = set()
    for change in changes:
        key = change["key"]
        if key in seen:
            raise IsolationPlanError("duplicate_binding_change")
        seen.add(key)
        expected_present = True if rollback else change["before_present"]
        expected = change["after"] if rollback else change["before"]
        if (key in bindings) != expected_present or (expected_present and bindings[key] != expected):
            raise IsolationPlanError("binding_changed_since_freeze")
        if rollback and not change["before_present"]:
            bindings.pop(key)
        else:
            bindings[key] = deepcopy(change["before"] if rollback else change["after"])
    return result


def recovery_action(*, phase: str, copied_verified: bool,
                    switched_verified: bool, acceptance_verified: bool,
                    target_at_switch: Mapping, target_now: Mapping) -> dict:
    """Never replay unknown writes or overwrite target increments on rollback."""
    if phase not in {"prepared", "copying", "copied", "switching", "switched", "accepted"}:
        raise IsolationPlanError("unknown_phase")
    if phase in {"copying", "switching"}:
        return {"action": "status_verify_no_replay", "source_cleanup_allowed": False}
    if phase == "prepared":
        return {"action": "no_live_mutation", "source_cleanup_allowed": False}
    if not copied_verified:
        return {"action": "verify_copy_keep_source", "source_cleanup_allowed": False}
    if phase == "copied":
        return {"action": "keep_source_binding", "source_cleanup_allowed": False}
    if not switched_verified or not target_at_switch or set(target_now) != set(target_at_switch):
        return {"action": "status_verify_no_replay", "source_cleanup_allowed": False}
    delta = sorted(k for k in target_at_switch if target_at_switch[k] != target_now[k])
    if delta:
        return {"action": "preserve_target_increment_reconcile", "changed_native_ids": delta,
                "source_cleanup_allowed": False}
    if phase == "accepted" and acceptance_verified:
        return {"action": "prepare_fresh_exact_source_cleanup_plan", "source_cleanup_allowed": True}
    return {"action": "exact_binding_rollback_candidate", "source_cleanup_allowed": False}


def recovery_report(batches: list[dict]) -> dict:
    """Keep completed siblings and cumulative mutation evidence on interruption."""
    ids = [batch["batch_id"] for batch in batches]
    if len(ids) != len(set(ids)):
        raise IsolationPlanError("duplicate_batch")
    decisions = []
    mutations = []
    completed = []
    for batch in batches:
        decision = recovery_action(**batch["state"])
        decisions.append({"batch_id": batch["batch_id"], **decision})
        mutations.extend({"batch_id": batch["batch_id"], **deepcopy(m)} for m in batch.get("mutations", []))
        if decision["action"] == "prepare_fresh_exact_source_cleanup_plan":
            completed.append(batch["batch_id"])
    return {"batches": decisions, "cumulative_mutations": mutations,
            "completed_batches": completed,
            "whole_store_restore_permitted": False}
