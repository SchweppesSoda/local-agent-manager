"""Explicit, run-bound user consent for a future lifecycle support entry.

This is not deletion authority or a recovery protocol. No process is enumerated,
closed, started or restarted here. Frozen controllers must not be patched to
retroactively claim this consent. A new caller must observe identities afresh.
"""
from dataclasses import dataclass
import math
import os
import time

from .windows_dialogs import ConfirmationDialog, show_confirmation


@dataclass(frozen=True)
class LifecycleTarget:
    software: str
    pid: int
    created: str
    executable: str
    home: str
    affected_tasks: tuple[str, ...]
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecycleScope:
    run_id: str
    targets: tuple[LifecycleTarget, ...]
    actions: tuple[str, ...] = ('close',)
    reason: str = ''
    details: str = ''

    def validate(self):
        if (not self.run_id or not self.reason.strip() or not self.targets or not self.actions
                or len(set(self.targets)) != len(self.targets)
                or len({t.pid for t in self.targets}) != len(self.targets)
                or len(set(self.actions)) != len(self.actions)
                or any(a not in ('close', 'restart') for a in self.actions)):
            raise ValueError('exact lifecycle scope required')
        for t in self.targets:
            values = (t.software, t.created, t.executable, t.home, *t.affected_tasks, *t.details)
            if (t.pid <= 0 or not t.affected_tasks
                    or any(not v or any(ord(c) < 32 for c in v) for v in values)):
                raise ValueError('software, process identity and affected tasks required')
        if any(ord(c) < 32 for c in self.run_id):
            raise ValueError('invalid run identity')
        if any(ord(c) < 32 and c != '\n' for c in self.reason + self.details):
            raise ValueError('invalid confirmation text')


def is_interactive_windows_desktop():
    """Return (is_default_visible_desktop, label) for the current Win32 thread."""
    if os.name != 'nt':
        return False, 'non-windows'
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    user.GetProcessWindowStation.restype = wintypes.HANDLE
    user.GetThreadDesktop.argtypes = (wintypes.DWORD,)
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    )
    user.GetUserObjectInformationW.restype = wintypes.BOOL

    def _obj_name(handle):
        if not handle:
            return ''
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return ''
        return buf.value

    class _USEROBJECTFLAGS(ctypes.Structure):
        _fields_ = [('fInherit', wintypes.BOOL), ('fReserved', wintypes.BOOL), ('dwFlags', wintypes.DWORD)]

    winsta = user.GetProcessWindowStation()
    desk = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    winsta_name = _obj_name(winsta)
    desk_name = _obj_name(desk)
    flags = _USEROBJECTFLAGS()
    needed = wintypes.DWORD()
    visible = bool(
        winsta
        and user.GetUserObjectInformationW(winsta, 1, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed))
        and (flags.dwFlags & 0x1)
    )
    label = f'{winsta_name}\\{desk_name}' if (winsta_name or desk_name) else 'unknown'
    ok = visible and winsta_name.casefold() == 'winsta0' and desk_name.casefold() == 'default'
    return ok, label


def windows_confirmation(message):
    """Interactive only. Cancel is default; never substitute --yes or a receipt."""
    if os.name != 'nt':
        raise RuntimeError('interactive Windows confirmation unavailable')
    ok, desk_label = is_interactive_windows_desktop()
    if not ok:
        raise RuntimeError(f'interactive Windows desktop unavailable ({desk_label})')
    if isinstance(message, str):
        message = ConfirmationDialog('是否继续？', message, '继续')
    return show_confirmation(message)


def lifecycle_dialog(scope: LifecycleScope) -> ConfirmationDialog:
    scope.validate()
    action = '关闭' if 'close' in scope.actions else '重新打开'
    content = [scope.reason, f'将{action}：']
    details = [f'本次操作编号：{scope.run_id}',
               '操作：' + '、'.join('关闭' if a == 'close' else '重新打开' for a in scope.actions)]
    for target in scope.targets:
        content.append(f'• {target.software}\n  ' + '；'.join(target.affected_tasks))
        details.extend((f'\n{target.software}（PID {target.pid}）',
                        f'进程启动时间：{target.created}', f'程序路径：{target.executable}',
                        f'数据目录：{target.home}', *target.details))
    if scope.details:
        details.append('\n' + scope.details)
    return ConfirmationDialog(
        instruction=f'需要{action}以下软件', content='\n\n'.join(content),
        confirm_label=f'{action}并继续', details='\n'.join(details),
    )


class LifecycleGate:
    """In-memory admission, bound to this run, exact scope and fresh identities.

    prompt/clock injection is for isolated tests and a trusted host UI adapter,
    never user-supplied JSON. A reconstructed gate is unconfirmed. Each target
    action is single use; neither an old cleanup approval nor an on-disk arm
    file is accepted. Callers still own normal startup fences and exit proof.
    """
    def __init__(self, scope, *, prompt=windows_confirmation, clock=time.monotonic, ttl=300):
        scope.validate()
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('finite confirmation lifetime required')
        self.scope, self._prompt, self._clock, self._ttl = scope, prompt, clock, ttl
        self._confirmed_until = None
        self._armed = False
        self._used = set()

    def confirm(self):
        if self._confirmed_until is not None or self._armed:
            raise RuntimeError('new lifecycle admission required')
        if self._prompt(lifecycle_dialog(self.scope)) is not True:
            raise RuntimeError('user lifecycle confirmation declined')
        self._confirmed_until = self._clock() + self._ttl

    def _require(self, run_id):
        if (run_id != self.scope.run_id or self._confirmed_until is None
                or self._clock() >= self._confirmed_until):
            raise RuntimeError('fresh run-bound lifecycle confirmation required')

    def arm(self, current_scope):
        self._require(current_scope.run_id)
        if current_scope != self.scope or self._armed:
            raise RuntimeError('lifecycle scope changed or already armed')
        self._armed = True

    def perform(self, action, *, current_run_id, current_target, execute):
        self._require(current_run_id)
        key = (action, current_target)
        if (not self._armed or action not in self.scope.actions
                or current_target not in self.scope.targets or key in self._used):
            raise RuntimeError('confirmed lifecycle identity/action mismatch')
        # Reserve before dispatch: an ambiguous callback must not be replayed.
        self._used.add(key)
        return execute(current_target)
