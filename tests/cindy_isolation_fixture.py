"""Temporary exact-task stores, with real Windows boundary IO and no live roots."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3

from local_agent_manager import frozen_files as ff
from local_agent_manager.cindy_isolation_plan import directory_identity, fingerprint
from local_agent_manager.cindy_isolation_recovery import digest


def make_fixture(root):
    root = Path(root).resolve()
    source, profile, target, packets = (root / p for p in ("official", "Cindy", "Cindy/codex-home", "packets"))
    for path in (source, profile, target, packets): path.mkdir(parents=True, exist_ok=True)
    source_file, staged, target_file = source / "sessions/rollout-selected.jsonl", packets / "selected.jsonl", target / "sessions/rollout-selected.jsonl"
    source_file.parent.mkdir(); target_file.parent.mkdir()
    source_file.write_text(json.dumps({"type": "session_meta", "payload": {"id": "selected", "source": "app-server", "timestamp": "2026-07-31T00:00:00Z", "cwd": str(root), "originator": "cindy"}}) + '\n{"type":"event_msg","payload":{"type":"user_message"}}\n', encoding="utf8")
    staged.write_bytes(source_file.read_bytes())
    schemas = {
        "state_5.sqlite": ["create table threads(id text primary key,rollout_path text,attached_thread_parent_id text,thread_section_id text,project_id text,updated_at int,created_at int default 1,archived int default 0,source text,thread_source text,agent_role text,agent_nickname text,cwd text)",
                           "create table thread_spawn_edges(parent_thread_id text,child_thread_id text,status text,primary key(parent_thread_id,child_thread_id))"],
        "thread_history_1.sqlite": ["create table thread_items(thread_id text,item_id text,item_json text,primary key(thread_id,item_id))",
                                     "create table thread_history_projection_state(thread_id text primary key,next_rollout_byte_offset int,next_rollout_ordinal int)"]}
    packet_rows = {"state_5.sqlite": {"threads": 1}, "thread_history_1.sqlite": {"thread_items": 1, "thread_history_projection_state": 1}}
    for name, statements in schemas.items():
        for base in (source, target, packets):
            with closing(sqlite3.connect(base / name)) as c:
                c.execute("pragma journal_mode=wal")
                for sql in statements: c.execute(sql)
                c.execute("create table protected(id text primary key,opaque blob)")
                c.execute("insert into protected values('keep',?)", (b"protected synthetic",)); c.commit()
        with closing(sqlite3.connect(source / name)) as c:
            if name == "state_5.sqlite": c.execute("insert into threads(id,rollout_path,updated_at,source,cwd) values('selected',?,1,'app-server',?)", (str(source_file),str(root)))
            else:
                c.execute("insert into thread_items values('selected','original','opaque synthetic')")
                c.execute("insert into thread_history_projection_state values('selected',?,1)", (source_file.stat().st_size,))
            c.commit()
        with closing(sqlite3.connect(packets / name)) as c:
            if name == "state_5.sqlite": c.execute("insert into threads(id,rollout_path,updated_at,source,cwd) values('selected',?,1,'app-server',?)", (str(target_file),str(root)))
            else:
                c.execute("insert into thread_items values('selected','original','opaque synthetic')")
                c.execute("insert into thread_history_projection_state values('selected',?,1)", (source_file.stat().st_size,))
            c.commit()
    auth_source, auth_target = source / "auth.json", target / "auth.json"
    auth_source.write_text('{"synthetic":"not-a-real-token"}')
    os.symlink(auth_source, auth_target)
    registry = profile / "accounts.json"
    before = {"pools": {"openai": {"accounts": [{"id": "default"}]}}, "bindings": {}}
    registry.write_text(json.dumps(before))
    location = profile / "location.json"; location.write_text('{"threadId":"selected","home":"official"}')
    def meta(path):
        s = path.stat()
        return {"path": str(path), "device": s.st_dev, "inode": s.st_ino, "size": s.st_size, "mtime_ns": s.st_mtime_ns}
    native = {"native_id": "selected", "source": {**meta(source_file), "sha256": digest(source_file)}, "target_path": str(target_file)}
    dependencies = []
    for name, rows in packet_rows.items():
        dependencies.append({"identity": {"path": str(source / name)}, "tables": {
            table: {"class": "copy_selected_rows", "selected_rows": count,
                    "selectors": ["id" if table == "threads" else "thread_id"]} for table, count in rows.items()}})
    draft = {"groups": {"current_shared_candidates": {"batches": [{"root_ids": ["selected"], "native_ids": ["selected"], "task_ids": ["task"]}],
              "native_files": [native], "dependencies": dependencies, "source_home": {"path": str(source)}}}}
    executable, package = root / "fixture-cmd.exe", root / "fixture-package"
    shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32/cmd.exe", executable)
    package.write_bytes(b"synthetic package")
    startup = []
    for path in (executable, package):
        evidence, _ = ff.read_file(path.parent, path.name)
        startup.append({"root": str(path.parent), "file": evidence})
    spec = {"profile": directory_identity(profile), "source": directory_identity(source), "target": directory_identity(target),
            "native_files": [{"native_id": "selected", "path": str(staged), "target_path": str(target_file), "size": staged.stat().st_size, "sha256": digest(staged)}],
            "database_packets": [{"packet": str(packets / name), "sha256": digest(packets / name), "rows_inserted": rows} for name, rows in packet_rows.items()],
            "binding_changes": [{"key": "openai:native:selected", "before_present": False, "before": None, "after": {"providerId": "openai", "accountId": "default"}}],
            "registry_path": str(registry), "registry_before_sha256": digest(registry),
            "route_change": {"before_present": False, "before": None, "after": True},
            "location_changes": [{"file": str(location), "before": {"threadId": "selected", "home": "official"}, "after": {"threadId": "selected", "home": "legacy"}}],
            "auth": {"source": meta(auth_source), "target": meta(auth_target)},
            "controller": {"startup_files": startup, "registry_before": before}}
    spec["plan_sha256"] = fingerprint(spec)
    return spec, draft
