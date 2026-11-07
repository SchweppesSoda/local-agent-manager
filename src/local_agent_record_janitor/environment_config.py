"""Narrow, read-only projections of known agent environment metadata."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import frozen_files
from .discovery import release_version
from .environment_paths import BoundedPathProbe

MAX_CONFIG_BYTES = 256 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid_json_constant")


def metadata_json(
    root: Path, relative: str, probe: BoundedPathProbe, label: str
) -> tuple[dict[str, Any], dict | None]:
    path = root / relative
    status, info = probe.inspect(path, label, directory=False)
    observation = {"path": str(path), "path_status": status, "schema_status": "unknown"}
    if info is None:
        return observation, None
    if info.st_size > MAX_CONFIG_BYTES:
        probe.issue(label, "metadata_budget_exceeded", path)
        return observation, None
    try:
        # A stable, single-link regular file is required, including at reopen.
        # No digest, full document, or parser exception leaves this function.
        _, raw = frozen_files.read_file(root, relative, content=True, limit=MAX_CONFIG_BYTES)
        data = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, ValueError, RuntimeError, RecursionError):
        probe.issue(label, "metadata_unreadable_or_invalid", path)
        return observation, None
    if not isinstance(data, dict):
        probe.issue(label, "metadata_schema_unknown", path)
        return observation, None
    return observation, data


def package_version(
    root: Path, package: str, probe: BoundedPathProbe, label: str
) -> str | None:
    _, data = metadata_json(root, "package.json", probe, label)
    if data is None:
        return None
    value = data.get("version")
    if data.get("name") != package or not isinstance(value, str):
        probe.issue(label, "metadata_schema_unknown", root / "package.json")
        return None
    version = release_version(value)
    if version is None:
        probe.issue(label, "metadata_schema_unknown", root / "package.json")
    return version


# See getpaseo/paseo docs/custom-providers.md (v1 agents.providers.command).
_PASEO_PROVIDERS = {
    "codex": "codex",
    "claude": "claude",
    "pi": "pi",
    "antigravity": "antigravity-cli",
}


def paseo_runtime_hints(
    root: Path, probe: BoundedPathProbe
) -> tuple[dict[str, Any], list[tuple[str, str | None, str | None, bool | None]]]:
    observation, data = metadata_json(root, "config.json", probe, "paseo_config")
    if data is None:
        return observation, []
    agents = data.get("agents")
    if (
        type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(agents, dict)
        or not isinstance(agents.get("providers"), dict)
    ):
        probe.issue("paseo_config", "metadata_schema_unknown", root / "config.json")
        return observation, []
    observation["schema_status"] = "known_projection"
    hints = []
    for provider, engine in _PASEO_PROVIDERS.items():
        value = agents["providers"].get(provider)
        if not isinstance(value, dict) or "extends" in value:
            # A custom provider can shadow a built-in with a different engine.
            continue
        command = value.get("command")
        if command is not None and not (
            isinstance(command, list) and command and isinstance(command[0], str)
        ):
            probe.issue("paseo_config", "provider_command_shape_unknown", root / "config.json")
            continue
        enabled = value.get("enabled")
        hints.append((engine, command[0] if command is not None else None, None,
                      enabled if isinstance(enabled, bool) else None))
    return observation, hints


# See pingdotgg/t3code packages/contracts/src/settings.ts
# (ProviderInstanceConfig, Codex/Claude/AntigravitySettings). Only fixed IDs
# with their matching drivers are projected, never user-defined IDs or args.
_T3_PROVIDERS = {
    "codex": "codex",
    "claudeAgent": "claude",
    "antigravity": "antigravity-acp",
}


def t3_runtime_hints(
    root: Path, probe: BoundedPathProbe
) -> tuple[dict[str, Any], list[tuple[str, str | None, str | None, bool | None]]]:
    relative = "userdata/settings.json"
    observation, data = metadata_json(root, relative, probe, "t3code_config")
    if data is None:
        return observation, []
    providers = data.get("providerInstances")
    if "version" in data or not isinstance(providers, dict):
        probe.issue("t3code_config", "metadata_schema_unknown", root / relative)
        return observation, []
    observation["schema_status"] = "known_projection"
    hints = []
    for driver, engine in _T3_PROVIDERS.items():
        instance = providers.get(driver)
        if not isinstance(instance, dict) or instance.get("driver") != driver:
            continue
        config = instance.get("config")
        if not isinstance(config, dict):
            continue
        binary, home = config.get("binaryPath"), config.get("homePath")
        enabled = instance.get("enabled")
        if config.get("enabled") is False:
            enabled = False
        if (
            binary is not None and not isinstance(binary, str)
            or engine in ("codex", "claude") and home is not None and not isinstance(home, str)
        ):
            probe.issue("t3code_config", "provider_setting_shape_unknown", root / relative)
            continue
        hints.append((engine, binary, home if engine in ("codex", "claude") else None,
                      enabled if isinstance(enabled, bool) else None))
    return observation, hints


# See T3 Code AntigravityInstallation.ts: the pointer names a SHA-256 release
# directory, and its completion record describes the two installed files.
_T3_ACP_RELEASE_ID = re.compile(r"[a-f0-9]{64}")


def t3_acp_managed_release(platform: Path, probe: BoundedPathProbe) -> tuple[Path, str] | None:
    label = "t3_antigravity_installer"
    pointer_path = platform / "active.json"
    _, pointer = metadata_json(platform, "active.json", probe, label)
    if pointer is None:
        return None
    release_id = pointer.get("releaseId")
    if not isinstance(release_id, str) or not _T3_ACP_RELEASE_ID.fullmatch(release_id):
        probe.issue(label, "metadata_schema_unknown", pointer_path)
        return None

    release = platform / "versions" / release_id
    _, manifest = metadata_json(release, ".install-complete.json", probe, label)
    if manifest is not None and manifest.get("releaseId") == release_id:
        names = ("agy_acp_server.exe", "localharness_external.exe") if platform.name.startswith("win32-") else (
            "agy_acp_server.par", "localharness_external",
        )
        version_text = manifest.get("version")
        version = release_version(version_text) if isinstance(version_text, str) else None
        if version is not None and all(
            _installed_file_matches(release / name, manifest.get(key), probe, label)
            for key, name in zip(("executable", "harness"), names)
        ):
            return release / names[0], version

    probe.issue(label, "unverified_installer_pointer", pointer_path)
    return None


def _installed_file_matches(path: Path, record: Any, probe: BoundedPathProbe, label: str) -> bool:
    if not isinstance(record, dict) or record.get("name") != path.name:
        return False
    expected_size = record.get("bytes")
    if type(expected_size) is not int or expected_size <= 0:
        return False
    _, info = probe.inspect(path, label, directory=False)
    return info is not None and info.st_size == expected_size


def paseo_antigravity_backup_metadata(
    backup: Path, probe: BoundedPathProbe,
) -> tuple[dict[str, Any], bool]:
    """Identify the fixed manifest ID without opening plugin source or state."""
    observation, manifest = metadata_json(backup, "paseo-plugin.json", probe, "paseo_antigravity_backup")
    known = manifest is not None and manifest.get("id") == "antigravity-provider"
    if known:
        observation["schema_status"] = "known_projection"
    return observation, known
