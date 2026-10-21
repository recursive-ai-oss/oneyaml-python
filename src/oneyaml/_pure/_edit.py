"""Edits to a lossless document: set a value, insert a member, remove a member.

An edit changes the text of one place and nothing else, then repairs the row table around it:
the rows of the edited node are replaced, the offsets behind it shift and the ancestors' spans are
fixed. The new text copies the document: its line break, indentation, quote style and the way its
sibling collections are laid out.

Every function checks everything it needs before it changes anything, so a refused edit leaves
the document as it was.
"""

from __future__ import annotations

import re
from typing import Any

from .._errors import PatchError
from .._types import Tagged
from . import _tree as T
from . import _write as W
from ._parser import parse_events
from ._tree import K_ALIAS, K_MAP, K_SCALAR, K_SEQ, Doc

_BRK = re.compile(r"\r\n|\r|\n")
_YAML = "tag:yaml.org,2002:"
_SKIP_B = T._SKIP_B
_COMMENT = re.compile(r"([ \t]*)(#[^\r\n]*)").match
_HEADER = re.compile(r"[|>](?:([1-9])([+-]?)|([+-]?)([1-9])?)").match
_CORE_SCALAR_TAGS = {
    _YAML + t: t for t in ("str", "int", "float", "bool", "null", "binary", "map", "seq")
}


def refuse(category: str, message: str) -> PatchError:
    return PatchError(message, category=category)


# ---- text helpers ---------------------------------------------------------------------------


def line_start(text: str, pos: int) -> int:
    return max(text.rfind("\n", 0, pos), text.rfind("\r", 0, pos)) + 1


def line_end(text: str, pos: int) -> int:
    m = _BRK.search(text, pos)
    return m.start() if m else len(text)


def next_line(text: str, pos: int) -> int:
    m = _BRK.search(text, pos)
    return m.end() if m else len(text)


def prev_line(text: str, ls: int) -> int:
    j = ls - 1
    if j > 0 and text[j] == "\n" and text[j - 1] == "\r":
        j -= 1
    return line_start(text, j)


def col(text: str, pos: int) -> int:
    ls = line_start(text, pos)
    c = pos - ls
    if c and text[ls] == "\ufeff":
        c -= 1
    return c


def only_blanks_before(text: str, pos: int) -> bool:
    ls = line_start(text, pos)
    return text[ls:pos].strip(" \t\ufeff") == ""


def following_breaks(text: str, pos: int) -> int:
    """How many line breaks follow ``pos`` back to back: the end of its line and the blank lines
    after it. A keep scalar's value counts them, so new text that ends with one needs to know."""
    k = line_end(text, pos)
    if text[pos:k].strip(" \t") != "" or k >= len(text):
        return 0
    j = next_line(text, k)
    n = 1
    while j < len(text):
        k = line_end(text, j)
        if k < len(text) and text[j:k].strip(" \t") == "":
            n += 1
            j = next_line(text, k)
        else:
            break
    return n


def comment_after(text: str, pos: int) -> int | None:
    """The indentation of the comment line that comes next after ``pos`` (past the rest of its
    line and blank lines), or None when something else comes."""
    j = next_line(text, pos) if text[pos : line_end(text, pos)].strip(" \t") == "" else pos
    while j < len(text):
        k = line_end(text, j)
        line = text[j:k]
        s = line.lstrip(" \t")
        if s:
            return len(line) - len(s) if s[0] == "#" else None
        j = next_line(text, k)
    return None


def doc_break(doc: Doc, near: int = 0) -> str:
    m = _BRK.search(doc.text, near) or _BRK.search(doc.text)
    return m.group() if m else "\n"


# ---- locating -------------------------------------------------------------------------------


class Loc:
    """A node found by a path: its row, the key row of its entry, its parent and ancestors."""

    __slots__ = ("alias", "anc", "index", "key", "parent", "prev_end", "row", "token")

    def __init__(self) -> None:
        self.row = 0
        self.key = -1
        self.parent = -1
        self.anc: list[int] = []
        self.alias = False  # the path went through an alias on the way
        self.index = -1
        self.prev_end = -1
        self.token: str | None = None


def parse_index(tok: str) -> int:
    if tok.isascii() and tok.isdigit() and (tok == "0" or tok[0] != "0"):
        return int(tok)
    return -1


def locate(doc: Doc, tokens: list[str]) -> Loc:
    """Find the node at ``tokens``; raises ``patch-path`` or ``refused-ambiguous``."""
    loc = Loc()
    row = 0
    kind = doc.kind
    for tok in tokens:
        r = row
        if kind[r] == K_ALIAS:
            r = T.alias_map(doc)[r]
            loc.alias = True
        k = kind[r]
        if k == K_MAP:
            v, kr = T.find_member(doc, r, tok)
            if v == -1:
                raise refuse("patch-path", f"no key {tok!r}")
            if v == -2:
                raise refuse("refused-ambiguous", f"more than one key reads {tok!r}")
            loc.anc.append(r)
            loc.parent = r
            loc.key = kr
            loc.index = -1
            row = v
        elif k == K_SEQ:
            i = tok if type(tok) is int else parse_index(tok) if type(tok) is str else -1
            if i < 0:
                raise refuse("patch-path", f"{tok!r} is not an index of a sequence")
            c, pe = item_at(doc, r, i)
            if c < 0:
                raise refuse("patch-path", f"index {i} is out of range")
            loc.anc.append(r)
            loc.parent = r
            loc.key = -1
            loc.index = i
            loc.prev_end = pe
            row = c
        else:
            raise refuse("patch-path", f"cannot go into a scalar at {tok!r}")
        loc.token = tok
    loc.row = row
    return loc


def item_at(doc: Doc, row: int, index: int) -> tuple[int, int]:
    """The row of the ``index``-th item of a sequence and the end of the item before it."""
    sz = doc.sz
    en = doc.en
    j = row + 1
    end = row + sz[row]
    pe = doc.st[row]
    while j < end:
        if index == 0:
            return j, pe
        index -= 1
        pe = en[j]
        j += sz[j]
    return -1, pe


