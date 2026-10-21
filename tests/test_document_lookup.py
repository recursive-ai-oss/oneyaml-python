"""Reading a mapping by a word reads the same whichever way a key is written.

A lookup by text looks at the first keys of a mapping itself before asking the engine. These
tests build mappings out of every way a key can be written (plain, quoted, escaped, tagged,
anchored, aliased, explicit, a collection, the merge key, a number that looks like a word) and
compare the answer of every word with what a load of the same text gives, on both backends.
"""

import importlib
import random

import pytest

import oneyaml
from oneyaml import Limits, YAMLError

WORDS = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "eps_1",
    "Zeta",
    "k",
    "yes",
    "no",
    "On",
    "null",
    "True",
    "n",
]


def _key_forms(word, rng):
    forms = [
        word,
        f"'{word}'",
        f'"{word}"',
        "".join(f"\\x{ord(c):02x}" if rng.random() < 0.5 else c for c in word).join('""'),
        f"!!str {word}",
        f"&a{rng.randrange(1000)} {word}",
        f"? {word}\n:",
        f"? |-\n  {word}\n:",
        f"!local {word}",
    ]
    return forms


def _document(rng):
    count = rng.choice([1, 2, 5, 23, 24, 25, 26, 40, 60])
    words = rng.sample(WORDS, k=min(len(WORDS), rng.randrange(1, 8))) + [
        f"w{i}" for i in range(count)
    ]
    rng.shuffle(words)
    lines = []
    seen = set()
    for i, word in enumerate(words[:count]):
        if word in seen:
            continue
        seen.add(word)
        form = rng.choice(_key_forms(word, rng))
        if form.startswith("? "):
            lines.append(f"{form} v{i}")
        else:
            lines.append(f"{form}: v{i}")
        extra = rng.random()
        if extra < 0.04:
            lines.append(f"? [{i}, {i}]\n: seq{i}")
        elif extra < 0.08:
            lines.append(f"{i}: n{i}")
        elif extra < 0.10:
            lines.append(f"<<: {{m{i}: 1}}")
        elif extra < 0.12:
            lines.append(f"0x{i:x}: h{i}")
        elif extra < 0.14:
            lines.append(f"~: nil{i}")
        elif extra < 0.16:
            lines.append(f"&anch{i} anchored{i}: a{i}")
            lines.append(f"*anch{i} : al{i}")
    if rng.random() < 0.3:
        lines.insert(0, "%YAML 1.1\n---")
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def backends():
    return [importlib.import_module("oneyaml._native"), importlib.import_module("oneyaml._pure")]


def _lookup(backend, text, word):
    h = backend.parse(text, Limits())
    try:
        return ("ok", h.value(0, [word]))
    except YAMLError as e:
        return ("error", type(e).__name__, e.category)
    except Exception as e:  # noqa: BLE001 - compared by type
        return ("error", type(e).__name__)


@pytest.mark.native_only
def test_every_word_reads_as_a_load_reads_it(backends):
    checked = 0
    for seed in range(400):
        rng = random.Random(seed)
        text = _document(rng)
        try:
            loaded = oneyaml.loads(text)
        except YAMLError:
            continue
        if not isinstance(loaded, dict):
            continue
        for word in [*WORDS, *(f"w{i}" for i in range(0, 62, 3)), "absent"]:
            native, pure = (_lookup(b, text, word) for b in backends)
            if word in loaded:
                assert native == ("ok", loaded[word]), (seed, word, text)
            else:
                assert native[0] == "error" and native[1] == "KeyError", (seed, word, text)
            assert native == pure, (seed, word, text)
            checked += 1
    assert checked > 5000


@pytest.mark.native_only
def test_edits_through_a_word_land_where_a_load_says(backends):
    done = 0
    for seed in range(200):
        rng = random.Random(1000 + seed)
        text = _document(rng)
        try:
            loaded = oneyaml.loads(text)
        except YAMLError:
            continue
        if not isinstance(loaded, dict):
            continue
        word = rng.choice([*WORDS, "w0", "w7", "w30", "absent"])
        results = []
        for b in backends:
            h = b.parse(text, Limits())
            try:
                h.set(0, [word], "changed")
                results.append(("ok", h.value(0, [])))
            except Exception as e:  # noqa: BLE001
                results.append(("error", type(e).__name__))
        expected = dict(loaded)
        expected[word] = "changed"
        if results[0][0] == "ok":
            assert results[0][1] == expected, (seed, word, text)
        # Next to a key with a tag of its own the backends differ; the test below pins that.
        if "!local" not in text:
            assert results[0] == results[1], (seed, word, text)
        done += 1
    assert done > 50


@pytest.mark.native_only
def test_removing_through_a_word_removes_what_a_load_says(backends):
    done = 0
    for seed in range(200):
        rng = random.Random(5000 + seed)
        text = _document(rng)
        try:
            loaded = oneyaml.loads(text)
        except YAMLError:
            continue
        if not isinstance(loaded, dict):
            continue
        word = rng.choice([*WORDS, "w0", "w7", "w30", "absent"])
        results = []
        for b in backends:
            h = b.parse(text, Limits())
            try:
                h.remove(0, [word])
                results.append(("ok", h.value(0, [])))
            except Exception as e:  # noqa: BLE001
                results.append(("error", type(e).__name__))
        if word in loaded:
            expected = {k: v for k, v in loaded.items() if k != word}
            assert results[0] == ("ok", expected), (seed, word, text)
        # Next to a key with a tag of its own the backends differ; the test below pins that.
        if "!local" not in text:
            assert results[0] == results[1], (seed, word, text)
        done += 1
    assert done > 50


@pytest.mark.native_only
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the pure backend takes a key with a tag of its own (`!local yes`) for the word `yes` "
        "when setting it and replaces that key's value; the native backend adds a plain `yes` "
        "key next to it, as a load and a read see it"
    ),
)
def test_setting_a_word_next_to_a_tagged_key_agrees(backends):
    text = "!local yes: v\nb: 1\n"
    results = []
    for b in backends:
        h = b.parse(text, Limits())
        h.set(0, ["yes"], "changed")
        results.append(h.value(0, []))
    assert results[0] == results[1]
