"""The node classes a PyYAML constructor callback receives.

A node is one element of the composed document: a scalar, a sequence or a mapping, with its
tag, its value (text for a scalar, nodes for the others) and where it began and ended. The
names and attributes are PyYAML's, so code written against it keeps working.

Marks are built when a callback asks for them: a document of a million nodes does not pay for
a million marks that nobody reads.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CollectionNode", "MappingNode", "Node", "ScalarNode", "SequenceNode"]


class Node:
    """A node of the composed document."""

    id = "node"

    # Where a composed node sits in its text; nodes a callback makes itself have none of these.
    _source: Any = None
    _event: tuple | None = None
    _end: int = 0
    _start_mark: Any = None
    _end_mark: Any = None
    # Set by the composer: true when a plain scalar got its tag from the resolver, and the nodes,
    # bytes of text and height the node stands for (what an alias to it is charged).
    _implicit: bool = False
    _merge_key: bool = False
    _measure: tuple = (1, 0, 0)

    def __init__(self, tag: str, value: Any, start_mark: Any, end_mark: Any) -> None:
        self.tag = tag
        self.value = value
        self._start_mark = start_mark
        self._end_mark = end_mark

    @property
    def start_mark(self) -> Any:
        mark = self._start_mark
        if mark is None and self._source is not None:
            mark = self._start_mark = self._source.mark(self._event[1])
        return mark

    @start_mark.setter
    def start_mark(self, mark: Any) -> None:
        self._start_mark = mark

    @property
    def end_mark(self) -> Any:
        mark = self._end_mark
        if mark is None and self._source is not None:
            mark = self._end_mark = self._source.mark(self._end_offset())
        return mark

    @end_mark.setter
    def end_mark(self, mark: Any) -> None:
        self._end_mark = mark

    def _end_offset(self) -> int:
        return self._end

    def __repr__(self) -> str:
        value = self.value
        # A long value would swamp the line; PyYAML prints a short form of the value as well.
        text = repr(value)
        if len(text) > 60:
            text = text[:57] + "..."
        return f"{type(self).__name__}(tag={self.tag!r}, value={text})"


class ScalarNode(Node):
    id = "scalar"

    def __init__(
        self,
        tag: str,
        value: str,
        start_mark: Any = None,
        end_mark: Any = None,
        style: str | None = None,
    ) -> None:
        super().__init__(tag, value, start_mark, end_mark)
        self.style = style


class CollectionNode(Node):
    def __init__(
        self,
        tag: str,
        value: Any,
        start_mark: Any = None,
        end_mark: Any = None,
        flow_style: bool | None = None,
    ) -> None:
        super().__init__(tag, value, start_mark, end_mark)
        self.flow_style = flow_style

    def _end_offset(self) -> int:
        # PyYAML ends a collection where the next token begins, or after its closing bracket.
        return self._source.collection_end(self._end, bool(self.flow_style))


class SequenceNode(CollectionNode):
    id = "sequence"


class MappingNode(CollectionNode):
    id = "mapping"
