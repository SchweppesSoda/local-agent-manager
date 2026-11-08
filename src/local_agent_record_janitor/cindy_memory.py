"""Inspect one Cindy project memory store and export explicitly selected shards."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .environment_config import _reject_constant, _unique_object
from .environment_paths import BoundedPathProbe, is_redirected, local_path
from .frozen_files import FrozenFilesError, _parent_fence, read_file


_SCHEMA = "larj.cindy-memory.v1"
_SHARD_NAME = re.compile(r"(user|feedback|project|reference)_[a-z0-9_-]+\.md")
_MAX_DIRECTORY_ENTRIES = 512
_MAX_META_BYTES = 16 * 1024
_MAX_SHARD_BYTES = 64 * 1024
_MAX_SELECTED_SHARDS = 32
_MAX_SNAPSHOT_BYTES = 256 * 1024


class CindyMemoryError(RuntimeError):
    """A memory operation could not establish its requested scope or result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Binding:
    root: Path
    project: Path
    store: Path
    root_id: tuple[int, int]
    project_id: tuple[int, int]
    store_revision: tuple[int, int, int]
    metadata: dict


def _absolute(path: Path, label: str) -> Path:
    value = local_path(path)
    if value is None:
        raise CindyMemoryError("invalid_path", f"{label} requires an explicit absolute local path.")
    return value


def _directory(probe: BoundedPathProbe, path: Path, label: str) -> os.stat_result:
    status, info = probe.inspect(path, label, directory=True)
    if status == "missing":
        raise CindyMemoryError("directory_missing", f"The {label} directory is missing.")
    if status != "present" or info is None or not info.st_ino:
        raise CindyMemoryError("unsafe_directory", f"The {label} directory cannot be safely identified.")
    return info


def _folder_name(workdir: str) -> str:
    # Cindy's lossy mapping only locates a candidate. meta.json proves its scope.
    return re.sub(r"[/\\:]", "-", workdir).lstrip("-")


def _read_source(root: Path, relative: str, limit: int) -> tuple[dict, bytes]:
    try:
        evidence, data = read_file(root, relative, content=True, limit=limit)
    except FileNotFoundError:
        raise CindyMemoryError("source_missing", "A required source file is missing.") from None
    except FrozenFilesError as error:
        reason = str(error)
        if reason in ("client_file_replaced", "client_file_changed"):
            raise CindyMemoryError("source_drift", "A source file changed during the operation.") from None
        if reason == "client_file_budget_exceeded":
            raise CindyMemoryError("budget_exceeded", "A source file exceeds its read limit.") from None
        raise CindyMemoryError("unsafe_source", "A source file or ancestor is unsafe.") from None
    except OSError:
        raise CindyMemoryError("source_unreadable", "A required source file could not be read.") from None
    if data is None or not evidence["identity"][1]:
        raise CindyMemoryError("unsafe_source", "A source file cannot be safely identified.")
    return evidence, data


def _bind(maker_memory_root: Path, project: Path) -> _Binding:
    root = _absolute(maker_memory_root, "maker-memory root")
    project = _absolute(project, "project")
    if root.name != "maker-memory":
        raise CindyMemoryError("invalid_root", "Select one account's maker-memory directory.")
    folder_name = _folder_name(str(project))
    if not folder_name or len(folder_name) > 255:
        raise CindyMemoryError("invalid_path", "The project has no supported Cindy directory name.")
    store = _absolute(root / folder_name, "project store")
    probe = BoundedPathProbe()
    root_info = _directory(probe, root, "maker-memory root")
    project_info = _directory(probe, project, "project")
    store_info = _directory(probe, store, "project store")
    metadata, raw = _read_source(root, f"{folder_name}/meta.json", _MAX_META_BYTES)
    try:
        fields = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
        if not isinstance(fields, dict) or not isinstance(fields.get("absPath"), str):
            raise ValueError("absPath missing or invalid")
    except (UnicodeError, ValueError, RecursionError):
        raise CindyMemoryError("invalid_metadata", "Project meta.json has no valid absPath.") from None
    stored_scope = fields["absPath"]
    stored_path = local_path(stored_scope)
    # Keep case and path spelling significant for scope identity. Directory
    # names are lossy, and Windows can host case-sensitive directories.
    if stored_path is None or str(stored_path) != str(project) or _folder_name(stored_scope) != folder_name:
        raise CindyMemoryError("project_mismatch", "Project meta.json does not identify the selected path.")
    return _Binding(
        root, project, store,
        (root_info.st_dev, root_info.st_ino),
        (project_info.st_dev, project_info.st_ino),
        (store_info.st_dev, store_info.st_ino, store_info.st_mtime_ns),
        metadata,
    )


