"""
Logging setup: rotating file handler + stream handler (captured by systemd journal).
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(
    name: str = "advisor",
    log_level: str = "INFO",
    log_file: str = "logs/advisor.log",
) -> logging.Logger:
    """
    Configure and return the application logger.

    Outputs to:
    - A rotating file (10 MB max, 5 backups)
    - stdout (captured by systemd journal on the VM)
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Ensure log directory exists
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # Rotating file handler: 10 MB per file, keep 5 backups (50 MB total)
    fh = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Stream handler: visible in terminal and captured by systemd journal
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def get_logger(module_name: str) -> logging.Logger:
    """Return a child logger scoped to a specific module."""
    return logging.getLogger(f"advisor.{module_name}")
