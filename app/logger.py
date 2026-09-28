from __future__ import annotations

import collections
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# In-memory log buffer for dashboard streaming / display
LOG_BUFFER: collections.deque[str] = collections.deque(maxlen=300)


class MemoryLogHandler(logging.Handler):
    """Stores recent log messages in a deque for the dashboard."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
        except Exception:
            pass


def get_recent_logs(limit: int = 100) -> list[str]:
    """Retrieve the most recent log records."""
    logs = list(LOG_BUFFER)
    return logs[-limit:] if limit > 0 else logs


def setup_logging(
    level: int = logging.INFO,
    log_file: str | Path = "data/gateway.log",
) -> None:
    """Configure unified logging for console, rotating file, and dashboard memory buffer."""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid duplicate handlers on reload
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    # 1. Console Handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # 2. File Handler (UTF-8, 5 MB, 3 backups)
    file_handler = RotatingFileHandler(
        str(log_path),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 3. In-memory handler for dashboard
    mem_handler = MemoryLogHandler()
    mem_handler.setLevel(level)
    mem_handler.setFormatter(formatter)
    root_logger.addHandler(mem_handler)

    # Reduce spammy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
