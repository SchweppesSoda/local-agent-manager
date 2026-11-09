"""Strongly validated, side-effect-free v3 client model."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Mapping, Sequence


class ClientModelError(ValueError):
    """Raised when v3 client configuration is missing or inconsistent."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("Invalid client model:\n- " + "\n- ".join(self.errors))


@dataclass(frozen=True)
class MountPoint:
    id: str
    label: str
    root: Path
    delivery: str
    priority: int
    link_policy: Mapping[str, str]
    source_selection: str = "all"
    discovery_manifest: Path | None = None


@dataclass(frozen=True)
class Detector:
    mode: str
    commands: tuple[str, ...]
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class Verification:
    status: str
    instructions: str
    command: str | None = None


@dataclass(frozen=True)
class Consumer:
    id: str
    label: str
    status: str
    detector: Detector
    verification: Verification


@dataclass(frozen=True)
class DiscoveryRoot:
    mount_point_id: str
    priority: int
    role: str


@dataclass(frozen=True)
class Evidence:
    source_url: str | None
    source_level: str
    verified_version: str
    verified_on: str


@dataclass(frozen=True)
class PythonRuntimeVerification:
    status: str
    context_id: str
    capture_command: str
    python_command: str
    required_version: str
    context_environment: str
    expected_context: Path
    allow_unset_context: bool


@dataclass(frozen=True)
class RuntimeBinding:
    resolver: str
    skills_subdir: str
    binary_path: Path | None = None
    home_path: Path | None = None
    settings_path: Path | None = None
    instance_id: str = ""
    driver: str = ""
    frontend_path: Path | None = None


@dataclass(frozen=True)
class Adapter:
    id: str
    label: str
    consumer_ids: tuple[str, ...]
    mount_point_ids: tuple[str, ...]
    priority: int
    discovery_roots: tuple[DiscoveryRoot, ...]
    link_support_status: str
    link_support_evidence: Evidence
    consumer_detection: str
    native_verification: str
    management_status: str
    manage_by_default: bool
    create_when_absent: bool
    python_runtime_verification: PythonRuntimeVerification | None = None
    runtime_binding: RuntimeBinding | None = None


@dataclass(frozen=True)
class ClientModel:
    version: int
    mount_points: Mapping[str, MountPoint]
    consumers: Mapping[str, Consumer]
    adapters: Mapping[str, Adapter]

    @property
    def default_adapter_ids(self) -> tuple[str, ...]:
        return tuple(
            adapter.id
            for adapter in self.adapters.values()
            if adapter.manage_by_default and adapter.management_status == "confirmed"
        )


@dataclass(frozen=True)
class ConsumerDetection:
    consumer_id: str
    state: str
    command_hits: tuple[str, ...]
    path_hits: tuple[Path, ...]
    details: str


_ENV_PATTERN = re.compile(r"\$\{([^{}]+)\}")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _expand(text: str, environ: Mapping[str, str]) -> str:
    home = environ.get("HOME") or environ.get("USERPROFILE") or str(Path.home())
    # Older projections spelled the Windows roaming profile relative to HOME.
    # Prefer the machine's actual APPDATA location without requiring a write or
    # assuming that profile folders use the default layout.
    roaming_spelling = "${HOME}/AppData/Roaming"
    if environ.get("APPDATA") and (text == roaming_spelling or text.startswith(roaming_spelling + "/")):
        text = "${APPDATA}" + text[len(roaming_spelling):]
    values = {
        "HOME": home,
        "USERPROFILE": environ.get("USERPROFILE", home),
        "APPDATA": environ.get("APPDATA", str(Path(home) / "AppData" / "Roaming")),
        "CODEX_HOME": environ.get("CODEX_HOME", str(Path(home) / ".codex")),
        "WORKBUDDY_HOME": environ.get("WORKBUDDY_HOME", str(Path(home) / ".workbuddy")),
        "LOCALAPPDATA": environ.get("LOCALAPPDATA", str(Path(home) / "AppData" / "Local")),
        **environ,
    }
    result = text
    for _ in range(20):
        previous = result
        result = _ENV_PATTERN.sub(lambda match: values.get(match.group(1), match.group(0)), result)
        if result == previous:
            break
    if "${" in result:
        raise ValueError(f"unresolved environment expression: {result}")
    if result == "~" or result.startswith(("~/", "~\\")):
        result = home + result[1:]
    return os.path.expandvars(result)


