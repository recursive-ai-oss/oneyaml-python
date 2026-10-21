"""A YAML 1.2 library with a native and a pure-Python backend.

The native backend is used when its module imports, the pure one otherwise.
Set ONEYAML_BACKEND to ``auto`` (default), ``native`` or ``pure`` to choose.
"""

from __future__ import annotations

import os
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from types import ModuleType

from . import _api
from ._api import dump, dump_all, dumps, dumps_all, load, load_all, loads, loads_all
from ._errors import LimitError, ParseError, YAMLError
from ._limits import Limits
from ._types import FrozenMapping, Tagged

try:
    __version__ = version("oneyaml")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0+unknown"

__all__ = [
    "FrozenMapping",
    "LimitError",
    "Limits",
    "ParseError",
    "Tagged",
    "YAMLError",
    "__version__",
    "backend",
    "dump",
    "dump_all",
    "dumps",
    "dumps_all",
    "engine_version",
    "load",
    "load_all",
    "loads",
    "loads_all",
]

_CHOICES = ("auto", "native", "pure")


def _select() -> ModuleType:
    want = os.environ.get("ONEYAML_BACKEND", "auto").strip().lower() or "auto"
    if want not in _CHOICES:
        raise ValueError(f"ONEYAML_BACKEND must be one of {', '.join(_CHOICES)}; got {want!r}")
    if want != "pure":
        try:
            return import_module("oneyaml._native")
        except ImportError as exc:
            if want == "native":
                raise ImportError(
                    "ONEYAML_BACKEND=native, but the native module could not be imported "
                    f"({exc}). Install a oneyaml wheel built for this platform, "
                    "or unset ONEYAML_BACKEND to use the pure-Python backend."
                ) from exc
    return import_module("oneyaml._pure")


_impl = _select()
_api._impl = _impl

#: Name of the active backend: ``"native"`` or ``"pure"``.
backend: str = _impl.BACKEND


def engine_version() -> str | None:
    """Version of the parsing engine behind the native backend; None for the pure one."""
    return _impl.ENGINE_VERSION
