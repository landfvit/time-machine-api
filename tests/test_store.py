from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from timemachine.patches import apply_operations
from timemachine.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "nested" / "history.sqlite3")


def create(store, state=None, name="History"):
    return store.create(name, {"n": 0, "nested": {"items": []}} if state is None else state).payload


def increment(store, timeline, amount=1, key=None):
    return store.append(timeline["id"], "Adjust", [{"op": "increment", "path": "/n", "value": amount}],
                        timeline["head_version"], idempotency_key=key).payload


def test_creation_initial_state_and_disk_persistence(store):
    original = {"x": None, "a": [1, True], "n": 0}
    timeline = create(store, original)
    original["a"].append("later")
    assert timeline["head_version"] == 0
    assert timeline["parent_id"] is None
    snapshot = store.get_state(timeline["id"])
    assert snapshot["state"] == {"x": None, "a": [1, True], "n": 0}
    assert snapshot["state_hash"] == timeline["head_hash"]
    assert store.health() == {"status": "ok"}
    store.close()
    reopened = Store(store.path)
    assert reopened.get_timeline(timeline["id"]) == timeline


def test_append_and_all_historical_states(store):
    timeline = create(store)
    first = increment(store, timeline, 10)
    second = increment(store, first["timeline"], -3)
    assert first["event"]["version"] == 1
    assert first["state"]["state"]["n"] == 10
    assert second["state"]["state"]["n"] == 7
    assert not first["event"]["inherited"]
    assert first["event"]["source_event_id"] is None
    assert store.get_state(timeline["id"], version=0)["state"]["n"] == 0
    assert store.get_state(timeline["id"], version=1)["state"]["n"] == 10
    assert store.get_state(timeline["id"])["version"] == 2
    assert timeline["initial_at"] < first["event"]["recorded_at"] < second["event"]["recorded_at"]
    assert store.get_events(timeline["id"])["items"] == [first["event"], second["event"]]


def test_time_queries_include_boundary_and_normalize_offsets(store):
    timeline = create(store)
    event = increment(store, timeline)["event"]
    instant = datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00"))
    before = (instant - timedelta(microseconds=1)).isoformat()
    assert store.get_state(timeline["id"], at=before)["version"] == 0
    assert store.get_state(timeline["id"], at=event["recorded_at"])["version"] == 1
    offset = instant.astimezone(timezone(timedelta(hours=7))).isoformat()
    assert store.get_state(timeline["id"], at=offset)["version"] == 1
    assert store.get_state(timeline["id"], at=instant)["version"] == 1
    assert store.get_state(timeline["id"], at=timeline["initial_at"])["version"] == 0
    with pytest.raises(StoreError) as caught:
        store.get_state(timeline["id"], at="2000-01-01T00:00:00Z")
    assert caught.value.status == 404


@pytest.mark.parametrize("at", ["2020-01-01", "2020-01-01T00:00:00", "x", 0, "2020-33-01T00:00:00Z"])
def test_invalid_timestamp_queries(store, at):
    timeline = create(store)
    with pytest.raises(StoreError) as caught:
        store.get_state(timeline["id"], at=at)
    assert caught.value.status == 422


def test_patch_and_version_conflicts_are_atomic(store):
    timeline = create(store)
    with pytest.raises(StoreError):
        store.append(timeline["id"], "Failure", [{"op": "increment", "path": "/n", "value": 10},
                                                  {"op": "remove", "path": "/missing"}], 0)
    assert store.get_timeline(timeline["id"])["head_version"] == 0
    assert store.get_events(timeline["id"])["items"] == []
    increment(store, timeline)
    with pytest.raises(StoreError) as caught:
        increment(store, timeline)
    assert caught.value.status == 409
    assert caught.value.code == "version_conflict"
    assert caught.value.details == {"expected_version": 0, "head_version": 1}


def test_successful_create_and_append_keys_replay_before_stale_version(store):
    first = store.create("History", {"n": 0}, "create-key")
    repeated = store.create("History", {"n": 0}, "create-key")
    assert repeated.replayed and repeated.payload == first.payload
    written = increment(store, first.payload, 10, "event-key")
    replayed = store.append(first.payload["id"], "Adjust", [{"op": "increment", "path": "/n", "value": 10}], 0, "event-key")
    assert replayed.replayed and replayed.payload == written
    assert store.get_timeline(first.payload["id"])["head_version"] == 1
    assert len(store.list_timelines()["items"]) == 1
    with pytest.raises(StoreError) as caught:
        store.create("Changed", {"n": 0}, "create-key")
    assert caught.value.code == "idempotency_conflict"
    with pytest.raises(StoreError) as caught:
        increment(store, first.payload, 11, "event-key")
    assert caught.value.code == "idempotency_conflict"


