"""Read-only checks for the SSH shell, manager, worker protocol and physical store."""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
import re
import socket
import sqlite3
import subprocess

from . import __version__, remote_records


SCHEMA = "lam.remote-doctor.v1"
SHELL_MARKER = "lam-remote-shell-ok"
TIMEOUT = 20
OUTPUT_LIMIT = 16 * 1024


def inspect_store(request, root):
    """Worker reply. Resolve the same owner as inventory without listing records."""
    reply = {"diagnostic": SCHEMA, "protocol": remote_records.PROTOCOL,
             "destination": request["destination"], "execution_host": socket.gethostname(),
             "manager_version": __version__}
    try:
        owner = remote_records._owner(request["client"], root, request.get("pi_agent_dir"))
    except FileNotFoundError:
        error = "store_path_or_identity_file_missing"
    except PermissionError:
        error = "store_access_denied"
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        error = "store_identity_unverified"
    else:
        return 0, {**reply, "goal_status": "complete", "owner": owner}
    return 1, {**reply, "goal_status": "blocked", "error": error}


def diagnose(request, expected_owner=None):
    checks = dict.fromkeys(("ssh", "manager", "protocol", "store"), "not_checked")
    host = request["destination"]
    report = {"schema_version": SCHEMA, "destination": host, "client": request["client"],
              "requested_root": request["root"], "checks": checks, "goal_status": "blocked"}

    def failed(stage, reason):
        checks[stage] = "failed"
        report["error"] = reason
        return 1, report

    # Check arguments before opening a connection. The expected ID is a local
    # comparison with the observed owner, never an operation approval.
    try:
        remote_records._validate(request)
    except (ValueError, TypeError):
        return 1, {**report, "error": "invalid_diagnostic_target"}
    if expected_owner is not None and not remote_records.HASH.fullmatch(expected_owner):
        return 1, {**report, "error": "invalid_expected_store_id"}

    for stage, command in (("ssh", "echo " + SHELL_MARKER), ("manager", "local-agent-manager --version")):
        try:
            code, raw = remote_records._ssh(host, b"", command=command, timeout=TIMEOUT, limit=OUTPUT_LIMIT)
        except FileNotFoundError:
            return failed(stage, "ssh_client_unavailable")
        except subprocess.TimeoutExpired:
            return failed(stage, "ssh_probe_timed_out")
        except (OSError, ValueError, subprocess.SubprocessError):
            return failed(stage, "ssh_probe_unverified")
        if code == 255:
            return failed(stage, "ssh_connection_unverified")
        if stage == "ssh":
            if code or raw.strip() != SHELL_MARKER.encode("ascii"):
                return failed(stage, "remote_shell_unverified")
        else:
            if code:
                return failed(stage, "remote_manager_not_runnable")
            version = raw.strip()
            if not re.fullmatch(rb"[0-9][A-Za-z0-9.!+_-]{0,79}", version):
                return failed(stage, "remote_manager_version_unverified")
            report["manager_version"] = version.decode("ascii")
        checks[stage] = "ok"

    code, reply = remote_records.exchange(request)
    if reply.get("diagnostic") != SCHEMA:
        reason = ("worker_diagnostic_unsupported" if reply.get("error") == "remote_request_invalid"
                  else "worker_response_unverified")
        return failed("protocol", reason)
    hostname = reply.get("execution_host")
    if (not isinstance(hostname, str) or not 0 < len(hostname) <= 255 or not hostname.isprintable()
            or reply.get("manager_version") != report["manager_version"]):
        return failed("protocol", "worker_identity_unverified")
    checks["protocol"] = "ok"
    report.update(execution_host=hostname, protocol=reply["protocol"])
    if code:
        reason = reply.get("error")
        if reason not in ("store_path_or_identity_file_missing", "store_access_denied"):
            reason = "store_identity_unverified"
        return failed("store", reason)
    owner = reply["owner"]
    observed_root = owner.get("root")
    if not isinstance(observed_root, str) or not observed_root or not observed_root.isprintable():
        return failed("store", "store_identity_unverified")
    path_type = PureWindowsPath if PureWindowsPath(observed_root).drive else PurePosixPath
    if (reply.get("goal_status") != "complete" or not path_type(observed_root).is_absolute()
            or path_type(observed_root) != path_type(request["root"])):
        return failed("store", "store_identity_unverified")
    report["owner"] = {key: owner[key] for key in ("client", "root", "store_id", "instance_id",
                                                    "agent_dir", "cindy_ssh_provider") if key in owner}
    if expected_owner is not None and expected_owner != owner["store_id"]:
        return failed("store", "store_identity_changed")
    checks["store"] = "ok"
    report["goal_status"] = "complete"
    return 0, report
