"""Explicit, opt-in client connections; inventory remains read-only."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import TextIO

from . import frozen_files
from .environment_config import _reject_constant, _unique_object
from .environment_paths import BoundedPathProbe


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(ord(c) < 32 for c in value):
        raise argparse.ArgumentTypeError("请输入绝对路径")
    return path


def add_connection_commands(commands) -> None:
    cleanup = commands.add_parser("t3-cleanup", help="自动准备 T3 原生记录和桌面绑定")
    actions = cleanup.add_subparsers(dest="t3_cleanup_action", required=True)
    prepare = actions.add_parser("prepare", help="从配置和原生 ID 自动发现完整清理所需的绑定")
    prepare.add_argument("--t3code-root", type=_absolute)
    prepare.add_argument("--out", type=_absolute, help="可选的绑定文件路径")
    prepare.add_argument("--json", action="store_true")

    connect = commands.add_parser("claude-connect", help="准备或应用 Claude 客户端配置")
    actions = connect.add_subparsers(dest="connection_action", required=True)
    prepare = actions.add_parser("prepare", help="只读生成精确配置计划")
    prepare.add_argument("--target", choices=("paseo", "t3code"), required=True)
    prepare.add_argument("--config", type=_absolute, required=True)
    prepare.add_argument("--claude-binary", type=_absolute, required=True)
    prepare.add_argument("--claude-home", type=_absolute, required=True)
    prepare.add_argument("--provider", default="claude-cindy")
    prepare.add_argument("--out", type=_absolute, required=True)
    apply = actions.add_parser("apply", help="核对计划后修改选定配置文件")
    apply.add_argument("--plan", type=_absolute, required=True)
    apply.add_argument("--approve", required=True, help="已核对计划的 plan_sha256")
    apply.add_argument("--clients-closed", action="store_true")
    for action in (prepare, apply):
        action.add_argument("--json", action="store_true")

    sessions = commands.add_parser("claude-import", help="通过官方接口导入原生历史")
    actions = sessions.add_subparsers(dest="import_action", required=True)
    prepare = actions.add_parser("prepare", help="生成精确客户端与项目导入计划")
    prepare.add_argument("--target", choices=("paseo", "t3code"), required=True)
    prepare.add_argument("--executable", type=_absolute, required=True)
    prepare.add_argument("--profile", type=_absolute, required=True)
    prepare.add_argument("--project", type=_absolute, required=True)
    prepare.add_argument("--native-id")
    prepare.add_argument("--native-transcript", type=_absolute,
                         help="Paseo：所选 Claude home/projects 下该 UUID 的精确 JSONL")
    prepare.add_argument("--project-id")
    prepare.add_argument("--provider", default="claude-cindy")
    prepare.add_argument("--out", type=_absolute, required=True)
    apply = actions.add_parser("apply", help="执行已核对的官方导入请求")
    apply.add_argument("--plan", type=_absolute, required=True)
    apply.add_argument("--approve", required=True, help="已核对计划的 plan_sha256")
    apply.add_argument("--accept-project-history", action="store_true",
                       help="T3：接受项目级近期历史范围，可能含 Claude、Codex 和 Pi")
    verify = actions.add_parser("verify", help="只读核对已选定客户端的导入引用")
    verify.add_argument("--plan", type=_absolute, required=True)
    for action in (prepare, apply, verify):
        action.add_argument("--json", action="store_true")

    memory = commands.add_parser("cindy-memory", help="盘点或导出选定项目记忆")
    actions = memory.add_subparsers(dest="memory_action", required=True)
    for name in ("inspect", "export"):
        action = actions.add_parser(name)
        action.add_argument("--maker-memory-root", type=_absolute, required=True)
        action.add_argument("--project", type=_absolute, required=True)
        action.add_argument("--json", action="store_true")
        if name == "export":
            action.add_argument("--shard", action="append", required=True)
            action.add_argument("--out", type=_absolute, required=True)


def _read_plan(path: Path) -> dict:
    _plan_parent(path)
    _, raw = frozen_files.read_file(path.parent, path.name, content=True, limit=256 * 1024)
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("connection_plan_invalid") from None


def _save_plan(path: Path, plan: dict) -> None:
    _plan_parent(path)
    data = (json.dumps(plan, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > 256 * 1024:
        raise ValueError("connection_plan_too_large")
    # Refuse existing outputs; fence ancestors before creating the selected file.
    with frozen_files._parent_fence(path.parent, path.name) as checked:
        fd = os.open(checked, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def _plan_parent(path: Path) -> None:
    status, _ = BoundedPathProbe().inspect(path.parent, "connection plan", directory=True)
    if status != "present":
        raise ValueError("connection_plan_parent_unavailable")


def run_connection_command(args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO) -> int:
    from .t3code_discovery import BindingDiscoveryError, discover_bindings
    from .claude_connection import ClaudeConnectionError, apply_connection, prepare_connection
    from .cindy_memory import CindyMemoryError, export_memory, inspect_memory
    from .claude_session_import import (SessionImportError, prepare_paseo_import,
        prepare_t3code_import, run_session_import, verify_session_import)

    try:
        if args.environment_command == "t3-cleanup":
            manifest = discover_bindings(args.t3code_root)
            if args.out:
                _save_plan(args.out, manifest)
            report = {"schema_version": "lam.t3-cleanup-prepare.v1", "status": "ready",
                      "binding_manifest": manifest}
        elif args.environment_command == "claude-connect":
            if args.connection_action == "prepare":
                report = prepare_connection(target=args.target, config_path=args.config,
                    claude_binary=args.claude_binary, claude_home=args.claude_home,
                    provider_id=args.provider)
                _save_plan(args.out, report)
            else:
                report = apply_connection(_read_plan(args.plan), approved_sha256=args.approve,
                                          clients_closed=args.clients_closed)
        elif args.environment_command == "claude-import":
            if args.import_action == "prepare":
                options = dict(executable=args.executable, profile=args.profile, cwd=args.project)
                if args.target == "paseo":
                    if not args.native_id or not args.native_transcript or args.project_id:
                        raise SessionImportError("paseo_import_exact_transcript_required")
                    report = prepare_paseo_import(**options, native_id=args.native_id,
                                                  native_transcript=args.native_transcript,
                                                  provider=args.provider)
                else:
                    if args.native_id or args.native_transcript:
                        raise SessionImportError("t3code_import_exact_session_unsupported")
                    if not args.project_id or args.provider != "claude-cindy":
                        raise SessionImportError("t3code_import_project_scope_required")
                    report = prepare_t3code_import(**options, project_id=args.project_id)
                _save_plan(args.out, report)
            elif args.import_action == "apply":
                report = run_session_import(_read_plan(args.plan), approved_sha256=args.approve,
                                             accept_project_history=args.accept_project_history)
            else:
                report = verify_session_import(_read_plan(args.plan))
        else:
            options = dict(maker_memory_root=args.maker_memory_root, project=args.project)
            if args.memory_action == "inspect":
                report = inspect_memory(**options)
            else:
                report = export_memory(**options, shards=args.shard, out=args.out)
    except BindingDiscoveryError as exc:
        report = {"schema_version": "lam.t3-cleanup-prepare.v1", "status": "blocked", "blockers": exc.blockers}
    except (ClaudeConnectionError, CindyMemoryError, SessionImportError) as exc:
        uncertain = exc.code in {"config_write_unverified", "output_unverified", "config_lock_cleanup_unverified",
                                 "t3code_import_preflight_and_session_revoke_unverified"}
        report = {"schema_version": "larj.connection-error.v1",
                  "status": "unknown" if uncertain else "blocked", "code": exc.code}
    except (OSError, ValueError, RuntimeError, RecursionError, sqlite3.Error):
        # Parser/config/tool output can contain secrets. Only stable error codes leave the CLI.
        report = {"schema_version": "larj.connection-error.v1", "status": "blocked",
                  "code": "connection_io_or_plan_invalid"}
    json.dump(report, stdout, ensure_ascii=True, indent=2, allow_nan=False)
    stdout.write("\n")
    return 1 if report["status"] in {"blocked", "error", "unknown", "partial", "not_observed", "archived"} else 0
