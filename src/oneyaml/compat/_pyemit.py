"""PyYAML's output layout, written from a node tree.

The layout is PyYAML's: the same choices of plain, quoted and block scalars, the same
indentation (a sequence under a mapping key stays at the key's column, ``-   x`` at ``indent=4``),
the same folding at ``width``, the same ``...`` after a bare root scalar, the same tags and
anchors. It is written for speed in CPython, not as a copy of PyYAML's event machine: a node
tree is walked once with an explicit stack (so nesting never recurses), strings are classified
with ``re`` and ``str`` methods before any per-character loop runs, and text goes into one list.

``Writer`` holds the position state of the output; ``emit_document`` writes one document.
"""

from __future__ import annotations

import re
from typing import Any

from ._nodes import MappingNode, ScalarNode, SequenceNode

_YAML = "tag:yaml.org,2002:"

DEFAULT_TAG_PREFIXES = {"!": "!", _YAML: "!!"}

_WS = "\0 \t\r\n\x85\u2028\u2029"
_BREAKS = "\n\x85\u2028\u2029"

# ---- scalar analysis ---------------------------------------------------------------------------

# A string of this shape is plain-safe in every context: printable ASCII, no indicator at the start
# or inside, no space at the end. Most keys and words are this, so one regex settles them.
_REST = "".join(c for c in map(chr, range(0x20, 0x7F)) if c not in "#,:?[]{}")
_SIMPLE = re.compile("(?!\\.\\.\\.)[A-Za-z0-9_/.$^()=+<;~\\\\][" + re.escape(_REST) + "]*\\Z").match
_PRINTABLE = re.compile(r"[ -~]+\Z").match
_FLOW_INSIDE = re.compile(r"[,?\[\]{}]").search
_COLON_BLOCK = re.compile(r":(?: |\Z)").search

_ANALYSIS_EMPTY = (True, False, False, True, True, False)
_ANALYSIS_SIMPLE = (False, False, True, True, True, True)


def analyze_scalar(scalar: str, allow_unicode: bool) -> tuple:
    """``(empty, multiline, flow_plain, block_plain, single_quoted, block)`` for ``scalar``.

    Double quotes can always carry the text, so they have no flag."""
    if not scalar:
        return _ANALYSIS_EMPTY
    if scalar[-1] != " " and _SIMPLE(scalar):
        return _ANALYSIS_SIMPLE
    if _PRINTABLE(scalar):
        return _analyze_printable(scalar)
    return _analyze_general(scalar, allow_unicode)


def _analyze_printable(scalar: str) -> tuple:
    """The same answer as the general loop for printable ASCII without line breaks."""
    first = scalar[0]
    flow = block = False
    followed = len(scalar) == 1 or scalar[1] == " "
    if scalar.startswith(("---", "...")):
        flow = block = True
    if first in "#,[]{}&*!|>'\"%@`":
        flow = block = True
    elif first in "?:":
        flow = True
        if followed:
            block = True
    elif first == "-" and followed:
        flow = block = True
    if _FLOW_INSIDE(scalar, 1):
        flow = True
    if ":" in scalar[1:]:
        flow = True
        if _COLON_BLOCK(scalar, 1):
            block = True
    if " #" in scalar:
        flow = block = True
    leading = first == " "
    trailing = scalar[-1] == " "
    plain = not (leading or trailing)
    return (False, False, plain and not flow, plain and not block, True, not trailing)


