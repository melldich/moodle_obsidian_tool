"""Pre-commit guard: block secrets, session files, local state and downloaded course materials."""

from __future__ import annotations

import re
import sys

ALLOWED_NAMES = {".env.example", "config.example.yaml"}
FORBIDDEN = re.compile(
    r"(^|/)(downloads|cache|logs|playwright/\.auth)/"
    r"|^(vault|attachments)/"  # only at the repo root: src/moodle2obsidian/vault/ is code
    r"|(^|/)(\.env(\..*)?|config\.yaml|cookies[^/]*"
    r"|[^/]*\.(session|cookies|token|db|db-journal|sqlite|log|srt|vtt|mp4|mkv|webm|m4a|mp3|wav|opus))$",
    re.IGNORECASE,
)


def find_forbidden(paths: list[str]) -> list[str]:
    bad = []
    for raw in paths:
        path = raw.replace("\\", "/")
        if path.rsplit("/", 1)[-1] in ALLOWED_NAMES:
            continue
        if FORBIDDEN.search(path):
            bad.append(raw)
    return bad


def main(argv: list[str]) -> int:
    bad = find_forbidden(argv)
    for path in bad:
        print(f"refusing to commit {path}: secrets, state and course materials must stay local", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
