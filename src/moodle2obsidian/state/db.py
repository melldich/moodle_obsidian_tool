"""SQLite state: what was fetched (later: processed), with hashes and timemodified."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..fsutil import chmod_private, secure_mkdir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    shortname TEXT NOT NULL,
    fullname TEXT NOT NULL,
    data TEXT NOT NULL,
    synced_at INTEGER
);
CREATE TABLE IF NOT EXISTS modules (
    cmid INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL,
    section_num INTEGER,
    section_name TEXT,
    modname TEXT NOT NULL,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    hash TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    key TEXT PRIMARY KEY,
    course_id INTEGER NOT NULL,
    cmid INTEGER,
    filename TEXT NOT NULL,
    mimetype TEXT,
    filesize INTEGER,
    timemodified INTEGER,
    local_path TEXT,
    sha256 TEXT,
    downloaded_at INTEGER
);
CREATE INDEX IF NOT EXISTS files_sha256 ON files(sha256);
CREATE TABLE IF NOT EXISTS stages (
    item TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    info TEXT,
    done_at INTEGER NOT NULL,
    PRIMARY KEY (item, stage)
);
PRAGMA user_version = 2;
"""


def content_hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class State:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        if readonly:  # status/inspection while a sync may be writing: never touch the schema or journal
            self.conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
            self.conn.row_factory = sqlite3.Row
            return
        secure_mkdir(path.parent)
        self.conn = sqlite3.connect(path, timeout=30)
        chmod_private(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        # WAL: readers never block or roll back the writer's in-progress transaction
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)

    def __enter__(self) -> State:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def upsert_course(self, course: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO courses (id, shortname, fullname, data, synced_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET shortname=excluded.shortname, fullname=excluded.fullname, "
            "data=excluded.data, synced_at=excluded.synced_at",
            (
                int(course["id"]),
                str(course.get("shortname", "")),
                str(course.get("fullname", "")),
                json.dumps(course, ensure_ascii=False),
                int(time.time()),
            ),
        )

    def upsert_module(
        self, course_id: int, section_num: int, section_name: str, module: Mapping[str, Any], *, write: bool = True
    ) -> bool:
        """Store a module; returns True if it is new or its content changed."""
        digest = content_hash(module)
        row = self.conn.execute("SELECT hash FROM modules WHERE cmid = ?", (int(module["id"]),)).fetchone()
        if row is not None and row["hash"] == digest:
            return False
        if write:
            self.conn.execute(
                "INSERT INTO modules (cmid, course_id, section_num, section_name, modname, name, data, hash, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(cmid) DO UPDATE SET "
                "course_id=excluded.course_id, section_num=excluded.section_num, "
                "section_name=excluded.section_name, modname=excluded.modname, name=excluded.name, "
                "data=excluded.data, hash=excluded.hash, updated_at=excluded.updated_at",
                (
                    int(module["id"]),
                    course_id,
                    section_num,
                    section_name,
                    str(module.get("modname", "")),
                    str(module.get("name", "")),
                    json.dumps(module, ensure_ascii=False),
                    digest,
                    int(time.time()),
                ),
            )
        return True

    def get_file(self, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute("SELECT * FROM files WHERE key = ?", (key,)).fetchone()
        return row

    def set_stage(self, item: str, stage: str, status: str, version: int, info: str = "") -> None:
        self.conn.execute(
            "INSERT INTO stages (item, stage, status, version, info, done_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item, stage) DO UPDATE SET status=excluded.status, version=excluded.version, "
            "info=excluded.info, done_at=excluded.done_at",
            (item, stage, status, version, info, int(time.time())),
        )

    def files_needing_stage(self, stage: str, version: int) -> list[tuple[str, str, str]]:
        """Distinct downloaded files (sha256, local_path, mimetype) without an up-to-date `stage` result."""
        rows = self.conn.execute(
            "SELECT f.sha256, MIN(f.local_path), MIN(COALESCE(f.mimetype, '')) FROM files f "
            "LEFT JOIN stages s ON s.item = f.sha256 AND s.stage = ? "
            "WHERE f.sha256 IS NOT NULL AND f.local_path IS NOT NULL AND (s.item IS NULL OR s.version < ?) "
            "GROUP BY f.sha256",
            (stage, version),
        ).fetchall()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    def _count_stage(self, stage: str) -> int:
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM stages WHERE stage = ? AND status = 'ok'", (stage,)
            ).fetchone()
        except sqlite3.OperationalError:  # read-only open of a DB from before the stages table existed
            return 0
        return int(row[0])

    def courses(self) -> list[dict[str, Any]]:
        return [json.loads(r["data"]) for r in self.conn.execute("SELECT data FROM courses ORDER BY id")]

    def modules(self, course_id: int) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute("SELECT cmid, data FROM modules WHERE course_id = ?", (course_id,))
        return {int(r["cmid"]): json.loads(r["data"]) for r in rows}

    def files_for_module(self, cmid: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM files WHERE cmid = ? AND local_path IS NOT NULL ORDER BY key", (cmid,)
            ).fetchall()
        )

    def key_for_path(self, local_path: str) -> str | None:
        row = self.conn.execute("SELECT key FROM files WHERE local_path = ? LIMIT 1", (local_path,)).fetchone()
        return str(row["key"]) if row else None

    def path_for_sha256(self, sha256: str) -> str | None:
        row = self.conn.execute(
            "SELECT local_path FROM files WHERE sha256 = ? AND local_path IS NOT NULL LIMIT 1", (sha256,)
        ).fetchone()
        return str(row["local_path"]) if row else None

    def record_file(
        self,
        *,
        key: str,
        course_id: int,
        cmid: int,
        filename: str,
        mimetype: str,
        filesize: int,
        timemodified: int,
        local_path: str,
        sha256: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO files (key, course_id, cmid, filename, mimetype, filesize, timemodified, local_path, "
            "sha256, downloaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "course_id=excluded.course_id, cmid=excluded.cmid, filename=excluded.filename, "
            "mimetype=excluded.mimetype, filesize=excluded.filesize, timemodified=excluded.timemodified, "
            "local_path=excluded.local_path, sha256=excluded.sha256, downloaded_at=excluded.downloaded_at",
            (key, course_id, cmid, filename, mimetype, filesize, timemodified, local_path, sha256, int(time.time())),
        )

    def stats(self) -> dict[str, int]:
        one = self.conn.execute
        return {
            "courses": int(one("SELECT COUNT(*) FROM courses").fetchone()[0]),
            "modules": int(one("SELECT COUNT(*) FROM modules").fetchone()[0]),
            "files": int(one("SELECT COUNT(*) FROM files WHERE local_path IS NOT NULL").fetchone()[0]),
            "bytes": int(
                one("SELECT COALESCE(SUM(filesize), 0) FROM files WHERE local_path IS NOT NULL").fetchone()[0]
            ),
            "last_sync": int(one("SELECT COALESCE(MAX(synced_at), 0) FROM courses").fetchone()[0]),
            "extracted": self._count_stage("extracted"),
        }
