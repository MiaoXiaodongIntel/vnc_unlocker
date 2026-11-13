"""OCR (Windows.Media.Ocr via winocr) and lock-screen classification."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from PIL import Image


class State(str, Enum):
    NOT_LOCKED = "not_locked"
    CURTAIN = "curtain"          # lock screen with big clock/date, no password box yet
    PASSWORD = "password"        # single user tile + password box
    USER_PASS = "user_pass"      # "Other user": user name + password boxes
    INCORRECT = "incorrect"      # "The password is incorrect" message
    CAD = "cad"                  # "Press Ctrl+Alt+Del" (unsupported)


@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float
    norm: str = field(init=False)

    def __post_init__(self):
        self.norm = normalize(self.text)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class Analysis:
    state: State
    lines: list[Line]
    width: int
    height: int
    reason: str = ""

    def central(self) -> list[Line]:
        return [l for l in self.lines if 0.25 * self.width <= l.cx <= 0.75 * self.width]

    def bottom_left(self) -> list[Line]:
        return [l for l in self.lines if l.cx < 0.35 * self.width and l.cy > 0.5 * self.height]

    def find_central(self, keywords: list[str], max_len: int = 40) -> Line | None:
        for l in self.central():
            if len(l.norm) <= max_len and any(k in l.norm for k in keywords):
                return l
        return None


_WS = re.compile(r"[\s\-‐‑–—_]+")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_CLOCKISH = re.compile(r"^\d{1,2}[:.]?\S{0,3}$")
_DATE = re.compile(
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|january|february|march|april|may|june|july|august|september|october|november|december"
    r"|星期[一二三四五六日天]|周[一二三四五六日天]|\d{1,2}月\d{1,2}日)"
)


def normalize(text: str) -> str:
    return _WS.sub("", unicodedata.normalize("NFKC", text)).lower()


def ocr_lines(img: Image.Image, languages: list[str], min_width: int = 1600) -> list[Line]:
    import winocr

    scale = 1.0
    if img.width < min_width:
        scale = min(2.0, min_width / img.width)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    lines: list[Line] = []
    for lang in languages:
        result = winocr.recognize_pil_sync(img, lang)
        for ln in result.get("lines", []):
            words = ln.get("words") or []
            if not words:
                continue
            rects = [w["bounding_rect"] for w in words]
            x0 = min(r["x"] for r in rects)
            y0 = min(r["y"] for r in rects)
            x1 = max(r["x"] + r["width"] for r in rects)
            y1 = max(r["y"] + r["height"] for r in rects)
            lines.append(Line(ln.get("text", ""), x0 / scale, y0 / scale, (x1 - x0) / scale, (y1 - y0) / scale))
    return lines


def _any(lines: list[Line], keywords: list[str], max_len: int | None = None) -> Line | None:
    for l in lines:
        if max_len is not None and len(l.norm) > max_len:
            continue
        if any(k in l.norm for k in keywords):
            return l
    return None


def classify(lines: list[Line], width: int, height: int, keywords: dict[str, list[str]], max_lines: int = 30,
             user_names=()) -> Analysis:
    a = Analysis(State.NOT_LOCKED, lines, width, height)
    if not lines:
        a.reason = "no text"
        return a

    # Lock screens have no taskbar: a small clock in the bottom-right corner means a normal desktop.
    for l in lines:
        if _TIME.match(l.norm) and l.cy > 0.9 * height and l.cx > 0.75 * width and l.h < 0.05 * height:
            a.reason = f"taskbar clock '{l.text}'"
            return a

    # Checked before max_lines: the Win11 lock curtain may show a widget panel full of text.
    curtain = _find_curtain(lines, height)
    if curtain:
        a.state, a.reason = State.CURTAIN, curtain
        return a

    unique = {l.norm for l in lines}
    if len(unique) > max_lines:
        a.reason = f"too many lines ({len(unique)})"
        return a

    central = a.central()
    hit = _any(central, keywords["incorrect"])
    if hit:
        a.state, a.reason = State.INCORRECT, hit.text
        return a
    hit = _any(lines, keywords["cad"])
    if hit:
        a.state, a.reason = State.CAD, hit.text
        return a
    pw = _any(central, keywords["password"], max_len=40)
    if pw:
        user = _any(central, keywords["username"], max_len=40)
        if user:
            a.state, a.reason = State.USER_PASS, f"{user.text} / {pw.text}"
        else:
            a.state, a.reason = State.PASSWORD, pw.text
        return a

    # The small "Password" placeholder is often misread on busy backgrounds ("has swo rd"),
    # but the user tile name is large and sits exactly in the horizontal centre.
    tile = _find_user_tile(lines, width, height, user_names)
    if tile:
        a.state, a.reason = State.PASSWORD, f"user tile '{tile.text}'"
        return a

    a.reason = "no lock indicators"
    return a


def _find_user_tile(lines: list[Line], width: int, height: int, user_names) -> Line | None:
    names = {normalize(n) for n in user_names if normalize(n)}
    if not names:
        return None
    for l in lines:
        if l.norm in names and abs(l.cx - width / 2) <= 0.03 * width and 0.3 * height <= l.cy <= 0.7 * height:
            return l
    return None


def _find_curtain(lines: list[Line], height: int) -> str | None:
    """Big clock directly above a short date line ("Thursday, November 12" / "11月12日 星期四").

    The thin clock font is often misread ("9:iQ"), so the clock only needs to be a large
    line containing a digit that horizontally overlaps the date line.
    """
    for date in lines:
        if len(date.norm) > 30 or not _DATE.search(date.norm):
            continue
        for clock in lines:
            if clock is date or clock.h < max(0.05 * height, 1.8 * date.h) or not _CLOCKISH.search(clock.norm):
                continue
            gap = date.y - (clock.y + clock.h)
            overlap = min(clock.x + clock.w, date.x + date.w) - max(clock.x, date.x)
            if -0.2 * clock.h <= gap <= 1.0 * clock.h and overlap > 0:
                return f"{clock.text} / {date.text}"
    return None


def analyze(img: Image.Image, languages: list[str], keywords: dict[str, list[str]],
            min_width: int = 1600, max_lines: int = 30, user_names=()) -> Analysis:
    return classify(ocr_lines(img, languages, min_width), img.width, img.height, keywords, max_lines, user_names)
