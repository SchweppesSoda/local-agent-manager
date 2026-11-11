"""Interactive popup-confirmed detached T3 Code full local cleanup handoff.

Foreground preflight discovers exact T3 + bound native targets while T3 is open,
identifies running desktop clients that must be closed and reopened, and shows
an explicit Windows confirmation dialog (default Cancel). On confirmation, a
detached WMI worker outside T3's Job object waits for active T3 runs to save,
closes only the confirmed apps, settles any dead unstarted preflight child lock
via ``operation verify``, runs full cleanup + cold verification, writes a
receipt, reopens the closed desktop apps, and optionally notifies a preserved
T3 thread once.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from . import frozen_files, t3code_database
from .child_recovery import is_recoverable_preflight_child
from .codex_desktop_state import _relevant_process_ids, _running_related_process_records
from .lifecycle_confirmation import LifecycleGate, LifecycleScope, LifecycleTarget, windows_confirmation
from .mutation_guard import _journal_unknown
from .office_runtime import processes as list_processes
from .operation_store import OperationStore, strict_json_load, utc_now, write_new_json
from .record_identity import canonical_path
from .sqlite_utils import connect_readonly
from .state_paths import control_directory
from .t3code_bound_adapter import T3CodeBoundAdapter, load_manifest
from .t3code_cleanup import evidence_from_document
from .t3code_discovery import BindingDiscoveryError, discover_bindings
from .t3code_server_delete import _ENTRY as _T3_SERVER_ENTRY, _RUNTIME_FILES, _bundled_install, _locator
from .t3code_store import V2, database_path, default_root, identifier, local_root

HANDOFF_SPEC_SCHEMA = "larj.t3code-handoff-spec.v1"
HANDOFF_RECEIPT_SCHEMA = "larj.t3code-handoff-receipt.v1"

_AMBIENT_PROVIDER_ENV_VARS = (
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "GEMINI_HOME",
    "GEMINI_CLI_HOME",
    "AGY_ACP_FORCE_FILE_STORAGE",
    "PI_CODING_AGENT_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
    "ELECTRON_RUN_AS_NODE",
    "T3_ACP_MCP_NODE",
    "T3_ACP_MCP_ENTRYPOINT",
)

_TERMINAL_RUN_STATUSES = ("completed", "interrupted", "failed", "cancelled", "rolled_back")
_SYSTEM_SHELL_PARENTS = frozenset({
    "explorer.exe",
    "services.exe",
    "svchost.exe",
    "wininit.exe",
    "sihost.exe",
    "taskhostw.exe",
    "runtimebroker.exe",
    "wmiprvse.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
    "wt.exe",
    "windowsterminal.exe",
})


class HandoffError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.details = details
        self.retryable = retryable


def _safe_temp_dir(t3_root: Path) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local")) / "Temp"
    current = os.environ.get("TEMP") or os.environ.get("TMP")
    if not current:
        return base
    candidate = Path(current).expanduser()
    try:
        if candidate.resolve(strict=False).is_relative_to(t3_root.resolve(strict=False)):
            return base
    except (OSError, ValueError):
        return base
    return candidate


@contextmanager
def _clean_provider_environ(t3_root: Path, *, preserve: Sequence[str] = ()):
    keep = {name.upper() for name in preserve}
    saved = {k: os.environ[k] for k in _AMBIENT_PROVIDER_ENV_VARS if k in os.environ and k.upper() not in keep}
    old_temp = {k: os.environ.get(k) for k in ("TEMP", "TMP")}
    safe_temp = str(_safe_temp_dir(t3_root))
    try:
        for key in saved:
            os.environ.pop(key, None)
        for key in ("TEMP", "TMP"):
            if old_temp[key] != safe_temp:
                os.environ[key] = safe_temp
        yield
    finally:
        for key, value in old_temp.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.update(saved)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _append_worker_event(events_path: Path, event: str, **fields: Any) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": utc_now(), "event": event, **fields}
    with events_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def _kernel_process_start_unix(pid: int) -> float | None:
    if os.name != "nt" or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return None
        return ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11644473600
    finally:
        kernel.CloseHandle(handle)


def _iso_from_unix(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _expand_t3_selection(adapter: T3CodeBoundAdapter, selectors: Sequence[str]) -> list[str]:
    available = {row["id"] for row in adapter.observations}
    thread_ids = {identifier(s.removeprefix("t3code:")) for s in selectors}
    if not thread_ids or not thread_ids <= available:
        raise HandoffError("selector_not_found", "所选 T3 thread ID 不存在或未完成绑定。")
    while True:
        expanded = thread_ids | {r["id"] for r in adapter.observations if r["parent_thread_id"] in thread_ids}
        expanded.update(
            child["child_thread_id"]
            for child in adapter.observation["children"]
            if child["thread_id"] in thread_ids
        )
        if not expanded <= available:
            raise HandoffError("selected_subagent_thread_unbound", "级联子代理会话缺少有效绑定。")
        if expanded == thread_ids:
            break
        thread_ids = expanded
    if thread_ids & adapter.legacy_import_ids:
        raise HandoffError(
            "legacy_import_selected_cleanup_unqualified",
            "历史导入只读会话不支持纳入删除范围。",
            retryable=False,
        )
    return sorted(thread_ids)


def _inspect_store_journals(coordinator: Any, roots: Sequence[Path]) -> list[dict[str, str]]:
    recoverable: list[dict[str, str]] = []
    seen_dirs: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root)
        ops_dir = control_directory(root) / "operations"
        if not ops_dir.exists():
            continue
        for entry in sorted(ops_dir.iterdir()):
            if entry.name == ".mutation.lock" or not entry.is_dir():
                continue
            dir_key = canonical_path(entry)
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
            store = OperationStore(root, entry.name)
            if store.receipt_path.exists() and store._read_receipt() is not None and not store.lock_exists():
                continue
            store.read_plan()
            unknown, _ = _journal_unknown(store)
            if not unknown:
                continue
            if is_recoverable_preflight_child(coordinator, store):
                recoverable.append({"operation_id": store.operation_id, "codex_home": str(root)})
                continue
            raise HandoffError(
                "unknown_prior_operation_present",
                f"存储目录 {root} 存在未决 operation {store.operation_id}，请先运行 operation verify 核验。",
            )
    return recoverable


def _climb_desktop_owner(pid: int, by_pid: Mapping[int, Mapping[str, Any]]) -> Mapping[str, Any]:
    current = by_pid[pid]
    visited = {pid}
    while True:
        parent_id = int(current.get("parent") or 0)
        parent = by_pid.get(parent_id)
        if parent is None or parent_id in visited:
            break
        parent_name = str(parent.get("name") or "").casefold()
        if parent_name in _SYSTEM_SHELL_PARENTS or not parent.get("executable"):
            break
        visited.add(parent_id)
        current = parent
    return current


def _software_profile(row: Mapping[str, Any], *, t3_root: Path, target_homes: Mapping[str, str]) -> dict[str, Any]:
    name = str(row.get("name") or "")
    key = name.casefold()
    exe = str(row.get("executable") or "")
    if key.startswith("t3 code"):
        return {
            "software": "T3 Code Desktop",
            "home": str(t3_root),
            "reopen": True,
            "hold_executable": False,
        }
    if key == "cindy.exe":
        return {
            "software": "Cindy Desktop",
            "home": target_homes.get("codex") or str(Path(exe).parent),
            "reopen": True,
            "hold_executable": True,
        }
    if key == "chatgpt.exe":
        return {
            "software": "Codex / ChatGPT Desktop",
            "home": target_homes.get("codex") or str(Path(exe).parent),
            "reopen": True,
            "hold_executable": True,
        }
    if key.startswith("paseo"):
        return {
            "software": "Paseo Desktop",
            "home": target_homes.get("antigravity") or str(Path(exe).parent),
            "reopen": True,
            "hold_executable": True,
        }
    if key.startswith("magpie"):
        return {
            "software": "Magpie",
            "home": target_homes.get("codex") or str(Path(exe).parent),
            "reopen": True,
            "hold_executable": True,
        }
    return {
        "software": name or Path(exe).name,
        "home": next(iter(target_homes.values()), str(t3_root)),
        "reopen": False,
        "hold_executable": False,
    }


def _qualify_lifecycle_targets(
    *,
    t3_root: Path,
    manifest: Mapping[str, Any],
    target_stores: Sequence[tuple[str, str, str]],
    record_ids: Sequence[str],
    expanded_thread_ids: Sequence[str],
    native_record_count: int,
) -> list[dict[str, Any]]:
    census = list_processes()
    by_pid = {int(row["pid"]): row for row in census if isinstance(row.get("pid"), int) and row["pid"] > 0}
    runtime_keys = {canonical_path(path) for path in manifest.get("runtime_binaries", ()) if Path(path).exists()}
    target_engines = {engine for engine, _, _ in target_stores}
    target_homes = {engine: raw_root for engine, _, raw_root in target_stores}

    # 1. Locate T3 Code Desktop root(s) and their full descendant trees.
    t3_roots: set[int] = set()
    for pid, row in by_pid.items():
        name = str(row.get("name") or "").casefold()
        cmd = str(row.get("command") or "")
        if name.startswith("t3 code") and "--type=" not in cmd:
            owner = _climb_desktop_owner(pid, by_pid)
            if str(owner.get("name") or "").casefold().startswith("t3 code"):
                t3_roots.add(int(owner["pid"]))
    t3_tree = set(t3_roots)
    changed = True
    while changed:
        before = len(t3_tree)
        t3_tree.update(pid for pid, row in by_pid.items() if int(row.get("parent") or 0) in t3_tree)
        changed = len(t3_tree) != before

    # 2. Locate non-T3 processes that would block offline cleanup of target stores.
    blocking_pids: set[int] = set()
    touches_cindy_global = any("cindyglobal" in canon_root.casefold() for _, canon_root, _ in target_stores)
    if "codex" in target_engines:
        codex_records = _running_related_process_records()
        for engine, canon_root, raw_root in target_stores:
            if engine != "codex":
                continue
            for pid in _relevant_process_ids(Path(raw_root), codex_records):
                if pid > 0 and pid not in t3_tree and pid in by_pid:
                    blocking_pids.add(pid)
            for rec in codex_records:
                rec_pid = int(rec.get("process_id") or 0)
                if rec_pid <= 0 or rec_pid in t3_tree or rec_pid not in by_pid:
                    continue
                rec_home = rec.get("codex_home")
                if isinstance(rec_home, str) and rec_home and Path(rec_home).exists():
                    if canonical_path(rec_home) == canon_root:
                        blocking_pids.add(rec_pid)
    for pid, row in by_pid.items():
        if pid in t3_tree or pid == os.getpid():
            continue
        name = str(row.get("name") or "").casefold()
        exe = row.get("executable")
        exe_key = canonical_path(exe) if exe and Path(exe).exists() else None
        if exe_key and exe_key in runtime_keys:
            blocking_pids.add(pid)
            continue
        if touches_cindy_global and name == "cindy.exe":
            blocking_pids.add(pid)
            continue
        if "claude" in target_engines and name in {"claude", "claude.exe"}:
            blocking_pids.add(pid)
            continue
        if "antigravity" in target_engines and (
            name.startswith("paseo") or name in {"agy", "agy.exe", "agy_acp_server.exe"}
        ):
            blocking_pids.add(pid)

    owner_roots: list[int] = sorted(t3_roots)
    for pid in sorted(blocking_pids):
        owner = _climb_desktop_owner(pid, by_pid)
        owner_pid = int(owner["pid"])
        if owner_pid not in owner_roots and owner_pid not in t3_tree:
            owner_roots.append(owner_pid)

    specs: list[dict[str, Any]] = []
    selector_label = ", ".join(record_ids[:3]) + (f" 等 {len(record_ids)} 项" if len(record_ids) > 3 else "")
    for root_pid in owner_roots:
        row = by_pid[root_pid]
        exe = row.get("executable")
        if not isinstance(exe, str) or not exe or not Path(exe).is_file():
            raise HandoffError(
                "unverified_owner_executable",
                f"无法核实待关闭进程 PID {root_pid} ({row.get('name')}) 的可执行文件路径。",
            )
        started_unix = _kernel_process_start_unix(root_pid)
        if started_unix is None:
            continue
        created_iso = _iso_from_unix(started_unix)
        profile = _software_profile(row, t3_root=t3_root, target_homes=target_homes)
        if profile["software"] == "T3 Code Desktop":
            task_desc = (
                f"待当前回合结束后关闭并自动重开；清理 T3 会话 {selector_label}"
                f"（级联 {len(expanded_thread_ids)} 条 T3 记录、{native_record_count} 条原生记录）"
            )
        elif profile["reopen"]:
            task_desc = f"关闭占用目标原生存储的后台进程，并在清理核验完成后自动重开 {profile['software']}"
        else:
            task_desc = f"终止占用目标原生存储的独立进程 {profile['software']}"
        lifecycle_target = LifecycleTarget(
            software=profile["software"],
            pid=root_pid,
            created=created_iso,
            executable=str(Path(exe).resolve()),
            home=profile["home"],
            affected_tasks=(task_desc,),
        )
        specs.append({
            "software": lifecycle_target.software,
            "pid": lifecycle_target.pid,
            "created": lifecycle_target.created,
            "created_unix": started_unix,
            "executable": lifecycle_target.executable,
            "home": lifecycle_target.home,
            "affected_tasks": list(lifecycle_target.affected_tasks),
            "reopen": bool(profile["reopen"]),
            "hold_executable": bool(profile["hold_executable"]),
            "lifecycle_target": lifecycle_target,
        })
    return specs


def _python_interpreter() -> str:
    candidate = Path(sys.executable)
    if candidate.name.casefold().startswith("python"):
        return str(candidate)
    base = getattr(sys, "_base_executable", None)
    if isinstance(base, str) and base and Path(base).is_file():
        return base
    return str(candidate)


def _spawn_detached_worker(spec_path: Path) -> int:
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    worker_script = Path(__file__).resolve()
    cmd_line = subprocess.list2cmdline([_python_interpreter(), "-B", str(worker_script), "--worker", str(spec_path)])
    ps_script = (
        "$ErrorActionPreference='Stop'; "
        "$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{ CommandLine = $env:LAM_WORKER_CMD }; "
        "ConvertTo-Json -Compress @{ ReturnValue = [int]$res.ReturnValue; ProcessId = [int]($res.ProcessId) }"
    )
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PROVIDER_ENV_VARS}
    env["LAM_WORKER_CMD"] = cmd_line
    completed = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise HandoffError(
            "worker_spawn_failed",
            f"无法通过 WMI 启动独立后台清理进程：{completed.stderr.strip() or completed.stdout.strip()}",
        )
    payload = json.loads(completed.stdout.strip())
    if payload.get("ReturnValue") != 0 or int(payload.get("ProcessId") or 0) <= 0:
        raise HandoffError("worker_spawn_failed", f"Win32_Process.Create 返回异常：{payload}")
    return int(payload["ProcessId"])


def start_t3code_handoff(
    args: argparse.Namespace,
    *,
    coordinator: Any,
    scope: Mapping[str, Any],
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    prompt: Callable[[str], bool] | None = None,
    spawner: Callable[[Path], int] | None = None,
) -> dict[str, Any]:
    del progress_callback
    prompt_fn = windows_confirmation if prompt is None else prompt
    if os.name != "nt":
        return {
            "schema_version": "larj.operation-result.v1",
            "document_type": "operation_result",
            "command": "delete",
            "subcommand": "run",
            "goal_status": "blocked",
            "goal_satisfied": False,
            "modified": False,
            "mutation_started": False,
            "scope": dict(scope),
            "blockers": [{
                "blocker_code": "handoff_windows_only",
                "scope": "environment",
                "severity": "error",
                "retryable": False,
                "message": "--handoff 仅支持 Windows 桌面环境。",
            }],
        }

    handoff_id = f"t3handoff-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
    try:
        roots = tuple(getattr(args, "t3code_root", ()) or ())
        if len(roots) > 1:
            raise HandoffError("multiple_t3code_roots_unsupported", "--handoff 一次仅支持单个 T3 配置目录。", retryable=False)
        t3_root = local_root(roots[0]) if roots else default_root()

        explicit_out = getattr(args, "out", None)
        handoff_dir = (
            Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
            / "local-agent-manager"
            / "handoffs"
            / handoff_id
        )
        receipt_path = Path(explicit_out).expanduser().resolve() if explicit_out is not None else (handoff_dir / "receipt.json")
        if receipt_path.exists():
            raise HandoffError("receipt_output_exists", f"输出回执路径已存在：{receipt_path}", retryable=False)

        preserve_env = ["CODEX_HOME"] if getattr(args, "codex_home", None) else []
        with _clean_provider_environ(t3_root, preserve=preserve_env):
            record_ids = list(dict.fromkeys(identifier(r.removeprefix("t3code:")) for r in scope.get("record_ids", ())))
            if not record_ids:
                raise HandoffError("empty_selection", "必须指定至少一个完整 --record-id。", retryable=False)
            notify_raw = getattr(args, "notify_thread_id", None)
            notify_thread_id = identifier(notify_raw.removeprefix("t3code:")) if notify_raw else None

            try:
                manifest = (
                    load_manifest(args.t3code_bindings)
                    if getattr(args, "t3code_bindings", None) is not None
                    else discover_bindings(t3_root)
                )
            except BindingDiscoveryError as exc:
                raise HandoffError(
                    "t3code_binding_discovery_blocked",
                    f"T3 绑定自动发现受阻：{exc}",
                    details={"sources": exc.blockers},
                ) from exc

            adapter = T3CodeBoundAdapter(manifest)
            ref_snapshot = adapter.snapshot_references(refresh=True)
            expanded_thread_ids = _expand_t3_selection(adapter, record_ids)
            if notify_thread_id is not None and notify_thread_id in set(expanded_thread_ids):
                raise HandoffError(
                    "notify_thread_in_deletion_scope",
                    f"--notify-thread-id ({notify_thread_id}) 位于本次级联删除范围内，已拒绝执行。",
                    retryable=False,
                )

            with closing(connect_readonly(database_path(t3_root))) as db:
                db.execute("BEGIN")
                t3code_database._check(db)
                t3code_database.closure(db, expanded_thread_ids)
                placeholders = ",".join("?" for _ in expanded_thread_ids)
                status_placeholders = ",".join("?" for _ in _TERMINAL_RUN_STATUSES)
                active_target = db.execute(
                    f"SELECT thread_id, status FROM {V2}runs "
                    f"WHERE thread_id IN ({placeholders}) AND status NOT IN ({status_placeholders}) LIMIT 1",
                    (*expanded_thread_ids, *_TERMINAL_RUN_STATUSES),
                ).fetchone()
                if active_target is not None:
                    raise HandoffError(
                        "target_thread_active",
                        f"待删除会话 {active_target[0]} 当前仍处于运行状态 ({active_target[1]})，请待其停止后再试。",
                    )
                if notify_thread_id is not None:
                    row = db.execute(
                        f"SELECT deleted_at FROM {V2}threads WHERE thread_id=?",
                        (notify_thread_id,),
                    ).fetchone()
                    if row is None or row[0] is not None:
                        raise HandoffError(
                            "notify_thread_not_found",
                            f"未找到有效保留会话 --notify-thread-id={notify_thread_id}。",
                            retryable=False,
                        )

            expanded_set = set(expanded_thread_ids)
            target_store_map: dict[tuple[str, str], tuple[str, str, str]] = {}
            for item in manifest.get("native_stores", ()):
                if item["thread_id"] in expanded_set:
                    key = (item["engine"], canonical_path(item["root"]))
                    target_store_map[key] = (item["engine"], key[1], str(item["root"]))
            target_stores = sorted(target_store_map.values())
            target_refs = [ref for ref in ref_snapshot.references if ref.frontend_id in expanded_set]
            native_record_count = len({(ref.engine, ref.native_id) for ref in target_refs})

            recoverable_children = _inspect_store_journals(
                coordinator,
                [t3_root, *(Path(raw_root) for _, _, raw_root in target_stores)],
            )
            target_specs = _qualify_lifecycle_targets(
                t3_root=t3_root,
                manifest=manifest,
                target_stores=target_stores,
                record_ids=record_ids,
                expanded_thread_ids=expanded_thread_ids,
                native_record_count=native_record_count,
            )

        if target_specs:
            actions = ("close", "restart") if any(item["reopen"] for item in target_specs) else ("close",)
            lifecycle_scope = LifecycleScope(
                run_id=handoff_id,
                targets=tuple(item["lifecycle_target"] for item in target_specs),
                actions=actions,
            )
            gate = LifecycleGate(lifecycle_scope, prompt=prompt_fn)
            try:
                gate.confirm()
            except RuntimeError as exc:
                if "desktop unavailable" in str(exc):
                    raise HandoffError(
                        "interactive_desktop_unavailable",
                        f"当前进程位于隔离桌面 ({exc})，无法在主桌面弹出确认窗口；请在本机 PowerShell、Windows Terminal 或 T3 内置终端直接运行此命令。",
                        retryable=True,
                    ) from exc
                if "declined" in str(exc):
                    return {
                        "schema_version": "larj.operation-result.v1",
                        "document_type": "operation_result",
                        "command": "delete",
                        "subcommand": "run",
                        "operation_id": handoff_id,
                        "goal_status": "blocked",
                        "goal_satisfied": False,
                        "modified": False,
                        "mutation_started": False,
                        "scope": dict(scope),
                        "blockers": [{
                            "blocker_code": "user_confirmation_declined",
                            "scope": "lifecycle_confirmation",
                            "severity": "error",
                            "retryable": True,
                            "message": "用户在确认弹窗中取消了客户端关闭与重开；未执行任何修改。",
                        }],
                    }
                raise
        else:
            summary_msg = (
                f"Run: {handoff_id}\n"
                f"当前未检测到运行中的 T3 或关联客户端进程。\n"
                f"即将永久清理 T3 会话 {', '.join(record_ids)}（级联共 {len(expanded_thread_ids)} 条 T3 记录、"
                f"{native_record_count} 条原生记录）。\n"
                f"是否确认执行？"
            )
            try:
                confirmed = prompt_fn(summary_msg)
            except RuntimeError as exc:
                if "desktop unavailable" in str(exc):
                    raise HandoffError(
                        "interactive_desktop_unavailable",
                        f"当前进程位于隔离桌面 ({exc})，无法在主桌面弹出确认窗口；请在本机 PowerShell、Windows Terminal 或 T3 内置终端直接运行此命令。",
                        retryable=True,
                    ) from exc
                raise
            if confirmed is not True:
                return {
                    "schema_version": "larj.operation-result.v1",
                    "document_type": "operation_result",
                    "command": "delete",
                    "subcommand": "run",
                    "operation_id": handoff_id,
                    "goal_status": "blocked",
                    "goal_satisfied": False,
                    "modified": False,
                    "mutation_started": False,
                    "scope": dict(scope),
                    "blockers": [{
                        "blocker_code": "user_confirmation_declined",
                        "scope": "lifecycle_confirmation",
                        "severity": "error",
                        "retryable": True,
                        "message": "用户在确认弹窗中取消了清理；未执行任何修改。",
                    }],
                }

        cli_forwards: list[str] = []
        if roots:
            cli_forwards.extend(["--t3code-root", str(t3_root)])
        if getattr(args, "t3code_bindings", None) is not None:
            cli_forwards.extend(["--t3code-bindings", str(Path(args.t3code_bindings).resolve())])
        for flag, attr in (
            ("--codex-home", "codex_home"),
            ("--codex-bin", "codex_bin"),
            ("--antigravity-root", "antigravity_root"),
            ("--antigravity-bin", "antigravity_bin"),
            ("--operation-home", "operation_home"),
        ):
            val = getattr(args, attr, None)
            if val is not None:
                cli_forwards.extend([flag, str(Path(val).expanduser().resolve())])
        for flag, attr in (
            ("--orca-root", "orca_root"),
            ("--herdr-root", "herdr_root"),
            ("--workbuddy-root", "workbuddy_root"),
            ("--paseo-root", "paseo_root"),
        ):
            for item in getattr(args, attr, ()) or ():
                cli_forwards.extend([flag, str(Path(item).expanduser().resolve())])

        serialized_targets = [
            {k: v for k, v in item.items() if k != "lifecycle_target"}
            for item in target_specs
        ]
        spec = {
            "schema_version": HANDOFF_SPEC_SCHEMA,
            "handoff_id": handoff_id,
            "created_at": utc_now(),
            "t3code_root": str(t3_root),
            "t3code_install": str(_bundled_install()),
            "record_ids": record_ids,
            "expanded_thread_ids": expanded_thread_ids,
            "native_record_count": native_record_count,
            "notify_thread_id": notify_thread_id,
            "timeout": float(getattr(args, "timeout", 30.0) or 30.0),
            "quiesce_timeout": 180.0,
            "handoff_dir": str(handoff_dir),
            "receipt_path": str(receipt_path),
            "cli_forwards": cli_forwards,
            "recoverable_preflight_children": recoverable_children,
            "confirmed_targets": serialized_targets,
        }
        handoff_dir.mkdir(parents=True, exist_ok=True)
        spec_path = handoff_dir / "spec.json"
        write_new_json(spec_path, spec)
        worker_pid = (spawner or _spawn_detached_worker)(spec_path)
        return {
            "schema_version": "larj.operation-result.v1",
            "document_type": "operation_result",
            "command": "delete",
            "subcommand": "run",
            "operation_id": handoff_id,
            "status": "handoff_started",
            "goal_status": "ready",
            "goal_satisfied": False,
            "modified": False,
            "mutation_started": False,
            "scope": dict(scope),
            "handoff_id": handoff_id,
            "worker_pid": worker_pid,
            "spec_path": str(spec_path),
            "receipt_path": str(receipt_path),
            "selected_record_ids": record_ids,
            "expanded_thread_count": len(expanded_thread_ids),
            "native_record_count": native_record_count,
            "recoverable_preflight_children": recoverable_children,
            "confirmed_targets": [
                {
                    "software": item["software"],
                    "pid": item["pid"],
                    "executable": item["executable"],
                    "reopen": item["reopen"],
                }
                for item in serialized_targets
            ],
            "blockers": [],
        }
    except HandoffError as exc:
        blocker: dict[str, Any] = {
            "blocker_code": exc.code,
            "scope": "handoff_preflight",
            "severity": "error",
            "retryable": exc.retryable,
            "message": str(exc),
        }
        if exc.details is not None:
            blocker["details"] = exc.details
        return {
            "schema_version": "larj.operation-result.v1",
            "document_type": "operation_result",
            "command": "delete",
            "subcommand": "run",
            "operation_id": handoff_id,
            "goal_status": "blocked",
            "goal_satisfied": False,
            "modified": False,
            "mutation_started": False,
            "scope": dict(scope),
            "blockers": [blocker],
        }


def _wait_for_t3_quiescent(t3_root: Path, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    db_file = database_path(t3_root)
    status_placeholders = ",".join("?" for _ in _TERMINAL_RUN_STATUSES)
    consecutive_idle = 0
    while time.monotonic() < deadline:
        try:
            with closing(connect_readonly(db_file)) as db:
                active_run = db.execute(
                    f"SELECT run_id, thread_id, status FROM {V2}runs "
                    f"WHERE status NOT IN ({status_placeholders}) LIMIT 1",
                    _TERMINAL_RUN_STATUSES,
                ).fetchone()
                active_effect = db.execute(
                    "SELECT effect_id FROM orchestration_v2_effect_outbox "
                    "WHERE status IN ('pending','running') LIMIT 1"
                ).fetchone()
            if active_run is None and active_effect is None:
                consecutive_idle += 1
                if consecutive_idle >= 2:
                    return
            else:
                consecutive_idle = 0
        except Exception:
            consecutive_idle = 0
        time.sleep(0.5)
    raise HandoffError("quiesce_timeout", "等待 T3 活动回合落盘超时；未关闭任何软件或执行修改。")


def _capture_descendant_tree(root_pid: int, expected_start_unix: float) -> dict[int, float]:
    current_start = _kernel_process_start_unix(root_pid)
    if current_start is None or abs(current_start - expected_start_unix) > 2.0:
        return {}
    census = list_processes()
    tree_pids = {root_pid}
    changed = True
    while changed:
        before = len(tree_pids)
        for row in census:
            pid = row.get("pid")
            parent = row.get("parent")
            if isinstance(pid, int) and pid > 0 and parent in tree_pids and pid != os.getpid():
                tree_pids.add(pid)
        changed = len(tree_pids) != before
    verified: dict[int, float] = {}
    for pid in tree_pids:
        started = _kernel_process_start_unix(pid)
        if started is not None and (pid == root_pid or started >= expected_start_unix - 2.0):
            verified[pid] = started
    return verified


def _request_graceful_close(pid: int) -> None:
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        f"$p = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
        "if ($null -ne $p) { $null = $p.CloseMainWindow() }"
    )
    subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _terminate_pid_verified(pid: int, expected_start_unix: float) -> None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD

    handle = kernel.OpenProcess(0x0001 | 0x1000 | 0x100000, False, pid)
    if not handle:
        return
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return
        started = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11644473600
        if abs(started - expected_start_unix) > 2.0:
            return
        kernel.TerminateProcess(handle, 1)
        kernel.WaitForSingleObject(handle, 10_000)
    finally:
        kernel.CloseHandle(handle)


def _close_confirmed_target(target: Mapping[str, Any]) -> dict[str, Any]:
    root_pid = int(target["pid"])
    expected_start = float(target["created_unix"])
    tree = _capture_descendant_tree(root_pid, expected_start)
    if not tree:
        return {"software": target["software"], "pid": root_pid, "was_running": False, "closed_pids": []}

    close_cutoff = time.time() + 2.0
    _request_graceful_close(root_pid)
    grace_deadline = time.monotonic() + 6.0
    while time.monotonic() < grace_deadline:
        alive = [
            pid for pid, started in tree.items()
            if (cur := _kernel_process_start_unix(pid)) is not None and abs(cur - started) <= 2.0
        ]
        if not alive:
            break
        time.sleep(0.25)

    # Refresh once in case children spawned just before root exit.
    for row in list_processes():
        pid = row.get("pid")
        parent = row.get("parent")
        if isinstance(pid, int) and pid > 0 and pid not in tree and parent in tree and pid != os.getpid():
            started = _kernel_process_start_unix(pid)
            if started is not None and started <= close_cutoff:
                tree[pid] = started

    for pid, started in sorted(tree.items(), reverse=True):
        cur = _kernel_process_start_unix(pid)
        if cur is not None and abs(cur - started) <= 2.0:
            _terminate_pid_verified(pid, started)

    still_alive = [
        pid for pid, started in tree.items()
        if (cur := _kernel_process_start_unix(pid)) is not None and abs(cur - started) <= 2.0
    ]
    if still_alive:
        raise HandoffError(
            "process_close_incomplete",
            f"{target['software']} 进程树仍未完全退出：{still_alive}",
        )
    return {
        "software": target["software"],
        "pid": root_pid,
        "was_running": True,
        "closed_pids": sorted(tree),
    }


def _reopen_desktop_target(target: Mapping[str, Any]) -> dict[str, Any]:
    exe = Path(str(target["executable"]))
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        "$ErrorActionPreference='Stop'; "
        "try { "
        "$shell = New-Object -ComObject Shell.Application; "
        "$shell.ShellExecute($env:LAM_REOPEN_EXE, '', $env:LAM_REOPEN_WORKDIR, 'open', 1) "
        "} catch { "
        "Start-Process -FilePath $env:LAM_REOPEN_EXE -WorkingDirectory $env:LAM_REOPEN_WORKDIR | Out-Null "
        "}"
    )
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PROVIDER_ENV_VARS}
    env["LAM_REOPEN_EXE"] = str(exe)
    env["LAM_REOPEN_WORKDIR"] = str(exe.parent)
    completed = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {
        "software": target["software"],
        "executable": str(exe),
        "reopened": completed.returncode == 0,
        **({"error": completed.stderr.strip()} if completed.returncode != 0 else {}),
    }


def _run_cli_json(argv: Sequence[str], *, coordinator: Any = None) -> tuple[int, dict[str, Any]]:
    from .cli import main as cli_main

    out, err = StringIO(), StringIO()
    code = cli_main(list(argv), stdout=out, stderr=err, operation_coordinator=coordinator)
    raw = out.getvalue().strip()
    payload = json.loads(raw) if raw else {"raw_stderr": err.getvalue()}
    return code, payload


def _send_t3_completion_notification(
    *,
    t3_root: Path,
    t3_install: Path,
    notify_thread_id: str,
    handoff_dir: Path,
    message: str,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    marker = handoff_dir / "notification.sent.json"
    if marker.exists():
        return {"sent": False, "reason": "already_recorded"}

    exe = t3_install / _RUNTIME_FILES[0]
    entry = t3_install / _T3_SERVER_ENTRY
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("NODE_", "ELECTRON_", "T3CODE_")) and k not in _AMBIENT_PROVIDER_ENV_VARS
    }
    env.update(ELECTRON_RUN_AS_NODE="1", T3CODE_HOME=str(t3_root))

    deadline = time.monotonic() + timeout_seconds
    ready = False
    while time.monotonic() < deadline:
        try:
            _locator(t3_root)
            probe = subprocess.run(
                [str(exe), str(entry), "thread", "show", "--base-dir", str(t3_root), "--json", notify_thread_id],
                env=env,
                cwd=t3_root,
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if probe.returncode == 0:
                ready = True
                break
        except Exception:
            pass
        time.sleep(1.0)

    if not ready:
        return {"sent": False, "reason": "server_not_ready_before_timeout"}

    write_new_json(marker, {"notify_thread_id": notify_thread_id, "recorded_at": utc_now()})
    send_proc = subprocess.run(
        [str(exe), str(entry), "thread", "send", "--base-dir", str(t3_root), "--json", notify_thread_id, "-"],
        input=message.encode("utf-8"),
        env=env,
        cwd=t3_root,
        capture_output=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {
        "sent": send_proc.returncode == 0,
        "returncode": send_proc.returncode,
        **({"stderr": send_proc.stderr.decode("utf-8", errors="replace").strip()} if send_proc.returncode != 0 else {}),
    }


def run_handoff_worker(spec_path: Path) -> int:
    spec = strict_json_load(Path(spec_path))
    if not isinstance(spec, Mapping) or spec.get("schema_version") != HANDOFF_SPEC_SCHEMA:
        raise ValueError("Invalid handoff specification")

    handoff_id = str(spec["handoff_id"])
    t3_root = Path(str(spec["t3code_root"]))
    t3_install = Path(str(spec["t3code_install"]))
    handoff_dir = Path(str(spec["handoff_dir"]))
    receipt_path = Path(str(spec["receipt_path"]))
    events_path = handoff_dir / "events.jsonl"

    receipt: dict[str, Any] = {
        "schema_version": HANDOFF_RECEIPT_SCHEMA,
        "handoff_id": handoff_id,
        "started_at": utc_now(),
        "goal_status": "unknown",
        "goal_satisfied": False,
        "selected_record_ids": list(spec["record_ids"]),
        "expanded_thread_ids": list(spec["expanded_thread_ids"]),
        "closed_targets": [],
        "settled_preflight_children": [],
        "plan": None,
        "apply": None,
        "cold_verify": None,
        "reopened_targets": [],
        "notification": None,
        "errors": [],
    }
    closed_for_reopen: list[Mapping[str, Any]] = []

    preserve_env = ["CODEX_HOME"] if "--codex-home" in spec.get("cli_forwards", ()) else []
    with _clean_provider_environ(t3_root, preserve=preserve_env):
        try:
            _append_worker_event(events_path, "worker_started", worker_pid=os.getpid())
            _wait_for_t3_quiescent(t3_root, float(spec.get("quiesce_timeout", 180.0)))
            _append_worker_event(events_path, "t3_quiescent")

            # Close auxiliary desktop owners first, then T3 Code Desktop.
            ordered_targets = sorted(
                spec.get("confirmed_targets", ()),
                key=lambda item: 1 if item.get("software") == "T3 Code Desktop" else 0,
            )
            for target in ordered_targets:
                closed_info = _close_confirmed_target(target)
                receipt["closed_targets"].append(closed_info)
                if closed_info["was_running"] and target.get("reopen"):
                    closed_for_reopen.append(target)
            _append_worker_event(events_path, "targets_closed", count=len(receipt["closed_targets"]))

            from .cleanup_service import CleanupService
            from .operation_coordinator import OperationCoordinator
            from .windows_held_files import HeldFiles

            with ExitStack() as lease_stack:
                held = lease_stack.enter_context(HeldFiles())
                held_paths: set[str] = set()
                for target in ordered_targets:
                    if not target.get("hold_executable"):
                        continue
                    exe_path = Path(str(target["executable"]))
                    exe_key = canonical_path(exe_path)
                    if exe_key in held_paths or not exe_path.is_file():
                        continue
                    proof, _ = frozen_files.read_file(exe_path.parent, exe_path.name)
                    held.acquire(exe_path.parent, proof)
                    held_paths.add(exe_key)

                for item in spec.get("recoverable_preflight_children", ()):
                    _, child_res = _run_cli_json([
                        "operation",
                        "verify",
                        "--operation-id",
                        str(item["operation_id"]),
                        "--codex-home",
                        str(item["codex_home"]),
                        "--json",
                    ])
                    receipt["settled_preflight_children"].append(child_res)
                    if child_res.get("goal_status") not in {"completed_with_residuals", "complete"}:
                        raise HandoffError(
                            "preflight_child_recovery_failed",
                            f"无法结算残留预检锁 {item['operation_id']}: {child_res.get('blockers')}",
                        )

                plan_path = handoff_dir / "cleanup.plan.json"
                warm_coordinator = OperationCoordinator(CleanupService())
                plan_argv = [
                    "delete",
                    "plan",
                    "--client",
                    "t3code",
                    *(arg for rid in spec["record_ids"] for arg in ("--record-id", str(rid))),
                    "--out",
                    str(plan_path),
                    *spec.get("cli_forwards", ()),
                    "--json",
                ]
                _, plan_res = _run_cli_json(plan_argv, coordinator=warm_coordinator)
                receipt["plan"] = {
                    "operation_id": plan_res.get("operation_id"),
                    "plan_path": plan_res.get("plan_path"),
                    "plan_sha256": plan_res.get("plan_sha256"),
                    "goal_status": plan_res.get("goal_status"),
                    "blockers": plan_res.get("blockers", []),
                }
                if plan_res.get("goal_status") != "ready":
                    raise HandoffError("offline_plan_blocked", f"离线生成清理计划受阻：{plan_res.get('blockers')}")

                frozen_doc = strict_json_load(plan_path)
                evidence = evidence_from_document(frozen_doc)
                if evidence is None or evidence.get("selected_thread_ids") != list(spec["expanded_thread_ids"]):
                    raise HandoffError(
                        "offline_scope_drift",
                        "离线计划级联范围与弹窗确认范围不一致，已中止执行。",
                    )
                _append_worker_event(events_path, "plan_ready", operation_id=plan_res["operation_id"])

                apply_argv = [
                    "delete",
                    "apply",
                    "--client",
                    "t3code",
                    "--operation-id",
                    str(plan_res["operation_id"]),
                    "--plan",
                    str(plan_path),
                    "--authorized-plan-sha256",
                    str(plan_res["plan_sha256"]),
                    "--clients-closed",
                    "--timeout",
                    str(spec.get("timeout", 30.0)),
                    *spec.get("cli_forwards", ()),
                    "--json",
                ]
                _, apply_res = _run_cli_json(apply_argv, coordinator=warm_coordinator)
                receipt["apply"] = apply_res
                _append_worker_event(
                    events_path,
                    "apply_finished",
                    operation_id=plan_res["operation_id"],
                    goal_status=apply_res.get("goal_status"),
                )

                verify_argv = [
                    "operation",
                    "verify",
                    "--operation-id",
                    str(plan_res["operation_id"]),
                    "--plan",
                    str(plan_path),
                    "--json",
                ]
                _, verify_res = _run_cli_json(verify_argv, coordinator=None)
                receipt["cold_verify"] = verify_res
                _append_worker_event(
                    events_path,
                    "cold_verify_finished",
                    operation_id=plan_res["operation_id"],
                    goal_status=verify_res.get("goal_status"),
                )
                if apply_res.get("goal_status") == "complete" and verify_res.get("goal_status") == "complete":
                    receipt["goal_status"] = "complete"
                    receipt["goal_satisfied"] = True
                else:
                    receipt["goal_status"] = str(verify_res.get("goal_status") or apply_res.get("goal_status") or "unknown")
                    receipt["goal_satisfied"] = False
        except Exception as exc:
            if receipt["goal_status"] == "unknown" and receipt["apply"] is None:
                receipt["goal_status"] = "blocked"
            receipt["errors"].append({
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
            })
            _append_worker_event(events_path, "worker_error", error=str(exc))
        finally:
            # Reopen T3 Desktop first so server startup begins immediately, then auxiliary apps.
            reopen_order = sorted(
                closed_for_reopen,
                key=lambda item: 0 if item.get("software") == "T3 Code Desktop" else 1,
            )
            for target in reopen_order:
                try:
                    info = _reopen_desktop_target(target)
                except Exception as exc:
                    info = {"software": target.get("software"), "reopened": False, "error": str(exc)}
                receipt["reopened_targets"].append(info)
            receipt["completed_at"] = utc_now()
            _atomic_write_json(receipt_path, receipt)

            notify_thread_id = spec.get("notify_thread_id")
            if notify_thread_id and any(t.get("software") == "T3 Code Desktop" for t in closed_for_reopen):
                op_id = (receipt.get("plan") or {}).get("operation_id") or handoff_id
                status_label = receipt["goal_status"]
                msg = (
                    f"[local-agent-manager] T3 离线清理与重开已完成："
                    f"operation_id={op_id}，goal_status={status_label}，"
                    f"级联清理 {len(spec['expanded_thread_ids'])} 条 T3 记录。"
                    f"回执文件：{receipt_path}"
                )
                try:
                    receipt["notification"] = _send_t3_completion_notification(
                        t3_root=t3_root,
                        t3_install=t3_install,
                        notify_thread_id=str(notify_thread_id),
                        handoff_dir=handoff_dir,
                        message=msg,
                    )
                except Exception as exc:
                    receipt["notification"] = {"sent": False, "error": str(exc)}
                receipt["completed_at"] = utc_now()
                _atomic_write_json(receipt_path, receipt)
            _append_worker_event(events_path, "worker_finished", goal_status=receipt["goal_status"])

    return 0 if receipt["goal_satisfied"] else 1


def _worker_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local_agent_manager.t3code_handoff")
    parser.add_argument("--worker", type=Path, required=True)
    parsed = parser.parse_args(argv)
    return run_handoff_worker(parsed.worker)


if __name__ == "__main__":
    src_root = str(Path(__file__).resolve().parents[1])
    if src_root not in sys.path:
        sys.path.insert(0, src_root)
    from local_agent_manager.t3code_handoff import _worker_cli as _entry

    raise SystemExit(_entry())
