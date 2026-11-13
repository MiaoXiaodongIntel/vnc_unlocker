"""Shared test data taken from the local config.yaml (or config.example.yaml on a fresh clone),
so tests follow whatever accounts/hosts are configured and never hard-code real names."""
from __future__ import annotations

from pathlib import Path

import pytest

import config

EXAMPLE_PATH = config.BASE_DIR / "config.example.yaml"
LOCAL_PATH: Path = config.CONFIG_PATH if config.CONFIG_PATH.exists() else EXAMPLE_PATH
LOCAL_SETTINGS = config.load(LOCAL_PATH)

# One entry per distinct account (hosts may share accounts), defaults cleared.
ACCOUNTS: list[config.Account] = list({
    acc.user: config.Account(acc.user, list(acc.display_names), acc.cred)
    for host in LOCAL_SETTINGS.hosts for acc in host.accounts
}.values())

# Synthetic second account used as the "someone else" counterpart in account tests.
OTHER_ACCOUNT = config.Account(".\\zz_test_other", ["Zz Test Other"], ".\\zz_test_other")


@pytest.fixture(scope="session")
def local_settings() -> config.Settings:
    return LOCAL_SETTINGS


def pytest_generate_tests(metafunc):
    if "account" in metafunc.fixturenames:
        metafunc.parametrize("account", ACCOUNTS, ids=[a.user for a in ACCOUNTS])