def _analyze_general(scalar: str, allow_unicode: bool) -> tuple:
    block_indicators = flow_indicators = False
    line_breaks = special_characters = False
    leading_space = leading_break = trailing_space = trailing_break = False
    break_space = space_break = False
    if scalar.startswith(("---", "...")):
        block_indicators = flow_indicators = True
    preceded_by_whitespace = True
    n = len(scalar)
    followed_by_whitespace = n == 1 or scalar[1] in _WS
    previous_space = previous_break = False
    index = 0
    for ch in scalar:
        if index == 0:
            if ch in "#,[]{}&*!|>'\"%@`":
                flow_indicators = block_indicators = True
            if ch in "?:":
                flow_indicators = True
                if followed_by_whitespace:
                    block_indicators = True
            if ch == "-" and followed_by_whitespace:
                flow_indicators = block_indicators = True
        else:
            if ch in ",?[]{}":
                flow_indicators = True
            if ch == ":":
                flow_indicators = True
                if followed_by_whitespace:
                    block_indicators = True
            if ch == "#" and preceded_by_whitespace:
                flow_indicators = block_indicators = True
        if ch in _BREAKS:
            line_breaks = True
            if ch != "\n":
                # NEL, LS and PS fold to a space or a newline when read inside quotes or a block,
                # so they are escaped even when other non-ASCII text is written as it is.
                special_characters = True
        if not (ch == "\n" or " " <= ch <= "~"):
            if (
                ch == "\x85"
                or "\xa0" <= ch <= "\ud7ff"
                or "\ue000" <= ch <= "\ufffd"
                or "\U00010000" <= ch < "\U0010ffff"
            ) and ch != "\ufeff":
                if not allow_unicode:
                    special_characters = True
            else:
                special_characters = True
        if ch == " ":
            if index == 0:
                leading_space = True
            if index == n - 1:
                trailing_space = True
            if previous_break:
                break_space = True
            previous_space = True
            previous_break = False
        elif ch in _BREAKS:
            if index == 0:
                leading_break = True
            if index == n - 1:
                trailing_break = True
            if previous_space:
                space_break = True
            previous_space = False
            previous_break = True
        else:
            previous_space = previous_break = False
        index += 1  # noqa: SIM113
        preceded_by_whitespace = ch in _WS
        followed_by_whitespace = index + 1 >= n or scalar[index + 1] in _WS
    flow_plain = block_plain = single = block = True
    if leading_space or leading_break or trailing_space or trailing_break:
        flow_plain = block_plain = False
    if trailing_space:
        block = False
    if break_space:
        flow_plain = block_plain = single = False
    if space_break or special_characters:
        flow_plain = block_plain = single = block = False
    if line_breaks:
        flow_plain = block_plain = False
    if flow_indicators:
        flow_plain = False
    if block_indicators:
        block_plain = False
    return (False, line_breaks, flow_plain, block_plain, single, block)


# ---- tags and anchors --------------------------------------------------------------------------

_ANCHOR_OK = re.compile(r"[0-9A-Za-z_-]+\Z").match
_TAG_CHARS = frozenset("-;/?:@&=+$,_.~*'()[]") | frozenset(
    chr(c) for c in (*range(48, 58), *range(65, 91), *range(97, 123))
)
_HANDLE_OK = re.compile(r"[0-9A-Za-z_-]*\Z").match


def prepare_version(version: Any) -> str:
    major, minor = version
    if major != 1:
        raise _error("EmitterError", f"unsupported YAML version: {major}.{minor}")
    return f"{major}.{minor}"


def prepare_tag_handle(handle: str) -> str:
    if not handle:
        raise _error("EmitterError", "tag handle must not be empty")
    if handle[0] != "!" or handle[-1] != "!":
        raise _error("EmitterError", f"tag handle must start and end with '!': {handle!r}")
    if not _HANDLE_OK(handle[1:-1]):
        bad = next(ch for ch in handle[1:-1] if not _HANDLE_OK(ch))
        raise _error("EmitterError", f"invalid character {bad!r} in the tag handle: {handle!r}")
    return handle


def _escape(text: str, safe: frozenset) -> str:
    chunks = []
    for ch in text:
        if ch in safe:
            chunks.append(ch)
        else:
            chunks.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(chunks)


def prepare_tag_prefix(prefix: str) -> str:
    if not prefix:
        raise _error("EmitterError", "tag prefix must not be empty")
    head = ""
    if prefix[0] == "!":
        head, prefix = "!", prefix[1:]
    return head + _escape(prefix, _TAG_CHARS | {"!"})


def prepare_tag(tag: str, prefixes: dict[str, str]) -> str:
    if not tag:
        raise _error("EmitterError", "tag must not be empty")
    if tag == "!":
        return tag
    handle = None
    suffix = tag
    for prefix in sorted(prefixes):
        if tag.startswith(prefix) and (prefix == "!" or len(prefix) < len(tag)):
            handle = prefixes[prefix]
            suffix = tag[len(prefix) :]
    safe = _TAG_CHARS if handle == "!" else _TAG_CHARS | {"!"}
    text = _escape(suffix, safe)
    return f"{handle}{text}" if handle else f"!<{text}>"


