"""Disposable native-UI fixture with a loopback-only resumable media server."""
from __future__ import annotations

import argparse
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from threadgoon.config import AppConfig, save_config
from threadgoon.controller import Controller
from threadgoon.models import Attachment, BoardInfo, DownloadStatus, WatchRecord, WatchStatus
from threadgoon.scanner import ScanFilters, parse_thread


class MediaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        total = 10 * 1024 * 1024
        offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
        self.send_response(206 if offset else 200)
        self.send_header("Content-Length", str(total - offset))
        if offset:
            self.send_header("Content-Range", f"bytes {offset}-{total-1}/{total}")
        self.end_headers()
        try:
            for start in range(offset, total, 65536):
                self.wfile.write(b"x" * min(65536, total - start))
                self.wfile.flush()
                time.sleep(0.06)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Read boards, one /po/ catalog and one thread")
    args = parser.parse_args()
    if args.workspace.exists():
        raise SystemExit("Choose a new disposable workspace")
    server = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
    controller = Controller(args.workspace, start_background=False)
    repo = controller.repository
    try:
        controller.config = AppConfig(boards=["po"])
        save_config(controller.config, controller.paths)
        if args.live:
            records = controller.scanner.fast_scan(["po"])
            record = controller.scanner.fetch_direct("po", records[0].thread_id, ScanFilters.from_config(controller.config))
        else:
            repo.upsert_boards([BoardInfo("po", "Papercraft & Origami", True)])
            record = parse_thread("po", {"posts": [{"no": 10, "sub": "Проверка интерфейса", "com": "Тестовые данные для проверки очереди"}]}, ScanFilters(frozenset({".jpg"})))
            repo.upsert_thread(record)
        for index, state in enumerate([DownloadStatus.PAUSED, DownloadStatus.FAILED, DownloadStatus.WAITING, DownloadStatus.COMPLETED], 1):
            attachment = Attachment("po", record.thread_id, record.thread_id, index, ".bin", f"qa-{index}", f"qa-{index}", 10 * 1024 * 1024)
            destination = f"output/qa-{index}.bin"
            job = repo.enqueue_download(attachment, url=f"http://127.0.0.1:{server.server_port}/media", destination=destination, status=state)
            if state is DownloadStatus.COMPLETED:
                controller.paths.local_path(destination).write_bytes(b"Disposable UI test file\n")
                repo.update_download(job.id, downloaded_size=24)
            if state is DownloadStatus.FAILED:
                repo.update_download(job.id, error="Тестовая ошибка: соединение прервано. Нажмите «Повторить».")
        repo.add_watch(WatchRecord("po", record.thread_id, 60, False, WatchStatus.PAUSED))
    finally:
        controller.close()
    print(f"Disposable workspace: {args.workspace.resolve()}\nLoopback media server: {server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
