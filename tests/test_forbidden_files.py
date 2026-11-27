from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_forbidden_files", Path(__file__).parents[1] / "scripts" / "check_forbidden_files.py"
)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "sub/.env.local",
        "state.db",
        "downloads/lecture.pdf",
        "x/cookies.json",
        "a.session",
        "lecture.srt",
        "logs/m2o.log",
        "config.yaml",
        "playwright/.auth/state.json",
        "cache\\video.mp4",
    ],
)
def test_blocks(path: str) -> None:
    assert guard.find_forbidden([path]) == [path]


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "config.example.yaml",
        "src/moodle2obsidian/config.py",
        "src/moodle2obsidian/vault/writer.py",
        "tests/fixtures/course.json",
    ],
)
def test_allows(path: str) -> None:
    assert guard.find_forbidden([path]) == []
