"""The yaml-test-suite event notation for parser events."""

from __future__ import annotations

from ._parser import (
    ALIAS,
    DOC_END,
    DOC_START,
    MAP_END,
    MAP_START,
    SCALAR,
    SEQ_END,
    SEQ_START,
    STREAM_END,
    STREAM_START,
)

_ESCAPES = str.maketrans(
    {"\\": "\\\\", "\0": "\\0", "\b": "\\b", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
)
_STYLE = {":": ":", "'": "'", '"': '"', "|": "|", ">": ">"}


def render(events) -> list[str]:
    """Event tuples as the lines of the test-suite tree notation."""
    out: list[str] = []
    add = out.append
    for e in events:
        k = e[0]
        if k == SCALAR:
            anchor, tag = e[4], e[5]
            head = "=VAL"
            if anchor:
                head += " &" + anchor
            if tag:
                head += " <" + tag + ">"
            add(f"{head} {e[6]}{e[7].translate(_ESCAPES)}")
        elif k in (MAP_START, SEQ_START):
            head = "+MAP" if k == MAP_START else "+SEQ"
            if e[5]:
                head += " {}" if k == MAP_START else " []"
            if e[3]:
                head += " &" + e[3]
            if e[4]:
                head += " <" + e[4] + ">"
            add(head)
        elif k == MAP_END:
            add("-MAP")
        elif k == SEQ_END:
            add("-SEQ")
        elif k == ALIAS:
            add("=ALI *" + e[3])
        elif k == DOC_START:
            add("+DOC ---" if e[3] else "+DOC")
        elif k == DOC_END:
            add("-DOC ..." if e[3] else "-DOC")
        elif k == STREAM_START:
            add("+STR")
        elif k == STREAM_END:
            add("-STR")
    return out
