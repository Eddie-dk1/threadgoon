"""UI-independent application runtime and asynchronous command controller."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from .api import ApiCancelled, ApiClient, ApiNotFound
from .config import AppConfig, load_config, save_config
from .database import Repository
from .downloader import DownloadManager
from .logging_config import configure_logging
from .models import DownloadStatus, KeywordScope, ThreadRecord, ThreadStatus, WatchStatus
from .paths import AppPaths
from .scanner import ScanFilters, ScanService, parse_thread_url
from .watcher import WatchService

PROTOCOL_VERSION = 1
EventSink = Callable[[str, dict[str, Any]], None]


def thread_data(record: ThreadRecord) -> dict[str, Any]:
    data = asdict(record)
    data["id"] = f"{record.board}/{record.thread_id}"
    return data


@dataclass
class Operation:
    id: str
    group: str
    scanner: ScanService
    thread: threading.Thread | None = None
    state: str = "running"
    current: int = 0
    total: int = 0
    message: str = "Подготовка…"

    def data(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in
                ("id", "group", "state", "current", "total", "message")}


class Controller:
    """Own services, workers and the workspace lease for either desktop or TUI."""

    def __init__(self, root: Path | None = None, *, config_override: str | None = None,
                 verbose: bool = False, emit: EventSink | None = None,
                 start_background: bool = True):
        self.paths = AppPaths.build(root)
        self.paths.ensure()
        # Acquire the lease before configuration, migration, or recovery writes.
        self.repository = Repository(self.paths.database)
        try:
            self.config_path = self.paths.config_path(config_override)
            self.config = load_config(self.paths, self.config_path)
            configure_logging(self.paths, verbose=verbose)
            self.api = ApiClient(self.repository, interval=self.config.api_interval)
            self.scanner = ScanService(self.api, self.repository)
            self.downloader = DownloadManager(self.repository, self.paths,
                                              output_dir=self.config.output_dir,
                                              max_workers=self.config.media_workers)
            self.watcher = WatchService(self.api, self.repository, self.downloader,
                                        ScanFilters.from_config(self.config))
        except BaseException:
            self.repository.close()
            raise
        self.emit = emit or (lambda _event, _data: None)
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._operations: dict[str, Operation] = {}
        self._prepared: dict[str, list[ThreadRecord]] = {}
        self._download_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._closed = False
        self._workers: set[threading.Thread] = set()
        if start_background:
            self._watch_thread = threading.Thread(target=self._watch_loop, name="threadgoon-watch")
            self._workers.add(self._watch_thread)
            self._watch_thread.start()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            operations = [op.data() for op in self._operations.values()]
        return {
            "workspace": str(self.paths.root),
            "config": asdict(self.config),
            "threads": [thread_data(record) for record in self.repository.list_threads()],
            "boards": [asdict(board) for board in self.repository.list_boards()],
            "downloads": [asdict(job) for job in self.repository.list_downloads()],
            "watches": [dict(asdict(watch), id=f"{watch.board}/{watch.thread_id}")
                        for watch in self.repository.list_watches()],
            "download_paused": not self.downloader.pause_event.is_set(),
            "downloads_running": bool(self._download_thread and self._download_thread.is_alive()),
            "operations": operations,
        }

    def publish(self) -> None:
        if not self._closing.is_set():
            self.emit("snapshot", self.snapshot())

    def _filters(self, values: dict[str, Any] | None = None) -> ScanFilters:
        if values is None:
            return ScanFilters.from_config(self.config)
        config = AppConfig(**{**asdict(self.config), **values})
        config.keyword_scope = KeywordScope(config.keyword_scope)
        config.validate(self.paths)
        return ScanFilters.from_config(config)

    @staticmethod
    def _keys(values: Sequence[str]) -> list[tuple[str, int]]:
        keys = []
        for value in dict.fromkeys(values):
            board, thread = value.split("/", 1)
            # Use the same validation as imported URLs.
            keys.append(parse_thread_url(f"https://boards.4chan.org/{board}/thread/{thread}"))
        return keys

    def exact_records(self, keys: Sequence[tuple[str, int]], filters: ScanFilters,
                      scanner: ScanService | None = None) -> list[ThreadRecord]:
        """Revalidate conditional cache and apply today's filters, never stored matches."""
        scanner = scanner or ScanService(self.api, self.repository)
        records = []
        for board, thread_id in keys:
            record = scanner.fetch_direct(board, thread_id, filters)
            records.append(record)
        return records

    def _start(self, group: str, task: Callable[[Operation], dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self._closing.is_set():
                raise RuntimeError("Приложение завершает работу")
            if any(op.group == group for op in self._operations.values()):
                raise ValueError("Эта операция уже выполняется")
            op = Operation(uuid.uuid4().hex, group, ScanService(self.api, self.repository))
            self._operations[op.id] = op
            op.thread = threading.Thread(target=self._run, args=(op, task), name=f"threadgoon-{group}")
            self.emit("operation", op.data())
            self._workers = {worker for worker in self._workers if worker.is_alive()}
            self._workers.add(op.thread)
            op.thread.start()
            return {"operation_id": op.id}

    def _run(self, op: Operation, task: Callable[[Operation], dict[str, Any]]) -> None:
        try:
            result = task(op)
            op.state = "cancelled" if op.scanner.cancel_event.is_set() else "completed"
            data = {**op.data(), "result": result}
        except ApiCancelled:
            op.state = "cancelled"
            data = {**op.data(), "result": {}}
        except Exception as exc:
            logging.getLogger("threadgoon.controller").exception("Operation %s failed", op.group)
            op.state = "failed"
            data = {**op.data(), "error": str(exc), "result": {}}
        # Keep the operation registered until it has emitted all final events.
        if not self._closing.is_set():
            self.emit("operation_finished", data)
        with self._lock:
            self._operations.pop(op.id, None)
        self.publish()

    def _progress(self, op: Operation, current: int, total: int, message: str) -> None:
        op.current, op.total, op.message = current, total, message
        if not self._closing.is_set():
            self.emit("operation", op.data())

    def dispatch(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closing.is_set():
            raise RuntimeError("Приложение завершает работу")
        if command == "snapshot":
            return self.snapshot()
        if command == "refresh_boards":
            return self._start("boards", lambda op: {
                "boards": [asdict(board) for board in op.scanner.refresh_boards()]})
        if command == "search":
            boards = list(params.get("boards", self.config.boards))
            candidate = AppConfig(boards=boards)
            candidate.validate(self.paths)
            if not candidate.boards:
                raise ValueError("Выберите хотя бы одну доску")
            filters = self._filters(params.get("filters"))
            query = str(params.get("query", "")).strip().casefold()
            terms = [term.strip() for term in query.split(",") if term.strip()]
            exact = bool(params.get("exact", False))
            def search(op: Operation) -> dict[str, Any]:
                records = op.scanner.fast_scan(candidate.boards, on_progress=lambda n, total, _, msg:
                                               self._progress(op, n, total, msg))
                if op.scanner.cancel_event.is_set():
                    raise ApiCancelled("Операция отменена")
                if exact:
                    records = op.scanner.deep_scan(records, filters, on_progress=lambda n, total, record, msg:
                                                  self._search_progress(op, n, total, record, msg))
                    records = [record for record in records if record.status is ThreadStatus.AVAILABLE
                               and record.file_count >= filters.min_matched_files]
                else:
                    records = [record for record in records if not terms or any(term in
                               f"{record.board} {record.thread_id} {record.title} {record.snippet}".casefold()
                               for term in terms)]
                return {"threads": [thread_data(record) for record in records], "exact": exact,
                        "boards": [asdict(board) for board in self.repository.list_boards()]}
            return self._start("search", search)
        if command in {"pause_operation", "resume_operation", "cancel_operation"}:
            with self._lock:
                op = self._operations.get(str(params["operation_id"]))
                if op is None:
                    raise ValueError("Операция уже закончилась")
                if command == "cancel_operation":
                    op.scanner.cancel()
                    op.state = "cancelling"
                elif command == "pause_operation":
                    op.scanner.pause()
                    op.state = "paused"
                else:
                    op.scanner.resume()
                    op.state = "running"
                self.emit("operation", op.data())
                return op.data()
        if command == "detail":
            key = self._keys([params["key"]])[0]
            filters = self._filters(params.get("filters"))
            return self._start("detail", lambda op: {
                "thread": thread_data(self.exact_records([key], filters, op.scanner)[0])})
        if command == "deep_scan":
            keys = self._keys(params.get("keys", []))
            filters = self._filters(params.get("filters"))
            def deep(op: Operation) -> dict[str, Any]:
                records = [self.repository.get_thread(*key) for key in keys]
                records = [record for record in records if record is not None]
                results = op.scanner.deep_scan(records, filters, on_progress=lambda n, total, record, msg:
                                               self._search_progress(op, n, total, record, msg))
                return {"threads": [thread_data(record) for record in results]}
            return self._start("search", deep)
        if command == "import":
            keys = [parse_thread_url(url) for url in params.get("urls", [])]
            filters = self._filters(params.get("filters"))
            return self._start("import", lambda op: {
                "threads": [thread_data(record) for record in self.exact_records(keys, filters, op.scanner)]})
        if command == "prepare_download":
            keys = self._keys(params.get("keys", []))
            if not keys:
                raise ValueError("Выберите треды для скачивания")
            filters = self._filters(params.get("filters"))
            def prepare(op: Operation) -> dict[str, Any]:
                records = []
                warnings = []
                for i, key in enumerate(keys, 1):
                    try:
                        record = self.exact_records([key], filters, op.scanner)[0]
                        if record.stale:
                            raise ValueError("Нет связи: точный состав загрузки не подтверждён. Повторите при подключении.")
                        if record.attachments:
                            records.append(record)
                        else:
                            warnings.append(f"/{key[0]}/{key[1]}: нет подходящих файлов")
                    except ApiCancelled:
                        raise
                    except Exception as exc:
                        warnings.append(f"/{key[0]}/{key[1]}: {exc}")
                    self._progress(op, i, len(keys), "Уточняю файлы для скачивания…")
                if op.scanner.cancel_event.is_set():
                    raise ApiCancelled("Операция отменена")
                if not records:
                    raise ValueError(("; ".join(warnings) or "Нет подходящих файлов") + ". Измените фильтры или выберите другие треды.")
                token = uuid.uuid4().hex
                with self._lock:
                    self._prepared.clear()
                    self._prepared[token] = records
                sizes = [attachment.size for record in records for attachment in record.attachments]
                return {"token": token, "thread_count": len(records), "file_count": len(sizes),
                        "total_size": sum(sizes) if all(size is not None for size in sizes) else None,
                        "warnings": warnings}
            return self._start("prepare", prepare)
        if command == "discard_prepared":
            with self._lock:
                self._prepared.pop(str(params["token"]), None)
            return {}
        if command == "confirm_download":
            with self._lock:
                records = self._prepared.pop(str(params["token"]), None)
            if records is None:
                raise ValueError("Подтверждение устарело. Подготовьте загрузку снова.")
            count = 0
            for record in records:
                count += len(self.downloader.enqueue_attachments(record))
            self.downloader.resume()
            self.start_downloads()
            self.publish()
            return {"count": count}
        if command == "download_control":
            action = params["action"]
            ids = set(int(value) for value in params.get("ids", []))
            if action == "pause":
                self.downloader.pause()
            elif action in {"resume", "start"}:
                self.downloader.resume()
                self.start_downloads()
            elif action == "retry":
                for job in self.repository.list_downloads([DownloadStatus.FAILED, DownloadStatus.CANCELLED]):
                    if (not ids and job.status is DownloadStatus.FAILED) or job.id in ids:
                        self.downloader.retry(job.id)
                self.downloader.resume(include_paused=False)
                self.start_downloads()
            elif action == "cancel":
                if not ids:
                    raise ValueError("Выберите задания для отмены")
                for job in self.repository.list_downloads():
                    if job.id in ids and job.status is not DownloadStatus.COMPLETED:
                        self.downloader.cancel_job(job.id)
            elif action == "approve":
                if not ids:
                    raise ValueError("Выберите ожидающие файлы")
                self.downloader.approve_waiting(ids)
                self.downloader.resume(include_paused=False)
                self.start_downloads()
            else:
                raise ValueError("Неизвестное действие загрузки")
            self.publish()
            return {}
        if command == "watch_add":
            keys = self._keys(params.get("keys", []))
            if not keys:
                raise ValueError("Выберите треды для наблюдения")
            def add(op: Operation) -> dict[str, Any]:
                records = self.exact_records(keys, self._filters(params.get("filters")), op.scanner)
                for record in records:
                    if record.stale:
                        raise ValueError("Подключитесь к сети перед добавлением наблюдения")
                    self.watcher.add(record.board, record.thread_id, interval=self.config.watch_interval,
                                     auto_download=self.config.auto_download_new)
                return {"count": len(records)}
            return self._start("watch-add", add)
        if command in {"watch_update", "watch_remove"}:
            key = self._keys([params["key"]])[0]
            watch = next((w for w in self.repository.list_watches() if (w.board, w.thread_id) == key), None)
            if watch is None:
                raise ValueError("Наблюдение уже удалено")
            if command == "watch_remove":
                self.watcher.remove(*key)
            else:
                if "status" in params:
                    watch.status = WatchStatus(params["status"])
                if "auto_download" in params:
                    watch.auto_download = bool(params["auto_download"])
                self.repository.add_watch(watch)
            self.publish()
            return {}
        if command == "boards_save":
            if any(op.group == "search" for op in self._operations.values()):
                raise ValueError("Дождитесь завершения поиска")
            config = replace(self.config, boards=list(params["boards"]))
            config.validate(self.paths)
            save_config(config, self.paths, self.config_path)
            self.config = config
            self.publish()
            return {}
        if command == "settings_save":
            if self.busy:
                raise ValueError("Остановите операции перед изменением настроек")
            config = AppConfig(**params["config"])
            config.keyword_scope = KeywordScope(config.keyword_scope)
            config.validate(self.paths)
            save_config(config, self.paths, self.config_path)
            self.config = config
            self.watcher.filters = ScanFilters.from_config(config)
            self.api.limiter.interval = config.api_interval
            self.downloader.output_dir = self.paths.local_path(config.output_dir)
            self.downloader.output_dir.mkdir(parents=True, exist_ok=True)
            self.downloader.max_workers = config.media_workers
            self.publish()
            return {"config": asdict(config)}
        if command == "diagnostics":
            logfile = self.paths.logs / "threadgoon.log"
            return {"log": logfile.read_text(encoding="utf-8", errors="replace")[-32000:]
                    if logfile.exists() else "Журнал пока пуст"}
        raise ValueError(f"Неизвестная команда: {command}")

    def _search_progress(self, op: Operation, current: int, total: int,
                         record: ThreadRecord | None, message: str) -> None:
        self._progress(op, current, total, message)
        if record and record.status is ThreadStatus.AVAILABLE and record.file_count > 0:
            self.emit("search_result", {"operation_id": op.id, "thread": thread_data(record)})

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._operations or (self._download_thread and self._download_thread.is_alive()))

    def start_downloads(self) -> None:
        with self._lock:
            if self._closing.is_set() or (self._download_thread and self._download_thread.is_alive()):
                return
            if not self.repository.list_downloads([DownloadStatus.PENDING]):
                return
            self._download_thread = threading.Thread(target=self._download_loop, name="threadgoon-downloads")
            self._workers = {worker for worker in self._workers if worker.is_alive()}
            self._workers.add(self._download_thread)
            self._download_thread.start()

    def _download_loop(self) -> None:
        try:
            while not self._closing.is_set() and self.downloader.pause_event.is_set():
                if not self.repository.list_downloads([DownloadStatus.PENDING]):
                    break
                self.downloader.process_pending(on_update=lambda job: self.emit("download_update", asdict(job))
                                                if not self._closing.is_set() else None)
        except Exception as exc:
            logging.getLogger("threadgoon.controller").exception("Download worker failed")
            for job in self.repository.list_downloads([DownloadStatus.PENDING, DownloadStatus.DOWNLOADING]):
                self.repository.update_download(job.id, status=DownloadStatus.FAILED, error=str(exc))
        finally:
            with self._lock:
                self._download_thread = None
                # Enqueue can race with the worker's final empty-queue check.
                if not self._closing.is_set() and self.downloader.pause_event.is_set():
                    self.start_downloads()
            self.publish()

    def _watch_loop(self) -> None:
        self.watcher.run_forever(on_update=self._watch_updated)

    def _watch_updated(self, _watch, _count, _error) -> None:
        if self._closing.is_set():
            return
        if _watch.auto_download and _count:
            self.start_downloads()
        self.publish()

    def stop(self) -> None:
        with self._lock:
            self._closing.set()
            for op in self._operations.values():
                op.scanner.cancel()
            self.scanner.cancel()
            self.downloader.stop()
            self.watcher.stop()

    def close(self) -> None:
        if self._closed:
            return
        self.stop()
        with self._lock:
            threads = list(self._workers)
        for worker in threads:
            if worker and worker is not threading.current_thread():
                worker.join()
        self.api.close()
        self.repository.close()
        self._closed = True
