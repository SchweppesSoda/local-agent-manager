"""Copied verbatim into the reviewed one-task bundle; no mutable import first."""
import atexit
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys


_handles = []
_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
_kernel.CreateFileW.restype = wintypes.HANDLE
_kernel.CloseHandle.argtypes = (wintypes.HANDLE,)


def _close():
    while _handles: _kernel.CloseHandle(_handles.pop())
atexit.register(_close)


def _plain(path, directory=False):
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024 or (not directory and info.st_nlink != 1):
        raise SystemExit('Reviewed path became a link')
    return info


_parents = set()
def _pin(path, directory=False):
    path = Path(path)
    for parent in reversed(path.parents):
        if str(parent) not in _parents:
            _plain(parent, directory=True)
            handle = _kernel.CreateFileW(str(parent), 0x80000000, 3, None, 3, 0x2200000, None)
            if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
            _handles.append(handle); _parents.add(str(parent))
    _plain(path, directory=directory)
    handle = _kernel.CreateFileW(str(path), 0x80000000, 3 if directory else 1, None, 3,
                                  0x200000 | (0x2000000 if directory else 0), None)
    if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
    _handles.append(handle)


def _digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def _private_admin_cache(path):
    """Atomic new directory ACL; no existing software/store/lock ACL changes."""
    class SA(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                                                                         ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW('O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)', 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        sa = SA(ctypes.sizeof(SA), descriptor, False)
        _kernel.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SA)); _kernel.CreateDirectoryW.restype = wintypes.BOOL
        if not _kernel.CreateDirectoryW(str(path), ctypes.byref(sa)): raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel.LocalFree.argtypes = (ctypes.c_void_p,); _kernel.LocalFree(descriptor)
    _pin(path, directory=True)


def main():
    bundle = Path(__file__).resolve().parent
    try: approved = sys.argv[sys.argv.index('--manifest-sha256') + 1]
    except (ValueError, IndexError): raise SystemExit('Exact --manifest-sha256 is required')
    if not re.fullmatch('[0-9a-f]{64}', approved): raise SystemExit('Invalid manifest hash')
    manifest = bundle / 'execution-manifest.json'
    _pin(manifest)
    if _digest(manifest) != approved: raise SystemExit('Manifest hash mismatch')
    data = json.loads(manifest.read_text(encoding='utf8'))
    if Path(sys.executable) != Path(data['python_executable']): raise SystemExit('Reviewed Python executable required')
    expected_source = bundle.parents[2] / 'src'
    if Path(data['source_path']) != expected_source: raise SystemExit('Not the authoritative checkout source')
    spec_path=bundle/'execution-spec.json';_pin(spec_path)
    spec_entry=next(e for e in data['files'] if Path(e['path'])==spec_path)
    if _digest(spec_path)!=spec_entry['sha256']:raise SystemExit('Reviewed spec changed')
    spec = json.loads(spec_path.read_text(encoding='utf8'))
    startup = {os.path.normcase(str(Path(entry['root']) / entry['file']['path'])) for entry in spec['controller']['startup_files']}
    for entry in data['files']:
        path = Path(entry['path'])
        # The product boundary needs its own EXCLUSIVE launcher handles. All
        # other reviewed code/data/native runtime files keep READ-sharing pins
        # for this process's full bounded lifetime, including lazy imports.
        if os.path.normcase(str(path)) not in startup: _pin(path)
        else:
            if sys.argv[1:2] and sys.argv[1] in {'ui','status','verify'}:continue
            _plain(path)
        if _digest(path) != entry['sha256']: raise SystemExit('Reviewed execution file changed')
    if sys.argv[1:2] and sys.argv[1] in {'internal-supervisor', 'internal-direct'}:
        try: run_name = sys.argv[sys.argv.index('--run') + 1]
        except (ValueError, IndexError): raise SystemExit('Bound run required')
        if not re.fullmatch('run-[0-9a-f]{24}', run_name): raise SystemExit('Invalid run')
        run = bundle / 'runs' / run_name
        _pin(run, directory=True)
        cache = run / ('admin-cache-' + secrets.token_hex(16))
        _private_admin_cache(cache)
        # Read no caller-writable pre-existing .pyc during deferred privileged
        # imports. The reviewed .py files remain pinned; nothing is installed.
        sys.pycache_prefix = str(cache)
        sys.dont_write_bytecode = True
    sys.path.insert(0, data['source_path'])
    from local_agent_manager.cindy_isolation_entry import main as entry_main
    return entry_main(bundle, approved)


if __name__ == '__main__': raise SystemExit(main())
