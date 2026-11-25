import ctypes
import re

import pytest

import config
from finder import parse_title
from inputsim import UnsupportedChar, char_to_vk


@pytest.mark.parametrize("title,expected", [
    ("123456 (MYHOST01) - RealVNC Viewer", ("123456", "MYHOST01")),
    ("10.0.0.5 - RealVNC Viewer", ("10.0.0.5", None)),
    ("RealVNC Viewer", None),
    ("Notes - Notepad", None),
])
def test_parse_title(title, expected):
    assert parse_title(title) == expected


US_LAYOUT = (ctypes.windll.user32.GetKeyboardLayout(0) & 0xFFFF) in (0x0409, 0x0804)  # zh-CN uses US keys


@pytest.mark.skipif(not US_LAYOUT, reason="expects US keyboard layout")
def test_char_to_vk_us():
    assert char_to_vk("a") == (ord("A"), False)
    assert char_to_vk("A") == (ord("A"), True)
    assert char_to_vk("1") == (ord("1"), False)
    assert char_to_vk("!") == (ord("1"), True)
    assert char_to_vk("\\") == (0xDC, False)
    assert char_to_vk("_") == (0xBD, True)
    assert char_to_vk(".")[1] is False
    for ch in "~`@#$%^&*()+={}[]|:;\"'<>,?/":
        char_to_vk(ch)


def test_char_to_vk_unsupported():
    with pytest.raises(UnsupportedChar):
        char_to_vk("密")


def test_config_load(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "scan_interval: 3\n"
        "extra_keywords:\n  password: ['Mot de passe']\n"
        "hosts:\n"
        "  - match: 'MYHOST01'\n"
        "    accounts:\n"
        "      - user: 'DOMAIN\\jdoe'\n"
        "        default: true\n"
        "      - user: '.\\admin'\n"
        "        cred: 'h\\admin'\n",
        encoding="utf-8",
    )
    s = config.load(p)
    assert s.scan_interval == 3
    assert "motdepasse" in s.keywords["password"]
    host = s.host_for_title("123456 (MYHOST01) - RealVNC Viewer")
    assert host is not None and host.default_account.user == "DOMAIN\\jdoe"
    assert host.accounts[0].cred == "DOMAIN\\jdoe" and "jdoe" in host.accounts[0].display_names
    assert host.accounts[1].cred == "h\\admin" and "admin" in host.accounts[1].display_names
    assert s.host_for_title("other (X) - RealVNC Viewer") is None


def test_config_two_defaults_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "hosts:\n  - match: 'x'\n    accounts:\n"
        "      - {user: a, default: true}\n      - {user: b, default: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_local_config_is_valid(local_settings):
    """config.yaml (or config.example.yaml when absent): every host resolves and literal matches hit a host."""
    assert local_settings.hosts
    creds = dict(local_settings.all_creds())
    for host in local_settings.hosts:
        assert host.accounts
        assert len(host.accounts) > 1 or host.default_account is host.accounts[0]
        for acc in host.accounts:
            assert acc.cred in creds and acc.display_names
        for m in host.match.split(", "):
            if re.fullmatch(r"[\w.-]+", m):
                title = f"123456 ({m}) - RealVNC Viewer"
                assert parse_title(title) == ("123456", m)
                assert local_settings.host_for_title(title) is not None


def test_config_shared_accounts(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "accounts:\n"
        "  sys:\n    user: '.\\admin'\n"
        "  me:\n    user: 'DOMAIN\\jdoe'\n    display_names: ['Doe, John']\n"
        "hosts:\n"
        "  - match: ['MYHOST01', 'MYHOST02']\n"
        "    accounts: [sys, me]\n"
        "    default: me\n"
        "  - match: 'OTHER01'\n"
        "    accounts: [sys]\n",
        encoding="utf-8",
    )
    s = config.load(p)
    h1 = s.host_for_title("123456 (MYHOST01) - RealVNC Viewer")
    h2 = s.host_for_title("123457 (myhost02) - RealVNC Viewer")
    h3 = s.host_for_title("x (OTHER01) - RealVNC Viewer")
    assert h1 is h2 and h1.default_account.user == "DOMAIN\\jdoe"
    assert h3.default_account.user == ".\\admin"  # single account is the implicit default
    assert not h3.accounts[0].default and h1.accounts[0].default is False
    creds = dict(s.all_creds())
    assert set(creds) == {".\\admin", "DOMAIN\\jdoe"}  # one password per account, shared by hosts
    assert "OTHER01" in creds[".\\admin"] and "MYHOST02" in creds[".\\admin"]