def _error(name: str, message: str) -> Exception:
    from . import pyyaml

    return getattr(pyyaml, name)(message)


# ---- the writer --------------------------------------------------------------------------------

_ESCAPES = {
    "\0": "0", "\x07": "a", "\x08": "b", "\x09": "t", "\x0a": "n", "\x0b": "v", "\x0c": "f",
    "\x0d": "r", "\x1b": "e", '"': '"', "\\": "\\", "\x85": "N", "\xa0": "_", "\u2028": "L",
    "\u2029": "P",
}  # fmt: skip
_DQ_PLAIN = re.compile(r"[ !#-\[\]-~]*\Z").match
_DQ_PLAIN_UNICODE = re.compile(
    "[ !#-\\[\\]-~\xa0-\u2027\u202a-\ud7ff\ue000-\ufefe\uff00-\ufffd]*\\Z"
).match
_TOKENS = re.compile(r" +|[^ ]+").findall

# Set by the dumper module: the emitter's own increase_indent, to tell when it was replaced.
DEFAULT_INCREASE: Any = None

# Block constants for the frames of the walk.
_BSEQ, _BMAP, _FSEQ, _FMAP = 0, 1, 2, 3

TAG_STR = _YAML + "str"
TAG_SEQ = _YAML + "seq"
TAG_MAP = _YAML + "map"


