"""Extract text for every downloaded file that has no up-to-date Markdown yet (keyed by sha256)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ExtractConfig
from ..fsutil import secure_mkdir
from ..log import redact
from ..state.db import State

log = logging.getLogger(__name__)
EXTRACTOR_VERSION = 1  # bump to re-extract everything after improving a parser
STAGE = "extracted"
# images go to the vault as they are; video/audio is transcribed in stage 6
SKIP_MIME_PREFIXES = ("image/", "video/", "audio/")


@dataclass
class ExtractReport:
    extracted: int = 0
    unsupported: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def extract_pending(
    state: State, out_dir: Path, cfg: ExtractConfig, on_event: Callable[[str], None] = log.info
) -> ExtractReport:
    secure_mkdir(out_dir)
    options = json.dumps(
        {
            "ocr": cfg.ocr,
            "ocr_languages": cfg.ocr_languages,
            "max_zip_bytes": cfg.max_zip_mb * 1024 * 1024,
            "max_zip_members": cfg.max_zip_members,
        }
    )
    report = ExtractReport()
    for sha256, local_path, mimetype in state.files_needing_stage(STAGE, EXTRACTOR_VERSION):
        name = Path(local_path).name
        if (mimetype or "").startswith(SKIP_MIME_PREFIXES):
            status, info = "skipped", mimetype
            report.skipped += 1
        else:
            status, info = run_worker(Path(local_path), out_dir / f"{sha256}.md", options, cfg.timeout_sec)
            if status == "ok":
                report.extracted += 1
                on_event(f"≡ {name}")
            elif status == "unsupported":
                report.unsupported += 1
            else:
                report.failed.append((name, info))
                log.warning("%s: %s", name, info)
        state.set_stage(sha256, STAGE, status, EXTRACTOR_VERSION, info)
        state.commit()
    return report


def run_worker(src: Path, dst: Path, options: str, timeout: float) -> tuple[str, str]:
    # sys.executable is the project's own interpreter; fixed argv, no shell, paths come from our own state DB
    command = [sys.executable, "-m", "moodle2obsidian.extract.worker", str(src), str(dst), options]
    try:
        proc = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return "failed", f"timeout after {timeout:.0f}s"
    lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    reason = redact(lines[-1])[:300] if lines else ""
    if proc.returncode == 0:
        return "ok", reason
    if proc.returncode == 3:
        return "unsupported", reason
    return "failed", reason or f"exit code {proc.returncode}"
