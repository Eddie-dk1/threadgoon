from __future__ import annotations

import threading
from pathlib import Path

import pytest

from threadgoon.downloader import DownloadError, DownloadManager
from threadgoon.models import DownloadStatus

from .fakes import FakeResponse, FakeSession
from .test_downloader import _setup


def test_preflight_failure_is_failed_not_pending(project_dir, monkeypatch):
    paths, repo, record, _ = _setup(project_dir)
    manager = DownloadManager(repo, paths)
    job = manager.enqueue_attachments(record)[0]
    monkeypatch.setattr(manager, "_ensure_space", lambda *args: (_ for _ in ()).throw(DownloadError("No space")))
    manager.process_pending()
    assert repo.get_download(job.id).status is DownloadStatus.FAILED
    assert "No space" in repo.get_download(job.id).error
    assert manager.process_pending() == []
    repo.close()


def test_pause_pending_resume_and_cancel_preserve_controls(project_dir):
    paths, repo, record, _ = _setup(project_dir)
    session = FakeSession([FakeResponse(200, headers={"Content-Length": "6"}, chunks=[b"abcdef"])])
    manager = DownloadManager(repo, paths, session_factory=lambda: session)
    job = manager.enqueue_attachments(record)[0]
    manager.pause()
    assert repo.get_download(job.id).status is DownloadStatus.PAUSED
    manager.process_pending()
    assert not session.requests
    assert not manager.pause_event.is_set()
    manager.resume()
    manager.cancel_job(job.id)
    manager.process_pending()
    assert repo.get_download(job.id).status is DownloadStatus.CANCELLED
    assert not session.requests
    manager.retry(job.id)
    manager.process_pending()
    assert repo.get_download(job.id).status is DownloadStatus.COMPLETED
    repo.close()


def test_stop_is_not_reset_when_processing_queue(project_dir):
    paths, repo, record, _ = _setup(project_dir)
    session = FakeSession([])
    manager = DownloadManager(repo, paths, session_factory=lambda: session)
    job = manager.enqueue_attachments(record)[0]
    manager.stop()
    manager.process_pending()
    assert manager.stop_event.is_set()
    assert not session.requests
    assert repo.get_download(job.id).status is DownloadStatus.PENDING
    repo.close()


def test_active_reenqueue_does_not_reset_job(project_dir):
    paths, repo, record, _ = _setup(project_dir)
    manager = DownloadManager(repo, paths)
    job = manager.enqueue_attachments(record)[0]
    repo.update_download(job.id, status=DownloadStatus.DOWNLOADING, downloaded_size=3)
    again = manager.enqueue_attachments(record)[0]
    assert again.id == job.id
    assert again.status is DownloadStatus.DOWNLOADING
    assert again.downloaded_size == 3
    assert len(repo.list_downloads()) == 1
    repo.close()


def test_bad_range_cannot_corrupt_partial_file(project_dir):
    paths, repo, record, _ = _setup(project_dir)
    session = FakeSession([FakeResponse(206, headers={"Content-Range": "bytes 0-2/6", "Content-Length": "3"}, chunks=[b"bad"])])
    manager = DownloadManager(repo, paths, session_factory=lambda: session)
    job = manager.enqueue_attachments(record)[0]
    destination = paths.local_path(job.destination)
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(b"abc")
    manager.process_pending()
    assert repo.get_download(job.id).status is DownloadStatus.FAILED
    assert part.read_bytes() == b"abc"
    repo.close()


def test_cancel_active_job_keeps_part_and_blocks_final_rename(project_dir):
    paths, repo, record, _ = _setup(project_dir)
    session = FakeSession([FakeResponse(200, headers={"Content-Length": "6"}, chunks=[b"abc", b"def"])])
    manager = DownloadManager(repo, paths, session_factory=lambda: session)
    job = manager.enqueue_attachments(record)[0]
    original = manager._wait_for_control
    calls = 0
    def cancel_after_first_chunk(event):
        nonlocal calls
        calls += 1
        if calls == 3:
            manager.cancel_job(job.id)
        original(event)
    manager._wait_for_control = cancel_after_first_chunk
    manager.process_pending()
    destination = paths.local_path(job.destination)
    assert repo.get_download(job.id).status is DownloadStatus.CANCELLED
    assert destination.with_name(destination.name + ".part").read_bytes() == b"abc"
    assert not destination.exists()
    repo.close()
