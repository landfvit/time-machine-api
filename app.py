from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from timemachine.schemas import (
    AppendEvent,
    AppendedEvent,
    Comparison,
    CreateTimeline,
    ErrorResponse,
    EventPage,
    Experiment,
    ExperimentTimeline,
    ForkTimeline,
    Health,
    MergePreview,
    MergedEvent,
    MergeTimeline,
    State,
    Timeline,
    TimelineExport,
    TimelinePage,
    WorkshopAction,
)
from timemachine.store import Store, StoreError
from timemachine.workshop import perform_action

ROOT = Path(__file__).resolve().parent
MAX_REQUEST_BYTES = 256 * 1024
ERRORS = {
    status: {"model": ErrorResponse}
    for status in (400, 401, 403, 404, 409, 413, 422)
}


def error_response(code: str, message: str, status: int, details=None, headers=None):
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status_code=status,
        headers=headers,
    )


def strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object keys must be unique.")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"{value} is not a valid JSON number.")


def finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON numbers must be finite.")
    return parsed


class RequestGuard:
    def __init__(self, app, api_key: str | None):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope["headers"]}
        authority = headers.get("host", "")
        try:
            host_url = urlsplit(f"//{authority}")
            hostname = host_url.hostname
            port = host_url.port
            if host_url.username or host_url.password or host_url.path or "?" in authority or "#" in authority:
                raise ValueError
        except ValueError:
            hostname, port = None, None
        if hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return await error_response("invalid_host", "Use a localhost address.", 400)(scope, receive, send)

        if scope["path"].startswith("/v1/"):
            if self.api_key and not hmac.compare_digest(headers.get("x-api-key", "").encode(), self.api_key.encode()):
                response = error_response(
                    "unauthorized", "A valid X-API-Key header is required.", 401,
                    headers={"WWW-Authenticate": "ApiKey"},
                )
                return await response(scope, receive, send)
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and "origin" in headers:
                try:
                    origin = urlsplit(headers["origin"])
                    scheme = scope.get("scheme", "http")
                    local_port = port or (443 if scheme == "https" else 80)
                    origin_port = origin.port or (443 if origin.scheme == "https" else 80)
                    same_origin = (
                        origin.scheme == scheme
                        and origin.hostname == hostname
                        and origin_port == local_port
                        and not origin.path
                        and not origin.query
                        and not origin.fragment
                        and origin.username is None
                    )
                except ValueError:
                    same_origin = False
                if not same_origin:
                    return await error_response("forbidden_origin", "Browser writes must come from this origin.", 403)(scope, receive, send)

            #read once, then replay the body for fastapi
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_REQUEST_BYTES:
                    return await error_response("request_too_large", "Request bodies are limited to 256 KiB.", 413)(scope, receive, send)
                if not message.get("more_body", False):
                    break
            if body:
                media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type != "application/json" and not media_type.endswith("+json"):
                    return await error_response("invalid_json", "Send the request as application/json.", 400)(scope, receive, send)
                try:
                    parsed = json.loads(bytes(body), object_pairs_hook=strict_pairs, parse_constant=reject_constant, parse_float=finite_float)
                    json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (ValueError, UnicodeError, RecursionError):
                    return await error_response("invalid_json", "Send valid JSON with unique keys and finite numbers.", 400)(scope, receive, send)

            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        await self.app(scope, receive, send)


