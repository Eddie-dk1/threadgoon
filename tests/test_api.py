from __future__ import annotations

from pathlib import Path

import pytest

from threadgoon.api import ApiClient, ApiNotFound, RateLimiter
from threadgoon.database import Repository

from .fakes import FakeResponse, FakeSession


def test_rate_limiter_keeps_one_second_gap() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(value: float) -> None:
        sleeps.append(value)
        now[0] += value

    limiter = RateLimiter(1.0, clock=clock, sleeper=sleep)
    limiter.acquire()
    limiter.acquire()

    assert sum(sleeps) == pytest.approx(1.0)
    assert now[0] == pytest.approx(1.0)


def test_conditional_304_uses_cached_payload(project_dir: Path) -> None:
    repository = Repository(project_dir / "state.sqlite3")
    repository.save_cache(
        "catalog:gif",
        [{"page": 1, "threads": []}],
        last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        etag='"abc"',
    )
    session = FakeSession([FakeResponse(304)])
    client = ApiClient(
        repository,
        session=session,
        limiter=RateLimiter(1.0),
    )

    result = client.fetch_catalog("gif")

    assert result.not_modified is True
    assert result.data[0]["page"] == 1
    headers = session.requests[0][1]["headers"]
    assert headers["If-Modified-Since"].startswith("Mon")
    assert headers["If-None-Match"] == '"abc"'
    assert list((project_dir / "cache").glob("*.json"))
    repository.close()


def test_404_is_not_retried(project_dir: Path) -> None:
    repository = Repository(project_dir / "state.sqlite3")
    session = FakeSession([FakeResponse(404)])
    client = ApiClient(repository, session=session, max_attempts=4)

    with pytest.raises(ApiNotFound):
        client.fetch_catalog("r")

    assert len(session.requests) == 1
    repository.close()
