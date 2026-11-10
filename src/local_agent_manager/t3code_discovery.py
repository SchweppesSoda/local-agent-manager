"""Derive T3 cleanup bindings from configuration and native ID evidence."""
from __future__ import annotations

from pathlib import Path
import os

from . import frozen_files
from .herdr_bound_adapter import native_catalog
from .providers.t3 import strict_json
from .record_identity import canonical_path
from .t3code_bound_adapter import SCHEMA, engine, history_references, thread_engine, validate_manifest
from .t3code_store import default_root, identifier, local_root, read_database


class BindingDiscoveryError(ValueError):
    kind = "t3code_binding_discovery_blocked"

    def __init__(self, blockers):
        self.blockers = blockers
        super().__init__("; ".join(item["blocker_code"] + ":" + item.get("thread_id", "profile") for item in blockers))


def _plain_directory(path):
    path = local_root(path)
    for parent in (*reversed(path.parents), path):
        frozen_files._plain(parent.lstat(), directory=True)
    return path


def _environment_paths(instance, name):
    environment = instance.get("environment", {})
    if isinstance(environment, dict):
        value = environment.get(name)
        values = [] if value is None else [value]
    elif isinstance(environment, list):
        values = [item.get("value") for item in environment
                  if isinstance(item, dict) and item.get("name") == name]
    else:
        raise ValueError("provider_environment_unverified")
    if len(values) > 1 or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("provider_environment_unverified")
    return values


def _catalog_ids(binding):
    catalog = native_catalog(binding)
    if catalog.errors:
        raise ValueError("native_inventory_incomplete")
    if binding["engine"] == "codex":
        return {record.thread_id for record in catalog.records}
    return {record.session_id for record in catalog.records}


def _native_binding(row, providers, instance, cache):
    driver = row["default_provider"]
    if driver == "pi":
        return _pi_binding(row, providers, instance, cache)
    backend = {"codex": "codex", "claudeAgent": "claude"}.get(driver)
    if backend is None:
        raise ValueError("provider_cleanup_unsupported")
    config = instance.get("config", {})
    if not isinstance(config, dict):
        raise ValueError("provider_configuration_unverified")
    declared = config.get("homePath")
    if declared is not None and (not isinstance(declared, str) or not declared.strip()):
        raise ValueError("provider_home_unverified")
    homes = [declared] if declared else _environment_paths(
        instance, "CODEX_HOME" if backend == "codex" else "CLAUDE_CONFIG_DIR")
    if not homes:
        # A default directory is only a candidate. Native IDs must prove it.
        homes = [str(Path.home() / (".codex" if backend == "codex" else ".claude"))]
    native_ids = {item["native_id"] for item in providers if item["thread_id"] == row["thread_id"]
                  and item["native_id"] is not None and item["native_strength"] == "strong"}
    if not native_ids and not declared:
        raise ValueError("native_root_unproven")
    matches = []
    for raw in homes:
        root = _plain_directory(raw)
        binding = {"thread_id": row["thread_id"], "engine": backend, "root": str(root)}
        if backend == "codex" and config.get("binaryPath"):
            if not isinstance(config["binaryPath"], str):
                raise ValueError("provider_binary_unverified")
            binary = local_root(config["binaryPath"])
            _plain_directory(binary.parent)
            frozen_files._plain(binary.lstat())
            binding["codex_binary"] = str(binary)
        key = backend, canonical_path(root)
        if key not in cache:
            # A failed root also stays failed for this preparation. Hundreds
            # of imported threads must not trigger hundreds of full rescans.
            cache[key] = None
            cache[key] = _catalog_ids(binding)
        if cache[key] is None:
            raise ValueError("native_inventory_incomplete")
        if native_ids <= cache[key]:
            matches.append(binding)
    if len(matches) != 1:
        raise ValueError("native_ids_not_in_configured_home")
    return matches[0]


def _pi_binding(row, providers, instance, cache):
    config = instance.get("config", {})
    if not isinstance(config, dict) or config.get("launchArgs", ""):
        raise ValueError("pi_launch_arguments_require_explicit_binding")
    agent_dirs = _environment_paths(instance, "PI_CODING_AGENT_DIR")
    agent_dir = _plain_directory(agent_dirs[0] if agent_dirs else Path.home() / ".pi/agent")
    session_dirs = _environment_paths(instance, "PI_CODING_AGENT_SESSION_DIR")
    root = _plain_directory(session_dirs[0] if session_dirs else agent_dir / "sessions")
    binding = {"thread_id": row["thread_id"], "engine": "pi", "root": str(root), "agent_dir": str(agent_dir)}
    key = "pi", canonical_path(root)
    if key not in cache:
        catalog = native_catalog(binding)
        if catalog.errors:
            raise ValueError("native_inventory_incomplete")
        cache[key] = {canonical_path(record.path) for record in catalog.records}
    handles = [item["native_id"] for item in providers if item["thread_id"] == row["thread_id"]
               and item["native_id"] is not None and item["native_strength"] == "strong"]
    if not handles and not agent_dirs and not session_dirs:
        raise ValueError("native_root_unproven")
    if any(canonical_path(local_root(path)) not in cache[key] for path in handles):
        raise ValueError("native_ids_not_in_configured_home")
    return binding


