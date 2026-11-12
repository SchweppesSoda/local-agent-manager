"""Exercise native UI wiring with injected APIs, never click a real approval."""
from contextlib import nullcontext
import ctypes
import os
import unittest
from unittest.mock import Mock, patch

from local_agent_manager import windows_dialogs as dialogs


class WindowsDialogTests(unittest.TestCase):
    def setUp(self):
        self.message = dialogs.ConfirmationDialog('需要关闭以下软件', 'Fixture Agent',
                                                   '关闭并继续', 'PID 123')

    def test_cancel_and_error_never_become_approval(self):
        with (patch.object(dialogs, '_task_dialog', return_value=False) as task,
              patch.object(dialogs, '_message_box') as fallback):
            self.assertFalse(dialogs.show_confirmation(self.message))
            fallback.assert_not_called()
            task.side_effect = OSError('native controls unavailable')
            fallback.return_value = False
            self.assertFalse(dialogs.show_confirmation(self.message))
            fallback.assert_called_once_with(self.message)
            fallback.side_effect = OSError('no UI')
            with self.assertRaises(OSError):
                dialogs.show_confirmation(self.message)

    @unittest.skipUnless(os.name == 'nt', 'Win32 ABI')
    def test_native_buttons_default_cancel_and_expanded_details(self):
        def native(config_ptr, pressed_ptr, _radio, _verification):
            config = config_ptr._obj
            self.assertEqual(config.cbSize, 160 if ctypes.sizeof(ctypes.c_void_p) == 8 else 96)
            self.assertEqual(config.pszMainInstruction, self.message.instruction)
            self.assertEqual(config.pszContent, 'Fixture Agent')
            self.assertEqual(config.pszExpandedInformation, 'PID 123')
            self.assertEqual(config.nDefaultButton, 2)
            self.assertTrue(config.dwFlags & 0x8)  # Escape and title-bar close cancel.
            self.assertFalse(config.dwFlags & 0x80)  # Details start collapsed.
            self.assertEqual([(config.pButtons[i].nButtonID, config.pButtons[i].pszButtonText)
                              for i in range(config.cButtons)], [(100, '关闭并继续'), (2, '取消')])
            pressed_ptr._obj.value = selected
            return result

        dll = Mock()
        dll.TaskDialogIndirect.side_effect = native
        with (patch.object(dialogs, '_common_controls_v6', side_effect=nullcontext),
              patch.object(ctypes, 'WinDLL', return_value=dll)):
            for selected, expected in ((2, False), (0, False), (100, True), (1, False)):
                result = 0
                self.assertIs(dialogs._task_dialog(self.message), expected)
            result = -1
            with self.assertRaises(OSError):
                dialogs._task_dialog(self.message)

    @unittest.skipUnless(os.name == 'nt', 'Win32 API')
    def test_fallback_keeps_reason_identity_and_cancel_default(self):
        dll = Mock()
        with patch.object(ctypes, 'WinDLL', return_value=dll):
            dll.MessageBoxW.return_value = 2
            self.assertFalse(dialogs._message_box(self.message))
            _, text, title, flags = dll.MessageBoxW.call_args.args
            for part in (self.message.instruction, self.message.content, self.message.details,
                         self.message.confirm_label):
                self.assertIn(part, text)
            self.assertEqual(title, '本地 Agent 管理器')
            self.assertTrue(flags & 0x100)
            dll.MessageBoxW.return_value = 1
            self.assertTrue(dialogs._message_box(self.message))
            dll.MessageBoxW.return_value = 0
            with self.assertRaises(OSError):
                dialogs._message_box(self.message)

    @unittest.skipUnless(os.name == 'nt', 'Win32 API')
    def test_bundled_manifest_loads_native_v6_api_without_showing_a_dialog(self):
        with dialogs._common_controls_v6():
            self.assertTrue(ctypes.WinDLL('comctl32').TaskDialogIndirect)


if __name__ == '__main__':
    unittest.main()
