import copy
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent_manager import magpie_store as files, magpie_cleanup as cleanup, frozen_files
from local_agent_manager.cleanup_service import CleanupService
from local_agent_manager.operation_coordinator import OperationCoordinator
from local_agent_manager.t3code_bound_adapter import T3CodeBoundAdapter, SCHEMA
from local_agent_manager import t3code_lifecycle
from tests.t3code_support import create_store, add_thread, V2

FRONT = "11111111-1111-4111-8111-111111111111"
KEEP_FRONT = "22222222-2222-4222-8222-222222222222"
NATIVE = "33333333-3333-4333-8333-333333333333"
KEEP = "44444444-4444-4444-8444-444444444444"
PRIVATE = "DO_NOT_PUBLISH_TRANSCRIPT_OR_CREDENTIAL"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else files.encode(value))


def seed_magpie(base, native_root, selected_path, keeper_path, engine="claude", selected_id=NATIVE, keep_id=KEEP):
    config, cache = base / "magpie-config", base / "magpie-cache"
    config.mkdir(); cache.mkdir()
    binary = base / "synthetic-magpie.exe"
    binary.write_bytes(b"test executable identity; never launched")
    profile = {"config_root": str(config), "cache_root": str(cache), "runtime_binaries": [str(binary)],
        "records": [{"engine": engine, "root": str(native_root), "id": selected_id,
                     "gateway_keys": [{"agent": engine, "id": selected_id}]}]}
    write(config / "settings.json", {"secret": PRIVATE})
    write(cache / "sessions.json", {"version": 29, "zone": "UTC", "parsers": {}, "files": {
        str(selected_path): {"agent": engine, "id": selected_id, "title": PRIVATE},
        str(keeper_path): {"agent": engine, "id": keep_id, "title": "keep"}}})
    kept = b'{ "agent":"' + engine.encode() + b'", "session":"' + keep_id.encode() + b'", "route_id":"keep-route", "private":"keep" }\r\n'
    write(config / "usage.jsonl", files.encode({"agent": engine, "session": selected_id, "native_session": selected_id,
        "route_id": "target-route", "prompt": PRIVATE}) + kept)
    routed = files.encode({"agent": engine, "conv": selected_id, "id": "target-route"})
    keep_route = b'{ "agent":"' + engine.encode() + b'", "conv":"' + keep_id.encode() + b'", "id":"keep-route" }\r\n'
    write(config / "routing/2026-11-12.jsonl", routed + keep_route)
    write(config / "routing/2026-11-11.jsonl.gz", gzip.compress(routed + keep_route))
    write(config / "gateway-sessions-hidden.json", [{"agent": engine, "id": selected_id, "last": "time"},
        {"agent": engine, "id": keep_id, "last": "time"}])
    bucket = "gateway-conversations/2026-11-12/" + files.identity(engine, selected_id)
    write(config / bucket / "turns.jsonl", {"agent": engine, "session": selected_id, "input": PRIVATE})
    for kind in ("native", "gateway"):
        trash = config / ("trash/sessions/gateway" if kind == "gateway" else "trash/sessions/" + engine) / kind
        write(trash / "session.json", {"agent": engine, "id": selected_id, "gateway": kind == "gateway",
            "items": [{"from": str(selected_path) if kind == "native" else str(config / bucket), "name": "files/0"}]})
        write(trash / "files/0", PRIVATE.encode())
    for p in (selected_path, keeper_path):
        for ext in ("gob", "rev"):
            write(cache / "calls-v15" / (hashlib.sha256(str(p).encode()).hexdigest() + "." + ext), PRIVATE.encode())
    for name in files.DERIVED:
        write(cache / name, PRIVATE.encode())
    return profile, kept, keep_route


class MagpieFilesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.native = self.base / "native"; self.native.mkdir()
        self.selected = self.native / (NATIVE + ".jsonl")
        self.keep = self.native / (KEEP + ".jsonl")
        self.profile, self.kept, self.keep_route = seed_magpie(self.base, self.native, self.selected, self.keep)
        self.config, self.cache = Path(self.profile["config_root"]), Path(self.profile["cache_root"])

    def freeze(self):
        return files.freeze(self.profile, [str(self.selected)])

    def test_exact_formats_keep_unrelated_bytes_and_never_native_files(self):
        write(self.selected, PRIVATE.encode())
        evidence = self.freeze()
        self.assertNotIn(PRIVATE, json.dumps(evidence))
        self.assertEqual(evidence["counts"]["trash_sessions"], 2)
        for store in evidence["stores"]:
            for change in store["changes"]:
                if change["kind"] == "rewrite":
                    frozen_files.apply_rewrite(Path(store["root"]), change, lambda raw: files.transform(evidence, change, raw))
                else:
                    frozen_files.apply_remove(Path(store["root"]), change)
        self.assertEqual((self.config / "usage.jsonl").read_bytes(), self.kept)
        self.assertEqual((self.config / "routing/2026-11-12.jsonl").read_bytes(), self.keep_route)
        self.assertEqual(gzip.decompress((self.config / "routing/2026-11-11.jsonl.gz").read_bytes()), self.keep_route)
        self.assertEqual(self.selected.read_bytes(), PRIVATE.encode())
        self.assertEqual(len(list((self.cache / "calls-v15").iterdir())), 2)
        self.assertFalse(files.remaining(evidence))

    def test_root_resolution_uses_launch_config_and_portable_precedence(self):
        self.assertEqual(files.agent_of("pi-coding-agent/1.2"), "pi")
        self.assertEqual(files.agent_of("pi-desktop/1.2"), "pi-desktop")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "must-not-be-used"}):
            roots = files.resolve_roots(home=self.base, launch_env={"XDG_CONFIG_HOME": str(self.base / "xdg")})
            self.assertEqual(roots["config_root"], str(self.base / "xdg/magpie"))
        (self.base / ".portable").touch()
        roots = files.resolve_roots(home=self.native, executable=self.base / "magpie.exe")
        self.assertEqual(roots["cache_root"], str(self.base / "data/cache"))
        with self.assertRaises(files.MagpieError):
            files.resolve_roots(home=self.base, config_root=self.config)

    def test_same_native_id_in_other_home_or_unselected_pi_path_blocks(self):
        for outside in (self.base / "other/record.jsonl", self.native / "other.jsonl"):
            cache = files.decode((self.cache / "sessions.json").read_bytes())
            cache["files"][str(outside)] = {"agent": "claude", "id": NATIVE}
            write(self.cache / "sessions.json", cache)
            with self.assertRaises(files.MagpieError):
                self.freeze()
            del cache["files"][str(outside)]; write(self.cache / "sessions.json", cache)

    def test_malformed_ledger_unknown_versions_and_shared_routes_block(self):
        ledger = (self.config / "usage.jsonl").read_bytes()
        write(self.config / "usage.jsonl", ledger + b'{"broken":\n')
        with self.assertRaises(files.MagpieError): self.freeze()
        write(self.config / "usage.jsonl", ledger.replace(b'keep-route', b'target-route'))
        with self.assertRaisesRegex(files.MagpieError, "route_identity_shared"): self.freeze()
        write(self.config / "usage.jsonl", ledger)
        (self.cache / "calls-v14").mkdir()
        with self.assertRaisesRegex(files.MagpieError, "version_unqualified"): self.freeze()

    def test_mapping_config_and_after_state_drift_fail_closed(self):
        before = self.freeze()
        write(self.config / "settings.json", {"changed": True})
        with self.assertRaisesRegex(files.MagpieError, "store_changed"): files.check_before(before)
        ledger = (self.config / "usage.jsonl").read_bytes().replace(NATIVE.encode(), KEEP.encode(), 1)
        write(self.config / "usage.jsonl", ledger)
        with self.assertRaisesRegex(files.MagpieError, "gateway_identity_unmapped"): self.freeze()

    def test_gateway_bucket_conflict_and_trash_outside_root_block(self):
        bucket = self.config / "gateway-conversations/2026-11-12" / files.identity("claude", NATIVE)
        write(bucket / "turns.jsonl", {"agent": "claude", "session": KEEP})
        with self.assertRaisesRegex(files.MagpieError, "conversation_identity_conflict"): self.freeze()
        write(bucket / "turns.jsonl", {"agent": "claude", "session": NATIVE})
        write(self.config / "trash/sessions/claude/native/session.json",
            {"agent": "claude", "id": NATIVE, "items": [{"from": str(self.base / "foreign"), "name": "files/0"}]})
        with self.assertRaisesRegex(files.MagpieError, "trash_native_mapping_unproven"): self.freeze()

    def test_archive_reference_never_disappears_without_qualified_archive_writer(self):
        write(self.config / "usage.jsonl", {"agent": "claude", "session": NATIVE, "archive": "date/id"})
        with self.assertRaisesRegex(files.MagpieError, "request_archive_scope_unqualified"): self.freeze()

    def test_unknown_derived_version_and_unparsed_history_sidecars_block(self):
        write(self.cache / "log-v2.bin", b"unknown")
        with self.assertRaisesRegex(files.MagpieError, "derived_cache_version_unqualified"): self.freeze()
        (self.cache / "log-v2.bin").unlink()
        write(self.config / "gateway-sessions-hidden.json.unparsed", b"recovery")
        with self.assertRaisesRegex(files.MagpieError, "history_sidecar_unqualified"): self.freeze()

    def test_hardlink_and_malformed_gzip_block(self):
        extra = self.config / "linked"
        os.link(self.config / "usage.jsonl", extra)
        with self.assertRaises(frozen_files.FrozenFilesError): self.freeze()
        extra.unlink()
        write(self.config / "routing/2026-11-11.jsonl.gz", b"broken gzip")
        with self.assertRaisesRegex(files.MagpieError, "routing_gzip_invalid"): self.freeze()

    def test_routing_without_usage_match_and_case_sensitive_hash(self):
        write(self.config / "routing/2026-11-12.jsonl", {"agent": "claude", "conv": NATIVE, "id": "route-without-ledger"})
        evidence = self.freeze()
        self.assertEqual(evidence["counts"]["routing"], 1)
        self.assertEqual(files.identity("a<", "b\u2028"), hashlib.sha256(b'["a\\u003c","b\\u2028"]').hexdigest())

    def test_gateway_trash_source_is_bound_to_profile_and_forgot_has_no_payload(self):
        manifest = self.config / "trash/sessions/gateway/gateway/session.json"
        row = files.decode(manifest.read_bytes())
        row["items"][0]["from"] = str(self.base / "foreign/2026-11-12" / files.identity("claude", NATIVE))
        write(manifest, row)
        with self.assertRaisesRegex(files.MagpieError, "trash_gateway_mapping_unproven"): self.freeze()
        row.update(items=[], gateway=False, forgot=True)
        write(manifest, row)
        self.assertEqual(self.freeze()["counts"]["trash_sessions"], 2)
        with self.assertRaises(files.MagpieError): files.decode(b'{"n":1e999}')

    def test_codex_catalog_backup_cannot_include_retained_thread(self):
        base = self.base / "codex"; base.mkdir()
        profile, _, _ = seed_magpie(base, self.native, self.selected, self.keep, engine="codex")
        manifest = Path(profile["config_root"]) / "trash/sessions/codex/native/session.json"
        row = files.decode(manifest.read_bytes())
        row["codex_catalog"] = [{"db": str(base / "desktop/codex.db"), "cols": ["thread_id", "title"],
                                 "vals": [{"s": KEEP}, {"s": PRIVATE}]}]
        write(manifest, row)
        with self.assertRaisesRegex(files.MagpieError, "trash_catalog_retained_record"):
            files.freeze(profile, [str(self.selected)])
        row["codex_catalog"][0]["vals"][0] = {"s": NATIVE}
        write(manifest, row)
        evidence = files.freeze(profile, [str(self.selected)])
        self.assertEqual(evidence["counts"]["trash_sessions"], 2)
        self.assertNotIn(PRIVATE, json.dumps(evidence))


