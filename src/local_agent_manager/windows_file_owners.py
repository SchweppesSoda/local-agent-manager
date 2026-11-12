"""Read current file users via Restart Manager; never call RmShutdown.

https://learn.microsoft.com/windows/win32/api/restartmanager/nf-restartmanager-rmgetlist
An empty result is not proof that a store is idle. Mutation guards still apply.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class FileOwnersError(RuntimeError):
    pass


class UniqueProcess(ctypes.Structure):
    _fields_ = [('pid', wintypes.DWORD), ('started', wintypes.FILETIME)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('process', UniqueProcess), ('name', wintypes.WCHAR * 256),
                ('service', wintypes.WCHAR * 64), ('kind', ctypes.c_int),
                ('status', wintypes.ULONG), ('session', wintypes.DWORD), ('restartable', wintypes.BOOL)]


def file_owners(paths):
    paths = sorted({str(Path(p).absolute()) for p in paths if Path(p).is_file()})
    if not paths:
        return []
    if os.name != 'nt':
        raise FileOwnersError('windows_file_owners_unavailable')
    rm = ctypes.WinDLL('Rstrtmgr')
    rm.RmStartSession.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR)
    rm.RmRegisterResources.argtypes = (wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT, ctypes.POINTER(UniqueProcess), wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR))
    rm.RmGetList.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(ProcessInfo), ctypes.POINTER(wintypes.DWORD))
    rm.RmEndSession.argtypes = (wintypes.DWORD,)
    for name in ('RmStartSession', 'RmRegisterResources', 'RmGetList', 'RmEndSession'):
        getattr(rm, name).restype = wintypes.DWORD

    def check(code):
        if code:
            raise FileOwnersError(f'windows_file_owners_error:{code}')

    session, key = wintypes.DWORD(), ctypes.create_unicode_buffer(33)
    check(rm.RmStartSession(ctypes.byref(session), 0, key))
    try:
        names = (wintypes.LPCWSTR * len(paths))(*paths)
        check(rm.RmRegisterResources(session, len(paths), names, 0, None, 0, None))
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        buffer = None
        for _ in range(5):
            code = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), buffer, ctypes.byref(reasons))
            if code == 234:
                if needed.value > 4096:
                    raise FileOwnersError('windows_file_owners_budget_exceeded')
                count.value = needed.value
                buffer = (ProcessInfo * count.value)()
                continue
            check(code)
            return [{'pid': row.process.pid,
                     'created_unix': ((row.process.started.dwHighDateTime << 32)
                        | row.process.started.dwLowDateTime) / 10_000_000 - 11_644_473_600,
                     'source': 'windows_restart_manager', 'resources': paths}
                    for row in list(buffer or ())[:count.value]]
        raise FileOwnersError('windows_file_owners_unstable')
    finally:
        check(rm.RmEndSession(session))
