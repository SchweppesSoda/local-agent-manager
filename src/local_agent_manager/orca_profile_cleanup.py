"""Version-3 Orca profile domain updates that preserve automation and settings."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path

from . import frozen_sqlite
from .orca_journal_cleanup import decode, encode, fail, integer, rows, text, _sql
from .orca_frontend_json import ui_state

DDL = """
CREATE TABLE profile_state_meta(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL);
CREATE TABLE profile_state_documents(domain TEXT PRIMARY KEY NOT NULL,payload TEXT NOT NULL,domain_version INTEGER NOT NULL,revision INTEGER NOT NULL,updated_at INTEGER NOT NULL,content_hash TEXT NOT NULL);
CREATE TABLE profile_state_automation_runs_meta(domain TEXT PRIMARY KEY NOT NULL,presence TEXT NOT NULL,domain_version INTEGER NOT NULL,revision INTEGER NOT NULL,updated_at INTEGER NOT NULL,content_hash TEXT NOT NULL);
CREATE TABLE profile_state_automation_runs(run_id TEXT PRIMARY KEY NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,content_hash TEXT NOT NULL,revision INTEGER NOT NULL,updated_at INTEGER NOT NULL);
"""
PARTS_DDL = """
CREATE TABLE profile_state_document_parts_meta(domain TEXT PRIMARY KEY NOT NULL,domain_version INTEGER NOT NULL,revision INTEGER NOT NULL,updated_at INTEGER NOT NULL,content_hash TEXT NOT NULL,legacy_revision INTEGER);
CREATE TABLE profile_state_document_parts(domain TEXT NOT NULL,part_key TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(domain,part_key));
"""
SPLIT_DEPTH = {"workspaceSession": 2, "workspaceSessionsByHostId": 3, "remoteLayoutSnapshots": 3}
WORKTREE_PARTS = {"worktreeMeta": 1, "worktreeMetaByIdentity": 1, "worktreeIdentityAliases": 1}
DOMAINS = {*SPLIT_DEPTH, "mobileClientTabSelectionsByDeviceId", "ui"}


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def meta(db):
    value = dict(rows(db, "SELECT key,value FROM profile_state_meta"))
    if set(value) - {"profile_id", "revision", "legacy_json_acceptance"} or not text(value.get("profile_id")):
        fail("profile_meta_unverified")
    revision = value.get("revision", "0")
    if not isinstance(revision, str) or not re.fullmatch(r"0|[1-9][0-9]*", revision) or not integer(int(revision)):
        fail("profile_revision_unverified")
    return value, int(revision)


def parts(value, depth, keys=()):
    if depth and isinstance(value, dict) and value:
        for key, child in value.items():
            yield from parts(child, depth - 1, (*keys, key))
    else:
        payload = encode(value)
        yield encode(keys), payload, sha(payload)


def synchronized_parts(db, revision, *, all_domains=False):
    """A closed writer refreshes the legacy row. Qualify both readable copies."""
    depths = SPLIT_DEPTH
    if all_domains:
        depths = {**depths, **WORKTREE_PARTS}
        domains = rows(db, "SELECT domain FROM profile_state_document_parts_meta UNION SELECT domain FROM profile_state_document_parts")
        if any(domain not in depths for (domain,) in domains):
            fail("profile_split_domain_unverified")
    for domain, depth in depths.items():
        meta_row = db.execute("SELECT domain_version,revision,updated_at,content_hash,legacy_revision "
                              "FROM profile_state_document_parts_meta WHERE domain=?", (domain,)).fetchone()
        stored = rows(db, "SELECT part_key,payload,content_hash,ordinal FROM profile_state_document_parts "
                          "WHERE domain=? ORDER BY ordinal", (domain,))
        if not meta_row and not stored:
            continue
        doc = db.execute("SELECT payload,revision,content_hash FROM profile_state_documents WHERE domain=?", (domain,)).fetchone()
        if (not meta_row or not doc or meta_row[0] != 1 or not integer(meta_row[1], 1) or meta_row[1] > revision
                or not integer(meta_row[2]) or tuple(meta_row[3:]) != (doc[2], doc[1])):
            fail("profile_split_restore_versions_diverged")
        expected = list(parts(decode(doc[0]), depth))
        if ([tuple(row[:3]) for row in stored] != expected or len({row[3] for row in stored}) != len(stored)
                or sha(encode(decode(doc[0]))) != doc[2]):
            fail("profile_split_restore_parts_unverified")


def write_parts(db, domain, value, revision, timestamp, digest):
    if domain not in SPLIT_DEPTH or not db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='profile_state_document_parts_meta'").fetchone():
        return
    if not db.execute("SELECT 1 FROM profile_state_document_parts_meta WHERE domain=?", (domain,)).fetchone():
        return
    existing = {k: (ordinal, hashed) for k, ordinal, hashed in rows(db,
        "SELECT part_key,ordinal,content_hash FROM profile_state_document_parts WHERE domain=?", (domain,))}
    updated = list(parts(value, SPLIT_DEPTH[domain]))
    # Removing leaves preserves the ordering (and row identities) of survivors.
    old_order = [existing[k][0] for k, _, _ in updated if k in existing]
    reuse = len(old_order) == len(updated) and old_order == sorted(old_order)
    for i, (key, payload, hashed) in enumerate(updated):
        ordinal = old_order[i] if reuse else i
        if existing.pop(key, None) != (ordinal, hashed):
            db.execute("INSERT INTO profile_state_document_parts VALUES(?,?,?,?,?) ON CONFLICT(domain,part_key) "
                       "DO UPDATE SET ordinal=excluded.ordinal,payload=excluded.payload,content_hash=excluded.content_hash",
                       (domain, key, ordinal, payload, hashed))
    db.executemany("DELETE FROM profile_state_document_parts WHERE domain=? AND part_key=?", ((domain, k) for k in existing))
    db.execute("UPDATE profile_state_document_parts_meta SET revision=?,updated_at=?,content_hash=?,legacy_revision=? WHERE domain=?",
               (revision, timestamp, digest, revision, domain))


def qualify(db, *, all_domains=False):
    if db.execute("PRAGMA user_version").fetchone()[0] != 3 or db.execute("PRAGMA application_id").fetchone()[0] != 0:
        fail("profile_writer_schema_unverified")
    actual = set()
    for kind, name, sql in db.execute("SELECT type,name,sql FROM sqlite_master"):
        if kind == "index" and sql is None and name.startswith("sqlite_autoindex_"):
            continue
        if kind != "table" or not isinstance(sql, str):
            fail("profile_writer_schema_unverified")
        actual.add(_sql(sql))
    base = {_sql(statement) for statement in DDL.split(";") if statement.strip()}
    split = base | {_sql(statement) for statement in PARTS_DDL.split(";") if statement.strip()}
    if actual not in (base, split):
        fail("profile_writer_schema_unverified")
    _, revision = meta(db)
    for domain, payload, version, row_revision, updated, digest in rows(db, "SELECT * FROM profile_state_documents"):
        if (not text(domain) or not isinstance(payload, str) or version != 1
                or not integer(row_revision, 1) or row_revision > revision or not integer(updated) or sha(payload) != digest):
            fail("profile_document_unverified")
        decode(payload)
    if actual == split:
        synchronized_parts(db, revision, all_domains=all_domains)
    markers = rows(db, "SELECT * FROM profile_state_automation_runs_meta")
    if len(markers) != 1 or markers[0][0] != "automationRuns":
        fail("profile_automation_projection_unverified")
    _, presence, version, row_revision, updated, digest = markers[0]
    if (presence not in {"document", "array", "null", "absent"} or version != 1
            or not integer(row_revision) or row_revision > revision or not integer(updated)):
        fail("profile_automation_projection_unverified")
    runs = rows(db, "SELECT * FROM profile_state_automation_runs ORDER BY ordinal")
    if presence == "document":
        if (row_revision, updated, digest) != (0, 0, "") or runs:
            fail("profile_automation_projection_unverified")
    elif not integer(row_revision, 1) or (digest != "" if presence == "absent" else not re.fullmatch(r"[0-9a-f]{64}", str(digest))):
        fail("profile_automation_projection_unverified")
    if presence != "array" and runs:
        fail("profile_automation_projection_unverified")
    marker_revision, marker_updated, marker_hash = row_revision, updated, digest
    for index, (run_id, ordinal, payload, digest, row_revision, updated) in enumerate(runs):
        if (not text(run_id) or not integer(ordinal) or not isinstance(payload, str) or sha(payload) != digest
                or ordinal != index or not integer(row_revision, 1) or row_revision > marker_revision
                or not integer(updated) or row_revision == marker_revision and updated != marker_updated):
            fail("profile_automation_projection_unverified")
        value = decode(payload)
        if not isinstance(value, dict) or value.get("id") != run_id:
            fail("profile_automation_projection_unverified")
    if presence == "array" and sha("[" + ",".join(row[2] for row in runs) + "]") != marker_hash:
        fail("profile_automation_projection_unverified")
    if presence == "null" and marker_hash != sha("null"):
        fail("profile_automation_projection_unverified")


def projection(db):
    return {domain: decode(payload) for domain, payload in rows(db, "SELECT domain,payload FROM profile_state_documents") if domain in DOMAINS}


def transform(db, selected, known_tabs, native_ids, *, timestamp, json_hashes, remote=None):
    before = projection(db)
    after = ui_state(before, selected, known_tabs, native_ids, remote=remote)
    changes = {domain: value for domain, value in after.items() if before[domain] != value}
    metadata, revision = meta(db)
    acceptance = None
    if "legacy_json_acceptance" in metadata:
        value = decode(metadata["legacy_json_acceptance"])
        if not isinstance(value, dict) or set(value) - {"jsonHash", "acceptedRevision", "pending"}:
            fail("legacy_json_acceptance_unverified")
        if "pending" in value and (not isinstance(value["pending"], dict)
                or set(value["pending"]) != {"jsonHash", "acceptedRevision"}):
            fail("legacy_json_acceptance_unverified")
        acceptance = deepcopy(value)
        for item in (acceptance, acceptance.get("pending")):
            if item is None:
                continue
            if (not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("jsonHash", "")))
                    or not integer(item.get("acceptedRevision"), 1) or item["acceptedRevision"] > revision):
                fail("legacy_json_acceptance_unverified")
            old_hash = item["jsonHash"]
            if old_hash not in json_hashes:
                fail("legacy_json_restore_source_unverified")
            if json_hashes[old_hash] != old_hash:
                item["jsonHash"] = json_hashes[old_hash]
        if "pending" in value and value["pending"]["acceptedRevision"] < value["acceptedRevision"]:
            fail("legacy_json_acceptance_unverified")
        if acceptance == value:
            acceptance = None
        else:
            # Both accepted byte versions now describe this new transaction;
            # keep pending >= accepted even when only one JSON copy changed.
            acceptance["acceptedRevision"] = revision + 1
            if "pending" in acceptance:
                acceptance["pending"]["acceptedRevision"] = revision + 1
    if not changes and acceptance is None:
        return
    if not integer(revision + 1, 1) or not integer(timestamp):
        fail("profile_revision_unverified")
    for domain, value in changes.items():
        payload = encode(value)
        db.execute("UPDATE profile_state_documents SET payload=?,revision=?,updated_at=?,content_hash=? WHERE domain=?",
            (payload, revision + 1, timestamp, sha(payload), domain))
        write_parts(db, domain, value, revision + 1, timestamp, sha(payload))
    if acceptance is not None:
        db.execute("UPDATE profile_state_meta SET value=? WHERE key='legacy_json_acceptance'", (encode(acceptance),))
    db.execute("INSERT INTO profile_state_meta(key,value) VALUES ('revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(revision + 1),))


def freeze(root, relative, selected, known_tabs, native_ids, *, timestamp, json_hashes, remote=None):
    expected = relative.split("/")[1] if relative.startswith("profiles/") else None
    def qualified(db):
        qualify(db)
        if expected is not None and meta(db)[0]["profile_id"] != expected:
            fail("profile_identity_mismatch")
    evidence = frozen_sqlite.freeze(root, relative, qualifier=qualified,
        transform=lambda db: transform(db, selected, known_tabs, native_ids, timestamp=timestamp, json_hashes=json_hashes, remote=remote))
    return {**evidence, "profile_id": expected}


def _qualifier(evidence):
    expected = evidence.get("profile_id")
    parts = Path(evidence["relative"]).parts
    if parts[0] == "profiles" and (len(parts) != 3 or expected != parts[1]):
        fail("profile_identity_mismatch")
    def qualified(db):
        qualify(db)
        if expected is not None and meta(db)[0]["profile_id"] != expected:
            fail("profile_identity_mismatch")
    return qualified


def apply(evidence, selected, known_tabs, native_ids, *, timestamp, json_hashes, phase_callback, remote=None):
    frozen_sqlite.apply(evidence, qualifier=_qualifier(evidence), phase_callback=phase_callback,
        transform=lambda db: transform(db, selected, known_tabs, native_ids, timestamp=timestamp, json_hashes=json_hashes, remote=remote))


def remaining(evidence, selected, known_tabs, native_ids, *, terminal_verified=False, remote=None):
    def count(db):
        value = projection(db)
        return int(ui_state(value, selected, known_tabs, native_ids, remote=remote) != value)
    return frozen_sqlite.remaining(evidence, qualifier=_qualifier(evidence), count=count, terminal_verified=terminal_verified)
