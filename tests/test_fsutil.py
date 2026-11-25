from __future__ import annotations

from pathlib import Path

import pytest

from moodle2obsidian.errors import M2OError
from moodle2obsidian.fsutil import exclusive_lock


def test_second_sync_is_refused_and_lock_is_released(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    with exclusive_lock(lock), pytest.raises(M2OError, match="already running"), exclusive_lock(lock):
        pass
    with exclusive_lock(lock):  # released after the first holder exits
        pass
