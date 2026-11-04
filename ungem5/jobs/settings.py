"""Service settings: ports, who may run jobs, who administers, sockets.

They live in /etc/ungem5/config.toml, which root owns, so a worker run
by root can trust them; command-line flags override them. A user's own
~/.config/ungem5/config.toml may set the [lab] section only, which says
where `helper console --lab` finds the lab scheduler. $UNGEM5_CONFIG
names another system file (and skips the user's), for tests.

    [worker]
    port = 9100
    bind = "0.0.0.0"
    allow_gids = [1024023017]   # run by root: who may run jobs
    min_uid = 1000
    store = "/var/lib/ungem5"

    [scheduler]                 # the lab scheduler
    name = "lab"
    socket = "/run/ungem5/scheduler.sock"
    dashboard_port = 9200
    allow_gids = [1024023017]
    admins = ["msamani"]        # names, or numeric uids

    [lab]
    socket = "/run/ungem5/scheduler.sock"
"""

import os
import pwd
import tomllib

from pathlib import Path

SYSTEM_FILE = Path("/etc/ungem5/config.toml")
USER_FILE = Path("~/.config/ungem5/config.toml")
# NOTE: What a user's file may set.
USER_SECTIONS = ("lab",)

DEFAULTS: dict = {
    "worker": {
        "port": 9100,
        "bind": "0.0.0.0",
        "allow_gids": [],
        "min_uid": 1000,
        "store": "/var/lib/ungem5",
    },
    "scheduler": {
        "name": "lab",
        "socket": "/run/ungem5/scheduler.sock",
        "dashboard_port": 9200,
        "allow_gids": [],
        "admins": [],
    },
    "lab": {"socket": "/run/ungem5/scheduler.sock"},
}


class SettingsError(Exception):
    pass


def _read(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{path}: {e}") from None


def load(system: Path | None = None, user: Path | None = None) -> dict:
    """The settings: the defaults, under the system file (SYSTEM_FILE, or
    $UNGEM5_CONFIG), under the user's file (USER_FILE; its
    USER_SECTIONS only). Unknown keys are refused, so a typo can't
    silently leave a default in place."""
    if system is None:
        system = Path(os.environ.get("UNGEM5_CONFIG") or SYSTEM_FILE)
    if user is None and "UNGEM5_CONFIG" not in os.environ:
        user = USER_FILE
    settings = {section: dict(values) for section, values in DEFAULTS.items()}
    sources = [(system, DEFAULTS.keys())]
    if user is not None:
        sources.append((user.expanduser(), USER_SECTIONS))
    for path, allowed in sources:
        for section, values in _read(path).items():
            if section not in allowed:
                raise SettingsError(
                    f"{path}: section [{section}] is not allowed here"
                )
            for key, value in values.items():
                if key not in settings[section]:
                    raise SettingsError(
                        f"{path}: unknown key {key!r} in [{section}]"
                    )
                settings[section][key] = value
    return settings


def user_names(admins: list) -> frozenset[str]:
    """`admins` as user names: numeric uids are looked up."""
    names = set()
    for admin in admins:
        if isinstance(admin, int):
            try:
                names.add(pwd.getpwuid(admin).pw_name)
            except KeyError:
                raise SettingsError(f"no user with uid {admin}") from None
        else:
            names.add(str(admin))
    return frozenset(names)


def is_root() -> bool:
    return os.geteuid() == 0
