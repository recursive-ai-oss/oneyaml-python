"""Lossless documents: read a file, change one value, write it back, and only that value moves.

``parse`` returns a :class:`Document` over the active backend's handle. Collections read from it
are live views (:class:`MappingView`, :class:`SequenceView`); scalars read as plain Python
values. Views hold no data: each is a path from the document root, resolved on every use, so
they never show anything but the current text. An edit that removes or replaces a node, or
moves the items of a sequence, makes the views it strands raise :class:`StaleViewError` instead
of quietly addressing whatever is there now.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterable, Iterator, MutableMapping, MutableSequence, Sequence
from typing import IO, Any, NamedTuple

from . import _api
from ._errors import PatchError, YAMLError
from ._limits import Limits
from ._types import FrozenMapping

__all__ = [
    "Document",
    "Location",
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


class Location(NamedTuple):
    """Where a node is in the text, as :meth:`Document.locate` reports it.

    ``line`` and ``column`` are where the node starts, 1-based and counted in characters like
    the position of a :class:`YAMLError`; ``end_line`` and ``end_column`` are the place just
    after its last character; ``comment`` is the node's comment as ``.comment`` of a view reads
    it (a scalar value is plain data and has no ``.comment`` of its own)."""

    line: int
    column: int
    end_line: int
    end_column: int
    comment: str | None = None


# Shifts of one sequence kept apart before they are folded into a single stamp, and paths
# remembered before the books are cleared (views older than that count as stale).
_SHIFTS_KEPT = 64
_BOOKS_MAX = 100_000


def _norm(token: Any) -> str:
    """A token as text, so that a view built from a pointer compares with one built from keys."""
    if isinstance(token, str):
        return token
    if token is None:
        return "null"
    if isinstance(token, bool):
        return "true" if token else "false"
    return str(token)


# Types whose values are read without running any code of the caller's.
_PLAIN = frozenset({str, int, float, bool, type(None)})

_KIDS = dict  # the map type of the books' tree (a test swaps in one that counts lookups)


class _Book:
    """What edits did at one path: the node there was removed or replaced (``dead``), or the
    items of the sequence there moved (``moves``, ``floor``). Children are the paths below."""

    __slots__ = ("dead", "floor", "kids", "moves")

    def __init__(self) -> None:
        self.dead = 0
        self.floor = 0
        self.moves: list[tuple[int, int]] | None = None
        self.kids: dict[str, _Book] = _KIDS()


class _Core:
    """What every view of one stream shares: the backend handle and what edits did to paths.

    An edit that removes or replaces the node at a path stamps that path in a tree of books
    (one per document); one that moves the items of a sequence from an index on stamps the
    sequence. A view asks about the books along its own path in one pass down the tree, so a
    check costs the depth of the view and never the number of edits made since the view was
    taken, and stops at the first path nothing was ever recorded under.
    """

    __slots__ = ("books", "floor", "h", "lock", "nbooks", "tick")

    def __init__(self, handle: Any) -> None:
        self.h = handle
        # Held across "is this view still good?" + the backend call + the note of an edit, so
        # that another thread's insert cannot slip in between and move the item under a view.
        # No user code ever runs under it: values are converted before it is taken.
        self.lock = threading.RLock()
        self.tick = 0
        self.books: dict[int, _Book] = {}
        self.nbooks = 0
        # Views older than this are all treated as stale (the books were cleared).
        self.floor = 0

    def record(self, d: int, path: tuple, index: int | None = None) -> None:
        """Note an edit: the node at ``path`` is gone (``index`` None), or the items of the
        sequence at ``path`` from ``index`` on moved."""
        self.tick += 1
        book = self.books.get(d)
        if book is None:
            book = self.books[d] = _Book()
            self.nbooks += 1
        for step in path:
            name = _norm(step)
            kid = book.kids.get(name)
            if kid is None:
                kid = book.kids[name] = _Book()
                self.nbooks += 1
            book = kid
        if index is None:
            book.dead = self.tick
        else:
            if book.moves is None:
                book.moves = []
            book.moves.append((self.tick, index))
            if len(book.moves) > _SHIFTS_KEPT:
                book.floor = self.tick
                book.moves.clear()
        if self.nbooks > _BOOKS_MAX:
            self._clear()
            self.floor = self.tick

    def _clear(self) -> None:
        self.books.clear()
        self.nbooks = 0

    def invalidate(self) -> None:
        """Every view taken so far is stale: a patch changed the document in ways not tracked
        edit by edit. A document's root is not a view of a node and stays good."""
        self.tick += 1
        self._clear()
        self.floor = self.tick

    def stale(self, d: int, path: tuple, tick: int) -> bool:
        if tick < self.floor and path:
            return True
        book = self.books.get(d)
        last = len(path)
        i = 0
        while book is not None:
            if book.dead > tick:
                return True
            if i == last:
                return False
            step = _norm(path[i])
            if book.floor > tick:
                return True
            moves = book.moves
            if moves and step.isdigit():
                at = int(step)
                if any(t > tick and index <= at for t, index in moves):
                    return True
            book = book.kids.get(step)
            i += 1
        return False


