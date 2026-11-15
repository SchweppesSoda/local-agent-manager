from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from local_agent_manager.cindy_isolation_lifecycle import CindyLifecycle, windows_identity
from local_agent_manager.cindy_isolation_windows import ReadLeases, auth_permissions
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
        result = cycle.close(); process.wait(timeout=5)
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


if __name__ == '__main__': unittest.main()
