import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from local_agent_record_janitor import paseo_daemon_delete as paseo
from local_agent_record_janitor.cli import main
from tests.orca_native_support import add_native_record, create_native_schema
from tests.test_paseo_fullflow import AGENT, KEEP, NATIVE, OTHER, agent, write


class PaseoDaemonDeleteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve(strict=True)
        self.profile = base / "paseo"
        self.plan_path = base / "plan.json"
        self.target = self.profile / "agents" / f"{AGENT}.json"
        self.keeper = self.profile / "agents" / f"{KEEP}.json"
        write(self.target, agent(AGENT, NATIVE, base))
        antigravity = agent(KEEP, OTHER, base)
        antigravity.update(provider="antigravity",
                           persistence={"provider": "antigravity", "sessionId": '{"opaque":true}'},
                           runtimeInfo={"provider": "antigravity", "sessionId": None})
        write(self.keeper, antigravity)
        (self.profile / "server-id").write_text("srv_fixture", encoding="utf-8")
        self.native = base / "codex" / "rollout.jsonl"
        self.native.parent.mkdir()
        self.native.write_bytes(b"retained codex history")
        install = base / "Paseo"
        for name in paseo._RUNTIME_FILES:
            path = install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture runtime")
        runtime = patch.object(paseo, "_bundled_install", return_value=install)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.calls = []
        self.live = {AGENT, KEEP}
        self.remove_registry = True
        self.timeout_after_delete = False
        transport = patch.object(paseo, "_rpc", side_effect=self.rpc)
        transport.start()
        self.addCleanup(transport.stop)
        state = patch.dict(os.environ, {"LOCALAPPDATA": str(base / "state"), "XDG_STATE_HOME": str(base / "state")})
        state.start()
        self.addCleanup(state.stop)

    def rpc(self, evidence, verb, timeout):
        self.assertEqual(evidence["root"], str(self.profile))
        identifier = evidence["agent_id"]
        self.calls.append((identifier, verb))
        if verb == "delete":
            self.live.remove(identifier)
            if self.remove_registry:
                (self.profile / evidence["record_path"]).unlink()
            if self.timeout_after_delete:
                raise paseo.PaseoDaemonError("fixture request timed out")
        return identifier in self.live

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        status = main([*args, "--json"], stdin=io.StringIO(), stdout=output, stderr=errors)
        return status, json.loads(output.getvalue())

    def plan(self, identifier=AGENT, *extra):
        return self.invoke("delete", "plan", "--client", "paseo", "--paseo-only",
                           "--paseo-root", str(self.profile), "--record-id", identifier,
                           "--out", str(self.plan_path), *extra)

    def apply(self, plan, *extra, approved=True):
        approval = ("--plan-sha256", plan["plan_sha256"]) if approved else ()
        return self.invoke("delete", "apply", "--paseo-only", "--operation-id", plan["operation_id"],
                           "--plan", str(self.plan_path), *approval, *extra)

    def query(self, verb, plan):
        return self.invoke("operation", verb, "--operation-id", plan["operation_id"], "--plan", str(self.plan_path))

    def test_cold_apply_and_verify_preserve_native_and_unselected_antigravity(self):
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        self.assertEqual([action["kind"] for action in plan["actions"]], [paseo.KIND])
        self.assertNotIn(NATIVE, json.dumps(plan))
        self.assertNotIn("PRIVATE", json.dumps(plan))
        keeper = self.keeper.read_bytes()
        code, result = self.apply(plan)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["modified"])
        self.assertTrue(result["goal_satisfied"])
        self.assertFalse(self.target.exists())
        self.assertEqual(self.keeper.read_bytes(), keeper)
        self.assertEqual(self.native.read_bytes(), b"retained codex history")
        self.assertEqual(self.query("status", plan)[1]["goal_status"], "complete")
        self.assertEqual(self.query("verify", plan)[1]["goal_status"], "complete")
        self.assertEqual(self.calls.count((AGENT, "delete")), 1)

    def test_paseo_only_accepts_opaque_antigravity_without_native_binding(self):
        code, plan = self.plan(KEEP)
        self.assertEqual(code, 0, plan)
        code, result = self.apply(plan)
        self.assertEqual(code, 0, result)
        self.assertFalse(self.keeper.exists())
        self.assertTrue(self.target.exists())
        self.assertEqual(self.calls.count((KEEP, "delete")), 1)

    def test_batch_runs_all_four_providers_and_keeps_native_history(self):
        selected = [AGENT, KEEP, "55555555-5555-4555-8555-555555555555", "66666666-6666-4666-8666-666666666666"]
        for identifier, provider in zip(selected[2:], ("claude", "pi")):
            value = agent(identifier, NATIVE, self.profile.parent)
            value.update(provider=provider, persistence={"provider": provider, "sessionId": NATIVE},
                         runtimeInfo={"provider": provider, "sessionId": None})
            write(self.profile / "agents" / (identifier + ".json"), value)
            self.live.add(identifier)
        selectors = [part for identifier in selected for part in ("--record-id", identifier)]
        code, result = self.invoke("delete", "run", "--client", "paseo", "--paseo-only",
            "--paseo-root", str(self.profile), *selectors, "--yes", "--out", str(self.profile.parent / "batch.json"))
        self.assertEqual(code, 0, result)
        self.assertTrue(result["goal_satisfied"])
        self.assertEqual(len(result["plans"]), 4)
        self.assertEqual(self.native.read_bytes(), b"retained codex history")
        for identifier in selected:
            self.assertEqual(self.calls.count((identifier, "delete")), 1)

    def test_batch_failure_preflights_all_targets_and_never_repeats_unknown(self):
        arguments = ("delete", "run", "--client", "paseo", "--paseo-only", "--paseo-root", str(self.profile),
                     "--record-id", AGENT, "--record-id", KEEP, "--yes")
        self.assertNotEqual(self.invoke(*arguments[:-1])[0], 0)
        self.assertEqual(self.calls, [])
        _, result = self.invoke(*arguments, "--record-id", "not-a-uuid")
        self.assertFalse(result["mutation_started"])
        self.assertFalse(any(verb == "delete" for _, verb in self.calls))
        self.remove_registry = False
        _, result = self.invoke(*arguments)
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertEqual(self.calls.count((AGENT, "delete")), 1)
        self.assertNotIn((KEEP, "delete"), self.calls)

    def test_batch_lost_apply_response_recovers_without_a_second_delete(self):
        from local_agent_manager.operation_coordinator import OperationCoordinator
        original = OperationCoordinator.apply_operation
        def response_lost(coordinator, **kwargs):
            original(coordinator, **kwargs)
            raise RuntimeError("fixture response lost after completed mutation")
        with patch.object(OperationCoordinator, "apply_operation", response_lost):
            code, result = self.invoke("delete", "run", "--client", "paseo", "--paseo-only",
                "--paseo-root", str(self.profile), "--record-id", AGENT, "--record-id", KEEP, "--yes")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["goal_satisfied"])
        self.assertEqual(self.calls.count((AGENT, "delete")), 1)
        self.assertEqual(self.calls.count((KEEP, "delete")), 1)

    def test_exact_id_approval_profile_and_snapshot_are_required(self):
        self.assertNotEqual(self.plan(AGENT[:8])[0], 0)
        self.assertEqual(self.calls, [])
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        self.assertFalse(self.apply(plan, approved=False)[1]["mutation_started"])
        self.assertFalse(self.apply(plan, "--paseo-root", str(self.profile.parent))[1]["mutation_started"])
        self.target.write_bytes(self.target.read_bytes() + b" ")
        code, result = self.apply(plan)
        self.assertNotEqual(code, 0)
        self.assertEqual(result["goal_status"], "blocked", result)
        self.assertFalse(result["mutation_started"])
        self.assertNotIn((AGENT, "delete"), self.calls)

    def test_ack_with_registry_residual_stays_unknown_and_cannot_retry(self):
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        self.remove_registry = False
        _, result = self.apply(plan)
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertTrue(result["mutation_started"])
        self.assertEqual(self.query("verify", plan)[1]["goal_status"], "unknown")
        self.assertEqual(self.apply(plan)[1]["goal_status"], "unknown")
        self.assertEqual(self.calls.count((AGENT, "delete")), 1)
        self.plan_path = self.plan_path.with_name("other-plan.json")
        code, sibling_plan = self.plan(KEEP)
        self.assertEqual(code, 0, sibling_plan)
        self.assertFalse(self.apply(sibling_plan)[1]["mutation_started"])
        self.assertNotIn((KEEP, "delete"), self.calls)

    def test_only_local_daemon_endpoints_are_admitted(self):
        pid_path = self.profile / "paseo.pid"
        identity = {"pid": 123, "startedAt": "fixture", "serverId": "srv_fixture"}
        for endpoint in ("127.0.0.1:6767", r"\\.\pipe\paseo-fixture"):
            with self.subTest(endpoint=endpoint):
                write(pid_path, {**identity, "listen": endpoint})
                self.assertEqual(paseo._daemon_locator(self.profile, "srv_fixture"), endpoint)
        for endpoint in ("192.0.2.1:6767", "wss://example.invalid", r"\\remote\pipe\paseo", "127.0.0.1:65536"):
            with self.subTest(endpoint=endpoint):
                write(pid_path, {**identity, "listen": endpoint})
                with self.assertRaises(paseo.PaseoDaemonError):
                    paseo._daemon_locator(self.profile, "srv_fixture")

    def test_timeout_recovers_by_observation_without_replaying_request(self):
        code, plan = self.plan()
        self.assertEqual(code, 0, plan)
        self.timeout_after_delete = True
        _, result = self.apply(plan)
        self.assertEqual(result["goal_status"], "unknown", result)
        self.assertEqual(self.query("status", plan)[1]["goal_status"], "unknown")
        self.assertEqual(self.apply(plan)[1]["goal_status"], "unknown")
        _, verified = self.query("verify", plan)
        self.assertEqual(verified["goal_status"], "complete", verified)
        self.assertTrue(verified["modified"])
        self.assertEqual(self.calls.count((AGENT, "delete")), 1)


