"""Prepare this machine's exact Cindy execution bundle; never elevate/close/write stores."""
from contextlib import closing
from copy import deepcopy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from local_agent_manager import frozen_files as ff
from local_agent_manager.cindy_isolation_plan import (binding_changes, directory_identity, fingerprint, legacy_destination, readonly_sqlite, physical_descendant)
from local_agent_manager.cindy_isolation_recovery import digest, q
from local_agent_manager.cindy_isolation_merge import TABLES, merge_selected_rows
from local_agent_manager.cindy_isolation_windows import auth_permissions, closure_in_transaction, current_user_sid
from local_agent_manager.cindy_isolation_controller import TASK, protected_snapshot, supported_account
from local_agent_manager.operation_store import write_new_json
from local_agent_manager.cindy_isolation_ui import window_behavior


def meta(path):
    path = Path(path); info = path.stat()
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def installed_source(package):
    with package.open("rb") as stream:
        header = stream.read(16); tree = json.loads(stream.read(struct.unpack("<4I", header)[3]))
        base = 8 + struct.unpack("<4I", header)[1]
        def read(relative):
            node = tree
            for part in relative.split("/"): node = node["files"][part]
            stream.seek(base + int(node["offset"])); return stream.read(node["size"]).decode()
        boot = read(".vite/build/bootstrap-electron-DrVGBz1v.js")
        match = re.search(r'\bindex\$b=require\("\./([^"/]+)"\)', boot)
        if not match: raise RuntimeError("Installed deep-link module unavailable")
        index = read(".vite/build/" + match[1])
        # Follow the actual branded constant, not DATA_URL_PREFIX's suffix.
        if 'URL_PREFIX=index$b.DEEP_LINK_URL_PREFIX' not in boot: raise RuntimeError("Deep-link binding changed")
        schemes = set(re.findall(r'primaryScheme:\s*"([^"\s]+)"', index))
        if "cindy" not in schemes or 'DEEP_LINK_URL_PREFIX=`${DEEP_LINK_PRIMARY_SCHEME}://`' not in index:
            raise RuntimeError("Installed primary deep-link scheme unqualified")
        return {"package_sha256": digest(package), "deep_link_prefix": "cindy://",
                "brand_module": match[1], "bootstrap_sha256": hashlib.sha256(boot.encode()).hexdigest(),
                "buildSessionDeepLink": boot[boot.index("function buildSessionDeepLink("):boot.index("function buildSessionDeepLink(") + 250]}


