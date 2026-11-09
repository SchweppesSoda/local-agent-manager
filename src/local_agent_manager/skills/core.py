#!/usr/bin/env python3
"""Declarative manager for mounting work-skills into agent skill roots.

The repository is the source of truth. Targets are declared in
config/skill-targets.yaml. The manager supports read-only planning,
application of safe mount actions, repository content audits, and doctor checks.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import datetime
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable


from .clients import (
    ClientModel, ClientModelError, ConsumerDetection, RuntimeEvidenceError,
    detect_consumers, load_python_runtime_evidence, load_client_model,
    validate_python_runtime_evidence, validate_python_runtime_evidence_freshness,
)
from .clients.runtime_binding import (
    RuntimeBindingError, binding_input_paths, inspect_runtime_binding,
    validate_binding_evidence,
)

from ..providers.t3 import strict_json
from .coordination import SharedResourceBusyError, shared_resource_locks

CLIENTS_DIR = Path(__file__).resolve().parent


REPO_ROOT_SENTINEL = "__REPO_ROOT__"
STATE_DIR_NAME = ".work-skills-state"
STATE_VERSION = 1
MAX_LINK_HOPS = 40
SYSTEM_PYTHON_ENV = "WORK_SKILLS_SYSTEM_PYTHON"
SYSTEM_PYTHON_CHECK_TIMEOUT = 240

# Reviewed source-repository document dependencies, relative to skills_root.
# This is not an import exception or a general allowance for sibling skills.

OBSERVATION_ACTIONS = {
    "CLIENT_RUNTIME_BINDING",
    "DISCOVERY_SOURCE_SCAN",
    "OK_DISCOVERY_MANIFEST",
    "GLOBAL_DELETE_INVENTORY_SCAN",
    "CONSUMER_INSTALLED",
    "CONSUMER_NOT_INSTALLED",
    "CONSUMER_AMBIGUOUS",
    "CONSUMER_NATIVE_UNVERIFIED",
    "PREFLIGHT",
    "OK_LINK",
    "PRESERVE_UNMANAGED_LINK",
    "PRESERVE_LOCAL_ONLY",
    "PRESERVE_LOCAL_REAL_SKILL",
    "PRESERVE_NON_SKILL_DIR",
    "PRESERVE_FILE",
    "PRESERVE_SPECIAL_ENTRY",
    "PRESERVE_UNREGISTERED_LINK",
    "PRESERVE_BACKUP_LIKE_ENTRY",
    "BROKEN_LINK",
    "LINK_LOOP",
    "EXCLUDED",
}

KNOWN_MUTATING_ACTIONS = {
    "CREATE_DISCOVERY_MANIFEST",
    "UPDATE_DISCOVERY_MANIFEST",
    "VERIFY_CONSUMER",
    "GLOBAL_DELETE_CONSUMER",
    "GLOBAL_DELETE_REPO_SKILL",
    "CREATE_TARGET_ROOT",
    "REPLACE_TARGET_ROOT_LINK",
    "REMOVE_STALE_REPO_LINK",
    "REPLACE_WRONG_LINK",
    "MOVE_FILE_TO_BACKUP",
    "MOVE_REAL_DIR_TO_BACKUP",
    "IMPORT_LOCAL_SKILL",
    "CREATE_LINK",
    "PRUNE_BACKUP",
    "LEGACY_CLEANUP",
}


class WorkSkillsError(Exception):
    exit_code = 1


class ConfigError(WorkSkillsError):
    exit_code = 2


class BlockedError(WorkSkillsError):
    exit_code = 3


class TransactionError(WorkSkillsError):
    exit_code = 4


@dataclass(frozen=True)
class PathState:
    """Three-way path observation without discarding the configured spelling.

    ``raw`` is the exact expanded config value, ``lexical`` is the absolute
    path before following links, and ``final`` is the eventual destination.
    ``lstat_kind`` always describes the lexical directory entry itself.
    """

    raw: str
    lexical: Path
    lstat_kind: str
    final: Path
    chain: tuple[Path, ...] = ()
    broken: bool = False
    loop: bool = False

    @property
    def is_link(self) -> bool:
        return self.lstat_kind in {"symlink", "junction", "reparse"}

    @property
    def direct(self) -> bool:
        return self.is_link and len(self.chain) == 1 and not self.broken and not self.loop


@dataclass(frozen=True)
class LinkSnapshot:
    kind: str
    raw_target: str


@dataclass
class Action:
    kind: str
    target: str
    path: Path | None = None
    source: Path | None = None
    message: str = ""
    blocking: bool = False
    destructive: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    action_id: str = ""

    def line(self) -> str:
        status = "BLOCK" if self.blocking else self.kind
        parts = [f"[{status}]", self.target]
        if self.action_id:
            parts.append(f"id={self.action_id}")
        if self.message:
            parts.append(self.message)
        if self.path is not None:
            parts.append(f"path={self.path}")
        if self.source is not None:
            parts.append(f"source={self.source}")
        return " | ".join(parts)


@dataclass
class TargetConfig:
    name: str
    label: str
    root: Path
    backup_root: Path
    import_local: bool
    local_only: list[str]
    exclude: list[str]
    legacy_cleanup: list[dict[str, Any]] = field(default_factory=list)
    root_raw: str = ""
    backup_root_raw: str = ""
    consumer_verified: bool | None = None
    mount_point_id: str | None = None
    adapter_ids: tuple[str, ...] = ()
    consumer_ids: tuple[str, ...] = ()
    adapter_consumers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    client_model: ClientModel | None = field(default=None, repr=False, compare=False)
    runtime_bindings: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False, compare=False)
    discovery_manifest: Path | None = None


@dataclass
class DeletionInventoryRoot:
    name: str
    root: Path
    mount_point_ids: list[str] = field(default_factory=list)
    adapter_ids: list[str] = field(default_factory=list)
    eligible_adapter_ids: list[str] = field(default_factory=list)
    manifest_target_ids: list[str] = field(default_factory=list)


@dataclass
class RuntimeOptions:
    repo_root: Path
    config_path: Path
    targets: list[str]
    import_local_override: bool | None = None
    replace_wrong_links: bool = False
    replace_root_link: bool = False
    replace_files: bool = False
    replace_real_dirs: bool = False
    adopt_existing: bool = False
    prune_backups: bool = False
    keep_backups: int = 10
    delete_skills: list[str] = field(default_factory=list)
    exclude_skills: list[str] = field(default_factory=list)
    confirm_global_delete: bool = False
    yes: bool = False
    output_format: str = "text"
    create_only: bool = False


@dataclass
class Plan:
    repo_root: Path
    skills_root: Path
    targets: list[TargetConfig]
    source_skills: dict[str, Path]
    warnings: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    fingerprints: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict, repr=False)
    semantic_summary: dict[str, Any] = field(default_factory=dict, repr=False)
    selection_intent: dict[str, Any] = field(default_factory=dict, repr=False)
    implementation_digest: str = ""

    @property
    def blockers(self) -> list[Action]:
        return [a for a in self.actions if a.blocking]


@dataclass
class AuditIssue:
    severity: str
    code: str
    skill: str
    path: Path
    message: str

    def line(self) -> str:
        return f"[{self.severity.upper()}] {self.code} | {self.skill} | {self.path} | {self.message}"


@dataclass
class AuditReport:
    repo_root: Path
    skills_root: Path
    skills: list[str]
    issues: list[AuditIssue] = field(default_factory=list)
    shared_resource_references: dict[str, list[str]] = field(default_factory=dict)

    @property
    def errors(self) -> list[AuditIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[AuditIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]


@dataclass(frozen=True)
class SystemDependencyAudit:
    """Result returned by the separately launched system-Python checker."""

    selected_executable: str | None
    returncode: int
    payload: dict[str, Any]

    @property
    def errors(self) -> list[str]:
        value = self.payload.get("errors")
        if isinstance(value, list):
            errors = [str(item) for item in value]
        else:
            errors = []
        if self.returncode != 0 and not errors:
            errors.append(
                str(self.payload.get("message") or "system Python dependency check failed")
            )
        return errors

    @property
    def error_count(self) -> int:
        return max(len(self.errors), 1 if self.returncode != 0 else 0)

    def as_dict(self) -> dict[str, Any]:
        result = dict(self.payload)
        result.setdefault("executable", self.selected_executable)
        result["selectedExecutable"] = self.selected_executable
        result["returnCode"] = self.returncode
        result["errors"] = self.errors
        result["errorCount"] = self.error_count
        return result


def is_windows() -> bool:
    return platform.system().lower().startswith("win")


def lexical_path(path: Path | str) -> Path:
    """Return an absolute path while deliberately not following any link."""
    return Path(os.path.abspath(os.fspath(path)))


def _strip_windows_namespace(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def lexical_norm_path(path: Path) -> str:
    value = _strip_windows_namespace(os.path.abspath(os.fspath(path))).replace("\\", "/").rstrip("/")
    return value.lower() if is_windows() else value


def norm_path(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        resolved = path.absolute()
    value = _strip_windows_namespace(str(resolved)).replace("\\", "/").rstrip("/")
    return value.lower() if is_windows() else value


def is_relative_to(child: Path, parent: Path) -> bool:
    child_s = norm_path(child)
    parent_s = norm_path(parent)
    return child_s == parent_s or child_s.startswith(parent_s + "/")


def same_path(left: Path, right: Path) -> bool:
    return norm_path(left) == norm_path(right)


def lexical_contains(child: Path, parent: Path) -> bool:
    child_s = lexical_norm_path(child)
    parent_s = lexical_norm_path(parent)
    return child_s == parent_s or child_s.startswith(parent_s + "/")


def paths_overlap(left: PathState, right: PathState) -> bool:
    """Compare both lexical and final identities in both directions."""
    left_views = (left.lexical, left.final)
    right_views = (right.lexical, right.final)
    return any(
        lexical_contains(a, b) or lexical_contains(b, a)
        for a in left_views for b in right_views
    )


def _lstat_kind(path: Path) -> str:
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    mode = info.st_mode
    attrs = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(mode):
        return "symlink"
    if attrs & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return "junction" if stat.S_ISDIR(mode) else "reparse"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _read_link_destination(path: Path) -> Path:
    raw = _strip_windows_namespace(os.readlink(path))
    destination = Path(raw)
    return lexical_path(destination if destination.is_absolute() else path.parent / destination)


def inspect_path(raw: Path | str) -> PathState:
    raw_text = os.fspath(raw)
    lexical = lexical_path(raw_text)
    kind = _lstat_kind(lexical)
    chain: list[Path] = []
    current = lexical
    seen: set[str] = {lexical_norm_path(current)}
    broken = False
    loop = False
    if kind in {"symlink", "junction", "reparse"}:
        for _ in range(MAX_LINK_HOPS):
            try:
                destination = _read_link_destination(current)
            except (OSError, ValueError):
                broken = True
                break
            chain.append(destination)
            key = lexical_norm_path(destination)
            if key in seen:
                current = destination
                loop = True
                break
            seen.add(key)
            current = destination
            next_kind = _lstat_kind(current)
            if next_kind == "missing":
                broken = True
                break
            if next_kind not in {"symlink", "junction", "reparse"}:
                break
        else:
            loop = True
    try:
        final = lexical_path(current.resolve(strict=False))
    except (OSError, RuntimeError):
        final = current
    return PathState(raw_text, lexical, kind, final, tuple(chain), broken, loop)


def direct_link_to(path: Path, expected: Path) -> bool:
    state = inspect_path(path)
    if not state.direct:
        return False
    destination = state.chain[0]
    expected_state = inspect_path(expected)
    return lexical_norm_path(destination) == lexical_norm_path(expected_state.lexical) and norm_path(state.final) == norm_path(expected_state.final)


def read_yaml_simple(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ConfigError(
            "PyYAML is required to read config/skill-targets.yaml. Install it in the managed Python environment or keep using wrapper scripts until available."
        ) from exc
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return data


def expand_value(value: str, repo_root: Path, config_dir: Path) -> str:
    replacements = {
        "CONFIG_DIR": str(config_dir),
        "REPO_ROOT": str(repo_root),
        "USERPROFILE": os.environ.get("USERPROFILE") or str(Path.home()),
        "HOME": str(Path.home()),
        "WORKBUDDY_HOME": os.environ.get("WORKBUDDY_HOME", str(Path.home() / ".workbuddy")),
        "CODEX_HOME": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        "QODERWORK_HOME": os.environ.get("QODERWORK_HOME", str(Path.home() / ".qoderworkcn")),
    }

    pattern = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

    def replace_match(match: re.Match[str]) -> str:
        key = match.group(1)
        default = match.group(2)
        if key in os.environ and os.environ[key]:
            return os.environ[key]
        if key in replacements:
            return replacements[key]
        if default is not None:
            return expand_value(default, repo_root, config_dir)
        return match.group(0)

    result = value
    previous = None
    for _ in range(10):
        if result == previous:
            break
        previous = result
        result = pattern.sub(replace_match, result)
    result = os.path.expandvars(os.path.expanduser(result))
    return result


def resolve_config_path(repo_root: Path, config_arg: str | None) -> Path:
    if config_arg:
        p = Path(config_arg)
        return (p if p.is_absolute() else repo_root / p).resolve(strict=False)
    return (repo_root / "config" / "skill-targets.yaml").resolve(strict=False)


def configured_repo_root(config_path: Path, explicit_root: Path | None = None) -> Path | None:
    declaration = (read_yaml_simple(config_path).get("repo") or {}).get("root")
    if not declaration:
        return None
    expanded = Path(expand_value(str(declaration), explicit_root or config_path.parent, config_path.parent))
    return (expanded if expanded.is_absolute() else config_path.parent / expanded).resolve()


def repo_root_from_args(repo_arg: str | None, config_arg: str | None = None) -> Path:
    if repo_arg:
        return Path(repo_arg).expanduser().resolve()
    if config_arg:
        root = configured_repo_root(Path(config_arg).expanduser().resolve())
        if root is None:
            raise ConfigError("Configuration-only use requires repo.root")
        return root
    candidate = Path.cwd().resolve()
    if not (candidate / "config" / "skill-targets.yaml").is_file():
        raise ConfigError("Specify --repo-root for the Skill source repository")
    return candidate


V3_MODEL_SENTINEL = "__client_model_v3__"
V3_DETECTIONS_SENTINEL = "__consumer_detections_v3__"
V3_SEMANTICS_SENTINEL = "__client_semantics_v3__"
V3_SELECTION_SENTINEL = "__selection_intent_v3__"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Path):
        return lexical_norm_path(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ConfigError(f"Unsupported value in canonical client semantics: {type(value).__name__}")


def _v3_semantic_summary(config: dict[str, Any], model: ClientModel) -> dict[str, Any]:
    raw = {
        key: value for key, value in config.items()
        if not str(key).startswith("__")
    }
    return {
        "validatedConfig": _canonical_value(raw),
        "validatedModel": _canonical_value(model),
    }


def manager_implementation_digest() -> str:
    digest = hashlib.sha256()
    package = Path(__file__).resolve().parent
    paths = [path for path in package.rglob("*") if path.suffix in {".py", ".json"}]
    paths.extend((package.parent / "providers").glob("*.py"))
    for path in sorted(paths, key=lambda item: item.relative_to(package.parent).as_posix()):
        digest.update(path.relative_to(package.parent).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _selection_intent(
    options: RuntimeOptions, target_sets: list[str] | None, all_targets: bool
) -> dict[str, Any]:
    return {
        "explicitTargets": list(options.targets),
        "targetSets": list(target_sets or []),
        "all": bool(all_targets),
        "createOnly": options.create_only,
    }


def _load_v3_model(config: dict[str, Any]) -> ClientModel:
    try:
        return load_client_model(config)
    except ClientModelError as exc:
        raise ConfigError(str(exc)) from exc


def _v3_requested_adapters(
    config: dict[str, Any],
    model: ClientModel,
    options: RuntimeOptions,
    target_sets: list[str] | None,
    all_targets: bool,
) -> list[tuple[str, str | None, bool]]:
    """Resolve compatibility names to adapter IDs and optional mount filters.

    The boolean records a default-like target-set request, for which
    ``manageByDefault`` is an additional mutation gate.
    """

    requests: list[tuple[str, bool]] = []
    sets = config.get("targetSets", {}) or {}
    if not isinstance(sets, dict):
        raise ConfigError("Config field 'targetSets' must be a mapping.")
    if all_targets:
        requests.extend((adapter_id, False) for adapter_id in model.adapters)
    for set_name in target_sets or []:
        if set_name == "all":
            members = list(model.adapters)
        else:
            members = sets.get(set_name)
        if members is None and set_name in {"default", "both"}:
            members = list(model.default_adapter_ids)
        if not isinstance(members, list):
            raise ConfigError(f"Unknown or invalid target set: {set_name}")
        requests.extend((str(member), set_name in {"default", "both"}) for member in members)
    requests.extend((name, False) for name in options.targets)
    if not requests:
        raise ConfigError("Select at least one target with --target, --target-set, or --all.")

    raw_targets = config.get("targets", {}) or {}
    resolved: list[tuple[str, str | None, bool]] = []
    seen: set[tuple[str, str | None]] = set()
    for name, default_like in requests:
        adapter_id: str | None = name if name in model.adapters else None
        mount_filter: str | None = None
        if adapter_id is None and name in model.mount_points:
            mount_filter = name
            candidates = [
                adapter.id for adapter in model.adapters.values()
                if name in adapter.mount_point_ids
            ]
            if len(candidates) == 1:
                adapter_id = candidates[0]
            elif len(candidates) > 1:
                confirmed = [
                    candidate for candidate in candidates
                    if model.adapters[candidate].management_status == "confirmed"
                ]
                if len(confirmed) == 1:
                    adapter_id = confirmed[0]
                else:
                    raise ConfigError(
                        f"Mount point {name!r} has multiple adapters; select an adapter explicitly"
                    )
        if adapter_id is None and isinstance(raw_targets, dict) and name in raw_targets:
            raw = raw_targets.get(name) or {}
            root_raw = raw.get("root") if isinstance(raw, dict) else None
            if root_raw:
                expanded = lexical_path(expand_value(str(root_raw), options.repo_root, options.config_path.parent))
                matching_mounts = [
                    mount.id for mount in model.mount_points.values()
                    if lexical_norm_path(mount.root) == lexical_norm_path(expanded)
                ]
                candidates = [
                    adapter.id for adapter in model.adapters.values()
                    if any(mount_id in adapter.mount_point_ids for mount_id in matching_mounts)
                ]
                if len(candidates) == 1 and len(matching_mounts) == 1:
                    adapter_id, mount_filter = candidates[0], matching_mounts[0]
        if adapter_id is None:
            raise ConfigError(f"Unknown adapter or compatibility target: {name}")
        key = (adapter_id, mount_filter)
        if key not in seen:
            seen.add(key)
            resolved.append((adapter_id, mount_filter, default_like))
    return resolved


def _v3_policy_target(
    config: dict[str, Any], options: RuntimeOptions, adapter_id: str, mount_id: str, root: Path
) -> dict[str, Any]:
    raw_targets = config.get("targets", {}) or {}
    if not isinstance(raw_targets, dict):
        return {}
    candidates: list[dict[str, Any]] = []
    for name in (mount_id, adapter_id):
        raw = raw_targets.get(name)
        if isinstance(raw, dict):
            candidates.append(raw)
    for raw in raw_targets.values():
        if not isinstance(raw, dict) or raw in candidates or not raw.get("root"):
            continue
        expanded = lexical_path(expand_value(str(raw["root"]), options.repo_root, options.config_path.parent))
        if lexical_norm_path(expanded) == lexical_norm_path(root):
            candidates.append(raw)
    return candidates[0] if candidates else {}


def _load_v3_target_configs(
    config: dict[str, Any],
    options: RuntimeOptions,
    target_sets: list[str] | None,
    all_targets: bool,
) -> list[TargetConfig]:
    model = _load_v3_model(config)
    detections = {item.consumer_id: item for item in detect_consumers(model)}
    requests = _v3_requested_adapters(config, model, options, target_sets, all_targets)
    defaults = config.get("defaults", {}) or {}
    by_mount: dict[str, TargetConfig] = {}

    for adapter_id, mount_filter, default_like in requests:
        adapter = model.adapters[adapter_id]
        consumer = model.consumers[adapter.consumer_detection]
        detection = detections[adapter.consumer_detection]
        may_manage = (
            adapter.management_status == "confirmed"
            and consumer.status == "confirmed"
            and detection.state == "installed"
            and (not default_like or adapter.manage_by_default)
        )
        if not may_manage:
            continue
        roots = sorted(adapter.discovery_roots, key=lambda item: item.priority, reverse=True)
        for discovery in roots:
            mount = model.mount_points[discovery.mount_point_id]
            if mount_filter is not None and mount.id != mount_filter:
                continue
            if mount_filter is None and (
                mount.delivery in {"compatibility", "private-only", "candidate"}
                or discovery.role in {"transitional", "private-only"}
            ):
                continue
            if mount.source_selection == "none":
                continue
            existing = by_mount.get(mount.id)
            if existing is not None:
                existing.adapter_ids = tuple(dict.fromkeys((*existing.adapter_ids, adapter.id)))
                existing.consumer_ids = tuple(dict.fromkeys((*existing.consumer_ids, *adapter.consumer_ids)))
                existing.adapter_consumers[adapter.id] = adapter.consumer_ids
                existing.consumer_verified = bool(existing.consumer_verified) and adapter.native_verification == "verified"
                existing.client_model = model
                continue

            raw = _v3_policy_target(config, options, adapter.id, mount.id, mount.root)
            mount_cfg = raw.get("mount", {}) or {}
            exclude = list(mount_cfg.get("exclude") or []) + list(options.exclude_skills)
            local_only = list(defaults.get("localOnly") or []) + list(raw.get("localOnly") or [])
            import_local = bool(raw.get("importLocal", defaults.get("importLocal", False)))
            if options.import_local_override is not None:
                import_local = options.import_local_override
            backup_raw = raw.get("backupRoot") or defaults.get("backupRoot") or "{targetRoot}_backups"
            backup_value = str(backup_raw).replace("{targetRoot}", str(mount.root))
            backup_root = lexical_path(expand_value(backup_value, options.repo_root, options.config_path.parent))
            by_mount[mount.id] = TargetConfig(
                name=mount.id,
                label=mount.label,
                root=lexical_path(mount.root),
                backup_root=backup_root,
                import_local=import_local,
                local_only=local_only,
                exclude=exclude,
                legacy_cleanup=list(raw.get("legacyCleanup") or []),
                root_raw=str(raw.get("root") or mount.root),
                backup_root_raw=str(backup_raw),
                # Deployment ownership is not native consumer verification.
                # v3 becomes green only from structured state evidence.
                consumer_verified=False,
                mount_point_id=mount.id,
                adapter_ids=(adapter.id,),
                consumer_ids=adapter.consumer_ids,
                adapter_consumers={adapter.id: adapter.consumer_ids},
                client_model=model,
                discovery_manifest=mount.discovery_manifest,
            )

    config[V3_MODEL_SENTINEL] = model
    config[V3_DETECTIONS_SENTINEL] = detections
    config[V3_SEMANTICS_SENTINEL] = _v3_semantic_summary(config, model)
    config[V3_SELECTION_SENTINEL] = _selection_intent(options, target_sets, all_targets)
    return list(by_mount.values())


def load_target_configs(options: RuntimeOptions, target_sets: list[str] | None = None, all_targets: bool = False) -> tuple[dict[str, Any], list[TargetConfig]]:
    config = read_yaml_simple(options.config_path)
    config_dir = options.config_path.parent

    repo_cfg = config.get("repo", {}) or {}
    skills_dir = repo_cfg.get("skillsRoot") or repo_cfg.get("skillsDir") or "skills"
    skills_root = lexical_path(options.repo_root / str(skills_dir))
    repo_root = lexical_path(options.repo_root)
    skills_state = inspect_path(skills_root)
    if not lexical_contains(skills_root, repo_root) or lexical_norm_path(skills_root) == lexical_norm_path(repo_root):
        raise ConfigError(f"Repo skills root must be a child of the repository: {skills_root}")
    if (
        skills_state.lstat_kind != "directory"
        or skills_state.is_link
        or lexical_norm_path(skills_state.lexical) != lexical_norm_path(skills_state.final)
        or not lexical_contains(skills_state.final, repo_root)
    ):
        raise ConfigError(f"Repo skills root must be a real directory inside the repository: {skills_root}")

    if config.get("version") == 3:
        result = _load_v3_target_configs(config, options, target_sets, all_targets)
        config[REPO_ROOT_SENTINEL] = str(options.repo_root)
        config["__skills_root__"] = str(skills_root)
        return config, result

    raw_targets = config.get("targets", {}) or {}
    if not isinstance(raw_targets, dict):
        raise ConfigError("Config field 'targets' must be a mapping.")

    selected_names: list[str] = []
    if all_targets:
        selected_names.extend(raw_targets.keys())
    if target_sets:
        sets = config.get("targetSets", {}) or {}
        for set_name in target_sets:
            members = sets.get(set_name)
            if members is None and set_name == "both":
                members = ["codex", "workbuddy"]
            if not isinstance(members, list):
                raise ConfigError(f"Unknown or invalid target set: {set_name}")
            selected_names.extend(str(x) for x in members)
    selected_names.extend(options.targets)
    if not selected_names:
        raise ConfigError("Select at least one target with --target, --target-set, or --all.")

    result: list[TargetConfig] = []
    seen: set[str] = set()
    defaults = config.get("defaults", {}) or {}
    for name in selected_names:
        if name in seen:
            continue
        seen.add(name)
        raw = raw_targets.get(name)
        if not isinstance(raw, dict):
            raise ConfigError(f"Unknown target: {name}")
        label = str(raw.get("label") or name)
        root_raw = raw.get("root")
        if not root_raw:
            raise ConfigError(f"Target {name} missing root.")
        root_value = expand_value(str(root_raw), options.repo_root, config_dir)
        root = lexical_path(root_value)
        backup_raw = raw.get("backupRoot") or defaults.get("backupRoot") or "{targetRoot}_backups"
        backup_value = str(backup_raw).replace("{targetRoot}", str(root))
        backup_expanded = expand_value(backup_value, options.repo_root, config_dir)
        backup_root = lexical_path(backup_expanded)
        local_only = list(defaults.get("localOnly") or []) + list(raw.get("localOnly") or [])
        mount_cfg = raw.get("mount", {}) or {}
        exclude = list(mount_cfg.get("exclude") or []) + list(options.exclude_skills)
        import_local = bool(raw.get("importLocal", defaults.get("importLocal", False)))
        if options.import_local_override is not None:
            import_local = options.import_local_override
        legacy = list(raw.get("legacyCleanup") or [])
        verified_raw = raw.get("verified")
        consumer_verified = verified_raw if isinstance(verified_raw, bool) else None
        result.append(TargetConfig(
            name, label, root, backup_root, import_local, local_only, exclude, legacy,
            root_raw=str(root_raw), backup_root_raw=str(backup_raw),
            consumer_verified=consumer_verified,
        ))

    config[REPO_ROOT_SENTINEL] = str(options.repo_root)
    config["__skills_root__"] = str(skills_root)
    return config, result


def state_dir(repo_root: Path) -> Path:
    override = os.environ.get("WORK_SKILLS_STATE_DIR")
    if override:
        return lexical_path(override)
    repo_id = hashlib.sha256(norm_path(repo_root).encode("utf-8")).hexdigest()[:20]
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return lexical_path(base / "work-skills" / repo_id)


def repository_id(repo_root: Path) -> str:
    return hashlib.sha256(norm_path(repo_root).encode("utf-8")).hexdigest()


def empty_manifest(repo_root: Path) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "repoId": repository_id(repo_root),
        "repoRoot": str(lexical_path(repo_root)),
        "consumers": {},
        "backups": {},
    }


def load_manifest(repo_root: Path) -> dict[str, Any]:
    path = state_dir(repo_root) / "manifest.json"
    if not path.exists():
        return empty_manifest(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"State manifest is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise ConfigError(f"Unsupported state manifest: {path}")
    if data.get("repoId") != repository_id(repo_root) or norm_path(Path(str(data.get("repoRoot", "")))) != norm_path(repo_root):
        raise ConfigError(f"State manifest belongs to another repository: {path}")
    consumers = data.get("consumers")
    backups = data.get("backups")
    if not isinstance(consumers, dict) or not isinstance(backups, dict):
        raise ConfigError(f"State manifest has invalid collections: {path}")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


@contextlib.contextmanager
def apply_lock(repo_root: Path) -> Iterable[Path]:
    directory = state_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "apply.lock"
    payload = json.dumps({"pid": os.getpid(), "created": time.time(), "repoRoot": str(repo_root)})
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        try:
            detail = lock_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            detail = "unreadable"
        raise BlockedError(f"Another apply transaction holds {lock_path}: {detail}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        yield lock_path
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def path_fingerprint(path: Path, *, recursive: bool = False) -> dict[str, Any]:
    lexical = lexical_path(path)
    kind = _lstat_kind(lexical)
    result: dict[str, Any] = {"path": lexical_norm_path(lexical), "kind": kind}
    if kind == "missing":
        return result
    try:
        info = os.lstat(lexical)
        result.update({
            "mode": info.st_mode,
            "size": info.st_size,
            "mtimeNs": info.st_mtime_ns,
            "device": info.st_dev,
            "inode": info.st_ino,
        })
        if kind in {"symlink", "junction", "reparse"}:
            result["link"] = os.readlink(lexical)
    except OSError as exc:
        result["error"] = type(exc).__name__
        return result
    if recursive and kind == "directory":
        digest = hashlib.sha256()
        for current_root, dir_names, file_names in os.walk(lexical, topdown=True, followlinks=False):
            current = Path(current_root)
            dir_names.sort(key=str.lower)
            file_names.sort(key=str.lower)
            for name in [*dir_names, *file_names]:
                child = current / name
                relative = child.relative_to(lexical).as_posix()
                child_kind = _lstat_kind(child)
                digest.update(relative.encode("utf-8", errors="surrogatepass"))
                digest.update(b"\0" + child_kind.encode("ascii") + b"\0")
                try:
                    child_info = os.lstat(child)
                    digest.update(f"{child_info.st_mode}:{child_info.st_size}:{child_info.st_mtime_ns}".encode("ascii"))
                    if child_kind == "file":
                        with child.open("rb") as fh:
                            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                                digest.update(chunk)
                    elif child_kind in {"symlink", "junction", "reparse"}:
                        digest.update(os.readlink(child).encode("utf-8", errors="surrogatepass"))
                except OSError as exc:
                    digest.update(type(exc).__name__.encode("ascii"))
        result["treeSha256"] = digest.hexdigest()
    return result


def shallow_listing_fingerprint(path: Path) -> dict[str, Any]:
    """Seal one discovery-root level using lstat only; never read descendants."""
    result = path_fingerprint(path, recursive=False)
    if result.get("kind") != "directory":
        return result
    children: list[dict[str, Any]] = []
    try:
        entries = sorted(Path(path).iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        result["listingError"] = type(exc).__name__
        return result
    for child in entries:
        child_kind = _lstat_kind(child)
        entry: dict[str, Any] = {"name": child.name, "kind": child_kind}
        try:
            info = os.lstat(child)
            entry.update({
                "mode": info.st_mode,
                "size": info.st_size,
                "mtimeNs": info.st_mtime_ns,
                "device": info.st_dev,
                "inode": info.st_ino,
            })
            if child_kind in {"symlink", "junction", "reparse"}:
                entry["link"] = os.readlink(child)
                state = inspect_path(child)
                hops: list[dict[str, Any]] = []
                for hop_path in (state.lexical, *state.chain)[: MAX_LINK_HOPS + 1]:
                    hop_kind = _lstat_kind(hop_path)
                    hop: dict[str, Any] = {
                        "path": lexical_norm_path(hop_path),
                        "kind": hop_kind,
                    }
                    try:
                        hop_info = os.lstat(hop_path)
                        hop.update({
                            "mode": hop_info.st_mode,
                            "size": hop_info.st_size,
                            "mtimeNs": hop_info.st_mtime_ns,
                            "device": hop_info.st_dev,
                            "inode": hop_info.st_ino,
                        })
                        if hop_kind in {"symlink", "junction", "reparse"}:
                            hop["link"] = os.readlink(hop_path)
                    except OSError as exc:
                        hop["error"] = type(exc).__name__
                    hops.append(hop)
                entry["resolution"] = {
                    "final": lexical_norm_path(state.final),
                    "broken": state.broken,
                    "loop": state.loop,
                    "hops": hops,
                }
        except OSError as exc:
            entry["error"] = type(exc).__name__
        children.append(entry)
    result["children"] = children
    return result


def _action_precondition_paths(action: Action) -> list[tuple[Path, bool]]:
    paths: list[tuple[Path, bool]] = []
    if action.kind == "CLIENT_RUNTIME_BINDING":
        paths.extend((Path(value), False) for value in action.data.get("inputPaths", []))
    if (
        action.path is not None
        and not action.data.get("postRootReplacement")
        and not action.data.get("postRootFileReplacement")
    ):
        deep = action.kind in {
            "IMPORT_LOCAL_SKILL",
            "GLOBAL_DELETE_REPO_SKILL",
        }
        paths.append((action.path, deep))
    if action.source is not None:
        # Seal source content, not just its directory entry. Editing SKILL.md
        # or a bundled resource must invalidate the reviewed plan.
        deep = _lstat_kind(action.source) == "directory"
        paths.append((action.source, deep))
    return paths


def _action_fingerprint(action: Action, path: Path, recursive: bool) -> dict[str, Any]:
    if action.kind == "GLOBAL_DELETE_INVENTORY_SCAN":
        return shallow_listing_fingerprint(path)
    return path_fingerprint(path, recursive=recursive)


def seal_plan(plan: Plan) -> None:
    plan.fingerprints.clear()
    for action in plan.actions:
        preconditions: dict[str, dict[str, Any]] = {}
        for path, recursive in _action_precondition_paths(action):
            key = lexical_norm_path(path)
            fingerprint = _action_fingerprint(action, path, recursive)
            preconditions[key] = fingerprint
            plan.fingerprints[key] = fingerprint
        action.data["preconditions"] = preconditions
        identity = json.dumps({
            "kind": action.kind,
            "target": action.target,
            "path": str(action.path) if action.path is not None else None,
            "source": str(action.source) if action.source is not None else None,
            "data": action.data,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        action.action_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    action_payload = [
        {
            "kind": action.kind,
            "target": action.target,
            "path": str(action.path) if action.path is not None else None,
            "source": str(action.source) if action.source is not None else None,
            "blocking": action.blocking,
            "destructive": action.destructive,
            "message": action.message,
            "data": action.data,
        }
        for action in plan.actions
    ]
    payload = json.dumps(
        {
            "repoRoot": lexical_norm_path(plan.repo_root),
            "skillsRoot": lexical_norm_path(plan.skills_root),
            "sourceSkills": {
                name: lexical_norm_path(path) for name, path in sorted(plan.source_skills.items())
            },
            "targets": [
                {
                    "name": target.name,
                    "root": lexical_norm_path(target.root),
                    "backupRoot": lexical_norm_path(target.backup_root),
                    "importLocal": target.import_local,
                    "localOnly": target.local_only,
                    "exclude": target.exclude,
                    "adapterIds": target.adapter_ids,
                    "consumerIds": target.consumer_ids,
                    "adapterConsumers": target.adapter_consumers,
                }
                for target in plan.targets
            ],
            "warnings": plan.warnings,
            "manifest": plan.manifest,
            "semanticSummary": plan.semantic_summary,
            "selectionIntent": plan.selection_intent,
            "implementationDigest": plan.implementation_digest,
            "fingerprints": plan.fingerprints,
            "actions": action_payload,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    plan.plan_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_action_preconditions(action: Action) -> None:
    expected = action.data.get("preconditions")
    if not isinstance(expected, dict):
        raise TransactionError(f"Unsealed action in plan: {action.kind}")
    for path, recursive in _action_precondition_paths(action):
        key = lexical_norm_path(path)
        actual = _action_fingerprint(action, path, recursive)
        if actual != expected.get(key):
            raise BlockedError(f"Plan precondition changed for {path}; re-run plan/apply")


def verify_plan_fingerprints(plan: Plan) -> None:
    for action in plan.actions:
        verify_action_preconditions(action)


def verify_plan_semantics(plan: Plan, options: RuntimeOptions) -> None:
    if manager_implementation_digest() != plan.implementation_digest:
        raise BlockedError("Manager or client-model implementation changed after planning; review a new plan")
    config = read_yaml_simple(options.config_path)
    if config.get("version") == 3:
        model = _load_v3_model(config)
        actual = _v3_semantic_summary(config, model)
    else:
        actual = {"validatedConfig": _canonical_value(config)}
    if actual != plan.semantic_summary:
        raise BlockedError("Validated configuration semantics changed after planning; review a new plan")
    if list(options.targets) != list(plan.selection_intent.get("explicitTargets") or []):
        raise BlockedError("Explicit target selection changed after planning; review a new plan")
    if options.create_only != plan.selection_intent.get("createOnly", False):
        raise BlockedError("Create-only policy changed after planning; review a new plan")


def write_transaction(repo_root: Path, transaction: dict[str, Any]) -> Path:
    path = state_dir(repo_root) / "transactions" / f"{transaction['id']}.json"
    atomic_write_json(path, transaction)
    return path


def manifest_consumer(manifest: dict[str, Any], target: str, skill: str) -> dict[str, Any] | None:
    targets = manifest.get("consumers", {})
    target_state = targets.get(target) if isinstance(targets, dict) else None
    skills = target_state.get("skills") if isinstance(target_state, dict) else None
    value = skills.get(skill) if isinstance(skills, dict) else None
    return value if isinstance(value, dict) else None


def manifest_tracks_consumer_path(manifest: dict[str, Any], path: Path) -> bool:
    """Keep recorded deployments protected even if their target was renamed."""
    consumers = manifest.get("consumers", {})
    if not isinstance(consumers, dict):
        return False
    for target_state in consumers.values():
        records = target_state.get("skills", {}) if isinstance(target_state, dict) else {}
        if not isinstance(records, dict):
            continue
        for record in records.values():
            recorded_path = record.get("path") if isinstance(record, dict) else None
            if isinstance(recorded_path, str) and recorded_path and lexical_norm_path(Path(recorded_path)) == lexical_norm_path(path):
                return True
    return False


def _native_evidence_is_valid(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    verified_on = evidence.get("verifiedOn")
    try:
        verified_date = datetime.date.fromisoformat(verified_on)
    except (TypeError, ValueError):
        return False
    return (
        evidence.get("method") in {"native-probe", "manual-native-check"}
        and isinstance(evidence.get("verifiedVersion"), str)
        and bool(evidence.get("verifiedVersion").strip())
        and verified_date <= datetime.date.today()
    )


def inspect_adapter_runtime_binding(model: ClientModel, adapter_id: str, root: Path) -> dict[str, Any]:
    adapter = model.adapters[adapter_id]
    mount = next(item for item in model.mount_points.values() if lexical_norm_path(item.root) == lexical_norm_path(root))
    return inspect_runtime_binding(adapter.runtime_binding, root, mount.discovery_manifest)


def _adapter_native_record_is_current(
    record: Any, target: TargetConfig, adapter_id: str, consumer_ids: tuple[str, ...]
) -> bool:
    base_is_current = (
        isinstance(record, dict)
        and record.get("status") == "verified"
        and record.get("adapterId") == adapter_id
        and set(record.get("consumerIds") or []) == set(consumer_ids)
        and record.get("mountPointId") == target.mount_point_id
        and lexical_norm_path(Path(str(record.get("root") or "."))) == lexical_norm_path(target.root)
        and _native_evidence_is_valid(record.get("evidence"))
    )
    if not base_is_current:
        return False
    model = target.client_model
    if not isinstance(model, ClientModel):
        return True
    adapter = model.adapters.get(adapter_id)
    if adapter is None:
        return False
    if adapter.runtime_binding is not None:
        try:
            snapshot = target.runtime_bindings.get(adapter_id)
            if snapshot is None:
                snapshot = inspect_adapter_runtime_binding(model, adapter_id, target.root)
                target.runtime_bindings[adapter_id] = snapshot
        except RuntimeBindingError:
            return False
        if record.get("evidence", {}).get("runtimeBinding") != snapshot:
            return False
    if adapter.python_runtime_verification is None:
        return True
    evidence = record.get("evidence")
    python_runtime = evidence.get("pythonRuntime") if isinstance(evidence, dict) else None
    return not validate_python_runtime_evidence(model, adapter_id, python_runtime)


def _target_requires_python_runtime_evidence(target: TargetConfig) -> bool:
    model = target.client_model
    if not isinstance(model, ClientModel):
        return False
    return any(
        adapter_id not in model.adapters
        or model.adapters[adapter_id].python_runtime_verification is not None
        or model.adapters[adapter_id].runtime_binding is not None
        for adapter_id in target.adapter_ids
    )


def native_verification_is_current(target_state: Any, target: TargetConfig) -> bool:
    if not isinstance(target_state, dict):
        return False
    if not target.adapter_ids:  # v1 compatibility surface for direct API tests
        return target_state.get("nativeVerified") is True
    records = target_state.get("nativeVerifications")
    if isinstance(records, dict) and all(
        _adapter_native_record_is_current(
            records.get(adapter_id),
            target,
            adapter_id,
            target.adapter_consumers.get(adapter_id, target.consumer_ids),
        )
        for adapter_id in target.adapter_ids
    ):
        return True
    # Aggregate v3 records predate adapter-bound Python runtime evidence.  A
    # policy-bearing adapter must be reverified into its own record; otherwise
    # stale or cross-client evidence could make the shared mount look healthy.
    if _target_requires_python_runtime_evidence(target):
        return False
    # Read the original aggregate v3 record so existing state and focused API
    # tests migrate without a false regression. New writes are per adapter.
    record = target_state.get("nativeVerification")
    return (
        isinstance(record, dict)
        and record.get("status") == "verified"
        and set(record.get("adapterIds") or []) == set(target.adapter_ids)
        and set(record.get("consumerIds") or []) == set(target.consumer_ids)
        and record.get("mountPointId") == target.mount_point_id
        and lexical_norm_path(Path(str(record.get("root") or "."))) == lexical_norm_path(target.root)
        and _native_evidence_is_valid(record.get("evidence"))
    )


def consumer_is_verified(manifest: dict[str, Any], target: TargetConfig, skill: str, path: Path, source: Path) -> bool:
    entry = manifest_consumer(manifest, target.name, skill)
    if entry is None or entry.get("deploymentOwned") is not True or entry.get("deploymentHealthy") is not True:
        return False
    target_state = (manifest.get("consumers", {}) or {}).get(target.name, {})
    if not native_verification_is_current(target_state, target):
        return False
    return (
        lexical_norm_path(Path(str(entry.get("path", "")))) == lexical_norm_path(path)
        and lexical_norm_path(Path(str(entry.get("source", "")))) == lexical_norm_path(source)
    )


def register_consumer(manifest: dict[str, Any], target: TargetConfig, skill: str, path: Path, source: Path) -> None:
    consumers = manifest.setdefault("consumers", {})
    target_state = consumers.setdefault(target.name, {})
    was_native_verified = native_verification_is_current(target_state, target)
    target_state.update({
        "label": target.label,
        "root": str(target.root),
        "rootRaw": target.root_raw,
        "deploymentHealthy": True,
        "nativeVerified": (target.consumer_verified is True) if not target.adapter_ids else was_native_verified,
    })
    if target.adapter_ids:
        records = target_state.setdefault("nativeVerifications", {})
        if not isinstance(records, dict):
            records = {}
            target_state["nativeVerifications"] = records
        for adapter_id in target.adapter_ids:
            consumer_ids = target.adapter_consumers.get(adapter_id, target.consumer_ids)
            if not _adapter_native_record_is_current(
                records.get(adapter_id), target, adapter_id, consumer_ids
            ):
                records[adapter_id] = {
                    "status": "unverified",
                    "adapterId": adapter_id,
                    "consumerIds": list(consumer_ids),
                    "mountPointId": target.mount_point_id,
                    "root": str(target.root),
                    "evidence": None,
                }
    skills = target_state.setdefault("skills", {})
    observed = inspect_path(path)
    skills[skill] = {
        "path": str(lexical_path(path)),
        "source": str(lexical_path(source)),
        "deploymentOwned": True,
        "deploymentHealthy": direct_link_to(path, source),
        "nativeVerified": (target.consumer_verified is True) if not target.adapter_ids else was_native_verified,
        "kind": observed.lstat_kind,
        "immediateTarget": str(observed.chain[0]) if observed.chain else None,
        "sourceFingerprint": path_fingerprint(source, recursive=True),
        "updated": time.time(),
    }


def unregister_consumer(manifest: dict[str, Any], target: str, skill: str) -> None:
    consumers = manifest.get("consumers")
    if not isinstance(consumers, dict):
        return
    target_state = consumers.get(target)
    skills = target_state.get("skills") if isinstance(target_state, dict) else None
    if isinstance(skills, dict):
        skills.pop(skill, None)


def discover_source_skills(skills_root: Path) -> tuple[dict[str, Path], list[str]]:
    skills: dict[str, Path] = {}
    warnings: list[str] = []
    for entry in sorted(skills_root.iterdir(), key=lambda p: p.name.lower()):
        entry_kind = _lstat_kind(entry)
        if entry_kind != "directory":
            if entry_kind in {"symlink", "junction", "reparse"}:
                raise ConfigError(
                    f"Linked/reparse entries are forbidden in the only source tree: {entry}"
                )
            continue
        if _lstat_kind(entry / "SKILL.md") == "file":
            skills[entry.name] = lexical_path(entry)
        else:
            warnings.append(f"Skipping source directory without SKILL.md: {entry}")
    if not skills:
        raise ConfigError(f"No source skills found under: {skills_root}")
    return skills, warnings


def parse_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ConfigError("PyYAML is required for repository content audits.") from exc
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("YAML root must be a mapping")
    return data


def parse_skill_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", text, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md must start with YAML frontmatter delimited by ---")
    try:
        import yaml  # type: ignore
        metadata = yaml.safe_load(match.group(1)) or {}
    except Exception as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("frontmatter must be a mapping")
    return metadata, text[match.end():]


def add_audit_issue(report: AuditReport, severity: str, code: str, skill: str, path: Path, message: str) -> None:
    report.issues.append(AuditIssue(severity, code, skill, path, message))


def referenced_resource_paths(body: str) -> set[str]:
    candidates: set[str] = set()
    for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", body):
        value = raw.strip().strip("<>").split(maxsplit=1)[0]
        candidates.add(value)
    for raw in re.findall(r"`([^`\r\n]+)`", body):
        value = raw.strip()
        # Inline script commands include arguments, unlike resource paths.
        # Keep spaces in document names and quoted script names intact.
        command = re.match(
            r"^(?P<quote>[\"']?)(?P<script>(?:\./)?scripts[/\\].+?\.(?:py|ps1|sh))"
            r"(?P=quote)(?=\s|$)",
            value,
            re.I,
        )
        if command:
            value = command.group("script")
        if re.match(r"^(?:\.?/?)(?:scripts|references|assets|specs)[/\\]", value):
            candidates.add(value)
    return candidates


def markdown_resource_sources(skill_root: Path) -> list[Path]:
    skill_md = skill_root / "SKILL.md"
    sources = [skill_md]
    sources.extend(
        path
        for path in skill_root.rglob("*.md")
        if path.is_file() and path != skill_md
    )
    return sorted(set(sources))


def normalized_resource_reference(raw: str) -> str | None:
    if not raw or raw.startswith(("#", "http://", "https://", "mailto:", "skill://")):
        return None
    value = urllib.parse.unquote(raw.split("#", 1)[0].split("?", 1)[0]).strip().strip("<>")
    value = value.replace("\\", "/")
    if not value or any(token in value for token in ("<", ">", "*", "{", "}")):
        return None
    if Path(value.replace("/", os.sep)).is_absolute() or PureWindowsPath(value).is_absolute():
        return None
    return value


def resolve_resource_reference(skill_root: Path, source: Path, value: str) -> Path:
    candidate = Path(value.replace("/", os.sep))
    root_relative = bool(re.match(r"^(?:\./)?(?:scripts|references|assets|specs)[/\\]", value, re.I))
    base = skill_root if root_relative else source.parent
    return (base / candidate).resolve(strict=False)


def validate_resource_reference(report: AuditReport, skill: str, skill_root: Path, source: Path, raw: str) -> None:
    value = normalized_resource_reference(raw)
    if value is None:
        return
    candidate = Path(value.replace("/", os.sep))
    resolved = resolve_resource_reference(skill_root, source, value)
    if not is_relative_to(resolved, skill_root):
        # Match both the written destination and its final filesystem target.
        # A junction/symlink must not redirect an approved document elsewhere.
        written = lexical_path(source.parent / candidate)
        shared = any(
            written == report.skills_root / approved
            and resolved == report.skills_root / approved
            and (report.skills_root / Path(approved).parts[0] / "SKILL.md").is_file()
            for approved in report.shared_resource_references.get(skill, ())
        )
        if not shared:
            add_audit_issue(report, "error", "REFERENCE_ESCAPE", skill, source, f"reference escapes skill directory: {raw}")
            return
        if not resolved.is_file():
            add_audit_issue(report, "error", "REFERENCE_MISSING", skill, source, f"referenced file does not exist: {raw}")
            return
    if not resolved.exists():
        add_audit_issue(report, "error", "REFERENCE_MISSING", skill, source, f"referenced file does not exist: {raw}")


FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("SANDBOX_BYPASS", re.compile(r"dangerouslyDisableSandbox|--dangerously-skip-permissions", re.I), "sandbox bypass instruction"),
    ("CLIENT_SCRIPT_PATH", re.compile(r"(?:\.workbuddy|\.codex|\.qoderworkcn)[/\\]skills[/\\][^\s`'\"]+[/\\](?:scripts|references|assets)[/\\]", re.I), "client-specific bundled-resource path"),
    ("DESKTOP_RESOURCE_PATH", re.compile(r"(?:[A-Za-z]:)?[/\\](?:Users[/\\][^/\\]+[/\\])?(?:Desktop|桌面)[/\\]", re.I), "desktop resource path"),
    ("UNSAFE_RM", re.compile(r"\bbash\s+(?:-lc\s+)?['\"]?rm\s+-rf\b", re.I), "unsafe shell deletion instruction"),
)


def source_forbidden_checks(config: dict[str, Any]) -> set[str]:
    policy = config.get("sourceAudit")
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise ConfigError("sourceAudit must be a mapping")
    checks = {"SANDBOX_BYPASS", "UNSAFE_RM"}
    for code, flag in (("DESKTOP_RESOURCE_PATH", "forbidDesktopResourcePaths"),
                       ("CLIENT_SCRIPT_PATH", "forbidClientResourcePaths")):
        enabled = policy.get(flag, False)
        if type(enabled) is not bool:
            raise ConfigError(f"sourceAudit.{flag} must be boolean")
        if enabled:
            checks.add(code)
    return checks


def inspect_imports(script: Path) -> list[tuple[str, str]]:
    """Return (module, severity) for third-party imports.

    Imports at module scope are required. Imports guarded by try/except or
    loaded lazily inside a function are warnings when unavailable.
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8-sig"), filename=str(script))
    except (OSError, SyntaxError, UnicodeError):
        return []
    found: dict[str, str] = {}

    def walk(node: ast.AST, guarded: bool = False, lazy: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            next_guarded = guarded or isinstance(node, ast.Try)
            next_lazy = lazy or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if isinstance(child, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in child.names]
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                names = [child.module.split(".", 1)[0]]
            else:
                names = []
            for name in names:
                severity = "warning" if next_guarded or next_lazy else "error"
                if found.get(name) != "error":
                    found[name] = severity
            walk(child, next_guarded, next_lazy)

    walk(tree)
    local_modules = {p.stem for p in script.parent.glob("*.py")}
    return sorted(
        (name, severity)
        for name, severity in found.items()
        if name not in sys.stdlib_module_names and name not in local_modules
    )


_IMPORT_DISTRIBUTION_ALIASES = {
    "cv2": "opencv-python",
    "docx": "python-docx",
    "fitz": "pymupdf",
    "pil": "pillow",
    "pythoncom": "pywin32",
    "ntsecuritycon": "pywin32",
    "pywintypes": "pywin32",
    "win32api": "pywin32",
    "win32com": "pywin32",
    "win32con": "pywin32",
    "win32process": "pywin32",
    "win32security": "pywin32",
    "yaml": "pyyaml",
}


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def declared_runtime_requirements(skill_root: Path) -> dict[str, str]:
    """Return normalized distribution names declared by one self-contained skill."""

    declared: dict[str, str] = {}
    candidates = sorted(skill_root.glob("requirements*.txt"))
    lock = skill_root / "requirements.lock"
    if _lstat_kind(lock) == "file":
        candidates.append(lock)
    for requirements_path in candidates:
        if _lstat_kind(requirements_path) != "file":
            continue
        try:
            lines = requirements_path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            value = line.split("#", 1)[0].strip()
            if not value or value.startswith(("-", "git+", "http://", "https://")):
                continue
            match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)", value)
            if match:
                raw_name = match.group(1)
                declared[_normalize_distribution_name(raw_name)] = raw_name
    return declared


