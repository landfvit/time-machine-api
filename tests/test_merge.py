from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from timemachine.merging import merge_documents
from timemachine.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "history.db")


def append(store, timeline, operations):
    return store.append(timeline["id"], "Edit", operations, timeline["head_version"]).payload["timeline"]


def edit(store, timeline, path, value):
    return append(store, timeline, [{"op": "add", "path": path, "value": value}])


def branches(store, state=None):
    root = store.create("Original", {"a": 0, "b": 0} if state is None else state).payload
    return root, store.fork(root["id"], "Alternative", 0).payload


def merge(store, target, source, **kwargs):
    return store.merge(target["id"], source["id"], source["head_version"], target["head_version"], **kwargs)


def test_disjoint_changes_merge_as_one_immutable_event(store):
    target, source = branches(store)
    target, source = edit(store, target, "/a", 1), edit(store, source, "/b", 2)
    before = store.export(source["id"])
    preview = store.preview_merge(target["id"], source["id"])
    assert preview["base"]["timeline_id"] == target["id"]
    assert preview["base"]["version"] == 0
    assert preview["merged_state"] == {"a": 1, "b": 2}
    result = merge(store, target, source).payload
    assert result["state"]["state"] == preview["merged_state"]
    assert result["timeline"]["head_version"] == 2
    assert result["event"]["operations"] == [{"op": "replace", "path": "", "value": {"a": 1, "b": 2}}]
    assert result["event"]["merge"] == {"source_timeline_id": source["id"], "source_version": 1,
                                        "base_timeline_id": target["id"], "base_version": 0}
    assert store.export(source["id"]) == before
    assert store.get_state(target["id"], version=1)["state"] == {"a": 1, "b": 0}
    assert store.get_events(target["id"])["items"][-1] == result["event"]


@pytest.mark.parametrize("base,target,source,expected", [
    ({"x": 0}, {"x": 0}, {"x": 1}, {"x": 1}),
    ({"x": 0}, {"x": 2}, {"x": 0}, {"x": 2}),
    ({"x": 0}, {"x": 2}, {"x": 2}, {"x": 2}),
    ({"x": 0}, {}, {}, {}),
    ({}, {"x": {"a": 1}}, {"x": {"b": 2}}, {"x": {"a": 1, "b": 2}}),
    ({"x": {"a": 1, "b": 2}}, {"x": {"a": 3, "b": 2}}, {"x": {"a": 1}}, {"x": {"a": 3}}),
    ({"x": None}, {}, {"x": None}, {}),
    ({"x": True}, {"x": 1}, {"x": True}, {"x": 1}),
    ({"x": [1]}, {"x": [1, 2]}, {"x": [1]}, {"x": [1, 2]}),
])
def test_recursive_one_sided_and_identical_changes(base, target, source, expected):
    actual, conflicts = merge_documents(base, target, source)
    assert conflicts == [] and actual == expected


@pytest.mark.parametrize("base,target,source,path", [
    ({"x": 0}, {"x": 1}, {"x": 2}, "/x"),
    ({"x": 0}, {}, {"x": None}, "/x"),
    ({"x": None}, {}, {"x": 0}, "/x"),
    ({"x": {"n": 1}}, {}, {"x": {"n": 2}}, "/x"),
    ({"x": [0, 0]}, {"x": [1, 0]}, {"x": [0, 2]}, "/x"),
    ({"x": None}, {"x": True}, {"x": 1}, "/x"),
    ({"x": 0}, {"x": {}}, {"x": {"new": 1}}, "/x"),
    ({"a/b": {"~": 0}}, {"a/b": {"~": 1}}, {"a/b": {"~": 2}}, "/a~1b/~0"),
])
def test_conflict_types_are_explicit_and_arrays_atomic(base, target, source, path):
    _, conflicts = merge_documents(base, target, source)
    assert [conflict["path"] for conflict in conflicts] == [path]
    result, again = merge_documents(base, target, source, {path: "source"})
    assert result == source and again == conflicts


def test_missing_null_and_nested_inputs_are_not_mutated():
    base, target, source = {"x": 1}, {}, {"x": None}
    _, conflicts = merge_documents(base, target, source)
    assert conflicts[0]["target"] == {"exists": False, "value": None}
    assert conflicts[0]["source"] == {"exists": True, "value": None}
    result, _ = merge_documents({}, {"x": []}, {"y": []})
    result["x"].append(1)
    assert target == {}