@unittest.skipUnless(os.name == "nt", "Windows startup fences")
class MagpieOperationTests(unittest.TestCase):
    def setUp(self):
        # A nested gateway hash exceeds MAX_PATH beneath Windows AppContainer
        # TEMP. Use a unique, owned workspace fixture rather than a live home.
        temp = tempfile.TemporaryDirectory(prefix=".magpie-test-", dir=Path(__file__).resolve().parent.parent)
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root, self.native = self.base / "t3", self.base / "custom-account-home"
        self.path = self.native / "projects/synthetic" / (NATIVE + ".jsonl")
        self.keeper = self.path.with_name(KEEP + ".jsonl")
        write(self.path, {"sessionId": NATIVE, "message": PRIVATE})
        write(self.keeper, {"sessionId": KEEP, "message": "keep"})
        with closing(create_store(self.root)) as db:
            add_thread(db, FRONT, native=NATIVE, driver="claudeAgent")
            add_thread(db, KEEP_FRONT, native=KEEP, driver="claudeAgent")
        (self.root / "userdata/environment-id").write_text("env_test\n")
        (self.root / t3code_lifecycle.OWNER).touch()
        runtime = self.base / "synthetic-t3.exe"; runtime.write_bytes(b"test; never executed")
        binding = {"schema_version": SCHEMA, "profile_root": str(self.root), "environment_id": "env_test",
            "runtime_binaries": [str(runtime)], "desktop_profiles": [], "native_stores": [
                {"thread_id": FRONT, "engine": "claude", "root": str(self.native)},
                {"thread_id": KEEP_FRONT, "engine": "claude", "root": str(self.native)}]}
        self.adapter = T3CodeBoundAdapter(binding)
        self.profile, self.kept, _ = seed_magpie(self.base, self.native, self.path, self.keeper)
        self.manifest = {"schema_version": files.SCHEMA, "profiles": [self.profile]}
        self.bindings, self.plan_path = self.base / "bindings.json", self.base / "plan.json"
        write(self.bindings, self.manifest)
        for item in (patch("local_agent_manager.office_runtime.processes", return_value=[]),
                patch("pathlib.Path.home", return_value=self.base),
                patch.dict(os.environ, {"APPDATA": str(self.base / "appdata"), "LOCALAPPDATA": str(self.base / "local"),
                    "PASEO_HOME": str(self.base / "absent-paseo"), "T3CODE_HOME": str(self.root)})):
            item.start(); self.addCleanup(item.stop)

    def coordinator(self):
        return OperationCoordinator(CleanupService(client_inspector=lambda *_a, **_kw: ()))

    def plan(self):
        result = self.coordinator().plan_operation(client="t3code", record_ids=(FRONT,), adapters=(self.adapter,),
            plan_path=self.plan_path, operation_id="magpie-integration", magpie_bindings=self.bindings)
        self.assertEqual(result.get("goal_status"), "ready", result)
        self.assertNotIn(PRIVATE, json.dumps(result))
        return result

    def apply(self, plan, **kw):
        return self.coordinator().apply_operation(plan_path=self.plan_path, operation_id=plan["operation_id"],
            plan_sha256=plan["plan_sha256"], clients_closed=True, **kw)

    def test_cold_plan_apply_status_verify_and_dependency_order(self):
        plan = self.plan()
        families = [b["mutation_family"] for b in plan["child_batches"]]
        self.assertEqual(families, ["delete_claude_session", "delete_t3code_frontend", cleanup.KIND, cleanup.KIND])
        cleanup.validate_document(plan)
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertFalse(self.path.exists()); self.assertTrue(self.keeper.exists())
        self.assertEqual((Path(self.profile["config_root"]) / "usage.jsonl").read_bytes(), self.kept)
        for method in ("status_operation", "verify_operation"):
            result = getattr(self.coordinator(), method)(operation_id=plan["operation_id"], plan_path=self.plan_path)
            self.assertEqual(result["goal_status"], "complete", result)

    def test_changed_magpie_blocks_before_native_write(self):
        plan = self.plan()
        write(Path(self.profile["config_root"]) / "settings.json", {"changed": True})
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertTrue(self.path.exists())
        self.assertFalse(result.get("mutation_started"))

    def test_run_forwards_binding_and_closed_acknowledgement(self):
        result = self.coordinator().run_operation(client="t3code", record_ids=(FRONT,), adapters=(self.adapter,),
            plan_path=self.plan_path, magpie_bindings=self.bindings, clients_closed=True)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertTrue(result["related_stores"]["magpie"]["included"])

    def test_running_writer_and_changed_runtime_block_before_native(self):
        plan = self.plan()
        with patch("local_agent_manager.office_runtime.processes", return_value=[{"name": "magpie.exe"}]):
            result = self.apply(plan)
        self.assertEqual(result["goal_status"], "blocked", result)
        Path(self.profile["runtime_binaries"][0]).write_bytes(b"changed binary")
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertTrue(self.path.exists())

    def test_frontend_id_cannot_authorize_native_mapping(self):
        self.manifest["profiles"][0]["records"][0]["id"] = FRONT
        write(self.bindings, self.manifest)
        result = self.coordinator().plan_operation(client="t3code", record_ids=(FRONT,), adapters=(self.adapter,),
            plan_path=self.plan_path, magpie_bindings=self.bindings)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertTrue(self.path.exists())

    def test_prepare_freezes_explicit_custom_home_without_environment_guess(self):
        from local_agent_manager.magpie_discovery import prepare
        native_plan = self.coordinator().plan_operation(client="t3code", record_ids=(FRONT,), adapters=(self.adapter,),
            plan_path=self.base / "native-plan.json", operation_id="native-preview")
        self.assertEqual(native_plan["related_stores"]["magpie"]["scope"], "excluded")
        result = prepare(native_plan, config_root=self.profile["config_root"], cache_root=self.profile["cache_root"],
                         runtime_binaries=self.profile["runtime_binaries"])
        self.assertEqual(result["goal_status"], "ready")
        self.assertEqual(result["root_selection"], "explicit")
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(result["bindings"]["profiles"][0]["records"][0]["id"], NATIVE)
        self.assertTrue(self.path.exists())

    def test_repeated_binding_change_and_hash_tamper_cannot_widen_scope(self):
        plan = self.plan()
        self.manifest["profiles"][0]["records"][0]["id"] = KEEP
        write(self.bindings, self.manifest)
        result = self.apply(plan, magpie_bindings=self.bindings)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertTrue(self.path.exists())
        altered = copy.deepcopy(plan)
        altered["actions"][-1]["target"]["thread_id"] = KEEP
        from local_agent_manager.operation_store import plan_sha256
        altered["plan_sha256"] = plan_sha256(altered)
        with self.assertRaises(files.MagpieError): cleanup.validate_document(altered)

    def test_unknown_shared_file_write_is_not_replayed_and_is_root_wide(self):
        from local_agent_manager.mutation_guard import action_target_ids
        plan = self.plan()
        self.assertIsNone(action_target_ids(plan["actions"][-1]))
        original = frozen_files.apply_rewrite
        calls = []
        def interrupted(root, change, transform, **kw):
            calls.append(change)
            original(root, change, transform, **kw)
            raise RuntimeError("synthetic interruption after atomic replace")
        with patch.object(frozen_files, "apply_rewrite", interrupted):
            result = self.apply(plan)
        self.assertEqual(result["goal_status"], "unknown", result)
        with patch.object(frozen_files, "apply_rewrite", side_effect=AssertionError("must not replay")):
            again = self.apply(plan)
        self.assertEqual(again["goal_status"], "unknown", again)
        self.assertEqual(len(calls), 1)
        verified = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertFalse(verified["goal_satisfied"], verified)
        self.assertTrue(self.keeper.exists())

    def test_resurrected_gateway_record_is_residual_on_cold_verify(self):
        plan = self.plan()
        result = self.apply(plan)
        self.assertTrue(result["goal_satisfied"], result)
        write(Path(self.profile["config_root"]) / "usage.jsonl", {"agent": "claude", "session": NATIVE})
        result = self.coordinator().verify_operation(operation_id=plan["operation_id"], plan_path=self.plan_path)
        self.assertFalse(result["goal_satisfied"], result)

    def test_native_failure_preserves_magpie_copies(self):
        plan = self.plan()
        from local_agent_manager import claude_delete
        with patch.object(claude_delete, "execute_claude_delete", side_effect=RuntimeError("synthetic native failure")):
            result = self.apply(plan)
        self.assertFalse(result["goal_satisfied"], result)
        self.assertTrue(self.path.exists())
        self.assertIn(NATIVE, (Path(self.profile["config_root"]) / "usage.jsonl").read_text())

    def test_t3_pi_path_binding_and_cindy_magpie_parser(self):
        agent_dir = self.base / "pi-account"
        sessions = agent_dir / "sessions"
        selected = sessions / "project/session.jsonl"
        keeper = selected.with_name("keep.jsonl")
        write(selected, {"type": "session", "version": 3, "id": NATIVE, "timestamp": "2026-11-10T00:00:00Z", "cwd": str(self.base)})
        write(keeper, {"type": "session", "version": 3, "id": KEEP, "timestamp": "2026-11-10T00:00:00Z", "cwd": str(self.base)})
        with closing(sqlite3.connect(self.root / "userdata/statev2.sqlite")) as db:
            db.execute(f"UPDATE {V2}threads SET default_provider='pi' WHERE thread_id=?", (FRONT,))
            db.execute(f"UPDATE {V2}provider_threads SET driver='pi',provider='pi',payload_json=? WHERE thread_id=?",
                (json.dumps({"nativeThreadRef": {"driver": "pi", "nativeId": str(selected), "strength": "strong"}}), FRONT))
            db.commit()
        manifest = copy.deepcopy(self.adapter.manifest)
        manifest["native_stores"][0].update(engine="pi", root=str(sessions), agent_dir=str(agent_dir))
        self.adapter = T3CodeBoundAdapter(manifest)
        base = self.base / "pi-magpie"; base.mkdir()
        self.profile, _, _ = seed_magpie(base, sessions, selected, keeper, engine="cindy")
        self.profile["records"][0]["engine"] = "pi"
        write(self.bindings, {"schema_version": files.SCHEMA, "profiles": [self.profile]})
        plan = self.plan()
        result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertFalse(selected.exists()); self.assertTrue(keeper.exists())

    def test_paseo_custom_claude_binding_completes(self):
        from tests.test_paseo_fullflow import agent
        from local_agent_manager.paseo_bound_adapter import PaseoBoundAdapter, SCHEMA as PASEO_SCHEMA
        root = self.base / "paseo"
        write(root / "server-id", b"srv_test\n")
        binary = self.base / "synthetic-paseo.exe"; binary.write_bytes(b"test executable")
        for frontend, native in ((FRONT, NATIVE), (KEEP_FRONT, KEEP)):
            row = agent(frontend, native, self.base)
            row["provider"] = row["persistence"]["provider"] = row["runtimeInfo"]["provider"] = "claude"
            write(root / "agents" / (frontend + ".json"), row)
        adapter = PaseoBoundAdapter({"schema_version": PASEO_SCHEMA, "profile_root": str(root), "server_id": "srv_test",
            "runtime_binaries": [str(binary)], "desktop_profiles": [], "native_stores": [
                {"agent_id": FRONT, "engine": "claude", "root": str(self.native)},
                {"agent_id": KEEP_FRONT, "engine": "claude", "root": str(self.native)}]})
        with patch.dict(os.environ, {"T3CODE_HOME": str(self.base / "absent-t3"), "PASEO_HOME": str(root)}):
            plan = self.coordinator().plan_operation(client="paseo", record_ids=(FRONT,), adapters=(adapter,),
                plan_path=self.plan_path, magpie_bindings=self.bindings)
            self.assertEqual(plan["goal_status"], "ready", plan)
            result = self.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertFalse(self.path.exists()); self.assertTrue(self.keeper.exists())
        self.assertFalse((root / "agents" / (FRONT + ".json")).exists())
        self.assertTrue((root / "agents" / (KEEP_FRONT + ".json")).exists())


