from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import pytest

from app import MAX_REQUEST_BYTES, create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "history.sqlite3", api_key="")) as active:
        yield active


def create(client, name="Workshop", initial=None, headers=None):
    response = client.post("/v1/timelines", json={
        "name": name, "initial_state": initial if initial is not None else {"stock": 10, "cash": 0}
    }, headers=headers)
    assert response.status_code == 201, response.text
    assert response.headers["location"] == f"/v1/timelines/{response.json()['id']}"
    return response


def append(client, timeline_id, version, amount=1, headers=None):
    return client.post(f"/v1/timelines/{timeline_id}/events", json={
        "label": "Delivery", "expected_version": version,
        "operations": [{"op": "increment", "path": "/stock", "value": amount}],
    }, headers=headers)


def test_create_append_and_historical_reads(client):
    timeline = create(client).json()
    assert timeline["head_version"] == 0
    assert timeline["parent_id"] is None
    first = client.get(f"/v1/timelines/{timeline['id']}/state").json()
    response = append(client, timeline["id"], 0, 4)
    assert response.status_code == 201
    assert response.json()["state"]["state"] == {"stock": 14, "cash": 0}
    assert response.json()["event"]["version"] == 1
    assert client.get(response.headers["location"]).json()["version"] == 1
    old = client.get(f"/v1/timelines/{timeline['id']}/state", params={"version": 0}).json()
    assert old["state"] == first["state"]
    assert old["head_version"] == 1
    assert old["state_hash"] == first["state_hash"]


def test_historical_timestamp_is_inclusive_and_requires_timezone(client):
    timeline_id = create(client).json()["id"]
    base = client.get(f"/v1/timelines/{timeline_id}/state").json()
    added = append(client, timeline_id, 0, 4).json()["state"]
    for expected in (base, added):
        response = client.get(f"/v1/timelines/{timeline_id}/state", params={"at": expected["recorded_at"]})
        assert response.status_code == 200
        assert response.json()["state"] == expected["state"]
        assert response.json()["version"] == expected["version"]
    assert client.get(f"/v1/timelines/{timeline_id}/state", params={"at": "2025-01-01T00:00:00"}).status_code == 422
    assert client.get(f"/v1/timelines/{timeline_id}/state", params={"at": added["recorded_at"], "version": 0}).status_code == 422


