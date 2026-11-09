"""Explicit local Paseo and T3 session imports through their product interfaces."""

from __future__ import annotations

from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from unicodedata import normalize
from urllib.parse import urlsplit
from uuid import UUID

from . import frozen_files
from .environment_paths import BoundedPathProbe, local_path
from .paseo_store import PaseoAdapter
from .sqlite_utils import connect_readonly
from .t3code_store import V2, database_path, qualify, rows


SCHEMA = "larj.claude-session-import.v1"
PASEO_VERSION = "0.15.3"
T3_VERSION = "0.1.8-nightly.20261108.4353"
_PASEO_ARCHIVE = "resources/app.asar"
_PASEO_RUNNER = "resources/app.asar.unpacked/dist/daemon/node-entrypoint-runner.js"
_T3_ARCHIVE = "resources/server.asar"
_MAX_JSON = 1024 * 1024
_MAX_PROJECT_MAPPINGS = 1000
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
_MAX_TRANSCRIPT_LINE = 4 * 1024 * 1024
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z", re.ASCII)
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z", re.ASCII)
_SERVER_ID = re.compile(r"srv_[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_LOCAL_PASEO = re.compile(
    r"(?:(?:127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})|"
    r"(?:pipe://)?\\\\\.\\pipe\\(?:LOCAL\\)?[A-Za-z0-9_.-]{1,180})\Z",
    re.ASCII | re.IGNORECASE,
)


