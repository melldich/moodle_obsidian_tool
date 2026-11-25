from __future__ import annotations

import os
import stat
from pathlib import Path

from moodle2obsidian.state import State


def test_module_change_detection(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        module = {"id": 5, "modname": "page", "name": "Intro"}
        assert state.upsert_module(1, 0, "General", module) is True
        assert state.upsert_module(1, 0, "General", module) is False
        assert state.upsert_module(1, 0, "General", {**module, "name": "Intro v2"}) is True


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        module = {"id": 5, "modname": "page", "name": "Intro"}
        assert state.upsert_module(1, 0, "General", module, write=False) is True
        assert state.upsert_module(1, 0, "General", module, write=False) is True
        assert state.stats()["modules"] == 0


def test_files_and_dedup_lookup(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        state.upsert_course({"id": 1, "shortname": "A", "fullname": "Alpha"})
        state.record_file(
            key="5:/a.pdf",
            course_id=1,
            cmid=5,
            filename="a.pdf",
            mimetype="application/pdf",
            filesize=10,
            timemodified=99,
            local_path="/x/a.pdf",
            sha256="abc",
        )
        row = state.get_file("5:/a.pdf")
        assert row is not None and row["timemodified"] == 99
        assert state.path_for_sha256("abc") == "/x/a.pdf"
        assert state.path_for_sha256("nope") is None
        stats = state.stats()
        assert (stats["courses"], stats["files"], stats["bytes"]) == (1, 1, 10)


def test_state_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "data" / "state.db"
    State(path).close()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_readonly_open_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with State(path) as state:
        state.upsert_course({"id": 1, "shortname": "A", "fullname": "Alpha"})
    with State(path, readonly=True) as state:
        assert state.stats()["courses"] == 1


def test_key_for_path(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        state.record_file(
            key="5:/a.pdf",
            course_id=1,
            cmid=5,
            filename="a.pdf",
            mimetype="",
            filesize=1,
            timemodified=1,
            local_path="/x/a.pdf",
            sha256="h",
        )
        assert state.key_for_path("/x/a.pdf") == "5:/a.pdf"
        assert state.key_for_path("/x/b.pdf") is None