class MagpieCliTests(unittest.TestCase):
    def test_mixed_frontend_plan_cannot_silently_exclude_agy(self):
        document = {"scope": {"client": "t3code"}, "actions": [{"impact": {"external_action_payload": {
            "t3code_evidence": {"native_targets": [{"engine": "antigravity"}]}}}}]}
        with self.assertRaisesRegex(files.MagpieError, "native_engine_scope_unqualified"):
            cleanup._bind({"profiles": []}, document)

    def test_parser_requires_explicit_prepare_and_accepts_delete_bindings(self):
        from local_agent_manager.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["delete", "plan", "--client", "native", "--record-id", NATIVE,
                                  "--magpie-bindings", "bindings.json"])
        self.assertEqual(args.magpie_bindings, Path("bindings.json"))
        args = parser.parse_args(["environment", "magpie-cleanup", "prepare", "--native-plan", "plan.json",
                                  "--runtime-binary", "magpie.exe"])
        self.assertIsNone(args.home)

    def test_handoff_cannot_silently_drop_magpie_scope(self):
        from io import StringIO
        from local_agent_manager.cli import main
        output = StringIO()
        code = main(["delete", "run", "--client", "t3code", "--record-id", FRONT, "--handoff",
                     "--magpie-bindings", "bindings.json", "--json"], stdout=output, stderr=StringIO())
        self.assertNotEqual(code, 0)
        self.assertIn("magpie_native_disk_plan_required", output.getvalue())

    def test_legacy_delete_cannot_ignore_bindings(self):
        from io import StringIO
        from local_agent_manager.cli import main
        output = StringIO()
        code = main(["delete", "--magpie-bindings", "bindings.json", "--json"], stdout=output, stderr=StringIO())
        self.assertNotEqual(code, 0)
        self.assertIn("magpie_bindings_requires_plan_apply_or_run", output.getvalue())


