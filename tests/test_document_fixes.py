"""Regressions found by probing the lossless document, each as the smallest input that showed it."""

import os
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence

import pytest

import oneyaml
from oneyaml import LimitError, Limits, StaleViewError, YAMLError

# ---- aliases to anchors outside a view ----


def nested_outside(n):
    opens = "".join(f"&a{i} [" for i in range(n)) + "]" * n
    use = ", ".join(f"*a{i}" for i in reversed(range(n)))
    return f"a: {opens}\nb: [{use}]\n"


def test_a_view_charges_aliases_to_outside_anchors_as_loads_does(api):
    text = nested_outside(3000)
    with pytest.raises(LimitError):
        oneyaml.loads(text)
    doc = oneyaml.parse(text)
    with pytest.raises(LimitError) as e:
        doc["b"].to_python()
    assert e.value.category == "limit"


def test_a_view_of_outside_anchors_reads_as_loads_does(api):
    text = nested_outside(40)
    doc = oneyaml.parse(text)
    assert doc["b"].to_python() == oneyaml.loads(text)["b"]
    whole = doc["b"].to_python()
    assert whole[0] is whole[1][0] if isinstance(whole[1], list) and whole[1] else True


@pytest.mark.parametrize(
    "text",
    [
        "x: &a 0\nz: &b [*a]\ny: [&a 1, *b]\n",
        "x: &a 0\nz: &b {k: *a}\ny: {p: &a 1, q: *b}\n",
        "x: &a 0\ny: [&a 1, *a, &a 2, *a]\n",
    ],
)
def test_a_name_defined_again_inside_a_view_does_not_capture_outside_aliases(api, text):
    whole = oneyaml.loads(text)
    doc = oneyaml.parse(text)
    assert doc["y"].to_python() == whole["y"]
    assert doc.to_python()["y"] == whole["y"]


def test_a_view_never_refuses_what_the_whole_read_accepts(api):
    text = "big: [" + ",".join(["0"] * 2000) + "]\nanc: &a [" + ",".join(["0"] * 100) + "]\n"
    text += "v: [" + ",".join(["*a"] * 20) + "]\n"
    limits = Limits(alias_nodes=(10, 0), alias_bytes=(10, 0))
    assert len(oneyaml.loads(text, limits=limits)["v"]) == 20
    doc = oneyaml.parse(text, limits=limits)
    assert len(doc.to_python()["v"]) == 20
    assert len(doc["v"].to_python()) == 20
    assert len(doc.at("/v").to_python()) == 20


# ---- staleness cost ----


class Counting:
    """Wraps the backend handle and counts calls by name."""

    def __init__(self, h):
        self._h, self.calls = h, {}

    def __getattr__(self, name):
        attr = getattr(self._h, name)
        if not callable(attr):
            return attr

        def call(*a):
            self.calls[name] = self.calls.get(name, 0) + 1
            return attr(*a)

        return call


class CountingDict(dict):
    reads = 0

    def get(self, *a):
        type(self).reads += 1
        return super().get(*a)

    def __getitem__(self, k):
        type(self).reads += 1
        return super().__getitem__(k)

    def __contains__(self, k):
        type(self).reads += 1
        return super().__contains__(k)


def test_a_staleness_check_does_not_grow_with_the_number_of_edits(api):
    doc = oneyaml.parse("l: [" + ", ".join(["0"] * 300) + "]\nother: {k: 1}\n")
    other = doc["other"]
    core = doc._core
    for _ in range(300):
        del doc["l"][0]
    # Whatever the bookkeeping is, looking at an untouched view reads a few entries, not all.
    for name in ("dead", "shifts"):
        if hasattr(core, name):
            setattr(core, name, CountingDict(getattr(core, name)))
    CountingDict.reads = 0
    assert other["k"] == 1
    assert CountingDict.reads <= 12
    assert not hasattr(core, "log") or len(core.log) <= 1


def test_clearing_asks_the_backend_for_the_keys_once(api):
    doc = oneyaml.parse("m: {" + ", ".join(f"k{i}: 0" for i in range(50)) + "}\nl: [1, 2, 3]\n")
    spy = Counting(doc._core.h)
    doc._core.h = spy
    doc["m"].clear()
    assert spy.calls.get("keys", 0) <= 1
    assert spy.calls.get("remove", 0) == 50
    spy.calls.clear()
    doc["l"].clear()
    assert spy.calls.get("remove", 0) == 3 and spy.calls.get("keys", 0) == 0
    assert doc.to_python() == {"m": {}, "l": []}


