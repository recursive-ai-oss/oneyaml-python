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

    return SimpleNamespace(
        name=request.param,
        impl=mod,
        loads=oneyaml.loads,
        loads_all=oneyaml.loads_all,
        load=oneyaml.load,
        load_all=oneyaml.load_all,
        events_text=mod.events_text,
    )


def pytest_collection_modifyitems(items):
    if not HAS_NATIVE:
        items[:] = [i for i in items if "native_only" not in i.keywords]
    _mark_native_engine_deviations(items)


# ---- the native engine's known deviations ------------------------------------------------------
# Inputs on which the native backend differs from the expected answer because the parsing engine
# does. Each entry says why. The marks are strict: when a newer engine fixes one, the test
# passes, the run fails, and the entry is deleted here.
NATIVE_ENGINE_DEVIATIONS = {
    # (test, input): why
    # The engine reads a float key past the range of a double as infinity (or zero), so such
    # keys are duplicates of each other; the kit allows a limit error and forbids the duplicate
    # (its cases for this are patches, which match keys by text and pass; none loads the keys).
    ("test_float_keys_outside_a_double_are_a_limit", "1e400: a\n.inf: b\n"): (
        "own:schema/finite-float-key-and-infinity-are-not-duplicates"
    ),
    ("test_float_keys_outside_a_double_are_a_limit", "1e400: a\n2e400: b\n"): (
        "own:schema/keys-of-overflowing-floats-with-different-values-are-not-duplicates"
    ),
    ("test_float_keys_outside_a_double_are_a_limit", "1e-400: a\n2e-400: b\n"): (
        "own:schema/keys-of-underflowing-floats-with-different-values-are-not-duplicates"
    ),
    # One test over several inputs. `? a` / `? b` / `c`: the engine stops before the empty value
    # of the last key, the pure parser reports it first (the error is the same).
    ("test_backends_agree_on_hard_input", None): "own:diagnostics/mapping-key-without-a-colon",
}


# Document tests on which the native engine differs (the pure backend passes them).
NATIVE_DOCUMENT_DEVIATIONS = {}

_DEVIATION_MODULES = ("test_pure_events", "test_load", "test_load_edges")


def _native_engine_deviation(item):
    spec = getattr(item, "callspec", None)
    if item.module.__name__.split(".")[-1] not in _DEVIATION_MODULES:
        return None
    if spec is not None and not spec.id.startswith("native"):
        return None
    if spec is None and not HAS_NATIVE:
        return None  # a test over both backends has nothing to deviate from
    text = spec.params.get("text") if spec is not None else None
    return NATIVE_ENGINE_DEVIATIONS.get((item.originalname, text))


def _mark_native_engine_deviations(items):
    for item in items:
        spec = getattr(item, "callspec", None)
        why = NATIVE_DOCUMENT_DEVIATIONS.get(item.originalname)
        if why and spec is not None and spec.id.startswith("native"):
            item.add_marker(pytest.mark.xfail(strict=True, reason=why))
        case = _native_engine_deviation(item)
        if case is not None:
            item.add_marker(
                pytest.mark.xfail(
                    strict=True, reason=f"the native engine's known deviation: {case}"
                )
            )
