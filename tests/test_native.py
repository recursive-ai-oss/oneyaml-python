"""What only the native backend has: its guard against panics, thread safety, key sharing."""

import sys
import sysconfig
import threading

import pytest

import oneyaml
from oneyaml import YAMLError

pytestmark = pytest.mark.native_only


@pytest.fixture
def native(monkeypatch):
    from oneyaml import _api, _native

    monkeypatch.setattr(_api, "_impl", _native)
    return _native


def test_a_panic_becomes_a_yaml_error(native):
    with pytest.raises(YAMLError) as e:
        native._panic_probe()
    assert e.value.category == "other"
    assert "internal error" in e.value.message
    assert "deliberate panic" in e.value.message
    # The interpreter is unharmed and the next call works.
    assert oneyaml.loads("a: 1") == {"a": 1}


def test_a_panic_is_not_the_pyo3_panic_exception(native):
    try:
        native._panic_probe()
    except BaseException as e:  # noqa: BLE001 - the point is what type arrives
        assert type(e).__name__ != "PanicException"
        assert isinstance(e, YAMLError)


def test_repeated_keys_share_one_string(native):
    rows = oneyaml.loads("- {name: a, size: 1}\n- {name: b, size: 2}\n- {name: c, size: 3}\n")
    keys = [k for row in rows for k in row]
    assert keys[0] is keys[2] is keys[4]
    assert keys[1] is keys[3] is keys[5]


def test_values_are_not_shared_between_loads(native):
    a = oneyaml.loads("k: [1]")
    b = oneyaml.loads("k: [1]")
    a["k"].append(2)
    assert b == {"k": [1]}


def test_wrong_argument_type_from_the_module_itself(native):
    with pytest.raises(TypeError):
        native.load(5, oneyaml.Limits(), False)
    with pytest.raises(TypeError):
        native.events_text(5)
    with pytest.raises(AttributeError):
        native.load("a", object(), False)


def test_native_module_state():
    from oneyaml import _native

    assert _native.BACKEND == "native"


FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def test_several_threads_load_at_once(native):
    text = "".join(f"k{i}: [{i}, {{a: &x{i} v{i}}}, *x{i}]\n" for i in range(200))
    expected = oneyaml.loads(text)
    bad = []

    def work():
        try:
            for _ in range(30):
                if oneyaml.loads(text) != expected:
                    bad.append("different result")
                try:
                    oneyaml.loads("a: [")
                except YAMLError:
                    pass
                else:
                    bad.append("no error")
        except BaseException as e:  # noqa: BLE001
            bad.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bad == []


def test_importing_the_module_keeps_the_gil_off_on_a_free_threaded_build():
    # The module declares it needs no GIL; if it did not, importing it would turn the GIL back on.
    if FREE_THREADED:
        assert not sys._is_gil_enabled()


def test_several_threads_load_big_documents_at_once(native):
    # Big input is read in stages on helper threads; concurrent loads must not mix up results.
    def doc(seed):
        return "".join(
            f"r{seed}_{i}: {{a: [{i}, {seed}], b: &x{i} v{seed}, c: *x{i}}}\n" for i in range(12000)
        )

    texts = [doc(s) for s in range(4)]
    assert all(len(t) > 300_000 for t in texts)
    expected = [oneyaml.loads(t) for t in texts]
    bad = []

    def work(n):
        try:
            for _ in range(5):
                if oneyaml.loads(texts[n]) != expected[n]:
                    bad.append(f"mixed result in {n}")
                try:
                    oneyaml.loads(texts[n] + "x: [\n")
                except YAMLError:
                    pass
                else:
                    bad.append("no error")
        except BaseException as e:  # noqa: BLE001
            bad.append(repr(e))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bad == []
