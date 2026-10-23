from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from app import create_app
from timemachine.store import Store, StoreError
from timemachine.workshop import perform_action


def inventory(frames=20, wheels=40, built=0, notes=None):
    return {"stock": {"frames": frames, "wheels": wheels}, "orders": {"built": built}, "notes": [] if notes is None else notes}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "workshop.sqlite3", api_key="")) as active:
        yield active


def create_workshop(client, state=None):
    response = client.post("/v1/timelines", json={
        "name": "Bicycle workshop", "initial_state": inventory() if state is None else state,
    })
    assert response.status_code == 201
    return response.json()["id"]


def action(client, timeline_id, kind, quantity, version, item=None, headers=None):
    body = {"kind": kind, "quantity": quantity, "expected_version": version}
    if item is not None:
        body["item"] = item
    return client.post(f"/v1/workshop/{timeline_id}/actions", json=body, headers=headers)


def test_workshop_actions_create_real_inventory_events(client):
    timeline_id = create_workshop(client)
    delivered = action(client, timeline_id, "delivery", 10, 0, "frames")
    assert delivered.status_code == 201
    assert delivered.json()["state"]["state"] == inventory(frames=30)
    assert delivered.json()["event"]["operations"] == [{"op": "increment", "path": "/stock/frames", "value": 10}]
    built = action(client, timeline_id, "build", 6, 1)
    assert built.status_code == 201
    assert built.json()["state"]["state"] == inventory(frames=24, wheels=28, built=6)
    assert built.json()["event"]["operations"] == [
        {"op": "increment", "path": "/stock/frames", "value": -6},
        {"op": "increment", "path": "/stock/wheels", "value": -12},
        {"op": "increment", "path": "/orders/built", "value": 6},
    ]
    returned = action(client, timeline_id, "return", 2, 2, "frames")
    assert returned.status_code == 201
    assert returned.json()["state"]["state"] == inventory(frames=22, wheels=28, built=6)
    assert client.get(returned.headers["location"]).json()["version"] == 3
    assert client.get(f"/v1/timelines/{timeline_id}/state", params={"version": 0}).json()["state"] == inventory()


@pytest.mark.parametrize("frames,wheels", [(0, 10), (10, 0), (1, 3)])
def test_build_rejects_insufficient_parts_atomically(client, frames, wheels):
    timeline_id = create_workshop(client, inventory(frames=frames, wheels=wheels))
    response = action(client, timeline_id, "build", 2, 0)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    assert client.get(f"/v1/timelines/{timeline_id}/state").json()["state"] == inventory(frames=frames, wheels=wheels)
    assert client.get(f"/v1/timelines/{timeline_id}/events").json()["items"] == []


def test_return_rejects_more_parts_than_available(client):
    timeline_id = create_workshop(client)
    response = action(client, timeline_id, "return", 41, 0, "wheels")
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"item": "wheels", "required": 41, "available": 40}
    assert client.get(f"/v1/timelines/{timeline_id}").json()["head_version"] == 0


def test_build_can_consume_the_last_available_parts(client):
    timeline_id = create_workshop(client, inventory(frames=2, wheels=4))
    response = action(client, timeline_id, "build", 2, 0)
    assert response.status_code == 201
    assert response.json()["state"]["state"] == inventory(frames=0, wheels=0, built=2)


def test_action_retry_uses_original_version_after_stock_is_consumed(client):
    timeline_id = create_workshop(client, inventory(frames=1, wheels=2))
    headers = {"Idempotency-Key": "last-bicycle"}
    first = action(client, timeline_id, "build", 1, 0, headers=headers)
    assert first.status_code == 201
    replayed = action(client, timeline_id, "build", 1, 0, headers=headers)
    assert replayed.status_code == 201
    assert replayed.json() == first.json()
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert action(client, timeline_id, "build", 1, 1).status_code == 409
    assert client.get(f"/v1/timelines/{timeline_id}").json()["head_version"] == 1


def test_concurrent_actions_cannot_consume_the_same_parts_twice(client):
    timeline_id = create_workshop(client, inventory(frames=1, wheels=2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: action(client, timeline_id, "build", 1, 0), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert client.get(f"/v1/timelines/{timeline_id}/state").json()["state"] == inventory(frames=0, wheels=0, built=1)


def test_stale_action_must_refresh_instead_of_using_live_inventory(client):
    timeline_id = create_workshop(client)
    assert action(client, timeline_id, "delivery", 2, 0, "frames").status_code == 201
    stale = action(client, timeline_id, "build", 1, 0)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"
    assert client.get(f"/v1/timelines/{timeline_id}/state").json()["state"] == inventory(frames=22)


@pytest.mark.parametrize("quantity", [0, -1, 1.0, True, "2"])
def test_action_quantity_must_be_a_positive_integer(client, quantity):
    timeline_id = create_workshop(client)
    response = action(client, timeline_id, "build", quantity, 0)
    assert response.status_code == 422
    assert client.get(f"/v1/timelines/{timeline_id}").json()["head_version"] == 0


@pytest.mark.parametrize("body", [
    {"kind": "delivery", "quantity": 1, "expected_version": 0},
    {"kind": "return", "item": "bicycles", "quantity": 1, "expected_version": 0},
    {"kind": "build", "item": "frames", "quantity": 1, "expected_version": 0},
    {"kind": "sell", "quantity": 1, "expected_version": 0},
    {"kind": "build", "quantity": 1, "expected_version": True},
])
def test_action_contract_rejects_ambiguous_or_unknown_fields(client, body):
    timeline_id = create_workshop(client)
    assert client.post(f"/v1/workshop/{timeline_id}/actions", json=body).status_code == 422


@pytest.mark.parametrize("state", [
    {}, inventory(frames=-1), inventory(frames=1.0), inventory(wheels=True),
    inventory(built=-1), inventory(built=True), inventory(notes={}),
])
def test_actions_require_a_valid_workshop_document(client, state):
    timeline_id = create_workshop(client, state)
    response = action(client, timeline_id, "delivery", 1, 0, "frames")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_workshop_state"
    assert client.get(f"/v1/timelines/{timeline_id}").json()["head_version"] == 0


def test_workshop_service_can_be_used_without_http(tmp_path):
    store = Store(tmp_path / "workshop.sqlite3")
    timeline_id = store.create("Workshop", inventory(frames=2, wheels=4)).payload["id"]
    result = perform_action(store, timeline_id, "build", 2, 0)
    assert result.payload["state"]["state"] == inventory(frames=0, wheels=0, built=2)
    with pytest.raises(StoreError, match="positive integer"):
        perform_action(store, timeline_id, "build", True, 1)


def test_large_integer_delivery_preserves_exact_counts_with_a_bounded_label(client):
    timeline_id = create_workshop(client)
    quantity = 10 ** 130
    response = action(client, timeline_id, "delivery", quantity, 0, "frames")
    assert response.status_code == 201
    assert response.json()["state"]["state"]["stock"]["frames"] == quantity + 20
    assert len(response.json()["event"]["label"]) <= 120


def test_workshop_actions_are_documented_as_a_discriminated_union(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/workshop/{timeline_id}/actions"]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["discriminator"]["propertyName"] == "kind"
    assert set(body_schema["discriminator"]["mapping"]) == {"delivery", "build", "return"}
