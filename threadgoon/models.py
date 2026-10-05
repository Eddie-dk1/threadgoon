"""Shared domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class KeywordScope(StrEnum):
    FILE_POST = "file_post"
    WHOLE_THREAD = "whole_thread"


class Accuracy(StrEnum):
    ESTIMATED = "estimated"
    EXACT = "exact"


class ThreadStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    EXPIRED = "expired"
    ERROR = "error"


class DownloadStatus(StrEnum):
    WAITING = "waiting"
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WatchStatus(StrEnum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    PAUSED = "paused"


@dataclass(slots=True)
class BoardInfo:
    board: str
    title: str
    ws_board: bool
    available: bool = True
    error: str | None = None


@dataclass(slots=True)
class Attachment:
    board: str
    thread_id: int
    post_id: int
    tim: int
    ext: str
    filename: str
    display_name: str
    size: int | None
    keyword_match: bool = False
    mime_type: str | None = None
    local_path: str | None = None
    thumbnail_path: str | None = None

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.board, self.tim, self.ext)


@dataclass(slots=True)
class ThreadRecord:
    board: str
    thread_id: int
    title: str
    semantic_url: str = ""
    source_url: str = ""
    file_count: int = 0
    total_size: int | None = None
    accuracy: Accuracy = Accuracy.ESTIMATED
    status: ThreadStatus = ThreadStatus.AVAILABLE
    snippet: str = ""
    updated_at: float = 0.0
    attachments: list[Attachment] = field(default_factory=list)
    thumbnail_url: str = ""
    stale: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return (self.board, self.thread_id)


@dataclass(slots=True)
class DownloadJob:
    id: int
    board: str
    thread_id: int
    tim: int
    ext: str
    url: str
    destination: str
    status: DownloadStatus
    expected_size: int | None = None
    downloaded_size: int = 0
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(slots=True)
class WatchRecord:
    board: str
    thread_id: int
    interval: int
    auto_download: bool
    status: WatchStatus = WatchStatus.ACTIVE
    last_checked: float | None = None
    last_error: str | None = None


@dataclass(slots=True)
class CacheEntry:
    key: str
    payload: Any
    last_modified: str | None = None
    etag: str | None = None
    fetched_at: float = 0.0
