"""Ordinary-user lifecycle caller for the task-specific Cindy home split.

No elevation, forced termination or T3 operation. Source/target storage exclusion
belongs to a separate held boundary, not to this consent mechanism.
"""
from pathlib import Path
import os
import time

from .cindy_isolation_plan import IsolationPlanError
from .lifecycle_confirmation import LifecycleGate, LifecycleScope, LifecycleTarget


def windows_identity(pid):
    from .t3code_handoff import _kernel_process_image
    return _kernel_process_image(pid)


def windows_census():
    from .office_runtime import processes
    return processes()


def windows_close_window(target):
    """Send WM_CLOSE only to windows belonging to the freshly checked PID."""
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user.PostMessageW.restype = wintypes.BOOL
    errors = []
    def recheck():
        identity = windows_identity(target.pid)
        if identity is None or Path(identity[0]) != Path(target.executable) or str(identity[1]) != target.created:
            raise IsolationPlanError("cindy_identity_changed_before_window_close")
    recheck()
    @callback_type
    def visit(window, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == target.pid:
            try:
                recheck()
                if not user.PostMessageW(window, 0x10, 0, 0): errors.append(ctypes.get_last_error())
            except IsolationPlanError:
                errors.append("identity_changed")
        return True
    if not user.EnumWindows(visit, 0) or errors:
        raise IsolationPlanError("cindy_graceful_close_unverified")


class CindyLifecycle:
    def __init__(self, *, run_id, executable, profile, affected_tasks,
                 census=windows_census, identity=windows_identity, close_window=windows_close_window,
                 gate_factory=LifecycleGate):
        self.run_id, self.executable, self.profile = run_id, str(Path(executable)), str(Path(profile))
        self.tasks = tuple(sorted(set(affected_tasks)))
        if not self.tasks:
            raise IsolationPlanError("all_affected_cindy_tasks_required")
        self.census, self.identity, self.close_window, self.gate_factory = census, identity, close_window, gate_factory

    def observe(self):
        targets = []
        for row in self.census():
            if str(row.get("name", "")).casefold() not in ("cindy", "cindy.exe"):
                continue
            current = self.identity(row["pid"])
            if current is None or Path(current[0]) != Path(self.executable):
                raise IsolationPlanError("cindy_process_identity_unqualified")
            targets.append(LifecycleTarget("Cindy", row["pid"], str(current[1]), self.executable,
                                           self.profile, self.tasks,
                                           ("T3 保持运行；不关闭其他 provider；只请求正常退出。",)))
        return LifecycleScope(self.run_id, tuple(sorted(targets, key=lambda t: t.pid)),
                              actions=("close",), reason="为原 Cindy codex-home 隔离取得本轮离线窗口；全部账号任务暂时不可用。")

    def close(self):
        if os.name == "nt":
            import ctypes
            if ctypes.windll.shell32.IsUserAnAdmin():
                raise IsolationPlanError("lifecycle_requires_ordinary_user_process")
        scope = self.observe()
        if not scope.targets:
            return {"already_closed": True, "gate_displayed": False, "closed_pids": []}
        gate = self.gate_factory(scope)
        gate.confirm()  # Only when a caller actually requests closure.
        gate.arm(self.observe())
        initial = set(scope.targets)
        for target in scope.targets:
            current = self.observe()
            if set(current.targets) - initial:
                raise IsolationPlanError("new_cindy_process_requires_new_lifecycle_gate")
            matching = [item for item in current.targets if item.pid == target.pid]
            if not matching:
                continue  # Graceful main-window shutdown can release children.
            gate.perform("close", current_run_id=self.run_id, current_target=matching[0], execute=self.close_window)
        deadline = time.monotonic() + 10
        while self.observe().targets:
            if time.monotonic() >= deadline:
                raise IsolationPlanError("cindy_exit_unproven_no_forced_termination")
            time.sleep(0.1)
        return {"already_closed": False, "gate_displayed": True,
                "closed_pids": [target.pid for target in scope.targets], "startup_fence_held": False}
