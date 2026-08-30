"""In-process thread monitoring built on the shared API limiter."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .api import ApiClient, ApiNotFound
from .database import Repository
from .downloader import DownloadManager
from .models import (
    DownloadStatus,
    ThreadRecord,
    ThreadStatus,
    WatchRecord,
    WatchStatus,
)
from .scanner import ScanFilters, parse_thread

WatchCallback = Callable[[WatchRecord, int, str | None], None]


class WatchService:
    """Poll watched threads only while the owning TUI is running."""

    def __init__(
        self,
        api: ApiClient,
        repository: Repository,
        downloader: DownloadManager,
        filters: ScanFilters,
    ):
        self.api = api
        self.repository = repository
        self.downloader = downloader
        self.filters = filters
        self.stop_event = threading.Event()
        self.logger = logging.getLogger("threadgoon.watch")

    def add(
        self,
        board: str,
        thread_id: int,
        *,
        interval: int = 60,
        auto_download: bool = False,
    ) -> WatchRecord:
        if interval < 10:
            raise ValueError("Интервал мониторинга не может быть меньше 10 секунд")
        watch = WatchRecord(
            board=board,
            thread_id=thread_id,
            interval=interval,
            auto_download=auto_download,
        )
        self.repository.add_watch(watch)
        return watch

    def remove(self, board: str, thread_id: int) -> None:
        self.repository.remove_watch(board, thread_id)

    def stop(self) -> None:
        self.stop_event.set()

    def reset(self) -> None:
        self.stop_event.clear()

    def poll_once(
        self,
        watch: WatchRecord,
    ) -> tuple[ThreadRecord | None, int, str | None]:
        now = time.time()
        existing = self.repository.attachment_keys(watch.board, watch.thread_id)
        try:
            result = self.api.fetch_thread(
                watch.board,
                watch.thread_id,
                cancel_event=self.stop_event,
            )
            if result.not_modified:
                self.repository.update_watch(
                    watch.board,
                    watch.thread_id,
                    status=WatchStatus.ACTIVE,
                    last_checked=now,
                    last_error=None,
                )
                return self.repository.get_thread(watch.board, watch.thread_id), 0, None

            record = parse_thread(watch.board, result.data, self.filters)
            new_attachments = [
                item for item in record.attachments if item.key not in existing
            ]
            self.repository.upsert_thread(record)
            self.repository.upsert_attachments(record.attachments)
            queue_status = (
                DownloadStatus.PENDING
                if watch.auto_download
                else DownloadStatus.WAITING
            )
            if new_attachments:
                self.downloader.enqueue_attachments(
                    record,
                    new_attachments,
                    status=queue_status,
                )
            self.repository.update_watch(
                watch.board,
                watch.thread_id,
                status=WatchStatus.ACTIVE,
                last_checked=now,
                last_error=None,
            )
            return record, len(new_attachments), None
        except ApiNotFound:
            message = "Тред больше недоступен"
            self.repository.mark_thread_status(
                watch.board,
                watch.thread_id,
                ThreadStatus.EXPIRED,
                snippet=message,
            )
            self.repository.update_watch(
                watch.board,
                watch.thread_id,
                status=WatchStatus.UNAVAILABLE,
                last_checked=now,
                last_error=message,
            )
            return None, 0, message
        except Exception as exc:
            message = str(exc)
            self.repository.update_watch(
                watch.board,
                watch.thread_id,
                status=WatchStatus.ACTIVE,
                last_checked=now,
                last_error=message,
            )
            self.logger.warning(
                "Watch failed for /%s/%s: %s",
                watch.board,
                watch.thread_id,
                exc,
            )
            return None, 0, message

    def run_forever(
        self,
        *,
        on_update: WatchCallback | None = None,
    ) -> None:
        self.reset()
        while not self.stop_event.is_set():
            watches = self.repository.list_watches()
            now = time.time()
            due = [
                watch
                for watch in watches
                if watch.status is not WatchStatus.PAUSED
                and (
                    watch.last_checked is None
                    or now - watch.last_checked >= watch.interval
                )
            ]
            if not due:
                self.stop_event.wait(0.5)
                continue
            for watch in due:
                if self.stop_event.is_set():
                    return
                _record, count, error = self.poll_once(watch)
                if on_update:
                    current = next(
                        (
                            item
                            for item in self.repository.list_watches()
                            if item.board == watch.board
                            and item.thread_id == watch.thread_id
                        ),
                        watch,
                    )
                    on_update(current, count, error)
