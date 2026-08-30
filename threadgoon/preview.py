"""Preview extension point; version 6.0 deliberately renders metadata only."""

from __future__ import annotations

from typing import Protocol

from .models import Attachment
from .scanner import format_size


class PreviewProvider(Protocol):
    def describe(self, attachment: Attachment) -> str:
        """Return a textual representation for the TUI details panel."""


class MetadataPreviewProvider:
    def describe(self, attachment: Attachment) -> str:
        mime = attachment.mime_type or "неизвестный тип"
        location = attachment.local_path or "ещё не загружен"
        return (
            f"{attachment.display_name}{attachment.ext}\n"
            f"Тип: {mime}\n"
            f"Размер: {format_size(attachment.size)}\n"
            f"Файл: {location}"
        )
