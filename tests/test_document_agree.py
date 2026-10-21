"""The two backends' lossless handles agree: the same calls give the same answers.

Reads are compared exactly. Edits are compared by what they do to the value and by the error they
raise (its type and category); the exact bytes an edit writes are the kit's concern, not this
test's.
"""

# The calls are made at once, inside the loops that bind their arguments, and the sample
# documents are long lines.
# ruff: noqa: B023, E501
import copy
import importlib

import pytest

from oneyaml import Limits, YAMLError
from oneyaml._types import FrozenMapping, Tagged

pytestmark = pytest.mark.native_only

DOCS = [
    "a: 1\nb: [x, 2.5, null]\nc: {d: e}\n",
    "# lead\nname: api  # trailing\nitems:\n  # above first\n  - one\n  - two: 2\n    three: 3\n  -\n    - nested\n",
    "- a\n- &x [1, 2]\n- *x\n- {k: v}\n- 'q'\n- \"dq\"\n- |\n  lit\n- >\n  fold\n",
    "x: &a {k: 1}\ny: *a\nz: !!str 5\nw: !t value\nv: &s plain\n",
    "1: int\n1.0: float\ntrue: bool\n~: nil\n'1': str\n0x10: hex\n? [1, 2]\n: seq key\n? {a: 1}\n: map key\n",
    "a: 1\n---\nb: [2]\n---\n- c\n",
    "plain scalar document\n",
    "\ufeffa: 1\r\nb:\r\n  - 2\r\n",
    "---\n",
    "{a: 1, b: [1, 2], c: {d: 1}}\n",
    "k: |+\n  keep\n\nz: 1\n",
]


@pytest.fixture(scope="module")
def backends():
    return importlib.import_module("oneyaml._native"), importlib.import_module("oneyaml._pure")


def handles(backends, text):
    return [b.parse(text, Limits()) for b in backends]


def outcome(call):
    """What a call gave: its value, or the error's type and category."""
    try:
        return ("ok", call())
    except YAMLError as e:
        return ("error", type(e).__name__, e.category)
    except Exception as e:  # noqa: BLE001 - any other type is compared by name
        return ("error", type(e).__name__)


def walk_paths(h, d):
    """Every path of document ``d``, with exact tokens (through the native handle)."""
    out = [[]]
    todo = [[]]
    while todo:
        p = todo.pop()
        try:
            kind = h.kind(d, p)
        except Exception:  # noqa: BLE001
            continue
        if kind == "mapping":
            for k in h.keys(d, p):
                out.append([*p, k])
                todo.append([*p, k])
        elif kind == "sequence":
            for i in range(h.length(d, p)):
                out.append([*p, str(i)])
                todo.append([*p, str(i)])
    return out


