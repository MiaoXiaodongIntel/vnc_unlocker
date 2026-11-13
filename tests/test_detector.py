import pytest
from conftest import OTHER_ACCOUNT

from config import DEFAULT_KEYWORDS
from detector import Analysis, Line, State, classify, normalize
from unlocker import find_tile, match_account

W, H = 1920, 1080
KW = {k: [normalize(x) for x in v] for k, v in DEFAULT_KEYWORDS.items()}


def run(*lines):
    return classify(list(lines), W, H, KW)


def L(text, x, y, w=200, h=30):
    return Line(text, x, y, w, h)


def test_normalize():
    assert normalize("Sign-in  options") == "signinoptions"
    assert normalize("1 2：45") == "12:45"
    assert normalize("密 码") == "密码"


@pytest.mark.parametrize("lines", [
    # Win10 English, big clock bottom-left
    [L("10:15", 100, 700, 300, 120), L("Thursday, November 12", 100, 830, 400, 40)],
    # Win10 Chinese
    [L("10:15", 100, 700, 300, 120), L("11月12日 星期四", 100, 830, 300, 40)],
    # Win11 with widgets, clock misread by OCR, lots of other text
    [L("9:iQ", 870, 110, 220, 140), L("Thursday, November 12", 840, 280, 270, 25)]
    + [L(f"news item {i}", 1200, 300 + 20 * i, 300, 15) for i in range(40)],
])
def test_curtain(lines):
    assert run(*lines).state == State.CURTAIN


def test_date_without_big_clock_is_not_curtain():
    assert run(L("December 14, 2023", 100, 480), L("everywhere", 100, 350, 500, 90)).state == State.NOT_LOCKED


@pytest.mark.parametrize("rest", [
    [L("Password", 860, 580), L("Sign-in options", 880, 640)],
    [L("密码", 860, 580), L("登录选项", 880, 640)],
    [L("I forgot my password", 860, 640)],
])
def test_password(account, rest):
    for name in account.display_names:
        assert run(L(name, 860, 480, 200, 45), *rest).state == State.PASSWORD


@pytest.mark.parametrize("lines", [
    [L("Other user", 860, 450), L("User name", 860, 540), L("Password", 860, 590)],
    [L("其他用户", 860, 450), L("用户名", 860, 540), L("密码", 860, 590)],
])
def test_user_pass(lines):
    assert run(*lines).state == State.USER_PASS


@pytest.mark.parametrize("text", ["The password is incorrect. Try again.", "密码不正确。请再试一次。"])
def test_incorrect(text):
    assert run(L(text, 760, 500, 400), L("OK", 920, 600, 80)).state == State.INCORRECT


def test_cad():
    assert run(L("Press Ctrl+Alt+Delete to unlock.", 760, 600, 400)).state == State.CAD


def test_desktop_with_taskbar_clock_is_not_locked():
    a = run(L("Password", 860, 580), L("9:10", 1800, 1040, 40, 15))
    assert a.state == State.NOT_LOCKED and "taskbar" in a.reason


def test_busy_screen_with_password_word_is_not_locked():
    lines = [L("Password", 860, 580)] + [L(f"text line {i}", 100, 20 * i, 300, 15) for i in range(40)]
    assert run(*lines).state == State.NOT_LOCKED


def test_password_word_off_centre_is_not_locked():
    assert run(L("Password", 50, 580)).state == State.NOT_LOCKED


def centred(text, y, w=80, h=25):
    return L(text, W // 2 - w // 2, y, w, h)


def test_user_tile_name_detects_password_screen(account):
    name = account.display_names[0]
    lines = [centred(name, 480), L("has swo rd", 860, 560, 60, 18)]
    assert classify(lines, W, H, KW, user_names=[name]).state == State.PASSWORD
    assert classify(lines, W, H, KW).state == State.NOT_LOCKED  # name unknown -> no evidence
    off = [L(name, 100, 480, 80, 25)]
    assert classify(off, W, H, KW, user_names=[name]).state == State.NOT_LOCKED
    longer = [centred(f"{name} settings", 480, 160)]
    assert classify(longer, W, H, KW, user_names=[name]).state == State.NOT_LOCKED


def test_forgot_link_detects_password_screen(account):
    lines = [centred(account.display_names[0], 480), L("Forg ot your BSsword?", 890, 600, 128, 18)]
    assert run(*lines).state == State.PASSWORD


def analysis(*lines):
    return Analysis(State.PASSWORD, list(lines), W, H)


def test_match_account_central_only(account):
    accounts = [account, OTHER_ACCOUNT]
    a = analysis(L(account.display_names[0], 860, 480), L(OTHER_ACCOUNT.display_names[0], 30, 900))
    assert match_account(a, accounts) is account  # list entry bottom-left ignored
    a = analysis(L(OTHER_ACCOUNT.display_names[0], 860, 480), L(account.display_names[0], 30, 900))
    assert match_account(a, accounts) is OTHER_ACCOUNT


def test_match_account_display_name(account):
    for name in account.display_names:
        assert match_account(analysis(L(name, 860, 480)), [OTHER_ACCOUNT, account]) is account


def test_match_account_none(account):
    assert match_account(analysis(L("Other user", 860, 480)), [account, OTHER_ACCOUNT]) is None


def test_find_tile_bottom_left(account):
    name = account.display_names[0]
    a = analysis(L("Other user", 860, 480), L(OTHER_ACCOUNT.display_names[0], 60, 900), L(name, 60, 960))
    tile = find_tile(a, account)
    assert tile is not None and tile.text == name
    assert find_tile(analysis(L(name, 860, 480)), account) is None


def test_frames_equal_tolerates_caret_only():
    from PIL import Image, ImageDraw

    from unlocker import frames_equal

    base = Image.new("RGB", (800, 600), (20, 40, 90))
    caret = base.copy()
    ImageDraw.Draw(caret).rectangle((400, 300, 401, 320), fill="white")
    moved = base.copy()
    ImageDraw.Draw(moved).rectangle((200, 200, 600, 400), fill="white")
    assert frames_equal(base, base.copy())
    assert frames_equal(base, caret)
    assert not frames_equal(base, moved)
    assert not frames_equal(base, base.resize((400, 300)))
