"""Persistent, resumable media download queue."""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

import requests
from requests.exceptions import RequestException

from .api import MEDIA_BASE_URL
from .database import Repository
from .models import Attachment, DownloadJob, DownloadStatus, ThreadRecord
from .paths import AppPaths
from .scanner import sanitize_filename

DownloadCallback = Callable[[DownloadJob], None]


class DownloadError(RuntimeError):
    """A media file could not be safely completed."""


class DownloadInterrupted(DownloadError):
    """A queue or individual job was cancelled."""


class DownloadManager:
    """Processes persistent jobs while keeping all files inside the project."""

    CHUNK_SIZE = 128 * 1024

    def __init__(
        self,
        repository: Repository,
        paths: AppPaths,
        *,
        output_dir: str = "output",
        max_workers: int = 3,
        session_factory: Callable[[], requests.Session] = requests.Session,
        timeout: tuple[float, float] = (10.0, 60.0),
    ):
        if not 1 <= max_workers <= 3:
            raise ValueError("Media worker count must be in range 1-3")
        self.repository = repository
        self.paths = paths
        self.output_dir = paths.local_path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self.session_factory = session_factory
        self.timeout = timeout
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()
        self._job_cancel: dict[int, threading.Event] = {}
        self._control_lock = threading.Lock()
        self._processing_lock = threading.Lock()
        self.logger = logging.getLogger("threadgoon.download")

    def pause(self) -> None:
        self.pause_event.clear()
        for job in self.repository.list_downloads([DownloadStatus.PENDING]):
            self.repository.update_download(job.id, status=DownloadStatus.PAUSED)

    def resume(self, *, include_paused: bool = True) -> None:
        self.pause_event.set()
        if not include_paused:
            return
        jobs = self.repository.list_downloads([DownloadStatus.PAUSED])
        for job in jobs:
            self.repository.update_download(
                job.id,
                status=DownloadStatus.PENDING,
                error=None,
            )

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()

    def reset_control(self) -> None:
        self.stop_event.clear()
        self.pause_event.set()

    def cancel_job(self, job_id: int) -> None:
        with self._control_lock:
            event = self._job_cancel.setdefault(job_id, threading.Event())
            event.set()
        job = self.repository.get_download(job_id)
        if job and job.status in {
            DownloadStatus.WAITING,
            DownloadStatus.PENDING,
            DownloadStatus.PAUSED,
            DownloadStatus.FAILED,
        }:
            self.repository.update_download(
                job_id,
                status=DownloadStatus.CANCELLED,
                error="Отменено пользователем",
            )

    def retry(self, job_id: int) -> None:
        with self._control_lock:
            self._job_cancel.pop(job_id, None)
        self.repository.update_download(
            job_id,
            status=DownloadStatus.PENDING,
            error=None,
        )

    def approve_waiting(self, job_ids: Iterable[int] | None = None) -> None:
        selected = set(job_ids or ())
        for job in self.repository.list_downloads([DownloadStatus.WAITING]):
            if not selected or job.id in selected:
                self.repository.update_download(
                    job.id,
                    status=DownloadStatus.PENDING,
                    error=None,
                )

    def destination_for(
        self,
        record: ThreadRecord,
        attachment: Attachment,
    ) -> Path:
        directory_title = sanitize_filename(record.semantic_url or record.title)
        directory = self.output_dir / record.board / (
            f"{record.thread_id}_{directory_title}"
        )
        filename = (
            f"{attachment.tim}_"
            f"{sanitize_filename(attachment.display_name or attachment.filename)}"
            f"{attachment.ext}"
        )
        path = self.paths.local_path(directory / filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def enqueue_attachments(
        self,
        record: ThreadRecord,
        attachments: Iterable[Attachment] | None = None,
        *,
        status: DownloadStatus = DownloadStatus.PENDING,
    ) -> list[DownloadJob]:
        jobs: list[DownloadJob] = []
        source = list(attachments if attachments is not None else record.attachments)
        for attachment in source:
            destination = self.destination_for(record, attachment)
            relative_destination = str(destination.relative_to(self.paths.root))
            url = f"{MEDIA_BASE_URL}/{attachment.board}/{attachment.tim}{attachment.ext}"
            jobs.append(
                self.repository.enqueue_download(
                    attachment,
                    url=url,
                    destination=relative_destination,
                    status=status,
                )
            )
        return jobs

    def process_pending(
        self,
        *,
        on_update: DownloadCallback | None = None,
    ) -> list[DownloadJob]:
        if not self._processing_lock.acquire(blocking=False):
            return []
        try:
            return self._process_pending(on_update=on_update)
        finally:
            self._processing_lock.release()

    def _process_pending(self, *, on_update: DownloadCallback | None = None) -> list[DownloadJob]:
        jobs = self.repository.list_downloads([DownloadStatus.PENDING])
        if not jobs:
            return []
        completed: list[DownloadJob] = []
        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="threadgoon-media",
        ) as executor:
            futures = {
                executor.submit(self._download_job, job.id, on_update): job.id
                for job in jobs
            }
            for future in as_completed(futures):
                try:
                    job = future.result()
                except Exception as exc:
                    self.repository.update_download(futures[future], status=DownloadStatus.FAILED, error=str(exc))
                    job = self._notify(futures[future], on_update)
                    if job is None:
                        continue
                completed.append(job)
        return completed

    def _download_job(
        self,
        job_id: int,
        on_update: DownloadCallback | None,
    ) -> DownloadJob:
        job = self.repository.get_download(job_id)
        if job is None:
            raise DownloadError(f"Задание {job_id} не найдено")
        if job.status is not DownloadStatus.PENDING:
            return self._notify(job.id, on_update)
        cancel_event = self._cancel_event(job_id)
        part_path: Path | None = None
        session = None
        try:
            self._wait_for_control(cancel_event)
            destination = self.paths.local_path(job.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            part_path = self.paths.local_path(destination.with_name(destination.name + ".part"))
            expected = job.expected_size
            if destination.exists() and expected is not None and destination.stat().st_size == expected:
                self.repository.update_download(job.id, status=DownloadStatus.COMPLETED,
                                                downloaded_size=expected, error=None)
                self.repository.mark_attachment_downloaded(job.board, job.tim, job.ext,
                                                          str(destination.relative_to(self.paths.root)))
                return self._notify(job.id, on_update)
            existing_part = part_path.stat().st_size if part_path.exists() else 0
            self._ensure_space(destination.parent, expected, existing_part)
            self.repository.update_download(job.id, status=DownloadStatus.DOWNLOADING,
                                            downloaded_size=existing_part, error=None)
            self._notify(job.id, on_update)
            session = self.session_factory()
            session.headers.update({"User-Agent": "threadgoon/6.1", "Accept": "*/*",
                                    "Accept-Encoding": "identity"})
            total = self._stream(
                session,
                job,
                part_path,
                existing_part,
                cancel_event,
                on_update,
            )
            expected = total if total is not None else job.expected_size
            actual = part_path.stat().st_size
            if expected is not None and actual != expected:
                raise DownloadError(
                    f"Размер файла не совпадает: получено {actual}, ожидалось {expected}"
                )
            if actual <= 0:
                raise DownloadError("Сервер вернул пустой файл")
            self._wait_for_control(cancel_event)
            os.replace(part_path, destination)
            relative = str(destination.relative_to(self.paths.root))
            self.repository.mark_attachment_downloaded(
                job.board, job.tim, job.ext, relative
            )
            self.repository.update_download(
                job.id,
                status=DownloadStatus.COMPLETED,
                downloaded_size=actual,
                expected_size=expected or actual,
                error=None,
            )
            self.logger.info("Downloaded %s", relative)
        except DownloadInterrupted as exc:
            if cancel_event.is_set():
                status = DownloadStatus.CANCELLED
            elif self.stop_event.is_set():
                status = DownloadStatus.PENDING
            else:
                status = DownloadStatus.PAUSED
            current_size = _part_size(part_path)
            self.repository.update_download(
                job.id,
                status=status,
                downloaded_size=current_size,
                error=None if status is DownloadStatus.PENDING else str(exc),
            )
        except Exception as exc:
            current_size = _part_size(part_path)
            self.repository.update_download(
                job.id,
                status=DownloadStatus.FAILED,
                downloaded_size=current_size,
                error=str(exc),
            )
            self.logger.warning("Download failed for %s: %s", job.url, exc)
        finally:
            if session is not None:
                session.close()
        return self._notify(job.id, on_update)

    def _stream(
        self,
        session: requests.Session,
        job: DownloadJob,
        part_path: Path,
        offset: int,
        cancel_event: threading.Event,
        on_update: DownloadCallback | None,
    ) -> int | None:
        headers: dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        response = session.get(
            job.url,
            headers=headers,
            stream=True,
            timeout=self.timeout,
        )

        if response.status_code == 416 and offset:
            response.close()
            if job.expected_size is not None and offset == job.expected_size:
                return job.expected_size
            offset = 0
            response = session.get(
                job.url,
                stream=True,
                timeout=self.timeout,
            )

        try:
            response.raise_for_status()
            if offset and response.status_code == 206:
                match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if not match or int(match.group(1)) != offset:
                    raise DownloadError("Сервер вернул неверный диапазон для докачивания")
        except BaseException:
            response.close()
            raise
        append = bool(offset and response.status_code == 206)
        if not append:
            offset = 0
        try:
            expected = _expected_total(response, offset)
            if job.expected_size is not None:
                expected = job.expected_size
            self._ensure_space(part_path.parent, expected, offset)
            self.repository.update_download(job.id, expected_size=expected)
            mode = "ab" if append else "wb"
            downloaded = offset
            last_report = downloaded
            with part_path.open(mode) as stream:
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    self._wait_for_control(cancel_event)
                    if not chunk:
                        continue
                    stream.write(chunk)
                    downloaded += len(chunk)
                    if downloaded - last_report >= 1024 * 1024:
                        self.repository.update_download(
                            job.id,
                            downloaded_size=downloaded,
                        )
                        self._notify(job.id, on_update)
                        last_report = downloaded
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            response.close()
        self.repository.update_download(job.id, downloaded_size=downloaded)
        return expected

    def _wait_for_control(self, cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise DownloadInterrupted("Отменено пользователем")
        if self.stop_event.is_set():
            raise DownloadInterrupted("Приложение остановлено")
        if not self.pause_event.is_set():
            raise DownloadInterrupted("Загрузка приостановлена")

    def _cancel_event(self, job_id: int) -> threading.Event:
        with self._control_lock:
            return self._job_cancel.setdefault(job_id, threading.Event())

    @staticmethod
    def _ensure_space(
        directory: Path,
        expected_size: int | None,
        existing_size: int,
    ) -> None:
        if expected_size is None:
            return
        remaining = max(0, expected_size - existing_size)
        free = shutil.disk_usage(directory).free
        if free < remaining:
            raise DownloadError(
                f"Недостаточно места: требуется {remaining} B, доступно {free} B"
            )

    def _notify(
        self,
        job_id: int,
        callback: DownloadCallback | None,
    ) -> DownloadJob:
        job = self.repository.get_download(job_id)
        if job is None:
            raise DownloadError(f"Задание {job_id} исчезло")
        if callback:
            callback(job)
        return job


_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


def _expected_total(response: requests.Response, offset: int) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    match = _CONTENT_RANGE.match(content_range)
    if match and match.group(3) != "*":
        return int(match.group(3))
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return offset + int(content_length)
    return None


def _part_size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path is not None and path.exists() else 0
    except OSError:
        return 0
