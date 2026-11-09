"""Agent environment evidence and explicit client connection workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TextIO

from .rendering import escape_terminal_controls as display


def _root_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or "\0" in value:
        raise argparse.ArgumentTypeError("请输入有效的绝对目录路径")
    return path


def _path_value(value: str) -> str:
    if "\0" in value:
        raise argparse.ArgumentTypeError("PATH 列表不能包含 NUL 字符")
    return value


def add_environment_commands(parser: argparse.ArgumentParser) -> None:
    from .environment_inventory import ENVIRONMENT_CLIENT_CHOICES

    commands = parser.add_subparsers(
        dest="environment_command",
        required=True,
        title="环境命令",
    )
    from .connection_cli import add_connection_commands
    add_connection_commands(commands)
    for name, help_text in (
        ("inventory", "盘点已识别的运行时和配置路径"),
        ("doctor", "诊断运行时选择、路径和配置证据"),
    ):
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--json", action="store_true", help="输出机器可读 JSON")
        command.add_argument(
            "--home", type=_root_path, metavar="PATH", help="覆盖用户主目录（绝对路径）"
        )
        command.add_argument(
            "--appdata", type=_root_path, metavar="PATH", help="覆盖 APPDATA（绝对路径）"
        )
        command.add_argument(
            "--local-appdata", type=_root_path, metavar="PATH",
            help="覆盖 LOCALAPPDATA（绝对路径）",
        )
        command.add_argument(
            "--path-value", type=_path_value, metavar="PATH_LIST",
            help="覆盖 PATH 目录列表，空字符串跳过 PATH 探测",
        )
        command.add_argument(
            "--client", action="append", choices=ENVIRONMENT_CLIENT_CHOICES,
            help="限制客户端探测范围，可重复",
        )


def run_environment_command(
    args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO
) -> int:
    if args.environment_command not in {"inventory", "doctor"}:
        from .connection_cli import run_connection_command
        return run_connection_command(args, stdout=stdout, stderr=stderr)

    from .environment_inventory import collect_environment_inventory

    try:
        report = collect_environment_inventory(
            user_home=args.home,
            appdata=args.appdata,
            local_appdata=args.local_appdata,
            path_value=args.path_value,
            clients=args.client,
        )
    except OSError as exc:
        if args.json:
            _write_json(
                {
                    "schema_version": "larj.environment-error.v1",
                    "status": "error",
                    "code": "environment_probe_failed",
                    "message": str(exc),
                },
                stdout,
            )
        else:
            print(f"环境探测失败：{display(exc)}", file=stderr)
        return 1

    doctor = args.environment_command == "doctor"
    if doctor:
        report = {
            **report,
            "schema_version": "larj.environment-doctor.v1",
            "status": _doctor_status(report),
        }

    if args.json:
        _write_json(report, stdout)
    else:
        _write_human(report, stdout)
    return 1 if doctor and report["status"] == "error" else 0


def _doctor_status(report: dict[str, Any]) -> str:
    severities = {finding["severity"] for finding in report["findings"]}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "attention"
    if report["coverage"]["status"] == "partial":
        return "inconclusive"
    return "no_issues_observed"


def _write_json(report: dict[str, Any], stdout: TextIO) -> None:
    json.dump(report, stdout, indent=2, allow_nan=False)
    stdout.write("\n")


def _write_human(report: dict[str, Any], stdout: TextIO) -> None:
    def line(message: str) -> None:
        print(message, file=stdout)

    if "status" in report:
        line(f"Agent 环境诊断 — {display(report['status'])}")
    else:
        line("Agent 环境盘点")
    line(f"观测时间  {display(report['observed_at'])}")

    line("\n探测根目录")
    for name, path in report["roots"].items():
        line(f"  {display(name)}  {display(path) or '未知'}")

    line("\n运行时")
    if not report["runtimes"]:
        line("  范围内未观测到运行时")
    for runtime in report["runtimes"]:
        version = display(runtime["version"]) or "未知版本"
        source = display(runtime["version_source"])
        if source:
            version = f"{version} [{source}]"
        line(f"  {display(runtime['client'])} / {display(runtime['engine'])}  {version}")
        line(f"    {display(runtime['path'])} [{display(runtime['path_status'])}]")
        line(f"    ID {display(runtime['id'])} · 选择 {display(runtime['selection'])}")

    line("\n配置与存储关联")
    if not report["bindings"]:
        line("  范围内未观测到关联")
    for binding in report["bindings"]:
        path = display(binding["store_path"]) or "未知路径"
        runtime_id = display(binding["executable_id"]) or "未知"
        line(
            f"  {display(binding['client'])} / {display(binding['engine'])}  "
            f"{path} [{display(binding['store_status'])}]"
        )
        line(
            f"    ID {display(binding['id'])} · 运行时 {runtime_id} · "
            f"选择 {display(binding['selection'])}"
        )

    line("\n发现")
    if not report["findings"]:
        line("  范围内无诊断条目")
    for finding in report["findings"]:
        line(
            f"  [{display(finding['severity'])}] {display(finding['code'])}  "
            f"{display(finding['message'])}"
        )
        if finding["affected_ids"]:
            line(f"    相关 ID  {', '.join(map(display, finding['affected_ids']))}")
        if finding["suggestion"]:
            line(f"    {display(finding['suggestion'])}")

    coverage = report["coverage"]
    line(f"\n覆盖范围  {display(coverage['status'])}")
    line(f"  客户端  {', '.join(map(display, coverage['clients']))}")
    for issue in coverage["issues"]:
        line(
            f"  [{display(issue['code'])}] {display(issue['probe'])}  "
            f"{display(issue['path'])}"
        )
    for limitation in coverage["limitations"]:
        line(f"  {display(limitation)}")
