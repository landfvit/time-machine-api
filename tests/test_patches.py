import copy
import math

import pytest

from timemachine.patches import MAX_STATE_BYTES, PatchError, apply_operations, state_hash, validate_state


def patch(state, kind, path, **values):
    return apply_operations(state, [{"op": kind, "path": path, **values}])


def test_full_standard_patch_operations_and_input_isolation():
    initial = {"items": ["a", "b"], "nested": {"x": 3}, "empty": None}
    before = copy.deepcopy(initial)
    operations = [
        {"op": "add", "path": "/items/1", "value": "middle"},
        {"op": "replace", "path": "/empty", "value": []},
        {"op": "copy", "from": "/nested", "path": "/copied"},
        {"op": "increment", "path": "/nested/x", "value": 2},
        {"op": "move", "from": "/items/0", "path": "/items/-"},
        {"op": "remove", "path": "/items/0"},
        {"op": "test", "path": "/copied/x", "value": 3.0},
    ]
    actual = apply_operations(initial, operations)
    assert actual == {"items": ["b", "a"], "nested": {"x": 5}, "copied": {"x": 3}, "empty": []}
    assert initial == before
    actual["copied"]["x"] = 99
    assert actual["nested"]["x"] == 5


def test_failure_after_valid_operation_is_atomic():
    state = {"value": 4}
    with pytest.raises(PatchError) as caught:
        apply_operations(state, [{"op": "increment", "path": "/value", "value": 6},
                                 {"op": "remove", "path": "/missing"}])
    assert caught.value.details["operation_index"] == 1
    assert state == {"value": 4}


@pytest.mark.parametrize("pointer", ["x", "/~", "/~2", "/a~0~9", None, 12])
def test_malformed_pointers(pointer):
    with pytest.raises(PatchError, match="pointer"):
        patch({}, "add", pointer, value=1)


def test_escaped_and_empty_object_keys():
    state = {"a/b": {"~": 2}, "": 1, "~1": 4}
    assert patch(state, "increment", "/a~1b/~0", value=3)["a/b"]["~"] == 5
    assert patch(state, "replace", "/", value=9)[""] == 9
    assert patch(state, "test", "/~01", value=4) == state


@pytest.mark.parametrize("pointer", ["/a/01", "/a/-1", "/a/1.0", "/a/١", "/a/", "/a/9999999999999999999999"])
def test_invalid_array_indexes(pointer):
    with pytest.raises(PatchError):
        patch({"a": [1, 2]}, "replace", pointer, value=3)


def test_array_end_rules_and_empty_arrays():
    assert patch({"a": []}, "add", "/a/0", value=1) == {"a": [1]}
    assert patch({"a": []}, "add", "/a/-", value=1) == {"a": [1]}
    for kind in ("remove", "replace", "test"):
        with pytest.raises(PatchError):
            patch({"a": [1]}, kind, "/a/-", value=1)
    with pytest.raises(PatchError):
        patch({"a": []}, "add", "/a/1", value=1)


def test_move_and_copy_to_root_and_noop_move():
    assert patch({"a": {"b": 2}}, "move", "", **{"from": "/a"}) == {"b": 2}
    assert patch({"a": {"b": 2}}, "copy", "", **{"from": "/a"}) == {"b": 2}
    assert patch({"a": 1}, "move", "", **{"from": ""}) == {"a": 1}
    assert patch({"a": 1}, "move", "/a", **{"from": "/a"}) == {"a": 1}
    with pytest.raises(PatchError):
        patch({}, "move", "/missing", **{"from": "/missing"})


def test_cannot_move_parent_into_child_and_copy_is_deep():
    with pytest.raises(PatchError, match="own child"):
        patch({"a": {"x": 2}}, "move", "/a/nested", **{"from": "/a"})
    result = apply_operations({"a": {"x": []}}, [
        {"op": "copy", "from": "/a", "path": "/b"},
        {"op": "add", "path": "/a/x/-", "value": 1},
    ])
    assert result == {"a": {"x": [1]}, "b": {"x": []}}


