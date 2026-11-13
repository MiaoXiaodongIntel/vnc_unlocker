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