class SessionImportError(ValueError):
    """A local import request could not be qualified."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _local_path(value) -> Path:
    if not isinstance(value, (str, Path)):
        raise SessionImportError("import_path_invalid")
    path = local_path(value)
    if path is None or not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise SessionImportError("import_path_invalid")
    return path


def _path_info(path, *, directory):
    status, info = BoundedPathProbe().inspect(path, "claude_session_import", directory=directory)
    if status != "present" or info is None:
        raise SessionImportError("import_local_path_unverified")
    frozen_files._plain(info, directory=directory)
    return info


def _directory(path: Path) -> list[int]:
    return frozen_files._identity(_path_info(path, directory=True))


def _same_directory(observed, expected):
    try:
        observed = _local_path(observed)
    except SessionImportError:
        return False
    expected = Path(expected)
    # Foreign paths found in product metadata never need a filesystem probe.
    return observed == expected and _directory(observed) == _directory(expected)


def _executable(path: Path) -> dict:
    if path.suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        raise SessionImportError("import_executable_required")
    if os.name == "nt" and path.suffix.casefold() != ".exe":
        raise SessionImportError("import_executable_required")
    _path_info(path, directory=False)
    return frozen_files.read_file(path.parent, path.name)[0]


def _unique_object(items):
    result = {}
    for name, value in items:
        if name in result:
            raise SessionImportError("import_json_duplicate_field")
        result[name] = value
    return result


def _reject_constant(_value):
    raise SessionImportError("import_json_invalid")


def _decode(raw):
    if not isinstance(raw, bytes) or len(raw) > _MAX_JSON:
        raise SessionImportError("import_json_budget_exceeded")
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant)


def _object(root, relative):
    proof, raw = frozen_files.read_file(root, relative, content=True, limit=_MAX_JSON)
    value = _decode(raw)
    if not isinstance(value, dict):
        raise SessionImportError("import_json_object_required")
    return proof, value


def _profile_id(root, relative, pattern):
    _, raw = frozen_files.read_file(root, relative, content=True, limit=256)
    value = raw.decode("utf-8").strip()
    if not pattern.fullmatch(value):
        raise SessionImportError("import_profile_identity_unverified")
    return value


def _paseo_locator(profile, server_id):
    _, value = _object(profile, "paseo.pid")
    listen = value.get("listen")
    match = _LOCAL_PASEO.fullmatch(listen) if isinstance(listen, str) else None
    if (
        value.get("serverId") != server_id
        or type(value.get("pid")) is not int
        or value["pid"] <= 0
        or not isinstance(value.get("startedAt"), str)
        or not match
        or match[1] and int(match[1]) > 65535
    ):
        raise SessionImportError("paseo_import_local_daemon_unverified")
    return {key: value[key] for key in ("pid", "serverId", "startedAt", "listen")}


def _t3_locator(profile):
    _, value = _object(profile, "userdata/server-runtime.json")
    raw = value.get("origin")
    url = urlsplit(raw) if isinstance(raw, str) else None
    if (
        not url or url.scheme != "http" or url.hostname != "127.0.0.1"
        or url.path not in {"", "/"} or url.query or url.fragment or url.username or url.password
        or url.port is None or not 1 <= url.port <= 65535
        or type(value.get("port")) is not int or url.port != value["port"]
        or type(value.get("version")) is not int or value["version"] != 1
        or value.get("serverVersion") != T3_VERSION
        or type(value.get("pid")) is not int or value["pid"] <= 0
        or not isinstance(value.get("startedAt"), str)
        or not isinstance(value.get("ownerId"), str) or not value["ownerId"]
        or value.get("devUrl") or value.get("managedPolicy")
    ):
        raise SessionImportError("t3code_import_local_server_unverified")
    return {key: value[key] for key in ("pid", "origin", "port", "startedAt", "serverVersion", "ownerId")}


def _configured_claude(executable, home):
    binary, root = _local_path(executable), _local_path(home)
    return {"executable": str(binary), "identity": _executable(binary),
            "config_root": str(root), "config_root_identity": _directory(root)}


def _paseo_provider(profile, provider):
    proof, settings = _object(profile, "config.json")
    instance = settings["agents"]["providers"][provider]
    command = instance["command"]
    home_values = [value for key, value in instance["env"].items() if key.upper() == "CLAUDE_CONFIG_DIR"]
    if (type(settings.get("version")) is not int or settings["version"] != 1
            or instance.get("extends") != "claude" or instance.get("enabled") is False
            or not isinstance(command, list) or len(command) != 1 or len(home_values) != 1):
        raise SessionImportError("paseo_import_claude_provider_unverified")
    # The qualified provider invokes the exact binary without extra flags that
    # could override the config directory, session ID or resume behavior.
    return proof, _configured_claude(command[0], home_values[0])


def _t3_provider(profile):
    proof, settings = _object(profile, "userdata/settings.json")
    instance = settings["providerInstances"]["claudeAgent"]
    config = instance["config"]
    if instance.get("driver") != "claudeAgent" or instance.get("enabled") is False or config.get("enabled") is False:
        raise SessionImportError("t3code_import_claude_provider_unverified")
    return proof, _configured_claude(config["binaryPath"], config["homePath"])


def _common(target, executable, profile, cwd):
    executable, profile, cwd = map(_local_path, (executable, profile, cwd))
    return executable, profile, cwd, {
        "schema_version": SCHEMA, "status": "ready", "target": target, "executable": str(executable),
        "runtime": [_executable(executable)], "profile": str(profile),
        "profile_identity": _directory(profile), "cwd": str(cwd), "cwd_identity": _directory(cwd),
    }


def _sha256(plan):
    encoded = json.dumps({key: val for key, val in plan.items() if key != "plan_sha256"},
                         ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _seal(plan):
    return {**plan, "plan_sha256": _sha256(plan)}


def _paseo_transcript(native_transcript, config_root, native_id, cwd):
    path = _local_path(native_transcript)
    root = Path(config_root)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise SessionImportError("paseo_import_native_transcript_outside_claude_home") from None
    # Paseo v0.15.3 first resolves the Claude SDK project directory from the
    # real cwd. Qualify that exact short layout, without using its UUID fallback.
    canonical = str(cwd.resolve(strict=True))
    if sys.platform == "darwin":
        canonical = normalize("NFC", canonical)
    encoded = "".join(char if char.isascii() and char.isalnum() else "-" * (2 if ord(char) > 0xFFFF else 1)
                      for char in canonical)
    if len(encoded) > 200 or relative.parts != ("projects", encoded, f"{native_id}.jsonl"):
        raise SessionImportError("paseo_import_native_transcript_path_unverified")
    _path_info(path, directory=False)
    proof, raw = frozen_files.read_file(root, relative.as_posix(), content=True, limit=_MAX_TRANSCRIPT_BYTES)
    transcript = BytesIO(raw)
    matched_message = False
    verified_cwds = set()
    while line := transcript.readline(_MAX_TRANSCRIPT_LINE + 1):
        if len(line) > _MAX_TRANSCRIPT_LINE:
            raise SessionImportError("paseo_import_native_transcript_line_budget_exceeded")
        if not line.strip():
            continue
        value = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise SessionImportError("paseo_import_native_transcript_unverified")
        observed_id = value.get("sessionId", value.get("session_id"))
        observed_cwd = value.get("cwd")
        if observed_id is not None and observed_id != native_id:
            raise SessionImportError("paseo_import_native_transcript_identity_conflict")
        if observed_cwd is not None:
            if not isinstance(observed_cwd, str):
                raise SessionImportError("paseo_import_native_transcript_identity_conflict")
            if observed_cwd not in verified_cwds:
                if not _same_directory(observed_cwd, cwd):
                    raise SessionImportError("paseo_import_native_transcript_identity_conflict")
                verified_cwds.add(observed_cwd)
        if (value.get("type") in {"user", "assistant"} and isinstance(value.get("message"), dict)
                and observed_id == native_id and observed_cwd is not None):
            matched_message = True
    if not matched_message:
        raise SessionImportError("paseo_import_native_transcript_unverified")
    return {"path": str(path), "identity": proof}


def prepare_paseo_import(*, executable, profile, cwd, native_id, native_transcript, provider="claude-cindy"):
    """Bind one explicit Claude UUID and original cwd to a local Paseo daemon."""
    try:
        if str(UUID(native_id)) != native_id or not _PROVIDER_ID.fullmatch(provider):
            raise SessionImportError("paseo_import_target_invalid")
        executable, profile, cwd, plan = _common("paseo", executable, profile, cwd)
        server_id = _profile_id(profile, "server-id", _SERVER_ID)
        bundled = executable.name == "Paseo.exe"
        if bundled:
            plan["runtime"].extend(frozen_files.read_file(executable.parent, part)[0]
                                   for part in (_PASEO_ARCHIVE, _PASEO_RUNNER))
        config, claude = _paseo_provider(profile, provider)
        source = _paseo_transcript(native_transcript, claude["config_root"], native_id, cwd)
        return _seal({**plan, "profile_id": server_id, "locator": _paseo_locator(profile, server_id),
                      "version": PASEO_VERSION, "bundled": bundled, "provider": provider,
                      "native_id": native_id, "scope": "exact_claude_session",
                      "settings": config, "claude": claude, "native_transcript": source})
    except SessionImportError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError):
        raise SessionImportError("paseo_import_preflight_unverified") from None


def _t3_state(profile, project_id, cwd):
    path = database_path(profile)
    identity = frozen_files._identity(path.lstat())
    with closing(connect_readonly(path)) as db:
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        db.execute("BEGIN")
        qualify(db)
        project = rows(db, "SELECT workspace_root FROM projection_projects WHERE project_id=? AND deleted_at IS NULL",
                       (project_id,))
        if len(project) != 1 or not _same_directory(project[0]["workspace_root"], cwd):
            raise SessionImportError("t3code_import_project_unverified")
        mappings = rows(db, f"""
            SELECT t.thread_id, p.driver, p.provider_instance_id,
                json_extract(p.payload_json,'$.nativeThreadRef.nativeId') AS native_id
            FROM {V2}threads AS t JOIN {V2}provider_threads AS p ON t.thread_id=p.thread_id
            WHERE t.project_id=? AND t.deleted_at IS NULL AND t.thread_id LIKE 'import:%'
                AND json_extract(p.payload_json,'$.nativeThreadRef.strength')='strong'
                AND json_extract(p.payload_json,'$.nativeThreadRef.driver')=p.driver
            ORDER BY t.thread_id,p.provider_thread_id
            LIMIT ?
            """, (project_id, _MAX_PROJECT_MAPPINGS + 1))
    if frozen_files._identity(database_path(profile).lstat()) != identity:
        raise SessionImportError("t3code_import_project_database_replaced")
    if len(mappings) > _MAX_PROJECT_MAPPINGS:
        raise SessionImportError("t3code_import_project_projection_budget_exceeded")
    safe = []
    for row in mappings:
        if (row["driver"] not in {"claudeAgent", "codex", "pi"}
                or any(not isinstance(row[field], str) or not _RECORD_ID.fullmatch(row[field])
                       for field in ("thread_id", "provider_instance_id"))):
            raise SessionImportError("t3code_import_project_projection_unverified")
        if row["driver"] == "pi":
            _local_path(row["native_id"])
        elif not isinstance(row["native_id"], str) or not _RECORD_ID.fullmatch(row["native_id"]):
            raise SessionImportError("t3code_import_project_projection_unverified")
        if row not in safe:
            safe.append(row)
    return safe


def prepare_t3code_import(*, executable, profile, cwd, project_id):
    """Bind T3's existing project and version-qualified whole-project import."""
    try:
        if not _RECORD_ID.fullmatch(project_id):
            raise SessionImportError("t3code_import_project_id_invalid")
        executable, profile, cwd, plan = _common("t3code", executable, profile, cwd)
        if executable.name != "T3 Code (Nightly).exe":
            raise SessionImportError("t3code_import_bundled_executable_required")
        plan["runtime"].append(frozen_files.read_file(executable.parent, _T3_ARCHIVE)[0])
        environment_id = _profile_id(profile, "userdata/environment-id", _RECORD_ID)
        config, claude = _t3_provider(profile)
        _t3_state(profile, project_id, cwd)
        return _seal({**plan, "profile_id": environment_id, "locator": _t3_locator(profile),
                      "version": T3_VERSION, "project_id": project_id, "settings": config,
                      "claude": claude, "scope": "all_eligible_recent_project_sessions",
                      "may_include_other_engines": True})
    except SessionImportError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError, sqlite3.Error):
        raise SessionImportError("t3code_import_preflight_unverified") from None


