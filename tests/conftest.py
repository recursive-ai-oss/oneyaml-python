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
    _mark_pure_documents(items)


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
    # The engine reports these at the start of the construct, not at the line break after the
    # properties.
    ("test_errors", "a: 1\n!!map\n!!str b: c\n"): (
        "own:diagnostics/properties-on-their-own-line-before-a-key-of-an-open-mapping"
    ),
    ("test_errors", "a: 1\n!!map\nb: 2\n"): (
        "own:diagnostics/properties-on-their-own-line-before-a-key-of-an-open-mapping"
    ),
    # The engine accepts DEL and C1 controls in comments.
    ("test_errors", "# c\x7f\na: 1\n"): "own:diagnostics/del-in-a-comment-is-an-error",
    ("test_errors", "# c\x9f\na: 1\n"): "own:diagnostics/c1-control-in-a-comment-is-an-error",
    ("test_syntax_errors_have_one_based_positions", "a: 1\n- b\n"): (
        "own:diagnostics/sequence-entry-after-a-block-mapping"
    ),
    # The engine reads a float key past the range of a double as infinity (or zero), so such
    # keys are duplicates of each other; the kit allows a limit error and forbids the duplicate.
    ("test_float_keys_outside_a_double_are_a_limit", "1e400: a\n.inf: b\n"): (
        "own:schema/finite-float-key-and-infinity-are-not-duplicates"
    ),
    ("test_float_keys_outside_a_double_are_a_limit", "1e400: a\n2e400: b\n"): (
        "own:schema/keys-of-overflowing-floats-with-different-values-are-not-duplicates"
    ),
    ("test_float_keys_outside_a_double_are_a_limit", "1e-400: a\n2e-400: b\n"): (
        "own:schema/keys-of-underflowing-floats-with-different-values-are-not-duplicates"
    ),
    ("test_events", '"\\ud83d\\ude00 \\x41 \\U0001F600 \\N\\_"\n'): (
        "own:schema/escaped-surrogate-pair-is-one-character"
    ),
    # One test over several inputs. The engine accounts for these of them: `- a` / ` -b: c`
    # (position-implicit-key-over-two-lines), `a: "\x4"` (hex-escape-with-a-non-hex-digit),
    # `? a` / `? b` / `c` (mapping-key-without-a-colon) and the escaped surrogate pair above.
    ("test_backends_agree_on_hard_input", None): (
        "own:diagnostics/position-implicit-key-over-two-lines, "
        "own:diagnostics/hex-escape-with-a-non-hex-digit, "
        "own:diagnostics/mapping-key-without-a-colon, "
        "own:schema/escaped-surrogate-pair-is-one-character"
    ),
}


_DEVIATION_MODULES = ("test_pure_events", "test_load", "test_load_edges")


def _native_engine_deviation(item):
    spec = getattr(item, "callspec", None)
    if item.module.__name__.split(".")[-1] not in _DEVIATION_MODULES:
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


# The pure backend has no lossless document yet. Strict, so the marks turn into failures (and are
# deleted) the day it has one.
def _mark_pure_documents(items):
    for item in items:
        spec = getattr(item, "callspec", None)
        if item.module.__name__.split(".")[-1].startswith("test_document") and (
            spec is not None and spec.id.startswith("pure")
        ):
            item.add_marker(
                pytest.mark.xfail(
                    strict=True,
                    raises=NotImplementedError,
                    reason="pure document not yet implemented",
                )
            )