@unittest.skipUnless(os.name == "nt", "Windows startup fences")
class MagpieCindyCodexTests(unittest.TestCase):
    def test_cascade_uses_actual_home_and_does_not_touch_official_store(self):
        from tests.test_cindy_codex_operations import CindyCodexOperationTests
        fixture = CindyCodexOperationTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        profile, kept, _ = seed_magpie(fixture.root, fixture.home, fixture.paths["delete"], fixture.paths["keep"],
                                      engine="codex", selected_id="delete", keep_id="keep")
        profile["records"].append({"engine": "codex", "root": str(fixture.home), "id": "child",
                                   "gateway_keys": [{"agent": "codex", "id": "child"}]})
        bindings = fixture.root / "magpie-bindings.json"
        write(bindings, {"schema_version": files.SCHEMA, "profiles": [profile]})
        with patch("pathlib.Path.home", return_value=fixture.root), patch("local_agent_manager.office_runtime.processes", return_value=[]), patch.dict(
                os.environ, {"APPDATA": str(fixture.root / "appdata"), "LOCALAPPDATA": str(fixture.root / "local"),
                             "T3CODE_HOME": str(fixture.root / "absent-t3"), "PASEO_HOME": str(fixture.root / "absent-paseo")}):
            plan = fixture.plan(record_ids=("delete",), magpie_bindings=bindings)
            self.assertEqual(plan["goal_status"], "ready", plan)
            result = fixture.apply(plan)
        self.assertEqual(result["goal_status"], "complete", result)
        self.assertEqual(fixture.calls, ["delete"])
        self.assertTrue(fixture.paths["keep"].exists())
        self.assertEqual((fixture.official / "state_5.sqlite").read_bytes(), fixture.official_before)
        self.assertEqual((Path(profile["config_root"]) / "usage.jsonl").read_bytes(), kept)


if __name__ == "__main__":
    unittest.main()
