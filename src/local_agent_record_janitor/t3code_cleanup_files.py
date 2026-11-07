"""Frozen T3 database and terminal-history closure; unknown attachments block."""
import base64
import copy
import hashlib
from pathlib import Path
import re

from . import frozen_files, t3code_database as database
from .t3code_store import fail, identifier, read_database

SCHEMA = "larj.t3code-files.v1"
TERMINALS = "userdata/logs/terminals"


def _encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _names(root, relative):
    path = frozen_files.checked_path(root, relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": relative, "absent": True}
    frozen_files._plain(info, directory=True)
    names = []
    for entry in path.iterdir():
        if len(names) >= 20000:
            fail("file_budget_exceeded")
        names.append(entry.name)
    return {"path": relative, "identity": frozen_files._identity(info), "entries": sorted(names)}


def _terminal_owner(name, ids):
    owners = []
    for identifier_ in ids:
        prefixes = ("terminal_" + _encoded(identifier_.encode()), "terminal_sha256." + _encoded(hashlib.sha256(identifier_.encode()).digest()))
        legacy = re.sub(r"[^a-zA-Z0-9._-]", "_", identifier_) + ".log"
        if name == legacy or any(name == p + ".log" or name.startswith(p + "_") and name.endswith(".log") for p in prefixes):
            owners.append(identifier_)
    if len(owners) > 1:
        fail("terminal_identity_ambiguous")
    return owners[0] if owners else None


def _require_known_database_family(root):
    directory = frozen_files.checked_path(root, "userdata")
    for path in directory.iterdir():
        name = path.name.casefold()
        if (name.startswith("state") or name.endswith((".sqlite", ".sqlite3", ".db", ".bak"))) and name not in {
                "statev2.sqlite", "statev2.sqlite-wal", "statev2.sqlite-shm", "statev2.sqlite-journal",
                "server-owner.sqlite"}:
            fail("recovery_database_unqualified")


def freeze(root, ids):
    root = Path(root)
    ids = sorted({identifier(i) for i in ids})
    _require_known_database_family(root)
    db = database.freeze(root, ids)
    all_ids = tuple(read_database(root)["threads"])
    directories = [_names(root, relative) for relative in
        (TERMINALS, "userdata/attachments", "userdata/browser-artifacts", "userdata/backups", "userdata/snapshots")]
    # Shared and uploaded attachments need their own proven reference graph.
    # Do not silently remove just a DB pointer and claim full local deletion.
    if any(d.get("entries") for d in directories[1:]):
        fail("attachment_closure_unqualified")
    changes, preserved = [], []
    for name in directories[0].get("entries", ()):
        relative = TERMINALS + "/" + name
        proof, _ = frozen_files.read_file(root, relative)
        owner = _terminal_owner(name, all_ids)
        if owner is None:
            fail("terminal_owner_unqualified")
        if owner in ids:
            changes.append({"kind": "remove", "path": relative, "files": [proof], "directories": []})
        else:
            preserved.append(proof)
    if directories != [_names(root, d["path"]) for d in directories]:
        fail("file_closure_changed")
    return {"schema_version": SCHEMA, "root": str(root), "thread_ids": ids, "database": db,
        "directories": directories, "changes": changes, "preserved": preserved}


def validate(evidence):
    if evidence.get("schema_version") != SCHEMA or evidence.get("thread_ids") != evidence.get("database", {}).get("thread_ids"):
        fail("file_evidence_invalid")
    if evidence.get("root") != evidence["database"].get("root"):
        fail("file_evidence_invalid")
    database.validate(evidence["database"])
    return Path(evidence["root"])


def remaining(evidence):
    root = validate(evidence)
    _require_known_database_family(root)
    missing, count = set(), database.remaining(evidence["database"])
    for change in evidence["changes"]:
        if frozen_files.satisfied(root, change):
            missing.add(change["path"])
        else:
            if frozen_files.freeze_remove(root, change["path"]) != change:
                fail("file_state_unknown")
            count += 1
    expected = copy.deepcopy(evidence["directories"])
    for directory in expected:
        if "entries" in directory:
            directory["entries"] = [name for name in directory["entries"] if directory["path"] + "/" + name not in missing]
    if [_names(root, d["path"]) for d in expected] != expected:
        fail("file_closure_changed")
    for proof in evidence["preserved"]:
        current, _ = frozen_files.read_file(root, proof["path"])
        if current != proof:
            fail("unselected_file_changed")
    return count


def apply(evidence, *, phase_callback):
    root = validate(evidence)
    remaining(evidence)
    for change in evidence["changes"]:
        phase_callback("mutation_started")
        frozen_files.apply_remove(root, change)
    database.apply(evidence["database"], phase_callback=phase_callback)
    if remaining(evidence):
        fail("files_after_unverified")