def test_failed_write_does_not_reserve_key(store):
    timeline = create(store)
    with pytest.raises(StoreError):
        store.append(timeline["id"], "Adjust", [{"op": "increment", "path": "/missing", "value": 1}], 0, "retry")
    result = increment(store, timeline, 4, "retry")
    assert result["state"]["state"]["n"] == 4


def test_idempotency_scopes_are_independent_between_timelines(store):
    left, right = create(store), create(store)
    assert increment(store, left, 1, "shared")["state"]["state"]["n"] == 1
    assert increment(store, right, 2, "shared")["state"]["state"]["n"] == 2


def test_concurrent_appends_cannot_overwrite_or_duplicate_version(store):
    timeline = create(store)
    def write(_):
        try:
            return increment(Store(store.path), timeline)
        except StoreError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(6)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert all(item.code == "version_conflict" for item in results if isinstance(item, StoreError))
    assert store.get_state(timeline["id"])["state"]["n"] == 1


def test_concurrent_identical_idempotent_requests_commit_once(store):
    timeline = create(store)
    def write(_):
        return Store(store.path).append(timeline["id"], "Adjust", [{"op": "increment", "path": "/n", "value": 1}], 0, "once")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(write, range(5)))
    assert sum(not item.replayed for item in results) == 1
    assert len({item.payload["event"]["id"] for item in results}) == 1


def test_fork_prefix_is_immutable_and_nested_branches_are_isolated(store):
    source = create(store)
    first = increment(store, source, 4)
    second = increment(store, first["timeline"], 6)
    branch = store.fork(source["id"], "Branch", 1).payload
    assert branch["parent_id"] == source["id"] and branch["fork_version"] == 1
    assert branch["initial_at"] == source["initial_at"]
    assert branch["head_hash"] == first["state"]["state_hash"]
    inherited = store.get_events(branch["id"])["items"][0]
    assert inherited["inherited"]
    assert inherited["source_event_id"] == first["event"]["id"]
    assert inherited["source_timeline_id"] == source["id"]
    assert inherited["recorded_at"] == first["event"]["recorded_at"]
    assert inherited["id"] != first["event"]["id"]
    changed = store.append(branch["id"], "Branch only", [{"op": "add", "path": "/nested/items/-", "value": {"x": 1}}], 1)
    grandchild = store.fork(branch["id"], "Another branch", 2).payload
    store.append(grandchild["id"], "Change nested", [{"op": "replace", "path": "/nested/items/0/x", "value": 9}], 2)
    assert store.get_state(branch["id"])["state"]["nested"]["items"] == [{"x": 1}]
    assert store.get_state(source["id"])["state"] == second["state"]["state"]
    assert store.get_state(branch["id"], at=first["event"]["recorded_at"])["version"] == 1
    assert changed.payload["event"]["inherited"] is False


def test_zero_and_current_version_forks_and_cached_default_head(store):
    source = create(store)
    first = increment(store, source)
    zero = store.fork(source["id"], "Start", 0).payload
    assert store.get_state(zero["id"])["state"]["n"] == 0
    branch = store.fork(source["id"], "Current", idempotency_key="fork")
    increment(store, first["timeline"])
    cached = store.fork(source["id"], "Current", idempotency_key="fork")
    assert cached.replayed and cached.payload == branch.payload
    assert branch.payload["head_version"] == 1


def test_experiment_replays_effects_instead_of_copying_old_snapshots(store):
    source = create(store)
    first = increment(store, source, 10)
    second = increment(store, first["timeline"], -3)
    third = increment(store, second["timeline"], 2)
    altered = store.experiment(source["id"], "Alternative", 1, [{"op": "increment", "path": "/n", "value": 20}],
                               label="More delivered", idempotency_key="experiment")
    branch = altered.payload["timeline"]
    assert altered.payload["replayed_events"] == 2
    assert altered.payload["changed_version"] == 1
    assert store.get_state(branch["id"])["state"]["n"] == 19
    assert store.get_state(source["id"])["state"]["n"] == 9
    events = store.get_events(branch["id"])["items"]
    assert events[0]["label"] == "More delivered"
    assert [event["version"] for event in events] == [1, 2, 3]
    assert all(not event["inherited"] for event in events)
    assert events[0]["source_event_id"] == first["event"]["id"]
    assert events[1]["source_event_id"] == second["event"]["id"]
    assert events[2]["source_event_id"] == third["event"]["id"]
    assert events[0]["recorded_at"] > third["event"]["recorded_at"]
    replay = store.experiment(source["id"], "Alternative", 1, [{"op": "increment", "path": "/n", "value": 20}],
                              label="More delivered", idempotency_key="experiment")
    assert replay.replayed and replay.payload == altered.payload


