from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from local_agent_manager.cindy_isolation_lifecycle import CindyLifecycle, windows_identity
from local_agent_manager.cindy_isolation_windows import ReadLeases, auth_permissions, NativeAuthLocks, qualify_auth_writers
from local_agent_manager.lifecycle_confirmation import LifecycleGate


@unittest.skipUnless(os.name == 'nt', 'actual isolated Windows boundaries')
class CindyHostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'

    def test_ui_script_parses_and_native_input_types_compile_without_operating_product(self):
        script = Path(__file__).resolve().parent.parent/'src/local_agent_manager/cindy_isolation_ui.ps1'
        env = dict(os.environ, LAM_UI_SOURCE=str(script))
        command = """$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
[void][System.Management.Automation.Language.Parser]::ParseFile($env:LAM_UI_SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'UI parse failed'}
$source=[IO.File]::ReadAllText($env:LAM_UI_SOURCE)
$match=[regex]::Match($source,"Add-Type @'\r?\n([\\s\\S]*?)\r?\n'@")
if(!$match.Success){throw 'Native input declarations missing'}
Add-Type $match.Groups[1].Value
if([Runtime.InteropServices.Marshal]::SizeOf([type][CindyImportUI+INPUT]) -ne 40){throw 'Unexpected Windows INPUT ABI'}
"""
        result = subprocess.run([str(self.shell), '-NoProfile', '-NonInteractive', '-Command', command],
            env=env, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, ascii(result.stderr))

    def test_actual_hidden_window_wm_close_fresh_gate_and_kernel_exit(self):
        script = self.root/'window.ps1'; ready = self.root/'ready'
        script.write_text("""Add-Type -AssemblyName System.Windows.Forms
$f=New-Object System.Windows.Forms.Form
$f.Text='LAM TEMP lifecycle smoke';$f.ShowInTaskbar=$false;$f.Opacity=0
$f.add_Shown({[IO.File]::WriteAllText($env:LAM_TEMP_READY,'ready')})
[void]$f.ShowDialog()
""", encoding='ascii')
        process = subprocess.Popen([str(self.shell), '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-File', str(script)],
            env=dict(os.environ, LAM_TEMP_READY=str(ready)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        def cleanup():
            if process.poll() is None: process.terminate()  # Only our isolated helper if the smoke failed.
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic()+15
        while not ready.exists():
            self.assertIsNone(process.poll()); self.assertLess(time.monotonic(), deadline); time.sleep(.05)
        image, _ = windows_identity(process.pid)
        prompts = []
        cycle = CindyLifecycle(run_id='isolated-wm-close', executable=image, profile=self.root, affected_tasks=['TEMP-only'],
            census=lambda: [] if process.poll() is not None else [{'name':'Cindy.exe','pid':process.pid}],
            gate_factory=lambda scope: LifecycleGate(scope, prompt=lambda message: prompts.append(message) or True))
        # Exercise real WM_CLOSE on our helper at the runner's token level;
        # the separate lifecycle tests cover rejection of elevated callers.
        with patch("ctypes.windll.shell32.IsUserAnAdmin", return_value=0):
            result = cycle.close()
        process.wait(timeout=5)
        self.assertEqual(result['closed_pids'], [process.pid]); self.assertTrue(result['exit_rechecked'])
        self.assertEqual(len(prompts), 1); self.assertEqual(result['targets'][0]['executable'], image)

    def test_bootstrap_read_pins_block_code_mutation_but_allow_native_launch(self):
        path = Path(__file__).resolve().parent.parent/'tools/cindy_legacy_entry_bootstrap.py'
        module_spec = importlib.util.spec_from_file_location('task_bootstrap_fixture', path)
        module = importlib.util.module_from_spec(module_spec); module_spec.loader.exec_module(module)
        self.addCleanup(module._close)
        code = self.root/'reviewed.py'; code.write_bytes(b'fixture')
        module._pin(code)
        with self.assertRaises(OSError): code.write_bytes(b'unreviewed')
        with self.assertRaises(OSError): code.unlink()
        import shutil
        native = self.root/'native-cmd.exe'; shutil.copyfile(Path(os.environ['SystemRoot'])/'System32/cmd.exe', native)
        module._pin(native)
        result = subprocess.run([str(native), '/d', '/c', 'exit', '0'], timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)

    def test_private_run_created_with_protected_acl_and_no_command_child(self):
        from local_agent_manager.cindy_isolation_entry import private_run_directory
        path = self.root/'runs'/'run-fixture'; private_run_directory(path)
        proof = auth_permissions(path)
        self.assertTrue(proof['protected'])
        with self.assertRaises(OSError): private_run_directory(path)

    def test_native_auth_lock_blocks_full_range_peer_without_replacing_lock(self):
        home = self.root/'home'; home.mkdir()
        script = '''
import ctypes,sys
from ctypes import wintypes
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.CreateFileW.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
k.CreateFileW.restype=wintypes.HANDLE
class OVERLAPPED(ctypes.Structure):
 _fields_=[('internal',ctypes.c_size_t),('high',ctypes.c_size_t),('offset',wintypes.DWORD),('offsetHigh',wintypes.DWORD),('event',wintypes.HANDLE)]
k.LockFileEx.argtypes=(wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(OVERLAPPED))
k.LockFileEx.restype=wintypes.BOOL
h=k.CreateFileW(sys.argv[1],0xc0000000,7,None,3,0,None)
o=OVERLAPPED()
ok=k.LockFileEx(h,3,0,0xffffffff,0xffffffff,ctypes.byref(o))
error=ctypes.get_last_error()
raise SystemExit(0 if ok else 23 if error==33 else 24)
'''
        import sys
        with NativeAuthLocks([home]) as locks:
            path = home/'auth.lock'; identity = path.stat().st_ino
            self.assertEqual(locks.created, [str(path)])
            self.assertEqual(subprocess.run([sys.executable,'-c',script,str(path)],timeout=5).returncode,23)
        self.assertEqual(subprocess.run([sys.executable,'-c',script,str(path)],timeout=5).returncode,0)
        self.assertEqual(path.stat().st_ino, identity); self.assertEqual(path.stat().st_size, 0)

    def test_native_runtime_junction_is_qualified_separately_from_home(self):
        from local_agent_manager.cindy_isolation_recovery import digest
        import shutil
        source, target, physical = (self.root/name for name in ('official','legacy','release'))
        for directory in (source,target,physical): directory.mkdir()
        binary = physical/'codex.exe'; shutil.copyfile(Path(os.environ['SystemRoot'])/'System32/cmd.exe', binary)
        alias = self.root/'runtime-bin'
        result = subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/cmd.exe'),'/d','/c','mklink','/J',str(alias),str(physical)],
            capture_output=True,timeout=5,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0)
        spec={'source':{'path':str(source)},'target':{'path':str(target)},'controller':{'auth_lock_runtimes':{
            str(binary):{'version':'codex-cli 99.0.0','sha256':digest(binary)}}}}
        with patch('local_agent_manager.cindy_isolation_windows.auth_protocol_census',return_value=[
            {'name':'codex.exe','codex_home':str(target),'executable_path':str(alias/'codex.exe')}]):
            with self.assertRaisesRegex(ValueError, 'protocol_unqualified'): qualify_auth_writers(spec)
            from local_agent_manager.cindy_isolation_windows import AUTH_LOCK_PROTOCOL
            spec['controller']['auth_lock_runtimes'][str(binary)]['lock_protocol'] = AUTH_LOCK_PROTOCOL
            qualify_auth_writers(spec)
            spec['controller']['auth_lock_runtimes'][str(binary)]['sha256']='0'*64
            with self.assertRaises(ValueError): qualify_auth_writers(spec)

    def test_refresh_probe_rejects_a_writer_that_skips_the_native_lock(self):
        from contextlib import contextmanager
        from urllib.request import ProxyHandler, build_opener
        from local_agent_manager.cindy_auth_lock_probe import verify_refresh_lock

        @contextmanager
        def uncoordinated_server(*, codex_home, environment, **kwargs):
            class Server:
                def request(self, method, params):
                    path = codex_home / "auth.json"
                    auth = json.loads(path.read_bytes())
                    body = json.dumps({"refresh_token": auth["tokens"]["refresh_token"]}).encode()
                    url = environment["CODEX_REFRESH_TOKEN_URL_OVERRIDE"]
                    with build_opener(ProxyHandler({})).open(url, data=body, timeout=3) as response:
                        auth["tokens"].update(json.load(response))
                    path.write_text(json.dumps(auth))
            yield Server()

        with patch("local_agent_manager.cindy_auth_lock_probe.CodexAppServer", uncoordinated_server):
            with self.assertRaisesRegex(ValueError, "auth_probe_writer_did_not_wait"):
                verify_refresh_lock(self.root / "unused.exe")


if __name__ == '__main__': unittest.main()
