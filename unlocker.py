"""Per-host unlock state machine."""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import logging
import secrets
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

import win32service

import capture
import credstore
import detector
from config import Account, HostConfig, Settings
from detector import Analysis, State
from finder import VncWindow, find_windows
from inputsim import (VK_BACK, VK_RETURN, VK_SPACE, VK_TAB, InputSession, Interrupted, UnsupportedChar,
                      caps_lock_on, foreground_process_name, idle_seconds, last_input_tick, mouse_button_down)
from PIL import Image, ImageChops

log = logging.getLogger("vnc_unlocker")

SETTLE_POLL = 0.5


def frames_equal(a: Image.Image, b: Image.Image, max_changed: float = 0.002) -> bool:
    """True if at most a tiny fraction of pixels differ (tolerates a blinking caret)."""
    if a.size != b.size:
        return False
    diff = ImageChops.difference(a.convert("L"), b.convert("L")).point(lambda v: 255 if v > 24 else 0)
    if diff.getbbox() is None:
        return True
    changed = diff.histogram()[255]
    return changed <= max_changed * a.width * a.height


@dataclass
class HostState:
    attempts: int = 0
    failures: int = 0  # consecutive runs that ended without typing a password (focus/capture/unknown screen)
    paused: bool = False
    next_action: float = 0.0
    last_state: State | None = None
    last_notice: str = ""
    waiting: str = ""
    paused_cred: str | None = None
    paused_fp: bytes | None = None


_FP_KEY = secrets.token_bytes(32)


def password_fingerprint(cred: str) -> bytes | None:
    """Keyed hash used only to notice that a stored password changed; the key never leaves this process."""
    pw = credstore.get_password(cred)
    return None if pw is None else hmac.new(_FP_KEY, pw.encode("utf-8"), hashlib.sha256).digest()


class _WTSINFOEX_LEVEL1(ctypes.Structure):
    _fields_ = [("SessionId", wintypes.ULONG), ("SessionState", ctypes.c_int), ("SessionFlags", wintypes.LONG)]


class _WTSINFOEX_LEVEL(ctypes.Union):
    _fields_ = [("Level1", _WTSINFOEX_LEVEL1), ("_align", ctypes.c_longlong)]  # real struct has LARGE_INTEGERs