@unittest.skipUnless(os.name == "nt" and os.environ.get("LARJ_PASEO_BUNDLE_ACCEPTANCE") == "1",
                     "Opt in to a local temporary daemon with installed Paseo 0.15.3")
class PaseoBundleAcceptanceTests(unittest.TestCase):
    def test_real_daemon_deletes_four_providers_and_preserves_native(self):
        install = paseo._bundled_install()
        cli = [str(install / "Paseo.exe"), "--disable-warning=DEP0040", str(install / paseo._RUNNER),
               "node-script", str(install / paseo._ARCHIVE / "node_modules/@getpaseo/cli/dist/index.js")]
        with tempfile.TemporaryDirectory(prefix="larj-paseo-acceptance-") as temporary:
            base = Path(temporary).resolve(strict=True)
            profile, user = base / "profile", base / "isolated-user"
            native = user / ".codex"
            native.mkdir(parents=True)
            create_native_schema(native)
            rollout = add_native_record(native, NATIVE)
            native_before = {path: path.read_bytes() for path in native.glob("*.sqlite")}
            native_before[rollout] = rollout.read_bytes()
            target = profile / "agents" / f"{AGENT}.json"
            sibling = profile / "agents" / f"{KEEP}.json"
            write(target, agent(AGENT, NATIVE, base))
            write(sibling, {**agent(KEEP, OTHER, base), "title": AGENT})
            selected = [AGENT]
            for identifier, provider in (("55555555-5555-4555-8555-555555555555", "claude"),
                                         ("66666666-6666-4666-8666-666666666666", "pi"),
                                         ("77777777-7777-4777-8777-777777777777", "antigravity")):
                value = agent(identifier, NATIVE, base)
                value.update(provider=provider, persistence={"provider": provider, "sessionId": NATIVE},
                             runtimeInfo={"provider": provider, "sessionId": None})
                write(profile / "agents" / (identifier + ".json"), value)
                selected.append(identifier)
                sentinel = user / provider / "native-sentinel"
                sentinel.parent.mkdir(parents=True)
                sentinel.write_bytes(b"provider native history")
                native_before[sentinel] = sentinel.read_bytes()

            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                endpoint = f"127.0.0.1:{listener.getsockname()[1]}"
            env = {k: v for k, v in os.environ.items()
                   if not k.upper().startswith(("PASEO_", "ELECTRON_", "NODE_", "CODEX_", "CLAUDE_",
                                                "ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_", "AGY_", "PI_"))}
            env.update(ELECTRON_RUN_AS_NODE="1", PASEO_NODE_ENV="production", PASEO_HOME=str(profile),
                       PASEO_LISTEN=endpoint, PASEO_RELAY_ENABLED="false",
                       PASEO_VOICE_MODE_ENABLED="false", PASEO_DICTATION_ENABLED="false",
                       USERPROFILE=str(user), HOME=str(user), APPDATA=str(user),
                       LOCALAPPDATA=str(user), CODEX_HOME=str(native))
            with (base / "fixture-daemon.log").open("wb") as log:
                proc = subprocess.Popen(
                    [*cli, "daemon", "run", "--home", str(profile)], env=env, stdout=log, stderr=log,
                    stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    self.wait_until_ready(proc, profile, endpoint)
                    # Startup can migrate a legacy flat row into its project directory.
                    targets = [Path(paseo._snapshot(profile, identifier)["source"]) for identifier in selected]
                    sibling = Path(paseo._snapshot(profile, KEEP)["source"])
                    sibling_before = sibling.read_bytes()
                    self.exercise_operation(profile, base / "batch.json", selected)
                    self.assertFalse(any(path.exists() for path in targets))
                    self.assertEqual(sibling.read_bytes(), sibling_before)
                    self.assertEqual({path: path.read_bytes() for path in native_before}, native_before)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=15)

    def wait_until_ready(self, proc, profile, endpoint):
        for _ in range(100):
            self.assertIsNone(proc.poll(), "Paseo fixture daemon exited during startup")
            try:
                locator = json.loads((profile / "paseo.pid").read_bytes())
                if locator.get("listen") == endpoint and locator.get("serverId"):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        self.fail("Temporary Paseo daemon did not publish its local endpoint")

    def exercise_operation(self, profile, plan_path, record_ids):
        def invoke(*args):
            output, errors = io.StringIO(), io.StringIO()
            code = main([*args, "--json"], stdin=io.StringIO(), stdout=output, stderr=errors)
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0, result)
            return result

        selectors = [part for identifier in record_ids for part in ("--record-id", identifier)]
        applied = invoke("delete", "run", "--paseo-only", "--client", "paseo",
                         "--paseo-root", str(profile), *selectors, "--yes", "--out", str(plan_path))
        self.assertTrue(applied["goal_satisfied"])
        for plan in applied["plans"]:
            self.assertTrue(invoke("operation", "verify", "--operation-id", plan["operation_id"],
                "--plan", plan["plan_path"])["goal_satisfied"])


if __name__ == "__main__":
    unittest.main()
