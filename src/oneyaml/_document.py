"""Lossless documents: read a file, change one value, write it back, and only that value moves.

``parse`` returns a :class:`Document` over the active backend's handle. Collections read from it
are live views (:class:`MappingView`, :class:`SequenceView`); scalars read as plain Python
values. Views hold no data: each is a path from the document root, resolved on every use, so
they never show anything but the current text. An edit that removes or replaces a node, or
moves the items of a sequence, makes the views it strands raise :class:`StaleViewError` instead
of quietly addressing whatever is there now.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, MutableMapping, MutableSequence, Sequence
from typing import IO, Any, NamedTuple

from . import _api
from ._errors import YAMLError
from ._limits import Limits
from ._types import FrozenMapping

__all__ = [
    "Document",
    "MappingView",
    "Position",
    "SequenceView",
    "StaleViewError",
    "parse",
]


class StaleViewError(YAMLError):
    """A view was used after an edit removed or replaced the node it stood for."""

    def __init__(self, message: str = "the node this view stood for was removed or replaced"):
        super().__init__(message, category="other")


class Position(NamedTuple):
    """Where a node starts: 1-based line and column (columns count characters)."""

    line: int
    column: int


# Entries kept in a document's edit log before views older than the log are all treated as
# stale. An entry is a tuple; this is far more edits than any view outlives.
_LOG_MAX = 100_000


def _norm(token: Any) -> str:
    """A token as text, so that a view built from a pointer compares with one built from keys."""
    if isinstance(token, str):
        return token
    if token is None:
        return "null"
    if isinstance(token, bool):
        return "true" if token else "false"
    return str(token)


class _Core:
    """What every view of one stream shares: the backend handle and the log of edits."""

    __slots__ = ("base", "h", "log")

    def __init__(self, handle: Any) -> None:
        self.h = handle
        self.log: list[tuple[tuple[str, ...], int | None]] = []
        self.base = 0

    @property
    def tick(self) -> int:
        return self.base + len(self.log)

    def record(self, d: int, path: tuple, index: int | None = None) -> None:
        """Note an edit: the node at ``path`` is gone (``index`` None), or the items of the
        sequence at ``path`` from ``index`` on moved."""
        self.log.append(((str(d), *map(_norm, path)), index))
        if len(self.log) > _LOG_MAX:
            self.base += len(self.log)
            self.log.clear()

    def stale(self, d: int, path: tuple, tick: int) -> bool:
        if tick < self.base:
            return True
        mine = (str(d), *map(_norm, path))
        for where, index in self.log[tick - self.base :]:
            n = len(where)
            if mine[:n] != where:
                continue
            if index is None:
                return True
            if len(mine) > n and mine[n].isdigit() and int(mine[n]) >= index:
                return True
        return False


def _token(key: Any) -> Any:
    """The path token for a mapping key: text as it is, other scalars as themselves."""
    if isinstance(key, (str, int, float, tuple, FrozenMapping)) or key is None:
        return key
    raise KeyError(key)


class _Node:
    """What a document and its views have in common: a place in the stream."""

    __slots__ = ("_core", "_d", "_path", "_tick")
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, core: _Core, d: int, path: tuple) -> None:
        self._core = core
        self._d = d
        self._path = path
        self._tick = core.tick

    # -- plumbing

    def _h(self) -> Any:
        core = self._core
        if self._tick != core.tick and core.stale(self._d, self._path, self._tick):
            raise StaleViewError
        return core.h

    def _kind(self) -> str:
        raise NotImplementedError

    def _view(self, path: tuple) -> Any:
        """The value at ``path``: plain data for a scalar, a view for a collection."""
        h = self._core.h
        steps = list(path)
        kind = h.kind(self._d, steps)
        if kind == "scalar":
            return h.value(self._d, steps)
        cls = MappingView if kind == "mapping" else SequenceView
        return cls(self._core, self._d, path)

    def _length(self, h: Any) -> int:
        return h.length(self._d, list(self._path))

    def _index(self, h: Any, key: Any) -> int:
        """A sequence index made non-negative; out-of-range ones fail where the backend does."""
        if isinstance(key, bool) or not isinstance(key, int):
            raise TypeError(f"sequence indices must be integers, not {type(key).__name__}")
        if key < 0:
            key += self._length(h)
            if key < 0:
                raise IndexError("sequence index out of range")
        return key

    def _keyed(self, key: Any) -> tuple[str, tuple]:
        """``(kind, path)`` of the child ``key`` of this node."""
        kind = self._kind()
        h = self._h()
        if kind == "mapping":
            return kind, (*self._path, _token(key))
        if kind == "sequence":
            return kind, (*self._path, str(self._index(h, key)))
        raise TypeError("a scalar has no items")

    # -- mapping and sequence protocol, by what the node is

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            self._h()
            if self._kind() != "sequence":
                raise TypeError("only a sequence can be sliced")
            return [self[i] for i in range(*key.indices(len(self)))]
        _, path = self._keyed(key)
        return self._view(path)

    def __setitem__(self, key: Any, value: Any) -> None:
        if isinstance(key, slice):
            raise TypeError("slice assignment is not supported")
        _, path = self._keyed(key)
        h = self._h()
        if isinstance(value, _Node):
            value = value.to_python()
        before = h.version()
        h.set(self._d, list(path), value)
        if h.version() != before:
            self._core.record(self._d, path)

    def __delitem__(self, key: Any) -> None:
        if isinstance(key, slice):
            raise TypeError("slice deletion is not supported")
        kind, path = self._keyed(key)
        h = self._h()
        h.remove(self._d, list(path))
        if kind == "sequence":
            self._core.record(self._d, self._path, int(path[-1]))
        self._core.record(self._d, path)

    def __len__(self) -> int:
        return self._length(self._h())

    def __iter__(self) -> Iterator[Any]:
        h = self._h()
        kind = self._kind()
        steps = list(self._path)
        if kind == "mapping":
            return iter(h.keys(self._d, steps))
        if kind == "sequence":
            return (self._view((*self._path, str(i))) for i in range(self._length(h)))
        raise TypeError("a scalar is not iterable")

    def __contains__(self, key: Any) -> bool:
        kind = self._kind()
        h = self._h()
        if kind == "mapping":
            try:
                path = (*self._path, _token(key))
            except KeyError:
                return False
            try:
                h.kind(self._d, list(path))
            except (KeyError, IndexError):
                return False
            return True
        if kind == "sequence":
            return any(v is key or v == key for v in self)
        raise TypeError("a scalar has no items")

    def __eq__(self, other: Any) -> Any:
        if isinstance(other, _Node):
            other = other.to_python()
        return self.to_python() == other

    def __repr__(self) -> str:
        try:
            shown = repr(self.to_python())
        except StaleViewError:
            shown = "stale"
        return f"<{type(self).__name__} {shown}>"

    # -- reading and facts

    def to_python(self) -> Any:
        """The node as plain Python data, as ``loads`` would read it."""
        return self._h().value(self._d, list(self._path))

    def _info(self) -> tuple:
        return self._h().info(self._d, list(self._path))

    @property
    def style(self) -> str:
        """``"block"`` or ``"flow"`` for a collection (``"alias"`` for an alias)."""
        return self._info()[0]

    @property
    def tag(self) -> str | None:
        """The explicit tag written on the node, resolved to its full form, or None."""
        return self._info()[1]

    @property
    def anchor(self) -> str | None:
        """The anchor name written on the node (no ``&``), or None."""
        return self._info()[2]

    @property
    def comment(self) -> str | None:
        """The raw comment text at the end of the node's line, else the comment lines above it.

        Read-only."""
        return self._info()[3]

    @property
    def position(self) -> Position | None:
        """Line and column where the node starts, or None when it has no place in the text."""
        _, _, _, _, line, column = self._info()
        return None if line is None else Position(line, column)


class MappingView(_Node, MutableMapping):
    """A live view of a mapping in a document; edits write through, keeping the text around."""

    __slots__ = ()
    __hash__ = None  # type: ignore[assignment]

    def _kind(self) -> str:
        return "mapping"

    def __eq__(self, other: Any) -> Any:
        return _Node.__eq__(self, other)


class SequenceView(_Node, MutableSequence):
    """A live view of a sequence in a document; edits write through, keeping the text around."""

    __slots__ = ()
    __hash__ = None  # type: ignore[assignment]

    def _kind(self) -> str:
        return "sequence"

    def __eq__(self, other: Any) -> Any:
        return _Node.__eq__(self, other)

    def insert(self, index: int, value: Any) -> None:
        h = self._h()
        if index < 0:
            index = max(0, index + self._length(h))
        index = min(index, sys.maxsize)
        if isinstance(value, _Node):
            value = value.to_python()
        h.insert(self._d, list(self._path), index, value)
        # The items from `index` on moved, unless this went past the end (views cannot be there).
        self._core.record(self._d, self._path, index)

    def append(self, value: Any) -> None:
        # One call into the backend: the length is not read first, so two threads appending
        # never choose the same place.
        if isinstance(value, _Node):
            value = value.to_python()
        self._h().insert(self._d, list(self._path), sys.maxsize, value)


_MAPPING_API = frozenset(
    {"clear", "get", "items", "keys", "pop", "popitem", "setdefault", "update", "values"}
)
_SEQUENCE_API = frozenset(
    {
        "append",
        "clear",
        "count",
        "extend",
        "index",
        "insert",
        "pop",
        "remove",
        "reverse",
    }
)


class _Documents(Sequence):
    """The documents of a stream, each a :class:`Document` that shares the stream."""

    __slots__ = ("_core",)

    def __init__(self, core: _Core) -> None:
        self._core = core

    def __len__(self) -> int:
        return self._core.h.count()

    def __getitem__(self, index: Any) -> Any:
        n = len(self)
        if isinstance(index, slice):
            return [Document(self._core, i, ()) for i in range(*index.indices(n))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("document indices must be integers")
        if index < 0:
            index += n
        if not 0 <= index < n:
            raise IndexError("document index out of range")
        return Document(self._core, index, ())


class Document(_Node):
    """A YAML stream that writes back byte for byte, edited in place.

    A document behaves as the root of its first document (or of document ``d`` when taken from
    :attr:`documents`): ``doc["a"]["b"] = 1``, ``del doc["k"]``, ``len(doc)``, iteration and
    ``in``. ``str(doc)``, ``bytes(doc)`` and :meth:`dump` write the whole stream.
    """

    __slots__ = ()

    def _kind(self) -> str:
        h = self._core.h
        if h.count() <= self._d:
            return "scalar"
        try:
            return h.kind(self._d, [])
        except KeyError:  # a document with no content at all
            return "scalar"

    def __getattr__(self, name: str) -> Any:
        # `doc.keys()`, `doc.append(x)`: whatever the root is, it answers its own methods.
        if name in _MAPPING_API or name in _SEQUENCE_API:
            kind = self._kind()
            if kind == "mapping" and name in _MAPPING_API:
                return getattr(MappingView(self._core, self._d, ()), name)
            if kind == "sequence" and name in _SEQUENCE_API:
                return getattr(SequenceView(self._core, self._d, ()), name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def to_python(self) -> Any:
        """The document as plain Python data, as ``loads`` would read it."""
        if self._core.h.count() <= self._d:
            return None
        try:
            return super().to_python()
        except KeyError:
            return None

    @property
    def encoding(self) -> str:
        """The codec the input was read in (``"utf-8"``, ``"utf-16-le"``, ...)."""
        return self._core.h.encoding

    @property
    def documents(self) -> Sequence[Document]:
        """Every document of the stream, as handles that share it."""
        return _Documents(self._core)

    def __bytes__(self) -> bytes:
        return bytes(self._core.h.emit())

    def __str__(self) -> str:
        h = self._core.h
        return bytes(h.emit()).decode(h.encoding)

    def dump(self, fp: IO[str] | IO[bytes]) -> None:
        """Write the stream to an open file: bytes to a binary file, text to a text file."""
        binary = _is_binary(fp)
        fp.write(bytes(self) if binary else str(self))  # type: ignore[arg-type]

    def at(self, pointer: str, *, document: int | None = None) -> Any:
        """The value at an RFC 6901 pointer (``"/spec/containers/0/image"``): plain data for a
        scalar, a view for a collection. A missing path raises ``KeyError`` or ``IndexError``."""
        steps = _pointer(pointer)
        d = self._d if document is None else document
        node = Document(self._core, d, ())
        if document is not None and not 0 <= d < self._core.h.count():
            raise IndexError("document index out of range")
        if not steps:
            return node if node._kind() != "scalar" else node.to_python()
        return node._view(tuple(steps))

    def __repr__(self) -> str:
        return f"<Document {self.to_python()!r}>"


def _is_binary(fp: Any) -> bool:
    import io

    if isinstance(fp, (io.RawIOBase, io.BufferedIOBase)):
        return True
    if isinstance(fp, io.TextIOBase):
        return False
    return "b" in getattr(fp, "mode", "")


_ESCAPE = re.compile(r"~(.?)")


def _unescape(match: re.Match[str]) -> str:
    c = match.group(1)
    if c == "0":
        return "~"
    if c == "1":
        return "/"
    raise ValueError("in a JSON pointer, '~' must be followed by 0 or 1")


def _pointer(pointer: str) -> list[str]:
    """Split an RFC 6901 pointer into its tokens, unescaped."""
    if not isinstance(pointer, str):
        raise TypeError(f"a pointer is a str, not {type(pointer).__name__}")
    if pointer == "":
        return []
    if pointer[0] != "/":
        raise ValueError("a JSON pointer is empty or starts with '/'")
    return [
        _ESCAPE.sub(_unescape, part) if "~" in part else part for part in pointer[1:].split("/")
    ]


_DEFAULT_LIMITS = Limits()


def parse(data: str | bytes, *, limits: Limits | None = None) -> Document:
    """Read ``data`` as a lossless document.

    ``str(doc)`` and ``bytes(doc)`` give the input back byte for byte until it is edited, and
    after an edit differ from it only where the edit was. Bytes keep their encoding and byte
    order mark.

    Raises:
        YAMLError: the text is not valid YAML or goes past a limit.
        TypeError: ``data`` is neither text nor bytes, or ``limits`` is not a ``Limits``.
        NotImplementedError: the active backend has no lossless documents yet.
    """
    if limits is None:
        limits = _DEFAULT_LIMITS
    elif not isinstance(limits, Limits):
        raise TypeError(f"limits must be a Limits, not {type(limits).__name__}")
    if not isinstance(data, (str, bytes, bytearray, memoryview)):
        raise TypeError(f"expected str or bytes, not {type(data).__name__}")
    impl = _api._impl
    make = getattr(impl, "parse", None)
    if make is None:
        raise NotImplementedError("the active backend has no lossless documents yet")
    return Document(_Core(make(data, limits)), 0, ())
