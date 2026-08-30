from __future__ import annotations

from pathlib import Path

from threadgoon.database import Repository
from threadgoon.downloader import DownloadManager
from threadgoon.models import (
    Accuracy,
    Attachment,
    DownloadStatus,
    ThreadRecord,
)
from threadgoon.paths import AppPaths

from .fakes import FakeResponse, FakeSession


def _setup(project_dir: Path, size: int = 6):
    paths = AppPaths.build(project_dir)
    paths.ensure()
    repository = Repository(paths.database)
    attachment = Attachment(
        board="gif",
        thread_id=10,
        post_id=11,
        tim=123,
        ext=".jpg",
        filename="sample",
        display_name="sample",
        size=size,
    )
    record = ThreadRecord(
        board="gif",
        thread_id=10,
        title="Example",
        semantic_url="example",
        source_url="https://boards.4chan.org/gif/thread/10",
        file_count=1,
        total_size=size,
        accuracy=Accuracy.EXACT,
        attachments=[attachment],
    )
    repository.upsert_thread(record)
    repository.upsert_attachments([attachment])
    return paths, repository, record, attachment


def test_atomic_download_and_size_validation(project_dir: Path) -> None:
    paths, repository, record, attachment = _setup(project_dir)
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={"Content-Length": "6"},
                chunks=[b"abc", b"def"],
            )
        ]
    )
    manager = DownloadManager(
        repository,
        paths,
        session_factory=lambda: session,
        max_workers=1,
    )
    job = manager.enqueue_attachments(record)[0]

    manager.process_pending()

    finished = repository.get_download(job.id)
    destination = paths.local_path(job.destination)
    assert finished is not None
    assert finished.status is DownloadStatus.COMPLETED
    assert destination.read_bytes() == b"abcdef"
    assert not destination.with_name(destination.name + ".part").exists()
    repository.close()


def test_range_resume(project_dir: Path) -> None:
    paths, repository, record, attachment = _setup(project_dir)
    session = FakeSession(
        [
            FakeResponse(
                206,
                headers={
                    "Content-Length": "3",
                    "Content-Range": "bytes 3-5/6",
                },
                chunks=[b"def"],
            )
        ]
    )
    manager = DownloadManager(
        repository,
        paths,
        session_factory=lambda: session,
        max_workers=1,
    )
    job = manager.enqueue_attachments(record)[0]
    destination = paths.local_path(job.destination)
    part = destination.with_name(destination.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"abc")

    manager.process_pending()

    assert destination.read_bytes() == b"abcdef"
    assert session.requests[0][1]["headers"]["Range"] == "bytes=3-"
    repository.close()


def test_mismatched_content_stays_part_file(project_dir: Path) -> None:
    paths, repository, record, attachment = _setup(project_dir)
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={"Content-Length": "3"},
                chunks=[b"abc"],
            )
        ]
    )
    manager = DownloadManager(
        repository,
        paths,
        session_factory=lambda: session,
        max_workers=1,
    )
    job = manager.enqueue_attachments(record)[0]

    manager.process_pending()

    failed = repository.get_download(job.id)
    destination = paths.local_path(job.destination)
    assert failed is not None
    assert failed.status is DownloadStatus.FAILED
    assert not destination.exists()
    assert destination.with_name(destination.name + ".part").read_bytes() == b"abc"
    repository.close()
