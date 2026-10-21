"""A drop-in for ruamel.yaml 0.19: ``from oneyaml.compat.ruamel import YAML``.

The package mirrors ruamel's: ``comments``, ``scalarstring``, ``error`` (and the ``scanner``,
``parser``, ``constructor``, ``composer`` and ``reader`` error modules) are here under the same
names, so switching a program is replacing ``ruamel.yaml`` with ``oneyaml.compat.ruamel`` in
its imports. See :mod:`oneyaml.compat.ruamel.main` for how dumping works and what differs.
"""

from . import comments, compat, error, scalarstring
from .comments import CommentedMap, CommentedSeq
from .error import (
    ComposerError,
    ConstructorError,
    DuplicateKeyError,
    MarkedYAMLError,
    ParserError,
    ReaderError,
    RepresenterError,
    ScannerError,
    YAMLError,
)
from .main import YAML
from .scalarstring import (
    DoubleQuotedScalarString,
    FoldedScalarString,
    LiteralScalarString,
    PlainScalarString,
    PreservedScalarString,
    SingleQuotedScalarString,
)

__version__ = "0.19.1"
__with_libyaml__ = False
version_info = (0, 19, 1)

__all__ = [
    "YAML",
    "CommentedMap",
    "CommentedSeq",
    "ComposerError",
    "ConstructorError",
    "DoubleQuotedScalarString",
    "DuplicateKeyError",
    "FoldedScalarString",
    "LiteralScalarString",
    "MarkedYAMLError",
    "ParserError",
    "PlainScalarString",
    "PreservedScalarString",
    "ReaderError",
    "RepresenterError",
    "ScannerError",
    "SingleQuotedScalarString",
    "YAMLError",
    "comments",
    "compat",
    "error",
    "scalarstring",
]