class _WTSINFOEX(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", _WTSINFOEX_LEVEL)]


_wtsapi32 = ctypes.WinDLL("wtsapi32")
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_SESSION_INFO_EX = 25
WTS_SESSIONSTATE_LOCK = 0


def _session_flag_locked() -> bool:
    """WTS session lock flag; set as soon as the session is locked, even while LockApp's curtain is shown."""
    buf, size = ctypes.c_void_p(), wintypes.DWORD()
    if not _wtsapi32.WTSQuerySessionInformationW(None, wintypes.DWORD(WTS_CURRENT_SESSION), WTS_SESSION_INFO_EX,
                                                 ctypes.byref(buf), ctypes.byref(size)):
        return False
    try:
        info = _WTSINFOEX.from_address(buf.value)
        return info.Level == 1 and info.Data.Level1.SessionFlags == WTS_SESSIONSTATE_LOCK
    finally:
        _wtsapi32.WTSFreeMemory(buf)


def is_workstation_locked() -> bool:
    """True if the local session is locked.

    The input desktop only switches away from 'Default' once the password prompt is shown; while
    the lock screen curtain (LockApp) is up it is still 'Default', so the WTS flag and the
    foreground process are checked as well.
    """
    try:
        desk = win32service.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    except win32service.error:
        return True
    try:
        if str(win32service.GetUserObjectInformation(desk, 2)).lower() != "default":  # UOI_NAME
            return True
    finally:
        desk.CloseDesktop()
    try:
        if _session_flag_locked():
            return True
    except Exception:
        log.debug("WTS lock query failed", exc_info=True)
    return foreground_process_name() == "lockapp.exe"


def match_account(a: Analysis, accounts: list[Account]) -> Account | None:
    """Account whose display name appears in the centre of the screen (the selected user tile)."""
    best: tuple[int, Account] | None = None
    ambiguous = False
    for line in a.central():
        for acc in accounts:
            for dn in acc.display_names:
                n = detector.normalize(dn)
                if n and n in line.norm:
                    if best is None or len(n) > best[0]:
                        best, ambiguous = (len(n), acc), False
                    elif len(n) == best[0] and best[1] is not acc:
                        ambiguous = True
    return None if best is None or ambiguous else best[1]


def find_tile(a: Analysis, account: Account) -> detector.Line | None:
    """User tile for 'account' in the bottom-left user list."""
    names = [detector.normalize(dn) for dn in account.display_names]
    for line in a.bottom_left():
        if any(n and n in line.norm for n in names):
            return line
    return None


class Unlocker:
    def __init__(self, settings: Settings, notify: Callable[[str, str], None] | None = None):
        self.settings = settings
        self.notify_cb = notify or (lambda title, msg: None)
        self.hosts: dict[str, HostState] = {}
        self.paused = False
        self._own_tick: int | None = None  # GetLastInputInfo tick right after our last injected input
        self._idle_origin = 0.0            # monotonic time of the last real user input

    # ---------- helpers ----------
    def _state(self, key: str) -> HostState:
        return self.hosts.setdefault(key, HostState())

    def _notice(self, key: str, msg: str, level: int = logging.WARNING) -> None:
        hs = self._state(key)
        log.log(level, "[%s] %s", key, msg)
        if hs.last_notice != msg:
            hs.last_notice = msg
            self.notify_cb(f"VNC Unlocker: {key}", msg)

    def _pause_host(self, key: str, msg: str, cred: str | None = None) -> None:
        hs = self._state(key)
        hs.paused = True
        hs.paused_cred = cred
        hs.paused_fp = password_fingerprint(cred) if cred else None
        hint = "save a new password or use tray 'Reset hosts' to retry" if cred else "use tray 'Reset hosts' to retry"
        self._notice(key, f"{msg} — host paused ({hint})", logging.ERROR)

    def _resume_if_password_changed(self, key: str, hs: HostState) -> None:
        """Hosts paused because of a credential resume automatically once that password is updated."""
        if hs.paused and hs.paused_cred and password_fingerprint(hs.paused_cred) != hs.paused_fp:
            log.info("[%s] password for '%s' was updated; host resumed", key, hs.paused_cred)
            hs.attempts, hs.failures, hs.paused, hs.next_action, hs.last_notice, hs.waiting = 0, 0, False, 0.0, "", ""
            hs.paused_cred = hs.paused_fp = None

    def _fail(self, key: str, hs: HostState, reason: str) -> None:
        """An unlock run ended without typing a password; pause after too many in a row instead of retrying forever."""
        hs.failures += 1
        hs.next_action = time.monotonic() + self.settings.cooldown
        if hs.failures >= self.settings.max_failures:
            self._pause_host(key, f"gave up after {hs.failures} failed runs in a row (last: {reason})")
        else:
            log.info("[%s] run %d/%d failed: %s", key, hs.failures, self.settings.max_failures, reason)

    def _waiting(self, key: str, hs: HostState, reason: str) -> None:
        """Log why a locked host is not being acted on (once per reason)."""
        if hs.waiting != reason:
            hs.waiting = reason
            log.info("[%s] locked; %s", key, reason)

    def reset_hosts(self) -> None:
        for hs in self.hosts.values():
            hs.attempts, hs.failures, hs.paused, hs.next_action, hs.last_notice, hs.waiting = 0, 0, False, 0.0, "", ""
            hs.paused_cred = hs.paused_fp = None
        log.info("all hosts reset")

    def _analyze(self, w: VncWindow) -> tuple[Analysis | None, capture.Capture | None]:
        cap = capture.capture(w.hwnd)
        if cap is None:
            return None, None
        s = self.settings
        return s.analyze(cap.image), cap

    # ---------- main entry ----------
    def scan(self) -> None:
        if self.paused or is_workstation_locked():
            return
        for w in find_windows():
            try:
                self._scan_window(w)
            except Exception:
                log.exception("[%s] scan failed", w.key)

    def _scan_window(self, w: VncWindow) -> None:
        if w.minimized:
            return
        host = self.settings.host_for_title(w.title)
        if host is None:
            return
        hs = self._state(w.key)
        a, cap = self._analyze(w)
        if a is None:
            log.debug("[%s] capture unavailable", w.key)
            return
        if a.state != hs.last_state:
            log.info("[%s] state %s (%s, via %s)", w.key, a.state.value, a.reason, cap.method)
            hs.last_state = a.state
            hs.waiting = ""
        if a.state == State.NOT_LOCKED:
            if hs.attempts or hs.failures or hs.paused:
                log.info("[%s] unlocked; counters reset", w.key)
            hs.attempts, hs.failures, hs.paused, hs.last_notice = 0, 0, False, ""
            return
        if a.state == State.CAD:
            self._notice(w.key, "Ctrl+Alt+Del screen is not supported")
            return
        self._resume_if_password_changed(w.key, hs)
        if hs.paused:
            return self._waiting(w.key, hs, "host paused (tray 'Reset hosts' to retry)")
        if time.monotonic() < hs.next_action:
            return self._waiting(w.key, hs, "cooldown")
        idle = self._user_idle()
        if idle < self.settings.idle_threshold or mouse_button_down():
            return self._waiting(w.key, hs, f"waiting for local user to be idle {self.settings.idle_threshold:.0f}s")
        if caps_lock_on():
            self._notice(w.key, "Caps Lock is on; skipping auto unlock")
            return
        hs.waiting = ""
        if is_workstation_locked():  # the local session may have locked during OCR; never inject into it
            return
        log.info("[%s] local user idle %.0fs; starting unlock", w.key, idle)
        if self._own_tick is None:
            self._idle_origin = time.monotonic() - idle
        by_user = False
        session = InputSession(w.hwnd, self.settings.key_delay)
        try:
            session.focus()
            self._act(w, host, hs, session, a, cap)
        except Interrupted as e:
            by_user = e.by_user
            log.info("[%s] aborted: %s", w.key, e)
            hs.next_action = time.monotonic() + self.settings.cooldown
            if not by_user:
                self._fail(w.key, hs, str(e))
        finally:
            try:
                session.restore_focus()
            except Exception:
                pass
            # Our own injected input resets GetLastInputInfo; remember it so the next locked window
            # does not wait another idle_threshold. Real user input clears this.
            self._own_tick = None if by_user else last_input_tick()

    def _user_idle(self) -> float:
        """Seconds since the last *user* input, ignoring input injected by this program."""
        if self._own_tick is not None and last_input_tick() == self._own_tick:
            return time.monotonic() - self._idle_origin
        self._own_tick = None
        return idle_seconds()

    def _settled(self, w: VncWindow, session: InputSession | None, timeout: float,
                 until: Callable[[Analysis], bool] = lambda a: True) -> tuple[Analysis, capture.Capture]:
        """Poll until the VNC picture stops changing (ignoring a blinking caret) and 'until' holds.

        RealVNC updates can lag several seconds behind the remote; typing during a lock-screen
        animation loses keystrokes.
        """
        s = self.settings
        end = time.monotonic() + timeout
        prev = None
        last: tuple[Analysis, capture.Capture] | None = None
        while True:
            cap = capture.capture(w.hwnd)
            if cap is None:
                raise Interrupted("capture unavailable")
            if prev is not None and frames_equal(prev, cap.image):
                a = s.analyze(cap.image)
                last = (a, cap)
                if until(a):
                    log.info("[%s] state %s (%s)", w.key, a.state.value, a.reason)
                    return a, cap
            prev = cap.image
            if time.monotonic() >= end:
                if last is None:
                    cap = capture.capture(w.hwnd) or cap
                    a = s.analyze(cap.image)
                    last = (a, cap)
                log.info("[%s] screen did not settle in %.0fs; state %s (%s)",
                         w.key, timeout, last[0].state.value, last[0].reason)
                return last
            if session is not None:
                session.wait(SETTLE_POLL)
            else:
                time.sleep(SETTLE_POLL)

    def _act(self, w: VncWindow, host: HostConfig, hs: HostState, session: InputSession,
             a: Analysis, cap: capture.Capture) -> None:
        s = self.settings
        input_screen = lambda x: x.state in (State.PASSWORD, State.USER_PASS, State.INCORRECT, State.CAD)  # noqa: E731
        a, cap = self._settled(w, session, s.settle_timeout)
        if a.state == State.INCORRECT:
            session.key(VK_RETURN)  # dismiss the message; the failure was already counted
            a, cap = self._settled(w, session, s.settle_timeout,
                                   lambda x: x.state in (State.PASSWORD, State.USER_PASS, State.CURTAIN))
        if a.state == State.CURTAIN:
            session.key(VK_SPACE)
            # Require a positive sign-in screen: a transient or misread frame must not count as "unlocked".
            a, cap = self._settled(w, session, s.settle_timeout, input_screen)
        ox, oy = cap.origin

        account = None
        if a.state == State.PASSWORD:
            account = match_account(a, host.accounts)
            if account is None:
                default = host.default_account
                tile = find_tile(a, default) if default else None
                if tile is not None:
                    log.info("[%s] selecting default account %s", w.key, default.user)
                    session.click(ox + tile.cx, oy + tile.cy)
                    session.wait(s.click_wait)
                    a, cap = self._settled(w, session, s.settle_timeout)
                    account = match_account(a, host.accounts) if a.state == State.PASSWORD else None
            if a.state == State.PASSWORD and account is None:
                self._pause_host(w.key, "lock screen user does not match any configured account")
                return
        elif a.state == State.USER_PASS:
            account = host.default_account
            if account is None:
                self._pause_host(w.key, "user name + password screen but no default account configured")
                return
        elif a.state == State.NOT_LOCKED:
            hs.attempts, hs.failures, hs.last_notice = 0, 0, ""
            return
        else:
            log.info("[%s] not acting on state %s", w.key, a.state.value)
            self._fail(w.key, hs, f"not acting on state {a.state.value}")
            return

        password = credstore.get_password(account.cred)
        if password is None:
            self._pause_host(w.key, f"no password stored for '{account.cred}'", account.cred)
            return

        hs.attempts += 1
        hs.failures = 0
        hs.next_action = time.monotonic() + s.cooldown
        log.info("[%s] attempt %d/%d as %s (%s)", w.key, hs.attempts, s.max_attempts, account.user, a.state.value)
        try:
            if a.state == State.USER_PASS:
                field = a.find_central(s.keywords["username"])
                if field is not None:
                    session.click(ox + field.cx, oy + field.cy)
                self._clear_field(session)
                session.text(account.user)
                session.key(VK_TAB)
            else:
                field = a.find_central([k for k in s.keywords["password"] if k in ("password", "密码")], max_len=12)
                if field is not None:
                    session.click(ox + field.cx, oy + field.cy)
            self._clear_field(session)
            session.text(password)
            session.key(VK_RETURN)
        except UnsupportedChar as e:
            self._pause_host(w.key, f"cannot type credentials for {account.user}: {e}", account.cred)
            return
        finally:
            password = None  # noqa: F841 - drop the reference as early as possible

        # Signing in can take a while; wait for a definite result instead of a fixed delay.
        a, _ = self._settled(w, None, s.verify_timeout,
                             lambda x: x.state in (State.NOT_LOCKED, State.INCORRECT, State.CURTAIN))
        if a.state == State.NOT_LOCKED:
            hs.attempts, hs.failures, hs.last_notice = 0, 0, ""
            log.info("[%s] unlocked as %s", w.key, account.user)
            self.notify_cb(f"VNC Unlocker: {w.key}", f"Unlocked as {account.user}")
        elif hs.attempts >= s.max_attempts:
            self._pause_host(w.key, f"unlock failed {hs.attempts} times as {account.user} (last state: {a.state.value})",
                             account.cred)
        else:
            log.warning("[%s] still locked after attempt %d (%s)", w.key, hs.attempts, a.state.value)

    @staticmethod
    def _clear_field(session: InputSession) -> None:
        session.key(ord("A"), ctrl=True)
        session.key(VK_BACK)
