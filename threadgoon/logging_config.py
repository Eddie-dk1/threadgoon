"""Runtime logging without import-time side effects."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import AppPaths


def configure_logging(paths: AppPaths, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("threadgoon")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    destination = str(paths.logs / "threadgoon.log")
    if any(getattr(handler, "baseFilename", "") == destination for handler in logger.handlers):
        return logger
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()

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
