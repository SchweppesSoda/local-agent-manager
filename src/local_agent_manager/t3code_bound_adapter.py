"""Explicit local T3 Code thread/provider bindings; no inferred native homes."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
import math

from . import frozen_files
from .herdr_bound_adapter import native_catalog, _FrozenPathRecord
from .t3code_store import T3CodeAdapter, V2, RELATIVE, MAX_ROWS, fail, identifier, native_handle, local_root, engine as inventory_engine, rows, database_path
from .paseo_cleanup_files import decode
from .client_contracts import ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle, ReferenceSnapshot
from .record_identity import RecordKey, StoreKey, EngineCapability, canonical_path
from .sqlite_utils import connect_readonly
from .providers.t3 import strict_json
from contextlib import closing

SCHEMA = "larj.t3code-bindings.v1"
DATABASE_ONLY_SCHEMA = "larj.t3code-antigravity-database.v1"
_NATIVE_ENGINES = frozenset(("codex", "claude", "pi", "antigravity"))


def engine(provider):
    # Inventory alone still grants no native capability. Only this explicitly
    # bound T3 path qualifies the managed ACP layout.
    return 'antigravity' if provider == 'antigravity' else inventory_engine(provider)


def thread_engine(row, providers):
    backend = engine(row["default_provider"])
    if backend in _NATIVE_ENGINES:
        return backend
    # Recent T3 stores the custom instance ID as default_provider. Resolve its
    # driver from persisted provider rows for that exact thread and instance.
    matches = [item for item in providers if item["thread_id"] == row["thread_id"]]
    backends = {engine(item["driver"]) for item in matches}
    if (row["default_provider"] == row["provider_instance_id"] and len(backends) == 1
            and backends <= _NATIVE_ENGINES
            and all(item["provider_instance_id"] == row["provider_instance_id"] for item in matches)):
        return next(iter(backends))
    fail("provider_binding_conflict")


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
        if (not isinstance(item, dict) or set(item) - {"thread_id", "engine", "root", "agent_dir", "codex_binary", "provider_instance_id", "acp_runtime_binaries", "native_layout", "cli_binary", "cli_user_home"}
                or not {"thread_id", "engine", "root"} <= item.keys() or item["engine"] not in _NATIVE_ENGINES):
            fail("binding_store_invalid")
        identifier(item["thread_id"])
        if item["thread_id"] in seen:
            fail("binding_store_ambiguous")
        seen.add(item["thread_id"])
        local_root(item["root"])
        if item["engine"] == "pi":
            if "agent_dir" not in item:
                fail("binding_pi_agent_dir_required")
            local_root(item["agent_dir"])
        elif "agent_dir" in item:
            fail("binding_provider_fields_invalid")
        if "codex_binary" in item:
            local_root(item["codex_binary"])
        if item['engine'] == 'antigravity':
            from .antigravity_cli_binding import is_cli
            if is_cli(item):
                from .antigravity_cli_binding import check_binding
                identifier(item.get('provider_instance_id'))
            else:
                if set(item) & {'native_layout', 'cli_binary', 'cli_user_home'}:
                    fail('binding_provider_fields_invalid')
                from .antigravity_acp_cleanup import check_binding
            check_binding(value, item)
        elif set(item) & {'provider_instance_id', 'acp_runtime_binaries', 'native_layout', 'cli_binary', 'cli_user_home'}:
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


def _history_backend(db, item, bindings):
    backend = engine(item['provider'])
    if backend in _NATIVE_ENGINES:
        return backend
    binding = bindings.get(item['thread_id'])
    current = rows(db, f'SELECT default_provider,provider_instance_id FROM {V2}threads WHERE thread_id=?',
                   (item['thread_id'],))
    if (binding is None or len(current) != 1 or current[0]['provider_instance_id'] != item['provider']
            or item['provider_instance_id'] not in (None, item['provider'])
            or current[0]['default_provider'] != item['provider']
            and engine(current[0]['default_provider']) != binding['engine']):
        fail('provider_cleanup_unsupported')
    drivers = rows(db, f'''SELECT DISTINCT COALESCE(NULLIF(driver,''),provider) AS driver
        FROM {V2}provider_threads WHERE thread_id=? AND provider_instance_id=?''',
        (item['thread_id'], item['provider']))
    if not drivers or any(engine(row['driver']) != binding['engine'] for row in drivers):
        fail('provider_cleanup_unsupported')
    return binding['engine']


def _import_references(db, bindings):
    result = []
    imports = rows(db, '''SELECT thread_id,provider_name,provider_instance_id,resume_cursor_json,runtime_payload_json
        FROM provider_session_runtime''')
    fields = {'birthtimeMs', 'device', 'filePath', 'inode', 'mtimeMs', 'provider',
              'providerInstanceId', 'providerSessionId', 'size'}
    for item in imports:
        binding = bindings.get(item['thread_id'])
        backend = inventory_engine(item['provider_name'])
        cursor = strict_json(item['resume_cursor_json']) if item['resume_cursor_json'] is not None else None
        payload = strict_json(item['runtime_payload_json']) if item['runtime_payload_json'] is not None else None
        if (binding is None or backend not in {'codex', 'claude'} or binding['engine'] != backend
                or not isinstance(cursor, dict) or not isinstance(payload, dict)
                or set(payload) != {'cwd', 'importedTranscripts'}):
            fail('legacy_restore_closure_unqualified')
        if backend == 'codex' and set(cursor) == {'threadId'}:
            native = native_handle('codex', cursor['threadId'])
        elif backend == 'claude' and set(cursor) == {'threadId', 'resume'} and cursor['threadId'] == item['thread_id']:
            native = native_handle('claudeAgent', cursor['resume'])
        else:
            fail('legacy_restore_closure_unqualified')
        local_root(payload['cwd'])
        transcripts = payload['importedTranscripts']
        if not isinstance(transcripts, list) or not transcripts or len(transcripts) > MAX_ROWS:
            fail('legacy_restore_closure_unqualified')
        for entry in transcripts:
            if (not isinstance(entry, dict) or set(entry) != fields
                    or entry['provider'] != item['provider_name']
                    or entry['providerInstanceId'] != item['provider_instance_id']
                    or entry['providerSessionId'] != native):
                fail('legacy_restore_closure_unqualified')
            local_root(entry['filePath'])
            for key in ('birthtimeMs', 'device', 'inode', 'mtimeMs', 'size'):
                value = entry[key]
                if key in {'device', 'inode'} and isinstance(value, str) and value.isascii() and value.isdigit():
                    continue
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    fail('legacy_restore_closure_unqualified')
        result.append({'thread_id': item['thread_id'], 'provider_instance_id': item['provider_instance_id'],
                       'driver': item['provider_name'], 'native_id': native, 'strength': 'strong', 'legacy_import': True})
    return result


_THREAD_EVENT_FIELDS = '''createdBy creationSource id projectId title providerInstanceId modelSelection
    runtimeMode interactionMode branch worktreePath pullRequests activeProviderThreadId lineage forkedFrom
    createdAt updatedAt archivedAt settledOverride settledAt snoozedUntil snoozedAt lastVisitedAt deletedAt
    linkedPullRequest branchPullRequest historyOrigin unsettledAt pinnedAt pinOrderKey activeOrderKey
    guestShare autoSettleDisabledAt opens titleRegeneration'''
_SESSION_EVENT_FIELDS = '''id driver providerInstanceId status cwd model capabilities createdAt updatedAt
    lastError extensionStatuses'''
_RUN_EVENT_FIELDS = '''id threadId ordinal providerInstanceId modelSelection providerThreadId userMessageId
    rootNodeId activeAttemptId status queuePosition requestedAt startedAt completedAt checkpointId contextHandoffId
    workspacePreparation restartContinuationOfRunId workStartedAt delegatedCompletion delegatedTaskId'''
_ATTEMPT_EVENT_FIELDS = '''id nativeThreadId runId attemptOrdinal rootNodeId providerInstanceId providerThreadId
    providerTurnId reason status startedAt completedAt'''
_LEGACY_EVENT_FIELDS = {
    **{name: _THREAD_EVENT_FIELDS for name in ('thread.created', 'thread.deleted', 'thread.metadata-updated',
        'thread.model-selection-updated', 'thread.visited')},
    **{name: _SESSION_EVENT_FIELDS for name in ('provider-session.attached', 'provider-session.updated')},
    **{name: _RUN_EVENT_FIELDS for name in ('run.created', 'run.updated')},
    **{name: _ATTEMPT_EVENT_FIELDS for name in ('run-attempt.created', 'run-attempt.updated')},
    'checkpoint-scope.created': '''id threadId runId nodeId parentScopeId providerThreadId kind ordinalWithinParent
        advancesAppRunCount cwd createdAt''',
    'checkpoint.captured': '''id threadId scopeId runId nodeId parentCheckpointId ordinalWithinScope appRunOrdinal
        ref status files capturedAt''',
    'context-transfer.created': '''id type sourceThreadId targetThreadId sourcePoint basePoint sourceProviderInstanceId
        targetProviderInstanceId targetRunId status resolution createdBy error createdAt updatedAt consumedAt''',
    'message.updated': '''createdBy creationSource id threadId runId nodeId role text context attachments streaming
        createdAt updatedAt delegatedCompletion notification senderThreadId workflowScript''',
    'node.updated': '''id threadId runId parentNodeId rootNodeId kind status countsForRun providerThreadId providerTurnId
        nativeItemRef runtimeRequestId checkpointScopeId startedAt completedAt''',
    'plan.updated': 'id threadId runId nodeId status kind steps explanation',
    'provider-thread.updated': '''id driver providerInstanceId providerSessionId appThreadId ownerNodeId nativeThreadRef
        nativeConversationHeadRef status firstRunOrdinal lastRunOrdinal handoffIds forkedFrom createdAt updatedAt
        contextUsage pendingBackgroundTasks nativeMetadata goal''',
    'provider-turn.updated': '''id providerThreadId nodeId runAttemptId nativeTurnRef ordinal status startedAt completedAt
        tokenUsage model turnTokenUsage''',
    'runtime-request.updated': '''id nodeId providerTurnId nativeRequestRef kind status responseCapability createdAt
        resolvedAt answers''',
    'subagent.updated': '''id threadId runId parentNodeId origin createdBy driver providerInstanceId providerThreadId
        childThreadId nativeTaskRef prompt title model modelSelection status result startedAt completedAt updatedAt
        progress promptSequence completionWake completionDelivery''',
    'turn-item.updated': '''id threadId runId nodeId providerThreadId providerTurnId nativeItemRef parentItemId ordinal
        status title startedAt completedAt updatedAt createdBy creationSource type messageId inputIntent text context
        attachments input output exitCode streaming toolName subagentId origin driver providerInstanceId childThreadId
        prompt result progress promptSequence viewedImagePath checkpointId scopeId files patterns results fileName
        diffStr toolSurface toolSource changes planId steps explanation requestId questions responseMode questionAnswer
        failure retry source outcome summary detail senderThreadId workflowScript''',
}


def _qualify_legacy_events(db):
    """Admit known V2 mirror payloads, never arbitrary legacy restore events."""
    types = rows(db, "SELECT DISTINCT event_type FROM orchestration_events WHERE aggregate_kind='thread'")
    if any(row['event_type'] not in _LEGACY_EVENT_FIELDS for row in types):
        fail('legacy_restore_closure_unqualified')
    if db.execute("SELECT 1 FROM orchestration_events WHERE aggregate_kind='thread' AND json_type(payload_json)!='object' LIMIT 1").fetchone():
        fail('legacy_restore_closure_unqualified')
    # Qualify field names in SQLite. Private historical bodies are neither
    # exported nor materialized as a Python list of every event.
    fields = rows(db, '''SELECT DISTINCT event_type,j.key FROM orchestration_events AS t,
        json_each(t.payload_json) AS j WHERE aggregate_kind='thread' ''')
    for row in fields:
        if row['key'] not in _LEGACY_EVENT_FIELDS[row['event_type']].split():
            fail('legacy_restore_closure_unqualified')
    # A new restore cursor nested inside an otherwise known event is still an
    # unqualified recovery mechanism. Read-tool bodies remain audit content.
    if db.execute('''SELECT 1 FROM orchestration_events AS t,json_tree(t.payload_json) AS j
            WHERE aggregate_kind='thread' AND j.type!='null'
            AND (lower(j.key) LIKE '%resume%' OR lower(j.key) LIKE '%restore%')
            AND NOT lam_content_reference('orchestration_events','payload_json',t.payload_json,j.fullkey,t.event_type)
            LIMIT 1''').fetchone():
        fail('legacy_restore_closure_unqualified')


def history_references(root, bindings):
    """Project all persisted native thread handles without exporting payloads."""
    with closing(connect_readonly(database_path(root))) as db:
        db.execute("BEGIN")
        deadline = time.monotonic() + 20
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        from .t3code_store import qualify
        qualify(db)
        result = []
        from .t3code_database import THREAD_TABLES, _content_reference
        db.create_function('lam_content_reference', 5, _content_reference, deterministic=True)
        _qualify_legacy_events(db)
        # A replayable native handle can also occur in a run, handoff or
        # command payload. Inspect every qualified per-thread JSON payload.
        for table in (*THREAD_TABLES, 'orchestration_events'):
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            # A subagent row describes the child's provider, while thread_id
            # owns the parent's task metadata. Native handles belong to child.
            owner = 'child_thread_id' if table == V2 + 'subagents' else 'thread_id'
            predicate = '1'
            event_table = table in {'orchestration_events', 'orchestration_v2_events'}
            if event_table:
                physical_owner = 'stream_id' if table == 'orchestration_events' else 'thread_id'
                owner = f"CASE WHEN t.event_type='subagent.updated' THEN json_extract(t.payload_json,'$.childThreadId') ELSE t.{physical_owner} END"
            if table == 'orchestration_events':
                predicate = "aggregate_kind='thread'"
            if "provider" in columns:
                # Provider rows can exist without any projected native handle.
                provider = "COALESCE(NULLIF(driver, ''), provider)" if "driver" in columns else "provider"
                instance = 'provider_instance_id' if 'provider_instance_id' in columns else 'NULL'
                observed = rows(db, f"SELECT DISTINCT {owner} AS thread_id,{instance} AS provider_instance_id,{provider} AS provider FROM {table} AS t WHERE {provider} IS NOT NULL AND {owner} IS NOT NULL")
                for item in observed:
                    backend = _history_backend(db, item, bindings)
                    if backend == 'antigravity' and 'provider_instance_id' in columns:
                        bound = bindings.get(item['thread_id'])
                        if bound is None or bound['engine'] != 'antigravity':
                            fail('provider_binding_conflict')
                        if item['provider_instance_id'] != bound['provider_instance_id']:
                            fail('antigravity_historical_instance_unbound')
            if "payload_json" not in columns:
                continue
            event_type = 't.event_type' if 'event_type' in columns else 'NULL'
            content_filter = f"NOT lam_content_reference(?, 'payload_json', t.payload_json, j.fullkey, {event_type})"
            if db.execute(f'''SELECT 1 FROM {table} AS t,json_tree(t.payload_json) AS j WHERE ({predicate}) AND (
                    (j.key='nativeThreadRef' AND j.type NOT IN ('object','null')) OR
                    (j.key='nativeThreadId' AND j.type NOT IN ('text','null')))
                    AND {content_filter} LIMIT 1''', (table,)).fetchone():
                fail("native_reference_shape_unqualified")
            instance = 't.provider_instance_id' if 'provider_instance_id' in columns else 'NULL'
            if event_table:
                instance = "json_extract(t.payload_json,'$.providerInstanceId')"
            owner_expr = owner if event_table else 't.' + owner
            result.extend(rows(db, f'''SELECT DISTINCT {owner_expr} AS thread_id, {instance} AS provider_instance_id,
                json_extract(j.value,'$.driver') AS driver, json_extract(j.value,'$.nativeId') AS native_id,
                json_extract(j.value,'$.strength') AS strength
                FROM {table} AS t,json_tree(t.payload_json) AS j
                WHERE ({predicate}) AND j.key='nativeThreadRef' AND j.type='object'
                AND {content_filter} ''', (table,)))
            result.extend(rows(db, f'''SELECT DISTINCT {owner_expr} AS thread_id, NULL AS driver,
                j.value AS native_id, 'bare' AS strength FROM {table} AS t,json_tree(t.payload_json) AS j
                WHERE ({predicate}) AND j.key='nativeThreadId' AND j.type='text'
                AND {content_filter} ''', (table,)))
            if len(result) > MAX_ROWS:
                fail("inventory_budget_exceeded")
        # Legacy imports remain read-only protection sources. Their exact
        # cursors and transcript metadata supply native IDs independently of
        # the provider root selected for new turns in today's configuration.
        result.extend(_import_references(db, bindings))
        if db.execute('SELECT 1 FROM projection_threads LIMIT 1').fetchone():
            fail("legacy_restore_closure_unqualified")
    return result


class T3CodeBoundAdapter(T3CodeAdapter):
    def __init__(self, manifest):
        self.manifest = json.loads(json.dumps(validate_manifest(manifest)))
        super().__init__(profile_root=manifest["profile_root"])
        self.bindings = {item["thread_id"]: item for item in manifest["native_stores"]}
        self.frozen_references = ()
        self.observations = ()
        self.legacy_import_ids = set()

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
            if binding["engine"] != thread_engine(row, data["providers"]):
                fail("provider_binding_conflict")
            if any(p['thread_id'] == thread_id and engine(p['driver'] or p['provider']) != binding['engine'] for p in data['providers']):
                fail('provider_binding_conflict')
            if binding['engine'] == 'antigravity':
                from .antigravity_cli_binding import is_cli, selection
                if is_cli(binding):
                    selection(self.manifest, binding, 't3code')
                if (row['provider_instance_id'] != binding['provider_instance_id'] or any(
                        p['thread_id'] == thread_id and p['provider_instance_id'] != binding['provider_instance_id']
                        for p in data['providers'])):
                    fail('antigravity_instance_binding_conflict')
            observations.append({"id": thread_id, "parent_thread_id": row["parent_thread_id"],
                "source": str(self.profile_root / RELATIVE)})
            backend, root = binding["engine"], canonical_path(binding["root"])
            stores[(backend, root)] = StoreKey(backend, Path(root),
                kind="session_root" if backend == "pi" else "config_dir" if backend == "claude" else "directory")
        seen = set()
        history = history_references(self.profile_root, self.bindings)
        self.legacy_import_ids = {row['thread_id'] for row in history if row.get('legacy_import')}
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
            native_handle(row["driver"], row["native_id"])
            binding = self.bindings.get(row["thread_id"])
            if binding is None or binding["engine"] != engine(row["driver"]) or row["thread_id"] not in data["threads"]:
                fail("historical_provider_binding_unqualified")
            key_ = (row["thread_id"], row["driver"], row["native_id"])
            if key_ in seen:
                continue
            seen.add(key_)
            store = stores[(binding["engine"], canonical_path(binding["root"]))]
            if binding["engine"] == "pi":
                native = self._pi_record(binding, row, store)
            else:
                native = RecordKey(store, row["native_id"], kind="session" if binding["engine"] == "claude" else "record")
            fingerprint = hashlib.sha256(json.dumps([canonical_path(self.profile_root), key_, native.to_dict()], sort_keys=True).encode()).hexdigest()
            references.append(ClientReference("t3code", self.profile_root / RELATIVE, row["thread_id"], native.record_id,
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

    def _pi_record(self, binding, row, store):
        path = local_root(row["native_id"])
        try:
            path.relative_to(local_root(binding["root"]))
        except ValueError:
            fail("pi_path_outside_bound_root")
        frozen = [ref["native_record"] for ref in self.frozen_references
                  if ref["frontend_id"] == row["thread_id"] and ref["engine"] == "pi"
                  and str(Path(ref["native_record"]["path"])) == str(path)]
        if frozen:
            approved = frozen[0]
            try:
                path.lstat()
            except FileNotFoundError:
                pass
            else:
                if canonical_path(path) != approved["canonical_path"]:
                    fail("pi_path_binding_changed")
            return _FrozenPathRecord(store, approved["record_id"], kind="session", path=path,
                                     approved_path_key=approved["canonical_path"])
        catalog = native_catalog(binding)
        records = [record for record in catalog.records if canonical_path(record.path) == canonical_path(path)]
        if catalog.errors or len(records) != 1 or records[0].cwd is None:
            fail("pi_header_unverified")
        return RecordKey(store, records[0].session_id, kind="session", path=path)
