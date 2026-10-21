"""Pure-Python backend; mirrors the internal surface of the native module."""

from __future__ import annotations

from .._errors import YAMLError
from ._construct import load
from ._decode import decode
from ._emit import dump
from ._events import render
from ._parser import DEFAULT_MAX_DEPTH, parse_events

__all__ = ["BACKEND", "ENGINE_VERSION", "dump", "events_text", "load", "probe"]

BACKEND = "pure"
ENGINE_VERSION: str | None = None


def probe(data: bytes) -> bool:
    """Check that data is a document this backend accepts; true when it is."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def events_text(data: bytes | str, max_depth: int = DEFAULT_MAX_DEPTH) -> list[str]:
    """The event stream of ``data`` in the yaml-test-suite tree notation.

    Raises ``YAMLError``; its ``events`` holds the lines produced before the error."""
    text = decode(data)
    try:
        events = parse_events(text, max_depth)
    except YAMLError as exc:
        raw = exc.__dict__.pop("raw_events", None)
        exc.events = render(raw) if raw is not None else []
        raise
    return render(events)
