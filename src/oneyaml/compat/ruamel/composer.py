"""The composer of ruamel.yaml: the error class only."""

from .error import ComposerError, MarkedYAMLError

__all__ = ["MarkedYAMLError", "ComposerError"]
