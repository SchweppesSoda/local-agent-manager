"""Windows sharing-mode leases for a frozen, explicitly owned file set.

Unlike a sidecar lock, these handles prevent ordinary readers, writers,
renames and launches. Missing files are never created by this primitive.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path

from . import frozen_files


def ensure_descriptor_capacity(minimum):
    """Raise only this process's CRT descriptor table for a bounded lease set."""
    if os.name != 'nt' or minimum > 8192:
        raise frozen_files.FrozenFilesError('windows_file_lease_budget_exceeded')
    import ctypes
    crt = ctypes.CDLL('ucrtbase', use_errno=True)
    crt._getmaxstdio.restype = ctypes.c_int
    crt._setmaxstdio.argtypes = (ctypes.c_int,)
    crt._setmaxstdio.restype = ctypes.c_int
    if crt._getmaxstdio() < minimum and crt._setmaxstdio(8192) != 8192:
        raise frozen_files.FrozenFilesError('windows_file_lease_budget_unavailable')


def exclusive_fd(path, *, writable=False, delete=False, directory=False):
    if os.name != "nt":
        raise frozen_files.FrozenFilesError("windows_file_lease_unavailable")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000 | (0x40000000 if writable else 0) | (0x10000 if delete else 0),
                                0, None, 3, 0x200000 | (0x2000000 if directory else 0), None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    os.set_inheritable(fd, False)
    return fd


@contextmanager
def pipe_lease(name):
    if os.name != "nt" or not name.startswith("\\\\.\\pipe\\"):
        raise frozen_files.FrozenFilesError("windows_pipe_lease_unavailable")
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    kernel.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # First instance plus maxInstances=1 excludes competing servers, including
    # ones that do not request FILE_FLAG_FIRST_PIPE_INSTANCE themselves.
    handle = kernel.CreateNamedPipeW(name, 3 | 0x80000, 0, 1, 4096, 4096, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class HeldFiles:
    def __init__(self):
        self.stack = ExitStack()
        self.handles = {}
        self.parents = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def acquire(self, root, evidence, *, writable=False, delete=False):
        root = Path(root)
        key = (str(root), evidence["path"])
        if key in self.handles:
            raise frozen_files.FrozenFilesError("duplicate_file_lease")
        path = frozen_files.checked_path(root, evidence["path"])
        # One fence per directory for this complete lease lifetime. Reopening
        # the same ancestors for every file wastes CRT descriptors and does
        # not strengthen the namespace exclusion. Revalidate every reuse.
        for parent in reversed(path.parents):
            before = parent.lstat()
            frozen_files._plain(before, directory=True)
            key_parent = str(parent)
            if key_parent not in self.parents:
                parent_fd = frozen_files._windows_fd(parent, directory=True)
                self.stack.callback(os.close, parent_fd)
                self.parents[key_parent] = parent_fd
            opened = os.fstat(self.parents[key_parent])
            frozen_files._plain(opened, directory=True)
            if frozen_files._identity(opened) != frozen_files._identity(before):
                raise frozen_files.FrozenFilesError('client_file_parent_replaced')
        fd = exclusive_fd(path, writable=writable, delete=delete)
        self.stack.callback(os.close, fd)
        self.handles[key] = (fd, writable)
        current, _ = self.read(root, evidence["path"])
        if current != evidence:
            raise frozen_files.FrozenFilesError("held_file_evidence_changed")

    def dispose(self, root, relative, *, before):
        """Delete an already leased file, retaining its pending-delete handle.

        The caller must acquire DELETE access and hold the lease until every
        other file in its transaction has been checked. No pathname reopen or
        release/reacquire window is used.
        """
        import ctypes
        from ctypes import wintypes
        import msvcrt
        current, _ = self.read(root, relative)
        if current != before:
            raise frozen_files.FrozenFilesError("held_file_evidence_changed")
        fd, _ = self.handles[(str(root), relative)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetFileInformationByHandle.restype = wintypes.BOOL
        disposition = wintypes.BOOL(True)
        if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 4,
                                                 ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, root, relative, *, content=False, limit=frozen_files.MAX_BYTES):
        fd, _ = self.handles[(str(root), relative)]
        path = frozen_files.checked_path(Path(root), relative)
        before = os.fstat(fd)
        frozen_files._plain(before)
        if (frozen_files._revision(path.lstat()) != frozen_files._revision(before)
                or before.st_size > limit):
            raise frozen_files.FrozenFilesError("held_file_evidence_changed")
        os.lseek(fd, 0, os.SEEK_SET)
        chunks, digest, size = [], hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise frozen_files.FrozenFilesError("held_file_budget_exceeded")
            digest.update(chunk)
            if content:
                chunks.append(chunk)
        if frozen_files._revision(os.fstat(fd)) != frozen_files._revision(before):
            raise frozen_files.FrozenFilesError("held_file_evidence_changed")
        return {"path": relative, "identity": frozen_files._identity(before), "size": size,
                "mode": frozen_files._mode(before), "mtime_ns": before.st_mtime_ns,
                "sha256": digest.hexdigest()}, b"".join(chunks) if content else None

    def replace(self, root, relative, body, *, before, after_sha256):
        fd, writable = self.handles[(str(root), relative)]
        current, _ = self.read(root, relative)
        if not writable or current != before or hashlib.sha256(body).hexdigest() != after_sha256:
            raise frozen_files.FrozenFilesError("held_file_approval_changed")
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = memoryview(body)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("held_file_short_write")
            remaining = remaining[count:]
        os.ftruncate(fd, len(body))
        os.fsync(fd)
        observed, _ = self.read(root, relative)
        if observed["sha256"] != after_sha256 or observed["size"] != len(body):
            raise frozen_files.FrozenFilesError("held_file_after_unverified")
