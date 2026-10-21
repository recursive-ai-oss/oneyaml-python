"""Values of the YAML 1.1 types, built the way PyYAML's safe loader builds them.

Both backends resolve plain scalars themselves (that is the hot path). This module holds what
is shared and rare: the conversion of explicitly tagged values, and of dates, sets, ordered
maps and pairs. A backend calls :func:`construct` with a tag and the node's content (the text
for a scalar, the built list or mapping for a collection).
"""

from __future__ import annotations

import base64
import binascii
import datetime
import re
from typing import Any

from ._types import FrozenMapping, big_int

__all__ = [
    "Reject",
    "construct",
    "est_digits",
    "est_digits60",
    "sexagesimal_float",
    "sexagesimal_int",
    "timestamp",
]

_YAML = "tag:yaml.org,2002:"


class Reject(Exception):
    """A value does not fit its tag, or the tag cannot be built. A backend adds the position."""

    def __init__(self, message: str, category: str = "construct") -> None:
        super().__init__(message)
        self.message = message
        self.category = category


_TIMESTAMP = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{1,2})-(?P<day>[0-9]{1,2})"
    r"(?:(?:[Tt]|[ \t]+)(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]*))?"
    r"(?:[ \t]*(?P<tz>Z|(?P<tz_sign>[-+])(?P<tz_hour>[0-9]{1,2})(?::(?P<tz_minute>[0-9]{2}))?))?"
    r")?\Z"
)


def timestamp(v: str) -> datetime.date | datetime.datetime:
    """A date, or a date and time (aware when a zone is written)."""
    m = _TIMESTAMP.match(v)
    if m is None:
        raise Reject(f"{v!r} is not a valid timestamp")
    g = m.groupdict()
    try:
        if g["hour"] is None:
            return datetime.date(int(g["year"]), int(g["month"]), int(g["day"]))
        fraction = int((g["fraction"] or "")[:6].ljust(6, "0"))
        tz = None
        if g["tz_sign"]:
            delta = datetime.timedelta(hours=int(g["tz_hour"]), minutes=int(g["tz_minute"] or 0))
            tz = datetime.timezone(-delta if g["tz_sign"] == "-" else delta)
        elif g["tz"]:
            tz = datetime.timezone.utc
        return datetime.datetime(
            int(g["year"]), int(g["month"]), int(g["day"]),
            int(g["hour"]), int(g["minute"]), int(g["second"]), fraction, tzinfo=tz,
        )  # fmt: skip
    except (ValueError, OverflowError) as exc:
        raise Reject(f"{v!r} is not a valid timestamp: {exc}") from None


def est_digits(base: int, n: int) -> int:
    """The decimal digits an integer of ``n`` digits (no leading zeros) in ``base`` counts as
    against the digit limit: exact for base 10, an upper bound from the bit length otherwise.
    The native backend uses the same figures."""
    if base == 10:
        return max(n, 1)
    bits = n * {2: 1, 8: 3, 16: 4}[base]
    return bits * 30103 // 100_000 + 1


def est_digits60(first: int, parts: int) -> int:
    """The same for a base-60 integer: ``first`` digits in the first of ``parts`` parts."""
    return first + (parts - 1) * 17782 // 10_000 + 1


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        # Only a long run of digits is worth a second try: the interpreter converts at most a few
        # thousand digits at once by default.
        if len(text) > 4000 and text.isascii() and text.lstrip("+-").isdigit():
            return big_int(text)
        raise


def sexagesimal_int(text: str) -> int:
    """An integer written in base 60 (``1:30:15``), sign included, underscores already gone."""
    neg = text.startswith("-")
    value = 0
    for part in text.lstrip("+-").split(":"):
        value = value * 60 + _int(part)
    return -value if neg else value


def _check_digits(est: int, cap: int | None) -> None:
    if cap is not None and est > cap:
        raise Reject(f"an integer of {est} digits is over the limit of {cap} digits", "limit")


def _int_value(text: str, cap: int | None = None) -> int:
    """PyYAML's reading of ``!!int`` text, which is more lenient than its implicit rule.
    ``cap`` is the most decimal digits the integer may have (None for any)."""
    v = text.replace("_", "")
    if not v:
        raise Reject("an empty !!int")
    sign = -1 if v[0] == "-" else 1
    if v[0] in "+-":
        v = v[1:]
    try:
        if v == "0":
            return 0
        if v.startswith(("0b", "0x")):
            base = 2 if v[1] == "b" else 16
            _check_digits(est_digits(base, len(v[2:].lstrip("0"))), cap)
            return sign * int(v[2:], base)
        if v.startswith("0"):
            _check_digits(est_digits(8, len(v.lstrip("0"))), cap)
            return sign * int(v, 8)
        if ":" in v:
            parts = v.split(":")
            _check_digits(est_digits60(len(parts[0]), len(parts)), cap)
            return sign * sexagesimal_int(v)
        _check_digits(len(v), cap)
        return sign * int(v)
    except ValueError as exc:
        if "digits" in str(exc) and "limit" in str(exc):
            raise Reject(str(exc), "limit") from None
        raise Reject(f"{text!r} is not a valid !!int") from None


