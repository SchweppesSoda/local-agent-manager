"""Read-only runtime identities. These checks never launch a provider."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import struct
from typing import Any

from .model import RuntimeBinding
from ...providers.t3 import strict_json, provider_instance


class RuntimeBindingError(ValueError):
    pass


def file_identity(path: Path) -> dict[str, Any]:
    """Fingerprint the executable actually reached, including its configured path."""
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeBindingError("Runtime changed while its identity was being read")
    return {
        "path": str(path), "resolvedPath": str(resolved), "size": after.st_size,
        "mtimeNs": after.st_mtime_ns, "sha256": digest.hexdigest(),
    }


def frontend_version(path: Path) -> str:
    if path.suffix.lower() == ".asar":
        with path.open("rb") as handle:
            header = struct.unpack("<4I", handle.read(16))
            if header[3] > 16 * 1024 * 1024:
                raise RuntimeBindingError("Frontend archive header exceeds the inspection limit")
            tree = json.loads(handle.read(header[3]))
            entry = tree["files"]["package.json"]
            if entry.get("unpacked") or entry["size"] > 1024 * 1024:
                raise RuntimeBindingError("Frontend package metadata cannot be inspected")
            handle.seek(8 + header[1] + int(entry["offset"]))
            package = json.loads(handle.read(entry["size"]))
    else:
        package = json.loads(path.read_text(encoding="utf-8"))
    version = package.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeBindingError("Frontend version is unavailable")
    return version


def _environment_path(environment: dict, key: str, fallback: str) -> str:
    value = environment.get(key, fallback)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeBindingError(f"Runtime home field {key} requires an explicit literal path")
    return value


def inspect_runtime_binding(
    policy: RuntimeBinding, mount_root: Path, discovery_manifest: Path | None = None,
) -> dict[str, Any]:
    """Resolve only allowed launch fields and reject a home/root mismatch."""
    try:
        provider = None
        frontend = None
        binary, home = policy.binary_path, policy.home_path
        if policy.resolver == "t3-provider":
            settings = strict_json(policy.settings_path.read_text(encoding="utf-8-sig"))
            selected = provider_instance(settings, policy.instance_id, policy.driver)
            if not selected["enabled"]:
                raise RuntimeBindingError("Selected T3 provider instance is disabled")
            config, environment = selected["config"], selected["environment"]
            binary_text = config.get("binaryPath")
            if not isinstance(binary_text, str) or not Path(binary_text).is_absolute():
                raise RuntimeBindingError("T3 runtime binding requires an absolute configured binaryPath")
            binary = Path(binary_text)
            home_key = "USERPROFILE" if os.name == "nt" else "HOME"
            user_home = Path(_environment_path(environment, home_key, str(Path.home())))
            home_text = str(user_home / ".gemini")
            if policy.driver == "claudeAgent":
                home_text = config.get("homePath") or _environment_path(environment, "CLAUDE_CONFIG_DIR", str(user_home / ".claude"))
            if not isinstance(home_text, str):
                raise RuntimeBindingError("Selected T3 runtime home is invalid")
            home = user_home / home_text[2:] if home_text.startswith(("~/", "~\\")) else Path(home_text)
            provider = {
                "settingsPath": str(policy.settings_path), "instanceId": policy.instance_id, "driver": policy.driver,
                "launchArgsSha256": hashlib.sha256(json.dumps(config.get("launchArgs"), sort_keys=True).encode()).hexdigest(),
                "skillModeEnvironmentSha256": hashlib.sha256(json.dumps({
                    key: environment[key]
                    for key in ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE") if key in environment
                }, sort_keys=True).encode()).hexdigest(),
            }
            frontend = file_identity(policy.frontend_path)
            frontend["version"] = frontend_version(policy.frontend_path)
        if binary is None or home is None or not binary.is_absolute() or not home.is_absolute():
            raise RuntimeBindingError("Runtime binding requires absolute binary and home paths")
        if os.path.normcase(os.path.abspath(home / policy.skills_subdir)) != os.path.normcase(os.path.abspath(mount_root)):
            raise RuntimeBindingError("Effective runtime home does not match the configured skills root")
        result = {
            "resolver": policy.resolver, "binary": file_identity(binary),
            "home": str(home), "mountRoot": str(mount_root), "skillsSubdir": policy.skills_subdir,
        }
        if provider is not None:
            result["provider"] = provider
            result["frontend"] = frontend
        if discovery_manifest is not None:
            result["discoveryManifest"] = file_identity(discovery_manifest) if discovery_manifest.exists() else {
                "path": str(discovery_manifest), "status": "missing",
            }
        return result
    except RuntimeBindingError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error) as exc:
        # Parsing failures must not echo configuration content or credential values.
        raise RuntimeBindingError(f"Runtime identity could not be inspected ({type(exc).__name__})") from exc


def binding_input_paths(policy: RuntimeBinding, snapshot: dict[str, Any]) -> list[Path]:
    paths = [Path(snapshot["binary"]["path"])]
    if policy.resolver == "t3-provider":
        paths.extend([policy.settings_path, policy.frontend_path])
    if "discoveryManifest" in snapshot:
        paths.append(Path(snapshot["discoveryManifest"]["path"]))
    return paths


def validate_binding_evidence(evidence: Any, adapter_id: str, snapshot: dict[str, Any]) -> None:
    if not isinstance(evidence, dict) or evidence.get("adapterId") != adapter_id or evidence.get("binding") != snapshot:
        raise RuntimeBindingError("Binding evidence belongs to another context or the runtime has changed")
    try:
        captured = datetime.datetime.fromisoformat(evidence["capturedAt"])
        age = datetime.datetime.now(datetime.timezone.utc) - captured
        if not datetime.timedelta(0) <= age <= datetime.timedelta(hours=24):
            raise RuntimeBindingError("Binding evidence is stale or future-dated")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeBindingError("Binding evidence has no valid recent capture time") from exc
