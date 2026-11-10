"""Current native proof inside an exact, post-completion recovery ticket.

This module neither authorizes deletion nor changes a frozen after hash. Opaque
preserved stores still require exact preservation. Diagnostics have no restore
role, but their namespace, links and handles remain qualified.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path

from . import antigravity_cli_cleanup as native, antigravity_cli_restore as restore
from . import frozen_files as ff
from .office_database import logical_hash
from .windows_held_files import exclusive_fd, ensure_descriptor_capacity


def _reject_reference(raw, ids):
    # Interpret JSON escapes as well as literal bytes. Never emit any content.
    decoded = b''
    if raw.lstrip().startswith((b'{', b'[')):
        import json
        # SQL TEXT and artifacts can start with Markdown brackets. Structural
        # JSON sources are separately required to pass their exact decoders.
        try:
            value = json.loads(raw, object_pairs_hook=lambda pairs: pairs)
        except (ValueError, UnicodeError, RecursionError):
            pass
        else:
            decoded = json.dumps(value, ensure_ascii=False).encode()
    if any(s.encode() in raw or s.encode() in decoded for s in ids):
        native.fail('current_restore_reference')


class CurrentNative:
    def __init__(self, evidence, held):
        self.evidence = evidence
        self.root = native.validate(evidence)
        self.held = held
        self.names = native._root_names(self.root)
        self.link = native._log_link(self.root)
        self.trees = {}
        self.proof = None

    def read(self, root, relative, **options):
        if Path(root) != self.root:
            native.fail('current_reader_root_changed')
        return self.held.read(root, relative, **options)

    def stable(self):
        if (ff._identity(self.root.stat()) != self.evidence['root_identity']
                or native._root_names(self.root) != self.names
                or native._log_link(self.root) != self.link):
            native.fail('current_profile_changed')
        for tree in self.trees.values():
            if ff.freeze_remove(self.root, tree['path'], reader=self.read) != tree:
                native.fail('current_profile_changed')

    def qualify(self):
        self.stable()
        if self.proof is None:
            self.proof = self.inspect()
        return self.proof

    def inspect(self):
        e, root = self.evidence, self.root
        ids = set(e['ids'])
        allowed = native.PRESERVED | {'conversations', 'brain', 'annotations', 'presence',
            'cli.log', restore.PROTO, *(native.SUMMARY+s for s in native.SIDECARS)}
        if set(self.names) - allowed:
            native.fail('current_profile_layout_unqualified')
        for change in e['changes']:
            if ff.checked_path(root, change['path']).exists():
                native.fail('current_target_reappeared')
        with native.private_database(root, native.SUMMARY, reader=self.read) as (db, _, _):
            native._check(db, native.CONTRACT)
            if [tuple(row) for row in db.execute('PRAGMA integrity_check')] != [('ok',)]:
                native.fail('current_summary_corrupt')
            if native._schema_hash(native._objects(db)) != e['summary_schema_sha256']:
                native.fail('summary_schema_changed')
            rows = native._rows(db)
            present = set(rows)
            preserved = set(e['parents']) - ids
            if present & ids or not preserved <= present:
                native.fail('current_summary_target_or_missing_unselected')
            raw_hashes = {sid: hashlib.sha256(row['raw_summary'] or b'').hexdigest()
                          for sid, row in rows.items()}
            for row in rows.values():
                for cell in row:
                    if isinstance(cell, (str, bytes)):
                        _reject_reference(cell.encode() if isinstance(cell, str) else cell, ids)
            fts_ids = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversations_fts'").fetchone():
                fts_ids = sorted(r[0] for r in db.execute('SELECT DISTINCT conversation_id FROM conversations_fts'))
                if not set(fts_ids) <= present:
                    native.fail('current_search_orphan_or_target')
                for row in db.execute('SELECT * FROM conversations_fts'):
                    for cell in row:
                        if isinstance(cell, (str, bytes)):
                            _reject_reference(cell.encode() if isinstance(cell, str) else cell, ids)
            summary_hash = logical_hash(db)
        directories = [native._names(root, name) for name in
                       ('conversations', 'brain', 'cache', 'annotations', 'presence')]
        native._qualify_sessions(root, rows, ids, directories, reader=self.read,
                                 reference_check=_reject_reference)
        # All current artifacts and new sessions are protection sources. A new
        # source never appends anything to changes, ids or the child manifest.
        for name, tree in self.trees.items():
            if name in {'brain', 'annotations', 'presence'}:
                native._protect_reference(root, tree, ids, reader=self.read)
        mirrors = {}
        required = {c['before']['path'] for c in e['rewrites']}
        if e['cache']:
            required.add(native.CACHE)
        paths = {p['path'] for tree in self.trees.values() for p in tree['files']}
        if self.link and self.link['target'].replace('\\', '/') not in paths:
            native.fail('current_log_link_target_unqualified')
        if not required <= paths:
            native.fail('current_restore_source_missing')
        for path in (restore.PROTO, restore.METADATA, native.CACHE):
            if path not in paths:
                continue
            proof, raw = self.read(root, path, content=True, limit=ff.MAX_REWRITE_BYTES)
            _reject_reference(raw, ids)
            if path == restore.PROTO:
                restore.proto_after(raw, ids, raw_hashes)
                members = sorted(restore.proto_entries(raw))
            elif path == restore.METADATA:
                restore.metadata_after(raw, ids, raw_hashes)
                records = native._json(raw)['conversations']
                for record in records.values():
                    summary = record['summary']
                    text_fields = {'ID', 'Title', 'Preview', 'UpdatedAt', 'AppDataDir',
                                   'ProjectID', 'AgentName', 'GroupID'}
                    cloud = summary['CloudProject']
                    if (any(type(summary[k]) is not str for k in text_fields)
                            or any(type(summary[k]) is not bool for k in ('Loaded', 'Internal', 'Cloud'))
                            or any(type(summary[k]) is not int or summary[k] < 0 for k in ('NumSteps', 'PermissionPreset'))
                            or type(record['is_internal']) is not bool or type(record['last_modified_time']) is not str
                            or not isinstance(summary['WorkspaceURIs'], list)
                            or any(type(v) is not str for v in summary['WorkspaceURIs'])
                            or not isinstance(cloud, dict) or set(cloud) != {'ID', 'Name'}
                            or any(type(v) is not str for v in cloud.values()) or cloud['ID']):
                        native.fail('current_metadata_fields_unqualified')
                members = sorted(records)
            else:
                native.cache_after(raw, ids)
                members = sorted(set(native._json(raw).values()))
                if not set(members) <= present:
                    native.fail('current_resume_owner_unproven')
            mirrors[path] = {'sha256': proof['sha256'], 'ids': members}
        classifications = []
        blockers = []
        original = {p['path']: p for p in e['preserved']}
        # Original non-diagnostic files/trees must remain byte-for-byte, with
        # their complete membership. Only the exact boolean onboarding schema
        # is qualified for evolution. No implicit/cache name exemption exists.
        for path, prior in original.items():
            current = ff.freeze_remove(root, path, reader=self.read) if (root/path).exists() else None
            category = 'unchanged' if current == prior else 'unqualified_change'
            if path in {'log', 'crashes'}:
                category = 'diagnostic_namespace'
            elif path == 'cache/onboarding.json' and current is not None:
                value = native._json(self.read(root, path, content=True, limit=4096)[1])
                fields = {'consumerOnboardingComplete', 'enterpriseOnboardingComplete', 'onboardingComplete'}
                if not isinstance(value, dict) or set(value) != fields or any(type(v) is not bool for v in value.values()):
                    native.fail('current_onboarding_unqualified')
                category = 'qualified_boolean_onboarding' if current != prior else 'unchanged'
            if category == 'unqualified_change':
                blockers.append({'path': path, 'blocker_code': 'antigravity_cli_current_preserved_source_changed'})
            classifications.append({'path': path, 'classification': category,
                'original': prior, 'current': current})
        known_cache = {p.split('/')[1] for p in original if p.startswith('cache/')}
        known_cache |= {'last_conversations.json', 'conversation_metadata.json'}
        if set(directories[2].get('entries', ())) - known_cache:
            native.fail('current_cache_source_unqualified')
        # New top-level sources other than qualified owned/summary families and
        # diagnostics are unqualified, even when a byte scan has no ID match.
        if set(self.names) - set(e['root_entries']) - {'cli.log', 'log', 'crashes',
                *(native.SUMMARY+s for s in native.SIDECARS)}:
            native.fail('current_source_unqualified')
        for name, tree in self.trees.items():
            if name in {'log', 'crashes', 'builtin', 'skills', 'conversations', native.SUMMARY,
                        *(native.SUMMARY+s for s in native.SIDECARS[1:])}:
                continue
            for proof in tree['files']:
                _reject_reference(self.read(root, proof['path'], content=True)[1], ids)
        result = {'schema_version': 'lam.antigravity-cli-current.v1',
            'original_verified_after_sha256': e['summary_after'], 'logical_sha256': summary_hash,
            'summary_schema_sha256': e['summary_schema_sha256'], 'fts_ids': fts_ids,
            'target_ids': sorted(ids), 'preserved_summary_ids': sorted(preserved),
            'current_summary_ids': sorted(present), 'new_summary_ids': sorted(present-set(e['parents'])),
            'unchanged_raw_summary_ids': sorted(s for s in preserved if raw_hashes[s] == e['summary_raw_hashes'][s]),
            'changed_raw_summary_ids': sorted(s for s in preserved if raw_hashes[s] != e['summary_raw_hashes'][s]),
            'current_raw_summary_hashes': raw_hashes, 'mirrors': mirrors,
            'original_log_link': e['log_link'], 'current_log_link': self.link,
            'current_namespace': self.trees, 'differences': classifications, 'blockers': blockers,
            'basis': 'exact_completed_children_and_current_qualified_sources',
            'continued_data_byte_identity_claimed': False}
        self.stable()
        return result


@contextmanager
def hold_current(evidence, held):
    current = CurrentNative(evidence, held)
    if ff._identity(current.root.stat()) != evidence['root_identity']:
        native.fail('root_replaced')
    if current.link is not None:
        # OPEN_REPARSE_POINT leases the link itself. Never follow its target.
        fd = exclusive_fd(current.root/'cli.log')
        held.stack.callback(os.close, fd)
        if native._log_link(current.root) != current.link:
            native.fail('log_link_changed')
    for name in current.names:
        if name == 'cli.log':
            continue
        current.trees[name] = ff.freeze_remove(current.root, name)
    proofs = [p for tree in current.trees.values() for p in tree['files']]
    parents = {str(parent) for proof in proofs for parent in (current.root/proof['path']).parents}
    ensure_descriptor_capacity(len(proofs)+len(parents)+len(held.handles)+len(held.parents)+256)
    for proof in proofs:
        held.acquire(current.root, proof)
    current.stable()
    yield current
    current.stable()
