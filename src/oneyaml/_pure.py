"""Pure-Python backend; mirrors the internal surface of the native module."""

from __future__ import annotations

BACKEND = "pure"
ENGINE_VERSION: str | None = None


def probe(data: bytes) -> bool:
    """Check that data is a document this backend accepts; true when it is."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
