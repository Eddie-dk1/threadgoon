from __future__ import annotations

from pathlib import Path

from threadgoon.api import ApiResult
from threadgoon.database import Repository
from threadgoon.downloader import DownloadManager
from threadgoon.models import (
    Accuracy,
    Attachment,
    DownloadStatus,
    ThreadRecord,
    WatchRecord,
)
from threadgoon.paths import AppPaths
from threadgoon.scanner import ScanFilters
from threadgoon.watcher import WatchService


def test_watch_queues_only_new_attachments(project_dir: Path) -> None:
    class FakeApi:
        def fetch_thread(self, board, thread_id, **_kwargs):
            assert (board, thread_id) == ("gif", 10)
            return ApiResult(
                {
                    "posts": [
                        {
                            "no": 10,
                            "sub": "Example",
                            "tim": 100,
                            "ext": ".jpg",
                            "filename": "old",
                            "fsize": 3,
                        },
                        {
                            "no": 11,
                            "tim": 101,
                            "ext": ".jpg",
                            "filename": "new",
                            "fsize": 4,
                        },
                    ]
                }
            )

    paths = AppPaths.build(project_dir)
    paths.ensure()
    repository = Repository(paths.database)
    old = Attachment(
        board="gif",
        thread_id=10,
        post_id=10,
        tim=100,
        ext=".jpg",
        filename="old",
        display_name="old",
        size=3,
    )
    record = ThreadRecord(
        board="gif",
        thread_id=10,
        title="Example",
        source_url="https://boards.4chan.org/gif/thread/10",
        file_count=1,
        total_size=3,
        accuracy=Accuracy.EXACT,
        attachments=[old],
    )
    repository.upsert_thread(record)
    repository.upsert_attachments([old])
    downloader = DownloadManager(repository, paths, max_workers=1)
    watcher = WatchService(
        FakeApi(),
        repository,
        downloader,
        ScanFilters(extensions=frozenset({".jpg"})),
    )
    watch = WatchRecord(
        board="gif",
        thread_id=10,
        interval=60,
        auto_download=False,
    )
    repository.add_watch(watch)

    _updated, count, error = watcher.poll_once(watch)

    jobs = repository.list_downloads()
    assert error is None
    assert count == 1
    assert len(jobs) == 1
    assert jobs[0].tim == 101
    assert jobs[0].status is DownloadStatus.WAITING
    repository.close()
