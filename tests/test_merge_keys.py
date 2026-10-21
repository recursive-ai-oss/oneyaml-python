"""Merge keys: ``loads(..., merge_keys=True)`` on both backends.

YAML 1.2 has no merge keys, so a load without the option keeps ``<<`` as an ordinary key. With
it, a plain ``<<`` key adds the entries of the mapping (or list of mappings) it names.
"""

import random

import pytest

import oneyaml
from oneyaml import Limits, YAMLError, _api

NATIVE_AND_PURE = ("native", "pure")


def merged(api, text, **kw):
    return api.loads(text, merge_keys=True, **kw)


def test_default_keeps_the_key_as_it_is(api):
    text = "a: &b {x: 1}\nj:\n  <<: *b\n  y: 2\n"
    assert api.loads(text)["j"] == {"<<": {"x": 1}, "y": 2}
    assert api.loads(text, merge_keys=False)["j"] == {"<<": {"x": 1}, "y": 2}
    assert api.loads(text, merge_keys=True)["j"] == {"x": 1, "y": 2}


def test_a_merged_mapping_adds_its_entries(api):
    src = "base: &b {timeout: 30}\njob:\n  <<: *b\n  name: x\n"
    assert merged(api, src)["job"] == {"timeout": 30, "name": "x"}


def test_own_entries_win_and_keep_the_merged_places(api):
    src = "b: &b {x: 1, y: 2}\nj:\n  y: 9\n  <<: *b\n  z: 3\n"
    got = merged(api, src)["j"]
    assert got == {"x": 1, "y": 9, "z": 3}
    assert list(got) == ["x", "y", "z"]


def test_the_first_mapping_of_a_list_wins(api):
    src = "a: &a {k: from-a, only-a: 1}\nb: &b {k: from-b, only-b: 2}\nj:\n  <<: [*a, *b]\n"
    got = merged(api, src)["j"]
    assert got == {"k": "from-a", "only-a": 1, "only-b": 2}


def test_a_merged_mapping_may_merge_too(api):
    src = "a: &a {x: 1}\nb: &b\n  <<: *a\n  y: 2\nc:\n  <<: *b\n  z: 3\n"
    assert merged(api, src)["c"] == {"x": 1, "y": 2, "z": 3}


def test_an_inline_mapping_merges(api):
    assert merged(api, "j:\n  <<: {a: 1}\n  b: 2\n")["j"] == {"a": 1, "b": 2}


def test_a_quoted_key_is_not_a_merge_key(api):
    assert merged(api, 'a: &a {x: 1}\nj:\n  "<<": *a\n')["j"] == {"<<": {"x": 1}}


def test_a_value_or_a_sequence_item_is_text(api):
    assert merged(api, "a: <<\nb: [<<]\n") == {"a": "<<", "b": ["<<"]}


def test_merging_applies_in_every_document_of_a_stream(api):
    docs = api.loads_all(
        "a: &a {x: 1}\nb: {<<: *a}\n---\nc: &c {y: 2}\nd: {<<: *c}\n", merge_keys=True
    )
    assert docs[0]["b"] == {"x": 1}
    assert docs[1]["d"] == {"y": 2}


@pytest.mark.parametrize(
    "text",
    [
        "j: {<<: 1}\n",
        "j: {<<: [1]}\n",
        "j: {<<: [[{a: 1}]]}\n",
        "j: {<<: text}\n",
        "j: {<<: !t {a: 1}}\n",
    ],
)
def test_a_merge_of_something_that_is_not_a_mapping_is_an_error(api, text):
    with pytest.raises(YAMLError) as info:
        merged(api, text)
    assert info.value.category == "syntax"
    assert info.value.line == 1


def test_two_merge_keys_in_one_mapping_are_a_duplicate_key(api):
    with pytest.raises(YAMLError) as info:
        merged(api, "a: &a {x: 1}\nb: &b {y: 2}\nj:\n  <<: *a\n  <<: *b\n")
    assert info.value.category == "duplicate-key"
    assert info.value.line == 5


