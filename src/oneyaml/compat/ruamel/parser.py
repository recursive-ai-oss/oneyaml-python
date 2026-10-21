"""The parser of ruamel.yaml: the error class only."""

from .error import MarkedYAMLError, ParserError

__all__ = ["MarkedYAMLError", "ParserError"]
