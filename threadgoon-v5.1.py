"""Extended multi-board Adult downloader based on the original thread downloader script."""

import html
import json
import logging
import os
import re
import sys
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout
from tqdm import tqdm
from urllib3.util.retry import Retry

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    Console = None
    Panel = None
    Table = None
    RICH_AVAILABLE = False


BASE_URL = "https://a.4cdn.org"
IMAGE_URL = "https://i.4cdn.org"
ALL_ADULT_BOARDS = [
    "aco",
    "b",
    "d",
    "gif",
    "h",
    "hc",
    "hm",
    "hr",
    "r",
    "s",
    "t",
    "u",
    "e",
]
ALLOWED_EXTENSIONS = [".webm", ".mp4", ".gif", ".jpg", ".jpeg", ".png"]
KEYWORDS: List[str] = []
MIN_MATCHED_FILES = 1
MIN_FILE_SIZE: Optional[int] = None
MAX_FILE_SIZE: Optional[int] = None
REQUEST_TIMEOUT = (10, 30)
MAX_WORKERS = 8
OUTPUT_DIR = "output"
CHUNK_SIZE = 8192
LOG_FILE = "download-v5.log"
PAGE_SIZE = 50


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
console = Console() if RICH_AVAILABLE else None


@dataclass
class MatchedFile:
    board: str
    thread_id: int
    post_id: int
    tim: int
    ext: str
    filename: str
    display_name: str
    size: Optional[int]
    post_text: str
    keyword_match: bool


@dataclass
class ThreadRecord:
    board: str
    thread_id: int
    title: str
    semantic_url: str
    matched_files: List[MatchedFile] = field(default_factory=list)
    matched_count: int = 0
    total_size: int = 0
    extensions: Set[str] = field(default_factory=set)
    keyword_match: bool = False


@dataclass
class SessionStats:
    processed_threads: int = 0
    matched_threads: int = 0
    downloaded_files: int = 0
    skipped_files: int = 0
    errors: int = 0
    downloaded_bytes: int = 0


def sanitize_filename(name: Optional[str]) -> str:
    if not name:
        return "unnamed"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name[:200] if len(name) > 200 else name or "unnamed"


def strip_html(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_catalog_url(board: str) -> str:
    return f"{BASE_URL}/{board}/catalog.json"


def get_thread_url(board: str, thread_id: int) -> str:
    return f"{BASE_URL}/{board}/thread/{thread_id}.json"


def get_file_url(board: str, tim: int, ext: str) -> str:
    return f"{IMAGE_URL}/{board}/{tim}{ext}"


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": "threadgoon-v5/1.0",
            "Accept": "application/json, text/plain, */*",
        }
    )
    return session


def format_size(size: Optional[int]) -> str:
    if not size:
        return "n/a"

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def normalize_extensions(extensions: Sequence[str]) -> Set[str]:
    normalized = set()
    for extension in extensions:
        if not extension:
            continue
        ext = extension.lower().strip()
        if not ext.startswith("."):
            ext = f".{ext}"
        normalized.add(ext)
    return normalized


def normalize_keywords(keywords: Sequence[str]) -> List[str]:
    return [keyword.casefold().strip() for keyword in keywords if keyword and keyword.strip()]


