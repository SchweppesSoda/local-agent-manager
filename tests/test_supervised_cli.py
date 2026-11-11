from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from local_agent_manager.supervised_cli import wait_with_diagnostics, WriterStillRunning, periodic_stacks
import io
import threading
import time


class SupervisorTests(unittest.TestCase):
    def test_handler_ownership_and_metadata_only_failure_sample(self):
        from local_agent_manager.supervised_cli import main
        for already_enabled in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'stacks'
                secret_fixture_local = 'do-not-dump-frame-locals'
                with patch.object(sys, 'argv', ['fixture', str(path), '120']), \
                        patch('local_agent_manager.entrypoint.main', return_value=1), \
                        patch('faulthandler.is_enabled', return_value=already_enabled), \
                        patch('faulthandler.enable') as enable, patch('faulthandler.disable') as disable:
                    self.assertEqual(main(), 1)
                self.assertEqual(enable.call_count, int(not already_enabled))
                self.assertEqual(disable.call_count, int(not already_enabled))
                self.assertNotIn(secret_fixture_local, path.read_text())

    def test_sampler_never_uses_unsafe_c_watchdog_and_stops_before_sink_closes(self):
        stream = io.StringIO()
        with patch('faulthandler.dump_traceback_later', side_effect=AssertionError('unsafe watchdog')), \
                patch('faulthandler.dump_traceback', side_effect=AssertionError('unsafe traversal')):
            with periodic_stacks(stream, .001) as dump:
                dump('explicit sample')
                time.sleep(.02)
        size = len(stream.getvalue())
        time.sleep(.01)
        self.assertEqual(len(stream.getvalue()), size)
        self.assertIn('explicit sample', stream.getvalue())
        self.assertIn('GIL-protected', stream.getvalue())
        self.assertFalse(any(t.name == 'lam-stack-sampler' for t in threading.enumerate()))

    def test_sampler_rejects_nonfinite_and_nonpositive_intervals(self):
        for interval in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError), periodic_stacks(io.StringIO(), interval):
                pass

    def test_failed_stack_sink_does_not_change_work_result(self):
        stream = Mock()
        stream.write.side_effect = OSError('sink failed')
        with periodic_stacks(stream, .001) as dump:
            dump()
            time.sleep(.01)

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
        # Hosted scheduling can exceed the tiny diagnostic interval even while
        # this child makes progress. A stall notice must never kill or replay it.
        self.assertEqual(notices[-1]['status'], 'exited')
        self.assertTrue(all(not n.get('writer_terminated', False) for n in notices))
        self.assertTrue(all(not n.get('replay_allowed', False) for n in notices))

    def test_observed_progress_resets_idle_across_many_intervals(self):
        child = Mock(pid=123)
        child.wait.side_effect = [subprocess.TimeoutExpired('synthetic', .01)] * 12 + [0]
        notices = []
        with patch('local_agent_manager.supervised_cli.subprocess.Popen', return_value=child), \
                patch('local_agent_manager.supervised_cli.time.monotonic',
                      side_effect=[0] + [i * .025 for i in range(1, 14)]), \
                patch('local_agent_manager.supervised_cli.Path.stat',
                      side_effect=[Mock(st_size=i, st_mtime_ns=i) for i in range(12)]):
            result = wait_with_diagnostics(['synthetic'], stdout=None, stderr=None,
                progress_path='unused', observe=notices.append, idle_seconds=.05, poll_seconds=.01)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(any(n['status'] == 'running' for n in notices), notices)
        self.assertFalse(any(n['status'] == 'suspected_stall' for n in notices), notices)
        self.assertGreater(notices[-1]['elapsed_seconds'], .05 * 5)
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

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
