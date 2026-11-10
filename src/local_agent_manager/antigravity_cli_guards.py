"""Read-only protection of known T3/Paseo native handles and restore copies.

An ID match is sufficient to block deletion, never sufficient to assign a
native home or authorize removal of a frontend record. Deleted/archived
frontend rows remain protection sources while they retain a native handle.
"""
from pathlib import Path
import time

from . import frozen_files as ff
from .antigravity_cli_cleanup import fail, private_database, root_path
from .record_identity import canonical_path


def locations(*, t3code_roots=(), paseo_roots=()):
    from .t3code_store import default_root as t3_default
    from .paseo_store import default_root as paseo_default
    # Explicit roots add coverage; they cannot replace default/env protection.
    groups = {'t3code': [Path.home()/'.t3', t3_default(), *map(Path, t3code_roots)],
              'paseo': [Path.home()/'.paseo', paseo_default(), *map(Path, paseo_roots)]}
    return [{'client': client, 'root': str(path)} for client, paths in groups.items()
            for _, path in sorted({canonical_path(p): p for p in paths}.items())]


def freeze(sources, ids, *, joint=None):
    """Allow only the exact frozen frontend transform to remove protection.

    Scan its private after-image, including every unselected row/restore
    family. A profile-level exemption or arbitrary list of UUIDs is never
    accepted. The owning cleanup module validates the joint closure first.
    """
    result = []
    for source in sources:
        root = Path(source['root'])
        item = dict(source)
        try:
            root.lstat()
        except FileNotFoundError:
            # Check any existing prefix, including reparse points.
            for parent in reversed(root.parents):
                if parent.exists():
                    ff._plain(parent.lstat(), directory=True)
            result.append({**item, 'absent': True})
            continue
        root_path(root)
        item['identity'] = ff._identity(root.stat())
        owned = bool(joint and source['client'] == joint['client']
                     and canonical_path(root) == canonical_path(joint['files']['root']))
        if owned:
            from .office_cleanup import digest
            item['joint_files_sha256'] = digest(joint['files'])
            if source['client'] == 't3code':
                from .t3code_cleanup_files import remaining
            else:
                from .paseo_cleanup_files import remaining
            remaining(joint['files'])
        if source['client'] == 't3code':
            from .t3code_store import RELATIVE, qualify
            from .t3code_cleanup_files import _names, _require_known_database_family
            _require_known_database_family(root)
            item['directories'] = [_names(root, name) for name in
                                   ('userdata', 'userdata/backups', 'userdata/snapshots')]
            if any(d.get('entries') for d in item['directories'][1:]):
                fail('t3code_recovery_source_unqualified')
            with private_database(root, RELATIVE) as (db, proofs, _):
                qualify(db)
                if owned:
                    from .t3code_database import _transform
                    from .office_database import logical_hash
                    database = joint['files']['database']
                    current = logical_hash(db)
                    if current == database['before_sha256']:
                        _transform(db, database['thread_ids'])
                    elif current != database['after_sha256']:
                        fail('joint_database_state_unknown')
                    if logical_hash(db) != database['after_sha256']:
                        fail('joint_database_transform_changed')
                # Project only counts, never a payload or credential. Include
                # legacy events and all restore-capable tables, not merely the
                # currently visible provider_threads projection.
                tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                deadline = time.monotonic()+20
                db.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
                for table in tables:
                    quoted = '"'+table.replace('"', '""')+'"'
                    columns = [r[1] for r in db.execute('PRAGMA table_info('+quoted+')')]
                    for column in columns:
                        qcol = '"'+column.replace('"', '""')+'"'
                        for sid in ids:
                            if db.execute('SELECT 1 FROM '+quoted+' WHERE instr(CAST('+qcol+' AS BLOB),?)>0 LIMIT 1',
                                          (sid.encode(),)).fetchone():
                                fail('t3code_native_or_restore_reference')
                            # JSON escapes must not hide a UUID from the
                            # cross-profile guard. Interpret JSON only as a
                            # protection source, never as ownership proof.
                            if db.execute('SELECT 1 FROM '+quoted+' AS t, json_tree(CASE WHEN json_valid(t.'+qcol+') '
                                          'THEN t.'+qcol+" ELSE 'null' END) AS j WHERE j.type='text' AND instr(j.value,?)>0 LIMIT 1",
                                          (sid,)).fetchone():
                                fail('t3code_native_or_restore_reference')
                item['files'] = [] if owned else proofs
                if owned:
                    # The owning lifecycle protects this DB; its byte changes
                    # are authorized by the exact logical before/after pair.
                    item['directories'] = item['directories'][1:]
        elif source['client'] == 'paseo':
            from .paseo_cleanup_files import rows, transform
            entries, directories = rows(root)
            item['directories'] = directories
            item['files'] = []
            for entry in entries:
                raw = transform(entry, set(joint['files']['agent_ids'])) if owned else entry['raw']
                if raw is None:
                    continue
                if any(sid.encode() in raw for sid in ids):
                    fail('paseo_native_or_restore_reference')
                row = entry['row']
                if any(h['value'] in ids for h in row.get('references', ())):
                    fail('paseo_native_or_restore_reference')
                if row.get('provider') == 'antigravity' and not row.get('antigravity_handle_qualified'):
                    fail('paseo_opaque_native_reference')
                if not owned:
                    item['files'].append(entry['before'])
            if owned:
                item['directories'] = []
        else:
            fail('guard_client_unqualified')
        result.append(item)
    return result


def recheck(evidence, ids, *, joint=None):
    sources = [{'client': s['client'], 'root': s['root']} for s in evidence]
    # A newly selected default profile also becomes a protection source.
    expected = {(s['client'], canonical_path(s['root'])) for s in sources}
    if any((s['client'], canonical_path(s['root'])) not in expected for s in locations()):
        fail('guard_discovery_changed')
    if freeze(sources, ids, joint=joint) != evidence:
        fail('guard_source_changed')


def acquire(evidence, held):
    for source in evidence:
        for proof in source.get('files', ()):
            held.acquire(Path(source['root']), proof)


def require_closed(evidence, census=None):
    """An existing frontend may retain handles in memory after disk removal."""
    from .office_runtime import processes, _RUNTIMES
    rows = (census or processes)()
    if not isinstance(rows, (list, tuple)) or len(rows) > 10000:
        fail('frontend_census_unverified')
    roots = [str(s['root']).replace('\\', '/').casefold() for s in evidence]
    for row in rows:
        if not isinstance(row, dict) or type(row.get('pid')) is not int:
            fail('frontend_census_unverified')
        name = str(row.get('name') or '').casefold()
        if not name:
            fail('frontend_census_unverified')
        if name.startswith(('t3 code', 't3code', 'paseo')):
            fail('frontend_writer_running')
        if name in _RUNTIMES:
            command = row.get('command')
            if not isinstance(command, str):
                fail('frontend_census_unverified')
            command = command.replace('\\', '/').casefold()
            if any(root in command for root in roots) or 't3code' in command or 'paseo' in command:
                fail('frontend_writer_running')
