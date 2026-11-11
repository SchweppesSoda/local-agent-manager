"""Read only CODEX_HOME from a verified live x64 Windows process.

PEB/RTL_USER_PROCESS_PARAMETERS layout follows System Informer's phnt headers:
https://github.com/winsiderss/phnt/blob/master/ntpebteb.h
https://github.com/winsiderss/phnt/blob/master/ntrtl.h
No environment block, command line or credential enters the returned evidence.
Unsupported architectures, inaccessible or changing processes remain unproven.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import platform


def _extract_home(raw):
    text = raw.decode('utf-16-le', errors='strict')
    end = text.find('\0\0')
    if end < 0:
        raise ValueError('process_environment_unterminated')
    values = [entry.partition('=')[2] for entry in text[:end].split('\0')
              if entry.partition('=')[0].casefold() == 'codex_home']
    if len(values) != 1 or not values[0] or any(ord(ch) < 32 for ch in values[0]):
        raise ValueError('process_home_absent_or_ambiguous')
    return values[0]


def read_codex_home(pid, executable, creation_ticks):
    """Return an explicit home only when image and creation identity match CIM."""
    if os.name != 'nt' or c.sizeof(c.c_void_p) != 8 or platform.machine().casefold() not in {'amd64', 'x86_64'}:
        raise OSError('process_environment_architecture_unsupported')
    if not executable or not creation_ticks:
        raise ValueError('process_identity_missing')
    kernel = c.WinDLL('kernel32', use_last_error=True)
    nt = c.WinDLL('ntdll')
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.GetProcessTimes.argtypes = [w.HANDLE, c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME)]
    kernel.IsWow64Process.argtypes = [w.HANDLE, c.POINTER(w.BOOL)]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    nt.NtQueryInformationProcess.argtypes = [w.HANDLE, w.ULONG, c.c_void_p, w.ULONG, c.POINTER(w.ULONG)]
    nt.NtQueryInformationProcess.restype = w.LONG
    handle = kernel.OpenProcess(0x0400 | 0x0010 | 0x100000, False, int(pid))
    if not handle:
        raise c.WinError(c.get_last_error())
    try:
        wow = w.BOOL()
        if not kernel.IsWow64Process(handle, c.byref(wow)) or wow.value:
            raise OSError('process_environment_architecture_unsupported')
        image = c.create_unicode_buffer(32768)
        length = w.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, c.byref(length)):
            raise c.WinError(c.get_last_error())
        if not Path(image.value).samefile(executable):
            raise ValueError('process_image_changed')
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(c.byref(t) for t in times)):
            raise c.WinError(c.get_last_error())
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        # CIM DateTime has microsecond precision; compare at that precision.
        if (ticks + 504911232000000000) // 10 != int(creation_ticks) // 10:
            raise ValueError('process_creation_changed')

        def read(address, size):
            result = c.create_string_buffer(size)
            count = c.c_size_t()
            if not address or not kernel.ReadProcessMemory(handle, address, result, size, c.byref(count)) or count.value != size:
                raise OSError('process_memory_unreadable')
            return result.raw

        # PROCESS_BASIC_INFORMATION: exit status, PEB, affinity, priority, PID, PPID.
        basic = (c.c_uint64 * 6)()
        returned = w.ULONG()
        if nt.NtQueryInformationProcess(handle, 0, basic, c.sizeof(basic), c.byref(returned)) < 0:
            raise OSError('process_peb_unreadable')
        peb = basic[1]
        params = int.from_bytes(read(peb + 0x20, 8), 'little')
        environment = int.from_bytes(read(params + 0x80, 8), 'little')

        def snapshot():
            # Read page-bounded chunks until the aligned UTF-16 terminator.
            raw = bytearray()
            while len(raw) < 1024 * 1024:
                address = environment + len(raw)
                size = min(4096 - address % 4096, 1024 * 1024 - len(raw))
                raw.extend(read(address, size))
                for offset in range(max(0, len(raw) - size - 2), len(raw) - 3, 2):
                    if raw[offset:offset + 4] == b'\0\0\0\0':
                        return bytes(raw[:offset + 4])
            raise ValueError('process_environment_budget')

        before = snapshot()
        home = _extract_home(before)
        if (before != snapshot() or int.from_bytes(read(peb + 0x20, 8), 'little') != params
                or int.from_bytes(read(params + 0x80, 8), 'little') != environment
                or kernel.WaitForSingleObject(handle, 0) != 258):
            raise ValueError('process_environment_changed')
        return home
    finally:
        kernel.CloseHandle(handle)
