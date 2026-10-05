"""SQLite persistence for cached metadata, jobs, and watches."""

from __future__ import annotations

import fcntl
import shutil
import json
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

from .models import (
    Accuracy,
    Attachment,
    BoardInfo,
    CacheEntry,
    DownloadJob,
    DownloadStatus,
    ThreadRecord,
    ThreadStatus,
    WatchRecord,
    WatchStatus,
)


_UNSET = object()


class Repository:
    """Thread-safe repository with project-local SQLite migrations."""

    SCHEMA_VERSION = 2

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._closed = False
        self._lease = path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lease.close()
            raise RuntimeError("Эта рабочая папка уже открыта в другом Threadgoon") from exc
        self.cache_dir = path.parent / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path,
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA temp_store = MEMORY")
        self._connection.execute("PRAGMA journal_mode = WAL")
        try:
            self._migrate()
            self.reset_interrupted_downloads()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()
                fcntl.flock(self._lease, fcntl.LOCK_UN)
                self._lease.close()

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER NOT NULL)"
            )
            row = self._connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            version = int(row["version"]) if row else 0
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"База данных создана более новой версией Threadgoon: {version}"
                )
            if 0 < version < self.SCHEMA_VERSION:
                directory = self.path.parent / "backups" / f"v{version}-{time.time_ns()}"
                directory.mkdir(parents=True)
                with sqlite3.connect(directory / self.path.name) as backup:
                    self._connection.backup(backup)
                config = self.path.parent / "config.toml"
                if config.exists():
                    shutil.copy2(config, directory / config.name)
            if version < 1:
                self._connection.executescript(
                    """
                    CREATE TABLE http_cache (
                        key TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        last_modified TEXT,
                        etag TEXT,
                        fetched_at REAL NOT NULL
                    );

                    CREATE TABLE boards (
                        board TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        ws_board INTEGER NOT NULL,
                        available INTEGER NOT NULL DEFAULT 1,
                        error TEXT,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE threads (
                        board TEXT NOT NULL,
                        thread_id INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        semantic_url TEXT NOT NULL DEFAULT '',
                        source_url TEXT NOT NULL,
                        file_count INTEGER NOT NULL DEFAULT 0,
                        total_size INTEGER,
                        accuracy TEXT NOT NULL,
                        status TEXT NOT NULL,
                        snippet TEXT NOT NULL DEFAULT '',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (board, thread_id)
                    );

                    CREATE TABLE attachments (
                        board TEXT NOT NULL,
                        thread_id INTEGER NOT NULL,
                        post_id INTEGER NOT NULL,
                        tim INTEGER NOT NULL,
                        ext TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        size INTEGER,
                        keyword_match INTEGER NOT NULL DEFAULT 0,
                        mime_type TEXT,
                        local_path TEXT,
                        thumbnail_path TEXT,
                        PRIMARY KEY (board, tim, ext),
                        FOREIGN KEY (board, thread_id)
                            REFERENCES threads(board, thread_id)
                            ON DELETE CASCADE
                    );

                    CREATE TABLE download_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        board TEXT NOT NULL,
                        thread_id INTEGER NOT NULL,
                        tim INTEGER NOT NULL,
                        ext TEXT NOT NULL,
                        url TEXT NOT NULL,
                        destination TEXT NOT NULL,
                        status TEXT NOT NULL,
                        expected_size INTEGER,
                        downloaded_size INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE (board, tim, ext, destination)
                    );

                    CREATE INDEX download_jobs_status_idx
                        ON download_jobs(status, created_at);

                    CREATE TABLE watches (
                        board TEXT NOT NULL,
                        thread_id INTEGER NOT NULL,
                        interval INTEGER NOT NULL,
                        auto_download INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        last_checked REAL,
                        last_error TEXT,
                        PRIMARY KEY (board, thread_id)
                    );

                    DELETE FROM schema_version;
                    INSERT INTO schema_version(version) VALUES (1);
                    """
                )

            if version < 2:
                self._connection.execute("ALTER TABLE threads ADD COLUMN thumbnail_url TEXT NOT NULL DEFAULT ''")
                self._connection.execute("ALTER TABLE threads ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")
                self._connection.execute("UPDATE schema_version SET version = 2")

    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            return int(row["version"])

    # HTTP cache

    def get_cache(self, key: str) -> CacheEntry | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        cache_path = self._cache_path(key)
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return CacheEntry(
            key=row["key"],
            payload=payload,
            last_modified=row["last_modified"],
            etag=row["etag"],
            fetched_at=float(row["fetched_at"]),
        )

    def save_cache(
        self,
        key: str,
        payload: object,
        *,
        last_modified: str | None,
        etag: str | None,
    ) -> None:
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            cache_path = self._cache_path(key)
            temporary = cache_path.with_suffix(".json.tmp")
            temporary.write_text(encoded, encoding="utf-8")
            os.replace(temporary, cache_path)
            self._connection.execute(
                """
                INSERT INTO http_cache(key, payload, last_modified, etag, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    payload = excluded.payload,
                    last_modified = excluded.last_modified,
                    etag = excluded.etag,
                    fetched_at = excluded.fetched_at
                """,
                (key, encoded, last_modified, etag, now),
            )

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def touch_cache(self, key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE http_cache SET fetched_at = ? WHERE key = ?",
                (time.time(), key),
            )

    # Boards and threads

    def upsert_boards(self, boards: Iterable[BoardInfo]) -> None:
        now = time.time()
        values = [
            (
                item.board,
                item.title,
                int(item.ws_board),
                int(item.available),
                item.error,
                now,
            )
            for item in boards
        ]
        if not values:
            return
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT INTO boards(board, title, ws_board, available, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(board) DO UPDATE SET
                    title = excluded.title,
                    ws_board = excluded.ws_board,
                    available = excluded.available,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                values,
            )

    def mark_board_unavailable(self, board: str, error: str) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO boards(board, title, ws_board, available, error, updated_at)
                VALUES (?, ?, 0, 0, ?, ?)
                ON CONFLICT(board) DO UPDATE SET
                    available = 0,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (board, board, error, now),
            )

    def list_boards(self) -> list[BoardInfo]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM boards ORDER BY ws_board, board"
            ).fetchall()
        return [
            BoardInfo(
                board=row["board"],
                title=row["title"],
                ws_board=bool(row["ws_board"]),
                available=bool(row["available"]),
                error=row["error"],
            )
            for row in rows
        ]

    def upsert_thread(self, record: ThreadRecord) -> None:
        updated_at = record.updated_at or time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO threads(
                    board, thread_id, title, semantic_url, source_url,
                    file_count, total_size, accuracy, status, snippet, updated_at, thumbnail_url, stale
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board, thread_id) DO UPDATE SET
                    title = excluded.title,
                    semantic_url = excluded.semantic_url,
                    source_url = excluded.source_url,
                    file_count = CASE WHEN excluded.accuracy = 'estimated' AND threads.accuracy = 'exact'
                        THEN threads.file_count ELSE excluded.file_count END,
                    total_size = CASE WHEN excluded.accuracy = 'estimated' AND threads.accuracy = 'exact'
                        THEN threads.total_size ELSE excluded.total_size END,
                    accuracy = CASE WHEN excluded.accuracy = 'estimated' AND threads.accuracy = 'exact'
                        THEN threads.accuracy ELSE excluded.accuracy END,
                    status = excluded.status,
                    snippet = excluded.snippet,
                    updated_at = excluded.updated_at,
                    thumbnail_url = excluded.thumbnail_url,
                    stale = excluded.stale
                """,
                (
                    record.board,
                    record.thread_id,
                    record.title,
                    record.semantic_url,
                    record.source_url,
                    record.file_count,
                    record.total_size,
                    record.accuracy.value,
                    record.status.value,
                    record.snippet,
                    updated_at,
                    record.thumbnail_url,
                    int(record.stale),
                ),
            )

    def mark_thread_status(
        self,
        board: str,
        thread_id: int,
        status: ThreadStatus,
        *,
        snippet: str | None = None,
    ) -> None:
        with self._lock, self._connection:
            if snippet is None:
                self._connection.execute(
                    """
                    UPDATE threads
                    SET status = ?, updated_at = ?
                    WHERE board = ? AND thread_id = ?
                    """,
                    (status.value, time.time(), board, thread_id),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE threads
                    SET status = ?, snippet = ?, updated_at = ?
                    WHERE board = ? AND thread_id = ?
                    """,
                    (status.value, snippet, time.time(), board, thread_id),
                )

    def list_threads(self, search: str = "") -> list[ThreadRecord]:
        query = "SELECT * FROM threads"
        parameters: tuple[object, ...] = ()
        if search.strip():
            value = f"%{search.strip().casefold()}%"
            query += (
                " WHERE lower(title) LIKE ? OR lower(board) LIKE ? "
                "OR CAST(thread_id AS TEXT) LIKE ?"
            )
            parameters = (value, value, value)
        query += " ORDER BY board, file_count DESC, thread_id DESC"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._thread_from_row(row) for row in rows]

    def get_thread(self, board: str, thread_id: int) -> ThreadRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM threads WHERE board = ? AND thread_id = ?",
                (board, thread_id),
            ).fetchone()
        if not row:
            return None
        record = self._thread_from_row(row)
        record.attachments = self.get_attachments(board, thread_id)
        return record

    @staticmethod
    def _thread_from_row(row: sqlite3.Row) -> ThreadRecord:
        return ThreadRecord(
            board=row["board"],
            thread_id=int(row["thread_id"]),
            title=row["title"],
            semantic_url=row["semantic_url"],
            source_url=row["source_url"],
            file_count=int(row["file_count"]),
            total_size=row["total_size"],
            accuracy=Accuracy(row["accuracy"]),
            status=ThreadStatus(row["status"]),
            snippet=row["snippet"],
            updated_at=float(row["updated_at"]),
            thumbnail_url=row["thumbnail_url"],
            stale=bool(row["stale"]),
        )

    # Attachments

    def attachment_keys(self, board: str, thread_id: int) -> set[tuple[str, int, str]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT board, tim, ext FROM attachments "
                "WHERE board = ? AND thread_id = ?",
                (board, thread_id),
            ).fetchall()
        return {(row["board"], int(row["tim"]), row["ext"]) for row in rows}

    def upsert_attachments(self, attachments: Iterable[Attachment]) -> None:
        values = [
            (
                item.board,
                item.thread_id,
                item.post_id,
                item.tim,
                item.ext,
                item.filename,
                item.display_name,
                item.size,
                int(item.keyword_match),
                item.mime_type,
                item.local_path,
                item.thumbnail_path,
            )
            for item in attachments
        ]
        if not values:
            return
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT INTO attachments(
                    board, thread_id, post_id, tim, ext, filename, display_name,
                    size, keyword_match, mime_type, local_path, thumbnail_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board, tim, ext) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    post_id = excluded.post_id,
                    filename = excluded.filename,
                    display_name = excluded.display_name,
                    size = excluded.size,
                    keyword_match = excluded.keyword_match,
                    mime_type = excluded.mime_type,
                    local_path = COALESCE(attachments.local_path, excluded.local_path),
                    thumbnail_path = COALESCE(
                        attachments.thumbnail_path, excluded.thumbnail_path
                    )
                """,
                values,
            )

    def get_attachments(self, board: str, thread_id: int) -> list[Attachment]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM attachments
                WHERE board = ? AND thread_id = ?
                ORDER BY post_id, tim
                """,
                (board, thread_id),
            ).fetchall()
        return [
            Attachment(
                board=row["board"],
                thread_id=int(row["thread_id"]),
                post_id=int(row["post_id"]),
                tim=int(row["tim"]),
                ext=row["ext"],
                filename=row["filename"],
                display_name=row["display_name"],
                size=row["size"],
                keyword_match=bool(row["keyword_match"]),
                mime_type=row["mime_type"],
                local_path=row["local_path"],
                thumbnail_path=row["thumbnail_path"],
            )
            for row in rows
        ]

    def mark_attachment_downloaded(
        self, board: str, tim: int, ext: str, local_path: str
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE attachments SET local_path = ?
                WHERE board = ? AND tim = ? AND ext = ?
                """,
                (local_path, board, tim, ext),
            )

    # Download queue

    def enqueue_download(
        self,
        attachment: Attachment,
        *,
        url: str,
        destination: str,
        status: DownloadStatus = DownloadStatus.PENDING,
    ) -> DownloadJob:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO download_jobs(
                    board, thread_id, tim, ext, url, destination, status,
                    expected_size, downloaded_size, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                ON CONFLICT(board, tim, ext, destination) DO UPDATE SET
                    status = CASE
                        WHEN download_jobs.status IN ('completed', 'downloading', 'pending', 'paused', 'cancelled', 'failed') THEN download_jobs.status
                        ELSE excluded.status
                    END,
                    expected_size = COALESCE(
                        excluded.expected_size, download_jobs.expected_size
                    ),
                    error = download_jobs.error,
                    updated_at = download_jobs.updated_at
                """,
                (
                    attachment.board,
                    attachment.thread_id,
                    attachment.tim,
                    attachment.ext,
                    url,
                    destination,
                    status.value,
                    attachment.size,
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT * FROM download_jobs
                WHERE board = ? AND tim = ? AND ext = ? AND destination = ?
                """,
                (attachment.board, attachment.tim, attachment.ext, destination),
            ).fetchone()
        return self._download_from_row(row)

    def list_downloads(
        self, statuses: Sequence[DownloadStatus] | None = None
    ) -> list[DownloadJob]:
        query = "SELECT * FROM download_jobs"
        parameters: tuple[object, ...] = ()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            parameters = tuple(item.value for item in statuses)
        query += " ORDER BY created_at, id"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._download_from_row(row) for row in rows]

    def get_download(self, job_id: int) -> DownloadJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM download_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._download_from_row(row) if row else None

    def update_download(
        self,
        job_id: int,
        *,
        status: DownloadStatus | object = _UNSET,
        downloaded_size: int | object = _UNSET,
        expected_size: int | None | object = _UNSET,
        error: str | None | object = _UNSET,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: list[object] = [time.time()]
        if status is not _UNSET:
            assignments.append("status = ?")
            values.append(status.value)
        if downloaded_size is not _UNSET:
            assignments.append("downloaded_size = ?")
            values.append(downloaded_size)
        if expected_size is not _UNSET:
            assignments.append("expected_size = ?")
            values.append(expected_size)
        if error is not _UNSET:
            assignments.append("error = ?")
            values.append(error)
        values.append(job_id)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE download_jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def reset_interrupted_downloads(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE download_jobs
                SET status = ?, error = NULL, updated_at = ?
                WHERE status = ?
                """,
                (
                    DownloadStatus.PENDING.value,
                    time.time(),
                    DownloadStatus.DOWNLOADING.value,
                ),
            )

    @staticmethod
    def _download_from_row(row: sqlite3.Row) -> DownloadJob:
        return DownloadJob(
            id=int(row["id"]),
            board=row["board"],
            thread_id=int(row["thread_id"]),
            tim=int(row["tim"]),
            ext=row["ext"],
            url=row["url"],
            destination=row["destination"],
            status=DownloadStatus(row["status"]),
            expected_size=row["expected_size"],
            downloaded_size=int(row["downloaded_size"]),
            error=row["error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    # Watches

    def add_watch(self, watch: WatchRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO watches(
                    board, thread_id, interval, auto_download, status,
                    last_checked, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board, thread_id) DO UPDATE SET
                    interval = excluded.interval,
                    auto_download = excluded.auto_download,
                    status = excluded.status,
                    last_error = NULL
                """,
                (
                    watch.board,
                    watch.thread_id,
                    watch.interval,
                    int(watch.auto_download),
                    watch.status.value,
                    watch.last_checked,
                    watch.last_error,
                ),
            )

    def remove_watch(self, board: str, thread_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM watches WHERE board = ? AND thread_id = ?",
                (board, thread_id),
            )

    def list_watches(self) -> list[WatchRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM watches ORDER BY board, thread_id"
            ).fetchall()
        return [
            WatchRecord(
                board=row["board"],
                thread_id=int(row["thread_id"]),
                interval=int(row["interval"]),
                auto_download=bool(row["auto_download"]),
                status=WatchStatus(row["status"]),
                last_checked=row["last_checked"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def update_watch(
        self,
        board: str,
        thread_id: int,
        *,
        status: WatchStatus,
        last_checked: float | None,
        last_error: str | None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE watches
                SET status = CASE WHEN status = 'paused' AND ? = 'active' THEN status ELSE ? END, last_checked = ?, last_error = ?
                WHERE board = ? AND thread_id = ?
                """,
                (status.value, status.value, last_checked, last_error, board, thread_id),
            )
