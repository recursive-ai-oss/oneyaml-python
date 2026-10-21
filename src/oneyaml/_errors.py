"""Exceptions raised by oneyaml.

Every failure on a YAML input is a :class:`YAMLError`, a ``ValueError`` subclass (as
``json.JSONDecodeError`` is), so ``except ValueError`` catches it too.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["LimitError", "ParseError", "PatchError", "YAMLError"]

Category = Literal[
    "syntax",
    "encoding",
    "limit",
    "duplicate-key",
    "unresolved-tag",
    "patch-path",
    "patch-test",
    "patch-invalid",
    "refused-alias",
    "refused-ambiguous",
    "other",
]

# Categories that mean "the text is not acceptable YAML" and "a resource limit was hit".
_PARSE = frozenset({"syntax", "encoding", "duplicate-key", "unresolved-tag"})
# Categories of an edit or a patch that was refused.
_PATCH = frozenset(
    {"patch-path", "patch-test", "patch-invalid", "refused-alias", "refused-ambiguous"}
)


class YAMLError(ValueError):
    """A YAML input could not be read.

    Attributes:
        category: why it failed: ``syntax``, ``encoding``, ``limit``, ``duplicate-key``,
            ``unresolved-tag``, or ``other``.
        line: 1-based line of the problem, or None when it has no place in the text.
        column: 1-based column, counted in characters, or None.
        offset: 0-based character offset into the decoded text, or None.
        message: the description without the position.
        source: the name of the file the text was read from, when ``load`` knew it, else None.
        events: lines of the event stream produced before the error (a debugging aid; may be
            empty).
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
        self.source: str | None = None
        self.message = message
        self.category: Category = category  # type: ignore[assignment]
        self.line = line
        self.column = column
        self.offset = offset
        self.events = list(events) if events else []

    def set_source(self, name: str) -> None:
        """Record the file the text came from; the message then names it."""
        if self.source is not None:
            return
        self.source = name
        where = (
            f"{name}" if self.line is None else f"{name}, line {self.line}, column {self.column}"
        )
        self.args = (f"{self.message} ({where})",)

    def __reduce__(self):
        return (_rebuild, (type(self), self.message, self.category, self.line, self.column,
                           self.offset, self.events))  # fmt: skip


class ParseError(YAMLError):
    """The input is not acceptable YAML: syntax, encoding, a duplicate key or a bad tag."""


class LimitError(YAMLError):
    """The input exceeds a configured limit (depth, size, alias expansion, digits)."""


class PatchError(YAMLError):
    """A patch or an edit could not be applied; the document is unchanged.

    ``index`` is the position of the failing operation in the patch (None for a merge patch or a
    single edit); ``category`` is ``patch-path``, ``patch-test``, ``patch-invalid``,
    ``refused-alias``, ``refused-ambiguous``, ``limit`` or ``other``."""

    def __init__(self, message: str = "", *, index: int | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.index = index

    def __reduce__(self):
        return (_rebuild_patch, (self.message, self.index, self.category))


def _rebuild_patch(message, index, category):
    return PatchError(message, index=index, category=category)


def make_patch_error(message: str, category: str, index: int | None = None) -> PatchError:
    """Build a :class:`PatchError` (used by the native backend)."""
    return PatchError(message, index=index, category=category)


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
    elif category in _PATCH:
        cls = PatchError
    else:
        cls = YAMLError
    err = cls(message, category=category, line=line, column=column, offset=offset, events=events)
    err._construct = construct
    return err
