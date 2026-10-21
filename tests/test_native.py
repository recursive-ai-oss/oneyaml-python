"""What only the native backend has: its guard against panics, thread safety, key sharing."""

import os
import signal
import subprocess
import sys
import sysconfig
import threading
import time

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


# ---- stages, threads, signals, crashes ---------------------------------------------------------

(OFF, PANIC_IN_PARSER, PANIC_IN_TRANSLATOR, REFUSE_THREADS, INTERRUPT) = range(5)


_NATIVE_ENV = {**os.environ, "ONEYAML_BACKEND": "native"}


def _big():
    text = "".join(f"k{i}: [a{i}, {{b: c{i}}}]\n" for i in range(20000))
    assert len(text) > 128 * 1024
    return text


def _threads():
    # The helper threads are native ones, which `threading` does not list.
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        pytest.skip("needs /proc")


def _no_thread_left(before):
    # A joined thread's entry can linger in /proc for a moment after the join returns.
    for _ in range(200):
        if _threads() <= before:
            return
        time.sleep(0.005)
    assert _threads() <= before


@pytest.fixture
def hook(native):
    yield native._test_hook
    native._test_hook(OFF)


@pytest.mark.parametrize("stage", [PANIC_IN_PARSER, PANIC_IN_TRANSLATOR])
def test_a_panic_in_a_pipeline_stage_becomes_a_yaml_error(native, hook, stage):
    text = _big()
    expected = oneyaml.loads(text)
    before = _threads()
    hook(stage)
    with pytest.raises(YAMLError) as e:
        oneyaml.loads(text)
    assert e.value.category == "other"
    assert "deliberate panic" in e.value.message
    hook(OFF)
    _no_thread_left(before)
    assert oneyaml.loads(text) == expected


def test_big_input_is_read_on_one_thread_when_the_os_refuses_threads(native, hook):
    text = _big()
    expected = oneyaml.loads(text)
    with pytest.raises(YAMLError) as normal:
        oneyaml.loads(text + "x: [\n")
    hook(REFUSE_THREADS)
    assert oneyaml.loads(text) == expected
    with pytest.raises(YAMLError) as e:
        oneyaml.loads(text + "x: [\n")
    assert (e.value.category, e.value.line, e.value.column) == (
        normal.value.category,
        normal.value.line,
        normal.value.column,
    )


def test_big_input_loads_when_the_process_may_start_no_threads():
    code = (
        "import resource, oneyaml\n"
        "text = ''.join(f'k{i}: [a{i}, {{b: c{i}}}]\\n' for i in range(20000))\n"
        "resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))\n"
        "print(len(oneyaml.loads(text)))\n"
    )
    if sys.platform != "linux":
        pytest.skip("setrlimit(RLIMIT_NPROC) is checked on Linux")
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=_NATIVE_ENV
    )
    assert done.stdout.strip() == "20000", done.stderr[-500:]


def _interrupted(text, hook, probe=None):
    hook(INTERRUPT)

    def handler(signum, frame):
        if probe is not None:
            probe()
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGINT, handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            oneyaml.loads(text)
    finally:
        signal.signal(signal.SIGINT, old)
        hook(OFF)


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signals reach the main thread",
)
@pytest.mark.parametrize("size", ["small", "big"])
def test_a_pending_signal_ends_a_load_and_leaves_no_thread(native, hook, size):
    text = "- 1\n" * 6000 if size == "small" else _big()
    before = _threads()
    _interrupted(text, hook)
    _no_thread_left(before)
    assert oneyaml.loads(text) == oneyaml.loads(text)


def test_the_input_buffer_is_released_before_the_load_runs(native, hook):
    # While a bytearray is exported it cannot change size; the load copies it and lets go.
    buf = bytearray(b"- 1\n" * 6000)
    seen = []

    def probe():
        try:
            buf.append(10)
            seen.append("released")
        except BufferError:
            seen.append("still held")

    _interrupted(buf, hook, probe)
    assert seen == ["released"]


def test_many_leading_byte_order_marks_do_not_overflow_the_stack():
    code = (
        "import oneyaml\n"
        "try:\n"
        "    oneyaml.loads('\\ufeff' * 100000 + 'a: [\\n')\n"
        "except oneyaml.YAMLError as e:\n"
        "    print(e.category)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=_NATIVE_ENV
    )
    assert done.stdout.strip() == "syntax", (done.returncode, done.stderr[-300:])


def test_probe_still_answers(native):
    assert native.probe(b"a: 1\n") is True
    assert native.probe(b"a: [\n") is False
    assert native.probe(b"\xff") is False
