#!/usr/bin/env python3
"""Relocate a built .app and smoke-test its frozen engine without source/Python paths."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="threadgoon-portable-") as temporary:
        directory = Path(temporary)
        app = directory / "Relocated App" / "Threadgoon.app"
        shutil.copytree(ROOT / "dist/Threadgoon.app", app, symlinks=True)
        workspace = directory / "Work folder with spaces"
        workspace.mkdir()
        requests = [
            {"version": 1, "id": "snapshot", "command": "snapshot", "params": {}},
            {"version": 1, "id": "quit", "command": "shutdown", "params": {}},
        ]
        helper = app / "Contents/Resources/engine/threadgoon-engine"
        result = subprocess.run([str(helper), "--workspace", str(workspace), "--no-watch"],
                                input="".join(json.dumps(item) + "\n" for item in requests),
                                capture_output=True, text=True, cwd=directory, timeout=20,
                                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/missing-source",
                                     "PYTHONHOME": "/missing-python"})
        assert result.returncode == 0, result.stderr
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        assert messages[0]["type"] == "ready", messages
        assert messages[0]["app_version"] == "6.1.0"
        assert messages[1]["id"] == "snapshot" and messages[1]["ok"]
        assert messages[1]["data"]["workspace"] == str(workspace.resolve())
        assert messages[-1]["id"] == "quit" and messages[-1]["ok"]
        assert (workspace / ".threadgoon/state.sqlite3").exists()
        assert (workspace / ".threadgoon/config.toml").exists()
        print("Relocated .app helper: launch, JSON protocol, workspace and shutdown passed without source/Python environment")


if __name__ == "__main__":
    main()