def test_conflicting_apply_rolls_back_and_resolutions_can_delete(store):
    target, source = branches(store, {"x": 0, "z": 0})
    target = append(store, target, [{"op": "remove", "path": "/x"}, {"op": "replace", "path": "/z", "value": 1}])
    source = edit(store, edit(store, source, "/x", None), "/z", 2)
    preview = store.preview_merge(target["id"], source["id"])
    assert preview["merged_state"] is None
    assert {item["path"] for item in preview["conflicts"]} == {"/x", "/z"}
    with pytest.raises(StoreError) as caught:
        merge(store, target, source, idempotency_key="retry")
    assert caught.value.code == "merge_conflict" and caught.value.status == 409
    assert caught.value.details["base"] == preview["base"]
    assert store.get_timeline(target["id"]) == target
    with pytest.raises(StoreError) as caught:
        merge(store, target, source, resolutions=[{"path": "/x", "choice": "target"}])
    assert [item["path"] for item in caught.value.details["conflicts"]] == ["/z"]
    result = merge(store, target, source, resolutions=[{"path": "/x", "choice": "target"}, {"path": "/z", "choice": "source"}],
                   idempotency_key="retry")
    assert result.payload["state"]["state"] == {"z": 2}


@pytest.mark.parametrize("resolutions", [
    {}, [None], [{"path": "/a", "choice": "other"}], [{"path": "/a"}],
    [{"path": "/a", "choice": "source", "value": 9}],
    [{"path": "/a", "choice": "source"}, {"path": "/a", "choice": "target"}],
    [{"path": "/unknown", "choice": "source"}],
])
def test_invalid_resolutions_never_append(store, resolutions):
    target, source = branches(store)
    target, source = edit(store, target, "/a", 1), edit(store, source, "/a", 2)
    with pytest.raises(StoreError) as caught:
        merge(store, target, source, resolutions=resolutions)
    assert caught.value.code == "invalid_resolution" and caught.value.status == 422
    assert store.get_timeline(target["id"]) == target


def test_identical_content_does_not_invent_ancestry(store):
    left = store.create("Left", {"x": 1}).payload
    right = store.create("Right", {"x": 1}).payload
    with pytest.raises(StoreError) as caught:
        store.preview_merge(left["id"], right["id"])
    assert caught.value.code == "unrelated_timelines"


def test_source_version_is_pinned_and_previous_merge_changes_next_base(store):
    target, source = branches(store)
    target, source = edit(store, target, "/a", 1), edit(store, source, "/b", 2)
    pinned = source["head_version"]
    source = edit(store, source, "/later", True)
    merged = store.merge(target["id"], source["id"], pinned, target["head_version"]).payload["timeline"]
    assert "later" not in store.get_state(merged["id"])["state"]
    preview = store.preview_merge(merged["id"], source["id"])
    assert preview["base"]["timeline_id"] == source["id"] and preview["base"]["version"] == pinned
    assert preview["merged_state"] == {"a": 1, "b": 2, "later": True}
    again = merge(store, merged, source).payload
    assert again["state"]["state"]["later"] is True
    with pytest.raises(StoreError) as caught:
        merge(store, again["timeline"], source)
    assert caught.value.code == "already_merged"


def test_siblings_nested_forks_and_inherited_merge_metadata(store):
    original = store.create("Original", {"a": 0, "b": 0}).payload
    left = store.fork(original["id"], "Left", 0).payload
    right = store.fork(original["id"], "Right", 0).payload
    left, right = edit(store, left, "/a", 1), edit(store, right, "/b", 2)
    merged = merge(store, left, right).payload
    nested = store.fork(left["id"], "Nested", merged["timeline"]["head_version"]).payload
    copied = store.get_events(nested["id"])["items"][-1]
    assert copied["inherited"] and copied["merge"] == merged["event"]["merge"]
    assert store.export(nested["id"])["events"][-1]["merge"] == copied["merge"]
    right = edit(store, right, "/b", 3)
    preview = store.preview_merge(nested["id"], right["id"])
    assert preview["base"]["timeline_id"] == right["id"] and preview["base"]["version"] == 1
    assert merge(store, nested, right).payload["state"]["state"] == {"a": 1, "b": 3}


def test_fork_aliases_shared_prefix_instead_of_treating_equal_states_as_new_nodes(store):
    target = store.create("Original", {"n": 0}).payload
    target = edit(store, target, "/n", 1)
    source = store.fork(target["id"], "Fork", 1).payload
    nested = store.fork(source["id"], "Nested", 1).payload
    nested = edit(store, nested, "/n", 2)
    preview = store.preview_merge(target["id"], nested["id"])
    assert preview["base"]["timeline_id"] == target["id"] and preview["base"]["version"] == 1
    assert preview["merged_state"] == {"n": 2}


def test_criss_cross_histories_reject_multiple_incomparable_bases(store):
    left, right = branches(store)
    left, right = edit(store, left, "/a", 1), edit(store, right, "/b", 2)
    merged_left = merge(store, left, right).payload["timeline"]
    merged_right = store.merge(right["id"], left["id"], 1, 1).payload["timeline"]
    with pytest.raises(StoreError) as caught:
        store.preview_merge(merged_left["id"], merged_right["id"])
    assert caught.value.code == "ambiguous_merge_base"
    assert len(caught.value.details["candidates"]) == 2


