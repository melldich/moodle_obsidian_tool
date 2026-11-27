"""Safe note writing: generated text lives between markers; everything the user wrote is kept.

- Text outside `<!-- m2o:start -->` / `<!-- m2o:end -->` is never touched.
- Frontmatter keys the user added survive; ours are refreshed.
- If the user edited *inside* the markers, the note is left alone and the new version goes to `<name>.m2o-new.md`.
- Unchanged content is not rewritten (no mtime churn, no sync noise).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml

START = "<!-- m2o:start -->"
END = "<!-- m2o:end -->"
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)

Result = Literal["created", "updated", "unchanged", "conflict"]


def block_hash(block: str) -> str:
    return hashlib.sha256(block.strip().replace("\r\n", "\n").encode()).hexdigest()[:16]


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str]:
    """(parsed frontmatter or None if absent/invalid, raw frontmatter text, body)."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, "", text
    raw = match.group(1)
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return None, raw, text[match.end() :]
    return (data if isinstance(data, dict) else None), raw, text[match.end() :]


def _render(frontmatter: dict[str, Any] | str, body: str) -> str:
    raw = (
        frontmatter
        if isinstance(frontmatter, str)
        else yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    )
    return f"---\n{raw.strip()}\n---\n{body.lstrip()}"


def _wrap(block: str) -> str:
    return f"{START}\n{block.strip()}\n{END}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".m2o-tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def is_pristine(path: Path) -> bool:
    """True if the note is exactly what we generated: no user text outside the markers, no edits inside."""
    data, _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    body = body.strip()
    if not data or not data.get("hash") or not (body.startswith(START) and body.endswith(END)):
        return False
    return block_hash(body[len(START) : -len(END)]) == data["hash"]


def write_note(path: Path, fields: dict[str, Any], block: str) -> Result:
    digest = block_hash(block)
    fields = {**fields, "hash": digest}
    if not path.exists():
        _atomic_write(path, _render(fields, _wrap(block) + "\n"))
        return "created"

    data, raw_frontmatter, body = split_frontmatter(path.read_text(encoding="utf-8"))
    start, end = body.find(START), body.find(END)
    if start == -1 or end < start:
        # the user removed our markers: keep all of their text and add the generated block below it
        new_body = body.rstrip() + "\n\n" + _wrap(block) + "\n"
    else:
        old_block = body[start + len(START) : end]
        if block_hash(old_block) == digest:
            return "unchanged"
        if data and data.get("hash") and block_hash(old_block) != data["hash"]:
            # edited inside the generated block by hand: never overwrite those edits
            _atomic_write(path.with_name(path.stem + ".m2o-new.md"), _render(fields, _wrap(block) + "\n"))
            return "conflict"
        new_body = body[:start] + _wrap(block) + body[end + len(END) :]

    # invalid user YAML is kept verbatim rather than "fixed"
    frontmatter: dict[str, Any] | str = {**data, **fields} if data is not None else raw_frontmatter
    _atomic_write(path, _render(frontmatter, new_body))
    return "updated"