def members(doc: Doc, row: int) -> list[tuple[int, int]]:
    """``(key row, value row)`` of every entry of a mapping."""
    sz = doc.sz
    out = []
    j = row + 1
    end = row + sz[row]
    while j < end:
        v = j + sz[j]
        out.append((j, v))
        j = v + sz[v]
    return out


def items(doc: Doc, row: int) -> list[int]:
    return T.children(doc, row)


# ---- indicators, entries --------------------------------------------------------------------


def colon_after(doc: Doc, key: int, mcol: int | None = None) -> int:
    """The position of the ``:`` of the entry whose key row is ``key``, or -1 when it has none.
    A ``:`` on a later line belongs to the entry only when it is in the mapping's column."""
    text = doc.text
    q = _SKIP_B(text, doc.en[key]).end()
    if text[q : q + 1] != ":":
        return -1
    if mcol is not None and _BRK.search(text, doc.en[key], q) and col(text, q) != mcol:
        return -1
    return q


def entry_start(doc: Doc, key: int) -> int:
    """Where an entry begins: its ``?`` for an explicit key, else its key."""
    text = doc.text
    p = doc.ps[key]
    j = p
    while j > 0 and text[j - 1] in " \t":
        j -= 1
    if j > 0 and text[j - 1] == "?" and (j - 1 == 0 or text[j - 2] in " \t\r\n[{,-"):
        return j - 1
    return p


def dash_of(doc: Doc, seq: int, index: int, prev_end: int) -> int:
    if index == 0:
        return doc.st[seq]
    text = doc.text
    q = _SKIP_B(text, prev_end).end()
    return q


def scalar_header(doc: Doc, r: int) -> tuple[str, int | None]:
    """The chomping indicator and indentation indicator of a block scalar's header."""
    m = _HEADER(doc.text, doc.st[r])
    if m is None:
        return "", None
    if m.group(1):
        return m.group(2), int(m.group(1))
    return m.group(3) or "", int(m.group(4)) if m.group(4) else None


def scalar_extent(doc: Doc, r: int) -> int:
    """The end of a scalar row, a keep block scalar's trailing blank lines included."""
    text = doc.text
    e = doc.en[r]
    if doc.sty[r] in "|>" and scalar_header(doc, r)[0] == "+":
        j = next_line(text, e)
        if j == e:
            return e
        while j < len(text):
            k = line_end(text, j)
            if text[j:k].strip(" \t") != "":
                break
            j = next_line(text, k)
        return j
    return e


def node_extent(doc: Doc, r: int) -> int:
    """The end of a node, with the blank lines of a keep scalar that ends it."""
    last = r + doc.sz[r] - 1
    if doc.kind[last] == K_SCALAR and doc.sty[last] in "|>":
        return max(doc.en[r], scalar_extent(doc, last))
    return doc.en[r]


def block_content_indent(doc: Doc, r: int) -> int | None:
    """The indentation of the first content line of a block scalar, or None without one."""
    text = doc.text
    j = next_line(text, doc.st[r])
    end = doc.en[r]
    while j < end:
        k = line_end(text, j)
        line = text[j:k]
        if line.strip(" \t") != "":
            return len(line) - len(line.lstrip(" "))
        j = next_line(text, k)
    return None


# ---- the row table --------------------------------------------------------------------------


def splice(
    doc: Doc,
    a: int,
    b: int,
    new: str,
    lo: int,
    hi: int,
    rows: Doc | None,
    anc: list[int],
    ends: tuple[int, int] | None = None,
) -> None:
    """Replace text ``a:b`` by ``new`` and rows ``lo:hi`` by ``rows`` (offsets already final).

    ``anc`` are the collections that hold the change. ``ends`` is ``(old, new)``: the end of the
    changed node before and after, for ancestors that ended with it."""
    text = doc.text
    delta = len(new) - (b - a)
    doc.text = text[:a] + new + text[b:]
    n_new = len(rows.kind) if rows is not None else 0
    n_row = len(doc.kind)
    if hi < n_row and delta:
        doc.ps[hi:] = [x + delta for x in doc.ps[hi:]]
        doc.st[hi:] = [x + delta for x in doc.st[hi:]]
        doc.en[hi:] = [x + delta for x in doc.en[hi:]]
    if rows is None:
        for lst in (doc.kind, doc.ps, doc.st, doc.en, doc.sz, doc.sty, doc.anc, doc.tag, doc.val):
            del lst[lo:hi]
    else:
        doc.kind[lo:hi] = rows.kind
        doc.ps[lo:hi] = rows.ps
        doc.st[lo:hi] = rows.st
        doc.en[lo:hi] = rows.en
        doc.sz[lo:hi] = rows.sz
        doc.sty[lo:hi] = rows.sty
        doc.anc[lo:hi] = rows.anc
        doc.tag[lo:hi] = rows.tag
        doc.val[lo:hi] = rows.val
    d_rows = n_new - (hi - lo)
    en = doc.en
    sty = doc.sty
    for r in reversed(anc):
        doc.sz[r] += d_rows
        e = en[r]
        if ends is not None and e == ends[0] and sty[r] != "f":
            en[r] = ends[1]
        elif e >= b:
            en[r] = e + delta
    doc.amap = None
    doc.exp = None


def set_start(doc: Doc, anc: list[int], old: int, new: int) -> None:
    """The first child of a collection moved: the collections that began with it move too."""
    for r in reversed(anc):
        if doc.st[r] != old:
            break
        doc.st[r] = new
        if doc.ps[r] == old:
            doc.ps[r] = new


def scalar_rows(
    ps: int, st: int, en: int, style: str, anchor: str | None, tag: str | None, val: str
) -> Doc:
    d = Doc("")
    d.kind.append(K_SCALAR)
    d.ps.append(ps)
    d.st.append(st)
    d.en.append(en)
    d.sz.append(1)
    d.sty.append(style)
    d.anc.append(anchor)
    d.tag.append(tag)
    d.val.append(val)
    return d


