from __future__ import annotations

import os

import pytest

from moodle2obsidian.config import MoodleConfig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("M2O_"):
            monkeypatch.delenv(name)


@pytest.fixture
def moodle_cfg() -> MoodleConfig:
    return MoodleConfig(url="https://moodle.test", rate_limit_per_sec=10, max_retries=2)
