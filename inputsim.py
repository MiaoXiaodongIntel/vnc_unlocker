"""Low-level keyboard/mouse injection with user-interruption detection."""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import win32gui
import win32process

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP = 0x2
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_RETURN, VK_TAB, VK_SPACE, VK_CAPITAL, VK_BACK = 0x0D, 0x09, 0x20, 0x14, 0x08
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]


class Interrupted(Exception):
    def __init__(self, msg: str, by_user: bool = False):
        super().__init__(msg)
        self.by_user = by_user


class UnsupportedChar(Exception):
    pass


def last_input_tick() -> int:
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(info))
    return info.dwTime


def idle_seconds() -> float:
    return ((kernel32.GetTickCount() - last_input_tick()) & 0xFFFFFFFF) / 1000.0


def caps_lock_on() -> bool:
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


def mouse_button_down() -> bool:
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000 or user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)


def char_to_vk(ch: str) -> tuple[int, bool]:
    """Map a character to (virtual key, needs_shift) using the current keyboard layout."""
    r = user32.VkKeyScanW(ch)
    if r == -1:
        raise UnsupportedChar("character not on keyboard layout")
    vk, mods = r & 0xFF, (r >> 8) & 0xFF
    if mods & ~1:
        raise UnsupportedChar("character needs Ctrl/Alt")
    return vk, bool(mods & 1)


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise OSError(ctypes.get_last_error(), "SendInput failed")


def _key(vk: int, up: bool) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP if up else 0, 0, 0)))


def _mouse(flags: int) -> INPUT:
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)))


def _wait_foreground(hwnd: int, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


def force_foreground(hwnd: int, timeout: float = 1.0) -> bool:
    """Bring hwnd to the foreground despite Windows' foreground lock.

    A background process may only call SetForegroundWindow under narrow conditions; an injected
    Alt press lifts the lock. Alt is released only after the switch, so the previous window never
    sees a lone Alt tap (which would open its menu bar).
    """
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    attached = bool(fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    if _wait_foreground(hwnd, 0.3):
        return True
    _send(_key(VK_MENU, False))
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        _send(_key(VK_MENU, True))
    if _wait_foreground(hwnd, timeout / 2):
        return True
    user32.SwitchToThisWindow(hwnd, True)
    return _wait_foreground(hwnd, timeout / 2)


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]


def foreground_process_name() -> str:
    """Executable name (lowercase, e.g. 'lockapp.exe') of the foreground window's process, or ''."""
    fg = user32.GetForegroundWindow()
    if not fg:
        return ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(fg)
    except Exception:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(h)


def describe_foreground() -> str:
    fg = user32.GetForegroundWindow()
    if not fg:
        return "no foreground window"
    try:
        _, pid = win32process.GetWindowThreadProcessId(fg)
    except Exception:
        pid = 0
    return f"'{win32gui.GetWindowText(fg)[:60]}' (pid {pid})"


class InputSession:
    """Sends input to one window; aborts if focus moves or any input not sent by us is detected."""

    def __init__(self, hwnd: int, key_delay: float = 0.03):
        self.hwnd = hwnd
        self.key_delay = key_delay
        self._tick = last_input_tick()
        self._cursor = win32gui.GetCursorPos()
        self.previous_fg = user32.GetForegroundWindow()

    def _settle(self) -> None:
        # Baseline right after our own input is processed, then wait: input during the wait is caught by check().
        # The cursor baseline is only changed by click(), so any foreign mouse movement is always detected.
        time.sleep(0.015)
        self._tick = last_input_tick()
        time.sleep(0.03 + self.key_delay)

    def check(self) -> None:
        if user32.GetForegroundWindow() != self.hwnd:
            raise Interrupted("focus left target window", by_user=True)
        if last_input_tick() != self._tick or win32gui.GetCursorPos() != self._cursor:
            raise Interrupted("user input detected", by_user=True)

    def focus(self) -> None:
        if not force_foreground(self.hwnd):
            raise Interrupted(f"could not focus target window (foreground: {describe_foreground()})")
        self._settle()

    def key(self, vk: int, shift: bool = False, ctrl: bool = False) -> None:
        self.check()
        seq = []
        if ctrl:
            seq.append(_key(VK_CONTROL, False))
        if shift:
            seq.append(_key(VK_SHIFT, False))
        seq += [_key(vk, False), _key(vk, True)]
        if shift:
            seq.append(_key(VK_SHIFT, True))
        if ctrl:
            seq.append(_key(VK_CONTROL, True))
        _send(*seq)
        self._settle()

    def text(self, s: str) -> None:
        keys = [char_to_vk(ch) for ch in s]  # validate everything before typing anything
        for vk, shift in keys:
            self.key(vk, shift)

    def click(self, x: int, y: int) -> None:
        self.check()
        user32.SetCursorPos(int(x), int(y))
        self._cursor = win32gui.GetCursorPos()
        _send(_mouse(MOUSEEVENTF_LEFTDOWN), _mouse(MOUSEEVENTF_LEFTUP))
        self._settle()

    def wait(self, seconds: float) -> None:
        """Sleep while still watching for user interruption."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(0.1)

    def restore_focus(self) -> None:
        prev = self.previous_fg
        if prev and prev != self.hwnd and win32gui.IsWindow(prev):
            force_foreground(prev, timeout=0.5)
