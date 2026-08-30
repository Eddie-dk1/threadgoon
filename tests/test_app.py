from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, TabbedContent

from threadgoon.cli import create_app


@pytest.mark.asyncio
async def test_tui_mounts_and_keyboard_navigation(project_dir: Path) -> None:
    app = create_app(root=project_dir, start_background=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert len(app.query(DataTable)) == 4
        assert app.query_one("#tabs", TabbedContent).active == "catalog"

        await pilot.press("?")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "help"

        app.query_one("#tabs", TabbedContent).active = "catalog"
        await pilot.press("/")
        await pilot.pause()
        assert app.query_one("#search", Input).has_focus

    app.repository.close()
