"""Command-line entry point for the Textual application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .api import ApiClient
from .config import ConfigurationError, load_config
from .database import Repository
from .downloader import DownloadManager
from .logging_config import configure_logging
from .paths import AppPaths, PathOutsideProjectError
from .scanner import ScanFilters, ScanService, parse_thread_url
from .watcher import WatchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="threadgoon",
        description="Локальный Textual TUI для каталогов и загрузок.",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="Ссылки на треды 4chan/4channel для первоначального импорта",
    )
    parser.add_argument(
        "--config",
        help="Путь к TOML-конфигурации внутри проекта",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Threadgoon {__version__}",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Подробный технический журнал",
    )
    return parser


def create_app(
    *,
    config_override: str | None = None,
    initial_urls: Sequence[str] = (),
    verbose: bool = False,
    root: Path | None = None,
    start_background: bool = True,
):
    from .app import ThreadgoonApp

    paths = AppPaths.build(root)
    paths.ensure()
    config_path = paths.config_path(config_override)
    config = load_config(paths, config_path)
    logger = configure_logging(paths, verbose=verbose)
    logger.info("Starting Threadgoon %s", __version__)

    for url in initial_urls:
        parse_thread_url(url)

    repository = Repository(paths.database)
    api = ApiClient(repository, interval=config.api_interval)
    scanner = ScanService(api, repository)
    downloader = DownloadManager(
        repository,
        paths,
        output_dir=config.output_dir,
        max_workers=config.media_workers,
    )
    watcher = WatchService(
        api,
        repository,
        downloader,
        ScanFilters.from_config(config),
    )
    return ThreadgoonApp(
        paths=paths,
        config=config,
        config_path=config_path,
        repository=repository,
        api=api,
        scanner=scanner,
        downloader=downloader,
        watcher=watcher,
        initial_urls=initial_urls,
        start_background=start_background,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        app = create_app(
            config_override=args.config,
            initial_urls=args.urls,
            verbose=args.verbose,
        )
    except (ConfigurationError, PathOutsideProjectError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
        return 2
    try:
        app.run()
    except KeyboardInterrupt:
        return 130
    finally:
        app.repository.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
