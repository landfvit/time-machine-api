"""A small inventory client built on immutable timeline events."""

from __future__ import annotations

from .store import Store, StoreError, WriteResult


def _inventory(state: dict) -> tuple[dict, dict]:
    stock, orders = state.get("stock"), state.get("orders")
    if (
        type(stock) is not dict or type(orders) is not dict
        or type(state.get("notes")) is not list
        or any(type(stock.get(item)) is not int or stock[item] < 0 for item in ("frames", "wheels"))
        or type(orders.get("built")) is not int or orders["built"] < 0
    ):
        raise StoreError(
            "invalid_workshop_state",
            "Workshop state requires nonnegative integer stock.frames, stock.wheels, orders.built, and a notes array.",
        )
    return stock, orders


def perform_action(
    store: Store, timeline_id: str, kind: str, quantity: int, expected_version: int,
    item: str | None = None, idempotency_key: str | None = None,
) -> WriteResult:
    if type(quantity) is not int or quantity <= 0:
        raise StoreError("invalid_quantity", "Quantity must be a positive integer.")
    if type(expected_version) is not int or expected_version < 0:
        raise StoreError("invalid_parameter", "expected_version must be a nonnegative integer.")
    if kind not in {"delivery", "build", "return"}:
        raise StoreError("invalid_action", "Choose delivery, build, or return.")
    if (kind == "build" and item is not None) or (kind != "build" and item not in {"frames", "wheels"}):
        raise StoreError("invalid_item", "Delivery and return require frames or wheels. Build takes no item.")

    #validate the immutable request version so retries do not inspect changed stock
    snapshot = store.get_state(timeline_id, version=expected_version)
    stock, _orders = _inventory(snapshot["state"])
    if kind == "build":
        required = {"frames": quantity, "wheels": quantity * 2}
        if any(stock[part] < amount for part, amount in required.items()):
            raise StoreError(
                "insufficient_stock", "Not enough stock. Each bicycle requires one frame and two wheels.", 409,
                {"required": required, "available": {part: stock[part] for part in required}},
            )
        operations = [
            {"op": "increment", "path": "/stock/frames", "value": -quantity},
            {"op": "increment", "path": "/stock/wheels", "value": -(quantity * 2)},
            {"op": "increment", "path": "/orders/built", "value": quantity},
        ]
        label = f"Build {quantity} bicycles"
    else:
        if kind == "return" and stock[item] < quantity:
            raise StoreError(
                "insufficient_stock", "The return quantity exceeds available stock.", 409,
                {"item": item, "required": quantity, "available": stock[item]},
            )
        amount = quantity if kind == "delivery" else -quantity
        operations = [{"op": "increment", "path": f"/stock/{item}", "value": amount}]
        verb = "Deliver" if kind == "delivery" else "Return"
        label = f"{verb} {quantity} {item}"
    if len(label) > 120:
        #the exact quantity remains in the event operations
        label = "Build bicycles" if kind == "build" else f"{verb} {item}"
    return store.append(timeline_id, label, operations, expected_version, idempotency_key=idempotency_key)
