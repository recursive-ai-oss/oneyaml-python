"""A YAML 1.2.2 scanner and parser in one pass, producing events as tuples.

The parser reads the decoded text with compiled regular expressions and ``str`` methods, keeps
block structure on explicit stacks (so nesting depth never becomes recursion) and appends one
tuple per event to a list. Offsets are indices into the decoded text.

Event tuples (the first item is the kind):

``(STREAM_START,)``
``(STREAM_END, end)``
``(DOC_START, start, end, explicit, version, tags)``
    ``start:end`` is the ``---`` marker (empty when the document starts without one);
    ``version`` is ``(major, minor)`` or ``None``; ``tags`` maps the handles the document's
    ``%TAG`` directives declare to their prefixes (``None`` when there are none).
``(DOC_END, start, end, explicit)``
``(SEQ_START, props, start, anchor, tag, flow)`` and ``(MAP_START, ...)`` the same
    ``props`` is where the node's anchor or tag begins (``start`` when it has none); ``start``
    is the first character of the collection (the ``[`` or ``{``, the first ``-``, ``?`` or
    key); ``tag`` is resolved (``tag:yaml.org,2002:str``, ``!local``, ``!`` for the bare
    non-specific tag); the text as written is ``text[props:start]``.
``(SEQ_END, end)`` and ``(MAP_END, end)``
    ``end`` is the end of the last item.
``(SCALAR, props, start, end, anchor, tag, style, value)``
    ``style`` is one of ``:`` (plain), ``'``, ``"``, ``|`` and ``>``; ``start:end`` is the
    scalar as written, quotes and block header included; ``value`` is the decoded text. An
    empty node has ``start == end`` and style ``:``.
``(ALIAS, start, end, name)``

Everything between the spans (white space, comments, indicators) is trivia a lossless layer
can re-read with the same regular expressions.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable

from .._errors import YAMLError, make_error

(
    STREAM_START,
    STREAM_END,
    DOC_START,
    DOC_END,
    SEQ_START,
    SEQ_END,
    MAP_START,
    MAP_END,
    SCALAR,
    ALIAS,
) = range(10)

DEFAULT_MAX_DEPTH = 10_000

_Always = Callable[[str, int], re.Match[str]]


def _always(pattern: str) -> _Always:
    """``match`` of a pattern that matches at every position (it can match nothing)."""
    return re.compile(pattern).match  # type: ignore[return-value]


_CMT = r"(?<![^ \t\r\n\ufeff])#[^\r\n]*"  # a byte order mark may precede a comment at a line start
# White space, comments and line breaks. Group 1 is the indentation of the last line reached
# (it takes part only when a line break was crossed).
_SKIP = _always(rf"[ \t]*(?:{_CMT})?(?:(?:\r\n|\r|\n)( *)[ \t]*(?:{_CMT})?)*")
_INLINE = _always(r"[ \t]*")
_RUN_B = _always(r"[^\r\n:#]*")
_RUN_F = _always(r"[^\r\n:#,\[\]{}]*")
# From the end of a scalar line to the next content: blank lines, then the indentation.
_CONT = _always(r"[ \t]*(?:\r\n|\r|\n)(?:[ \t]*(?:\r\n|\r|\n))*( *)[ \t]*")
_ANCHOR = re.compile(r"[^ \t\r\n,\[\]{}]+").match
_TAG = re.compile(r"!(?:([0-9A-Za-z-]*)!)?([0-9A-Za-z\-#;/?:@&=+$_.~*'()%]*)").match
_URI_CHAR = _always(r"[0-9A-Za-z\-#;/?:@&=+$,_.!~*'()\[\]%]*")
# The closing quote is not followed by another: ``'a''b`` is not closed.
_SQ = re.compile(r"'[^']*(?:''[^']*)*'(?!')").match
_DQ = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"', re.S).match
# Folding starts at the beginning of a run of blanks, so a long run is scanned once.
_FOLD = re.compile(r"(?<![ \t])[ \t]*(?:\r\n|\r|\n)(?:[ \t]*(?:\r\n|\r|\n))*[ \t]*")
_HEADER = re.compile(r"[|>](?:([1-9])([+-])?|([+-])([1-9])?)?").match
_LEAD = _always(r"(?:[ ]*(?:\r\n|\r|\n))*( *)")
_DIRECTIVE = re.compile(r"%([^ \t\r\n]+)([^\r\n]*)").match
_NONPRINTABLE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ud800-\udfff\ufffe\uffff]"
)
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_HEX = "0123456789abcdefABCDEF"

_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
    "L": "\u2028", "P": "\u2029",
}  # fmt: skip
_HEX_LEN = {"x": 2, "u": 4, "U": 8}
_DQ_TOKEN = re.compile(
    r"\\(?:(?P<brk>\r\n|\r|\n)[ \t]*"
    r"|u(?P<hi>[Dd][89ABab][0-9A-Fa-f]{2})\\u(?P<lo>[Dd][C-Fc-f][0-9A-Fa-f]{2})"
    r"|(?P<c>.))"
    # A run of blanks is taken whole, so a long run is scanned once.
    r"|(?P<fold>[ \t]*(?:\r\n|\r|\n)(?:[ \t]*(?:\r\n|\r|\n))*[ \t]*)|(?P<blank>[ \t]+)",
    re.S,
)
_BODY_CACHE: dict[int, re.Pattern[str]] = {}

# Characters that cannot begin a plain scalar.
_INDICATORS = frozenset(",[]{}#&*!|>'\"%@`")
_WS = " \t\r\n"


def linecol(s: str, off: int) -> tuple[int, int]:
    """The 1-based line and column of an offset (LF, CRLF and lone CR are one break each)."""
    head = s[:off]
    if "\r" in head:
        parts = re.split(r"\r\n|\r|\n", head)
        return len(parts), len(parts[-1]) + 1
    line = head.count("\n")
    return line + 1, len(head) - (head.rfind("\n") + 1) + 1


def _fail(s: str, off: int, message: str, category: str = "syntax") -> YAMLError:
    line, col = linecol(s, off)
    err = make_error(message, category, line, col, off)
    err.offset = off  # type: ignore[attr-defined]
    return err


def _last_line_start(s: str, a: int, b: int, ls: int) -> int:
    k = max(s.rfind("\n", a, b), s.rfind("\r", a, b))
    return k + 1 if k >= 0 else ls


def _fold_repl(m: re.Match[str]) -> str:
    t = m.group()
    k = t.count("\n") + t.count("\r") - t.count("\r\n")
    return " " if k == 1 else "\n" * (k - 1)


def _unescape(s: str, raw: str, base: int) -> str:
    """The value of the inside of a double-quoted scalar (escapes and line folding)."""
    out: list[str] = []
    pos = 0
    n = len(raw)
    search = _DQ_TOKEN.search
    while True:
        m = search(raw, pos)
        if m is None:
            out.append(raw[pos:])
            return "".join(out)
        out.append(raw[pos : m.start()])
        g = m.lastgroup
        pos = m.end()
        if g == "fold":
            f = m.group("fold")
            k = f.count("\n") + f.count("\r") - f.count("\r\n")
            out.append(" " if k == 1 else "\n" * (k - 1))
        elif g == "brk":
            pass
        elif g == "blank":
            out.append(m.group())
        elif g == "lo":
            hi, lo = int(m.group("hi"), 16), int(m.group("lo"), 16)
            out.append(chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)))
        else:
            ch = m.group("c")
            simple = _ESCAPES.get(ch)
            if simple is not None:
                out.append(simple)
                continue
            cnt = _HEX_LEN.get(ch)
            if cnt is None:
                raise _fail(s, base + m.start() + 1, f"unknown escape sequence \\{ch}")
            for k in range(cnt):
                if pos + k >= n or raw[pos + k] not in _HEX:
                    raise _fail(s, base + pos + k, "an escape needs hexadecimal digits")
            code = int(raw[pos : pos + cnt], 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                raise _fail(s, base + m.start(), "escape is not a character (lone surrogate)")
            out.append(chr(code))
            pos += cnt


def _block_value(text: str, ci: int, style: str, chomp: str, final_break: bool) -> str:
    """Value of a block scalar body (``text`` is the body lines, breaks normalised to LF)."""
    lines = text.split("\n")
    if final_break:
        lines.pop()
    if ci:
        pre = " " * ci
        lines = [ln[ci:] if ln.startswith(pre) else "" for ln in lines]
    last = len(lines) - 1
    while last >= 0 and not lines[last]:
        last -= 1
    trailing = len(lines) - 1 - last
    if last < 0:
        return "\n" * trailing if chomp == "+" else ""
    content = lines[: last + 1]
    if style == "|":
        body = "\n".join(content)
    else:
        lead = 0
        while not content[lead]:
            lead += 1
        parts = ["\n" * lead, content[lead]]
        prev = content[lead]
        blanks = 0
        for ln in content[lead + 1 :]:
            if not ln:
                blanks += 1
                continue
            if prev[0] in " \t" or ln[0] in " \t":
                parts.append("\n" * (blanks + 1))
            else:
                parts.append(" " if not blanks else "\n" * blanks)
            parts.append(ln)
            prev = ln
            blanks = 0
        body = "".join(parts)
    if chomp == "-":
        return body
    if chomp == "+":
        return body + "\n" * (trailing + (1 if final_break else 0))
    return body + "\n" if (trailing or final_break) else body


def _body_re(ci: int) -> re.Pattern[str]:
    pat = _BODY_CACHE.get(ci)
    if pat is None:
        # At indent 0 (a top-level scalar) a document marker ends the body.
        guard = r"(?!(?:---|\.\.\.)(?:[ \t\r\n]|\Z))" if ci == 0 else ""
        sp = f" {{{ci}}}" if ci else ""
        pat = re.compile(rf"(?:(?:{sp}{guard}[^\r\n]*| *)(?:\r\n|\r|\n))*(?:{sp}{guard}[^\r\n]+)?")
        if len(_BODY_CACHE) > 256:
            _BODY_CACHE.clear()
        _BODY_CACHE[ci] = pat
    return pat


def _block_scalar(s: str, j: int, n: int, pi: int) -> tuple[int, str]:
    """Scan a literal or folded scalar whose indicator is at ``j``; parent indent is ``pi``.

    Returns the end of the scalar (just before its last line break) and its value."""
    style = s[j]
    m = _HEADER(s, j)
    assert m is not None
    if m.group(1):
        indent, chomp = int(m.group(1)), m.group(2) or ""
    else:
        indent, chomp = (int(m.group(4)) if m.group(4) else 0), m.group(3) or ""
    h = m.end()
    if h < n and s[h] == "0":
        raise _fail(s, h, "a block scalar indentation indicator cannot be zero")
    k = _INLINE(s, h).end()
    if k < n and s[k] == "#":
        if k == h:
            raise _fail(s, k, "a comment needs a space before it")
        k = _line_end(s, k)
    if k < n and s[k] not in "\r\n":
        raise _fail(s, k, "unexpected text after a block scalar header")
    hdr_end = k
    if k >= n:
        return hdr_end, ""
    n0 = n
    if not s.endswith(("\n", "\r")):
        # The last line of the input ends the scalar like a line break would.
        s += "\n"
        n += 1
    b = k + 2 if s.startswith("\r\n", k) else k + 1
    lead = _LEAD(s, b)
    first = len(lead.group(1))
    lead_end = lead.start(1)
    seg = s[b:lead_end]
    longest = max((len(x) for x in re.split(r"\r\n|\r|\n", seg)), default=0) if seg else 0
    if indent:
        ci = pi + indent
    elif lead.end() >= n:
        ci = longest + 1
    elif first <= pi:
        if s[lead_end + first] == "\t":
            raise _fail(s, lead_end + first, "a tab cannot indent a block scalar")
        # No content line: blank lines of any width are empty lines, not content.
        ci = max(pi + 1, longest + 1)
    else:
        if longest > first:
            raise _fail(s, lead_end + first, "a leading blank line has too many spaces")
        ci = first
    body = _body_re(ci).match(s, b)
    assert body is not None
    e = body.end()
    if e == b:
        return hdr_end, ""
    region = s[b:e]
    if "\r" in region:
        region = region.replace("\r\n", "\n").replace("\r", "\n")
    value = _block_value(region, ci, style, chomp, True)
    e -= 2 if s.startswith("\r\n", e - 2) else 1
    return min(e, n0), value


def _line_end(s: str, k: int) -> int:
    """The index of the next line break at or after ``k`` (or the end of the text)."""
    a = s.find("\n", k)
    b = s.find("\r", k)
    if a < 0:
        a = len(s)
    if b < 0:
        b = len(s)
    return min(a, b)


_BOM_LINE = re.compile(r"(?:---|\.\.\.)(?=[ \t\r\n]|$)|[ \t]*(?:#[^\r\n]*)?(?=[\r\n]|$)").match
_BOM_BEFORE_MARKER = re.compile(
    r"\ufeff[ \t]*(?:#[^\r\n]*)?(?:\r\n|\r|\n)"
    r"(?:\ufeff?[ \t]*(?:#[^\r\n]*)?(?:\r\n|\r|\n))*"
    r"\ufeff?(?:---|\.\.\.)(?=[ \t\r\n]|\Z)"
).match
_END_LINE = re.compile(r"\.\.\.(?:[ \t]+#[^\r\n]*|[ \t]*)$").match
_PROPS_SEQ = re.compile(
    r"[&!][^ \t\r\n]*(?:[ \t]+[&!][^ \t\r\n]*)*[ \t]*(?:#[^\r\n]*)?"
    r"(?:\r\n|\r|\n)[ \t]*-(?:[ \t\r\n]|$)"
).match


def _merge(s: str, oa, ot, anchor, tag, pos: int):
    """Properties given on two lines, joined; a property given twice is an error."""
    if oa is not None:
        if anchor is not None:
            raise _fail(s, pos, "a node has one anchor")
        anchor = oa
    if ot is not None:
        if tag is not None:
            raise _fail(s, pos, "a node has one tag")
        tag = ot
    return anchor, tag


def _marker_at(s: str, j: int) -> bool:
    return s.startswith(("---", "..."), j) and (j + 3 >= len(s) or s[j + 3] in _WS)


def _quoted(s: str, j: int, n: int, fmin: int) -> tuple[int, str, bool]:
    """Scan a quoted scalar at ``j``; returns its end, value and whether it spans lines."""
    q = s[j]
    m = (_SQ if q == "'" else _DQ)(s, j)
    if m is None:
        raise _fail(s, n, "unterminated quoted scalar")
    e = m.end()
    raw = s[j + 1 : e - 1]
    multi = "\n" in raw or "\r" in raw
    if multi:
        # The closing quote is part of its line: ``---"`` is no marker, and a quote at the
        # margin is a line with content.
        body = s[j + 1 : e]
        for mm in re.finditer(r"(?:\r\n|\r|\n)(---|\.\.\.)(?=[ \t\r\n])", body):
            raise _fail(s, j + 1 + mm.start(1), "document marker inside a quoted scalar")
        if fmin > 0:
            bad = re.search(rf"(?:\r\n|\r|\n)(?! {{{fmin}}})(?=[ \t]*[^ \t\r\n])", body)
            if bad:
                p = bad.end()
                while body[p] in " \t":
                    p += 1
                raise _fail(s, j + 1 + p, "a continuation line is not indented enough")
    if q == "'":
        if "'" in raw:
            raw = raw.replace("''", "'")
        if multi:
            raw = _FOLD.sub(_fold_repl, raw)
        return e, raw, multi
    if multi or "\\" in raw:
        return e, _unescape(s, raw, j + 1), multi
    return e, raw, multi


_PCT_ASCII = re.compile(r"%([0-7][0-9A-Fa-f])")


def _unpercent(text: str) -> str:
    """Expand escaped ASCII characters; escapes of other bytes stay as written."""
    if "%" not in text:
        return text
    return _PCT_ASCII.sub(lambda m: chr(int(m.group(1), 16)), text)


def _resolve_tag(s: str, j: int, handles: dict[str, str] | None, flow: bool) -> tuple[int, str]:
    """Scan the tag at ``j``; returns the end and the resolved tag."""
    if s.startswith("!<", j):
        a = j + 2
        e = _URI_CHAR(s, a).end()
        if e >= len(s) or s[e] != ">":
            raise _fail(s, e, "a verbatim tag ends with >")
        uri = s[a:e]
        first = uri[:1]
        if not first or not (first.isascii() and (first.isalpha() or first == "!")):
            raise _fail(s, a if first else e, "a verbatim tag is a URI or a local tag")
        if first == "!" and len(uri) == 1:
            raise _fail(s, e, "a verbatim local tag needs a name")
        bad = _BAD_PERCENT.search(uri)
        if bad:
            p = bad.start() + 1
            while p < len(uri) and uri[p] in _HEX and p < bad.start() + 3:
                p += 1
            raise _fail(s, a + p, "a percent escape needs two hexadecimal digits")
        return e + 1, _unpercent(uri)
    m = _TAG(s, j)
    assert m is not None
    handle, suffix = m.group(1), m.group(2)
    e = m.end()
    bad = _BAD_PERCENT.search(suffix)
    if bad:
        p = bad.start() + 1
        while p < len(suffix) and suffix[p] in _HEX and p < bad.start() + 3:
            p += 1
        raise _fail(s, e - len(suffix) + p, "a percent escape needs two hexadecimal digits")
    if handle is None:
        prefix = handles.get("!", "!") if handles else "!"
        return e, prefix + _unpercent(suffix)
    if handle == "":
        if not suffix:
            raise _fail(s, e, "a tag with the !! handle needs a name")
        prefix = handles.get("!!", "tag:yaml.org,2002:") if handles else "tag:yaml.org,2002:"
        return e, prefix + _unpercent(suffix)
    name = f"!{handle}!"
    if not handles or name not in handles:
        raise _fail(s, e - len(suffix), f"the tag handle {name} is not declared")
    if not suffix:
        raise _fail(s, e, "a tag with a named handle needs a name")
    return e, handles[name] + _unpercent(suffix)


def parse_events(s: str, max_depth: int = DEFAULT_MAX_DEPTH) -> list[tuple]:
    """Parse decoded text into event tuples.

    Raises ``YAMLError``; the exception's ``raw_events`` holds the tuples read before the error
    (and its ``offset`` the index of the failing character)."""
    bad = _NONPRINTABLE.search(s)
    if bad is not None and _QUOTED_ONLY(bad.group()):
        return _parse_masked(s, max_depth)
    stop = bad.start() if bad else -1
    if "\ufeff" in s:
        stop = _first_stray_bom(s, stop)
    text = s
    if stop >= 0:
        s = s[:stop]
    ev: list[tuple] = [(STREAM_START,)]
    try:
        _parse(s, ev, max_depth, stop >= 0)
    except YAMLError as exc:
        if stop >= 0 and getattr(exc, "offset", 0) >= stop:
            exc = _fail(text, stop, "character not allowed in a YAML stream")
        if ev[-1][0] == DOC_START and not ev[-1][3] and not getattr(exc, "keep_doc", False):
            # A document that has not started with a marker has no start before its first node.
            ev.pop()
        exc.raw_events = ev  # type: ignore[attr-defined]
        raise exc from None
    return ev


# Characters only a quoted scalar may hold: it takes every character from U+0020 up (section 5.1),
# where the rest of the stream takes printable ones.
_QUOTED_ONLY_CHARS = "[\x7f-\x84\x86-\x9f\ufffe\uffff]"
_QUOTED_ONLY = re.compile(_QUOTED_ONLY_CHARS).fullmatch
_QUOTED_ONLY_ALL = re.compile(_QUOTED_ONLY_CHARS)


def _parse_masked(s: str, max_depth: int) -> list[tuple]:
    """Parse a stream that holds characters only quoted scalars may hold.

    They are swapped for stand-ins the parser accepts anywhere, then checked: one outside a
    quoted scalar is an error, and in the scalars they are put back. A stand-in is a character
    the text does not contain, so nothing real is confused with one."""
    found = sorted({m.group() for m in _QUOTED_ONLY_ALL.finditer(s)})
    stand: dict[str, str] = {}
    code = 0xF0000
    for ch in found:
        while chr(code) in s:
            code += 1
        stand[ch] = chr(code)
        code += 1
    masked = s.translate({ord(c): r for c, r in stand.items()})
    back = {ord(r): c for c, r in stand.items()}
    positions = [m.start() for m in _QUOTED_ONLY_ALL.finditer(s)]
    try:
        events = parse_events(masked, max_depth)
        err = None
    except YAMLError as exc:
        err = exc
        events = exc.__dict__.pop("raw_events", [])
    # The first masked character outside every quoted scalar read so far.
    spans = sorted((e[2], e[3]) for e in events if e[0] == SCALAR and e[6] in "'\"")
    starts = [a for a, _ in spans]
    limit = getattr(err, "offset", len(s)) if err is not None else len(s)
    for p in positions:
        if p >= limit:
            break
        k = bisect_right(starts, p) - 1
        if k < 0 or p >= spans[k][1]:
            raise _fail(s, p, "character not allowed in a YAML stream")
    if err is not None:
        err.raw_events = [_unmask(e, back) for e in events]  # type: ignore[attr-defined]
        raise err
    return [_unmask(e, back) for e in events]


def _unmask(e: tuple, back: dict[int, str]) -> tuple:
    if e[0] == SCALAR and e[6] in "'\"":
        return (*e[:7], e[7].translate(back))
    return e


def _first_stray_bom(s: str, stop: int) -> int:
    """The first byte order mark that is not at the start of a document, or ``stop``."""
    pos = s.find("\ufeff")
    while pos >= 0 and (stop < 0 or pos < stop):
        ok = False
        if pos == 0 or s[pos - 1] in "\r\n":
            # A mark may begin a document marker line or a comment line; after a document
            # end marker it may begin anything.
            ok = bool(_BOM_LINE(s, pos + 1))
            if not ok and pos > 0:
                p = pos - 1
                if s[p] == "\n" and p > 0 and s[p - 1] == "\r":
                    p -= 1
                q = max(s.rfind("\n", 0, p), s.rfind("\r", 0, p)) + 1
                ok = bool(_END_LINE(s, q, p))
        if not ok:
            return pos
        pos = s.find("\ufeff", pos + 1)
    return stop


def _parse(s: str, ev: list[tuple], max_depth: int, truncated: bool) -> None:  # noqa: C901
    n = len(s)
    app = ev.append
    skip = _SKIP
    i = 0
    ls = 0
    last_end = 0
    handles: dict[str, str] | None = None
    anchors: set[str] = set()

    def fail(off: int, message: str, category: str = "syntax") -> YAMLError:
        return _fail(s, off, message, category)

    def eof() -> YAMLError:
        return _fail(s, n, "unexpected end of the input")

    # -- flow collections ---------------------------------------------------------------
    def plain_line(k: int) -> int:
        """The end of the run of plain scalar text (flow context) starting at ``k``."""
        e = _RUN_F(s, k).end()
        while e < n:
            ch = s[e]
            if ch == ":":
                if e + 1 >= n or s[e + 1] in " \t\r\n,[]{}":
                    break
                e = _RUN_F(s, e + 1).end()
            elif ch == "#":
                if s[e - 1] in " \t":
                    break
                e = _RUN_F(s, e + 1).end()
            else:
                break
        return e

    def plain_flow(j: int, fmin: int, no_colon: bool) -> tuple[int, str]:
        """A plain scalar inside a flow collection starting at ``j``; returns end and value."""
        e = plain_line(j)
        end = e
        while s[end - 1] in " \t":
            end -= 1
        multi = False
        while e < n and s[e] in "\r\n":
            m2 = _CONT(s, end)
            k = m2.end()
            if k >= n:
                break
            c3 = s[k]
            if (
                len(m2.group(1)) < fmin
                or c3 in "#,[]{}"
                or (c3 == ":" and (k + 1 >= n or s[k + 1] in " \t\r\n,[]{}"))
                or (k == m2.start(1) and _marker_at(s, k))
            ):
                break
            e = plain_line(k)
            if no_colon and e < n and s[e] == ":" and (e + 1 >= n or s[e + 1] in " \t\r\n,[]{}"):
                raise fail(e + 1, "a plain scalar cannot contain : followed by a space")
            end = e
            while s[end - 1] in " \t":
                end -= 1
            multi = True
        val = s[j:end]
        if multi:
            val = _FOLD.sub(_fold_repl, val)
        return end, val

    def flow(j: int, pstart: int, anchor, tag, fmin: int, base_depth: int) -> int:  # noqa: C901
        """Parse the flow collection at ``j``; returns the index after its closing bracket.

        Frame kinds: 0 sequence, 1 mapping, 2 single pair inside a sequence. States: 0 an
        entry is expected, 1 a key (or sequence item) is complete, 2 the ``:`` was read,
        3 the value is complete, 4 the ``?`` was read."""
        nonlocal last_end
        kinds: list[int] = []
        sts: list[int] = []
        items: list = []
        prev_json = False
        start = j
        c = s[j]
        app((SEQ_START if c == "[" else MAP_START, pstart, j, anchor, tag, True))
        if anchor is not None:
            anchors.add(anchor)
        kinds.append(0 if c == "[" else 1)
        sts.append(0)
        items.append(None)
        if base_depth + 1 > max_depth:
            raise fail(j, "nesting is too deep", "limit")
        j += 1
        while True:
            m = skip(s, j)
            j = m.end()
            if m.lastindex:
                if j >= n:
                    raise eof()
                col = len(m.group(1))
                if col < fmin:
                    raise fail(j, "a flow continuation line is not indented enough")
                if col == 0 and _marker_at(s, j):
                    raise fail(j, "document marker inside a flow collection")
            if j >= n:
                raise eof()
            c = s[j]
            k = kinds[-1]
            st = sts[-1]
            if c == "," or c == "]" or c == "}":
                if k == 2:
                    if st == 4:
                        app((SCALAR, j, j, j, None, None, ":", ""))
                    if st != 3:
                        app((SCALAR, j, j, j, None, None, ":", ""))
                    app((MAP_END, j))
                    kinds.pop()
                    sts.pop()
                    items.pop()
                    k = 0
                    st = 1
                if c == ",":
                    if st == 0:
                        raise fail(j, "empty entry in a flow collection")
                    if k == 1:
                        if st == 4:
                            app((SCALAR, j, j, j, None, None, ":", ""))
                        if st != 3:
                            app((SCALAR, j, j, j, None, None, ":", ""))
                    sts[-1] = 0
                    j += 1
                    continue
                if (c == "]") != (k == 0):
                    raise fail(j, "mismatched flow collection brackets")
                if k == 1:
                    if st == 4:
                        app((SCALAR, j, j, j, None, None, ":", ""))
                    if st == 4 or st == 1 or st == 2:
                        app((SCALAR, j, j, j, None, None, ":", ""))
                if len(kinds) == 1:
                    kk = _INLINE(s, j + 1).end()
                    if (
                        kk < n
                        and s[kk] == ":"
                        and (kk + 1 >= n or s[kk + 1] in _WS)
                        and (s.find("\n", start, j) >= 0 or s.find("\r", start, j) >= 0)
                    ):
                        raise fail(kk, "an implicit key must be on one line")
                app((SEQ_END if k == 0 else MAP_END, j))
                kinds.pop()
                sts.pop()
                items.pop()
                j += 1
                last_end = j
                if not kinds:
                    return j
                prev_json = True
                sts[-1] = 1 if sts[-1] == 0 or sts[-1] == 4 else 3
                continue
            if c == ":":
                nx = s[j + 1] if j + 1 < n else " "
                json_key = prev_json and st == 1
                if nx in " \t\r\n,[]{}" or json_key:
                    if k == 0:
                        if st == 1:
                            idx, pos = items[-1]
                            if s.find("\n", pos, j) >= 0 or s.find("\r", pos, j) >= 0:
                                raise fail(j, "an implicit key must be on one line")
                            if j - pos > 1024:
                                raise fail(j + 1, "an implicit key is longer than 1024 characters")
                            ev.insert(idx, (MAP_START, pos, pos, None, None, True))
                        elif st == 0:
                            app((MAP_START, j, j, None, None, True))
                            app((SCALAR, j, j, j, None, None, ":", ""))
                        else:
                            raise fail(j, "unexpected value indicator")
                        sts[-1] = 1
                        kinds.append(2)
                        sts.append(2)
                        items.append(None)
                        if base_depth + len(kinds) > max_depth:
                            raise fail(j, "nesting is too deep", "limit")
                    else:
                        if st == 0 or st == 4:
                            app((SCALAR, j, j, j, None, None, ":", ""))
                        elif st != 1:
                            raise fail(j, "unexpected value indicator")
                        sts[-1] = 2
                    if not json_key and nx in "[{":
                        raise fail(j + 1, "a value indicator needs a space before a collection")
                    prev_json = False
                    j += 1
                    continue
            elif c == "?" and (j + 1 >= n or s[j + 1] in " \t\r\n,[]{}"):
                if k == 0 and st == 0:
                    app((MAP_START, j, j, None, None, True))
                    sts[-1] = 1
                    kinds.append(2)
                    sts.append(4)
                    items.append(None)
                    if base_depth + len(kinds) > max_depth:
                        raise fail(j, "nesting is too deep", "limit")
                elif k == 1 and st == 0:
                    sts[-1] = 4
                else:
                    raise fail(j, "unexpected explicit key indicator")
                j += 1
                prev_json = False
                continue
            # -- a node
            if k == 0:
                if st != 0:
                    raise fail(j, "expected , or ] in a flow sequence")
                items[-1] = (len(ev), j)
            elif st == 1 or st == 3:
                raise fail(j, "expected , or } in a flow mapping")
            pstart = j
            anchor = tag = None
            if c == "&" or c == "!":
                while True:
                    if c == "&":
                        if anchor is not None:
                            raise fail(j, "a node has one anchor")
                        am = _ANCHOR(s, j + 1)
                        if am is None:
                            raise fail(j + 1, "an anchor needs a name")
                        anchor = am.group()
                        j = am.end()
                    else:
                        if tag is not None:
                            raise fail(j, "a node has one tag")
                        j, tag = _resolve_tag(s, j, handles, True)
                    if j < n and s[j] not in " \t\r\n,]}":
                        raise fail(j, "a node property must be followed by a space")
                    m = skip(s, j)
                    pend = j
                    j = m.end()
                    if m.lastindex and j < n:
                        col = len(m.group(1))
                        if col < fmin:
                            raise fail(j, "a flow continuation line is not indented enough")
                        if col == 0 and _marker_at(s, j):
                            raise fail(j, "document marker inside a flow collection")
                    if j >= n:
                        raise eof()
                    c = s[j]
                    if c != "&" and c != "!":
                        break
                if c in ",]}" or (c == ":" and (j + 1 >= n or s[j + 1] in " \t\r\n,[]{}")):
                    app((SCALAR, pstart, pend, pend, anchor, tag, ":", ""))
                    if anchor is not None:
                        anchors.add(anchor)
                    prev_json = False
                    last_end = pend
                    sts[-1] = 1 if st == 0 or st == 4 else 3
                    continue
            if c == "[" or c == "{":
                if base_depth + len(kinds) + 1 > max_depth:
                    raise fail(j, "nesting is too deep", "limit")
                app((SEQ_START if c == "[" else MAP_START, pstart, j, anchor, tag, True))
                if anchor is not None:
                    anchors.add(anchor)
                kinds.append(0 if c == "[" else 1)
                sts.append(0)
                items.append(None)
                j += 1
                continue
            if c == "*":
                if anchor is not None or tag is not None:
                    raise fail(pstart, "an alias has no properties")
                am = _ANCHOR(s, j + 1)
                if am is None:
                    raise fail(j + 1, "an alias needs a name")
                name = am.group()
                if name not in anchors:
                    raise fail(j, f"the alias {name} has no anchor")
                a = j
                j = am.end()
                app((ALIAS, a, j, name))
                prev_json = False
            elif c == '"' or c == "'":
                a = j
                j, val, _ = _quoted(s, j, n, fmin)
                app((SCALAR, pstart, a, j, anchor, tag, c, val))
                prev_json = True
            else:
                if c in _INDICATORS or (c in "-?:" and (j + 1 >= n or s[j + 1] in " \t\r\n,[]{}")):
                    raise fail(j, "this character cannot start a plain scalar")
                a = j
                j, val = plain_flow(j, fmin, k == 0 or st == 2)
                app((SCALAR, pstart, a, j, anchor, tag, ":", val))
                prev_json = False
            if anchor is not None:
                anchors.add(anchor)
            last_end = j
            sts[-1] = 1 if st == 0 or st == 4 else 3

    # -- documents ----------------------------------------------------------------------
    while True:
        # Between documents: comments, blank lines, directives, the start marker.
        version = None
        tags: dict[str, str] | None = None
        saw_directive = False
        explicit = False
        while True:
            m = skip(s, i)
            j = m.end()
            if m.lastindex:
                ls = m.end(1) - len(m.group(1))
                # A tab before a marker or a directive keeps it off the margin.
                col = j - ls
            else:
                col = j - ls
            if j < n and s[j] == "\ufeff" and col == 0:
                # A byte order mark begins a document prefix; what follows is at column 0.
                i = ls = j + 1
                continue
            if j >= n:
                break
            c = s[j]
            if col == 0 and c == "%":
                dm = _DIRECTIVE(s, j)
                if dm is None:
                    raise fail(j + 1, "a directive needs a name")
                name, rest = dm.group(1), dm.group(2)
                rest = re.split(r"[ \t]#", rest, maxsplit=1)[0]
                if name == "YAML":
                    parts = rest.split()
                    if version is not None:
                        raise fail(j, "a document has one %YAML directive")
                    mv = re.fullmatch(r"(\d+)\.(\d+)", parts[0]) if len(parts) == 1 else None
                    if mv is None or rest[:1] not in " \t":
                        raise fail(j, "malformed %YAML directive")
                    version = (int(mv.group(1)), int(mv.group(2)))
                    if version[0] != 1:
                        raise fail(j, "only YAML 1.x documents are supported")
                elif name == "TAG":
                    parts = rest.split()
                    if (
                        len(parts) != 2
                        or not re.fullmatch(r"!(?:[0-9A-Za-z-]*!)?", parts[0])
                        or rest[:1] not in " \t"
                    ):
                        raise fail(j, "malformed %TAG directive")
                    tags = tags or {}
                    if parts[0] in tags:
                        raise fail(j, f"the tag handle {parts[0]} is declared twice")
                    tags[parts[0]] = parts[1]
                saw_directive = True
                i = dm.end()
                continue
            break
        if j >= n:
            if saw_directive:
                raise fail(n, "a directive needs a document")
            break
        c = s[j]
        if col == 0 and _marker_at(s, j) and s[j] == "-":
            explicit = True
            i = j + 3
            handles = tags
            app((DOC_START, j, j + 3, True, version, tags))
        elif col == 0 and _marker_at(s, j):  # "..." with no document
            if saw_directive:
                raise fail(j, "a directive needs a document start marker")
            i = j + 3
            m = skip(s, i)
            if m.end() < n and not m.lastindex:
                raise fail(m.end(), "unexpected text after a document end marker")
            continue
        else:
            if saw_directive:
                raise fail(j, "a directive needs a document start marker")
            handles = None
            app((DOC_START, j, j, False, None, None))
        anchors = set()
        last_end = j
        kinds: list[int] = []  # 0 sequence, 1 mapping
        inds: list[int] = []
        sts: list[int] = []
        need = True
        pi = -1
        inline_ok = not explicit
        same_ok = False
        mk = False
        plain_open = False
        ended = False  # the document ended at a marker
        while True:
            m = skip(s, i)
            j = m.end()
            if m.lastindex:
                col = len(m.group(1))
                ls = m.end(1) - col
                nl = True
                tab = j > m.end(1)
            else:
                col = j - ls
                nl = i == ls
                tab = nl and j > i and "\t" in s[i:j]
            if j >= n:
                if truncated:
                    raise eof()
                break
            c = s[j]
            if nl and col == 0 and not tab:
                if c == "\ufeff":
                    if _marker_at(s, j + 1):
                        j += 1
                        c = s[j]
                    elif not need and not kinds and _BOM_LINE(s, j + 1):
                        i = _line_end(s, j)  # a comment line in the prefix of the next document
                        continue
                    elif not need and _BOM_BEFORE_MARKER(s, j):
                        # The prefix of the next document may open with a mark even when the
                        # document before it was never closed with a marker of its own.
                        i = _line_end(s, j)
                        continue
                    else:
                        raise fail(j, "a byte order mark cannot appear here")
                if (c == "-" or c == ".") and _marker_at(s, j):
                    ended = True
                    break
            if need:
                # ---- a node is expected -------------------------------------------------
                if (
                    nl
                    and col <= pi
                    and not (same_ok and col == pi and c == "-" and (j + 1 >= n or s[j + 1] in _WS))
                ):
                    if tab:
                        raise fail(j, "a tab cannot indent a block collection")
                    if same_ok and (c == "&" or c == "!") and _PROPS_SEQ(s, j):
                        raise fail(j, "the properties of a sequence must be indented")
                    app((SCALAR, j, j, j, None, None, ":", ""))
                    need = False
                    mk = False
                    plain_open = False
                    continue
                pstart = gstart = j
                gcol = col
                anchor: str | None = None
                tag: str | None = None
                oa: str | None = None
                ot: str | None = None
                props_nl = False
                first_end = -1
                cnl = nl
                ctab = tab
                sep_tab = not nl and "\t" in s[i:j]
                if c == "&" or c == "!":
                    alone = False
                    while True:
                        if c == "&":
                            if anchor is not None:
                                raise fail(j, "a node has one anchor")
                            am = _ANCHOR(s, j + 1)
                            if am is None:
                                raise fail(j + 1, "an anchor needs a name")
                            anchor = am.group()
                            j = am.end()
                        else:
                            if tag is not None:
                                raise fail(j, "a node has one tag")
                            j, tag = _resolve_tag(s, j, handles, False)
                        if j < n and s[j] not in _WS:
                            raise fail(j, "a node property must be followed by a space")
                        pend = j
                        m = skip(s, j)
                        j = m.end()
                        if m.lastindex:
                            col = len(m.group(1))
                            ls = m.end(1) - col
                            ctab = j > m.end(1)
                            # The tab before the properties does not indent what follows.
                            sep_tab = False
                            props_nl = True
                            if (
                                j >= n
                                or (col == 0 and _marker_at(s, j))
                                or (
                                    col <= pi
                                    and not (
                                        same_ok
                                        and col == pi
                                        and s[j] == "-"
                                        and (j + 1 >= n or s[j + 1] in _WS)
                                    )
                                )
                            ):
                                alone = True
                                break
                            c = s[j]
                            if c == "&" or c == "!":
                                # More properties on a new line: the earlier ones belong to a
                                # block collection that starts with this node.
                                if oa is not None or ot is not None:
                                    raise fail(j, "too many property lines")
                                oa, ot = anchor, tag
                                first_end = pend
                                anchor = tag = None
                                gstart = j
                                gcol = col
                                cnl = True
                                props_nl = False
                                continue
                            break
                        col = j - ls
                        if j >= n:
                            alone = True
                            break
                        c = s[j]
                        if c != "&" and c != "!":
                            break
                    two = oa is not None or ot is not None
                    if two and (props_nl or alone):
                        anchor, tag = _merge(s, oa, ot, anchor, tag, gstart)
                        oa = ot = None
                        two = False
                    cnl = cnl or props_nl
                    if mk and props_nl and not alone:
                        raise fail(
                            first_end if first_end >= 0 else pend,
                            "the properties of a mapping key are on its line",
                        )
                    if alone:
                        if mk:
                            raise fail(pend, "a mapping key needs a colon")
                        # Properties with no node after them: an empty node carries them.
                        app((SCALAR, pstart, pend, pend, anchor, tag, ":", ""))
                        if anchor is not None:
                            anchors.add(anchor)
                        last_end = pend
                        need = False
                        mk = False
                        plain_open = False
                        i = pend
                        continue
                else:
                    two = False
                inner = anchor is not None or tag is not None
                has_props = inner or two
                ws_next = j + 1 >= n or s[j + 1] in _WS
                # ---- block structure indicators
                if c == "-" and ws_next:
                    if mk:
                        raise fail(j + 1, "a sequence entry cannot start here")
                    if two:
                        anchor, tag = _merge(s, oa, ot, anchor, tag, gstart)
                    if (not cnl and not inline_ok) or (has_props and not props_nl):
                        raise fail(j, "a block sequence cannot start here")
                    if ctab or sep_tab:
                        raise fail(j, "a tab cannot indent a block collection")
                    if len(kinds) >= max_depth:
                        raise fail(j, "nesting is too deep", "limit")
                    app((SEQ_START, pstart, j, anchor, tag, False))
                    if anchor is not None:
                        anchors.add(anchor)
                    kinds.append(0)
                    inds.append(col)
                    sts.append(0)
                    i = j + 1
                    pi = col
                    inline_ok = True
                    same_ok = False
                    continue
                if (c == "?" or c == ":") and ws_next:
                    if (not cnl and not inline_ok) or (has_props and not props_nl and c == "?"):
                        raise fail(j, "a block mapping entry cannot start here")
                    if ctab or sep_tab:
                        raise fail(j, "a tab cannot indent a block collection")
                    if len(kinds) >= max_depth:
                        raise fail(j, "nesting is too deep", "limit")
                    if c == "?":
                        if two:
                            anchor, tag = _merge(s, oa, ot, anchor, tag, gstart)
                        app((MAP_START, pstart, j, anchor, tag, False))
                        if anchor is not None:
                            anchors.add(anchor)
                        kinds.append(1)
                        inds.append(col)
                        sts.append(1)
                        i = j + 1
                        pi = col
                        inline_ok = True
                        same_ok = True
                    else:
                        if two:
                            ma, mt, ka, kt, kps, kcol = oa, ot, anchor, tag, gstart, gcol
                        elif props_nl:
                            ma, mt, ka, kt, kps, kcol = anchor, tag, None, None, j, col
                        else:
                            ma, mt, ka, kt, kps, kcol = None, None, anchor, tag, pstart, gcol
                        if mk:
                            # A key with properties but no text, in the mapping that is open.
                            if props_nl or not (kinds and kinds[-1] == 1 and inds[-1] == kcol):
                                raise fail(j, "a mapping key is expected")
                        else:
                            app((MAP_START, pstart, kps, ma, mt, False))
                            kinds.append(1)
                            inds.append(kcol)
                            sts.append(0)
                        app((SCALAR, kps, j, j, ka, kt, ":", ""))
                        for nm in (ma, ka):
                            if nm is not None:
                                anchors.add(nm)
                        i = j + 1
                        pi = kcol
                        inline_ok = False
                        same_ok = True
                    mk = False
                    continue
                # ---- a scalar, alias or flow collection, possibly an implicit key
                multi = False
                if c == "[" or c == "{":
                    if mk:
                        raise fail(j, "a mapping key is expected")
                    ev_idx = len(ev)
                    fstart = j
                    e = flow(j, gstart, anchor, tag, pi + 1, len(kinds))
                    multi = s.find("\n", j, e) >= 0 or s.find("\r", j, e) >= 0
                    if multi:
                        ls = _last_line_start(s, j, e, ls)
                    k = _INLINE(s, e).end()
                    if k < n and s[k] == ":" and (k + 1 >= n or s[k + 1] in _WS):
                        if multi:
                            raise fail(k, "an implicit key must be on one line")
                        if not cnl and not inline_ok:
                            raise fail(k + 1, "a block mapping cannot start here")
                        if k - j > 1024:
                            raise fail(k + 1, "an implicit key is longer than 1024 characters")
                        if two:
                            ma, mt, ka, kt, kps, kcol = oa, ot, anchor, tag, gstart, gcol
                        elif props_nl:
                            ma, mt, ka, kt, kps, kcol = anchor, tag, None, None, j, col
                        else:
                            ma, mt, ka, kt, kps, kcol = None, None, anchor, tag, pstart, gcol
                        if mk:
                            raise fail(j, "a mapping key is expected")
                        ev[ev_idx] = (ev[ev_idx][0], kps, j, ka, kt, True)
                        if len(kinds) >= max_depth:
                            raise fail(j, "nesting is too deep", "limit")
                        ev.insert(ev_idx, (MAP_START, pstart, kps, ma, mt, False))
                        kinds.append(1)
                        inds.append(kcol)
                        sts.append(0)
                        last_end = e
                        i = k + 1
                        pi = kcol
                        inline_ok = False
                        same_ok = True
                        continue
                    if two:
                        ma, mt = _merge(s, oa, ot, anchor, tag, gstart)
                        ev[ev_idx] = (ev[ev_idx][0], pstart, fstart, ma, mt, True)
                        if ma is not None:
                            anchors.add(ma)
                    last_end = e
                    need = False
                    plain_open = False
                    i = e
                    continue
                if c == "|" or c == ">":
                    if mk:
                        raise fail(j, "a mapping key is expected")
                    if two:
                        anchor, tag = _merge(s, oa, ot, anchor, tag, gstart)
                    a = j
                    e, text = _block_scalar(s, j, n, pi)
                    app((SCALAR, pstart, a, e, anchor, tag, c, text))
                    if anchor is not None:
                        anchors.add(anchor)
                    ls = _last_line_start(s, a, e, ls)
                    last_end = e
                    need = False
                    plain_open = False
                    i = e
                    continue
                a = j
                node: tuple | None = None
                val: str | None = None
                if c == "*":
                    if (inner and not props_nl) or two:
                        raise fail(pstart, "an alias has no properties")
                    am = _ANCHOR(s, j + 1)
                    if am is None:
                        raise fail(j + 1, "an alias needs a name")
                    name = am.group()
                    if name not in anchors:
                        raise fail(j, f"the alias {name} has no anchor")
                    e = am.end()
                    node = (ALIAS, j, e, name)
                    style = ""
                    k = _INLINE(s, e).end()
                elif c == '"' or c == "'":
                    e, val, multi = _quoted(s, j, n, pi + 1)
                    style = c
                    k = _INLINE(s, e).end()
                else:
                    if c in _INDICATORS:
                        raise fail(j, "this character cannot start a plain scalar")
                    e = _RUN_B(s, j).end()
                    while e < n:
                        ch = s[e]
                        if ch == ":":
                            if e + 1 >= n or s[e + 1] in _WS:
                                break
                            e = _RUN_B(s, e + 1).end()
                        elif ch == "#":
                            if s[e - 1] in " \t":
                                break
                            e = _RUN_B(s, e + 1).end()
                        else:
                            break
                    style = ":"
                    k = e  # a key's colon follows directly (blanks are inside the run)
                    end = e
                    while s[end - 1] in " \t":
                        end -= 1
                    e = end
                if k < n and s[k] == ":" and (k + 1 >= n or s[k + 1] in _WS):
                    # ---- an implicit key
                    if two:
                        ma, mt, ka, kt, kps, kcol = oa, ot, anchor, tag, gstart, gcol
                    elif props_nl:
                        ma, mt, ka, kt, kps, kcol = anchor, tag, None, None, a, col
                    else:
                        ma, mt, ka, kt, kps, kcol = None, None, anchor, tag, pstart, gcol
                    if node is not None and (ka is not None or kt is not None):
                        raise fail(pstart, "an alias has no properties")
                    if node is not None:
                        kev = node
                    else:
                        if val is None:
                            val = s[a:e]
                        kev = (SCALAR, kps, a, e, ka, kt, style, val)
                    if not cnl and not inline_ok:
                        if style == "'" or style == '"':
                            app(kev)
                        raise fail(k + 1, "a block mapping cannot start here")
                    if multi:
                        raise fail(k, "an implicit key must be on one line")
                    if k - a > 1024:
                        raise fail(k + 1, "an implicit key is longer than 1024 characters")
                    if ctab or sep_tab:
                        raise fail(k + 1, "a tab cannot indent a block collection")
                    if mk:
                        if two:
                            # Properties alone on an earlier line would belong to a new mapping,
                            # and this key continues the open one.
                            raise fail(first_end, "the properties of a mapping key are on its line")
                        if props_nl or not (kinds and kinds[-1] == 1 and inds[-1] == kcol):
                            raise fail(j, "unexpected indentation")
                    else:
                        if len(kinds) >= max_depth:
                            raise fail(j, "nesting is too deep", "limit")
                        app((MAP_START, pstart, kps, ma, mt, False))
                        kinds.append(1)
                        inds.append(kcol)
                        sts.append(0)
                    app(kev)
                    for nm in (ma, ka):
                        if nm is not None:
                            anchors.add(nm)
                    last_end = e
                    i = k + 1
                    pi = kcol
                    inline_ok = False
                    same_ok = True
                    mk = False
                    plain_open = False
                    continue
                # ---- not a key
                if mk:
                    raise fail(
                        _line_end(s, e) if style == ":" else e, "a mapping key needs a colon"
                    )
                if two:
                    anchor, tag = _merge(s, oa, ot, anchor, tag, gstart)
                if node is not None:
                    if anchor is not None or tag is not None:
                        raise fail(pstart, "an alias has no properties")
                    app(node)
                    last_end = e
                    i = e
                    need = False
                    plain_open = False
                    continue
                if style == ":":
                    # Continuation lines of a plain scalar.
                    plain_open = True
                    cmin = pi + 1
                    if k < n and s[k] == "#":
                        plain_open = False
                    elif k < n and s[k] in "\r\n":
                        while True:
                            m2 = _CONT(s, e)
                            nk = m2.end()
                            if nk >= n:
                                break
                            ind = len(m2.group(1))
                            c3 = s[nk]
                            if (
                                ind < cmin
                                or c3 == "#"
                                or (ind == 0 and _marker_at(s, nk))
                                or c3 == "\ufeff"
                                or (c3 == ":" and (nk + 1 >= n or s[nk + 1] in _WS))
                            ):
                                break
                            e2 = _RUN_B(s, nk).end()
                            while e2 < n:
                                ch = s[e2]
                                if ch == ":":
                                    if e2 + 1 >= n or s[e2 + 1] in _WS:
                                        err = fail(e2 + 1, "a mapping key cannot span lines")
                                        err.keep_doc = True  # type: ignore[attr-defined]
                                        raise err
                                    e2 = _RUN_B(s, e2 + 1).end()
                                elif ch == "#":
                                    if s[e2 - 1] in " \t":
                                        break
                                    e2 = _RUN_B(s, e2 + 1).end()
                                else:
                                    break
                            multi = True
                            end = e2
                            while s[end - 1] in " \t":
                                end -= 1
                            e = end
                            if e2 < n and s[e2] == "#":
                                plain_open = False
                                break
                            if e2 >= n or s[e2] not in "\r\n":
                                break
                    val = s[a:e]
                    if multi:
                        val = _FOLD.sub(_fold_repl, val)
                        ls = _last_line_start(s, a, e, ls)
                else:
                    plain_open = False
                if val is None:
                    val = s[a:e]
                app((SCALAR, pstart, a, e, anchor, tag, style, val))
                if anchor is not None:
                    anchors.add(anchor)
                if multi and style != ":":
                    ls = _last_line_start(s, a, e, ls)
                last_end = e
                need = False
                i = e
                continue
            # ---- no node expected: continue the open structure --------------------------
            if not nl:
                raise fail(j + 1 if (c == ":" and plain_open) else j, "unexpected text")
            while kinds and inds[-1] > col:
                kk = kinds.pop()
                inds.pop()
                if sts.pop() == 1 and kk == 1:
                    app((SCALAR, last_end, last_end, last_end, None, None, ":", ""))
                app((MAP_END if kk else SEQ_END, last_end))
            if not kinds or inds[-1] != col:
                raise fail(j + 1 if (c == ":" and plain_open) else j, "unexpected indentation")
            ws_next = j + 1 >= n or s[j + 1] in _WS
            if kinds[-1] == 0:
                if c == "-" and ws_next:
                    if tab:
                        raise fail(j, "a tab cannot indent a block collection")
                    i = j + 1
                    need = True
                    pi = col
                    inline_ok = True
                    same_ok = False
                    continue
                if len(kinds) > 1 and kinds[-2] == 1 and inds[-2] == col:
                    app((SEQ_END, last_end))
                    kinds.pop()
                    inds.pop()
                    sts.pop()
                else:
                    raise fail(j, "unexpected text in a block sequence")
            # the top frame is a mapping at this column
            if tab:
                raise fail(j, "a tab cannot indent a block collection")
            if c == "-" and ws_next:
                raise fail(j + 1, "a sequence entry cannot follow a mapping entry")
            st = sts[-1]
            if c == "?" and ws_next:
                if st == 1:
                    app((SCALAR, last_end, last_end, last_end, None, None, ":", ""))
                sts[-1] = 1
                i = j + 1
                need = True
                pi = col
                inline_ok = True
                same_ok = True
                continue
            if c == ":" and ws_next:
                i = j + 1
                need = True
                pi = col
                if st == 1:
                    sts[-1] = 0
                    inline_ok = True
                    same_ok = True
                else:
                    app((SCALAR, j, j, j, None, None, ":", ""))
                    inline_ok = False
                    same_ok = True
                continue
            if st == 1:
                app((SCALAR, last_end, last_end, last_end, None, None, ":", ""))
                sts[-1] = 0
            need = True
            pi = col - 1
            inline_ok = True
            same_ok = False
            mk = True
        # ---- end of the document ------------------------------------------------------------
        end_pos = last_end
        if need:
            app((SCALAR, end_pos, end_pos, end_pos, None, None, ":", ""))
        while kinds:
            kk = kinds.pop()
            inds.pop()
            if sts.pop() == 1 and kk == 1:
                app((SCALAR, end_pos, end_pos, end_pos, None, None, ":", ""))
            app((MAP_END if kk else SEQ_END, end_pos))
        if ended and s[j] == ".":
            app((DOC_END, j, j + 3, True))
            i = j + 3
            m = skip(s, i)
            if m.end() < n and not m.lastindex:
                raise fail(m.end(), "unexpected text after a document end marker")
            continue
        app((DOC_END, end_pos, end_pos, False))
        if ended:
            i = j
            ls = j  # the next document starts with a marker at the start of a line
            continue
        break
    if truncated:
        raise eof()
    app((STREAM_END, n))
