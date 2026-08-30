"""Textual user interface for Threadgoon 6.0."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Sequence

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RichLog,
    Select,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from .api import ApiClient
from .config import AppConfig, ConfigurationError, save_config
from .database import Repository
from .downloader import DownloadManager
from .models import (
    Accuracy,
    BoardInfo,
    DownloadJob,
    DownloadStatus,
    KeywordScope,
    ThreadRecord,
    ThreadStatus,
    WatchRecord,
    WatchStatus,
)
from .paths import AppPaths
from .preview import MetadataPreviewProvider
from .scanner import ScanFilters, ScanService, format_size, parse_thread_url
from .watcher import WatchService


HELP_MARKDOWN = """
# Threadgoon 6.0

**Источник данных:** [4chan](https://www.4chan.org/). Приложение не является
официальным продуктом 4chan.

- / — поиск по каталогу
- Space — выбрать текущий тред
- Enter — открыть подробности
- d — подготовить выбранные треды к загрузке
- w — добавить выбранные треды в мониторинг
- r — обновить каталоги
- ? — открыть эту справку
- q — выйти

Быстрое сканирование читает только каталоги. Deep scan получает точные файлы
выбранных тредов через общую очередь API с интервалом не менее одной секунды.
Все данные приложения находятся в .threadgoon/ и output/ этого проекта.
"""


class BoardSelectionScreen(ModalScreen[list[str] | None]):
    """Choose boards without leaving the catalog."""

    def __init__(self, boards: Sequence[BoardInfo], selected: Sequence[str]):
        super().__init__()
        known = {item.board for item in boards}
        merged = list(boards)
        merged.extend(
            BoardInfo(board=item, title=item, ws_board=False)
            for item in selected
            if item not in known
        )
        self.boards = sorted(merged, key=lambda item: item.board)
        self.selected = set(selected)

    def compose(self) -> ComposeResult:
        choices = [
            (
                f"/{item.board}/ — {item.title}"
                + ("" if item.available else " · недоступна"),
                item.board,
                item.board in self.selected,
            )
            for item in self.boards
        ]
        with Vertical(classes="modal-card"):
            yield Static("Доски для сканирования", classes="modal-title")
            yield Static(
                "Недоступные доски можно оставить в наборе: ошибка одной доски "
                "не остановит остальные.",
                classes="muted",
            )
            yield SelectionList(*choices, id="board-selection")
            with Horizontal(classes="modal-actions"):
                yield Button("Отмена", id="cancel")
                yield Button("Сохранить", id="save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            selection = self.query_one("#board-selection", SelectionList)
            self.dismiss([str(item) for item in selection.selected])


class ConfirmDownloadScreen(ModalScreen[bool]):
    """Confirm exact download count and size."""

    def __init__(self, records: Sequence[ThreadRecord]):
        super().__init__()
        self.records = records

    def compose(self) -> ComposeResult:
        files = sum(item.file_count for item in self.records)
        total = sum(item.total_size or 0 for item in self.records)
        with Vertical(classes="modal-card compact-modal"):
            yield Static("Подтвердите загрузку", classes="modal-title")
            yield Static(
                f"Тредов: {len(self.records)}\n"
                f"Файлов: {files}\n"
                f"Общий объём: {format_size(total)}"
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Отмена", id="cancel")
                yield Button("Добавить в очередь", id="confirm", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class ThreadDetailScreen(Screen[None]):
    """Metadata-only details screen with a future preview boundary."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(self, record: ThreadRecord):
        super().__init__()
        self.record = record
        self.preview = MetadataPreviewProvider()

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-screen"):
            with Horizontal(classes="screen-title-row"):
                yield Button("← Назад", id="back")
                yield Static(
                    f"/{self.record.board}/{self.record.thread_id} · {self.record.title}",
                    classes="screen-title",
                )
            with ScrollableContainer(id="detail-scroll"):
                yield Static(self._summary(), id="detail-summary")
                if self.record.attachments:
                    for attachment in self.record.attachments[:200]:
                        yield Static(
                            self.preview.describe(attachment),
                            classes="attachment-row",
                        )
                else:
                    yield Static(
                        "Точные вложения ещё не загружены. Запустите deep scan.",
                        classes="empty-state",
                    )

    def _summary(self) -> str:
        accuracy = "точные данные" if self.record.accuracy is Accuracy.EXACT else "оценка"
        return (
            f"Источник: {self.record.source_url}\n"
            f"Файлов: {self.record.file_count} · {format_size(self.record.total_size)} "
            f"· {accuracy}\n"
            f"Статус: {self.record.status.value}\n\n"
            f"{self.record.snippet or 'Описание отсутствует'}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()


class ThreadgoonApp(App[None]):
    """Full-screen project-local Threadgoon application."""

    TITLE = "Threadgoon 6.0"
    SUB_TITLE = "локальный каталог и загрузки"
    CSS_PATH = "styles.tcss"
    MIN_SIZE = (80, 24)

    BINDINGS = [
        Binding("/", "focus_search", "Поиск"),
        Binding("space", "toggle_selected", "Выбрать"),
        Binding("enter", "show_details", "Подробнее"),
        Binding("d", "queue_download", "Скачать"),
        Binding("w", "watch_selected", "Мониторинг"),
        Binding("r", "refresh", "Обновить"),
        Binding("?", "help", "Справка"),
        Binding("q", "quit", "Выход"),
    ]

    def __init__(
        self,
        *,
        paths: AppPaths,
        config: AppConfig,
        config_path: Path,
        repository: Repository,
        api: ApiClient,
        scanner: ScanService,
        downloader: DownloadManager,
        watcher: WatchService,
        initial_urls: Sequence[str] = (),
        start_background: bool = True,
    ):
        super().__init__()
        self.paths = paths
        self.config = config
        self.config_path = config_path
        self.repository = repository
        self.api = api
        self.scanner = scanner
        self.downloader = downloader
        self.watcher = watcher
        self.initial_urls = list(initial_urls)
        self.start_background = start_background
        self.filters = ScanFilters.from_config(config)
        self.records: list[ThreadRecord] = []
        self.visible_records: list[ThreadRecord] = []
        self.selected_keys: set[tuple[str, int]] = set()
        self._prepared_downloads: list[ThreadRecord] = []
        self._sort_column = 1
        self._sort_reverse = False
        self._downloads_running = False
        self.logger = logging.getLogger("threadgoon.ui")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            "Готово · данные хранятся только внутри проекта",
            id="global-status",
        )
        with TabbedContent(initial="catalog", id="tabs"):
            with TabPane("Каталог", id="catalog"):
                with Horizontal(classes="toolbar"):
                    yield Button("Доски", id="choose-boards")
                    yield Button("Быстрое сканирование", id="fast-scan", variant="primary")
                    yield Button("Deep scan", id="deep-scan")
                with Horizontal(classes="toolbar"):
                    yield Button("Пауза", id="pause-scan")
                    yield Button("Отмена", id="cancel-scan")
                with Horizontal(classes="toolbar secondary"):
                    yield Input(
                        placeholder="Поиск по доске, ID или названию",
                        id="search",
                    )
                    yield Input(
                        placeholder="Вставьте URL треда и нажмите Enter",
                        id="thread-url",
                    )
                yield Static(
                    "Нажмите «Быстрое сканирование» или вставьте ссылку на тред.",
                    id="catalog-state",
                    classes="empty-state",
                )
                yield DataTable(
                    id="catalog-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            with TabPane("Загрузки", id="downloads"):
                with Horizontal(classes="toolbar"):
                    yield Button("Запустить", id="start-downloads", variant="primary")
                    yield Button("Пауза", id="pause-downloads")
                    yield Button("Продолжить", id="resume-downloads")
                    yield Button("Отменить", id="cancel-download")
                with Horizontal(classes="toolbar"):
                    yield Button("Повторить ошибки", id="retry-downloads")
                    yield Button("Разрешить ожидающие", id="approve-downloads")
                yield Static("Очередь пуста", id="downloads-state", classes="empty-state")
                yield DataTable(
                    id="downloads-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            with TabPane("Мониторинг", id="watch"):
                with Horizontal(classes="toolbar"):
                    yield Button("Пауза / продолжить", id="toggle-watch")
                    yield Button("Автозагрузка", id="toggle-watch-auto")
                    yield Button("Удалить", id="remove-watch")
                yield Static(
                    "Мониторинг работает только пока открыт этот TUI.",
                    classes="notice",
                )
                yield DataTable(
                    id="watch-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            with TabPane("История", id="history"):
                yield DataTable(
                    id="history-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            with TabPane("Настройки", id="settings"):
                with ScrollableContainer(id="settings-scroll"):
                    yield Label("Доски через запятую", classes="field-label")
                    yield Input(",".join(self.config.boards), id="settings-boards")
                    yield Label("Расширения через запятую", classes="field-label")
                    yield Input(",".join(self.config.extensions), id="settings-extensions")
                    yield Label("Ключевые слова через запятую", classes="field-label")
                    yield Input(",".join(self.config.keywords), id="settings-keywords")
                    yield Label("Область ключевых слов", classes="field-label")
                    yield Select(
                        [
                            ("Пост с файлом", KeywordScope.FILE_POST.value),
                            ("Весь тред", KeywordScope.WHOLE_THREAD.value),
                        ],
                        value=self.config.keyword_scope.value,
                        allow_blank=False,
                        id="settings-scope",
                    )
                    yield Label("Минимальный размер, байт", classes="field-label")
                    yield Input(
                        "" if self.config.min_file_size is None else str(self.config.min_file_size),
                        type="integer",
                        id="settings-min-size",
                    )
                    yield Label("Максимальный размер, байт", classes="field-label")
                    yield Input(
                        "" if self.config.max_file_size is None else str(self.config.max_file_size),
                        type="integer",
                        id="settings-max-size",
                    )
                    yield Label("Папка загрузок внутри проекта", classes="field-label")
                    yield Input(self.config.output_dir, id="settings-output")
                    yield Label("Интервал API, секунд", classes="field-label")
                    yield Input(str(self.config.api_interval), type="number", id="settings-api")
                    yield Label("Интервал мониторинга, секунд", classes="field-label")
                    yield Input(
                        str(self.config.watch_interval),
                        type="integer",
                        id="settings-watch",
                    )
                    yield Label("Параллельных загрузок, 1–3", classes="field-label")
                    yield Input(
                        str(self.config.media_workers),
                        type="integer",
                        id="settings-workers",
                    )
                    with Horizontal(classes="switch-row"):
                        yield Switch(
                            self.config.auto_download_new,
                            id="settings-auto-download",
                        )
                        yield Label("Автоматически загружать новые вложения")
                    yield Button("Сохранить настройки", id="save-settings", variant="primary")
                    yield Static("", id="settings-state")
            with TabPane("Журнал", id="logs"):
                yield RichLog(
                    id="log-view",
                    wrap=True,
                    markup=True,
                    max_lines=1000,
                )
            with TabPane("Справка", id="help"):
                yield Markdown(HELP_MARKDOWN)
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self._reload_from_database()
        self._load_log_tail()
        if self.start_background:
            self._start_watch_worker()
        if self.initial_urls:
            self._import_urls(self.initial_urls)

    def on_unmount(self) -> None:
        self.scanner.cancel()
        self.downloader.stop()
        self.watcher.stop()
        self.api.close()

    def _setup_tables(self) -> None:
        catalog = self.query_one("#catalog-table", DataTable)
        catalog.add_columns(
            "✓", "Доска", "ID", "Название", "Файлы", "Размер", "Точность", "Статус", "Обновлён"
        )
        downloads = self.query_one("#downloads-table", DataTable)
        downloads.add_columns(
            "ID", "Тред", "Файл", "Статус", "Получено", "Размер", "Ошибка"
        )
        watches = self.query_one("#watch-table", DataTable)
        watches.add_columns(
            "Тред", "Интервал", "Авто", "Статус", "Последняя проверка", "Ошибка"
        )
        history = self.query_one("#history-table", DataTable)
        history.add_columns(
            "ID", "Тред", "Файл", "Статус", "Размер", "Обновлено"
        )

    def _reload_from_database(self) -> None:
        self.records = self.repository.list_threads()
        self._render_catalog()
        self._render_downloads()
        self._render_watches()
        self._render_history()

    def _render_catalog(self) -> None:
        search = self.query_one("#search", Input).value.casefold().strip()
        records = [
            item
            for item in self.records
            if not search
            or search in item.board.casefold()
            or search in str(item.thread_id)
            or search in item.title.casefold()
        ]
        records.sort(key=self._sort_key, reverse=self._sort_reverse)
        self.visible_records = records
        table = self.query_one("#catalog-table", DataTable)
        table.clear()
        for item in records:
            table.add_row(
                "●" if item.key in self.selected_keys else "",
                f"/{item.board}/",
                str(item.thread_id),
                item.title,
                str(item.file_count),
                format_size(item.total_size),
                "точно" if item.accuracy is Accuracy.EXACT else "оценка",
                _thread_status(item.status),
                _format_time(item.updated_at),
                key=f"{item.board}:{item.thread_id}",
            )
        state = self.query_one("#catalog-state", Static)
        if records:
            state.update(
                f"Показано {len(records)} тредов · выбрано {len(self.selected_keys)}"
            )
            state.remove_class("empty-state")
        else:
            state.update(
                "Каталог пуст. Запустите быстрое сканирование или измените поиск."
            )
            state.add_class("empty-state")

    def _sort_key(self, record: ThreadRecord) -> object:
        keys = (
            record.key in self.selected_keys,
            record.board,
            record.thread_id,
            record.title.casefold(),
            record.file_count,
            record.total_size or -1,
            record.accuracy.value,
            record.status.value,
            record.updated_at,
        )
        return keys[self._sort_column]

    def _render_downloads(self) -> None:
        jobs = self.repository.list_downloads()
        table = self.query_one("#downloads-table", DataTable)
        table.clear()
        for job in jobs:
            table.add_row(
                str(job.id),
                f"/{job.board}/{job.thread_id}",
                Path(job.destination).name,
                _download_status(job.status),
                format_size(job.downloaded_size),
                format_size(job.expected_size),
                job.error or "",
                key=str(job.id),
            )
        state = self.query_one("#downloads-state", Static)
        state.update(f"В очереди {len(jobs)} заданий" if jobs else "Очередь пуста")
        state.set_class(not jobs, "empty-state")

    def _render_watches(self) -> None:
        table = self.query_one("#watch-table", DataTable)
        table.clear()
        for watch in self.repository.list_watches():
            table.add_row(
                f"/{watch.board}/{watch.thread_id}",
                f"{watch.interval} с",
                "да" if watch.auto_download else "нет",
                watch.status.value,
                _format_time(watch.last_checked),
                watch.last_error or "",
                key=f"{watch.board}:{watch.thread_id}",
            )

    def _render_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        for job in reversed(self.repository.list_downloads()):
            table.add_row(
                str(job.id),
                f"/{job.board}/{job.thread_id}",
                Path(job.destination).name,
                _download_status(job.status),
                format_size(job.expected_size or job.downloaded_size),
                _format_time(job.updated_at),
                key=str(job.id),
            )

    def _current_record(self) -> ThreadRecord | None:
        table = self.query_one("#catalog-table", DataTable)
        index = table.cursor_row
        if 0 <= index < len(self.visible_records):
            return self.visible_records[index]
        return None

    def _selected_records(self) -> list[ThreadRecord]:
        selected = [item for item in self.records if item.key in self.selected_keys]
        current = self._current_record()
        if not selected and current:
            selected = [current]
        return selected

    def action_focus_search(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "catalog"
        self.query_one("#search", Input).focus()

    def action_toggle_selected(self) -> None:
        if not self.query_one("#catalog-table", DataTable).has_focus:
            return
        record = self._current_record()
        if not record:
            return
        if record.key in self.selected_keys:
            self.selected_keys.remove(record.key)
        else:
            self.selected_keys.add(record.key)
        self._render_catalog()

    def action_show_details(self) -> None:
        if not self.query_one("#catalog-table", DataTable).has_focus:
            return
        record = self._current_record()
        if not record:
            return
        stored = self.repository.get_thread(record.board, record.thread_id) or record
        self.push_screen(ThreadDetailScreen(stored))

    def action_refresh(self) -> None:
        self._start_fast_scan()

    def action_help(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "help"

    def action_queue_download(self) -> None:
        records = self._selected_records()
        if records:
            self._prepare_downloads(records)

    def action_watch_selected(self) -> None:
        records = self._selected_records()
        if records:
            self._prepare_watches(records)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._render_catalog()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "thread-url" and event.value.strip():
            self._import_urls([event.value.strip()])
            event.input.value = ""

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.data_table.id != "catalog-table":
            return
        if self._sort_column == event.column_index:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = event.column_index
            self._sort_reverse = False
        self._render_catalog()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "catalog-table":
            self.action_show_details()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "choose-boards":
            self._choose_boards()
        elif action == "fast-scan":
            self._start_fast_scan()
        elif action == "deep-scan":
            records = self._selected_records() or self.visible_records
            self._start_deep_scan(records)
        elif action == "pause-scan":
            if self.scanner.pause_event.is_set():
                self.scanner.pause()
                event.button.label = "Продолжить"
                self._set_status("Deep scan приостановлен")
            else:
                self.scanner.resume()
                event.button.label = "Пауза"
                self._set_status("Deep scan продолжен")
        elif action == "cancel-scan":
            self.scanner.cancel()
            self._set_status("Сканирование отменяется")
        elif action == "start-downloads":
            self._ensure_download_worker()
        elif action == "pause-downloads":
            self.downloader.pause()
            self._set_status("Загрузки приостановлены")
        elif action == "resume-downloads":
            self.downloader.resume()
            self._render_downloads()
            self._ensure_download_worker()
        elif action == "retry-downloads":
            for job in self.repository.list_downloads(
                [DownloadStatus.FAILED, DownloadStatus.CANCELLED]
            ):
                self.downloader.retry(job.id)
            self._render_downloads()
            self._ensure_download_worker()
        elif action == "approve-downloads":
            self.downloader.approve_waiting()
            self._render_downloads()
            self._ensure_download_worker()
        elif action == "cancel-download":
            job = self._current_download()
            if job:
                self.downloader.cancel_job(job.id)
                self._render_downloads()
        elif action == "toggle-watch":
            watch = self._current_watch()
            if watch:
                watch.status = (
                    WatchStatus.ACTIVE
                    if watch.status is WatchStatus.PAUSED
                    else WatchStatus.PAUSED
                )
                self.repository.add_watch(watch)
                self._render_watches()
        elif action == "toggle-watch-auto":
            watch = self._current_watch()
            if watch:
                watch.auto_download = not watch.auto_download
                self.repository.add_watch(watch)
                self._render_watches()
        elif action == "remove-watch":
            watch = self._current_watch()
            if watch:
                self.watcher.remove(watch.board, watch.thread_id)
                self._render_watches()
        elif action == "save-settings":
            self._save_settings()

    def _current_download(self) -> DownloadJob | None:
        table = self.query_one("#downloads-table", DataTable)
        jobs = self.repository.list_downloads()
        return jobs[table.cursor_row] if 0 <= table.cursor_row < len(jobs) else None

    def _current_watch(self) -> WatchRecord | None:
        table = self.query_one("#watch-table", DataTable)
        watches = self.repository.list_watches()
        return (
            watches[table.cursor_row]
            if 0 <= table.cursor_row < len(watches)
            else None
        )

    def _choose_boards(self) -> None:
        boards = self.repository.list_boards()

        def apply_selection(selected: list[str] | None) -> None:
            if selected is None:
                return
            self.config.boards = selected
            self.query_one("#settings-boards", Input).value = ",".join(selected)
            save_config(self.config, self.paths, self.config_path)
            self._set_status(f"Выбрано досок: {len(selected)}")

        self.push_screen(
            BoardSelectionScreen(boards, self.config.boards),
            apply_selection,
        )

    @work(thread=True, exclusive=True, group="scan")
    def _start_fast_scan(self) -> None:
        self.call_from_thread(self._set_status, "Загружаю каталоги…")
        try:
            self.scanner.fast_scan(
                self.config.boards,
                on_progress=lambda current, total, _record, message: self.call_from_thread(
                    self._scan_progress, current, total, message
                ),
            )
            self.call_from_thread(self._scan_finished, "Быстрое сканирование завершено")
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Сканирование не удалось: {exc}")

    @work(thread=True, exclusive=True, group="scan")
    def _start_deep_scan(self, records: Sequence[ThreadRecord]) -> None:
        self.call_from_thread(self._set_status, f"Deep scan: 0/{len(records)}")
        try:
            self.scanner.deep_scan(
                list(records),
                self.filters,
                on_progress=lambda current, total, _record, message: self.call_from_thread(
                    self._scan_progress, current, total, message
                ),
            )
            self.call_from_thread(self._scan_finished, "Deep scan завершён")
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Deep scan не удался: {exc}")

    def _scan_progress(self, current: int, total: int, message: str) -> None:
        self._set_status(f"{current}/{total} · {message}")
        self.records = self.repository.list_threads()
        self._render_catalog()

    def _scan_finished(self, message: str) -> None:
        self.records = self.repository.list_threads()
        self._render_catalog()
        self._set_status(message)

    @work(thread=True, exclusive=True, group="prepare-download")
    def _prepare_downloads(self, records: Sequence[ThreadRecord]) -> None:
        try:
            exact = self._ensure_exact(records)
            exact = [
                item
                for item in exact
                if item.status is ThreadStatus.AVAILABLE and item.attachments
            ]
            self.call_from_thread(self._confirm_downloads, exact)
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Не удалось подготовить загрузку: {exc}")

    def _confirm_downloads(self, records: list[ThreadRecord]) -> None:
        if not records:
            self._show_error("В выбранных тредах нет подходящих файлов")
            return
        self._prepared_downloads = records
        self.push_screen(ConfirmDownloadScreen(records), self._download_confirmed)

    def _download_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            self._prepared_downloads = []
            return
        count = 0
        for record in self._prepared_downloads:
            count += len(self.downloader.enqueue_attachments(record))
        self._prepared_downloads = []
        self._render_downloads()
        self.query_one("#tabs", TabbedContent).active = "downloads"
        self._set_status(f"В очередь добавлено файлов: {count}")
        self._ensure_download_worker()

    @work(thread=True, exclusive=True, group="prepare-watch")
    def _prepare_watches(self, records: Sequence[ThreadRecord]) -> None:
        try:
            exact = self._ensure_exact(records)
            for record in exact:
                if record.status is ThreadStatus.AVAILABLE:
                    self.watcher.add(
                        record.board,
                        record.thread_id,
                        interval=self.config.watch_interval,
                        auto_download=self.config.auto_download_new,
                    )
            self.call_from_thread(self._watches_prepared, len(exact))
        except Exception as exc:
            self.call_from_thread(self._show_error, f"Не удалось включить мониторинг: {exc}")

    def _watches_prepared(self, count: int) -> None:
        self._render_watches()
        self.query_one("#tabs", TabbedContent).active = "watch"
        self._set_status(f"Добавлено в мониторинг: {count}")

    def _ensure_exact(self, records: Sequence[ThreadRecord]) -> list[ThreadRecord]:
        exact: list[ThreadRecord] = []
        for record in records:
            stored = self.repository.get_thread(record.board, record.thread_id)
            if stored and stored.accuracy is Accuracy.EXACT:
                exact.append(stored)
                continue
            fetched = self.scanner.fetch_direct(
                record.board, record.thread_id, self.filters
            )
            exact.append(fetched)
        return exact

    @work(thread=True, exclusive=True, group="import")
    def _import_urls(self, urls: Sequence[str]) -> None:
        imported = 0
        errors: list[str] = []
        for value in urls:
            try:
                board, thread_id = parse_thread_url(value)
                self.scanner.fetch_direct(board, thread_id, self.filters)
                imported += 1
            except Exception as exc:
                errors.append(f"{value}: {exc}")
        self.call_from_thread(self._imports_finished, imported, errors)

    def _imports_finished(self, imported: int, errors: list[str]) -> None:
        self.records = self.repository.list_threads()
        self._render_catalog()
        if errors:
            self._show_error("; ".join(errors))
        else:
            self._set_status(f"Импортировано тредов: {imported}")

    def _ensure_download_worker(self) -> None:
        if self._downloads_running:
            return
        if not self.repository.list_downloads([DownloadStatus.PENDING]):
            return
        self._downloads_running = True
        self._download_worker()

    @work(thread=True, group="downloads")
    def _download_worker(self) -> None:
        try:
            self.downloader.process_pending(
                on_update=lambda job: self.call_from_thread(
                    self._download_updated, job
                )
            )
        finally:
            self.call_from_thread(self._downloads_finished)

    def _download_updated(self, _job: DownloadJob) -> None:
        self._render_downloads()
        self._render_history()

    def _downloads_finished(self) -> None:
        self._downloads_running = False
        self._render_downloads()
        self._render_history()
        if self.repository.list_downloads([DownloadStatus.PENDING]):
            self._ensure_download_worker()
        else:
            self._set_status("Очередь загрузок обработана")

    @work(thread=True, exclusive=True, group="watch-loop")
    def _start_watch_worker(self) -> None:
        self.watcher.run_forever(
            on_update=lambda watch, count, error: self.call_from_thread(
                self._watch_updated, watch, count, error
            )
        )

    def _watch_updated(
        self,
        watch: WatchRecord,
        new_count: int,
        error: str | None,
    ) -> None:
        self._render_watches()
        self._render_downloads()
        if error:
            self._set_status(
                f"/{watch.board}/{watch.thread_id}: {error}",
                error=True,
            )
        elif new_count:
            self._set_status(
                f"/{watch.board}/{watch.thread_id}: новых файлов {new_count}"
            )
            if watch.auto_download:
                self._ensure_download_worker()

    def _save_settings(self) -> None:
        try:
            config = AppConfig(
                boards=_csv(self.query_one("#settings-boards", Input).value),
                extensions=_csv(self.query_one("#settings-extensions", Input).value),
                keywords=_csv(self.query_one("#settings-keywords", Input).value),
                keyword_scope=KeywordScope(
                    self.query_one("#settings-scope", Select).value
                ),
                min_file_size=_optional_int(
                    self.query_one("#settings-min-size", Input).value
                ),
                max_file_size=_optional_int(
                    self.query_one("#settings-max-size", Input).value
                ),
                min_matched_files=self.config.min_matched_files,
                output_dir=self.query_one("#settings-output", Input).value.strip(),
                api_interval=float(self.query_one("#settings-api", Input).value),
                watch_interval=int(self.query_one("#settings-watch", Input).value),
                media_workers=int(self.query_one("#settings-workers", Input).value),
                auto_download_new=self.query_one(
                    "#settings-auto-download", Switch
                ).value,
            )
            config.validate(self.paths)
            save_config(config, self.paths, self.config_path)
        except (ConfigurationError, ValueError, TypeError) as exc:
            self.query_one("#settings-state", Static).update(f"Ошибка: {exc}")
            return
        self.config = config
        self.filters = ScanFilters.from_config(config)
        self.watcher.filters = self.filters
        self.api.limiter.interval = config.api_interval
        self.downloader.output_dir = self.paths.local_path(config.output_dir)
        self.downloader.output_dir.mkdir(parents=True, exist_ok=True)
        self.downloader.max_workers = config.media_workers
        self.query_one("#settings-state", Static).update(
            "Настройки сохранены внутри проекта"
        )
        self._set_status("Настройки сохранены")

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#global-status", Static)
        status.update(message)
        status.set_class(error, "error-state")
        self._log(message, error=error)

    def _show_error(self, message: str) -> None:
        self._set_status(message, error=True)

    def _log(self, message: str, *, error: bool = False) -> None:
        if not self.is_mounted:
            return
        log = self.query_one("#log-view", RichLog)
        style = "bold red" if error else "dim"
        log.write(f"[{style}]{datetime.now():%H:%M:%S}[/] {message}")

    def _load_log_tail(self) -> None:
        path = self.paths.logs / "threadgoon.log"
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            return
        log = self.query_one("#log-view", RichLog)
        for line in lines:
            log.write(line)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def _format_time(value: float | None) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%d.%m %H:%M")


def _thread_status(value: ThreadStatus) -> str:
    return {
        ThreadStatus.AVAILABLE: "доступен",
        ThreadStatus.UNAVAILABLE: "недоступен",
        ThreadStatus.EXPIRED: "завершён",
        ThreadStatus.ERROR: "ошибка",
    }[value]


def _download_status(value: DownloadStatus) -> str:
    return {
        DownloadStatus.WAITING: "ожидает подтверждения",
        DownloadStatus.PENDING: "в очереди",
        DownloadStatus.DOWNLOADING: "загрузка",
        DownloadStatus.PAUSED: "пауза",
        DownloadStatus.COMPLETED: "готово",
        DownloadStatus.FAILED: "ошибка",
        DownloadStatus.CANCELLED: "отменено",
    }[value]
