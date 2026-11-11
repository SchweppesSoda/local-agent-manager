"""AI-reviewed runtime retirement with exact, recheckable local evidence.

The reviewer establishes ownership, selection, consumers and rollback policy.
This module bounds the selectable units and binds that assessment to files.
It never infers that the highest version is selected or that age proves disuse.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import re
import stat
import struct

from . import frozen_files as ff
from .discovery import release_version
from .operation_store import plan_sha256
from .path_identity import canonical_existing_path_key
from .windows_held_files import exclusive_fd
from .orca_target_safety import _windows_process_snapshot

MAX_RUNTIME_BYTES = 4 * 1024 * 1024 * 1024


def _pointer(path):
    info = path.lstat()
    if getattr(info, 'st_reparse_tag', None) not in {0xA0000003, 0xA000000C} and not stat.S_ISLNK(info.st_mode):
        raise ValueError('runtime_reference_pointer_unknown')
    target_text = os.readlink(path)
    direct = target_text
    if os.name == 'nt' and direct.startswith('\\\\?\\'):
        direct = direct[4:]
    target = Path(os.path.abspath(path.parent / direct))
    # Only a direct ordinary target is qualified. Chained selectors would
    # require a separate lease on every intermediate reparse point.
    ff._plain(ff.checked_path(target.parent, target.name).lstat(), directory=target.is_dir())
    return {'identity': ff._identity(info), 'mtime_ns': info.st_mtime_ns,
            'target': target_text, 'directory': bool(getattr(info, 'st_file_attributes', 0) & 0x10)
            or stat.S_ISDIR(info.st_mode)}


def _names(root, relative):
    path = ff.checked_path(root, relative)
    try:
        ff._plain(path.lstat(), directory=True)
    except FileNotFoundError:
        return []
    names = []
    for entry in path.iterdir():
        names.append(entry.name)
        if len(names) > ff.MAX_ENTRIES:
            raise ValueError('runtime_inventory_budget')
    return sorted(names)


def _snapshot(root, relative):
    """Metadata only. Hash file contents when accepting a reviewed plan."""
    files, directories, pending = [], [], [relative]
    while pending:
        name = pending.pop()
        path = ff.checked_path(root, name)
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        ff._plain(info, directory=directory)
        if directory:
            entries = _names(root, name)
            directories.append({'path': name, 'identity': ff._identity(info), 'entries': entries})
            pending.extend(name + '/' + child for child in reversed(entries))
        else:
            files.append({'path': name, 'identity': ff._identity(info), 'size': info.st_size,
                          'mtime_ns': info.st_mtime_ns, 'mode': ff._mode(info)})
        if len(files) + len(directories) + len(pending) > ff.MAX_ENTRIES or name.count('/') > 128:
            raise ValueError('runtime_inventory_budget')
    return {'files': files, 'directories': directories}


def _freeze_unit(root, relative):
    snapshot = _snapshot(root, relative)
    files, total = [], 0
    for entry in snapshot['files']:
        proof, _ = ff.read_file(root, entry['path'], limit=min(ff.MAX_BYTES, MAX_RUNTIME_BYTES - total))
        files.append(proof)
        total += proof['size']
    if _snapshot(root, relative) != snapshot:
        raise ValueError('runtime_inventory_changed')
    return {'kind': 'remove', 'path': relative, 'files': files, 'directories': snapshot['directories']}


def _bases(client):
    # These are install layouts, never native conversation or recovery roots.
    if client == 'cindy':
        return [('codex-package', 'codex', ('bin/codex.exe', 'bin/codex')),
                ('codex', 'codex', ('codex.exe', 'codex')),
                ('claude-code', 'claude', ('claude.exe', 'claude')),
                ('pi', 'pi', ('pi.exe', 'pi', 'node_modules/@mariozechner/pi-coding-agent/package.json'))]
    if client == 'codex':
        return [('packages/' + role + '/releases', 'codex', ('bin/codex.exe', 'bin/codex', 'codex.exe', 'codex'))
                for role in ('standalone', 'app-server', 'app-server-daemon')] + [
                    ('.sandbox-bin', 'codex-command-runner', ())]
    if client == 'claude':
        return [('versions', 'claude', ('claude.exe', 'claude'))]
    if client == 'codex-desktop':
        return [('bin', 'codex', ('codex.exe', 'codex'))]
    if client == 't3code':
        return [('tools/' + engine + '/' + platform + '/versions', engine, markers)
                for engine, markers in (
                    ('codex', ('codex.exe', 'codex')),
                    ('antigravity', ('agy_acp_server.exe', 'agy_acp_server.par')))
                for platform in ('win32-x64', 'win32-arm64', 'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64')]
    return []


def inventory(client, root):
    candidates, unqualified, containers = [], [], []
    for base, engine, markers in _bases(client):
        try:
            names = _names(root, base)
            try:
                identity = ff._identity(ff.checked_path(root, base).lstat())
            except FileNotFoundError:
                identity = None
            containers.append({'path': base, 'identity': identity, 'entries': names})
        except (OSError, ValueError, RuntimeError):
            unqualified.append({'path': base, 'reason': 'runtime_container_unreadable_or_linked'})
            continue
        for name in names:
            # Installer release units append a target triple, not a SemVer
            # prerelease (x86_64 contains an underscore). Keep generic version
            # parsing strict; qualify this install layout locally.
            target_release = client == 'codex' and re.fullmatch(
                r'(.+)-(?:x86_64|aarch64)-(?:pc-windows-msvc|unknown-linux-(?:gnu|musl)|apple-darwin)', name)
            helper = client == 'codex' and base == '.sandbox-bin' and re.fullmatch(
                r'codex-command-runner-(.+)\.exe', name)
            if not (release_version(name) or re.fullmatch(r'[0-9a-f]{16,64}', name)
                    or target_release and release_version(target_release[1])
                    or helper and release_version(helper[1])
                    or engine == 'pi' and re.fullmatch(r'\d+\.\d+\.\d+-[0-9a-f-]{36}', name)):
                if client == 'codex':
                    unqualified.append({'path': base + '/' + name, 'reason': 'runtime_unit_name_unrecognized'})
                continue
            relative = base + '/' + name
            try:
                path = ff.checked_path(root, relative)
                info = path.lstat()
                if (client == 'claude' or helper) and stat.S_ISREG(info.st_mode):
                    present = [relative]
                else:
                    present = []
                    for marker in markers:
                        candidate = relative + '/' + marker
                        try:
                            ff._plain(ff.checked_path(root, candidate).lstat())
                            present.append(candidate)
                        except FileNotFoundError:
                            pass
                if not present:
                    raise ValueError('runtime_executable_marker_missing')
                snapshot = _snapshot(root, relative)
                if (sum(item['size'] for item in snapshot['files']) > MAX_RUNTIME_BYTES
                        or any(item['size'] > ff.MAX_BYTES for item in snapshot['files'])):
                    raise ValueError('runtime_inventory_byte_budget')
                # Check the namespace again before publishing a review token.
                if snapshot != _snapshot(root, relative):
                    raise ValueError('runtime_inventory_changed')
                candidates.append({'path': relative, 'engine': engine, 'markers': present,
                                   'snapshot_sha256': plan_sha256(snapshot),
                                   'file_count': len(snapshot['files']),
                                   'logical_bytes': sum(item['size'] for item in snapshot['files'])})
            except (OSError, ValueError, RuntimeError):
                unqualified.append({'path': relative, 'reason': 'runtime_unit_unreadable_linked_or_unknown'})
    check_containers(root, containers)
    return {'candidates': candidates, 'unqualified': unqualified, 'containers': containers,
            'coverage': 'recognized_versioned_install_units',
            'selection_and_consumers': 'requires_reviewer_assessment'}


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 8000:
        raise ValueError('runtime_review_reason_required')


def _writer_image(root, relative, proof):
    path = ff.checked_path(root, relative)
    if path.suffix.casefold() != '.exe':
        raise ValueError('runtime_writer_requires_native_windows_executable')
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError('runtime_writer_requires_native_windows_executable')
        offset = struct.unpack_from('<I', header, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise ValueError('runtime_writer_requires_native_windows_executable')
        stream.seek(offset)
        if stream.read(4) != b'PE\x00\x00':
            raise ValueError('runtime_writer_requires_native_windows_executable')
    current, _ = ff.read_file(root, relative)
    if current != proof:
        raise ValueError('runtime_writer_image_changed')


def prepare(client, root, observed, review, *, freeze):
    """Validate decisions; retain full assessment and exact dependency evidence."""
    if (not isinstance(review, dict) or set(review) != {
            'schema_version', 'client', 'root', 'assessment', 'remove', 'preserve', 'references'}
            or review['schema_version'] != 'lam.runtime-review.v1'
            or review['client'] != client or review['root'] != str(root)):
        raise ValueError('runtime_review_scope_invalid')
    assessment = review['assessment']
    if not isinstance(assessment, dict) or set(assessment) != {'ownership', 'selection', 'consumers', 'rollback'}:
        raise ValueError('runtime_review_assessment_required')
    for reason in assessment.values():
        _text(reason)
    candidates = {item['path']: item for item in observed['candidates']}
    selected, retained = [], []
    seen = set()
    for key, destination in (('remove', selected), ('preserve', retained)):
        if not isinstance(review[key], list) or len(review[key]) > ff.MAX_ENTRIES:
            raise ValueError('runtime_review_decisions_invalid')
        for decision in review[key]:
            if not isinstance(decision, dict) or set(decision) != {'path', 'snapshot_sha256', 'reason'}:
                raise ValueError('runtime_review_decision_invalid')
            _text(decision['reason'])
            relative = decision['path']
            if not isinstance(relative, str) or relative not in candidates or relative in seen:
                raise ValueError('runtime_review_candidate_invalid')
            if decision['snapshot_sha256'] != candidates[relative]['snapshot_sha256']:
                raise ValueError('runtime_review_candidate_changed')
            seen.add(relative)
            destination.append(candidates[relative])
    if not selected or seen != set(candidates):
        raise ValueError('runtime_review_classify_all_candidates')
    if {item['engine'] for item in selected} - {item['engine'] for item in retained}:
        raise ValueError('runtime_review_retained_replacement_required')
    references = review['references']
    if not isinstance(references, list) or not 1 <= len(references) <= 128:
        raise ValueError('runtime_review_reference_evidence_required')
    sources = []
    source_keys = set()
    for reference in references:
        if (not isinstance(reference, dict) or set(reference) - {'root', 'path', 'reason', 'writer'}
                or not {'root', 'path', 'reason'} <= set(reference)
                or type(reference.get('writer', False)) is not bool):
            raise ValueError('runtime_review_reference_invalid')
        _text(reference['reason'])
        source_root = Path(reference['root'])
        path = ff.checked_path(source_root, reference['path'])
        source_key = os.path.normcase(str(path))
        if source_key in source_keys:
            raise ValueError('runtime_review_duplicate_reference')
        source_keys.add(source_key)
        for item in selected:
            selected_path = root / item['path']
            if path == selected_path or selected_path in path.parents:
                raise ValueError('runtime_review_reference_overlaps_removal')
        # A selector can be a metadata file, direct installer link or absent slot.
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        proof, pointer = None, None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                pointer = _pointer(path)
                target = Path(canonical_existing_path_key(path))
                for item in selected:
                    selected_path = Path(canonical_existing_path_key(root / item['path']))
                    if target == selected_path or selected_path in target.parents:
                        raise ValueError('runtime_review_selected_pointer_target')
            else:
                proof, _ = ff.read_file(source_root, reference['path'])
        writer = reference.get('writer', False)
        if writer:
            if proof is None:
                raise ValueError('runtime_writer_requires_native_windows_executable')
            _writer_image(source_root, reference['path'], proof)
        sources.append({'root': str(source_root), 'root_identity': ff._identity(source_root.lstat()),
                        'path': reference['path'], 'file': proof, 'pointer': pointer, 'writer': writer})
    if any(source['file'] is None and source['pointer'] is None for source in sources) and not any(
            source['writer'] for source in sources):
        raise ValueError('runtime_absent_reference_requires_writer_exclusion')
    result = {'review': review, 'preserved': [], 'references': sources, 'containers': observed['containers']}
    changes = []
    if freeze:
        for group, destination in ((selected, changes), (retained, result['preserved'])):
            for item in group:
                evidence = _freeze_unit(root, item['path'])
                if plan_sha256(_snapshot(root, item['path'])) != item['snapshot_sha256']:
                    raise ValueError('runtime_review_candidate_changed')
                destination.append(evidence)
    return result, changes


def dependencies(root, evidence):
    for unit in evidence['preserved']:
        for proof in unit['files']:
            yield root, proof
    for source in evidence['references']:
        if source['file'] is not None:
            yield Path(source['root']), source['file']


def check_absences(evidence):
    for source in evidence['references']:
        root = Path(source['root'])
        if ff._identity(root.lstat()) != source['root_identity']:
            raise ValueError('runtime_reference_root_changed')
        path = ff.checked_path(root, source['path'])
        if source['pointer'] is not None:
            if _pointer(path) != source['pointer']:
                raise ValueError('runtime_reference_pointer_changed')
        elif source['file'] is None:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            raise ValueError('runtime_previously_absent_reference_appeared')


@contextmanager
def hold_pointers(evidence):
    with ExitStack() as stack:
        for source in evidence['references']:
            if source['pointer'] is None:
                continue
            path = stack.enter_context(ff._parent_fence(Path(source['root']), source['path']))
            fd = exclusive_fd(path, directory=source['pointer']['directory'])
            stack.callback(os.close, fd)
            if (ff._identity(os.fstat(fd)) != source['pointer']['identity']
                    or _pointer(path) != source['pointer']):
                raise ValueError('runtime_reference_pointer_changed')
        yield


def check_directories(root, units):
    for unit in units:
        for directory in unit['directories']:
            path = ff.checked_path(root, directory['path'])
            if (ff._identity(path.lstat()) != directory['identity']
                    or _names(root, directory['path']) != directory['entries']):
                raise ValueError('runtime_preserved_directory_changed')


def check_containers(root, containers):
    for container in containers:
        path = ff.checked_path(root, container['path'])
        try:
            identity = ff._identity(path.lstat())
        except FileNotFoundError:
            identity = None
        if identity != container['identity'] or _names(root, container['path']) != container['entries']:
            raise ValueError('runtime_version_container_changed')


def check_writers(evidence):
    names = {Path(source['path']).name.casefold() for source in evidence['references'] if source['writer']}
    if not names:
        return
    try:
        processes = _windows_process_snapshot()
    except (OSError, ValueError, RuntimeError):
        raise ValueError('runtime_reference_process_probe_unavailable') from None
    if any(item['name'].casefold() in names for item in processes):
        raise ValueError('runtime_reference_writer_still_running')


def check_removed_directories(root, units):
    for unit in units:
        directory_paths = {directory['path'] for directory in unit['directories']}
        for directory in unit['directories']:
            path = ff.checked_path(root, directory['path'])
            names = set(_names(root, directory['path']))
            required = {name for name in directory['entries'] if directory['path'] + '/' + name in directory_paths}
            if (ff._identity(path.lstat()) != directory['identity']
                    or names - set(directory['entries']) or required - names):
                raise ValueError('runtime_removed_unit_namespace_changed')


def verify(root, evidence):
    check_absences(evidence)
    check_containers(root, evidence['containers'])
    for source_root, proof in dependencies(root, evidence):
        if ff.read_file(source_root, proof['path'])[0] != proof:
            raise ValueError('runtime_dependency_changed')
    check_directories(root, evidence['preserved'])
