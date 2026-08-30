from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def project_dir() -> Path:
    root = Path(__file__).resolve().parents[1]
    temp_root = root / ".threadgoon" / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="pytest-", dir=temp_root))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
