"""Prove the owner of a device-hosted native session using Cindy's launch metadata."""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys

from . import frozen_files
from .path_identity import is_local_absolute_locator
from .operation_store import atomic_write_json, strict_json_load
from .providers.t3 import strict_json

BACKUP_HEADER_BYTES = 16 * 1024
BACKUP_BODY_BYTES = 16 * 1024 * 1024
HOST_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-a[0-9a-f]{3}-[0-9a-f]{12}")


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def locator_key(value):
    # Pi's Windows launcher also returns forward-slash paths. Compare only
    # separators; never resolve a foreign path on the controller.
    if isinstance(value, str) and re.match(r"^[A-Za-z]:[\\/]", value):
        return value.replace("/", "\\")
    return value


def locator_matches(value, expected):
    return locator_key(value) == locator_key(expected)


def possible_reference(value, expected):
    if locator_matches(value, expected):
        return True
    return (isinstance(value, str) and isinstance(expected, str)
            and bool(re.match(r"^[A-Za-z]:[\\/]", expected))
            and ntpath.normcase(ntpath.normpath(value)) == ntpath.normcase(ntpath.normpath(expected)))


def host_session_id(peer, session_id):
    h = _hash(peer + "\0" + session_id)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def _hardware_id():
    # node-machine-id uses the native registry view, DBus before /etc, and
    # IOPlatformUUID on macOS. An unavailable physical ID cannot bind a task.
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value = winreg.QueryValueEx(key, "MachineGuid")[0]
    elif sys.platform.startswith("linux"):
        for path in (Path("/var/lib/dbus/machine-id"), Path("/etc/machine-id")):
            try:
                value = path.read_text(encoding="ascii").partition("\n")[0]
                break
            except FileNotFoundError:
                continue
        else:
            raise ValueError("cindy_physical_device_id_unavailable")
    elif sys.platform == "darwin":
        import plistlib
        output = subprocess.check_output(["/usr/sbin/ioreg", "-ard1", "-c", "IOPlatformExpertDevice"], timeout=10)
        value = plistlib.loads(output)[0]["IOPlatformUUID"]
    else:
        raise ValueError("cindy_physical_device_id_unavailable")
    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", value.strip()):
        raise ValueError("cindy_physical_device_id_invalid")
    return _hash(value.strip().lower())


def device_id(profile):
    if os.environ.get("XDT_DEVICE_ID_OVERRIDE"):
        raise ValueError("cindy_device_override_unverified")
    physical = _hardware_id()
    for name in ("device-identity.json", "device-identity.json.bak"):
        try:
            _, raw = frozen_files.read_file(profile, name, content=True, limit=65536)
        except FileNotFoundError:
            continue
        record = strict_json(raw.decode("utf-8"))
        if type(record.get("version")) is not int or record["version"] != 1:
            raise ValueError("cindy_device_identity_invalid")
        if record.get("replaces") != physical:
            return physical
        if not re.fullmatch(r"[0-9a-f]{64}", record.get("deviceId", "")):
            raise ValueError("cindy_device_identity_invalid")
        return record["deviceId"]
    return physical


def guest_index(profile):
    try:
        _, raw = frozen_files.read_file(profile, "remote-agent/guest-sessions.json", content=True,
                                        limit=frozen_files.MAX_REWRITE_BYTES)
    except FileNotFoundError:
        raw = b"{}"
    index = strict_json(raw.decode("utf-8"))
    if not isinstance(index, dict):
        raise ValueError("cindy_guest_index_invalid")
    return index


def guest_identity(peer):
    parts = strict_json(peer.rstrip("~"))
    if (not isinstance(parts, list) or len(parts) != 5 or parts[0] != "provider-share" or parts[2] != "guest"
            or not isinstance(parts[4], str) or not re.fullmatch(r"[a-f0-9]{64}", parts[4])
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", v)
                   for v in (parts[1], parts[3]))
            or json.dumps(parts, separators=(",", ":")).ljust(129, "~") != peer):
        raise ValueError("cindy_guest_controller_mismatch")
    return parts[1], parts[4]


