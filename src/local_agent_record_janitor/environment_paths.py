"""Filesystem metadata probes for agent environments. Never follows redirects."""
from __future__ import annotations

import os
import stat
from itertools import islice
from pathlib import Path
from typing import Callable

from .path_identity import is_local_absolute_locator

MAX_PATH_PROBES = 4096
MAX_DIRECTORY_ENTRIES = 128
MAX_PATH_DEPTH = 64


def local_path(value: str | Path) -> Path | None:
    raw = os.fspath(value)
    if (
        len(raw) > 4096
        or not is_local_absolute_locator(raw)
        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in raw)
    ):
        return None
    path = Path(raw)
    if ".." in path.parts or len(path.parts) > MAX_PATH_DEPTH:
        return None
    if os.name == "nt" and any(
        ":" in part
        or part.endswith((" ", "."))
        or _is_reserved(part)
        for part in path.parts[1:]
    ):
        return None
    return path


def _is_reserved(value: str) -> bool:
    check = getattr(os.path, "isreserved", None)
    return check(value) if check is not None else Path(value).is_reserved()


def _is_local_drive(path: Path) -> bool:
    if os.name != "nt":
        return True
    # Drive-qualified paths can still be mapped SMB shares. Do not issue
    # filesystem requests on a drive that Windows does not report as local.
    import ctypes
    from ctypes import wintypes

    get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT
    anchor = path.anchor.removeprefix("\\\\?\\")
    return get_drive_type(anchor) in (2, 3, 5, 6)


def is_redirected(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


class BoundedPathProbe:
    def __init__(self, *, drive_is_local: Callable[[Path], bool] | None = None) -> None:
        self._drive_is_local = drive_is_local or _is_local_drive
        self._drive_cache: dict[str, bool] = {}
        self._paths: dict[str, tuple[str, os.stat_result | None]] = {}
        self.issues: list[dict[str, str | None]] = []
        self._seen_issues: set[tuple[str, str, str | None]] = set()

    @property
    def count(self) -> int:
        return len(self._paths)

    def issue(self, probe: str, code: str, path: Path | None = None) -> None:
        value = str(path) if path is not None else None
        key = (probe, code, value)
        if key not in self._seen_issues and len(self.issues) < MAX_PATH_PROBES:
            self._seen_issues.add(key)
            self.issues.append({"probe": probe, "code": code, "path": value})

    def _entry(self, path: Path) -> tuple[str, os.stat_result | None]:
        key = str(path)
        if key in self._paths:
            return self._paths[key]
        if len(self._paths) >= MAX_PATH_PROBES:
            return "path_budget_exceeded", None
        if path.parent != path:
            parent_status, parent = self._entry(path.parent)
            if parent_status != "present":
                return parent_status, None
            if parent is None or not stat.S_ISDIR(parent.st_mode):
                return "unexpected_type", None
        else:
            if path.anchor not in self._drive_cache:
                try:
                    self._drive_cache[path.anchor] = self._drive_is_local(path)
                except OSError:
                    return "unreadable", None
            if not self._drive_cache[path.anchor]:
                return "nonlocal_drive", None
        if len(self._paths) >= MAX_PATH_PROBES:
            return "path_budget_exceeded", None
        try:
            info = path.lstat()
        except FileNotFoundError:
            result = ("missing", None)
        except (OSError, ValueError):
            result = ("unreadable", None)
        else:
            result = ("redirected", None) if is_redirected(info) else ("present", info)
        self._paths[key] = result
        return result

    def inspect(
        self, path: Path, probe: str, *, directory: bool | None = None
    ) -> tuple[str, os.stat_result | None]:
        if local_path(path) is None:
            self.issue(probe, "nonlocal_or_opaque_path")
            return "nonlocal_or_opaque_path", None
        status, info = self._entry(path)
        if status == "present":
            expected = (
                stat.S_ISDIR if directory is True else
                stat.S_ISREG if directory is False else None
            )
            if info is None or (expected is not None and not expected(info.st_mode)):
                status, info = "unexpected_type", None
        if status not in ("present", "missing"):
            self.issue(probe, status, path)
        return status, info

    def direct_link_target(self, path: Path, probe: str) -> tuple[bool, Path | None]:
        """Read one link name. Callers must prove its target by exact layout."""
        if local_path(path) is None or self._entry(path)[0] != "redirected":
            return False, None
        if self._entry(path.parent)[0] != "present":
            self.issue(probe, "redirected", path)
            return True, None
        try:
            raw = os.readlink(path)
        except (OSError, ValueError):
            self.issue(probe, "unreadable_redirect", path)
            return True, None
        if os.name == "nt":
            # readlink supplies this spelling for local installer junctions.
            # local_path rejects names with ambiguous trailing dots/spaces.
            raw = raw.removeprefix("\\\\?\\")
        target = local_path(raw)
        if target is None:
            self.issue(probe, "unproven_redirect_target", path)
        return True, target

    def children(self, path: Path, probe: str) -> tuple[Path, ...]:
        """List one plain directory or report incomplete coverage; never recurse."""
        status, before = self.inspect(path, probe, directory=True)
        if status != "present" or before is None:
            return ()
        try:
            with os.scandir(path) as entries:
                names = list(islice((entry.name for entry in entries), MAX_DIRECTORY_ENTRIES + 1))
            after = path.lstat()
            if is_redirected(after) or (
                before.st_dev, before.st_ino, before.st_mtime_ns
            ) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                self.issue(probe, "directory_changed", path)
                return ()
        except OSError:
            self.issue(probe, "enumeration_failed", path)
            return ()
        if len(names) > MAX_DIRECTORY_ENTRIES:
            # Avoid presenting an order-dependent subset as the newest or only entry.
            self.issue(probe, "directory_budget_exceeded", path)
            return ()
        return tuple(path / name for name in sorted(names, key=lambda name: (name.casefold(), name)))
