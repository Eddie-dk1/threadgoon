from __future__ import annotations

from pathlib import Path

from threadgoon.database import Repository
from threadgoon.models import (
    Accuracy,
    Attachment,
    DownloadStatus,
    ThreadRecord,
)


def _record() -> ThreadRecord:
    return ThreadRecord(
        board="gif",
        thread_id=10,
        title="Example",
        source_url="https://boards.4chan.org/gif/thread/10",
        file_count=1,
        total_size=3,
        accuracy=Accuracy.EXACT,
    )


def _attachment() -> Attachment:
    return Attachment(
        board="gif",
        thread_id=10,
        post_id=10,
        tim=100,
        ext=".jpg",
        filename="file",
        display_name="file",
        size=3,
    )


def test_schema_and_interrupted_job_recovery(project_dir: Path) -> None:
    path = project_dir / "state.sqlite3"
    repository = Repository(path)
    repository.upsert_thread(_record())
    repository.upsert_attachments([_attachment()])
    job = repository.enqueue_download(
        _attachment(),
        url="https://i.4cdn.org/gif/100.jpg",
        destination="output/gif/file.jpg",
    )
    repository.update_download(job.id, status=DownloadStatus.DOWNLOADING)
    repository.close()

    reopened = Repository(path)
    recovered = reopened.get_download(job.id)

    assert reopened.schema_version() == 1
    assert recovered is not None
    assert recovered.status is DownloadStatus.PENDING
    reopened.close()
