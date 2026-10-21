import importlib
import os

import pytest

# ONEYAML_TEST_BACKENDS=pure restricts the run to the pure backend, for interpreters that
# have no native wheel. By default both run, and a missing native module is a failure.
BACKENDS = tuple(os.environ.get("ONEYAML_TEST_BACKENDS", "native,pure").split(","))
HAS_NATIVE = "native" in BACKENDS


def pytest_configure(config):
    config.addinivalue_line("markers", "native_only: needs the native module")


@pytest.fixture(params=BACKENDS)
def impl(request):
    """Each backend module in turn."""
    return importlib.import_module(f"oneyaml._{request.param}")


def pytest_collection_modifyitems(items):
    if not HAS_NATIVE:
        items[:] = [i for i in items if "native_only" not in i.keywords]