def create_app(db_path: str | Path | None = None, api_key: str | None = None) -> FastAPI:
    database = Path(db_path or os.environ.get("TIMEMACHINE_DB", ROOT / ".local" / "history.sqlite3"))
    access_key = api_key if api_key is not None else os.environ.get("TIMEMACHINE_API_KEY")

    @asynccontextmanager
    async def lifespan(application):
        application.state.store = Store(database)
        try:
            yield
        finally:
            application.state.store.close()

    application = FastAPI(
        title="TimeMachineAPI",
        version="0.1.0",
        description=(
            "Append JSON changes, reconstruct past state, and explore alternate histories. "
            "Versions start at 0. Event timestamps are server recording times, not editable business dates. "
            "The increment operation applies a relative numeric change during replay. "
            "Use expected_version for concurrent writes and Idempotency-Key for safe retries. "
            "When TIMEMACHINE_API_KEY is configured, send it in X-API-Key for every /v1 request."
        ),
        lifespan=lifespan,
    )
    application.add_middleware(RequestGuard, api_key=access_key)

    @application.exception_handler(StoreError)
    async def store_error(_request: Request, exc: StoreError):
        status = 413 if exc.code == "state_too_large" else exc.status
        return error_response(exc.code, exc.message, status, exc.details)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = {"issues": [
            {"location": list(issue["loc"]), "message": issue["msg"], "type": issue["type"]}
            for issue in exc.errors()
        ]}
        return error_response("validation_error", "The request does not match the API contract.", 422, details)

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        return error_response(code, str(exc.detail), exc.status_code, headers=exc.headers)

    @application.exception_handler(Exception)
    async def unexpected_error(_request: Request, _exc: Exception):
        return error_response("internal_error", "The request could not be completed.", 500)

    def write_response(result, location: str):
        headers = {"Location": location}
        if result.replayed:
            headers["Idempotent-Replayed"] = "true"
        return JSONResponse(result.payload, status_code=201, headers=headers)

    @application.get("/health", response_model=Health, responses={503: {"model": ErrorResponse}}, tags=["Service"])
    def health(request: Request):
        try:
            request.app.state.store.health()
        except sqlite3.Error:
            return error_response("unavailable", "The history database is unavailable.", 503)
        return {"status": "ok", "api_key_required": bool(access_key)}

    @application.get("/v1/timelines", response_model=TimelinePage, responses=ERRORS, tags=["Timelines"])
    def list_timelines(request: Request, limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=1024)):
        return request.app.state.store.list_timelines(limit=limit, cursor=cursor)

    @application.post(
        "/v1/timelines", response_model=Timeline, status_code=201, responses=ERRORS, tags=["Timelines"],
        summary="Create a timeline with an immutable initial state",
    )
    def create_timeline(body: CreateTimeline, request: Request, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        result = request.app.state.store.create(body.name, body.initial_state, idempotency_key=idempotency_key)
        return write_response(result, f"/v1/timelines/{result.payload['id']}")

    @application.get("/v1/timelines/{timeline_id}", response_model=Timeline, responses=ERRORS, tags=["Timelines"])
    def timeline(timeline_id: str, request: Request):
        return request.app.state.store.get_timeline(timeline_id)

    @application.get("/v1/timelines/{timeline_id}/events", response_model=EventPage, responses=ERRORS, tags=["Events"])
    def events(timeline_id: str, request: Request, after_version: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return request.app.state.store.get_events(timeline_id, after_version=after_version, limit=limit)

    @application.post(
        "/v1/timelines/{timeline_id}/events", response_model=AppendedEvent, status_code=201, responses=ERRORS, tags=["Events"],
        summary="Atomically append JSON Patch operations",
        description="Supports add, remove, replace, move, copy, test, and numeric increment. Every operation must succeed.",
    )
    def append_event(timeline_id: str, body: AppendEvent, request: Request, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        result = request.app.state.store.append(
            timeline_id, body.label, [operation.model_dump(by_alias=True) for operation in body.operations],
            body.expected_version, idempotency_key=idempotency_key,
        )
        version = result.payload["state"]["version"]
        return write_response(result, f"/v1/timelines/{timeline_id}/state?version={version}")

    @application.get(
        "/v1/timelines/{timeline_id}/state", response_model=State,
        responses={**ERRORS, 304: {"description": "The selected state representation has not changed."}}, tags=["History"],
        summary="Reconstruct state at a version or server recording time",
    )
    def state(
        timeline_id: str, request: Request, version: int | None = Query(None, ge=0),
        at: datetime | None = Query(None, description="An ISO 8601 timestamp with a timezone. Mutually exclusive with version."),
        if_none_match: Annotated[str | None, Header()] = None,
    ):
        if version is not None and at is not None:
            return error_response("validation_error", "Choose either version or at.", 422)
        if at is not None and at.utcoffset() is None:
            return error_response("validation_error", "The at timestamp must include a timezone.", 422)
        payload = request.app.state.store.get_state(timeline_id, version=version, at=at.isoformat() if at else None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        candidates = [item.strip().removeprefix("W/") for item in (if_none_match or "").split(",")]
        if etag in candidates or "*" in candidates:
            return Response(status_code=304, headers=headers)
        return JSONResponse(payload, headers=headers)

    @application.post(
        "/v1/timelines/{timeline_id}/fork", response_model=Timeline, status_code=201, responses=ERRORS, tags=["Branches"],
        summary="Branch from a historical version without changing its source",
    )
    def fork(timeline_id: str, body: ForkTimeline, request: Request, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        result = request.app.state.store.fork(timeline_id, body.name, body.version, idempotency_key=idempotency_key)
        return write_response(result, f"/v1/timelines/{result.payload['id']}")

    @application.post(
        "/v1/timelines/{timeline_id}/experiment", response_model=Experiment, status_code=201, responses=ERRORS, tags=["Branches"],
        summary="Change an earlier event on a branch and replay subsequent events",
        description="The source is unchanged. If replay fails, the entire experiment is rejected and no branch is saved. Experiments cannot replace or replay merge events. Fork after the last merge before experimenting.",
    )
    def experiment(timeline_id: str, body: ExperimentTimeline, request: Request, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        result = request.app.state.store.experiment(
            timeline_id, body.name, body.event_version,
            [operation.model_dump(by_alias=True) for operation in body.operations],
            label=body.label, idempotency_key=idempotency_key,
        )
        return write_response(result, f"/v1/timelines/{result.payload['timeline']['id']}")

    @application.get(
        "/v1/timelines/{timeline_id}/merge-preview", response_model=MergePreview,
        responses=ERRORS, tags=["Branches"], summary="Preview a three-way merge into this timeline",
        description="Compares the common historical base, target head, and selected source version. Conflicts require explicit choices.",
    )
    def merge_preview(timeline_id: str, request: Request, source: str, source_version: int | None = Query(None, ge=0)):
        return request.app.state.store.preview_merge(timeline_id, source, source_version=source_version)

    @application.post(
        "/v1/timelines/{timeline_id}/merge", response_model=MergedEvent, status_code=201,
        responses=ERRORS, tags=["Branches"], summary="Append a reviewed merge as one new target event",
        description="Pin source_version and expected_version from the preview. Resolve each conflict by choosing target or source. The source timeline is unchanged.",
    )
    def merge(timeline_id: str, body: MergeTimeline, request: Request, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        result = request.app.state.store.merge(
            timeline_id, body.source_id, body.source_version, body.expected_version,
            resolutions=[resolution.model_dump() for resolution in body.resolutions],
            label=body.label, idempotency_key=idempotency_key,
        )
        version = result.payload["state"]["version"]
        return write_response(result, f"/v1/timelines/{timeline_id}/state?version={version}")

    @application.post(
        "/v1/workshop/{timeline_id}/actions", response_model=AppendedEvent, status_code=201,
        responses=ERRORS, tags=["Workshop"], summary="Deliver parts, build bicycles, or return parts to a supplier",
        description="The workshop client validates nonnegative integer inventory. Build consumes one frame and two wheels per bicycle. Delivery and return require an item. Use expected_version and Idempotency-Key for concurrency and safe retries.",
    )
    def workshop_action(timeline_id: str, body: WorkshopAction, request: Request, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        result = perform_action(
            request.app.state.store, timeline_id, body.kind, body.quantity, body.expected_version,
            item=getattr(body, "item", None), idempotency_key=idempotency_key,
        )
        version = result.payload["state"]["version"]
        return write_response(result, f"/v1/timelines/{timeline_id}/state?version={version}")

    @application.get("/v1/compare", response_model=Comparison, responses=ERRORS, tags=["History"])
    def compare(
        request: Request, left: str, right: str,
        left_version: int | None = Query(None, ge=0), right_version: int | None = Query(None, ge=0),
    ):
        return request.app.state.store.compare(left, right, left_version=left_version, right_version=right_version)

    @application.get(
        "/v1/timelines/{timeline_id}/export", response_model=TimelineExport,
        responses=ERRORS, tags=["History"], summary="Download a timeline and its event history",
    )
    def export(timeline_id: str, request: Request, version: int | None = Query(None, ge=0)):
        payload = request.app.state.store.export(timeline_id, version=version)
        return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="timeline-{hashlib.sha256(timeline_id.encode()).hexdigest()[:16]}.json"'})

    @application.post("/v1/examples/inventory", response_model=Timeline, status_code=201, responses=ERRORS, tags=["Examples"])
    def example(request: Request, idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        result = request.app.state.store.create_example(idempotency_key=idempotency_key)
        return write_response(result, f"/v1/timelines/{result.payload['id']}")

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @application.get("/workshop", include_in_schema=False)
    def workshop():
        return FileResponse(ROOT / "static" / "workshop.html")

    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    def openapi_schema():
        if application.openapi_schema is None:
            schema = get_openapi(
                title=application.title, version=application.version,
                description=application.description, routes=application.routes,
            )
            if access_key:
                schema.setdefault("components", {}).setdefault("securitySchemes", {})["LocalApiKey"] = {
                    "type": "apiKey", "in": "header", "name": "X-API-Key",
                }
                for path, operations in schema["paths"].items():
                    if path.startswith("/v1/"):
                        for operation in operations.values():
                            if isinstance(operation, dict):
                                operation["security"] = [{"LocalApiKey": []}]
            application.openapi_schema = schema
        return application.openapi_schema

    application.openapi = openapi_schema
    return application


app = create_app()
