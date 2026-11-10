"""Qualified disposable artifacts, separate from conversation deletion authority."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import base64
import json
import os
from pathlib import Path
import re
import stat
import struct
import sqlite3
import sys
from itertools import islice
import time

from local_agent_manager import frozen_files as ff
from local_agent_manager import runtime_artifacts
from local_agent_manager.discovery import CINDY_PROFILE_NAMES, default_appdata, default_codex_home, default_local_appdata

CLIENTS = ('cindy', 'codex', 'codex-desktop', 'claude', 'pi', 't3code', 'paseo',
           'antigravity-cli', 'antigravity-desktop', 'workbuddy', 'orca', 'herdr', 'qwenwork')
CATEGORIES = ('caches', 'logs', 'backups', 'downloads', 'runtimes')
DESKTOP = frozenset(CLIENTS) - {'codex', 'claude', 'pi', 'antigravity-cli'}
_LOG = re.compile(r'(?:agent-\d{4}-\d{2}-\d{2}\.ndjson|[A-Za-z][A-Za-z0-9_-]*\.log\.\d+|[A-Za-z][A-Za-z0-9_-]*[.-]\d{4}-\d{2}-\d{2}\.log|\d{4}-\d{2}-\d{2}\.log)\Z')
_CINDY_BACKUP = re.compile(r'(cindy-[A-Za-z0-9_-]+\.db)\.bak\.(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z)\Z')
_CODEX_TEMP = re.compile(r'\.\.codex-global-state\.json\.bak\.tmp-(\d{13})-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')


def root_path(value):
    root = Path(value).expanduser()
    ff.checked_path(root, '.maintenance-path-check')
    return root


def default_profiles(clients, *, include_runtime_roots=False):
    """Known candidates and environment roots remain observations until qualified."""
    home, roaming, local = Path.home(), default_appdata(), default_local_appdata()
    if os.name != 'nt':
        roaming = home / 'Library/Application Support' if sys.platform == 'darwin' else Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
    roots = {
        'cindy': [roaming / name for name in CINDY_PROFILE_NAMES],
        'codex': [home / '.codex', default_codex_home()],
        'codex-desktop': [roaming / 'Codex'],
        'claude': [Path(os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude'))],
        'pi': [Path(os.environ.get('PI_CODING_AGENT_DIR', home / '.pi/agent'))],
        't3code': [roaming / 'T3 Code', roaming / 't3code-v2', Path(os.environ.get('T3CODE_HOME', home / '.t3')),
                   *([local / 't3code-updater'] if os.name == 'nt' else [])],
        'paseo': [roaming / 'Paseo', Path(os.environ.get('PASEO_HOME', home / '.paseo'))],
        'antigravity-cli': [home / '.gemini/antigravity-cli'],
        'antigravity-desktop': [roaming / 'Antigravity'],
        'workbuddy': [Path(os.environ.get('WORKBUDDY_CONFIG_DIR', home / '.workbuddy')), roaming / 'WorkBuddy'],
        'orca': [Path(os.environ.get('ORCA_USER_DATA_PATH', roaming / 'orca'))],
        'herdr': [roaming / 'herdr', roaming / 'herdr-dev'],
        'qwenwork': [roaming / name for name in ('QwenWorkCN', 'QwenWorkCNDev', 'QwenWorkCNCanary')],
    }
    if include_runtime_roots:
        roots['claude'].append(home / '.local/share/claude')
        roots['codex-desktop'].extend((local / 'OpenAI/Codex', local / 'Programs/OpenAI/Codex'))
    if 'orca' in clients:
        from local_agent_manager.orca_discovery import default_orca_root
        roots['orca'] = [default_orca_root()]
    if 'herdr' in clients:
        from local_agent_manager.herdr_discovery import default_herdr_roots
        roots['herdr'] = list(default_herdr_roots())
    if 'codex-desktop' in clients and os.name == 'nt':
        packages = local / 'Packages'
        if packages.is_dir():
            # Report a budget failure rather than silently declare full coverage.
            ff._plain(packages.lstat(), directory=True)
            names = list(islice(packages.iterdir(), ff.MAX_ENTRIES + 1))
            if len(names) > ff.MAX_ENTRIES:
                raise ff.FrozenFilesError('maintenance_package_discovery_budget')
            roots['codex-desktop'].extend(p / 'LocalCache/Roaming/Codex' for p in names
                                          if p.name.casefold().startswith('openai.codex_'))
    return [{'client': client, 'root': str(path)} for client in clients
            for path in dict.fromkeys(roots[client])]


def _names(root, relative):
    path = ff.checked_path(root, relative)
    try:
        ff._plain(path.lstat(), directory=True)
    except FileNotFoundError:
        return []
    entries = []
    for item in path.iterdir():
        entries.append(item.name)
        if len(entries) > ff.MAX_ENTRIES:
            raise ff.FrozenFilesError('maintenance_entry_budget')
    return sorted(entries)


def _prefix(root, relative, length=8):
    path = ff.checked_path(root, relative)
    before = path.lstat()
    ff._plain(before)
    with path.open('rb') as handle:
        if ff._revision(os.fstat(handle.fileno())) != ff._revision(before):
            raise ff.FrozenFilesError('maintenance_file_changed')
        raw = handle.read(length)
    if ff._revision(path.lstat()) != ff._revision(before):
        raise ff.FrozenFilesError('maintenance_file_changed')
    return raw


def _metadata(root, relative):
    """Bounded metadata accounting. Redirects and hard links are never savings."""
    pending, files, visited, total = [relative], 0, 0, 0
    while pending:
        name = pending.pop()
        visited += 1
        path = ff.checked_path(root, name)
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        ff._plain(info, directory=directory)
        if directory:
            pending.extend(name + '/' + child for child in _names(root, name))
        else:
            files += 1
            total += info.st_size
        if visited + len(pending) > ff.MAX_ENTRIES or name.count('/') > 128:
            raise ff.FrozenFilesError('maintenance_entry_budget')
    return {'file_count': files, 'logical_bytes': total, 'allocated_bytes': None}


def _row(root, path, category, decision, reason):
    row = {'path': path, 'category': category, 'decision': decision, 'reason': reason}
    try:
        row.update(_metadata(root, path))
    except (OSError, ValueError, RuntimeError):
        row.update(decision='unqualified', reason='unreadable_linked_or_budgeted', logical_bytes=None,
                   allocated_bytes=None, file_count=None)
    return row


def _cache(root, relative):
    # Chromium blockfile disk_format.h. Unknown formats stay outside approval.
    names = _names(root, relative)
    if not names:
        return False
    if 'index' not in names or any(not re.fullmatch(r'(?:index|data_\d+|f_[0-9a-f]{6,})', name) for name in names):
        raise ff.FrozenFilesError('maintenance_cache_format_unknown')
    proof, body = ff.read_file(root, relative + '/index', content=True, limit=ff.MAX_REWRITE_BYTES)
    raw = body[:8]
    if len(raw) != 8 or struct.unpack('<II', raw) not in {
        (0xC103CAC3, 0x20000), (0xC103CAC3, 0x20001), (0xC103CAC3, 0x30000),
    }:
        raise ff.FrozenFilesError('maintenance_cache_format_unknown')
    for name in names:
        ff._plain(ff.checked_path(root, relative + '/' + name).lstat())
    return proof


def _json_proof(root, relative):
    proof, raw = ff.read_file(root, relative, content=True, limit=ff.MAX_REWRITE_BYTES)
    if not isinstance(_json(raw), dict):
        raise ff.FrozenFilesError('maintenance_json_format_unknown')
    return proof


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('maintenance_duplicate_json_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _diagnostic(root, relative, client):
    raw = _prefix(root, relative, 64 * 1024)
    if not raw:
        return False
    first = raw.splitlines()[0]
    if client == 'cindy' and Path(relative).name.startswith('agent-') and relative.endswith('.ndjson'):
        value = _json(first)
        return (isinstance(value, dict)
                and {'level', 'msg', 'scope', 'seq', 'sessionId', 'source', 'ts', 'tz'} <= value.keys()
                and isinstance(value['level'], str) and value['level'] in {'trace', 'debug', 'info', 'warn', 'error', 'fatal'}
                and type(value['seq']) is int
                and all(isinstance(value[key], str) for key in ('msg', 'scope', 'sessionId', 'source'))
                and type(value['ts']) is int and value['ts'] > 0
                and type(value['tz']) is int and -1440 <= value['tz'] <= 1440)
    # Common electron-log/tracing diagnostic headers. An extension alone
    # never qualifies a transcript or an arbitrary user file for removal.
    return bool(re.match(rb'\[?\d{4}-\d{2}-\d{2}[ T][0-9:.+Z-]+\]?\s+\[?(?:TRACE|DEBUG|INFO|WARN|ERROR|trace|debug|info|warn|error)\]?(?:\s|:)', first))


def _migration_proof(root, source):
    proof, raw = ff.read_file(root, source + '.migration-runtime.json', content=True, limit=ff.MAX_REWRITE_BYTES)
    value = _json(raw)
    if (not isinstance(value, dict) or set(value) != {'version', 'legacyBaselineVersion', 'migrations'}
            or value['version'] != 1 or type(value['legacyBaselineVersion']) is not int
            or not isinstance(value['migrations'], list) or not value['migrations']):
        raise ff.FrozenFilesError('maintenance_migration_format_unknown')
    for item in value['migrations']:
        if (not isinstance(item, dict) or set(item) != {'fileName', 'scriptHash', 'seq', 'sqlHash'}
                or type(item['seq']) is not int or not isinstance(item['fileName'], str)
                or not all((key == 'scriptHash' and item[key] is None)
                           or isinstance(item[key], str) and re.fullmatch('[0-9a-f]{64}', item[key])
                           for key in ('scriptHash', 'sqlHash'))):
            raise ff.FrozenFilesError('maintenance_migration_format_unknown')
    return proof


def _updater(root):
    """electron-updater temp files are discarded before each download attempt."""
    proof, raw = ff.read_file(root, 'pending/update-info.json', content=True, limit=1024 * 1024)
    data = _json(raw)
    if (not isinstance(data, dict) or set(data) - {'fileName', 'sha512', 'isAdminRightsRequired',
            'packageFileName', 'version', 'installOnNextLaunch'} or not {'fileName', 'sha512'} <= data.keys()
            or not isinstance(data['fileName'], str) or not re.fullmatch(r'[A-Za-z0-9_. ()+-]+\.exe', data['fileName'])
            or not isinstance(data['sha512'], str) or len(base64.b64decode(data['sha512'], validate=True)) != 64):
        raise ff.FrozenFilesError('maintenance_updater_metadata_unknown')
    for key in ('isAdminRightsRequired', 'installOnNextLaunch'):
        if key in data and type(data[key]) is not bool:
            raise ff.FrozenFilesError('maintenance_updater_metadata_unknown')
    proofs = [proof, ff.read_file(root, 'pending/' + data['fileName'])[0]]
    for relative in ('installer.exe', 'current.blockmap', 'pending/current.blockmap'):
        if (root / relative).exists():
            proofs.append(ff.read_file(root, relative)[0])
    if data.get('packageFileName') is not None:
        package = data['packageFileName']
        if not isinstance(package, str) or not re.fullmatch(r'[A-Za-z0-9_. ()+-]+', package):
            raise ff.FrozenFilesError('maintenance_updater_metadata_unknown')
        proofs.append(ff.read_file(root, 'pending/' + package)[0])
    pattern = re.compile(r'(?:[0-2]-)?temp-' + re.escape(data['fileName']) + r'\Z')
    return pattern, proofs


def _database(root, relative):
    # A private snapshot prevents a read from initializing the user's WAL SHM.
    from local_agent_manager.antigravity_cli_cleanup import private_database
    with private_database(root, relative) as (db, proofs, _):
        if [tuple(row) for row in db.execute('PRAGMA quick_check')] != [('ok',)]:
            raise ff.FrozenFilesError('maintenance_replacement_database_unhealthy')
        schema = [tuple(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        if not schema:
            raise ff.FrozenFilesError('maintenance_database_schema_unknown')
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(), proofs


def _sidecar_absences(root, relative, proofs):
    known = {proof['path']: proof for proof in proofs}
    absent = []
    for suffix in ('-wal', '-shm', '-journal'):
        name = relative + suffix
        try:
            current, _ = ff.read_file(root, name)
        except FileNotFoundError:
            if name in known:
                raise ff.FrozenFilesError('maintenance_sidecar_changed')
            absent.append(name)
        else:
            if current != known.get(name):
                raise ff.FrozenFilesError('maintenance_sidecar_changed')
    return absent


def _generation_ns(stamp):
    try:
        value = datetime.strptime(stamp, '%Y-%m-%dT%H-%M-%S-%fZ').replace(tzinfo=timezone.utc)
        epoch = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (epoch.days * 86400 + epoch.seconds) * 1000000000 + epoch.microseconds * 1000
    except ValueError:
        return None


def _past_log_date(name, cutoff_ns):
    cutoff = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=cutoff_ns // 1000)).date()
    try:
        return all(datetime.strptime(day, '%Y-%m-%d').date() < cutoff
                   for day in re.findall(r'\d{4}-\d{2}-\d{2}', name))
    except ValueError:
        return False


def inspect_profile(client, value, *, categories=CATEGORIES, cutoff_ns=0, freeze=False, runtime_review=None):
    """Classify only named product artifacts. Bodies never enter public reports."""
    if client not in CLIENTS or not categories or set(categories) - set(CATEGORIES):
        raise ValueError('maintenance_scope_invalid')
    root = root_path(value)
    observations, changes, dependencies, absent_paths = [], [], {}, set()
    if runtime_review is not None and 'runtimes' not in categories:
        raise ValueError('runtime_review_requires_runtime_category')
    runtimes, runtime_evidence = None, None

    def candidate(relative, category, reason, *, proofs=(), expected_files=(), absent=()):
        row = _row(root, relative, category, 'eligible_for_plan', reason)
        if row['decision'] == 'eligible_for_plan' and freeze:
            change = ff.freeze_remove(root, relative)
            frozen = {proof['path']: proof for proof in change['files']}
            if any(frozen.get(proof['path']) != proof for proof in expected_files):
                raise ff.FrozenFilesError('maintenance_qualification_changed')
            for proof in proofs:
                previous = dependencies.get(proof['path'])
                if previous is not None and previous != proof:
                    raise ff.FrozenFilesError('maintenance_dependency_changed')
            dependencies.update((proof['path'], proof) for proof in proofs)
            changes.append({'category': category, 'evidence': change})
            absent_paths.update(absent)
        observations.append(row)

    if client in DESKTOP and 'caches' in categories:
        http = 'Cache/Cache_Data' if (root / 'Cache/Cache_Data').exists() else 'Cache'
        for relative in (http, 'GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache', 'GrShaderCache', 'ShaderCache'):
            path = ff.checked_path(root, relative)
            if not path.exists():
                continue
            try:
                index = _cache(root, relative)
                if index:
                    candidate(relative, 'caches', 'qualified_chromium_blockfile_cache', expected_files=[index])
            except (OSError, ValueError, RuntimeError):
                observations.append(_row(root, relative, 'caches', 'unqualified', 'cache_format_or_access_unverified'))

    if 'logs' in categories:
        for directory in ('logs', 'log', 'userdata/logs'):
            for name in _names(root, directory):
                relative = directory + '/' + name
                info = ff.checked_path(root, relative).lstat()
                if stat.S_ISDIR(info.st_mode):
                    # Session/terminal transcripts retain their owning record authority.
                    observations.append(_row(root, relative, 'logs', 'retain', 'nested_or_session_diagnostics'))
                elif _LOG.fullmatch(name) and info.st_mtime_ns < cutoff_ns and _past_log_date(name, cutoff_ns):
                    try:
                        if _diagnostic(root, relative, client):
                            candidate(relative, 'logs', 'closed_rotated_diagnostics_past_explicit_retention')
                        else:
                            observations.append(_row(root, relative, 'logs', 'unqualified', 'diagnostic_format_unverified'))
                    except (OSError, ValueError, RuntimeError):
                        observations.append(_row(root, relative, 'logs', 'unqualified', 'diagnostic_format_or_access_unverified'))
                else:
                    observations.append(_row(root, relative, 'logs', 'retain', 'current_recent_or_unrecognized_log'))

    if 'backups' in categories:
        # Keep two newest generations per source, the live DB and all current slots.
        names = sorted(item.name for item in islice(root.iterdir(), ff.MAX_ENTRIES + 1))
        if len(names) > ff.MAX_ENTRIES:
            raise ff.FrozenFilesError('maintenance_entry_budget')
        groups = {}
        for name in names:
            match = _CINDY_BACKUP.fullmatch(name) if client == 'cindy' else None
            if match:
                groups.setdefault(match[1], []).append(name)
            elif client == 'codex' and _CODEX_TEMP.fullmatch(name):
                generation = int(_CODEX_TEMP.fullmatch(name)[1]) * 1000000
                if generation >= cutoff_ns or (root / name).lstat().st_mtime_ns >= cutoff_ns:
                    observations.append(_row(root, name, 'backups', 'retain', 'recent_temporary_generation'))
                    continue
                try:
                    proofs = [_json_proof(root, '.codex-global-state.json'),
                              _json_proof(root, '.codex-global-state.json.bak')]
                    target_proof = _json_proof(root, name)
                    candidate(name, 'backups', 'orphan_atomic_fallback_generation', proofs=proofs, expected_files=[target_proof])
                except (OSError, ValueError, RuntimeError):
                    observations.append(_row(root, name, 'backups', 'unqualified', 'valid_current_and_fallback_required'))
            elif 'backup' in name.casefold() or '.bak' in name.casefold():
                observations.append(_row(root, name, 'backups', 'retain', 'current_slot_recovery_or_unknown_producer'))
        for source, backups in groups.items():
            for index, name in enumerate(sorted(backups, reverse=True)):
                generation = _generation_ns(_CINDY_BACKUP.fullmatch(name)[2])
                if generation is None or generation >= cutoff_ns or index < 2 or (root / name).lstat().st_mtime_ns >= cutoff_ns:
                    observations.append(_row(root, name, 'backups', 'retain', 'invalid_or_recent_generation_or_newest_two'))
                    continue
                try:
                    migration = _migration_proof(root, source)
                    signature, proofs = _database(root, source)
                    absent = _sidecar_absences(root, source, proofs)
                    proofs.append(migration)
                    backup_signature, target_proofs = _database(root, name)
                    absent.extend(_sidecar_absences(root, name, target_proofs))
                    if signature != backup_signature or len(target_proofs) != 1:
                        raise ff.FrozenFilesError('maintenance_backup_schema_mismatch')
                    for kept in sorted(backups, reverse=True)[:2]:
                        kept_generation = _generation_ns(_CINDY_BACKUP.fullmatch(kept)[2])
                        kept_signature, kept_proofs = _database(root, kept)
                        if kept_signature != signature or kept_generation is None or kept_generation > time.time_ns():
                            raise ff.FrozenFilesError('maintenance_retained_generation_unqualified')
                        absent.extend(_sidecar_absences(root, kept, kept_proofs))
                        proofs.extend(kept_proofs)
                    candidate(name, 'backups', 'healthy_replacement_and_two_newer_generations',
                              proofs=proofs, expected_files=target_proofs, absent=absent)
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    observations.append(_row(root, name, 'backups', 'unqualified', 'backup_replacement_or_recovery_unverified'))

    if 'downloads' in categories and (root / 'pending/update-info.json').exists():
        try:
            pattern, proofs = _updater(root)
            for name in _names(root, 'pending'):
                relative = 'pending/' + name
                if pattern.fullmatch(name) and (root / relative).lstat().st_mtime_ns < cutoff_ns:
                    candidate(relative, 'downloads', 'unselected_electron_updater_temporary_download', proofs=proofs)
        except (OSError, ValueError, RuntimeError):
            observations.append(_row(root, 'pending', 'downloads', 'unqualified', 'updater_metadata_or_selection_unverified'))

    if 'runtimes' in categories:
        runtimes = runtime_artifacts.inventory(client, root)
        selected = set()
        if runtime_review is not None:
            runtime_evidence, runtime_changes = runtime_artifacts.prepare(
                client, root, runtimes, runtime_review, freeze=freeze)
            changes.extend({'category': 'runtimes', 'evidence': change} for change in runtime_changes)
            selected = {item['path'] for item in runtime_review['remove']}
        for item in runtimes['candidates']:
            observations.append({**item, 'category': 'runtimes',
                'decision': 'eligible_for_plan' if item['path'] in selected else 'retain',
                'reason': 'ai_reviewed_retirement' if item['path'] in selected else 'runtime_review_required_or_preserved'})
        observations.extend({**item, 'category': 'runtimes', 'decision': 'unqualified'}
                            for item in runtimes['unqualified'])

    for category, relatives, reason in (
        ('downloads', ('agent-binary-downloads', 'updates', 'pending', 'installer.exe', 'current.blockmap'), 'updater_selection_and_install_receipt_required'),
        ('runtimes', ('claude-code', 'codex-package', 'codex', 'pi', 'tools', 'versions', 'managed-runtime', 'browser-runtime'), 'runtime_container_preserved_unit_decisions_require_review'),
    ):
        if category in categories:
            for relative in relatives:
                if (root / relative).exists():
                    observations.append(_row(root, relative, category, 'retain', reason))
    return {'schema_version': 'lam.client-artifacts.v1', 'client': client, 'root': str(root),
            'root_identity': ff._identity(root.lstat()), 'categories': sorted(categories),
            'cutoff_ns': cutoff_ns, 'observations': observations, 'changes': changes,
            'dependencies': sorted(dependencies.values(), key=lambda item: item['path']),
            'absent_paths': sorted(absent_paths),
            'runtime_inventory': runtimes, 'runtime_evidence': runtime_evidence,
            'coverage': {'scope': 'registered_relative_artifacts', 'session_stores_scanned': False,
                         'allocated_bytes_known': False}}
