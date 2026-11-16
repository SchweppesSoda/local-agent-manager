from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parent.parent))
from tests import bootstrap as _test_bootstrap

import os
import unittest
from unittest.mock import patch
from local_agent_manager.cindy_isolation_lifecycle import CindyLifecycle
from local_agent_manager.cindy_isolation_plan import IsolationPlanError
from local_agent_manager.lifecycle_confirmation import LifecycleGate


class CindyIsolationLifecycleTests(unittest.TestCase):
    def setUp(self):
        if os.name == "nt":
            # The CI runner may be elevated; this fixture models an ordinary caller.
            ordinary = patch("ctypes.windll.shell32.IsUserAnAdmin", return_value=0)
            ordinary.start(); self.addCleanup(ordinary.stop)
        self.rows = [{"name": "Cindy.exe", "pid": 101}, {"name": "T3.exe", "pid": 202}]
        self.prompts, self.closed = [], []
        self.created = "first"
        def close(target):
            self.closed.append(target.pid)
            self.rows = [r for r in self.rows if r["pid"] != target.pid]
        def gate(scope):
            def prompt(message):
                self.prompts.append(message)
                return True
            return LifecycleGate(scope, prompt=prompt)
        self.caller = CindyLifecycle(run_id="this-run", executable="/Cindy.exe", profile="/profile",
            affected_tasks=["root-task", "second-account-task"], census=lambda: self.rows,
            identity=lambda pid: ("/Cindy.exe", self.created), close_window=close, gate_factory=gate)

    def test_observation_does_not_prompt_or_close(self):
        scope = self.caller.observe()
        self.assertEqual(scope.targets[0].affected_tasks, ("root-task", "second-account-task"))
        self.assertEqual(self.prompts, []); self.assertEqual(self.closed, [])

    def test_real_gate_and_close_keep_t3(self):
        result = self.caller.close()
        self.assertTrue(result["gate_displayed"])
        self.assertEqual(self.closed, [101])
        self.assertEqual(self.rows, [{"name": "T3.exe", "pid": 202}])
        self.assertFalse(result["startup_fence_held"])

    def test_changed_process_during_dialog_is_not_closed(self):
        def gate(scope):
            def prompt(message): self.created = "reused"; return True
            return LifecycleGate(scope, prompt=prompt)
        self.caller.gate_factory = gate
        with self.assertRaisesRegex(RuntimeError, "scope changed"): self.caller.close()
        self.assertEqual(self.closed, [])

    def test_no_process_means_no_unnecessary_lifecycle_dialog(self):
        self.rows = [r for r in self.rows if r["pid"] != 101]
        self.assertFalse(self.caller.close()["gate_displayed"])
        self.assertEqual(self.prompts, [])

    def test_unknown_process_identity_blocks_before_dialog(self):
        self.caller.identity = lambda _: None
        with self.assertRaises(IsolationPlanError): self.caller.close()
        self.assertEqual(self.prompts, [])

    @unittest.skipUnless(os.name == "nt", "Windows privilege boundary")
    def test_elevated_caller_is_rejected_before_observation_or_prompt(self):
        with patch("ctypes.windll.shell32.IsUserAnAdmin", return_value=1), \
             patch.object(self.caller, "observe") as observe:
            with self.assertRaisesRegex(IsolationPlanError, "lifecycle_requires_ordinary_user_process"):
                self.caller.close()
        observe.assert_not_called()
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.closed, [])


if __name__ == "__main__": unittest.main()