def test_experiment_copies_only_prefix_and_replays_later_absolute_replacements(store):
    source = create(store)
    first = increment(store, source, 4)
    second = increment(store, first["timeline"], 3)
    store.append(source["id"], "Set exact value", [{"op": "replace", "path": "/n", "value": 100}], 2)
    branch = store.experiment(source["id"], "Different middle", 2, [{"op": "increment", "path": "/n", "value": 20}]).payload["timeline"]
    assert store.get_state(branch["id"], version=2)["state"]["n"] == 24
    assert store.get_state(branch["id"])["state"]["n"] == 100
    assert branch["fork_version"] == 1
    assert [event["inherited"] for event in store.get_events(branch["id"])["items"]] == [True, False, False]


def test_failed_replay_rolls_back_every_branch_row_and_retry_key(store):
    source = create(store)
    first = increment(store, source, 4)
    store.append(source["id"], "Assert count", [{"op": "test", "path": "/n", "value": 4}], 1)
    before = store.export(source["id"])
    with pytest.raises(StoreError) as caught:
        store.experiment(source["id"], "Impossible", 1, [{"op": "increment", "path": "/n", "value": 8}], idempotency_key="try")
    assert caught.value.status == 409 and caught.value.code == "replay_conflict"
    assert caught.value.details["source_version"] == 2
    assert caught.value.details["reason"] == "test_failed"
    assert store.export(source["id"]) == before
    assert len(store.list_timelines()["items"]) == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 0
    good = store.experiment(source["id"], "Possible", 1, [{"op": "increment", "path": "/n", "value": 4}], idempotency_key="try")
    assert good.payload["timeline"]["head_version"] == 2


def test_invalid_replacement_also_leaves_no_branch(store):
    source = create(store)
    increment(store, source)
    with pytest.raises(StoreError) as caught:
        store.experiment(source["id"], "Bad", 1, [{"op": "remove", "path": "/missing"}])
    assert caught.value.details["source_version"] == 1
    assert len(store.list_timelines()["items"]) == 1


def test_compare_and_export_preserve_history_and_json_types(store):
    left = create(store, {"flag": True, "items": [True, None], "a/b": {"~": 2}})
    right = create(store, {"flag": 1, "items": [1, None, 3], "a/b": {"~": 9}})
    diff = store.compare(left["id"], right["id"])
    patched = apply_operations(diff["left"]["state"], diff["changes"])
    assert type(patched["flag"]) is int
    assert type(patched["items"][0]) is int
    assert patched == diff["right"]["state"]
    assert store.compare(left["id"], left["id"])["changes"] == []
    current = store.append(left["id"], "Note", [{"op": "add", "path": "/note", "value": "later"}], 0).payload
    old_export = store.export(left["id"], version=0)
    assert old_export["events"] == [] and "note" not in old_export["state"]["state"]
    assert old_export["timeline"]["head_version"] == 1
    assert store.export(left["id"])["events"] == [current["event"]]
    json.dumps(store.export(left["id"]), allow_nan=False)


def test_pagination_is_complete_without_duplicates(store):
    timelines = [create(store, name=f"Timeline {index}") for index in range(5)]
    first = store.list_timelines(limit=2)
    second = store.list_timelines(limit=2, cursor=first["next_cursor"])
    third = store.list_timelines(limit=2, cursor=second["next_cursor"])
    assert [row["id"] for page in (first, second, third) for row in page["items"]] == [row["id"] for row in timelines]
    assert third["next_cursor"] is None
    source = timelines[0]
    for _ in range(5):
        source = increment(store, source)["timeline"]
    page1 = store.get_events(source["id"], limit=2)
    page2 = store.get_events(source["id"], after_version=page1["next_after_version"], limit=2)
    page3 = store.get_events(source["id"], after_version=page2["next_after_version"], limit=2)
    assert [event["version"] for page in (page1, page2, page3) for event in page["items"]] == [1, 2, 3, 4, 5]
    assert page3["next_after_version"] is None


