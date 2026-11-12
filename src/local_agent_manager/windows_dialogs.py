"""Windows-owned confirmation UI; no lifecycle or cleanup actions live here."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConfirmationDialog:
    instruction: str
    content: str
    confirm_label: str
    details: str = ""
    title: str = "本地 Agent 管理器"

    def plain_text(self) -> str:
        return "\n\n".join(part for part in (self.instruction, self.content, self.details) if part)


@contextmanager
def _common_controls_v6():
    """Activate native themed controls on this thread, including python.exe hosts."""
    class ACTCTX(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.ULONG), ("dwFlags", wintypes.DWORD),
            ("lpSource", wintypes.LPCWSTR), ("wProcessorArchitecture", wintypes.WORD),
            ("wLangId", wintypes.WORD), ("lpAssemblyDirectory", wintypes.LPCWSTR),
            ("lpResourceName", wintypes.LPCWSTR), ("lpApplicationName", wintypes.LPCWSTR),
            ("hModule", wintypes.HMODULE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateActCtxW.argtypes = (ctypes.POINTER(ACTCTX),)
    kernel.CreateActCtxW.restype = wintypes.HANDLE
    kernel.ActivateActCtx.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t))
    kernel.ActivateActCtx.restype = wintypes.BOOL
    kernel.DeactivateActCtx.argtypes = (wintypes.DWORD, ctypes.c_size_t)
    kernel.DeactivateActCtx.restype = wintypes.BOOL
    kernel.ReleaseActCtx.argtypes = (wintypes.HANDLE,)
    kernel.ReleaseActCtx.restype = None
    config = ACTCTX(cbSize=ctypes.sizeof(ACTCTX), lpSource=str(Path(__file__).with_suffix(".manifest")))
    handle = kernel.CreateActCtxW(ctypes.byref(config))
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    cookie = ctypes.c_size_t()
    try:
        if not kernel.ActivateActCtx(handle, ctypes.byref(cookie)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            kernel.DeactivateActCtx(0, cookie)
    finally:
        kernel.ReleaseActCtx(handle)


def _task_dialog(message: ConfirmationDialog) -> bool:
    # CommCtrl.h wraps these two structures in pshpack1.h, including on x64.
    class BUTTON(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("nButtonID", ctypes.c_int), ("pszButtonText", wintypes.LPCWSTR)]

    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
        wintypes.LPARAM, ctypes.c_ssize_t,
    )

    class CONFIG(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("cbSize", wintypes.UINT), ("hwndParent", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE), ("dwFlags", wintypes.UINT),
            ("dwCommonButtons", wintypes.UINT), ("pszWindowTitle", wintypes.LPCWSTR),
            ("pszMainIcon", ctypes.c_void_p), ("pszMainInstruction", wintypes.LPCWSTR),
            ("pszContent", wintypes.LPCWSTR), ("cButtons", wintypes.UINT),
            ("pButtons", ctypes.POINTER(BUTTON)), ("nDefaultButton", ctypes.c_int),
            ("cRadioButtons", wintypes.UINT), ("pRadioButtons", ctypes.POINTER(BUTTON)),
            ("nDefaultRadioButton", ctypes.c_int), ("pszVerificationText", wintypes.LPCWSTR),
            ("pszExpandedInformation", wintypes.LPCWSTR), ("pszExpandedControlText", wintypes.LPCWSTR),
            ("pszCollapsedControlText", wintypes.LPCWSTR), ("pszFooterIcon", ctypes.c_void_p),
            ("pszFooter", wintypes.LPCWSTR), ("pfCallback", callback_type),
            ("lpCallbackData", ctypes.c_ssize_t), ("cxWidth", wintypes.UINT),
        ]

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT)
    user.SetWindowPos.restype = wintypes.BOOL

    @callback_type
    def on_event(hwnd, notification, _wparam, _lparam, _data):
        if notification == 0:  # TDN_CREATED: keep the confirmation above the invoking app.
            user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
        return 0

    confirm_id, cancel_id = 100, 2
    buttons = (BUTTON * 2)(BUTTON(confirm_id, message.confirm_label), BUTTON(cancel_id, "取消"))
    config = CONFIG(
        cbSize=ctypes.sizeof(CONFIG), dwFlags=0x8,  # Escape/Alt-F4/close all cancel.
        pszWindowTitle=message.title, pszMainInstruction=message.instruction,
        pszContent=message.content, cButtons=2, pButtons=buttons,
        nDefaultButton=cancel_id, pszExpandedInformation=message.details or None,
        pszExpandedControlText="查看技术详情", pszCollapsedControlText="收起技术详情",
        pfCallback=on_event, cxWidth=360,
    )
    pressed = ctypes.c_int(cancel_id)
    with _common_controls_v6():
        task_dialog = ctypes.WinDLL("comctl32", use_last_error=True).TaskDialogIndirect
        task_dialog.argtypes = (ctypes.POINTER(CONFIG), ctypes.POINTER(ctypes.c_int),
                               ctypes.POINTER(ctypes.c_int), ctypes.POINTER(wintypes.BOOL))
        task_dialog.restype = ctypes.c_long
        result = task_dialog(ctypes.byref(config), ctypes.byref(pressed), None, None)
    if result < 0:
        raise OSError(f"TaskDialogIndirect failed (HRESULT 0x{result & 0xffffffff:08x})")
    return result == 0 and pressed.value == confirm_id


def _message_box(message: ConfirmationDialog) -> bool:
    """Native fallback for hosts that cannot load Common Controls v6."""
    box = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
    box.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    box.restype = ctypes.c_int
    text = message.plain_text() + f"\n\n点击“确定”将{message.confirm_label}。"
    result = box(None, text, message.title, 0x1 | 0x30 | 0x100 | 0x1000 | 0x10000 | 0x40000)
    if result == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return result == 1


def show_confirmation(message: ConfirmationDialog) -> bool:
    try:
        return _task_dialog(message)
    except (OSError, AttributeError):
        return _message_box(message)