class Writer:
    """The output text and where its cursor is, with PyYAML's rules for moving it."""

    def __init__(
        self,
        canonical: Any,
        indent: Any,
        width: Any,
        allow_unicode: Any,
        line_break: Any,
    ) -> None:
        self.out: list[str] = []
        self.column = 0
        self.whitespace = True
        self.indention = True
        self.open_ended = False
        self.indent: int | None = None
        self.indents: list[int | None] = []
        self.flow_level = 0
        self.canonical = bool(canonical)
        self.allow_unicode = bool(allow_unicode)
        self.best_indent = 2
        if indent and 1 < indent < 10:
            self.best_indent = indent
        self.best_width = 80
        if width and width > self.best_indent * 2:
            self.best_width = width
        self.best_line_break = "\n"
        if line_break in ("\r", "\n", "\r\n"):
            self.best_line_break = line_break
        self.tag_prefixes = dict(DEFAULT_TAG_PREFIXES)
        self.root_context = False
        self.mapping_context = False
        self.simple_key_context = False
        self._analysis: dict[str, tuple] = {}
        self._tags: dict[str, str] = {}

    # -- cursor moves --

    def write_indicator(
        self,
        indicator: str,
        need_whitespace: bool,
        whitespace: bool = False,
        indention: bool = False,
    ) -> None:
        data = indicator if (self.whitespace or not need_whitespace) else " " + indicator
        self.whitespace = whitespace
        self.indention = self.indention and indention
        self.column += len(data)
        self.open_ended = False
        self.out.append(data)

    def write_indent(self) -> None:
        indent = self.indent or 0
        column = self.column
        if not self.indention or column > indent or (column == indent and not self.whitespace):
            self.write_line_break()
            column = 0
        if column < indent:
            self.whitespace = True
            self.out.append(" " * (indent - column))
            self.column = indent

    def write_line_break(self, data: str | None = None) -> None:
        self.whitespace = True
        self.indention = True
        self.column = 0
        self.out.append(self.best_line_break if data is None else data)

    # -- analysis and tags, cached for the stream --

    def analysis(self, text: str) -> tuple:
        got = self._analysis.get(text)
        if got is None:
            got = self._analysis[text] = analyze_scalar(text, self.allow_unicode)
        return got

    def tag_text(self, tag: str) -> str:
        got = self._tags.get(tag)
        if got is None:
            got = self._tags[tag] = prepare_tag(tag, self.tag_prefixes)
        return got

    def reset_tags(self) -> None:
        self.tag_prefixes = dict(DEFAULT_TAG_PREFIXES)
        self._tags = {}

    # -- scalars --

    def choose_style(self, style: Any, implicit0: bool, analysis: tuple, text: str) -> str:
        empty, multiline, flow_plain, block_plain, single, block = analysis
        if style == '"' or self.canonical:
            return '"'
        simple_key = self.simple_key_context
        if not style and implicit0:  # noqa: SIM102
            if not (simple_key and (empty or multiline)) and (
                (self.flow_level and flow_plain) or (not self.flow_level and block_plain)
            ):
                return ""
        if style and style in "|>" and not self.flow_level and not simple_key and block:  # noqa: SIM102
            # A block scalar at the root with an indentation indicator is read by a YAML 1.2
            # reader one column further left than PyYAML reads it; quotes are unambiguous.
            if not (self.root_context and text[:1] in (" ", "\n", "\x85", "\u2028", "\u2029")):
                return style
        if (not style or style == "'") and single and not (simple_key and multiline):
            return "'"
        return '"'

    def write_scalar(self, text: str, style: str, split: bool) -> None:
        if style == '"':
            self.write_double_quoted(text, split)
        elif style == "'":
            self.write_single_quoted(text, split)
        elif style == ">":
            self.write_folded(text)
        elif style == "|":
            self.write_literal(text)
        else:
            self.write_plain(text, split)

    def write_plain(self, text: str, split: bool = True) -> None:
        if self.root_context:
            self.open_ended = True
        if not text:
            return
        out = self.out
        if not self.whitespace:
            out.append(" ")
            self.column += 1
        self.whitespace = False
        self.indention = False
        if " " not in text or not split or self.column + len(text) <= self.best_width:
            out.append(text)
            self.column += len(text)
            return
        for token in _TOKENS(text):
            if token[0] == " " and len(token) == 1 and self.column > self.best_width:
                self.write_indent()
                self.whitespace = False
                self.indention = False
            else:
                out.append(token)
                self.column += len(token)

    def write_single_quoted(self, text: str, split: bool = True) -> None:
        self.write_indicator("'", True)
        out = self.out
        if (
            not any(b in text for b in _BREAKS)
            and (" " not in text or not split or self.column + len(text) + text.count("'") <= self.best_width)  # noqa: E501
        ):  # fmt: skip
            data = text.replace("'", "''") if "'" in text else text
            out.append(data)
            self.column += len(data)
            self.write_indicator("'", False)
            return
        spaces = breaks = False
        start = end = 0
        length = len(text)
        while end <= length:
            ch = text[end] if end < length else None
            if spaces:
                if ch is None or ch != " ":
                    if (
                        start + 1 == end
                        and self.column > self.best_width
                        and split
                        and start != 0
                        and end != length
                    ):
                        self.write_indent()
                    else:
                        data = text[start:end]
                        self.column += len(data)
                        out.append(data)
                    start = end
            elif breaks:
                if ch is None or ch not in _BREAKS:
                    if text[start] == "\n":
                        self.write_line_break()
                    for br in text[start:end]:
                        self.write_line_break(None if br == "\n" else br)
                    self.write_indent()
                    start = end
            elif ch is None or ch in " \n\x85\u2028\u2029" or ch == "'":  # noqa: SIM102
                if start < end:
                    data = text[start:end]
                    self.column += len(data)
                    out.append(data)
                    start = end
            if ch == "'":
                self.column += 2
                out.append("''")
                start = end + 1
            if ch is not None:
                spaces = ch == " "
                breaks = ch in _BREAKS
            end += 1
        self.write_indicator("'", False)

    def write_double_quoted(self, text: str, split: bool = True) -> None:
        self.write_indicator('"', True)
        out = self.out
        plain = _DQ_PLAIN_UNICODE if self.allow_unicode else _DQ_PLAIN
        if plain(text) and (
            " " not in text or not split or self.column + len(text) <= self.best_width
        ):
            out.append(text)
            self.column += len(text)
            self.write_indicator('"', False)
            return
        allow_unicode = self.allow_unicode
        start = end = 0
        length = len(text)
        # True right after a fold that began the new line with a backslash to keep a space.
        marker = False
        while end <= length:
            ch = text[end] if end < length else None
            if (
                ch is None
                or ch in '"\\\x85\u2028\u2029\ufeff'
                or not (
                    " " <= ch <= "~"
                    or (allow_unicode and ("\xa0" <= ch <= "\ud7ff" or "\ue000" <= ch <= "\ufffd"))
                )
            ):
                if start < end:
                    data = text[start:end]
                    self.column += len(data)
                    out.append(data)
                    start = end
                    marker = False
                if ch is not None:
                    marker = False
                    if ch in _ESCAPES:
                        data = "\\" + _ESCAPES[ch]
                    elif ch <= "\xff":
                        data = f"\\x{ord(ch):02X}"
                    elif ch <= "\uffff":
                        data = f"\\u{ord(ch):04X}"
                    else:
                        data = f"\\U{ord(ch):08X}"
                    self.column += len(data)
                    out.append(data)
                    start = end + 1
            if (
                0 < end < length - 1
                and (ch == " " or start >= end)
                and self.column + (end - start) > self.best_width
                and split
                # A second fold with nothing written since the first would put two backslashes
                # at the start of the line, which read back as one literal backslash.
                and not (marker and start >= end)
            ):
                data = text[start:end] + "\\"
                if start < end:
                    start = end
                self.column += len(data)
                out.append(data)
                self.write_indent()
                self.whitespace = False
                self.indention = False
                marker = text[start] == " "
                if marker:
                    self.column += 1
                    out.append("\\")
            end += 1
        self.write_indicator('"', False)

    def _block_hints(self, text: str) -> str:
        hints = ""
        if text:
            if text[0] in " \n\x85\u2028\u2029":
                hints += str(self.best_indent)
            if text[-1] not in _BREAKS:
                hints += "-"
            elif len(text) == 1 or text[-2] in _BREAKS:
                hints += "+"
        return hints

    def write_folded(self, text: str) -> None:
        hints = self._block_hints(text)
        self.write_indicator(">" + hints, True)
        if hints[-1:] == "+":
            self.open_ended = True
        self.write_line_break()
        out = self.out
        leading_space = True
        spaces = False
        breaks = True
        start = end = 0
        length = len(text)
        while end <= length:
            ch = text[end] if end < length else None
            if breaks:
                if ch is None or ch not in _BREAKS:
                    if not leading_space and ch is not None and ch != " " and text[start] == "\n":
                        self.write_line_break()
                    leading_space = ch == " "
                    for br in text[start:end]:
                        self.write_line_break(None if br == "\n" else br)
                    if ch is not None:
                        self.write_indent()
                    start = end
            elif spaces:
                if ch != " ":
                    if start + 1 == end and self.column > self.best_width:
                        self.write_indent()
                    else:
                        data = text[start:end]
                        self.column += len(data)
                        out.append(data)
                    start = end
            elif ch is None or ch in " \n\x85\u2028\u2029":
                data = text[start:end]
                self.column += len(data)
                out.append(data)
                if ch is None:
                    self.write_line_break()
                start = end
            if ch is not None:
                breaks = ch in _BREAKS
                spaces = ch == " "
            end += 1

    def write_literal(self, text: str) -> None:
        hints = self._block_hints(text)
        self.write_indicator("|" + hints, True)
        if hints[-1:] == "+":
            self.open_ended = True
        self.write_line_break()
        out = self.out
        breaks = True
        start = end = 0
        length = len(text)
        while end <= length:
            ch = text[end] if end < length else None
            if breaks:
                if ch is None or ch not in _BREAKS:
                    for br in text[start:end]:
                        self.write_line_break(None if br == "\n" else br)
                    if ch is not None:
                        self.write_indent()
                    start = end
            elif ch is None or ch in _BREAKS:
                out.append(text[start:end])
                if ch is None:
                    self.write_line_break()
                start = end
            if ch is not None:
                breaks = ch in _BREAKS
            end += 1


