"""Collect courses, modules, assignments, forums and files into the state DB and downloads dir."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CoursesConfig, FetchConfig
from ..errors import M2OError
from ..fsutil import secure_mkdir
from ..moodle.client import MoodleClient, MoodleError
from ..safety import safe_join, sanitize_filename, split_moodle_path
from ..state.db import State
from .download import download_file

log = logging.getLogger(__name__)

# shortcut: short path components keep Windows paths under MAX_PATH (260); deep folders may still exceed it
_COURSE_LEN, _SECTION_LEN, _MODULE_LEN, _SUBDIR_LEN, _FILE_LEN = 30, 30, 40, 30, 80


def fetch_courses(client: MoodleClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    info: dict[str, Any] = client.call("core_webservice_get_site_info")
    courses: list[dict[str, Any]] = client.call("core_enrol_get_users_courses", userid=info["userid"])
    return info, courses


def available_functions(site_info: Mapping[str, Any]) -> set[str]:
    return {str(f.get("name")) for f in site_info.get("functions") or [] if isinstance(f, Mapping)}


def course_skip_reason(course: Mapping[str, Any], cfg: CoursesConfig, now: float) -> str | None:
    cid = str(course.get("id"))
    text = f"{course.get('fullname', '')} {course.get('shortname', '')}".casefold()

    def matches(patterns: list[str]) -> bool:
        return any(p == cid if p.isdigit() else p.casefold() in text for p in patterns)

    if cfg.include and not matches(cfg.include):
        return "не в include / not included"
    if matches(cfg.exclude):
        return "exclude"
    if cfg.skip_hidden and course.get("hidden"):
        return "скрыт / hidden"
    end = int(course.get("enddate") or 0)
    if cfg.skip_past and end and end < now:
        return "завершён / ended"
    return None


@dataclass
class FetchReport:
    courses: int = 0
    modules: int = 0
    changed_modules: int = 0
    downloaded: int = 0
    deduplicated: int = 0
    unchanged: int = 0
    skipped: int = 0
    bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)


class Collector:
    def __init__(
        self,
        client: MoodleClient,
        state: State,
        cfg: FetchConfig,
        downloads_dir: Path,
        available: set[str],
        *,
        dry_run: bool = False,
        no_video: bool = False,
        on_event: Callable[[str], None] = log.info,
    ) -> None:
        self.client = client
        self.state = state
        self.cfg = cfg
        # create before resolving, so root and every resolved destination agree (even under FS redirection)
        self.root = secure_mkdir(downloads_dir).resolve()
        self.available = available
        self.dry_run = dry_run
        self.no_video = no_video
        self.on_event = on_event
        self.report = FetchReport()
        self._warned: set[str] = set()
        self._claimed: dict[Path, str] = {}

    def _unique_dest(self, dest: Path, key: str) -> Path:
        """Two different Moodle files must never share a local path (truncated names can collide)."""
        owner = self._claimed.get(dest) or self.state.key_for_path(str(dest))
        if owner is not None and owner != key:
            tag = hashlib.sha256(key.encode()).hexdigest()[:8]
            dest = dest.with_name(f"{dest.stem}~{tag}{dest.suffix}")
        self._claimed[dest] = key
        return dest

    def optional(self, wsfunction: str, **params: Any) -> Any:
        """Call a function the tool can live without; degrade to None with a warning."""
        if wsfunction not in self.available:
            if wsfunction not in self._warned:
                self._warned.add(wsfunction)
                log.warning("%s недоступна на этом сайте — пропускаю / not available — skipped", wsfunction)
            return None
        try:
            return self.client.call(wsfunction, **params)
        except MoodleError as exc:
            log.warning("%s: %s — пропускаю / skipped", wsfunction, exc.errorcode)
            return None

    def run(self, courses: list[dict[str, Any]]) -> FetchReport:
        ids = [int(c["id"]) for c in courses]
        assignments = self._assignments(ids) if ids else {}
        forums = self._forums(ids) if ids else {}
        for course in courses:
            self._course(course, assignments, forums)
            self.state.commit()  # a crash later keeps what is done; the next run resumes from here
        return self.report

    def _assignments(self, course_ids: list[int]) -> dict[int, dict[str, Any]]:
        data = self.optional("mod_assign_get_assignments", courseids=course_ids) or {}
        return {int(a["cmid"]): a for c in data.get("courses") or [] for a in c.get("assignments") or [] if "cmid" in a}

    def _forums(self, course_ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for forum in self.optional("mod_forum_get_forums_by_courses", courseids=course_ids) or []:
            if "cmid" not in forum:
                continue
            discussions = self.optional("mod_forum_get_forum_discussions", forumid=forum["id"]) or {}
            out[int(forum["cmid"])] = {**forum, "discussions": discussions.get("discussions") or []}
        return out

    def _course(
        self, course: dict[str, Any], assignments: dict[int, dict[str, Any]], forums: dict[int, dict[str, Any]]
    ) -> None:
        cid = int(course["id"])
        name = str(course.get("shortname") or course.get("fullname") or cid)
        try:
            sections = self.client.call("core_course_get_contents", courseid=cid)
        except M2OError as exc:
            self.report.failed.append((name, exc.en))
            log.warning("курс %s: %s", name, exc.en)
            return
        self.report.courses += 1
        if not self.dry_run:
            self.state.upsert_course(course)
        course_dir = sanitize_filename(f"{cid} {name}", _COURSE_LEN)
        for section in sections:
            num = int(section.get("section") or 0)
            title = str(section.get("name") or f"Section {num}")
            section_dir = sanitize_filename(f"{num:02d} {title}", _SECTION_LEN)
            for module in section.get("modules") or []:
                self._module(cid, num, title, [course_dir, section_dir], module, assignments, forums)

    def _module(
        self,
        cid: int,
        section_num: int,
        section_name: str,
        dirs: list[str],
        module: dict[str, Any],
        assignments: dict[int, dict[str, Any]],
        forums: dict[int, dict[str, Any]],
    ) -> None:
        cmid = int(module["id"])
        if cmid in assignments:
            module = {**module, "assignment": assignments[cmid]}
        if cmid in forums:
            module = {**module, "forum": forums[cmid]}
        self.report.modules += 1
        if self.state.upsert_module(cid, section_num, section_name, module, write=not self.dry_run):
            self.report.changed_modules += 1

        files = [c for c in module.get("contents") or [] if c.get("type") == "file"]
        intro = module.get("assignment", {}).get("introattachments") or []
        files += [{**a, "filepath": "/intro/"} for a in intro]
        # cmid keeps the dir unique when long module names truncate to the same prefix
        module_dir = sanitize_filename(f"{cmid} {module.get('name') or ''}", _MODULE_LEN)
        for content in files:
            self._file(cid, cmid, [*dirs, module_dir], content)

    def _file(self, cid: int, cmid: int, dirs: list[str], content: dict[str, Any]) -> None:
        url = str(content.get("fileurl") or "")
        filename = str(content.get("filename") or "file")
        mimetype = str(content.get("mimetype") or "")
        if not url:
            return
        if self.no_video and mimetype.startswith("video/"):
            self.report.skipped += 1
            return
        filepath = str(content.get("filepath") or "/")
        key = f"{cmid}:{filepath}{filename}"
        size = int(content.get("filesize") or 0)
        timemodified = int(content.get("timemodified") or 0)

        row = self.state.get_file(key)
        if (
            row is not None
            and row["timemodified"] == timemodified
            and row["filesize"] == size
            and row["local_path"]
            and Path(row["local_path"]).is_file()
        ):
            self.report.unchanged += 1
            return

        max_bytes = self.cfg.max_file_mb * 1024 * 1024
        if size > max_bytes:
            self.report.skipped += 1
            log.warning("%s: больше %d МБ — пропускаю / too big — skipped", filename, self.cfg.max_file_mb)
            return
        try:
            subdirs = [sanitize_filename(p, _SUBDIR_LEN) for p in split_moodle_path(filepath)]
            dest = self._unique_dest(safe_join(self.root, *dirs, *subdirs, sanitize_filename(filename, _FILE_LEN)), key)
        except M2OError as exc:
            self.report.failed.append((filename, exc.en))
            return
        label = str(dest.relative_to(self.root))
        if self.dry_run:
            self.report.planned.append(label)
            return
        try:
            sha256, nbytes = download_file(
                self.client,
                url,
                dest,
                max_bytes=max_bytes,
                timeout=self.cfg.download_timeout_sec,
                expected_mimetype=mimetype,
            )
        except M2OError as exc:
            self.report.failed.append((label, exc.en))
            log.warning("%s: %s", label, exc.en)
            return

        existing = self.state.path_for_sha256(sha256)
        if existing and existing != str(dest) and Path(existing).is_file():
            dest.unlink()  # identical file already downloaded elsewhere
            local_path = existing
            self.report.deduplicated += 1
        else:
            local_path = str(dest)
            self.report.downloaded += 1
            self.report.bytes += nbytes
            self.on_event(f"↓ {label}")
        self.state.record_file(
            key=key,
            course_id=cid,
            cmid=cmid,
            filename=filename,
            mimetype=mimetype,
            filesize=size,
            timemodified=timemodified,
            local_path=local_path,
            sha256=sha256,
        )
