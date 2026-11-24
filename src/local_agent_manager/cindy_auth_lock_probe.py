"""Exercise a Codex credential refresh against a disposable home and loopback responder."""
import base64
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from .cindy_isolation_windows import NativeAuthLocks, read_shared_fd, fail
from .codex_app_server import CodexAppServer, CodexAppServerResponseError
from .orca_runtime import isolated_environment


def _encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _writer_has_open_lock(path):
    try:
        fd = read_shared_fd(path)
    except OSError as error:
        if error.winerror == 32:  # The other process has opened auth.lock for writing.
            return True
        raise
    os.close(fd)
    return False


def _check_refresh(binary, *, replace_generation):
    account = "00000000-0000-4000-8000-000000000001"
    claims = {"email": "test@example.invalid", "exp": 1,
              "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": "free"}}
    token = ".".join((_encoded({"alg": "none"}), _encoded(claims), "test"))
    refreshed = threading.Event()

    class Responder(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/refresh" or not 0 < size < 8192:
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(size))
            if request.get("refresh_token") != "test-before":
                self.send_error(400)
                return
            fresh = ".".join((_encoded({"alg": "none"}), _encoded(dict(claims, exp=int(time.time()) + 3600)), "test"))
            body = json.dumps({"refresh_token": "test-after", "access_token": fresh}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            refreshed.set()

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="lam-auth-lock-") as tmp:
        root = Path(tmp)
        home = root / "store"
        home.mkdir()
        env, cwd = isolated_environment(root, home)
        auth = home / "auth.json"
        initial = {
            "auth_mode": "chatgpt",
            "tokens": {"id_token": token, "access_token": token, "refresh_token": "test-before", "account_id": account},
            "last_refresh": "2020-01-01T00:00:00Z",
        }
        auth.write_text(json.dumps(initial))
        before = auth.read_bytes()
        with ThreadingHTTPServer(("127.0.0.1", 0), Responder) as http:
            port = http.server_port
            env["CODEX_REFRESH_TOKEN_URL_OVERRIDE"] = f"http://127.0.0.1:{port}/refresh"
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                command = [str(binary), "app-server", "-c", 'cli_auth_credentials_store="file"',
                           "-c", f'chatgpt_base_url="http://127.0.0.1:{port}"', "-c", "analytics.enabled=false",
                           "-c", "check_for_update_on_startup=false", "-c", "features.plugins=false",
                           "-c", "features.apps=false"]

                def refresh():
                    with CodexAppServer(codex_home=home, command=command, environment=env,
                                        working_directory=cwd, timeout=12) as server:
                        try:
                            server.request("account/read", {"refreshToken": True})
                        except CodexAppServerResponseError:
                            # Workspace discovery can fail after committing the local refresh.
                            pass

                with ThreadPoolExecutor(1) as pool:
                    with NativeAuthLocks([home]):
                        lock = home / "auth.lock"
                        if _writer_has_open_lock(lock):
                            fail("auth_probe_unexpected_writer")
                        request = pool.submit(refresh)
                        if not refreshed.wait(5):
                            fail("auth_probe_no_local_refresh")
                        deadline = time.monotonic() + 4
                        while True:
                            time.sleep(0.02)
                            if _writer_has_open_lock(lock):
                                break
                            if request.done() or time.monotonic() >= deadline:
                                fail("auth_probe_writer_did_not_wait")
                        # Exclude sampling a brief open as evidence of a blocked write.
                        time.sleep(0.15)
                        if not _writer_has_open_lock(lock):
                            fail("auth_probe_writer_did_not_wait")
                        if request.done() or auth.read_bytes() != before:
                            fail("auth_probe_commit_while_locked")
                        if replace_generation:
                            newer = json.loads(before)
                            newer["tokens"]["refresh_token"] = "test-replacement"
                            replacement = home / "replacement.json"
                            replacement.write_text(json.dumps(newer))
                            replacement.replace(auth)
                    request.result()
                    expected = "test-replacement" if replace_generation else "test-after"
                    if json.loads(auth.read_bytes())["tokens"]["refresh_token"] != expected:
                        fail("auth_probe_commit_unverified")
            finally:
                http.shutdown()
                worker.join()


def verify_refresh_lock(binary):
    """Check both an uncontested commit and one superseded while waiting."""
    _check_refresh(binary, replace_generation=False)
    _check_refresh(binary, replace_generation=True)