def declared_distribution_for_import(module: str, declared: dict[str, str]) -> str | None:
    normalized_module = _normalize_distribution_name(module)
    candidate = _IMPORT_DISTRIBUTION_ALIASES.get(normalized_module, normalized_module)
    raw_name = declared.get(_normalize_distribution_name(candidate))
    return raw_name


def audit_python_script(report: AuditReport, skill: str, script: Path) -> None:
    declared = declared_runtime_requirements(report.skills_root / skill)
    for module, severity in inspect_imports(script):
        if declared_distribution_for_import(module, declared) is None:
            add_audit_issue(
                report,
                severity,
                "RUNTIME_DEPENDENCY_UNDECLARED",
                skill,
                script,
                f"Third-party import has no matching skill requirement declaration: {module}",
            )


def run_content_audit(repo_root: Path, config_path: Path | None = None) -> AuditReport:
    config_path = config_path or resolve_config_path(repo_root, None)
    config = read_yaml_simple(config_path)
    checks = source_forbidden_checks(config)
    if config.get("version") == 3:
        _load_v3_model(config)
    skills_dir = str((config.get("repo", {}) or {}).get("skillsRoot") or "skills")
    repo_root = lexical_path(repo_root)
    skills_root = lexical_path(repo_root / skills_dir)
    skills_state = inspect_path(skills_root)
    if (
        not lexical_contains(skills_root, repo_root)
        or lexical_norm_path(skills_root) == lexical_norm_path(repo_root)
        or skills_state.lstat_kind != "directory"
        or skills_state.is_link
        or lexical_norm_path(skills_state.lexical) != lexical_norm_path(skills_state.final)
        or not lexical_contains(skills_state.final, repo_root)
    ):
        raise ConfigError(f"Repo skills root must be a real directory inside the repository: {skills_root}")
    source_skills, discovery_warnings = discover_source_skills(skills_root)
    source_audit = config.get("sourceAudit") or {}
    if not isinstance(source_audit, dict):
        raise ConfigError("sourceAudit must be a mapping")
    references = source_audit.get("sharedResourceReferences", {})
    if not isinstance(references, dict) or any(
        not isinstance(key, str) or not isinstance(values, list)
        or any(not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts for value in values)
        for key, values in references.items()
    ):
        raise ConfigError("sourceAudit.sharedResourceReferences must contain relative resource paths")
    report = AuditReport(repo_root, skills_root, sorted(source_skills), shared_resource_references=references)
    for warning in discovery_warnings:
        add_audit_issue(report, "warning", "SOURCE_DIRECTORY_SKIPPED", "repo", skills_root, warning)

    for skill, skill_root in sorted(source_skills.items()):
        skill_md = skill_root / "SKILL.md"
        try:
            metadata, body = parse_skill_frontmatter(skill_md)
        except ValueError as exc:
            add_audit_issue(report, "error", "FRONTMATTER_INVALID", skill, skill_md, str(exc))
            metadata, body = {}, skill_md.read_text(encoding="utf-8", errors="replace")
        standard_fields = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
        extras = sorted(set(metadata) - standard_fields)
        if extras:
            add_audit_issue(report, "warning", "FRONTMATTER_UNKNOWN_FIELDS", skill, skill_md, f"client/extension fields are retained and should be ignored when unsupported: {', '.join(extras)}")
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not name.strip():
            add_audit_issue(report, "error", "FRONTMATTER_NAME", skill, skill_md, "name must be a non-empty string")
        elif name != skill:
            add_audit_issue(report, "error", "NAME_FOLDER_MISMATCH", skill, skill_md, f"frontmatter name '{name}' must match folder '{skill}'")
        elif len(name) > 63 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            add_audit_issue(report, "error", "SKILL_NAME_INVALID", skill, skill_md, "name must be lowercase hyphen-case and shorter than 64 characters")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            add_audit_issue(report, "error", "FRONTMATTER_DESCRIPTION", skill, skill_md, "description must be a non-empty string of at most 1024 characters")
        if "license" in metadata and (not isinstance(metadata["license"], str) or not metadata["license"].strip()):
            add_audit_issue(report, "error", "FRONTMATTER_LICENSE", skill, skill_md, "license must be a non-empty string when present")
        if "compatibility" in metadata and (
            not isinstance(metadata["compatibility"], str) or not metadata["compatibility"].strip() or len(metadata["compatibility"]) > 500
        ):
            add_audit_issue(report, "error", "FRONTMATTER_COMPATIBILITY", skill, skill_md, "compatibility must be a non-empty string of at most 500 characters")
        if "metadata" in metadata:
            metadata_value = metadata["metadata"]
            if not isinstance(metadata_value, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata_value.items()):
                add_audit_issue(report, "error", "FRONTMATTER_METADATA", skill, skill_md, "metadata must map string keys to string values")
        if "allowed-tools" in metadata and not isinstance(metadata["allowed-tools"], str):
            add_audit_issue(report, "error", "FRONTMATTER_ALLOWED_TOOLS", skill, skill_md, "allowed-tools must be a space-delimited string when present")

        agent_path = skill_root / "agents" / "openai.yaml"
        if agent_path.is_file():
            try:
                agent_data = parse_yaml_mapping(agent_path)
                interface = agent_data.get("interface")
                if not isinstance(interface, dict):
                    raise ValueError("top-level interface mapping is required")
                for key in ("display_name", "short_description", "default_prompt"):
                    if not isinstance(interface.get(key), str) or not interface[key].strip():
                        add_audit_issue(report, "error", "OPENAI_INTERFACE_FIELD", skill, agent_path, f"interface.{key} must be a non-empty string")
                short_description = interface.get("short_description") if isinstance(interface, dict) else None
                if isinstance(short_description, str) and not 25 <= len(short_description.strip()) <= 64:
                    add_audit_issue(report, "error", "OPENAI_SHORT_DESCRIPTION", skill, agent_path, "interface.short_description must be 25-64 characters")
                prompt = interface.get("default_prompt") if isinstance(interface, dict) else None
                if isinstance(prompt, str) and f"${skill}" not in prompt:
                    add_audit_issue(report, "error", "OPENAI_DEFAULT_PROMPT", skill, agent_path, f"interface.default_prompt must mention ${skill}")
                for key in ("icon_small", "icon_large"):
                    icon = interface.get(key) if isinstance(interface, dict) else None
                    if isinstance(icon, str):
                        validate_resource_reference(report, skill, skill_root, agent_path, icon)
            except ValueError as exc:
                add_audit_issue(report, "error", "OPENAI_YAML_INVALID", skill, agent_path, str(exc))

        for reference_source in markdown_resource_sources(skill_root):
            try:
                reference_body = body if reference_source == skill_md else reference_source.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            for raw in referenced_resource_paths(reference_body):
                validate_resource_reference(report, skill, skill_root, reference_source, raw)

        text_files = [skill_md]
        text_files.extend(p for p in skill_root.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".yaml", ".yml", ".py", ".ps1", ".sh"})
        for text_path in sorted(set(text_files)):
            try:
                text = text_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for code, pattern, label in FORBIDDEN_PATTERNS:
                if code in checks and pattern.search(text):
                    add_audit_issue(report, "error", code, skill, text_path, label)

        for script in sorted((skill_root / "scripts").rglob("*.py")) if (skill_root / "scripts").is_dir() else []:
            audit_python_script(report, skill, script)

    return report


