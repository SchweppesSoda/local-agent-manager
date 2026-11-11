"""Explicit, run-bound user consent for a future lifecycle support entry.

This is not deletion authority or a recovery protocol. No process is enumerated,
closed, started or restarted here. Frozen controllers must not be patched to
retroactively claim this consent. A new caller must observe identities afresh.
"""
from dataclasses import dataclass
import math
import os
import time


@dataclass(frozen=True)
class LifecycleTarget:
    software: str
    pid: int
    created: str
    executable: str
    home: str
    affected_tasks: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleScope:
    run_id: str
    targets: tuple[LifecycleTarget, ...]
    actions: tuple[str, ...] = ('close',)

    def validate(self):
        if (not self.run_id or not self.targets or not self.actions
                or len(set(self.targets)) != len(self.targets)
                or len({t.pid for t in self.targets}) != len(self.targets)
                or len(set(self.actions)) != len(self.actions)
                or any(a not in ('close', 'restart') for a in self.actions)):
            raise ValueError('exact lifecycle scope required')
        for t in self.targets:
            values = (t.software, t.created, t.executable, t.home, *t.affected_tasks)
            if (t.pid <= 0 or not t.affected_tasks
                    or any(not v or any(ord(c) < 32 for c in v) for v in values)):
                raise ValueError('software, process identity and affected tasks required')
        if any(ord(c) < 32 for c in self.run_id):
            raise ValueError('invalid run identity')


def windows_confirmation(message):
    """Interactive only. Cancel is default; never substitute --yes or a receipt."""
    if os.name != 'nt':
        raise RuntimeError('interactive Windows confirmation unavailable')
    import ctypes
    from ctypes import wintypes
    box = ctypes.WinDLL('user32', use_last_error=True).MessageBoxW
    box.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    box.restype = ctypes.c_int
    # OK/CANCEL, warning, second/default button, foreground, task modal.
    return box(None, message, 'Agent lifecycle confirmation', 0x1 | 0x30 | 0x100 | 0x10000 | 0x2000) == 1


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
        rows = [f'Run: {self.scope.run_id}', 'Actions: ' + ', '.join(self.scope.actions),
                'The following software and tasks will be interrupted:']
        for t in self.scope.targets:
            rows.extend((f'{t.software}: PID {t.pid}, created {t.created}',
                         f'Executable: {t.executable}', f'Home: {t.home}',
                         'Affected tasks: ' + ', '.join(t.affected_tasks)))
        rows.append('Confirm only these lifecycle actions for this run?')
        if self._prompt('\n'.join(rows)) is not True:
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
