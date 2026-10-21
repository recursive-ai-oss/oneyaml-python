"""Random edit sequences against a plain-data model: the document must always read back as the
model, the text must re-parse to the model, and a one-value edit must be one contiguous change."""

import difflib
import random

import pytest

import oneyaml

SCALARS = [
    None, True, False, 0, 1, -7, 3.5, "", "x", "yes", "1", "a: b", "# no", "two words",
    "multi\nline", "multi\nline\n", " pad ", "é", "~", "null",
]  # fmt: skip

START = [
    "a: 1\nb: [1, 2]\nc: {d: x}\n",
    "- 1\n- a: b\n- [x, y]\n",
    "# top\nname: api  # n\nitems:\n  - id: 1\n    tags: [a]\n  - id: 2\n\nopts:\n  k: v\n",
    "{a: 1, b: [1, 2], c: {d: x}}\n",
    "a: |\n  text\nb: >\n  folded\nc: 'q'\nd: \"dq\"\n",
]


def value(rng, depth=0):
    r = rng.random()
    if r < 0.7 or depth > 1:
        return rng.choice(SCALARS)
    if r < 0.85:
        return [value(rng, depth + 1) for _ in range(rng.randrange(3))]
    return {rng.choice("pqr"): value(rng, depth + 1) for _ in range(rng.randrange(3))}


def containers(model, path=()):
    """Every (path, container) in the model."""
    yield path, model
    items = model.items() if isinstance(model, dict) else enumerate(model)
    for k, v in items:
        if isinstance(v, (dict, list)):
            yield from containers(v, (*path, k))


def at(root, path):
    for k in path:
        root = root[k]
    return root


def view_at(doc, path):
    for k in path:
        doc = doc[k]
    return doc


def changed_blocks(before, after):
    sm = difflib.SequenceMatcher(None, before.splitlines(True), after.splitlines(True), False)
    return [op for op in sm.get_opcodes() if op[0] != "equal"]


@pytest.mark.parametrize("seed", range(40))
def test_random_edits_match_a_model(api, seed):
    rng = random.Random(seed)
    text = START[seed % len(START)]
    doc = oneyaml.parse(text)
    model = oneyaml.loads(text)
    applied = 0
    for step in range(25):
        before = str(doc)
        path, box = rng.choice(list(containers(model)))
        view = view_at(doc, path)
        op = rng.choice(["set", "set", "add", "del", "insert"])
        what = (seed, step, op, path)
        if isinstance(box, dict):
            if op == "set" and box:
                k = rng.choice(list(box))
                v = value(rng)
                box[k] = v
                view[k] = v
            elif op in ("add", "insert"):
                k = rng.choice("pqrst") + str(rng.randrange(3))
                v = value(rng)
                box[k] = v
                view[k] = v
            elif op == "del" and box:
                k = rng.choice(list(box))
                del box[k]
                del view[k]
            else:
                continue
        else:
            if op == "set" and box:
                i = rng.randrange(len(box))
                v = value(rng)
                box[i] = v
                view[i] = v
            elif op == "add":
                v = value(rng)
                box.append(v)
                view.append(v)
            elif op == "insert":
                i = rng.randrange(len(box) + 1)
                v = value(rng)
                box.insert(i, v)
                view.insert(i, v)
            elif op == "del" and box:
                i = rng.randrange(len(box))
                del box[i]
                del view[i]
            else:
                continue
        applied += 1
        out = str(doc)
        assert oneyaml.loads(out) == model, (what, before, out)
        assert doc.to_python() == model, (what, before, out)
        # An edit is one change in the text, not scattered ones.
        assert len(changed_blocks(before, out)) <= 1, (what, before, out)
        # And the whole document reparses to the same text it writes.
        assert str(oneyaml.parse(out)) == out
        # Views taken fresh read the model.
        for p, box2 in containers(model):
            assert view_at(doc, p).to_python() == box2
    assert applied >= 10
