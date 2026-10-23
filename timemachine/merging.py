"""Three-way JSON merges with explicit choices for conflicting values."""

from __future__ import annotations

import copy

from .patches import _equal


MISSING = object()


def _same(left, right):
    if left is MISSING or right is MISSING:
        return left is right
    return _equal(left, right)


def _value(value):
    return {"exists": value is not MISSING, "value": None if value is MISSING else copy.deepcopy(value)}


def merge_documents(base: dict, target: dict, source: dict, resolutions: dict[str, str] | None = None):
    """Return a candidate document and all conflicts, without mutating inputs."""
    conflicts = []
    resolutions = resolutions or {}

    def merge(before, left, right, path):
        if _same(left, right):
            return left
        if _same(left, before):
            return right
        if _same(right, before):
            return left
        if isinstance(left, dict) and isinstance(right, dict) and (isinstance(before, dict) or before is MISSING):
            original = before if isinstance(before, dict) else {}
            result = {}
            for key in sorted(original.keys() | left.keys() | right.keys()):
                escaped = key.replace("~", "~0").replace("/", "~1")
                value = merge(original.get(key, MISSING), left.get(key, MISSING), right.get(key, MISSING), f"{path}/{escaped}")
                if value is not MISSING:
                    result[key] = value
            return result
        conflicts.append({"path": path, "base": _value(before), "target": _value(left), "source": _value(right)})
        return right if resolutions.get(path) == "source" else left

    document = merge(base, target, source, "")
    return copy.deepcopy(document), conflicts