@pytest.mark.parametrize("hosts,msg", [
    ("  - match: 'X'\n    accounts: [nope]\n", "not defined"),
    ("  - match: 'X'\n    accounts: [sys]\n    default: nope\n", "not defined"),
    ("  - match: '('\n    accounts: [sys]\n", "invalid regex"),
])
def test_config_shared_accounts_errors(tmp_path, hosts, msg):
    p = tmp_path / "c.yaml"
    p.write_text("accounts:\n  sys:\n    user: 'u'\nhosts:\n" + hosts, encoding="utf-8")
    with pytest.raises(config.ConfigError, match=msg):
        config.load(p)


def test_own_injected_input_does_not_reset_idle(monkeypatch):
    import unlocker

    clock = {"t": 1000.0}
    monkeypatch.setattr(unlocker.time, "monotonic", lambda: clock["t"])
    tick = {"v": 5}
    monkeypatch.setattr(unlocker, "last_input_tick", lambda: tick["v"])
    monkeypatch.setattr(unlocker, "idle_seconds", lambda: 0.0)  # system idle was reset by our SendInput
    u = unlocker.Unlocker(config.Settings())
    u._idle_origin = 960.0
    u._own_tick = 5
    assert u._user_idle() == 40.0  # still counts from the last real user input
    tick["v"] = 6  # real user input happened afterwards
    assert u._user_idle() == 0.0 and u._own_tick is None


def test_unknown_screen_has_no_input_and_warns_once_until_recognized(monkeypatch, caplog):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import unlocker

    now = 100.0
    monkeypatch.setattr(unlocker.time, "monotonic", lambda: now)
    input_session = Mock()
    monkeypatch.setattr(unlocker, "InputSession", input_session)
    settings = config.Settings(hosts=[config.HostConfig("H", re.compile("H"), [])])
    notify = Mock()
    u = unlocker.Unlocker(settings, notify)
    w = unlocker.VncWindow(1, "H - RealVNC Viewer", "H", None, False)
    a = unlocker.Analysis(unlocker.State.UNKNOWN, [], 100, 100, "no text")
    monkeypatch.setattr(u, "_analyze", lambda w: (a, SimpleNamespace(method="test")))
    hs = u._state(w.key)
    hs.attempts = 1

    for now in (100, 129):
        u._scan_window(w)
    notify.assert_not_called()
    for now in (130, 180):
        u._scan_window(w)
    notify.assert_called_once()
    assert "still scanning without sending input" in caplog.text
    assert hs.attempts == 1

    a.state, a.reason = unlocker.State.NOT_LOCKED, "taskbar clock"
    u._scan_window(w)
    assert hs.attempts == 0
    a.state, a.reason = unlocker.State.UNKNOWN, "no text"
    for now in (181, 211):
        u._scan_window(w)
    assert notify.call_count == 2
    input_session.assert_not_called()


