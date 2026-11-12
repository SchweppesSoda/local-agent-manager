"""Prove Paseo built-in native stores using launch settings and saved handles."""
import os
from pathlib import Path

from . import frozen_files, paseo_cleanup_files as files
from .herdr_bound_adapter import native_catalog
from .paseo_bound_adapter import SCHEMA, PaseoBoundAdapter, validate_manifest
from .paseo_store import _plain, default_root, local_root
from .record_identity import canonical_path


class BindingDiscoveryError(ValueError):
    def __init__(self, blockers):
        self.blockers = blockers
        super().__init__("paseo_binding_discovery_blocked")


def _directory(value):
    path = local_root(value)
    _plain(path, directory=True)
    return path


def _environment(*layers):
    result = {}
    fields = {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"}
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("provider_environment_unverified")
        selected = {}
        for name, value in layer.items():
            if name.upper() not in fields:
                continue
            if name.upper() in selected or not isinstance(value, str) or not value.strip():
                raise ValueError("provider_environment_unverified")
            selected[name.upper()] = value
        result.update(selected)
    return result


def _binding(entry, settings, cache):
    row, value = entry["row"], entry["value"]
    backend = row["provider"]
    if backend == 'antigravity':
        from .antigravity_cli_binding import paseo_launch
        from .antigravity_cli_operations import records
        if not row.get('antigravity_handle_qualified'):
            raise ValueError('antigravity_handle_unqualified')
        binary, home = paseo_launch(settings.get(backend, {}), value.get('launchEnv', {}))
        root = _directory(home/'.gemini/antigravity-cli')
        key = backend, canonical_path(root)
        if key not in cache:
            cache[key] = {r['record_id'] for r in records(root)['records']}
        if any(h['value'] not in cache[key] for h in row['references']):
            raise ValueError('native_handle_not_in_configured_home')
        return {'agent_id': row['id'], 'engine': backend, 'root': str(root), 'native_layout': 'cli',
                'cli_binary': str(binary), 'cli_user_home': str(home)}
    if backend not in {"codex", "claude", "pi"}:
        raise ValueError("provider_native_cleanup_unsupported")
    provider = settings.get(backend, {})
    if not isinstance(provider, dict) or provider.get("extends") is not None:
        raise ValueError("provider_implementation_unverified")
    environment = _environment(provider.get("env", {}), value.get("launchEnv", {}))
    field = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR", "pi": "PI_CODING_AGENT_DIR"}[backend]
    default = {"codex": ".codex", "claude": ".claude", "pi": ".pi/agent"}[backend]
    home = _directory(environment.get(field, Path.home() / default))
    root = _directory(environment.get("PI_CODING_AGENT_SESSION_DIR", home / "sessions")) if backend == "pi" else home
    binding = {"agent_id": row["id"], "engine": backend, "root": str(root)}
    if backend == "pi":
        binding["agent_dir"] = str(home)
    command = provider.get("command")
    if command is not None:
        # Extra wrapper arguments can override homes. Explicit bindings remain
        # available for installations whose launch contract needs inspection.
        if not isinstance(command, list) or len(command) != 1 or not isinstance(command[0], str):
            raise ValueError("provider_command_requires_explicit_binding")
        if backend == "codex" and Path(command[0]).is_absolute():
            binary = local_root(command[0])
            try:
                _plain(binary)
            except FileNotFoundError as exc:
                raise ValueError('configured_codex_binary_missing') from exc
            binding["codex_binary"] = str(binary)
    key = backend, canonical_path(root), canonical_path(home)
    if key not in cache:
        catalog = native_catalog(binding)
        if catalog.errors:
            raise ValueError("native_inventory_incomplete")
        cache[key] = catalog.records
    records = cache[key]
    ids = {record.thread_id if backend == "codex" else record.session_id for record in records}
    handles = row["references"]
    if not handles and field not in environment:
        raise ValueError("native_root_unproven")
    for handle in handles:
        if handle["provider"] != backend:
            raise ValueError("provider_handle_conflict")
        if handle["kind"] == "path":
            path = local_root(handle["value"])
            matches = [record for record in records if canonical_path(record.path) == canonical_path(path)]
            if len(matches) != 1:
                raise ValueError("native_handle_not_in_configured_home")
        elif handle["value"] not in ids:
            raise ValueError("native_handle_not_in_configured_home")
    return binding


def discover_bindings(root=None):
    """Read-only binding preparation; selected deletion still uses the planner."""
    from .paseo_daemon_delete import _bundled_install, _server_id

    root = _directory(default_root() if root is None else root)
    config = root / "config.json"
    proof = None
    settings = {}
    if config.exists():
        proof, raw = frozen_files.read_file(root, "config.json", content=True, limit=1024 * 1024)
        data = files.decode(raw)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise BindingDiscoveryError([{"blocker_code": "paseo_config_unverified"}])
        agents = data.get("agents", {})
        if not isinstance(agents, dict):
            raise BindingDiscoveryError([{"blocker_code": "paseo_config_unverified"}])
        settings = agents.get("providers", {})
        if not isinstance(settings, dict):
            raise BindingDiscoveryError([{"blocker_code": "paseo_config_unverified"}])
    install = _directory(_bundled_install())
    binary = install / "Paseo.exe"
    _plain(binary)
    bindings, blockers, cache = {}, [], {}
    entries, _ = files.rows(root)
    for entry in entries:
        if entry["kind"] != "agent":
            continue
        agent_id = entry["row"]["id"]
        try:
            binding = _binding(entry, settings, cache)
            if agent_id in bindings and bindings[agent_id] != binding:
                raise ValueError("duplicate_agent_binding_conflict")
            bindings[agent_id] = binding
        except (OSError, ValueError, RuntimeError) as exc:
            code = str(exc) if type(exc) is ValueError else "native_binding_unverified"
            blockers.append({"blocker_code": code, "agent_id": agent_id})
    if blockers:
        raise BindingDiscoveryError(blockers)
    desktops = []
    desktop = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Paseo"
    if desktop.exists():
        _directory(desktop)
        runtime = os.environ.get("LARJ_PASEO_ELECTRON_RUNTIME")
        if (desktop / "IndexedDB/paseo_app_0.indexeddb.leveldb").exists():
            if not runtime:
                raise BindingDiscoveryError([{"blocker_code": "paseo_desktop_electron_runtime_required"}])
            from .paseo_indexeddb import runtime as qualify_runtime
            qualify_runtime(_directory(runtime))
        # No IndexedDB needs no browser helper. A newly appearing database
        # invalidates the frozen closure before execution.
        desktops.append({"root": str(desktop), "electron_runtime": str(_directory(runtime) if runtime else install),
                         "runtime_binaries": [str(binary)]})
    manifest = validate_manifest({"schema_version": SCHEMA, "profile_root": str(root), "server_id": _server_id(root),
        "runtime_binaries": sorted({str(binary), *(b['cli_binary'] for b in bindings.values() if b.get('native_layout') == 'cli')}),
        "native_stores": list(bindings.values()), "desktop_profiles": desktops})
    # The bound adapter additionally proves Pi UUID/path/cwd agreement and
    # rejects opaque or conflicting handles before returning a usable binding.
    PaseoBoundAdapter(manifest).snapshot_references()
    if (frozen_files.read_file(root, "config.json")[0] if config.exists() else None) != proof:
        raise BindingDiscoveryError([{"blocker_code": "paseo_config_changed"}])
    return manifest