def _peer(profile, controller, sid, native_id, retired_key=None, *, recorded_host=None):
    candidates = []
    for key, row in guest_index(profile).items():
        peer = row["controller"]
        relay = row.get("relay")
        scope = peer + ("\0relay:" + relay if relay else "")
        if key != _hash(scope)[:32]:
            raise ValueError("cindy_guest_index_invalid")
        hosts, natives = row.get("hostSessionIds", []), row.get("nativeIds", [])
        if any(not isinstance(v, list) or any(not isinstance(x, str) or not x for x in v) for v in (hosts, natives)):
            raise ValueError("cindy_guest_index_invalid")
        host_id = recorded_host or host_session_id(scope, sid)
        forks = row.get("forkWorkspaces", {})
        if (any(possible_reference(v, native_id) for v in forks) or
                any(host_id in values or sid and host_session_id(controller, sid) in values for values in forks.values())
                or any(possible_reference(v, native_id) for v in row.get("members", {}))):
            raise ValueError("cindy_hosted_fork_or_relay_reference")
        has_native = any(locator_matches(v, native_id) for v in natives)
        if not has_native and any(possible_reference(v, native_id) for v in natives):
            raise ValueError("cindy_guest_session_ownership_ambiguous")
        retired = key == retired_key and host_id not in hosts and not has_native
        if not retired and not has_native and host_id not in hosts:
            continue
        if (relay or not (retired or host_id in hosts and has_native)
                or len(set(hosts)) != len(hosts) or len(set(natives)) != len(natives)
                or row.get("members") or row.get("forkWorkspaces")
                or row.get("relayDevices") or row.get("forgetPending") or row.get("directOwnerDeviceId")
                or set(row) - {"controller", "hostSessionIds", "nativeIds", "attachmentRunIds", "members", "forkWorkspaces",
                                "relayDevices", "forgetPending", "cleanupEpoch"} or not peer.startswith('["provider-share",')):
            raise ValueError("cindy_guest_session_ownership_ambiguous")
        share_id, guest_device = guest_identity(peer)
        if guest_device != controller:
            raise ValueError("cindy_guest_controller_mismatch")
        candidates.append((peer, "share:" + share_id, row))
    if len(candidates) > 1:
        raise ValueError("cindy_guest_session_ownership_ambiguous")
    return candidates[0] if candidates else (controller, None, None)


def _guest_bindings(profile, owner, workspaces, row, *, approved, selected, locator):
    """Match each native history to its shadow, independently of either set's order."""
    from .remote_records import _records
    hosts, natives = set(row["hostSessionIds"]), row["nativeIds"]
    if len(hosts) != len(natives):
        raise ValueError("cindy_guest_session_ownership_ambiguous")
    root = Path(owner["root"])
    records = _records(owner["client"], root, agent_dir=owner.get("agent_dir"),
                       hosted_locators=natives)
    by_locator = {}
    for record in records:
        by_locator.setdefault(locator_key(record.get("path", record["record_id"])), []).append(record)
    bindings = {}
    for native in natives:
        if owner["client"] == "claude" and _claude_home(profile, native)[0] != root:
            raise ValueError("cindy_guest_native_home_unverified")
        matches = by_locator.get(locator_key(native), ())
        if not matches and approved and locator_matches(native, locator):
            # Only the selected transcript can disappear between native completion
            # and the index checkpoint. The native receipt is checked by the caller.
            host = selected
        else:
            if len(matches) != 1:
                raise ValueError("cindy_guest_native_history_unverified")
            owners = set()
            for cwd in matches[0]["cwds"]:
                if not isinstance(cwd, str) or not is_local_absolute_locator(cwd) or Path(cwd) != Path(os.path.abspath(cwd)):
                    raise ValueError("cindy_guest_native_history_unverified")
                parts = Path(cwd).relative_to(workspaces).parts
                if len(parts) < 2 or parts[0] not in hosts or parts[1] != "fs":
                    raise ValueError("cindy_guest_native_history_unverified")
                owners.add(parts[0])
            if len(owners) != 1:
                raise ValueError("cindy_guest_native_history_unverified")
            host, = owners
        if host in bindings:
            raise ValueError("cindy_guest_session_ownership_ambiguous")
        bindings[host] = native
    if set(bindings) != hosts or not locator_matches(bindings.get(selected), locator):
        raise ValueError("cindy_guest_session_ownership_ambiguous")
    return bindings


