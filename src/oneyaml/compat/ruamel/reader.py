"""The reader of ruamel.yaml: the error class only."""

from .error import MarkedYAMLError, ReaderError

__all__ = ["MarkedYAMLError", "ReaderError"]
