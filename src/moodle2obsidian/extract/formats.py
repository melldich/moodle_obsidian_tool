"""Turn downloaded files into Markdown text. Runs inside the isolated worker process (see worker.py)."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import nh3
from bs4 import BeautifulSoup
from markdownify import markdownify

from ..safety import split_moodle_path


class Unsupported(Exception):
    pass


class ExtractError(Exception):
    pass


@dataclass(frozen=True)
class Options:
    ocr: bool = True
    ocr_languages: str = "ukr+eng"
    max_zip_bytes: int = 200 * 1024 * 1024
    max_zip_members: int = 500


CODE_LANG = {
    ".py": "python",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".js": "javascript",
    ".ts": "typescript",
    ".sql": "sql",
    ".r": "r",
    ".sh": "bash",
    ".tex": "latex",
    ".json": "json",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".m": "matlab",
}
PLAIN_TEXT = {".txt", ".md", ".csv"}


def extract_bytes(name: str, data: bytes, opts: Options, *, depth: int = 0) -> str:
    ext = PurePosixPath(name.lower()).suffix
    if ext == ".pdf":
        return pdf_to_md(data, opts)
    if ext == ".docx":
        return docx_to_md(data)
    if ext == ".pptx":
        return pptx_to_md(data)
    if ext in (".html", ".htm"):
        return html_to_markdown(decode(data))
    if ext == ".ipynb":
        return ipynb_to_md(data)
    if ext == ".zip":
        if depth:
            raise Unsupported("nested zip")
        return zip_to_md(data, opts)
    if ext in CODE_LANG:
        return fence(decode(data), CODE_LANG[ext])
    if ext in PLAIN_TEXT:
        return decode(data)
    raise Unsupported(ext or "no extension")


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def fence(text: str, lang: str = "") -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def html_to_markdown(html: str) -> str:
    """Untrusted HTML → Markdown with no active content (scripts, iframes, on* handlers, javascript: links)."""
    soup = BeautifulSoup(html, "html.parser")
    # keep embedded videos as plain links, so lectures on YouTube/Drive stay visible in the notes
    for frame in soup.find_all(["iframe", "embed", "video"]):
        src = frame.get("src") or (frame.find("source") or {}).get("src") or ""
        if isinstance(src, str) and src.startswith("//"):
            src = "https:" + src
        if isinstance(src, str) and src.startswith(("http://", "https://")):
            link = soup.new_tag("a", href=src)
            link.string = src
            frame.replace_with(link)
        else:
            frame.decompose()
    clean = nh3.clean(str(soup), url_schemes={"http", "https", "mailto"})
    markdown = markdownify(clean, heading_style="ATX")
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


def pdf_to_md(data: bytes, opts: Options) -> str:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise ExtractError(f"broken PDF: {exc}") from None
    parts = []
    for index in range(len(pdf)):
        page = pdf[index]
        text = page.get_textpage().get_text_bounded().strip()
        if len(text) < 20 and opts.ocr:  # likely a scanned page
            text = _ocr(page, opts.ocr_languages) or text
        parts.append(f"<!-- page {index + 1} -->\n{text}")
    return "\n\n".join(parts)


def _ocr(page: Any, languages: str) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "page.png"
        page.render(scale=200 / 72).to_pil().save(image)
        # our own temp file and a validated language string; no shell
        proc = subprocess.run(  # noqa: S603
            [exe, str(image), "stdout", "-l", languages], capture_output=True, timeout=120, check=False
        )
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""


def docx_to_md(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises many types on corrupt files
        raise ExtractError(f"broken DOCX: {type(exc).__name__}") from None
    lines = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name if paragraph.style is not None else "").lower()
        if style.startswith("heading") and style[-1:].isdigit():
            lines.append("#" * min(int(style[-1]) + 1, 6) + " " + text)
        elif style.startswith("list"):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    # shortcut: tables are appended after the text, not in place; fine for summarising
    for table in document.tables:
        rows = [[cell.text.strip().replace("|", "\\|").replace("\n", " ") for cell in row.cells] for row in table.rows]
        if rows:
            lines.append(_md_table(rows))
    return "\n\n".join(lines)


def _md_table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, *body = rows
    out = ["| " + " | ".join(head) + " |", "|" + " --- |" * width]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


def pptx_to_md(data: bytes) -> str:
    from pptx import Presentation

    try:
        presentation = Presentation(io.BytesIO(data))
    except Exception as exc:  # python-pptx raises many types on corrupt files
        raise ExtractError(f"broken PPTX: {type(exc).__name__}") from None
    slides = []
    for number, slide in enumerate(presentation.slides, 1):
        title_shape = slide.shapes.title
        title = title_shape.text.strip() if title_shape is not None and title_shape.text.strip() else f"Slide {number}"
        body = [
            shape.text_frame.text.strip()
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip() and shape.text_frame.text.strip() != title
        ]
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
        block = f"## {title}\n\n" + "\n\n".join(body)
        if notes:
            block += "\n\n" + "\n".join(f"> {line}" for line in notes.splitlines())
        slides.append(block.strip())
    return "\n\n".join(slides)


def ipynb_to_md(data: bytes) -> str:
    try:
        notebook = json.loads(decode(data))
    except ValueError:
        raise ExtractError("broken notebook JSON") from None
    if not isinstance(notebook, dict):
        raise ExtractError("broken notebook JSON")
    lang = str(notebook.get("metadata", {}).get("language_info", {}).get("name") or "python")
    parts = []
    for cell in notebook.get("cells") or []:
        source = cell.get("source") or ""
        text = "".join(source) if isinstance(source, list) else str(source)
        if not text.strip():
            continue
        parts.append(text if cell.get("cell_type") == "markdown" else fence(text, lang))
    return "\n\n".join(parts)


def zip_to_md(data: bytes, opts: Options) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ExtractError("broken zip") from None
    members = [i for i in archive.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
    if len(members) > opts.max_zip_members:
        raise ExtractError(f"zip has {len(members)} files (limit {opts.max_zip_members})")
    if sum(i.file_size for i in members) > opts.max_zip_bytes:
        raise ExtractError("zip is too large when unpacked")
    parts = []
    total = 0
    for info in members:
        # members are read into memory and never written to disk, so zip-slip paths cannot escape anywhere;
        # reads are capped because declared sizes in the header can lie (zip bombs)
        try:
            with archive.open(info) as member:
                content = member.read(opts.max_zip_bytes - total + 1)
        except (RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:  # encrypted / unsupported method
            raise ExtractError(f"cannot read zip member: {type(exc).__name__}") from None
        total += len(content)
        if total > opts.max_zip_bytes:
            raise ExtractError("zip bomb: unpacked size exceeds the limit")
        display = "/".join(split_moodle_path(info.filename)) or "file"
        try:
            text = extract_bytes(display, content, opts, depth=1)
        except Unsupported:
            continue
        except ExtractError as exc:
            text = f"_(extraction failed: {exc})_"
        parts.append(f"## {display}\n\n{text}")
    return "\n\n".join(parts)
