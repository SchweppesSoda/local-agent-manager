"""Bounded, metadata-only observations of agent runtimes and profiles."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Mapping, Sequence

from .discovery import CINDY_PROFILE_NAMES, cindy_codex_candidates, release_version
from .environment_config import package_version, paseo_runtime_hints, t3_runtime_hints
from .environment_paths import BoundedPathProbe, MAX_DIRECTORY_ENTRIES, MAX_PATH_PROBES, local_path

SCHEMA_VERSION = "larj.environment-inventory.v1"
_CLIENTS = (
    "codex", "codex-desktop", "claude", "pi", "cindy", "paseo", "t3code",
    "antigravity-cli", "antigravity-desktop",
)
ENVIRONMENT_CLIENT_CHOICES = (*_CLIENTS, "antigravity")
_MAX_PATH_ENTRIES = 128
_MAX_PROCESS_PATHS = 256
_MAX_PATH_VALUE = 32768
_PLATFORM_DIRS = ("win32-x64", "win32-arm64", "linux-x64", "linux-arm64", "darwin-arm64", "darwin-x64")
_COMMANDS = {
    "codex": ("codex",), "claude": ("claude",), "pi": ("pi",),
    "paseo": ("paseo",), "t3code": ("t3", "t3code"), "antigravity-cli": ("agy",),
}
_SUFFIXES = (".exe", ".com", ".cmd", ".bat", ".ps1", "")
_ACP_NAMES = ("agy_acp_server.exe", "agy_acp_server.par")
_STANDALONE = frozenset(("codex", "claude", "pi", "antigravity-cli"))
_SUGGESTIONS = {
    "multiple_path_candidates": "Compare executable paths in the shell or client that launches this engine.",
    "multiple_runtime_candidates": "Check the client's selected executable or installer state before changing an installation.",
    "same_observed_file": "Compare the observed names and link count with the owning installer before changing a file.",
    "client_not_observed": "Check the host profile, PATH, or the client's installation settings.",
    "configured_executable_unavailable": "Check the configured path on the host running this client.",
    "managed_acp_helper_unavailable": "Check the runtime and helper together in T3 Code's provider settings.",
    "inventory_partial": "Review coverage issues and inspect the skipped location in its owning client.",
}


def _identifier(kind: str, *values: str) -> str:
    return kind + ":" + hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:24]


def _names(stem: str) -> tuple[str, ...]:
    return (stem + ".exe", stem)


def _profile_config(probe: BoundedPathProbe, root: Path | None, name: str, label: str) -> dict | None:
    if root is None:
        return None
    path = root / name
    status, _ = probe.inspect(path, label, directory=False)
    return {"path": str(path), "path_status": status, "schema_status": "unknown"}


class _Inventory:
    def __init__(self, probe: BoundedPathProbe, processes: set[str]) -> None:
        self.probe = probe
        self.processes = processes
        self.runtimes: dict[tuple[str, str], dict] = {}
        self.bindings: dict[str, dict] = {}
        self.findings: list[dict] = []
        self.alias_dirs: dict[str, list[dict]] = defaultdict(list)

    def finding(self, code: str, severity: str, message: str, ids: Sequence[str] = ()) -> None:
        self.findings.append({
            "code": code, "severity": severity, "message": message,
            "affected_ids": sorted(set(ids)), "suggestion": _SUGGESTIONS[code],
        })

    def runtime(
        self, client: str, engine: str, path: Path, via: str, *,
        version: str | None = None, version_source: str = "layout_version_label",
        path_index: int | None = None, include_missing: bool = False,
        kind: str = "executable", owner: str | None = None,
    ) -> dict | None:
        key = (engine, str(path))
        row = self.runtimes.get(key)
        if row is None:
            status, info = self.probe.inspect(path, via, directory=False)
            if local_path(path) is None or (
                status in ("path_budget_exceeded", "missing") and not include_missing
            ):
                return None
            running = status == "present" and str(path) in self.processes
            row = {
                "id": _identifier("runtime", *key), "client": client, "engine": engine,
                "owner": owner or ("standalone" if client in _STANDALONE else client),
                "kind": kind, "path": str(path), "path_status": status, "version": version,
                "version_source": version_source if version is not None else "unknown",
                "discovered_via": [], "selection": "observed_process_path" if running else "unknown",
            }
            if info is not None and info.st_ino and info.st_nlink > 0:
                row["file_identity"] = {
                    "device_id": info.st_dev, "file_id": info.st_ino, "nlink": info.st_nlink,
                    "observed_runtime_ids": [], "alias_coverage_complete": False,
                }
            self.runtimes[key] = row
        if via not in row["discovered_via"]:
            row["discovered_via"].append(via)
        if path_index is not None:
            row["path_index"] = min(path_index, row.get("path_index", path_index))
        return row

    def installer_alias(self, path: Path, target: dict, via: str) -> dict:
        key = ("codex", str(path))
        row = {
            "id": _identifier("runtime", *key), "client": "codex", "engine": "codex",
            "owner": "standalone", "kind": "installer_alias", "path": str(path),
            "path_status": "present", "version": target["version"],
            "version_source": "installer_alias_target_label" if target["version"] else "unknown",
            "discovered_via": [via], "alias_target_id": target["id"],
            "selection": "observed_process_path" if str(path) in self.processes else "installer_pointer",
        }
        self.runtimes[key] = row
        self.alias_dirs[str(path.parent)].append(row)
        return row

    def executables(
        self, client: str, engine: str, parent: Path | None, names: Sequence[str], via: str,
        **options: Any,
    ) -> list[dict]:
        if parent is None or self.probe.inspect(parent, via, directory=True)[0] != "present":
            return []
        return [
            row for name in names
            if (row := self.runtime(client, engine, parent / name, via, **options)) is not None
        ]

    def versioned(
        self, client: str, engine: str, base: Path | None, names: Sequence[str], via: str, *,
        subdirs: Sequence[str] = ("",), unversioned: bool = False,
        label_parser: Callable[[str], str | None] = release_version,
    ) -> list[dict]:
        if base is None:
            return []
        rows: list[dict] = []
        for candidate in self.probe.children(base, via):
            version = label_parser(candidate.name)
            if version is None and not unversioned:
                continue
            status, info = self.probe.inspect(candidate, via)
            if status != "present" or info is None or not stat.S_ISDIR(info.st_mode):
                continue
            for subdir in subdirs:
                rows.extend(self.executables(client, engine, candidate / subdir, names, via, version=version))
        return rows

    def binding(
        self, client: str, engine: str, store: Path | None, via: str, *,
        runtime: dict | None = None, configuration: dict | None = None,
        selection: str = "unknown", store_scope: str = "native_profile",
        profile: Path | None = None, enabled: bool | None = None,
    ) -> dict | None:
        status, _ = self.probe.inspect(store, via, directory=True) if store else ("unknown", None)
        if status == "missing" and runtime is None and (
            configuration is None or configuration["path_status"] == "missing"
        ) and profile is None:
            return None
        executable_id = runtime["id"] if runtime else None
        key = _identifier("binding", client, engine, str(store or ""), executable_id or "",
                          str(profile or ""), via)
        row = {
            "id": key, "client": client, "engine": engine,
            "executable_id": executable_id, "store_path": str(store) if store else None,
            "store_status": status, "store_scope": store_scope,
            "profile_path": str(profile) if profile else None, "evidence": [via],
            "selection": selection, "configuration": configuration,
        }
        if enabled is not None:
            row["configured_enabled"] = enabled
        self.bindings[key] = row
        return row

    def profile(
        self, client: str, engine: str, store: Path | None, via: str, rows: Sequence[dict], **options: Any,
    ) -> None:
        for runtime in rows or (None,):
            self.binding(client, engine, store, via, runtime=runtime, **options)

    def configure(
        self, client: str, profile: Path, config: dict,
        hints: Sequence[tuple[str, str | None, str | None, bool | None]],
    ) -> None:
        for engine, binary, home, enabled in hints:
            runtime = None
            via = client + "_config"
            evidence = dict(config)
            if binary is not None:
                path = local_path(binary) if binary else None
                names = _ACP_NAMES if engine == "antigravity-acp" else tuple(
                    name + suffix for name in _COMMANDS[engine] for suffix in _SUFFIXES
                )
                if path is not None and path.name.casefold() in names:
                    runtime = self.runtime(client, engine, path, via, include_missing=True, owner="unverified")
                    evidence["binary_setting"] = "absolute_path"
                elif binary == "" or binary.casefold() in names:
                    evidence["binary_setting"] = "default_or_command_name"
                else:
                    evidence["binary_setting"] = "unverified"
            store = local_path(home) if home else None
            if home:
                evidence["home_setting"] = "absolute_path" if store is not None else "unverified"
            row = self.binding(client, engine, store, via, runtime=runtime, configuration=evidence,
                               profile=profile, enabled=enabled, selection="configured_hint",
                               store_scope="configured_native_profile")
            if runtime is not None and runtime["path_status"] != "present" and enabled is not False:
                self.finding(
                    "configured_executable_unavailable", "warning" if runtime["path_status"] == "missing" else "info",
                    f"{client} has a configured {engine} executable that was not verified.",
                    [runtime["id"], row["id"]] if row is not None else [runtime["id"]],
                )


    def finish_findings(self, selected: Sequence[str], *, explicit: bool) -> None:
        links: dict[tuple[int, int], list[dict]] = defaultdict(list)
        paths: dict[str, list[dict]] = defaultdict(list)
        owners: dict[tuple[str, str], list[dict]] = defaultdict(list)
        observed_clients: set[str] = set()
        for row in self.runtimes.values():
            if row["path_status"] == "present":
                observed_clients.add(row["client"])
                if "path_index" in row:
                    paths[row["engine"]].append(row)
                owners[row["client"], row["engine"]].append(row)
            if (identity := row.get("file_identity")) is not None:
                links[identity["device_id"], identity["file_id"]].append(row)
        for rows in links.values():
            ids = sorted(row["id"] for row in rows)
            for row in rows:
                row["file_identity"]["observed_runtime_ids"] = ids
            if len({row["path"] for row in rows}) > 1:
                self.finding(
                    "same_observed_file", "info",
                    "Multiple observed runtime paths report the same file identity.", ids,
                )
        for engine, rows in paths.items():
            if len({str(Path(row["path"]).parent) for row in rows}) > 1:
                self.finding(
                    "multiple_path_candidates", "warning",
                    f"More than one PATH directory contains a {engine} command candidate.",
                    [row["id"] for row in rows],
                )
        for (client, engine), rows in owners.items():
            if len({row["version"] for row in rows if row["version"] is not None}) > 1:
                self.finding(
                    "multiple_runtime_candidates", "info",
                    f"Several version labels are visible for {client} / {engine}; selection is unverified.",
                    [row["id"] for row in rows],
                )
        observed_clients.update(
            row["client"] for row in self.bindings.values() if row["store_status"] == "present" or (row["configuration"] or {}).get("path_status") == "present"
        )
        if explicit:
            for client in selected:
                if client not in observed_clients:
                    self.finding(
                        "client_not_observed", "info",
                        f"No {client} runtime or profile was observed in the inspected locations.",
                    )
        if self.probe.issues:
            self.finding("inventory_partial", "info", "Some environment probes could not be completed.")


def _root(probe: BoundedPathProbe, value: Path | str | None, label: str, *,
          home: Path | None = None) -> Path | None:
    if value is None:
        return None
    raw = os.fspath(value)
    if home is not None and raw.startswith(("~/", "~\\")):
        value = home / raw[2:]
    path = local_path(value)
    if path is None:
        probe.issue(label, "nonlocal_or_opaque_path")
    return path


def _join(base: Path | None, relative: str) -> Path | None:
    return base / relative if base is not None else None


def _release_label(value: str) -> str | None:
    # OpenAI's Windows standalone installer appends the Rust target triple.
    return release_version(re.sub(r"-(?:x86_64|aarch64)-pc-windows-msvc$", "", value))


def _claude(inv: _Inventory, home: Path | None, roaming: Path | None) -> None:
    inv.executables("claude", "claude", _join(home, ".local/bin"), _names("claude"), "claude_native_bin")
    versions = _join(home, ".local/share/claude/versions")
    if versions is not None:
        for candidate in inv.probe.children(versions, "claude_native_versions"):
            version = release_version(candidate.name.removesuffix(".exe"))
            if version is None:
                continue
            status, info = inv.probe.inspect(candidate, "claude_native_versions")
            if info is not None and stat.S_ISDIR(info.st_mode):
                inv.executables("claude", "claude", candidate, _names("claude"),
                                "claude_native_versions", version=version)
            elif status in ("present", "redirected"):
                inv.runtime("claude", "claude", candidate, "claude_native_versions", version=version)
    if roaming is None:
        return
    npm = roaming / "npm"
    for arch in ("win32-x64", "win32-arm64"):
        package = "claude-code-" + arch
        for modules in (
            npm / "node_modules",
            npm / "node_modules" / "@anthropic-ai" / "claude-code" / "node_modules",
        ):
            root = modules / "@anthropic-ai" / package
            if inv.probe.inspect(root, "claude_npm", directory=True)[0] != "present":
                continue
            version = package_version(root, "@anthropic-ai/" + package, inv.probe, "claude_npm")
            for parent in (root, root / "bin"):
                inv.executables("claude", "claude", parent, ("claude.exe",), "claude_npm",
                                version=version, version_source="npm_package_manifest")
    inv.executables("claude", "claude", npm, ("claude.cmd", "claude.ps1", "claude"),
                    "claude_npm_shim", kind="command_wrapper")
    inv.executables("claude", "claude", npm / "node_modules/@anthropic-ai/claude-code/bin",
                    ("claude.exe",), "claude_npm")


def _codex_desktop(inv: _Inventory, local: Path | None, roaming: Path | None,
                   program_files: Path | None) -> None:
    bin_dir = _join(local, "OpenAI/Codex/bin")
    rows = inv.executables("codex-desktop", "codex", bin_dir, _names("codex"), "codex_desktop_bin")
    rows.extend(inv.versioned("codex-desktop", "codex", bin_dir, _names("codex"),
                              "codex_desktop_bundle", unversioned=True))
    if program_files is not None:
        for package in inv.probe.children(program_files / "WindowsApps", "codex_desktop_windowsapps"):
            if package.name.casefold().startswith("openai.codex_"):
                for relative in ("app/resources", "resources"):
                    rows.extend(inv.executables("codex-desktop", "codex", package / relative,
                                                _names("codex"), "codex_desktop_windowsapps"))
    inv.profile("codex-desktop", "codex", _join(roaming, "Codex"),
                "desktop_bundle_and_profile", rows, store_scope="frontend_profile")
    if local is not None:
        for package in inv.probe.children(local / "Packages", "codex_desktop_profile"):
            if package.name.casefold().startswith("openai.codex_"):
                inv.binding("codex-desktop", "codex", package, "desktop_package_profile",
                            store_scope="frontend_profile")


def _cindy(inv: _Inventory, roaming: Path | None) -> None:
    for brand in CINDY_PROFILE_NAMES:
        root = _join(roaming, brand)
        if root is not None:
            status, _ = inv.probe.inspect(root, "cindy_profile", directory=True)
            if status != "missing":
                inv.binding("cindy", "cindy", root, "cindy_profile", store_scope="frontend_profile")
            if status == "present":
                codex = [
                    row for path, version, layout in cindy_codex_candidates(root, inv.probe)
                    if (row := inv.runtime("cindy", "codex", path, layout, version=version)) is not None
                ]
                claude = inv.versioned("cindy", "claude", root / "claude-code", _names("claude"),
                                       "cindy_claude_code", subdirs=("", "bin"))
                pi = inv.versioned("cindy", "pi", root / "pi", _names("pi"), "cindy_pi")
                inv.profile("cindy", "codex", root / "codex-home", "cindy_codex_home", codex,
                            selection="layout_hint", profile=root,
                            configuration=_profile_config(inv.probe, root / "codex-home",
                                                          "config.toml", "cindy_codex_config"))
                inv.profile("cindy", "claude", root / "claude-home", "cindy_claude_home_candidate",
                            claude, profile=root)
                inv.profile("cindy", "pi", root / "pi-agent-home", "cindy_pi_agent_home", pi,
                            selection="layout_hint", profile=root,
                            configuration=_profile_config(inv.probe, root / "pi-agent-home",
                                                          "settings.json", "cindy_pi_config"))


def _t3_acp(inv: _Inventory, root: Path) -> list[dict]:
    base = root / "tools" / "antigravity-acp"
    layout = "t3_antigravity_acp"
    rows = inv.executables("t3code", "antigravity-acp", base, _ACP_NAMES, layout)
    for platform in _PLATFORM_DIRS:
        rows.extend(inv.versioned("t3code", "antigravity-acp", base / platform / "versions",
                                  _ACP_NAMES, layout, unversioned=True))
    for row in rows:
        path = Path(row["path"])
        helper = path.with_name("localharness_external.exe" if path.suffix == ".exe" else "localharness_external")
        status, _ = inv.probe.inspect(helper, layout, directory=False)
        row["companion"] = {"path": str(helper), "path_status": status}
        if row["path_status"] == "present" and status != "present":
            inv.finding(
                "managed_acp_helper_unavailable", "warning",
                "A T3 Antigravity ACP candidate lacks a verified companion executable.", [row["id"]],
            )
    return rows


def _t3_tools(inv: _Inventory, root: Path) -> None:
    tools = root / "tools" / "codex"
    codex = inv.executables("t3code", "codex", tools, _names("codex"), "t3code_tools")
    for candidate in (tools, *(tools / platform / "versions" for platform in _PLATFORM_DIRS)):
        codex.extend(inv.versioned("t3code", "codex", candidate, _names("codex"), "t3code_tools",
                                   subdirs=("", "bin"), label_parser=_release_label))
    if codex:
        inv.profile("t3code", "codex", None, "t3code_tools", codex, profile=root,
                    selection="layout_hint", store_scope="unknown")
    acp = _t3_acp(inv, root)
    inv.profile("t3code", "antigravity-acp", root / "userdata/providers/antigravity",
                "t3code_antigravity_profiles", acp, selection="layout_hint",
                store_scope="provider_profiles")


def _path_runtimes(inv: _Inventory, selected: Sequence[str], value: str,
                   known_roots: Sequence[tuple[str, Path | None]]) -> None:
    commands = [(client, name) for client in selected for name in _COMMANDS.get(client, ())]
    if not value or not commands:
        return
    if len(value) > _MAX_PATH_VALUE:
        inv.probe.issue("path", "path_value_budget_exceeded")
        return
    # Accept semicolon-separated paths on POSIX for portable Windows fixtures.
    entries = value.split(";" if ";" in value else os.pathsep)
    if len(entries) > _MAX_PATH_ENTRIES:
        inv.probe.issue("path", "path_entry_budget_exceeded")
    seen: set[str] = set()
    for index, raw in enumerate(entries[:_MAX_PATH_ENTRIES]):
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        directory = _root(inv.probe, raw, "path") if raw else None
        if directory is None or str(directory) in seen:
            continue
        seen.add(str(directory))
        # Do not relabel a managed install as standalone when filtered out.
        managed = next(
            (client for client, root in known_roots if root is not None
             and (directory == root or root in directory.parents)), None
        )
        if managed is not None and managed not in selected:
            continue
        if str(directory) in inv.alias_dirs:
            for row in inv.alias_dirs[str(directory)]:
                inv.runtime(row["client"], row["engine"], Path(row["path"]), "path", path_index=index)
            continue
        if inv.probe.inspect(directory, "path", directory=True)[0] != "present":
            continue
        for client, name in commands:
            for suffix in _SUFFIXES:
                inv.runtime(managed or client, client, directory / (name + suffix), "path",
                            path_index=index,
                            kind="command_wrapper" if suffix in (".cmd", ".bat", ".ps1") else "executable")


def _native_codex(inv: _Inventory, home: Path | None, local: Path | None, codex: Path | None,
                  visible: str | None) -> None:
    bin_dir = _root(inv.probe, visible, "CODEX_INSTALL_DIR", home=home) if visible else _join(
        local, "Programs/OpenAI/Codex/bin"
    )
    inv.executables("codex", "codex", _join(home, ".local/bin"), _names("codex"), "codex_native_bin")
    packages = _join(codex, "packages/standalone")
    current = _join(packages, "current")
    releases = _join(packages, "releases")
    release_rows = inv.versioned("codex", "codex", releases, _names("codex"),
                                 "codex_native_release", subdirs=("", "bin"), label_parser=_release_label)
    current_rows: dict[str, list[dict]] = defaultdict(list)
    if current is not None:
        redirected, target = inv.probe.direct_link_target(current, "codex_native_current")
        if redirected:
            if (target is not None and releases is not None and str(target.parent) == str(releases)
                    and _release_label(target.name) is not None):
                for row in release_rows:
                    path = Path(row["path"])
                    relative_parent = "" if str(path.parent) == str(target) else "bin"
                    if (str(path.parent) == str(target / relative_parent)
                            and row["path_status"] == "present"):
                        if row["selection"] == "unknown":
                            row["selection"] = "installer_pointer"
                        current_rows[relative_parent].append(inv.installer_alias(
                            current / relative_parent / path.name, row, "codex_installer_current_alias",
                        ))
            if not current_rows:
                inv.probe.issue("codex_native_current", "unverified_installer_pointer", current)
        else:
            for relative in ("", "bin"):
                current_rows[relative] = inv.executables(
                    "codex", "codex", current / relative, _names("codex"), "codex_native_current",
                )
    if bin_dir is not None:
        redirected, target = inv.probe.direct_link_target(bin_dir, "codex_native_bin")
        if not redirected:
            inv.executables("codex", "codex", bin_dir, _names("codex"), "codex_native_bin")
        else:
            # Only the installer's literal current and current/bin pointers
            # are accepted. We inspect the already proven plain release and
            # never make filesystem requests through the visible link.
            matched = False
            if target is not None and current is not None:
                for relative, rows in current_rows.items():
                    if str(target) == str(current / relative):
                        for row in rows:
                            if row["path_status"] == "present":
                                inv.installer_alias(bin_dir / Path(row["path"]).name, row,
                                                    "codex_installer_visible_alias")
                                matched = True
            if not matched:
                inv.probe.issue("codex_native_bin", "unverified_installer_pointer", bin_dir)

def _frontend(inv: _Inventory, client: str, root: Path | None, local: Path | None,
              roaming: Path | None) -> None:
    desktop_name = "Paseo" if client == "paseo" else "T3 Code"
    inv.executables(client, client, _join(local, "Programs/" + desktop_name),
                    (desktop_name + ".exe",), client + "_desktop")
    rows = [row for row in inv.runtimes.values() if row["client"] == client and row["engine"] == client]
    if root is not None:
        hints_reader = paseo_runtime_hints if client == "paseo" else t3_runtime_hints
        config, hints = hints_reader(root, inv.probe)
        inv.profile(client, client, root, client + "_profile", rows,
                    configuration=config, store_scope="frontend_profile")
        inv.configure(client, root, config, hints)
    desktop_profile = _join(roaming, desktop_name)
    if desktop_profile is not None:
        inv.binding(client, client, desktop_profile, client + "_desktop_profile",
                    store_scope="frontend_profile")


def collect_environment_inventory(
    *, user_home: Path | None = None, appdata: Path | None = None,
    local_appdata: Path | None = None, path_value: str | None = None,
    clients: Sequence[str] | None = None, environ: Mapping[str, str] | None = None,
    process_executables: Sequence[str | Path] | None = None,
    drive_is_local: Callable[[Path], bool] | None = None,
) -> dict:
    """Report local candidates without launching tools or reading record stores.

    An injected environment (including an empty mapping) replaces os.environ.
    An explicit home without an environment uses its own profile roots instead
    of the host user's application and config roots. PATH is independent.
    Optional process paths match only exact observed candidates.
    """
    requested = tuple(clients) if clients is not None else _CLIENTS
    if any(client not in ENVIRONMENT_CLIENT_CHOICES for client in requested):
        raise ValueError("Unknown environment client selector")
    selected = tuple(
        client for client in _CLIENTS
        if client in requested or ("antigravity" in requested and client.startswith("antigravity-"))
    )
    env = os.environ if environ is None else environ
    root_env = env if user_home is None or environ is not None else {}
    probe = BoundedPathProbe(drive_is_local=drive_is_local)
    home_value = user_home
    if home_value is None:
        if environ is None:
            home_value = Path.home()
        else:
            home_value = env.get("USERPROFILE" if os.name == "nt" else "HOME")
            if not home_value:
                probe.issue("user_home", "home_not_supplied")
    home = _root(probe, home_value, "user_home")
    roaming = _root(probe, appdata if appdata is not None else (
        root_env.get("APPDATA") or _join(home, "AppData/Roaming")
    ), "appdata")
    local = _root(probe, local_appdata if local_appdata is not None else (
        root_env.get("LOCALAPPDATA") or _join(home, "AppData/Local")
    ), "local_appdata")
    program_files = _root(probe, root_env.get("ProgramFiles"), "program_files")
    process_paths: set[str] = set()
    if process_executables is not None:
        if len(process_executables) > _MAX_PROCESS_PATHS:
            probe.issue("process_observations", "process_path_budget_exceeded")
        for value in process_executables[:_MAX_PROCESS_PATHS]:
            if (path := _root(probe, value, "process_observations")) is not None:
                process_paths.add(str(path))
    inv = _Inventory(probe, process_paths)

    def effective(key: str, default: str) -> tuple[Path | None, str]:
        value = root_env.get(key)
        if value:
            return _root(probe, value, key, home=home), "environment_root"
        return _join(home, default), "default_root"

    if "codex-desktop" in selected:
        _codex_desktop(inv, local, roaming, program_files)
    if "cindy" in selected:
        _cindy(inv, roaming)
    t3, _ = effective("T3CODE_HOME", ".t3") if "t3code" in selected else (None, "")
    if t3 is not None:
        _t3_tools(inv, t3)
    codex, codex_via = effective("CODEX_HOME", ".codex") if "codex" in selected else (None, "")
    if "codex" in selected:
        _native_codex(inv, home, local, codex, root_env.get("CODEX_INSTALL_DIR"))
    if "claude" in selected:
        _claude(inv, home, roaming)
    if "antigravity-cli" in selected:
        for base in (_join(local, "agy/bin"), _join(home, ".local/bin"),
                     _join(program_files, "Google/antigravity-cli")):
            inv.executables("antigravity-cli", "antigravity-cli", base, _names("agy"),
                            "antigravity_cli_install")
    if "antigravity-desktop" in selected:
        desktop = inv.executables("antigravity-desktop", "antigravity-desktop",
                                  _join(local, "Programs/antigravity"), ("Antigravity.exe",),
                                  "antigravity_desktop_install")
        inv.profile("antigravity-desktop", "antigravity-desktop", _join(roaming, "Antigravity"),
                    "antigravity_desktop_profile", desktop, store_scope="frontend_profile")
    managed = [
        ("codex-desktop", _join(local, "OpenAI/Codex/bin")),
        ("t3code", _join(t3 or _join(home, ".t3"), "tools")),
        *(("cindy", _join(roaming, brand)) for brand in CINDY_PROFILE_NAMES),
    ]
    _path_runtimes(inv, selected, env.get("PATH", "") if path_value is None else path_value, managed)

    standalone_profiles = {
        "codex": (codex, codex_via, "config.toml"),
        "antigravity-cli": (_join(home, ".gemini/antigravity-cli"), "default_root", "settings.json"),
    }
    if "claude" in selected:
        standalone_profiles["claude"] = (*effective("CLAUDE_CONFIG_DIR", ".claude"), "settings.json")
    if "pi" in selected:
        standalone_profiles["pi"] = (*effective("PI_CODING_AGENT_DIR", ".pi/agent"), "settings.json")
    for client, (store, via, filename) in standalone_profiles.items():
        if client in selected:
            rows = [row for row in inv.runtimes.values() if row["client"] == client and row["engine"] == client]
            inv.profile(client, client, store, via, rows,
                        configuration=_profile_config(probe, store, filename, via))
    if "pi" in selected and (override := root_env.get("PI_CODING_AGENT_SESSION_DIR")):
        session_root = _root(probe, override, "PI_CODING_AGENT_SESSION_DIR", home=home)
        inv.binding("pi", "pi", session_root, "environment_session_root", store_scope="session_directory")
    if "t3code" in selected:
        _frontend(inv, "t3code", t3, local, roaming)
    if "paseo" in selected:
        paseo, _ = effective("PASEO_HOME", ".paseo")
        _frontend(inv, "paseo", paseo, local, roaming)

    inv.finish_findings(selected, explicit=clients is not None)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "roots": {name: str(path) if path is not None else None for name, path in (
            ("user_home", home), ("appdata", roaming), ("local_appdata", local),
        )},
        "runtimes": sorted(inv.runtimes.values(), key=lambda row: (row["client"], row["engine"], row["path"])),
        "bindings": sorted(inv.bindings.values(), key=lambda row: (row["client"], row["engine"], row["id"])),
        "findings": inv.findings,
        "coverage": {
            "status": "partial" if probe.issues else "bounded",
            "clients": list(selected),
            "probes": {
                "path_metadata": probe.count,
                "process_observations_supplied": process_executables is not None,
                "max_path_probes": MAX_PATH_PROBES,
                "max_path_entries": _MAX_PATH_ENTRIES,
                "max_directory_entries": MAX_DIRECTORY_ENTRIES,
            },
            "issues": probe.issues,
            "limitations": [
                "Known local layouts and absolute PATH entries only; unverified redirects and remote paths are skipped.",
                "No binaries run, signatures checked, or live processes enumerated.",
                "PATH order and filesystem presence do not prove launch selection, version, or usability.",
                "Configured paths are observations of known built-in provider fields only; effective settings may differ.",
                "File identities cover observed paths only; equal content, sign-in, and record identity are not inferred.",
                "Authentication files, record databases, transcripts, and environment secrets are not read.",
            ],
        },
    }
