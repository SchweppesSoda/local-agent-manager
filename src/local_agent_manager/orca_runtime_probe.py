"""Measure the configured Windows Codex on disposable records before Orca can use it."""

from contextlib import closing
from datetime import date, timedelta
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import uuid

from .orca_codex_schema import ATTACHED_STATE_SCHEMA, SCHEMA_METADATA
from .orca_runtime import IsolatedCodexRuntime, _windows_directory


def _verify_publisher(executable: Path) -> None:
    # Only the owned standalone copy is executed, after Windows verifies its signer.
    shell = _windows_directory() / "System32/WindowsPowerShell/v1.0/powershell.exe"
    check = ("$ErrorActionPreference='Stop'; "
             "Import-Module ($PSHOME + '\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1'); "
             "$s = Get-AuthenticodeSignature -LiteralPath $env:LAM_PROBE_EXE; "
             "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.GetNameInfo('SimpleName',$false) "
             "-cne 'OpenAI OpCo, LLC') { exit 1 }")
    environment = {**os.environ, "LAM_PROBE_EXE": str(executable), "PSModulePath": str(shell.parent / "Modules")}
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", check],
                            env=environment, cwd=executable.parent,
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("orca_runtime_publisher_unverified")


def _seed(home, ids, attached):
    parent, child, _ = ids
    index = home / "session_index.jsonl"
    sessions = home / "sessions/2026/10/03"
    sessions.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db, index.open("x", encoding="utf-8") as stream:
        db.execute("INSERT INTO projects(id,name,position,created_at_ms,updated_at_ms) VALUES('shared','probe',0,1,1)")
        for tid in ids:
            path = sessions / f"rollout-2026-10-03T00-00-00-{tid}.jsonl"
            source = {"subAgent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}} if tid == child else "cli"
            meta = {"id": tid, "timestamp": "2026-10-03T00:00:00Z", "cwd": str(home.parent),
                    "source": source, "originator": "codex_cli_rs", "model_provider": "janitor_local", "cli_version": "probe"}
            path.write_text(json.dumps({"timestamp": meta["timestamp"], "type": "session_meta", "payload": meta}) + "\n")
            db.execute("INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,"
                       "sandbox_policy,approval_mode,project_id) VALUES(?,?,1,1,?,'janitor_local',?,'','{}','never','shared')",
                       (tid, str(path), json.dumps(source) if tid == child else source, str(home.parent)))
            db.execute("INSERT INTO thread_attachments VALUES(?,?,'file','shared','{}',1)", (tid, tid))
            if attached:
                db.execute("INSERT INTO guardian_review_feedback VALUES(?,?,?)", (tid, tid, b"probe feedback"))
                db.execute("INSERT INTO thread_read_receipts VALUES(?,'turn','revision','published')", (tid,))
            stream.write(json.dumps({"id": tid, "thread_name": "", "updated_at": meta["timestamp"]}) + "\n")
        db.execute("INSERT INTO thread_spawn_edges VALUES(?,?,'closed')", (parent, child))
        if attached:
            db.execute("UPDATE threads SET attached_thread_parent_id=? WHERE id=?", (parent, child))
    return sessions


def _rows(home, exclude):
    """All logical rows in the five synthetic databases, including migration history."""
    result = {}
    scoped = {"threads": "id", "thread_attachments": "thread_id", "guardian_review_feedback": "thread_id",
              "thread_read_receipts": "thread_id", "thread_spawn_edges": "child_thread_id"}
    for filename in SCHEMA_METADATA:
        with closing(sqlite3.connect((home / filename).as_uri() + "?mode=ro", uri=True)) as db:
            for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                query = 'SELECT * FROM "' + table.replace('"', '""') + '"'
                key = scoped.get(table) if filename == "state_5.sqlite" else None
                args = ()
                if key and exclude:
                    query += f" WHERE {key} NOT IN (?,?)"
                    args = exclude
                result[filename, table] = sorted(db.execute(query, args), key=repr)
    return result


def _probe(binary, digest, home):
    from .orca_target_safety import _DATABASES, _FAMILY, _STARTUP_PATHS, _schema_metadata, _startup_manifest
    with IsolatedCodexRuntime(binary, expected_sha256=digest) as runtime:
        _verify_publisher(runtime.executable)
        with runtime.server(codex_home=home, timeout=15) as server:
            account = server.request("account/read", {"refreshToken": False})
            if account.get("account") is not None or account.get("requiresOpenaiAuth") is not False:
                raise ValueError("orca_runtime_account_not_isolated")
            server.request("thread/list", {})
    schemas = {name: _schema_metadata(home / name)["schema_sha256"] for name in _DATABASES}
    skills = home / "skills/.system"
    files = {}
    for path in skills.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            files[path.relative_to(home).as_posix()] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    arg_dirs = [p.name for p in (home / "tmp").iterdir()]
    if arg_dirs not in (["arg0"], ["arg0-windows"]):
        raise ValueError("orca_runtime_startup_unverified")
    contract = {"schemas": schemas, "skills": files, "arg0": arg_dirs[0], "sandbox_log": (home / ".sandbox").is_dir(),
                "index_lock": False, "sandbox_history_limit": 0}
    history = {}
    if contract["sandbox_log"]:
        # The reviewed daily writer retains 90 logs. Exercise 88 older slots
        # and reserve room for a new UTC day even across midnight.
        for day in range(88):
            name = f"sandbox.{date(2000, 1, 1) + timedelta(days=day)}.log"
            path = home / ".sandbox" / name
            path.write_bytes(name.encode())
            history[path] = path.stat().st_ino
    ids = tuple(str(uuid.uuid4()) for _ in range(3))
    sessions = _seed(home, ids, schemas["state_5.sqlite"] == ATTACHED_STATE_SCHEMA["schema_sha256"])
    sentinel, = sessions.glob("*" + ids[-1] + ".jsonl")
    before = (sentinel.stat().st_ino, sentinel.read_bytes())
    index = home / "session_index.jsonl"
    initial_index = [json.loads(line) for line in index.read_text().splitlines()]
    expected_index = [entry for entry in initial_index if entry["id"] == ids[-1]]
    expected = _rows(home, ids[:2])
    with IsolatedCodexRuntime(binary, expected_sha256=digest) as runtime:
        with runtime.server(codex_home=home, timeout=15) as server:
            _startup_manifest(home, after_start=True, contract=contract)
            server.delete_thread(ids[0])
    allowed = set(_FAMILY) | set(_STARTUP_PATHS) | {".sqlite-maintenance.lock", "sessions"}
    contract["index_lock"] = (home / "session_index.jsonl.lock").exists()
    if contract["index_lock"]:
        allowed.add("session_index.jsonl.lock")
    if contract["sandbox_log"]:
        allowed.add(".sandbox")
        if all(p.is_file() and p.stat().st_ino == inode and p.read_bytes() == p.name.encode() for p, inode in history.items()):
            contract["sandbox_history_limit"] = len(history)
    _startup_manifest(home, after_start=True, contract=contract)
    remaining = [json.loads(line) for line in index.read_text().splitlines()]
    actual = _rows(home, ())
    # Protocol tracing can flush new process diagnostics on a timer. Preserve
    # all pre-existing rows and allow only app-server logs without a thread ID.
    logs = ("logs_2.sqlite", "logs")
    prior_logs, final_logs = set(expected.pop(logs)), set(actual.pop(logs))
    with closing(sqlite3.connect(home / logs[0])) as db:
        cols = [row[1] for row in db.execute("PRAGMA table_info(logs)")]
    for state in (expected, actual):
        state.pop(("logs_2.sqlite", "sqlite_sequence"))
    new_logs = final_logs - prior_logs
    if (any(p.name not in allowed for p in home.iterdir()) or expected != actual or not prior_logs <= final_logs
            or any(row[cols.index("thread_id")] is not None or not row[cols.index("target")].startswith("codex_app_server")
                   for row in new_logs)
            or remaining != expected_index or list(sessions.iterdir()) != [sentinel]
            or (sentinel.stat().st_ino, sentinel.read_bytes()) != before):
        raise ValueError("orca_runtime_delete_scope_unverified")
    return contract


@lru_cache(maxsize=8)
def _measured(binary, digest):
    with tempfile.TemporaryDirectory(prefix="lam-orca-probe-") as directory:
        home = Path(directory).resolve() / "home"
        home.mkdir()
        try:
            return json.dumps(_probe(binary, digest, home), sort_keys=True)
        except (RuntimeError, sqlite3.Error, subprocess.SubprocessError, KeyError, OSError) as exc:
            raise ValueError("orca_runtime_probe_failed") from exc


def probe_contract(binary, digest):
    # Cache successful probes only; never expose the mutable cached evidence.
    return json.loads(_measured(binary, digest))