def audit_report_dict(report: AuditReport) -> dict[str, Any]:
    return {
        "repoRoot": str(report.repo_root),
        "skillsRoot": str(report.skills_root),
        "skills": report.skills,
        "issues": [
            {"severity": issue.severity, "code": issue.code, "skill": issue.skill, "path": str(issue.path), "message": issue.message}
            for issue in report.issues
        ],
        "errors": len(report.errors),
        "warnings": len(report.warnings),
    }


def print_audit_report(report: AuditReport, fmt: str = "text") -> None:
    if fmt == "json":
        print(json.dumps(audit_report_dict(report), ensure_ascii=False, indent=2))
        return
    print(f"Audit skills ({len(report.skills)}): {', '.join(report.skills)}")
    for issue in report.issues:
        print("  " + issue.line())
    print(f"Audit: errors={len(report.errors)} warnings={len(report.warnings)}")


def resolve_system_python_executable(
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve only the explicit override or ``python`` from PATH.

    The selected interpreter validates itself in ``check_system_python.py``;
    this manager process may be running in its own virtual environment and
    must never substitute ``sys.executable`` for the business runtime.
    """

    environment = os.environ if environ is None else environ
    lookup = shutil.which if which is None else which
    override = environment.get(SYSTEM_PYTHON_ENV, "").strip()
    if override:
        candidate = os.path.expandvars(os.path.expanduser(override.strip('"')))
        resolved = lookup(candidate)
        if resolved is None and Path(candidate).is_file():
            resolved = str(Path(candidate).resolve())
        if resolved is None:
            return None, f"{SYSTEM_PYTHON_ENV} does not resolve to an executable: {override}"
        return str(Path(resolved).resolve()), None

    resolved = lookup("python")
    if resolved is None:
        return None, (
            "system Python was not found on PATH; install Python 3.12 or set "
            f"{SYSTEM_PYTHON_ENV} to its python executable"
        )
    return str(Path(resolved).resolve()), None


def _system_dependency_failure(
    executable: str | None, message: str, returncode: int = 3
) -> SystemDependencyAudit:
    return SystemDependencyAudit(
        selected_executable=executable,
        returncode=returncode,
        payload={
            "executable": executable,
            "version": None,
            "errors": [message],
            "errorCount": 1,
            "packages": 0,
            "imports": 0,
            "scripts": 0,
        },
    )


def run_system_dependency_audit(
    repo_root: Path,
    *,
    strict_versions: bool = False,
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> SystemDependencyAudit:
    """Run the business dependency audit in a clean system-Python process."""

    environment = dict(os.environ if environ is None else environ)
    executable, resolution_error = resolve_system_python_executable(environment, which)
    if resolution_error:
        return _system_dependency_failure(executable, resolution_error)
    assert executable is not None

    checker = lexical_path(repo_root) / "scripts" / "check_system_python.py"
    if _lstat_kind(checker) != "file":
        return _system_dependency_failure(
            executable, f"system Python checker is missing: {checker}"
        )

    # Do not inherit a client/manager Python overlay.  The interpreter and its
    # normal system/user site-packages remain available.
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    run = subprocess.run if runner is None else runner
    command = [
        executable,
        "-B",
        str(checker),
        "--repo-root",
        str(lexical_path(repo_root)),
        "--format",
        "json",
    ]
    if strict_versions:
        command.append("--strict-versions")
    try:
        proc = run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=SYSTEM_PYTHON_CHECK_TIMEOUT,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return _system_dependency_failure(
            executable,
            f"system Python dependency check timed out after {SYSTEM_PYTHON_CHECK_TIMEOUT}s",
        )
    except OSError as exc:
        return _system_dependency_failure(
            executable, f"could not launch system Python dependency check: {exc}"
        )

    try:
        payload = json.loads(proc.stdout)
        if not isinstance(payload, dict):
            raise ValueError("JSON root is not an object")
    except (json.JSONDecodeError, ValueError) as exc:
        detail = (proc.stderr or proc.stdout).strip().replace("\r", " ").replace("\n", " ")
        message = f"system Python checker returned invalid JSON: {exc}"
        if detail:
            message += f"; output: {detail[:500]}"
        return _system_dependency_failure(
            executable, message, proc.returncode if proc.returncode != 0 else 3
        )

    result = SystemDependencyAudit(executable, proc.returncode, payload)
    if proc.returncode == 0 and result.errors:
        return SystemDependencyAudit(executable, 3, payload)
    return result


def print_system_dependency_audit(result: SystemDependencyAudit) -> None:
    payload = result.as_dict()
    print(f"System Python: {payload.get('executable') or result.selected_executable or '<unresolved>'}")
    if payload.get("version"):
        print(f"Python version: {payload['version']}")
    for error in result.errors:
        print(f"[ERROR] {error}")
    version_drifts = payload.get("versionDrifts")
    if not isinstance(version_drifts, list):
        version_drifts = []
    for drift in version_drifts:
        print(f"[INFO] {drift}")
    print(
        f"System dependency check: errors={result.error_count} "
        f"versionDrifts={len(version_drifts)} packages={payload.get('packages', 0)} "
        f"imports={payload.get('imports', 0)} "
        f"scripts={payload.get('scripts', 0)}"
    )


def combined_audit_report_dict(
    report: AuditReport, system_audit: SystemDependencyAudit
) -> dict[str, Any]:
    payload = audit_report_dict(report)
    payload["staticErrors"] = len(report.errors)
    payload["systemPython"] = system_audit.as_dict()
    payload["errors"] = len(report.errors) + system_audit.error_count
    return payload


def is_local_only(name: str, target: TargetConfig) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in target.local_only)


def link_target(path: Path) -> Path | None:
    observed = inspect_path(path)
    return observed.final if observed.is_link else None


def is_link_or_junction(path: Path) -> bool:
    return inspect_path(path).is_link


def git_diff_summary(repo_dir: Path, local_dir: Path) -> str:
    commands = [
        ["git", "--no-pager", "diff", "--no-index", "--stat", "--", str(repo_dir), str(local_dir)],
        ["git", "--no-pager", "diff", "--no-index", "--name-status", "--", str(repo_dir), str(local_dir)],
    ]
    output: list[str] = []
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
            text = (proc.stdout + proc.stderr).strip()
            if text:
                output.append(text)
        except FileNotFoundError:
            output.append("git not available; cannot produce diff summary")
            break
    return "\n".join(output)


def validate_backup_root(plan: Plan, target: TargetConfig) -> None:
    # Retained as a public compatibility helper. Full pairwise validation is
    # performed by validate_path_topology.
    source = inspect_path(plan.skills_root)
    backup = inspect_path(target.backup_root)
    root = inspect_path(target.root)
    if paths_overlap(backup, root) or paths_overlap(backup, source):
        plan.actions.append(Action("BACKUP_ROOT_UNSAFE", target.name, target.backup_root, message="backup root overlaps an active source/consumer root", blocking=True))


def _configured_state(path: Path, raw: str) -> PathState:
    state = inspect_path(path)
    return PathState(raw or str(path), state.lexical, state.lstat_kind, state.final, state.chain, state.broken, state.loop)


def validate_path_topology(plan: Plan, targets: list[TargetConfig]) -> None:
    roots: list[tuple[str, str, PathState, Path]] = [
        ("source", "repo", _configured_state(plan.skills_root, str(plan.skills_root)), plan.skills_root),
    ]
    source_state = roots[0][2]
    if source_state.lstat_kind != "directory" or source_state.is_link or source_state.broken or source_state.loop:
        plan.actions.append(Action(
            "SOURCE_ROOT_UNSAFE", "repo", plan.skills_root,
            message=f"source skills root must be a real directory (lstat={source_state.lstat_kind})",
            blocking=True,
        ))
    elif lexical_norm_path(source_state.lexical) != lexical_norm_path(source_state.final):
        plan.actions.append(Action(
            "PATH_ANCESTOR_LINK", "repo", plan.skills_root,
            message=f"source path traverses a linked/reparse ancestor (lexical={source_state.lexical}, final={source_state.final})",
            blocking=True,
        ))
    for target in targets:
        target_state = _configured_state(target.root, target.root_raw)
        backup_state = _configured_state(target.backup_root, target.backup_root_raw)
        roots.extend([
            ("consumer", target.name, target_state, target.root),
            ("backup", target.name, backup_state, target.backup_root),
        ])
        if backup_state.is_link or backup_state.broken or backup_state.loop:
            plan.actions.append(Action(
                "BACKUP_ROOT_UNSAFE", target.name, target.backup_root,
                message=f"backup root may not be a link/reparse path (lstat={backup_state.lstat_kind})",
                blocking=True,
            ))
        for kind_name, observed, path in (("consumer", target_state, target.root), ("backup", backup_state, target.backup_root)):
            if not observed.is_link and lexical_norm_path(observed.lexical) != lexical_norm_path(observed.final):
                plan.actions.append(Action(
                    "PATH_ANCESTOR_LINK", target.name, path,
                    message=f"{kind_name} path traverses a linked/reparse ancestor (lexical={observed.lexical}, final={observed.final})",
                    blocking=True,
                ))
    for index, (left_kind, left_name, left, left_path) in enumerate(roots):
        for right_kind, right_name, right, right_path in roots[index + 1:]:
            if paths_overlap(left, right):
                plan.actions.append(Action(
                    "PATH_OVERLAP", f"{left_name},{right_name}", left_path, right_path,
                    message=(
                        f"{left_kind} root overlaps {right_kind} root; "
                        f"left(raw={left.raw}, lexical={left.lexical}, final={left.final}) "
                        f"right(raw={right.raw}, lexical={right.lexical}, final={right.final})"
                    ),
                    blocking=True,
                ))


def _topology_target_union(
    selected_targets: list[TargetConfig], configured_targets: list[TargetConfig]
) -> list[TargetConfig]:
    """Keep selected policy first while retaining every other physical root."""
    result: list[TargetConfig] = []
    seen: set[str] = set()
    for target in (*selected_targets, *configured_targets):
        identity = lexical_norm_path(target.root)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(target)
    return result


def _valid_skill_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)) and len(name) <= 63


def _v3_deletion_inventory(
    config: dict[str, Any],
    model: ClientModel,
    detections: dict[str, ConsumerDetection],
    options: RuntimeOptions,
    manifest: dict[str, Any],
) -> list[DeletionInventoryRoot]:
    """Return every declared physical root without creating or managing it."""
    by_root: dict[str, DeletionInventoryRoot] = {}
    raw_targets = config.get("targets", {}) or {}
    for mount in sorted(model.mount_points.values(), key=lambda item: item.id):
        key = lexical_norm_path(mount.root)
        inventory = by_root.get(key)
        if inventory is None:
            inventory = DeletionInventoryRoot(mount.id, lexical_path(mount.root))
            by_root[key] = inventory
        if mount.id not in inventory.mount_point_ids:
            inventory.mount_point_ids.append(mount.id)
        if mount.id not in inventory.manifest_target_ids:
            inventory.manifest_target_ids.append(mount.id)

        for adapter in model.adapters.values():
            if mount.id not in adapter.mount_point_ids:
                continue
            if adapter.id not in inventory.adapter_ids:
                inventory.adapter_ids.append(adapter.id)
            consumer = model.consumers[adapter.consumer_detection]
            detection = detections[adapter.consumer_detection]
            eligible = (
                mount.source_selection == "all"
                and adapter.management_status == "confirmed"
                and consumer.status == "confirmed"
                and detection.state == "installed"
            )
            if eligible and adapter.id not in inventory.eligible_adapter_ids:
                inventory.eligible_adapter_ids.append(adapter.id)

        if isinstance(raw_targets, dict):
            for target_id, raw in raw_targets.items():
                if not isinstance(raw, dict) or not raw.get("root"):
                    continue
                expanded = lexical_path(
                    expand_value(str(raw["root"]), options.repo_root, options.config_path.parent)
                )
                if lexical_norm_path(expanded) == key and str(target_id) not in inventory.manifest_target_ids:
                    inventory.manifest_target_ids.append(str(target_id))

    # State may outlive a renamed or removed registry entry. Historical roots
    # remain deletion inventory, but deliberately gain no management adapter:
    # links found there require explicit cleanup before source deletion.
    manifest_consumers = manifest.get("consumers", {})
    if isinstance(manifest_consumers, dict):
        for target_id, target_state in manifest_consumers.items():
            if not isinstance(target_state, dict) or not target_state.get("root"):
                continue
            root = lexical_path(Path(str(target_state["root"])))
            key = lexical_norm_path(root)
            inventory = by_root.get(key)
            if inventory is None:
                inventory = DeletionInventoryRoot(str(target_id), root)
                by_root[key] = inventory
            if str(target_id) not in inventory.manifest_target_ids:
                inventory.manifest_target_ids.append(str(target_id))
    return list(by_root.values())


def _link_state_reaches_source(observed: PathState, source: Path) -> bool:
    if not observed.is_link:
        return False
    source_key = lexical_norm_path(source)
    return (
        lexical_norm_path(observed.final) == source_key
        or any(lexical_norm_path(destination) == source_key for destination in observed.chain)
    )


def _owned_inventory_consumer(
    manifest: dict[str, Any],
    inventory: DeletionInventoryRoot,
    skill_name: str,
    path: Path,
    source: Path,
) -> str | None:
    for target_id in inventory.manifest_target_ids:
        registered = manifest_consumer(manifest, target_id, skill_name)
        if not isinstance(registered, dict) or registered.get("deploymentOwned") is not True:
            continue
        if (
            lexical_norm_path(Path(str(registered.get("path") or "."))) == lexical_norm_path(path)
            and lexical_norm_path(Path(str(registered.get("source") or "."))) == lexical_norm_path(source)
        ):
            return target_id
    return None


def _inventory_root_safety_reason(plan: Plan, root: Path, source: Path) -> str | None:
    root = lexical_path(root)
    anchor = lexical_path(Path(root.anchor))
    if lexical_norm_path(root) == lexical_norm_path(anchor):
        return "filesystem anchor"
    protected = (
        ("HOME", lexical_path(Path.home())),
        ("repository", lexical_path(plan.repo_root)),
        ("deleted source", lexical_path(source)),
    )
    for label, path in protected:
        if lexical_contains(path, root):
            return f"ancestor of or identical to {label}"
    return None


def _plan_v3_inventory_delete(
    plan: Plan,
    inventory_roots: list[DeletionInventoryRoot],
    skill_name: str,
    source: Path,
) -> None:
    for inventory in inventory_roots:
        unsafe_reason = _inventory_root_safety_reason(plan, inventory.root, source)
        if unsafe_reason:
            plan.actions.append(Action(
                "GLOBAL_DELETE_INVENTORY_UNSAFE_ROOT",
                inventory.name,
                inventory.root,
                source,
                message=f"declared deletion inventory root is a broad {unsafe_reason}; preserving source",
                blocking=True,
                destructive=True,
                data={
                    "skill": skill_name,
                    "inventoryRoot": str(inventory.root),
                    "mountPointIds": list(inventory.mount_point_ids),
                },
            ))
            continue
        root_state = inspect_path(inventory.root)
        scan_data = {
            "skill": skill_name,
            "inventoryRoot": str(inventory.root),
            "mountPointIds": list(inventory.mount_point_ids),
        }
        if _link_state_reaches_source(root_state, source):
            plan.actions.append(Action(
                "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP",
                inventory.name,
                inventory.root,
                source,
                message="declared mount root itself links to the deleted skill source; preserving source",
                blocking=True,
                destructive=True,
                data=scan_data | {
                    "adapterIds": list(inventory.adapter_ids),
                    "eligibleAdapterIds": list(inventory.eligible_adapter_ids),
                    "rootPointsToSource": True,
                },
            ))
            continue
        if root_state.is_link and not root_state.broken and not root_state.loop:
            backing_unsafe = _inventory_root_safety_reason(plan, root_state.final, source)
            # A discovery root backed by the overall skillsRoot is not itself a
            # consumer for one skill. It remains safe to inspect one level.
            if (
                backing_unsafe
                and lexical_norm_path(root_state.final) != lexical_norm_path(plan.skills_root)
            ):
                plan.actions.append(Action(
                    "GLOBAL_DELETE_INVENTORY_UNSAFE_ROOT",
                    inventory.name,
                    inventory.root,
                    source,
                    message=f"linked deletion inventory backing is a broad {backing_unsafe}; preserving source",
                    blocking=True,
                    destructive=True,
                    data=scan_data | {"linkedBacking": str(root_state.final)},
                ))
                continue
        plan.actions.append(Action(
            "GLOBAL_DELETE_INVENTORY_SCAN",
            inventory.name,
            inventory.root,
            message="seal declared deletion inventory root using an immediate lstat listing",
            data=scan_data | {"view": "lexical"},
        ))
        if root_state.is_link and not root_state.broken and not root_state.loop:
            final_kind = _lstat_kind(root_state.final)
            if final_kind == "directory":
                plan.actions.append(Action(
                    "GLOBAL_DELETE_INVENTORY_SCAN",
                    inventory.name,
                    root_state.final,
                    message="seal linked inventory backing using an immediate lstat listing",
                    data=scan_data | {"view": "linked-backing"},
                ))
        if root_state.lstat_kind == "directory" or (
            root_state.is_link and not root_state.broken and not root_state.loop
        ):
            try:
                entries = sorted(inventory.root.iterdir(), key=lambda path: path.name.lower())
            except (FileNotFoundError, NotADirectoryError):
                entries = []
            except OSError as exc:
                plan.actions.append(Action(
                    "GLOBAL_DELETE_INVENTORY_UNREADABLE",
                    inventory.name,
                    inventory.root,
                    source,
                    message=f"cannot enumerate declared mount root: {exc}; preserving source",
                    blocking=True,
                    destructive=True,
                    data={"skill": skill_name, "mountPointIds": list(inventory.mount_point_ids)},
                ))
                continue
        else:
            entries = []
        root_is_safe = (
            root_state.lstat_kind == "directory"
            and not root_state.is_link
            and lexical_norm_path(root_state.lexical) == lexical_norm_path(root_state.final)
        )
        for entry in entries:
            observed = inspect_path(entry)
            if not _link_state_reaches_source(observed, source):
                continue
            correct_name = entry.name == skill_name
            healthy_direct = direct_link_to(entry, source)
            manifest_target = _owned_inventory_consumer(
                plan.manifest, inventory, skill_name, entry, source
            ) if correct_name else None
            eligible = bool(inventory.eligible_adapter_ids)
            if root_is_safe and eligible and correct_name and healthy_direct and manifest_target:
                plan.actions.append(Action(
                    "GLOBAL_DELETE_CONSUMER",
                    manifest_target,
                    entry,
                    source,
                    message=f"remove eligible managed consumer before deleting {skill_name}",
                    destructive=True,
                    data={
                        "skill": skill_name,
                        "inventoryRoot": str(inventory.root),
                        "mountPointIds": list(inventory.mount_point_ids),
                        "adapterIds": list(inventory.adapter_ids),
                        "eligibleAdapterIds": list(inventory.eligible_adapter_ids),
                    },
                ))
                continue
            reasons: list[str] = []
            if not root_is_safe:
                reasons.append("inventory root is linked or traverses an alias")
            if not eligible:
                reasons.append("no confirmed installed adapter may manage this root")
            if not correct_name:
                reasons.append(f"wrong consumer name {entry.name!r}")
            if not healthy_direct:
                reasons.append("link is multi-hop or aliases the source")
            if manifest_target is None:
                reasons.append("deployment is not manifest-owned at the expected name")
            plan.actions.append(Action(
                "GLOBAL_DELETE_CONSUMER_REQUIRES_CLEANUP",
                inventory.name,
                entry,
                source,
                message="; ".join(reasons) + "; preserving source",
                blocking=True,
                destructive=True,
                data={
                    "skill": skill_name,
                    "inventoryRoot": str(inventory.root),
                    "mountPointIds": list(inventory.mount_point_ids),
                    "adapterIds": list(inventory.adapter_ids),
                    "eligibleAdapterIds": list(inventory.eligible_adapter_ids),
                },
            ))


def plan_discovery_manifest(plan: Plan, target: TargetConfig) -> None:
    path = target.discovery_manifest
    root = plan.skills_root
    content = {"entries": []}
    exists = _lstat_kind(path) != "missing"
    try:
        if exists:
            if _lstat_kind(path) != "file":
                raise ValueError("discovery manifest must be a regular file")
            content = strict_json(path.read_text(encoding="utf-8-sig"))
        entries = content.get("entries")
        if not isinstance(entries, list):
            raise ValueError("discovery entries must be a list")
        matches = 0
        scanned = set()
        own_names = {name.casefold() for name in plan.source_skills}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not Path(entry["path"]).is_absolute():
                raise ValueError("discovery entry requires an absolute source path")
            other = Path(entry["path"])
            key = norm_path(other)
            if key == norm_path(root):
                matches += 1
                continue
            if key in scanned:
                continue
            scanned.add(key)
            plan.actions.append(Action("DISCOVERY_SOURCE_SCAN", target.name, source=other,
                                       message="seal other source content for shared discovery"))
            directories = other.iterdir() if other.is_dir() else ()
            for directory in directories:
                if not directory.is_dir() or not (directory / "SKILL.md").is_file():
                    continue
                name = directory.name
                metadata, _ = parse_skill_frontmatter(directory / "SKILL.md")
                declared = str(metadata.get("name", name))
                if {name.casefold(), declared.casefold()} & own_names:
                    plan.actions.append(Action("DISCOVERY_SKILL_NAME_CONFLICT", target.name, directory,
                                               message="another discovered source publishes the same Skill name", blocking=True))
        if matches > 1:
            raise ValueError("multiple discovery entries identify this source")
        if matches == 1:
            plan.actions.append(Action("OK_DISCOVERY_MANIFEST", target.name, path,
                                       message="this source is already registered; preserving all entries"))
            return
        updated = dict(content)
        updated["entries"] = [*entries, {"path": str(root).replace("\\", "/")}]
        plan.actions.append(Action("UPDATE_DISCOVERY_MANIFEST" if exists else "CREATE_DISCOVERY_MANIFEST",
                                   target.name, path, root, message="register this source and preserve other discovery entries",
                                   data={"content": updated}))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        plan.actions.append(Action("DISCOVERY_MANIFEST_CONFLICT", target.name, path,
                                   message=f"discovery manifest needs review ({type(exc).__name__})", blocking=True))


def restore_discovery_manifest(path: Path, before: bytes | None, after: bytes) -> None:
    if not path.is_file() or path.read_bytes() != after:
        raise TransactionError("Discovery file changed outside the transaction; retained journal requires review")
    if before is None:
        path.unlink()
    else:
        atomic_write_bytes(path, before)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_plan(options: RuntimeOptions, target_sets: list[str] | None = None, all_targets: bool = False) -> Plan:
    config, targets = load_target_configs(options, target_sets, all_targets)
    skills_root = lexical_path(str(config["__skills_root__"]))
    source_skills, warnings = discover_source_skills(skills_root)
    manifest = load_manifest(options.repo_root)
    client_model = config.get(V3_MODEL_SENTINEL)
    semantic_summary = config.get(V3_SEMANTICS_SENTINEL)
    if not isinstance(semantic_summary, dict):
        semantic_summary = {
            "validatedConfig": _canonical_value({
                key: value for key, value in config.items()
                if not str(key).startswith("__")
            })
        }
    plan = Plan(
        options.repo_root,
        skills_root,
        targets,
        source_skills,
        warnings,
        manifest=manifest,
        semantic_summary=semantic_summary,
        selection_intent=config.get(V3_SELECTION_SENTINEL) or _selection_intent(options, target_sets, all_targets),
        implementation_digest=manager_implementation_digest(),
    )
    client_detections = config.get(V3_DETECTIONS_SENTINEL)
    for target in targets:
        if target.discovery_manifest is not None:
            plan_discovery_manifest(plan, target)
    if isinstance(client_model, ClientModel):
        for target in targets:
            for adapter_id in target.adapter_ids:
                policy = client_model.adapters[adapter_id].runtime_binding
                if policy is None:
                    continue
                try:
                    snapshot = inspect_adapter_runtime_binding(client_model, adapter_id, target.root)
                    target.runtime_bindings[adapter_id] = snapshot
                    plan.actions.append(Action(
                        "CLIENT_RUNTIME_BINDING", adapter_id,
                        message="runtime identity bound to this plan; native loading is checked separately",
                        data={"binding": snapshot, "inputPaths": [str(path) for path in binding_input_paths(policy, snapshot)]},
                    ))
                except RuntimeBindingError as exc:
                    plan.actions.append(Action("CLIENT_RUNTIME_BINDING_INVALID", adapter_id, message=str(exc), blocking=True))
    if isinstance(client_model, ClientModel) and isinstance(client_detections, dict):
        adapters_by_consumer: dict[str, list[str]] = {}
        for adapter in client_model.adapters.values():
            for consumer_id in adapter.consumer_ids:
                adapters_by_consumer.setdefault(consumer_id, []).append(adapter.id)
        for consumer in client_model.consumers.values():
            detection = client_detections[consumer.id]
            adapter_ids = adapters_by_consumer.get(consumer.id, [])
            management = [client_model.adapters[item].management_status for item in adapter_ids]
            kind = {
                "installed": "CONSUMER_INSTALLED",
                "not_installed": "CONSUMER_NOT_INSTALLED",
                "ambiguous": "CONSUMER_AMBIGUOUS",
            }[detection.state]
            suffix = ""
            if detection.state != "installed":
                suffix = "; report only; discovery roots will not be created"
            elif not any(status == "confirmed" for status in management):
                suffix = "; adapter management is report-only; no deployment action"
            plan.actions.append(Action(
                kind,
                consumer.id,
                message=f"{consumer.label}: {detection.details}{suffix}",
                data={
                    "consumer": consumer.id,
                    "state": detection.state,
                    "consumerStatus": consumer.status,
                    "adapterIds": adapter_ids,
                    "managementStatuses": management,
                    "commandHits": list(detection.command_hits),
                    "pathHits": [str(path) for path in detection.path_hits],
                    "nativeVerification": consumer.verification.status,
                },
            ))

    # Topology is global policy, even when a mount operation selects one
    # consumer. A dangerous unselected overlap must not become a side door.
    _, configured_targets = load_target_configs(options, None, True)
    is_v3 = isinstance(client_model, ClientModel) and isinstance(client_detections, dict)
    deletion_inventory = (
        _v3_deletion_inventory(config, client_model, client_detections, options, manifest)
        if is_v3 else []
    )
    if options.delete_skills and not is_v3:
        known_plan_targets = {target.name for target in plan.targets}
        plan.targets.extend(target for target in configured_targets if target.name not in known_plan_targets)
    # Explicit compatibility/private mounts are intentionally absent from the
    # all/default management request. They still need topology validation when
    # selected, alongside every globally manageable physical root.
    topology_targets = _topology_target_union(plan.targets, configured_targets)
    validate_path_topology(plan, topology_targets)
    for skill_name, source in source_skills.items():
        source_state = inspect_path(source)
        if source_state.lstat_kind != "directory" or source_state.is_link:
            plan.actions.append(Action(
                "SOURCE_SKILL_UNSAFE", "repo", source,
                message=f"source skill '{skill_name}' must be a real directory (lstat={source_state.lstat_kind})",
                blocking=True,
            ))

    pending_source_deletes: list[Action] = []
    for skill_name in options.delete_skills:
        if not _valid_skill_name(skill_name):
            plan.actions.append(Action("GLOBAL_DELETE_INVALID", "repo", skills_root / skill_name, message=f"invalid skill name: {skill_name}", blocking=True, destructive=True))
        elif not options.confirm_global_delete:
            plan.actions.append(Action("GLOBAL_DELETE_NEEDS_CONFIRM", "repo", skills_root / skill_name, message="global delete requires --confirm-global-delete", blocking=True, destructive=True))
        elif skill_name in source_skills:
            source = source_skills[skill_name]
            if is_v3:
                _plan_v3_inventory_delete(plan, deletion_inventory, skill_name, source)
            else:
                for target in configured_targets:
                    consumer_path = target.root / skill_name
                    observed = inspect_path(consumer_path)
                    registered = manifest_consumer(manifest, target.name, skill_name)
                    owned = registered is not None and registered.get("deploymentOwned") is True
                    if observed.lstat_kind == "missing":
                        if owned:
                            plan.actions.append(Action(
                                "GLOBAL_DELETE_CONSUMER", target.name, consumer_path, source,
                                message=f"forget missing managed consumer before deleting {skill_name}",
                                destructive=True, data={"skill": skill_name, "missing": True},
                            ))
                        continue
                    if not owned:
                        plan.actions.append(Action(
                            "GLOBAL_DELETE_UNREGISTERED_CONSUMER", target.name, consumer_path, source,
                            message="unregistered content shadows a globally deleted skill; preserving",
                            blocking=True, destructive=True, data={"skill": skill_name},
                        ))
                    elif not direct_link_to(consumer_path, source):
                        plan.actions.append(Action(
                            "GLOBAL_DELETE_CONSUMER_UNSAFE", target.name, consumer_path, source,
                            message="registered consumer is not the expected direct link; refusing removal",
                            blocking=True, destructive=True, data={"skill": skill_name},
                        ))
                    else:
                        plan.actions.append(Action(
                            "GLOBAL_DELETE_CONSUMER", target.name, consumer_path, source,
                            message=f"remove managed consumer before deleting {skill_name}",
                            destructive=True, data={"skill": skill_name},
                        ))
            pending_source_deletes.append(Action(
                "GLOBAL_DELETE_REPO_SKILL", "repo", source,
                message=f"move repo skill to the retained recovery bundle last: {skill_name}", destructive=True,
                data={"skill": skill_name},
            ))
        else:
            plan.warnings.append(f"Repo skill requested for deletion not found: {skill_name}")

    effective_source = {k: v for k, v in source_skills.items() if k not in options.delete_skills}

    for target in targets:
        plan.actions.append(Action("PREFLIGHT", target.name, target.root, message=f"target root for {target.label}"))
        root_state = inspect_path(target.root)
        if root_state.is_link:
            if options.replace_root_link:
                plan.actions.append(Action("REPLACE_TARGET_ROOT_LINK", target.name, target.root, message="replace target root link transactionally; backing directory remains untouched", destructive=True))
            else:
                detail = "broken " if root_state.broken else ""
                plan.actions.append(Action("TARGET_ROOT_LINK", target.name, target.root, message=f"target root is a {detail}{root_state.lstat_kind}; use --replace-root-link to replace", blocking=True))
        elif root_state.lstat_kind not in {"missing", "directory"}:
            if options.replace_files:
                plan.actions.append(Action("MOVE_FILE_TO_BACKUP", target.name, target.root, message="move target root file to backup", destructive=True))
                plan.actions.append(Action(
                    "CREATE_TARGET_ROOT",
                    target.name,
                    target.root,
                    message="create target root after transactional file backup",
                    data={"postRootFileReplacement": True},
                ))
            else:
                plan.actions.append(Action("TARGET_ROOT_FILE", target.name, target.root, message="target root is a file; use --replace-files", blocking=True))
        elif root_state.lstat_kind == "missing":
            plan.actions.append(Action("CREATE_TARGET_ROOT", target.name, target.root, message="create target skills root"))

        entries = list(target.root.iterdir()) if root_state.lstat_kind == "directory" else []
        accounted_names: set[str] = set()
        for entry in sorted(entries, key=lambda p: p.name.lower()):
            if entry.name in options.delete_skills:
                accounted_names.add(entry.name)
                continue
            if entry.name == "_backups" or ".backup-" in entry.name:
                plan.actions.append(Action("PRESERVE_BACKUP_LIKE_ENTRY", target.name, entry, message="unregistered backup-like entry inside active root; preserving"))
                continue
            entry_state = inspect_path(entry)
            entry_link_target = link_target(entry)
            if entry_link_target is not None:
                accounted_names.add(entry.name)
                expected = effective_source.get(entry.name)
                registered = manifest_consumer(manifest, target.name, entry.name)
                if entry_state.broken or entry_state.loop:
                    kind = "LINK_LOOP" if entry_state.loop else "BROKEN_LINK"
                    owned = registered is not None and registered.get("deploymentOwned") is True
                    if options.replace_wrong_links and expected is not None:
                        plan.actions.append(Action(
                            "REPLACE_WRONG_LINK",
                            target.name,
                            entry,
                            expected,
                            message=f"replace {kind.lower().replace('_', ' ')} with the expected direct source link",
                            destructive=True,
                            data={"skill": entry.name},
                        ))
                    else:
                        plan.actions.append(Action(
                            kind, target.name, entry, entry_link_target,
                            message=(f"{kind.lower().replace('_', ' ')} detected; " + ("managed source is missing" if owned else "preserving")),
                            blocking=expected is not None or owned,
                        ))
                elif entry_state.is_link and not entry_state.direct:
                    external_unmanaged = (
                        expected is None
                        and registered is None
                        and not manifest_tracks_consumer_path(manifest, entry)
                        and entry_state.final.is_dir()
                        and all(
                            not lexical_contains(hop, plan.repo_root)
                            and not is_relative_to(hop, plan.repo_root)
                            for hop in (*entry_state.chain, entry_state.final)
                        )
                    )
                    if external_unmanaged:
                        plan.actions.append(Action(
                            "PRESERVE_UNMANAGED_LINK", target.name, entry, entry_link_target,
                            message="healthy external multi-hop link is not managed by this repository; preserving",
                        ))
                    elif options.replace_wrong_links and expected is not None:
                        plan.actions.append(Action(
                            "REPLACE_WRONG_LINK", target.name, entry, expected,
                            message="normalize multi-hop consumer to expected direct link",
                            destructive=True, data={"skill": entry.name},
                        ))
                    else:
                        plan.actions.append(Action(
                            "MULTI_HOP_LINK", target.name, entry, entry_link_target,
                            message="consumer must link directly to its source skill; use --replace-wrong-links to normalize", blocking=True,
                        ))
                elif expected is not None and direct_link_to(entry, expected):
                    if entry.name != expected.name:
                        plan.actions.append(Action("WRONG_NAME_MAPPING", target.name, entry, expected, message="consumer name does not match source folder", blocking=True))
                    elif consumer_is_verified(manifest, target, entry.name, entry, expected):
                        plan.actions.append(Action("OK_LINK", target.name, entry, expected, message="managed direct link and native consumer verification ok", data={"skill": entry.name}))
                    elif registered is not None and registered.get("deploymentOwned") is True:
                        plan.actions.append(Action(
                            "CONSUMER_NATIVE_UNVERIFIED", target.name, entry, expected,
                            message="deployment is managed and direct, but the consumer native probe is not verified",
                            blocking=not isinstance(client_model, ClientModel), data={"skill": entry.name},
                        ))
                    elif options.adopt_existing:
                        plan.actions.append(Action("VERIFY_CONSUMER", target.name, entry, expected, message="explicitly adopt healthy direct deployment; native consumer verification remains required", data={"skill": entry.name}))
                    else:
                        plan.actions.append(Action("UNREGISTERED_EXPECTED_LINK", target.name, entry, expected, message="healthy direct link is not registered; preserving (use --adopt-existing explicitly)", blocking=True))
                elif lexical_contains(entry_link_target, plan.skills_root):
                    mapped_name = entry_link_target.name
                    if mapped_name != entry.name:
                        if options.replace_wrong_links and expected is not None:
                            plan.actions.append(Action("REPLACE_WRONG_LINK", target.name, entry, expected, message=f"repair wrong name mapping '{entry.name}' -> '{mapped_name}'", destructive=True, data={"skill": entry.name}))
                        else:
                            plan.actions.append(Action("WRONG_NAME_MAPPING", target.name, entry, entry_link_target, message=f"consumer '{entry.name}' points to source '{mapped_name}'", blocking=True))
                    elif _lstat_kind(entry_link_target) == "missing":
                        if registered and registered.get("deploymentOwned") is True:
                            plan.actions.append(Action("REMOVE_STALE_REPO_LINK", target.name, entry, entry_link_target, message="remove registered stale repo link", destructive=True, data={"skill": entry.name}))
                        else:
                            plan.actions.append(Action("PRESERVE_UNREGISTERED_LINK", target.name, entry, entry_link_target, message="unregistered stale link preserved"))
                    elif expected is not None:
                        if options.replace_wrong_links:
                            plan.actions.append(Action("REPLACE_WRONG_LINK", target.name, entry, expected, message="replace repo skill link pointing to wrong source", destructive=True, data={"skill": entry.name}))
                        else:
                            plan.actions.append(Action("WRONG_REPO_LINK", target.name, entry, entry_link_target, message="repo skill link points elsewhere; use --replace-wrong-links", blocking=True))
                    else:
                        plan.actions.append(Action("PRESERVE_UNREGISTERED_LINK", target.name, entry, entry_link_target, message="unregistered repo link preserved"))
                else:
                    plan.actions.append(Action("SKILL_SOURCE_CONFLICT" if expected is not None else "PRESERVE_UNMANAGED_LINK",
                                               target.name, entry, entry_link_target,
                                               message="same-name Skill belongs to another source" if expected is not None else "link does not point into this repo; preserving",
                                               blocking=expected is not None))
                continue

            if entry_state.lstat_kind == "directory" and (entry / "SKILL.md").is_file():
                accounted_names.add(entry.name)
                if is_local_only(entry.name, target):
                    plan.actions.append(Action("PRESERVE_LOCAL_ONLY", target.name, entry, message="local-only skill pattern matched; preserving"))
                    continue
                repo_skill = effective_source.get(entry.name)
                if target.import_local:
                    if repo_skill is None:
                        plan.actions.append(Action("IMPORT_LOCAL_SKILL", target.name, entry, plan.skills_root / entry.name, message="copy local skill into repo, backup local dir, then mount", destructive=True, data={"skill": entry.name}))
                    else:
                        diff = git_diff_summary(repo_skill, entry)
                        plan.actions.append(Action("REPORT_IMPORT_CONFLICT", target.name, entry, repo_skill, message=f"local real skill conflicts with repo skill '{entry.name}'\n{diff}", blocking=True))
                elif repo_skill is not None and options.replace_real_dirs:
                    plan.actions.append(Action("MOVE_REAL_DIR_TO_BACKUP", target.name, entry, repo_skill, message="explicitly move real skill to backup before mount", destructive=True, data={"skill": entry.name}))
                else:
                    plan.actions.append(Action("PRESERVE_LOCAL_REAL_SKILL", target.name, entry, message="local real skill preserved; importLocal=false"))
                continue

            if entry_state.lstat_kind == "directory":
                plan.actions.append(Action("PRESERVE_NON_SKILL_DIR", target.name, entry, message="directory without SKILL.md; preserving"))
            elif entry_state.lstat_kind == "file":
                plan.actions.append(Action("PRESERVE_FILE", target.name, entry, message="file in target root; preserving"))
            else:
                plan.actions.append(Action("PRESERVE_SPECIAL_ENTRY", target.name, entry, message=f"special entry ({entry_state.lstat_kind}) preserved"))

        for skill_name, source in effective_source.items():
            if skill_name in target.exclude:
                plan.actions.append(Action("EXCLUDED", target.name, target.root / skill_name, source, message="excluded by target config"))
                continue
            target_path = target.root / skill_name
            if skill_name in accounted_names:
                continue
            if root_state.is_link and options.replace_root_link:
                plan.actions.append(Action(
                    "CREATE_LINK", target.name, target_path, source,
                    message="mount repo skill into transactionally replaced root",
                    data={"skill": skill_name, "postRootReplacement": True},
                ))
                continue
            target_state = inspect_path(target_path)
            if target_state.lstat_kind != "missing":
                current_target = link_target(target_path)
                if current_target is not None:
                    if direct_link_to(target_path, source):
                        continue
                    if is_relative_to(current_target, plan.skills_root):
                        if options.replace_wrong_links:
                            plan.actions.append(Action("REPLACE_WRONG_LINK", target.name, target_path, source, message="replace wrong repo link", destructive=True))
                        else:
                            plan.actions.append(Action("WRONG_LINK_BLOCK", target.name, target_path, current_target, message="existing link points elsewhere", blocking=True))
                    else:
                        plan.actions.append(Action("UNMANAGED_LINK_SHADOWS_SKILL", target.name, target_path, current_target, message="unmanaged link has same name as repo skill; preserving", blocking=True))
                elif target_path.is_dir():
                    if is_local_only(skill_name, target):
                        plan.actions.append(Action("LOCAL_ONLY_SHADOWS_REPO", target.name, target_path, source, message="local-only real dir shadows repo skill; preserving", blocking=True))
                    elif options.replace_real_dirs:
                        plan.actions.append(Action("MOVE_REAL_DIR_TO_BACKUP", target.name, target_path, source, message="move real dir to backup before mount", destructive=True))
                    else:
                        plan.actions.append(Action("REAL_DIR_BLOCK", target.name, target_path, source, message="real directory blocks mount; use import/merge or --replace-real-dirs", blocking=True))
                elif target_path.is_file():
                    if options.replace_files:
                        plan.actions.append(Action("MOVE_FILE_TO_BACKUP", target.name, target_path, source, message="move file before mount", destructive=True))
                    else:
                        plan.actions.append(Action("FILE_BLOCK", target.name, target_path, source, message="file blocks mount; use --replace-files", blocking=True))
            else:
                plan.actions.append(Action("CREATE_LINK", target.name, target_path, source, message="mount repo skill into target", data={"skill": skill_name}))

        if options.prune_backups:
            plan.actions.append(Action(
                "PRUNE_BACKUPS_DISABLED", target.name, target.backup_root,
                message="automatic backup pruning is disabled in the P0 transactional core; registered backups are preserved",
                blocking=True, destructive=True,
            ))

        for legacy in target.legacy_cleanup:
            if not legacy.get("enabled", True):
                continue
            raw_path = legacy.get("path")
            if not raw_path:
                continue
            legacy_path = lexical_path(expand_value(str(raw_path), options.repo_root, options.config_path.parent))
            if _lstat_kind(legacy_path) != "missing":
                plan.actions.append(Action("LEGACY_CLEANUP", target.name, legacy_path, message=str(legacy.get("label") or "legacy entry"), destructive=True))

    # Source deletion is terminal. Consumers are removed first; source content
    # then moves atomically into the retained, ignored recovery bundle.
    plan.actions.extend(pending_source_deletes)
    seal_plan(plan)
    return plan


def print_plan(plan: Plan, fmt: str = "text") -> None:
    if fmt == "json":
        print(json.dumps({
            "planId": plan.plan_id,
            "repoRoot": str(plan.repo_root),
            "skillsRoot": str(plan.skills_root),
            "sourceSkills": sorted(plan.source_skills),
            "warnings": plan.warnings,
            "actions": [a.__dict__ | {"path": str(a.path) if a.path else None, "source": str(a.source) if a.source else None} for a in plan.actions],
            "blockers": len(plan.blockers),
        }, ensure_ascii=False, indent=2))
        return
    print(f"Repository: {plan.repo_root}")
    print(f"Plan ID: {plan.plan_id}")
    print(f"Skills root: {plan.skills_root}")
    print(f"Source skills ({len(plan.source_skills)}): {', '.join(sorted(plan.source_skills))}")
    for warning in plan.warnings:
        print(f"[WARN] {warning}")
    print("Actions:")
    for action in plan.actions:
        print("  " + action.line().replace("\n", "\n    "))
    if plan.blockers:
        print(f"Blockers: {len(plan.blockers)}")
    else:
        print("Blockers: 0")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _ast_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _ast_call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_absolute_path(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    value = node.value
    if (
        value.startswith(("/", "\\"))
        or bool(re.match(r"^[A-Za-z]:[/\\]", value))
        or value.startswith(("~/", "~\\"))
    ):
        return value
    return None


def validate_import_python_ast(script: Path, text: str) -> None:
    """Reject high-confidence dangerous behavior without executing untrusted code."""

    try:
        tree = ast.parse(text, filename=str(script))
    except SyntaxError as exc:
        raise BlockedError(f"Imported Python script has invalid syntax: {script}: {exc}") from exc
    dangerous_calls = {
        "eval",
        "exec",
        "compile",
        "__import__",
        "builtins.eval",
        "builtins.exec",
        "os.system",
        "os.popen",
    }
    path_calls = {
        "Path",
        "PurePath",
        "PurePosixPath",
        "PureWindowsPath",
        "pathlib.Path",
        "pathlib.PurePath",
        "open",
        "io.open",
    }
    destructive_literal = re.compile(
        r"(?:^|\s)(?:rm\s+-rf|Remove-Item\b[^\r\n]*-(?:Recurse|Force)|del\s+/[sq]|rmdir\s+/s)\s+(?:/|~|[A-Za-z]:\\)",
        re.I,
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _ast_call_name(node.func)
        if call_name in dangerous_calls:
            raise BlockedError(f"Imported Python script uses dangerous call {call_name}: {script}")
        if any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            raise BlockedError(f"Imported Python script enables shell execution: {script}")
        if call_name in path_calls and node.args:
            absolute = _literal_absolute_path(node.args[0])
            if absolute is not None:
                raise BlockedError(
                    f"Imported Python script hard-codes an absolute/external path {absolute!r}: {script}"
                )
        for argument in node.args:
            values: list[str] = []
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                values.append(argument.value)
            elif isinstance(argument, (ast.List, ast.Tuple)):
                values.extend(
                    item.value
                    for item in argument.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
            if any(destructive_literal.search(value) for value in values):
                raise BlockedError(f"Imported Python script contains a destructive command literal: {script}")


def validate_import_tree(source: Path, skill_name: str, *, forbidden_checks: set[str] | None = None) -> None:
    checks = {code for code, _, _ in FORBIDDEN_PATTERNS} if forbidden_checks is None else forbidden_checks
    """Static validation for untrusted local imports; never execute scripts."""
    if not _valid_skill_name(skill_name):
        raise BlockedError(f"Unsafe import skill name: {skill_name}")
    source_state = inspect_path(source)
    if source_state.lstat_kind != "directory" or source_state.is_link:
        raise BlockedError(f"Import source must be a real directory: {source}")
    for current_root, dir_names, file_names in os.walk(source, topdown=True, followlinks=False):
        current = Path(current_root)
        dir_names.sort(key=str.lower)
        file_names.sort(key=str.lower)
        for name in [*dir_names, *file_names]:
            if name in {".git", STATE_DIR_NAME}:
                raise BlockedError(f"Import contains forbidden registry metadata: {current / name}")
            child = current / name
            kind = _lstat_kind(child)
            if kind in {"symlink", "junction", "reparse", "other"}:
                raise BlockedError(f"Import contains unsafe {kind} entry: {child}")
            if kind == "file":
                try:
                    if os.lstat(child).st_nlink != 1:
                        raise BlockedError(f"Import contains a hard-linked file: {child}")
                except OSError as exc:
                    raise BlockedError(f"Import file cannot be inspected: {child}: {exc}") from exc
            if not lexical_contains(child, source):
                raise BlockedError(f"Import entry escapes source: {child}")
    skill_md = source / "SKILL.md"
    if _lstat_kind(skill_md) != "file":
        raise BlockedError(f"Imported skill is missing SKILL.md: {source}")
    try:
        metadata, body = parse_skill_frontmatter(skill_md)
    except (OSError, UnicodeError, ValueError) as exc:
        raise BlockedError(f"Imported SKILL.md is invalid: {exc}") from exc
    if metadata.get("name") != skill_name:
        raise BlockedError(f"Imported frontmatter name must match folder '{skill_name}'")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise BlockedError("Imported frontmatter description must be 1-1024 characters")
    for reference_source in markdown_resource_sources(source):
        try:
            reference_body = body if reference_source == skill_md else reference_source.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            raise BlockedError(f"Imported skill reference cannot be read: {reference_source}: {exc}") from exc
        for raw in referenced_resource_paths(reference_body):
            value = normalized_resource_reference(raw)
            if value is None:
                continue
            referenced = lexical_path(resolve_resource_reference(source, reference_source, value))
            if not lexical_contains(referenced, source) or _lstat_kind(referenced) == "missing":
                raise BlockedError(
                    f"Imported skill has an unsafe or missing resource reference in {reference_source}: {raw}"
                )

    agent_path = source / "agents" / "openai.yaml"
    if _lstat_kind(agent_path) == "file":
        try:
            agent_data = parse_yaml_mapping(agent_path)
        except ValueError as exc:
            raise BlockedError(f"Imported agents/openai.yaml is invalid: {exc}") from exc
        interface = agent_data.get("interface")
        if not isinstance(interface, dict):
            raise BlockedError("Imported agents/openai.yaml requires an interface mapping")
        for key in ("display_name", "short_description", "default_prompt"):
            if not isinstance(interface.get(key), str) or not interface[key].strip():
                raise BlockedError(f"Imported agents/openai.yaml interface.{key} must be non-empty")
        short_description = interface.get("short_description")
        if not 25 <= len(short_description.strip()) <= 64:
            raise BlockedError("Imported agents/openai.yaml short_description must be 25-64 characters")
        if f"${skill_name}" not in interface.get("default_prompt", ""):
            raise BlockedError(f"Imported agents/openai.yaml default_prompt must mention ${skill_name}")
        for icon_key in ("icon_small", "icon_large"):
            icon = interface.get(icon_key)
            if icon is None:
                continue
            if not isinstance(icon, str) or not icon.strip():
                raise BlockedError(f"Imported agents/openai.yaml interface.{icon_key} must be a path string")
            icon_path = lexical_path(source / icon.replace("/", os.sep))
            if Path(icon).is_absolute() or not lexical_contains(icon_path, source) or _lstat_kind(icon_path) != "file":
                raise BlockedError(f"Imported agents/openai.yaml has an unsafe {icon_key}: {icon}")

    text_suffixes = {
        ".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
        ".conf", ".py", ".ps1", ".sh", ".js", ".ts", ".html", ".xml",
        ".csv", ".tsv",
    }
    for candidate in sorted(path for path in source.rglob("*") if _lstat_kind(path) == "file"):
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            raise BlockedError(f"Imported file cannot be read: {candidate}: {exc}") from exc
        must_be_text = candidate.suffix.lower() in text_suffixes or candidate.parent.name == "scripts"
        if not must_be_text and b"\0" in payload[:8192]:
            continue
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            if must_be_text:
                raise BlockedError(f"Imported text/script is not valid UTF-8: {candidate}") from exc
            continue
        for code, pattern, label in FORBIDDEN_PATTERNS:
            if code in checks and pattern.search(text):
                raise BlockedError(f"Imported skill contains {label} ({code}): {candidate}")
        if candidate.suffix.lower() == ".py":
            validate_import_python_ast(candidate, text)


def copy_tree_static(source: Path, destination: Path) -> None:
    """Copy one lstat-verified entry at a time without following links."""
    if _lstat_kind(destination) != "missing":
        raise BlockedError(f"Import staging destination already exists: {destination}")
    destination.mkdir(parents=False)
    for current_root, dir_names, file_names in os.walk(source, topdown=True, followlinks=False):
        current = Path(current_root)
        relative = current.relative_to(source)
        target_root = destination / relative
        dir_names.sort(key=str.lower)
        file_names.sort(key=str.lower)
        for name in dir_names:
            child = current / name
            if _lstat_kind(child) != "directory":
                raise BlockedError(f"Import changed during copy: {child}")
            (target_root / name).mkdir()
        for name in file_names:
            child = current / name
            if _lstat_kind(child) != "file":
                raise BlockedError(f"Import changed during copy: {child}")
            shutil.copy2(child, target_root / name, follow_symlinks=False)


def backup_path(target: TargetConfig, path: Path) -> Path:
    target.backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target.backup_root / f"{path.name}.backup-{stamp}"
    i = 1
    while _lstat_kind(candidate) != "missing":
        candidate = target.backup_root / f"{path.name}.backup-{stamp}-{i}"
        i += 1
    return candidate


def remove_link(path: Path) -> None:
    observed = inspect_path(path)
    if observed.lstat_kind in {"junction", "reparse"} and is_windows():
        # Junction delete; rmdir removes the junction itself, not the target.
        os.rmdir(path)
    else:
        path.unlink()


def snapshot_link(path: Path) -> LinkSnapshot:
    observed = inspect_path(path)
    if not observed.is_link:
        raise BlockedError(f"Expected a link to snapshot: {path}")
    return LinkSnapshot(observed.lstat_kind, os.readlink(path))


def restore_link_snapshot(path: Path, snapshot: LinkSnapshot) -> None:
    """Restore the original primitive and raw target without source probes."""
    if _lstat_kind(path) != "missing":
        raise TransactionError(f"Rollback link path is occupied: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_windows() and snapshot.kind in {"junction", "reparse"}:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(path), snapshot.raw_target],
            check=True,
        )
    else:
        os.symlink(snapshot.raw_target, path, target_is_directory=True)
    if not inspect_path(path).is_link:
        raise TransactionError(f"Rollback failed to recreate link primitive: {path}")


def create_link(source: Path, target: Path) -> None:
    source_state = inspect_path(source)
    if source_state.lstat_kind != "directory" or source_state.is_link:
        raise BlockedError(f"Link source must be a real directory: {source}")
    if _lstat_kind(target) != "missing":
        raise BlockedError(f"Link target appeared after planning: {target}")
    ensure_parent(target)
    if is_windows():
        subprocess.run([
            "cmd", "/c", "mklink", "/J", str(target), str(source)
        ], check=True)
    else:
        os.symlink(source, target, target_is_directory=True)
    if not direct_link_to(target, source):
        with contextlib.suppress(OSError):
            remove_link(target)
        raise TransactionError(f"Created link is not the expected direct link: {target} -> {source}")


def move_to_trash(path: Path) -> None:
    if is_windows():
        ps = "Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($args[0], 'OnlyErrorDialogs', 'SendToRecycleBin')"
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps, str(path)], check=True)
    else:
        trash = shutil.which("gio")
        if trash:
            subprocess.run([trash, "trash", str(path)], check=True)
            return
        trash_put = shutil.which("trash-put")
        if trash_put:
            subprocess.run([trash_put, str(path)], check=True)
            return
        raise ConfigError(f"No OS trash command available; refusing permanent delete: {path}")


def find_target(plan: Plan, name: str) -> TargetConfig:
    for target in plan.targets:
        if target.name == name:
            return target
    raise ConfigError(f"Internal error: target not found {name}")


def transaction_resource_paths(plan: Plan) -> list[Path]:
    paths = {item for target in plan.targets for item in (target.root, target.discovery_manifest) if item is not None}
    deletion_roots = set()
    for action in plan.actions:
        if action.kind == "GLOBAL_DELETE_INVENTORY_SCAN" and action.path is not None:
            deletion_roots.add(action.path)
        if action.kind == "GLOBAL_DELETE_CONSUMER" and action.path is not None:
            deletion_roots.add(action.path.parent)
    paths.update(deletion_roots)
    paths.update(root.with_suffix(".json") for root in deletion_roots)
    if deletion_roots:
        mounts = plan.semantic_summary.get("validatedModel", {}).get("mount_points", {})
        paths.update(Path(mount["discovery_manifest"]) for mount in mounts.values() if mount.get("discovery_manifest"))
    return list(paths)


def apply_plan(plan: Plan, options: RuntimeOptions) -> None:
    unknown = [
        action.kind for action in plan.actions
        if not action.blocking and action.kind not in OBSERVATION_ACTIONS and action.kind not in KNOWN_MUTATING_ACTIONS
    ]
    if unknown:
        raise TransactionError(f"Unknown action kind(s); refusing partial apply: {', '.join(sorted(set(unknown)))}")
    if plan.blockers:
        raise BlockedError("Plan has blockers; resolve them before apply.")
    mutating = [a for a in plan.actions if a.kind in KNOWN_MUTATING_ACTIONS]
    if options.create_only and any(action.kind != "CREATE_LINK" for action in mutating):
        raise BlockedError("Create-only apply permits CREATE_LINK only; review onboarding or repair separately")
    if mutating and not options.yes:
        raise BlockedError("Apply requires --yes when there are filesystem changes.")
    if not mutating:
        return

    with apply_lock(plan.repo_root), shared_resource_locks(transaction_resource_paths(plan)):
        verify_plan_semantics(plan, options)
        current_manifest = load_manifest(plan.repo_root)
        if current_manifest != plan.manifest:
            raise BlockedError("State manifest changed after planning; re-run plan/apply")
        verify_plan_fingerprints(plan)
        for action in plan.actions:
            if action.kind != "CLIENT_RUNTIME_BINDING":
                continue
            target = next(item for item in plan.targets if action.target in item.adapter_ids)
            policy = target.client_model.adapters[action.target].runtime_binding
            try:
                current_binding = inspect_adapter_runtime_binding(target.client_model, action.target, target.root)
            except RuntimeBindingError as exc:
                raise BlockedError(str(exc)) from exc
            if current_binding != action.data.get("binding"):
                raise BlockedError("Runtime identity changed after planning; review a new plan")
        manifest = json.loads(json.dumps(current_manifest))
        original_manifest = json.loads(json.dumps(current_manifest))
        transaction_id = uuid.uuid4().hex
        transaction: dict[str, Any] = {
            "id": transaction_id,
            "planId": plan.plan_id,
            "repoId": repository_id(plan.repo_root),
            "status": "running",
            "created": time.time(),
            "completedActions": [],
            "undo": [],
            "manifestExisted": (state_dir(plan.repo_root) / "manifest.json").exists(),
            "originalManifest": original_manifest,
            "replacedLinks": [],
        }
        for action in mutating:
            if action.kind == "REPLACE_WRONG_LINK" and action.path is not None:
                snapshot = snapshot_link(action.path)
                transaction["replacedLinks"].append({
                    "actionId": action.action_id, "path": str(action.path),
                    "kind": snapshot.kind, "rawTarget": snapshot.raw_target,
                    "newTarget": str(action.source),
                })
        recovery_parent = plan.repo_root / ".work-skills-trash"
        recovery_root = recovery_parent / transaction_id
        transaction["recoveryPath"] = str(recovery_root)
        journal_path = write_transaction(plan.repo_root, transaction)
        undo_stack: list[tuple[str, Any]] = []
        source_staging: list[tuple[Path, Path]] = []
        prune_staging: list[tuple[Path, Path]] = []
        created_recovery_directories: list[Path] = []
        removed_consumer_paths: set[str] = set()

        def ensure_recovery_directory(kind: str) -> Path:
            parent_state = inspect_path(recovery_parent)
            if parent_state.lstat_kind == "missing":
                recovery_parent.mkdir(exist_ok=False)
                created_recovery_directories.append(recovery_parent)
            elif (
                parent_state.lstat_kind != "directory"
                or parent_state.is_link
                or lexical_norm_path(parent_state.lexical) != lexical_norm_path(parent_state.final)
            ):
                raise BlockedError(f"Recovery parent must be a real directory: {recovery_parent}")
            if _lstat_kind(recovery_root) == "missing":
                recovery_root.mkdir(exist_ok=False)
                created_recovery_directories.append(recovery_root)
            elif _lstat_kind(recovery_root) != "directory" or is_link_or_junction(recovery_root):
                raise BlockedError(f"Recovery transaction path is unsafe: {recovery_root}")
            destination = recovery_root / kind
            if _lstat_kind(destination) == "missing":
                destination.mkdir(exist_ok=False)
                created_recovery_directories.append(destination)
            elif _lstat_kind(destination) != "directory" or is_link_or_junction(destination):
                raise BlockedError(f"Recovery staging path is unsafe: {destination}")
            return destination

        def record_undo(description: str, callback: Any) -> None:
            undo_stack.append((description, callback))
            transaction["undo"].append(description)
            write_transaction(plan.repo_root, transaction)

        def remember_backup(target: TargetConfig, destination: Path) -> None:
            backups = manifest.setdefault("backups", {}).setdefault(target.name, [])
            if str(destination) not in backups:
                backups.append(str(destination))

        def allocate_backup(target: TargetConfig, path: Path) -> Path:
            root_was_missing = _lstat_kind(target.backup_root) == "missing"
            destination = backup_path(target, path)
            if root_was_missing:
                record_undo(
                    f"remove created backup root {target.backup_root}",
                    lambda root=target.backup_root: root.rmdir() if _lstat_kind(root) == "directory" and not any(root.iterdir()) else None,
                )
            return destination

        def stage_source_delete(original: Path) -> None:
            staged = ensure_recovery_directory("sources") / original.name
            if _lstat_kind(staged) != "missing":
                raise TransactionError(f"Delete staging collision: {staged}")
            os.replace(original, staged)
            source_staging.append((original, staged))
            record_undo(
                f"restore source {original}",
                lambda original=original, staged=staged: os.replace(staged, original) if _lstat_kind(staged) != "missing" else None,
            )

        def rollback(cause: BaseException) -> None:
            failures: list[str] = []
            with contextlib.suppress(Exception):
                atomic_write_json(state_dir(plan.repo_root) / "manifest.json", original_manifest)
            for description, callback in reversed(undo_stack):
                try:
                    callback()
                except Exception as exc:  # best effort but fully reported
                    failures.append(f"{description}: {exc}")
            # Undo callbacks restore every staged item first. Remove only known
            # empty recovery directories; never recurse over unexpected data.
            for directory in reversed(created_recovery_directories):
                try:
                    if _lstat_kind(directory) == "directory" and not any(directory.iterdir()):
                        directory.rmdir()
                except OSError as exc:
                    failures.append(f"remove empty recovery directory {directory}: {exc}")
            transaction["status"] = "rollback_failed" if failures else "rolled_back"
            transaction["error"] = f"{type(cause).__name__}: {cause}"
            transaction["rollbackFailures"] = failures
            with contextlib.suppress(Exception):
                write_transaction(plan.repo_root, transaction)
            if failures:
                raise TransactionError(
                    f"Apply failed and rollback was incomplete: {cause}; {'; '.join(failures)}; journal={journal_path}"
                ) from cause

        def verify_skill_inventory_scans(skill_name: str) -> None:
            for planned_action in plan.actions:
                if (
                    planned_action.kind == "GLOBAL_DELETE_INVENTORY_SCAN"
                    and planned_action.data.get("skill") == skill_name
                ):
                    expected_by_path = planned_action.data.get("preconditions")
                    if not isinstance(expected_by_path, dict) or planned_action.path is None:
                        raise TransactionError("Unsealed deletion inventory scan")
                    key = lexical_norm_path(planned_action.path)
                    expected = json.loads(json.dumps(expected_by_path.get(key)))
                    actual = shallow_listing_fingerprint(planned_action.path)
                    removed_names = {
                        Path(path).name
                        for path in removed_consumer_paths
                        if lexical_norm_path(Path(path).parent) == key
                    }
                    expected_children = expected.get("children") if isinstance(expected, dict) else None
                    if removed_names and isinstance(expected_children, list):
                        removed_expected = {
                            item.get("name") for item in expected_children
                            if isinstance(item, dict) and item.get("name") in removed_names
                        }
                        if removed_expected:
                            expected["children"] = [
                                item for item in expected_children
                                if not isinstance(item, dict) or item.get("name") not in removed_expected
                            ]
                            actual_children = actual.get("children")
                            if isinstance(actual_children, list) and any(
                                isinstance(item, dict) and item.get("name") in removed_expected
                                for item in actual_children
                            ):
                                raise BlockedError(
                                    f"Removed consumer reappeared under {planned_action.path}; re-run plan/apply"
                                )
                            for mutable in ("size", "mtimeNs"):
                                expected.pop(mutable, None)
                                actual.pop(mutable, None)
                    if actual != expected:
                        raise BlockedError(
                            f"Deletion inventory changed before source removal: {planned_action.path}; re-run plan/apply"
                        )

        try:
            for action in plan.actions:
                if action.kind in OBSERVATION_ACTIONS:
                    continue
                if action.kind not in KNOWN_MUTATING_ACTIONS:
                    raise TransactionError(f"Unknown action during execution: {action.kind}")
                verify_action_preconditions(action)
                target = (
                    find_target(plan, action.target)
                    if action.target != "repo" and action.kind != "GLOBAL_DELETE_CONSUMER"
                    else None
                )
                skill = str(action.data.get("skill") or (action.path.name if action.path else ""))

                if action.kind == "VERIFY_CONSUMER" and action.path and action.source and target:
                    if not direct_link_to(action.path, action.source):
                        raise BlockedError(f"Consumer is not an expected direct link: {action.path}")
                    register_consumer(manifest, target, skill, action.path, action.source)
                    print(f"Adopted managed consumer deployment: {action.path}")
                elif action.kind == "GLOBAL_DELETE_CONSUMER" and action.path and action.source:
                    if not action.data.get("missing"):
                        previous_snapshot = snapshot_link(action.path)
                        if not direct_link_to(action.path, action.source):
                            raise BlockedError(f"Managed consumer changed before delete: {action.path}")
                        remove_link(action.path)
                        removed_consumer_paths.add(lexical_norm_path(action.path))
                        record_undo(
                            f"recreate consumer {action.path}",
                            lambda path=action.path, snapshot=previous_snapshot: restore_link_snapshot(path, snapshot),
                        )
                        print(f"Removed managed consumer: {action.path}")
                    unregister_consumer(manifest, action.target, skill)
                elif action.kind == "GLOBAL_DELETE_REPO_SKILL" and action.path:
                    verify_skill_inventory_scans(skill)
                    stage_source_delete(action.path)
                    print(f"Moved repo skill into retained recovery bundle: {action.path}")
                elif action.kind == "CREATE_TARGET_ROOT" and action.path:
                    action.path.mkdir(parents=True, exist_ok=False)
                    record_undo(
                        f"remove created target root {action.path}",
                        lambda path=action.path: path.rmdir() if _lstat_kind(path) == "directory" and not any(path.iterdir()) else None,
                    )
                    print(f"Created target root: {action.path}")
                elif action.kind == "REPLACE_TARGET_ROOT_LINK" and action.path and target:
                    previous = inspect_path(action.path)
                    if not previous.is_link or not previous.chain:
                        raise BlockedError(f"Target root link changed before replacement: {action.path}")
                    previous_snapshot = snapshot_link(action.path)
                    remove_link(action.path)
                    def restore_root_link(path: Path = action.path, snapshot: LinkSnapshot = previous_snapshot) -> None:
                        if _lstat_kind(path) == "directory":
                            if any(path.iterdir()):
                                raise TransactionError(f"Replaced root still contains unrolled entries: {path}")
                            path.rmdir()
                        if _lstat_kind(path) == "missing":
                            restore_link_snapshot(path, snapshot)
                    record_undo(f"restore target root link {action.path}", restore_root_link)
                    action.path.mkdir(parents=True, exist_ok=False)
                    print(f"Replaced target root link: {action.path}")
                elif action.kind in {"REMOVE_STALE_REPO_LINK", "REPLACE_WRONG_LINK"} and action.path:
                    previous = inspect_path(action.path)
                    if not previous.is_link or not previous.chain:
                        raise BlockedError(f"Link changed before replacement: {action.path}")
                    previous_snapshot = snapshot_link(action.path)
                    remove_link(action.path)
                    record_undo(
                        f"restore link {action.path}",
                        lambda path=action.path, snapshot=previous_snapshot: restore_link_snapshot(path, snapshot),
                    )
                    unregister_consumer(manifest, action.target, skill)
                    print(f"Removed link: {action.path}")
                    if action.kind == "REPLACE_WRONG_LINK" and action.source and target:
                        create_link(action.source, action.path)
                        record_undo(f"remove replacement link {action.path}", lambda path=action.path: remove_link(path))
                        register_consumer(manifest, target, skill, action.path, action.source)
                        print(f"Mounted: {action.path} -> {action.source}")
                elif action.kind in {"MOVE_FILE_TO_BACKUP", "MOVE_REAL_DIR_TO_BACKUP"} and action.path and target:
                    dest = allocate_backup(target, action.path)
                    os.replace(action.path, dest)
                    remember_backup(target, dest)
                    record_undo(f"restore backup {dest} -> {action.path}", lambda src=dest, dst=action.path: os.replace(src, dst))
                    print(f"Moved to backup: {action.path} -> {dest}")
                    if action.source:
                        create_link(action.source, action.path)
                        record_undo(f"remove mounted link {action.path}", lambda path=action.path: remove_link(path))
                        register_consumer(manifest, target, skill, action.path, action.source)
                        print(f"Mounted: {action.path} -> {action.source}")
                elif action.kind == "IMPORT_LOCAL_SKILL" and action.path and action.source and target:
                    validate_import_tree(action.path, skill, forbidden_checks=source_forbidden_checks(plan.semantic_summary.get("validatedConfig", {})))
                    staging_parent = plan.skills_root.parent
                    staging = Path(tempfile.mkdtemp(prefix=f".work-skills-import-{transaction_id}-", dir=str(staging_parent))) / skill
                    try:
                        copy_tree_static(action.path, staging)
                        validate_import_tree(staging, skill, forbidden_checks=source_forbidden_checks(plan.semantic_summary.get("validatedConfig", {})))
                        if path_fingerprint(action.path, recursive=True) != action.data["preconditions"][lexical_norm_path(action.path)]:
                            raise BlockedError(f"Import source changed during isolated copy: {action.path}")
                        dest = allocate_backup(target, action.path)
                        os.replace(action.path, dest)
                        remember_backup(target, dest)
                        record_undo(f"restore imported local skill {dest}", lambda src=dest, dst=action.path: os.replace(src, dst))
                        os.replace(staging, action.source)
                        record_undo(
                            f"remove generated imported repo skill {action.source}",
                            lambda source=action.source: shutil.rmtree(source) if _lstat_kind(source) == "directory" else None,
                        )
                        create_link(action.source, action.path)
                        record_undo(f"remove imported mount {action.path}", lambda path=action.path: remove_link(path))
                        register_consumer(manifest, target, skill, action.path, action.source)
                        print(f"Imported and mounted after static validation: {skill}")
                    finally:
                        # This directory is uniquely created by this transaction
                        # and contains only generated copies, so recursive
                        # cleanup cannot remove pre-existing user content.
                        with contextlib.suppress(OSError):
                            shutil.rmtree(staging.parent)
                elif action.kind in {"CREATE_DISCOVERY_MANIFEST", "UPDATE_DISCOVERY_MANIFEST"} and action.path:
                    before = action.path.read_bytes() if action.kind == "UPDATE_DISCOVERY_MANIFEST" else None
                    after = (json.dumps(action.data["content"], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    transaction.setdefault("discoveryManifests", []).append({
                        "path": str(action.path),
                        "before": base64.b64encode(before).decode("ascii") if before is not None else None,
                        "afterSha256": hashlib.sha256(after).hexdigest(),
                    })
                    write_transaction(plan.repo_root, transaction)
                    if before is None:
                        action.path.parent.mkdir(parents=True, exist_ok=True)
                        with action.path.open("xb") as handle:
                            handle.write(after)
                            handle.flush()
                            os.fsync(handle.fileno())
                    else:
                        atomic_write_bytes(action.path, after)
                    record_undo(f"restore discovery manifest {action.path}",
                                lambda path=action.path, original=before, written=after: restore_discovery_manifest(path, original, written))
                    for affected in plan.targets:
                        if affected.discovery_manifest == action.path:
                            affected.runtime_bindings.clear()
                    print(f"Registered native discovery source: {action.path}")
                elif action.kind == "CREATE_LINK" and action.path and action.source and target:
                    create_link(action.source, action.path)
                    record_undo(f"remove created link {action.path}", lambda path=action.path: remove_link(path))
                    register_consumer(manifest, target, skill, action.path, action.source)
                    print(f"Mounted: {action.path} -> {action.source}")
                elif action.kind == "PRUNE_BACKUP" and action.path:
                    recovery_backups = ensure_recovery_directory("backups")
                    staged = recovery_backups / f"{len(prune_staging):04d}-{action.path.name}"
                    if _lstat_kind(staged) != "missing":
                        raise TransactionError(f"Backup recovery collision: {staged}")
                    os.replace(action.path, staged)
                    prune_staging.append((action.path, staged))
                    record_undo(f"restore pruned backup {action.path}", lambda original=action.path, staged=staged: os.replace(staged, original) if _lstat_kind(staged) != "missing" else None)
                    print(f"Moved old backup into retained recovery bundle: {action.path}")
                elif action.kind == "LEGACY_CLEANUP" and action.path and target:
                    if is_link_or_junction(action.path):
                        previous_snapshot = snapshot_link(action.path)
                        remove_link(action.path)
                        record_undo(
                            f"restore legacy link {action.path}",
                            lambda path=action.path, snapshot=previous_snapshot: restore_link_snapshot(path, snapshot),
                        )
                        print(f"Removed legacy link: {action.path}")
                    else:
                        dest = allocate_backup(target, action.path)
                        os.replace(action.path, dest)
                        remember_backup(target, dest)
                        record_undo(f"restore legacy entry {action.path}", lambda src=dest, dst=action.path: os.replace(src, dst))
                        print(f"Moved legacy entry to backup: {action.path} -> {dest}")
                else:
                    raise TransactionError(f"Malformed or unhandled action: {action.kind}")

                transaction["completedActions"].append(action.kind)
                write_transaction(plan.repo_root, transaction)

            atomic_write_json(state_dir(plan.repo_root) / "manifest.json", manifest)
            transaction["status"] = "committing"
            write_transaction(plan.repo_root, transaction)

            transaction["status"] = "committed"
            transaction["committed"] = time.time()
            with contextlib.suppress(Exception):
                write_transaction(plan.repo_root, transaction)
        except BaseException as exc:
            rollback(exc)
            raise


def run_doctor(
    options: RuntimeOptions,
    target_sets: list[str] | None,
    all_targets: bool,
    system_audit: SystemDependencyAudit | None = None,
) -> int:
    """Report deployment, static content, and system-runtime health.

    A trusted source wrapper may explicitly supply its runtime audit result.
    Generic doctor performs static and deployment checks.
    """

    if system_audit is None:
        system_audit = SystemDependencyAudit(None, 0, {"status": "not-requested", "errors": []})
    plan = build_plan(options, target_sets, all_targets)
    audit = run_content_audit(options.repo_root, options.config_path)
    unverified_targets: list[str] = []
    manifest_consumers = plan.manifest.get("consumers", {})
    for target in plan.targets:
        target_state = manifest_consumers.get(target.name) if isinstance(manifest_consumers, dict) else None
        if not native_verification_is_current(target_state, target):
            unverified_targets.append(target.name)
    client_warnings = [
        action for action in plan.actions
        if action.kind in {"CONSUMER_NOT_INSTALLED", "CONSUMER_AMBIGUOUS"}
        or (
            action.kind == "CONSUMER_INSTALLED"
            and "confirmed" not in action.data.get("managementStatuses", [])
        )
    ]
    if options.output_format == "json":
        payload = {
            "mount": {
                "planId": plan.plan_id,
                "repoRoot": str(plan.repo_root),
                "skillsRoot": str(plan.skills_root),
                "sourceSkills": sorted(plan.source_skills),
                "warnings": plan.warnings,
                "actions": [a.__dict__ | {"path": str(a.path) if a.path else None, "source": str(a.source) if a.source else None} for a in plan.actions],
                "blockers": len(plan.blockers),
                "unverifiedConsumers": unverified_targets,
                "clientWarnings": [
                    {"consumer": action.target, "kind": action.kind, "message": action.message}
                    for action in client_warnings
                ],
            },
            "audit": combined_audit_report_dict(audit, system_audit),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_plan(plan, options.output_format)
        print_audit_report(audit, options.output_format)
        print_system_dependency_audit(system_audit)
        for target_name in unverified_targets:
            print(f"[ERROR] CONSUMER_NATIVE_UNVERIFIED | {target_name} | consumer native probe has not been verified")
        for action in client_warnings:
            print(f"[WARN] {action.kind} | {action.target} | {action.message}")
    # Doctor describes the realized deployment, not whether an apply would be
    # permitted. Every pending mutation and every unverified/adverse state is
    # unhealthy, even when the planner can repair it safely.
    errors = [
        action for action in plan.actions
        if action.blocking or action.kind in KNOWN_MUTATING_ACTIONS
    ]
    total_errors = (
        len(errors)
        + len(audit.errors)
        + len(unverified_targets)
        + system_audit.error_count
    )
    if total_errors:
        if options.output_format != "json":
            print(f"Doctor: errors={total_errors}")
        return 3
    warning_kinds = {"PRESERVE_UNMANAGED_LINK", "PRESERVE_LOCAL_REAL_SKILL", "PRESERVE_NON_SKILL_DIR", "PRESERVE_FILE"}
    warnings = [a for a in plan.actions if a.kind in warning_kinds] + client_warnings + [Action("WARN", "repo", message=w) for w in plan.warnings]
    if options.output_format != "json":
        print(f"Doctor: warnings={len(warnings) + len(audit.warnings)}")
    return 0


def validate_runtime_evidence_destination(repo_root: Path, raw_path: str) -> Path:
    """Require a new, non-aliased evidence path strictly outside the repo."""

    repo_root = lexical_path(repo_root)
    raw_destination = Path(raw_path).expanduser()
    if not raw_destination.is_absolute():
        raise BlockedError(
            "Runtime evidence destination must be a new absolute path outside "
            f"the repository: {raw_path}"
        )
    destination = lexical_path(raw_destination)
    if lexical_contains(destination, repo_root):
        raise BlockedError(
            f"Runtime evidence must be kept outside the repository: {destination}"
        )
    if _lstat_kind(destination) != "missing":
        raise BlockedError(
            f"Refusing to overwrite existing runtime evidence path: {destination}"
        )
    try:
        final_destination = destination.resolve(strict=False)
    except OSError as exc:
        raise BlockedError(
            f"Cannot resolve runtime evidence destination safely: {destination}: {exc}"
        ) from exc
    if is_relative_to(final_destination, repo_root):
        raise BlockedError(
            "Runtime evidence destination resolves inside the repository: "
            f"{destination} -> {final_destination}"
        )
    if lexical_norm_path(destination) != lexical_norm_path(final_destination):
        raise BlockedError(
            "Runtime evidence destination traverses a linked/aliased path: "
            f"{destination} -> {final_destination}"
        )
    return destination


def run_clients_capture(
    options: RuntimeOptions,
    runtime_evidence_path: str | None,
    *,
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    """Capture adapter-bound runtime evidence from the current client shell."""

    if len(options.targets) != 1:
        raise ConfigError("clients capture requires exactly one explicit --target raw adapter ID")
    if not runtime_evidence_path:
        raise ConfigError(
            "clients capture requires --runtime-evidence "
            "<new-absolute-path-outside-repository>"
        )
    try:
        model = load_client_model(options.config_path)
    except ClientModelError as exc:
        raise ConfigError(str(exc)) from exc
    adapter_id = options.targets[0]
    adapter = model.adapters.get(adapter_id)
    if adapter is None:
        raise ConfigError(
            f"clients capture --target must be a raw adapter ID: {adapter_id}"
        )
    policy = adapter.python_runtime_verification
    if policy is None:
        raise ConfigError(
            f"Adapter {adapter_id} does not declare pythonRuntimeVerification"
        )

    environment = dict(os.environ if environ is None else environ)
    executable, resolution_error = resolve_system_python_executable(environment, which)
    if resolution_error:
        raise BlockedError(resolution_error)
    assert executable is not None
    capture_script = Path(__file__).with_name("probes") / "capture_runtime_evidence.py"
    if _lstat_kind(capture_script) != "file":
        raise ConfigError(f"Python runtime evidence capture script is missing: {capture_script}")

    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    destination = validate_runtime_evidence_destination(
        options.repo_root, runtime_evidence_path
    )
    command = [
        executable,
        "-B",
        str(capture_script),
        "--adapter",
        adapter_id,
    ]
    for consumer_id in adapter.consumer_ids:
        command.extend(["--consumer", consumer_id])
    command.extend(
        [
            "--context-id",
            policy.context_id,
            "--context-environment",
            policy.context_environment,
            "--expected-context",
            str(policy.expected_context),
            "--python-command",
            policy.python_command,
            "--required-version",
            policy.required_version,
            "--output",
            str(destination),
        ]
    )
    if policy.allow_unset_context:
        command.append("--allow-unset-context")

    run = subprocess.run if runner is None else runner
    try:
        proc = run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise BlockedError("Python runtime evidence capture timed out after 60s") from exc
    except OSError as exc:
        raise BlockedError(f"Could not launch Python runtime evidence capture: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise BlockedError(
            f"Python runtime evidence capture failed for {adapter_id}: "
            f"{detail[:1000] or f'exit status {proc.returncode}'}"
        )

    try:
        captured = load_python_runtime_evidence(destination)
    except (OSError, json.JSONDecodeError, RuntimeEvidenceError) as exc:
        raise BlockedError(f"Captured runtime evidence cannot be loaded: {exc}") from exc
    validation_errors = validate_python_runtime_evidence(model, adapter_id, captured)
    if validation_errors:
        raise BlockedError(
            f"Captured Python runtime evidence is invalid for {adapter_id}:\n- "
            + "\n- ".join(validation_errors)
        )
    if options.output_format == "json":
        print(
            json.dumps(
                {
                    "adapter": adapter_id,
                    "runtimeEvidence": str(destination),
                    "systemPython": executable,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print((proc.stdout or "").strip() or f"Python runtime evidence written: {destination}")
        print(f"Validated for adapter: {adapter_id}")
        print(f"System Python: {executable}")
    return 0


def run_clients_verify(
    options: RuntimeOptions,
    verified_version: str | None,
    confirmed: bool,
    runtime_evidence_path: str | None = None,
    binding_evidence_path: str | None = None,
    *,
    runtime_evidence_now: datetime.datetime | None = None,
) -> int:
    """Record an explicit native discovery check without changing any mount."""

    if not confirmed:
        raise BlockedError(
            "clients verify requires --confirm-native-check after the client has actually loaded a repository skill"
        )
    if not isinstance(verified_version, str) or not verified_version.strip():
        raise ConfigError("clients verify requires a non-empty --verified-version")
    if len(options.targets) != 1:
        raise ConfigError("clients verify requires exactly one explicit --target raw adapter ID")

    config = read_yaml_simple(options.config_path)
    model = _load_v3_model(config)
    detections = {item.consumer_id: item for item in detect_consumers(model)}
    adapter_id = options.targets[0]
    if adapter_id not in model.adapters:
        raise ConfigError(
            f"clients verify --target must be a raw adapter ID, not a mount or compatibility alias: {adapter_id}"
        )
    runtime_policy = model.adapters[adapter_id].python_runtime_verification
    binding_policy = model.adapters[adapter_id].runtime_binding
    binding_evidence = None
    if binding_policy is not None:
        if not binding_evidence_path:
            raise BlockedError("clients verify requires --binding-evidence captured before the native probe")
        try:
            binding_evidence = json.loads(Path(binding_evidence_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BlockedError("Cannot read runtime binding evidence") from exc
    elif binding_evidence_path:
        raise ConfigError("This adapter does not declare runtimeBinding")
    runtime_evidence: dict[str, Any] | None = None
    if runtime_policy is not None:
        if not runtime_evidence_path:
            raise BlockedError(
                f"clients verify for {adapter_id} requires --runtime-evidence captured "
                f"inside that client context; run: {runtime_policy.capture_command}"
            )
        try:
            loaded_evidence = load_python_runtime_evidence(runtime_evidence_path)
        except (OSError, json.JSONDecodeError, RuntimeEvidenceError) as exc:
            raise BlockedError(
                f"Cannot load Python runtime evidence for {adapter_id}: {exc}"
            ) from exc
        validation_errors = validate_python_runtime_evidence(
            model, adapter_id, loaded_evidence
        )
        if not validation_errors:
            validation_errors = validate_python_runtime_evidence_freshness(
                loaded_evidence, now=runtime_evidence_now
            )
        if validation_errors:
            raise BlockedError(
                f"Invalid Python runtime evidence for {adapter_id}:\n- "
                + "\n- ".join(validation_errors)
            )
        runtime_evidence = _canonical_value(dict(loaded_evidence))
    elif runtime_evidence_path:
        raise ConfigError(
            f"Adapter {adapter_id} does not declare pythonRuntimeVerification; "
            "--runtime-evidence is not accepted"
        )
    requests = [(adapter_id, None, False)]
    selections: list[tuple[str, tuple[str, ...], str, Path]] = []
    for adapter_id, mount_filter, _default_like in requests:
        adapter = model.adapters[adapter_id]
        detection = detections[adapter.consumer_detection]
        if detection.state != "installed":
            raise BlockedError(
                f"Cannot verify {adapter_id}: consumer detection is {detection.state}"
            )
        roots = sorted(adapter.discovery_roots, key=lambda item: item.priority, reverse=True)
        selected = None
        for discovery in roots:
            mount = model.mount_points[discovery.mount_point_id]
            if mount_filter is not None and mount.id != mount_filter:
                continue
            if mount.source_selection == "none" or mount.delivery == "private-only":
                continue
            selected = (adapter_id, adapter.consumer_ids, mount.id, lexical_path(mount.root))
            break
        if selected is None:
            raise BlockedError(f"Cannot verify {adapter_id}: no shared/dedicated discovery root")
        selections.append(selected)

    verified: list[dict[str, Any]] = []
    with apply_lock(options.repo_root), shared_resource_locks(
        [item for _, _, mount_id, root in selections for item in (root, model.mount_points[mount_id].discovery_manifest) if item is not None]
    ):
        manifest = load_manifest(options.repo_root)
        consumers = manifest.get("consumers", {})
        for adapter_id, consumer_ids, mount_id, root in selections:
            target_state = consumers.get(mount_id) if isinstance(consumers, dict) else None
            if not isinstance(target_state, dict):
                raise BlockedError(
                    f"Cannot verify {adapter_id}: mount {mount_id} has no managed deployment state"
                )
            if lexical_norm_path(Path(str(target_state.get("root") or "."))) != lexical_norm_path(root):
                raise BlockedError(
                    f"Cannot verify {adapter_id}: managed root does not match {root}"
                )
            binding_snapshot = None
            if binding_policy is not None:
                try:
                    binding_snapshot = inspect_adapter_runtime_binding(model, adapter_id, root)
                    validate_binding_evidence(binding_evidence, adapter_id, binding_snapshot)
                except RuntimeBindingError as exc:
                    raise BlockedError(str(exc)) from exc
            skills = target_state.get("skills")
            owned = [
                entry for entry in skills.values()
                if isinstance(entry, dict) and entry.get("deploymentOwned") is True
            ] if isinstance(skills, dict) else []
            if not owned:
                raise BlockedError(
                    f"Cannot verify {adapter_id}: mount {mount_id} has no owned skill deployment"
                )
            for entry in owned:
                path = Path(str(entry.get("path") or ""))
                source = Path(str(entry.get("source") or ""))
                if not direct_link_to(path, source):
                    raise BlockedError(
                        f"Cannot verify {adapter_id}: deployment is no longer a healthy direct link: {path}"
                    )
            record = {
                "status": "verified",
                "adapterId": adapter_id,
                "consumerIds": list(consumer_ids),
                "mountPointId": mount_id,
                "root": str(root),
                "evidence": {
                    "method": "manual-native-check",
                    "verifiedVersion": verified_version.strip(),
                    "verifiedOn": datetime.date.today().isoformat(),
                    "instructionsAcknowledged": True,
                },
            }
            if runtime_evidence is not None:
                record["evidence"]["pythonRuntime"] = runtime_evidence
            if binding_snapshot is not None:
                record["evidence"]["runtimeBinding"] = binding_snapshot
            records = target_state.setdefault("nativeVerifications", {})
            if not isinstance(records, dict):
                records = {}
                target_state["nativeVerifications"] = records
            records[adapter_id] = record
            validation_target = TargetConfig(
                name=mount_id,
                label=mount_id,
                root=root,
                backup_root=root,
                import_local=False,
                local_only=[],
                exclude=[],
                mount_point_id=mount_id,
                adapter_ids=tuple(records),
                client_model=model,
            )
            verified_adapters = sorted(
                key
                for key, value in records.items()
                if key in model.adapters
                and _adapter_native_record_is_current(
                    value,
                    validation_target,
                    key,
                    model.adapters[key].consumer_ids,
                )
            )
            target_state["nativeVerifiedAdapters"] = verified_adapters
            for entry in owned:
                entry["nativeVerifiedAdapters"] = verified_adapters
            verified.append({
                "adapter": adapter_id,
                "mountPoint": mount_id,
                "root": str(root),
                "verifiedVersion": verified_version.strip(),
            })
        atomic_write_json(state_dir(options.repo_root) / "manifest.json", manifest)

    if options.output_format == "json":
        print(json.dumps({"verified": verified}, ensure_ascii=False, indent=2))
    else:
        for item in verified:
            print(
                f"[VERIFIED] {item['adapter']} | mount={item['mountPoint']} | "
                f"version={item['verifiedVersion']} | root={item['root']}"
            )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Skill sources and client deployments declaratively.")
    parser.add_argument("command", choices=["plan", "apply", "doctor", "audit", "clients"])
    parser.add_argument("client_command", nargs="?", choices=["detect", "verify", "capture", "capture-binding"])
    parser.add_argument("--repo-root")
    parser.add_argument("--config")
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--target-set", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--import-local", dest="import_local", action="store_true")
    parser.add_argument("--no-import-local", dest="no_import_local", action="store_true")
    parser.add_argument("--replace-wrong-links", action="store_true")
    parser.add_argument("--replace-root-link", action="store_true")
    parser.add_argument("--replace-files", action="store_true")
    parser.add_argument("--replace-real-dirs", action="store_true")
    parser.add_argument("--adopt-existing", action="store_true")
    parser.add_argument("--prune-backups", action="store_true")
    parser.add_argument("--keep-backups", type=int, default=10)
    parser.add_argument("--exclude-skill", action="append", default=[])
    parser.add_argument("--delete-skill", action="append", default=[])
    parser.add_argument("--confirm-global-delete", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--plan-id")
    parser.add_argument("--verified-version")
    parser.add_argument("--confirm-native-check", action="store_true")
    parser.add_argument("--runtime-evidence")
    parser.add_argument("--binding-evidence")
    parser.add_argument("--create-only", action="store_true", help="allow only new skill links during apply")
    parser.add_argument(
        "--strict-versions",
        action="store_true",
        help="require installed system-Python direct packages to exactly match the lock",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser.parse_args(argv)


def main(argv: list[str], *, runtime_audit: bool = False) -> int:
    args = parse_args(argv)
    config_argument = args.config
    if config_argument and not args.repo_root:
        config_argument = str(Path(config_argument).expanduser().resolve())
    repo_root = repo_root_from_args(args.repo_root, config_argument)
    config_path = resolve_config_path(repo_root, config_argument)
    declared_root = configured_repo_root(config_path, repo_root)
    if declared_root is not None and norm_path(declared_root) != norm_path(repo_root):
        raise ConfigError("Configured repo.root does not match --repo-root")
    if args.strict_versions and not runtime_audit:
        raise ConfigError("Strict runtime versions require the source's explicitly enabled runtime audit")
    import_override = True if args.import_local else False if args.no_import_local else None
    options = RuntimeOptions(
        repo_root=repo_root,
        config_path=config_path,
        targets=args.target,
        import_local_override=import_override,
        replace_wrong_links=args.replace_wrong_links,
        replace_root_link=args.replace_root_link,
        replace_files=args.replace_files,
        replace_real_dirs=args.replace_real_dirs,
        adopt_existing=args.adopt_existing,
        prune_backups=args.prune_backups,
        keep_backups=args.keep_backups,
        delete_skills=args.delete_skill,
        exclude_skills=args.exclude_skill,
        confirm_global_delete=args.confirm_global_delete,
        yes=args.yes,
        output_format=args.format,
        create_only=args.create_only,
    )

    if args.runtime_evidence and not (
        args.command == "clients" and args.client_command in {"capture", "verify"}
    ):
        raise ConfigError(
            "--runtime-evidence is accepted only by clients capture or clients verify"
        )
    if args.binding_evidence and not (
        args.command == "clients" and args.client_command in {"capture-binding", "verify"}
    ):
        raise ConfigError("--binding-evidence is accepted only by clients capture-binding or clients verify")

    if args.command == "clients":
        if args.client_command == "capture-binding":
            if len(args.target) != 1 or args.target_set or args.all or not args.binding_evidence:
                raise ConfigError("capture-binding requires one --target and a new --binding-evidence path")
            model = _load_v3_model(read_yaml_simple(config_path))
            adapter = model.adapters.get(args.target[0])
            if adapter is None or adapter.runtime_binding is None:
                raise ConfigError("Selected adapter has no runtimeBinding policy")
            root = model.mount_points[adapter.discovery_roots[0].mount_point_id].root
            try:
                snapshot = inspect_adapter_runtime_binding(model, adapter.id, root)
            except RuntimeBindingError as exc:
                raise BlockedError(str(exc)) from exc
            destination = Path(args.binding_evidence)
            if not destination.is_absolute() or is_relative_to(destination.resolve(), repo_root.resolve()):
                raise ConfigError("Binding evidence requires an absolute path outside the repository")
            evidence = {"adapterId": adapter.id, "capturedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(), "binding": snapshot}
            with destination.open("x", encoding="utf-8") as handle:
                json.dump(evidence, handle, ensure_ascii=False, indent=2)
            print(json.dumps({"adapter": adapter.id, "bindingEvidence": str(destination), "binding": snapshot}, ensure_ascii=False, indent=2))
            return 0
        if args.client_command == "capture":
            if args.target_set or args.all:
                raise ConfigError("clients capture accepts explicit --target adapters only")
            return run_clients_capture(options, args.runtime_evidence)
        if args.client_command == "verify":
            if args.target_set or args.all:
                raise ConfigError("clients verify accepts explicit --target adapters only")
            return run_clients_verify(
                options,
                args.verified_version,
                args.confirm_native_check,
                args.runtime_evidence,
                args.binding_evidence,
            )
        if args.client_command != "detect":
            raise ConfigError(
                "clients requires a subcommand: clients detect, clients capture, or clients verify"
            )
        try:
            model = load_client_model(config_path)
        except ClientModelError as exc:
            raise ConfigError(str(exc)) from exc
        detections = detect_consumers(model)
        if args.format == "json":
            print(json.dumps({
                "version": model.version,
                "consumers": [
                    {
                        "id": detection.consumer_id,
                        "state": detection.state,
                        "commandHits": list(detection.command_hits),
                        "pathHits": [str(path) for path in detection.path_hits],
                        "details": detection.details,
                    }
                    for detection in detections
                ],
            }, ensure_ascii=False, indent=2))
        else:
            for detection in detections:
                print(f"[{detection.state}] {detection.consumer_id} | {detection.details}")
        return 0

    if args.command == "audit":
        report = run_content_audit(repo_root, config_path)
        system_audit = (run_system_dependency_audit(
            repo_root, strict_versions=args.strict_versions
        ) if runtime_audit else SystemDependencyAudit(None, 0, {"status": "not-requested", "errors": []}))
        if args.format == "json":
            print(
                json.dumps(
                    combined_audit_report_dict(report, system_audit),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print_audit_report(report, args.format)
            print_system_dependency_audit(system_audit)
            print(
                f"Audit total: errors={len(report.errors) + system_audit.error_count} "
                f"warnings={len(report.warnings)}"
            )
        return 3 if report.errors or system_audit.error_count else 0
    if args.command == "doctor":
        return run_doctor(
            options,
            args.target_set,
            args.all,
            (run_system_dependency_audit(
                repo_root, strict_versions=args.strict_versions
            ) if runtime_audit else SystemDependencyAudit(None, 0, {"status": "not-requested", "errors": []})),
        )
    plan = build_plan(options, args.target_set, args.all)
    print_plan(plan, args.format)
    if args.command == "plan":
        return 3 if plan.blockers else 0
    mutating = [action for action in plan.actions if action.kind in KNOWN_MUTATING_ACTIONS]
    if mutating and args.plan_id is None:
        raise BlockedError("Mutating apply requires --plan-id from the reviewed plan command")
    if args.plan_id is not None and args.plan_id != plan.plan_id:
        raise BlockedError(
            f"Plan ID changed: expected {args.plan_id}, rebuilt {plan.plan_id}; review a new plan"
        )
    apply_plan(plan, options)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except WorkSkillsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)


def cli_main(argv: list[str]) -> int:
    try:
        return main(argv)
    except (WorkSkillsError, SharedResourceBusyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 3)
