"""The service settings file."""

import os
import pwd

import pytest

from ungem5.jobs import settings


def test_defaults_then_system_then_user(tmp_path):
    system = tmp_path / "config.toml"
    user = tmp_path / "user.toml"
    assert settings.load(system, user)["worker"]["port"] == 9100
    system.write_text(
        '[worker]\nport = 9360\nallow_gids = [1024023017]\n'
        '[scheduler]\nadmins = ["msamani", 1016235]\n'
        '[lab]\nsocket = "/run/x.sock"\n'
    )
    loaded = settings.load(system, user)
    assert loaded["worker"]["port"] == 9360
    assert loaded["worker"]["bind"] == "0.0.0.0"
    assert loaded["scheduler"]["admins"] == ["msamani", 1016235]
    user.write_text('[lab]\nsocket = "/tmp/mine.sock"\n')
    assert settings.load(system, user)["lab"]["socket"] == "/tmp/mine.sock"
    # NOTE: A user's file may not set the services' sections.
    user.write_text("[worker]\nport = 1\n")
    with pytest.raises(settings.SettingsError, match="not allowed"):
        settings.load(system, user)
    system.write_text("[worker]\nprot = 1\n")
    with pytest.raises(settings.SettingsError, match="unknown key"):
        settings.load(system, None)


def test_the_environment_names_the_system_file(tmp_path, monkeypatch):
    other = tmp_path / "other.toml"
    other.write_text("[worker]\nport = 7\n")
    monkeypatch.setenv("EXPERIMENT_CONFIG", str(other))
    assert settings.load()["worker"]["port"] == 7


def test_admins_may_be_uids():
    me = pwd.getpwuid(os.getuid())
    assert settings.user_names([me.pw_uid, "boss"]) == {me.pw_name, "boss"}
    with pytest.raises(settings.SettingsError, match="no user"):
        settings.user_names([2**31 - 7])