def _check_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA or plan.get("status") != "ready":
        raise SessionImportError("import_plan_invalid")
    common = {"schema_version", "status", "target", "executable", "runtime", "profile", "profile_identity",
              "cwd", "cwd_identity", "profile_id", "locator", "version", "scope", "settings", "claude",
              "plan_sha256"}
    target_fields = {"paseo": {"bundled", "provider", "native_id", "native_transcript"},
                     "t3code": {"project_id", "may_include_other_engines"}}
    target = plan.get("target")
    if not isinstance(target, str) or target not in target_fields or set(plan) != common | target_fields[target]:
        raise SessionImportError("import_plan_invalid")
    try:
        if plan["plan_sha256"] != _sha256(plan):
            raise SessionImportError("import_plan_digest_mismatch")
    except SessionImportError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise SessionImportError("import_plan_invalid") from None


def _recheck(plan):
    shared = {key: plan[key] for key in ("executable", "profile", "cwd")}
    if plan["target"] == "paseo":
        try:
            transcript = plan["native_transcript"]["path"]
        except (TypeError, KeyError):
            raise SessionImportError("import_plan_invalid") from None
        current = prepare_paseo_import(**shared, native_id=plan["native_id"], provider=plan["provider"],
                                       native_transcript=transcript)
    else:
        current = prepare_t3code_import(**shared, project_id=plan["project_id"])
    if current != plan:
        raise SessionImportError("import_preflight_changed")