def _acp_binding(root, row, instance, runtime_binaries):
    from .antigravity_acp_cleanup import RUNTIMES
    from .environment_config import t3_acp_managed_release
    from .environment_paths import BoundedPathProbe
    import hashlib

    config = instance.get("config", {})
    binary = config.get("binaryPath") if isinstance(config, dict) else None
    if binary:
        if not isinstance(binary, str):
            raise ValueError("provider_binary_unverified")
        server = local_root(binary)
        if server.name != "agy_acp_server.exe":
            raise ValueError("antigravity_cli_acp_cleanup_unsupported")
        directory = server.parent
    else:
        release = t3_acp_managed_release(root / "tools/antigravity-acp/win32-x64", BoundedPathProbe())
        if release is None:
            raise ValueError("antigravity_managed_runtime_unproven")
        directory = release[0]
    binaries = []
    for name, expected_hash in RUNTIMES.items():
        proof, _ = frozen_files.read_file(directory, name)
        if proof["sha256"] != expected_hash:
            raise ValueError("antigravity_managed_runtime_unqualified")
        binaries.append(str(directory / name))
    runtime_binaries.update(binaries)
    instance_id = identifier(row["provider_instance_id"])
    home = root / "userdata/providers/antigravity" / hashlib.sha256(instance_id.encode()).hexdigest() / "antigravity-acp"
    _plain_directory(home)
    return {"thread_id": row["thread_id"], "engine": "antigravity", "root": str(home),
            "provider_instance_id": instance_id, "acp_runtime_binaries": binaries}


def discover_bindings(root=None):
    """Prepare all profile protection bindings; grant no deletion authorization."""
    from .t3code_server_delete import _bundled_install, _RUNTIME_FILES, _environment_id

    root = _plain_directory(default_root() if root is None else root)
    settings_proof, raw = frozen_files.read_file(root, "userdata/settings.json", content=True, limit=1024 * 1024)
    settings = strict_json(raw.decode("utf-8-sig"))
    instances = settings.get("providerInstances") if isinstance(settings, dict) else None
    if not isinstance(instances, dict):
        raise BindingDiscoveryError([{"blocker_code": "t3code_provider_settings_unverified"}])
    data = read_database(root)
    install = _plain_directory(_bundled_install())
    binary = install / _RUNTIME_FILES[0]
    frozen_files.read_file(binary.parent, binary.name)
    binaries = {str(binary)}
    bindings, blockers, cache = [], [], {}
    for thread_id, copies in sorted(data["threads"].items()):
        try:
            if len(copies) != 1 or copies[0]["version"] != "v2":
                raise ValueError("legacy_thread_binding_unverified")
            row = copies[0]
            instance_id = row["provider_instance_id"] or row["default_provider"]
            instance = instances.get(instance_id)
            if (not isinstance(instance, dict)
                    or row["default_provider"] not in {instance.get("driver"), instance_id}):
                raise ValueError("provider_instance_unverified")
            driver = instance.get("driver")
            if not isinstance(driver, str):
                raise ValueError("provider_instance_unverified")
            if thread_engine(row, data["providers"]) != engine(driver):
                raise ValueError("provider_instance_unverified")
            if driver == "antigravity":
                binding = _acp_binding(root, row, instance, binaries)
            else:
                binding = _native_binding({**row, "default_provider": driver}, data["providers"], instance, cache)
            bindings.append(binding)
        except (OSError, ValueError, RuntimeError) as exc:
            code = str(exc) if type(exc) is ValueError else "native_binding_unverified"
            blockers.append({"blocker_code": code, "thread_id": thread_id})
    if blockers:
        raise BindingDiscoveryError(blockers)
    desktops = []
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    for name in ("t3code-v2", "T3 Code"):
        path = appdata / name
        if path.exists():
            _plain_directory(path)
            desktops.append({"root": str(path), "runtime_binaries": [str(binary)]})
    manifest = validate_manifest({"schema_version": SCHEMA, "profile_root": str(root),
        "environment_id": _environment_id(root), "runtime_binaries": sorted(binaries),
        "native_stores": bindings, "desktop_profiles": desktops})
    # Validate historical handles as well as the current projection before an
    # automatic binding can be passed to the established operation planner.
    history = history_references(root, {item["thread_id"]: item for item in bindings})
    for item in history:
        if item["native_id"] is None or item["strength"] != "strong":
            continue
        binding = next(value for value in bindings if value["thread_id"] == item["thread_id"])
        if binding["engine"] in {"codex", "claude", "pi"}:
            ids = cache[binding["engine"], canonical_path(binding["root"])]
            native_id = canonical_path(local_root(item["native_id"])) if binding["engine"] == "pi" else item["native_id"]
            if native_id not in ids:
                raise BindingDiscoveryError([{"blocker_code": "historical_native_root_unproven", "thread_id": item["thread_id"]}])
    if frozen_files.read_file(root, "userdata/settings.json")[0] != settings_proof:
        raise BindingDiscoveryError([{"blocker_code": "t3code_provider_settings_changed"}])
    return manifest
