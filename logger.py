"""Minimal run logger: timestamped lines to stdout and output/run.log."""
from __future__ import annotations

import datetime
from pathlib import Path

_log_path: Path | None = None
warnings_seen: list[str] = []


def init_logger(output_dir: Path) -> None:
    global _log_path
    output_dir.mkdir(parents=True, exist_ok=True)
    _log_path = output_dir / "run.log"
    _log_path.write_text("", encoding="utf-8")
    warnings_seen.clear()


def log(message: str, level: str = "INFO") -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {message}"
    print(line, flush=True)
    if level in ("WARNING", "ERROR"):
        warnings_seen.append(message)
    if _log_path is not None:
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def step(number: int, total: int, message: str) -> None:
    log(f"[{number}/{total}] {message}")
