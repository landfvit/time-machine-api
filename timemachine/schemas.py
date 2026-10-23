from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictFloat, StrictInt


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ValueOperation(Contract):
    op: Literal["add", "replace", "test"]
    path: str
    value: JsonValue


class RemoveOperation(Contract):
    op: Literal["remove"]
    path: str


class TransferOperation(Contract):
    op: Literal["move", "copy"]
    path: str
    from_path: str = Field(alias="from")


class IncrementOperation(Contract):
    op: Literal["increment"]
    path: str
    value: StrictInt | StrictFloat


Operation = Annotated[
    ValueOperation | RemoveOperation | TransferOperation | IncrementOperation,
    Field(discriminator="op"),
]
Operations = Annotated[list[Operation], Field(min_length=1, max_length=100)]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Label = Annotated[str, Field(min_length=1, max_length=120)]
Version = Annotated[StrictInt, Field(ge=0)]


class CreateTimeline(Contract):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "name": "Workshop inventory", "initial_state": {"stock": {"frames": 10}, "cash": 250}
    }]})
    name: Name
    initial_state: dict[str, JsonValue] = Field(
        description="The JSON document at version 0. Events never overwrite this snapshot."
    )


class AppendEvent(Contract):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "label": "Deliver five frames", "expected_version": 0,
        "operations": [{"op": "increment", "path": "/stock/frames", "value": 5}]
    }]})
    label: Label
    operations: Operations
    expected_version: Version = Field(
        description="The current head version. A stale value returns 409 without adding an event."
    )


class ForkTimeline(Contract):
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "Alternate delivery", "version": 1}]})
    name: Name
    version: Version = Field(description="The source version that becomes the branch head.")


class ExperimentTimeline(Contract):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "name": "Larger delivery", "event_version": 1, "label": "Deliver eight frames",
        "operations": [{"op": "increment", "path": "/stock/frames", "value": 8}]
    }]})
    name: Name
    event_version: Annotated[StrictInt, Field(ge=1)] = Field(
        description="Replace this event on a new branch, then replay every later event."
    )
    operations: Operations
    label: Label | None = None


class MergeResolution(Contract):
    path: str
    choice: Literal["target", "source"]


class MergeTimeline(Contract):
    source_id: Annotated[str, Field(min_length=1, max_length=128)]
    source_version: Version = Field(description="Pin the exact source version reviewed in the preview.")
    expected_version: Version = Field(description="The target head version reviewed in the preview.")
    resolutions: list[MergeResolution] = Field(default_factory=list)
    label: Label | None = None


class WorkshopStockAction(Contract):
    kind: Literal["delivery", "return"]
    item: Literal["frames", "wheels"]
    quantity: Annotated[StrictInt, Field(gt=0)]
    expected_version: Version


class WorkshopBuildAction(Contract):
    kind: Literal["build"]
    quantity: Annotated[StrictInt, Field(gt=0)]
    expected_version: Version


WorkshopAction = Annotated[WorkshopStockAction | WorkshopBuildAction, Field(discriminator="kind")]


class Timeline(Contract):
    id: str
    name: str
    created_at: str
    initial_at: str
    parent_id: str | None
    fork_version: int | None
    head_version: int
    head_hash: str


class TimelinePage(Contract):
    items: list[Timeline]
    next_cursor: str | None


class MergeMetadata(Contract):
    source_timeline_id: str
    source_version: int
    base_timeline_id: str
    base_version: int


class Event(Contract):
    id: str
    timeline_id: str
    version: int
    label: str
    operations: list[Operation]
    recorded_at: str
    state_hash: str
    inherited: bool
    source_event_id: str | None
    source_timeline_id: str | None
    merge: MergeMetadata | None = None


class EventPage(Contract):
    items: list[Event]
    next_after_version: int | None


class State(Contract):
    timeline_id: str
    version: int
    recorded_at: str
    state: dict[str, JsonValue]
    state_hash: str
    head_version: int


class AppendedEvent(Contract):
    timeline: Timeline
    event: Event
    state: State


class MergeValue(Contract):
    exists: bool
    value: JsonValue = None


class MergeConflict(Contract):
    path: str
    base: MergeValue
    target: MergeValue
    source: MergeValue


class MergePreview(Contract):
    base: State
    target: State
    source: State
    merged_state: dict[str, JsonValue] | None
    conflicts: list[MergeConflict]


class MergedEvent(AppendedEvent):
    base: State
    source: State


class Experiment(Contract):
    timeline: Timeline
    replayed_events: int
    changed_version: int


class Comparison(Contract):
    left: State
    right: State
    changes: list[Operation]


class TimelineExport(Contract):
    timeline: Timeline
    initial_state: dict[str, JsonValue]
    events: list[Event]
    state: State


class ErrorDetail(Contract):
    code: str
    message: str
    details: JsonValue


class ErrorResponse(Contract):
    error: ErrorDetail


class Health(Contract):
    status: Literal["ok"]
    api_key_required: bool
