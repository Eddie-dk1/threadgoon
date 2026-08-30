"""Runtime logging without import-time side effects."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import AppPaths


def configure_logging(paths: AppPaths, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("threadgoon")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    handler = RotatingFileHandler(
        paths.logs / "threadgoon.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s")
    )
    logger.addHandler(handler)
    return logger
