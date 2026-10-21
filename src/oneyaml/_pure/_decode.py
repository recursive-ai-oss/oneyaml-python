"""Turn the bytes of a YAML stream into text (YAML 1.2.2 section 5.2)."""

from __future__ import annotations

import re

from .._errors import make_error

_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
_BREAKS = re.compile(r"\r\n|\r|\n")
_SURROGATE = re.compile("[\ud800-\udfff]").search


def detect(data: bytes) -> tuple[str, int]:
    """The codec of a stream and the length of its byte order mark."""
    for bom, codec in _BOMS:
        if data.startswith(bom):
            return codec, len(bom)
    head = data[:4]
    if len(head) == 4:
        if not head[0] and not head[1] and not head[2] and head[3]:
            return "utf-32-be", 0
        if head[0] and not head[1] and not head[2] and not head[3]:
            return "utf-32-le", 0
    if len(head) >= 2:
        if not head[0] and head[1]:
            return "utf-16-be", 0
        if head[0] and not head[1]:
            return "utf-16-le", 0
    return "utf-8", 0


def decode(data: bytes | bytearray | memoryview | str) -> str:
    """Decode a stream; a byte order mark is removed. Malformed input raises ``encoding``."""
    if isinstance(data, str):
        # A lone surrogate cannot be written as UTF-8, so the string is not text.
        if not data.isascii() and _SURROGATE(data):
            raise make_error("the string holds a lone surrogate, which is not text", "encoding")
        return data[1:] if data.startswith("\ufeff") else data
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"YAML input must be bytes or str, not {type(data).__name__}")
    data = bytes(data)
    codec, skip = detect(data)
    body = data[skip:] if skip else data
    try:
        return body.decode(codec)
    except UnicodeDecodeError as exc:
        before = body[: exc.start].decode(codec, errors="ignore")
        breaks = _BREAKS.findall(before)
        if breaks:
            line = 1 + len(breaks)
            column = len(_BREAKS.split(before)[-1]) + 1
        else:
            line, column = 1, len(before) + 1
        raise make_error(f"invalid {codec} input: {exc.reason}", "encoding", line, column) from None
