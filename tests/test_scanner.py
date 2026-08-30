from __future__ import annotations

from pathlib import Path

from threadgoon.api import ApiNotFound, ApiResult
from threadgoon.database import Repository
from threadgoon.models import KeywordScope
from threadgoon.scanner import (
    ScanFilters,
    ScanService,
    collect_attachments,
    parse_catalog,
    parse_thread_url,
)


THREAD_POSTS = [
    {
        "no": 100,
        "sub": "target topic",
        "com": "opening",
        "tim": 1,
        "ext": ".jpg",
        "filename": "op",
        "fsize": 10,
    },
    {
        "no": 101,
        "com": "ordinary reply",
        "tim": 2,
        "ext": ".jpg",
        "filename": "reply",
        "fsize": 20,
    },
]


def test_keyword_scopes_are_explicit() -> None:
    strict = ScanFilters(
        extensions=frozenset({".jpg"}),
        keywords=("target",),
        keyword_scope=KeywordScope.FILE_POST,
    )
    whole = ScanFilters(
        extensions=frozenset({".jpg"}),
        keywords=("target",),
        keyword_scope=KeywordScope.WHOLE_THREAD,
    )

    strict_files, _ = collect_attachments("x", THREAD_POSTS, strict)
    whole_files, _ = collect_attachments("x", THREAD_POSTS, whole)

    assert [item.post_id for item in strict_files] == [100]
    assert [item.post_id for item in whole_files] == [100, 101]


def test_fast_scan_never_fetches_full_threads(project_dir: Path) -> None:
    class FakeApi:
        thread_calls = 0

        def fetch_boards(self, **_kwargs):
            return ApiResult(
                {"boards": [{"board": "gif", "title": "GIF", "ws_board": 0}]}
            )

        def fetch_catalog(self, board, **_kwargs):
            assert board == "gif"
            return ApiResult(
                [
                    {
                        "page": 1,
                        "threads": [
                            {
                                "no": 123,
                                "sub": "Example",
                                "images": 7,
                                "semantic_url": "example",
                            }
                        ],
                    }
                ]
            )

        def fetch_thread(self, *_args, **_kwargs):
            self.thread_calls += 1
            raise AssertionError("fast scan requested a full thread")

    repository = Repository(project_dir / "state.sqlite3")
    api = FakeApi()
    service = ScanService(api, repository)

    records = service.fast_scan(["gif"])

    assert len(records) == 1
    assert records[0].file_count == 7
    assert api.thread_calls == 0
    repository.close()


def test_catalog_and_url_parsing() -> None:
    records = parse_catalog(
        "gif",
        [{"page": 1, "threads": [{"no": 9, "sub": "Title", "images": 2}]}],
    )
    assert records[0].source_url.endswith("/gif/thread/9")
    assert parse_thread_url(
        "https://boards.4chan.org/gif/thread/12345"
    ) == ("gif", 12345)


def test_unavailable_board_does_not_abort_other_catalogs(project_dir: Path) -> None:
    class FakeApi:
        def fetch_boards(self, **_kwargs):
            return ApiResult(
                {
                    "boards": [
                        {"board": "r", "title": "Requests", "ws_board": 0},
                        {"board": "gif", "title": "GIF", "ws_board": 0},
                    ]
                }
            )

        def fetch_catalog(self, board, **_kwargs):
            if board == "r":
                raise ApiNotFound("missing")
            return ApiResult(
                [{"page": 1, "threads": [{"no": 99, "sub": "Works"}]}]
            )

    repository = Repository(project_dir / "state.sqlite3")
    service = ScanService(FakeApi(), repository)

    records = service.fast_scan(["r", "gif"])

    assert [item.thread_id for item in records] == [99]
    boards = {item.board: item for item in repository.list_boards()}
    assert boards["r"].available is False
    repository.close()
