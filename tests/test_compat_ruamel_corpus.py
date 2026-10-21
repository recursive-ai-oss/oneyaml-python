"""The ruamel compat module against ruamel.yaml itself.

The kit comparisons need ruamel.yaml (a development dependency) and the kit checkout named by
``ONEYAML_KIT``; the generated-scalar comparison runs everywhere ruamel.yaml is installed.
Differences are pinned: a listed difference must still be one, every other case must agree.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import warnings

import pytest

import oneyaml
from oneyaml.compat.ruamel import YAML
from pyyaml_corpus import scalars
from ruamel_corpus import edit_outcome, edit_scripts, kit_cases, load_difference, same

ruamel = pytest.importorskip("ruamel.yaml")

HERE = pathlib.Path(__file__).parent
KIT = os.environ.get("ONEYAML_KIT")


@pytest.fixture(autouse=True)
def _quiet():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # ruamel warns about reused anchors in some cases
        yield


# ---- generated scalars ---------------------------------------------------------------------------

_GROUPED = (
    r"[-+]?(?:0x[0-9a-fA-F]+(?:_[0-9a-fA-F]+)*|0o[0-7]+(?:_[0-7]+)*|0b[01]+(?:_[01]+)*"
    r"|[0-9]+(?:_[0-9]+)*|[0-9]+(?:_[0-9]+)*\.(?:[0-9]+(?:_[0-9]+)*)?(?:[eE][-+]?[0-9]+)?"
    r"|\.[0-9]+(?:_[0-9]+)*(?:[eE][-+]?[0-9]+)?)"
)


def _intended(scalar: str, how: str) -> str | None:
    """The reason this library intentionally reads ``scalar`` differently, or None."""
    if how == "ruamel rejects input that YAML 1.2 accepts":
        return how  # ruamel crashes on dates and numbers it half-matches
    if "_" in scalar and not re.fullmatch(_GROUPED, scalar):
        return "ruamel reads misplaced underscores as digit separators"
    if re.fullmatch(r"\.[0-9]+[eE][-+]?[0-9]+", scalar):
        return "ruamel does not read a float with no digit before the dot and an exponent"
    if scalar in ("<<", "="):
        return "ruamel refuses a bare merge or value indicator"
    if re.match(r"[0-9]{4}-", scalar) and ("\t" in scalar or re.search(r"\.[0-9]{7,}", scalar)):
        return "a timestamp with a tab, or more than six fraction digits"
    if re.match(r"[0-9]{4}-", scalar):
        return "an impossible date: ruamel raises ValueError"
    return None


@pytest.mark.parametrize("typ", ["safe", "rt"])
def test_generated_scalars_read_as_ruamel_reads_them(api, typ):
    odd = []
    compared = 0
    for s in scalars(1500):
        text = f"k: {s}\n"
        how = load_difference(ruamel.YAML, text, typ)
        compared += 1
        if how is not None and _intended(s, how) is None:
            odd.append((s, how))
    assert compared >= 1500
    assert not odd, odd[:10]


# ---- the kit's cases -----------------------------------------------------------------------------

needs_kit = pytest.mark.skipif(not KIT, reason="set ONEYAML_KIT to the kit checkout")


def _text(path):
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


@needs_kit
@pytest.mark.parametrize("typ", ["safe", "rt"])
def test_kit_cases_read_as_ruamel_reads_them(api, typ):
    listed = json.loads((HERE / "ruamel_differences.json").read_text())
    seen = set()
    compared = 0
    for cid, path in kit_cases(KIT):
        text = _text(path)
        if text is None:
            continue
        how = load_difference(ruamel.YAML, text, typ)
        compared += 1
        key = f"{typ}:{cid}"
        entry = listed.get(key, {})
        # A listed difference must still be one, with the same reason; all else must agree.
        assert entry.get(api.name) == how, (key, how, entry)
        if how is not None:
            seen.add(key)
    assert compared > 900
    assert seen == {k for k, v in listed.items() if k.startswith(typ + ":") and api.name in v}


@needs_kit
def test_kit_cases_round_trip_byte_for_byte(api):
    n = 0
    for cid, path in kit_cases(KIT):
        raw = path.read_bytes()  # bytes: the kit has UTF-16 and byte-order-mark cases
        try:
            y = YAML()
            docs = list(y.load_all(raw))
        except oneyaml.YAMLError:
            continue
        if not docs or not all(isinstance(d, (dict, list)) for d in docs):
            continue  # a scalar or empty document has nothing to remember its text by
        out = io.BytesIO()
        y.dump_all(docs, out)
        assert out.getvalue() == raw, cid
        n += 1
    assert n > 600


@needs_kit
def test_kit_cases_edited_by_script_read_back_as_the_edited_data(api):
    gaps = json.loads((HERE / "ruamel_edit_gaps.json").read_text())
    seen = set()
    counts = {"ok": 0, "refused": 0}
    for cid, path in kit_cases(KIT):
        text = _text(path)
        if text is None:
            continue
        try:
            data = oneyaml.loads(text)
            YAML().load(text)
        except oneyaml.YAMLError:
            continue
        if not isinstance(data, (dict, list)) or not data:
            continue
        for name, _ in edit_scripts(data):
            outcome = edit_outcome(text, name)
            key = f"{cid}|{name}"
            applies = api.name in gaps.get(key, {})
            if applies:
                seen.add(key)
            if outcome in counts:
                counts[outcome] += 1
            assert (outcome in ("ok", "refused")) != applies, (key, outcome)
    assert seen == {k for k, v in gaps.items() if api.name in v}
    assert counts["ok"] > 2500
    print(f"[{api.name}] edits ok {counts['ok']}, refused {counts['refused']}, gaps {len(seen)}")  # noqa: T201


def test_pinned_lists_are_explained():
    for name in ("ruamel_differences.json", "ruamel_edit_gaps.json"):
        listed = json.loads((HERE / name).read_text())
        assert listed
        for key, entry in listed.items():
            assert entry and set(entry) <= {"pure", "native"}, key
            assert all(reason.strip() for reason in entry.values()), key


# ---- the data layer on its own -------------------------------------------------------------------


def _random_tree(rng, depth=0):
    kinds = ["s", "i", "f", "b", "n"] + (["m", "l"] * 2 if depth < 4 else [])
    k = rng.choice(kinds)
    if k == "s":
        return rng.choice(["a", "b c", "d", "x y z", "q", "text", "7x", "k"])
    if k == "i":
        return rng.randint(-5, 500)
    if k == "f":
        return rng.choice([0.5, 1.5, 2.25])
    if k == "b":
        return rng.choice([True, False])
    if k == "n":
        return None
    if k == "m":
        return {
            rng.choice(["a", "b", "c", "d", "e", "f", "g"]) + str(i): _random_tree(rng, depth + 1)
            for i in range(rng.randint(1, 4))
        }
    return [_random_tree(rng, depth + 1) for _ in range(rng.randint(1, 4))]


def _mutate(rng, tree):
    """Apply a few random edits to a CommentedMap/Seq tree and return the plain value it should
    now equal."""
    from oneyaml.compat.ruamel import CommentedMap, CommentedSeq

    nodes = []
    stack = [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, (CommentedMap, CommentedSeq)):
            nodes.append(n)
            stack.extend(n.values() if isinstance(n, dict) else n)
    for _ in range(rng.randint(1, 4)):
        n = rng.choice(nodes)
        if isinstance(n, dict):
            op = rng.choice(["set", "add", "del", "sort"])
            if op == "set" and n:
                n[rng.choice(list(n))] = rng.choice([1, "z", True, 2.5, None, [1, 2], {"n": 1}])
            elif op == "add":
                n["new" + str(rng.randint(0, 9))] = rng.choice([3, "t", [], {}, [{"k": 1}]])
            elif op == "del" and len(n) > 1:
                del n[rng.choice(list(n))]
            elif op == "sort":
                items = sorted(n.items(), key=lambda kv: kv[0])
                n.clear()
                n.update(items)
        else:
            op = rng.choice(["set", "append", "insert", "pop", "reverse"])
            if op == "set" and n:
                n[rng.randrange(len(n))] = rng.choice([1, "z", True, None, {"n": 1}])
            elif op == "append":
                n.append(rng.choice([4, "w", [5], {"w": 1}]))
            elif op == "insert":
                n.insert(rng.randint(0, len(n)), rng.choice([4, "w", {"w": 1}]))
            elif op == "pop" and len(n) > 1:
                n.pop(rng.randrange(len(n)))
            elif op == "reverse":
                n.reverse()


def _to_plain(tree):
    """A plain copy (no Commented types), without recursion."""
    root = [None]
    stack = [(tree, root, 0)]
    while stack:
        node, holder, key = stack.pop()
        if isinstance(node, dict):
            new = {}
            holder[key] = new
            stack.extend((v, new, k) for k, v in node.items())
            for k in node:
                new[k] = None  # keep the order
        elif isinstance(node, list):
            new = [None] * len(node)
            holder[key] = new
            stack.extend((v, new, i) for i, v in enumerate(node))
        else:
            holder[key] = node
    return root[0]


@pytest.mark.parametrize("seed", range(12))
def test_random_edits_of_random_documents_read_back_as_the_edited_tree(api, seed):
    import random

    rng = random.Random(seed)
    for _ in range(25):
        tree = {"top" + str(i): _random_tree(rng) for i in range(rng.randint(1, 4))}
        text = oneyaml.dumps(tree, indent=rng.choice([2, 4]))
        y = YAML()
        data = y.load(text)
        _mutate(rng, data)
        want = _to_plain(data)
        out = io.StringIO()
        y.dump(data, out)
        assert same(oneyaml.loads(out.getvalue()), want), (text, out.getvalue())
