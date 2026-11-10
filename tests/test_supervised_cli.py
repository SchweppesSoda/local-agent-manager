from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from local_agent_manager.supervised_cli import wait_with_diagnostics, WriterStillRunning


class SupervisorTests(unittest.TestCase):
    def test_immediate_failure_dumps_stack_before_periodic_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'stacks'
            code = "import sys;from unittest.mock import patch;from local_agent_manager.supervised_cli import main;sys.argv=['fixture',sys.argv[1],'120'];\nwith patch('local_agent_manager.entrypoint.main',return_value=1): raise SystemExit(main())"
            result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn('supervised_cli.py', path.read_text())

    def run_child(self, body, observer=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notices = []
            with (root/'out').open('wb') as out, (root/'progress').open('wb') as err:
                result = wait_with_diagnostics([sys.executable, '-u', '-c', body], stdout=out, stderr=err,
                    progress_path=root/'progress', observe=observer or notices.append,
                    idle_seconds=.05, poll_seconds=.01)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((root/'out').read_text().strip(), 'complete')
            return notices

    def test_long_progress_outlives_many_intervals_without_kill(self):
        notices = self.run_child("import sys,time\nfor i in range(12):\n print(i,file=sys.stderr,flush=True);time.sleep(.025)\nprint('complete')")
        self.assertTrue(any(n['status'] == 'running' for n in notices), notices)
        self.assertFalse(any(n['status'] == 'suspected_stall' for n in notices), notices)

    def test_idle_is_diagnosed_but_not_killed_or_replayed(self):
        notices = self.run_child("import time;time.sleep(.25);print('complete')")
        self.assertTrue(any(n['status'] == 'suspected_stall' for n in notices), notices)
        self.assertTrue(all(not n.get('replay_allowed', False) for n in notices))

    def test_broken_diagnostic_sink_does_not_orphan_writer(self):
        def broken(_):
            raise OSError('sink unavailable')
        self.run_child("import time;time.sleep(.12);print('complete')", broken)

    def test_wait_interruption_reports_unknown_writer_without_kill(self):
        child = Mock(pid=123)
        child.wait.side_effect = KeyboardInterrupt()
        with patch('local_agent_manager.supervised_cli.subprocess.Popen', return_value=child):
            with self.assertRaises(WriterStillRunning) as error:
                wait_with_diagnostics(['synthetic'], stdout=None, stderr=None,
                    progress_path='unused', observe=lambda _: None)
        self.assertEqual(error.exception.pid, 123)
        child.kill.assert_not_called(); child.terminate.assert_not_called()

    def test_blocked_cli_dumps_stack_without_changing_its_exit_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A private child uses the actual diagnostics entry and a sleeping
            # CLI seam. No user profile or mutation command is invoked.
            code = "import sys,time;from unittest.mock import patch;from local_agent_manager.supervised_cli import main;sys.argv=['fixture',sys.argv[1],'.05'];\nwith patch('local_agent_manager.entrypoint.main',side_effect=lambda a:time.sleep(.2) or 0): main()"
            result = subprocess.run([sys.executable, '-c', code, str(root/'stacks')], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Timeout', (root/'stacks').read_text())
            self.assertIn('supervised_cli.py', (root/'stacks').read_text())