def test_etag_represents_selected_state_and_current_head(client):
    timeline_id = create(client).json()["id"]
    url = f"/v1/timelines/{timeline_id}/state?version=0"
    before = client.get(url)
    etag = before.headers["etag"]
    unchanged = client.get(url, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag
    assert client.get(url, headers={"If-None-Match": f'"other", W/{etag}'}).status_code == 304
    append(client, timeline_id, 0)
    changed = client.get(url, headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert changed.json()["head_version"] == 1
    assert changed.json()["state"] == before.json()["state"]


def test_json_patch_variants_and_null_values(client):
    timeline_id = create(client, initial={"a": 1, "items": ["x"], "keep": True}).json()["id"]
    response = client.post(f"/v1/timelines/{timeline_id}/events", json={
        "label": "Edit document", "expected_version": 0, "operations": [
            {"op": "test", "path": "/a", "value": 1},
            {"op": "copy", "from": "/a", "path": "/copy"},
            {"op": "move", "from": "/a", "path": "/moved"},
            {"op": "add", "path": "/items/-", "value": None},
            {"op": "replace", "path": "/moved", "value": 2},
            {"op": "remove", "path": "/keep"},
        ],
    })
    assert response.status_code == 201, response.text
    assert response.json()["state"]["state"] == {"copy": 1, "moved": 2, "items": ["x", None]}


def test_failing_patch_is_atomic(client):
    timeline_id = create(client).json()["id"]
    response = client.post(f"/v1/timelines/{timeline_id}/events", json={
        "label": "Fail halfway", "expected_version": 0, "operations": [
            {"op": "increment", "path": "/stock", "value": 50},
            {"op": "remove", "path": "/missing"},
        ],
    })
    assert response.status_code == 422
    state = client.get(f"/v1/timelines/{timeline_id}/state").json()
    assert state["version"] == 0
    assert state["state"]["stock"] == 10
    assert client.get(f"/v1/timelines/{timeline_id}/events").json()["items"] == []


def test_stale_writer_and_concurrent_requests(client):
    timeline_id = create(client).json()["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: append(client, timeline_id, 0), range(2)))
    assert sorted(item.status_code for item in responses) == [201, 409]
    state = client.get(f"/v1/timelines/{timeline_id}/state").json()
    assert state["version"] == 1
    assert state["state"]["stock"] == 11


def test_create_retry_and_conflicting_idempotency_key(client):
    header = {"Idempotency-Key": "create-workshop"}
    first = create(client, headers=header)
    replayed = create(client, headers=header)
    assert replayed.json() == first.json()
    assert replayed.headers["Idempotent-Replayed"] == "true"
    conflict = client.post("/v1/timelines", json={"name": "Changed", "initial_state": {}}, headers=header)
    assert conflict.status_code == 409
    assert len(client.get("/v1/timelines").json()["items"]) == 1


def test_concurrent_append_retries_create_one_event(client):
    timeline_id = create(client).json()["id"]
    header = {"Idempotency-Key": "one-delivery"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: append(client, timeline_id, 0, 5, header), range(2)))
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert sum(response.headers.get("Idempotent-Replayed") == "true" for response in responses) == 1
    assert client.get(f"/v1/timelines/{timeline_id}/state").json()["state"]["stock"] == 15


def test_fork_inherits_history_and_then_diverges(client):
    timeline_id = create(client).json()["id"]
    append(client, timeline_id, 0, 2)
    append(client, timeline_id, 1, -3)
    forked = client.post(f"/v1/timelines/{timeline_id}/fork", json={"name": "Alternative", "version": 1})
    assert forked.status_code == 201
    branch = forked.json()
    assert branch["parent_id"] == timeline_id
    assert branch["head_version"] == branch["fork_version"] == 1
    inherited = client.get(f"/v1/timelines/{branch['id']}/events").json()["items"]
    assert len(inherited) == 1
    assert inherited[0]["inherited"] is True
    append(client, branch["id"], 1, 20)
    comparison = client.get("/v1/compare", params={"left": timeline_id, "right": branch["id"]})
    assert comparison.status_code == 200
    assert comparison.json()["left"]["state"]["stock"] == 9
    assert comparison.json()["right"]["state"]["stock"] == 32
    assert {"op": "replace", "path": "/stock", "value": 32} in comparison.json()["changes"]


def test_experiment_replays_later_relative_changes(client):
    timeline_id = create(client).json()["id"]
    append(client, timeline_id, 0, 5)
    append(client, timeline_id, 1, -2)
    response = client.post(f"/v1/timelines/{timeline_id}/experiment", json={
        "name": "Larger delivery", "event_version": 1,
        "operations": [{"op": "increment", "path": "/stock", "value": 20}],
    })
    assert response.status_code == 201, response.text
    assert response.json()["changed_version"] == 1
    branch_id = response.json()["timeline"]["id"]
    assert client.get(f"/v1/timelines/{branch_id}/state").json()["state"]["stock"] == 28
    assert client.get(f"/v1/timelines/{timeline_id}/state").json()["state"]["stock"] == 13


def test_experiment_replay_failure_saves_no_branch(client):
    timeline_id = create(client).json()["id"]
    append(client, timeline_id, 0, 1)
    append(client, timeline_id, 1, -1)
    response = client.post(f"/v1/timelines/{timeline_id}/experiment", json={
        "name": "Broken history", "event_version": 1,
        "operations": [{"op": "remove", "path": "/stock"}],
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "replay_conflict"
    assert len(client.get("/v1/timelines").json()["items"]) == 1


def test_timeline_and_event_pagination(client):
    for name in ("One", "Two", "Three"):
        create(client, name=name)
    first = client.get("/v1/timelines", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]
    second = client.get("/v1/timelines", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    timeline_id = first["items"][0]["id"]
    for version in range(3):
        assert append(client, timeline_id, version).status_code == 201
    page = client.get(f"/v1/timelines/{timeline_id}/events", params={"limit": 2}).json()
    assert [row["version"] for row in page["items"]] == [1, 2]
    tail = client.get(f"/v1/timelines/{timeline_id}/events", params={"after_version": page["next_after_version"]}).json()
    assert [row["version"] for row in tail["items"]] == [3]


def test_export_is_an_attachment_at_the_selected_version(client):
    timeline_id = create(client).json()["id"]
    append(client, timeline_id, 0)
    response = client.get(f"/v1/timelines/{timeline_id}/export", params={"version": 0})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('attachment; filename="timeline-')
    exported = response.json()
    assert set(exported) == {"timeline", "initial_state", "events", "state"}
    assert exported["timeline"]["head_version"] == 1
    assert exported["initial_state"] == {"stock": 10, "cash": 0}
    assert exported["events"] == []
    assert exported["state"]["version"] == 0


def test_restart_preserves_history(tmp_path):
    database = tmp_path / "history.sqlite3"
    with TestClient(create_app(database, api_key="")) as first:
        timeline_id = create(first).json()["id"]
        append(first, timeline_id, 0, 7)
    with TestClient(create_app(database, api_key="")) as restarted:
        assert restarted.get(f"/v1/timelines/{timeline_id}/state").json()["state"]["stock"] == 17


def test_example_is_a_real_queryable_timeline(client):
    response = client.post("/v1/examples/inventory", headers={"Idempotency-Key": "sample"})
    assert response.status_code == 201
    timeline_id = response.json()["id"]
    assert response.json()["head_version"] > 0
    assert client.get(f"/v1/timelines/{timeline_id}/events").json()["items"]
    replayed = client.post("/v1/examples/inventory", headers={"Idempotency-Key": "sample"})
    assert replayed.json() == response.json()
    assert replayed.headers["Idempotent-Replayed"] == "true"


@pytest.mark.parametrize("body", [
    '{"name":"one","name":"two","initial_state":{}}',
    '{"name":"one","initial_state":{"number":NaN}}',
    '{"name":"one","initial_state":{"number":Infinity}}',
    '{"name":"one","initial_state":{"number":1e999}}',
    '{"name":"\\ud800","initial_state":{}}',
    '{"name":',
])
def test_invalid_json_is_rejected_before_mutation(client, body):
    response = client.post("/v1/timelines", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"
    assert client.get("/v1/timelines").json()["items"] == []


def test_body_size_limit_and_wrong_content_type(client):
    oversized = json.dumps({"name": "Large", "initial_state": {"text": "x" * MAX_REQUEST_BYTES}})
    response = client.post("/v1/timelines", content=oversized, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    response = client.post("/v1/timelines", content='{"name":"One","initial_state":{}}', headers={"Content-Type": "text/plain"})
    assert response.status_code == 400


def test_snapshot_size_limit_returns_413_without_mutation(client):
    response = client.post("/v1/timelines", json={
        "name": "Large snapshot", "initial_state": {"text": "x" * (128 * 1024)}
    })
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "state_too_large"
    assert client.get("/v1/timelines").json()["items"] == []
    timeline_id = create(client).json()["id"]
    response = client.post(f"/v1/timelines/{timeline_id}/events", json={
        "label": "Large value", "expected_version": 0,
        "operations": [{"op": "add", "path": "/text", "value": "x" * (128 * 1024)}],
    })
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "state_too_large"
    assert client.get(f"/v1/timelines/{timeline_id}/state").json()["version"] == 0


@pytest.mark.parametrize("body", [
    {"name": "Missing state"},
    {"name": "List state", "initial_state": []},
    {"name": "Extra", "initial_state": {}, "unexpected": 1},
])
def test_contract_validation_has_a_consistent_error_envelope(client, body):
    response = client.post("/v1/timelines", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["issues"]
    assert "input" not in error["details"]["issues"][0]


@pytest.mark.parametrize("operation", [
    {"op": "increment", "path": "/stock", "value": True},
    {"op": "add", "path": "/new"},
    {"op": "remove", "path": "/stock", "value": 1},
    {"op": "unknown", "path": "/stock"},
])
def test_malformed_operation_is_rejected(client, operation):
    timeline_id = create(client).json()["id"]
    response = client.post(f"/v1/timelines/{timeline_id}/events", json={
        "label": "Invalid", "expected_version": 0, "operations": [operation]
    })
    assert response.status_code == 422
    assert client.get(f"/v1/timelines/{timeline_id}").json()["head_version"] == 0


def test_missing_resources_and_bad_queries_use_error_envelopes(client):
    response = client.get("/v1/timelines/not-present")
    assert response.status_code == 404
    assert set(response.json()) == {"error"}
    response = client.get("/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert client.get("/v1/timelines", params={"limit": 0}).status_code == 422
    assert client.get("/v1/compare").status_code == 422


def test_api_key_protects_all_v1_routes_without_exposing_the_key(tmp_path):
    with TestClient(create_app(tmp_path / "private.sqlite3", api_key="local-test-secret")) as private:
        health = private.get("/health")
        assert health.json() == {"status": "ok", "api_key_required": True}
        assert "local-test-secret" not in health.text
        assert private.get("/docs").status_code == 200
        denied = private.get("/v1/timelines")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "ApiKey"
        assert private.get("/v1/timelines", headers={"X-API-Key": "wrong"}).status_code == 401
        assert private.get("/v1/timelines", headers={"X-API-Key": "local-test-secret"}).status_code == 200
        schema = private.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["LocalApiKey"]["name"] == "X-API-Key"
        assert schema["paths"]["/v1/timelines"]["get"]["security"] == [{"LocalApiKey": []}]


@pytest.mark.parametrize("host", [
    "attacker.example", "localhost.attacker.example", "example.com:8000",
    "localhost?evil", "localhost#evil", "localhost?", "localhost#",
])
def test_non_localhost_hostnames_are_rejected(client, host):
    response = client.get("/health", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_host"


@pytest.mark.parametrize("host", ["localhost:8001", "127.0.0.1:8001", "[::1]:8001"])
def test_localhost_addresses_are_accepted(client, host):
    assert client.get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("origin", ["https://example.com", "null", "http://testserver:9000", "http://testserver.attacker.example"])
def test_cross_origin_browser_writes_are_rejected(client, origin):
    response = client.post("/v1/timelines", json={"name": "Blocked", "initial_state": {}}, headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"
    assert client.get("/v1/timelines").json()["items"] == []


def test_same_origin_browser_write_and_non_browser_api_write(client):
    create(client, headers={"Origin": "http://testserver"})
    create(client, name="Terminal request")
    assert len(client.get("/v1/timelines").json()["items"]) == 2


def test_openapi_documents_contracts_and_examples(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "TimeMachineAPI"
    assert schema["components"]["schemas"]["CreateTimeline"]["examples"]
    assert schema["components"]["schemas"]["AppendEvent"]["additionalProperties"] is False
    responses = schema["paths"]["/v1/timelines/{timeline_id}/state"]["get"]["responses"]
    assert "304" in responses and "422" in responses
    parameters = schema["paths"]["/v1/timelines"]["post"]["parameters"]
    assert any(item["name"] == "idempotency-key" for item in parameters)
    exported = schema["paths"]["/v1/timelines/{timeline_id}/export"]["get"]["responses"]["200"]
    assert exported["content"]["application/json"]["schema"]["$ref"].endswith("/TimelineExport")
    assert set(schema["components"]["schemas"]["TimelineExport"]["properties"]) == {
        "timeline", "initial_state", "events", "state",
    }


def test_long_json_pointer_remains_usable_in_comparisons(client):
    key = "a" * 2100
    left = create(client, initial={key: 1}).json()["id"]
    right = create(client, initial={key: 2}).json()["id"]
    response = client.get("/v1/compare", params={"left": left, "right": right})
    assert response.status_code == 200
    assert response.json()["changes"] == [{"op": "replace", "path": f"/{key}", "value": 2}]


def test_unexpected_error_does_not_expose_internal_details(tmp_path, monkeypatch):
    application = create_app(tmp_path / "history.sqlite3", api_key="")
    with TestClient(application, raise_server_exceptions=False) as client:
        def fail(_timeline_id):
            raise RuntimeError("private database path and internal details")

        monkeypatch.setattr(application.state.store, "get_timeline", fail)
        response = client.get("/v1/timelines/example")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "private" not in response.text


def merge_branches(client):
    target_id = create(client).json()["id"]
    source_id = client.post(f"/v1/timelines/{target_id}/fork", json={"name": "Source", "version": 0}).json()["id"]
    assert append(client, target_id, 0, 2).status_code == 201
    return target_id, source_id


def test_merge_preview_and_apply_preserve_independent_changes(client):
    target_id, source_id = merge_branches(client)
    source_event = client.post(f"/v1/timelines/{source_id}/events", json={
        "label": "Add cash", "expected_version": 0,
        "operations": [{"op": "increment", "path": "/cash", "value": 15}],
    })
    assert source_event.status_code == 201
    preview = client.get(f"/v1/timelines/{target_id}/merge-preview", params={"source": source_id})
    assert preview.status_code == 200, preview.text
    assert preview.json()["base"]["version"] == 0
    assert preview.json()["conflicts"] == []
    assert preview.json()["merged_state"] == {"stock": 12, "cash": 15}
    merged = client.post(f"/v1/timelines/{target_id}/merge", json={
        "source_id": source_id, "source_version": 1, "expected_version": 1,
    }, headers={"Idempotency-Key": "merge-once"})
    assert merged.status_code == 201, merged.text
    assert merged.json()["state"]["state"] == preview.json()["merged_state"]
    assert merged.json()["event"]["version"] == 2
    assert merged.json()["event"]["merge"] == {
        "source_timeline_id": source_id, "source_version": 1,
        "base_timeline_id": target_id, "base_version": 0,
    }
    assert client.get(merged.headers["location"]).json()["state"] == preview.json()["merged_state"]
    source = client.get(f"/v1/timelines/{source_id}/state").json()
    assert source["version"] == 1
    assert source["state"] == {"stock": 10, "cash": 15}
    replayed = client.post(f"/v1/timelines/{target_id}/merge", json={
        "source_id": source_id, "source_version": 1, "expected_version": 1,
    }, headers={"Idempotency-Key": "merge-once"})
    assert replayed.status_code == 201
    assert replayed.json() == merged.json()
    assert replayed.headers["Idempotent-Replayed"] == "true"


def test_merge_conflicts_require_an_explicit_resolution(client):
    target_id, source_id = merge_branches(client)
    assert append(client, source_id, 0, 5).status_code == 201
    preview = client.get(f"/v1/timelines/{target_id}/merge-preview", params={"source": source_id, "source_version": 1})
    assert preview.status_code == 200
    assert preview.json()["merged_state"] is None
    assert preview.json()["conflicts"] == [{
        "path": "/stock", "base": {"exists": True, "value": 10},
        "target": {"exists": True, "value": 12}, "source": {"exists": True, "value": 15},
    }]
    body = {"source_id": source_id, "source_version": 1, "expected_version": 1}
    unresolved = client.post(f"/v1/timelines/{target_id}/merge", json=body)
    assert unresolved.status_code == 409
    assert unresolved.json()["error"]["code"] == "merge_conflict"
    assert client.get(f"/v1/timelines/{target_id}").json()["head_version"] == 1
    resolved = client.post(f"/v1/timelines/{target_id}/merge", json={
        **body, "resolutions": [{"path": "/stock", "choice": "source"}], "label": "Accept source stock",
    })
    assert resolved.status_code == 201, resolved.text
    assert resolved.json()["state"]["state"]["stock"] == 15
    assert resolved.json()["event"]["label"] == "Accept source stock"


def test_merge_pins_source_version_and_rejects_stale_target(client):
    target_id, source_id = merge_branches(client)
    client.post(f"/v1/timelines/{source_id}/events", json={
        "label": "First cash", "expected_version": 0,
        "operations": [{"op": "increment", "path": "/cash", "value": 5}],
    })
    client.post(f"/v1/timelines/{source_id}/events", json={
        "label": "Later cash", "expected_version": 1,
        "operations": [{"op": "increment", "path": "/cash", "value": 100}],
    })
    stale = client.post(f"/v1/timelines/{target_id}/merge", json={
        "source_id": source_id, "source_version": 1, "expected_version": 0,
    })
    assert stale.status_code == 409
    merged = client.post(f"/v1/timelines/{target_id}/merge", json={
        "source_id": source_id, "source_version": 1, "expected_version": 1,
    })
    assert merged.status_code == 201
    assert merged.json()["state"]["state"]["cash"] == 5
    assert merged.json()["source"]["version"] == 1


@pytest.mark.parametrize("changes", [
    {"source_version": None}, {"source_version": True},
    {"resolutions": [{"path": "/stock", "choice": "both"}]},
    {"label": "x" * 121},
])
def test_merge_request_contracts_are_validated(client, changes):
    target_id, source_id = merge_branches(client)
    body = {"source_id": source_id, "source_version": 0, "expected_version": 1, **changes}
    assert client.post(f"/v1/timelines/{target_id}/merge", json=body).status_code == 422


def test_merge_requires_a_pinned_source_version_and_has_typed_openapi(client):
    target_id, source_id = merge_branches(client)
    response = client.post(f"/v1/timelines/{target_id}/merge", json={
        "source_id": source_id, "expected_version": 1,
    })
    assert response.status_code == 422
    schema = client.get("/openapi.json").json()
    assert "source_version" in schema["components"]["schemas"]["MergeTimeline"]["required"]
    assert "MergePreview" in schema["components"]["schemas"]
    assert schema["components"]["schemas"]["Event"]["properties"]["merge"]


def test_merge_can_resolve_more_than_one_hundred_supported_conflicts(client):
    initial = {f"field_{index}": 0 for index in range(125)}
    target_id = create(client, initial=initial).json()["id"]
    source_id = client.post(f"/v1/timelines/{target_id}/fork", json={"name": "Many changes", "version": 0}).json()["id"]
    for timeline_id, value in ((target_id, 1), (source_id, 2)):
        response = client.post(f"/v1/timelines/{timeline_id}/events", json={
            "label": "Update fields", "expected_version": 0,
            "operations": [{"op": "replace", "path": "", "value": {key: value for key in initial}}],
        })
        assert response.status_code == 201
    preview = client.get(f"/v1/timelines/{target_id}/merge-preview", params={"source": source_id}).json()
    assert len(preview["conflicts"]) == 125
    merged = client.post(f"/v1/timelines/{target_id}/merge", json={
        "source_id": source_id, "source_version": 1, "expected_version": 1,
        "resolutions": [{"path": conflict["path"], "choice": "source"} for conflict in preview["conflicts"]],
    })
    assert merged.status_code == 201, merged.text
    assert merged.json()["state"]["state"] == {key: 2 for key in initial}
