"""Fast catalog and exact thread scanning services."""

from __future__ import annotations

import html
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence
from urllib.parse import urlparse

from .api import (
    API_BASE_URL,
    SOURCE_BASE_URL,
    ApiCancelled,
    ApiClient,
    ApiNotFound,
)
from .config import AppConfig
from .database import Repository
from .models import (
    Accuracy,
    Attachment,
    BoardInfo,
    KeywordScope,
    ThreadRecord,
    ThreadStatus,
)

ProgressCallback = Callable[[int, int, ThreadRecord | None, str], None]


@dataclass(frozen=True, slots=True)
class ScanFilters:
    extensions: frozenset[str]
    keywords: tuple[str, ...] = ()
    keyword_scope: KeywordScope = KeywordScope.FILE_POST
    min_file_size: int | None = None
    max_file_size: int | None = None
    min_matched_files: int = 1

    @classmethod
    def from_config(cls, config: AppConfig) -> "ScanFilters":
        return cls(
            extensions=frozenset(config.extensions),
            keywords=config.normalized_keywords,
            keyword_scope=config.keyword_scope,
            min_file_size=config.min_file_size,
            max_file_size=config.max_file_size,
            min_matched_files=config.min_matched_files,
        )


class ScanService:
    """Coordinates cancellable catalog and deep scans."""

    def __init__(self, api: ApiClient, repository: Repository):
        self.api = api
        self.repository = repository
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()

    def reset_control(self) -> None:
        self.cancel_event.clear()
        self.pause_event.set()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.pause_event.set()

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    def refresh_boards(self) -> list[BoardInfo]:
        result = self.api.fetch_boards(cancel_event=self.cancel_event)
        raw_boards = result.data.get("boards", []) if isinstance(result.data, dict) else []
        boards = [
            BoardInfo(
                board=str(item.get("board", "")),
                title=str(item.get("title", item.get("board", ""))),
                ws_board=bool(item.get("ws_board", 0)),
            )
            for item in raw_boards
            if item.get("board")
        ]
        self.repository.upsert_boards(boards)
        return boards

    def fast_scan(
        self,
        boards: Sequence[str],
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[ThreadRecord]:
        try:
            self.refresh_boards()
        except ApiCancelled:
            raise
        except Exception:
            if not self.repository.list_boards():
                raise

        records: list[ThreadRecord] = []
        total = len(boards)
        for index, board in enumerate(boards, start=1):
            if self.cancel_event.is_set():
                break
            self._wait_if_paused()
            try:
                result = self.api.fetch_catalog(
                    board, cancel_event=self.cancel_event
                )
                parsed = parse_catalog(board, result.data)
                for record in parsed:
                    record.stale = result.stale
                    self.repository.upsert_thread(record)
                records.extend(parsed)
                message = (
                    f"/{board}/: кэш без изменений"
                    if result.not_modified
                    else f"/{board}/: {len(parsed)} тредов"
                )
                if result.stale:
                    message = f"/{board}/: показан сохранённый кэш"
            except ApiNotFound as exc:
                self.repository.mark_board_unavailable(board, str(exc))
                message = f"/{board}/: каталог недоступен"
            except ApiCancelled:
                raise
            except Exception as exc:
                self.repository.mark_board_unavailable(board, str(exc))
                message = f"/{board}/: ошибка — {exc}"
            if on_progress:
                on_progress(index, total, None, message)
        return records

    def deep_scan(
        self,
        records: Sequence[ThreadRecord],
        filters: ScanFilters,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[ThreadRecord]:
        completed: list[ThreadRecord] = []
        total = len(records)
        for index, record in enumerate(records, start=1):
            if self.cancel_event.is_set():
                break
            self._wait_if_paused()
            try:
                result = self.api.fetch_thread(
                    record.board,
                    record.thread_id,
                    cancel_event=self.cancel_event,
                )
                exact = parse_thread(record.board, result.data, filters)
                exact.stale = result.stale
                self._persist_exact(exact)
                completed.append(exact)
                message = (
                    f"/{record.board}/{record.thread_id}: "
                    f"{exact.file_count} подходящих файлов"
                )
            except ApiNotFound:
                record.status = ThreadStatus.EXPIRED
                record.updated_at = time.time()
                self.repository.mark_thread_status(
                    record.board,
                    record.thread_id,
                    ThreadStatus.EXPIRED,
                    snippet="Тред больше недоступен",
                )
                completed.append(record)
                message = f"/{record.board}/{record.thread_id}: тред недоступен"
            except ApiCancelled:
                raise
            except Exception as exc:
                record.status = ThreadStatus.ERROR
                record.updated_at = time.time()
                self.repository.mark_thread_status(
                    record.board,
                    record.thread_id,
                    ThreadStatus.ERROR,
                    snippet=str(exc),
                )
                completed.append(record)
                message = f"/{record.board}/{record.thread_id}: ошибка — {exc}"
            if on_progress:
                on_progress(index, total, completed[-1], message)
        return completed

    def fetch_direct(
        self,
        board: str,
        thread_id: int,
        filters: ScanFilters,
    ) -> ThreadRecord:
        self._wait_if_paused()
        result = self.api.fetch_thread(
            board, thread_id, cancel_event=self.cancel_event
        )
        record = parse_thread(board, result.data, filters)
        record.stale = result.stale
        self._persist_exact(record)
        return record

    def _persist_exact(self, record: ThreadRecord) -> None:
        self.repository.upsert_thread(record)
        self.repository.upsert_attachments(record.attachments)
        saved = {item.key: item for item in self.repository.get_attachments(record.board, record.thread_id)}
        for item in record.attachments:
            previous = saved.get(item.key)
            if previous:
                item.local_path = previous.local_path
                item.thumbnail_path = previous.thumbnail_path

    def _wait_if_paused(self) -> None:
        while not self.pause_event.wait(0.1):
            if self.cancel_event.is_set():
                raise ApiCancelled("Операция отменена")
        if self.cancel_event.is_set():
            raise ApiCancelled("Операция отменена")


def thumbnail_url(board: str, post: dict) -> str:
    if post.get("tim") and not post.get("spoiler") and not post.get("filedeleted"):
        return f"https://i.4cdn.org/{board}/{int(post['tim'])}s.jpg"
    return ""


def parse_catalog(board: str, payload: object) -> list[ThreadRecord]:
    if not isinstance(payload, list):
        return []
    records: list[ThreadRecord] = []
    now = time.time()
    for page in payload:
        if not isinstance(page, dict):
            continue
        for item in page.get("threads", []):
            if not isinstance(item, dict) or not item.get("no"):
                continue
            thread_id = int(item["no"])
            semantic_url = str(item.get("semantic_url", "") or "")
            title = str(
                item.get("sub")
                or semantic_url
                or item.get("filename")
                or f"thread_{thread_id}"
            ).strip()
            snippet = strip_html(str(item.get("com", "")))[:240]
            estimated = int(item.get("images", 0) or 0) + int(bool(item.get("tim") and not item.get("filedeleted")))
            records.append(
                ThreadRecord(
                    board=board,
                    thread_id=thread_id,
                    title=strip_html(title),
                    semantic_url=semantic_url,
                    source_url=source_thread_url(board, thread_id),
                    file_count=estimated,
                    total_size=None,
                    accuracy=Accuracy.ESTIMATED,
                    status=ThreadStatus.AVAILABLE,
                    snippet=snippet,
                    updated_at=now,
                    thumbnail_url=thumbnail_url(board, item),
                )
            )
    return records


def parse_thread(
    board: str,
    payload: object,
    filters: ScanFilters,
) -> ThreadRecord:
    posts = payload.get("posts", []) if isinstance(payload, dict) else []
    if not posts:
        raise ValueError("Ответ API не содержит постов")
    op = posts[0]
    thread_id = int(op.get("no", 0) or 0)
    semantic_url = str(op.get("semantic_url", "") or "")
    title = str(
        op.get("sub")
        or semantic_url
        or op.get("filename")
        or f"thread_{thread_id}"
    ).strip()
    attachments, snippet = collect_attachments(board, posts, filters)
    if len(attachments) < filters.min_matched_files:
        attachments = []
    total_size = (sum(item.size or 0 for item in attachments)
                  if all(item.size is not None for item in attachments) else None)
    return ThreadRecord(
        board=board,
        thread_id=thread_id,
        title=strip_html(title),
        semantic_url=semantic_url,
        source_url=source_thread_url(board, thread_id),
        file_count=len(attachments),
        total_size=total_size,
        accuracy=Accuracy.EXACT,
        status=ThreadStatus.AVAILABLE,
        snippet=snippet or strip_html(str(op.get("com", "")))[:240],
        updated_at=time.time(),
        attachments=attachments,
        thumbnail_url=thumbnail_url(board, op),
    )


def collect_attachments(
    board: str,
    posts: Iterable[dict],
    filters: ScanFilters,
) -> tuple[list[Attachment], str]:
    normalized_posts: list[tuple[dict, str, bool]] = []
    first_match = ""
    for post in posts:
        text = extract_post_text(post)
        matched = matches_keywords(text, filters.keywords)
        if filters.keywords and matched and not first_match:
            first_match = text[:240]
        normalized_posts.append((post, text, matched))

    thread_matches = not filters.keywords or any(item[2] for item in normalized_posts)
    if filters.keyword_scope is KeywordScope.WHOLE_THREAD and not thread_matches:
        return [], ""

    attachments: list[Attachment] = []
    for post, _text, post_matches in normalized_posts:
        ext = str(post.get("ext", "") or "").casefold()
        tim = post.get("tim")
        if not ext or ext not in filters.extensions or not tim or post.get("filedeleted"):
            continue
        size = post.get("fsize")
        size = int(size) if isinstance(size, int) else None
        if filters.min_file_size is not None and (
            size is None or size < filters.min_file_size
        ):
            continue
        if filters.max_file_size is not None and (
            size is None or size > filters.max_file_size
        ):
            continue
        if (
            filters.keywords
            and filters.keyword_scope is KeywordScope.FILE_POST
            and not post_matches
        ):
            continue
        filename = str(post.get("filename", "unnamed") or "unnamed")
        attachments.append(
            Attachment(
                board=board,
                thread_id=int(posts_thread_id(normalized_posts)),
                post_id=int(post.get("no", 0) or 0),
                tim=int(tim),
                ext=ext,
                filename=filename,
                display_name=sanitize_filename(filename),
                size=size,
                keyword_match=post_matches,
                mime_type=mimetypes.guess_type(f"file{ext}")[0],
            )
        )
    return attachments, first_match


def posts_thread_id(posts: Sequence[tuple[dict, str, bool]]) -> int:
    return int(posts[0][0].get("no", 0) or 0) if posts else 0


def extract_post_text(post: dict) -> str:
    values = (
        str(post.get("sub", "") or ""),
        strip_html(str(post.get("com", "") or "")),
        str(post.get("filename", "") or ""),
    )
    return " ".join(item for item in values if item).strip()


def matches_keywords(text: str, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    haystack = text.casefold()
    return any(keyword in haystack for keyword in keywords)


def sanitize_filename(value: str | None) -> str:
    name = value or "unnamed"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip(". ")
    return (name[:180] or "unnamed").strip()


def strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def source_thread_url(board: str, thread_id: int) -> str:
    return f"{SOURCE_BASE_URL}/{board}/thread/{thread_id}"


def api_thread_url(board: str, thread_id: int) -> str:
    return f"{API_BASE_URL}/{board}/thread/{thread_id}.json"


_THREAD_URL_PATTERN = re.compile(
    r"^/(?P<board>[a-z0-9_]+)/thread/(?P<thread_id>\d+)(?:\.json)?/?$",
    re.IGNORECASE,
)


def parse_thread_url(value: str) -> tuple[str, int]:
    parsed = urlparse(value.strip())
    allowed_hosts = {
        "boards.4chan.org",
        "boards.4channel.org",
        "a.4cdn.org",
    }
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() not in allowed_hosts:
        raise ValueError("Поддерживаются только ссылки на треды 4chan/4channel")
    match = _THREAD_URL_PATTERN.match(parsed.path)
    if not match:
        raise ValueError("Не удалось распознать доску и ID треда")
    return match.group("board").casefold(), int(match.group("thread_id"))


def format_size(size: int | None) -> str:
    if size is None:
        return "—"
    if size == 0:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
