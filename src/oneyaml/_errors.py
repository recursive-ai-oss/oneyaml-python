"""Exceptions raised by oneyaml."""

from __future__ import annotations


class YAMLError(ValueError):
    """The input is not acceptable YAML, or a limit was reached.

    ``category`` is a stable name (``syntax``, ``encoding``, ``limit``, ...); ``line`` and
    ``column`` are 1-based positions counted in decoded characters (``None`` when the error has
    no position); ``message`` is free text. ``events`` holds the event lines produced before the
    error, when the failing operation produces events.
    """

    def __init__(
        self,
        message: str,
        category: str = "syntax",
        line: int | None = None,
        column: int | None = None,
        events: list[str] | None = None,
    ) -> None:
        where = f" at line {line}, column {column}" if line is not None else ""
        super().__init__(f"{message}{where}")
        self.message = message
        self.category = category
        self.line = line
        self.column = column
        self.events = events