def _owner_directories(profile, directory, pattern):
    root = frozen_files.checked_path(profile, directory)
    try:
        frozen_files._plain(root.lstat(), directory=True)
    except FileNotFoundError:
        return
    for entry in root.iterdir():
        if re.fullmatch(pattern, entry.name):
            frozen_files._plain(entry.lstat(), directory=True)
            yield entry.name


def _codex_location(profile, home, native, rollouts, retired):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", native):
        raise ValueError("cindy_codex_thread_id_invalid")
    matches = []
    for account in _owner_directories(profile, "codex-accounts", r"[a-f0-9]{64}"):
        relative = f"codex-accounts/{account}/thread-index/locations/{native}.json"
        try:
            _, raw = frozen_files.read_file(profile, relative, content=True, limit=65536)
        except FileNotFoundError:
            continue
        loc = strict_json(raw.decode("utf-8"))
        rollout = Path(loc["path"])
        history = next((p.parent for p in rollout.parents if p.name in {"sessions", "archived_sessions"}), None)
        if (loc.get("threadId") != native or set(loc) - {"threadId", "path", "sqliteHome"}
                or not is_local_absolute_locator(str(rollout)) or rollout != Path(os.path.abspath(rollout))
                or history != home or Path(loc.get("sqliteHome", home)) != home
                or rollouts is not None and [Path(p) for p in rollouts] != [rollout]):
            raise ValueError("cindy_codex_thread_location_mismatch")
        matches.append((relative, str(rollout)))
    if retired and not matches:
        return retired["thread_location"], retired["rollout_path"]
    if len(matches) != 1:
        raise ValueError("cindy_codex_unique_thread_location_required")
    return matches[0]


def _claude_home(profile, native):
    homes = []
    for account in _owner_directories(profile, "owners", r"[a-f0-9]{20}"):
        try:
            _, raw = frozen_files.read_file(profile, f"owners/{account}/subscription-accounts.json",
                                            content=True, limit=8 * 1024 * 1024)
        except FileNotFoundError:
            continue
        bindings = strict_json(raw.decode("utf-8"))["bindings"]
        record = bindings.get("anthropic:native:" + native, bindings.get("native:" + native))
        if record is None or record.get("providerId") != "anthropic":
            continue
        identifier = record["accountId"]
        if identifier == "default":
            homes.append((Path.home() / ".claude", account))
        elif re.fullmatch(r"cindy-" + account[:12] + r"-[A-Za-z0-9_-]{1,100}", identifier):
            homes.append((Path.home() / (".claude-" + identifier), account))
        else:
            raise ValueError("cindy_claude_native_account_unverified")
    if len(homes) > 1:
        raise ValueError("cindy_claude_native_account_ambiguous")
    return homes[0] if homes else (Path.home() / ".claude", None)


def matches_shadow(record, shadow):
    cwds = record.get("cwds", ())
    return bool(cwds) and all(isinstance(v, str) and is_local_absolute_locator(v)
                              and Path(v) == Path(os.path.abspath(v)) and Path(v).is_relative_to(shadow) for v in cwds)


