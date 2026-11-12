import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_agent_manager.windows_file_owners import file_owners
from local_agent_manager.t3code_handoff import _kernel_process_start_unix


@unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager')
class FileOwnerTests(unittest.TestCase):
    def test_real_holder_identity_and_release_on_temporary_database(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)/'only-synthetic.sqlite'
            path.write_bytes(b'synthetic')
            code = ("import os, sys; f=open(sys.argv[1],'rb'); print(os.getpid(),flush=True); "
                    "sys.stdin.readline(); f.close()")
            with subprocess.Popen([sys.executable, '-B', '-c', code, str(path)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW) as child:
                try:
                    # In a Windows virtual environment, Popen can identify the launcher.
                    holder_pid = int(child.stdout.readline())
                    rows = file_owners([path])
                    holder = next(row for row in rows if row['pid'] == holder_pid)
                    self.assertEqual(holder['created_unix'], _kernel_process_start_unix(holder_pid))
                    self.assertEqual(holder['resources'], [str(path)])
                finally:
                    child.communicate('\n', timeout=10)
            self.assertNotIn(holder_pid, [row['pid'] for row in file_owners([path])])
