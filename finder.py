"""Enumerate RealVNC Viewer session windows."""
from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from dataclasses import dataclass

import win32gui
import win32process

# "123456 (MYHOST01) - RealVNC Viewer" -> name=123456, host=MYHOST01
TITLE_RE = re.compile(r"^(?P<name>.+?)(?:\s+\((?P<host>[^()]+)\))?\s+-\s+RealVNC Viewer$")
VIEWER_EXE = "vncviewer.exe"


@dataclass
class VncWindow:
    hwnd: int
    title: str
    name: str
    host: str | None
    minimized: bool

    @property
    def key(self) -> str:
        return self.host or self.name


def parse_title(title: str) -> tuple[str, str | None] | None:
    m = TITLE_RE.match(title.strip())
    if not m:
        return None
    return m.group("name").strip(), (m.group("host") or "").strip() or None


def _process_exe(hwnd: int) -> str:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        k32.CloseHandle(h)


def find_windows() -> list[VncWindow]:
    result: list[VncWindow] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        parsed = parse_title(title)
        if parsed and _process_exe(hwnd).lower() == VIEWER_EXE:
            name, host = parsed
            result.append(VncWindow(hwnd, title, name, host, bool(win32gui.IsIconic(hwnd))))
        return True

    win32gui.EnumWindows(cb, None)
    return result