def snippet_rows(snippet: str, pad: int, base: int, trail: int = 1, nl: str = "\n") -> Doc:
    """The rows of a collection written as ``snippet``. ``pad`` spaces are put before it for
    parsing (it continues a line that already holds ``- ``), and ``base`` is where its first
    character lands in the document."""
    # A final line break is added for parsing: the text after the snippet supplies it in the
    # document, and a keep scalar at the end holds its blank lines only because of it.
    text = " " * pad + snippet + nl * trail
    events = parse_events(text)
    docs, _ = T.build(text, events)
    d = docs[0]
    shift = base - pad
    if shift:
        d.ps[:] = [x + shift for x in d.ps]
        d.st[:] = [x + shift for x in d.st]
        d.en[:] = [x + shift for x in d.en]
    return d


def new_block(
    doc: Doc, value: Any, indent: int, lay: W.Layout, inline_first: bool, pos: int, extra: int = 0
) -> tuple[str, int]:
    """The block text for ``value`` that is followed by the document text from ``pos`` (and by
    ``extra`` line breaks of its own), and how many breaks follow it."""
    text = doc.text
    following = extra + following_breaks(text, pos)
    block = W.block_text(value, indent, lay, inline_first, following, comment_after(text, pos))
    return block, following


def drop_first_row(d: Doc) -> Doc:
    for lst in (d.kind, d.ps, d.st, d.en, d.sz, d.sty, d.anc, d.tag, d.val):
        del lst[0]
    return d


# ---- layout of new text ---------------------------------------------------------------------


def _hint_of(doc: Doc, row: int) -> str | None:
    if doc.kind[row] != K_SCALAR:
        return None
    s = doc.sty[row]
    if s in "'\"":
        return s
    if s == ":" and doc.val[row] is not None and doc.tag[row] is None:
        return ":"
    return None


def sibling_hint(
    doc: Doc, parent: int, before_row: int | None = None
) -> tuple[str | None, str | None]:
    """The style new strings take: that of the nearest scalar before ``before_row`` among the
    values of ``parent`` (the nearest after it when none is before), and likewise for keys."""
    if parent < 0:
        return None, None
    kind = doc.kind
    sz = doc.sz
    end = parent + sz[parent]
    j = parent + 1
    is_map = kind[parent] == K_MAP
    val_before = val_after = key_before = key_after = None
    while j < end:
        k = j
        v = j + sz[j] if is_map else j
        before = before_row is None or v < before_row
        h = _hint_of(doc, v)
        if h is not None:
            if before:
                val_before = h
            elif val_after is None:
                val_after = h
        if is_map:
            kh = _hint_of(doc, k)
            if kh is not None:
                if before:
                    key_before = kh
                elif key_after is None:
                    key_after = kh
        j = v + sz[v]
        if not before and val_after is not None and (not is_map or key_after is not None):
            break
    vh = val_before or val_after
    kh = key_before or key_after
    return (None if vh == ":" else vh), (None if kh == ":" else kh)


def _step_of_map(doc: Doc, m: int, anc: list[int]) -> int | None:
    """The indentation step of block mapping ``m`` when it is the value of a key."""
    p = None
    for i, r in enumerate(anc):
        if r == m:
            p = anc[i - 1] if i else None
            break
    if p is None or doc.kind[p] != K_MAP or doc.sty[m] != "b":
        return None
    # the key of m is the row just before it when the key is a scalar
    key = m - 1
    if doc.kind[key] in (K_MAP, K_SEQ):
        return None
    t = doc.text
    d = col(t, doc.st[m]) - col(t, entry_start(doc, key))
    return d if d > 0 else None


def doc_step(doc: Doc, parent: int, anc: list[int]) -> int:
    """The document's block indentation step (the kit's rule: the enclosing mapping's own step
    first, else the nearest preceding nested mapping's, else two)."""
    chain = [*anc]
    if parent >= 0 and (not chain or chain[-1] != parent):
        chain.append(parent)
    for m in reversed(chain):
        if doc.kind[m] == K_MAP:
            s = _step_of_map(doc, m, chain)
            if s:
                return s
    # the nearest nested block mapping before the parent
    limit = parent if parent >= 0 else len(doc.kind)
    kind = doc.kind
    sty = doc.sty
    text = doc.text
    for r in range(limit - 1, 0, -1):
        if kind[r] == K_MAP and sty[r] == "b" and kind[r - 1] == K_SCALAR:
            key = r - 1
            # the key must be a key: its text is followed by a colon
            c = colon_after(doc, key)
            if c < 0 or doc.st[r] <= c:
                continue
            d = col(text, doc.st[r]) - col(text, entry_start(doc, key))
            if d > 0 and only_blanks_before(text, entry_start(doc, key)):
                return d
    return 2


def make_layout(doc: Doc, parent: int, anc: list[int], before_row: int | None = None) -> W.Layout:
    """The layout for new block text inside collection ``parent``."""
    text = doc.text
    step = None
    seq_off = None
    if parent >= 0 and doc.kind[parent] == K_MAP and doc.sty[parent] == "b":
        for k, v in members(doc, parent):
            if doc.kind[v] == K_MAP and doc.sty[v] == "b" and step is None:
                step = col(text, doc.st[v]) - col(text, entry_start(doc, k))
            if doc.kind[v] == K_SEQ and doc.sty[v] == "b" and seq_off is None:
                seq_off = col(text, doc.st[v]) - col(text, entry_start(doc, k))
    if step is None or step <= 0:
        step = doc_step(doc, parent, anc)
    if seq_off is None or seq_off < 0:
        seq_off = step
    hint, key_hint = sibling_hint(doc, parent, before_row)
    near = doc.en[parent] if parent >= 0 else 0
    return W.Layout(step, seq_off, doc_break(doc, near), hint, key_hint)


