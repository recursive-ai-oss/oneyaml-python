"""``CommentedMap`` and ``CommentedSeq``: what a round-trip load returns.

They are a ``dict`` and a ``list``, so reading, ``len``, iteration, ``==`` and ``json.dumps`` work
as on the plain types, at the plain types' speed. A loaded tree remembers the document it came
from; when it is dumped, the document is edited to match the tree, so what the program did not
change keeps its bytes. The mutating methods below only note that something changed.

Comments are written for data built in the program: ``yaml_set_start_comment``,
``yaml_set_comment_before_after_key`` and ``yaml_add_eol_comment`` take effect when the data is
dumped without a document behind it. On a loaded tree they raise ``NotImplementedError`` at dump
time instead of being dropped; the comments already in the document are kept.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

__all__ = ["CommentedMap", "CommentedSeq"]


class _Source:
    """The document a loaded tree came from, shared by every container of the tree."""

    __slots__ = ("changed", "comments", "dirty", "doc", "limits", "raw")

    def __init__(self, raw: Any, doc: Any, limits: Any) -> None:
        self.raw = raw  # the input, to read again when an earlier dump edited the document
        self.doc = doc
        self.limits = limits
        self.changed = False  # some container of the tree was mutated
        self.dirty = False  # the document holds edits from an earlier dump
        self.comments = False  # a comment was set on a container of the tree


class _Extras:
    """Comments, flow/block choice and anchor of one container (made when first needed)."""

    __slots__ = ("after", "anchor", "before", "eol", "flow", "start")

    def __init__(self) -> None:
        self.start: list[str] = []
        self.before: dict[Any, list[str]] = {}
        self.after: dict[Any, list[str]] = {}
        self.eol: dict[Any, str] = {}
        self.flow: bool | None = None
        self.anchor: str | None = None

    # The ``fa`` face (format attributes) and the ``ca`` face (comment attributes) of ruamel.
    def set_flow_style(self) -> None:
        self.flow = True

    def set_block_style(self) -> None:
        self.flow = False

    def flow_style(self, default: bool | None = None) -> bool | None:
        return default if self.flow is None else self.flow

    @property
    def items(self) -> dict:
        return self.eol

    @property
    def comment(self) -> list[str] | None:
        return self.start or None

    @property
    def end(self) -> list:
        return []

    @property
    def pre(self) -> list:
        return []

    def pending(self) -> bool:
        return bool(self.start or self.before or self.after or self.eol)


def _comment_lines(text: str | None, indent: int) -> list[str]:
    if not text:
        return []
    pad = " " * indent
    return [pad + ("# " + ln if ln and not ln.startswith("#") else ln) for ln in text.split("\n")]


def _eol(text: str) -> str:
    text = text.strip()
    return text if text.startswith("#") else "# " + text


class _Mixin:
    """What both containers share: the document link and the comment helpers."""

    __slots__ = ()
    _src: _Source | None
    _d: int | None
    _x: _Extras | None

    def _extras(self) -> _Extras:
        x = self._x
        if x is None:
            x = self._x = _Extras()
        return x

    def _note(self, comment: bool = False) -> None:
        s = self._src
        if s is not None:
            s.changed = True
            if comment:
                s.comments = True

    @property
    def ca(self) -> _Extras:
        return self._extras()

    @property
    def fa(self) -> _Extras:
        return self._extras()

    def yaml_set_anchor(self, value: str, always_dump: bool = False) -> None:
        self._extras().anchor = value

    def yaml_anchor(self) -> str | None:
        x = self._x
        return None if x is None else x.anchor

    def yaml_set_start_comment(self, comment: str, indent: int = 0) -> None:
        """Comment lines before the first entry."""
        self._extras().start = _comment_lines(comment, indent)
        self._note(True)

    def yaml_set_comment_before_after_key(
        self, key: Any, before: str | None = None, indent: int = 0, after: str | None = None
    ) -> None:
        """Comment lines above the entry ``key`` (an index in a sequence) and below its line."""
        x = self._extras()
        if before is not None:
            x.before[key] = _comment_lines(before, indent)
        if after is not None:
            x.after[key] = _comment_lines(after, indent)
        self._note(True)

    def yaml_add_eol_comment(self, comment: str, key: Any, column: int | None = None) -> None:
        """A comment at the end of the line of the entry ``key``."""
        self._extras().eol[key] = _eol(comment)
        self._note(True)


class CommentedMap(_Mixin, dict):
    """A mapping read from YAML: a ``dict`` in document order."""

    __slots__ = ("_d", "_src", "_x")

    def __init__(self, *args: Any, **kw: Any) -> None:
        dict.__init__(self, *args, **kw)
        self._src = None
        self._d = None
        self._x = None

    # -- mutation: the same as dict's, plus a note for the next dump

    def __setitem__(self, key: Any, value: Any) -> None:
        self._note()
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: Any) -> None:
        self._note()
        dict.__delitem__(self, key)

    def __ior__(self, other: Any) -> CommentedMap:
        self._note()
        dict.update(self, other)
        return self

    def clear(self) -> None:
        self._note()
        dict.clear(self)

    def pop(self, key: Any, *default: Any) -> Any:
        self._note()
        return dict.pop(self, key, *default)

    def popitem(self) -> tuple[Any, Any]:
        self._note()
        return dict.popitem(self)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._note()
        return dict.setdefault(self, key, default)

    def update(self, *args: Any, **kw: Any) -> None:
        self._note()
        dict.update(self, *args, **kw)

    def insert(self, pos: int, key: Any, value: Any, comment: str | None = None) -> None:
        """Put ``key: value`` at position ``pos`` (an existing ``key`` moves there)."""
        self._note()
        items = [(k, v) for k, v in dict.items(self) if k != key]
        if pos < 0:
            pos = max(0, pos + len(items) + 1)
        items.insert(pos, (key, value))
        dict.clear(self)
        dict.update(self, items)
        if comment is not None:
            self.yaml_add_eol_comment(comment, key)

    def update_key_value(self, key: Any) -> None:
        """Move ``key`` to the end, as if it had just been set."""
        if key in self:
            self._note()
            dict.__setitem__(self, key, dict.pop(self, key))

    # -- copies

    def copy(self) -> CommentedMap:
        new = CommentedMap(self)
        new._x = self._x
        return new

    __copy__ = copy

    def __deepcopy__(self, memo: dict) -> CommentedMap:
        new = CommentedMap()
        memo[id(self)] = new
        for k, v in dict.items(self):
            dict.__setitem__(new, deepcopy(k, memo), deepcopy(v, memo))
        new._x = self._x
        # The copy stays tied to the same document: dumping it edits what differs from the
        # document as it was read.
        new._src = self._src
        new._d = self._d
        return new

    def __reduce__(self) -> Any:
        return (CommentedMap, (dict(self),))


class CommentedSeq(_Mixin, list):
    """A sequence read from YAML: a ``list``."""

    __slots__ = ("_d", "_src", "_x")

    def __init__(self, *args: Any) -> None:
        list.__init__(self, *args)
        self._src = None
        self._d = None
        self._x = None

    def __setitem__(self, index: Any, value: Any) -> None:
        self._note()
        list.__setitem__(self, index, value)

    def __delitem__(self, index: Any) -> None:
        self._note()
        list.__delitem__(self, index)

    def __iadd__(self, other: Any) -> CommentedSeq:
        self._note()
        list.extend(self, other)
        return self

    def __imul__(self, n: Any) -> CommentedSeq:
        self._note()
        list.__imul__(self, n)
        return self

    def append(self, value: Any) -> None:
        self._note()
        list.append(self, value)

    def clear(self) -> None:
        self._note()
        list.clear(self)

    def extend(self, values: Any) -> None:
        self._note()
        list.extend(self, values)

    def insert(self, index: int, value: Any) -> None:
        self._note()
        list.insert(self, index, value)

    def pop(self, *index: Any) -> Any:
        self._note()
        return list.pop(self, *index)

    def remove(self, value: Any) -> None:
        self._note()
        list.remove(self, value)

    def reverse(self) -> None:
        self._note()
        list.reverse(self)

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        self._note()
        list.sort(self, key=key, reverse=reverse)

    def copy(self) -> CommentedSeq:
        new = CommentedSeq(self)
        new._x = self._x
        return new

    __copy__ = copy

    def __deepcopy__(self, memo: dict) -> CommentedSeq:
        new = CommentedSeq()
        memo[id(self)] = new
        list.extend(new, [deepcopy(v, memo) for v in list.__iter__(self)])
        new._x = self._x
        new._src = self._src
        new._d = self._d
        return new

    def __reduce__(self) -> Any:
        return (CommentedSeq, (list(self),))