def observe(request, owner, native_locator, *, record=None, approved=None, retired=None):
    """Only the executor interprets executor paths. Shadows may be gone after close."""
    from .remote_records import directory_identity
    profile, sid, controller = Path(request["profile"]), request.get("task_id"), request["controller_device"]
    profile_id = directory_identity(profile)
    device = device_id(profile)
    if device == controller:
        raise ValueError("cindy_execution_device_not_distinct")
    recorded = request.get("host_session_id")
    if recorded is not None and (sid is not None or not HOST_SESSION_ID.fullmatch(recorded)):
        raise ValueError("cindy_hosted_session_id_invalid")
    peer, share, row = _peer(profile, controller, sid, native_locator, (retired or {}).get("guest_key"), recorded_host=recorded)
    host_id = recorded or host_session_id(peer, sid)
    peer_dir = _hash(peer)[:16]
    shadow = profile / "remote-agent" / "workspaces" / peer_dir / host_id / "fs"
    if record is not None and not matches_shadow(record, shadow):
        raise ValueError("cindy_native_shadow_workspace_mismatch")
    result = {**request, "profile_id": profile_id, "host_device": device, "execution_host": share or "device:" + device,
              "host_session_id": host_id, "workspace_root": str(shadow)}
    guest_home = profile / "remote-agent" / "guest-homes" / peer_dir
    root = Path(owner["root"])
    if share:
        result["guest_key"] = _hash(peer)[:32]
        if retired and "guest_bindings" in retired:
            result.update(guest_peer=peer, guest_bindings=retired["guest_bindings"])
        elif not retired and (len(row["hostSessionIds"]) > 1 or len(row["nativeIds"]) > 1):
            result.update(guest_peer=peer, guest_bindings=_guest_bindings(profile, owner, shadow.parent.parent, row,
                approved=approved, selected=host_id, locator=native_locator))
    if owner["client"] == "pi":
        agent = profile / "pi-agent-home"
        if Path(owner["agent_dir"]) != agent or root != (guest_home / "pi-sessions" if share else agent / "sessions"):
            raise ValueError("cindy_pi_native_store_mismatch")
    elif owner["client"] == "claude":
        home, account = _claude_home(profile, native_locator)
        if root != home:
            raise ValueError("cindy_claude_native_store_mismatch")
        if account is not None:
            result["account_scope"] = account
    elif share:
        if root != guest_home / "codex":
            raise ValueError("cindy_guest_native_store_mismatch")
    else:
        relative, result["rollout_path"] = _codex_location(profile, root, native_locator,
            record["rollouts"] if record is not None else None, retired)
        result.update(thread_location=relative, account_scope=relative.split("/")[1])
    return result


def guest_transform(raw, hosted, native):
    index = strict_json(raw.decode("utf-8"))
    row = index[hosted["guest_key"]]
    natives = row.get("nativeIds", [])
    hosts = row.get("hostSessionIds", [])
    bindings = hosted.get("guest_bindings", {hosted["host_session_id"]: native})
    if (set(hosts) != bindings.keys() or len(hosts) != len(bindings) or len(natives) != len(bindings)
            or len(set(map(locator_key, natives))) != len(natives)
            or set(map(locator_key, natives)) != set(map(locator_key, bindings.values()))):
        raise ValueError("cindy_guest_session_ownership_changed")
    row["hostSessionIds"] = [h for h in hosts if h != hosted["host_session_id"]]
    row["nativeIds"] = [n for n in natives if not locator_matches(n, native)]
    return json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def index_change(hosted, native):
    root = Path(hosted["profile"])
    if "thread_location" in hosted:
        relative = hosted["thread_location"]
        file, raw = frozen_files.read_file(root, relative, content=True, limit=65536)
        location = strict_json(raw.decode("utf-8"))
        if location.get("threadId") != native or Path(location["path"]) != Path(hosted["rollout_path"]):
            raise ValueError("cindy_codex_thread_location_mismatch")
        return {"kind": "remove", "path": relative, "files": [file], "directories": []}
    return frozen_files.freeze_rewrite(root, "remote-agent/guest-sessions.json",
        lambda raw: guest_transform(raw, hosted, native))


def index_phase(path, approval):
    try:
        frozen_files._plain(path.lstat())
    except FileNotFoundError:
        return "pending"
    mark = strict_json_load(path)
    if mark.get("approval") != approval or mark.get("phase") not in {"started", "complete"}:
        raise ValueError("cindy_host_index_receipt_invalid")
    return mark["phase"]