# ---- anchors in use -------------------------------------------------------------------------


def check_anchors_free(doc: Doc, lo: int, hi: int, self_row: int | None) -> None:
    """Refuse when an anchor defined inside rows ``lo:hi`` (other than ``self_row``'s) is used by
    an alias outside them."""
    anc = doc.anc
    inside = [r for r in range(lo, hi) if anc[r] is not None and r != self_row]
    if not inside:
        return
    am = T.alias_map(doc)
    if not am:
        return
    want = set(inside)
    for a, t in am.items():
        if t in want and not lo <= a < hi:
            raise refuse(
                "refused-alias", f"the anchor {anc[t]!r} is used by an alias outside the change"
            )


# ---- tags -----------------------------------------------------------------------------------


def tag_survives(tag: str | None, value: Any) -> bool:
    """Whether a tag still describes ``value`` (and reads back as exactly it)."""
    if tag is None:
        return False
    if tag == "!":
        return isinstance(value, str)
    core = _CORE_SCALAR_TAGS.get(tag)
    if core is None:
        return not tag.startswith(_YAML)
    if core == "str":
        return isinstance(value, str)
    if core == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if core == "float":
        return isinstance(value, float)
    if core == "bool":
        return isinstance(value, bool)
    if core == "null":
        return value is None
    if core == "binary":
        return isinstance(value, (bytes, bytearray))
    if core == "map":
        return isinstance(value, dict) or (hasattr(value, "items") and not isinstance(value, str))
    return isinstance(value, (list, tuple))


def _is_null(doc: Doc, r: int) -> bool:
    if doc.kind[r] != K_SCALAR:
        return False
    tag = doc.tag[r]
    if tag is None:
        return doc.sty[r] == ":" and doc.val[r] in ("", "~", "null", "Null", "NULL")
    return tag == _YAML + "null" and doc.val[r] in ("", "~", "null", "Null", "NULL")


def props_text(doc: Doc, r: int, keep_tag: bool) -> str:
    """The properties of a node that survive an edit, as text ending in a space (or empty)."""
    ps, st = doc.ps[r], doc.st[r]
    if ps >= st:
        return ""
    toks = doc.text[ps:st].split()
    keep = [t for t in toks if t[0] == "&" or (t[0] == "!" and keep_tag)]
    return " ".join(keep) + " " if keep else ""


# ---- set a value ----------------------------------------------------------------------------

_MARKER = re.compile(r"(?m)^\ufeff?---(?=[ \t\r\n]|\Z)")


def root_marker_end(doc: Doc) -> int:
    """The position after the ``---`` that opens the document, or -1 without one."""
    end = -1
    for m in _MARKER.finditer(doc.text, 0, doc.ps[0]):
        end = m.end()
    return end


def _slot(doc: Doc, loc: Loc) -> tuple[str, int]:
    """How the node is reached: ``map`` / ``seq`` with the position after the ``:`` / ``-`` that
    leads to it (-1 for an explicit key without a ``:``), ``flow``, or ``root`` with the
    position after the document marker (-1 without one)."""
    p = loc.parent
    if p < 0:
        return "root", root_marker_end(doc)
    if doc.sty[p] in "fp":
        return "flow", -1
    if doc.kind[p] == K_MAP:
        c = colon_after(doc, loc.key, col(doc.text, doc.st[p]))
        return "map", (c + 1 if c >= 0 else -1)
    return "seq", dash_of(doc, p, loc.index, loc.prev_end) + 1


def _line_comment(text: str, pos: int) -> str:
    # A comment right after a line break is a line of its own, not a trailing comment.
    if pos > 0 and text[pos - 1] in "\r\n":
        return ""
    m = _COMMENT(text, pos)
    return (m.group(1) + m.group(2)) if m else ""


def replace_value(doc: Doc, loc: Loc, value: Any) -> None:
    """Make the node at ``loc`` hold ``value``, changing only that node's text."""
    r = loc.row
    if loc.alias:
        raise refuse("refused-alias", "the path goes through an alias")
    parent = loc.parent
    pflow = parent >= 0 and doc.sty[parent] in "fp"
    check_anchors_free(doc, r, r + doc.sz[r], r)
    tv = value
    new_tag: str | None = None
    if isinstance(value, Tagged):
        new_tag = value.tag
        tv = value.value
    is_bytes = isinstance(tv, (bytes, bytearray, memoryview))
    if is_bytes:
        new_tag = _YAML + "binary"
    old_tag = doc.tag[r]
    keep_old = new_tag is None and tag_survives(old_tag, tv)
    row_tag = old_tag if keep_old else new_tag
    anchor = doc.anc[r]
    if tv is None and new_tag is None and _is_null(doc, r):
        return  # an existing null keeps its spelling
    if new_tag is not None:
        pr = ("&" + anchor + " " if anchor else "") + W.tag_text(new_tag) + " "
    else:
        pr = props_text(doc, r, keep_old)
    slot_kind, ind_end = _slot(doc, loc)
    coll = W.is_collection(tv)
    if coll and len(tv) > 0:
        _put_collection(doc, loc, tv, pr, anchor, row_tag, slot_kind, ind_end, pflow)
    else:
        _put_inline(doc, loc, tv, coll, is_bytes, pr, anchor, row_tag, slot_kind, ind_end, pflow)


