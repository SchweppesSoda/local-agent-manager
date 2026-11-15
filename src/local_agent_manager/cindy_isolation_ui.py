"""Ordinary Windows product acceptance, with observation/action/refresh frames.

The trusted operator reviews real screenshots and attests rendered history and
side/automation views. Input is restricted to Cindy and a fixed nonce probe;
no arbitrary text, terminals, login, account creation, IPC injection or tokens.
Native write evidence is required in addition to visual attestations.
"""
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time

from . import frozen_files as ff
from .cindy_isolation_plan import readonly_sqlite, physical_descendant
from .cindy_isolation_recovery import digest
from .cindy_isolation_controller import fail, signed, supported_account, ROLES
from .operation_store import atomic_write_json


def clean_product_environment():
    return {k: v for k, v in os.environ.items() if k.upper() not in {
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "ELECTRON_RUN_AS_NODE", "CODEX_ACCESS_TOKEN", "CODEX_API_KEY",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_FEDERATION_RULE_ID", "OPENAI_IDENTITY_TOKEN_FILE",
        "OPENAI_WORKLOAD_IDENTITY_CONTEXT"}}


def launch_cindy(spec, *arguments):
    if ctypes_is_admin(): fail("product_start_requires_ordinary_user")
    return subprocess.Popen([spec["controller"]["executable"], "--force-renderer-accessibility", *arguments],
                            env=clean_product_environment(), cwd=spec["profile"]["path"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid


def ctypes_is_admin():
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin()) if os.name == "nt" else False


def window_behavior(spec):
    path = Path(spec["profile"]["path"]) / "window-behavior-settings.json"
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    defaults = {"swallowActivationClick": False, "windowsCloseBehavior": None, "linuxCloseBehavior": None,
                "awayGuard": False, "awayGuardDelaySeconds": None}
    return {key: value.get(key, default) for key, default in defaults.items()}


class WindowsProductUI:
    def __init__(self, spec, run):
        self.spec, self.run = spec, Path(run)

    def perform(self, action, frame_name, *, expected=None, x=None, y=None, delta=None, text=None):
        if ctypes_is_admin(): fail("product_ui_requires_ordinary_user")
        request = {"action": action, "executable": self.spec["controller"]["executable"],
                   "screenshot": str(self.run / frame_name), "expected": expected,
                   "x": x, "y": y, "delta": delta, "text": text}
        path = self.run / "ordinary-win-ui-request.json"
        atomic_write_json(path, request)
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-File",
                                 str(Path(__file__).with_suffix(".ps1")), "-Request", str(path)],
                                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: fail("product_ui_action_or_refresh_unverified")
        return json.loads(result.stdout.decode("utf-8-sig"))


def _texts(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"text", "input_text", "output_text"} and isinstance(child, str): yield child
            elif isinstance(child, (dict, list)): yield from _texts(child)
    elif isinstance(value, list):
        for child in value: yield from _texts(child)


def probe_in_rollout(path, *, offset, prompt, answer):
    """Only new probe user/assistant messages count; tool text cannot fake it."""
    user_seen, answer_seen = False, False
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        for raw in stream:
            try: event = json.loads(raw)
            except (ValueError, UnicodeError): continue  # last live line may be partial
            payload = event.get("payload", {})
            if event.get("type") != "response_item" or payload.get("type") != "message": continue
            texts = "\n".join(_texts(payload.get("content", [])))
            if payload.get("role") == "user" and prompt in texts: user_seen = True
            if user_seen and payload.get("role") == "assistant" and answer in texts: answer_seen = True
    return user_seen and answer_seen


def probe_home(spec, native_id, role):
    if role != 'new_default_task': return Path(spec['target']['path']), 'default'
    registry=json.loads(Path(spec['registry_path']).read_text(encoding='utf-8-sig'))
    binding=registry.get('bindings',{}).get('openai:native:'+native_id) or registry.get('bindings',{}).get('native:'+native_id)
    if not binding or binding.get('providerId')!='openai': fail('new_task_requires_actual_product_native_account_binding')
    account=binding.get('accountId')
    identity=spec['controller'].get('allowed_test_homes',{}).get(account)
    if identity is None: fail('new_task_selected_unqualified_or_unauthenticated_account')
    from .cindy_isolation_plan import directory_identity
    home=Path(identity['path'])
    if directory_identity(home)!=identity or os.path.samefile(home,Path(spec['source']['path'])):
        fail('new_task_account_home_not_independent')
    return home,account