def _recheck_binding(binding: _Binding) -> None:
    try:
        unchanged = _bind(binding.root, binding.project) == binding
    except CindyMemoryError:
        unchanged = False
    if not unchanged:
        raise CindyMemoryError("source_drift", "The project memory binding changed during the operation.")


def _shard_type(filename: str) -> str | None:
    match = _SHARD_NAME.fullmatch(filename) if len(filename) <= 255 else None
    return match.group(1) if match else None


def _shard_metadata(probe: BoundedPathProbe, store: Path, filename: str, kind: str) -> tuple[dict, tuple]:
    path = store / filename
    status, info = probe.inspect(path, "memory shard", directory=False)
    if status == "missing":
        raise CindyMemoryError("shard_missing", "A selected memory shard is missing.")
    if status != "present" or info is None or info.st_nlink != 1 or not info.st_ino:
        raise CindyMemoryError("unsafe_shard", "A memory shard cannot be safely identified.")
    revision = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return {"filename": filename, "type": kind, "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}, revision


def _report(binding: _Binding, shards: list[dict], *, status: str) -> dict:
    return {
        "schema_version": _SCHEMA,
        "status": status,
        "project": str(binding.project),
        "store": str(binding.store),
        "shards": shards,
    }


def inspect_memory(*, maker_memory_root: Path, project: Path) -> dict:
    """Report metadata for curated candidates in one verified project store."""
    binding = _bind(maker_memory_root, project)
    try:
        with os.scandir(binding.store) as entries:
            names = sorted(entry.name for entry in islice(entries, _MAX_DIRECTORY_ENTRIES + 1))
    except OSError:
        raise CindyMemoryError("store_unreadable", "Could not list the project memory directory.") from None
    if len(names) > _MAX_DIRECTORY_ENTRIES:
        raise CindyMemoryError("budget_exceeded", "The project memory directory exceeds its entry limit.")
    rows = []
    probe = BoundedPathProbe()
    for filename in names:
        kind = _shard_type(filename)
        if kind is not None:
            row, _ = _shard_metadata(probe, binding.store, filename, kind)
            rows.append(row)
    _recheck_binding(binding)
    return _report(binding, rows, status="ok")


def _selected_shards(shards: Sequence[str]) -> list[tuple[str, str]]:
    if not 1 <= len(shards) <= _MAX_SELECTED_SHARDS:
        raise CindyMemoryError("invalid_selection", "Select between 1 and 32 explicit shard filenames.")
    selected = []
    seen = set()
    for filename in shards:
        kind = _shard_type(filename) if isinstance(filename, str) else None
        if kind is None or filename in seen:
            raise CindyMemoryError("invalid_selection", "Select distinct curated shard filenames.")
        seen.add(filename)
        selected.append((filename, kind))
    return selected


def _fenced_shard(filename: str, raw: bytes) -> str:
    try:
        source = raw.decode("utf-8")
    except UnicodeError:
        raise CindyMemoryError("invalid_utf8", "A selected shard is not valid UTF-8.") from None
    # Claude's import parser skips fenced blocks. Choose a fence longer than
    # any backtick run in the copied data so it cannot introduce a new @import.
    longest = max((len(run.group()) for run in re.finditer(r"`+", source)), default=0)
    fence = "`" * max(3, longest + 1)
    newline = "" if source.endswith("\n") else "\n"
    return f"## {filename}\n\n{fence}text\n{source}{newline}{fence}\n"


def _destination(binding: _Binding, out: Path) -> tuple[Path, tuple[int, int]]:
    out = _absolute(out, "snapshot output")
    probe = BoundedPathProbe()
    parent_info = _directory(probe, out.parent, "output parent")
    root_info = _directory(probe, binding.root, "maker-memory root")
    if (root_info.st_dev, root_info.st_ino) != binding.root_id:
        raise CindyMemoryError("source_drift", "The maker-memory root changed during the export.")
    try:
        parent_path = out.parent.resolve(strict=True)
        input_path = binding.root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CindyMemoryError("invalid_output", "The output or input directory cannot be resolved.") from None
    if parent_path.is_relative_to(input_path):
        raise CindyMemoryError("invalid_output", "The snapshot must be outside the maker-memory input tree.")
    parts = tuple(part.casefold() for part in parent_path.parts)
    if (
        out.suffix.lower() != ".md"
        or out.name.casefold() in {
            "claude.md", "claude.local.md", "agents.md", "agents.local.md",
            "agents.override.md", "memory.md", "skill.md",
        }
        or ".git" in parts
        or (".claude", "rules") in zip(parts, parts[1:])
    ):
        raise CindyMemoryError("invalid_output", "Choose a Markdown snapshot outside instruction files and rule directories.")
    status, _ = probe.inspect(out, "snapshot output")
    if status != "missing":
        raise CindyMemoryError("output_exists", "The snapshot output already exists or cannot be checked.")
    return out, (parent_info.st_dev, parent_info.st_ino)


def _remove_prepared(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        is_redirected(info)
        or not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino) != identity
    ):
        raise CindyMemoryError("output_drift", "The preparation path now names a different file.")
    path.unlink()


