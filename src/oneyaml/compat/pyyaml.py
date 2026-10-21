"""A drop-in for PyYAML's loading functions: ``import oneyaml.compat.pyyaml as yaml``.

The names, arguments and results are PyYAML's: YAML 1.1 scalars (``yes``, ``0o``-less octal,
base-60 numbers, dates), merge keys, the last of a repeated key, ``!!set``/``!!omap``/``!!binary``,
exceptions with marks. What is not kept:

* Nothing is ever constructed by running code. ``Loader``, ``FullLoader`` and ``UnsafeLoader``
  read the same safe types as ``SafeLoader``; a ``!!python/...`` tag is an error.
* The limits of :class:`oneyaml.Limits` stay on, and merge keys count against the alias budget,
  so a document cannot expand without bound.
* ``safe_load_all`` reads the whole stream before it yields the first document.
"""

from __future__ import annotations

import re
from typing import Any

from .. import _api
from .._errors import YAMLError as _BaseError

__all__ = [
    "BaseLoader",
    "CBaseLoader",
    "CFullLoader",
    "CLoader",
    "CSafeLoader",
    "CUnsafeLoader",
    "ComposerError",
    "ConstructorError",
    "FullLoader",
    "Loader",
    "Mark",
    "MarkedYAMLError",
    "ParserError",
    "ReaderError",
    "SafeLoader",
    "ScannerError",
    "UnsafeLoader",
    "YAMLError",
    "__version__",
    "__with_libyaml__",
    "full_load",
    "full_load_all",
    "load",
    "load_all",
    "safe_load",
    "safe_load_all",
    "unsafe_load",
    "unsafe_load_all",
]

# The PyYAML release whose documented behaviour this follows.
__version__ = "6.0.3"
# The C-named loaders exist (they are the same loaders), so code that picks them can.
__with_libyaml__ = True


# ---- errors ------------------------------------------------------------------------------------


class Mark:
    """A position in the input; ``line`` and ``column`` count from 0, as in PyYAML."""

    def __init__(
        self,
        name: str,
        index: int,
        line: int,
        column: int,
        buffer: str | None = None,
        pointer: int | None = None,
    ) -> None:
        self.name = name
        self.index = index
        self.line = line
        self.column = column
        self.buffer = buffer
        self.pointer = pointer

    def get_snippet(self, indent: int = 4, max_length: int = 75) -> None:
        return None

    def __str__(self) -> str:
        return f'  in "{self.name}", line {self.line + 1}, column {self.column + 1}'


class YAMLError(_BaseError):
    """Base class of every error this module raises (it is also ``oneyaml.YAMLError``)."""


class MarkedYAMLError(YAMLError):
    def __init__(
        self,
        context: str | None = None,
        context_mark: Mark | None = None,
        problem: str | None = None,
        problem_mark: Mark | None = None,
        note: str | None = None,
    ) -> None:
        self.context = context
        self.context_mark = context_mark
        self.problem = problem
        self.problem_mark = problem_mark
        self.note = note
        super().__init__(problem or context or "")

    def __str__(self) -> str:
        lines = []
        if self.context is not None:
            lines.append(self.context)
        mark = self.context_mark
        if mark is not None and (
            self.problem is None
            or self.problem_mark is None
            or mark.name != self.problem_mark.name
            or mark.line != self.problem_mark.line
            or mark.column != self.problem_mark.column
        ):
            lines.append(str(mark))
        if self.problem is not None:
            lines.append(self.problem)
        if self.problem_mark is not None:
            lines.append(str(self.problem_mark))
        if self.note is not None and self.note:
            lines.append(self.note)
        return "\n".join(lines)

    def __reduce__(self):
        return (
            type(self),
            (self.context, self.context_mark, self.problem, self.problem_mark, self.note),
        )


class ReaderError(YAMLError):
    def __init__(
        self, name: str, position: int, character: Any, encoding: str, reason: str
    ) -> None:
        self.name = name
        self.character = character
        self.position = position
        self.encoding = encoding
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f'{self.reason}\n  in "{self.name}", position {self.position}'

    def __reduce__(self):
        return (
            type(self),
            (self.name, self.position, self.character, self.encoding, self.reason),
        )


class ScannerError(MarkedYAMLError):
    pass


class ParserError(MarkedYAMLError):
    pass


class ComposerError(MarkedYAMLError):
    pass


class ConstructorError(MarkedYAMLError):
    pass


# Both backends word an unknown or self-containing alias their own way.
_ALIAS_PROBLEM = re.compile(r"(?:the )?alias ").match


