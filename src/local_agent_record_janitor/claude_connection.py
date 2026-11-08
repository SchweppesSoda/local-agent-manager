"""Prepare and apply one explicit Claude provider configuration."""
from __future__ import annotations

import codecs
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re

from . import frozen_files
from .environment_config import MAX_CONFIG_BYTES
from .environment_paths import BoundedPathProbe, local_path
from .operation_store import plan_sha256

PLAN_SCHEMA = "larj.claude-connection-plan.v1"
RESULT_SCHEMA = "larj.claude-connection-result.v1"
_PASEO_PROVIDER = re.compile(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*")
_LOCK_NAME = ".larj-claude-connection.lock"
_SHA256 = re.compile(r"[a-f0-9]{64}")
_T3_SERVER_FIELDS = frozenset((
    "defaultRuntimeMode", "enableProviderUpdateChecks", "projectSettingsOverrides", "worktreesDirectory",
))


class ClaudeConnectionError(ValueError):
    """A connection error that keeps configuration values out of diagnostics."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _absolute(value: Path | str, field: str) -> Path:
    candidate = local_path(value) if isinstance(value, (str, Path)) else None
    if candidate is None:
        raise ClaudeConnectionError("invalid_path", f"{field} must be an absolute local path.")
    return candidate


def _provider(target: str, supplied: str) -> str:
    if target == "t3code":
        if supplied not in ("claude-cindy", "claudeAgent"):
            raise ClaudeConnectionError("invalid_provider", "T3 uses the default claudeAgent instance.")
        return "claudeAgent"
    if target == "paseo":
        if not isinstance(supplied, str) or len(supplied) > 64 or not _PASEO_PROVIDER.fullmatch(supplied):
            raise ClaudeConnectionError("invalid_provider", "Use a Paseo provider ID such as claude-cindy.")
        return supplied
    raise ClaudeConnectionError("unknown_target", "Select Paseo or T3 Code as the configuration target.")


def _object_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _number(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def _parse(raw: bytes) -> dict:
    try:
        data = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_object_pairs,
            parse_float=_number, parse_constant=_number,
        )
    except (ValueError, RecursionError):
        raise ClaudeConnectionError("invalid_config", "Configuration must be valid, unambiguous UTF-8 JSON.") from None
    if not isinstance(data, dict):
        raise ClaudeConnectionError("unknown_config_schema", "Configuration must contain a JSON object.")
    return data


def _object(parent: dict, key: str) -> dict:
    child = parent.setdefault(key, {})
    if not isinstance(child, dict):
        raise ClaudeConnectionError("unknown_config_schema", f"Expected an object at {key}.")
    return child


def _field_state(existing: str | None, requested: str) -> str:
    if existing is None or existing == "":
        return "unset"
    return "same" if existing == requested else "different"


def _set_string(parent: dict, key: str, value: str) -> str:
    previous = parent.get(key)
    if key in parent and not isinstance(previous, str):
        raise ClaudeConnectionError("unknown_config_schema", f"Expected a string at {key}.")
    parent[key] = value
    return _field_state(previous, value)


def _enabled(parent: dict, *, default: bool = True) -> bool:
    enabled = parent.get("enabled", default)
    if type(enabled) is not bool:
        raise ClaudeConnectionError("unknown_config_schema", "Expected a boolean provider enable setting.")
    return enabled


def _patch_paseo(document: dict, provider: str, binary: str, home: str) -> dict:
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ClaudeConnectionError("unknown_config_schema", "Paseo configuration must use version 1.")
    providers = _object(_object(document, "agents"), "providers")
    created = provider not in providers
    if created:
        providers[provider] = {"extends": "claude", "label": "Claude (Cindy)"}
    slot = providers[provider]
    if not isinstance(slot, dict) or slot.get("extends") != "claude":
        raise ClaudeConnectionError("provider_conflict", "The selected Paseo slot does not extend Claude.")
    label = slot.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ClaudeConnectionError("unknown_config_schema", "The Paseo Claude provider needs a label.")
    command = slot.get("command")
    if "command" in slot and (
        not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command)
    ):
        raise ClaudeConnectionError("unknown_config_schema", "Expected a nonempty command array in the Paseo provider.")
    previous = command[0] if command else None
    args = command[1:] if command else []
    slot["command"] = [binary, *args]
    env = _object(slot, "env")
    if os.name == "nt" and any(
        key.upper() == "CLAUDE_CONFIG_DIR" and key != "CLAUDE_CONFIG_DIR" for key in env
    ):
        raise ClaudeConnectionError("provider_conflict", "Paseo has another spelling of CLAUDE_CONFIG_DIR.")
    return {
        "provider_created": created,
        "provider_enabled": _enabled(slot),
        "command_executable": _field_state(previous, binary),
        "retained_argument_count": len(args),
        "CLAUDE_CONFIG_DIR": _set_string(env, "CLAUDE_CONFIG_DIR", home),
    }


def _patch_t3(document: dict, provider: str, binary: str, home: str) -> dict:
    # Current ServerSettings decodes {} and defaults providerInstances to {}.
    # For a nonempty file without that map, require an observed server key.
    if ("version" in document or "providers" in document
            or ("providerInstances" not in document and document
                and not _T3_SERVER_FIELDS.intersection(document))):
        raise ClaudeConnectionError("unknown_config_schema", "T3 Code configuration must use providerInstances.")
    providers = _object(document, "providerInstances")
    created = provider not in providers
    if created:
        providers[provider] = {"driver": "claudeAgent", "config": {}}
    slot = providers[provider]
    if not isinstance(slot, dict) or slot.get("driver") != "claudeAgent":
        raise ClaudeConnectionError("provider_conflict", "The T3 claudeAgent slot belongs to another provider.")
    config = _object(slot, "config")
    slot_enabled = _enabled(slot)
    config_enabled = _enabled(config)
    return {
        "provider_created": created,
        "provider_enabled": slot_enabled and config_enabled,
        "binaryPath": _set_string(config, "binaryPath", binary),
        "homePath": _set_string(config, "homePath", home),
    }


def _render(raw: bytes, target: str, provider: str, binary: str, home: str) -> tuple[bytes, dict]:
    if len(raw) > MAX_CONFIG_BYTES:
        raise ClaudeConnectionError("config_too_large", "Configuration exceeds the supported size.")
    document = _parse(raw)
    if target == "paseo":
        review = _patch_paseo(document, provider, binary, home)
        unchanged = review["command_executable"] == review["CLAUDE_CONFIG_DIR"] == "same"
    else:
        review = _patch_t3(document, provider, binary, home)
        unchanged = review["binaryPath"] == review["homePath"] == "same"
    if unchanged and not review["provider_created"]:
        return raw, review

    newline = "\r\n" if b"\r\n" in raw else "\n"
    try:
        result = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        if newline == "\r\n":
            result = result.replace("\n", "\r\n")
        encoding = "utf-8-sig" if raw.startswith(codecs.BOM_UTF8) else "utf-8"
        rendered = (result + newline).encode(encoding)
    except (ValueError, RecursionError):
        raise ClaudeConnectionError("invalid_config", "Configuration could not be serialized as UTF-8 JSON.") from None
    if len(rendered) > MAX_CONFIG_BYTES:
        raise ClaudeConnectionError("config_too_large", "Updated configuration exceeds the supported size.")
    return rendered, review


def _require_present(probe: BoundedPathProbe, path: Path, field: str, *, directory: bool) -> os.stat_result:
    status, info = probe.inspect(path, "claude_connection", directory=directory)
    if status != "present" or info is None or not info.st_ino:
        raise ClaudeConnectionError(
            f"{field}_unavailable", f"{field} must be an existing, inspectable local {'directory' if directory else 'file'}.",
        )
    return info


def _configuration_path(target: str, value: Path | str) -> Path:
    config = _absolute(value, "Configuration path")
    if target == "paseo" and config.name != "config.json":
        raise ClaudeConnectionError("wrong_config_path", "Select the Paseo profile's config.json.")
    if target == "t3code" and (config.name, config.parent.name) != ("settings.json", "userdata"):
        raise ClaudeConnectionError("wrong_config_path", "Select the T3 profile's userdata/settings.json.")
    return config


@contextmanager
def _config_lock(root: Path):
    """Exclude cooperating Janitor writes to this configuration directory."""
    with ExitStack() as stack:
        try:
            path = stack.enter_context(frozen_files._parent_fence(root, _LOCK_NAME))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            raise ClaudeConnectionError(
                "config_locked", "A connection lock exists. Inspect its owner before doing anything with it.",
            ) from None
        except (OSError, ValueError, RuntimeError):
            raise ClaudeConnectionError("config_lock_unavailable", "Could not acquire the configuration lock.") from None
        opened = None
        try:
            opened = os.fstat(fd)
            yield
        finally:
            try:
                try:
                    evidence, _ = frozen_files.read_file(root, _LOCK_NAME, limit=0)
                    if (opened is None or not opened.st_ino
                            or evidence["identity"] != [opened.st_dev, opened.st_ino]
                            or evidence["mtime_ns"] != opened.st_mtime_ns):
                        raise ClaudeConnectionError(
                            "config_lock_cleanup_unverified", "The configuration lock identity changed.",
                        )
                finally:
                    os.close(fd)
                frozen_files._delete_frozen(root, evidence)
            except (OSError, ValueError, RuntimeError):
                raise ClaudeConnectionError(
                    "config_lock_cleanup_unverified",
                    "Could not verify release of the configuration lock. Inspect before retrying.",
                ) from None


def _prepare_config_file(temporary: Path, destination: Path) -> None:
    """Apply Windows destination access controls before staging config bytes."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    get_security = security.GetNamedSecurityInfoW
    get_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             *(ctypes.POINTER(pointer),) * 5)
    get_security.restype = wintypes.DWORD
    set_security = security.SetNamedSecurityInfoW
    set_security.argtypes = (wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                             pointer, pointer, pointer, pointer)
    set_security.restype = wintypes.DWORD
    get_control = security.GetSecurityDescriptorControl
    get_control.argtypes = (pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD))
    get_control.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (pointer,)
    kernel.LocalFree.restype = pointer

    file_object = 1
    owner_group_dacl = 0x1 | 0x2 | 0x4
    owner, group, dacl, descriptor = (pointer() for _ in range(4))
    error = get_security(str(destination), file_object, owner_group_dacl, ctypes.byref(owner),
                         ctypes.byref(group), ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not owner or not group or not dacl:
            raise OSError("Could not qualify Windows configuration access controls")
        protected_dacl = 0x1000
        protection = 0x80000000 if control.value & protected_dacl else 0x20000000
        error = set_security(str(temporary), file_object, owner_group_dacl | protection,
                             owner, group, dacl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


def _replace_config_file(temporary: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(temporary, destination)
        return
    import ctypes
    from ctypes import wintypes

    replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    replace.restype = wintypes.BOOL
    # No ignore-ACL-errors flag or fallback that would weaken the DACL.
    if not replace(str(destination), str(temporary), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())

def _prepare(
    *, target: str, config_path: Path | str, claude_binary: Path | str,
    claude_home: Path | str, provider_id: str,
) -> tuple[dict, Callable[[bytes], bytes]]:
    provider = _provider(target, provider_id)
    config = _configuration_path(target, config_path)
    binary = _absolute(claude_binary, "Claude executable")
    home = _absolute(claude_home, "Claude home")
    if binary.name.casefold() not in ("claude", "claude.exe"):
        raise ClaudeConnectionError("wrong_binary_path", "Select a Claude executable named claude or claude.exe.")

    probe = BoundedPathProbe()
    config_info = _require_present(probe, config, "config", directory=False)
    binary_info = _require_present(probe, binary, "claude_binary", directory=False)
    home_info = _require_present(probe, home, "claude_home", directory=True)
    if config_info.st_nlink != 1:
        raise ClaudeConnectionError("config_unsafe", "Configuration must be a regular file with one link.")
    if config_info.st_size > MAX_CONFIG_BYTES:
        raise ClaudeConnectionError("config_too_large", "Configuration exceeds the supported size.")
    try:
        before, raw = frozen_files.read_file(
            config.parent, config.name, content=True, limit=MAX_CONFIG_BYTES,
        )
    except (OSError, ValueError, RuntimeError):
        raise ClaudeConnectionError("config_unsafe", "Configuration could not be safely read.") from None
    if before["identity"] != [config_info.st_dev, config_info.st_ino] or raw is None:
        raise ClaudeConnectionError("config_unsafe", "Configuration identity changed during preparation.")
    after, field_changes = _render(raw, target, provider, str(binary), str(home))
    change = {
        "kind": "rewrite",
        "before": before,
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "after_size": len(after),
    }
    plan = {
        "schema_version": PLAN_SCHEMA,
        "target": target,
        "provider_id": provider,
        "config_path": str(config),
        "claude_binary": str(binary),
        "claude_home": str(home),
        "binary_evidence": {
            "identity": [binary_info.st_dev, binary_info.st_ino],
            "size": binary_info.st_size,
            "mtime_ns": binary_info.st_mtime_ns,
        },
        "home_identity": [home_info.st_dev, home_info.st_ino],
        "config_change": change,
        "field_changes": field_changes,
        "status": "already_configured" if before["sha256"] == change["after_sha256"] else "ready",
    }
    plan["plan_sha256"] = plan_sha256(plan)

    def transform(original: bytes) -> bytes:
        return _render(original, target, provider, str(binary), str(home))[0]

    return plan, transform


def prepare_connection(
    *, target: str, config_path: Path, claude_binary: Path, claude_home: Path,
    provider_id: str = "claude-cindy",
) -> dict:
    """Inspect an existing configuration and return a reviewable, redacted plan."""
    plan, _ = _prepare(target=target, config_path=config_path, claude_binary=claude_binary,
                       claude_home=claude_home, provider_id=provider_id)
    return plan


def apply_connection(plan: dict, *, approved_sha256: str, clients_closed: bool) -> dict:
    """Apply only the approved projection to the same unchanged configuration."""
    if clients_closed is not True:
        raise ClaudeConnectionError("clients_not_closed", "Confirm the destination client and its local server are closed.")
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        raise ClaudeConnectionError("invalid_plan", "Expected a Claude connection plan.")
    supplied = plan.get("plan_sha256")
    if not isinstance(approved_sha256, str) or not _SHA256.fullmatch(approved_sha256) or (
        not isinstance(supplied, str) or not _SHA256.fullmatch(supplied)
        or not hmac.compare_digest(supplied, approved_sha256)
    ):
        raise ClaudeConnectionError("approval_mismatch", "The approved hash does not match the plan.")
    try:
        digest = plan_sha256(plan)
    except (ValueError, TypeError, RecursionError):
        raise ClaudeConnectionError("invalid_plan", "The plan is not valid JSON.") from None
    if not hmac.compare_digest(digest, approved_sha256):
        raise ClaudeConnectionError("approval_mismatch", "The approved hash does not match the plan.")

    fields = ("target", "config_path", "claude_binary", "claude_home", "provider_id")
    if not all(isinstance(plan.get(key), str) for key in fields):
        raise ClaudeConnectionError("invalid_plan", "The plan is missing required path or target fields.")
    try:
        _provider(plan["target"], plan["provider_id"])
        config = _configuration_path(plan["target"], plan["config_path"])
        info = _require_present(BoundedPathProbe(), config, "config", directory=False)
        if info.st_nlink != 1:
            raise ClaudeConnectionError("config_unsafe", "Configuration must have one link.")
    except ClaudeConnectionError:
        raise ClaudeConnectionError("stale_plan", "Connection inputs changed. Prepare and review a fresh plan.") from None
    with _config_lock(config.parent):
        return _apply_locked(plan, fields)


def _apply_locked(plan: dict, fields: tuple[str, ...]) -> dict:
    try:
        fresh, transform = _prepare(**{key: plan[key] for key in fields})
    except ClaudeConnectionError:
        raise ClaudeConnectionError("stale_plan", "Connection inputs changed. Prepare and review a fresh plan.") from None
    if fresh != plan:
        raise ClaudeConnectionError("stale_plan", "Connection inputs changed. Prepare and review a fresh plan.")

    modified = fresh["status"] == "ready"
    if modified:
        config = Path(fresh["config_path"])
        try:
            frozen_files.apply_rewrite(
                config.parent, fresh["config_change"], transform,
                prepare_file=_prepare_config_file, replace_file=_replace_config_file,
            )
        except (OSError, ValueError, RuntimeError):
            raise ClaudeConnectionError(
                "config_write_unverified", "The configuration write could not be verified. Inspect before retrying.",
            ) from None
    return {
        "schema_version": RESULT_SCHEMA,
        "target": fresh["target"],
        "provider_id": fresh["provider_id"],
        "config_path": fresh["config_path"],
        "status": "applied" if modified else "already_configured",
        "modified": modified,
        "provider_enabled": fresh["field_changes"]["provider_enabled"],
        "plan_sha256": fresh["plan_sha256"],
    }
