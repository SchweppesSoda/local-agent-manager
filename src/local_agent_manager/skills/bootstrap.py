"""Dependency bootstrapper and sole launcher for the existing mount core."""

from __future__ import annotations

import hashlib
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import BinaryIO, Iterator


REPO_ROOT = Path.cwd().resolve()
LOCK_FILE = REPO_ROOT / "requirements.lock"
_runtime_value = Path(os.environ.get("WORK_SKILLS_RUNTIME_DIR", ".work-skills-runtime")).expanduser()
RUNTIME_ROOT = _runtime_value if _runtime_value.is_absolute() else REPO_ROOT / _runtime_value
VENV_ROOT = RUNTIME_ROOT / "venv"
CORE = REPO_ROOT / "scripts" / "work_skills.py"
MARKER = VENV_ROOT / ".requirements.sha256"
RUNTIME_LOCK = RUNTIME_ROOT / ".bootstrap.lock"
LOCKFILE_CHANGE_RETRIES = 3


def _venv_python() -> Path:
    return VENV_ROOT / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _lock_digest() -> str:
    return hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest()


def _lock_wait_seconds() -> float:
    raw = os.environ.get("WORK_SKILLS_BOOTSTRAP_LOCK_TIMEOUT", "900")
    try:
        value = float(raw)
    except ValueError as exc:
        raise OSError(f"invalid WORK_SKILLS_BOOTSTRAP_LOCK_TIMEOUT: {raw!r}") from exc
    if value <= 0:
        raise OSError("WORK_SKILLS_BOOTSTRAP_LOCK_TIMEOUT must be positive")
    return value


def _acquire_file_lock(handle: BinaryIO) -> None:
    deadline = time.monotonic() + _lock_wait_seconds()
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                # The lock file was already opened successfully, so EACCES or
                # EAGAIN from LK_NBLCK denotes byte-lock contention. Invalid
                # handles and every other permanent error fail immediately.
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for runtime bootstrap lock: {RUNTIME_LOCK}") from exc
                time.sleep(0.05)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EINTR}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for runtime bootstrap lock: {RUNTIME_LOCK}") from exc
                time.sleep(0.05)


def _release_file_lock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _runtime_lock() -> Iterator[None]:
    """Use a crash-safe OS lock; the kernel releases it with the process."""

    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with RUNTIME_LOCK.open("a+b") as handle:
        _acquire_file_lock(handle)
        try:
            yield
        finally:
            _release_file_lock(handle)


def _run_quiet(command: list[str]) -> bool:
    try:
        return subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def _runtime_ready(python: Path, digest: str) -> bool:
    try:
        marker_matches = MARKER.is_file() and MARKER.read_text(encoding="ascii").strip() == digest
    except (OSError, UnicodeError):
        marker_matches = False
    return marker_matches and _run_quiet([str(python), "-c", "import jsonschema, yaml"])


def _same_executable(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _ensure_pip(python: Path) -> None:
    if _run_quiet([str(python), "-m", "pip", "--version"]):
        return
    if _run_quiet([str(python), "-m", "ensurepip", "--upgrade"]) and _run_quiet(
        [str(python), "-m", "pip", "--version"]
    ):
        return
    # An incomplete venv can be rebuilt safely by an external interpreter.
    # A bootstrap currently running from that venv must fail after removing
    # its ready marker; the next wrapper invocation will choose a base Python.
    if _same_executable(Path(sys.executable), python):
        raise OSError(f"managed runtime cannot repair its own pip installation: {python}")
    subprocess.run([sys.executable, "-m", "venv", "--clear", str(VENV_ROOT)], check=True)
    if not _run_quiet([str(python), "-m", "pip", "--version"]):
        raise OSError(f"managed runtime has no usable pip after rebuild: {python}")


def _write_ready_marker(digest: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".requirements.", suffix=".tmp", dir=str(VENV_ROOT))
    path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            handle.write(digest + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path, MARKER)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _ensure_runtime() -> Path:
    """Provision and reuse the ignored, lock-pinned manager runtime."""

    python = _venv_python()
    with _runtime_lock():
        for _attempt in range(LOCKFILE_CHANGE_RETRIES):
            # Every waiter reads the digest only after the provisioning owner
            # has exited the critical section.
            digest = _lock_digest()
            if _runtime_ready(python, digest) and _lock_digest() == digest:
                return python
            try:
                MARKER.unlink()
            except FileNotFoundError:
                pass
            if not python.is_file():
                subprocess.run([sys.executable, "-m", "venv", str(VENV_ROOT)], check=True)
            _ensure_pip(python)
            subprocess.run(
                [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(LOCK_FILE)],
                check=True,
            )
            if _lock_digest() != digest:
                continue
            if not _run_quiet([str(python), "-c", "import jsonschema, yaml"]):
                raise OSError("managed runtime dependency import check failed after provisioning")
            _write_ready_marker(digest)
            if _lock_digest() == digest:
                return python
            try:
                MARKER.unlink()
            except FileNotFoundError:
                pass
        raise OSError("requirements.lock changed repeatedly during runtime provisioning; retry when edits stop")


def main(argv: list[str]) -> int:
    if sys.version_info < (3, 10):
        print("work-skills requires Python 3.10 or newer; no package manager was invoked", file=sys.stderr)
        return 2
    try:
        python = _ensure_runtime()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"work-skills bootstrap failed: {exc}", file=sys.stderr)
        print(f"Install dependencies with: {sys.executable} -m pip install -r {LOCK_FILE}", file=sys.stderr)
        return 2
    environment = os.environ.copy()
    package_parent = str(Path(__file__).resolve().parents[2])
    previous = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = package_parent + (os.pathsep + previous if previous else "")
    return subprocess.run([str(python), str(CORE), *argv], cwd=REPO_ROOT, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
