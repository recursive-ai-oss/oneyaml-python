import os
import subprocess
import sys

import pytest

import oneyaml

native_only = pytest.mark.native_only


def test_names(impl):
    assert impl.BACKEND in ("native", "pure")
    assert impl.__name__.endswith("_" + impl.BACKEND)


def test_probe_accepts_text_and_rejects_bad_utf8(impl):
    assert impl.probe(b"a: 1\nb: [x, y]\n") is True
    assert impl.probe(b"\xff\xfe") is False


@native_only
def test_native_links_engine():
    from oneyaml import _native

    assert _native.ENGINE_VERSION[0].isdigit()
    assert _native.probe(b"a: 1\n") is True
    # The engine, not just a UTF-8 check, rejects a malformed document.
    assert _native.probe(b"a: [1, 2\n") is False


def test_pure_has_no_engine():
    from oneyaml import _pure

    assert _pure.ENGINE_VERSION is None


def _run(backend):
    env = {k: v for k, v in os.environ.items() if k != "ONEYAML_BACKEND"}
    if backend is not None:
        env["ONEYAML_BACKEND"] = backend
    code = "import oneyaml; print(oneyaml.backend, oneyaml.engine_version())"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("choice", [pytest.param("native", marks=native_only), "pure"])
def test_env_forces_backend(choice):
    r = _run(choice)
    assert r.returncode == 0, r.stderr
    name, engine = r.stdout.split()
    assert name == choice
    assert (engine == "None") == (choice == "pure")


@native_only
def test_auto_prefers_native():
    for value in (None, "auto", "AUTO"):
        r = _run(value)
        assert r.returncode == 0, r.stderr
        assert r.stdout.split()[0] == "native"


def test_bad_value_is_rejected():
    r = _run("fast")
    assert r.returncode != 0
    assert "ONEYAML_BACKEND must be one of" in r.stderr


def test_active_backend_matches_env():
    want = os.environ.get("ONEYAML_BACKEND", "auto")
    # A run restricted to the pure backend has no native module to pick.
    has_native = "native" in os.environ.get("ONEYAML_TEST_BACKENDS", "native,pure").split(",")
    assert oneyaml.backend == ("pure" if want == "pure" or not has_native else "native")
    assert isinstance(oneyaml.__version__, str)