def test_a_repeated_own_key_is_still_an_error(api):
    with pytest.raises(YAMLError) as info:
        merged(api, "a: &a {x: 1}\nj:\n  <<: *a\n  y: 1\n  y: 2\n")
    assert info.value.category == "duplicate-key"


def test_a_merge_chain_that_expands_exponentially_hits_the_alias_limits(api):
    lines = ["a0: &a0 {x: 1}"]
    for i in range(1, 40):
        lines.append(f"a{i}: &a{i} {{<<: [*a{i - 1}, *a{i - 1}], k{i}: 1}}")
    with pytest.raises(YAMLError) as info:
        merged(api, "\n".join(lines) + "\n")
    assert info.value.category == "limit"


def test_the_limits_given_are_the_limits_used(api):
    src = "a: &a {x: 1}\n" + "".join(f"b{i}: {{<<: *a}}\n" for i in range(50))
    with pytest.raises(YAMLError) as info:
        merged(api, src, limits=Limits(alias_nodes=10))
    assert info.value.category == "limit"


def test_an_alias_to_a_mapping_that_merged_shares_the_merged_value(api):
    got = merged(api, "a: &a {x: 1}\nb: &b {<<: *a, y: 2}\nc: *b\n")
    assert got["c"] == {"x": 1, "y": 2}


def test_a_syntax_error_after_a_merge_is_still_reported(api):
    with pytest.raises(YAMLError) as info:
        merged(api, "a: &a {x: 1}\nj: {<<: *a}\nk: [\n")
    assert info.value.category == "syntax"


# ---- the two backends agree -------------------------------------------------------------------


def generate(seed):
    """A document of anchored mappings and mappings that merge them, now and then broken."""
    rng = random.Random(seed)
    keys = ["a", "b", "c", "d", "e", "<<x", "1", "true"]
    lines = []
    anchors = []

    def body(depth=0):
        parts = []
        for k in rng.sample(keys, rng.randint(0, 4)):
            v = rng.choice(["1", "two", "[1, 2]", "{n: 1}", "~"])
            parts.append(f"{k}: {v}")
        if anchors and rng.random() < 0.7:
            kind = rng.random()
            if kind < 0.5:
                parts.insert(rng.randint(0, len(parts)), f"<<: *{rng.choice(anchors)}")
            elif kind < 0.9:
                picked = ", ".join(f"*{rng.choice(anchors)}" for _ in range(rng.randint(1, 3)))
                parts.insert(rng.randint(0, len(parts)), f"<<: [{picked}]")
            else:
                parts.append(
                    rng.choice(["<<: 5", "<<: [1]", "<<: *missing", "<<: {z: 1}", '"<<": 1'])
                )
        if rng.random() < 0.05:
            parts.append("<<: {q: 1}")  # a second merge key, usually
        return "{" + ", ".join(parts) + "}"

    for i in range(rng.randint(2, 8)):
        name = f"m{i}"
        if rng.random() < 0.6:
            lines.append(f"{name}: &{name} {body()}")
            anchors.append(name)
        else:
            lines.append(f"{name}: {body()}")
    return "\n".join(lines) + "\n"


def run(mod, text, monkeypatch, **kw):
    monkeypatch.setattr(_api, "_impl", mod)
    try:
        return ("ok", oneyaml.loads(text, merge_keys=True, **kw))
    except YAMLError as exc:
        return ("error", exc.category, exc.line, exc.column)


@pytest.mark.native_only
def test_the_backends_agree_on_generated_merge_documents(monkeypatch):
    import importlib

    native = importlib.import_module("oneyaml._native")
    pure = importlib.import_module("oneyaml._pure")
    kinds = set()
    for seed in range(400):
        text = generate(seed)
        a = run(native, text, monkeypatch)
        b = run(pure, text, monkeypatch)
        assert a == b, (seed, text)
        kinds.add(a[0] if a[0] == "ok" else a[1])
    assert "ok" in kinds and len(kinds) >= 3  # the generator reaches successes and errors