_NAN = float("nan")


def sexagesimal_float(parts: list[float]) -> float:
    """PyYAML's base-60 float formula, ``sum(part * 60**k)`` from the right, without ever
    overflowing: a value past the range of a double is infinity. No part is below zero, so no
    step can make a NaN."""
    value = 0.0
    weight = 1
    for k, part in enumerate(reversed(parts)):
        if part:
            if k > 175:  # 60**175 is past the range of a double; one nonzero part is enough
                return float("inf")
            try:
                value += part * weight
            except OverflowError:
                return float("inf")
        if k <= 175:
            weight *= 60
    return value


def _float_value(text: str) -> float:
    v = text.replace("_", "").lower()
    if not v:
        raise Reject("an empty !!float")
    sign = -1.0 if v[0] == "-" else 1.0
    if v[0] in "+-":
        v = v[1:]
    try:
        if v == ".inf":
            return sign * float("inf")
        if v == ".nan":
            return _NAN
        if ":" in v:
            return sign * sexagesimal_float([float(p) for p in v.split(":")])
        return sign * float(v)
    except ValueError:
        raise Reject(f"{text!r} is not a valid !!float") from None


_BOOLS = {"yes": True, "no": False, "true": True, "false": False, "on": True, "off": False}


def _kind(value: Any) -> str:
    if isinstance(value, str):
        return "scalar"
    if isinstance(value, (list, tuple)):
        return "sequence"
    return "mapping"


def _want(tag: str, value: Any, kind: str) -> None:
    found = _kind(value)
    if found != kind:
        raise Reject(f"the tag {tag} needs a {kind}, but this is a {found}")


def _pairs(tag: str, value: Any, what: str) -> Any:
    _want(tag, value, "sequence")
    out = []
    for item in value:
        if not isinstance(item, (dict, FrozenMapping)):
            raise Reject(f"{what}: expected a mapping of length 1, but found {_kind(item)}")
        if len(item) != 1:
            raise Reject(f"{what}: expected a single mapping item, but found {len(item)} items")
        ((k, v),) = item.items()
        out.append((k, v))
    # A sequence inside a mapping key arrives as a tuple and has to stay hashable.
    return tuple(out) if type(value) is tuple else out


def construct(tag: str, value: Any, cap: int | None = None) -> Any:
    """The value a tag stands for.

    ``value`` is the scalar text or the collection built from the node. Anything but the
    standard tags is refused: nothing is ever built from a tag by running code. ``cap`` is the
    most decimal digits an integer may have (None for any)."""
    if tag.startswith(_YAML):
        kind = tag[len(_YAML) :]
        if kind == "str":
            _want(tag, value, "scalar")
            return value
        if kind == "null":
            _want(tag, value, "scalar")
            return None
        if kind == "bool":
            _want(tag, value, "scalar")
            try:
                return _BOOLS[value.lower()]
            except KeyError:
                raise Reject(f"{value!r} is not a valid !!bool") from None
        if kind == "int":
            _want(tag, value, "scalar")
            return _int_value(value, cap)
        if kind == "float":
            _want(tag, value, "scalar")
            return _float_value(value)
        if kind == "binary":
            _want(tag, value, "scalar")
            try:
                return base64.decodebytes(value.encode("ascii"))
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise Reject(f"failed to decode base64 data: {exc}") from None
        if kind == "timestamp":
            _want(tag, value, "scalar")
            return timestamp(value)
        if kind == "map":
            _want(tag, value, "mapping")
            return value
        if kind == "seq":
            _want(tag, value, "sequence")
            return value
        if kind == "set":
            _want(tag, value, "mapping")
            return frozenset(value) if type(value) is FrozenMapping else set(value)
        if kind == "omap":
            return _pairs(tag, value, "while constructing an ordered map")
        if kind == "pairs":
            return _pairs(tag, value, "while constructing pairs")
        if kind.startswith("python/"):
            raise Reject(
                f"the tag {tag} would run Python code and is never built; "
                "remove the tag or load the file with a loader of your own"
            )
    elif tag == "!":
        return value
    raise Reject(f"could not determine a constructor for the tag {tag!r}")
