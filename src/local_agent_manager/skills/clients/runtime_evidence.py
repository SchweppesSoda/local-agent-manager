"""Adapter-bound evidence for the Python used inside a native client context.

The probe is intentionally separate from consumer detection.  A shared mount
root or a generic manager shell is not evidence that a particular client uses
the approved business Python.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
from typing import Any, Mapping, Sequence

from .model import ClientModel, PythonRuntimeVerification


EVIDENCE_SCHEMA_VERSION = 1
_PRIVATE_RUNTIME_PARTS = (
    ".workbuddy",
    "workbuddy",
    ".cache/codex-runtimes",
    "codex-runtimes",
    ".codex",
    "cindyglobal/codex",
    "codex-home",
    ".work-skills-runtime",
)
DEFAULT_MAX_EVIDENCE_AGE = timedelta(hours=24)


class RuntimeEvidenceError(ValueError):
    """Raised when runtime evidence cannot be captured safely."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("Invalid Python runtime evidence:\n- " + "\n- ".join(self.errors))


def _path_key(value: str | os.PathLike[str]) -> str:
    text = os.fspath(value).strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/").casefold()


def _is_absolute_path(value: str) -> bool:
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()


def _private_runtime_part(value: str) -> str | None:
    padded = f"/{_path_key(value).strip('/')}/"
    for part in _PRIVATE_RUNTIME_PARTS:
        if f"/{part.casefold().strip('/')}/" in padded:
            return part
    return None