def _mapping(value: Any, field: str, errors: list[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be a mapping")
        return {}
    return value


def _strings(value: Any, field: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        errors.append(f"{field} must be a non-empty list of strings")
        return ()
    return tuple(value)


def _integer(value: Any, field: str, errors: list[str]) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{field} must be an integer")
        return 0
    return value


def _load_mapping(config: str | Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise ClientModelError(
            ["PyYAML is required for path loading; run a bin/work-skills wrapper to bootstrap requirements.lock"]
        ) from exc
    path = Path(config)
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, Mapping):
        raise ClientModelError([f"configuration root must be a mapping: {path}"])
    return value


def _expanded_path(value: Any, field: str, env: Mapping[str, str], errors: list[str]) -> Path:
    if not isinstance(value, str) or not value:
        errors.append(f"{field} must be a non-empty string")
        return Path(".")
    try:
        path = Path(_expand(value, env))
    except ValueError as exc:
        errors.append(f"{field} {exc}")
        return Path(value)
    if not path.is_absolute():
        errors.append(f"{field} must expand to an absolute path")
    return path


def load_client_model(
    config: str | Path | Mapping[str, Any], *, environ: Mapping[str, str] | None = None
) -> ClientModel:
    """Load and strongly validate v3 fields from a path or mapping."""

    raw = _load_mapping(config)
    env = dict(os.environ if environ is None else environ)
    errors: list[str] = []
    if raw.get("version") != 3:
        errors.append("version must be 3")
    raw_mounts = _mapping(raw.get("mountPoints"), "mountPoints", errors)
    raw_consumers = _mapping(raw.get("consumers"), "consumers", errors)
    raw_adapters = _mapping(raw.get("adapters"), "adapters", errors)
    mounts: dict[str, MountPoint] = {}
    consumers: dict[str, Consumer] = {}
    adapters: dict[str, Adapter] = {}

    for mount_id, value in raw_mounts.items():
        field = f"mountPoints.{mount_id}"
        if not isinstance(mount_id, str) or not _ID_PATTERN.fullmatch(mount_id):
            errors.append(f"{field} has an invalid stable id")
            continue
        item = _mapping(value, field, errors)
        link_policy = _mapping(item.get("linkPolicy"), f"{field}.linkPolicy", errors)
        for platform in ("windows", "posix"):
            if not isinstance(link_policy.get(platform), str):
                errors.append(f"{field}.linkPolicy.{platform} must be a string")
        delivery = str(item.get("delivery") or "")
        if delivery not in {"shared", "dedicated", "compatibility", "private-only", "candidate"}:
            errors.append(f"{field}.delivery has an unsupported value")
        source_selection = str(item.get("sourceSelection") or "all")
        if source_selection not in {"all", "none"}:
            errors.append(f"{field}.sourceSelection must be all or none")
        mounts[mount_id] = MountPoint(
            mount_id,
            str(item.get("label") or mount_id),
            _expanded_path(item.get("root"), f"{field}.root", env, errors),
            delivery,
            _integer(item.get("priority"), f"{field}.priority", errors),
            dict(link_policy),
            source_selection,
            _expanded_path(item["discoveryManifest"], f"{field}.discoveryManifest", env, errors)
            if item.get("discoveryManifest") is not None else None,
        )
        manifest_path = mounts[mount_id].discovery_manifest
        if manifest_path is not None and manifest_path != mounts[mount_id].root.with_suffix(".json"):
            errors.append(f"{field}.discoveryManifest must be the sibling JSON file of its skills root")

    for consumer_id, value in raw_consumers.items():
        field = f"consumers.{consumer_id}"
        if not isinstance(consumer_id, str) or not _ID_PATTERN.fullmatch(consumer_id):
            errors.append(f"{field} has an invalid stable id")
            continue
        item = _mapping(value, field, errors)
        consumer_status = str(item.get("status") or "")
        if consumer_status not in {"confirmed", "report-only", "ambiguous"}:
            errors.append(f"{field}.status has an unsupported value")
        detector_raw = _mapping(item.get("detector"), f"{field}.detector", errors)
        mode = str(detector_raw.get("mode") or "")
        if mode not in {"any", "all"}:
            errors.append(f"{field}.detector.mode must be any or all")
        commands_raw = detector_raw.get("commands", [])
        paths_raw = detector_raw.get("paths", [])
        if not isinstance(commands_raw, list) or any(not isinstance(v, str) or not v for v in commands_raw):
            errors.append(f"{field}.detector.commands must be a list of strings")
            commands_raw = []
        if not isinstance(paths_raw, list) or any(not isinstance(v, str) or not v for v in paths_raw):
            errors.append(f"{field}.detector.paths must be a list of strings")
            paths_raw = []
        if not commands_raw and not paths_raw:
            errors.append(f"{field}.detector must declare a command or path")
        paths = tuple(
            _expanded_path(path, f"{field}.detector.paths[{index}]", env, errors)
            for index, path in enumerate(paths_raw)
        )
        verification_raw = _mapping(item.get("verification"), f"{field}.verification", errors)
        instructions = verification_raw.get("instructions")
        if not isinstance(instructions, str) or not instructions:
            errors.append(f"{field}.verification.instructions must be a non-empty string")
            instructions = ""
        verification_status = str(verification_raw.get("status") or "")
        if not verification_status:
            errors.append(f"{field}.verification.status must be a non-empty string")
        consumers[consumer_id] = Consumer(
            consumer_id,
            str(item.get("label") or consumer_id),
            consumer_status,
            Detector(mode, tuple(commands_raw), paths),
            Verification(
                verification_status,
                instructions,
                verification_raw.get("command") if isinstance(verification_raw.get("command"), str) else None,
            ),
        )

    for adapter_id, value in raw_adapters.items():
        field = f"adapters.{adapter_id}"
        if not isinstance(adapter_id, str) or not _ID_PATTERN.fullmatch(adapter_id):
            errors.append(f"{field} has an invalid stable id")
            continue
        item = _mapping(value, field, errors)
        consumer_ids = _strings(item.get("consumerIds"), f"{field}.consumerIds", errors)
        mount_ids = _strings(item.get("mountPointIds"), f"{field}.mountPointIds", errors)
        for ref in consumer_ids:
            if ref not in consumers:
                errors.append(f"{field}.consumerIds references unknown consumer {ref!r}")
        for ref in mount_ids:
            if ref not in mounts:
                errors.append(f"{field}.mountPointIds references unknown mount point {ref!r}")
        roots_raw = item.get("discoveryRoots")
        roots: list[DiscoveryRoot] = []
        if not isinstance(roots_raw, list) or not roots_raw:
            errors.append(f"{field}.discoveryRoots must be a non-empty list")
        else:
            for index, root_value in enumerate(roots_raw):
                root_raw = _mapping(root_value, f"{field}.discoveryRoots[{index}]", errors)
                ref = root_raw.get("mountPointId")
                if ref not in mount_ids:
                    errors.append(f"{field}.discoveryRoots[{index}] must reference one of mountPointIds")
                roots.append(
                    DiscoveryRoot(
                        str(ref or ""),
                        _integer(root_raw.get("priority"), f"{field}.discoveryRoots[{index}].priority", errors),
                        str(root_raw.get("role") or ""),
                    )
                )
        link_raw = _mapping(item.get("linkSupport"), f"{field}.linkSupport", errors)
        evidence_raw = _mapping(link_raw.get("evidence"), f"{field}.linkSupport.evidence", errors)
        source_url = evidence_raw.get("sourceUrl")
        if source_url is not None and (not isinstance(source_url, str) or not source_url.startswith("https://")):
            errors.append(f"{field}.linkSupport.evidence.sourceUrl must be null or an https URL")
            source_url = None
        for key in ("sourceLevel", "verifiedVersion"):
            if not isinstance(evidence_raw.get(key), str) or not evidence_raw.get(key):
                errors.append(f"{field}.linkSupport.evidence.{key} must be a non-empty string")
        verified_on = evidence_raw.get("verifiedOn")
        if not isinstance(verified_on, str) or not _DATE_PATTERN.fullmatch(verified_on):
            errors.append(f"{field}.linkSupport.evidence.verifiedOn must be YYYY-MM-DD")
            verified_on = ""
        management = _mapping(item.get("management"), f"{field}.management", errors)
        manage_default = management.get("manageByDefault")
        create_absent = management.get("createWhenAbsent")
        if not isinstance(manage_default, bool):
            errors.append(f"{field}.management.manageByDefault must be boolean")
            manage_default = False
        if not isinstance(create_absent, bool):
            errors.append(f"{field}.management.createWhenAbsent must be boolean")
            create_absent = False
        status = str(management.get("status") or "")
        if status not in {"confirmed", "report-only", "ambiguous"}:
            errors.append(f"{field}.management.status has an unsupported value")
        if manage_default and status != "confirmed":
            errors.append(f"{field} can manageByDefault only when management.status is confirmed")
        if manage_default and any(consumers.get(ref) and consumers[ref].status != "confirmed" for ref in consumer_ids):
            errors.append(f"{field} can manageByDefault only for confirmed consumers")
        if create_absent:
            errors.append(f"{field}.management.createWhenAbsent must remain false")
        detection_ref = item.get("consumerDetection")
        if detection_ref not in consumer_ids:
            errors.append(f"{field}.consumerDetection must reference one of consumerIds")
        native_verification = str(item.get("nativeVerification") or "")
        if not native_verification:
            errors.append(f"{field}.nativeVerification must be a non-empty string")
        runtime_raw_value = item.get("pythonRuntimeVerification")
        runtime_verification: PythonRuntimeVerification | None = None
        if runtime_raw_value is not None:
            runtime_field = f"{field}.pythonRuntimeVerification"
            runtime_raw = _mapping(runtime_raw_value, runtime_field, errors)
            runtime_status = str(runtime_raw.get("status") or "")
            if runtime_status != "required":
                errors.append(f"{runtime_field}.status must be required")
            context_id = str(runtime_raw.get("contextId") or "")
            if not _ID_PATTERN.fullmatch(context_id):
                errors.append(f"{runtime_field}.contextId must be a stable id")
            capture_command = runtime_raw.get("captureCommand")
            if not isinstance(capture_command, str) or not capture_command.strip():
                errors.append(f"{runtime_field}.captureCommand must be a non-empty string")
                capture_command = ""
            python_command = runtime_raw.get("pythonCommand")
            if not isinstance(python_command, str) or not python_command.strip():
                errors.append(f"{runtime_field}.pythonCommand must be a non-empty string")
                python_command = ""
            required_version = str(runtime_raw.get("requiredVersion") or "")
            if not re.fullmatch(r"\d+\.\d+", required_version):
                errors.append(f"{runtime_field}.requiredVersion must be major.minor")
            context_raw = _mapping(
                runtime_raw.get("contextEnvironment"),
                f"{runtime_field}.contextEnvironment",
                errors,
            )
            context_environment = context_raw.get("name")
            if not isinstance(context_environment, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]*", context_environment
            ):
                errors.append(
                    f"{runtime_field}.contextEnvironment.name must be an uppercase environment name"
                )
                context_environment = ""
            allow_unset_context = context_raw.get("allowUnset")
            if not isinstance(allow_unset_context, bool):
                errors.append(f"{runtime_field}.contextEnvironment.allowUnset must be boolean")
                allow_unset_context = False
            expected_context = _expanded_path(
                context_raw.get("expected"),
                f"{runtime_field}.contextEnvironment.expected",
                env,
                errors,
            )
            runtime_verification = PythonRuntimeVerification(
                runtime_status,
                context_id,
                capture_command.strip(),
                python_command.strip(),
                required_version,
                context_environment,
                expected_context,
                allow_unset_context,
            )
        link_status = str(link_raw.get("status") or "")
        if not link_status:
            errors.append(f"{field}.linkSupport.status must be a non-empty string")
        binding = None
        if item.get("runtimeBinding") is not None:
            binding_field = f"{field}.runtimeBinding"
            binding_raw = _mapping(item["runtimeBinding"], binding_field, errors)
            resolver = str(binding_raw.get("resolver") or "")
            subdir = str(binding_raw.get("skillsSubdir") or "").replace("\\", "/")
            if resolver not in {"explicit", "t3-provider"}:
                errors.append(f"{binding_field}.resolver must be explicit or t3-provider")
            if not subdir or Path(subdir).is_absolute() or ".." in Path(subdir).parts:
                errors.append(f"{binding_field}.skillsSubdir must be a relative path within the home")
            path_values = {}
            required_paths = ("binaryPath", "homePath") if resolver == "explicit" else ("settingsPath", "frontendPath")
            for key in required_paths:
                path_values[key] = _expanded_path(binding_raw.get(key), f"{binding_field}.{key}", env, errors)
            instance_id = str(binding_raw.get("instanceId") or "")
            driver = str(binding_raw.get("driver") or "")
            if resolver == "t3-provider" and (not instance_id or driver not in {"claudeAgent", "antigravity"}):
                errors.append(f"{binding_field} requires instanceId and a supported driver")
            binding = RuntimeBinding(
                resolver, subdir, path_values.get("binaryPath"), path_values.get("homePath"),
                path_values.get("settingsPath"), instance_id, driver, path_values.get("frontendPath"),
            )
        adapters[adapter_id] = Adapter(
            adapter_id,
            str(item.get("label") or adapter_id),
            consumer_ids,
            mount_ids,
            _integer(item.get("priority"), f"{field}.priority", errors),
            tuple(roots),
            link_status,
            Evidence(
                source_url,
                str(evidence_raw.get("sourceLevel") or ""),
                str(evidence_raw.get("verifiedVersion") or ""),
                verified_on,
            ),
            str(detection_ref or ""),
            native_verification,
            status,
            manage_default,
            create_absent,
            runtime_verification,
            binding,
        )
    if errors:
        raise ClientModelError(errors)
    return ClientModel(3, mounts, consumers, adapters)


def detect_consumers(
    model: ClientModel,
    *,
    command_lookup: Callable[[str], str | None] = shutil.which,
    path_exists: Callable[[Path], bool] = Path.exists,
) -> tuple[ConsumerDetection, ...]:
    """Return installation evidence without creating or changing any path."""

    results: list[ConsumerDetection] = []
    for consumer in model.consumers.values():
        command_hits = tuple(command for command in consumer.detector.commands if command_lookup(command))
        path_hits = tuple(path for path in consumer.detector.paths if path_exists(path))
        total = len(consumer.detector.commands) + len(consumer.detector.paths)
        hit_count = len(command_hits) + len(path_hits)
        if hit_count == 0:
            state = "not_installed"
            details = "no detector signal matched; report only and do not create discovery roots"
        elif consumer.status == "ambiguous" or (consumer.detector.mode == "all" and hit_count != total):
            state = "ambiguous"
            details = "partial or explicitly ambiguous evidence; native verification is required"
        else:
            state = "installed"
            details = "configured detector evidence matched"
        results.append(ConsumerDetection(consumer.id, state, command_hits, path_hits, details))
    return tuple(results)
