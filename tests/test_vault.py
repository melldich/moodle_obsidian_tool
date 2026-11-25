from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from moodle2obsidian.config import Settings
from moodle2obsidian.state import State
from moodle2obsidian.vault.merge import END, START, split_frontmatter, write_note
from moodle2obsidian.vault.writer import VaultWriter, course_title, note_name, safe_link

# --- merge ---------------------------------------------------------------------------------------------


def test_create_and_unchanged(tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    assert write_note(note, {"type": "lecture"}, "Hello") == "created"
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---\n") and f"{START}\nHello\n{END}" in text
    mtime = note.stat().st_mtime_ns
    assert write_note(note, {"type": "lecture"}, "Hello") == "unchanged"
    assert note.stat().st_mtime_ns == mtime


def test_update_keeps_user_text_and_frontmatter(tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    write_note(note, {"type": "lecture", "updated": "2026-01-01"}, "v1")
    data, _, body = split_frontmatter(note.read_text(encoding="utf-8"))
    assert data is not None
    data["my_rating"] = 5
    raw = yaml.safe_dump(data, allow_unicode=True)
    note.write_text(f"---\n{raw}---\nMy intro\n\n{body.strip()}\n\nMy outro\n", encoding="utf-8")

    assert write_note(note, {"type": "lecture", "updated": "2026-02-02"}, "v2") == "updated"
    data, _, body = split_frontmatter(note.read_text(encoding="utf-8"))
    assert data is not None
    assert data["my_rating"] == 5 and data["updated"] == "2026-02-02"
    assert "My intro" in body and "My outro" in body
    assert "v2" in body and "v1" not in body


def test_edit_inside_block_is_never_overwritten(tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    write_note(note, {}, "generated")
    edited = note.read_text(encoding="utf-8").replace("generated", "generated + my fix")
    note.write_text(edited, encoding="utf-8")
    assert write_note(note, {}, "generated v2") == "conflict"
    assert note.read_text(encoding="utf-8") == edited
    assert "generated v2" in (tmp_path / "n.m2o-new.md").read_text(encoding="utf-8")


def test_removed_markers_append_block(tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    note.write_text("Only my notes\n", encoding="utf-8")
    assert write_note(note, {"type": "lecture"}, "generated") == "updated"
    text = note.read_text(encoding="utf-8")
    assert "Only my notes" in text and f"{START}\ngenerated\n{END}" in text


def test_invalid_user_yaml_is_kept(tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    note.write_text(f"---\nbad: [unclosed\n---\n{START}\nold\n{END}\n", encoding="utf-8")
    assert write_note(note, {"type": "lecture"}, "new") == "updated"
    text = note.read_text(encoding="utf-8")
    assert "bad: [unclosed" in text and "new" in text


# --- helpers -------------------------------------------------------------------------------------------


def test_note_name_and_links() -> None:
    assert note_name("Week [1] #intro | a^b") == "Week 1 intro a b"
    assert safe_link("javascript:alert(1)", "x") is None
    assert safe_link("https://x.org/a b", "x") is None
    assert safe_link("https://x.org/a(1)", "Docs [v2]") == "[Docs (v2)](<https://x.org/a(1)>)"
    course = {"id": 1, "shortname": "MATH101 (2026-2027)", "fullname": "MATH101 Intro to Math / Вступ до математики"}
    assert course_title(course, "uk") == "MATH101 Вступ до математики"
    assert course_title(course, "en") == "MATH101 Intro to Math"


# --- end to end ----------------------------------------------------------------------------------------

SITE = "https://moodle.test"


def _file(state: State, root: Path, cmid: int, name: str, data: bytes, mimetype: str, filepath: str = "/") -> None:
    path = root / f"{cmid}-{filepath.strip('/')}-{name}"
    path.write_bytes(data)
    state.record_file(
        key=f"{cmid}:{filepath}{name}",
        course_id=101,
        cmid=cmid,
        filename=name,
        mimetype=mimetype,
        filesize=len(data),
        timemodified=1,
        local_path=str(path),
        sha256=f"{cmid:08d}{filepath}{name}",
    )


@pytest.fixture
def populated(tmp_path: Path) -> tuple[State, Settings]:
    downloads = tmp_path / "dl"
    downloads.mkdir()
    (tmp_path / "vault" / ".obsidian").mkdir(parents=True)
    state = State(tmp_path / "state.db")
    sections = [
        {
            "id": 1,
            "section": 0,
            "name": "General",
            "summary": "<p>Welcome<script>alert(1)</script></p>",
            "modules": [11, 13, 14, 15, 16],
        },
        {"id": 2, "section": 1, "name": "Week 1: Graphs", "summary": "", "modules": [17]},
        {"id": 3, "section": 2, "name": "Empty", "summary": "", "modules": []},
        {"id": 4, "section": 3, "name": "Exams", "summary": "", "modules": [18, 19]},
    ]
    course = {"id": 101, "shortname": "ALG (2026)", "fullname": "ALG Algorithms / Алгоритми", "sections": sections}
    state.upsert_course(course)
    structure = json.dumps([{"title": "Intro", "href": "1/index.html"}, {"title": "Trees", "href": "2/index.html"}])
    modules: list[dict[str, Any]] = [
        {"id": 11, "modname": "resource", "name": "Slides", "url": f"{SITE}/mod/resource/view.php?id=11"},
        {
            "id": 13,
            "modname": "assign",
            "name": "HW1",
            "url": f"{SITE}/mod/assign/view.php?id=13",
            "assignment": {"name": "HW1", "duedate": 4102444800, "intro": "<p>Solve <b>all</b></p>"},
        },
        {
            "id": 14,
            "modname": "forum",
            "name": "News",
            "forum": {
                "discussions": [
                    {"name": "Welcome", "created": 1767225600, "message": "<p>Hi <img src=x onerror=alert(1)></p>"}
                ]
            },
        },
        {
            "id": 15,
            "modname": "url",
            "name": "Lecture video",
            "contents": [{"type": "url", "fileurl": "https://www.youtube.com/watch?v=abc"}],
        },
        {"id": 16, "modname": "label", "name": "x", "description": "<p>Read chapter 1</p>"},
        {
            "id": 17,
            "modname": "book",
            "name": "Graph book",
            "url": f"{SITE}/mod/book/view.php?id=17",
            "contents": [
                {"type": "content", "filename": "structure", "content": structure},
                {"type": "file", "filename": "index.html", "filepath": "/2/"},
                {"type": "file", "filename": "index.html", "filepath": "/1/"},
                {"type": "file", "filename": "graph.png", "filepath": "/1/"},
            ],
        },
    ]
    modules += [
        {"id": 18, "modname": "quiz", "name": "Midterm quiz", "url": f"{SITE}/mod/quiz/view.php?id=18"},
        {
            "id": 19,
            "modname": "assign",
            "name": "Final project",
            "url": f"{SITE}/mod/assign/view.php?id=19",
            "assignment": {"name": "Final project", "duedate": 4102531200},
        },
    ]
    for module in modules:
        state.upsert_module(101, 0, "General", module)
    _file(state, downloads, 11, "slides.pdf", b"%PDF", "application/pdf")
    _file(state, downloads, 13, "task.pdf", b"%PDF task", "application/pdf", "/intro/")
    _file(state, downloads, 17, "index.html", b"<h1>Intro</h1><img src='graph.png'>", "text/html", "/1/")
    _file(state, downloads, 17, "index.html", b"<p>Trees are graphs</p>", "text/html", "/2/")
    _file(state, downloads, 17, "graph.png", b"\x89PNG", "image/png", "/1/")
    state.commit()
    settings = Settings.model_validate(
        {"moodle": {"url": SITE}, "vault": {"path": str(tmp_path / "vault")}, "language": "uk"}
    )
    return state, settings


def test_vault_end_to_end(populated: tuple[State, Settings]) -> None:
    state, settings = populated
    report = VaultWriter(state, settings).write_all()
    root = settings.vault.path / "Moodle"
    assert report.conflicts == []

    dashboard = (root / "00 Dashboard.md").read_text(encoding="utf-8")
    assert "[[ALG Алгоритми (MOC)]]" in dashboard and "FLATTEN file.lists" in dashboard
    assert "HW1 · [[ALG 00 General]]" in dashboard and "Final project (ALG)" in dashboard

    course_dir = root / "ALG Алгоритми"
    assert not (course_dir / "Завдання").exists()  # assignments are not graph nodes
    assert sorted(p.name for p in (course_dir / "Теми").iterdir()) == ["ALG 00 General.md", "ALG 01 Week 1_ Graphs.md"]
    moc = (course_dir / "ALG Алгоритми (MOC).md").read_text(encoding="utf-8")
    assert "[[ALG 00 General]]" in moc and "[[ALG 01 Week 1_ Graphs]]" in moc
    assert "Empty" not in moc and "Exams" not in moc  # no topic for empty / tests-only sections
    assert "HW1 · [[ALG 00 General]]" in moc and "Final project" in moc and "Midterm quiz" in moc

    week0 = (course_dir / "Теми" / "ALG 00 General.md").read_text(encoding="utf-8")
    front, _, body = split_frontmatter(week0)
    assert front is not None and front["type"] == "topic" and "course/alg" in front["tags"]
    assert body.index("## Конспект") < body.index("## Матеріали") < body.index("## Завдання")
    assert "[[ALG Алгоритми (MOC)]]" in body
    assert "Welcome" in body and "Read chapter 1" in body
    assert "### Відео" in body and "[Lecture video](<https://www.youtube.com/watch?v=abc>)" in body
    assert "- **Slides**: [[slides-00000011.pdf]]" in body
    assert "[deadline:: 2100-01-01T" in body and "**HW1**" in body
    assert "> [!todo]- Умова" in body and "**all**" in body and "[[task-00000013.pdf]]" in body
    assert "> [!quote]- News" in body
    for bad in ("<script", "alert", "onerror"):
        assert bad not in week0

    book = (course_dir / "Теми" / "ALG 01 Week 1_ Graphs.md").read_text(encoding="utf-8")
    assert "> [!note]- Graph book" in book
    assert book.index("#### Intro") < book.index("#### Trees")  # chapters in book order, not key order
    assert "![[graph-00000017.png]]" in book

    attachments = {p.name for p in (root / "attachments" / "ALG Алгоритми").iterdir()}
    assert attachments == {"slides-00000011.pdf", "task-00000013.pdf", "graph-00000017.png"}
    for path in root.rglob("*"):
        assert path.resolve().is_relative_to(root.resolve())

    # second run: nothing changes; user text outside the block survives a later update
    again = VaultWriter(state, settings).write_all()
    assert again.created == again.updated == 0
    note = course_dir / "Теми" / "ALG 00 General.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nMy own notes\n", encoding="utf-8")
    VaultWriter(state, settings).write_all()
    assert "My own notes" in note.read_text(encoding="utf-8")
    state.close()


def test_stale_notes_removed_only_if_untouched(populated: tuple[State, Settings]) -> None:
    state, settings = populated
    old = settings.vault.path / "Moodle" / "ALG Алгоритми" / "Завдання"
    write_note(old / "ALG Old task.md", {"type": "assignment"}, "old generated")
    write_note(old / "ALG Edited task.md", {"type": "assignment"}, "old generated")
    edited = old / "ALG Edited task.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nmy answer\n", encoding="utf-8")
    mine = settings.vault.path / "Moodle" / "My own page.md"
    mine.write_text("---\ntype: assignment\n---\nnot generated\n", encoding="utf-8")

    report = VaultWriter(state, settings).write_all()
    assert report.removed == 1
    assert not (old / "ALG Old task.md").exists()
    assert edited.exists() and report.kept_stale == [str(Path("ALG Алгоритми") / "Завдання" / "ALG Edited task.md")]
    assert mine.exists()
    state.close()


def test_missing_vault_is_a_clear_error(populated: tuple[State, Settings], tmp_path: Path) -> None:
    state, settings = populated
    settings.vault.path = tmp_path / "nope"
    with pytest.raises(Exception, match="Vault"):
        VaultWriter(state, settings)
    state.close()


def test_videos_embedded_in_pages_are_listed(populated: tuple[State, Settings]) -> None:
    state, settings = populated
    page = {
        "id": 20,
        "modname": "page",
        "name": "Before class",
        "contents": [{"type": "file", "filename": "index.html", "filepath": "/"}],
    }
    state.upsert_module(101, 0, "General", page)
    course = state.courses()[0]
    course["sections"][1]["modules"].append(20)
    state.upsert_course(course)
    root = Path(state.files_for_module(17)[0]["local_path"]).parent
    html = b"<iframe src='https://www.youtube.com/embed/xyz'></iframe><a href='https://youtu.be/xyz2'>v</a>"
    _file(state, root, 20, "index.html", html, "text/html")
    VaultWriter(state, settings).write_all()
    note = settings.vault.path / "Moodle" / "ALG Алгоритми" / "Теми" / "ALG 01 Week 1_ Graphs.md"
    videos = note.read_text(encoding="utf-8").split("### Відео", 1)[1].split("###", 1)[0]
    assert "https://www.youtube.com/embed/xyz" in videos and "https://youtu.be/xyz2" in videos
    state.close()