class ProductAcceptance:
    def __init__(self, spec, run, *, ui=None, launch=launch_cindy, clock=time.monotonic):
        self.spec, self.run, self.launch, self.clock = spec, Path(run), launch, clock
        self.ui = ui or WindowsProductUI(spec, run)
        self.frames, self.roles, self.actions, self.probes = {}, {}, [], {}
        self.current_frame, self.current_view, self.current_task = None, None, None
        self.focus_frame, self.typed_frame, self.selected_confirmed = None, None, False
        self.scroll_counts = {}
        self.new_baseline = set()

    def _fresh(self, frame):
        if frame != self.current_frame or not frame or self.clock() - self.frames[frame]["observed_monotonic"] > 45:
            fail("fresh_product_frame_required")
        return self.frames[frame]["window"]

    def capture(self, action="observe", **options):
        expected = options.pop("expected", None)
        name = "frame-%06d.png" % (len(self.frames) + 1)
        result = self.ui.perform(action, name, expected=expected, **options)
        self.frames[name] = {"sha256": digest(self.run / name), "window": result,
                             "observed_monotonic": self.clock(), "action": action}
        self.current_frame = name
        self.actions.append({"action": action, "frame": name, "view": self.current_view})
        atomic_write_json(self.run / "ordinary-ui-state.json", self.state())
        return self.state()

    def state(self):
        prompt = self.probes.get(self.current_view, {}).get("prompt")
        candidates = []
        if self.current_view == 'new_default_task' and self.current_task is None:
            with readonly_sqlite(Path(self.spec['controller']['cindy_database'])) as c:
                candidates = [dict(r) for r in c.execute('select id,title,agent_kind,sdk_session_id from sessions')
                    if r['id'] not in self.new_baseline and r['agent_kind'] == 'codex'][:20]
        return {"current_frame": self.current_frame, "frame_path": str(self.run / self.current_frame) if self.current_frame else None,
                "view": self.current_view, "task": self.current_task, "fixed_probe": prompt,
                'new_task_candidates': candidates,
                "roles_done": sorted(self.roles), "roles_pending": sorted(ROLES - set(self.roles)),
                "controls": self.frames.get(self.current_frame, {}).get("window", {}).get("controls", []),
                "input_requires_current_frame": True}

    def _session(self, task_id):
        with readonly_sqlite(Path(self.spec["controller"]["cindy_database"])) as c:
            row = c.execute("select id,sdk_session_id,agent_kind,title from sessions where id=?", (task_id,)).fetchone()
        if row is None or row["agent_kind"] != "codex": fail("product_task_not_codex")
        return dict(row)

    def command(self, request):
        if set(request) - {"action", "frame", "role", "x", "y", "delta", "observed_title", "observed_native_id", "task_id"}:
            fail("unapproved_product_request_fields")
        action = request["action"]
        if action == "observe": return self.capture()
        if action == "open":
            role = request["role"]
            if role not in {"root", "long_history", "side_history", "automations", "new_default_task"}: fail("unknown_acceptance_view")
            self.current_view, self.selected_confirmed, self.focus_frame, self.typed_frame = role, False, None, None
            if role in {"root", "long_history", "side_history"}:
                representative = self.spec["controller"]["representatives"]["long_history" if role == "side_history" else role]
                self.current_task = self._session(representative["task_id"])
                if self.current_task["sdk_session_id"] != representative["native_id"]: fail("original_task_native_binding_changed")
                self.launch(self.spec, "cindy://session/" + self.current_task["id"])
            elif role == "new_default_task":
                with readonly_sqlite(Path(self.spec["controller"]["cindy_database"])) as c:
                    self.new_baseline = {r[0] for r in c.execute("select id from sessions")}
                self.current_task = None
                self.launch(self.spec, "--open-folder", self.spec["controller"]["test_workdir"])
            else:
                self.current_task = None  # operator opens visible Automations with a fresh click
            if role in {"root", "long_history", "new_default_task"}:
                nonce = secrets.token_hex(10)
                self.probes[role] = {"nonce": nonce, "prompt": "隔离验收测试 " + nonce + "：请只回复 ISOLATION_OK " + nonce + "，不调用工具、不修改文件。", "answer": "ISOLATION_OK " + nonce}
            return self.capture()
        window = self._fresh(request.get("frame"))
        if action == "confirm_task":
            if self.current_view == "new_default_task":
                candidate = self._session(request["task_id"])
                if candidate["id"] in self.new_baseline: fail("new_task_is_not_new")
                self.current_task = candidate
            if not self.current_task or request.get("observed_title") != self.current_task["title"]:
                fail("observed_original_task_title_mismatch")
            self.selected_confirmed = True
            self.actions.append({"action": "task_visually_confirmed", "task_id": self.current_task["id"], "frame": self.current_frame,
                                 "view": self.current_view})
            return self.state()
        if action in {"click", "focus_composer", "scroll", "submit_probe"}:
            if action in {"focus_composer", "submit_probe"} and not self.selected_confirmed: fail("original_task_not_visually_confirmed")
            if action == "submit_probe" and self.typed_frame != self.current_frame: fail("probe_text_must_be_observed_before_submit")
            result = self.capture(action, expected=window, x=request["x"], y=request["y"], delta=request.get("delta"))
            if action == "focus_composer": self.focus_frame = self.current_frame
            if action == "scroll": self.scroll_counts[self.current_view] = self.scroll_counts.get(self.current_view, 0) + 1
            if action == "submit_probe": self.probes[self.current_view]["submitted"] = True
            return result
        if action == "type_probe":
            if self.focus_frame != self.current_frame or not self.selected_confirmed: fail("composer_focus_must_be_observed_first")
            result = self.capture("type_probe", expected=window, text=self.probes[self.current_view]["prompt"])
            self.typed_frame = self.current_frame
            return result
        if action == "attest":
            role = self.current_view
            proof = {"frame": self.current_frame, "operator_reviewed_product_view": True}
            if role in {"root", "long_history", "new_default_task"}:
                proof.update(self.verify_probe())
                if role == "long_history" and self.scroll_counts.get(role, 0) < 2: fail("long_history_requires_observed_paging_scrolls")
            elif role == "side_history":
                native = request.get("observed_native_id")
                if native not in self.spec["controller"]["side_native_ids"]: fail("side_thread_outside_frozen_closure")
                if not any(a["view"] == role and a["action"] == "click" for a in self.actions): fail("side_view_requires_product_navigation")
                proof["native_id"] = native
            elif role == "automations":
                if not any(a["view"] == role and a["action"] == "click" for a in self.actions): fail("automation_view_requires_product_navigation")
                proof["schedule_metadata_preserved"] = True
            else: fail("view_cannot_be_attested")
            self.roles[role] = proof
            atomic_write_json(self.run / "ordinary-ui-state.json", self.state())
            return self.state()
        fail("unsupported_product_action")

    def verify_probe(self):
        role = self.current_view; probe = self.probes[role]
        if not probe.get("submitted") or not self.current_task: fail("no_product_probe_submitted")
        session = self._session(self.current_task["id"])
        native = session["sdk_session_id"]
        home,account=probe_home(self.spec,native,role)
        with readonly_sqlite(home / "state_5.sqlite") as c:
            row = c.execute("select rollout_path from threads where id=?", (native,)).fetchone()
        if row is None: fail("probe_did_not_write_legacy_target")
        path = physical_descendant(Path(row[0]),home)
        original = next((f for f in self.spec["native_files"] if f["native_id"] == native), None)
        offset = original["size"] if original else 0
        if not probe_in_rollout(path, offset=offset, prompt=probe["prompt"], answer=probe["answer"]): fail("native_probe_response_not_yet_verified")
        if role == "new_default_task":
            with readonly_sqlite(Path(self.spec["source"]["path"]) / "state_5.sqlite") as c:
                if c.execute("select count(*) from threads where id=?", (native,)).fetchone()[0]: fail("new_default_task_wrote_official_source")
        elif native != self.spec["controller"]["representatives"][role]["native_id"]:
            fail("probe_written_to_wrong_original_native_id")
        return {"task_id": session["id"], "native_id": native, "native_probe_verified": True,
                "nonce": probe["nonce"], "target_rollout": str(path), 'account_id':account,'actual_home':str(home)}

    def record_cold_start(self, account, before, after):
        if account["identity_sha256"] != self.spec["controller"]["account_identity_sha256"]: fail("cold_existing_account_mismatch")
        if not before.get("independent_auth") or not after.get("independent_auth"): fail("cold_start_auth_relinked")
        self.roles["cold_start"] = {"frame": self.current_frame, "independent_auth": True,
            "before": before, "after": after, "supported_account_identity_sha256": account["identity_sha256"],
            "native_probe_verified": self.roles["root"]["native_probe_verified"],
            "native_probe_nonce": self.roles["root"]["nonce"]}

    def acceptance_packet(self, key, nonce, account):
        if set(self.roles) != ROLES: fail("product_acceptance_incomplete")
        return signed({"spec_sha256": self.spec["plan_sha256"], "nonce": nonce, "roles": self.roles,
                       "frames": {name: {"sha256": frame["sha256"], "window_identity": {
                           key: frame["window"][key] for key in ("pid", "created", "hwnd")}} for name, frame in self.frames.items()},
                       "actions": self.actions, "account": account, "lifecycle_closed_for_cleanup": True}, key)
