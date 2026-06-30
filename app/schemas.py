from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DEVICE_ID_RE = re.compile(r"^[a-f0-9]{64}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
TASK_CATEGORIES = {
    "C0",
    "C1",
    "C2",
    "C3",
    "C4",
    "C5",
    "C6",
    "C7",
}


class TimeSyncConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    method: Literal["HTTP_MIDPOINT"]
    region: str
    server_time_field: Literal["serverTimeMillis"] = Field(alias="serverTimeField")
    recommended_ntp_servers: list[str] = Field(alias="recommendedNtpServers", min_length=1)
    max_acceptable_rtt_millis: int = Field(alias="maxAcceptableRttMillis", gt=0)


class ConfigResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    server_study_salt: str = Field(alias="serverStudySalt")
    rules_version: str = Field(alias="rulesVersion")
    server_time_millis: int = Field(alias="serverTimeMillis")
    time_sync: TimeSyncConfig = Field(alias="timeSync")


class UiRedactionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    target: Literal["text", "content_description", "node"]
    action: Literal["REDACT", "DROP", "ALLOW"]
    pattern: str | None = None
    replacement: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_rule_contract(self) -> "UiRedactionRule":
        if self.action in {"REDACT", "DROP"} and not self.pattern:
            raise ValueError("redaction_rule_requires_pattern")
        if self.pattern:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError("invalid_redaction_rule_pattern") from exc
        return self


class RulesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    updated_at: str
    rules: list[UiRedactionRule] = Field(default_factory=list)
    package_blocklist: list[str] = Field(default_factory=list)
    max_text_length: int = Field(ge=1)
    default_text_action: Literal["REDACT", "DROP", "ALLOW"]
    rule_hash: str

    @field_validator("rule_hash")
    @classmethod
    def validate_rule_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("invalid_rule_hash")
        return value


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["LZ4_FRAME+JSON"]
    payload_base64: str = Field(min_length=1)
    payload_sha256_hex: str
    device_id: str
    batch_id: str
    rule_version: str
    rule_hash: str
    created_at_wall_millis: int = Field(ge=0)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not DEVICE_ID_RE.fullmatch(value):
            raise ValueError("invalid_device_id")
        return value

    @field_validator("payload_sha256_hex", "rule_hash")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("invalid_sha256_hex")
        return value

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        import uuid

        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("invalid_batch_id") from exc


class BoundsGrid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: int
    top: int
    right: int
    bottom: int


class NodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_id: str
    class_name: str | None = None
    viewIdResourceName: str | None = None
    bounds_grid: BoundsGrid | None = None
    clickable: bool = False
    editable: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    visible_to_user: bool = True
    long_clickable: bool = False
    password: bool = False
    input_type_category: str | None = None
    child_count: int = 0
    text: str | None = None
    text_redacted: str | None = None
    content_desc_redacted: str | None = None
    actions_summary: list[str] = Field(default_factory=list)
    depth: int = 0

    @model_validator(mode="after")
    def reject_password_nodes(self) -> "NodeSnapshot":
        if self.password:
            raise ValueError("password_node_must_be_dropped")
        if self.editable and self.text not in {None, "", "<EDITABLE_TEXT_DROPPED>"}:
            raise ValueError("editable_text_must_be_dropped")
        return self


class SensorSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sensor_type: Literal["ACCELEROMETER", "GYROSCOPE", "MAGNETIC_FIELD"]
    timestamp_elapsed_nanos: int = Field(ge=0)
    wall_time_estimated_millis: int = Field(ge=0)
    x: float
    y: float
    z: float
    accuracy: int | None = None


class TouchEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: Literal[
        "TOUCH_INTERACTION_START",
        "TOUCH_INTERACTION_END",
        "TOUCH_DOWN",
        "TOUCH_UP",
        "TOUCH_POINTER_DOWN",
        "TOUCH_POINTER_UP",
        "TOUCH_CANCEL",
    ]
    event_time_uptime_millis: int = Field(ge=0)
    event_time_wall_millis: int = Field(ge=0)
    collected_at_wall_millis: int = Field(ge=0)


class ContextEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    event_type: str
    event_time_wall_millis: int = Field(ge=0)
    app_package_name: str | None = None
    foreground_activity_class_name: str | None = None
    foreground_component_name: str | None = None
    input_method_visible: bool = False
    coarse_orientation: Literal["portrait", "landscape", "portrait_reverse", "landscape_reverse", "unknown"] = "unknown"
    window_title_redacted: str | None = None
    root_nodes: list[NodeSnapshot] = Field(default_factory=list)
    redaction_summary: dict[str, int] = Field(default_factory=dict)


class ContextFeature(BaseModel):
    model_config = ConfigDict(extra="allow")

    feature_id: str
    event_id: str
    computed_at_wall_millis: int = Field(ge=0)
    collection_source: Literal["BUILTIN_TASK", "THIRD_PARTY_APP"]
    task_sequence: int | None = None
    task_id: str | None = None
    task_name: str | None = None
    task_intuitive_description: str | None = None
    task_category: str | None = None
    task_session_id: str | None = None
    input_method_visible: bool = False
    keyboard_visible_estimated: bool | None = None
    editable_count: int = 0
    scrollable_count: int = 0
    clickable_count: int = 0
    password_node_seen: bool = False
    media_like_score: float = 0.0
    list_like_score: float = 0.0
    form_like_score: float = 0.0
    game_like_score: float = 0.0
    node_class_histogram: dict[str, int] = Field(default_factory=dict)
    event_type: str | None = None
    coarse_orientation: Literal["portrait", "landscape", "portrait_reverse", "landscape_reverse", "unknown"] | None = None
    estimated_context_category: str = "UNKNOWN"

    @model_validator(mode="after")
    def validate_category(self) -> "ContextFeature":
        if self.task_category is not None and self.task_category not in TASK_CATEGORIES:
            raise ValueError("invalid_task_category")
        return self


class BatchDiagnostics(BaseModel):
    model_config = ConfigDict(extra="allow")

    sensor_sample_count: int = 0
    context_event_count: int = 0
    touch_event_count: int = 0
    redaction_applied: Literal[True]
    compression: Literal["lz4_frame"]
    encryption: Literal["none"]
    sampling_rate_hz: int | None = None


class Batch(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_id: str
    device_id: str
    session_id: str = Field(min_length=1)
    record_type: Literal["collection"]
    collection_source: Literal["BUILTIN_TASK", "THIRD_PARTY_APP"]
    app_package_name: str
    foreground_activity_class_name: str | None = None
    foreground_component_name: str | None = None
    sampling_rate_hz: int = Field(gt=0)
    batch_duration_seconds: int = Field(ge=0)
    task_sequence: int | None = None
    task_id: str | None = None
    task_name: str | None = None
    task_intuitive_description: str | None = None
    task_category: str | None = None
    task_session_id: str | None = None
    task_started_at_wall_millis: int | None = None
    task_elapsed_seconds_at_batch_end: int | None = None
    app_version: str
    rule_version: str
    rule_hash: str
    consent_version: str
    started_at_wall_millis: int = Field(ge=0)
    ended_at_wall_millis: int = Field(ge=0)
    base_elapsed_nanos: int = Field(ge=0)
    sensor_samples: list[SensorSample] = Field(default_factory=list)
    touch_events: list[TouchEvent] = Field(default_factory=list)
    context_events: list[ContextEvent] = Field(default_factory=list)
    context_features: list[ContextFeature] = Field(default_factory=list)
    skip_events: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: BatchDiagnostics

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not DEVICE_ID_RE.fullmatch(value):
            raise ValueError("invalid_device_id")
        return value

    @field_validator("batch_id")
    @classmethod
    def validate_batch_uuid(cls, value: str) -> str:
        import uuid

        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("invalid_batch_id") from exc

    @model_validator(mode="after")
    def validate_task_label_contract(self) -> "Batch":
        if self.started_at_wall_millis > self.ended_at_wall_millis:
            raise ValueError("batch_started_after_ended")
        if self.collection_source == "BUILTIN_TASK":
            missing = [
                self.task_id,
                self.task_sequence,
                self.task_name,
                self.task_intuitive_description,
                self.task_category,
                self.task_session_id,
                self.task_started_at_wall_millis,
                self.task_elapsed_seconds_at_batch_end,
            ]
            if any(item is None for item in missing):
                raise ValueError("builtin_task_requires_task_fields")
            if self.task_category not in TASK_CATEGORIES:
                raise ValueError("invalid_task_category")
            if self.task_id != self.task_category:
                raise ValueError("task_id_must_match_task_category")
            if self.task_sequence != int(self.task_id[1:]):
                raise ValueError("invalid_task_sequence")
        else:
            if any(
                item is not None
                for item in [
                    self.task_sequence,
                    self.task_id,
                    self.task_name,
                    self.task_intuitive_description,
                    self.task_category,
                    self.task_session_id,
                    self.task_started_at_wall_millis,
                    self.task_elapsed_seconds_at_batch_end,
                ]
            ):
                raise ValueError("third_party_task_fields_must_be_null")

        for feature in self.context_features:
            if feature.collection_source != self.collection_source:
                raise ValueError("context_feature_collection_source_mismatch")
            if feature.task_category != self.task_category:
                raise ValueError("context_feature_task_category_mismatch")
            if feature.task_id != self.task_id:
                raise ValueError("context_feature_task_id_mismatch")
            if feature.task_sequence != self.task_sequence:
                raise ValueError("context_feature_task_sequence_mismatch")
            if feature.task_name != self.task_name:
                raise ValueError("context_feature_task_name_mismatch")
            if feature.task_intuitive_description != self.task_intuitive_description:
                raise ValueError("context_feature_task_intuitive_description_mismatch")
            if feature.task_session_id != self.task_session_id:
                raise ValueError("context_feature_task_session_id_mismatch")
        context_event_ids = {event.event_id for event in self.context_events}
        for feature in self.context_features:
            if feature.event_id not in context_event_ids:
                raise ValueError("context_feature_event_id_not_found")
        if self.diagnostics.sensor_sample_count != len(self.sensor_samples):
            raise ValueError("diagnostics_sensor_sample_count_mismatch")
        if self.diagnostics.context_event_count != len(self.context_events):
            raise ValueError("diagnostics_context_event_count_mismatch")
        if self.diagnostics.touch_event_count != len(self.touch_events):
            raise ValueError("diagnostics_touch_event_count_mismatch")
        if self.diagnostics.sampling_rate_hz is not None and self.diagnostics.sampling_rate_hz != self.sampling_rate_hz:
            raise ValueError("diagnostics_sampling_rate_mismatch")
        return self
