"""Capture the client area of a window, even when it is covered by other windows."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass

import win32gui
import win32ui
from PIL import Image, ImageGrab

PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2
GA_ROOT = 2


@dataclass
class Capture:
    image: Image.Image
    method: str  # "printwindow" | "screen"
    origin: tuple[int, int]  # screen coords of client (0, 0)


def client_origin(hwnd: int) -> tuple[int, int]:
    return win32gui.ClientToScreen(hwnd, (0, 0))


def is_mostly_black(img: Image.Image, threshold: int = 12, ratio: float = 0.995) -> bool:
    hist = img.convert("L").histogram()
    dark = sum(hist[:threshold])
    return dark >= ratio * (img.width * img.height)


def _print_window(hwnd: int, w: int, h: int) -> Image.Image | None:
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hwnd_dc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(src, w, h)
        mem.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not ok:
            return None
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def is_client_visible(hwnd: int, grid: int = 5) -> bool:
    """True if the window is on top at a grid of sample points across its client area."""
    _, _, w, h = win32gui.GetClientRect(hwnd)
    ox, oy = client_origin(hwnd)
    for i in range(grid):
        for j in range(grid):
            pt = (ox + int((i + 0.5) * w / grid), oy + int((j + 0.5) * h / grid))
            try:
                top = win32gui.WindowFromPoint(pt)
            except win32gui.error:
                return False
            if not top or ctypes.windll.user32.GetAncestor(top, GA_ROOT) != hwnd:
                return False
    return True


def capture(hwnd: int) -> Capture | None:
    """PrintWindow first; fall back to a screen grab if the result is black and the window is fully visible."""
    if win32gui.IsIconic(hwnd):
        return None
    _, _, w, h = win32gui.GetClientRect(hwnd)
    if w <= 0 or h <= 0:
        return None
    origin = client_origin(hwnd)
    img = _print_window(hwnd, w, h)
    if img is not None and not is_mostly_black(img):
        return Capture(img, "printwindow", origin)
    if not is_client_visible(hwnd):
        return None
    ox, oy = origin
    img = ImageGrab.grab(bbox=(ox, oy, ox + w, oy + h), all_screens=True)
    return Capture(img.convert("RGB"), "screen", origin)
