from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from threadgoon.api import ApiCancelled, ApiResult
from threadgoon.bridge import serve
from threadgoon.controller import Controller
from threadgoon.database import Repository
from threadgoon.models import Accuracy, DownloadStatus, KeywordScope, ThreadRecord, WatchStatus
from threadgoon.scanner import ScanFilters, ScanService, parse_catalog, parse_thread

from .fakes import FakeResponse, FakeSession
from .test_downloader import _setup


class FakeApi:
    def __init__(self):
        self.payload = {"posts": [
            {"no": 10, "sub": "Title", "tim": 100, "ext": ".jpg", "filename": "photo", "fsize": 3},
            {"no": 11, "com": "target", "tim": 101, "ext": ".png", "filename": "target", "fsize": 4},
        ]}
        self.stale = False
        self.block = None

    def fetch_boards(self, **kwargs):
        return ApiResult({"boards": [{"board": "gif", "title": "GIF", "ws_board": 0}]})

    def fetch_catalog(self, board, **kwargs):
        return ApiResult([{"threads": [{"no": 10, "sub": "Title", "com": "catalog description", "images": 1, "tim": 100}]}])

    def fetch_thread(self, board, thread_id, **kwargs):
        if self.block:
            self.block.set()
            cancel = kwargs["cancel_event"]
            if cancel.wait(2):
                raise ApiCancelled("cancelled")
        return ApiResult(self.payload, stale=self.stale)

    def close(self):
        pass


@pytest.fixture
def runtime(project_dir):
    events = []
    controller = Controller(project_dir, emit=lambda event, data: events.append((event, data)), start_background=False)
    controller.api.close()
    controller.api = FakeApi()
    controller.watcher.api = controller.api
    controller.downloader.session_factory = lambda: FakeSession([FakeResponse(200, headers={"Content-Length": "3"}, chunks=[b"abc"])])
    try:
        yield controller, events
    finally:
        controller.close()


def finish(controller, events, response):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        done = next((data for event, data in events if event == "operation_finished" and data["id"] == response["operation_id"]), None)
        if done and response["operation_id"] not in controller._operations:
            return done
        time.sleep(0.002)
    raise AssertionError("operation did not finish")


def test_catalog_update_preserves_exact_metrics(project_dir):
    repo = Repository(project_dir / "state.sqlite3")
    try:
        exact = parse_thread("gif", FakeApi().payload, ScanFilters(frozenset({".jpg", ".png"})))
        repo.upsert_thread(exact)
        repo.upsert_attachments(exact.attachments)
        estimate = parse_catalog("gif", [{"threads": [{"no": 10, "images": 99, "sub": "New title"}]}])[0]
        repo.upsert_thread(estimate)
        stored = repo.get_thread("gif", 10)
        assert stored.accuracy is Accuracy.EXACT
        assert stored.total_size == 7
        assert stored.file_count == 2
        assert stored.title == "New title"
        assert len(stored.attachments) == 2
    finally:
        repo.close()


def test_fresh_filters_do_not_reuse_saved_attachments(runtime):
    controller, events = runtime
    records = controller.exact_records([("gif", 10)], ScanFilters(frozenset({".jpg", ".png"})))
    assert len(records[0].attachments) == 2
    records = controller.exact_records([("gif", 10)], ScanFilters(frozenset({".png"})))
    assert [item.ext for item in records[0].attachments] == [".png"]
    # Historical .jpg metadata remains, but must never define today's download selection.
    assert len(controller.repository.get_attachments("gif", 10)) == 2
    result = finish(controller, events, controller.dispatch("prepare_download", {"keys": ["gif/10"], "filters": {"extensions": [".png"]}}))
    assert result["result"]["file_count"] == 1
    prepared = controller._prepared[result["result"]["token"]]
    assert [item.ext for item in prepared[0].attachments] == [".png"]


def test_cancel_is_not_reset_by_direct_fetch(runtime):
    controller, _ = runtime
    scanner = ScanService(controller.api, controller.repository)
    scanner.cancel()
    with pytest.raises(ApiCancelled):
        scanner.fetch_direct("gif", 10, ScanFilters(frozenset({".jpg"})))
    assert scanner.cancel_event.is_set()