def _publish(binding: _Binding, out: Path, parent_id: tuple[int, int], payload: bytes) -> None:
    def check_destination() -> None:
        _, current_parent_id = _destination(binding, out)
        if parent_id != current_parent_id:
            raise CindyMemoryError("output_drift", "The output parent changed during the operation.")

    def matches_payload(evidence: dict, identity: tuple[int, int]) -> bool:
        return (
            tuple(evidence["identity"]) == identity
            and evidence["size"] == len(payload)
            and evidence["sha256"] == digest
        )

    digest = hashlib.sha256(payload).hexdigest()
    published = False
    try:
        with _parent_fence(out.parent, out.name):
            check_destination()
            temporary = None
            identity = None
            try:
                # On POSIX the preparation file is created with mode 0600.
                # Windows inherits access from the chosen output directory.
                with tempfile.NamedTemporaryFile(dir=out.parent, prefix=".cindy-memory-", delete=False) as stream:
                    temporary = Path(stream.name)
                    info = os.fstat(stream.fileno())
                    identity = (info.st_dev, info.st_ino)
                    if not info.st_ino:
                        raise CindyMemoryError("output_unwritable", "The preparation file has no stable identity.")
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                prepared, _ = read_file(out.parent, temporary.name, limit=len(payload))
                if not matches_payload(prepared, identity):
                    raise CindyMemoryError("output_drift", "The preparation file changed before publication.")
                check_destination()
                if os.name == "nt":
                    os.rename(temporary, out)  # Windows rename refuses an existing destination.
                else:
                    os.link(temporary, out, follow_symlinks=False)  # POSIX rename can replace the destination.
                published = True
            finally:
                if temporary is not None and identity is not None and identity[1]:
                    _remove_prepared(temporary, identity)

            verified, _ = read_file(out.parent, out.name, limit=len(payload))
            if not matches_payload(verified, identity):
                raise CindyMemoryError("output_unverified", "The published snapshot differs from the prepared bytes.")
    except (CindyMemoryError, FrozenFilesError, OSError) as error:
        if published:
            raise CindyMemoryError("output_unverified", "The snapshot was published, but verification failed.") from None
        if isinstance(error, CindyMemoryError):
            raise
        if isinstance(error, FileExistsError):
            raise CindyMemoryError("output_exists", "The snapshot output already exists.") from None
        raise CindyMemoryError("output_unwritable", "The snapshot could not be written safely.") from None


def export_memory(*, maker_memory_root: Path, project: Path, shards: Sequence[str], out: Path) -> dict:
    """Write a new local Markdown snapshot of explicitly selected curated files."""
    selected = _selected_shards(shards)
    binding = _bind(maker_memory_root, project)
    out, parent_id = _destination(binding, out)
    probe = BoundedPathProbe()
    candidates = [
        _shard_metadata(probe, binding.store, filename, kind)
        for filename, kind in selected
    ]
    snapshot = ["# Cindy project memory snapshot\n\nSelected source shards for local review.\n"]
    for row, revision in candidates:
        filename = row["filename"]
        relative = f"{binding.store.name}/{filename}"
        evidence, raw = _read_source(binding.root, relative, _MAX_SHARD_BYTES)
        observed = (*evidence["identity"], evidence["size"], evidence["mtime_ns"])
        if observed != revision:
            raise CindyMemoryError("source_drift", "A selected memory shard changed before it could be read.")
        snapshot.append(_fenced_shard(filename, raw))
    payload = "\n".join(snapshot).encode("utf-8")
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise CindyMemoryError("budget_exceeded", "The selected snapshot exceeds its size limit.")
    probe = BoundedPathProbe()
    for (filename, kind), (_, revision) in zip(selected, candidates):
        try:
            _, observed = _shard_metadata(probe, binding.store, filename, kind)
        except CindyMemoryError:
            observed = None
        if observed != revision:
            raise CindyMemoryError("source_drift", "A selected memory shard changed during the export.")
    _recheck_binding(binding)
    _publish(binding, out, parent_id, payload)
    return {
        **_report(binding, [row for row, _ in candidates], status="exported"),
        "out": str(out),
        "size_bytes": len(payload),
    }
