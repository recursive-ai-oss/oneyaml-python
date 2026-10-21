"""Resolution of plain scalars under the YAML 1.1 rules of PyYAML's safe loader.

The forms are PyYAML's own implicit resolvers: ``yes``/``no``/``on``/``off``, ``0b``/``0x``/
leading-zero integers, ``_`` separators, base-60 numbers, floats that need a signed exponent,
dates and times, and the ``<<`` merge key. The native backend implements the same table.
"""

from __future__ import annotations

import re
from typing import Any

from .._types import big_int
from .._yaml11 import (
    Reject,
    construct,
    est_digits,
    est_digits60,
    sexagesimal_float,
    sexagesimal_int,
    timestamp,
)
from ._resolve import _PY_DIGITS, INF, NAN, NINF, Fail, _too_long


class _Merge:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<merge key>"


_MERGE_TAG = "tag:yaml.org,2002:merge"

# What a plain ``<<`` resolves to; the mapping that holds it takes the entries it names.
MERGE = _Merge()

KEYWORDS11: dict[str, Any] = {
    "": None, "~": None, "null": None, "Null": None, "NULL": None,
    "yes": True, "Yes": True, "YES": True, "true": True, "True": True, "TRUE": True,
    "on": True, "On": True, "ON": True,
    "no": False, "No": False, "NO": False, "false": False, "False": False, "FALSE": False,
    "off": False, "Off": False, "OFF": False,
    "<<": MERGE,
}  # fmt: skip

NUMBER_START11 = frozenset("0123456789+-.")
WORD_START11 = frozenset("yYnNtTfFoO~<")

# One match tells the form by the group that took part.
_NUMBER11 = re.compile(
    r"(?:([-+]?(?:0|[1-9][0-9_]*))"  # 1 decimal
    r"|([-+]?0b[0-1_]+)"  # 2 binary
    r"|([-+]?0[0-7_]+)"  # 3 octal
    r"|([-+]?0x[0-9a-fA-F_]+)"  # 4 hexadecimal
    r"|([-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)"  # 5 base 60
    r"|([-+]?[0-9][0-9_]*\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?)"  # 6
    r"|([-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*)"  # 7 base 60 float
    r"|([-+]?\.(?:inf|Inf|INF))"  # 8
    r"|(\.(?:nan|NaN|NAN))"  # 9
    r"|([0-9]{4}-[0-9]{2}-[0-9]{2}"  # 10 date or timestamp
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
    r"(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?))\Z"
).match


def _decimal(s: str, cap: int | None) -> int:
    n = len(s) - (s[0] in "+-")
    if n <= (_PY_DIGITS if cap is None or cap > _PY_DIGITS else cap):
        try:
            return int(s)
        except ValueError:  # the interpreter's own cap was set lower than ours
            return big_int(s)
    if cap is not None and n > cap:
        raise _too_long(n, cap)
    return big_int(s)


def number11(v: str, cap: int | None, wide: dict[int, float]) -> Any:
    """The value of a plain scalar that starts like a number, or ``v`` when it is not one."""
    m = _NUMBER11(v)
    if m is None:
        return v
    g = m.lastindex
    s = m[g]
    if g == 1:
        return _decimal(s.replace("_", ""), cap) if "_" in s else _decimal(s, cap)
    if g == 8:
        return NINF if s[0] == "-" else INF
    if g == 9:
        return NAN
    if g == 10:
        try:
            return timestamp(v)
        except Reject as exc:
            raise Fail(exc.message, exc.category) from None
    s = s.replace("_", "")
    neg = s[0] == "-"
    body = s[1:] if s[0] in "+-" else s
    if g == 6:
        return float(s)
    if g == 7:
        value = sexagesimal_float([float(p) for p in body.split(":")])
        return -value if neg else value
    if g == 5:
        parts = body.split(":")
        est = est_digits60(len(parts[0]), len(parts))
        if cap is not None and est > cap:
            raise _too_long(est, cap)
        return sexagesimal_int(s)
    # Binary, octal and hexadecimal.
    if g == 3:
        digits, base = body, 8
    else:
        digits, base = body[2:], 2 if g == 2 else 16
    if not digits:  # ``0b_``: no digits at all
        return v
    est = est_digits(base, len(digits.lstrip("0")))
    if cap is not None and est > cap:
        raise _too_long(est, cap)
    n = int(digits, base)
    return -n if neg else n


def tagged_scalar11(tag: str, v: str, cap: int | None, wide: dict[int, float]) -> Any:
    """A scalar with an explicit tag."""
    if tag == _MERGE_TAG:
        return MERGE
    try:
        return construct(tag, v, cap)
    except Reject as exc:
        raise Fail(exc.message, exc.category) from None


def tag_collection11(tag: str, v: Any, is_map: bool, e0: tuple) -> Any:
    try:
        return construct(tag, v)
    except Reject as exc:
        raise Fail(exc.message, exc.category, e0[2]) from None


def tagged_scalar_text(tag: str, v: str, cap: int | None, wide: dict[int, float]) -> Any:
    return v


def tag_collection_text(tag: str, v: Any, is_map: bool, e0: tuple) -> Any:
    return v
