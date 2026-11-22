"""Bind a local Cindy plugin session to the independently selected agy CLI store."""
from contextlib import closing
from pathlib import Path

from . import antigravity_cli_cleanup as native, frontend_session_cleanup as frontend
from .cindy_references import build_cindy_reference_catalog
from .discovery import discover_cindy_profiles, resolve_cindy_profiles
from .record_identity import canonical_path


def explicit_databases(options):
    database, root, appdata = (options.get(key) for key in ('cindy_db', 'cindy_root', 'appdata'))
    adapters = options.get('adapters')
    if database:
        return {Path(database).absolute()}
    if root or appdata:
        return {p.database.absolute() for p in resolve_cindy_profiles(appdata, root=root)}
    if adapters is not None:
        return {a.database.absolute() for a in adapters if getattr(a, 'name', None) == 'cindy'}
    return None


def databases(extra=()):
    profiles = list(discover_cindy_profiles())
    for database in extra:
        profiles.extend(resolve_cindy_profiles(database=Path(database)))
    return sorted({p.database.absolute() for p in profiles if p.database.is_file()})


def references(copy):
    catalog = build_cindy_reference_catalog(copy)
    if catalog.failures:
        native.fail('cindy_references_unqualified')
    return catalog.references


def select(sources, ids):
    """Freeze whole frontend rows only when every selected session is single-engine."""
    seeds = []
    for source in sources:
        if source['client'] != 'cindy':
            continue
        root, name = Path(source['root']), source['database']
        with native.private_database(root, name) as (_, _, copy):
            for ref in references(copy):
                if ref.cindy_session_id not in ids:
                    continue
                if ref.is_historical or ref.backend != 'antigravity':
                    native.fail('cindy_local_agy_binding_required')
                native.native_id(ref.native_session_id)
                seeds.append({'database': str(root/name), 'session_id': ref.cindy_session_id,
                              'expected_status': ref.session_status, 'explicitly_selected': True,
                              'session_schema_fingerprint': ref.session_schema_fingerprint,
                              'session_row_fingerprint': ref.session_row_fingerprint})
    if (len(seeds) != len(ids) or {s['session_id'] for s in seeds} != set(ids)
            or len({s['database'] for s in seeds}) != 1):
        native.fail('cindy_selection_missing_or_ambiguous')
    evidence = frontend.build_cindy_session_delete_evidence(seeds)
    frontend.guard_cindy_session_rows(evidence)
    return [item.to_dict() for item in evidence]


def inspect(source, ids, selected, reader):
    root, name = Path(source['root']), source['database']
    allowed = {row['session_id']: row for row in selected
               if canonical_path(row['database']) == canonical_path(root/name)}
    found = set()
    with native.private_database(root, name, reader=reader) as (_, proofs, copy):
        for ref in references(copy):
            row = allowed.get(ref.cindy_session_id)
            if row:
                if (ref.is_historical or ref.backend != 'antigravity'
                        or ref.native_session_id != row['expected_sdk_session_id']
                        or ref.session_row_fingerprint != row['session_row_fingerprint']):
                    native.fail('cindy_binding_changed')
                found.add(ref.cindy_session_id)
            elif ref.native_session_id in ids:
                native.fail('cindy_native_reference')
    if found != allowed.keys():
        native.fail('cindy_binding_changed')
    return proofs


def dependency_keys(sessions):
    ids = [s['session_id'] for s in sessions]
    with closing(frontend._connect(Path(sessions[0]['database']), readonly=True, vector_extension=None)) as db:
        db.execute('BEGIN')
        messages, vectors = frontend._dependent_ids(db, ids)
    return {'message_ids': list(messages), 'vector_rows': list(vectors)}


def remaining(document):
    sessions = document['cindy_sessions']
    database = Path(sessions[0]['database'])
    ids = [s['session_id'] for s in sessions]
    keys = document['cindy_dependency_keys']
    with native.private_database(database.parent, database.name) as (_, _, copy):
        with closing(frontend._connect(copy, readonly=True, vector_extension=None)) as db:
            if frontend._schema_hash(db) != sessions[0]['schema_bundle_fingerprint']:
                native.fail('cindy_schema_changed')
            present = frontend._selected_ids(db, 'sessions', 'id', ids)
            if not present:
                frontend._verify_absent(db, ids, keys['message_ids'], keys['vector_rows'])
            return len(present)


def remove(document, census, phase):
    from . import antigravity_cli_guards as guards
    sessions = document['cindy_sessions']
    database = Path(sessions[0]['database'])
    sources = document['guard_sources']
    ids = document['antigravity_cli_evidence']['ids']
    guards.recheck(sources, ids, cindy_sessions=sessions)
    guards.require_closed(sources, census)
    source = next(s for s in sources if s['client'] == 'cindy' and Path(s['root'])/s['database'] == database)

    def check_transaction(db, messages, vectors):
        # The exclusive native-file leases have closed. Recheck this physical
        # namespace and all current/switch bindings inside SQLite's write lock.
        from .cindy_references import _optional_native_id, _switch_payload
        if native.ff._identity(database.stat()) != source['files'][0]['identity']:
            native.fail('cindy_database_replaced')
        if {'message_ids': list(messages), 'vector_rows': list(vectors)} != document['cindy_dependency_keys']:
            native.fail('cindy_dependencies_changed')
        allowed = {s['session_id']: s['expected_sdk_session_id'] for s in sessions}
        for sid, handle in db.execute('SELECT id,sdk_session_id FROM sessions'):
            handle = _optional_native_id(handle, 'sessions.sdk_session_id')
            if (sid in allowed and handle != allowed[sid]) or (sid not in allowed and handle in ids):
                native.fail('cindy_binding_changed')
        for sid, content in db.execute("SELECT session_id,content FROM messages WHERE role='agent_switch'"):
            handle = _optional_native_id(_switch_payload(content).get('fromSdkSessionId'), 'fromSdkSessionId')
            if handle and (sid in allowed or handle in ids):
                native.fail('cindy_native_reference')

    frontend.execute_cindy_session_cleanup(sessions, owner_process_root=database.parent,
                                           transaction_guard=check_transaction, phase_callback=phase)