@pytest.mark.parametrize("prior_attempts", [0, 1])
def test_unknown_after_submit_does_not_report_success(monkeypatch, caplog, prior_attempts):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import unlocker

    account = config.Account("Test User", ["Test User"], "cred")
    host = config.HostConfig("H", re.compile("H"), [account])
    notify = Mock()
    u = unlocker.Unlocker(config.Settings(max_attempts=2), notify)
    hs = u._state("H")
    hs.attempts = prior_attempts
    w = unlocker.VncWindow(1, "H - RealVNC Viewer", "H", None, False)
    cap = SimpleNamespace(origin=(0, 0))
    prompt = unlocker.Analysis(unlocker.State.PASSWORD, [unlocker.detector.Line("Test User", 40, 30, 20, 10)],
                               100, 100)
    unknown = unlocker.Analysis(unlocker.State.UNKNOWN, [], 100, 100, "no text")
    verify_accepted = []

    def settled(w, session, timeout, until):
        if session is not None:
            return prompt, cap
        verify_accepted.append(until(unknown))
        return unknown, cap

    monkeypatch.setattr(u, "_settled", settled)
    monkeypatch.setattr(unlocker.credstore, "get_password", lambda _: "test-only")
    u._act(w, host, hs, Mock(), prompt, cap)
    assert verify_accepted == [False]
    assert hs.attempts == prior_attempts + 1
    assert "could not verify unlock" in caplog.text
    assert hs.paused == (prior_attempts == 1)
    assert all("Unlocked as" not in call.args[1] for call in notify.call_args_list)


def test_settle_timeout_does_not_return_an_older_password_frame(monkeypatch):
    from PIL import Image

    import unlocker

    w = unlocker.VncWindow(1, "H - RealVNC Viewer", "H", None, False)
    still, changed = (unlocker.capture.Capture(Image.new("RGB", (10, 10), color), "test", (0, 0))
                      for color in ("black", "white"))
    captures = iter((still, still, changed))
    clock = iter((0, 0, 1, 2))
    monkeypatch.setattr(unlocker.capture, "capture", lambda _: next(captures))
    monkeypatch.setattr(unlocker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(unlocker.time, "sleep", lambda _: None)
    settings = config.Settings()
    monkeypatch.setattr(settings, "analyze", lambda im: unlocker.Analysis(unlocker.State.PASSWORD, [], *im.size))

    a, cap = unlocker.Unlocker(settings)._settled(w, None, 2, lambda a: a.state == unlocker.State.NOT_LOCKED)
    assert a.state == unlocker.State.UNKNOWN and cap is changed


def test_paused_host_resumes_when_password_changes(monkeypatch):
    import unlocker

    store = {"cred": "old"}
    monkeypatch.setattr(unlocker.credstore, "get_password", lambda c: store.get(c))
    u = unlocker.Unlocker(config.Settings())
    u._pause_host("H", "failed", "cred")
    hs = u.hosts["H"]
    u._resume_if_password_changed("H", hs)
    assert hs.paused  # unchanged password keeps the host paused
    store["cred"] = "new"
    u._resume_if_password_changed("H", hs)
    assert not hs.paused and hs.attempts == 0


def test_failures_pause_host_and_reset(monkeypatch):
    import unlocker

    u = unlocker.Unlocker(config.Settings(max_failures=3))
    hs = u._state("H")
    for _ in range(2):
        u._fail("H", hs, "could not focus target window")
    assert not hs.paused and hs.failures == 2 and hs.next_action > 0
    u._fail("H", hs, "could not focus target window")
    assert hs.paused  # no endless retries
    u.reset_hosts()
    assert not hs.paused and hs.failures == 0


def test_workstation_locked_when_lockapp_in_front(monkeypatch):
    import unlocker

    monkeypatch.setattr(unlocker, "_session_flag_locked", lambda: False)
    monkeypatch.setattr(unlocker, "foreground_process_name", lambda: "lockapp.exe")
    assert unlocker.is_workstation_locked()  # lock screen curtain is still on the 'Default' desktop
    monkeypatch.setattr(unlocker, "foreground_process_name", lambda: "vncviewer.exe")
    assert not unlocker.is_workstation_locked()
    monkeypatch.setattr(unlocker, "_session_flag_locked", lambda: True)
    assert unlocker.is_workstation_locked()


def test_workstation_lock_query_real():
    import unlocker

    assert unlocker._session_flag_locked() is False  # tests run in an unlocked interactive session
