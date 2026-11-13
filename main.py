"""VNC Unlocker: tray app that auto-unlocks remote Windows lock screens shown in RealVNC Viewer.

Usage (always with the venv interpreter):
  .venv\\Scripts\\pythonw.exe main.py                 tray app (default)
  .venv\\Scripts\\python.exe  main.py run --console   run in the console without a tray icon
  .venv\\Scripts\\python.exe  main.py diag [--out DIR] capture + OCR every VNC window once
  .venv\\Scripts\\pythonw.exe main.py passwords       password manager window
  .venv\\Scripts\\python.exe  main.py shortcuts       create Desktop shortcuts
  .venv\\Scripts\\python.exe  main.py set-password CRED
  .venv\\Scripts\\python.exe  main.py list-creds
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
import credstore

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "vnc_unlocker.log"
log = logging.getLogger("vnc_unlocker")


def set_dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def setup_logging(console: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console and sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


class Worker(threading.Thread):
    def __init__(self, notify):
        super().__init__(daemon=True)
        from unlocker import Unlocker

        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self._mtime = 0.0
        self.unlocker = Unlocker(self._load(), notify)

    def _load(self) -> config.Settings:
        self._mtime = config.CONFIG_PATH.stat().st_mtime
        return config.load()

    def _maybe_reload(self) -> None:
        try:
            if config.CONFIG_PATH.stat().st_mtime != self._mtime:
                self.unlocker.settings = self._load()
                log.info("config reloaded")
        except Exception as e:
            log.error("config reload failed, keeping previous config: %s", e)

    def run(self) -> None:
        log.info("worker started")
        while not self.stop_event.is_set():
            self._maybe_reload()
            try:
                self.unlocker.scan()
            except Exception:
                log.exception("scan failed")
            self.wake_event.wait(self.unlocker.settings.scan_interval)
            self.wake_event.clear()
        log.info("worker stopped")

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()


MUTEX_NAME = "Local\\vnc_unlocker_worker"


def acquire_single_instance():
    """Return a mutex handle, or None if another instance is already running."""
    import win32api
    import win32event
    import winerror

    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return handle


def make_icon_image(paused: bool):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (150, 150, 150) if paused else (0, 120, 215)
    d.rounded_rectangle((8, 28, 56, 60), 6, fill=color)
    d.arc((18, 6, 46, 44), 180, 360, fill=color, width=6)
    d.rectangle((29, 38, 35, 50), fill=(255, 255, 255))
    return img


def run_tray() -> None:
    import pystray

    icon = pystray.Icon("vnc_unlocker", make_icon_image(False), "VNC Unlocker")

    def notify(title: str, msg: str) -> None:
        try:
            icon.notify(msg, title)
        except Exception:
            log.exception("notify failed")

    worker = Worker(notify)

    def toggle_pause(_icon, _item):
        worker.unlocker.paused = not worker.unlocker.paused
        icon.icon = make_icon_image(worker.unlocker.paused)
        log.info("paused" if worker.unlocker.paused else "resumed")

    def scan_now(_icon, _item):
        worker.wake_event.set()

    def reset_hosts(_icon, _item):
        worker.unlocker.reset_hosts()
        worker.wake_event.set()

    def set_password(_icon, _item):
        exe = Path(sys.executable)
        gui_exe = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
        subprocess.Popen([str(gui_exe), str(BASE_DIR / "main.py"), "passwords"], cwd=BASE_DIR)

    def open_log(_icon, _item):
        os.startfile(LOG_FILE)

    def open_config(_icon, _item):
        os.startfile(config.CONFIG_PATH)

    def quit_app(_icon, _item):
        worker.stop()
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem("Paused", toggle_pause, checked=lambda _i: worker.unlocker.paused),
        pystray.MenuItem("Scan now", scan_now, default=True),
        pystray.MenuItem("Reset hosts", reset_hosts),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Passwords...", set_password),
        pystray.MenuItem("Open config", open_config),
        pystray.MenuItem("Open log", open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", quit_app),
    )
    worker.start()
    icon.run()


def run_console() -> None:
    worker = Worker(lambda title, msg: log.warning("NOTIFY %s: %s", title, msg))
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        worker.stop()


def cmd_diag(out_dir: Path) -> None:
    import capture
    import detector
    from finder import find_windows
    from unlocker import is_workstation_locked, match_account

    s = config.load()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"local workstation locked: {is_workstation_locked()}")
    windows = find_windows()
    if not windows:
        print("no RealVNC Viewer session windows found")
    for w in windows:
        print(f"\n=== {w.title!r} hwnd={w.hwnd} key={w.key} minimized={w.minimized}")
        host = s.host_for_title(w.title)
        print(f"config host: {host.match if host else None}")
        cap = capture.capture(w.hwnd)
        if cap is None:
            print("capture: unavailable (minimized, or PrintWindow black and window covered)")
            continue
        safe = "".join(c if c.isalnum() else "_" for c in w.key)
        path = out_dir / f"{safe}.png"
        cap.image.save(path)
        print(f"capture: {cap.method} {cap.image.size} black={capture.is_mostly_black(cap.image)} -> {path}")
        lines = detector.ocr_lines(cap.image, s.ocr_languages, s.ocr_min_width)
        a = detector.classify(lines, cap.image.width, cap.image.height, s.keywords, s.max_lines, s.user_names)
        print(f"state: {a.state.value} ({a.reason})")
        if host:
            if a.state in (detector.State.PASSWORD, detector.State.INCORRECT):
                acc = match_account(a, host.accounts)
                print(f"matched account: {acc.user if acc else 'NONE - check display_names'}")
            elif a.state == detector.State.USER_PASS:
                acc = host.default_account
                print(f"default account: {acc.user if acc else 'NONE - set default: true'}")
        for l in lines:
            print(f"  [{l.x:6.0f},{l.y:6.0f} {l.w:5.0f}x{l.h:4.0f}] {l.text}")


def cmd_set_password(cred: str) -> int:
    pw = getpass.getpass(f"Password for '{cred}': ")
    if not pw or pw != getpass.getpass("Confirm: "):
        print("passwords empty or do not match; nothing saved")
        return 1
    credstore.set_password(cred, pw)
    print(f"saved '{cred}' to Windows Credential Manager (service '{credstore.SERVICE}')")
    return 0


def cmd_passwords() -> None:
    import password_gui

    password_gui.run()


def cmd_shortcuts() -> None:
    """Create Desktop shortcuts for the tray app and the password window."""
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    desktop = Path(shell.SpecialFolders("Desktop"))
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    for name, args in (("VNC Unlocker", ""), ("VNC Unlocker Passwords", " passwords")):
        lnk = shell.CreateShortcut(str(desktop / f"{name}.lnk"))
        lnk.TargetPath = str(pythonw)
        lnk.Arguments = f'"{BASE_DIR / "main.py"}"{args}'
        lnk.WorkingDirectory = str(BASE_DIR)
        lnk.Description = name
        lnk.Save()
        print(f"created {desktop / (name + '.lnk')}")


def cmd_list_creds() -> None:
    for cred, desc in config.load().all_creds():
        print(f"{'stored ' if credstore.has_password(cred) else 'MISSING'}  {cred}   ({desc})")


def main() -> int:
    p = argparse.ArgumentParser(description="Auto-unlock remote Windows lock screens in RealVNC Viewer")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run the tray app (default)")
    r.add_argument("--console", action="store_true", help="no tray icon, log to console")
    d = sub.add_parser("diag", help="capture + OCR every VNC window once")
    d.add_argument("--out", type=Path, default=BASE_DIR / "tmp" / "diag")
    sp = sub.add_parser("set-password", help="store a password in Windows Credential Manager")
    sp.add_argument("cred")
    sub.add_parser("passwords", help="open the password manager window")
    sub.add_parser("shortcuts", help="create Desktop shortcuts")
    sub.add_parser("list-creds", help="list configured credentials and whether they are stored")
    args = p.parse_args()

    set_dpi_aware()
    if args.cmd == "diag":
        cmd_diag(args.out)
    elif args.cmd == "set-password":
        return cmd_set_password(args.cred)
    elif args.cmd == "passwords":
        cmd_passwords()
    elif args.cmd == "shortcuts":
        cmd_shortcuts()
    elif args.cmd == "list-creds":
        cmd_list_creds()
    else:
        console = args.cmd == "run" and args.console
        setup_logging(console)
        mutex = acquire_single_instance()
        if mutex is None:
            log.warning("another instance is already running; exiting")
            if not console:
                ctypes.windll.user32.MessageBoxW(
                    None, "VNC Unlocker is already running (see the tray icon).", "VNC Unlocker", 0x40)
            return 1
        try:
            config.load()
        except Exception as e:
            log.error("invalid config: %s", e)
            if console:
                raise
            ctypes.windll.user32.MessageBoxW(None, f"Invalid config.yaml:\n{e}", "VNC Unlocker", 0x10)
            return 1
        run_console() if console else run_tray()
    return 0


if __name__ == "__main__":
    sys.exit(main())
