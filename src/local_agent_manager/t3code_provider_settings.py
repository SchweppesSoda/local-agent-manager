"""Provider configuration that can be qualified from a T3 Nightly profile."""
from pathlib import Path
import plistlib
import sys
from xml.parsers.expat import ExpatError

from . import frozen_files
from .providers.t3 import strict_json


def persisted_instances(profile):
    """Project launch configuration using the server's legacy migration precedence."""
    if not isinstance(profile, dict):
        return None
    instances = profile.get("providerInstances", {})
    legacy = profile.get("providers", {})
    if not isinstance(instances, dict) or not isinstance(legacy, dict):
        return None
    result = dict(instances)
    for driver in ("codex", "claudeAgent", "pi", "antigravity", "opencode"):
        entry = legacy.get(driver)
        if driver not in result and isinstance(entry, dict):
            result[driver] = {"driver": driver, "config": {k: v for k, v in entry.items() if k != "enabled"}}
    return result


def _provider_policy(document):
    settings = document.get("settings", {})
    limits = document.get("limits", {})
    return (not isinstance(settings, dict) or not isinstance(limits, dict)
            or any(key.split(".", 1)[0] == "providerInstances" for key in settings)
            or "maxRuntimeMode" in limits or bool(limits.get("providers", {}).get("instances")))


def _policy_file(path, *, preferences=False):
    try:
        _, raw = frozen_files.read_file(path.parent, path.name, content=True, limit=1024 * 1024)
    except FileNotFoundError:
        return False
    if preferences:
        document = plistlib.loads(raw)
        if "Forced" in document:
            document = document["Forced"][0]["mcx_preference_settings"]
    else:
        document = strict_json(raw.decode("utf-8-sig"))
    return _provider_policy(document)


def _registry_policy():
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\T3Tools\T3Code", 0,
                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except FileNotFoundError:
        return False
    with key:
        for name in ("Policy", "Settings", "ProviderInstances", "MaxRuntimeMode"):
            try:
                value, kind = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                continue
            if kind != winreg.REG_SZ:
                return True
            if name == "Policy":
                return _provider_policy(strict_json(value))
            if not value:
                continue
            if name == "Settings" and not _provider_policy({"settings": strict_json(value)}):
                continue
            if name == "ProviderInstances" and strict_json(value) == {}:
                continue
            return True
    return False


def has_provider_policy():
    """Machine policy overlays are absent from settings.json even after startup."""
    try:
        if sys.platform == "win32":
            return _policy_file(Path(r"C:\Program Files\T3Code\policy.json")) or _registry_policy()
        if sys.platform == "darwin":
            return (_policy_file(Path("/Library/Application Support/T3Code/policy.json")) or
                    _policy_file(Path("/Library/Managed Preferences/com.t3tools.t3code.plist"), preferences=True))
        return _policy_file(Path("/etc/t3code/policy.json"))
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RuntimeError,
            ExpatError, plistlib.InvalidFileException):
        return True
