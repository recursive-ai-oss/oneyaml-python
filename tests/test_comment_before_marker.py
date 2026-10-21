"""An own-line comment before ``---`` belongs to the next document's header, however deeply it is
indented, so an edit to the last entry of a document stays above it. Both backends do the same.

The shapes are compared across the two backends, and the first one is pinned to known bytes so
that agreement is not agreement on a wrong answer.
"""

import importlib

import pytest

import oneyaml
from oneyaml import YAMLError, _api

SHAPES = {
    "sequence": "k:\n  - a\n  # note\n---\nx: 1\n",
    "sequence-blank-lines": "k:\n  - a\n\n  # note\n\n# column zero\n---\nx: 1\n",
    "nested-sequences": "k:\n  - a\n  - - b\n    # deep\n  # mid\n---\nx: 1\n",
    "literal": "k: |\n  text\n  # inside the scalar\n  # note\n---\nx: 1\n",
    "literal-keep": "k: |+\n  text\n\n  # note\n---\nx: 1\n",
    "folded-strip": "k: >-\n  text\n  more\n   # note\n---\nx: 1\n",
    "mapping": "k:\n  m: 1\n  # note\n---\nx: 1\n",
    "crlf": "k:\r\n  - a\r\n  # note\r\n---\r\nx: 1\r\n",
    "three-documents": "k:\n  - a\n  # one\n---\nk:\n  - b\n    # two\n---\nk:\n  - c\n  # three\n",
    "explicit-start": "--- \nk:\n  - a\n  # note\n--- \nx: 1\n",
    "root-sequence": "- a\n- - b\n  # note\n---\n- c\n",
}


def _apply(text, shape, backend, op, which):
    _api._impl = importlib.import_module(f"oneyaml._{backend}")
    doc = oneyaml.parse(text)
    try:
        view = doc.documents[which]
        target = view if shape == "root-sequence" else view.get("k")
        match op:
            case "append":
                target.append("z")
            case "insert-first":
                target.insert(0, "z")
            case "add-key":
                view["n"] = 1
            case "remove":
                del view["k"]
            case "replace":
                view["k"] = "z"
            case "patch-add":
                doc.patch(
                    [
                        {
                            "op": "add",
                            "path": "/-" if shape == "root-sequence" else "/k/-",
                            "value": "z",
                        }
                    ],
                    document=which,
                )
            case "patch-remove":
                doc.patch([{"op": "remove", "path": "/k"}], document=which)
            case "merge-patch":
                doc.merge_patch({"n": 1}, document=which)
    except YAMLError as err:
        return ("error", type(err).__name__, err.category)
    except (KeyError, IndexError, TypeError) as err:
        return ("error", type(err).__name__)
    return ("ok", str(doc), [str(d) for d in doc.documents])


@pytest.fixture(autouse=True)
def _restore_backend():
    saved = _api._impl
    yield
    _api._impl = saved


pytestmark = pytest.mark.native_only

OPS = [
    "append",
    "insert-first",
    "add-key",
    "remove",
    "replace",
    "patch-add",
    "patch-remove",
    "merge-patch",
]


SEQUENCE_OPS = {"append", "insert-first", "patch-add"}
# Shapes whose "k" is not a sequence.
NOT_SEQUENCES = {"literal", "literal-keep", "folded-strip", "mapping"}
CASES = [
    (shape, op)
    for shape in SHAPES
    for op in OPS
    if (op not in SEQUENCE_OPS or shape not in NOT_SEQUENCES)
    # The root sequence has no "k" to remove or replace; and replacing it by a mapping
    # (merge patch) is quoted differently by the backends, which has nothing to do with comments.
    and not (shape == "root-sequence" and op not in SEQUENCE_OPS)
]


@pytest.mark.parametrize(("shape", "op"), CASES)
def test_backends_edit_alike_before_a_marker(shape, op):
    text = SHAPES[shape]
    # Only the documents that a marker follows: a comment at the very end of the stream stays
    # with the last entry, which is a separate question.
    for which in range(text.count("\n---")):
        native = _apply(text, shape, "native", op, which)
        pure = _apply(text, shape, "pure", op, which)
        assert native == pure, (shape, op, which, native, pure)


def test_an_edit_stays_above_the_comment_and_a_removal_leaves_it():
    text = SHAPES["sequence"]
    for backend in ("native", "pure"):
        assert (
            _apply(text, "sequence", backend, "append", 0)[1]
            == "k:\n  - a\n  - z\n  # note\n---\nx: 1\n"
        )
        assert (
            _apply(text, "sequence", backend, "add-key", 0)[1]
            == "k:\n  - a\nn: 1\n  # note\n---\nx: 1\n"
        )
        assert _apply(text, "sequence", backend, "remove", 0)[1] == "{}\n  # note\n---\nx: 1\n"
