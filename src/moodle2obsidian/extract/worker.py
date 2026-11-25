"""Isolated extraction process: `python -m moodle2obsidian.extract.worker SRC DST OPTIONS_JSON`.

A crash, hang or memory blow-up inside a parser on a broken file only kills this process, never the sync.
Exit codes: 0 = written, 3 = unsupported type, anything else = failed (reason on stderr).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .formats import ExtractError, Options, Unsupported, extract_bytes

EXIT_UNSUPPORTED = 3


def main(argv: list[str]) -> int:
    src, dst, raw_options = (Path(argv[0]), Path(argv[1]), argv[2])
    options = Options(**json.loads(raw_options))
    try:
        text = extract_bytes(src.name, src.read_bytes(), options)
    except Unsupported as exc:
        print(f"unsupported: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except ExtractError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_text(text + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, dst)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
