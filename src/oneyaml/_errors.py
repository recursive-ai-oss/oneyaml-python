"""Exceptions raised by oneyaml.

Every failure on a YAML input is a :class:`YAMLError`, a ``ValueError`` subclass (as
``json.JSONDecodeError`` is), so ``except ValueError`` catches it too.
"""

from __future__ import annotations

__all__ = ["LimitError", "ParseError", "YAMLError"]

# Categories that mean "the text is not acceptable YAML" and "a resource limit was hit".
_PARSE = frozenset({"syntax", "encoding", "duplicate-key", "unresolved-tag"})


class YAMLError(ValueError):
    """A YAML input could not be read.

    Attributes:
        category: why it failed: ``syntax``, ``encoding``, ``limit``, ``duplicate-key``,
            ``unresolved-tag``, or ``other``.
        line: 1-based line of the problem, or None when it has no place in the text.
        column: 1-based column, counted in characters, or None.
        offset: 0-based character offset into the decoded text, or None.
        message: the description without the position.
        events: lines of the event stream produced before the error (may be empty).
    """

    def __init__(
        self,
        message: str = "",
        *,
        category: str = "other",
        line: int | None = None,
        column: int | None = None,
        offset: int | None = None,
        events: list[str] | None = None,
    ) -> None:
        where = "" if line is None else f" (line {line}, column {column})"
        super().__init__(f"{message}{where}")
        self.message = message
        self.category = category
        self.line = line
        self.column = column
        self.offset = offset
        self.events = list(events) if events else []

    def __reduce__(self):
        return (_rebuild, (type(self), self.message, self.category, self.line, self.column,
                           self.offset, self.events))  # fmt: skip


class ParseError(YAMLError):
    """The input is not acceptable YAML: syntax, encoding, a duplicate key or a bad tag."""


class LimitError(YAMLError):
    """The input exceeds a configured limit (depth, size, alias expansion, digits)."""


def _rebuild(cls, message, category, line, column, offset, events):
    return cls(message, category=category, line=line, column=column, offset=offset, events=events)


def make_error(
    message: str,
    category: str = "other",
    line: int | None = None,
    column: int | None = None,
    offset: int | None = None,
    events: list[str] | None = None,
) -> YAMLError:
    """Build the exception class that fits the category (used by the native backend too)."""
    construct = category == "construct"
    if construct:
        # A value that does not fit its tag, met while building values: a syntax error to the
        # caller. The compat modules read the mark to call it a construction error.
        category = "syntax"
    if category == "limit":
        cls = LimitError
    elif category in _PARSE:
        cls = ParseError
    else:
        cls = YAMLError
    err = cls(message, category=category, line=line, column=column, offset=offset, events=events)
    err.construct = construct
    return err
