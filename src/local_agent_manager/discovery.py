from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .environment_paths import BoundedPathProbe, local_path
from .path_identity import canonical_existing_path_key


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def default_appdata() -> Path:
    configured = os.environ.get("APPDATA")
    if configured:
        return Path(configured)
    return Path.home() / "AppData" / "Roaming"


def default_local_appdata() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return Path(configured)
    return Path.home() / "AppData" / "Local"


def discover_path_codex() -> Path | None:
    executable = shutil.which("codex")
    return Path(executable) if executable else None


CINDY_PROFILE_NAMES = ("CindyGlobal", "Cindy", "CindyDev", "xdt-maker")
_VERSION = re.compile(
    r"v?([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})"
    r"(?:-([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*))?"
    r"(?:\+[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)?"
)


def release_version(value: str) -> str | None:
    """Recognize a complete version label without trusting arbitrary path names."""
    if len(value) <= 64 and _VERSION.fullmatch(value):
        return value.removeprefix("v")
    return None


def cindy_codex_candidates(
    cindy_root: Path, probe: BoundedPathProbe
) -> tuple[tuple[Path, str | None, str], ...]:
    """Bounded known-layout candidates, separate from selection and stores."""
    result: list[tuple[Path, str | None, str]] = []
    binaries = ("codex.exe", "codex")

    def add(directory: Path, version: str | None, layout: str) -> None:
        for filename in binaries:
            candidate = directory / filename
            status, _ = probe.inspect(candidate, layout, directory=False)
            if status == "present":
                result.append((candidate, version, layout))

    packaged = "cindy_codex_package"
    for version_dir in probe.children(cindy_root / "codex-package", packaged):
        if (version := release_version(version_dir.name)) is not None:
            add(version_dir / "bin", version, packaged)

    legacy = "cindy_codex_legacy"
    base = cindy_root / "codex"
    if probe.inspect(base, legacy, directory=True)[0] == "present":
        add(base, None, legacy)
    for version_dir in probe.children(base, legacy):
        status, info = probe.inspect(version_dir, legacy)
        if status == "present" and info is not None and stat.S_ISDIR(info.st_mode):
            version = release_version(version_dir.name)
            add(version_dir, version, legacy)
            # Older builds could leave a wrapper directory. Cover a fixed
            # depth below the legacy layout instead of recursively globbing.
            for child in probe.children(version_dir, legacy):
                if child.name in binaries:
                    continue
                status, info = probe.inspect(child, legacy)
                if status == "present" and info is not None and stat.S_ISDIR(info.st_mode):
                    add(child, release_version(child.name) or version, legacy)
    return tuple(result)


def _release_sort_key(value: str | None) -> tuple:
    if value is None:
        return (0, 0, 0), False, ()
    match = _VERSION.fullmatch(value)
    assert match is not None
    numbers = tuple(int(part) for part in match.group(1, 2, 3))
    prerelease = match.group(4)
    # SemVer compares numeric prerelease parts numerically and below text.
    parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    ) if prerelease else ()
    return numbers, prerelease is None, parts


def discover_cindy_codex(cindy_root: Path) -> Path | None:
    """Choose an adapter fallback, without claiming Cindy uses that runtime."""
    try:
        root = cindy_root.expanduser()
        if not root.is_absolute():
            root = root.absolute()
    except (OSError, RuntimeError):
        return None
    if local_path(root) is None:
        return None
    candidates = cindy_codex_candidates(root, BoundedPathProbe())
    if not candidates:
        return None
    preferred_name = "codex.exe" if os.name == "nt" else "codex"
    # Exact stable tie-breaking also covers legacy folders without version
    # labels. Inaccessible or redirected candidates do not become hints.
    return max(
        candidates,
        key=lambda row: (
            row[2] == "cindy_codex_package", row[1] is not None,
            _release_sort_key(row[1]), -len(row[0].parts),
            row[0].name == preferred_name, str(row[0]),
        ),
    )[0]

@dataclass(frozen=True)
class CindyProfile:
    """Compatibility projection of one Cindy frontend DB namespace.

    This is not a unique Cindy installation, backend store, or runtime.  A
    local namespace and multiple signed-in owner namespaces may share the same
    ``root``, ``codex_home``, and bundled Codex runtime while retaining their
    distinct database paths.
    """

    root: Path
    database: Path
    codex_home: Path


def discover_cindy_profiles(appdata: Path | None = None) -> tuple[CindyProfile, ...]:
    """Discover DB-namespace projections for known Cindy installations.

    Cindy's Electron ``userData`` directory is the profile root.  Database
    sidecars and backup copies are deliberately excluded: treating them as a
    second live frontend would duplicate references and weaken snapshots.
    Multiple returned projections can deliberately share one root, native
    store, and bundled runtime; consumers must union all such namespaces before
    authorizing a native mutation.
    """

    roaming = (appdata or default_appdata()).expanduser()
    profiles: list[CindyProfile] = []
    for brand in CINDY_PROFILE_NAMES:
        root = roaming / brand
        if not _optional_directory_exists(root):
            continue
        discovered_databases = _discover_cindy_databases(root)
        for database in discovered_databases:
            profiles.append(
                CindyProfile(
                    root=root,
                    database=database,
                    codex_home=root / "codex-home",
                )
            )
        dedicated_store_exists = False
        if not discovered_databases:
            codex_store_exists = _optional_directory_exists(root / "codex-home")
            pi_home_exists = _optional_directory_exists(root / "pi-agent-home")
            pi_store_exists = (
                _optional_directory_exists(root / "pi-agent-home" / "sessions")
                if pi_home_exists
                else False
            )
            dedicated_store_exists = codex_store_exists or pi_store_exists
        if not discovered_databases and dedicated_store_exists:
            # The frontend DB may have been removed while a dedicated native
            # store survived. A missing placeholder DB contributes no
            # frontend rows but keeps that Cindy-owned storage discoverable.
            profiles.append(
                CindyProfile(
                    root=root,
                    database=root / "cindy-local-v1.db",
                    codex_home=root / "codex-home",
                )
            )
    return tuple(profiles)


