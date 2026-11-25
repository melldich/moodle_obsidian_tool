"""End-to-end fetch of a fake course — no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from moodle2obsidian.config import CoursesConfig, FetchConfig, MoodleConfig
from moodle2obsidian.fetch.collect import Collector, available_functions, course_skip_reason, fetch_courses
from moodle2obsidian.moodle.client import MoodleClient
from moodle2obsidian.state import State

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "fake_moodle.json").read_text(encoding="utf-8"))
TOKEN = "c" * 32
FILE_BODIES = {
    "lec1.pdf": b"PDF-1",
    "lec1-copy.pdf": b"PDF-1",
    "evil.txt": b"evil",
    "CON.txt": b"con",
    "talk.mp4": b"video!",
    "hw1.pdf": b"hw!!",
}


def ws_handler(request: httpx.Request) -> httpx.Response:
    body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert body["wstoken"] == TOKEN
    data: Any = FIXTURE[body["wsfunction"]]
    if body["wsfunction"] == "core_course_get_contents":
        data = data[body["courseid"]]
    return httpx.Response(200, json=data)


def file_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["token"] == TOKEN
    return httpx.Response(200, content=FILE_BODIES[request.url.path.rsplit("/", 1)[-1]])


@pytest.fixture
def moodle() -> Any:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://moodle.test/webservice/rest/server.php").mock(side_effect=ws_handler)
        files = mock.get(url__startswith="https://moodle.test/webservice/pluginfile.php/").mock(
            side_effect=file_handler
        )
        outside = mock.route(host="example.org").mock(return_value=httpx.Response(200))
        yield files, outside


def run(cfg: MoodleConfig, tmp_path: Path, **kwargs: Any) -> Any:
    with MoodleClient(cfg, TOKEN, sleep=lambda _: None) as client, State(tmp_path / "state.db") as state:
        info, courses = fetch_courses(client)
        selected = [c for c in courses if course_skip_reason(c, CoursesConfig(), 0) is None]
        collector = Collector(client, state, FetchConfig(), tmp_path / "dl", available_functions(info), **kwargs)
        return collector.run(selected)


def test_full_fetch_then_incremental(moodle: Any, moodle_cfg: MoodleConfig, tmp_path: Path) -> None:
    files, outside = moodle
    report = run(moodle_cfg, tmp_path)

    assert report.courses == 1  # archived course 102 skipped
    assert report.modules == 7
    assert report.downloaded == 5  # lec1, evil, CON, talk, hw1
    assert report.deduplicated == 1  # lec1-copy has the same bytes as lec1
    assert report.failed == []
    assert not outside.called  # url module links are recorded, never fetched

    root = (tmp_path / "dl").resolve()
    written = [p for p in root.rglob("*") if p.is_file()]
    assert all(p.resolve().is_relative_to(root) for p in written)
    names = {p.name for p in written}
    assert {"lec1.pdf", "_.._evil.txt", "_CON.txt", "talk.mp4", "hw1.pdf"} <= names
    assert "lec1-copy.pdf" not in names
    assert not (tmp_path / "evil.txt").exists()

    calls = files.call_count
    again = run(moodle_cfg, tmp_path)
    assert again.downloaded == 0
    assert again.unchanged == 6
    assert again.changed_modules == 0
    assert files.call_count == calls


def test_dry_run_downloads_nothing(moodle: Any, moodle_cfg: MoodleConfig, tmp_path: Path) -> None:
    files, _ = moodle
    report = run(moodle_cfg, tmp_path, dry_run=True)
    assert not files.called
    assert len(report.planned) == 6
    with State(tmp_path / "state.db") as state:
        assert state.stats()["modules"] == 0


def test_no_video(moodle: Any, moodle_cfg: MoodleConfig, tmp_path: Path) -> None:
    report = run(moodle_cfg, tmp_path, no_video=True)
    assert report.skipped == 1
    assert not list((tmp_path / "dl").rglob("talk.mp4"))


def test_missing_optional_functions_degrade(moodle: Any, moodle_cfg: MoodleConfig, tmp_path: Path) -> None:
    with MoodleClient(moodle_cfg, TOKEN, sleep=lambda _: None) as client, State(tmp_path / "s.db") as state:
        _, courses = fetch_courses(client)
        only_core = {"core_webservice_get_site_info", "core_enrol_get_users_courses", "core_course_get_contents"}
        report = Collector(client, state, FetchConfig(), tmp_path / "dl", only_core).run(courses[:1])
    assert report.failed == []
    assert report.downloaded == 4  # no hw1.pdf without mod_assign


@pytest.mark.parametrize(
    ("cfg", "course", "skipped"),
    [
        (CoursesConfig(), {"id": 1, "fullname": "A"}, False),
        (CoursesConfig(), {"id": 1, "fullname": "A", "hidden": True}, True),
        (CoursesConfig(skip_hidden=False), {"id": 1, "fullname": "A", "hidden": True}, False),
        (CoursesConfig(include=["2"]), {"id": 1, "fullname": "A"}, True),
        (CoursesConfig(include=["algo"]), {"id": 1, "fullname": "Algorithms"}, False),
        (CoursesConfig(exclude=["1"]), {"id": 1, "fullname": "A"}, True),
        (CoursesConfig(skip_past=True), {"id": 1, "fullname": "A", "enddate": 50}, True),
        (CoursesConfig(skip_past=True), {"id": 1, "fullname": "A", "enddate": 0}, False),
    ],
)
def test_course_filter(cfg: CoursesConfig, course: dict[str, Any], skipped: bool) -> None:
    assert (course_skip_reason(course, cfg, now=100) is not None) is skipped


def test_truncated_names_never_share_a_path(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        collector = Collector(None, state, FetchConfig(), tmp_path / "dl", set())  # type: ignore[arg-type]
        same = tmp_path / "dl" / "x" / "index.html"
        first = collector._unique_dest(same, "11:/index.html")
        second = collector._unique_dest(same, "12:/index.html")
        assert first == same
        assert second != first and second.suffix == ".html"
        assert collector._unique_dest(same, "11:/index.html") == same  # same file keeps its path


def test_module_dirs_include_cmid(moodle: Any, moodle_cfg: MoodleConfig, tmp_path: Path) -> None:
    run(moodle_cfg, tmp_path)
    dirs = {p.parent.name for p in (tmp_path / "dl").rglob("*.pdf")}
    assert "11 Lecture 1 slides" in dirs