@pytest.mark.parametrize("value", [True, False, None, "2", [], {}])
def test_increment_rejects_nonnumeric_target_or_value(value):
    with pytest.raises(PatchError):
        patch({"x": value}, "increment", "/x", value=1)
    with pytest.raises(PatchError):
        patch({"x": 1}, "increment", "/x", value=value)


def test_increment_floats_missing_and_overflow():
    assert math.isclose(patch({"x": 0.1}, "increment", "/x", value=0.2)["x"], 0.3)
    with pytest.raises(PatchError):
        patch({}, "increment", "/x", value=1)
    with pytest.raises(PatchError):
        patch({"x": 1e308}, "increment", "/x", value=1e308)
    with pytest.raises(PatchError):
        patch({"x": 10**4000}, "increment", "/x", value=0.5)


@pytest.mark.parametrize("left,right", [(True, 1), (False, 0), ([True], [1]), ({"x": True}, {"x": 1}), (None, 0)])
def test_test_operation_uses_json_equality(left, right):
    with pytest.raises(PatchError) as caught:
        patch({"x": left}, "test", "/x", value=right)
    assert caught.value.code == "test_failed"
    assert patch({"x": left}, "test", "/x", value=left) == {"x": left}


def test_numeric_test_equivalence_and_object_order():
    assert patch({"x": {"a": 1, "b": 2}}, "test", "/x", value={"b": 2.0, "a": 1.0}) == {"x": {"a": 1, "b": 2}}


@pytest.mark.parametrize("value", [[], None, 2, "x", True])
def test_object_roots_only(value):
    with pytest.raises(PatchError):
        validate_state(value)
    with pytest.raises(PatchError):
        patch({"x": 1}, "replace", "", value=value)


def test_object_root_replacement_and_root_removal():
    assert patch({"x": 1}, "add", "", value={}) == {}
    assert patch({}, "replace", "", value={"x": None}) == {"x": None}
    with pytest.raises(PatchError):
        patch({}, "remove", "")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), (1, 2), {1: "bad"}, "\ud800"])
def test_strict_json_values(value):
    with pytest.raises(PatchError):
        validate_state({"x": value})
    with pytest.raises(PatchError):
        patch({}, "add", "/x", value=value)


def test_depth_and_bytes_are_bounded_after_every_operation():
    state = {}
    for _ in range(32):
        state = {"x": state}
    validate_state(state)
    with pytest.raises(PatchError, match="depth"):
        validate_state({"x": state})
    with pytest.raises(PatchError, match="depth"):
        patch({"x": {}}, "add", "/x/y", value=state)
    huge = "x" * MAX_STATE_BYTES
    with pytest.raises(PatchError, match="exceed"):
        apply_operations({}, [{"op": "add", "path": "/x", "value": huge}, {"op": "remove", "path": "/x"}])
    with pytest.raises(PatchError):
        validate_state({"x": "é" * (MAX_STATE_BYTES // 2)})


def test_circular_json_and_operation_metadata_rejected():
    state = {}
    state["x"] = state
    with pytest.raises(PatchError, match="circular"):
        validate_state(state)
    with pytest.raises(PatchError):
        apply_operations({}, [{"op": "add", "path": "/a", "value": 1, "extra": object()}])


@pytest.mark.parametrize("operations", [None, {}, [], [None], [{"op": "nope", "path": ""}], [{"op": "add", "path": "/x"}], [{"op": "copy", "path": "/x"}], [{"op": "remove"}], [{"op": "test", "path": "", "value": {}}] * 101])
def test_invalid_operation_lists(operations):
    with pytest.raises(PatchError):
        apply_operations({}, operations)


def test_hash_is_stable_across_key_order_and_sensitive_to_changes():
    assert state_hash({"b": 2, "a": 1}) == state_hash({"a": 1, "b": 2})
    assert state_hash({"x": True}) != state_hash({"x": 1})
