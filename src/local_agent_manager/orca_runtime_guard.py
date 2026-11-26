"""Private writer census for an approved Orca userData closure."""
from pathlib import Path
import ctypes
from datetime import datetime
import os
import re

from . import frozen_files
from .orca_journal_cleanup import decode, fail, integer, text


def instance_lock(root):
    try:
        observed, raw = frozen_files.read_file(root, "orcad.lock", content=True, limit=64 * 1024)
    except FileNotFoundError:
        return None
    lock = decode(raw)
    required = {"pid", "startedAtMs", "identity", "version", "acquiredAt", "nonce"}
    if (not isinstance(lock, dict) or not required <= lock.keys()
            or not integer(lock["pid"], 1)
            or any(not text(lock[k]) for k in ("identity", "version", "acquiredAt", "nonce"))
            or "role" in lock and lock["role"] not in ("desktop", "orcad")
            or len(lock["identity"]) > 1024
            or any(not text(lock[k]) or len(lock[k]) > 255 for k in ("nonce", "version", "machineIdentity", "host", "bootId") if k in lock)
            or "linuxStartTicks" in lock and not (isinstance(lock["linuxStartTicks"], str)
                and re.fullmatch(r"[0-9]{1,32}", lock["linuxStartTicks"]))):
        fail("runtime_metadata_unverified")
    start = lock["startedAtMs"]
    if start is not None and (type(start) not in (float, int) or not 0 <= start < float("inf")):
        fail("runtime_metadata_unverified")
    timestamp = lock["acquiredAt"]
    try:
        acquired = datetime.fromisoformat(timestamp.removesuffix("Z") + "+00:00" if timestamp.endswith("Z") else timestamp)
    except ValueError:
        fail("runtime_metadata_unverified")
    if acquired.tzinfo is None or "T" not in timestamp:
        fail("runtime_metadata_unverified")
    return observed, lock


def lock_evidence(current):
    if current is None:
        return {"path": "orcad.lock", "absent": True}
    observed, lock = current
    fields = {"role": lock["role"], "version": lock["version"]} if "role" in lock else {}
    return {**observed, "pid": lock["pid"], **fields}


def runtime_observation(root):
    """Metadata from the published instance lock. Liveness is checked separately."""
    try:
        current = instance_lock(root)
    except (OSError, RuntimeError, ValueError):
        return {"lock_state": "unverified", "role": None, "version": None}
    if current is None:
        return {"lock_state": "absent", "role": None, "version": None}
    lock = current[1]
    return {"lock_state": "recorded", "role": lock.get("role"), "version": lock["version"]}


def check_lock_host(root, lock):
    # The Windows writer may judge a recorded PID only on the machine that
    # published it. Do not expose the machine identifier in plans or reports.
    if "bootId" in lock or "linuxStartTicks" in lock or os.name != "nt":
        fail("instance_lock_machine_unverified")
    if "machineIdentity" in lock:
        from .orca_runtime import windows_machine_guid
        guid = windows_machine_guid().strip().lower()
        if (not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", guid)
                or guid == "00000000-0000-0000-0000-000000000000" or lock["machineIdentity"] != "win32-machine-guid:" + guid):
            fail("instance_lock_machine_unverified")
    # A folder on a mapped network drive looks like a local Windows path.
    # Only local disks can establish locality for a legacy lock without a machine ID.
    if ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) not in (2, 3, 6):
        fail("instance_lock_machine_unverified")


def related(rows, root):
    from .office_runtime import _RUNTIMES
    owned = set()
    normalized = str(root).replace("\\", "/").casefold()
    for row in rows:
        if type(row.get("pid")) is not int or type(row.get("parent")) is not int:
            fail("writer_census_unverified")
        if row["pid"] == os.getpid():
            continue
        name = str(row.get("name") or "").casefold()
        exe = str(row.get("executable") or "").replace("\\", "/").casefold()
        runtime = name in _RUNTIMES or exe.rsplit("/", 1)[-1] in _RUNTIMES
        if runtime and not isinstance(row.get("command"), str):
            fail("writer_census_unverified")
        command = str(row.get("command") or "").replace("\\", "/").casefold()
        branded = name in {"orca", "orca.exe", "orcad", "orcad.exe", "orca-daemon", "orca-daemon.exe"}
        branded |= bool(re.search(r"/(?:orca(?:\.app)?|orca-dev)/", exe))
        branded |= runtime and (normalized in command or bool(re.search(
            r"""(?:^|[/\s"'])orcad(?:-server|/index)?(?:\.[cm]?js)?(?:[\s"']|$)""", command)))
        if branded:
            owned.add(row["pid"])
    for _ in range(len(rows) + 1):
        children = {row["pid"] for row in rows if row["parent"] in owned}
        if children <= owned:
            break
        owned.update(children)
    return owned


def require_closed(evidence, *, client_inspector=None):
    from .codex_desktop_state import running_related_clients
    from .orca_metadata import journal_name, read_orca_journal
    from .orca_target_safety import inspect_orca_target_processes
    root = Path(evidence["root"])
    if evidence.get("remote_owner"):
        from .orca_remote import check_pairing
        check_pairing(evidence["remote_owner"])
    current = instance_lock(root)
    expected = next((item for item in evidence["runtime_observations"] if item["path"] == "orcad.lock"), None)
    if lock_evidence(current) != expected:
        fail("instance_lock_changed")
    if current:
        check_lock_host(root, current[1])
    if client_inspector is not None and client_inspector is not running_related_clients:
        from .frontend_session_cleanup import _inspect_owner_process
        if tuple(_inspect_owner_process(client_inspector, root, owner_client="orca")):
            fail("writer_running")
        return
    from .office_runtime import processes
    rows = processes()
    declared = {item["pid"] for item in evidence["runtime_observations"] if "pid" in item}
    if related(rows, root) or any(row["pid"] in declared for row in rows):
        fail("writer_running")
    if evidence.get("remote_owner") and evidence["journal"].get("absent"):
        from .orca_frontend import _exists
        if _exists(root, journal_name(root)):
            fail("controller_journal_appeared")
        return
    records, errors = read_orca_journal(root / journal_name(root), root)
    if errors:
        fail("writer_lease_unverified")
    leases = (record.lease for record in records if not evidence.get("remote_owner") or record.host == "local")
    report = inspect_orca_target_processes(leases)
    if report.get("clients_closed") is not True:
        fail("writer_lease_unverified")
