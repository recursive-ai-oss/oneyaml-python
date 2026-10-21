"""The scanner of ruamel.yaml: the error class only."""

from .error import MarkedYAMLError, ScannerError

__all__ = ["MarkedYAMLError", "ScannerError"]
