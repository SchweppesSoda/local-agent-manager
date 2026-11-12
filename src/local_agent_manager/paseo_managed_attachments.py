"""Exact disposal of the desktop's unreferenced managed image copies."""
from pathlib import Path
import re

from . import frozen_files, paseo_cleanup_files as files

RELATIVE = "desktop-attachments"
SCHEMA = "larj.paseo-managed-images.v1"
NAME = re.compile(r"(?:att_msg_[0-9]{13}_[a-z0-9]+|preview_[0-9]+_[a-z0-9]+)\.(png|jpg)\Z")


def freeze(root, selected, all_agents):
    root = Path(root)
    path = frozen_files.checked_path(root, RELATIVE)
    evidence = {"schema_version": SCHEMA, "root": str(root), "files": None}
    try:
        path.lstat()
    except FileNotFoundError:
        return evidence
    snapshot = frozen_files.freeze_remove(root, RELATIVE)
    if snapshot["files"] and set(selected) != set(all_agents):
        files.fail("managed_attachment_partial_scope_unqualified")
    if len(snapshot["directories"]) != 1:
        files.fail("managed_attachment_layout_unqualified")
    for item in snapshot["files"]:
        name = Path(item["path"]).name
        match = NAME.fullmatch(name)
        if not match:
            files.fail("managed_attachment_format_unqualified")
        proof, body = frozen_files.read_file(root, item["path"], content=True, limit=16 * 1024 * 1024)
        if proof != item or (match[1] == "png" and not (body.startswith(b"\x89PNG\r\n\x1a\n")
                and body.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"))
                or match[1] == "jpg" and not (body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9"))):
            files.fail("managed_attachment_format_unqualified")
        if name.startswith("preview_") and int(name.split("_", 2)[1]) != item["size"]:
            files.fail("managed_attachment_size_unverified")
    evidence["files"] = snapshot
    return evidence


def names(evidence):
    return sorted(Path(item["path"]).name for item in (evidence["files"] or {}).get("files", ()))


def remaining(evidence):
    if evidence.get("schema_version") != SCHEMA or evidence.get("files") is not None and evidence["files"].get("path") != RELATIVE:
        files.fail("managed_attachment_scope_invalid")
    root = Path(evidence["root"])
    path = frozen_files.checked_path(root, RELATIVE)
    if evidence["files"] is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return 0
        files.fail("managed_attachment_closure_changed")
    if frozen_files.satisfied(root, evidence["files"]):
        return 0
    if frozen_files.freeze_remove(root, RELATIVE) != evidence["files"]:
        files.fail("managed_attachment_state_unknown")
    return max(1, len(evidence["files"]["files"]))


def apply(evidence, *, phase_callback):
    if remaining(evidence):
        phase_callback("mutation_started")
        frozen_files.apply_remove(Path(evidence["root"]), evidence["files"])
    if remaining(evidence):
        files.fail("managed_attachment_after_unverified")
