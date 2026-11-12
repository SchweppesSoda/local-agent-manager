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
            code = "import sys; f=open(sys.argv[1],'rb'); print('ready',flush=True); sys.stdin.readline(); f.close()"
            with subprocess.Popen([sys.executable, '-B', '-c', code, str(path)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), 'ready')
                    rows = file_owners([path])
                    holder = next(row for row in rows if row['pid'] == child.pid)
                    self.assertEqual(holder['created_unix'], _kernel_process_start_unix(child.pid))
                    self.assertEqual(holder['resources'], [str(path)])
                finally:
                    child.communicate('\n', timeout=10)
            self.assertNotIn(child.pid, [row['pid'] for row in file_owners([path])])