def test_operations_have_independent_cancel_and_duplicate_guard(runtime):
    controller, events = runtime
    controller.api.block = threading.Event()
    response = controller.dispatch("detail", {"key": "gif/10"})
    assert controller.api.block.wait(1)
    with pytest.raises(ValueError, match="уже выполняется"):
        controller.dispatch("detail", {"key": "gif/10"})
    other = controller.dispatch("refresh_boards", {})
    controller.dispatch("pause_operation", {"operation_id": response["operation_id"]})
    assert controller._operations[response["operation_id"]].state == "paused"
    controller.dispatch("resume_operation", {"operation_id": response["operation_id"]})
    controller.dispatch("cancel_operation", {"operation_id": response["operation_id"]})
    assert finish(controller, events, response)["state"] == "cancelled"
    assert finish(controller, events, other)["state"] == "completed"


def test_fast_search_includes_catalog_description(runtime):
    controller, events = runtime
    result = finish(controller, events, controller.dispatch("search", {"boards": ["gif"], "query": "description"}))
    assert len(result["result"]["threads"]) == 1
    assert result["result"]["threads"][0]["accuracy"] == "estimated"


def test_exact_search_filters_full_posts_and_reports_results(runtime):
    controller, events = runtime
    result = finish(controller, events, controller.dispatch("search", {
        "boards": ["gif"], "exact": True, "query": "target",
        "filters": {"keywords": ["target"], "extensions": [".png"]}}))
    assert result["result"]["threads"][0]["file_count"] == 1
    assert any(event == "search_result" for event, _ in events)


def test_empty_search_and_invalid_boards(runtime):
    controller, events = runtime
    with pytest.raises(ValueError, match="хотя бы"):
        controller.dispatch("search", {"boards": []})
    result = finish(controller, events, controller.dispatch("search", {"boards": ["gif"], "query": "nonmatching"}))
    assert result["result"]["threads"] == []


def test_stale_cache_cannot_silently_confirm_download(runtime):
    controller, events = runtime
    controller.api.stale = True
    result = finish(controller, events, controller.dispatch("prepare_download", {"keys": ["gif/10"]}))
    assert result["state"] == "failed"
    assert "Нет связи" in result["error"]
    assert not controller.repository.list_downloads()


def test_confirmation_token_is_single_use_and_starts_queue(runtime):
    controller, events = runtime
    result = finish(controller, events, controller.dispatch("prepare_download", {"keys": ["gif/10"], "filters": {"extensions": [".jpg"]}}))
    assert not controller.repository.list_downloads()
    token = result["result"]["token"]
    controller.dispatch("confirm_download", {"token": token})
    deadline = time.monotonic() + 2
    while controller.busy and time.monotonic() < deadline:
        time.sleep(0.002)
    assert controller.repository.list_downloads()[0].status is DownloadStatus.COMPLETED
    with pytest.raises(ValueError, match="устарело"):
        controller.dispatch("confirm_download", {"token": token})


def test_discard_confirmation_does_not_enqueue(runtime):
    controller, events = runtime
    result = finish(controller, events, controller.dispatch("prepare_download", {"keys": ["gif/10"]}))
    controller.dispatch("discard_prepared", {"token": result["result"]["token"]})
    assert not controller.repository.list_downloads()


def test_workspace_lease_blocks_second_engine_and_releases_on_close(project_dir):
    first = Controller(project_dir, start_background=False)
    try:
        with pytest.raises(RuntimeError, match="другом"):
            Controller(project_dir, start_background=False)
    finally:
        first.close()
    second = Controller(project_dir, start_background=False)
    second.close()
    second.close()


def test_v1_migration_backs_up_database_and_config(project_dir):
    # Create an actual 6.0 schema using the original migration's table layout.
    path = project_dir / "state.sqlite3"
    repo = Repository(path)
    repo.upsert_thread(ThreadRecord("gif", 10, "Persisted"))
    repo.close()
    (project_dir / "config.toml").write_text("# original config\n")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE threads DROP COLUMN thumbnail_url")
        connection.execute("ALTER TABLE threads DROP COLUMN stale")
        connection.execute("UPDATE schema_version SET version = 1")
    migrated = Repository(path)
    try:
        assert migrated.schema_version() == 2
        assert migrated.get_thread("gif", 10).title == "Persisted"
        backups = list((project_dir / "backups").iterdir())
        assert len(backups) == 1
        assert (backups[0] / "config.toml").read_text() == "# original config\n"
        with sqlite3.connect(backups[0] / "state.sqlite3") as backup:
            assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    finally:
        migrated.close()