def _convert(exc: _BaseError, name: str) -> YAMLError:
    """The PyYAML exception that stands for one of ours, with its position as a mark."""
    category = exc.category
    message = exc.message
    if category == "encoding":
        out: YAMLError = ReaderError(name, exc.offset or 0, "?", "utf-8", message)
    else:
        mark = None
        if exc.line is not None and exc.column is not None:
            mark = Mark(name, exc.offset or 0, exc.line - 1, exc.column - 1)
        if category == "other" and "more than one document" in message:
            out = ComposerError(
                "expected a single document in the stream", mark, "but found another document", mark
            )
        elif category in ("syntax", "unresolved-tag", "duplicate-key"):
            cls = ComposerError if _ALIAS_PROBLEM(message) else ParserError
            out = cls(None, None, message, mark)
        else:
            # construct, limit, and anything else met while building values
            out = ConstructorError(None, None, message, mark)
    out.category = category
    out.message = message
    out.line = exc.line
    out.column = exc.column
    out.offset = exc.offset
    return out


# ---- loaders -----------------------------------------------------------------------------------


def _read(stream: Any) -> tuple[str | bytes, str]:
    """The text or bytes of a stream and the name to show in marks."""
    if isinstance(stream, str):
        return stream, "<unicode string>"
    if isinstance(stream, (bytes, bytearray, memoryview)):
        return bytes(stream), "<byte string>"
    try:
        read = stream.read
    except AttributeError:
        raise TypeError(
            f"expected a str, bytes or a file object, not {type(stream).__name__}"
        ) from None
    data = read()
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    elif not isinstance(data, (str, bytes)):
        raise TypeError(f"the stream gave {type(data).__name__}, not str or bytes")
    return data, getattr(stream, "name", "<file>")


class BaseLoader:
    """Reads every scalar as a string and ignores tags, as PyYAML's ``BaseLoader`` does."""

    _schema = "text"

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self._docs: list[Any] | None = None
        self._next = 0

    def _documents(self) -> list[Any]:
        if self._docs is None:
            data, name = _read(self.stream)
            try:
                self._docs = _api._impl.load(data, _api._DEFAULT_LIMITS, True, self._schema)
            except _BaseError as exc:
                raise _convert(exc, name) from None
        return self._docs

    def check_data(self) -> bool:
        return self._next < len(self._documents())

    def get_data(self) -> Any:
        docs = self._documents()
        if self._next >= len(docs):
            return None
        self._next += 1
        return docs[self._next - 1]

    def get_single_data(self) -> Any:
        docs = self._documents()
        if not docs:
            return None
        if len(docs) > 1:
            # Same error a single-document read gives, with the place of the second document.
            data, name = _read(self.stream)
            try:
                _api._impl.load(data, _api._DEFAULT_LIMITS, False, self._schema)
            except _BaseError as exc:
                raise _convert(exc, name) from None
        self._next = 1
        return docs[0]

    def dispose(self) -> None:
        self._docs = None


class SafeLoader(BaseLoader):
    """YAML 1.1 scalars and the standard tags, as PyYAML's ``SafeLoader``."""

    _schema = "yaml11"


class FullLoader(SafeLoader):
    """Reads the same types as ``SafeLoader``; ``!!python/...`` tags are refused."""


class UnsafeLoader(SafeLoader):
    """Kept for its name. It builds nothing by running code: ``!!python/...`` tags are refused."""


class Loader(SafeLoader):
    """Kept for its name; the same as :class:`UnsafeLoader`."""


class CBaseLoader(BaseLoader):
    pass


class CSafeLoader(SafeLoader):
    pass


class CFullLoader(FullLoader):
    pass


class CUnsafeLoader(UnsafeLoader):
    pass


class CLoader(Loader):
    pass


# ---- functions ---------------------------------------------------------------------------------


def load(stream: Any, Loader: Any) -> Any:  # noqa: N803 (PyYAML's own parameter name)
    """Read the single document in ``stream`` with ``Loader``."""
    loader = Loader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_all(stream: Any, Loader: Any):  # noqa: N803
    """Read every document in ``stream``; the whole stream is read before the first is given."""
    loader = Loader(stream)
    try:
        while loader.check_data():
            yield loader.get_data()
    finally:
        loader.dispose()


def full_load(stream: Any) -> Any:
    return load(stream, FullLoader)


def full_load_all(stream: Any):
    return load_all(stream, FullLoader)


def safe_load(stream: Any) -> Any:
    return load(stream, SafeLoader)


def safe_load_all(stream: Any):
    return load_all(stream, SafeLoader)


def unsafe_load(stream: Any) -> Any:
    return load(stream, UnsafeLoader)


def unsafe_load_all(stream: Any):
    return load_all(stream, UnsafeLoader)
