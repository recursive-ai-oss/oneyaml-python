"""Core-schema resolution of scalar text (YAML 1.2.2 section 10.3.2)."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from .._types import Tagged, big_int

NAN = float("nan")  # one object, so a dict finds a repeated ``.nan`` key
INF = float("inf")
NINF = float("-inf")


class Fail(Exception):
    """A construction error; the loader adds the position."""

    def __init__(self, message: str, category: str = "syntax", offset: int | None = None):
        super().__init__(message)
        self.message = message
        self.category = category
        self.offset = offset
        self.key_index: int | None = None


KEYWORDS: dict[str, Any] = {
    "": None, "~": None, "null": None, "Null": None, "NULL": None,
    "true": True, "True": True, "TRUE": True,
    "false": False, "False": False, "FALSE": False,
}  # fmt: skip

# One match tells the type by the group that took part. Group 5 holds the sign of an infinity;
# ``.nan`` takes part in no group.
_NUMBER = re.compile(
    r"(?:([-+]?[0-9]+)|0o([0-7]+)|0x([0-9a-fA-F]+)"
    r"|([-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?)"
    r"|([-+]?)\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\Z"
).match

# Characters a plain scalar needs to start with to be anything but a string.
NUMBER_START = frozenset("0123456789+-.")
WORD_START = frozenset("tTfFnN~")

# CPython refuses ``int(str)`` beyond this many digits; longer numbers are read in pieces.
_PY_DIGITS = 4300


def _too_long(n: int, cap: int) -> Fail:
    return Fail(f"an integer of {n} digits is over the limit of {cap} digits", "limit")


def number(v: str, cap: int | None, wide: dict[int, float]) -> Any:
    """The value of a plain scalar that starts like a number, or ``v`` when it is not one.

    ``cap`` is the most digits an integer may have (``None`` for any). A float outside the range
    of a double is recorded in ``wide`` (by identity) so a mapping can refuse it as a key."""
    m = _NUMBER(v)
    if m is None:
        return v
    g = m.lastindex
    if g == 1:
        s = m[1]
        if len(s) <= (_PY_DIGITS if cap is None or cap > _PY_DIGITS else cap):
            try:
                return int(s)
            except ValueError:  # the interpreter's own cap was set lower than ours
                return big_int(s)
        n = len(s) - (s[0] in "+-")
        if cap is not None and n > cap:
            raise _too_long(n, cap)
        return big_int(s)
    if g == 2 or g == 3:
        s = m[g]
        if cap is not None and len(s) > cap:
            raise _too_long(len(s), cap)
        return int(s, 8 if g == 2 else 16)
    if g == 4:
        x = float(m[4])
        if x == 0.0 or x in (INF, NINF):
            _note_range(x, m[4], wide)
        return x
    if g == 5:
        return NINF if m[5] == "-" else INF
    return NAN


_NONZERO = re.compile(r"[1-9]")


def _note_range(x: float, text: str, wide: dict[int, float]) -> None:
    """Remember a float that a double cannot hold: an overflow, or a non-zero that underflows."""
    if x == 0.0:
        mantissa = text.lower().split("e")[0]
        if _NONZERO.search(mantissa) is None:
            return
    wide[id(x)] = x


_YAML = "tag:yaml.org,2002:"
_SCALAR_TAGS = frozenset(
    _YAML + t for t in ("str", "int", "float", "bool", "null", "binary", "map", "seq")
)
COLLECTION_OK = {_YAML + "map": 1, _YAML + "seq": 0}
_WRONG_FOR_COLLECTION = frozenset(
    _YAML + t for t in ("str", "int", "float", "bool", "null", "binary")
)
_BLANKS = re.compile(r"[ \t\r\n]+")


def tagged_scalar(tag: str, v: str, cap: int | None, wide: dict[int, float]) -> Any:
    """A scalar with an explicit tag: Core tags decide the type, any other keeps the text."""
    if tag not in _SCALAR_TAGS:
        return v if tag == "!" else Tagged(tag, v)
    kind = tag[len(_YAML) :]
    if kind == "str":
        return v
    if kind == "null":
        if v in ("", "~", "null", "Null", "NULL"):
            return None
    elif kind == "bool":
        if v in KEYWORDS and v and v[0] in "tTfF":
            return KEYWORDS[v]
    elif kind == "int":
        r = number(v, cap, wide)
        if type(r) is int:
            return r
    elif kind == "float":
        m = _NUMBER(v)
        if m is not None:
            if m.lastindex == 1:
                n = len(m[1]) - (m[1][0] in "+-")
                if cap is not None and n > cap:
                    raise _too_long(n, cap)
                return float(m[1])
            r = number(v, cap, wide)
            if type(r) is float:
                return r
            if type(r) is int and m.lastindex in (2, 3):
                raise Fail(f"{v!r} is not a float")
    elif kind == "binary":
        try:
            return base64.b64decode(_BLANKS.sub("", v), validate=True)
        except (binascii.Error, ValueError):
            raise Fail("the text of a !!binary scalar is not base64") from None
    else:
        raise Fail(f"a !!{kind} tag cannot be put on a scalar")
    raise Fail(f"{v!r} is not a valid {tag[len(_YAML) :]} value")
