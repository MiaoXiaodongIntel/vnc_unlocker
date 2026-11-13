"""Password storage in Windows Credential Manager via keyring. Passwords never touch disk or logs."""
from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "vnc_unlocker"


def get_password(cred: str) -> str | None:
    return keyring.get_password(SERVICE, cred)


def set_password(cred: str, password: str) -> None:
    keyring.set_password(SERVICE, cred, password)


def delete_password(cred: str) -> bool:
    try:
        keyring.delete_password(SERVICE, cred)
        return True
    except PasswordDeleteError:
        return False


def has_password(cred: str) -> bool:
    return get_password(cred) is not None
