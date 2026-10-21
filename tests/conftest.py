import importlib
import os
from types import SimpleNamespace

import pytest

import oneyaml
from oneyaml import _api

# ONEYAML_TEST_BACKENDS=pure restricts the run to the pure backend, for interpreters that
# have no native wheel. By default both run, and a missing native module is a failure.
BACKENDS = tuple(os.environ.get("ONEYAML_TEST_BACKENDS", "native,pure").split(","))
HAS_NATIVE = "native" in BACKENDS

# Test modules whose pure-backend runs wait for the pure reader. They are expected to fail with
# NotImplementedError (strictly: once the pure backend passes them the run reports it, and the
# entry is deleted from this list).
PURE_NOT_YET_IMPLEMENTED = {
    "test_load",
    "test_events",
}


def pytest_configure(config):
    config.addinivalue_line("markers", "native_only: needs the native module")
    config.addinivalue_line("markers", "slow: stress and scaling tests, left out of the fast tier")


@pytest.fixture(params=BACKENDS)
def impl(request):
    """Each backend module in turn."""
    return importlib.import_module(f"oneyaml._{request.param}")


@pytest.fixture(params=BACKENDS)
def api(request, monkeypatch):
    """The public reading functions, running on each backend in turn."""
    mod = importlib.import_module(f"oneyaml._{request.param}")
    monkeypatch.setattr(_api, "_impl", mod)

    def events_text(data):
        try:
            fn = mod.events_text
        except AttributeError:
            raise NotImplementedError("not yet implemented on the pure backend") from None
        return fn(data)

    return SimpleNamespace(
        name=request.param,
        impl=mod,
        loads=oneyaml.loads,
        loads_all=oneyaml.loads_all,
        load=oneyaml.load,
        load_all=oneyaml.load_all,
        events_text=events_text,
    )


def pytest_collection_modifyitems(items):
    if not HAS_NATIVE:
        items[:] = [i for i in items if "native_only" not in i.keywords]
    for item in items:
        params = getattr(getattr(item, "callspec", None), "params", {})
        if params.get("api") == "pure" and item.module.__name__.split(".")[-1] in (
            PURE_NOT_YET_IMPLEMENTED
        ):
            item.add_marker(
                pytest.mark.xfail(
                    raises=NotImplementedError,
                    strict=True,
                    reason="not yet implemented on the pure backend",
                )
            )