def resolve_cindy_profiles(
    appdata: Path | None = None,
    *,
    root: Path | None = None,
    database: Path | None = None,
    codex_home: Path | None = None,
) -> tuple[CindyProfile, ...]:
    """Resolve every Cindy DB namespace that can guard one explicit store.

    With no explicit value this preserves global known-brand discovery.  Once
    any value is explicit, the effective Cindy installation root is bounded to
    ``root`` (or ``database.parent`` when only a DB is supplied), but every
    current sibling DB below that root remains a safety input.  An explicitly
    supplied DB is retained even when it lives outside an explicit root; this
    preserves custom mappings without allowing them to hide root siblings.
    """

    if root is None and database is None and codex_home is None:
        return discover_cindy_profiles(appdata)

    roaming = (appdata or default_appdata()).expanduser()
    explicit_database = database.expanduser() if database is not None else None
    effective_root = (
        root.expanduser()
        if root is not None
        else explicit_database.parent
        if explicit_database is not None
        else roaming / "CindyGlobal"
    )
    effective_home = (
        codex_home.expanduser()
        if codex_home is not None
        else effective_root / "codex-home"
    )

    siblings = (
        _discover_cindy_databases(effective_root)
        if _optional_directory_exists(effective_root)
        else ()
    )
    candidates: list[Path] = list(siblings)
    if explicit_database is not None:
        if _is_database_sidecar_or_backup(explicit_database):
            raise RuntimeError(
                f"Explicit Cindy database is a sidecar or backup: {explicit_database}"
            )
        try:
            explicit_status = explicit_database.stat()
        except FileNotFoundError:
            # A missing explicit DB remains a compatibility placeholder for a
            # native store whose frontend namespace has already disappeared.
            pass
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect explicit Cindy database {explicit_database}: {exc}"
            ) from exc
        else:
            if not stat.S_ISREG(explicit_status.st_mode):
                raise RuntimeError(
                    "Explicit Cindy database is not a regular file: "
                    f"{explicit_database}"
                )
        candidates.append(explicit_database)
    if not candidates:
        candidates.append(effective_root / "cindy-local-v1.db")

    profiles: list[CindyProfile] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda path: str(path).casefold()):
        key = _normalized_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        profiles.append(
            CindyProfile(
                root=effective_root,
                database=candidate,
                codex_home=effective_home,
            )
        )
    return tuple(profiles)


def _discover_cindy_databases(root: Path) -> tuple[Path, ...]:
    databases: list[Path] = []
    for pattern in ("cindy-*.db", "xdt-maker-*.db"):
        try:
            databases.extend(root.glob(pattern))
        except OSError as exc:
            raise RuntimeError(f"Could not enumerate Cindy profile {root}: {exc}") from exc
    databases = sorted(databases, key=lambda path: path.name.casefold())
    databases.extend(
        (
            root / "cindy-local-v1.db",
            root / "xdt-maker-local-v1.db",
            root / "cindy.db",
            root / "xdt-maker.db",
        )
    )
    return _unique_existing_files(
        [
            candidate
            for candidate in databases
            if not _is_database_sidecar_or_backup(candidate)
        ]
    )


def choose_codex_binary(hint: Path | None = None) -> Path | None:
    if hint is None:
        return discover_path_codex()
    try:
        expanded_hint = hint.expanduser()
        return expanded_hint if expanded_hint.is_file() else None
    except (OSError, RuntimeError):
        return None


def _unique_existing_files(candidates: list[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            candidate_status = candidate.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect frontend database candidate {candidate}: {exc}"
            ) from exc
        if not stat.S_ISREG(candidate_status.st_mode):
            raise RuntimeError(
                f"Frontend database candidate is not a regular file: {candidate}"
            )
        key = _normalized_path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


def _normalized_path_key(path: Path) -> str:
    return canonical_existing_path_key(path)


def _optional_directory_exists(path: Path) -> bool:
    try:
        path_status = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(f"Could not inspect frontend profile {path}: {exc}") from exc
    attributes = getattr(path_status, "st_file_attributes", 0)
    is_reparse = bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )
    if stat.S_ISLNK(path_status.st_mode) or is_reparse:
        raise RuntimeError(
            f"Frontend profile path is a symlink or reparse point: {path}"
        )
    if not stat.S_ISDIR(path_status.st_mode):
        raise RuntimeError(f"Frontend profile path is not a directory: {path}")
    return True


def _is_database_sidecar_or_backup(path: Path) -> bool:
    lowered = path.name.casefold()
    return (
        lowered.endswith(("-wal", "-shm", "-journal", ".bak", ".backup"))
        or ".backup." in lowered
        or "-backup." in lowered
        or ".bak." in lowered
        or lowered.startswith("backup-")
    )
