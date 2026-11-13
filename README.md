# VNC Unlocker

A Windows tray app that automatically unlocks remote Windows lock screens shown in
**RealVNC Viewer** windows. It watches every VNC session window, recognises the remote
lock / sign-in screen with Windows OCR, and types the configured account's password for you —
only while you are away from the keyboard.

## Features

- Detects the remote screen state via OCR (English and Chinese out of the box):
  lock-screen curtain, password prompt, "Other user" / user name + password, wrong password,
  Ctrl+Alt+Del prompt, or normal desktop.
- Works with covered or background VNC windows (captures the window content directly).
- Maps window titles to hosts with regular expressions; accounts are defined once and shared by
  multiple hosts. Picks the right account from the user tile shown on the lock screen and can select
  the default account from the user list after a reboot.
- Passwords are stored in **Windows Credential Manager** (via `keyring`), never in files or logs.
- Safety first:
  - acts only after the local user has been idle for `idle_threshold` seconds, and aborts
    immediately on any real keyboard / mouse input;
  - never types while the local workstation is locked or Caps Lock is on;
  - pauses a host after `max_attempts` wrong passwords (protects against AD account lockout) or after
    `max_failures` runs that could not type at all; changing the password or unlocking by hand resumes it.
- Tray menu: pause, scan now, reset hosts, password manager, open config, open log.
- `config.yaml` is reloaded automatically when it changes.

## Requirements

- Windows 10 / 11 with the OCR language packs you want to use (e.g. `en-US`, `zh-Hans-CN`)
- Python 3.10+
- RealVNC Viewer

## Installation

```powershell
git clone https://github.com/MiaoXiaodongIntel/vnc_unlocker.git
cd vnc_unlocker
py -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

## Configuration

Edit `config.yaml` (it is git-ignored, so your hosts and accounts stay local).
See the comments in [config.example.yaml](config.example.yaml) for every option.

```yaml
accounts:
  local_admin:
    user: '.\local_admin'          # local account; domain accounts use 'DOMAIN\user'
    display_names: ['local_admin'] # names shown on the lock screen user tile

hosts:
  - match: ['MYHOST01', 'MYHOST02']  # case-insensitive regex(es) searched in the window title,
    accounts: [local_admin]          # e.g. "123456 (MYHOST01) - RealVNC Viewer"
    default: local_admin             # account used on "Other user" / user list screens
```

Then store a password for every account (the credential name is the account's `cred`,
which defaults to `user`):

```powershell
.venv\Scripts\pythonw.exe main.py passwords            # password manager window
.venv\Scripts\python.exe  main.py set-password ".\local_admin"
.venv\Scripts\python.exe  main.py list-creds           # check which passwords are stored
```

## Usage

```powershell
.venv\Scripts\pythonw.exe main.py                 # tray app (default)
.venv\Scripts\python.exe  main.py run --console   # run in the console without a tray icon
.venv\Scripts\python.exe  main.py diag            # capture + OCR every VNC window once (output in tmp\diag)
.venv\Scripts\python.exe  main.py shortcuts       # create Desktop shortcuts for the app and password window
```

Logs are written to `logs\vnc_unlocker.log`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

Account- and host-related tests read their data from your local `config.yaml`
(falling back to `config.example.yaml`), so no test changes are needed after editing the config.
`tests/samples/<state>__<description>.png` are real screenshots classified with Windows OCR;
drop in new screenshots with the same naming scheme to extend coverage.

## Project layout

| File | Purpose |
|---|---|
| `main.py` | entry point: tray app, CLI commands |
| `unlocker.py` | per-host unlock state machine |
| `detector.py` | Windows OCR and lock-screen classification |
| `finder.py` | enumerate RealVNC Viewer session windows |
| `capture.py` | capture window content even when covered |
| `inputsim.py` | keyboard / mouse injection with user-interruption detection |
| `credstore.py` | password storage in Windows Credential Manager |
| `password_gui.py` | password manager window |
| `config.py` | load and validate `config.yaml` |
