"""Read-only, explicit-launch-context Magpie bindings preparation."""
from pathlib import Path

from . import magpie_store as files, magpie_cleanup as cleanup, frozen_files as ff
from .operation_store import plan_sha256, write_new_json


def read_json(path):
    p = files.path(str(Path(path).absolute()))
    return files.decode(ff.read_file(p.parent, p.name, content=True, limit=files.MAX_BYTES)[1])


def prepare(document, *, home=None, launch_env=None, executable=None, config_root=None, cache_root=None,
            runtime_binaries=()):
    if (document.get("document_type") != "operation_plan" or document.get("goal_status") != "ready"
            or document.get("plan_sha256") != plan_sha256(document) or cleanup.evidences(document)):
        files.fail("ready_native_plan_required")
    roots = files.resolve_roots(home=home, launch_env=launch_env, executable=executable,
                               config_root=config_root, cache_root=cache_root)
    native = cleanup.native_bindings(document)
    records = [{"engine": engine, "root": root, "id": rid, "gateway_keys": [{"agent": engine, "id": rid}]}
               for engine, root, rid in sorted(native)]
    profile = {k: roots[k] for k in ("config_root", "cache_root")}
    profile.update(runtime_binaries=[str(files.path(str(b))) for b in runtime_binaries], records=records)
    paths = sorted({p for v in native.values() for p in v["paths"]})
    summary = files._read_optional(Path(roots["cache_root"]), "sessions.json")[1]
    if summary is not None:
        summary = files.decode(summary)
        for locator in files.selected_cache(profile, summary, paths):
            row = summary["files"][locator]
            for record in records:
                if record["id"] == row["id"] and files._within(locator, Path(record["root"])):
                    key = {"agent": row["agent"], "id": row["id"]}
                    if key not in record["gateway_keys"]:
                        record["gateway_keys"].append(key)
    raw = files._read_optional(Path(roots["config_root"]), "usage.jsonl")[1]
    for _, row in files._lines(raw or b""):
        if row is None:
            continue
        matches = [r for r in records if row.get("native_session") == r["id"] or
                   row.get("session") == r["id"] and files.agent_of(row.get("agent", "other")) == r["engine"]]
        if len(matches) > 1:
            files.fail("gateway_identity_ambiguous_across_homes")
        if matches:
            key = {"agent": files.agent_of(row.get("agent", "other")), "id": files.text(row.get("session"))}
            if key not in matches[0]["gateway_keys"]:
                matches[0]["gateway_keys"].append(key)
    manifest = files.validate_manifest({"schema_version": files.SCHEMA, "profiles": [profile]})
    cleanup._bind(manifest, document)
    cleanup._runtime(profile)
    evidence = files.freeze(profile, paths)
    return {"schema_version": "lam.magpie-prepare.v1", "goal_status": "ready", "modified": False,
            "native_plan_sha256": document["plan_sha256"], "root_selection": roots["selection"],
            "bindings": manifest, "counts": evidence["counts"],
            "excluded": ["remote_sync_and_backups", "request_archives", "webview_and_os_caches", "other_profiles"]}


def add_commands(commands):
    parser = commands.add_parser("magpie-cleanup", help="从原生清理计划和实际 Magpie 启动配置准备关联映射")
    actions = parser.add_subparsers(dest="magpie_cleanup_action", required=True)
    parser = actions.add_parser("prepare", help="只读解析；可另存绑定文件，不删除会话")
    parser.add_argument("--native-plan", type=Path, required=True)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--home", type=Path, help="Magpie 进程实际使用的用户 Home；不使用管理器的环境推断")
    parser.add_argument("--launch-environment", type=Path, help="已核实的 Magpie 启动环境 JSON，仅接受 XDG_CONFIG_HOME/XDG_CACHE_HOME")
    parser.add_argument("--runtime-binary", type=Path, action="append", required=True,
                        help="实际 Magpie 程序；Windows exe/com 都须绑定，可重复")
    parser.add_argument("--out", type=Path, help="另存绑定文件；已有文件不覆盖")
    parser.add_argument("--json", action="store_true")


def run(args, *, stdout, stderr):
    import json
    try:
        env = read_json(args.launch_environment) if args.launch_environment else {}
        if not isinstance(env, dict) or set(env) - {"XDG_CONFIG_HOME", "XDG_CACHE_HOME"}:
            files.fail("launch_environment_unqualified")
        result = prepare(read_json(args.native_plan), home=args.home, launch_env=env,
            executable=args.runtime_binary[0], config_root=args.config_root, cache_root=args.cache_root,
            runtime_binaries=args.runtime_binary)
        if args.out:
            write_new_json(args.out.absolute(), result["bindings"])
            result["bindings_path"] = str(args.out.absolute())
        code = 0
    except Exception as exc:
        result = {"goal_status": "blocked", "modified": False,
                  "blockers": [{"blocker_code": getattr(exc, "kind", "magpie_discovery_failed")} ]}
        code = 2
    stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return code