def _inline_target(doc: Doc, loc: Loc, slot_kind: str, ind_end: int) -> tuple[int, int, str, str]:
    """Where inline text for the node goes: ``(a, b, prefix, suffix)``; the node's text from
    ``a`` to ``b`` is replaced by ``prefix + new + suffix``."""
    r = loc.row
    text = doc.text
    k = doc.kind[r]
    st0, en0 = doc.st[r], doc.en[r]
    ps0 = doc.ps[r] if k != K_ALIAS else st0
    if k == K_SCALAR and ps0 == st0 == en0:  # nothing there yet
        if slot_kind in ("map", "seq"):
            a = ind_end
            if text[a : a + 1] in (" ", "\t"):
                a += 1
                prefix = ""
            else:
                prefix = " "
        elif slot_kind == "root":
            a = ind_end if ind_end >= 0 else st0
            prefix = " " if ind_end >= 0 and text[a : a + 1] not in (" ", "\t") else ""
            if prefix == "" and ind_end >= 0 and text[a : a + 1] in (" ", "\t"):
                a += 1
        else:
            a = st0
            prefix = ""
        suffix = " " if text[a : a + 1] == "#" else ""
        return a, a, prefix, suffix
    if k in (K_MAP, K_SEQ) and doc.sty[r] == "b" and slot_kind in ("map", "seq"):
        # a block collection becomes inline: it stays on the key's line, with the comment there
        e2 = node_extent(doc, r)
        return (
            ind_end,
            e2 + (len(_line_comment(text, e2)) if e2 == en0 else 0),
            " ",
            (_line_comment(text, ind_end) if ps0 > ind_end else ""),
        )
    b = scalar_extent(doc, r) if k == K_SCALAR and doc.sty[r] in "|>" else en0
    return ps0, b, "", ""


def _put_inline(
    doc, loc, tv, is_empty_coll, is_bytes, pr, anchor, row_tag, slot_kind, ind_end, pflow
):
    r = loc.row
    text = doc.text
    k = doc.kind[r]
    en0 = doc.en[r]
    sty0 = doc.sty[r] if k == K_SCALAR else ":"
    old_block = k == K_SCALAR and sty0 in "|>"
    en_ext = scalar_extent(doc, r) if old_block else en0
    empty_old = k == K_SCALAR and doc.ps[r] == doc.st[r] == en0
    nl = doc_break(doc, en0)
    row_kind = K_SCALAR
    blk = None
    if is_empty_coll:
        content = "[]" if isinstance(tv, (list, tuple)) else "{}"
        style = "f"
        valtext = None
        row_kind = K_SEQ if content == "[]" else K_MAP
    elif is_bytes:
        import base64

        content = base64.b64encode(bytes(tv)).decode("ascii")
        style = ":"
        valtext = content
    else:
        if isinstance(tv, str) and old_block and not pflow:
            blk = _block_for(doc, loc, r, tv, sty0, en0, en_ext, nl)
        if blk is not None:
            content, style, valtext = blk, sty0, tv
        else:
            hint = sty0 if sty0 in "'\"" and not empty_old else None
            content, style, valtext = W.scalar_inline(tv, hint, pflow, False, row_tag is not None)
    if slot_kind == "map" and ind_end < 0:
        _set_missing_colon(doc, loc, pr, content, style, anchor, row_tag, valtext)
        return
    a, b, prefix, suffix = _inline_target(doc, loc, slot_kind, ind_end)
    full = prefix + pr + content + suffix
    if blk is None and b > a and text[b - 1] in "\r\n" and not full.endswith(("\r", "\n")):
        full += nl  # the old text ended with its line break (a keep scalar): give it back
    ps_new = a + len(prefix)
    st_new = ps_new + len(pr)
    en_new = st_new + len(content.rstrip("\r\n"))
    if row_kind == K_SCALAR:
        rows = scalar_rows(ps_new, st_new, en_new, style, anchor, row_tag, valtext)
    else:
        rows = scalar_rows(ps_new, st_new, st_new + 2, "f", anchor, row_tag, "")
        rows.kind[0] = row_kind
        rows.val[0] = None
    splice(doc, a, b, full, r, r + doc.sz[r], rows, loc.anc, (en0, rows.en[0]))


def _block_for(doc, loc, r, tv, sty0, en0, en_ext, nl) -> str | None:
    """A literal or folded scalar holding ``tv`` laid out like the old one (None: it cannot)."""
    text = doc.text
    chomp, ind = scalar_header(doc, r)
    p = loc.parent
    par = col(text, doc.st[p]) if p >= 0 else 0
    ci = block_content_indent(doc, r)
    if ind is not None:
        ci = par + ind
    elif ci is None:
        ci = par + 2
    res = W.block_scalar(tv, sty0, ci, nl, chomp, par, ind is not None)
    if res is None:
        return None
    body, t = res
    new_incl = body.split(nl, 1)[0].endswith("+")
    old_incl = en_ext > en0 and text[en_ext - 1] in "\r\n"
    if new_incl:
        extra = t - following_breaks(text, en_ext)
        if extra < 0:
            return None  # blank lines that follow would become part of the value
        body += nl * extra
    elif old_incl:
        body += nl
    return body


def _set_missing_colon(doc, loc, pr, content, style, anchor, row_tag, valtext):
    """The value of an explicit key that has no ``:`` line goes on a line of its own."""
    text = doc.text
    key = loc.key
    c = col(text, entry_start(doc, key))
    nl = doc_break(doc, doc.en[key])
    a = line_end(text, doc.en[key])
    lead = nl + " " * c + ": "
    full = lead + pr + content
    ps_new = a + len(lead)
    st_new = ps_new + len(pr)
    rows = scalar_rows(ps_new, st_new, st_new + len(content), style, anchor, row_tag, valtext)
    r = loc.row
    splice(doc, a, a, full, r, r + doc.sz[r], rows, loc.anc, (doc.en[r], rows.en[0]))