def test_popitem_and_clear_keep_their_meaning(api):
    doc = oneyaml.parse("m: {a: 1, b: 2}\n")
    m = doc["m"]
    k, v = m.popitem()
    assert (k, v) in (("a", 1), ("b", 2)) and len(m) == 1
    m.clear()
    assert len(m) == 0
    with pytest.raises(KeyError):
        m.popitem()


def test_views_survive_unrelated_edits_after_many_edits(api):
    doc = oneyaml.parse("a: {x: 1}\nl: [1, 2, 3, 4]\n")
    a, item = doc["a"], doc["l"]
    for _ in range(3):
        del doc["l"][0]
    assert a["x"] == 1 and item.to_python() == [4]
    first = doc["l"]
    doc["l"].append(5)
    assert first.to_python() == [4, 5]


# ---- tokens, indexes, keys ----


def test_integer_keys_beyond_64_bits_are_addressed_by_value(api):
    doc = oneyaml.parse("a: 1\n1180591620717411303424: big\n-1180591620717411303424: neg\nl: [1]\n")
    assert doc[2**70] == "big" and doc[-(2**70)] == "neg"
    assert 2**70 in doc and 2**80 not in doc and -(2**80) not in doc
    assert doc.get(2**80, "none") == "none"
    with pytest.raises(KeyError):
        doc[2**80]
    doc[2**80] = "new"
    assert oneyaml.loads(str(doc))[2**80] == "new"
    doc[2**70] = "changed"
    del doc[-(2**70)]
    assert oneyaml.loads(str(doc))[2**70] == "changed" and -(2**70) not in oneyaml.loads(str(doc))


def test_an_index_beyond_the_sequence_is_an_index_error(api):
    doc = oneyaml.parse("l: [1]\n")
    for i in (5, 10**30, -(10**30)):
        with pytest.raises(IndexError):
            doc["l"][i]
    with pytest.raises(IndexError):
        doc.at("/l/100000000000000000000000000000")


def test_keys_that_cannot_be_keys_are_type_errors(api):
    doc = oneyaml.parse("a: 1\n")
    for key in ([1], {"a": 1}, {1}, b"a", object()):
        with pytest.raises(TypeError):
            doc[key]
        with pytest.raises(TypeError):
            key in doc  # noqa: B015
        with pytest.raises(TypeError):
            doc[key] = 1
    assert str(doc) == "a: 1\n"


# ---- assigned values ----


class Evil(int):
    def __format__(self, spec):
        return "{evil: [1, 2]}"

    def __repr__(self):
        return "{evil: [1, 2]}"

    def __str__(self):
        return "{evil: [1, 2]}"


class EvilStr(str):
    def __str__(self):
        return "wrong"

    def __format__(self, spec):
        return "wrong"


class EvilFloat(float):
    def __repr__(self):
        return "wrong"

    def __format__(self, spec):
        return "wrong"


@pytest.mark.parametrize("value", [Evil(2**70), Evil(5), Evil(-(2**64)), Evil(0)])
def test_an_int_subclass_is_read_by_its_value_only(api, value):
    doc = oneyaml.parse("a: 1\n")
    doc["a"] = value
    assert "evil" not in str(doc)
    assert doc.to_python() == {"a": int(value) if type(value) is int else int.__int__(value)}
    assert type(oneyaml.loads(str(doc))["a"]) is int


def test_str_and_float_subclasses_are_read_by_their_base_value(api):
    doc = oneyaml.parse("a: 1\n")
    doc["a"] = {"s": EvilStr("right"), "f": EvilFloat(1.5), EvilStr("k"): 1}
    assert oneyaml.loads(str(doc))["a"] == {"s": "right", "f": 1.5, "k": 1}


class Cyclic(Mapping):
    def __init__(self, twice=False):
        self.twice = twice

    def __getitem__(self, k):
        raise KeyError(k)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def items(self):
        return [("k", 1), ("k", 2)] if self.twice else []


def test_assignment_errors_are_the_librarys_with_categories(api):
    doc = oneyaml.parse("a: 1\n")
    cycle = []
    cycle.append(cycle)
    cases = [
        (cycle, "other"),
        ("\ud800", "encoding"),
        (Cyclic(twice=True), "other"),
        (10**5000, "limit"),
        ({"k": 10**5000}, "limit"),
    ]
    for value, category in cases:
        with pytest.raises(YAMLError) as e:
            doc["a"] = value
        assert e.value.category == category, value
    assert str(doc) == "a: 1\n"
    with pytest.raises(TypeError):
        doc["a"] = object()


