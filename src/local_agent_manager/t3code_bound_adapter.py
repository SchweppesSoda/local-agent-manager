"""Explicit local T3 Code thread/provider bindings; no inferred native homes."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time

from . import frozen_files
from .herdr_bound_adapter import native_catalog
from .t3code_store import T3CodeAdapter, V2, RELATIVE, MAX_ROWS, fail, identifier, local_root, engine as inventory_engine, rows, database_path
from .paseo_cleanup_files import decode
from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot
from .record_identity import RecordKey, StoreKey, EngineCapability, canonical_path
from .sqlite_utils import connect_readonly
from contextlib import closing

SCHEMA = "larj.t3code-bindings.v1"
DATABASE_ONLY_SCHEMA = "larj.t3code-antigravity-database.v1"
_NATIVE_ENGINES = frozenset(("codex", "claude", "antigravity"))


def engine(provider):
    # Inventory alone still grants no native capability. Only this explicitly
    # bound T3 path qualifies the managed ACP layout.
    return 'antigravity' if provider == 'antigravity' else inventory_engine(provider)


def validate_manifest(value):
    fields = {"schema_version", "profile_root", "environment_id", "runtime_binaries", "native_stores", "desktop_profiles"}
    if not isinstance(value, dict):
        fail("binding_manifest_invalid")
    database_only = value.get("schema_version") == DATABASE_ONLY_SCHEMA
    if database_only:
        fields.add("provider_instance_id")
    if set(value) != fields or value["schema_version"] not in {SCHEMA, DATABASE_ONLY_SCHEMA}:
        fail("binding_manifest_invalid")
    local_root(value["profile_root"])
    identifier(value["environment_id"])
    if not isinstance(value["runtime_binaries"], list) or not value["runtime_binaries"]:
        fail("binding_runtime_required")
    for path in value["runtime_binaries"]:
        local_root(path)
    if database_only:
        identifier(value["provider_instance_id"])
        if value["native_stores"] != [] or value["desktop_profiles"] != []:
            fail("database_only_binding_scope_invalid")
        return value
    if not isinstance(value["native_stores"], list) or not value["native_stores"]:
        fail("binding_native_stores_required")
    seen = set()
    for item in value["native_stores"]:
        if (not isinstance(item, dict) or set(item) - {"thread_id", "engine", "root", "codex_binary", "provider_instance_id", "acp_runtime_binaries"}
                or not {"thread_id", "engine", "root"} <= item.keys() or item["engine"] not in _NATIVE_ENGINES):
            fail("binding_store_invalid")
        identifier(item["thread_id"])
        if item["thread_id"] in seen:
            fail("binding_store_ambiguous")
        seen.add(item["thread_id"])
        local_root(item["root"])
        if "codex_binary" in item:
            local_root(item["codex_binary"])
        if item['engine'] == 'antigravity':
            from .antigravity_acp_cleanup import check_binding
            check_binding(value, item)
        elif set(item) & {'provider_instance_id', 'acp_runtime_binaries'}:
            fail('binding_provider_fields_invalid')
    if not isinstance(value["desktop_profiles"], list):
        fail("binding_desktop_profiles_invalid")
    profiles = set()
    for item in value["desktop_profiles"]:
        if (not isinstance(item, dict) or set(item) != {"root", "runtime_binaries"}
                or not isinstance(item["runtime_binaries"], list) or not item["runtime_binaries"]):
            fail("binding_desktop_profile_invalid")
        local_root(item["root"])
        if canonical_path(item["root"]) in profiles:
            fail("binding_desktop_profile_ambiguous")
        profiles.add(canonical_path(item["root"]))
        for path in item["runtime_binaries"]:
            local_root(path)
    return value


def load_manifest(path):
    path = Path(path)
    _, raw = frozen_files.read_file(path.parent, path.name, content=True, limit=1024 * 1024)
    return validate_manifest(decode(raw))


def history_references(root, bindings):
    """Project all persisted native thread handles without exporting payloads."""
    with closing(connect_readonly(database_path(root))) as db:
        db.execute("BEGIN")
        deadline = time.monotonic() + 20
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        from .t3code_store import qualify
        qualify(db)
        result = []
        from .t3code_database import THREAD_TABLES
        # A replayable native handle can also occur in a run, handoff or
        # command payload. Inspect every qualified per-thread JSON payload.
        for table in THREAD_TABLES:
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            if "provider" in columns:
                # Provider rows can exist without any projected native handle.
                provider = "COALESCE(NULLIF(driver, ''), provider)" if "driver" in columns else "provider"
                observed = rows(db, f"SELECT DISTINCT {provider} AS provider FROM {table} WHERE {provider} IS NOT NULL")
                if any(engine(row["provider"]) not in _NATIVE_ENGINES for row in observed):
                    fail("provider_cleanup_unsupported")
                if 'provider_instance_id' in columns:
                    for item in rows(db, f"SELECT DISTINCT thread_id,provider_instance_id FROM {table} WHERE {provider}='antigravity'"):
                        bound = bindings.get(item['thread_id'])
                        if bound is None or bound['engine'] != 'antigravity':
                            fail('provider_binding_conflict')
                        if item['provider_instance_id'] != bound['provider_instance_id']:
                            fail('antigravity_historical_instance_unbound')
            if "payload_json" not in columns:
                continue
            if db.execute(f'''SELECT 1 FROM {table} AS t,json_tree(t.payload_json) AS j WHERE
                    (j.key='nativeThreadRef' AND j.type NOT IN ('object','null')) OR
                    (j.key='nativeThreadId' AND j.type NOT IN ('text','null')) LIMIT 1''').fetchone():
                fail("native_reference_shape_unqualified")
            instance = 't.provider_instance_id' if 'provider_instance_id' in columns else 'NULL'
            result.extend(rows(db, f'''SELECT DISTINCT t.thread_id, {instance} AS provider_instance_id,
                json_extract(j.value,'$.driver') AS driver, json_extract(j.value,'$.nativeId') AS native_id,
                json_extract(j.value,'$.strength') AS strength
                FROM {table} AS t,json_tree(t.payload_json) AS j
                WHERE j.key='nativeThreadRef' AND j.type='object' '''))
            result.extend(rows(db, f'''SELECT DISTINCT t.thread_id, NULL AS driver,
                j.value AS native_id, 'bare' AS strength FROM {table} AS t,json_tree(t.payload_json) AS j
                WHERE j.key='nativeThreadId' AND j.type='text' '''))
            if len(result) > MAX_ROWS:
                fail("inventory_budget_exceeded")
        # V1 runtime resume payloads and imported transcripts require separate
        # qualification before they can participate in a full deletion.
        if (any(db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() for table in
               ("projection_threads", "provider_session_runtime")) or
                db.execute("SELECT 1 FROM orchestration_events WHERE aggregate_kind='thread' LIMIT 1").fetchone()):
            fail("legacy_restore_closure_unqualified")
    return result


class T3CodeBoundAdapter(T3CodeAdapter):
    def __init__(self, manifest):
        self.manifest = json.loads(json.dumps(validate_manifest(manifest)))
        super().__init__(profile_root=manifest["profile_root"])
        self.bindings = {item["thread_id"]: item for item in manifest["native_stores"]}
        self.frozen_references = ()
        self.observations = ()

    def snapshot_references(self, *, refresh=False):
        _, raw = frozen_files.read_file(self.profile_root, "userdata/environment-id", content=True, limit=256)
        if raw.decode("utf-8").strip() != self.manifest["environment_id"]:
            fail("environment_identity_changed")
        snapshot = super().snapshot_references(refresh=True)
        if snapshot.errors or self.observation is None:
            fail("database_inventory_incomplete")
        if self.manifest["schema_version"] == DATABASE_ONLY_SCHEMA:
            return snapshot
        data = self.observation
        if data["peer_count"]:
            fail("peer_environment_closure_unqualified")
        references, observations, stores = [], [], {}
        for thread_id, copies in data["threads"].items():
            binding = self.bindings.get(thread_id)
            if binding is None or len(copies) != 1 or copies[0]["version"] != "v2":
                fail("thread_native_root_unbound")
            row = copies[0]
            if binding["engine"] != engine(row["default_provider"]):
                fail("provider_binding_conflict")
            if any(p['thread_id'] == thread_id and engine(p['driver'] or p['provider']) != binding['engine'] for p in data['providers']):
                fail('provider_binding_conflict')
            if binding['engine'] == 'antigravity':
                if (row['provider_instance_id'] != binding['provider_instance_id'] or any(
                        p['thread_id'] == thread_id and p['provider_instance_id'] != binding['provider_instance_id']
                        for p in data['providers'])):
                    fail('antigravity_instance_binding_conflict')
            observations.append({"id": thread_id, "parent_thread_id": row["parent_thread_id"],
                "source": str(self.profile_root / RELATIVE)})
            backend, root = binding["engine"], canonical_path(binding["root"])
            stores[(backend, root)] = StoreKey(backend, Path(root), kind="config_dir" if backend == "claude" else "directory")
        seen = set()
        history = history_references(self.profile_root, self.bindings)
        strong = {(r["thread_id"], r["native_id"]) for r in history if r["strength"] == "strong"}
        for row in history:
            if row["driver"] is not None and engine(row["driver"]) not in _NATIVE_ENGINES:
                fail("historical_provider_cleanup_unsupported")
            if row["strength"] == "bare":
                # Run attempts and handoff delivery carry bare historical IDs.
                # They need a same-thread strong provider reference, not a guess.
                if (row["thread_id"], row["native_id"]) not in strong:
                    fail("historical_native_reference_unbound")
                continue
            if row["strength"] not in {"strong", "none"} or (row["native_id"] is not None and row["strength"] != "strong"):
                fail("native_reference_strength_unqualified")
            binding = self.bindings.get(row['thread_id'])
            if binding is None or binding['engine'] != engine(row['driver']):
                fail('historical_provider_binding_unqualified')
            if binding['engine'] == 'antigravity' and row['provider_instance_id'] != binding['provider_instance_id']:
                fail('antigravity_historical_instance_unbound')
            if row["native_id"] is None:
                if row["strength"] != "none":
                    fail("native_reference_strength_unqualified")
                continue
            identifier(row["native_id"])
            binding = self.bindings.get(row["thread_id"])
            if binding is None or binding["engine"] != engine(row["driver"]) or row["thread_id"] not in data["threads"]:
                fail("historical_provider_binding_unqualified")
            key_ = (row["thread_id"], row["driver"], row["native_id"])
            if key_ in seen:
                continue
            seen.add(key_)
            native = RecordKey(stores[(binding["engine"], canonical_path(binding["root"]))], row["native_id"],
                               kind="session" if binding["engine"] == "claude" else "record")
            fingerprint = hashlib.sha256(json.dumps([canonical_path(self.profile_root), key_, native.to_dict()], sort_keys=True).encode()).hexdigest()
            references.append(ClientReference("t3code", self.profile_root / RELATIVE, row["thread_id"], row["native_id"],
                row["driver"], binding["engine"], fingerprint, native_record=native, kind=ReferenceKind.RESTORE,
                lifecycle=ReferenceLifecycle.RESTORABLE, source_locator="provider-native", evidence_complete=True,
                opaque_native_locator=row["native_id"]))
        names = tuple(sorted({b["engine"] for b in self.bindings.values()}))
        descriptor = ClientDescriptor("t3code", profile_root=self.profile_root, sources=(self.profile_root / RELATIVE,),
            native_stores=tuple(stores.values()), owner_process_root=self.profile_root, inventory_engines=names,
            capability_limits=tuple(EngineCapability("t3code", e, inventory=True, verify=True,
                reason="T3 Code mutation requires an approved local closure and held server ownership") for e in names))
        self.observations = tuple(observations)
        return ReferenceSnapshot(descriptor, tuple(references))
