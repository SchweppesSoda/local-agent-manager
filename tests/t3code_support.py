"""Synthetic, exact-schema T3 Code stores. Never copies user databases."""
import json
import sqlite3
from pathlib import Path

from local_agent_record_janitor.t3code_store import CONTRACT, RELATIVE, V2

PRIVATE = "T3_PRIVATE_MESSAGE_TITLE_TOKEN_SENTINEL"
STAMP = "2026-11-07T00:00:00.000Z"


def create_store(root):
    path = Path(root) / RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        objects = CONTRACT["objects"]
        for kind, name, table, sql in objects:
            if kind == "table" and not name.startswith("orchestration_v2_message_search_"):
                db.execute(sql)
        for kind, name, table, sql in objects:
            if kind in {"index", "trigger", "view"} and sql:
                db.execute(sql)
        db.commit()
    except BaseException:
        db.close()
        raise
    return db


def insert(db, table, **values):
    for _, name, type_, required, default, pk in db.execute(f'PRAGMA table_info("{table}")'):
        if name not in values and required and default is None:
            values[name] = 0 if type_.upper() in {"INT", "INTEGER"} else "{}" if name.endswith("_json") else STAMP
    names = list(values)
    db.execute(f'INSERT INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" for _ in names)})', tuple(values[n] for n in names))


def add_thread(db, identifier="thread-1", *, native=None, driver="codex", legacy=False):
    if not db.execute("SELECT 1 FROM projection_projects WHERE project_id='project-1'").fetchone():
        insert(db, "projection_projects", project_id="project-1", title=PRIVATE, workspace_root=str(Path.cwd()), scripts_json="[]")
    if legacy:
        insert(db, "projection_threads", thread_id=identifier, project_id="project-1", title=PRIVATE)
        insert(db, "projection_thread_messages", message_id="old-message-" + identifier, thread_id=identifier, role="user", text=PRIVATE)
    else:
        payload = {"id": identifier, "projectId": "project-1", "title": PRIVATE, "worktreePath": None,
            "lineage": {"parentThreadId": None, "rootThreadId": identifier, "relationshipToParent": None}}
        insert(db, V2 + "threads", thread_id=identifier, project_id="project-1", title=PRIVATE,
            default_provider=driver, provider_instance_id=driver, payload_json=json.dumps(payload))
        insert(db, V2 + "messages", message_id="message-" + identifier, thread_id=identifier, role="user", streaming=0,
            payload_json=json.dumps({"id": "message-" + identifier, "threadId": identifier, "text": PRIVATE}))
        insert(db, "orchestration_v2_events", event_id="event-" + identifier, thread_id=identifier,
            event_type="thread.created", payload_json=json.dumps(payload))
        if native:
            payload = {"id": "provider-" + identifier, "appThreadId": identifier, "driver": driver,
                "nativeThreadRef": {"driver": driver, "nativeId": native, "strength": "strong"}}
            insert(db, V2 + "provider_threads", provider_thread_id="provider-" + identifier, thread_id=identifier,
                provider=driver, driver=driver, provider_instance_id=driver, status="closed", payload_json=json.dumps(payload))
    db.commit()