def _put_collection(doc, loc, tv, pr, anchor, row_tag, slot_kind, ind_end, pflow):  # noqa: C901
    """A non-empty collection becomes the node: in flow style where the node is in a flow
    collection or was one, else in block style under the key or after the dash."""
    r = loc.row
    text = doc.text
    k = doc.kind[r]
    anc = loc.anc
    old_flow = k in (K_MAP, K_SEQ) and doc.sty[r] in "fp"
    inner = tv.value if isinstance(tv, Tagged) else tv
    if pflow or old_flow:
        flow = W.flow_text(tv, None)
        a, b, prefix, suffix = _inline_target(doc, loc, slot_kind, ind_end)
        full = prefix + pr + flow + suffix
        base = a + len(prefix) + len(pr)
        rows = snippet_rows(flow, 0, base)
        rows.ps[0] = a + len(prefix)
        rows.anc[0] = anchor
        rows.tag[0] = row_tag
        splice(doc, a, b, full, r, r + doc.sz[r], rows, anc, (doc.en[r], rows.en[0]))
        return
    is_seq = isinstance(inner, (list, tuple))
    p = loc.parent
    st0 = doc.st[r]
    ps0 = doc.ps[r] if k != K_ALIAS else st0
    empty_old = k == K_SCALAR and ps0 == st0 == doc.en[r]
    if slot_kind == "root":
        lay = make_layout(doc, -1, [], None)
        lay.hint = lay.key_hint = None
        indent = col(text, st0) if (k in (K_MAP, K_SEQ) and only_blanks_before(text, st0)) else 0
    else:
        lay = make_layout(doc, p, anc, r)
        if slot_kind == "map":
            kc = col(text, entry_start(doc, loc.key))
            indent = kc + (lay.seq_off if is_seq else lay.step)
        else:
            indent = col(text, ind_end - 1) + 2
    nl = lay.nl
    # what the key's line keeps: the comment that is on it
    later_line = ind_end >= 0 and ps0 > ind_end and bool(_BRK.search(text, ind_end, ps0))
    if slot_kind == "root":
        marker = ind_end
        on_marker_line = marker >= 0 and not _BRK.search(text, marker, ps0)
        if marker >= 0 and (empty_old or on_marker_line):
            a = marker
            if empty_old:
                tail = _line_comment(text, marker)
                b = a + len(tail)
            else:
                tail = ""
                b = scalar_extent(doc, r) if k == K_SCALAR else doc.en[r]
                tail = _line_comment(text, b)
                b += len(tail)
            head = (" " + pr.rstrip() if pr else "") + tail
            consumed = b > a and text[b - 1] in "\r\n"
            block, fol = new_block(doc, tv, indent, lay, False, b, 1 if consumed else 0)
            full = head + nl + block
            if consumed:
                full += nl
            base = a + len(head) + len(nl)
            rows = snippet_rows(block, 0, base, fol, nl)
        else:
            a = ps0
            b = node_extent(doc, r)
            if b == doc.en[r]:
                b += len(_line_comment(text, b))
            consumed = b > a and text[b - 1] in "\r\n"
            block, fol = new_block(doc, tv, indent, lay, False, b, 1 if consumed else 0)
            if pr:
                full = pr.rstrip() + nl + block
                base = a + len(pr.rstrip()) + len(nl)
            else:
                full = block
                base = a
            if consumed:
                full += nl
            rows = snippet_rows(block, 0, base, fol, nl)
        rows.ps[0] = a + 1 if pr else rows.st[0]
        rows.anc[0] = anchor
        rows.tag[0] = row_tag
        splice(doc, a, b, full, r, r + doc.sz[r], rows, anc, (doc.en[r], rows.en[0]))
        return
    a = ind_end
    if empty_old:
        tail = _line_comment(text, ind_end)
        b = a + len(tail)
    elif later_line:
        tail = _line_comment(text, ind_end)
        b = node_extent(doc, r)
        if b == doc.en[r]:
            b += len(_line_comment(text, b))
    else:
        b0 = scalar_extent(doc, r) if k == K_SCALAR else doc.en[r]
        tail = _line_comment(text, b0)
        b = b0 + len(tail)
    compact = slot_kind == "seq" and not pr and not tail.strip()
    consumed = b > a and text[b - 1] in "\r\n"
    if compact:
        block, fb = new_block(doc, tv, indent, lay, True, b, 1 if consumed else 0)
        full = " " + block
        rows = snippet_rows(block, indent, a + 1, fb, nl)
        rows.ps[0] = rows.st[0]
    else:
        block, fb = new_block(doc, tv, indent, lay, False, b, 1 if consumed else 0)
        head = (" " + pr.rstrip() if pr else "") + tail
        full = head + nl + block
        rows = snippet_rows(block, 0, a + len(head) + len(nl), fb, nl)
        rows.ps[0] = a + 1 if pr else rows.st[0]
    rows.anc[0] = anchor
    rows.tag[0] = row_tag
    if b > a and text[b - 1] in "\r\n":
        full += nl  # the old text ended with its line break (a keep scalar): give it back
    splice(doc, a, b, full, r, r + doc.sz[r], rows, anc, (doc.en[r], rows.en[0]))


# ---- insert ---------------------------------------------------------------------------------


def entry_end(doc: Doc, key: int, val: int, key_col: int) -> int:
    """The position after the last line of an entry (or item) of a block collection: its value,
    the rest of that line, its break, and comment lines that are indented deeper than the key."""
    text = doc.text
    e = max(node_extent(doc, val), doc.en[key])
    b = e if e > 0 and e > doc.en[val] and text[e - 1] in "\r\n" else next_line(text, e)
    # deeper comment lines
    while b < len(text):
        m = re.compile(r"([ \t]*)#[^\r\n]*").match(text, b)
        if m and len(m.group(1)) > key_col:
            b = next_line(text, m.end())
        else:
            break
    return b