def fetch_catalog(board: str, session: requests.Session) -> List[Dict]:
    catalog_url = get_catalog_url(board)
    try:
        logger.info("Fetching catalog from %s", catalog_url)
        time.sleep(random.uniform(0.2, 0.8))
        response = session.get(catalog_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Timeout:
        logger.error("Timeout while fetching catalog from %s", catalog_url)
        raise
    except RequestException as exc:
        logger.error("Error fetching catalog for /%s/: %s", board, exc)
        raise
    except json.JSONDecodeError as exc:
        logger.error("Error parsing catalog JSON for /%s/: %s", board, exc)
        raise


def get_thread_data(board: str, thread_id: int, session: requests.Session) -> Dict:
    thread_url = get_thread_url(board, thread_id)
    try:
        logger.debug("Fetching thread data from %s", thread_url)
        time.sleep(random.uniform(0.2, 0.8))
        response = session.get(thread_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Timeout:
        logger.error("Timeout while fetching thread %s on /%s/", thread_id, board)
        raise
    except RequestException as exc:
        logger.error("Error fetching thread %s on /%s/: %s", thread_id, board, exc)
        raise
    except json.JSONDecodeError as exc:
        logger.error("Error parsing thread %s JSON on /%s/: %s", thread_id, board, exc)
        raise


def extract_post_text(post: Dict) -> str:
    parts = [
        post.get("sub", ""),
        strip_html(post.get("com", "")),
        post.get("filename", ""),
    ]
    return " ".join(part for part in parts if part).strip()


def matches_keywords(text: str, keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    haystack = text.casefold()
    return any(keyword in haystack for keyword in keywords)


def collect_matching_files(
    board: str,
    thread_data: Dict,
    allowed_extensions: Set[str],
    keywords: Sequence[str],
    min_file_size: Optional[int] = None,
    max_file_size: Optional[int] = None,
) -> Tuple[List[MatchedFile], bool]:
    posts = thread_data.get("posts", [])
    thread_id = int(posts[0].get("no", 0) or 0) if posts else 0
    file_candidates: List[MatchedFile] = []
    thread_keyword_match = False

    for post in posts:
        post_text = extract_post_text(post)
        post_keyword_match = matches_keywords(post_text, keywords) if keywords else False
        thread_keyword_match = thread_keyword_match or post_keyword_match

        ext = str(post.get("ext", "")).lower()
        tim = post.get("tim")
        if not ext or ext not in allowed_extensions or not tim:
            continue

        file_size = post.get("fsize")
        if min_file_size is not None and (file_size is None or file_size < min_file_size):
            continue
        if max_file_size is not None and file_size is not None and file_size > max_file_size:
            continue
        if keywords and not post_keyword_match:
            continue

        filename = post.get("filename", "unnamed")
        file_candidates.append(
            MatchedFile(
                board=board,
                thread_id=thread_id,
                post_id=int(post.get("no", 0) or 0),
                tim=int(tim),
                ext=ext,
                filename=str(filename),
                display_name=sanitize_filename(str(filename)),
                size=file_size if isinstance(file_size, int) else None,
                post_text=post_text,
                keyword_match=post_keyword_match,
            )
        )

    if keywords and not thread_keyword_match:
        return [], False

    return file_candidates, thread_keyword_match


def build_thread_record(
    board: str,
    thread_data: Dict,
    matched_files: List[MatchedFile],
    keyword_match: bool,
) -> ThreadRecord:
    posts = thread_data.get("posts", [])
    op_post = posts[0] if posts else {}
    thread_id = int(op_post.get("no", 0) or 0)
    title = (
        op_post.get("sub")
        or op_post.get("semantic_url")
        or op_post.get("filename")
        or f"thread_{thread_id}"
    )
    semantic_url = op_post.get("semantic_url", "") or ""

    total_size = sum(file.size for file in matched_files if file.size is not None)
    extensions = {file.ext for file in matched_files}

    return ThreadRecord(
        board=board,
        thread_id=thread_id,
        title=str(title).strip() or f"thread_{thread_id}",
        semantic_url=semantic_url,
        matched_files=matched_files,
        matched_count=len(matched_files),
        total_size=total_size,
        extensions=extensions,
        keyword_match=keyword_match,
    )


def get_record_title(record: ThreadRecord) -> str:
    return record.title or record.semantic_url or f"thread_{record.thread_id}"


def build_summary(
    records: Sequence[ThreadRecord],
    boards: Sequence[str],
    processed_threads: int,
) -> Dict[str, object]:
    extensions: Set[str] = set()
    matching_files = 0
    total_size = 0

    for record in records:
        matching_files += record.matched_count
        total_size += record.total_size
        extensions.update(record.extensions)

    unique_boards = sorted(set(boards))
    found_extensions = sorted(extensions)
    return {
        "boards_scanned": len(unique_boards),
        "threads_processed": processed_threads,
        "matching_threads": len(records),
        "matching_files": matching_files,
        "total_size_bytes": total_size,
        "total_size": format_size(total_size),
        "extensions_found": found_extensions,
        "extensions_display": ", ".join(found_extensions) if found_extensions else "n/a",
    }


def print_summary(summary: Dict[str, object]) -> None:
    if RICH_AVAILABLE and console and Table and Panel:
        summary_table = Table.grid(padding=(0, 2))
        summary_table.add_column(style="bold cyan")
        summary_table.add_column()
        summary_table.add_row("Boards scanned", str(summary["boards_scanned"]))
        summary_table.add_row("Threads processed", str(summary["threads_processed"]))
        summary_table.add_row("Matching threads", str(summary["matching_threads"]))
        summary_table.add_row("Matching files", str(summary["matching_files"]))
        summary_table.add_row("Total size", str(summary["total_size"]))
        summary_table.add_row("Extensions found", str(summary["extensions_display"]))
        console.print()
        console.print(Panel(summary_table, title="SUMMARY", border_style="cyan"))
        return

    print("\n" + "-" * 40)
    print("SUMMARY")
    print(f"Boards scanned: {summary['boards_scanned']}")
    print(f"Threads processed: {summary['threads_processed']}")
    print(f"Matching threads: {summary['matching_threads']}")
    print(f"Matching files: {summary['matching_files']}")
    print(f"Total size: {summary['total_size']}")
    print(f"Extensions found: {summary['extensions_display']}")
    print("-" * 40)


def print_download_summary(stats: SessionStats) -> None:
    if RICH_AVAILABLE and console and Table and Panel:
        summary_table = Table.grid(padding=(0, 2))
        summary_table.add_column(style="bold green")
        summary_table.add_column()
        summary_table.add_row("Processed threads", str(stats.processed_threads))
        summary_table.add_row("Matched threads", str(stats.matched_threads))
        summary_table.add_row("Downloaded files", str(stats.downloaded_files))
        summary_table.add_row("Skipped files", str(stats.skipped_files))
        summary_table.add_row("Errors", str(stats.errors))
        summary_table.add_row("Downloaded size", format_size(stats.downloaded_bytes))
        console.print()
        console.print(Panel(summary_table, title="DOWNLOAD SUMMARY", border_style="green"))
        return

    print("\n" + "=" * 60)
    print(f"Обработано тредов: {stats.processed_threads}")
    print(f"Найдено подходящих тредов: {stats.matched_threads}")
    print(f"Скачано файлов: {stats.downloaded_files}")
    print(f"Пропущено файлов: {stats.skipped_files}")
    print(f"Ошибок: {stats.errors}")
    print(f"Общий объём: {format_size(stats.downloaded_bytes)}")
    print("=" * 60)


def print_page(
    records: Sequence[ThreadRecord],
    page: int,
    page_size: int = PAGE_SIZE,
) -> None:
    total_records = len(records)
    total_pages = max(1, (total_records + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start_index = page * page_size
    end_index = min(start_index + page_size, total_records)
    header = f"Page {page + 1}/{total_pages} | showing {start_index}-{max(start_index, end_index - 1)} of {total_records}"

    if RICH_AVAILABLE and console and Table:
        table = Table(title=header, show_lines=False)
        table.add_column("Index", justify="right", style="bold cyan", no_wrap=True)
        table.add_column("Board", justify="center", style="magenta", no_wrap=True)
        table.add_column("Thread ID", justify="right", style="green", no_wrap=True)
        table.add_column("Files", justify="right", no_wrap=True)
        table.add_column("Exts", style="yellow", no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Title", overflow="fold")

        for index in range(start_index, end_index):
            record = records[index]
            table.add_row(
                str(index),
                record.board,
                str(record.thread_id),
                str(record.matched_count),
                ", ".join(sorted(record.extensions)) if record.extensions else "n/a",
                format_size(record.total_size),
                get_record_title(record),
            )

        console.print()
        console.print(table)
        return

    print(f"\n{header}")
    print("-" * 140)
    print(f"{'Index':>5} | {'Board':^7} | {'Thread ID':>10} | {'Files':>5} | {'Exts':<20} | {'Size':>10} | Title")
    print("-" * 140)
    for index in range(start_index, end_index):
        record = records[index]
        extensions = ", ".join(sorted(record.extensions)) if record.extensions else "n/a"
        print(
            f"{index:>5} | {record.board:^7} | {record.thread_id:>10} | {record.matched_count:>5} "
            f"| {extensions:<20} | {format_size(record.total_size):>10} | {get_record_title(record)}"
        )
    print("-" * 140)


def print_thread_list(records: Sequence[ThreadRecord]) -> None:
    print_page(records, page=0, page_size=max(1, len(records)))


def parse_selection_input(raw_value: str, max_index: int) -> Tuple[str, Optional[List[int] | int]]:
    if not raw_value:
        return "empty", None

    lowered = raw_value.casefold()
    if lowered == "n":
        return "next", None
    if lowered == "p":
        return "prev", None
    if lowered == "q":
        return "quit", None
    if lowered.startswith("g"):
        parts = raw_value.split()
        if len(parts) != 2 or parts[0].casefold() != "g":
            raise ValueError("Команда перехода должна быть в формате: g <номер_страницы>")
        try:
            page_number = int(parts[1])
        except ValueError as exc:
            raise ValueError(f"Некорректный номер страницы: {parts[1]}") from exc
        if page_number < 1:
            raise ValueError("Номер страницы должен быть >= 1")
        return "goto", page_number

    selected: List[int] = []
    for chunk in raw_value.split():
        if "-" in chunk:
            range_parts = chunk.split("-", 1)
            if len(range_parts) != 2 or not range_parts[0] or not range_parts[1]:
                logger.warning("Invalid range format: %s", chunk)
                print(f"Предупреждение: '{chunk}' не является корректным диапазоном, пропускаем")
                continue
            try:
                start_value = int(range_parts[0])
                end_value = int(range_parts[1])
            except ValueError:
                logger.warning("Invalid range format: %s", chunk)
                print(f"Предупреждение: '{chunk}' не является корректным диапазоном, пропускаем")
                continue

            range_start = min(start_value, end_value)
            range_end = max(start_value, end_value)
            selected.extend(range(range_start, range_end + 1))
            continue

        try:
            selected.append(int(chunk))
        except ValueError:
            logger.warning("Invalid choice format: %s", chunk)
            print(f"Предупреждение: '{chunk}' не является числом, пропускаем")

    selected = sorted(set(selected))
    invalid_choices = [value for value in selected if value < 0 or value > max_index]
    if invalid_choices:
        raise ValueError(
            f"Недопустимые номера: {invalid_choices}. Допустимый диапазон: 0-{max_index}"
        )
    return "select", selected


def parse_user_selection(raw_value: str, max_index: int) -> List[int]:
    action, value = parse_selection_input(raw_value, max_index)
    if action != "select":
        return []
    return value if isinstance(value, list) else []


def interactive_select_with_pagination(
    records: Sequence[ThreadRecord],
    page_size: int = PAGE_SIZE,
) -> List[int]:
    if not records:
        return []

    current_page = 0
    total_pages = max(1, (len(records) + page_size - 1) // page_size)
    max_index = len(records) - 1

    while True:
        print_page(records, current_page, page_size=page_size)
        help_text = "Команды: n = следующая, p = предыдущая, g <page> = перейти, q = выход, 1 5 10 или 10-20 = выбрать"
        if RICH_AVAILABLE and console:
            console.print(help_text, style="bold white")
        else:
            print(help_text)

        try:
            raw_selection = input("\nВведите команду или номера тредов: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nПрервано пользователем. Выход.")
            logger.info("Program interrupted by user during selection")
            sys.exit(0)

        try:
            action, value = parse_selection_input(raw_selection, max_index)
        except ValueError as exc:
            logger.error("Invalid selection command: %s", exc)
            print(f"Ошибка: {exc}")
            continue

        if action == "empty":
            logger.info("No selection made by user")
            return []
        if action == "quit":
            logger.info("User exited selection screen")
            return []
        if action == "next":
            if current_page < total_pages - 1:
                current_page += 1
            else:
                print("Уже открыта последняя страница")
            continue
        if action == "prev":
            if current_page > 0:
                current_page -= 1
            else:
                print("Уже открыта первая страница")
            continue
        if action == "goto":
            target_page = int(value) - 1 if isinstance(value, int) else 0
            if target_page < 0 or target_page >= total_pages:
                print(f"Ошибка: номер страницы вне диапазона 1-{total_pages}")
                continue
            current_page = target_page
            continue

        selected_indexes = value if isinstance(value, list) else []
        if not selected_indexes:
            print("Не выбрано ни одного валидного номера треда")
            continue
        return selected_indexes


def get_thread_directory(record: ThreadRecord, output_dir: str) -> str:
    base_title = sanitize_filename(record.semantic_url or record.title)
    directory_name = f"{record.thread_id}_{base_title}" if base_title else str(record.thread_id)
    return os.path.join(output_dir, record.board, directory_name)


def build_download_filename(matched_file: MatchedFile) -> str:
    safe_original = sanitize_filename(matched_file.display_name or matched_file.filename)
    return f"{matched_file.tim}_{safe_original}{matched_file.ext}"


def download_matching_files(
    thread_record: ThreadRecord,
    session: requests.Session,
    output_dir: str,
    stats: SessionStats,
) -> None:
    if not thread_record.matched_files:
        logger.info("No matched files for thread %s on /%s/", thread_record.thread_id, thread_record.board)
        return

    target_directory = get_thread_directory(thread_record, output_dir)
    try:
        os.makedirs(target_directory, exist_ok=True)
    except OSError as exc:
        logger.error("Error creating directory %s: %s", target_directory, exc)
        print(f"Ошибка при создании папки '{target_directory}': {exc}")
        stats.errors += 1
        return

    logger.info(
        "Downloading %s matched files from /%s/ thread %s",
        len(thread_record.matched_files),
        thread_record.board,
        thread_record.thread_id,
    )

    progress_label = f"/{thread_record.board}/ {thread_record.thread_id}"
    for matched_file in tqdm(thread_record.matched_files, desc=progress_label):
        file_url = get_file_url(matched_file.board, matched_file.tim, matched_file.ext)
        file_name = build_download_filename(matched_file)
        file_path = os.path.join(target_directory, file_name)
        download_success = False

        if os.path.exists(file_path):
            logger.info("File already exists, skipping: %s", file_path)
            stats.skipped_files += 1
            continue

        try:
            downloaded_size = 0
            time.sleep(random.uniform(0.2, 0.8))
            with session.get(file_url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()

                with open(file_path, "xb") as output_stream:
                    total_size = int(response.headers.get("content-length", 0))
                    if total_size > 0:
                        with tqdm(
                            total=total_size,
                            unit="B",
                            unit_scale=True,
                            unit_divisor=1024,
                            desc=file_name,
                            leave=False,
                        ) as file_progress:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                if not chunk:
                                    continue
                                output_stream.write(chunk)
                                downloaded_size += len(chunk)
                                file_progress.update(len(chunk))
                    else:
                        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                            if not chunk:
                                continue
                            output_stream.write(chunk)
                            downloaded_size += len(chunk)

            stats.downloaded_files += 1
            stats.downloaded_bytes += downloaded_size
            download_success = True
            logger.info("Downloaded file: %s", file_path)
            print(f"Скачан файл: {file_path}")

        except FileExistsError:
            logger.info("File was created while downloading, skipping: %s", file_path)
            stats.skipped_files += 1
            continue
        except Timeout:
            logger.error("Timeout while downloading %s", file_url)
            print(f"Ошибка: Превышено время ожидания при загрузке {file_name}")
            stats.errors += 1
        except RequestException as exc:
            logger.error("Error downloading %s: %s", file_url, exc)
            print(f"Ошибка при загрузке {file_name}: {exc}")
            stats.errors += 1
        except OSError as exc:
            logger.error("Filesystem error for %s: %s", file_path, exc)
            print(f"Ошибка записи файла {file_name}: {exc}")
            stats.errors += 1
        except Exception as exc:
            logger.error("Unexpected error downloading %s: %s", file_url, exc, exc_info=True)
            print(f"Неожиданная ошибка при обработке файла {file_name}: {exc}")
            stats.errors += 1
        finally:
            if not download_success and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass


def scan_boards_for_matches(
    boards: Sequence[str],
    allowed_extensions: Set[str],
    keywords: Sequence[str],
    min_matched_files: int,
    min_file_size: Optional[int],
    max_file_size: Optional[int],
    session: requests.Session,
    stats: SessionStats,
) -> List[ThreadRecord]:
    thread_targets: List[Tuple[str, int]] = []

    for board in boards:
        try:
            pages = fetch_catalog(board, session)
        except (Timeout, RequestException, json.JSONDecodeError) as exc:
            logger.error("Skipping board /%s/ due to catalog error: %s", board, exc)
            print(f"Ошибка при загрузке каталога /{board}/: {exc}")
            stats.errors += 1
            continue

        for page in pages:
            for thread in page.get("threads", []):
                thread_id = thread.get("no")
                if thread_id:
                    thread_targets.append((board, int(thread_id)))

    if not thread_targets:
        return []

    logger.info("Fetching %s thread JSON files with up to %s workers", len(thread_targets), MAX_WORKERS)
    records: List[ThreadRecord] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(get_thread_data, board, thread_id, session): (board, thread_id)
            for board, thread_id in thread_targets
        }

        for future in tqdm(as_completed(futures), total=len(futures), desc="Scanning threads"):
            board, thread_id = futures[future]
            stats.processed_threads += 1
            try:
                thread_data = future.result()
                matched_files, keyword_match = collect_matching_files(
                    board=board,
                    thread_data=thread_data,
                    allowed_extensions=allowed_extensions,
                    keywords=keywords,
                    min_file_size=min_file_size,
                    max_file_size=max_file_size,
                )
                if len(matched_files) < min_matched_files:
                    continue

                record = build_thread_record(board, thread_data, matched_files, keyword_match)
                records.append(record)
                stats.matched_threads += 1
                logger.info(
                    "Matched thread /%s/%s with %s files",
                    board,
                    thread_id,
                    record.matched_count,
                )
            except Exception as exc:
                logger.error(
                    "Error processing thread %s on /%s/: %s",
                    thread_id,
                    board,
                    exc,
                    exc_info=True,
                )
                stats.errors += 1

    records.sort(key=lambda item: (item.board, -item.matched_count, item.thread_id))
    return records


def main() -> None:
    allowed_extensions = normalize_extensions(ALLOWED_EXTENSIONS)
    keywords = normalize_keywords(KEYWORDS)
    stats = SessionStats()

    try:
        with build_session() as session:
            print("Сканирование Adult boards...")
            logger.info("Starting scan for boards: %s", ", ".join(ALL_ADULT_BOARDS))

            records = scan_boards_for_matches(
                boards=ALL_ADULT_BOARDS,
                allowed_extensions=allowed_extensions,
                keywords=keywords,
                min_matched_files=MIN_MATCHED_FILES,
                min_file_size=MIN_FILE_SIZE,
                max_file_size=MAX_FILE_SIZE,
                session=session,
                stats=stats,
            )

            summary = build_summary(
                records=records,
                boards=ALL_ADULT_BOARDS,
                processed_threads=stats.processed_threads,
            )
            print_summary(summary)

            if not records:
                logger.warning("No matching threads found")
                print("Подходящие треды не найдены")
                print_download_summary(stats)
                return

            while True:
                selected_indexes = interactive_select_with_pagination(records, page_size=PAGE_SIZE)

                if not selected_indexes:
                    logger.info("User exited selection loop")
                    print("Выход.")
                    print_download_summary(stats)
                    return

                selected_records = [records[index] for index in selected_indexes]
                logger.info("User selected threads: %s", selected_indexes)
                print(f"\nЗагрузка файлов из {len(selected_records)} тредов...")

                for record in selected_records:
                    print(
                        f"\nОбработка треда /{record.board}/ {record.thread_id} "
                        f"({record.matched_count} файлов)"
                    )
                    download_matching_files(record, session, OUTPUT_DIR, stats)

                logger.info(
                    "Download session completed. Processed: %s, Matched: %s, Downloaded files: %s, "
                    "Skipped: %s, Errors: %s, Total bytes: %s",
                    stats.processed_threads,
                    stats.matched_threads,
                    stats.downloaded_files,
                    stats.skipped_files,
                    stats.errors,
                    stats.downloaded_bytes,
                )
                print_download_summary(stats)

    except KeyboardInterrupt:
        print("\n\nПрервано пользователем. Выход.")
        logger.info("Program interrupted by user")
        sys.exit(0)
    except Exception as exc:
        logger.critical("Unexpected error in main: %s", exc, exc_info=True)
        print(f"Критическая ошибка: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