def test_protocol_rejects_malformed_and_wrong_version_and_shuts_down(project_dir):
    incoming = io.StringIO('\n'.join([
        'not json', '[]',
        json.dumps({"version": 9, "id": "wrong", "command": "snapshot"}),
        json.dumps({"version": 1, "id": "state", "command": "snapshot"}),
        json.dumps({"version": 1, "id": "exit", "command": "shutdown"}),
    ]) + '\n')
    output = io.StringIO()
    assert serve(project_dir, incoming, output, start_background=False) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0]["type"] == "ready"
    assert all(message["version"] == 1 for message in messages)
    assert next(m for m in messages if m.get("id") == "wrong")["ok"] is False
    assert next(m for m in messages if m.get("id") == "state")["data"]["workspace"] == str(project_dir)
    assert messages[-1]["id"] == "exit"
    reopened = Repository(project_dir / ".threadgoon/state.sqlite3")
    reopened.close()


def test_controller_shutdown_waits_for_cancelled_worker(runtime):
    controller, _ = runtime
    controller.api.block = threading.Event()
    controller.dispatch("detail", {"key": "gif/10"})
    assert controller.api.block.wait(1)
    controller.close()
    assert controller._closed
    assert not controller._operations


def test_unknown_size_is_not_reported_as_zero():
    payload = FakeApi().payload
    payload["posts"][0].pop("fsize")
    record = parse_thread("gif", payload, ScanFilters(frozenset({".jpg"})))
    assert record.total_size is None


def test_deleted_files_and_spoiler_thumbnails():
    payload = FakeApi().payload
    payload["posts"][0]["filedeleted"] = 1
    record = parse_thread("gif", payload, ScanFilters(frozenset({".jpg", ".png"})))
    assert record.file_count == 1
    assert not record.thumbnail_url
    payload["posts"][0].pop("filedeleted")
    payload["posts"][0]["spoiler"] = 1
    assert not parse_thread("gif", payload, ScanFilters(frozenset({".jpg"}))).thumbnail_url


def test_unavailable_watch_is_not_polled_again(runtime):
    controller, _ = runtime
    watch = controller.watcher.add("gif", 10)
    watch.status = WatchStatus.UNAVAILABLE
    controller.repository.add_watch(watch)
    # The worker's due selection must be active-only.
    called = []
    def stop_when_idle(timeout):
        controller.watcher.stop()
        return True
    controller.watcher.stop_event.wait = stop_when_idle
    controller.watcher.poll_once = lambda watch: called.append(watch)
    controller.watcher.run_forever()
    assert not called


def test_approve_after_pause_starts_only_confirmed_job(runtime):
    controller, _ = runtime
    records = controller.exact_records([("gif", 10)], ScanFilters(frozenset({".jpg", ".png"})))
    first, second = controller.downloader.enqueue_attachments(records[0], status=DownloadStatus.WAITING)
    controller.repository.update_download(second.id, status=DownloadStatus.PAUSED)
    controller.downloader.pause()
    controller.dispatch("download_control", {"action": "approve", "ids": [first.id]})
    deadline = time.monotonic() + 2
    while controller.repository.get_download(first.id).status is not DownloadStatus.COMPLETED and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.repository.get_download(first.id).status is DownloadStatus.COMPLETED
    assert controller.repository.get_download(second.id).status is DownloadStatus.PAUSED


def test_retry_errors_after_pause_does_not_restart_cancelled(runtime):
    controller, _ = runtime
    records = controller.exact_records([("gif", 10)], ScanFilters(frozenset({".jpg", ".png"})))
    first, second = controller.downloader.enqueue_attachments(records[0])
    controller.downloader.pause()
    controller.repository.update_download(first.id, status=DownloadStatus.FAILED)
    controller.repository.update_download(second.id, status=DownloadStatus.CANCELLED)
    controller.dispatch("download_control", {"action": "retry"})
    deadline = time.monotonic() + 2
    while controller.repository.get_download(first.id).status is not DownloadStatus.COMPLETED and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller.repository.get_download(first.id).status is DownloadStatus.COMPLETED
    assert controller.repository.get_download(second.id).status is DownloadStatus.CANCELLED
