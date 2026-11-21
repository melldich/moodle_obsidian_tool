from __future__ import annotations

from moodle2obsidian.moodle.doctor import FUNCTIONS, REQUIRED, check_functions


def test_check_functions_marks_available_and_required() -> None:
    site_info = {"functions": [{"name": "core_webservice_get_site_info"}, {"name": "mod_page_get_pages_by_courses"}]}
    checks = {c.name: c for c in check_functions(site_info)}
    assert len(checks) == len(FUNCTIONS)
    assert checks["core_webservice_get_site_info"].available
    assert checks["mod_page_get_pages_by_courses"].available
    assert not checks["core_course_get_contents"].available
    assert checks["core_course_get_contents"].required
    assert not checks["mod_book_get_books_by_courses"].required
    assert {c.name for c in checks.values() if c.required} == REQUIRED


def test_check_functions_tolerates_missing_list() -> None:
    assert not any(c.available for c in check_functions({}))
