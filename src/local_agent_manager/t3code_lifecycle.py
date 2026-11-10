"""Windows writer exclusion using T3's own server-owner SQLite lock."""
from contextlib import closing, contextmanager, ExitStack
from contextvars import ContextVar
import os
from pathlib import Path
import sqlite3

from . import frozen_files
from .record_identity import canonical_path
from .t3code_store import fail
from .windows_held_files import HeldFiles

_active = ContextVar("t3code_lifecycle", default=None)
_held = ContextVar("t3code_lifecycle_files", default=None)
OWNER = "userdata/server-owner.sqlite"


def current_boundary(root):
    value = _active.get()
    return value if value and canonical_path(value["root"]) == canonical_path(root) else None


def read_binary(root, binary, *, content=False):
    """Read only an exact runtime admitted by this profile's active hold.

    No cached-proof exemption and no release/reopen window: recheck identity,
    revision and bytes through the original exclusive handle on every read.
    Outside a lifecycle boundary the normal path reader remains authoritative.
    """
    boundary = current_boundary(root)
    binary = Path(binary)
    if boundary is None:
        if content:
            fail('runtime_not_held')
        return frozen_files.read_file(binary.parent, binary.name)[0]
    matches = [item for item in boundary['lifecycle']['binaries']
               if canonical_path(Path(item['root'])/item['file']['path']) == canonical_path(binary)]
    if len(matches) != 1 or _held.get() is None:
        fail('runtime_not_held')
    item = matches[0]
    proof, raw = _held.get().read(Path(item['root']), item['file']['path'], content=content)
    if proof != item['file']:
        fail('held_lifecycle_evidence_changed')
    return (proof, raw) if content else proof


def freeze(manifest):
    if os.name != "nt":
        fail("held_lifecycle_platform_unqualified")
    root = Path(manifest["profile_root"])
    environment, raw = frozen_files.read_file(root, "userdata/environment-id", content=True, limit=256)
    if raw.decode("utf-8").strip() != manifest["environment_id"]:
        fail("environment_identity_changed")
    owner = frozen_files.checked_path(root, OWNER)
    frozen_files._plain(owner.lstat())
    binaries = list(manifest["runtime_binaries"])
    binaries.extend(binary for profile in manifest["desktop_profiles"] for binary in profile["runtime_binaries"])
    unique = {canonical_path(binary): Path(binary) for binary in binaries}
    return {"schema_version": "larj.t3code-lifecycle.windows.v1", "environment_file": environment,
        "owner_identity": frozen_files._identity(owner.lstat()),
        "binaries": [{"root": str(path.parent), "file": frozen_files.read_file(path.parent, path.name)[0]}
            for _, path in sorted(unique.items())]}


def require_closed(evidence, *, census=None):
    from .office_runtime import processes, _RUNTIMES
    rows = (census or processes)()
    if not isinstance(rows, (list, tuple)) or len(rows) > 10000:
        fail("writer_census_unverified")
    binaries = {canonical_path(Path(i["root"]) / i["file"]["path"]) for i in evidence["lifecycle"]["binaries"]}
    roots = [evidence["root"], *(p["root"] for p in evidence["binding_manifest"]["desktop_profiles"])]
    for row in rows:
        if not isinstance(row, dict) or type(row.get("pid")) is not int or type(row.get("parent")) is not int:
            fail("writer_census_unverified")
        if row["pid"] == os.getpid():
            continue
        name = str(row.get("name") or "").casefold()
        executable, command = row.get("executable"), row.get("command")
        if name.startswith(("t3 code", "t3code")) or executable and canonical_path(executable) in binaries:
            fail("writer_running")
        if name in _RUNTIMES:
            if not isinstance(executable, str) or not isinstance(command, str):
                fail("writer_census_unverified")
            command = command.replace("\\", "/").casefold()
            if "t3code" in command or any(str(root).replace("\\", "/").casefold() in command for root in roots):
                fail("writer_running")


@contextmanager
def hold(evidence, *, census=None):
    root = Path(evidence["root"])
    if current_boundary(root) or freeze(evidence["binding_manifest"]) != evidence["lifecycle"]:
        fail("held_lifecycle_evidence_changed")
    require_closed(evidence, census=census)
    with ExitStack() as stack:
        held = stack.enter_context(HeldFiles())
        for item in evidence["lifecycle"]["binaries"]:
            held.acquire(Path(item["root"]), item["file"])
        path = stack.enter_context(frozen_files._parent_fence(root, OWNER))
        fd = frozen_files._windows_fd(path)
        stack.callback(os.close, fd)
        if frozen_files._identity(os.fstat(fd)) != evidence["lifecycle"]["owner_identity"]:
            fail("owner_lock_replaced")
        # Never unlink this permanent lock file. T3 uses the same transaction;
        # an existing owner or a racing startup prevents admission here.
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            try:
                frozen_files._plain(sidecar.lstat())
            except FileNotFoundError:
                pass
        db = stack.enter_context(closing(sqlite3.connect(path, timeout=0)))
        db.execute("BEGIN EXCLUSIVE")
        if db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            fail("owner_lock_schema_unverified")
        require_closed(evidence, census=census)
        token = _active.set(evidence)
        held_token = _held.set(held)
        try:
            yield
        finally:
            _held.reset(held_token)
            _active.reset(token)