def prepare():
    old = ROOT / ".agent-manager-state/manual/cindy-isolation-20261115-continuation"
    old_spec = json.loads((old / "legacy-execution-spec.json").read_text(encoding="utf8"))
    old_draft = json.loads((old / "legacy-bound-draft.json").read_text(encoding="utf8"))
    original = dict(old_spec); approved = original.pop("plan_sha256")
    if fingerprint(original) != approved: raise RuntimeError("Old source spec hash changed")
    bundle = ROOT / ".agent-manager-state/manual" / ("cindy-isolation-execution-" + datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    bundle.mkdir()
    draft, spec = deepcopy(old_draft), deepcopy(old_spec)
    spec.pop("plan_sha256"); spec["supersedes"] = approved
    spec['at']=datetime.datetime.now().astimezone().isoformat()
    spec['kind']='cindy_legacy_single_uac_execution_spec_v1'
    group = draft["groups"]["current_shared_candidates"]
    source, target, profile = (Path(spec[k]["path"]) for k in ("source", "target", "profile"))
    registry_path = Path(spec["registry_path"]); ff._plain(registry_path.lstat())
    registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    roots = sorted({i for b in group["batches"] for i in b["root_ids"]})
    tasks = sorted({i for b in group["batches"] for i in b["task_ids"]})
    with readonly_sqlite(source / "state_5.sqlite") as c:
        ids = closure_in_transaction(c, roots)
        source_rows = {r["id"]: dict(r) for r in c.execute("select * from threads") if r["id"] in ids}
        refs = [(r["thread_section_id"], r["project_id"]) for r in source_rows.values()]
    sections, projects = ({r[i] for r in refs if r[i] is not None} for i in (0, 1))
    with readonly_sqlite(target / "state_5.sqlite") as c:
        protected_target_ids = [r[0] for r in c.execute("select id from threads order by id")]
    if set(ids) & set(protected_target_ids): raise RuntimeError("Target already contains an incoming ID; use the bound recovery entry")
    changed = sorted(set(ids) - {i for b in group["batches"] for i in b["native_ids"]})
    removed = sorted({i for b in group['batches'] for i in b['native_ids']} - set(ids))
    if removed: raise RuntimeError('Previously frozen descendants disappeared; retain original goal and review authoritative residuals')
    native_files, staged, rewrites = [], [], {}
    old_staged = {f["native_id"]: f for f in old_spec["native_files"]}
    locations_root = Path(group["location_changes"][0]["file"]).parent
    source_locations = []
    for ident in ids:
        raw_path = Path(source_rows[ident]["rollout_path"])
        ff._plain(raw_path.lstat())
        path = physical_descendant(raw_path, source); relative = path.relative_to(source)
        ff.checked_path(source, relative.as_posix())
        destination = target / relative
        before = meta(path); sha = digest(path)
        if before != meta(path): raise RuntimeError("Source native changed while staging")
        if destination.exists(): raise RuntimeError("Target file exists; never overwrite")
        previous = old_staged.get(ident)
        if previous and previous["sha256"] == sha and Path(previous["target_path"]) == destination and digest(previous["path"]) == sha:
            copy = Path(previous["path"])
        else:
            copy = bundle / "files" / relative; copy.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as incoming, copy.open("xb") as outgoing: shutil.copyfileobj(incoming, outgoing)
            if digest(copy) != sha or meta(path) != before: raise RuntimeError("Staged native drift")
        native_files.append({"native_id": ident, "source": {**before, "sha256": sha}, "target_path": str(destination)})
        staged.append({"native_id": ident, "path": str(copy), "target_path": str(destination), "size": before["size"], "sha256": sha})
        rewrites[str(raw_path)] = str(destination)
        location = locations_root / (ident + ".json")
        if location.exists(): ff._plain(location.lstat())
        value = json.loads(location.read_text(encoding="utf-8-sig")) if location.exists() else None
        source_locations.append({"file": str(location), "before": value,
                                 "after": {"threadId": ident, "path": str(destination), "sqliteHome": str(target)}})
    group["native_files"], group["native_count"] = native_files, len(ids)
    group.update(root_count=len(roots), task_count=len(tasks), closure_added=changed, closure_missing=[])
    group["batches"] = [{"root_ids": roots, "task_ids": tasks, "native_ids": ids}]
    protected = {key for key, value in registry["bindings"].items() if value.get("accountId") in {spec["protected_second_account"], spec["retained_new_account"]["id"]}}
    changes = binding_changes(registry, task_ids=tasks, native_ids=ids, account_id="default", protected_keys=protected, legacy_default=True)
    spec["binding_changes"] = [c for c in changes if not c["before_present"] or c["before"] != c["after"]]
    group["binding_changes"] = spec["binding_changes"]
    legacy_locations = [c for c in old_spec["location_changes"] if c["after"]["threadId"] not in {f["native_id"] for f in old_spec["native_files"]}]
    for change in legacy_locations:
        path = Path(change["file"]); ff._plain(path.lstat())
        change["before"] = json.loads(path.read_text(encoding="utf-8-sig"))
    spec["location_changes"] = source_locations + legacy_locations
    group["location_changes"] = source_locations
    spec["native_files"] = staged
    spec["database_packets"] = []
    source_identities = {}
    for dependency in group["dependencies"]:
        path = Path(dependency["identity"]["path"])
        ff._plain(path.lstat()); dependency["identity"] = meta(path); source_identities[path.name] = meta(path)
        with readonly_sqlite(path) as c:
            qualified_schema = [dict(r) for r in c.execute('select type,name,tbl_name,sql from sqlite_master order by type,name')]
            if fingerprint(qualified_schema) != dependency['schema_sha256']:
                raise RuntimeError('Source database schema changed; new tables/dependencies require explicit qualification')
            schema = list(c.execute("select type,name,sql from sqlite_master where sql is not null and name not like 'sqlite_%' order by case type when 'table' then 1 when 'index' then 2 else 3 end,name"))
            references = []
            for table, evidence in dependency["tables"].items():
                if evidence.get('class') not in {'copy_selected_rows', 'preserve_source_migration_evidence'}:
                    evidence['source_count'] = c.execute('select count(*) from ' + q(table)).fetchone()[0]
                    continue
                if evidence.get('class') == 'preserve_source_migration_evidence':
                    # Original source rollout migration evidence is never imported.
                    values = sorted(rewrites)
                else:
                    values = sorted(sections if table == "thread_sections" else projects if table in {"projects", "project_roots", "project_idempotency_keys"} else ids)
                predicate = " or ".join(q(k) + " in (" + ",".join("?" for _ in values) + ")" for k in evidence["selectors"])
                count = c.execute("select count(*) from " + q(table) + " where " + predicate, values * len(evidence["selectors"])).fetchone()[0] if values else 0
                evidence["selected_rows"] = count
                columns = evidence.get('metadata_columns', [])
                if columns:
                    rows = [dict(row) for row in c.execute('select ' + ','.join(map(q, columns)) + ' from ' + q(table) +
                        ' where ' + predicate, values * len(evidence['selectors']))] if values else []
                    hashed = {fingerprint(row): row for row in rows}
                    evidence['metadata_sha256'] = fingerprint([hashed[key] for key in sorted(hashed)])
                if count and table not in TABLES: raise RuntimeError("New unsupported dependency: " + table)
                if count and table == "thread_attachments": raise RuntimeError("New selected attachment requires a qualified physical-file plan")
                if count:
                    all_columns = [r[1] for r in c.execute('pragma table_info(' + q(table) + ')')]
                    for column in (name for name in all_columns if name in {'payload', 'payload_json', 'item_json', 'record', 'metadata'}):
                        sql = ('select j.fullkey,j.atom from ' + q(table) + ' s,json_tree(case when json_valid(s.' + q(column) +
                            ') then s.' + q(column) + " else null end) j where (" + predicate + ") and j.type='text' and lower(cast(j.key as text)) in "
                            "('path','filepath','file_path','localpath','local_path','attachmentpath','attachment_path','image_path','input_csv_path','output_csv_path','uri','url')")
                        for row in c.execute(sql, values * len(evidence['selectors'])):
                            if not isinstance(row[1], str) or not (re.match(r'^[A-Za-z]:[\\/]', row[1]) or row[1].startswith('file:///')): continue
                            if table not in {'thread_items', 'thread_realtime_items'}:
                                raise RuntimeError('Selected non-history local file reference needs physical dependency qualification')
                            reference = {'table': table, 'column': column, 'json_path': row[0], 'path': row[1],
                                'category': 'historical_tool_argument_preserve_external'}
                            if reference not in references: references.append(reference)
            dependency['structured_paths'] = references
        tables = [table for table, evidence in dependency["tables"].items() if table in TABLES and evidence.get("selected_rows")]
        if not tables: continue
        packet = bundle / "packets" / path.name; packet.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(packet.as_uri()+'?mode=rwc',uri=True)) as c:
            for kind, name, sql in schema:
                if kind not in {"table", "index", "trigger"}: raise RuntimeError("Unqualified source schema object")
                c.execute(sql)
            c.commit(); c.execute("attach database ? as incoming", (path.as_uri() + "?mode=ro",)); c.execute("begin immediate")
            result = merge_selected_rows(c, native_ids=ids, tables=tables, rollout_rewrites=rewrites); c.commit()
        spec["database_packets"].append({"packet": str(packet), "sha256": digest(packet), "source_database": str(path), **result,'committed':True})
    spec["registry_before_sha256"] = digest(registry_path)
    destination = legacy_destination(registry, profile_root=profile, home=target, official_home=source)
    spec["route_change"] = destination["route_change"]
    for key in ("source", "target", "profile"): spec[key] = directory_identity(Path(spec[key]["path"]))
    for key in ("source", "target"): spec["auth"][key] = {**spec["auth"][key], **meta(spec["auth"][key]["path"])}
    package = Path(r"C:\Program Files\Cindy\resources\app.asar")
    executable = package.parents[1] / "Cindy.exe"
    product = installed_source(package)
    native_binary = profile / "codex-package/0.166.0/bin/codex.exe"
    version = subprocess.run([str(native_binary), "--version"], capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if version.returncode or not version.stdout.strip(): raise RuntimeError("Actual native binary smoke failed")
    startup = []
    for path in (executable, package):
        evidence, _ = ff.read_file(path.parent, path.name); startup.append({"root": str(path.parent), "file": evidence})
    database = profile / "cindy-cmscv4tqp03ly3301oje2kgmb.db"
    with readonly_sqlite(database) as c:
        all_rows = [dict(r) for r in c.execute("select id,title,sdk_session_id,agent_kind,status from sessions")]
    # The default flag changes every default binding. Refuse any newly
    # discovered official-home task that was not approved in the root set.
    if registry['pools']['openai'].get('legacyCodexDefault') is not True:
        with readonly_sqlite(source / 'state_5.sqlite') as c:
            source_ids = {row[0] for row in c.execute('select id from threads')}
        for row in all_rows:
            native = row['sdk_session_id']
            if row['agent_kind'] != 'codex' or native not in source_ids or native in ids or native in protected_target_ids: continue
            binding = registry['bindings'].get('openai:native:' + native) or registry['bindings'].get('native:' + native)
            if binding is None or binding.get('accountId') == 'default':
                raise RuntimeError('Default route would change an unapproved official-root task')
    eligible = [r for r in all_rows if r["id"] in tasks]
    if len(eligible) != len(tasks) or any(r["agent_kind"] != "codex" or r["sdk_session_id"] not in roots or not r["title"] for r in eligible):
        raise RuntimeError("Original product task ownership/title changed")
    with readonly_sqlite(source / "thread_history_1.sqlite") as c:
        turns = {r[0]: r[1] for r in c.execute("select thread_id,count(*) from thread_turns group by thread_id")}
    counts = {r["title"]: sum(x["title"] == r["title"] for x in all_rows) for r in eligible}
    unique = [r for r in eligible if counts[r["title"]] == 1]
    longest = max(unique, key=lambda r: turns.get(r["sdk_session_id"], 0))
    other = next(r for r in unique if r["id"] != longest["id"])
    representatives = {role: {"task_id": row["id"], "native_id": row["sdk_session_id"], "title": row["title"],
                              "product_deep_link": "cindy://session/" + row["id"], "frozen_turn_count": turns.get(row["sdk_session_id"], 0)}
                       for role, row in (("root", other), ("long_history", longest))}
    spec["controller"] = {"task": TASK, "bundle": str(bundle), "executable": str(executable), "executable_sha256": digest(executable),
        'executor_sid':current_user_sid(),
        "native_binary": str(native_binary), "native_version": version.stdout.strip(), "product_source": product,
        "startup_files": startup, "registry_before": registry, "source_root_ids": roots,
        "source_database_identities": source_identities, "protected_target_ids": protected_target_ids,
        'target_database_identities': {Path(p['packet']).name: meta(target / Path(p['packet']).name) for p in spec['database_packets']},
        "protected_second_home": directory_identity(source.parent / (".codex-" + spec["protected_second_account"])),
        "protected_bindings": {k: v for k, v in registry["bindings"].items() if k in protected},
        'expected_bindings':{c['key']:c['after'] for c in changes},
        "auth_effective_acl_sha256": auth_permissions(spec["auth"]["source"]["path"])["effective_acl_sha256"],
        "cindy_database": str(database), "representatives": representatives, "side_native_ids": sorted(set(ids) - set(roots)),
        "test_workdir": str(ROOT), "supervisor_max_seconds": 2400, "source_cleanup_requires_all_acceptance_roles": True}
    # Existing login, supported read-only RPC; no old evidence file is touched.
    spec["controller"]["account_identity_sha256"] = supported_account(spec)["identity_sha256"]
    second=spec['controller']['protected_second_home']
    spec['controller']['allowed_test_homes']={'default':spec['target'],spec['protected_second_account']:second}
    spec['controller']['allowed_test_accounts']={'default':spec['controller']['account_identity_sha256'],
        spec['protected_second_account']:supported_account(spec,home=second['path'])['identity_sha256']}
    spec['controller']['window_behavior_before']=window_behavior(spec)
    spec["controller"]["protected_snapshot"] = protected_snapshot(spec)
    draft["draft_sha256"] = fingerprint({k: v for k, v in draft.items() if k != "draft_sha256"})
    spec["source_draft_sha256"] = draft["draft_sha256"]
    spec["controller"]["draft_content_sha256"] = fingerprint(draft)
    spec["plan_sha256"] = fingerprint(spec)
    spec['move_groups'].update(current_shared_roots=len(roots),incoming_native=len(ids))
    spec['source_packet_state']=str(bundle/'preparation-state.json')
    spec.pop('plan_sha256');spec['plan_sha256']=fingerprint(spec)
    write_new_json(bundle/'preparation-state.json',{'online_snapshot_only':True,'requires_held_validation':True,
        'source_spec_sha256':spec['plan_sha256'],'native_files':staged,'database_packets':spec['database_packets'],
        'new_descendants':changed,'roots':roots,'native_ids':ids})
    write_new_json(bundle / "source-draft.json", draft); write_new_json(bundle / "execution-spec.json", spec)
    # Wrapper has no circular embedded hash. The reviewed command supplies the
    # manifest hash, which verifies this wrapper before any package is imported.
    shutil.copyfile(ROOT/'tools/cindy_legacy_entry_bootstrap.py',bundle/'entry.py')
    files = {p for p in (ROOT / "src/local_agent_manager").rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".json", ".cjs", ".manifest", ".ps1"}}
    files.update({bundle / "entry.py", bundle / "execution-spec.json", bundle / "source-draft.json", Path(sys.executable), native_binary, executable, package})
    files.update(Path(f["path"]) for f in staged); files.update(Path(p["packet"]) for p in spec["database_packets"])
    manifest = {"task": TASK, "spec_sha256": spec["plan_sha256"], "source_path": str(ROOT / "src"),
                'python_executable': sys.executable,
                "files": [{"path": str(p), "sha256": digest(p), "size": p.stat().st_size} for p in sorted(files, key=str)]}
    write_new_json(bundle / "execution-manifest.json", manifest)
    manifest_hash = digest(bundle / "execution-manifest.json")
    command = [sys.executable, "-I", str(bundle / "entry.py"), "execute", "--manifest-sha256", manifest_hash, "--uac-authorized"]
    write_new_json(bundle / "launch-command.json", {"argv": command, "manifest_sha256": manifest_hash,
        "spec_sha256": spec["plan_sha256"], "roots": len(roots), "native": len(ids), "new_native_descendants": changed,
        "bindings": len(spec["binding_changes"]), "locations": len(spec["location_changes"]),
        "uac_dispatched": False, "lifecycle_dispatched": False, "live_import_dispatched": False})
    print(json.dumps({"bundle": str(bundle), "manifest_sha256": manifest_hash, "spec_sha256": spec["plan_sha256"],
                      "root_count": len(roots), "native_count": len(ids), "new_descendants": changed,
                      "binding_changes": len(spec["binding_changes"]), "locations": len(spec["location_changes"]), "uac_dispatched": False}))


if __name__ == "__main__": prepare()
