"""Summarise each topic with the LLM: all of its materials in, a validated summary + key concepts out.

A topic is re-summarised only when its input changes (materials, prompt, model); results are cached as JSON
in <data>/notes/<section id>.json and tracked in the `stages` table. Budgets cap topics and tokens per run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import Settings, config_dir
from ..extract.formats import decode, html_to_markdown
from ..fsutil import secure_mkdir
from ..llm import Backend, LLMError, make_backend
from ..state.db import State

if TYPE_CHECKING:
    from ..config import LLMConfig

log = logging.getLogger(__name__)
VERSION = 1  # bump to re-summarise everything after changing the pipeline
STAGE = "summarized"
LANGUAGE_NAMES = {"uk": "Ukrainian", "en": "English"}
MATERIAL_MODULES = {"resource", "folder", "page", "book", "url"}  # a section with none of these is not a topic
CHARS_PER_TOKEN = 3.5  # rough estimate for --dry-run


class Concept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    en_key: str = Field(min_length=1, max_length=120)
    definition: str = Field(max_length=2000)
    related: list[str] = Field(default_factory=list, max_length=30)


class TopicNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=80_000)
    self_check: list[str] = Field(default_factory=list, max_length=20)
    concepts: list[Concept] = Field(default_factory=list, max_length=25)


_STRINGS = {"type": "array", "items": {"type": "string"}}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "self_check": _STRINGS,
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "en_key": {"type": "string"},
                    "definition": {"type": "string"},
                    "related": _STRINGS,
                },
                "required": ["name", "en_key", "definition", "related"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "self_check", "concepts"],
    "additionalProperties": False,
}


@dataclass
class TopicJob:
    section_id: int
    course: str
    topic: str
    sources: list[tuple[str, str, str]]  # (title, kind, text)
    input_hash: str

    @property
    def item(self) -> str:
        return f"topic:{self.section_id}"

    @property
    def chars(self) -> int:
        return sum(len(text) for _, _, text in self.sources)


@dataclass
class SummaryReport:
    done: int = 0
    cached: int = 0
    pending_left: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: str | None = None


def load_prompt(language: str) -> str:
    override = config_dir() / "prompts" / "topic_summary.md"
    path = override if override.is_file() else Path(__file__).parent.parent / "prompts" / "topic_summary.md"
    return path.read_text(encoding="utf-8").replace("{language}", LANGUAGE_NAMES[language])


def _escape(text: str) -> str:
    # a document must not be able to close our delimiters and smuggle text outside them
    return re.sub(r"(?i)</\s*(materials|source)", r"<\\/\1", text)


def build_user_message(job: TopicJob) -> str:
    parts = [f"Course: {job.course}", f"Topic: {job.topic}", "", "<materials>"]
    for title, kind, text in job.sources:
        safe_title = re.sub(r"[\"<>\n]", " ", title)
        parts.append(f'<source type="{kind}" title="{safe_title}">\n{_escape(text)}\n</source>')
    parts.append("</materials>")
    return "\n".join(parts)


def topic_sources(
    state: State, section: dict[str, Any], items: list[dict[str, Any]], extracted_dir: Path
) -> list[tuple[str, str, str]]:
    """Everything a student would study in this topic, as plain text."""
    sources: list[tuple[str, str, str]] = []
    summary = html_to_markdown(str(section.get("summary") or ""))
    if summary:
        sources.append(("Topic description", "description", summary))
    seen: set[str] = set()
    for module in items:
        name = str(module.get("name") or module.get("modname") or "")
        if module.get("description"):
            sources.append((name, "description", html_to_markdown(str(module["description"]))))
        intro = (module.get("assignment") or {}).get("intro")
        if intro:
            sources.append((name, "assignment", html_to_markdown(str(intro))))
        for row in state.files_for_module(int(module["id"])):
            sha = str(row["sha256"])
            if sha in seen:
                continue
            seen.add(sha)
            filename = str(row["filename"])
            if filename.lower().endswith((".html", ".htm")):
                try:
                    text = html_to_markdown(decode(Path(row["local_path"]).read_bytes()))
                except OSError:
                    continue
                kind = "page"
            else:
                extracted = extracted_dir / f"{sha}.md"
                if not extracted.is_file():
                    continue
                text = extracted.read_text(encoding="utf-8")
                kind = Path(filename).suffix.lstrip(".").lower() or "file"
            if text.strip():
                sources.append((f"{name} — {filename}", kind, text))
    return [s for s in sources if s[2].strip()]


def _fit(sources: list[tuple[str, str, str]], limit: int, topic: str) -> list[tuple[str, str, str]]:
    total = sum(len(text) for _, _, text in sources)
    if total <= limit:
        return sources
    log.warning(
        "тема «%s»: материалы %d символов > лимита %d — каждый источник сокращён пропорционально", topic, total, limit
    )
    marker = "\n[… truncated to fit llm.max_input_chars …]"
    return [(t, k, text[: max(200, int(len(text) * limit / total))] + marker) for t, k, text in sources]


def topic_jobs(state: State, settings: Settings) -> list[TopicJob]:
    from ..vault.writer import course_title  # local import: the writer imports this module

    prompt = load_prompt(settings.language)
    jobs = []
    for course in state.courses():
        title = course_title(course, settings.language)
        modules = state.modules(int(course["id"]))
        for section in course.get("sections") or []:
            items = [modules[m] for m in section.get("modules") or [] if m in modules]
            if not any(m.get("modname") in MATERIAL_MODULES for m in items):
                continue
            name = str(section.get("name") or f"Topic {section.get('section')}")
            sources = _fit(
                topic_sources(state, section, items, settings.extracted_dir), settings.llm.max_input_chars, name
            )
            if not sources:
                continue
            digest = hashlib.sha256(
                json.dumps(
                    [VERSION, settings.llm.backend, settings.llm.model, prompt, title, name, sources],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            jobs.append(TopicJob(int(section["id"]), title, name, sources, digest))
    return jobs


def _fresh(state: State, job: TopicJob, out_dir: Path) -> bool:
    row = state.get_stage(job.item, STAGE)
    if row is None or row["status"] != "ok" or not (out_dir / f"{job.section_id}.json").is_file():
        return False
    try:
        return bool(json.loads(row["info"] or "{}").get("hash") == job.input_hash)
    except ValueError:
        return False


def pending_jobs(state: State, settings: Settings) -> list[TopicJob]:
    return [job for job in topic_jobs(state, settings) if not _fresh(state, job, settings.notes_dir)]


def estimate_tokens(jobs: list[TopicJob]) -> int:
    return int(sum(job.chars for job in jobs) / CHARS_PER_TOKEN)


def load_topic_notes(notes_dir: Path, section_id: Any) -> TopicNotes | None:
    path = notes_dir / f"{section_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TopicNotes.model_validate({k: data[k] for k in ("summary", "self_check", "concepts")})
    except (ValueError, KeyError, ValidationError):
        log.warning("повреждён файл конспекта %s — будет пересоздан", path.name)
        return None


def summarize(
    state: State,
    settings: Settings,
    *,
    backend_factory: Callable[[LLMConfig], Backend] = make_backend,
    on_event: Callable[[str], None] = log.info,
) -> SummaryReport:
    out_dir = secure_mkdir(settings.notes_dir)
    jobs = topic_jobs(state, settings)
    todo = [job for job in jobs if not _fresh(state, job, out_dir)]
    report = SummaryReport(cached=len(jobs) - len(todo))
    if not todo:
        return report
    try:
        backend = backend_factory(settings.llm)
    except LLMError as exc:
        report.stopped = exc.en
        report.pending_left = len(todo)
        return report
    system = load_prompt(settings.language)
    budget = settings.llm
    for job in todo:
        if report.done + len(report.failed) >= budget.max_topics_per_run:
            report.stopped = f"max_topics_per_run={budget.max_topics_per_run}"
            break
        if report.input_tokens + report.output_tokens >= budget.max_tokens_per_run:
            report.stopped = f"max_tokens_per_run={budget.max_tokens_per_run}"
            break
        try:
            result = backend.complete_json(system, build_user_message(job), SCHEMA)
            notes = TopicNotes.model_validate(result.data)
        except LLMError as exc:
            if exc.fatal:
                report.stopped = exc.en
                break
            report.failed.append((job.topic, exc.en))
            state.set_stage(job.item, STAGE, "failed", VERSION, json.dumps({"error": exc.en}))
            continue
        except ValidationError as exc:
            reason = f"response does not match the schema ({exc.error_count()} errors)"
            report.failed.append((job.topic, reason))
            state.set_stage(job.item, STAGE, "failed", VERSION, json.dumps({"error": reason}))
            continue
        report.input_tokens += result.input_tokens
        report.output_tokens += result.output_tokens
        report.cost_usd += result.cost_usd or 0.0
        payload = {"topic": job.topic, "course": job.course, "model": budget.model, "input_hash": job.input_hash}
        (out_dir / f"{job.section_id}.json").write_text(
            json.dumps({**payload, **notes.model_dump()}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        state.set_stage(job.item, STAGE, "ok", VERSION, json.dumps({"hash": job.input_hash}))
        state.commit()
        report.done += 1
        on_event(f"✎ {job.course} · {job.topic}")
    report.pending_left = len(todo) - report.done - len(report.failed)
    return report
