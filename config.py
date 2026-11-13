"""Load and validate config.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

# Matched against OCR text after normalize() (NFKC, lowercase, whitespace removed).
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "password": ["password", "密码", "signinoptions", "登录选项", "iforgotmypassword", "我忘记了密码",
                 "forgotyour", "忘记密码"],
    "username": ["username", "用户名"],
    "other_user": ["otheruser", "其他用户"],
    "incorrect": ["incorrect", "不正确", "密码错误"],
    "cad": ["ctrl+alt+del", "ctrl+alt+delete"],
}


@dataclass
class Account:
    user: str
    display_names: list[str]
    cred: str
    default: bool = False


@dataclass
class HostConfig:
    match: str
    pattern: re.Pattern
    accounts: list[Account]

    @property
    def default_account(self) -> Account | None:
        for acc in self.accounts:
            if acc.default:
                return acc
        return self.accounts[0] if len(self.accounts) == 1 else None


@dataclass
class Settings:
    scan_interval: float = 5.0
    idle_threshold: float = 30.0
    cooldown: float = 60.0
    max_attempts: int = 2
    max_failures: int = 3
    settle_timeout: float = 15.0
    click_wait: float = 1.5
    verify_timeout: float = 20.0
    key_delay: float = 0.03
    ocr_languages: list[str] = field(default_factory=lambda: ["zh-Hans-CN", "en-US"])
    ocr_min_width: int = 1600
    max_lines: int = 30
    keywords: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_KEYWORDS.items()})
    hosts: list[HostConfig] = field(default_factory=list)

    def host_for_title(self, title: str) -> HostConfig | None:
        for host in self.hosts:
            if host.pattern.search(title):
                return host
        return None

    @property
    def user_names(self) -> list[str]:
        """Every configured display name; used to recognise the user tile on lock screens."""
        return sorted({n for h in self.hosts for acc in h.accounts for n in acc.display_names})

    def analyze(self, img) -> "Analysis":
        from detector import analyze

        return analyze(img, self.ocr_languages, self.keywords, self.ocr_min_width, self.max_lines, self.user_names)

    def all_creds(self) -> list[tuple[str, str]]:
        """Unique (cred, "user @ host, ...") pairs across all hosts."""
        seen: dict[str, list[str]] = {}
        for host in self.hosts:
            for acc in host.accounts:
                seen.setdefault(acc.cred, []).append(f"{acc.user} @ {host.match}")
        return [(cred, ", ".join(uses)) for cred, uses in seen.items()]


class ConfigError(ValueError):
    pass


def _norm_kw(s: str) -> str:
    from detector import normalize

    return normalize(s)


def load(path: Path = CONFIG_PATH) -> Settings:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("top level must be a mapping (key: value)")
    s = Settings()
    for name in (
        "scan_interval", "idle_threshold", "cooldown", "settle_timeout", "click_wait",
        "verify_timeout", "key_delay",
    ):
        if name in data:
            setattr(s, name, float(data[name]))
    for name in ("max_attempts", "max_failures", "ocr_min_width", "max_lines"):
        if name in data:
            setattr(s, name, int(data[name]))
    if data.get("ocr_languages"):
        s.ocr_languages = [str(x) for x in data["ocr_languages"]]

    for group, extra in (data.get("extra_keywords") or {}).items():
        if group not in s.keywords:
            raise ConfigError(f"extra_keywords: unknown group '{group}', expected one of {list(s.keywords)}")
        s.keywords[group].extend(str(x) for x in extra)
    s.keywords = {k: sorted({_norm_kw(x) for x in v if _norm_kw(x)}) for k, v in s.keywords.items()}

    shared: dict[str, Account] = {}
    raw_accounts = data.get("accounts") or {}
    if not isinstance(raw_accounts, dict):
        raise ConfigError("accounts: expected a mapping of name -> {user, display_names, cred}")
    for name, a in raw_accounts.items():
        shared[str(name)] = _parse_account(a, f"accounts.{name}")

    for i, h in enumerate(data.get("hosts") or []):
        if not isinstance(h, dict):
            raise ConfigError(f"hosts[{i}]: expected 'match:' and 'accounts:' keys, got {h!r} (check indentation)")
        raw_match = h.get("match")
        matches = [str(x).strip() for x in (raw_match if isinstance(raw_match, list) else [raw_match]) if x]
        matches = [m for m in matches if m]
        if not matches:
            raise ConfigError(f"hosts[{i}]: 'match' is required")
        for m in matches:
            try:
                re.compile(m)
            except re.error as e:
                raise ConfigError(f"hosts[{i}]: invalid regex '{m}': {e}") from e
        match = ", ".join(matches)
        pattern = re.compile("|".join(f"(?:{m})" for m in matches), re.IGNORECASE)
        default_name = str(h.get("default") or "").strip()
        if default_name and default_name not in shared:
            raise ConfigError(f"hosts[{i}] ({match}): default '{default_name}' is not defined under top-level accounts")
        accounts = []
        for j, a in enumerate(h.get("accounts") or []):
            where = f"hosts[{i}].accounts[{j}]"
            if isinstance(a, str):
                if a not in shared:
                    raise ConfigError(f"{where}: account '{a}' is not defined under top-level accounts "
                                      f"(defined: {', '.join(shared) or 'none'})")
                accounts.append(replace(shared[a], default=(a == default_name)))
            else:
                acc = _parse_account(a, where)
                acc.default = bool(a.get("default", False))
                accounts.append(acc)
        if not accounts:
            raise ConfigError(f"hosts[{i}] ({match}): at least one account is required")
        if default_name and not any(acc.default for acc in accounts):
            raise ConfigError(f"hosts[{i}] ({match}): default '{default_name}' is not in this host's accounts")
        if sum(a.default for a in accounts) > 1:
            raise ConfigError(f"hosts[{i}] ({match}): only one account may be default")
        s.hosts.append(HostConfig(match=match, pattern=pattern, accounts=accounts))
    return s


def _parse_account(a, where: str) -> Account:
    if not isinstance(a, dict):
        raise ConfigError(f"{where}: expected 'user:' etc., got {a!r} (check indentation)")
    user = str(a.get("user") or "").strip()
    if not user:
        raise ConfigError(f"{where}: 'user' is required")
    names = [str(x) for x in (a.get("display_names") or [])]
    short = user.split("\\")[-1].split("@")[0]
    if short not in names:
        names.append(short)
    return Account(user=user, display_names=names, cred=str(a.get("cred") or user))
