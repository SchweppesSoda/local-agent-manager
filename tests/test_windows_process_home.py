import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_agent_manager.windows_process_home import _extract_home, read_codex_home
from local_agent_manager.codex_desktop_state import _relevant_client_names


class ProcessHomeTests(unittest.TestCase):
    def test_projection_requires_one_explicit_home_and_discards_other_values(self):
        raw = 'SECRET=never returned\0CODEX_HOME=C:\\separate\0\0'.encode('utf-16-le')
        self.assertEqual(_extract_home(raw), 'C:\\separate')
        for text in ('SECRET=x\0\0', 'CODEX_HOME=\0\0', 'CODEX_HOME=a\0codex_home=b\0\0', 'CODEX_HOME=a'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _extract_home(text.encode('utf-16-le'))

    def test_default_requires_absent_override_and_qualified_process_profile(self):
        profile = Path('C:/process-user')
        raw = 'USERPROFILE=C:\\wrong\0HOME=C:\\wrong\0SECRET=discard\0\0'.encode('utf-16-le')
        self.assertEqual(_extract_home(raw, default_home=profile), str(profile/'.codex'))
        for value in ('CODEX_HOME=\0\0', 'CODEX_HOME=a\0codex_home=b\0\0', 'CODEX_HOME=a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _extract_home(value.encode('utf-16-le'), default_home=profile)
        self.assertEqual(_extract_home('CODEX_HOME=C:\\explicit\0\0'.encode('utf-16-le'),
                                       default_home=profile), 'C:\\explicit')

    def test_only_verified_existing_separate_process_home_is_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            native, separate = root / 'native', root / 'cli'
            native.mkdir(); separate.mkdir()
            executable = root / 'codex.exe'
            executable.write_bytes(b'fixture')
            record = {'process_id': 10, 'parent_process_id': 1, 'name': 'codex.exe',
                      'executable_path': str(executable), 'command_line': 'codex.exe app-server',
                      'codex_home': str(separate), 'codex_home_source': 'verified_process_environment'}
            self.assertEqual(_relevant_client_names(native, [record]), ())
            default_record = {**record, 'codex_home_source': 'verified_process_profile_default'}
            desktop = {'process_id': 1, 'parent_process_id': 0, 'name': 'ChatGPT.exe'}
            self.assertEqual(_relevant_client_names(native, [default_record, desktop]), ())
            self.assertEqual(set(_relevant_client_names(separate, [default_record, desktop])), {'ChatGPT.exe', 'codex.exe'})
            for changes in ({'codex_home': str(native)}, {'codex_home': str(root / 'missing')},
                            {'codex_home': 'relative'}, {'codex_home_source': 'configured_hint'},
                            {'name': 'ChatGPT.exe'}):
                with self.subTest(changes=changes):
                    self.assertEqual(len(_relevant_client_names(native, [{**record, **changes}])), 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows process environment')
    def test_live_fixture_process_identity_and_narrow_environment_read(self):
        # Disposable child only: no production client or native store is used.
        env = dict(os.environ, CODEX_HOME='C:\\fixture-codex-home', LAM_SECRET_FIXTURE='not returned')
        child = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read(1)'],
                                 env=env, stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                f'(Get-CimInstance Win32_Process -Filter "ProcessId={child.pid}").CreationDate.ToUniversalTime().Ticks.ToString()'],
                capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            ticks = result.stdout.strip()
            self.assertEqual(read_codex_home(child.pid, sys.executable, ticks), env['CODEX_HOME'])
            with self.assertRaises(ValueError):
                read_codex_home(child.pid, sys.executable, int(ticks) - 100000)
            with self.assertRaises((ValueError, OSError)):
                read_codex_home(child.pid, str(Path(sys.executable).parent), ticks)
        finally:
            child.communicate(b'x', timeout=5)

    @unittest.skipUnless(os.name == 'nt', 'Windows process profile')
    def test_default_uses_child_token_profile_not_environment_hints(self):
        expected = Path.home()/'.codex'
        env = {k:v for k,v in os.environ.items() if k.casefold() != 'codex_home'}
        env.update(USERPROFILE='C:\\wrong-userprofile', HOME='C:\\wrong-home', LAM_SECRET_FIXTURE='not returned')
        child = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read(1)'],
                                 env=env, stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                f'(Get-CimInstance Win32_Process -Filter "ProcessId={child.pid}").CreationDate.ToUniversalTime().Ticks.ToString()'],
                capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            with self.assertRaisesRegex(ValueError, 'process_home_absent_or_ambiguous'):
                read_codex_home(child.pid, sys.executable, result.stdout.strip())
            self.assertEqual(Path(read_codex_home(child.pid, sys.executable, result.stdout.strip(), allow_default=True)), expected)
            with self.assertRaisesRegex(ValueError, 'process_creation_changed'):
                read_codex_home(child.pid, sys.executable, int(result.stdout.strip())-100000, allow_default=True)
        finally:
            child.communicate(b'x', timeout=5)