def insert_member(doc: Doc, loc: Loc, key: str, value: Any) -> None:
    """Add ``key: value`` at the end of the mapping at ``loc`` (a key that exists is replaced)."""
    p = loc.row
    if loc.alias:
        raise refuse("refused-alias", "the path goes through an alias")
    v, kr = T.find_member(doc, p, key)
    if v == -2:
        raise refuse("refused-ambiguous", f"more than one key reads {key!r}")
    if v >= 0:
        child = Loc()
        child.row = v
        child.key = kr
        child.parent = p
        child.anc = [*loc.anc, p]
        replace_value(doc, child, value)
        return
    anc = [*loc.anc, p]
    text = doc.text
    if doc.sty[p] == "f":
        _insert_flow(doc, p, anc, None, key, value)
        return
    if doc.sty[p] == "p":
        raise refuse("patch-invalid", "cannot add a key to an implicit pair in a flow sequence")
    ents = members(doc, p)
    lk, lv = ents[-1]
    kcol = col(text, entry_start(doc, lk))
    lay = make_layout(doc, p, anc, None)
    pos = entry_end(doc, lk, lv, kcol)
    at_end_no_break = pos >= len(text) and (not text or text[-1] not in "\r\n")
    snippet, fol = new_block(doc, {key: value}, kcol, lay, False, pos, 0 if at_end_no_break else 1)
    if at_end_no_break:
        new = lay.nl + snippet
        base = pos + len(lay.nl)
    else:
        new = snippet + lay.nl
        base = pos
    rows = drop_first_row(snippet_rows(snippet, 0, base, fol, lay.nl))
    old_end = doc.en[p]
    hi = p + doc.sz[p]
    splice(doc, pos, pos, new, hi, hi, rows, anc, (old_end, rows.en[1]))


def _insert_flow(doc: Doc, p: int, anc: list[int], index: int | None, key: Any, value: Any) -> None:
    """Add a member or item to a flow collection at ``index`` (None: the end)."""
    is_map = doc.kind[p] == K_MAP
    hint, key_hint = sibling_hint(doc, p, None)
    if is_map:
        ktxt = W.scalar_inline(key, key_hint, True, True)[0]
        item = ktxt + ": " + W.flow_text(value, hint)
    else:
        item = W.flow_text(value, hint)
    n = doc.sz[p]
    if is_map:
        ents = members(doc, p)
        kids = [(entry_start(doc, k), max(doc.en[v], doc.en[k]), k) for k, v in ents]
    else:
        kids = [(doc.ps[c], doc.en[c], c) for c in items(doc, p)]
    if not kids:
        a = doc.st[p] + 1
        new = item
        row_at = p + 1
    elif index is None or index >= len(kids):
        a = kids[-1][1]
        new = ", " + item
        row_at = p + n
    else:
        a = kids[index][0]
        new = item + ", "
        row_at = kids[index][2]
    snippet = item
    off = a + (2 if new.startswith(", ") else 0)
    if is_map:
        txt = "{" + snippet + "}"
        d = snippet_rows(txt, 0, off - 1)
    else:
        txt = "[" + snippet + "]"
        d = snippet_rows(txt, 0, off - 1)
    rows = drop_first_row(d)
    splice(doc, a, a, new, row_at, row_at, rows, anc)


def insert_item(doc: Doc, loc: Loc, index: int | None, value: Any) -> None:
    """Add ``value`` as item ``index`` (None: the end) of the sequence at ``loc``."""
    p = loc.row
    if loc.alias:
        raise refuse("refused-alias", "the path goes through an alias")
    anc = [*loc.anc, p]
    text = doc.text
    kids = items(doc, p)
    n = len(kids)
    if index is None:
        index = n
    if index < 0 or index > n:
        raise refuse("patch-path", f"index {index} is out of range")
    if doc.sty[p] == "f":
        _insert_flow(doc, p, anc, index, None, value)
        return
    lay = make_layout(doc, p, anc, kids[index] if index < n else None)
    dcol = col(text, doc.st[p])
    if index == n:
        last = kids[-1]
        pos = entry_end(doc, last, last, dcol)
        at_end_no_break = pos >= len(text) and (not text or text[-1] not in "\r\n")
        snippet, fol = new_block(doc, [value], dcol, lay, False, pos, 0 if at_end_no_break else 1)
        if at_end_no_break:
            new = lay.nl + snippet
            base = pos + len(lay.nl)
        else:
            new = snippet + lay.nl
            base = pos
        rows = drop_first_row(snippet_rows(snippet, 0, base, fol, lay.nl))
        old_end = doc.en[p]
        hi = p + doc.sz[p]
        splice(doc, pos, pos, new, hi, hi, rows, anc, (old_end, rows.en[0]))
        return
    c = kids[index]
    _, pe = item_at(doc, p, index)
    dash = dash_of(doc, p, index, pe)
    only_blanks_before_text = only_blanks_before(text, dash)
    if only_blanks_before_text:
        pos = leading_comments_start(doc, line_start(text, dash), dcol, index > 0)
        snippet, fol = new_block(doc, [value], dcol, lay, False, pos, 1)
        new = snippet + lay.nl
        rows = drop_first_row(snippet_rows(snippet, 0, pos, fol, lay.nl))
    else:
        # the item shares its line with an outer "- ": the new item takes that line
        pos = dash
        pad = " " * col(text, dash)
        snippet, fol = new_block(doc, [value], dcol, lay, True, pos, 1)
        new = snippet + lay.nl + pad
        rows = drop_first_row(snippet_rows(snippet, dcol, pos, fol, lay.nl))
    old_st = doc.st[p]
    splice(doc, pos, pos, new, c, c, rows, anc)
    if index == 0 and only_blanks_before_text:
        set_start(doc, anc, old_st, pos + dcol)


def leading_comments_start(doc: Doc, ls: int, key_col: int, has_prev: bool) -> int:
    """Move up over the comment lines directly above the line at ``ls`` that lead its entry."""
    text = doc.text
    a = ls
    while a > 0:
        pls = prev_line(text, a)
        line = text[pls:a]
        stripped = line.strip(" \t\r\n\ufeff")
        if not stripped.startswith("#"):
            break
        indent = len(line) - len(line.lstrip(" \t"))
        if has_prev and indent > key_col:
            break
        a = pls
    return a


# ---- remove ---------------------------------------------------------------------------------


