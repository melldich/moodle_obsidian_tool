from __future__ import annotations

from pathlib import Path

import keyring
import pytest

from moodle2obsidian.auth import store

KEY = "moodle_token@moodle.test"


@pytest.fixture
def env_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(store, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(store, "_keyring_usable", lambda: False)
    return tmp_path


def test_env_file_fallback_roundtrip(env_only: Path) -> None:
    where = store.set_secret(KEY, "t" * 32)
    assert where.endswith(".env")
    assert "M2O_MOODLE_TOKEN_MOODLE_TEST=" in (env_only / ".env").read_text(encoding="utf-8")
    assert store.get_secret(KEY) == "t" * 32
    store.delete_secret(KEY)
    assert store.get_secret(KEY) is None


def test_env_var_overrides(env_only: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M2O_MOODLE_TOKEN", "fromenvironment1")
    assert store.get_secret(KEY) == "fromenvironment1"


def test_keyring_preferred(env_only: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(store, "_keyring_usable", lambda: True)
    monkeypatch.setattr(keyring, "set_password", lambda s, k, v: fake.__setitem__((s, k), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, k: fake.get((s, k)))
    assert store.set_secret(KEY, "k" * 32) == "system keyring"
    assert store.get_secret(KEY) == "k" * 32
    assert not (env_only / ".env").exists()
