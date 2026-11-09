"""Cross-source locks for shared deployment roots and discovery files."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path


class SharedResourceBusyError(RuntimeError):
    pass


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SharedResourceBusyError("Another source is modifying a shared Skill resource") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def shared_resource_locks(paths):
    base = os.environ.get("LOCAL_AGENT_MANAGER_LOCK_DIR")
    if not base:
        base = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
                if os.name == "nt" else Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"))
        base = Path(base) / "local-agent-manager/locks"
    keys = sorted({os.path.normcase(os.path.realpath(path)) for path in paths})
    with ExitStack() as stack:
        for key in keys:
            name = hashlib.sha256(os.fsencode(key)).hexdigest() + ".lock"
            stack.enter_context(_lock(Path(base) / name))
        yield
