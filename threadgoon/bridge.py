"""Versioned JSON-lines stdio transport. stdout contains protocol messages only."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path
from typing import TextIO

from . import __version__
from .controller import Controller, PROTOCOL_VERSION


class Transport:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.lock = threading.Lock()

    def send(self, kind: str, **values) -> None:
        message = {"version": PROTOCOL_VERSION, "type": kind, **values}
        with self.lock:
            try:
                self.stream.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
                self.stream.flush()
            except (BrokenPipeError, OSError):
                # EOF on stdin triggers orderly runtime shutdown.
                pass

    def event(self, event: str, data: dict) -> None:
        self.send("event", event=event, data=data)


def serve(root: Path, incoming: TextIO, outgoing: TextIO, *, start_background: bool = True) -> int:
    transport = Transport(outgoing)
    try:
        controller = Controller(root, emit=transport.event, start_background=start_background)
    except Exception as exc:
        transport.send("fatal", error=str(exc))
        return 1
    transport.send("ready", app_version=__version__, data=controller.snapshot())
    try:
        for line in incoming:
            request_id = None
            try:
                if len(line) > 1024 * 1024:
                    raise ValueError("Запрос слишком большой")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Ожидается JSON-объект")
                request_id = message.get("id")
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("Запросу нужен строковый id")
                if message.get("version") != PROTOCOL_VERSION:
                    raise ValueError("Несовместимая версия протокола")
                params = message.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params должен быть объектом")
                command = message.get("command")
                if command == "shutdown":
                    controller.close()
                    transport.send("response", id=request_id, ok=True, data={})
                    break
                data = controller.dispatch(command, params)
                transport.send("response", id=request_id, ok=True, data=data)
            except Exception as exc:
                transport.send("response", id=request_id, ok=False, error=str(exc))
    finally:
        controller.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Threadgoon desktop engine")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--no-watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    return serve(args.workspace, sys.stdin, sys.stdout, start_background=not args.no_watch)


if __name__ == "__main__":
    raise SystemExit(main())