class Fresh(Sequence):
    """A sequence whose items are made when asked for."""

    def __init__(self, make):
        self.make = make

    def __len__(self):
        return 1

    def __getitem__(self, i):
        if i != 0:
            raise IndexError(i)
        return self.make()


def listed(x):
    return [listed(i) for i in x] if isinstance(x, Sequence) and not isinstance(x, str) else x


def test_a_freed_container_does_not_look_like_a_cycle(api):
    inner = Fresh(lambda: [1])
    outer = Fresh(lambda: [inner])
    doc = oneyaml.parse("a: 1\n")
    doc["a"] = outer
    assert doc.to_python() == {"a": listed(outer)}
    assert listed(outer) == [[[[1]]]]
    deep = Fresh(lambda: Fresh(lambda: Fresh(lambda: [Fresh(lambda: [7])])))
    doc["a"] = deep
    assert doc.to_python() == {"a": listed(deep)}


# ---- encoding, alias staleness ----


# An empty stream in an encoding with no byte order mark is no bytes at all: nothing to detect.
@pytest.mark.parametrize(
    ("codec", "text"),
    [
        (c, t)
        for c in ("utf-16", "utf-16-be", "utf-32", "utf-32-be", "utf-8")
        for t in ("# c\n", "\n", "")
        if t or c in ("utf-16", "utf-32")
    ],
)
def test_a_stream_without_documents_keeps_its_encoding(api, codec, text):
    data = text.encode(codec)
    doc = oneyaml.parse(data)
    assert bytes(doc) == data
    assert str(doc).lstrip("\ufeff") == text
    assert doc.encoding.startswith("utf-" + codec.split("-")[1][:2])


def test_a_view_through_an_alias_is_stale_when_the_alias_becomes_a_scalar(api):
    doc = oneyaml.parse("a: &x {k: 1}\nb: *x\nc: [1]\n")
    v = doc["b"]
    assert v["k"] == 1
    doc["b"] = 2
    for use in (lambda: v["k"], lambda: len(v), lambda: list(v), lambda: v.to_python()):
        with pytest.raises(StaleViewError):
            use()
    assert doc["a"] == {"k": 1}


# ---- a sequence that never ends ----------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="needs an address-space limit")
@pytest.mark.slow
def test_an_endless_sequence_is_a_memory_error_not_an_abort(api):
    oneyaml.parse("a: 1\n")  # a backend without documents stops here
    code = (
        "import resource, sys\n"
        "resource.setrlimit(resource.RLIMIT_AS, (1 << 31, 1 << 31))\n"
        "from collections.abc import Sequence\n"
        "import oneyaml\n"
        "class Endless(Sequence):\n"
        "    def __len__(self): return 1\n"
        "    def __getitem__(self, i): return 0\n"
        "doc = oneyaml.parse('a: 1\\n')\n"
        "try:\n"
        "    doc['a'] = Endless()\n"
        "except MemoryError:\n"
        "    print('memory-error', str(doc) == 'a: 1\\n')\n"
    )
    env = dict(os.environ, ONEYAML_BACKEND=api.name)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.stdout.strip() == "memory-error True", out.stderr[-300:]


# ---- an edit by another thread between the check and the write ---------------------------------


class _Pause:
    """A handle that, once, lets another thread try an edit just before a write goes through."""

    def __init__(self, real, meanwhile):
        self._real = real
        self._meanwhile = meanwhile
        self.blocked = None
        self.thread = None

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name != "set" or self._meanwhile is None:
            return attr

        def paused(*args):
            thread = threading.Thread(target=self._meanwhile)
            self._meanwhile = None
            thread.start()
            thread.join(0.3)
            self.blocked = thread.is_alive()
            self.thread = thread
            return attr(*args)

        return paused


def test_another_threads_insert_cannot_land_between_a_views_check_and_its_write(api):
    doc = oneyaml.parse("s:\n" + "".join(f"- {{id: {i}}}\n" for i in range(4)))
    view = doc["s"][2]
    pause = _Pause(doc._core.h, lambda: doc["s"].insert(0, {"id": 99}))
    doc._core.h = pause
    view["hit"] = 1
    pause.thread.join()
    assert pause.blocked, "the other thread's edit ran while this edit was in progress"
    items = doc.to_python()["s"]
    assert [x.get("hit") for x in items] == [None, None, None, 1, None]
    assert [x["id"] for x in items] == [99, 0, 1, 2, 3]
    with pytest.raises(StaleViewError):
        view["hit"] = 2