def _paseo_state(plan):
    adapter = PaseoAdapter(profile_root=plan["profile"])
    snapshot = adapter.snapshot_references()
    if any(error.blocks_inventory for error in snapshot.errors):
        raise SessionImportError("paseo_import_registry_unverified")
    matches = []
    for row in adapter.observations:
        if row["provider"] != plan["provider"]:
            continue
        ids = {ref["value"] for ref in row["references"] if ref["kind"] == "id"}
        if row["opaque_session_handle_present"] or (not ids and row["opaque_native_handle_present"]):
            raise SessionImportError("paseo_import_registry_handle_unverified")
        if plan["native_id"] not in ids:
            continue
        if ids != {plan["native_id"]} or not _same_directory(row["cwd"], plan["cwd"]):
            raise SessionImportError("paseo_import_registry_handle_conflict")
        try:
            if str(UUID(row["id"])) != row["id"]:
                raise ValueError
        except ValueError:
            raise SessionImportError("paseo_import_agent_id_unverified") from None
        matches.append({"agent_id": row["id"], "native_id": plan["native_id"],
                        "provider": plan["provider"], "archived": row["archivedAt"] is not None})
    if len(matches) > 1:
        raise SessionImportError("paseo_import_registry_handle_conflict")
    return matches


def _result(plan, status, **details):
    scope = "project_projection" if plan["target"] == "t3code" else "exact_registry_mapping"
    return {"schema_version": "larj.claude-session-import-result.v1", "status": status,
            "target": plan["target"], "plan_sha256": plan["plan_sha256"],
            "verification_scope": scope, **details}


def _observe(plan):
    profile, cwd = _local_path(plan["profile"]), _local_path(plan["cwd"])
    if _directory(profile) != plan["profile_identity"] or _directory(cwd) != plan["cwd_identity"]:
        raise SessionImportError("import_target_identity_changed")
    if plan["target"] == "paseo":
        profile_id = _profile_id(profile, "server-id", _SERVER_ID)
        if profile_id != plan["profile_id"]:
            raise SessionImportError("import_target_identity_changed")
        proof, claude = _paseo_provider(profile, plan["provider"])
        if proof != plan["settings"] or claude != plan["claude"]:
            raise SessionImportError("import_target_provider_changed")
        return _paseo_state(plan)
    profile_id = _profile_id(profile, "userdata/environment-id", _RECORD_ID)
    if profile_id != plan["profile_id"]:
        raise SessionImportError("import_target_identity_changed")
    proof, claude = _t3_provider(profile)
    if proof != plan["settings"] or claude != plan["claude"]:
        raise SessionImportError("import_target_provider_changed")
    return _t3_state(profile, plan["project_id"], cwd)


