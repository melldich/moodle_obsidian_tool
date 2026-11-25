from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from moodle2obsidian.config import ExtractConfig
from moodle2obsidian.extract import runner, worker
from moodle2obsidian.extract.formats import ExtractError, Options, Unsupported, extract_bytes, html_to_markdown
from moodle2obsidian.state import State

OPTS = Options(ocr=False)


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 10 50 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    for number, body in enumerate(objects, 1):
        out += b"%d 0 obj" % number + body + b"endobj\n"
    return out + b"trailer<</Root 1 0 R>>\n%%EOF\n"  # no xref: pdfium rebuilds it


def test_pdf() -> None:
    text = extract_bytes("lecture.pdf", make_pdf("Hello lecture"), OPTS)
    assert "<!-- page 1 -->" in text
    assert "Hello lecture" in text


def test_broken_pdf() -> None:
    with pytest.raises(ExtractError):
        extract_bytes("broken.pdf", b"%PDF-1.4 garbage", OPTS)


def test_docx() -> None:
    import docx

    document = docx.Document()
    document.add_heading("Тема 1", level=1)
    document.add_paragraph("Визначення графа.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text, table.cell(1, 0).text, table.cell(1, 1).text = "a", "b", "1", "2"
    buffer = io.BytesIO()
    document.save(buffer)
    text = extract_bytes("notes.docx", buffer.getvalue(), OPTS)
    assert "## Тема 1" in text
    assert "Визначення графа." in text
    assert "| a | b |" in text


def test_pptx() -> None:
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Graphs"
    slide.placeholders[1].text = "A graph is a pair (V, E)"
    slide.notes_slide.notes_text_frame.text = "say this aloud"
    buffer = io.BytesIO()
    presentation.save(buffer)
    text = extract_bytes("slides.pptx", buffer.getvalue(), OPTS)
    assert "## Graphs" in text
    assert "A graph is a pair (V, E)" in text
    assert "> say this aloud" in text


def test_html_is_sanitized_and_videos_kept_as_links() -> None:
    html = (
        "<h2>Week 1</h2><p onclick='steal()'>Intro <a href='javascript:alert(1)'>x</a></p>"
        "<script>alert(1)</script><style>p{}</style><object data='x.swf'></object>"
        "<iframe src='https://www.youtube.com/embed/abc123'></iframe><iframe src='file:///etc/passwd'></iframe>"
    )
    md = html_to_markdown(html)
    assert "## Week 1" in md
    assert "https://www.youtube.com/embed/abc123" in md
    for bad in ("script", "onclick", "javascript:", "alert", "iframe", "object", "file://"):
        assert bad not in md


def test_code_and_text() -> None:
    assert extract_bytes("a.py", b"print(1)\n", OPTS) == "```python\nprint(1)\n```"
    assert extract_bytes("a.py", b"s = '```'", OPTS).startswith("````python")
    assert extract_bytes("notes.txt", "Привіт".encode("cp1251"), OPTS) == "Привіт"
    with pytest.raises(Unsupported):
        extract_bytes("photo.jpg", b"\xff\xd8", OPTS)


def test_ipynb() -> None:
    notebook = {
        "metadata": {"language_info": {"name": "python"}},
        "cells": [
            {"cell_type": "markdown", "source": ["# Lab 1\n", "Sorting"]},
            {"cell_type": "code", "source": "sorted([3, 1])"},
        ],
    }
    text = extract_bytes("lab.ipynb", json.dumps(notebook).encode(), OPTS)
    assert "# Lab 1\nSorting" in text
    assert "```python\nsorted([3, 1])\n```" in text


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_zip_reads_members_in_memory_only(tmp_path: Path) -> None:
    data = make_zip(
        {
            "../../evil.py": b"x = 1",
            "lab/readme.txt": b"hello",
            "__MACOSX/._junk": b"junk",
            "inner.zip": make_zip({"a.py": b"y = 2"}),
            "photo.jpg": b"\xff\xd8",
        }
    )
    text = extract_bytes("code.zip", data, OPTS)
    assert "## evil.py" in text and "x = 1" in text
    assert "## lab/readme.txt" in text
    assert "junk" not in text and "inner.zip" not in text and "photo.jpg" not in text
    assert not (tmp_path.parent / "evil.py").exists()


def test_zip_limits() -> None:
    with pytest.raises(ExtractError, match="files"):
        extract_bytes("many.zip", make_zip({f"f{i}.txt": b"x" for i in range(5)}), Options(max_zip_members=3))
    with pytest.raises(ExtractError, match="large"):
        extract_bytes("big.zip", make_zip({"big.txt": b"0" * 5000}), Options(max_zip_bytes=1000))


def test_worker_exit_codes(tmp_path: Path) -> None:
    options = json.dumps({"ocr": False})
    src = tmp_path / "a.py"
    src.write_text("print(1)", encoding="utf-8")
    assert worker.main([str(src), str(tmp_path / "out.md"), options]) == 0
    assert "print(1)" in (tmp_path / "out.md").read_text(encoding="utf-8")
    image = tmp_path / "p.jpg"
    image.write_bytes(b"\xff\xd8")
    assert worker.main([str(image), str(tmp_path / "img.md"), options]) == worker.EXIT_UNSUPPORTED
    broken = tmp_path / "b.pdf"
    broken.write_bytes(b"%PDF garbage")
    assert worker.main([str(broken), str(tmp_path / "b.md"), options]) == 1


def _record(state: State, tmp_path: Path, name: str, data: bytes, mimetype: str, sha: str) -> None:
    path = tmp_path / name
    path.write_bytes(data)
    state.record_file(
        key=f"1:/{name}",
        course_id=1,
        cmid=1,
        filename=name,
        mimetype=mimetype,
        filesize=len(data),
        timemodified=1,
        local_path=str(path),
        sha256=sha,
    )


def test_runner_isolates_failures_and_is_incremental(tmp_path: Path) -> None:
    with State(tmp_path / "state.db") as state:
        _record(state, tmp_path, "good.py", b"print(1)", "text/plain", "s1")
        _record(state, tmp_path, "broken.pdf", b"%PDF garbage", "application/pdf", "s2")
        _record(state, tmp_path, "pic.jpg", b"\xff\xd8", "image/jpeg", "s3")
        out = tmp_path / "extracted"
        report = runner.extract_pending(state, out, ExtractConfig(ocr=False))
        assert report.extracted == 1
        assert report.skipped == 1
        assert [name for name, _ in report.failed] == ["broken.pdf"]
        assert "print(1)" in (out / "s1.md").read_text(encoding="utf-8")
        again = runner.extract_pending(state, out, ExtractConfig(ocr=False))
        assert (again.extracted, again.skipped, again.failed) == (0, 0, [])
        assert state.stats()["extracted"] == 1


def test_runner_timeout_is_a_failure_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="worker", timeout=1)

    monkeypatch.setattr(runner.subprocess, "run", hang)
    with State(tmp_path / "state.db") as state:
        _record(state, tmp_path, "slow.pdf", b"%PDF", "application/pdf", "s9")
        report = runner.extract_pending(state, tmp_path / "out", ExtractConfig(timeout_sec=1))
    assert report.failed == [("slow.pdf", "timeout after 1s")]
