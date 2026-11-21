from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from moodle2obsidian.config import Settings, load_settings, save_settings
from moodle2obsidian.errors import M2OError


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_yaml(tmp_path: Path) -> None:
    path = write(tmp_path, "moodle:\n  url: https://moodle.test/\nlanguage: en\nvault:\n  path: ~/v\n")
    settings = load_settings(path)
    assert settings.moodle.url == "https://moodle.test"
    assert settings.moodle.host == "moodle.test"
    assert settings.language == "en"
    assert "~" not in str(settings.vault.path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(M2OError, match="m2o init"):
        load_settings(tmp_path / "nope.yaml")


@pytest.mark.parametrize("url", ["http://moodle.test", "ftp://moodle.test", "https://moodle.test/?a=1", "moodle"])
def test_rejects_non_https_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(M2OError):
        load_settings(write(tmp_path, f"moodle:\n  url: {url}\n"))


def test_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(M2OError):
        load_settings(write(tmp_path, "moodle:\n  url: https://moodle.test\ntypo_key: 1\n"))


def test_yaml_is_loaded_safely(tmp_path: Path) -> None:
    with pytest.raises(M2OError, match="YAML"):
        load_settings(write(tmp_path, "moodle: !!python/object/apply:os.system ['echo pwned']\n"))


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M2O_LANGUAGE", "en")
    monkeypatch.setenv("M2O_MOODLE__RATE_LIMIT_PER_SEC", "2")
    settings = load_settings(write(tmp_path, "moodle:\n  url: https://moodle.test\nlanguage: uk\n"))
    assert settings.language == "en"
    assert settings.moodle.rate_limit_per_sec == 2
    assert settings.moodle.url == "https://moodle.test"


def test_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.yaml"
    settings = Settings.model_validate({"moodle": {"url": "https://moodle.test"}, "language": "en"})
    save_settings(settings, path)
    assert load_settings(path).language == "en"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
