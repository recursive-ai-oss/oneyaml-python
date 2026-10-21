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
    _mark_native_engine_deviations(items)


# ---- the native engine's known deviations ------------------------------------------------------
# Inputs on which the native backend differs from the expected answer because the parsing engine
# does (error positions it reports at the start of a construct, a few inputs it accepts). Each
# entry names the kit case that lists the same engine deviation. The marks are strict: when a
# newer engine fixes one, the test passes, the run fails, and the entry is deleted here.
# Nothing goes on this list without a kit case.
NATIVE_ENGINE_DEVIATIONS = {
    # (test, input): kit case
    ("test_errors", 'a: "abc\n'): "own:diagnostics/unclosed-double-quote",
    ("test_errors", "a: 'abc"): "own:diagnostics/unclosed-single-quote-without-final-newline",
    ("test_errors", "a: b: c\n"): "own:diagnostics/second-colon-in-a-plain-scalar-value",
    ("test_errors", "a: b\n c: d\n"): "own:diagnostics/position-implicit-key-over-two-lines",
    ("test_errors", "a: b\r\n c: d\r\n"): "own:diagnostics/position-counts-crlf-as-one-line-break",
    ("test_errors", "a: b\r c: d\r"): "own:diagnostics/position-counts-a-bare-cr-as-a-line-break",
    ("test_errors", "a: 1\nb\nc: 2\n"): "own:diagnostics/mapping-key-without-a-colon",
    ("test_errors", "a: 1\n- b\n"): "own:diagnostics/sequence-entry-after-a-block-mapping",
    ("test_errors", 'a: "x\\qy"\n'): "own:diagnostics/invalid-escape-in-double-quotes",
    ("test_errors", 'a: "\\xZZ"\n'): "own:diagnostics/hex-escape-with-a-non-hex-digit",
    ("test_errors", 'a: "\\u12"\n'): "own:diagnostics/short-unicode-escape",
    ("test_errors", "a: |0\n x\n"): "own:diagnostics/zero-block-scalar-indentation-indicator",
    ("test_errors", "&a[b]\n"): "own:diagnostics/anchor-directly-before-flow-collection",
    ("test_errors", "{a:{}}\n"): "own:diagnostics/plain-key-with-adjacent-flow-value-is-invalid",
    ("test_errors", "{:{0}}\n"): "own:diagnostics/empty-key-with-adjacent-flow-value-is-invalid",
    ("test_errors", "!a%2 x\n"): "own:diagnostics/tag-percent-escape-with-one-hex-digit",
    ("test_errors", "- !<!> foo\n"): "own:diagnostics/verbatim-tag-of-a-lone-bang-is-invalid",
    ("test_errors", "a: &x &y b\n"): "own:diagnostics/second-anchor-on-one-node-is-an-error",
    (
        "test_errors",
        "%YAML 1.2\n%YAML 1.2\n---\n",
    ): "own:diagnostics/second-yaml-directive-is-an-error",
    ("test_errors", "'a''b"): "own:diagnostics/unclosed-single-quote-after-escaped-quote",
    ("test_events", '"\\ud83d\\ude00 \\x41 \\U0001F600 \\N\\_"\n'): (
        "own:schema/escaped-surrogate-pair-is-one-character"
    ),
    # One test over several inputs. The engine accounts for these of them: `- a` / ` -b: c`
    # (position-implicit-key-over-two-lines), `a: "\x4"` (hex-escape-with-a-non-hex-digit),
    # `? a` / `? b` / `c` (mapping-key-without-a-colon) and the escaped surrogate pair above.
    # The pure backend also rejects a comment after a byte order mark following a document end
    # marker (own:parse/bom-then-comment-after-document-end-marker), which keeps the test failing
    # until that is fixed too.
    ("test_backends_agree_on_hard_input", None): (
        "own:diagnostics/position-implicit-key-over-two-lines, "
        "own:diagnostics/hex-escape-with-a-non-hex-digit, "
        "own:diagnostics/mapping-key-without-a-colon, "
        "own:schema/escaped-surrogate-pair-is-one-character"
    ),
}


def _native_engine_deviation(item):
    spec = getattr(item, "callspec", None)
    if item.module.__name__.split(".")[-1] != "test_pure_events":
        return None
    if spec is not None and not spec.id.startswith("native"):
        return None
    text = spec.params.get("text") if spec is not None else None
    return NATIVE_ENGINE_DEVIATIONS.get((item.originalname, text))


def _mark_native_engine_deviations(items):
    for item in items:
        case = _native_engine_deviation(item)
        if case is not None:
            item.add_marker(
                pytest.mark.xfail(
                    strict=True, reason=f"the native engine's known deviation: {case}"
                )
            )
