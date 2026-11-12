from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from local_agent_manager.lifecycle_confirmation import (
    LifecycleGate, LifecycleScope, LifecycleTarget, windows_confirmation,
)


class LifecycleConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.target = LifecycleTarget('Fixture Agent', 123, 'fixture-creation-1',
                                      'C:/fixture/agent.exe', 'C:/fixture/home', ('fixture-task',))
        self.scope = LifecycleScope('fixture-run', (self.target,), ('close', 'restart'),
                                    reason='为了清理选中的聊天，需要关闭占用数据的软件。')
        self.prompt = Mock(return_value=True)  # No real UI or lifecycle action in tests.
        self.action = Mock()
        self.clock = Mock(return_value=1)
        self.gate = LifecycleGate(self.scope, prompt=self.prompt, clock=self.clock)

    def perform(self, **kw):
        args = dict(current_run_id=self.scope.run_id, current_target=self.target, execute=self.action)
        args.update(kw)
        return self.gate.perform('close', **args)

    def test_no_confirmation_cannot_arm_or_close(self):
        with self.assertRaises(RuntimeError):self.gate.arm(self.scope)
        with self.assertRaises(RuntimeError):self.perform()
        self.prompt.assert_not_called()
        self.action.assert_not_called()

    def test_cancel_does_not_arm_and_lists_exact_software_tasks_identity(self):
        self.prompt.return_value = False
        with self.assertRaises(RuntimeError):self.gate.confirm()
        with self.assertRaises(RuntimeError):self.gate.arm(self.scope)
        dialog = self.prompt.call_args.args[0]
        message = dialog.plain_text()
        for value in ('Fixture Agent', '123', 'fixture-creation-1', 'C:/fixture/agent.exe',
                      'C:/fixture/home', 'fixture-task', 'fixture-run', '关闭', '重新打开'):
            self.assertIn(value, message)
        self.assertIn(self.scope.reason, dialog.content)
        self.assertIn('Fixture Agent', dialog.content)
        self.assertIn('fixture-task', dialog.content)
        for technical in ('fixture-run', 'C:/fixture', 'fixture-creation-1', 'PID 123'):
            self.assertNotIn(technical, dialog.content)
        self.assertEqual(dialog.confirm_label, '关闭并继续')
        self.action.assert_not_called()

    def test_exact_confirmation_arm_action_and_no_replay_after_ambiguous_callback(self):
        self.gate.confirm()
        with self.assertRaises(RuntimeError):self.perform()  # Must arm first.
        self.gate.arm(self.scope)
        self.action.side_effect = OSError('fixture interrupted')
        with self.assertRaises(OSError):self.perform()
        with self.assertRaises(RuntimeError):self.perform()
        self.action.assert_called_once_with(self.target)

    def test_run_scope_process_creation_home_and_tasks_drift_block(self):
        self.gate.confirm()
        for scope in (replace(self.scope, run_id='other-run'),
                      replace(self.scope, reason='another reason'),
                      replace(self.scope, details='changed details'),
                      replace(self.scope, targets=(replace(self.target, affected_tasks=('other-task',)),)),
                      replace(self.scope, actions=('close',))):
            with self.assertRaises(RuntimeError):self.gate.arm(scope)
        self.gate.arm(self.scope)
        for field, value in (('pid', 124), ('created', 'reused-pid'), ('home', 'C:/other'),
                             ('executable', 'C:/other.exe'), ('affected_tasks', ('other-task',))):
            with self.assertRaises(RuntimeError):self.perform(current_target=replace(self.target, **{field: value}))
        with self.assertRaises(RuntimeError):self.perform(current_run_id='other-run')
        with self.assertRaises(RuntimeError):self.gate.perform('stop-all', current_run_id='fixture-run',
                                                               current_target=self.target, execute=self.action)
        self.action.assert_not_called()

    def test_expired_or_reconstructed_admission_is_not_confirmation(self):
        self.gate.confirm()
        self.gate.arm(self.scope)
        self.clock.return_value = 301
        with self.assertRaises(RuntimeError):self.perform()
        new_gate = LifecycleGate(self.scope, prompt=self.prompt)
        with self.assertRaises(RuntimeError):new_gate.arm(self.scope)
        self.action.assert_not_called()

    def test_missing_affected_tasks_rejected_before_popup(self):
        with self.assertRaises(ValueError):
            LifecycleGate(replace(self.scope, targets=(replace(self.target, affected_tasks=()),)), prompt=self.prompt)
        self.prompt.assert_not_called()

    def test_missing_reason_rejected_before_popup(self):
        with self.assertRaises(ValueError):
            LifecycleGate(replace(self.scope, reason=' '), prompt=self.prompt)
        self.prompt.assert_not_called()

    def test_noninteractive_desktop_never_displays_confirmation(self):
        with (patch('local_agent_manager.lifecycle_confirmation.os.name', 'nt'),
              patch('local_agent_manager.lifecycle_confirmation.is_interactive_windows_desktop',
                    return_value=(False, 'isolated')),
              patch('local_agent_manager.lifecycle_confirmation.show_confirmation') as show):
            with self.assertRaisesRegex(RuntimeError, 'desktop unavailable'):
                windows_confirmation('fixture')
            show.assert_not_called()


if __name__ == '__main__':
    unittest.main()
