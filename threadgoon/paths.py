"""Project-local filesystem layout and path validation."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path


class PathOutsideProjectError(ValueError):
    """Raised when configuration points outside the project root."""


def discover_project_root(start: Path | None = None) -> Path:
    """Find the Threadgoon source project."""

    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "pyproject.toml").is_file():
        return source_root

    candidate = (start or Path.cwd()).resolve()
    for directory in (candidate, *candidate.parents):
        marker = directory / "pyproject.toml"
        if marker.is_file() and (directory / "threadgoon").is_dir():
            return directory
    raise RuntimeError(
        "Не найден корень проекта Threadgoon. Запускайте приложение из исходного проекта."
    )


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    runtime: Path
    config: Path
    database: Path
    cache: Path
    logs: Path
    temp: Path
    output: Path

    @classmethod
    def build(cls, root: Path | None = None) -> "AppPaths":
        project_root = (root or discover_project_root()).resolve()
        runtime = project_root / ".threadgoon"
        paths = cls(
            root=project_root,
            runtime=runtime,
            config=runtime / "config.toml",
            database=runtime / "state.sqlite3",
            cache=runtime / "cache",
            logs=runtime / "logs",
            temp=runtime / "tmp",
            output=project_root / "output",
        )
        for location in (paths.runtime, paths.config, paths.database, paths.database.with_suffix(".lock"), paths.cache, paths.logs, paths.temp, paths.output):
            paths.local_path(location)
        return paths

    def ensure(self) -> None:
        for directory in (self.runtime, self.cache, self.logs, self.temp, self.output):
            directory.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(self.temp)

    def local_path(self, value: str | Path, *, allow_root: bool = False) -> Path:
        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PathOutsideProjectError(
                f"Путь должен находиться внутри проекта: {candidate}"
            ) from exc
        if not allow_root and candidate == self.root:
            raise PathOutsideProjectError("Корень проекта нельзя использовать как рабочую папку")
        return candidate

    def config_path(self, override: str | Path | None = None) -> Path:
        return self.config if override is None else self.local_path(override)
