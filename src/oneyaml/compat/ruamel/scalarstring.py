"""String types that choose a style for a value written from scratch.

``LiteralScalarString("a\\nb\\n")`` is written as a ``|`` block, ``DoubleQuotedScalarString`` in
double quotes, and so on. They are plain ``str`` otherwise. A value read from a document is a
plain ``str``: the document keeps its own style, so there is nothing to preserve.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DoubleQuotedScalarString",
    "FoldedScalarString",
    "LiteralScalarString",
    "PlainScalarString",
    "PreservedScalarString",
    "ScalarString",
    "SingleQuotedScalarString",
    "preserve_literal",
    "walk_tree",
]


class ScalarString(str):
    """A string that also says how it wants to be written (``style``)."""

    __slots__ = ("_anchor",)
    style = ""

    def __new__(cls, value: Any = "", anchor: str | None = None) -> ScalarString:
        obj = str.__new__(cls, value)
        obj._anchor = anchor
        return obj

    def replace(self, old: str, new: str, count: int = -1) -> ScalarString:  # type: ignore[override]
        return type(self)(str.replace(self, old, new, count))

    @property
    def anchor(self) -> str | None:
        return self._anchor

    def yaml_set_anchor(self, value: str, always_dump: bool = False) -> None:
        self._anchor = value


class LiteralScalarString(ScalarString):
    """Written as a ``|`` block."""

    __slots__ = ()
    style = "|"


PreservedScalarString = LiteralScalarString


class FoldedScalarString(ScalarString):
    """Written as a ``>`` block."""

    __slots__ = ()
    style = ">"


class SingleQuotedScalarString(ScalarString):
    """Written in single quotes."""

    __slots__ = ()
    style = "'"


class DoubleQuotedScalarString(ScalarString):
    """Written in double quotes."""

    __slots__ = ()
    style = '"'


class PlainScalarString(ScalarString):
    """Written without quotes when the text allows it."""

    __slots__ = ()
    style = ""


def preserve_literal(s: str) -> LiteralScalarString:
    """``s`` with line breaks normalised to ``\\n``, as a literal block string."""
    return LiteralScalarString(s.replace("\r\n", "\n").replace("\r", "\n"))


def walk_tree(base: Any, map: Any = None) -> None:
    """Turn the strings in a tree of dicts and lists that contain a given character into the
    string type mapped to that character (by default: multi-line strings become literal blocks).

    ``map`` is an ordered mapping from a character to a function that takes the string; the
    first character found wins. The tree is walked with an explicit stack.
    """
    from collections.abc import MutableMapping, MutableSequence

    if map is None:
        map = {"\n": preserve_literal}
    stack = [base]
    while stack:
        node = stack.pop()
        if isinstance(node, MutableMapping):
            slots: Any = list(node)
        elif isinstance(node, MutableSequence):
            slots = range(len(node))
        else:
            continue
        for k in slots:
            v = node[k]
            if isinstance(v, str):
                for ch in map:
                    if ch in v:
                        node[k] = map[ch](v)
                        break
            else:
                stack.append(v)
