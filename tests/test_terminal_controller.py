from __future__ import annotations

import pytest
from textual.widgets import DataTable, TabbedContent

from threadgoon.app import ConfirmDownloadScreen
from threadgoon.cli import create_app
from threadgoon.models import DownloadStatus

from .fakes import FakeResponse, FakeSession
from .test_desktop_engine import FakeApi


@pytest.mark.asyncio
async def test_terminal_search_and_confirm_use_shared_controller(project_dir):
    app = create_app(root=project_dir, start_background=False)
    app.controller.api.close()
    app.controller.api = FakeApi()
    app.config.boards = ["gif"]
    app.config.extensions = [".jpg"]
    app.controller.downloader.session_factory = lambda: FakeSession([
        FakeResponse(200, headers={"Content-Length": "3"}, chunks=[b"abc"])])
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.click("#fast-scan")
            for _ in range(20):
                await pilot.pause(0.05)
                if app.records and not app.controller.busy:
                    break
            assert app.query_one("#catalog-table", DataTable).row_count == 1
            app.selected_keys = {("gif", 10)}
            await pilot.click("#queue-selected")
            for _ in range(20):
                await pilot.pause(0.05)
                if isinstance(app.screen, ConfirmDownloadScreen):
                    break
            assert isinstance(app.screen, ConfirmDownloadScreen)
            await pilot.pause(0.15)
            assert await pilot.click("#confirm")
            for _ in range(20):
                await pilot.pause(0.05)
                if not app.controller.busy:
                    break
            assert app.query_one("#tabs", TabbedContent).active == "downloads", (str(app.query_one("#global-status").render()), app.controller.repository.list_downloads(), type(app.screen).__name__)
            assert app.controller.repository.list_downloads()[0].status is DownloadStatus.COMPLETED
    finally:
        app.controller.close()
