"""Bounded T3 Code v1/v2 metadata; native provider IDs never imply a home.

Pinned to 0.1.8-nightly.20261107.4301. SQL projections deliberately exclude
titles, event/message payloads, runtime credentials and authentication tables.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from importlib.resources import files
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot, SourceFailure
from .record_identity import EngineCapability, ProjectKey, RecordClassification, RecordKey, StoreKey, canonical_path, normalize_engine
from .path_identity import is_local_absolute_locator
from .sqlite_utils import connect_readonly
from . import frozen_files

CONTRACT = json.loads(files(__package__).joinpath("t3code_schema.json").read_text(encoding="utf-8"))
RELATIVE = "userdata/statev2.sqlite"
MAX_ROWS = 20000
MAX_DATABASE_BYTES = 512 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/%-]{0,255}\Z")
V2 = "orchestration_v2_projection_"


class T3CodeError(ValueError):
    def __init__(self, code, *, unknown=False):
        super().__init__(code)
        self.kind, self.unknown = code, unknown


def fail(code):
    raise T3CodeError("t3code_" + code)


def local_root(value):
    value = Path(value).expanduser()
    if not is_local_absolute_locator(str(value)) or any(ord(c) < 32 for c in str(value)):
        fail("local_root_unproven")
    return value


def default_root():
    raw = os.environ.get("T3CODE_HOME")
    if raw is None:
        return local_root(Path.home() / ".t3")
    # Upstream --base-dir/T3CODE_HOME resolves relative to the launch cwd.
    if not raw.strip() or "://" in raw or raw.startswith(("//", "\\\\")):
        fail("environment_root_unproven")
    path = Path(raw).expanduser()
    if not path.is_absolute() and not re.match(r"^[A-Za-z]:", raw):
        if raw.startswith(("/", "\\")):
            fail("environment_root_unproven")
        path = path.absolute()
    return local_root(path)


def identifier(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not ID.fullmatch(value):
        fail("metadata_identity_invalid")
    return value


def engine(provider):
    return {"codex": "codex", "claudeAgent": "claude"}.get(provider, normalize_engine("unsupported:" + str(provider)))


def database_path(root):
    path = frozen_files.checked_path(local_root(root), RELATIVE)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(path) + suffix)
        try:
            info = candidate.lstat()
            frozen_files._plain(info)
            if info.st_size > MAX_DATABASE_BYTES:
                fail("database_budget_exceeded")
        except FileNotFoundError:
            if not suffix:
                raise
    return path


def qualify(db):
    objects = [list(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    if objects != CONTRACT["objects"]:
        fail("schema_unverified")


def rows(db, query, args=()):
    result = db.execute(query, args).fetchmany(MAX_ROWS + 1)
    if len(result) > MAX_ROWS:
        fail("inventory_budget_exceeded")
    return [dict(row) for row in result]


def read_database(root):
    path = database_path(root)
    identity = frozen_files._identity(path.stat())
    with closing(connect_readonly(path)) as db:
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        db.execute("BEGIN")
        qualify(db)
        projects = rows(db, "SELECT project_id,workspace_root,created_at,updated_at,deleted_at FROM projection_projects ORDER BY project_id")
        legacy = rows(db, "SELECT thread_id,project_id,worktree_path,created_at,updated_at,archived_at,deleted_at FROM projection_threads ORDER BY thread_id")
        current = rows(db, f"""SELECT thread_id,project_id,default_provider,provider_instance_id,created_at,updated_at,archived_at,deleted_at,
            json_extract(payload_json,'$.worktreePath') AS worktree_path,
            json_extract(payload_json,'$.lineage.parentThreadId') AS parent_thread_id,
            json_extract(payload_json,'$.lineage.relationshipToParent') AS parent_relationship
            FROM {V2}threads ORDER BY thread_id""")
        providers = rows(db, f"""SELECT provider_thread_id,thread_id,provider,driver,provider_instance_id,status,
            json_extract(payload_json,'$.nativeThreadRef.driver') AS native_driver,
            json_extract(payload_json,'$.nativeThreadRef.nativeId') AS native_id,
            json_extract(payload_json,'$.nativeThreadRef.strength') AS native_strength
            FROM {V2}provider_threads ORDER BY provider_thread_id""")
        old_providers = rows(db, "SELECT thread_id,provider_name,provider_thread_id,provider_session_id,status FROM projection_thread_sessions ORDER BY thread_id")
        counts = rows(db, f"SELECT thread_id,count(*) AS count FROM {V2}messages GROUP BY thread_id")
        old_counts = rows(db, "SELECT thread_id,count(*) AS count FROM projection_thread_messages GROUP BY thread_id")
        schedules = rows(db, "SELECT task_id,thread_id,enabled FROM scheduled_tasks WHERE thread_id IS NOT NULL")
        transfers = rows(db, f"SELECT context_transfer_id,source_thread_id,target_thread_id FROM {V2}context_transfers")
        children = rows(db, f"SELECT subagent_id,thread_id,child_thread_id FROM {V2}subagents WHERE child_thread_id IS NOT NULL")
        peer_count = db.execute("SELECT count(*) FROM peer_environment_links").fetchone()[0]
    if frozen_files._identity(database_path(root).stat()) != identity:
        fail("database_replaced")
    by_project = {identifier(row["project_id"]): row for row in projects}
    by_thread = defaultdict(list)
    for version, items in (("v1", legacy), ("v2", current)):
        for row in items:
            identifier(row["thread_id"])
            identifier(row.get("parent_thread_id"), nullable=True)
            if row["project_id"] not in by_project:
                fail("project_reference_unresolved")
            row["version"] = version
            by_thread[row["thread_id"]].append(row)
    for row in providers:
        identifier(row["provider_thread_id"])
        identifier(row["thread_id"], nullable=True)
        identifier(row["native_id"], nullable=True)
        if row["native_id"] is not None and row["native_driver"] != (row["driver"] or row["provider"]):
            fail("provider_reference_conflict")
    # Orphaned projections remain visible as a coverage failure, never empty success.
    for row in old_providers:
        for field in ("provider_thread_id", "provider_session_id"):
            identifier(row[field], nullable=True)
    if any(row["thread_id"] not in by_thread for row in counts + old_counts + old_providers + providers):
        fail("orphan_projection_present")
    return {"projects": by_project, "threads": dict(by_thread), "providers": providers,
        "legacy_providers": old_providers, "message_counts": counts, "legacy_message_counts": old_counts,
        "schedules": schedules, "transfers": transfers, "children": children, "peer_count": peer_count}


def capability(value):
    return EngineCapability("t3code", value, verify=False,
        reason="T3 Code metadata; native homes and full local restoration closure require qualification")


class T3CodeAdapter:
    def __init__(self, *, profile_root):
        self.profile_root = local_root(profile_root)
        self.observation = None
        self._snapshot = None

    def snapshot_references(self, *, refresh=False):
        if self._snapshot is not None and not refresh:
            return self._snapshot
        path = self.profile_root / RELATIVE
        errors, references = [], []
        self.observation = None
        try:
            self.observation = read_database(self.profile_root)
        except (OSError, ValueError, sqlite3.Error, RecursionError) as exc:
            errors.append(SourceFailure(str(path), exc.kind if isinstance(exc, T3CodeError) else "t3code_database_unreadable",
                profile_root=self.profile_root, database=path, error_type="T3CodeInventoryIncomplete"))
        seen_engines = set()
        if self.observation:
            for row in self.observation["providers"]:
                backend = engine(row["driver"] or row["provider"])
                seen_engines.add(backend)
                if row["native_id"] is None or row["thread_id"] is None:
                    continue
                binding = hashlib.sha256(json.dumps([canonical_path(path), row], sort_keys=True).encode()).hexdigest()
                references.append(ClientReference("t3code", path, row["thread_id"], row["native_id"],
                    row["driver"] or row["provider"], backend, binding, kind=ReferenceKind.RESTORE,
                    lifecycle=ReferenceLifecycle.RESTORABLE, source_locator=V2 + "provider_threads/" + row["provider_thread_id"],
                    evidence_complete=False, opaque_native_locator=row["native_id"]))
            for copies in self.observation["threads"].values():
                seen_engines.update(engine(r["default_provider"]) for r in copies if r.get("default_provider"))
            for row in self.observation["legacy_providers"]:
                backend = engine(row["provider_name"])
                seen_engines.add(backend)
                for field in ("provider_thread_id", "provider_session_id"):
                    if row[field] is None:
                        continue
                    identifier(row[field])
                    binding = hashlib.sha256(json.dumps([canonical_path(path), row["thread_id"], field, row[field]]).encode()).hexdigest()
                    references.append(ClientReference("t3code", path, row["thread_id"], row[field], row["provider_name"], backend,
                        binding, kind=ReferenceKind.RESTORE, lifecycle=ReferenceLifecycle.RESTORABLE,
                        source_locator="projection_thread_sessions/" + field, evidence_complete=False, opaque_native_locator=row[field]))
        names = tuple(sorted(seen_engines)) or ("unknown",)
        descriptor = ClientDescriptor("t3code", profile_root=self.profile_root, sources=(path,),
            owner_process_root=self.profile_root, inventory_engines=names,
            capability_limits=tuple(capability(e) for e in names))
        self._snapshot = ReferenceSnapshot(descriptor, tuple(references), tuple(errors))
        return self._snapshot

    def describe_client(self):
        return self.snapshot_references().descriptor

    def native_catalog_for(self, engine):
        return None

    def inspect_runtime(self):
        return {"owner_client": "t3code", "owner_process_root": str(self.profile_root),
            "check_mode": "persisted_metadata", "clients_closed": None, "probe_complete": False,
            "coverage_complete": False, "errors": ["t3code_writer_coverage_unverified"]}


def build_inventory(adapters, *, engines=()):
    from .client_inventory import ClientInventory, ClientTarget
    selected = [a for a in adapters if isinstance(a, T3CodeAdapter)]
    if not selected:
        fail("adapter_missing")
    descriptors, errors, refs, targets, projects, resources, names, seen = [], [], [], [], {}, [], set(), set()
    for adapter in selected:
        snapshot = adapter.snapshot_references()
        errors.extend(snapshot.errors)
        key = canonical_path(adapter.profile_root)
        if key in seen:
            continue
        seen.add(key)
        descriptors.append(snapshot.descriptor)
        names.update(snapshot.descriptor.inventory_engines)
        refs.extend(snapshot.references)
        data = adapter.observation
        if data is None:
            continue
        resources.append((canonical_path(adapter.profile_root / RELATIVE), "t3code_database_metadata"))
        store = StoreKey("t3code", adapter.profile_root, kind="t3code_database")
        for identifier_, copies in data["threads"].items():
            references = tuple(r for r in snapshot.references if r.frontend_id == identifier_)
            backends = {r.engine for r in references} or {engine(r["default_provider"]) for r in copies if r.get("default_provider")} or {"unknown"}
            paths = {r["worktree_path"] or data["projects"][r["project_id"]]["workspace_root"] for r in copies}
            cwd = next(iter(paths)) if len(paths) == 1 else None
            project = ProjectKey.from_path("t3code", cwd) if cwd and is_local_absolute_locator(cwd) else None
            if project:
                projects[project.stable_id] = project
            for backend in sorted(backends):
                if engines and backend not in engines:
                    continue
                names.add(backend)
                targets.append(ClientTarget("t3code", backend, RecordKey(store, identifier_, kind="t3code_thread"),
                    project, None, (identifier_,), RecordClassification.UNVERIFIED, capability(backend),
                    references=references, frontend_binding_keys=tuple(r.binding_key for r in references),
                    is_subagent=any(r.get("parent_relationship") == "subagent" for r in copies), lineage_status="unknown",
                    blocker_codes=("t3code_full_cleanup_unverified",), record_metadata={
                        "scope": "database_metadata", "version": CONTRACT["version"], "upstream_commit": CONTRACT["upstream_commit"],
                        "snapshots": copies, "message_count": sum(r["count"] for r in data["message_counts"] if r["thread_id"] == identifier_),
                        "legacy_message_count": sum(r["count"] for r in data["legacy_message_counts"] if r["thread_id"] == identifier_),
                        "scheduled_task_count": sum(r["thread_id"] == identifier_ for r in data["schedules"]),
                        "peer_environment_count": data["peer_count"],
                        "coverage": {"native_root_verified": False, "full_record_closure": False, "remote_delete": False}}))
    names = tuple(engines) or tuple(sorted(names))
    return ClientInventory(client="t3code", engines=names, projects=tuple(projects.values()), records=(),
        frontend_sessions=(), unmapped_frontend_sessions=(), targets=tuple(targets), capabilities={e: capability(e) for e in names},
        errors=tuple(errors), descriptors=tuple(descriptors), references=tuple(refs), scanned_resources=tuple(resources))
