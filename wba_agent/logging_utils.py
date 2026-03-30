"""Logging helpers shared by the collector agent components."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DEFAULT_LOG_DIR = Path("logs")
MAX_LOG_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
LOG_RETENTION_DAYS = 365


def timestamp(debug: bool = False) -> str:
    """Return formatted timestamp (UTC + local) with optional millisecond precision."""

    now = datetime.now(timezone.utc)
    local = datetime.now()
    if debug:
        return f"{now.isoformat(timespec='milliseconds')}Z ({local.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]} {local.strftime('%Z')})"
    return f"{now.isoformat()}Z ({local.strftime('%Y-%m-%dT%H:%M:%S %Z')})"


@dataclass
class LogRotator:
    """Manage log rotation and retention for a single site."""

    site_name: str
    custom_dir: Optional[Path] = None
    max_size_bytes: int = MAX_LOG_SIZE_BYTES
    retention_days: int = LOG_RETENTION_DAYS

    def __post_init__(self) -> None:
        base_dir = self.custom_dir or DEFAULT_LOG_DIR
        self.site_dir = base_dir / self.site_name
        self.site_dir.mkdir(parents=True, exist_ok=True)
        self._current_file: Optional[Path] = None
        self._current_date: Optional[str] = None
        self._rollover_count = 0

    def _resolve_log_file(self) -> Path:
        today = datetime.now(timezone.utc).date().isoformat()

        if self._current_date != today:
            self._current_date = today
            self._rollover_count = 0
            self._current_file = self.site_dir / f"{today}_{self.site_name}.log"

        assert self._current_file is not None  # for type checkers

        if self._current_file.exists() and self._current_file.stat().st_size >= self.max_size_bytes:
            self._rollover_count += 1
            self._current_file = self.site_dir / f"{today}_{self.site_name}_{self._rollover_count:03d}.log"

        return self._current_file

    def write_line(self, message: str) -> None:
        log_file = self._resolve_log_file()
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")

    def cleanup(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - (self.retention_days * 86400)
        for path in self.site_dir.glob("*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                # Best effort; report via structured logging elsewhere.
                continue