def index_settled(path, approval, change, hosted, native):
    phase = index_phase(path, approval)
    if phase == "pending":
        return False
    root = Path(hosted["profile"])
    if phase == "started" or "thread_location" in hosted:
        return frozen_files.satisfied(root, change)
    # A later task may use this same grant. Keep its entries, but an old
    # task or native handle reappearing invalidates the original verification.
    _, raw = frozen_files.read_file(root, change["before"]["path"], content=True, limit=frozen_files.MAX_REWRITE_BYTES)
    index = strict_json(raw.decode("utf-8"))
    return all(hosted["host_session_id"] not in row.get("hostSessionIds", [])
               and not any(possible_reference(n, native) for n in row.get("nativeIds", [])) for row in index.values())


def finish_index(path, approval, change, hosted, native):
    from .codex_desktop_state import running_related_clients
    from .frontend_session_cleanup import _require_client_closed
    from .mutation_guard import mutation_roots
    root = Path(hosted["profile"])
    with mutation_roots((root,)):
        _require_client_closed(root, running_related_clients, owner_client="cindy")
        if index_settled(path, approval, change, hosted, native):
            if index_phase(path, approval) == "started":
                atomic_write_json(path, {"approval": approval, "phase": "complete"})
            return
        if index_phase(path, approval) == "complete" or index_change(hosted, native) != change:
            raise ValueError("cindy_host_index_changed_or_interrupted")
        atomic_write_json(path, {"approval": approval, "phase": "started"})
        if "thread_location" in hosted:
            frozen_files.apply_remove(root, change)
        else:
            frozen_files.apply_rewrite(root, change, lambda raw: guest_transform(raw, hosted, native))
        if not frozen_files.satisfied(root, change):
            raise ValueError("cindy_host_index_after_state_unverified")
        atomic_write_json(path, {"approval": approval, "phase": "complete"})


def controller_account(proof):
    database, profile = Path(proof["database"]), Path(proof["profile_root"])
    match = re.fullmatch(r"(?:cindy|xdt-maker)-(.+)\.db", database.name)
    if not match or match[1] == "local-v1" or database.parent != profile:
        raise ValueError("cindy_controller_account_unverified")
    return _hash(match[1])


def backup(proof):
    """Bind a single published Pi checkpoint to its controller account, task and device."""
    hosted = proof.get("hosted")
    if hosted:
        host, engine = hosted["execution_host"], proof["verify_request"]["client"]
    else:
        host, engine = proof.get("execution_host", ""), proof.get("engine")
    if not host.startswith("device:") or engine != "pi":
        return None
    profile = Path(proof["profile_root"])
    relative = f"owners/{controller_account(proof)[:20]}/native-history-backups/{_hash(proof['session_id'])}.backup"
    try:
        file, raw = frozen_files.read_file(profile, relative, content=True, limit=BACKUP_BODY_BYTES + BACKUP_HEADER_BYTES)
    except FileNotFoundError:
        return {"path": relative, "absent": True}
    header, sep, body = raw.partition(b"\n")
    if not sep or len(header) >= BACKUP_HEADER_BYTES:
        raise ValueError("cindy_native_history_backup_invalid")
    meta = strict_json(header.decode("utf-8"))
    manifest, revision = meta["manifest"], meta["revision"]
    if (type(meta.get("version")) is not int or meta["version"] != 1 or meta.get("sessionId") != proof["session_id"]
            or not isinstance(meta.get("anchor"), str) or not meta["anchor"] or type(meta.get("savedAt")) is not int
            or not isinstance(revision.get("nativeId"), str) or not revision["nativeId"]
            or not locator_matches(revision["nativeId"], proof["native_id"]) or revision.get("fromDevice") != host.removeprefix("device:")
            or manifest.get("nativeId") != revision.get("nativeId") or manifest.get("kind") != "pi" or "state" in manifest
            or type(manifest.get("bytes")) is not int or not 0 < len(body) == manifest["bytes"] <= BACKUP_BODY_BYTES
            or manifest.get("sha256") != hashlib.sha256(body).hexdigest()):
        raise ValueError("cindy_native_history_backup_mismatch")
    return {"kind": "remove", "path": relative, "files": [file], "directories": []}