def test_experiment_rejects_replaying_merges_but_preserves_prefix_merges(store):
    target, source = branches(store)
    target, source = edit(store, target, "/a", 1), edit(store, source, "/b", 2)
    merged = merge(store, target, source).payload["timeline"]
    merged = edit(store, merged, "/later", 1)
    for version in (1, 2):
        with pytest.raises(StoreError) as caught:
            store.experiment(merged["id"], "Unsafe replay", version, [{"op": "add", "path": "/a", "value": 9}])
        assert caught.value.code == "merge_replay_unsupported"
    assert len(store.list_timelines()["items"]) == 2
    safe = store.experiment(merged["id"], "Safe", 3, [{"op": "add", "path": "/later", "value": 9}]).payload["timeline"]
    assert store.get_events(safe["id"])["items"][1]["merge"]["source_timeline_id"] == source["id"]
    source = edit(store, source, "/b", 4)
    assert store.preview_merge(safe["id"], source["id"])["merged_state"] == {"a": 1, "b": 4, "later": 9}


def test_merge_keys_replay_original_success_before_expected_version_checks(store):
    target, source = branches(store)
    source = edit(store, source, "/b", 2)
    result = merge(store, target, source, idempotency_key="once")
    edit(store, result.payload["timeline"], "/a", 9)
    replay = merge(store, target, source, idempotency_key="once")
    assert replay.replayed and replay.payload == result.payload
    with pytest.raises(StoreError) as caught:
        merge(store, target, source, idempotency_key="once", label="Changed")
    assert caught.value.code == "idempotency_conflict"


def test_concurrent_target_edit_or_merge_has_exactly_one_winner(store):
    target, source = branches(store)
    source = edit(store, source, "/b", 2)
    def write(kind):
        try:
            if kind == "merge":
                return merge(Store(store.path), target, source)
            return edit(Store(store.path), target, "/a", 1)
        except StoreError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["merge", "edit"]))
    failures = [result for result in results if isinstance(result, StoreError)]
    assert len(failures) == 1 and failures[0].code == "version_conflict"
    assert store.get_timeline(target["id"])["head_version"] == 1


def test_merge_edges_survive_reopening_and_existing_database_migration(store):
    target, source = branches(store)
    source = edit(store, source, "/b", 2)
    previous = store.export(source["id"])
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE merge_parents")
    migrated = Store(store.path)
    assert migrated.export(source["id"]) == previous
    target = merge(migrated, target, source).payload["timeline"]
    source = edit(migrated, source, "/b", 3)
    reopened = Store(store.path)
    assert reopened.preview_merge(target["id"], source["id"])["base"]["version"] == 1
    assert reopened.get_events(target["id"])["items"][-1]["merge"]["source_version"] == 1


def test_combined_state_limit_failure_writes_no_event_or_ancestry(store):
    target, source = branches(store, {})
    target, source = edit(store, target, "/a", "x" * 70000), edit(store, source, "/b", "y" * 70000)
    with pytest.raises(StoreError) as caught:
        merge(store, target, source)
    assert caught.value.code == "state_too_large"
    assert store.get_timeline(target["id"]) == target
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM merge_parents").fetchone()[0] == 0


def test_self_merge_and_ancestor_only_source_are_not_new_versions(store):
    target, source = branches(store)
    target = edit(store, target, "/a", 1)
    for source_id, version in ((target["id"], 1), (target["id"], 0), (source["id"], 0)):
        with pytest.raises(StoreError) as caught:
            store.merge(target["id"], source_id, version, target["head_version"])
        assert caught.value.code == "already_merged"
    assert store.get_timeline(target["id"]) == target


def test_all_target_choices_still_record_source_ancestry(store):
    target, source = branches(store)
    target, source = edit(store, target, "/a", 1), edit(store, source, "/a", 2)
    result = merge(store, target, source, resolutions=[{"path": "/a", "choice": "target"}]).payload
    assert result["state"]["state"] == {"a": 1, "b": 0}
    assert result["timeline"]["head_version"] == 2
    assert result["event"]["merge"]["source_version"] == 1
    with pytest.raises(StoreError) as caught:
        merge(store, result["timeline"], source)
    assert caught.value.code == "already_merged"
    source = edit(store, source, "/b", 3)
    preview = store.preview_merge(result["timeline"]["id"], source["id"])
    assert preview["base"]["timeline_id"] == source["id"]
    assert preview["merged_state"] == {"a": 1, "b": 3}


@pytest.mark.parametrize("version", [None, -1, True, 0.5])
def test_apply_requires_pinned_valid_source_version(store, version):
    target, source = branches(store)
    with pytest.raises(StoreError):
        store.merge(target["id"], source["id"], version, 0)
