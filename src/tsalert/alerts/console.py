from __future__ import annotations

import sys
from typing import TextIO


class ConsoleChannel:
    """Prints alerts to a stream. Always configured, so a demo works with no
    credentials at all."""

    name = "console"

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self.stream = stream

    def is_configured(self) -> bool:
        return True

    def send(self, text: str) -> None:
        print(text, file=self.stream)