@pytest.mark.parametrize("cursor", ["x", "%%%", "e30", "W10", "bnVsbA", 5])
def test_invalid_pagination_cursors(store, cursor):
    with pytest.raises(StoreError) as caught:
        store.list_timelines(cursor=cursor)
    assert caught.value.code == "invalid_cursor"


def test_examples_are_complete_and_idempotent(store):
    example = store.create_example("example")
    replay = store.create_example("example")
    assert replay.replayed and replay.payload == example.payload
    assert example.payload["head_version"] == 6
    state = store.get_state(example.payload["id"])["state"]
    assert state["stock"] == {"frames": 18, "wheels": 40}
    assert state["orders"]["built"] == 10
    assert len(state["notes"]) == 1
    assert len(store.list_timelines()["items"]) == 1


def test_limits_rollback_and_replays_remain_available(store, monkeypatch):
    import timemachine.store as module
    monkeypatch.setattr(module, "MAX_TIMELINES", 2)
    first = store.create("First", {"n": 0}, "one").payload
    create(store, name="Second")
    with pytest.raises(StoreError) as caught:
        create(store)
    assert caught.value.code == "timeline_limit"
    assert store.create("First", {"n": 0}, "one").replayed
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    result = increment(store, first, key="event")
    with pytest.raises(StoreError) as caught:
        increment(store, result["timeline"])
    assert caught.value.code == "event_limit"
    assert increment(store, first, key="event") == result


def test_missing_resources_and_invalid_versions(store):
    with pytest.raises(StoreError) as caught:
        store.get_timeline("missing")
    assert caught.value.status == 404
    timeline = create(store)
    for method in (store.get_state, store.fork, store.export):
        with pytest.raises(StoreError):
            if method == store.fork:
                method(timeline["id"], "Future", version=1)
            else:
                method(timeline["id"], version=1)
    for value in (-1, True, 0.5, "1"):
        with pytest.raises(StoreError):
            store.get_state(timeline["id"], version=value)
    with pytest.raises(StoreError):
        store.get_state(timeline["id"], version=0, at=timeline["initial_at"])
    with pytest.raises(StoreError):
        store.experiment(timeline["id"], "Nothing to change", 0, [])


@pytest.mark.parametrize("key", ["", "x" * 129, 3, "\ud800"])
def test_invalid_idempotency_keys(store, key):
    with pytest.raises(StoreError):
        store.create("History", {}, key)


def test_invalid_names_and_strict_json(store):
    for name in ("", "   ", None, "x" * 121, "\ud800"):
        with pytest.raises(StoreError):
            create(store, name=name)
    for state in ([], None, {"x": float("nan")}):
        with pytest.raises(StoreError):
            store.create("History", state)


def test_checkpoint_boundaries_forks_and_experiments_reconstruct_exact_states(store):
    source = create(store, {"n": 0})
    for _ in range(105):
        source = increment(store, source)["timeline"]
    for version in (0, 1, 49, 50, 51, 99, 100, 101, 105):
        snapshot = store.get_state(source["id"], version=version)
        assert snapshot["state"] == {"n": version}
        if version:
            event = store.get_events(source["id"], after_version=version - 1, limit=1)["items"][0]
            assert snapshot["recorded_at"] == event["recorded_at"]
            assert snapshot["state_hash"] == event["state_hash"]
    branch = store.fork(source["id"], "Checkpoint fork", 100).payload
    assert store.get_state(branch["id"], version=100)["state"] == {"n": 100}
    assert increment(store, branch, 7)["state"]["state"] == {"n": 107}
    alternate = store.experiment(source["id"], "Across checkpoints", 50,
                                  [{"op": "increment", "path": "/n", "value": 11}]).payload["timeline"]
    assert store.get_state(alternate["id"], version=49)["state"] == {"n": 49}
    assert store.get_state(alternate["id"], version=50)["state"] == {"n": 60}
    assert store.get_state(alternate["id"], version=100)["state"] == {"n": 110}
    assert store.get_state(alternate["id"])["state"] == {"n": 115}
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoints WHERE timeline_id=?", (source["id"],)).fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM checkpoints WHERE timeline_id=?", (alternate["id"],)).fetchone()[0] == 2
