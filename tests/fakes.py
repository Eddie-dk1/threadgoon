from __future__ import annotations

from collections import deque
from typing import Any

import requests


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size: int):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = deque(responses)
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, **kwargs):
        self.requests.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"Unexpected request: {url}")
        return self.responses.popleft()

    def close(self) -> None:
        self.closed = True
