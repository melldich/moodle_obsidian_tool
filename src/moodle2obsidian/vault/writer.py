"""Build the vault from local state (no network): dashboard → course MOCs → one note per topic.

The graph branches only by topics: assignments, tests and files live inside their topic note and are not
graph nodes of their own. Each topic note is laid out as Summary → Key concepts → Materials → Assignments;
the summary is filled by the LLM stage.

Everything from Moodle is untrusted: HTML goes through html_to_markdown (nh3), plain text is escaped,
links must be http(s), and every path is computed here and checked with safe_join.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..config import Settings
from ..errors import M2OError
from ..extract.formats import decode, html_to_markdown
from ..notes.concepts import concept_key, sanitize_llm_markdown
from ..notes.summarize import MATERIAL_MODULES, load_topic_notes
from ..safety import safe_join, sanitize_filename
from ..state.db import State
from .merge import is_pristine, split_frontmatter, write_note

log = logging.getLogger(__name__)

LABELS: dict[str, dict[str, str]] = {
    "uk": {
        "topics": "Теми",
        "course": "Курс",
        "topic": "Тема",
        "summary": "Конспект",
        "summary_pending": "_Конспект з'явиться після обробки матеріалів теми (LLM)._",
        "concepts": "Ключові поняття",
        "materials": "Матеріали",
        "files": "Файли",
        "videos": "Відео",
        "links": "Посилання",
        "pages": "Тексти сторінок",
        "discussions": "Обговорення",
        "activities": "Тести та активності",
        "tasks": "Завдання",
        "tasks_and_tests": "Завдання та тести",
        "condition": "Умова",
        "deadline": "Дедлайн",
        "deadlines": "Найближчі дедлайни",
        "courses": "Курси",
        "no_deadline": "без дедлайну",
        "self_check": "Питання для самоперевірки",
        "concepts_dir": "Поняття",
        "related": "Пов'язані поняття",
        "mentioned": "Згадується в темах",
    },
    "en": {
        "topics": "Topics",
        "course": "Course",
        "topic": "Topic",
        "summary": "Summary",
        "summary_pending": "_The summary will appear after the topic materials are processed (LLM)._",
        "concepts": "Key concepts",
        "materials": "Materials",
        "files": "Files",
        "videos": "Videos",
        "links": "Links",
        "pages": "Page texts",
        "discussions": "Discussions",
        "activities": "Tests & activities",
        "tasks": "Assignments",
        "tasks_and_tests": "Assignments & tests",
        "condition": "Task",
        "deadline": "Deadline",
        "deadlines": "Upcoming deadlines",
        "courses": "Courses",
        "no_deadline": "no deadline",
        "self_check": "Self-check questions",
        "concepts_dir": "Concepts",
        "related": "Related concepts",
        "mentioned": "Appears in topics",
    },
}
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "drive.google.com", "panopto", "kaltura", "loom.com")
GENERATED_TYPES = {"topic", "lecture", "assignment", "moc", "dashboard", "concept"}  # incl. earlier layouts
DASHBOARD = "00 Dashboard"


@dataclass
class VaultReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    attachments: int = 0
    conflicts: list[str] = field(default_factory=list)
    kept_stale: list[str] = field(default_factory=list)


@dataclass
class _Course:
    cid: int
    code: str
    title: str
    folder: str
    moc: str
    tag: str
    used_names: set[str] = field(default_factory=set)
    tasks: list[str] = field(default_factory=list)


def note_name(text: str, max_len: int = 100) -> str:
    """A file name that is also a safe wikilink target (no # ^ [ ] |)."""
    return sanitize_filename(re.sub(r"[#^\[\]|]", " ", text), max_len)


def md_text(value: Any) -> str:
    """Plain text from Moodle as inert Markdown (no raw HTML, single line)."""
    return html.escape(" ".join(str(value or "").split()), quote=False)


def safe_link(url: Any, label: str) -> str | None:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")) or re.search(r"[\s<>]", url):
        return None
    text = md_text(label).replace("[", "(").replace("]", ")") or url
    return f"[{text}](<{url}>)"


def callout(kind: str, title: str, body: str) -> str:
    """A collapsed Obsidian callout, so long raw material stays out of the way."""
    lines = body.strip().splitlines() or [""]
    return f"> [!{kind}]- {md_text(title)}\n" + "\n".join(f"> {line}" if line else ">" for line in lines)


def course_code(course: dict[str, Any]) -> str:
    return str(course.get("shortname") or course["id"]).split(" (")[0].strip() or str(course["id"])


def course_title(course: dict[str, Any], language: str) -> str:
    full = str(course.get("fullname") or course.get("shortname") or course["id"])
    if " / " not in full:
        return full
    english, local = (part.strip() for part in full.split(" / ", 1))
    part = local if language == "uk" else english
    code = course_code(course)
    return part if part.startswith(code) else f"{code} {part}"


def _when(ts: int, iso: bool = False) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M" if iso else "%Y-%m-%d %H:%M")


_IMAGE = re.compile(r"!\[[^\]]*\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")


def _rewrite_images(markdown: str, attachments: dict[str, str]) -> str:
    """Point images of a Moodle page/book at the copies in attachments/ (matched by file name)."""

    def replace(match: re.Match[str]) -> str:
        name = unquote(match.group(1).split("?")[0].rsplit("/", 1)[-1])
        return f"![[{attachments[name]}]]" if name in attachments else match.group(0)

    return _IMAGE.sub(replace, markdown)


def _section(title: str, lines: list[str], level: int = 3) -> str:
    items = [line for line in lines if line]
    return f"{'#' * level} {title}\n\n" + "\n\n".join(items) if items else ""


class VaultWriter:
    def __init__(self, state: State, settings: Settings) -> None:
        if not settings.vault.path.is_dir():
            raise M2OError(f"Vault не найден: {settings.vault.path}", f"Vault not found: {settings.vault.path}")
        self.state = state
        self.language = settings.language
        self.t = LABELS[settings.language]
        self.subdir = settings.vault.subdir
        self.site = settings.moodle.url
        root = settings.vault.path / settings.vault.subdir
        root.mkdir(exist_ok=True)
        self.root = root.resolve()
        self.today = date.today().isoformat()
        self.report = VaultReport()
        self._deadlines: list[tuple[int, str, str]] = []  # (due, text, course code)
        self._written: set[Path] = set()
        self.notes_dir = settings.notes_dir
        self._concepts: dict[str, dict[str, Any]] = {}

    def write_all(self) -> VaultReport:
        mocs = [self._course(course) for course in self.state.courses()]
        self._concept_notes()
        self._dashboard(mocs)
        self._prune()
        return self.report

    # --- writing -----------------------------------------------------------------------------------------

    def _write(self, parts: list[str], fields: dict[str, Any], block: str) -> None:
        path = safe_join(self.root, *parts)
        self._written.add(path)
        result = write_note(path, fields, block)
        if result == "conflict":
            self.report.conflicts.append(str(path.relative_to(self.root)))
        else:
            setattr(self.report, result, getattr(self.report, result) + 1)

    def _prune(self) -> None:
        """Remove generated notes that are no longer produced — only if the user never touched them."""
        for path in sorted(self.root.rglob("*.md"), reverse=True):
            if path in self._written or path.name.endswith(".m2o-new.md") or "attachments" in path.parts:
                continue
            data, _, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            if not data or data.get("type") not in GENERATED_TYPES or not data.get("hash"):
                continue  # not ours
            if is_pristine(path):
                path.unlink()
                self.report.removed += 1
            else:
                self.report.kept_stale.append(str(path.relative_to(self.root)))
        for folder in sorted((p for p in self.root.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()

    def _unique(self, name: str, used: set[str]) -> str:
        candidate, n = name, 2
        while candidate.casefold() in used:
            candidate, n = f"{name} ({n})", n + 1
        used.add(candidate.casefold())
        return candidate

    def _moodle_link(self, url: Any, label: str = "Moodle") -> str | None:
        return safe_link(url, label) if str(url or "").startswith(self.site + "/") else None

    # --- course & topics ---------------------------------------------------------------------------------

    def _course(self, course: dict[str, Any]) -> str:
        cid = int(course["id"])
        code = course_code(course)
        title = course_title(course, self.language)
        tag = "course/" + (re.sub(r"[^a-z0-9]+", "-", code.lower()).strip("-") or str(cid))
        ctx = _Course(cid, code, title, note_name(title, 60), note_name(f"{title} (MOC)"), tag)
        base = {"course": title, "course_id": cid}
        modules = self.state.modules(cid)
        topics = []
        for section in course.get("sections") or []:
            num = int(section.get("section") or 0)
            name = str(section.get("name") or f"{self.t['topic']} {num}")
            items = [modules[m] for m in section.get("modules") or [] if m in modules]
            if not any(m.get("modname") in MATERIAL_MODULES for m in items):
                for module in items:  # tests/exams-only section: list its tasks in the MOC, no topic node
                    self._task(module, ctx, topic=None)
                continue
            note = self._unique(note_name(f"{code} {num:02d} {name}"), ctx.used_names)
            fields = {
                **base,
                "section": num,
                "type": "topic",
                "moodle_id": section.get("id"),
                "source_url": f"{self.site}/course/view.php?id={cid}#section-{num}",
                "tags": [ctx.tag, "type/topic"],
                "updated": self.today,
            }
            self._write([ctx.folder, self.t["topics"], f"{note}.md"], fields, self._topic(section, items, ctx, note))
            topics.append(f"- [[{note}]]")

        moc = [f"# {md_text(title)}", f"[[{DASHBOARD}]]", f"## {self.t['topics']}", "\n".join(topics) or "—"]
        if ctx.tasks:
            moc += [f"## {self.t['tasks_and_tests']}", "\n".join(ctx.tasks)]
        fields = {
            **base,
            "type": "moc",
            "source_url": f"{self.site}/course/view.php?id={cid}",
            "tags": [ctx.tag, "type/moc"],
            "updated": self.today,
        }
        self._write([ctx.folder, f"{ctx.moc}.md"], fields, "\n\n".join(moc))
        return ctx.moc

    def _topic(self, section: dict[str, Any], items: list[dict[str, Any]], ctx: _Course, note: str) -> str:
        intro = [html_to_markdown(str(section.get("summary") or ""))]
        files: list[str] = []
        videos: list[str] = []
        links: list[str] = []
        pages: list[str] = []
        discussions: list[str] = []
        activities: list[str] = []
        tasks: list[str] = []
        for module in items:
            modname = str(module.get("modname") or "")
            name = str(module.get("name") or modname)
            module_files = self.state.files_for_module(int(module["id"]))
            if modname == "label":
                intro.append(html_to_markdown(str(module.get("description") or "")))
            elif modname in ("resource", "folder"):
                attached = self._attachments(module_files, ctx)
                if attached:
                    files.append(f"- **{md_text(name)}**: " + ", ".join(attached))
            elif modname in ("page", "book"):
                text = "\n\n".join(
                    p
                    for p in (
                        html_to_markdown(str(module.get("description") or "")),
                        self._pages(module, module_files, ctx),
                    )
                    if p
                )
                if text:
                    pages.append(callout("note", name, text))
            elif modname == "url":
                for content in module.get("contents") or []:
                    link = safe_link(content.get("fileurl"), name) if content.get("type") == "url" else None
                    if link:
                        is_video = any(host in str(content.get("fileurl")) for host in VIDEO_HOSTS)
                        (videos if is_video else links).append(f"- {link}")
            elif modname == "forum":
                posts = self._forum(module)
                if posts:
                    discussions.append(callout("quote", name, posts))
            elif modname == "assign":
                tasks.append(self._task(module, ctx, topic=note, files=module_files))
            else:  # quiz, lti, feedback, h5p, …
                link = self._moodle_link(module.get("url"), name)
                if link:
                    activities.append(f"- {link}")

        # videos embedded in pages, labels and the topic summary also belong in the video list (once each)
        listed = {m.group(1) for m in re.finditer(r"\(<([^>]+)>\)", "\n".join(videos))}
        for url in dict.fromkeys(re.findall(r"https?://[^\s)<>\]\"']+", "\n".join(intro + pages))):
            if url not in listed and any(host in url for host in VIDEO_HOSTS) and (link := safe_link(url, url)):
                videos.append(f"- {link}")
                listed.add(url)
        materials = "\n\n".join(
            part
            for part in (
                _section(self.t["files"], ["\n".join(files)]),
                _section(self.t["videos"], ["\n".join(videos)]),
                _section(self.t["links"], ["\n".join(links)]),
                _section(self.t["pages"], pages),
                _section(self.t["discussions"], discussions),
                _section(self.t["activities"], ["\n".join(activities)]),
            )
            if part
        )
        parts = [
            f"# {md_text(section.get('name') or note)}",
            f"{self.t['course']}: [[{ctx.moc}]]",
            "\n\n".join(p for p in intro if p),
            *self._summary(section, note),
            _section(self.t["materials"], [materials], level=2),
            _section(self.t["tasks"], tasks, level=2),
        ]
        return "\n\n".join(p for p in parts if p)

    def _summary(self, section: dict[str, Any], note: str) -> list[str]:
        """Summary + self-check + key concepts from the LLM stage (or a placeholder until it has run)."""
        notes = load_topic_notes(self.notes_dir, section.get("id"))
        if notes is None:
            return [f"## {self.t['summary']}\n\n{self.t['summary_pending']}"]
        summary = sanitize_llm_markdown(notes.summary)
        questions = [f"- {sanitize_llm_markdown(q)}" for q in notes.self_check if q.strip()]
        if questions:
            summary += f"\n\n### {self.t['self_check']}\n\n" + "\n".join(questions)
        links = []
        for concept in notes.concepts:
            name = self._register_concept(concept, note)
            if name:
                links.append(f"- [[{name}]]")
        out = [f"## {self.t['summary']}\n\n{summary}"]
        if links:
            out.append(f"## {self.t['concepts']}\n\n" + "\n".join(dict.fromkeys(links)))
        return out

    def _register_concept(self, concept: Any, topic: str) -> str | None:
        key = concept_key(concept.en_key) or concept_key(concept.name)
        proposed = note_name(md_text(concept.name), 80)
        if not key or proposed == "_":
            return None
        if key not in self._concepts:
            name = self.state.concept_name(key, proposed)
            taken = {str(e["name"]).casefold() for e in self._concepts.values()}
            if name.casefold() in taken:  # two different concepts with the same display name
                name = note_name(f"{name} ({key})", 100)
            self._concepts[key] = {"name": name, "aliases": set(), "definition": "", "related": set(), "topics": []}
        entry = self._concepts[key]
        if proposed != entry["name"]:
            entry["aliases"].add(proposed)
        if len(concept.definition) > len(entry["definition"]):
            entry["definition"] = concept.definition
        entry["related"].update(concept_key(r) for r in concept.related)
        if topic not in entry["topics"]:
            entry["topics"].append(topic)
        return str(entry["name"])

    def _concept_notes(self) -> None:
        """One note per concept across all courses: definition, related concepts, where it appears."""
        for key in sorted(self._concepts):
            entry = self._concepts[key]
            name = str(entry["name"])
            related = sorted({self._concepts[r]["name"] for r in entry["related"] if r in self._concepts and r != key})
            body = [f"# {md_text(name)}", sanitize_llm_markdown(str(entry["definition"]))]
            if related:
                body.append(f"## {self.t['related']}\n\n" + "\n".join(f"- [[{r}]]" for r in related))
            body.append(f"## {self.t['mentioned']}\n\n" + "\n".join(f"- [[{t}]]" for t in entry["topics"]))
            fields = {
                "type": "concept",
                "concept_key": key,
                "aliases": sorted(entry["aliases"]),
                "tags": ["type/concept"],
                "updated": self.today,
            }
            self._write([self.t["concepts_dir"], f"{name}.md"], fields, "\n\n".join(b for b in body if b))

    def _task(self, module: dict[str, Any], ctx: _Course, topic: str | None, files: list[Any] | None = None) -> str:
        """An assignment (or a test in a tests-only section): text inside the topic + a plain line in the MOC."""
        modname = str(module.get("modname") or "")
        data = module.get("assignment") or {}
        name = str(data.get("name") or module.get("name") or modname)
        due = int(data.get("duedate") or 0)
        due_text = _when(due) if due else self.t["no_deadline"]
        moodle = self._moodle_link(module.get("url"))
        where = f" · [[{topic}]]" if topic else ""
        if modname == "assign" or topic is None:
            line = f"- {due_text} — {md_text(name)}{where}" + (f" · {moodle}" if moodle else "")
            if modname == "assign" or moodle:
                ctx.tasks.append(line)
        if due:
            self._deadlines.append((due, f"{md_text(name)}{where}", ctx.code))
        if topic is None:
            return ""
        # Dataview reads `[deadline:: …]` on list items; the dashboard queries them
        field_ = f"[deadline:: {_when(due, iso=True)}] " if due else f"({self.t['no_deadline']}) "
        head = f"- {field_}**{md_text(name)}**" + (f" · {moodle}" if moodle else "")
        details = "\n\n".join(
            p
            for p in (
                html_to_markdown(str(data.get("intro") or module.get("description") or "")),
                ", ".join(self._attachments(files or [], ctx)),
            )
            if p
        )
        return head + (f"\n\n{callout('todo', self.t['condition'], details)}" if details else "")

    def _forum(self, module: dict[str, Any]) -> str:
        posts = []
        for discussion in (module.get("forum") or {}).get("discussions") or []:
            stamp = int(discussion.get("created") or discussion.get("timemodified") or 0)
            title = md_text(discussion.get("name") or discussion.get("subject"))
            heading = f"**{title}** ({_when(stamp)})" if stamp else f"**{title}**"
            posts.append(f"{heading}\n\n{html_to_markdown(str(discussion.get('message') or ''))}")
        return "\n\n".join(posts)

    def _pages(self, module: dict[str, Any], files: list[Any], ctx: _Course) -> str:
        """The HTML of a page or the chapters of a book, with images pointing at attachments/."""
        contents = module.get("contents") or []
        order = {f"{module['id']}:{c.get('filepath') or '/'}{c.get('filename')}": i for i, c in enumerate(contents)}
        titles: dict[str, str] = {}  # book chapter href → title, in table-of-contents order
        for content in contents:
            if content.get("filename") == "structure" and content.get("content"):
                try:
                    for chapter in json.loads(content["content"]):
                        titles[str(chapter.get("href", "")).lstrip("/")] = str(chapter.get("title", ""))
                except (ValueError, AttributeError, TypeError):
                    pass
        toc = {href: i for i, href in enumerate(titles)}
        images = {
            f["filename"]: name
            for f in files
            if str(f["mimetype"] or "").startswith("image/") and (name := self._attach(f, ctx)) is not None
        }

        def rank(f: Any) -> tuple[int, int]:
            return toc.get(str(f["key"]).split(":", 1)[1].lstrip("/"), 1_000_000), order.get(f["key"], 1_000_000)

        out = []
        for page in sorted((f for f in files if str(f["filename"]).lower().endswith((".html", ".htm"))), key=rank):
            try:
                markdown = html_to_markdown(decode(Path(page["local_path"]).read_bytes()))
            except OSError:
                continue
            title = titles.get(str(page["key"]).split(":", 1)[1].lstrip("/"))
            out.append((f"#### {md_text(title)}\n\n" if title else "") + _rewrite_images(markdown, images))
        return "\n\n".join(out)

    def _attachments(self, files: list[Any], ctx: _Course) -> list[str]:
        links = []
        for f in files:
            mimetype = str(f["mimetype"] or "")
            if mimetype.startswith(("video/", "audio/")) or str(f["filename"]).lower().endswith((".html", ".htm")):
                continue  # pages are inlined; media is transcribed in stage 6
            name = self._attach(f, ctx)
            if name:
                links.append(f"![[{name}]]" if mimetype.startswith("image/") else f"[[{name}]]")
        return links

    def _attach(self, row: Any, ctx: _Course) -> str | None:
        src = Path(row["local_path"])
        if not src.is_file():
            return None
        original = Path(str(row["filename"]))
        name = note_name(f"{original.stem}-{str(row['sha256'])[:8]}{original.suffix.lower()}")
        dest = safe_join(self.root, "attachments", ctx.folder, name)
        if not (dest.is_file() and dest.stat().st_size == src.stat().st_size):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.unlink(missing_ok=True)
            try:
                os.link(src, dest)  # same volume: no extra disk space
            except OSError:
                shutil.copy2(src, dest)
            self.report.attachments += 1
        return name

    def _dashboard(self, mocs: list[str]) -> None:
        now = time.time()
        upcoming = sorted(d for d in self._deadlines if d[0] >= now)[:20]
        deadline_lines = [f"- {_when(ts)} — {text} ({md_text(code)})" for ts, text, code in upcoming]
        dataview = (
            "```dataview\n"
            f'TABLE WITHOUT ID L.text AS "{self.t["tasks"]}", L.deadline AS "{self.t["deadline"]}", '
            f'file.link AS "{self.t["topic"]}"\n'
            f'FROM "{self.subdir}"\n'
            "FLATTEN file.lists AS L\n"
            "WHERE L.deadline AND date(L.deadline) >= date(today)\n"
            "SORT L.deadline ASC\n"
            "```"
        )
        block = [
            "# Moodle",
            f"## {self.t['courses']}",
            "\n".join(f"- [[{moc}]]" for moc in mocs) or "—",
            f"## {self.t['deadlines']}",
            "\n".join(deadline_lines) or "—",
            dataview,
        ]
        fields = {"type": "dashboard", "tags": ["type/dashboard"], "updated": self.today}
        self._write([f"{DASHBOARD}.md"], fields, "\n\n".join(block))
