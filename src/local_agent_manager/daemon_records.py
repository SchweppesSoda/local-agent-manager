"""Body-free execution relationships in a T3 environment or Paseo registry."""
from contextlib import closing
from urllib.parse import urlsplit

from . import frozen_files
from .providers.t3 import strict_json
from .sqlite_utils import connect_readonly
from . import t3code_provider_settings as t3_settings
from .t3code_store import V2, database_path, identifier, provider_rows, rows


def settings_file(client):
    return "userdata/settings.json" if client == "t3code" else "config.json"


def _provider_settings(client, root):
    try:
        if client == "t3code" and t3_settings.has_provider_policy():
            return None
        _, raw = frozen_files.read_file(root, settings_file(client), content=True, limit=1024 * 1024)
        value = strict_json(raw.decode("utf-8-sig"))
        if client == "paseo":
            if type(value.get("version")) is not int or value["version"] != 1:
                return None
            return value.get("agents", {}).get("providers", {})
        return t3_settings.persisted_instances(value)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError, RuntimeError):
        # The daemon record remains observable without usable provider settings.
        return None


def _opencode_route(client, settings, instance, driver):
    """Only the selection in persisted configuration, never historical host identity."""
    if not isinstance(settings, dict):
        return "unverified" if driver == "opencode" else None
    entry = settings.get(instance, {})
    if not isinstance(entry, dict):
        return "unverified" if driver == "opencode" else None
    if client == "t3code":
        configured = entry.get("driver") if instance in settings else instance
        if configured != "opencode":
            return "unverified" if driver == "opencode" else None
        if driver not in (configured, instance, None):
            return "unverified"
        config = entry.get("config", {})
        if not isinstance(config, dict):
            return "unverified"
        url = config.get("serverUrl")
        if url is None:
            return "spawn_on_environment"
        if not isinstance(url, str):
            return "unverified"
        return "configured_server" if url.strip() else "spawn_on_environment"

    # Paseo creates its built-ins before it adds any derived provider IDs.
    if driver != "opencode" and (driver in {"codex", "claude", "pi", "omp", "copilot"}
                                 or entry.get("extends") != "opencode"):
        return None
    base = settings.get("opencode", {})
    base_env = base.get("env", {}) if isinstance(base, dict) else None
    env = entry.get("env", {})
    if not isinstance(base_env, dict) or not isinstance(env, dict):
        return "unverified"
    key = "PASEO_OPENCODE_SERVER_URL"
    url = env.get(key, base_env.get(key, ""))
    if not isinstance(url, str):
        return "unverified"
    if not url.strip():
        # Daemon process environment and healthy local services are runtime inputs.
        return "daemon_runtime_selection"
    try:
        address = urlsplit(url.strip())
        if address.scheme in {"http", "https"} and address.hostname and not (address.username or address.password):
            return "configured_server"
    except ValueError:
        pass
    return "unverified"


def t3_records(root):
    settings = _provider_settings("t3code", root)
    records = {}
    with closing(connect_readonly(database_path(root))) as db:
        db.execute("BEGIN")
        for table in ("projection_threads", V2 + "threads"):
            if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone():
                raise ValueError("t3code_thread_projection_unavailable")
            current = table.startswith(V2)
            extra = """,default_provider,provider_instance_id,
                json_type(payload_json,'$.linkOrigin') AS link_origin,
                json_extract(payload_json,'$.delegatedFrom.environmentId') AS parent_env,
                json_extract(payload_json,'$.delegatedFrom.threadId') AS parent_thread""" if current else ""
            for row in rows(db, f"SELECT thread_id,project_id,archived_at,deleted_at{extra} FROM {table} ORDER BY thread_id"):
                tid = identifier(row.pop("thread_id"))
                record = records.setdefault(tid, {"record_id": tid, "projections": [], "provider_references": []})
                if current:
                    if row.pop("link_origin") == "object":
                        record["peer_link_origin_present"] = True
                    env, parent = row.pop("parent_env"), row.pop("parent_thread")
                    if env is not None or parent is not None:
                        record["delegated_from"] = {"environment_id": identifier(env), "thread_id": identifier(parent)}
                    route = _opencode_route("t3code", settings, row["provider_instance_id"] or row["default_provider"],
                                            row["default_provider"])
                    if route:
                        record["opencode_configured_route"] = route
                record["projections"].append({"table": table, **row})
        for child in rows(db, f"""SELECT thread_id,subagent_id,
                json_extract(payload_json,'$.remoteChild.environmentId') AS environment_id,
                json_extract(payload_json,'$.remoteChild.threadId') AS child_id
                FROM {V2}subagents WHERE json_type(payload_json,'$.remoteChild')='object'
                ORDER BY thread_id,subagent_id"""):
            parent = records.get(child["thread_id"])
            if parent is None:
                raise ValueError("t3code_orphan_remote_task")
            parent.setdefault("remote_children", []).append({"task_id": identifier(child["subagent_id"]),
                "environment_id": identifier(child["environment_id"]), "thread_id": identifier(child["child_id"])})
        current, legacy = provider_rows(db)
        for row in (*current, *legacy):
            if row["thread_id"] not in records:
                raise ValueError("t3code_orphan_provider_projection")
            refs = records[row["thread_id"]]["provider_references"]
            if "native_id" in row:
                driver = row["driver"] or row["provider"]
                ref = {"provider": driver, "instance_id": row["provider_instance_id"], "locator": row["native_id"],
                       "kind": "nativeThreadRef", "strength": row["native_strength"]}
                route = _opencode_route("t3code", settings, row["provider_instance_id"], driver)
                if route:
                    ref["opencode_configured_route"] = route
                refs.append(ref)
            else:
                refs.extend({"provider": row["provider_name"], "kind": field, "locator": row[field]}
                            for field in ("provider_thread_id", "provider_session_id") if row[field] is not None)
    return [records[tid] for tid in sorted(records)]


def paseo_records(root):
    from .paseo_store import PaseoAdapter
    settings = _provider_settings("paseo", root)
    adapter = PaseoAdapter(profile_root=root)
    if any(error.blocks_inventory for error in adapter.snapshot_references().errors):
        raise ValueError("paseo_registry_inventory_incomplete")
    records = []
    for row in adapter.observations:
        record = {"record_id": row["id"], "provider": row["provider"], "status": row["lastStatus"],
                  **{k: row[k] for k in ("references", "opaque_session_handle_present",
                                         "opaque_native_handle_present", "native_handle_projected")}}
        if row["owner"]:
            record["hub_execution"] = row["owner"]
        if row["parent_agent_id"]:
            record["parent_agent"] = {"agent_id": row["parent_agent_id"], "server_id": row["parent_server_id"]}
        route = _opencode_route("paseo", settings, row["provider"], row["provider"])
        if route:
            record["opencode_configured_route"] = route
        records.append(record)
    return records


def require_native_route(record):
    routes = {record.get("opencode_configured_route")}
    routes.update(ref.get("opencode_configured_route") for ref in record.get("provider_references", ()))
    if "configured_server" in routes:
        raise ValueError("external_opencode_native_store_unverified")
    if routes - {None}:
        raise ValueError("opencode_native_writer_unqualified")
