from __future__ import annotations

from pathlib import Path

import pytest

from threadgoon.config import AppConfig, ConfigurationError, load_config, save_config
from threadgoon.paths import AppPaths, PathOutsideProjectError


def test_runtime_and_default_config_stay_inside_project(project_dir: Path) -> None:
    paths = AppPaths.build(project_dir)
    paths.ensure()
    config = load_config(paths)

    assert config.output_dir == "output"
    assert paths.config.is_file()
    assert paths.config.is_relative_to(project_dir)
    assert paths.database.is_relative_to(project_dir)
    assert paths.output.is_relative_to(project_dir)


def test_external_paths_are_rejected(project_dir: Path) -> None:
    paths = AppPaths.build(project_dir)
    paths.ensure()
    config = AppConfig(output_dir="/tmp/threadgoon-external")

    with pytest.raises(PathOutsideProjectError):
        config.validate(paths)


def test_invalid_intervals_are_rejected(project_dir: Path) -> None:
    paths = AppPaths.build(project_dir)
    paths.ensure()

    with pytest.raises(ConfigurationError):
        AppConfig(api_interval=0.9).validate(paths)
    with pytest.raises(ConfigurationError):
        AppConfig(watch_interval=9).validate(paths)


def test_config_round_trip(project_dir: Path) -> None:
    paths = AppPaths.build(project_dir)
    paths.ensure()
    config = AppConfig(
        boards=["gif", "h"],
        keywords=["alpha", "beta"],
        output_dir="output/custom",
        api_interval=1.5,
        watch_interval=90,
        media_workers=2,
    )
    save_config(config, paths)

    loaded = load_config(paths)
    assert loaded.boards == ["gif", "h"]
    assert loaded.keywords == ["alpha", "beta"]
    assert loaded.output_dir == "output/custom"
    assert loaded.api_interval == 1.5
    assert loaded.watch_interval == 90
    assert loaded.media_workers == 2
