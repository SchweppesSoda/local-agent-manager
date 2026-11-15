"""Ordinary-user lifecycle caller for the task-specific Cindy home split.

No elevation, forced termination or T3 operation. Source/target storage exclusion
belongs to a separate held boundary, not to this consent mechanism.
"""
from pathlib import Path
from dataclasses import asdict
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
        if identity is None: return False  # Graceful shutdown already released it.
        if Path(identity[0]) != Path(target.executable) or str(identity[1]) != target.created:
            raise IsolationPlanError("cindy_identity_changed_before_window_close")
        return True
    if not recheck(): return
    @callback_type
    def visit(window, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == target.pid:
            try:
                if not recheck(): return True
                if not user.PostMessageW(window, 0x10, 0, 0):
                    code = ctypes.get_last_error()
                    # A previous WM_CLOSE can destroy another top-level window
                    # in this exact process before enumeration reaches it.
                    owner_now = wintypes.DWORD()
                    user.GetWindowThreadProcessId(window, ctypes.byref(owner_now))
                    if code != 1400 or owner_now.value != 0: errors.append(code)
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
        self._tracked = {}

    def observe(self):
        targets, rows, family = [], list(self.census()), {}
        for row in rows:
            if str(row.get("name", "")).casefold() not in ("cindy", "cindy.exe"):
                continue
            current = self.identity(row["pid"])
            if current is None or Path(current[0]) != Path(self.executable):
                raise IsolationPlanError("cindy_process_identity_unqualified")
            family[row["pid"]] = (row, current)
        changed = True
        while changed:
            before = len(family)
            for row in rows:
                if row["pid"] not in family and row.get("parent") in family:
                    current = self.identity(row["pid"])
                    if current is None or float(current[1]) < float(family[row["parent"]][1][1]):
                        raise IsolationPlanError("cindy_descendant_identity_unqualified")
                    family[row["pid"]] = (row, current)
            changed = len(family) != before
        present = {r["pid"] for r in rows}
        for pid, (row, old) in self._tracked.items():
            if pid in present and pid not in family:
                current = self.identity(pid)
                if current != old: raise IsolationPlanError("cindy_descendant_pid_reused")
                family[pid] = (row, current)
        self._tracked.update(family)
        homes = {}
        if any(str(row.get('name', '')).casefold() == 'codex.exe' for row, _ in family.values()):
            from .codex_desktop_state import _running_related_process_records
            homes = {r['process_id']: r for r in _running_related_process_records()}
        for row, current in family.values():
            desktop = str(row.get('name', '')).casefold() in {'cindy', 'cindy.exe'}
            software = "Cindy" if desktop else "Cindy provider: " + str(row["name"])
            record = homes.get(row['pid'], {})
            # Ancestry establishes lifecycle ownership, never provider storage.
            # Unknown/non-Codex homes are explicitly unsupported, not profile.
            home = self.profile if desktop else record.get('codex_home') or 'unsupported: provider home unproven'
            if record and (not record.get('executable_path') or Path(record['executable_path']) != Path(current[0]) or self.identity(row['pid']) != current):
                raise IsolationPlanError('provider_home_process_identity_changed')
            targets.append(LifecycleTarget(software, row["pid"], str(current[1]), current[0], str(home),
                self.tasks, ("经本轮 PID/创建时间祖先链确认属于 Cindy；T3 不在关闭范围；只请求 WM_CLOSE。",)))
        return LifecycleScope(self.run_id, tuple(sorted(targets, key=lambda t: t.pid)),
                              actions=("close",), reason="为原 Cindy codex-home 隔离取得本轮离线窗口；全部账号任务暂时不可用。")

    def close(self):
        if os.name == "nt":
            import ctypes
            if ctypes.windll.shell32.IsUserAnAdmin():
                raise IsolationPlanError("lifecycle_requires_ordinary_user_process")
        scope = self.observe()
        self._last_closed_scope = scope
        if not scope.targets:
            return {"run_id": self.run_id, "already_closed": True, "gate_displayed": False, "closed_pids": [],
                    'targets': [], 'affected_tasks': list(self.tasks), 'exit_rechecked': True}
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
        return {"run_id": self.run_id, "already_closed": False, "gate_displayed": True,
                'targets': [asdict(target) for target in scope.targets], 'affected_tasks': list(self.tasks), 'exit_rechecked': True,
                "closed_pids": [target.pid for target in scope.targets], "startup_fence_held": False}

    def restart(self, *, expected_executable_sha256, launch):
        """Fresh ordinary-user restart consent, bound to the software just closed.

        A restart-only target explicitly has no running PID and is bound to
        the reviewed file entity; disk arm receipts supply no consent.
        """
        from .cindy_isolation_recovery import digest
        if self.observe().targets: raise IsolationPlanError("cindy_restarted_outside_controller")
        if digest(self.executable) != expected_executable_sha256: raise IsolationPlanError("restart_image_changed")
        from . import frozen_files as ff
        info = Path(self.executable).lstat(); ff._plain(info)
        target = LifecycleTarget("Cindy", 0, "not-running", self.executable, self.profile, self.tasks,
            ("当前 Cindy 及本轮已跟踪的子进程均已退出；本次只按程序文件实体普通启动。",),
            file_sha256=expected_executable_sha256, file_device=info.st_dev, file_inode=info.st_ino)
        current = LifecycleScope(self.run_id + "-restart", (target,), actions=("restart",),
                                 reason="离线阶段已核验，使用普通权限正常重开 Cindy 进行本轮验收。")
        gate = self.gate_factory(current); gate.confirm()
        after = Path(self.executable).lstat()
        if self.observe().targets or digest(self.executable) != expected_executable_sha256 or ff._identity(after) != ff._identity(info):
            raise IsolationPlanError("restart_identity_changed_during_dialog")
        gate.arm(current)
        return gate.perform("restart", current_run_id=current.run_id, current_target=target,
                            execute=lambda _: launch())
