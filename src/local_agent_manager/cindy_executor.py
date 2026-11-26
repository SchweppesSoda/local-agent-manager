"""Resolve an offline controller's task using only the Cindy executor's stores."""
from pathlib import Path
import re

from . import cindy_hosted as hosted, frozen_files
from .cindy_references import build_cindy_reference_catalog
from .discovery import resolve_cindy_profiles
from .providers.t3 import strict_json


DEVICE_ID = re.compile(r"[0-9a-f]{64}")


def _known_devices(profile):
    # Cindy caches these IDs for display when devices are offline. Only a native
    # working directory matching the derived shadow can establish ownership.
    try:
        _, raw = frozen_files.read_file(profile, "device-link-settings.json", content=True,
                                        limit=frozen_files.MAX_REWRITE_BYTES)
    except FileNotFoundError:
        return ()
    names = strict_json(raw.decode("utf-8")).get("lastKnownDeviceNames", {})
    if not isinstance(names, dict):
        raise ValueError("cindy_device_cache_invalid")
    return tuple(key for key in names if DEVICE_ID.fullmatch(key))


def _recorded_host(record, workspaces):
    ids = {Path(cwd).relative_to(workspaces).parts[0] for cwd in record["cwds"]
           if cwd and Path(cwd).is_relative_to(workspaces) and Path(cwd) != workspaces}
    if len(ids) == 1:
        value, = ids
        if hosted.HOST_SESSION_ID.fullmatch(value):
            return value
    return None


def records_in_shadow(rows, shadow):
    matches = []
    for row in rows:
        if any(cwd and Path(cwd).is_relative_to(shadow) for cwd in row["cwds"]):
            if not hosted.matches_shadow(row, shadow):
                raise ValueError("cindy_native_shadow_workspace_mismatch")
            matches.append(row)
    return matches


def select(profile, owner, *, task_id=None, record_id=None, controller_device=None):
    """Return the native ID and the input needed to recheck the same physical host task."""
    from .remote_records import _records, directory_identity

    directory_identity(profile)
    if not task_id and not record_id or task_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        raise ValueError("cindy_executor_exact_task_or_native_id_required")
    if controller_device is not None and not DEVICE_ID.fullmatch(controller_device):
        raise ValueError("cindy_executor_invalid_controller_device")
    # Include unindexed histories too. Another native ID using the selected
    # workspace makes attribution ambiguous even when the guest index omits it.
    rows = _records(owner["client"], Path(owner["root"]), agent_dir=owner.get("agent_dir"), all_workspaces=True)
    record = None
    locator = None
    if record_id:
        matches = [r for r in rows if r["record_id"] == record_id]
        if len(matches) != 1:
            raise ValueError("cindy_executor_unique_native_record_required")
        record, = matches
        locator = record.get("path", record_id)

    guests = []
    for row in hosted.guest_index(profile).values():
        peer = row["controller"]
        scope = peer + ("\0relay:" + row["relay"] if row.get("relay") else "")
        if (task_id and hosted.host_session_id(scope, task_id) in row.get("hostSessionIds", ())
                or locator and any(hosted.possible_reference(v, locator) for v in row.get("nativeIds", ()))):
            if row.get("relay") or not peer.startswith('["provider-share",'):
                raise ValueError("cindy_executor_direct_guest_required")
            guests.append(row)
    if len(guests) > 1:
        raise ValueError("cindy_guest_session_ownership_ambiguous")
    if guests:
        peer = guests[0]["controller"]
        _, guest_device = hosted.guest_identity(peer)
        if controller_device is not None and controller_device != guest_device:
            raise ValueError("cindy_guest_controller_mismatch")
        peers = [(peer, guest_device)]
    else:
        devices = (controller_device,) if controller_device else _known_devices(profile)
        if not devices:
            raise ValueError("cindy_executor_known_controller_device_required")
        peers = [(device, device) for device in devices]

    matches = []
    for peer, device in peers:
        workspaces = profile / "remote-agent" / "workspaces" / hosted._hash(peer)[:16]
        host_id = hosted.host_session_id(peer, task_id) if task_id else _recorded_host(record, workspaces)
        if not host_id or guests and host_id not in guests[0]["hostSessionIds"]:
            continue
        for row in records_in_shadow(rows, workspaces / host_id / "fs"):
            matches.append((row["record_id"], device, host_id))
    if len(matches) != 1 or record_id is not None and matches[0][0] != record_id:
        raise ValueError("cindy_executor_unique_native_workspace_required")
    selected, device, host_id = matches[0]
    claim = {"profile": str(profile), "controller_device": device}
    if task_id:
        claim["task_id"] = task_id
    else:
        claim["host_session_id"] = host_id
    return selected, claim


def guard_local_references(hosted_task, owner, record_id, locator):
    """A task in the executor's own Cindy databases may still reference this history."""
    profile = Path(hosted_task["profile"])
    engine = "codex" if owner["client"] == "native" else owner["client"]
    for namespace in resolve_cindy_profiles(root=profile):
        catalog = build_cindy_reference_catalog(namespace.database, profile_root=profile)
        if catalog.failures:
            raise ValueError("cindy_executor_reference_inventory_incomplete")
        for ref in catalog.references:
            if ref.backend != engine:
                continue
            device = ref.execution_host.removeprefix("device:")
            if ref.execution_host.startswith("device:") and DEVICE_ID.fullmatch(device) and device != hosted_task["host_device"]:
                continue
            if any(value and (hosted.possible_reference(value, locator) or value == record_id)
                   for value in (ref.native_session_id, ref.remote_native_locator)):
                raise ValueError("cindy_executor_native_handle_referenced")
