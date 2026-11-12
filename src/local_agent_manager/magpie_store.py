"""Magpie 0.1.3828 record formats; no native deletion and no host-global roots.

Source contracts: yetone/magpie v0.1.3828 internal/appdir, sessions,
usage/usage.go and gateway/history.go. Evidence contains identities and hashes,
never titles, prompts, transcript contents or configuration credentials.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re

from . import frozen_files as ff
from .record_identity import canonical_path
from .path_identity import is_local_absolute_locator

SCHEMA = "lam.magpie-bindings.v1"
STORE_SCHEMA = "lam.magpie-records.v1"
MAX_BYTES = ff.MAX_REWRITE_BYTES
DERIVED = ("log-v1.bin", "requests-v1.bin", "page-v1.json", "heat-v1.json")
ENGINES = {"codex": "codex", "claude": "claude", "pi": "pi", "agy": "antigravity"}


class MagpieError(ValueError):
    def __init__(self, code):
        self.kind = "magpie_" + code
        super().__init__(self.kind)


def fail(code):
    raise MagpieError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def path(value):
    if not isinstance(value, str) or not is_local_absolute_locator(value):
        fail("absolute_local_path_required")
    p = Path(value)
    if str(p) != os.path.abspath(p) or any(ord(c) < 32 for c in value):
        fail("path_alias")
    return p


def text(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        fail("identity_invalid")
    return value


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("duplicate_json_key")
            result[key] = value
        return result
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            fail("json_number_invalid")
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_float=finite, parse_constant=lambda _: fail("json_invalid"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, MagpieError):
            raise
        raise MagpieError("json_invalid") from None


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def identity(agent, session):
    # encoding/json escapes HTML and U+2028/U+2029 even for string arrays.
    raw = json.dumps([agent, session], ensure_ascii=False, separators=(",", ":"))
    for char, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                          ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        raw = raw.replace(char, escaped)
    return hashlib.sha256(raw.encode()).hexdigest()


def agent_of(value):
    # Only qualified names are normalized. Unknown plugin/user-agent names
    # remain distinct, never guessed by a substring or a project directory.
    if value == "":
        return "other"
    name = text(value).strip().split("/", 1)[0].split(" ", 1)[0]
    return {"claude": "claude", "codex": "codex", "pi": "pi", "cindy": "cindy", "agy": "agy",
            "claude-code": "claude", "claude-cli": "claude",
            "pi-coding-agent": "pi",
            "codex_cli_rs": "codex", "codex-cli": "codex"}.get(name.lower(), name)


def resolve_roots(*, home, launch_env=None, executable=None, config_root=None, cache_root=None):
    """Use explicit profile roots or the *Magpie launcher's* environment.

    Never read os.environ here: the manager's inherited XDG values do not
    prove the desktop launch context. Unmarked portable candidates are left
    for an explicit profile binding instead of guessing OS shared folders.
    """
    env = dict(launch_env or {})
    if config_root is not None or cache_root is not None:
        if config_root is None or cache_root is None:
            fail("both_roots_required")
        return {"config_root": str(path(str(config_root))), "cache_root": str(path(str(cache_root))), "selection": "explicit"}
    home = path(str(home))
    if executable:
        base = path(str(executable)).parent
        marker, data = base / ".portable", base / "data"
        if marker.exists():
            ff._plain(marker.lstat())
            return {"config_root": str(data), "cache_root": str(data / "cache"), "selection": "portable_marker"}
        if data.exists():
            fail("portable_roots_require_explicit_binding")
    config = path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    cache = path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache"
    return {"config_root": str(config / "magpie"), "cache_root": str(cache / "magpie"), "selection": "launch_environment"}


def validate_manifest(value, *, live=True):
    if not isinstance(value, dict) or set(value) != {"schema_version", "profiles"} or value["schema_version"] != SCHEMA:
        fail("bindings_schema_invalid")
    if not isinstance(value["profiles"], list) or not 1 <= len(value["profiles"]) <= 8:
        fail("profile_count_invalid")
    seen = set()
    for p in value["profiles"]:
        if not isinstance(p, dict) or set(p) != {"config_root", "cache_root", "runtime_binaries", "records"}:
            fail("profile_schema_invalid")
        roots = [path(p[k]) for k in ("config_root", "cache_root")]
        for root in roots:
            key = canonical_path(root)
            if key in seen:
                fail("shared_profile_root")
            seen.add(key)
            if live:
                ff._plain(root.lstat(), directory=True)
        if not isinstance(p["runtime_binaries"], list) or not p["runtime_binaries"]:
            fail("runtime_binding_required")
        for binary in p["runtime_binaries"]:
            b = path(binary)
            if live:
                ff.read_file(b.parent, b.name)
        if not isinstance(p["records"], list) or not p["records"]:
            fail("record_binding_required")
        keys, records = set(), set()
        for r in p["records"]:
            if not isinstance(r, dict) or set(r) != {"engine", "root", "id", "gateway_keys"}:
                fail("record_schema_invalid")
            if r["engine"] not in {"codex", "claude", "pi", "antigravity"}:
                fail("engine_unqualified")
            key = r["engine"], canonical_path(path(r["root"])), text(r["id"])
            if key in records:
                fail("duplicate_native_binding")
            records.add(key)
            if not isinstance(r["gateway_keys"], list):
                fail("gateway_keys_invalid")
            for k in r["gateway_keys"]:
                if not isinstance(k, dict) or set(k) != {"agent", "id"}:
                    fail("gateway_key_invalid")
                key = text(k["agent"]), text(k["id"])
                if key in keys:
                    fail("gateway_identity_shared")
                keys.add(key)
    return value


def _read_optional(root, relative):
    try:
        return ff.read_file(root, relative, content=True, limit=MAX_BYTES)
    except FileNotFoundError:
        return None, None


def _entries(root, relative, directories):
    p = ff.checked_path(root, relative)
    try:
        info = p.lstat()
    except FileNotFoundError:
        directories.append({"path": relative, "absent": True})
        return []
    ff._plain(info, directory=True)
    names = sorted(x.name for x in p.iterdir())
    if len(names) > ff.MAX_ENTRIES:
        fail("entry_budget_exceeded")
    directories.append({"path": relative, "identity": ff._identity(info), "entries": names})
    return names


def _lines(raw, compressed=False):
    if compressed:
        import io
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(MAX_BYTES + 1)
        except (OSError, EOFError):
            fail("routing_gzip_invalid")
    if len(raw) > MAX_BYTES:
        fail("record_budget_exceeded")
    result = []
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            result.append((line, None))
            continue
        value = decode(line)
        if not isinstance(value, dict):
            fail("record_shape_invalid")
        result.append((line, value))
    return result


def _within(value, root):
    candidate = path(value)
    return canonical_path(candidate) == canonical_path(root) or canonical_path(root) in {
        canonical_path(p) for p in candidate.parents}


def selected_cache(profile, cache, source_paths):
    if not isinstance(cache, dict) or set(cache) - {"version", "zone", "parsers", "files"} or cache.get("version") != 29 or not isinstance(cache.get("files"), dict):
        fail("summary_cache_schema_unqualified")
    selected = []
    for locator, row in cache["files"].items():
        if not isinstance(row, dict):
            fail("summary_cache_row_invalid")
        for r in profile["records"]:
            if row.get("id") != r["id"]:
                continue
            backend = "pi" if row.get("agent") == "cindy" else ENGINES.get(row.get("agent"))
            if backend != r["engine"]:
                continue
            if not _within(locator, path(r["root"])):
                # Same bare native ID in another home cannot supply ownership.
                fail("native_identity_ambiguous_across_homes")
            if canonical_path(locator) not in set(map(canonical_path, source_paths)):
                fail("native_source_path_unproven")
            selected.append(locator)
    return sorted(set(selected))


def route_selected(row, keys, route_ids):
    return row is not None and (row.get("id") in route_ids or
        (agent_of(row.get("agent", "other")), row.get("conv")) in keys)


def freeze(profile, source_paths=()):
    """Freeze all selected formats and their namespace; preserve other bytes."""
    config, cache_root = path(profile["config_root"]), path(profile["cache_root"])
    for entry in config.iterdir():
        if any(entry.name.startswith(name + ".") for name in ("usage.jsonl", "gateway-sessions-hidden.json")):
            fail("history_sidecar_unqualified")
    for entry in cache_root.iterdir():
        if (re.match(r"(?:log|requests|page|heat)-v", entry.name) and entry.name not in DERIVED
                or entry.name.startswith("sessions.json.")):
            fail("derived_cache_version_unqualified")
    keys = {(k["agent"], k["id"]) for r in profile["records"] for k in r["gateway_keys"]}
    hashes = {identity(*k) for k in keys}
    known_keys = {(agent, r["id"]) for r in profile["records"]
                  for agent in (("pi", "cindy") if r["engine"] == "pi" else (r["engine"],))}
    unbound_hashes = {identity(*k) for k in known_keys - keys}
    evidence = {"schema_version": STORE_SCHEMA, "profile": profile, "source_paths": list(source_paths),
                "stores": [], "counts": {}, "paths": [], "route_ids": []}
    stores = {"config": {"root": str(config), "guards": [], "directories": [], "changes": []},
              "cache": {"root": str(cache_root), "guards": [], "directories": [], "changes": []}}
    def inspect(kind, relative):
        proof, raw = _read_optional(path(stores[kind]["root"]), relative)
        stores[kind]["guards"].append(proof or {"path": relative, "absent": True})
        return raw
    def rewrite(kind, relative, mode, raw, after, count):
        if raw == after:
            return
        change = ff.freeze_rewrite(path(stores[kind]["root"]), relative, lambda _: after)
        change["format"] = mode
        stores[kind]["changes"].append(change)
        evidence["counts"][mode] = evidence["counts"].get(mode, 0) + count
    def remove(kind, relative, category):
        stores[kind]["changes"].append(ff.freeze_remove(path(stores[kind]["root"]), relative))
        evidence["counts"][category] = evidence["counts"].get(category, 0) + 1
    for name in ("settings.json", "applied.json", "gateway.json"):
        inspect("config", name)
    raw = inspect("cache", "sessions.json")
    if raw is not None:
        parsed = decode(raw)
        evidence["paths"] = selected_cache(profile, parsed, source_paths)
        if evidence["paths"]:
            parsed["files"] = {k: v for k, v in parsed["files"].items() if k not in evidence["paths"]}
            rewrite("cache", "sessions.json", "summary", raw, encode(parsed), len(evidence["paths"]))
    # Unmapped matching ledger IDs must not silently disappear from scope.
    raw = inspect("config", "usage.jsonl")
    if raw is not None:
        kept, count, routes, retained_routes = [], 0, set(), set()
        for line, row in _lines(raw):
            if row is None:
                kept.append(line); continue
            key = (agent_of(row.get("agent", "other")), row.get("session"))
            if key in keys:
                bindings = [r for r in profile["records"] if {"agent": key[0], "id": key[1]} in r["gateway_keys"]]
                if (len(bindings) != 1 or row.get("native_session") not in (None, "", bindings[0]["id"])
                        or row.get("native_session") != bindings[0]["id"] and (key[1] != bindings[0]["id"]
                            or ENGINES.get(key[0], "pi" if key[0] == "cindy" else None) != bindings[0]["engine"])):
                    fail("gateway_native_mapping_unproven")
                if row.get("archive"):
                    fail("request_archive_scope_unqualified")
                count += 1
                if row.get("route_id"):
                    routes.add(text(row["route_id"]))
            else:
                if any(row.get("native_session") == r["id"] or row.get("session") == r["id"] for r in profile["records"]):
                    fail("gateway_identity_unmapped")
                kept.append(line)
                if row.get("route_id"):
                    retained_routes.add(text(row["route_id"]))
        if routes & retained_routes:
            fail("route_identity_shared")
        evidence["route_ids"] = sorted(routes)
        rewrite("config", "usage.jsonl", "usage", raw, b"".join(kept), count)
    for name in _entries(config, "routing", stores["config"]["directories"]):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl(?:\.gz)?", name):
            fail("routing_layout_unqualified")
        rel = "routing/" + name
        raw = inspect("config", rel)
        kept, count = [], 0
        for line, row in _lines(raw, name.endswith(".gz")):
            if row is not None and (agent_of(row.get("agent", "other")), row.get("conv")) in known_keys - keys:
                fail("gateway_identity_unmapped")
            selected = route_selected(row, keys, evidence["route_ids"])
            if selected and row.get("conv") and (agent_of(row.get("agent", "other")), row["conv"]) not in keys:
                fail("route_identity_conflict")
            if selected:
                count += 1
            else:
                kept.append(line)
        if count:
            after = b"".join(kept)
            if name.endswith(".gz"):
                after = gzip.compress(after, mtime=0)
            rewrite("config", rel, "routing_gzip" if name.endswith(".gz") else "routing", raw, after, count)
    raw = inspect("config", "gateway-sessions-hidden.json")
    if raw is not None:
        rows = decode(raw)
        if not isinstance(rows, list) or any(not isinstance(r, dict) or set(r) != {"agent", "id", "last"} for r in rows):
            fail("hidden_schema_unqualified")
        if any((r["agent"], r["id"]) in known_keys - keys for r in rows):
            fail("gateway_identity_unmapped")
        kept = [r for r in rows if (r["agent"], r["id"]) not in keys]
        if len(kept) != len(rows):
            rewrite("config", "gateway-sessions-hidden.json", "hidden", raw, encode(kept), len(rows)-len(kept))
    for day in _entries(config, "gateway-conversations", stores["config"]["directories"]):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            fail("conversation_layout_unqualified")
        rel = "gateway-conversations/" + day
        for key in _entries(config, rel, stores["config"]["directories"]):
            if not re.fullmatch(r"[0-9a-f]{64}", key):
                fail("conversation_identity_invalid")
            if key in unbound_hashes:
                fail("gateway_identity_unmapped")
            if key in hashes:
                bucket = rel + "/" + key
                for name in _entries(config, bucket, stores["config"]["directories"]):
                    raw = inspect("config", bucket + "/" + name)
                    if name == "turns.jsonl":
                        turns = [r for _, r in _lines(raw) if r is not None]
                    elif name.endswith(".json"):
                        turns = [decode(raw)]
                    else:
                        fail("conversation_file_unqualified")
                    if any(not isinstance(r, dict) or (r.get("agent"), r.get("session")) not in keys
                           or identity(r["agent"], r["session"]) != key for r in turns):
                        fail("conversation_identity_conflict")
                remove("config", bucket, "recorded_conversations")
    for agent in _entries(config, "trash/sessions", stores["config"]["directories"]):
        group = "trash/sessions/" + agent
        for name in _entries(config, group, stores["config"]["directories"]):
            rel = group + "/" + name
            raw = inspect("config", rel + "/session.json")
            if raw is None:
                fail("trash_manifest_missing")
            row = decode(raw)
            if (not isinstance(row, dict) or set(row) - {"key", "agent", "id", "title", "cwd", "last", "deleted",
                    "size", "items", "gateway", "forgot", "codex_catalog"} or not isinstance(row.get("items"), list)
                    or any(not isinstance(i, dict) or set(i) != {"from", "name"} for i in row["items"])):
                fail("trash_manifest_unqualified")
            if row.get("gateway") and (row.get("agent"), row.get("id")) in known_keys - keys:
                fail("gateway_identity_unmapped")
            matched = (row.get("agent"), row.get("id")) in keys and (row.get("gateway") is True or row.get("forgot") is True)
            if matched and row.get("gateway"):
                if agent != "gateway":
                    fail("trash_gateway_layout_unqualified")
                for item in row["items"]:
                    if not isinstance(item, dict):
                        fail("trash_gateway_mapping_unproven")
                    original = path(item.get("from"))
                    if (original.name != identity(row["agent"], row["id"])
                            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", original.parent.name)
                            or canonical_path(original.parent.parent) != canonical_path(config / "gateway-conversations")):
                        fail("trash_gateway_mapping_unproven")
            if matched and row.get("forgot") and row["items"]:
                fail("trash_forgot_items_unqualified")
            for r in profile["records"]:
                backend = "pi" if row.get("agent") == "cindy" else ENGINES.get(row.get("agent"))
                if backend == r["engine"] and row.get("id") == r["id"] and not row.get("gateway") and not row.get("forgot"):
                    if not row["items"] or any(not isinstance(i, dict) or not _within(i.get("from"), path(r["root"])) for i in row["items"]):
                        fail("trash_native_mapping_unproven")
                    if r["engine"] == "pi" and any(canonical_path(i["from"]) not in set(map(canonical_path, source_paths)) for i in row["items"]):
                        fail("trash_pi_source_path_unproven")
                    matched = True
            if matched:
                catalog = row.get("codex_catalog", [])
                if not isinstance(catalog, list) or catalog and (row.get("agent") != "codex" or row.get("gateway") or row.get("forgot")):
                    fail("trash_catalog_scope_unproven")
                allowed_ids = {r["id"] for r in profile["records"] if r["engine"] == "codex"}
                for saved in catalog:
                    if (not isinstance(saved, dict) or set(saved) != {"db", "cols", "vals"}
                            or path(saved["db"]).name not in {"codex.db", "codex-dev.db"}
                            or not isinstance(saved["cols"], list) or not isinstance(saved["vals"], list)
                            or any(not isinstance(c, str) for c in saved["cols"])
                            or len(set(saved["cols"])) != len(saved["cols"]) or len(saved["cols"]) != len(saved["vals"])
                            or "thread_id" not in saved["cols"]
                            or any(not isinstance(v, dict) or set(v) - {"s", "i", "r", "b"} or len(v) > 1 for v in saved["vals"])):
                        fail("trash_catalog_scope_unproven")
                    value = saved["vals"][saved["cols"].index("thread_id")]
                    if value.get("s") not in allowed_ids:
                        fail("trash_catalog_retained_record")
                remove("config", rel, "trash_sessions")
    shards = {hashlib.sha256(p.encode()).hexdigest() for p in (*evidence["paths"], *source_paths)}
    versions = sorted(p.name for p in cache_root.iterdir() if p.name.startswith("calls-v"))
    if any(name != "calls-v15" for name in versions):
        fail("call_cache_version_unqualified")
    for name in _entries(cache_root, "calls-v15", stores["cache"]["directories"]):
        if not re.fullmatch(r"[0-9a-f]{64}\.(gob|rev)", name):
            fail("call_cache_layout_unqualified")
        if name.split(".")[0] in shards:
            remove("cache", "calls-v15/" + name, "call_shards")
    # These four files are derived indexes, not user content. Their whole
    # invalidation is explicit in the plan; unrelated ledger lines survive.
    for name in DERIVED:
        if inspect("cache", name) is not None:
            remove("cache", name, "derived_indexes")
    evidence["stores"] = list(stores.values())
    return evidence


def transform(evidence, change, raw):
    profile = evidence["profile"]
    keys = {(k["agent"], k["id"]) for r in profile["records"] for k in r["gateway_keys"]}
    kind = change["format"]
    if kind == "summary":
        value = decode(raw)
        value["files"] = {k: v for k, v in value["files"].items() if k not in evidence["paths"]}
        return encode(value)
    if kind == "hidden":
        return encode([r for r in decode(raw) if (r["agent"], r["id"]) not in keys])
    if kind not in {"usage", "routing", "routing_gzip"}:
        fail("transform_unqualified")
    kept = []
    for line, row in _lines(raw, kind == "routing_gzip"):
        selected = (row is not None and (agent_of(row.get("agent", "other")), row.get("session")) in keys
                    if kind == "usage" else route_selected(row, keys, evidence["route_ids"]))
        if not selected:
            kept.append(line)
    result = b"".join(kept)
    return gzip.compress(result, mtime=0) if kind == "routing_gzip" else result


def check_before(evidence):
    if freeze(evidence["profile"], evidence["source_paths"]) != evidence:
        fail("store_changed")


def remaining(evidence):
    """Exact frozen after-state plus a fresh scan catches resurrected records."""
    if any(not ff.satisfied(path(s["root"]), c) for s in evidence["stores"] for c in s["changes"]):
        return True
    # The fresh scan still retains unrelated records; cache invalidation alone
    # cannot hide gateway/trash records added after the frozen snapshot.
    fresh = freeze(evidence["profile"], evidence["source_paths"])
    return any(s["changes"] for s in fresh["stores"])