def _policy_fingerprint(
    adapter_id: str,
    consumer_ids: Sequence[str],
    policy: PythonRuntimeVerification,
) -> str:
    payload = {
        "adapterId": adapter_id,
        "consumerIds": list(consumer_ids),
        "contextId": policy.context_id,
        "contextEnvironment": policy.context_environment,
        "expectedContext": _path_key(policy.expected_context),
        "allowUnsetContext": policy.allow_unset_context,
        "pythonCommand": policy.python_command,
        "requiredVersion": policy.required_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _adapter_policy(
    model: ClientModel, adapter_id: str
) -> tuple[tuple[str, ...], PythonRuntimeVerification]:
    adapter = model.adapters.get(adapter_id)
    if adapter is None:
        raise RuntimeEvidenceError([f"unknown adapter: {adapter_id}"])
    policy = adapter.python_runtime_verification
    if policy is None:
        raise RuntimeEvidenceError(
            [f"adapter {adapter_id} does not declare pythonRuntimeVerification"]
        )
    return adapter.consumer_ids, policy


def capture_python_runtime(
    adapter_id: str,
    consumer_ids: Sequence[str],
    policy: PythonRuntimeVerification,
    *,
    environ: Mapping[str, str] | None = None,
    executable: str | os.PathLike[str] | None = None,
    version_info: Sequence[int] | None = None,
    prefix: str | os.PathLike[str] | None = None,
    base_prefix: str | os.PathLike[str] | None = None,
    resolved_python: str | os.PathLike[str] | None = None,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Capture evidence from the Python process running in one client context.

    Callers must execute this inside the named client.  The context marker and
    policy fingerprint prevent accidentally applying its result to another
    adapter, while executable/path checks reject manager and private runtimes.
    """

    env = dict(os.environ if environ is None else environ)
    actual_executable = os.fspath(sys.executable if executable is None else executable)
    actual_prefix = os.fspath(sys.prefix if prefix is None else prefix)
    actual_base_prefix = os.fspath(sys.base_prefix if base_prefix is None else base_prefix)
    actual_version = tuple(sys.version_info[:3] if version_info is None else version_info[:3])
    errors: list[str] = []

    if len(actual_version) != 3 or any(
        not isinstance(part, int) or isinstance(part, bool) for part in actual_version
    ):
        errors.append("version_info must contain three integers")

    raw_context = env.get(policy.context_environment)
    context_was_set = isinstance(raw_context, str) and bool(raw_context.strip())
    if not context_was_set and not policy.allow_unset_context:
        errors.append(
            f"{policy.context_environment} must be set by the {policy.context_id} client context"
        )
    effective_context = (
        raw_context.strip() if context_was_set else os.fspath(policy.expected_context)
    )
    if _path_key(effective_context) != _path_key(policy.expected_context):
        errors.append(
            f"{policy.context_environment} identifies a different client context: "
            f"{effective_context}"
        )

    resolved_by = "supplied"
    if resolved_python is None:
        override = env.get("WORK_SKILLS_SYSTEM_PYTHON", "").strip()
        if override:
            resolved_python = os.path.expandvars(os.path.expanduser(override))
            resolved_by = "WORK_SKILLS_SYSTEM_PYTHON"
        else:
            resolved_python = shutil.which(policy.python_command, path=env.get("PATH"))
            resolved_by = "PATH"
    if not resolved_python:
        errors.append(f"unable to resolve {policy.python_command!r} from the client context")
        resolved_text = ""
    else:
        resolved_text = os.fspath(resolved_python)
        if not _is_absolute_path(resolved_text):
            errors.append(f"resolved Python path is not absolute: {resolved_text}")
        if _path_key(resolved_text) != _path_key(actual_executable):
            errors.append(
                "the running interpreter does not match the client context's resolved Python: "
                f"running={actual_executable}, resolved={resolved_text}"
            )

    for label, value in (
        ("executable", actual_executable),
        ("prefix", actual_prefix),
        ("basePrefix", actual_base_prefix),
        ("resolvedExecutable", resolved_text),
    ):
        if not value or not _is_absolute_path(value):
            errors.append(f"Python {label} is not an absolute path: {value}")
        private_part = _private_runtime_part(value)
        if private_part:
            errors.append(f"Python {label} is inside forbidden private runtime {private_part}: {value}")

    if _path_key(actual_prefix) != _path_key(actual_base_prefix):
        errors.append(
            f"virtual environments are not allowed: prefix={actual_prefix}, "
            f"basePrefix={actual_base_prefix}"
        )

    if len(actual_version) == 3:
        actual_series = f"{actual_version[0]}.{actual_version[1]}"
        if actual_series != policy.required_version:
            errors.append(
                f"Python {policy.required_version}.x is required; observed "
                f"{actual_version[0]}.{actual_version[1]}.{actual_version[2]}"
            )
    if errors:
        raise RuntimeEvidenceError(errors)

    moment = captured_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    version_text = ".".join(str(part) for part in actual_version)
    evidence: dict[str, Any] = {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "method": "client-native-python-probe",
        "adapterId": adapter_id,
        "consumerIds": list(consumer_ids),
        "contextId": policy.context_id,
        "policyFingerprint": _policy_fingerprint(adapter_id, consumer_ids, policy),
        "context": {
            "environmentName": policy.context_environment,
            "effectiveValue": effective_context,
            "wasSet": context_was_set,
        },
        "python": {
            "executable": actual_executable,
            "resolvedExecutable": resolved_text,
            "resolvedBy": resolved_by,
            "version": version_text,
            "versionInfo": list(actual_version),
            "prefix": actual_prefix,
            "basePrefix": actual_base_prefix,
            "isVirtualEnvironment": False,
        },
        "capturedAt": moment.isoformat().replace("+00:00", "Z"),
    }
    validation_errors = validate_python_runtime_evidence_for_policy(
        adapter_id, consumer_ids, policy, evidence
    )
    if validation_errors:
        raise RuntimeEvidenceError(validation_errors)
    return evidence


def capture_python_runtime_evidence(
    model: ClientModel,
    adapter_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Capture evidence using one adapter's validated client-model policy."""

    consumer_ids, policy = _adapter_policy(model, adapter_id)
    return capture_python_runtime(adapter_id, consumer_ids, policy, **kwargs)


def validate_python_runtime_evidence_for_policy(
    adapter_id: str,
    consumer_ids: Sequence[str],
    policy: PythonRuntimeVerification,
    evidence: Mapping[str, Any],
) -> tuple[str, ...]:
    """Validate evidence without consulting or mutating machine state."""

    errors: list[str] = []
    if evidence.get("schemaVersion") != EVIDENCE_SCHEMA_VERSION:
        errors.append(f"schemaVersion must be {EVIDENCE_SCHEMA_VERSION}")
    if evidence.get("method") != "client-native-python-probe":
        errors.append("method must be client-native-python-probe")
    if evidence.get("adapterId") != adapter_id:
        errors.append(
            f"evidence belongs to adapter {evidence.get('adapterId')!r}, not {adapter_id!r}"
        )
    if evidence.get("consumerIds") != list(consumer_ids):
        errors.append("evidence consumerIds do not exactly match this adapter")
    if evidence.get("contextId") != policy.context_id:
        errors.append(
            f"evidence contextId does not match {policy.context_id!r}; cross-client evidence is invalid"
        )
    expected_fingerprint = _policy_fingerprint(adapter_id, consumer_ids, policy)
    if evidence.get("policyFingerprint") != expected_fingerprint:
        errors.append("evidence policyFingerprint does not match the current adapter policy")

    context = evidence.get("context")
    if not isinstance(context, Mapping):
        errors.append("context must be an object")
    else:
        if context.get("environmentName") != policy.context_environment:
            errors.append("context environmentName does not match the adapter policy")
        effective_value = context.get("effectiveValue")
        if not isinstance(effective_value, str) or _path_key(effective_value) != _path_key(
            policy.expected_context
        ):
            errors.append("context effectiveValue does not identify this adapter's client context")
        was_set = context.get("wasSet")
        if not isinstance(was_set, bool):
            errors.append("context wasSet must be boolean")
        elif not was_set and not policy.allow_unset_context:
            errors.append(
                f"context did not supply required environment marker {policy.context_environment}"
            )

    python = evidence.get("python")
    if not isinstance(python, Mapping):
        errors.append("python must be an object")
    else:
        values: dict[str, str] = {}
        for field in ("executable", "resolvedExecutable", "prefix", "basePrefix"):
            value = python.get(field)
            if not isinstance(value, str) or not value or not _is_absolute_path(value):
                errors.append(f"python.{field} must be an absolute path")
                value = ""
            values[field] = value
            private_part = _private_runtime_part(value)
            if private_part:
                errors.append(
                    f"python.{field} is inside forbidden private runtime {private_part}"
                )
        if values["executable"] and values["resolvedExecutable"] and _path_key(
            values["executable"]
        ) != _path_key(values["resolvedExecutable"]):
            errors.append("python.executable does not match python.resolvedExecutable")
        if values["prefix"] and values["basePrefix"] and _path_key(
            values["prefix"]
        ) != _path_key(values["basePrefix"]):
            errors.append("python prefix/basePrefix indicate a virtual environment")
        if python.get("isVirtualEnvironment") is not False:
            errors.append("python.isVirtualEnvironment must be false")

        version_info = python.get("versionInfo")
        if not isinstance(version_info, list) or len(version_info) != 3 or any(
            not isinstance(part, int) or isinstance(part, bool) for part in version_info
        ):
            errors.append("python.versionInfo must contain three integers")
        else:
            series = f"{version_info[0]}.{version_info[1]}"
            if series != policy.required_version:
                errors.append(
                    f"python.versionInfo must be {policy.required_version}.x, observed "
                    + ".".join(str(part) for part in version_info)
                )
            if python.get("version") != ".".join(str(part) for part in version_info):
                errors.append("python.version does not match python.versionInfo")

    captured_at = evidence.get("capturedAt")
    if not isinstance(captured_at, str):
        errors.append("capturedAt must be an ISO-8601 timestamp")
    else:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
        except ValueError:
            errors.append("capturedAt must be a timezone-aware ISO-8601 timestamp")
    return tuple(errors)


def validate_python_runtime_evidence(
    model: ClientModel,
    adapter_id: str,
    evidence: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return validation errors for one adapter; an empty tuple is valid."""

    try:
        consumer_ids, policy = _adapter_policy(model, adapter_id)
    except RuntimeEvidenceError as exc:
        return exc.errors
    if not isinstance(evidence, Mapping):
        return ("runtime evidence must be a JSON object",)
    return validate_python_runtime_evidence_for_policy(
        adapter_id, consumer_ids, policy, evidence
    )


def validate_python_runtime_evidence_freshness(
    evidence: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_EVIDENCE_AGE,
) -> tuple[str, ...]:
    """Validate capture recency only at ``clients verify`` time.

    Stored manifest evidence must continue to use structural and policy
    validation without this gate; otherwise a healthy client would turn red
    merely because the calendar advanced.
    """

    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    captured_at = evidence.get("capturedAt") if isinstance(evidence, Mapping) else None
    if not isinstance(captured_at, str):
        return ("capturedAt must be an ISO-8601 timestamp",)
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            raise ValueError("timezone required")
        captured = captured.astimezone(timezone.utc)
    except ValueError:
        return ("capturedAt must be a timezone-aware ISO-8601 timestamp",)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    if captured > current:
        return ("runtime evidence capturedAt is in the future",)
    age = current - captured
    if age > max_age:
        return (
            f"runtime evidence is stale ({age.total_seconds():.0f}s old; "
            f"maximum {max_age.total_seconds():.0f}s)",
        )
    return ()


def load_python_runtime_evidence(path: str | os.PathLike[str]) -> Mapping[str, Any]:
    """Load one evidence JSON file without interpreting it for an adapter."""

    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise RuntimeEvidenceError(["runtime evidence file must contain a JSON object"])
    return value