def _token(key: Any) -> Any:
    """The path token for a mapping key: text as it is, other scalars as themselves."""
    if isinstance(key, (str, int, float, tuple, FrozenMapping)) or key is None:
        return key
    raise TypeError(f"{type(key).__name__} cannot be a YAML mapping key here")


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
        # One call: the kind, and the value too when it is a scalar.
        kind, value = self._core.h.read(self._d, path)
        if kind == "scalar":
            return value
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

    def _keyed(self, key: Any) -> tuple[str, tuple, Any]:
        """``(kind, path, handle)`` of the child ``key`` of this node, the handle having passed
        the staleness check."""
        kind = self._kind()
        h = self._h()
        if kind == "mapping":
            return kind, (*self._path, _token(key)), h
        if kind == "sequence":
            return kind, (*self._path, str(self._index(h, key))), h
        raise TypeError("a scalar has no items")

    # -- mapping and sequence protocol, by what the node is

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            self._h()
            if self._kind() != "sequence":
                raise TypeError("only a sequence can be sliced")
            return [self[i] for i in range(*key.indices(len(self)))]
        with self._core.lock:
            path = self._keyed(key)[1]
            return self._view(path)

    def __setitem__(self, key: Any, value: Any) -> None:
        if isinstance(key, slice):
            raise TypeError("slice assignment is not supported")
        value = self._prepared(value)
        with self._core.lock:
            _, path, h = self._keyed(key)
            # True when the edit could move or replace a collection, which strands views.
            if h.set(self._d, path, value):
                self._core.record(self._d, path)

    def _prepared(self, value: Any) -> Any:
        """The value as the backend wants it, converted before the stream lock is taken: reading
        it may run any code of the caller's (a lazy sequence, say), which must never run while
        another thread waits for that lock."""
        if type(value) in _PLAIN:
            return value  # a plain scalar runs no code of the caller's to read
        if isinstance(value, _Node):
            value = value.to_python()
        return self._core.h.prepare(value)

    def __delitem__(self, key: Any) -> None:
        if isinstance(key, slice):
            raise TypeError("slice deletion is not supported")
        with self._core.lock:
            kind, path, h = self._keyed(key)
            h.remove(self._d, path)
            if kind == "sequence":
                self._core.record(self._d, self._path, int(path[-1]))
            self._core.record(self._d, path)

    def __len__(self) -> int:
        with self._core.lock:
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
            path = (*self._path, _token(key))
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

    # A copy of a document or a view is plain data, as ``to_python`` gives it: the live handle
    # behind it cannot be duplicated, and a copy that kept writing into the same text would
    # not be one. ``parse(bytes(doc))`` makes an independent document.
    def __copy__(self) -> Any:
        return self.to_python()

    def __deepcopy__(self, memo: Any) -> Any:
        return self.to_python()

    # -- reading and facts

    def to_python(self) -> Any:
        """The node as plain Python data, as ``loads`` would read it."""
        with self._core.lock:
            return self._h().value(self._d, list(self._path))

    def _info(self) -> tuple:
        with self._core.lock:
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
        """The comment that belongs to the node, as written (with its ``#``), or None.

        That is the comment at the end of the node's line, else the comment lines above it
        (several lines are joined with ``"\\n"``). A mapping entry owns the comments around its
        key, so a mapping value reads those: ``a: 1  # c`` gives ``"# c"`` for ``doc["a"]`` and
        for a collection under ``a:`` the comment after the colon. A comment directly above the
        first entry of a document belongs to that entry; the document itself reads the
        comments before ``---`` and those set apart from the content by a blank line.

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

    def clear(self) -> None:
        # The keys are read once; each entry is then removed by name.
        with self._core.lock:
            for key in self._h().keys(self._d, list(self._path)):
                del self[key]

    def popitem(self) -> tuple[Any, Any]:
        with self._core.lock:
            keys = self._h().keys(self._d, list(self._path))
            if not keys:
                raise KeyError("popitem(): mapping is empty")
            key = keys[-1]
            value = self[key]
            if isinstance(value, _Node):
                value = value.to_python()
            del self[key]
            return key, value

    def __eq__(self, other: Any) -> Any:
        return _Node.__eq__(self, other)


class SequenceView(_Node, MutableSequence):
    """A live view of a sequence in a document; edits write through, keeping the text around."""

    __slots__ = ()
    __hash__ = None  # type: ignore[assignment]

    def _kind(self) -> str:
        return "sequence"

    def clear(self) -> None:
        with self._core.lock:
            for i in reversed(range(self._length(self._h()))):
                del self[i]

    def __eq__(self, other: Any) -> Any:
        return _Node.__eq__(self, other)

    def insert(self, index: int, value: Any) -> None:
        value = self._prepared(value)
        with self._core.lock:
            h = self._h()
            if index < 0:
                index = max(0, index + self._length(h))
            index = min(index, sys.maxsize)
            h.insert(self._d, list(self._path), index, value)
            # The items from `index` on moved, unless this went past the end (views cannot be
            # there).
            self._core.record(self._d, self._path, index)

    def append(self, value: Any) -> None:
        # One call into the backend: the length is not read first, so two threads appending
        # never choose the same place.
        value = self._prepared(value)
        with self._core.lock:
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

    def __repr__(self) -> str:
        return f"<documents of a stream: {len(self)}>"

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

    __slots__ = ("_root",)

    def __init__(self, core: _Core, d: int, path: tuple) -> None:
        super().__init__(core, d, path)
        self._root: tuple[int, str] | None = None

    def _kind(self) -> str:
        # What the root is changes only with an edit that moves the tick (a patch, say), so the
        # answer is kept for as long as the tick stays. The tick is read first: an edit landing
        # between the two reads leaves a note that is already out of date.
        core = self._core
        tick = core.tick
        known = self._root
        if known is not None and known[0] == tick:
            return known[1]
        try:
            kind = core.h.kind(self._d, ())
        except (KeyError, IndexError):  # no such document, or one with no content at all
            kind = "scalar"
        self._root = (tick, kind)
        return kind

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
        _check_pointer(pointer)
        d = self._d if document is None else document
        node = Document(self._core, d, ())
        if document is not None and not 0 <= d < self._core.h.count():
            raise IndexError("document index out of range")
        if not pointer:
            return node if node._kind() != "scalar" else node.to_python()
        with self._core.lock:
            # The engine reads the pointer, naming a key by its text; two keys with the same
            # text make it ambiguous, which is refused rather than answered with one of them.
            # What it finds is addressed by the keys' own values, so a view of it is stale when
            # exactly that key goes.
            path = self._core.h.resolve(d, pointer)
            return node._view(tuple(path))

    def locate(self, path: str | Sequence[Any], *, document: int | None = None) -> Location:
        """Where the node at ``path`` is in the text, scalars included.

        ``path`` is an RFC 6901 pointer (``"/spec/containers/0/image"``) or a sequence of keys
        and indices, the form validation libraries report errors in (``("spec", "containers",
        0, "image")``). For a mapping value this is the place where the value starts; a
        block collection starts at its first entry. Lines and columns are 1-based, as in
        :class:`YAMLError`. A missing path raises ``KeyError`` or ``IndexError``, as :meth:`at`
        does.
        """
        d = self._d if document is None else document
        if isinstance(d, bool) or not isinstance(d, int):
            raise TypeError(f"document must be an integer, not {type(d).__name__}")
        h = self._core.h
        if not 0 <= d < h.count():
            raise IndexError("document index out of range")
        with self._core.lock:
            if isinstance(path, str):
                _check_pointer(path)
                steps = list(h.resolve(d, path))
            else:
                if isinstance(path, (bytes, bytearray)) or not isinstance(path, Iterable):
                    raise TypeError(
                        f"a path is a pointer or a sequence of keys, not {type(path).__name__}"
                    )
                steps = self._steps(h, d, path)
            return Location(*h.locate(d, steps))

    @staticmethod
    def _steps(h: Any, d: int, path: Iterable[Any]) -> list:
        """The backend tokens for keys and indices: an index into a sequence (negative counts
        from the end) becomes its text, a key stays exact, as a view reads it."""
        steps: list = []
        for key in path:
            kind = h.kind(d, steps)
            if kind == "mapping":
                steps.append(_token(key))
            elif kind == "sequence":
                if isinstance(key, str) and key.isascii() and key.isdigit():
                    key = int(key)
                if isinstance(key, bool) or not isinstance(key, int):
                    raise KeyError(key)
                if key < 0:
                    key += h.length(d, steps)
                    if key < 0:
                        raise IndexError("sequence index out of range")
                steps.append(str(key))
            else:
                raise KeyError(key)
        return steps

    def patch(self, ops: Any, *, document: int | None = None) -> None:
        """Apply an RFC 6902 patch, all or nothing.

        ``ops`` is a list of operation dicts (``{"op": "add", "path": "/a/b", "value": 1}``) or
        JSON text. Pointers read keys by their text. On any failure the document is left
        exactly as it was and a :class:`PatchError` names the operation and the reason. After a
        patch that changed something, views taken before it are stale. ``document`` picks a
        document of the stream (this handle's own by default).
        """
        self._apply("patch", ops, document)

    def merge_patch(self, patch: Any, *, document: int | None = None) -> None:
        """Apply an RFC 7396 merge patch (a JSON-shaped value; ``None`` members delete), all or
        nothing, as :meth:`patch` does."""
        self._apply("merge_patch", patch, document)

    def _apply(self, how: str, patch: Any, document: int | None) -> None:
        d = self._d if document is None else document
        if isinstance(d, bool) or not isinstance(d, int):
            raise TypeError(f"document must be an integer, not {type(d).__name__}")
        if d < 0:
            raise PatchError(f"the stream has no document {d}", category="patch-path")
        if isinstance(patch, _Node):
            patch = patch.to_python()
        core = self._core
        if not isinstance(patch, str):
            patch = core.h.prepare(patch)
        with core.lock:
            before = core.h.version()
            getattr(core.h, how)(min(d, sys.maxsize), patch)
            if core.h.version() != before:
                core.invalidate()

    def __repr__(self) -> str:
        return f"<Document {self.to_python()!r}>"


def _check_pointer(pointer: Any) -> None:
    if not isinstance(pointer, str):
        raise TypeError(f"a pointer is a str, not {type(pointer).__name__}")
    if pointer and not pointer.startswith("/"):
        raise PatchError(
            f"a JSON Pointer must be empty or start with '/', as in '/spec/ports'; got {pointer!r}",
            category="patch-invalid",
        )


def _is_binary(fp: Any) -> bool:
    import io

    if isinstance(fp, (io.RawIOBase, io.BufferedIOBase)):
        return True
    if isinstance(fp, io.TextIOBase):
        return False
    return "b" in getattr(fp, "mode", "")


_DEFAULT_LIMITS = Limits()

# Lets ``dumps`` write a document or a view as its current value.
_api._NODE = _Node


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