def verify_session_import(plan):
    """Observe matching registry/projection metadata without contacting the app."""
    _check_plan(plan)
    try:
        mappings = _observe(plan)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError, sqlite3.Error):
        return _result(plan, "unknown", code="import_verification_unavailable")
    if plan["target"] == "paseo" and mappings and mappings[0]["archived"]:
        return _result(plan, "archived", mappings=mappings)
    return _result(plan, "observed" if mappings else "not_observed", mappings=mappings)


def _subprocess(runner, argv, plan, *, bundled=False, stdin=None, timeout=20):
    env = {key: val for key, val in os.environ.items()
           if not key.upper().startswith(("NODE_", "ELECTRON_", "PASEO_", "T3CODE_"))}
    if bundled:
        env["ELECTRON_RUN_AS_NODE"] = "1"
    if plan["target"] == "paseo":
        env["PASEO_NODE_ENV"] = "production"
    else:
        env["T3CODE_HOME"] = plan["profile"]
    try:
        # Keep raw product output out of logs and bound the bytes loaded.
        # A child writes to a temporary file so large replies cannot exhaust
        # Python's memory before the length check.
        with tempfile.TemporaryFile() as output:
            result = runner([str(arg) for arg in argv], cwd=plan["profile"], env=env, input=stdin,
                            stdout=output, stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                raise SessionImportError("import_product_request_failed")
            output.seek(0)
            raw = output.read(_MAX_JSON + 1)
        if len(raw) > _MAX_JSON:
            raise SessionImportError("import_product_reply_budget_exceeded")
        return raw
    except (OSError, subprocess.SubprocessError):
        raise SessionImportError("import_product_request_unavailable") from None


def _paseo_argv(plan):
    executable = Path(plan["executable"])
    if not plan["bundled"]:
        return [executable]
    return [executable, "--disable-warning=DEP0040", executable.parent / _PASEO_RUNNER,
            "node-script", str(executable.parent / _PASEO_ARCHIVE) + "/node_modules/@getpaseo/cli/dist/index.js"]


def _run_paseo(plan, runner):
    argv = _paseo_argv(plan)
    version = _subprocess(runner, [*argv, "--version"], plan, bundled=plan["bundled"])
    if version.strip() != PASEO_VERSION.encode("ascii"):
        raise SessionImportError("paseo_import_version_unverified")
    before = _paseo_state(plan)
    if before:
        if before[0]["archived"]:
            raise SessionImportError("paseo_import_archived_restore_requires_separate_scope")
        return _result(plan, "already_imported", mappings=before)
    if _paseo_locator(Path(plan["profile"]), plan["profile_id"]) != plan["locator"]:
        raise SessionImportError("import_preflight_changed")
    try:
        raw = _subprocess(runner, [*argv, "import", plan["native_id"], "--provider", plan["provider"],
                                    "--cwd", plan["cwd"], "--home", plan["profile"], "--json"],
                          plan, bundled=plan["bundled"], timeout=60)
        reply = _decode(raw)
        after = _observe(plan)
        if (not isinstance(reply, dict) or not after or after[0]["archived"]
                or after[0]["agent_id"] != reply.get("agentId") or reply.get("provider") != plan["provider"]
                or not _same_directory(reply.get("cwd"), plan["cwd"])):
            raise SessionImportError("paseo_import_reply_unverified")
    except (OSError, ValueError, KeyError, RuntimeError):
        observed = verify_session_import(plan)
        return _result(plan, "unknown", code="paseo_import_outcome_unverified",
                       mappings=observed.get("mappings", []))
    return _result(plan, "imported", mappings=after)


def _t3_cli(plan, runner, *args):
    executable = Path(plan["executable"])
    entry = str(executable.parent / _T3_ARCHIVE) + "/apps/server/dist/bin.mjs"
    return _subprocess(runner, [executable, entry, *args, "--base-dir", plan["profile"]], plan, bundled=True)


def _t3_call(plan, runner, token, tag, payload):
    request = {"origin": plan["locator"]["origin"], "token": token, "tag": tag, "payload": payload}
    helper = Path(__file__).with_name("t3code_session_import_rpc.cjs")
    return _decode(_subprocess(runner, [plan["executable"], helper], plan, bundled=True,
                              stdin=json.dumps(request, separators=(",", ":")).encode("utf-8"), timeout=34))


def _t3_candidate(scan, plan):
    if (not isinstance(scan, dict) or scan.get("truncated", False) is not False
            or not isinstance(scan.get("candidates"), list)):
        raise SessionImportError("t3code_import_scan_incomplete")
    matches = []
    for row in scan["candidates"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise SessionImportError("t3code_import_scan_unverified")
        if _same_directory(row["path"], plan["cwd"]):
            matches.append(row)
    if (len(matches) != 1 or matches[0].get("projectId", plan["project_id"]) != plan["project_id"]
            or type(matches[0].get("threadCount")) is not int or matches[0]["threadCount"] < 1
            or not isinstance(matches[0].get("sources"), list) or "claudeAgent" not in matches[0]["sources"]):
        raise SessionImportError("t3code_import_project_candidates_unverified")


def _run_t3(plan, runner):
    try:
        session = _decode(_t3_cli(plan, runner, "auth", "session", "issue", "--ttl", "2m",
                                  "--label", "janitor session import", "--scope", "orchestration:read",
                                  "--scope", "orchestration:operate", "--json"))
    except (OSError, ValueError, RuntimeError):
        session = None
    if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str) or not _RECORD_ID.fullmatch(session["sessionId"]):
        return _result(plan, "unknown", code="t3code_import_session_issuance_unverified",
                       requested_session_ttl_seconds=120)
    result = None
    try:
        token = session.get("token")
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise SessionImportError("t3code_import_temporary_session_unverified")
        if _t3_locator(Path(plan["profile"])) != plan["locator"]:
            raise SessionImportError("import_preflight_changed")
        _t3_candidate(_t3_call(plan, runner, token, "agentSessions.scan", {}), plan)
        if _t3_locator(Path(plan["profile"])) != plan["locator"]:
            raise SessionImportError("import_preflight_changed")
        before = _observe(plan)
        try:
            reply = _t3_call(plan, runner, token, "agentSessions.import",
                             {"projectId": plan["project_id"], "expectedWorkspaceRoot": plan["cwd"]})
            if not isinstance(reply, dict) or any(type(reply.get(key)) is not int or reply[key] < 0
                                                  for key in ("importedCount", "skippedCount")):
                raise SessionImportError("t3code_import_reply_unverified")
            after = _observe(plan)
            if not all(row in after for row in before):
                raise SessionImportError("t3code_import_projection_changed")
            new = [row for row in after if row not in before]
            if len(new) > reply["importedCount"] or (reply["importedCount"] and not after):
                raise SessionImportError("t3code_import_projection_unverified")
            if reply["skippedCount"]:
                status = "partial"
            elif new:
                status = "imported"
            elif after:
                status = "observed"
            else:
                status = "not_observed"
            result = _result(plan, status, imported_count=reply["importedCount"], skipped_count=reply["skippedCount"],
                             new_mappings=new, mappings=after)
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError, sqlite3.Error):
            observed = verify_session_import(plan)
            result = _result(plan, "unknown", code="t3code_import_outcome_unverified",
                             mappings=observed.get("mappings", []))
    finally:
        try:
            _t3_cli(plan, runner, "auth", "session", "revoke", session["sessionId"])
        except SessionImportError:
            if result is None:
                raise SessionImportError("t3code_import_preflight_and_session_revoke_unverified") from None
            result["warnings"] = ["t3code_import_temporary_session_revoke_unverified"]
    return result


def run_session_import(plan, *, approved_sha256, accept_project_history=False, runner=None):
    """Apply the exact reviewed scope once through the local product interface."""
    _check_plan(plan)
    if approved_sha256 != plan["plan_sha256"]:
        raise SessionImportError("import_plan_approval_required")
    if plan["target"] == "t3code" and accept_project_history is not True:
        raise SessionImportError("t3code_import_project_history_approval_required")
    _recheck(plan)
    execute = runner or subprocess.run
    try:
        return _run_paseo(plan, execute) if plan["target"] == "paseo" else _run_t3(plan, execute)
    except SessionImportError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, RuntimeError, sqlite3.Error):
        raise SessionImportError("import_pre_dispatch_unverified") from None
