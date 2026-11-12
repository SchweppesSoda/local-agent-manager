"""Frozen T3 database, terminal and qualified attachment closure."""
import base64
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re

from . import frozen_files, t3code_database as database
from .t3code_store import fail, identifier, read_database
from .sqlite_utils import connect_readonly

SCHEMA = "larj.t3code-files.v1"
TERMINALS = "userdata/logs/terminals"


def _encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _names(root, relative):
    path = frozen_files.checked_path(root, relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": relative, "absent": True}
    frozen_files._plain(info, directory=True)
    names = []
    for entry in path.iterdir():
        if len(names) >= 20000:
            fail("file_budget_exceeded")
        names.append(entry.name)
    return {"path": relative, "identity": frozen_files._identity(info), "entries": sorted(names)}


def _terminal_owner(name, ids):
    owners = []
    for identifier_ in ids:
        prefixes = ("terminal_" + _encoded(identifier_.encode()), "terminal_sha256." + _encoded(hashlib.sha256(identifier_.encode()).digest()))
        legacy = re.sub(r"[^a-zA-Z0-9._-]", "_", identifier_) + ".log"
        if name == legacy or any(name == p + ".log" or name.startswith(p + "_") and name.endswith(".log") for p in prefixes):
            owners.append(identifier_)
    if len(owners) > 1:
        fail("terminal_identity_ambiguous")
    return owners[0] if owners else None


def _require_known_database_family(root):
    directory = frozen_files.checked_path(root, "userdata")
    for path in directory.iterdir():
        name = path.name.casefold()
        if (name.startswith("state") or name.endswith((".sqlite", ".sqlite3", ".db", ".bak"))) and name not in {
                "statev2.sqlite", "statev2.sqlite-wal", "statev2.sqlite-shm", "statev2.sqlite-journal",
                "server-owner.sqlite"}:
            fail("recovery_database_unqualified")


def _html_render_attachments(db):
    """Describe known completed HTML render outputs and their owning threads.

    T3's HtmlRender.publish writes <createAttachmentId(threadId, 'html')>.html;
    threadHtmlRenderAttachmentIds requires the same thread segment. Selected
    deletion also requires exact metadata and retained-reference checks in
    t3code_attachments; this descriptor alone grants no removal.
    """
    from .environment_config import _unique_object
    from .t3code_store import V2, rows
    result = []
    for row in rows(db, f'''SELECT thread_id,payload_json FROM {V2}turn_items
            WHERE type='dynamic_tool' AND status='completed'
            AND json_extract(payload_json,'$.toolName')='t3-code.html_render' '''):
        try:
            item = json.loads(row['payload_json'], object_pairs_hook=_unique_object)
            output = item.get('output')
            if (item.get('threadId') != row['thread_id'] or item.get('type') != 'dynamic_tool'
                    or item.get('status') != 'completed' or not isinstance(output, dict)
                    or set(output) - {'htmlRender', 'message', 'isError'}
                    or output.get('isError', False) is not False):
                continue
            render = output.get('htmlRender')
            if (not isinstance(render, dict) or set(render) - {'attachmentId', 'title', 'height', 'heights'}
                    or not isinstance(render.get('attachmentId'), str)
                    or not render['attachmentId'].endswith('-html')
                    or not isinstance(render.get('title'), str)
                    or type(render.get('height')) not in (int, float)):
                continue
            result.append({'thread_id': row['thread_id'], 'id': render['attachmentId'],
                           'type': 'file', 'name': 'render.html'})
        except (ValueError, TypeError):
            continue
    return result


def freeze(root, ids, *, native_ids=()):
    root = Path(root)
    ids = sorted({identifier(i) for i in ids})
    _require_known_database_family(root)
    all_ids = tuple(read_database(root)["threads"])
    directories = [_names(root, relative) for relative in
        (TERMINALS, "userdata/attachments", "userdata/browser-artifacts", "userdata/backups", "userdata/snapshots")]
    # Unknown browser, backup and snapshot families remain unqualified.
    if any(d.get("entries") for d in directories[2:]):
        fail("attachment_closure_unqualified")
    from .t3code_attachments import freeze as attachment_closure
    changes, preserved, attachment_ids = attachment_closure(root, directories[1], ids)
    # Protect exact attachment handles in retained rows, including sanitized
    # thread prefixes which need not contain the original selected thread ID.
    db = database.freeze(root, ids, native_ids=sorted(set(native_ids) | set(attachment_ids)))
    if (changes, preserved, attachment_ids) != attachment_closure(root, directories[1], ids):
        fail('attachment_reference_changed')
    for name in directories[0].get("entries", ()):
        relative = TERMINALS + "/" + name
        proof, _ = frozen_files.read_file(root, relative)
        owner = _terminal_owner(name, all_ids)
        if owner is None:
            fail("terminal_owner_unqualified")
        if owner in ids:
            changes.append({"kind": "remove", "path": relative, "files": [proof], "directories": []})
        else:
            preserved.append(proof)
    if directories != [_names(root, d["path"]) for d in directories]:
        fail("file_closure_changed")
    return {"schema_version": SCHEMA, "root": str(root), "thread_ids": ids, "database": db,
        "directories": directories, "changes": changes, "preserved": preserved}


def validate(evidence):
    if evidence.get("schema_version") != SCHEMA or evidence.get("thread_ids") != evidence.get("database", {}).get("thread_ids"):
        fail("file_evidence_invalid")
    if evidence.get("root") != evidence["database"].get("root"):
        fail("file_evidence_invalid")
    database.validate(evidence["database"])
    return Path(evidence["root"])


def remaining(evidence):
    root = validate(evidence)
    _require_known_database_family(root)
    missing, count = set(), database.remaining(evidence["database"])
    for change in evidence["changes"]:
        if frozen_files.satisfied(root, change):
            missing.add(change["path"])
        else:
            if frozen_files.freeze_remove(root, change["path"]) != change:
                fail("file_state_unknown")
            count += 1
    expected = copy.deepcopy(evidence["directories"])
    for directory in expected:
        if "entries" in directory:
            directory["entries"] = [name for name in directory["entries"] if directory["path"] + "/" + name not in missing]
    if [_names(root, d["path"]) for d in expected] != expected:
        fail("file_closure_changed")
    for proof in evidence["preserved"]:
        current, _ = frozen_files.read_file(root, proof["path"])
        if current != proof:
            fail("unselected_file_changed")
    return count


def apply(evidence, *, phase_callback):
    root = validate(evidence)
    remaining(evidence)
    for change in evidence["changes"]:
        phase_callback("mutation_started")
        frozen_files.apply_remove(root, change)
    database.apply(evidence["database"], phase_callback=phase_callback)
    if remaining(evidence):
        fail("files_after_unverified")
