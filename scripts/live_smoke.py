"""Opt-in live smoke test: boards, one catalog, and one thread only."""

from __future__ import annotations

import argparse

from threadgoon.api import ApiClient
from threadgoon.config import load_config
from threadgoon.database import Repository
from threadgoon.paths import AppPaths
from threadgoon.scanner import ScanFilters, parse_catalog, parse_thread


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", default="po")
    args = parser.parse_args()

    paths = AppPaths.build()
    paths.ensure()
    config = load_config(paths)
    repository = Repository(paths.database)
    api = ApiClient(repository, interval=max(1.0, config.api_interval))
    try:
        boards = api.fetch_boards().data.get("boards", [])
        catalog_result = api.fetch_catalog(args.board)
        records = parse_catalog(args.board, catalog_result.data)
        if not records:
            raise RuntimeError(f"/{args.board}/ returned an empty catalog")
        selected = records[0]
        thread_result = api.fetch_thread(args.board, selected.thread_id)
        exact = parse_thread(
            args.board,
            thread_result.data,
            ScanFilters.from_config(config),
        )
        repository.upsert_thread(exact)
        repository.upsert_attachments(exact.attachments)
        print(f"boards={len(boards)}")
        print(f"catalog_threads={len(records)}")
        print(f"thread=/{exact.board}/{exact.thread_id}")
        print(f"matching_attachments={exact.file_count}")
        print(f"cache={repository.cache_dir.relative_to(paths.root)}")
    finally:
        api.close()
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