# ---- the walk ----------------------------------------------------------------------------------


class Walk:
    """One document: the anchors chosen for its nodes and the state of the walk."""

    def __init__(self, writer: Writer, dumper: Any, anchors: dict) -> None:
        self.w = writer
        self.dumper = dumper
        self.anchors = anchors
        self.serialized: set = set()
        # Standard resolver tables need no call per scalar: the answer is a function of the text.
        cls = type(dumper)
        self.table_only = (
            getattr(cls, "resolve", None) is _base_resolve() and not dumper.yaml_path_resolvers
        )
        self.paths = bool(dumper.yaml_path_resolvers)
        # A dumper that replaced increase_indent (the usual way to indent lists under a key)
        # decides the indentation; the others take the inline arithmetic.
        self.custom_indent = getattr(type(dumper), "increase_indent", None) is not DEFAULT_INCREASE
        self.resolved: dict[str, str] = {}
        self.scalar_tag = dumper.DEFAULT_SCALAR_TAG
        self.seq_tag = dumper.DEFAULT_SEQUENCE_TAG
        self.map_tag = dumper.DEFAULT_MAPPING_TAG

    # -- tags --

    def implicit_scalar(self, tag: str, value: str) -> tuple[bool, bool]:
        if self.table_only:
            got = self.resolved.get(value)
            if got is None:
                got = self.resolved[value] = self._resolve_text(value)
            return tag == got, tag == self.scalar_tag
        resolve = self.dumper.resolve
        return (
            tag == resolve(ScalarNode, value, (True, False)),
            tag == resolve(ScalarNode, value, (False, True)),
        )

    def _resolve_text(self, value: str) -> str:
        table = self.dumper.yaml_implicit_resolvers
        found = table.get(value[0] if value else "", [])
        wild = table.get(None, [])
        for tag, regexp in (*found, *wild):
            if regexp.match(value):
                return tag
        return self.scalar_tag

    def implicit_collection(self, node: Any, seq: bool) -> bool:
        if self.table_only:
            return node.tag == (self.seq_tag if seq else self.map_tag)
        return node.tag == self.dumper.resolve(
            SequenceNode if seq else MappingNode, node.value, True
        )

    # -- simple keys (a short single-line scalar, an alias, an empty collection) --

    def simple_key(self, node: Any) -> bool:
        w = self.w
        anchor = self.anchors.get(node)
        length = 0
        if anchor is not None:
            length = len(_prepare_anchor(anchor))
        if node in self.serialized:
            return length < 128
        tag = node.tag
        if tag is not None:
            length += len(w.tag_text(tag))
        if isinstance(node, ScalarNode):
            text = node.value
            length += len(text)
            if length >= 128:
                return False
            a = w.analysis(text)
            return not a[0] and not a[1]
        return length < 128 and not node.value

    # -- one node --

    def begin(self, node: Any, stack: list, ctx: int, parent: Any, index: Any) -> None:
        """Write the start of ``node`` (a scalar or an alias completely) and push a frame when
        it is a non-empty block or flow collection. ``ctx`` is 1 root, 2 sequence item, 4 mapping
        value, 12 mapping key that is simple, 4 for any other mapping part."""
        w = self.w
        w.root_context = ctx == 1
        w.mapping_context = ctx & 4 != 0
        w.simple_key_context = ctx == 12
        anchor = self.anchors.get(node)
        if node in self.serialized:
            if anchor is None:
                raise _error("EmitterError", "anchor is not specified for alias")
            w.write_indicator("*" + _prepare_anchor(anchor), True)
            return
        self.serialized.add(node)
        if self.paths:
            self.dumper.descend_resolver(parent, index)
        if anchor is not None:
            w.write_indicator("&" + _prepare_anchor(anchor), True)
        tag = node.tag
        if isinstance(node, ScalarNode):
            self._scalar(node, tag)
            if self.paths:
                self.dumper.ascend_resolver()
            return
        implicit = self.implicit_collection(node, isinstance(node, SequenceNode))
        if (not w.canonical or tag is None) and implicit:
            pass
        else:
            if tag is None:
                raise _error("EmitterError", "tag is not specified")
            w.write_indicator(w.tag_text(tag), True)
        is_seq = isinstance(node, SequenceNode)
        items = node.value
        flow = w.flow_level or w.canonical or node.flow_style or not items
        if flow:
            w.write_indicator("[" if is_seq else "{", True, True)
            w.flow_level += 1
            prev = self._bump(True, False)
            stack.append([_FSEQ if is_seq else _FMAP, node, items, 0, prev, 0, False, None])
        elif is_seq:
            prev = self._bump(False, w.mapping_context and not w.indention)
            stack.append([_BSEQ, node, items, 0, prev, 0, False, None])
        else:
            prev = self._bump(False, False)
            stack.append([_BMAP, node, items, 0, prev, 0, False, None])

    def _scalar(self, node: Any, tag: Any) -> None:
        w = self.w
        value = node.value
        a = w.analysis(value)
        style = node.style
        implicit0, implicit1 = (
            self.implicit_scalar(tag, value) if tag is not None else (False, False)
        )
        chosen = w.choose_style(style, implicit0, a, value)
        # The tag: written unless the reader would resolve the text to this tag by itself.
        if (not w.canonical or tag is None) and (
            (chosen == "" and implicit0) or (chosen != "" and implicit1)
        ):
            pass
        else:
            if implicit0 and tag is None:
                tag = "!"
            if tag is None:
                raise _error("EmitterError", "tag is not specified")
            w.write_indicator(w.tag_text(tag), True)
        prev = self._bump(True, False)
        w.write_scalar(value, chosen, not w.simple_key_context)
        w.indent = prev

    def _bump(self, flow: bool, indentless: bool) -> int | None:
        """Move the indentation one level in (PyYAML's ``increase_indent``); the previous one is
        returned, to be put back when the node is done."""
        w = self.w
        prev = w.indent
        if self.custom_indent:
            saved = len(w.indents)
            self.dumper.increase_indent(flow, indentless)
            del w.indents[saved:]
        elif prev is None:
            w.indent = w.best_indent if flow else 0
        elif not indentless:
            w.indent = prev + w.best_indent
        return prev

    # -- the loop --

    def run(self, root: Any) -> None:
        w = self.w
        stack: list[list] = []
        begin = self.begin
        begin(root, stack, 1, None, None)
        while stack:
            f = stack[-1]
            kind = f[0]
            items = f[2]
            i = f[3]
            if kind == _BMAP:
                if f[5] == 0:
                    if i == len(items):
                        w.indent = f[4]
                        stack.pop()
                        self._ascend()
                        continue
                    key, f[7] = items[i]
                    w.write_indent()
                    simple = f[6] = self.simple_key(key)
                    f[5] = 1
                    if simple:
                        begin(key, stack, 12, f[1], None)
                    else:
                        w.write_indicator("?", True, False, True)
                        begin(key, stack, 4, f[1], None)
                else:
                    f[5] = 0
                    f[3] = i + 1
                    if f[6]:
                        w.write_indicator(":", False)
                    else:
                        w.write_indent()
                        w.write_indicator(":", True, False, True)
                    begin(f[7], stack, 4, f[1], items[i][0])
            elif kind == _BSEQ:
                if i == len(items):
                    w.indent = f[4]
                    stack.pop()
                    self._ascend()
                    continue
                f[3] = i + 1
                w.write_indent()
                w.write_indicator("-", True, False, True)
                begin(items[i], stack, 2, f[1], i)
            elif kind == _FSEQ:
                if i == len(items):
                    w.indent = f[4]
                    w.flow_level -= 1
                    if w.canonical and i:
                        w.write_indicator(",", False)
                        w.write_indent()
                    w.write_indicator("]", False)
                    stack.pop()
                    self._ascend()
                    continue
                f[3] = i + 1
                if i:
                    w.write_indicator(",", False)
                if w.canonical or w.column > w.best_width:
                    w.write_indent()
                begin(items[i], stack, 2, f[1], i)
            else:  # _FMAP
                if f[5] == 0:
                    if i == len(items):
                        w.indent = f[4]
                        w.flow_level -= 1
                        if w.canonical and i:
                            w.write_indicator(",", False)
                            w.write_indent()
                        w.write_indicator("}", False)
                        stack.pop()
                        self._ascend()
                        continue
                    key, f[7] = items[i]
                    if i:
                        w.write_indicator(",", False)
                    if w.canonical or w.column > w.best_width:
                        w.write_indent()
                    simple = f[6] = not w.canonical and self.simple_key(key)
                    f[5] = 1
                    if simple:
                        begin(key, stack, 12, f[1], None)
                    else:
                        w.write_indicator("?", True)
                        begin(key, stack, 4, f[1], None)
                else:
                    f[5] = 0
                    f[3] = i + 1
                    if f[6]:
                        w.write_indicator(":", False)
                    else:
                        if w.canonical or w.column > w.best_width:
                            w.write_indent()
                        w.write_indicator(":", True)
                    begin(f[7], stack, 4, f[1], items[i][0])

    def _ascend(self) -> None:
        if self.paths:
            self.dumper.ascend_resolver()


def _prepare_anchor(anchor: str) -> str:
    if not anchor:
        raise _error("EmitterError", "anchor must not be empty")
    if not _ANCHOR_OK(anchor):
        bad = next(ch for ch in anchor if not _ANCHOR_OK(ch))
        raise _error("EmitterError", f"invalid character {bad!r} in the anchor: {anchor!r}")
    return anchor


def _base_resolve() -> Any:
    from ._engine import BaseResolver

    return BaseResolver.resolve
