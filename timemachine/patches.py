"""Bounded JSON documents and atomic patch operations."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any


MAX_DEPTH = 32
MAX_STATE_BYTES = 128 * 1024
MAX_OPERATIONS = 100


class PatchError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _check_json(value: Any, depth: int = 0, active: set | None = None) -> None:
    if depth > MAX_DEPTH:
        raise PatchError("state_too_deep", f"JSON depth must not exceed {MAX_DEPTH}.")
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise PatchError("invalid_json", "JSON numbers must be finite.")
        return
    if type(value) not in (dict, list):
        raise PatchError("invalid_json", "Values must be valid JSON types.")
    active = active if active is not None else set()
    if id(value) in active:
        raise PatchError("invalid_json", "JSON must not contain circular references.")
    active.add(id(value))
    try:
        if isinstance(value, dict):
            if any(type(key) is not str for key in value):
                raise PatchError("invalid_json", "JSON object keys must be strings.")
            values = value.values()
        else:
            values = value
        for child in values:
            _check_json(child, depth + 1, active)
    finally:
        active.remove(id(value))


def canonical_json(value: Any, *, depth: int = 0) -> str:
    _check_json(value, depth)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8")
        return encoded
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PatchError("invalid_json", "The value cannot be encoded as JSON.") from exc


def validate_state(state: Any) -> dict:
    if type(state) is not dict:
        raise PatchError("invalid_state", "The document root must be a JSON object.")
    encoded = canonical_json(state)
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise PatchError("state_too_large", f"State must not exceed {MAX_STATE_BYTES} bytes.")
    return state


def state_hash(state: dict) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def _tokens(pointer: Any) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise PatchError("invalid_pointer", "A JSON pointer must be empty or start with '/'.")
    if pointer == "":
        return []
    tokens = []
    for token in pointer[1:].split("/"):
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 == len(token) or token[index + 1] not in "01":
                    raise PatchError("invalid_pointer", "JSON pointers only allow '~0' and '~1' escapes.")
                index += 2
            else:
                index += 1
        tokens.append(token.replace("~1", "/").replace("~0", "~"))
    return tokens


def _index(token: str, length: int, *, adding: bool = False) -> int:
    if token == "-" and adding:
        return length
    if not token or not token.isascii() or not token.isdecimal() or (len(token) > 1 and token[0] == "0"):
        raise PatchError("invalid_pointer", "Array indexes must be nonnegative integers without leading zeros.")
    #reject absurd indexes before integer conversion
    if len(token) > 20:
        raise PatchError("path_not_found", "The array index is outside the document.")
    index = int(token)
    if index >= length + int(adding):
        raise PatchError("path_not_found", "The array index is outside the document.")
    return index


def _get(document: Any, tokens: list[str]) -> Any:
    value = document
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                raise PatchError("path_not_found", "The JSON pointer does not exist.")
            value = value[token]
        elif isinstance(value, list):
            value = value[_index(token, len(value))]
        else:
            raise PatchError("path_not_found", "The JSON pointer traverses a scalar value.")
    return value


def _put(document: dict, tokens: list[str], value: Any, *, replacing: bool = False) -> dict:
    if not tokens:
        return copy.deepcopy(value)
    parent = _get(document, tokens[:-1])
    key = tokens[-1]
    value = copy.deepcopy(value)
    if isinstance(parent, dict):
        if replacing and key not in parent:
            raise PatchError("path_not_found", "A replacement target must already exist.")
        parent[key] = value
    elif isinstance(parent, list):
        index = _index(key, len(parent), adding=not replacing)
        if replacing:
            parent[index] = value
        else:
            parent.insert(index, value)
    else:
        raise PatchError("path_not_found", "The JSON pointer parent is not an object or array.")
    return document


def _remove(document: dict, tokens: list[str]) -> Any:
    if not tokens:
        raise PatchError("invalid_state", "Removing the document root would leave no JSON object.")
    parent = _get(document, tokens[:-1])
    key = tokens[-1]
    if isinstance(parent, dict):
        if key not in parent:
            raise PatchError("path_not_found", "The removal target does not exist.")
        return parent.pop(key)
    if isinstance(parent, list):
        return parent.pop(_index(key, len(parent)))
    raise PatchError("path_not_found", "The JSON pointer parent is not an object or array.")


def _equal(left: Any, right: Any) -> bool:
    #json booleans are not interchangeable with numeric values
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    return type(left) is type(right) and left == right


def apply_operations(state: dict, operations: Any) -> dict:
    """Apply a patch to a copy, so failures never mutate its input."""
    validate_state(state)
    if type(operations) is not list or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise PatchError("invalid_operations", f"Provide between 1 and {MAX_OPERATIONS} operations.")
    document = copy.deepcopy(state)
    for index, operation in enumerate(operations):
        try:
            if type(operation) is not dict:
                raise PatchError("invalid_operation", "Each operation must be a JSON object.")
            canonical_json(operation, depth=-2)
            kind = operation.get("op")
            if kind not in ("add", "remove", "replace", "move", "copy", "test", "increment"):
                raise PatchError("invalid_operation", "The patch operation is not supported.")
            if "path" not in operation:
                raise PatchError("invalid_operation", "Each operation requires a path.")
            path = _tokens(operation["path"])
            if kind in ("add", "replace", "test", "increment"):
                if "value" not in operation:
                    raise PatchError("invalid_operation", "This operation requires a value.")
                canonical_json(operation["value"])
            if kind == "add":
                document = _put(document, path, operation["value"])
            elif kind == "replace":
                document = _put(document, path, operation["value"], replacing=True)
            elif kind == "remove":
                _remove(document, path)
            elif kind in ("move", "copy"):
                if "from" not in operation:
                    raise PatchError("invalid_operation", "This operation requires a 'from' pointer.")
                source = _tokens(operation["from"])
                value = copy.deepcopy(_get(document, source))
                if kind == "move":
                    if path == source:
                        continue
                    if len(path) > len(source) and path[: len(source)] == source:
                        raise PatchError("invalid_operation", "A value cannot be moved into its own child.")
                    _remove(document, source)
                document = _put(document, path, value)
            elif kind == "test":
                if not _equal(_get(document, path), operation["value"]):
                    raise PatchError("test_failed", "The test operation did not match the current value.")
            else:
                current, amount = _get(document, path), operation["value"]
                if type(current) not in (int, float) or type(amount) not in (int, float):
                    raise PatchError("invalid_increment", "Increment requires an existing number and a numeric value.")
                try:
                    total = current + amount
                except (OverflowError, ValueError) as exc:
                    raise PatchError("invalid_increment", "The increment result must be a finite JSON number.") from exc
                document = _put(document, path, total, replacing=True)
            validate_state(document)
        except PatchError as exc:
            exc.details = {**exc.details, "operation_index": index}
            raise
    return document
