"""Error names of ruamel.yaml, on oneyaml's own exception classes.

``YAMLError`` is :class:`oneyaml.YAMLError` itself, so ``except YAMLError`` catches what the
library raises whichever name it was imported under. A read that fails raises the class ruamel
raises for that kind of problem (``ScannerError``/``ParserError`` for bad syntax,
``DuplicateKeyError``, ``ConstructorError``, ``ReaderError``), each a subclass of both
``MarkedYAMLError`` and the library's own category classes, with ``problem`` and
``problem_mark`` (a mark counting lines and columns from 0, as ruamel's do).
"""

from __future__ import annotations

from typing import Any

from ... import LimitError, ParseError, YAMLError

__all__ = [
    "ComposerError",
    "ConstructorError",
    "DuplicateKeyError",
    "MarkedYAMLError",
    "ParserError",
    "ReaderError",
    "RepresenterError",
    "ScannerError",
    "StringMark",
    "YAMLError",
    "YAMLStreamError",
    "YAMLWarning",
    "convert",
]


class StringMark:
    """A place in the input; ``line`` and ``column`` count from 0."""

    def __init__(self, name: str | None, index: int | None, line: int, column: int) -> None:
        self.name = name
        self.index = index
        self.line = line
        self.column = column

    def __str__(self) -> str:
        name = self.name or "<unicode string>"
        return f'  in "{name}", line {self.line + 1}, column {self.column + 1}'


class YAMLStreamError(Exception):
    pass


class YAMLWarning(Warning):
    pass


class MarkedYAMLError(ParseError):
    """A problem at a place in the input."""

    def __init__(self, message: str = "", **kw: Any) -> None:
        super().__init__(message, **kw)
        self.context: str | None = None
        self.context_mark: StringMark | None = None
        self.problem = message
        self.note: str | None = None
        line, column = kw.get("line"), kw.get("column")
        self.problem_mark = (
            None
            if line is None
            else StringMark(None, kw.get("offset"), line - 1, (column or 1) - 1)
        )


class RepresenterError(YAMLError):
    """A value that cannot be written as YAML (its type is not one the library writes)."""


class ReaderError(MarkedYAMLError):
    pass


class ScannerError(MarkedYAMLError):
    pass


class ParserError(MarkedYAMLError):
    pass


class _SyntaxError(ScannerError, ParserError):
    """Bad syntax: ruamel says scanner or parser by where it noticed; both names catch it here."""


class ComposerError(MarkedYAMLError):
    pass


class ConstructorError(MarkedYAMLError):
    pass


class DuplicateKeyError(ConstructorError):
    pass


def convert(exc: YAMLError) -> YAMLError:
    """The ruamel-named class for a library error (limit errors and the like stay as they are)."""
    if isinstance(exc, (MarkedYAMLError, LimitError)) or type(exc) is not ParseError:
        return exc
    cat = exc.category
    if cat == "duplicate-key":
        cls: type[MarkedYAMLError] = DuplicateKeyError
    elif cat == "encoding":
        cls = ReaderError
    elif cat == "syntax" and getattr(exc, "construct", False):
        cls = ConstructorError
    elif cat == "syntax":
        cls = _SyntaxError
    else:
        cls = ConstructorError
    new = cls(
        exc.message,
        category=cat,
        line=exc.line,
        column=exc.column,
        offset=exc.offset,
        events=exc.events,
    )
    new.__cause__ = exc.__cause__
    return new
