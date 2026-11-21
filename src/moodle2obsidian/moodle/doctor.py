"""What does this Moodle site allow? Compares site_info.functions with what the tool uses."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# (wsfunction, purpose) — order is the order shown in `m2o doctor`
FUNCTIONS: list[tuple[str, str]] = [
    ("core_webservice_get_site_info", "информация о сайте / site info"),
    ("core_enrol_get_users_courses", "список курсов / course list"),
    ("core_course_get_contents", "содержимое курса / course contents"),
    ("mod_resource_get_resources_by_courses", "файлы / files"),
    ("mod_page_get_pages_by_courses", "страницы / pages"),
    ("mod_url_get_urls_by_courses", "ссылки / links"),
    ("mod_assign_get_assignments", "задания / assignments"),
    ("mod_forum_get_forums_by_courses", "форумы / forums"),
    ("mod_forum_get_forum_discussions", "обсуждения / discussions"),
    ("mod_book_get_books_by_courses", "книги / books"),
    ("mod_folder_get_folders_by_courses", "папки / folders"),
    ("mod_label_get_labels_by_courses", "метки / labels"),
]
REQUIRED = {"core_webservice_get_site_info", "core_enrol_get_users_courses", "core_course_get_contents"}

EXTERNAL_TOOLS: dict[str, str] = {
    "ffmpeg": "аудио из видео / audio extraction",
    "yt-dlp": "скачивание видео / video download",
    "tesseract": "OCR сканов / OCR",
    "claude": "LLM-бэкенд claude_code",
}


@dataclass(frozen=True)
class FunctionCheck:
    name: str
    purpose: str
    available: bool
    required: bool


def check_functions(site_info: Mapping[str, Any]) -> list[FunctionCheck]:
    available = {f.get("name") for f in site_info.get("functions") or [] if isinstance(f, Mapping)}
    return [FunctionCheck(name, purpose, name in available, name in REQUIRED) for name, purpose in FUNCTIONS]


def check_tools() -> dict[str, str | None]:
    return {name: shutil.which(name) for name in EXTERNAL_TOOLS}