def remove_node(doc: Doc, loc: Loc) -> None:
    """Remove the member the path leads to."""
    if loc.alias:
        raise refuse("refused-alias", "the path goes through an alias")
    p = loc.parent
    if p < 0:
        raise refuse("patch-invalid", "the document root cannot be removed")
    text = doc.text
    r = loc.row
    anc = loc.anc
    is_map = doc.kind[p] == K_MAP
    if is_map:
        k = loc.key
        lo = k
        hi = r + doc.sz[r]
    else:
        lo = r
        hi = r + doc.sz[r]
    check_anchors_free(doc, lo, hi, None)
    n = T.count_items(doc, p)
    if n == 1:
        # the collection would be empty: it becomes {} or []
        pl = Loc()
        pl.row = p
        pl.anc = anc[:-1]
        pl.parent = anc[-2] if len(anc) >= 2 else -1
        if pl.parent >= 0:
            if doc.kind[pl.parent] == K_MAP:
                ents = members(doc, pl.parent)
                pl.key = next(eol for eol, val2 in ents if val2 == p)
            else:
                pl.index = [c for c in items(doc, pl.parent)].index(p)
                _, pl.prev_end = item_at(doc, pl.parent, pl.index)
        empty: Any = {} if is_map else []
        if doc.sty[p] == "p":
            # an implicit pair becomes an empty mapping
            _replace_pair(doc, pl, p)
            return
        replace_value(doc, pl, empty)
        return
    if doc.sty[p] in "fp":
        _remove_flow(doc, loc, lo, hi, is_map)
        return
    # block collection
    kcol = col(text, entry_start(doc, lo)) if is_map else col(text, doc.st[p])
    if is_map:
        s = entry_start(doc, lo)
    else:
        _, pe = item_at(doc, p, loc.index)
        s = dash_of(doc, p, loc.index, pe)
    has_prev = lo > p + 1 if is_map else loc.index > 0
    if not only_blanks_before(text, s):
        # first entry of a compact collection after "- " or "? ": remove up to the next entry
        nxt = _next_start(doc, p, lo, hi, is_map)
        old_st = doc.st[p]
        first = lo == p + 1
        splice(doc, s, nxt, "", lo, hi, None, anc)
        if first:
            set_start(doc, anc, old_st, s)
        return
    ls = line_start(text, s)
    a = leading_comments_start(doc, ls, kcol, has_prev)
    b = entry_end(doc, lo if is_map else r, r, kcol)
    # a white-space-only line after a block scalar would join it once the entry is gone
    is_last = hi == p + doc.sz[p]
    keep_prev = False
    if is_last and lo > p + 1 or (is_last and not is_map and r > p + 1):
        prev_last = _prev_value_row(doc, p, lo)
        if prev_last >= 0 and doc.kind[prev_last] == K_SCALAR and doc.sty[prev_last] in "|>":
            # blank lines after a keep scalar belong to its value; they must not grow
            keep_prev = scalar_header(doc, prev_last)[0] == "+"
            while b < len(text):
                eol = line_end(text, b)
                seg = text[b:eol]
                if seg.strip(" \t") == "" and (seg or keep_prev):
                    b = next_line(text, eol)
                else:
                    break
    if b >= len(text) and (not text or text[-1] not in "\r\n") and a > 0 and not keep_prev:
        # the last line has no break of its own: the break before it goes with it
        pls = prev_line(text, a)
        a = pls + len(text[pls:a].rstrip("\r\n"))
    ends = None
    if is_last:
        ends = (doc.en[r], _prev_end_before(doc, p, lo))
    first = lo == p + 1
    new_st = None
    if first:
        nstart = entry_start(doc, hi) if is_map else dash_of(doc, p, 1, doc.en[r])
        new_st = a + (nstart - b)
    old_st = doc.st[p]
    splice(doc, a, b, "", lo, hi, None, anc, ends)
    if new_st is not None:
        set_start(doc, anc, old_st, new_st)


def _prev_value_row(doc: Doc, p: int, lo: int) -> int:
    """The last row of the child before row ``lo`` in collection ``p``."""
    return lo - 1 if lo - 1 > p else -1


def _prev_end_before(doc: Doc, p: int, lo: int) -> int:
    """The end of the child of collection ``p`` that comes just before row ``lo``."""
    j = p + 1
    last = -1
    end = p + doc.sz[p]
    while j < lo and j < end:
        last = j
        j += doc.sz[j]
    return doc.en[last] if last >= 0 else doc.en[p]


def _next_start(doc: Doc, p: int, lo: int, hi: int, is_map: bool) -> int:
    """Where the entry after rows ``lo:hi`` begins."""
    if is_map:
        return entry_start(doc, hi)
    return dash_of(doc, p, 1, doc.en[lo])


def _remove_flow(doc: Doc, loc: Loc, lo: int, hi: int, is_map: bool) -> None:
    p = loc.parent
    anc = loc.anc
    if is_map:
        ents = members(doc, p)
        spans = [(entry_start(doc, k), max(doc.en[v], doc.en[k])) for k, v in ents]
        idx = next(i for i, (k, _) in enumerate(ents) if k == lo)
    else:
        kids = items(doc, p)
        spans = [(doc.ps[c], doc.en[c]) for c in kids]
        idx = kids.index(loc.row)
    s, e = spans[idx]
    if idx < len(spans) - 1:
        a, b = s, spans[idx + 1][0]
    else:
        a, b = spans[idx - 1][1], e
    splice(doc, a, b, "", lo, hi, None, anc)


def _replace_pair(doc: Doc, pl: Loc, p: int) -> None:
    """An implicit pair inside a flow sequence, emptied: it becomes ``{}``."""
    anc = pl.anc
    rows = Doc("")
    s = doc.st[p]
    rows.kind.append(K_MAP)
    rows.ps.append(s)
    rows.st.append(s)
    rows.en.append(s + 2)
    rows.sz.append(1)
    rows.sty.append("f")
    rows.anc.append(None)
    rows.tag.append(None)
    rows.val.append(None)
    splice(doc, s, doc.en[p], "{}", p, p + doc.sz[p], rows, anc)
