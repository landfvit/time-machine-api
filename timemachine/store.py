"""Immutable SQLite event histories with transactional branching."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import uuid

import jsonpatch

from .merging import merge_documents
from .patches import PatchError, _equal, apply_operations, canonical_json, state_hash, validate_state


MAX_TIMELINES = 100
MAX_EVENTS = 1000
CHECKPOINT_INTERVAL = 50


class StoreError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


@dataclass(frozen=True)
class WriteResult:
    payload: dict
    replayed: bool = False


def _dump(value) -> str:
    try:
        return canonical_json(value, depth=-4)
    except PatchError as exc:
        raise StoreError(exc.code, exc.message, details=exc.details) from exc


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise StoreError("invalid_timestamp", "Use an ISO 8601 timestamp with a timezone.") from exc


def _now(after: str | None = None) -> str:
    current = datetime.now(timezone.utc)
    if after is not None:
        current = max(current, _parse_time(after) + timedelta(microseconds=1))
    return _stamp(current)


def _integer(value, name: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise StoreError("invalid_parameter", f"{name} must be an integer {bounds}.")
    return value


def _name(value, field: str = "name") -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
        raise StoreError("invalid_parameter", f"{field} must contain between 1 and 120 characters.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise StoreError("invalid_parameter", f"{field} must contain valid Unicode text.") from exc
    return value.strip()


def _patch(state, operations):
    try:
        return apply_operations(state, operations)
    except PatchError as exc:
        raise StoreError(exc.code, exc.message, details=exc.details) from exc


def _strict_diff(left, right, path=""):
    #preserve json type changes that ordinary python equality treats as equal
    changes = []
    if isinstance(left, dict) and isinstance(right, dict):
        escaped = lambda key: key.replace("~", "~0").replace("/", "~1")
        for key in sorted(left.keys() - right.keys()):
            changes.append({"op": "remove", "path": f"{path}/{escaped(key)}"})
        for key in sorted(right.keys() - left.keys()):
            changes.append({"op": "add", "path": f"{path}/{escaped(key)}", "value": right[key]})
        for key in sorted(left.keys() & right.keys()):
            changes.extend(_strict_diff(left[key], right[key], f"{path}/{escaped(key)}"))
    elif isinstance(left, list) and isinstance(right, list):
        for index in range(min(len(left), len(right))):
            changes.extend(_strict_diff(left[index], right[index], f"{path}/{index}"))
        for index in range(len(left) - 1, len(right) - 1, -1):
            changes.append({"op": "remove", "path": f"{path}/{index}"})
        for index in range(len(left), len(right)):
            changes.append({"op": "add", "path": f"{path}/-", "value": right[index]})
    elif not _equal(left, right):
        changes.append({"op": "replace", "path": path, "value": right})
    return changes


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS timelines (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    initial_at TEXT NOT NULL,
                    parent_id TEXT REFERENCES timelines(id),
                    fork_version INTEGER,
                    initial_state TEXT NOT NULL,
                    head_version INTEGER NOT NULL,
                    head_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    timeline_id TEXT NOT NULL REFERENCES timelines(id),
                    version INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    operations TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    inherited INTEGER NOT NULL DEFAULT 0,
                    source_event_id TEXT,
                    source_timeline_id TEXT,
                    UNIQUE(timeline_id, version)
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response TEXT NOT NULL,
                    PRIMARY KEY(scope, key)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    timeline_id TEXT NOT NULL REFERENCES timelines(id),
                    version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    PRIMARY KEY(timeline_id, version)
                );
                CREATE TABLE IF NOT EXISTS merge_parents (
                    timeline_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_timeline_id TEXT NOT NULL REFERENCES timelines(id),
                    source_version INTEGER NOT NULL,
                    base_timeline_id TEXT NOT NULL REFERENCES timelines(id),
                    base_version INTEGER NOT NULL,
                    PRIMARY KEY(timeline_id, version),
                    FOREIGN KEY(timeline_id, version) REFERENCES events(timeline_id, version)
                );
                CREATE INDEX IF NOT EXISTS timeline_creation ON timelines(created_at, id);
                CREATE INDEX IF NOT EXISTS event_recording ON events(timeline_id, recorded_at);
            """)

    @contextlib.contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def _transaction(self, *, writing=False):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def close(self):
        #connections are scoped to individual operations
        return None

    def health(self) -> dict:
        with self._connection() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    def _cached(self, connection, scope, key, request) -> WriteResult | None:
        if key is None:
            return None
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise StoreError("invalid_idempotency_key", "Idempotency keys must contain 1 to 128 characters.")
        try:
            key.encode("utf-8")
        except UnicodeError as exc:
            raise StoreError("invalid_idempotency_key", "Idempotency keys must contain valid Unicode text.") from exc
        row = connection.execute("SELECT * FROM idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row is None:
            return None
        digest = hashlib.sha256(_dump(request).encode("utf-8")).hexdigest()
        if row["request_hash"] != digest:
            raise StoreError("idempotency_conflict", "This idempotency key was already used for a different request.", 409)
        return WriteResult(json.loads(row["response"]), replayed=True)

    def _remember(self, connection, scope, key, request, payload) -> WriteResult:
        if key is not None:
            digest = hashlib.sha256(_dump(request).encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO idempotency(scope,key,request_hash,response) VALUES(?,?,?,?)",
                (scope, key, digest, _dump(payload)),
            )
        return WriteResult(payload)

    def _row(self, connection, timeline_id):
        row = connection.execute("SELECT * FROM timelines WHERE id=?", (timeline_id,)).fetchone()
        if row is None:
            raise StoreError("timeline_not_found", "The timeline does not exist.", 404)
        return row

    @staticmethod
    def _timeline(row) -> dict:
        return {key: row[key] for key in (
            "id", "name", "created_at", "initial_at", "parent_id", "fork_version", "head_version", "head_hash"
        )}

    def _event(self, row, connection) -> dict:
        event = {
            **{key: row[key] for key in (
                "id", "timeline_id", "version", "label", "recorded_at", "state_hash",
                "source_event_id", "source_timeline_id"
            )},
            "operations": json.loads(row["operations"]),
            "inherited": bool(row["inherited"]),
        }
        merge = connection.execute("SELECT * FROM merge_parents WHERE timeline_id=? AND version=?",
                                   (row["timeline_id"], row["version"])).fetchone()
        if merge:
            event["merge"] = {key: merge[key] for key in
                              ("source_timeline_id", "source_version", "base_timeline_id", "base_version")}
        return event

    def _insert_timeline(self, connection, name, initial_state, *, initial_at=None, parent_id=None, fork_version=None):
        if connection.execute("SELECT count(*) FROM timelines").fetchone()[0] >= MAX_TIMELINES:
            raise StoreError("timeline_limit", f"A workspace can contain up to {MAX_TIMELINES} timelines.", 409)
        timeline_id, created_at = str(uuid.uuid4()), _now()
        connection.execute(
            "INSERT INTO timelines VALUES(?,?,?,?,?,?,?,?,?)",
            (timeline_id, name, created_at, initial_at or created_at, parent_id, fork_version,
             canonical_json(initial_state), 0, state_hash(initial_state)),
        )
        return self._row(connection, timeline_id)

    def create(self, name, initial_state, idempotency_key=None) -> WriteResult:
        name = _name(name)
        try:
            validate_state(initial_state)
        except PatchError as exc:
            raise StoreError(exc.code, exc.message, details=exc.details) from exc
        request = {"name": name, "initial_state": initial_state}
        with self._transaction(writing=True) as connection:
            cached = self._cached(connection, "create", idempotency_key, request)
            if cached:
                return cached
            timeline = self._timeline(self._insert_timeline(connection, name, initial_state))
            return self._remember(connection, "create", idempotency_key, request, timeline)

    def list_timelines(self, limit=50, cursor=None) -> dict:
        _integer(limit, "limit", 1, 100)
        values, condition = [], ""
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 512:
                    raise ValueError("invalid cursor")
                raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
                parsed = json.loads(raw)
                if set(parsed) != {"created_at", "id"} or not all(isinstance(value, str) for value in parsed.values()):
                    raise ValueError("invalid cursor")
                _parse_time(parsed["created_at"])
                condition = "WHERE created_at>? OR (created_at=? AND id>?)"
                values = [parsed["created_at"], parsed["created_at"], parsed["id"]]
            except (ValueError, TypeError, AttributeError, UnicodeError, StoreError) as exc:
                raise StoreError("invalid_cursor", "The pagination cursor is invalid.") from exc
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM timelines {condition} ORDER BY created_at,id LIMIT ?", (*values, limit + 1)
            ).fetchall()
            cursor_out = None
            if len(rows) > limit:
                last = rows[limit - 1]
                cursor_out = base64.urlsafe_b64encode(_dump({"created_at": last["created_at"], "id": last["id"]}).encode()).decode().rstrip("=")
            return {"items": [self._timeline(row) for row in rows[:limit]], "next_cursor": cursor_out}

    def get_timeline(self, timeline_id) -> dict:
        with self._transaction() as connection:
            return self._timeline(self._row(connection, timeline_id))

    def get_events(self, timeline_id, after_version=0, limit=50) -> dict:
        _integer(after_version, "after_version", 0)
        _integer(limit, "limit", 1, 100)
        with self._transaction() as connection:
            self._row(connection, timeline_id)
            rows = connection.execute(
                "SELECT * FROM events WHERE timeline_id=? AND version>? ORDER BY version LIMIT ?",
                (timeline_id, after_version, limit + 1),
            ).fetchall()
            return {"items": [self._event(row, connection) for row in rows[:limit]],
                    "next_after_version": rows[limit - 1]["version"] if len(rows) > limit else None}

    def _resolve_version(self, connection, row, version=None, at=None):
        if version is not None and at is not None:
            raise StoreError("ambiguous_state", "Choose a version or a timestamp, not both.")
        if at is not None:
            timestamp = _parse_time(at)
            if timestamp < _parse_time(row["initial_at"]):
                raise StoreError("state_not_found", "The timeline did not exist at that timestamp.", 404)
            last = connection.execute(
                "SELECT version FROM events WHERE timeline_id=? AND recorded_at<=? ORDER BY version DESC LIMIT 1",
                (row["id"], _stamp(timestamp)),
            ).fetchone()
            return last["version"] if last else 0
        if version is None:
            return row["head_version"]
        _integer(version, "version", 0)
        if version > row["head_version"]:
            raise StoreError("version_not_found", "The requested version does not exist.", 404)
        return version

    def _state(self, connection, row, version) -> dict:
        state, recorded_at = json.loads(row["initial_state"]), row["initial_at"]
        digest = state_hash(state)
        checkpoint = connection.execute(
            "SELECT * FROM checkpoints WHERE timeline_id=? AND version<=? ORDER BY version DESC LIMIT 1",
            (row["id"], version),
        ).fetchone()
        start = 0
        if checkpoint:
            state = json.loads(checkpoint["state"])
            recorded_at, digest, start = checkpoint["recorded_at"], checkpoint["state_hash"], checkpoint["version"]
        events = connection.execute(
            "SELECT * FROM events WHERE timeline_id=? AND version>? AND version<=? ORDER BY version",
            (row["id"], start, version),
        ).fetchall()
        for event in events:
            state = _patch(state, json.loads(event["operations"]))
            recorded_at, digest = event["recorded_at"], event["state_hash"]
        return {"timeline_id": row["id"], "version": version, "recorded_at": recorded_at,
                "state": state, "state_hash": digest, "head_version": row["head_version"]}

    def get_state(self, timeline_id, version=None, at=None) -> dict:
        with self._transaction() as connection:
            row = self._row(connection, timeline_id)
            return self._state(connection, row, self._resolve_version(connection, row, version, at))

    def _append(self, connection, row, label, operations, *, inherited=False, source=None, recorded_at=None, previous=None):
        if row["head_version"] >= MAX_EVENTS:
            raise StoreError("event_limit", f"A timeline can contain up to {MAX_EVENTS} events.", 409)
        previous = previous or self._state(connection, row, row["head_version"])
        state = _patch(previous["state"], operations)
        version = row["head_version"] + 1
        event_id, digest = str(uuid.uuid4()), state_hash(state)
        timestamp = recorded_at or _now(previous["recorded_at"])
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, row["id"], version, label, _dump(operations), timestamp, digest, int(inherited),
             source["id"] if source else None, source["timeline_id"] if source else None),
        )
        connection.execute("UPDATE timelines SET head_version=?,head_hash=? WHERE id=?", (version, digest, row["id"]))
        if version % CHECKPOINT_INTERVAL == 0:
            connection.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                               (row["id"], version, canonical_json(state), timestamp, digest))
        event = self._event(connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone(), connection)
        current = self._row(connection, row["id"])
        snapshot = {"timeline_id": row["id"], "version": version, "recorded_at": timestamp,
                    "state": state, "state_hash": digest, "head_version": version}
        return {"timeline": self._timeline(current), "event": event, "state": snapshot}

    def append(self, timeline_id, label, operations, expected_version, idempotency_key=None) -> WriteResult:
        label = _name(label, "label")
        _integer(expected_version, "expected_version", 0)
        request = {"label": label, "operations": operations, "expected_version": expected_version}
        scope = f"append:{timeline_id}"
        with self._transaction(writing=True) as connection:
            cached = self._cached(connection, scope, idempotency_key, request)
            if cached:
                return cached
            row = self._row(connection, timeline_id)
            if row["head_version"] != expected_version:
                raise StoreError("version_conflict", "The timeline changed. Reload its head version before appending.", 409,
                                 {"expected_version": expected_version, "head_version": row["head_version"]})
            result = self._append(connection, row, label, operations)
            return self._remember(connection, scope, idempotency_key, request, result)

    def _fork(self, connection, source, name, version):
        child = self._insert_timeline(connection, name, json.loads(source["initial_state"]),
                                      initial_at=source["initial_at"], parent_id=source["id"], fork_version=version)
        events = connection.execute(
            "SELECT * FROM events WHERE timeline_id=? AND version<=? ORDER BY version", (source["id"], version)
        ).fetchall()
        #copy the immutable prefix with direct source references
        for event in events:
            connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), child["id"], event["version"], event["label"], event["operations"],
                 event["recorded_at"], event["state_hash"], 1, event["id"], source["id"]),
            )
        connection.execute(
            "INSERT INTO checkpoints(timeline_id,version,state,recorded_at,state_hash) "
            "SELECT ?,version,state,recorded_at,state_hash FROM checkpoints WHERE timeline_id=? AND version<=?",
            (child["id"], source["id"], version),
        )
        connection.execute(
            "INSERT INTO merge_parents(timeline_id,version,source_timeline_id,source_version,base_timeline_id,base_version) "
            "SELECT ?,version,source_timeline_id,source_version,base_timeline_id,base_version "
            "FROM merge_parents WHERE timeline_id=? AND version<=?", (child["id"], source["id"], version),
        )
        if events:
            connection.execute("UPDATE timelines SET head_version=?,head_hash=? WHERE id=?",
                               (version, events[-1]["state_hash"], child["id"]))
        return self._row(connection, child["id"])

    def fork(self, timeline_id, name, version=None, idempotency_key=None) -> WriteResult:
        name = _name(name)
        request, scope = {"name": name, "version": version}, f"fork:{timeline_id}"
        with self._transaction(writing=True) as connection:
            cached = self._cached(connection, scope, idempotency_key, request)
            if cached:
                return cached
            source = self._row(connection, timeline_id)
            version = self._resolve_version(connection, source, version)
            child = self._fork(connection, source, name, version)
            return self._remember(connection, scope, idempotency_key, request, self._timeline(child))

    def experiment(self, timeline_id, name, event_version, operations, label=None, idempotency_key=None) -> WriteResult:
        name = _name(name)
        _integer(event_version, "event_version", 1)
        label = _name(label, "label") if label is not None else None
        request = {"name": name, "event_version": event_version, "operations": operations, "label": label}
        scope = f"experiment:{timeline_id}"
        with self._transaction(writing=True) as connection:
            cached = self._cached(connection, scope, idempotency_key, request)
            if cached:
                return cached
            source = self._row(connection, timeline_id)
            self._resolve_version(connection, source, event_version)
            if connection.execute("SELECT 1 FROM merge_parents WHERE timeline_id=? AND version>=? LIMIT 1",
                                  (timeline_id, event_version)).fetchone():
                raise StoreError("merge_replay_unsupported", "Experiments cannot replace or replay merge events. Fork after the merge instead.", 409)
            child = self._fork(connection, source, name, event_version - 1)
            events = connection.execute(
                "SELECT * FROM events WHERE timeline_id=? AND version>=? ORDER BY version", (timeline_id, event_version)
            ).fetchall()
            previous = self._state(connection, child, child["head_version"])
            for index, event in enumerate(events):
                try:
                    replacement = operations if index == 0 else json.loads(event["operations"])
                    event_label = (label or event["label"]) if index == 0 else event["label"]
                    outcome = self._append(connection, child, event_label, replacement, source=event, previous=previous)
                    previous = outcome["state"]
                    child = self._row(connection, child["id"])
                except StoreError as exc:
                    raise StoreError("replay_conflict", f"The alternative history failed at source version {event['version']}.",
                                     409, {"source_version": event["version"], "reason": exc.code,
                                           "message": exc.message, **exc.details}) from exc
            payload = {"timeline": self._timeline(child), "replayed_events": len(events) - 1,
                       "changed_version": event_version}
            return self._remember(connection, scope, idempotency_key, request, payload)

    def _merge_context(self, connection, target_id, source_id, source_version):
        target = self._row(connection, target_id)
        source = self._row(connection, source_id)
        source_version = self._resolve_version(connection, source, source_version)
        timelines = {row["id"]: row for row in connection.execute("SELECT * FROM timelines")}
        merges = {(row["timeline_id"], row["version"]): (row["source_timeline_id"], row["source_version"])
                  for row in connection.execute("SELECT * FROM merge_parents")}
        canonical_cache, parent_cache = {}, {}

        def canonical(node):
            trail = []
            while node not in canonical_cache:
                row = timelines[node[0]]
                if row["parent_id"] is None or node[1] > row["fork_version"]:
                    break
                trail.append(node)
                node = (row["parent_id"], node[1])
            node = canonical_cache.get(node, node)
            for alias in trail:
                canonical_cache[alias] = node
            return node

        def parents(node):
            if node not in parent_cache:
                result = set()
                if node[1]:
                    result.add(canonical((node[0], node[1] - 1)))
                if node in merges:
                    result.add(canonical(merges[node]))
                parent_cache[node] = result
            return parent_cache[node]

        def ancestors(node):
            visited, pending = set(), [canonical(node)]
            while pending:
                current = pending.pop()
                if current not in visited:
                    visited.add(current)
                    pending.extend(parents(current) - visited)
            return visited

        target_node = canonical((target_id, target["head_version"]))
        source_node = canonical((source_id, source_version))
        target_ancestors, source_ancestors = ancestors(target_node), ancestors(source_node)
        common = target_ancestors & source_ancestors
        if not common:
            raise StoreError("unrelated_timelines", "These timelines have no shared history. Fork a timeline before merging.", 409)
        #every parent of a common node is also common, so direct edges identify dominated bases
        dominated = set()
        for node in common:
            dominated.update(parents(node))
        best = common - dominated
        if len(best) != 1:
            candidates = [{"timeline_id": node[0], "version": node[1]} for node in sorted(best)]
            raise StoreError("ambiguous_merge_base", "These histories have multiple merge bases. Merge from a simpler shared branch.",
                             409, {"candidates": candidates})
        base_id, base_version = next(iter(best))
        base = self._state(connection, timelines[base_id], base_version)
        target_state = self._state(connection, target, target["head_version"])
        source_state = self._state(connection, source, source_version)
        return base, target_state, source_state, source_node in target_ancestors

    @staticmethod
    def _validate_merged(state):
        try:
            validate_state(state)
        except PatchError as exc:
            raise StoreError(exc.code, exc.message, details=exc.details) from exc

    def preview_merge(self, target_id, source_id, source_version=None) -> dict:
        with self._transaction() as connection:
            base, target, source, _ = self._merge_context(connection, target_id, source_id, source_version)
            state, conflicts = merge_documents(base["state"], target["state"], source["state"])
            if not conflicts:
                self._validate_merged(state)
            return {"base": base, "target": target, "source": source,
                    "merged_state": None if conflicts else state, "conflicts": conflicts}

    def merge(self, target_id, source_id, source_version, expected_version, resolutions=None, label=None,
              idempotency_key=None) -> WriteResult:
        _integer(source_version, "source_version", 0)
        _integer(expected_version, "expected_version", 0)
        label = _name(label, "label") if label is not None else None
        resolutions = [] if resolutions is None else resolutions
        if not isinstance(resolutions, list):
            raise StoreError("invalid_resolution", "Resolutions must be a list of conflict paths and choices.")
        choices = {}
        for resolution in resolutions:
            if (not isinstance(resolution, dict) or set(resolution) != {"path", "choice"}
                    or not isinstance(resolution["path"], str) or resolution["choice"] not in ("target", "source")):
                raise StoreError("invalid_resolution", "Each resolution requires a path and a target or source choice.")
            if resolution["path"] in choices:
                raise StoreError("invalid_resolution", "Each conflict path can be resolved only once.")
            choices[resolution["path"]] = resolution["choice"]
        request = {"source_id": source_id, "source_version": source_version, "expected_version": expected_version,
                   "resolutions": resolutions, "label": label}
        scope = f"merge:{target_id}"
        with self._transaction(writing=True) as connection:
            cached = self._cached(connection, scope, idempotency_key, request)
            if cached:
                return cached
            target_row = self._row(connection, target_id)
            if target_row["head_version"] != expected_version:
                raise StoreError("version_conflict", "The target changed. Preview the merge again before applying it.", 409,
                                 {"expected_version": expected_version, "head_version": target_row["head_version"]})
            base, target, source, incorporated = self._merge_context(connection, target_id, source_id, source_version)
            if incorporated:
                raise StoreError("already_merged", "The selected source history is already included in the target.", 409)
            state, conflicts = merge_documents(base["state"], target["state"], source["state"], choices)
            paths = {conflict["path"] for conflict in conflicts}
            if choices.keys() - paths:
                raise StoreError("invalid_resolution", "A resolution refers to a path that is not a merge conflict.",
                                 details={"paths": sorted(choices.keys() - paths)})
            unresolved = [conflict for conflict in conflicts if conflict["path"] not in choices]
            if unresolved:
                raise StoreError("merge_conflict", "Resolve every conflicting value before merging.", 409,
                                 {"conflicts": unresolved, "base": base})
            self._validate_merged(state)
            source_row = self._row(connection, source_id)
            result = self._append(connection, target_row, label or f"Merge {source_row['name']}"[:120],
                                  [{"op": "replace", "path": "", "value": state}], previous=target)
            connection.execute("INSERT INTO merge_parents VALUES(?,?,?,?,?,?)",
                               (target_id, result["event"]["version"], source_id, source_version,
                                base["timeline_id"], base["version"]))
            result["event"]["merge"] = {"source_timeline_id": source_id, "source_version": source_version,
                                        "base_timeline_id": base["timeline_id"], "base_version": base["version"]}
            payload = {**result, "base": base, "source": source}
            return self._remember(connection, scope, idempotency_key, request, payload)

    def compare(self, left, right, left_version=None, right_version=None) -> dict:
        with self._transaction() as connection:
            left_row, right_row = self._row(connection, left), self._row(connection, right)
            left_state = self._state(connection, left_row, self._resolve_version(connection, left_row, left_version))
            right_state = self._state(connection, right_row, self._resolve_version(connection, right_row, right_version))
            changes = jsonpatch.make_patch(left_state["state"], right_state["state"]).patch
            if not _equal(jsonpatch.apply_patch(left_state["state"], changes), right_state["state"]):
                changes = _strict_diff(left_state["state"], right_state["state"])
            return {"left": left_state, "right": right_state, "changes": changes}

    def export(self, timeline_id, version=None) -> dict:
        with self._transaction() as connection:
            row = self._row(connection, timeline_id)
            version = self._resolve_version(connection, row, version)
            events = connection.execute(
                "SELECT * FROM events WHERE timeline_id=? AND version<=? ORDER BY version", (timeline_id, version)
            ).fetchall()
            return {"timeline": self._timeline(row), "initial_state": json.loads(row["initial_state"]),
                    "events": [self._event(event, connection) for event in events], "state": self._state(connection, row, version)}

    def create_example(self, idempotency_key=None) -> WriteResult:
        sample = json.loads((Path(__file__).resolve().parent.parent / "samples" / "inventory.json").read_text())
        request = {"example": "inventory-v1"}
        with self._transaction(writing=True) as connection:
            cached = self._cached(connection, "example", idempotency_key, request)
            if cached:
                return cached
            row = self._insert_timeline(connection, _name(sample["name"]), validate_state(sample["initial_state"]))
            previous = self._state(connection, row, 0)
            for event in sample["events"]:
                outcome = self._append(connection, row, _name(event["label"], "label"), event["operations"], previous=previous)
                previous = outcome["state"]
                row = self._row(connection, row["id"])
            return self._remember(connection, "example", idempotency_key, request, self._timeline(row))