def same_value(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same_value(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, Tagged):
        return a.tag == b.tag and same_value(a.value, b.value)
    if isinstance(a, FrozenMapping):
        return same_value(dict(a), dict(b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


def agree(a, b):
    if a[0] == "ok" and b[0] == "ok":
        return same_value(a[1], b[1])
    return a == b


def edits_agree(a, b, refusal=""):
    """Two edits did the same: both worked, or both failed the same way. Two refusals the engine
    still makes where the pure backend edits: it will not give a trailing comment a new owner,
    and it will not empty a block sequence that is followed by a comment (``refusal`` is the
    native error message)."""
    if a[0] == b[0] == "ok":
        return True
    if a == b:
        return True
    return (
        a[:3] == ("error", "YAMLError", "other")
        and b[0] == "ok"
        and ("would belong to another owner" in refusal or "would not read back" in refusal)
    )


@pytest.mark.parametrize("text", DOCS)
def test_reads_agree(backends, text):
    nat, pure = handles(backends, text)
    assert nat.count() == pure.count()
    assert nat.encoding == pure.encoding
    assert bytes(nat.emit()) == bytes(pure.emit())
    for d in range(nat.count()):
        for path in walk_paths(nat, d):
            for name in ("kind", "value", "length", "keys"):
                a = outcome(lambda name=name, path=path, h=nat: getattr(h, name)(d, path))
                b = outcome(lambda name=name, path=path, h=pure: getattr(h, name)(d, path))
                assert agree(a, b), (text, d, path, name, a, b)
            a = outcome(lambda path=path: nat.info(d, path))
            b = outcome(lambda path=path: pure.info(d, path))
            assert a == b, (text, d, path, "info", a, b)


BAD_PATHS = [
    ["zz"],
    ["b", 9],
    ["b", -1],
    ["b", "x"],
    ["a", "x"],
    [5],
    [True],
    [None],
    [1.5],
    [("x",)],
    ["\ud800"],
    ["b", "01"],
    [2**70],
]


@pytest.mark.parametrize("text", DOCS)
def test_misses_agree(backends, text):
    nat, pure = handles(backends, text)
    for path in BAD_PATHS:
        for name in ("kind", "value", "length", "keys", "info"):
            a = outcome(lambda name=name, path=path: getattr(nat, name)(0, path))
            b = outcome(lambda name=name, path=path: getattr(pure, name)(0, path))
            assert agree(a, b), (text, path, name, a, b)
    for bad in (5, None):
        assert (
            outcome(lambda bad=bad: nat.value(0, bad))[:2]
            == outcome(lambda bad=bad: pure.value(0, bad))[:2]
        )
    for d in (7,):  # no such document
        a = outcome(lambda d=d: nat.kind(d, []))
        b = outcome(lambda d=d: pure.kind(d, []))
        assert a[:2] == b[:2], (d, a, b)


VALUES = [
    None,
    True,
    7,
    2.5,
    "s",
    "multi\nline",
    "",
    [1, [2]],
    {"k": [1]},
    [],
    {},
    b"\x00\x01",
    ("t",),
]


def state(h):
    """The whole stream's values, or the error reading them gave."""
    return [outcome(lambda d=d: h.value(d, [])) for d in range(h.count())]


@pytest.mark.parametrize("text", DOCS)
def test_set_agrees(backends, text):
    nat0, pure0 = handles(backends, text)
    for d in range(nat0.count()):
        for path in walk_paths(nat0, d)[:40]:
            for value in VALUES:
                nat, pure = handles(backends, text)
                a = outcome(lambda: nat.set(d, path, copy.deepcopy(value)))
                b = outcome(lambda: pure.set(d, path, copy.deepcopy(value)))
                assert edits_agree(a, b), (text, d, path, value, a, b)
                if a[0] == b[0] == "ok":
                    assert all(map(agree, state(nat), state(pure))), (text, d, path, value)
                    assert nat.version() == pure.version(), (text, d, path, value)


@pytest.mark.parametrize("text", DOCS)
def test_remove_and_insert_agree(backends, text):
    nat0, _ = handles(backends, text)
    for d in range(nat0.count()):
        for path in walk_paths(nat0, d)[:40]:
            nat, pure = handles(backends, text)
            a = outcome(lambda: nat.remove(d, path))
            b = outcome(lambda: pure.remove(d, path))
            assert edits_agree(a, b), (text, d, path, a, b)
            if a[0] == b[0] == "ok":
                assert all(map(agree, state(nat), state(pure))), (text, d, path)
                assert nat.version() == pure.version()
            for index in (0, 1, 99):
                nat, pure = handles(backends, text)
                a = outcome(lambda: nat.insert(d, path, index, "new"))
                b = outcome(lambda: pure.insert(d, path, index, "new"))
                assert edits_agree(a, b), (text, d, path, index, a, b)
                if a[0] == b[0] == "ok":
                    assert all(map(agree, state(nat), state(pure))), (text, d, path, index)
                    assert nat.version() == pure.version()


PATCHES = [
    [{"op": "add", "path": "/zz", "value": 1}],
    [{"op": "add", "path": "/-", "value": 1}],
    [{"op": "add", "path": "/0", "value": {"k": 1}}],
    [{"op": "replace", "path": "/a", "value": [1, 2]}],
    [{"op": "remove", "path": "/a"}],
    [{"op": "remove", "path": "/0"}],
    [{"op": "test", "path": "/a", "value": 1}],
    [{"op": "test", "path": "/a", "value": 2}],
    [{"op": "move", "from": "/a", "path": "/b"}],
    [{"op": "copy", "from": "/b", "path": "/q"}],
    [{"op": "replace", "path": "", "value": {"root": True}}],
    [{"op": "add", "path": "/x/k", "value": 2}],
    [{"op": "add", "path": "/y/k", "value": 2}],
    [{"op": "remove", "path": "/x"}],
    [{"op": "replace", "path": "/1", "value": 1}],
    [{"op": "bogus", "path": "/a"}],
    [{"op": "add", "path": "/a"}],
    [{"op": "add", "path": "a", "value": 1}],
    [{"op": "replace", "path": "/1", "value": 1}, {"op": "remove", "path": "/zz"}],
    [{"op": "test", "path": "/1", "value": "int"}, {"op": "remove", "path": "/1"}],
    [],
    '[{"op": "add", "path": "/n", "value": [1, 2.5, null]}]',
    "not json [",
    "{}",
    {"op": "add"},
]

MERGES = [{"a": None}, {"a": {"b": 1}}, {"b": [1]}, {}, 5, None, [1], {"x": {"k": None}}, "s"]


@pytest.mark.parametrize("text", DOCS)
def test_patches_agree(backends, text):
    for ops in PATCHES:
        for d in (0, 1, 5):
            nat, pure = handles(backends, text)
            a = outcome(lambda: nat.patch(d, copy.deepcopy(ops)))
            b = outcome(lambda: pure.patch(d, copy.deepcopy(ops)))
            if a[0] == "error" and b[0] == "error":
                assert a[:2] == b[:2] or a == b, (text, ops, d, a, b)
                assert bytes(nat.emit()) == bytes(pure.emit()) == text.encode(nat.encoding)
            else:
                assert edits_agree(a, b), (text, ops, d, a, b)
                if a[0] == b[0] == "ok":
                    assert all(map(agree, state(nat), state(pure))), (text, ops, d)
                    assert nat.version() == pure.version(), (text, ops, d)
    for patch in MERGES:
        for d in (0, 1):
            nat, pure = handles(backends, text)
            a = outcome(lambda: nat.merge_patch(d, copy.deepcopy(patch)))
            b = outcome(lambda: pure.merge_patch(d, copy.deepcopy(patch)))
            assert edits_agree(a, b), (text, patch, d, a, b)
            if a[0] == b[0] == "ok":
                assert all(map(agree, state(nat), state(pure))), (text, patch, d)


POINTERS = [
    "",
    "/a",
    "/b/0",
    "/b/-",
    "/0",
    "/1/two",
    "/x/k",
    "/1",
    "/0x10",
    "/true",
    "/~1",
    "/a~",
    "a",
    "/\ud800",
    "/b/01",
]


def test_random_edits_agree(backends):
    import random

    from handle_fuzz import gen, plain, rval

    for seed in range(40):
        r = random.Random(seed)
        text = gen(r)
        try:
            nat = backends[0].parse(text, Limits())
        except YAMLError:
            continue
        pure = backends[1].parse(text, Limits())
        for d in range(nat.count()):
            for path in walk_paths(nat, d):
                assert outcome(lambda: nat.info(d, path)) == outcome(lambda: pure.info(d, path))
        for _ in range(5):
            d = r.randrange(nat.count())
            path = r.choice(walk_paths(nat, d))
            op = r.choice(["set", "set", "insert", "remove"])
            value = plain(rval(r))
            index = r.randint(0, 3)
            calls = {
                "set": lambda h: h.set(d, path, copy.deepcopy(value)),
                "insert": lambda h: h.insert(d, path, index, copy.deepcopy(value)),
                "remove": lambda h: h.remove(d, path),
            }
            a = outcome(lambda: calls[op](nat))
            refusal = ""
            if a[0] == "error":
                try:
                    calls[op](nat)  # an edit that fails leaves the document as it was
                except Exception as e:  # noqa: BLE001 - only the message is wanted
                    refusal = str(e)
            b = outcome(lambda: calls[op](pure))
            assert edits_agree(a, b, refusal), (seed, op, d, path, a, b)
            if a[0] != b[0] or a[0] == "error":
                break  # the texts differ now: the engine refused what the pure backend did
            assert all(map(agree, state(nat), state(pure))), (seed, op, d, path)
            assert nat.version() == pure.version(), (seed, op, d, path)


@pytest.mark.parametrize("text", DOCS)
def test_resolve_agrees(backends, text):
    nat, pure = handles(backends, text)
    for d in (0, 1):
        for pointer in POINTERS:
            a = outcome(lambda: nat.resolve(d, pointer))
            b = outcome(lambda: pure.resolve(d, pointer))
            assert agree(a, b), (text, d, pointer, a, b)
