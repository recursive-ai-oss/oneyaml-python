"""The value types: hashing a nested key never recurses (no backend involved)."""

import sys

import pytest

from oneyaml import FrozenMapping, Tagged
from oneyaml._types import big_int

DEPTH = 20_000  # far past the interpreter's recursion limit


def test_hashing_nested_tagged_values_does_not_recurse():
    t = Tagged("!t", 1)
    for _ in range(DEPTH):
        t = Tagged("!t", (t,))
    assert isinstance(hash(t), int)


def test_hashing_nested_frozen_mappings_does_not_recurse():
    m = FrozenMapping({"a": 1})
    for _ in range(DEPTH):
        m = FrozenMapping({"a": m})
    assert isinstance(hash(m), int)


def test_hashes_agree_for_equal_values():
    assert hash(FrozenMapping({"a": (1, 2)})) == hash(FrozenMapping({"a": (1, 2)}))
    assert hash(Tagged("!t", (1,))) == hash(Tagged("!t", (1,)))
    assert {Tagged("!t", "x"): 1}[Tagged("!t", "x")] == 1


def test_a_node_holding_a_list_or_dict_is_unhashable():
    with pytest.raises(TypeError):
        hash(Tagged("!t", [1]))
    with pytest.raises(TypeError):
        hash(FrozenMapping({"a": [1]}))


@pytest.mark.parametrize("cap", [640, 700, 5000])
def test_big_int_under_a_lowered_interpreter_cap(cap):
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(cap)
    try:
        n = cap + 60
        assert big_int("-" + "7" * n) == -7 * (10**n - 1) // 9
        assert big_int("+" + "1" * n) == (10**n - 1) // 9
    finally:
        sys.set_int_max_str_digits(before)
