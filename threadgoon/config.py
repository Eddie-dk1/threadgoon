"""Project-local TOML configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import KeywordScope
from .paths import AppPaths


DEFAULT_BOARDS = (
    "aco", "b", "d", "gif", "h", "hc", "hm", "hr", "r", "s", "t", "u", "e"
)
DEFAULT_EXTENSIONS = (".webm", ".mp4", ".gif", ".jpg", ".jpeg", ".png")


class ConfigurationError(ValueError):
    """Invalid user configuration."""


@dataclass(slots=True)
class AppConfig:
    boards: list[str] = field(default_factory=lambda: list(DEFAULT_BOARDS))
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    keywords: list[str] = field(default_factory=list)
    keyword_scope: KeywordScope = KeywordScope.FILE_POST
    min_file_size: int | None = None
    max_file_size: int | None = None
    min_matched_files: int = 1
    output_dir: str = "output"
    api_interval: float = 1.0
    watch_interval: int = 60
    media_workers: int = 3
    auto_download_new: bool = False

    def validate(self, paths: AppPaths) -> None:
        self.boards = list(dict.fromkeys(_normalize_board(item) for item in self.boards))
        self.extensions = list(
            dict.fromkeys(_normalize_extension(item) for item in self.extensions)
        )
        self.keywords = [item.strip() for item in self.keywords if item.strip()]
        if self.api_interval < 1.0:
            raise ConfigurationError("api_interval не может быть меньше 1 секунды")
        if self.watch_interval < 10:
            raise ConfigurationError("watch_interval не может быть меньше 10 секунд")
        if not 1 <= self.media_workers <= 3:
            raise ConfigurationError("media_workers должен быть в диапазоне 1-3")
        if self.min_matched_files < 1:
            raise ConfigurationError("min_matched_files должен быть не меньше 1")
        if self.min_file_size is not None and self.min_file_size < 0:
            raise ConfigurationError("min_file_size не может быть отрицательным")
        if self.max_file_size is not None and self.max_file_size < 0:
            raise ConfigurationError("max_file_size не может быть отрицательным")
        if (
            self.min_file_size is not None
            and self.max_file_size is not None
            and self.min_file_size > self.max_file_size
        ):
            raise ConfigurationError("min_file_size не может превышать max_file_size")
        paths.local_path(self.output_dir)

    @property
    def normalized_keywords(self) -> tuple[str, ...]:
        return tuple(keyword.casefold() for keyword in self.keywords)


def _normalize_board(value: str) -> str:
    board = value.strip().strip("/").casefold()
    if not board or not board.replace("_", "").isalnum():
        raise ConfigurationError(f"Некорректная доска: {value!r}")
    return board


def _normalize_extension(value: str) -> str:
    extension = value.strip().casefold()
    if not extension.startswith("."):
        extension = f".{extension}"
    if len(extension) < 2 or not extension[1:].isalnum():
        raise ConfigurationError(f"Некорректное расширение: {value!r}")
    return extension


def load_config(paths: AppPaths, config_path: Path | None = None) -> AppConfig:
    path = config_path or paths.config
    if not path.exists():
        config = AppConfig()
        config.validate(paths)
        save_config(config, paths, path)
        return config

    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Не удалось прочитать конфигурацию {path}: {exc}") from exc

    scan = raw.get("scan", {})
    download = raw.get("download", {})
    network = raw.get("network", {})
    watch = raw.get("watch", {})
    try:
        config = AppConfig(
            boards=list(scan.get("boards", DEFAULT_BOARDS)),
            extensions=list(scan.get("extensions", DEFAULT_EXTENSIONS)),
            keywords=list(scan.get("keywords", [])),
            keyword_scope=KeywordScope(
                scan.get("keyword_scope", KeywordScope.FILE_POST.value)
            ),
            min_file_size=scan.get("min_file_size"),
            max_file_size=scan.get("max_file_size"),
            min_matched_files=int(scan.get("min_matched_files", 1)),
            output_dir=str(download.get("output_dir", "output")),
            media_workers=int(download.get("media_workers", 3)),
            api_interval=float(network.get("api_interval", 1.0)),
            watch_interval=int(watch.get("interval", 60)),
            auto_download_new=bool(watch.get("auto_download_new", False)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Некорректное значение в {path}: {exc}") from exc
    config.validate(paths)
    return config


def save_config(config: AppConfig, paths: AppPaths, config_path: Path | None = None) -> None:
    config.validate(paths)
    path = paths.local_path(config_path or paths.config)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Threadgoon 6.0 — все пути должны оставаться внутри проекта.",
        "[scan]",
        f"boards = {_toml_list(config.boards)}",
        f"extensions = {_toml_list(config.extensions)}",
        f"keywords = {_toml_list(config.keywords)}",
        f'keyword_scope = "{config.keyword_scope.value}"',
        f"min_matched_files = {config.min_matched_files}",
    ]
    if config.min_file_size is not None:
        lines.append(f"min_file_size = {config.min_file_size}")
    if config.max_file_size is not None:
        lines.append(f"max_file_size = {config.max_file_size}")
    lines.extend(
        [
            "",
            "[download]",
            f'output_dir = "{_escape(config.output_dir)}"',
            f"media_workers = {config.media_workers}",
            "",
            "[network]",
            f"api_interval = {config.api_interval:.1f}",
            "",
            "[watch]",
            f"interval = {config.watch_interval}",
            f"auto_download_new = {str(config.auto_download_new).lower()}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_escape(value)}"' for value in values) + "]"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
