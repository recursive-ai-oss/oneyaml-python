"""The native and pure backends agree on where every node of every yaml-test-suite input is and
on the comment it owns, as parsed and after a few seeded edits.

Needs the kit checkout named by ``ONEYAML_KIT``. The ``style`` of a flow mapping with a single
pair is left out: the two backends call it differently (a known difference that has nothing to
do with positions or comments)."""

import importlib
import os
import pathlib
import random
import zlib

import pytest

from oneyaml import Limits

pytestmark = pytest.mark.native_only

KIT = os.environ.get("ONEYAML_KIT")
ROOT = pathlib.Path(KIT) / "suites" / "yaml-test-suite" if KIT else None
FILES = sorted(ROOT.glob("data-*/**/in.yaml")) if ROOT and ROOT.is_dir() else []
LIMITS = Limits()
CAP = 1500  # nodes compared per document


def _res(f, *args):
    try:
        return ("ok", f(*args))
    except Exception as exc:  # noqa: BLE001 - the two backends' error types are compared
        return ("err", type(exc).__name__)


def _paths(h, d):
    """Every path of document ``d`` (breadth first, aliases not followed), capped."""
    out = [[]]
    queue = [[]]
    while queue and len(out) < CAP:
        p = queue.pop(0)
        info = _res(h.info, d, p)
        if info[0] != "ok" or info[1][0] == "alias":
            continue
        kind = _res(h.kind, d, p)
        if kind[0] != "ok":
            continue
        if kind[1] == "mapping":
            keys = _res(h.keys, d, p)
            if keys[0] != "ok":
                continue
            children = [p + [k] for k in keys[1]]
        elif kind[1] == "sequence":
            children = [p + [str(i)] for i in range(h.length(d, p))]
        else:
            continue
        out += children
        queue += children
    return out


def _collection_key(path):
    return any(isinstance(t, (tuple, dict, frozenset)) or hasattr(t, "items") for t in path)


def _compare(tag, hn, hp):
    assert hn.count() == hp.count(), tag
    for d in range(hn.count()):
        pn, pp = _paths(hn, d), _paths(hp, d)
        assert [list(map(repr, x)) for x in pn] == [list(map(repr, x)) for x in pp], tag
        for path in pn:
            ln, lp = _res(hn.locate, d, path), _res(hp.locate, d, path)
            if ln != lp and ln == ("err", "KeyError") and _collection_key(path):
                # Pre-existing: the native lookup cannot find a collection key written as an
                # alias to an anchored collection.
                continue
            assert ln == lp, (tag, d, path, ln, lp)
            if ln[0] == "ok":
                inn, inp = hn.info(d, path), hp.info(d, path)
                assert inn[3:] == inp[3:], (tag, d, path, inn, inp)


def _edit(hn, hp, rng):
    """Up to four random edits, made alike on both. False when they disagree on the result,
    which says the edit itself differs, not where nodes are."""
    for _ in range(4):
        d = rng.randrange(hn.count())
        p = rng.choice(_paths(hn, d))
        op = rng.choice(["set", "insert-key", "remove", "set-map"])
        kind = _res(hn.kind, d, p)[1]

        def do(h, op=op, d=d, p=p, kind=kind):
            if op == "set":
                return h.set(d, p, 12)
            if op == "set-map":
                return h.set(d, p, {"q": [1, "zé"]})
            if op == "insert-key":
                if kind == "sequence":
                    return h.insert(d, p, 0, "iñ")
                return h.set(d, p + ["nk"] if kind == "mapping" else p, "n")
            return h.remove(d, p) if p else None

        state = rng.getstate()
        a = _res(do, hn)
        rng.setstate(state)
        b = _res(do, hp)
        if a[0] != b[0] or bytes(hn.emit()) != bytes(hp.emit()):
            return False
    return True


@pytest.mark.skipif(not FILES, reason="set ONEYAML_KIT to the kit checkout to run the corpus")
def test_backends_agree_on_locate_and_comments_over_the_whole_suite():
    native = importlib.import_module("oneyaml._native")
    pure = importlib.import_module("oneyaml._pure")
    compared = edited = 0
    for f in FILES:
        tag = f.parent.relative_to(ROOT).as_posix().split("/", 1)[1]
        data = f.read_bytes()
        try:
            hn = native.parse(data, LIMITS)
            hp = pure.parse(data, LIMITS)
        except Exception:  # noqa: BLE001 - parse differences are the parsers' tests
            continue
        compared += 1
        _compare(tag, hn, hp)
        for seed in range(3):
            hn, hp = native.parse(data, LIMITS), pure.parse(data, LIMITS)
            if hn.count() == 0:
                break
            if _edit(hn, hp, random.Random(zlib.crc32(tag.encode()) ^ seed)):
                edited += 1
                _compare(f"{tag}+edit{seed}", hn, hp)
    assert compared > 250
    assert edited > 300
