"""
models.py
=========

Defines the "shape" of the data this API accepts and returns, using
Pydantic (a library FastAPI uses to validate incoming JSON automatically).

If you're new to Python/Pydantic: think of each class below as a form
with labeled fields. When a request comes in, FastAPI checks the JSON
body against the matching class here. If a required field is missing or
has the wrong type, FastAPI automatically returns a clear HTTP 422 error
explaining what's wrong -- you don't have to write that validation code
yourself.

`Optional[str] = None` means "this field is a string, but it's allowed to
be missing/null". Fields without `= None` are required.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TelemetryRecordIn(BaseModel):
    """One traceability log entry, pushed by telemetry-handler.js or
    telemetry-watcher.js via POST /api/telemetry.

    This intentionally mirrors the exact JSON structure already written
    to the local ~/.kiro/logs/telemetry.log file by those two scripts, so
    the same record can be logged locally AND sent here unchanged
    ("keep the traceability log entry as it is now", per requirements).

    Every field except `timestamp`, `type`, and `session_id` is optional
    because the two source scripts produce two different kinds of
    records ("turn_tokens" from the handler, "turn_credits" from the
    watcher) that don't share every field.
    """

    timestamp: str
    type: str
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    model: Optional[str] = None

    # --- Source-control context fields --------------------------------
    # Populated by telemetry-handler.js / telemetry-watcher.js from the
    # repository (if any) present in `cwd` at the time of the event.
    # These match the field names actually sent by those scripts. All
    # optional: a session may not be inside a repository at all, or the
    # remote may not follow a recognizable hosting convention.
    project_name: Optional[str] = Field(
        default=None,
        description=(
            "The project name as reported by the handler/watcher, e.g. "
            "\"APIM-Kong-DevOps\". In practice this is usually the same "
            "value as repo_name, but is reported separately in case the "
            "two ever diverge (e.g. a monorepo subproject)."
        ),
    )
    repo_name: Optional[str] = Field(
        default=None,
        description=(
            "The repository name, e.g. \"APIM-Kong-DevOps\". Used to group "
            "usage by repository regardless of which remote host, "
            "protocol, or organization was used."
        ),
    )
    organization: Optional[str] = Field(
        default=None,
        description=(
            "The organization/account that owns the remote repository, "
            "e.g. \"ericsson\". Parsed from the remote URL."
        ),
    )
    branch: Optional[str] = Field(
        default=None,
        description=(
            "The current branch checked out in `cwd` at the time of the "
            "event, e.g. \"main\" or \"master\"."
        ),
    )
    scm_type: Optional[str] = Field(
        default=None,
        description=(
            "The source-control hosting platform, e.g. \"Azure DevOps\", "
            "\"GitHub\", or \"GitLab\". Distinct from vcs_type (the "
            "underlying version-control tool)."
        ),
    )
    vcs_type: Optional[str] = Field(
        default=None,
        description=(
            "The version-control system in use, e.g. \"git\". Present so "
            "this schema can support non-git systems later without a "
            "breaking change."
        ),
    )
    remote_url: Optional[str] = Field(
        default=None,
        description=(
            "The repository's remote URL exactly as reported by the "
            "handler/watcher, such as "
            "\"https://ericsson@dev.azure.com/ericsson/APIM-Kong-DevOps/_git/APIM-Kong-DevOps\" "
            "or \"https://github.com/acme/payments-api.git\". Used to "
            "attribute usage to a specific remote/host."
        ),
    )

    # Fields specific to "turn_tokens" records (from telemetry-handler.js)
    prompt_tokens_estimated: Optional[int] = None
    response_tokens_estimated: Optional[int] = None
    total_tokens_estimated: Optional[int] = None
    token_estimation_method: Optional[str] = None

    # Only present when the corresponding capture setting is enabled on
    # the client (telemetry.captureRequestPayload / ...ResponsePayload).
    request_payload: Optional[str] = None
    response_payload: Optional[str] = None

    # Fields specific to "turn_credits" records (from telemetry-watcher.js)
    credits_used: Optional[float] = None
    credit_unit: Optional[str] = None
    tools_used: Optional[List[str]] = None
    elapsed_ms: Optional[int] = None
    status: Optional[str] = None
    source_event_timestamp: Optional[str] = None

    class Config:
        # Allows any extra fields not explicitly listed above to pass
        # through without being rejected. This keeps the API forward
        # compatible if the handler/watcher scripts add new fields later.
        extra = "allow"


class TelemetryRecordOut(TelemetryRecordIn):
    """Same shape as TelemetryRecordIn, plus the server-assigned record id
    and the time the server received it. Returned by GET /api/telemetry.
    """

    record_id: str
    received_at: str


class TelemetryIngestResponse(BaseModel):
    """What POST /api/telemetry replies with on success."""

    status: str = "ok"
    record_id: str


class HealthHeartbeatIn(BaseModel):
    """One heartbeat, pushed by telemetry-handler.js or
    telemetry-watcher.js via POST /api/health, roughly every 5 minutes
    (watcher) or on every hook invocation (handler).
    """

    component: str = Field(..., description='"handler" or "watcher"')
    session_id: Optional[str] = Field(
        default=None,
        description="Kiro CLI session id this component is reporting for; "
        "may be null for a watcher heartbeat sent before any session "
        "activity has been observed yet.",
    )
    machine_name: str
    user_name: str
    pid: Optional[int] = None
    timestamp: str

    # Optional source-control context, same meaning as on TelemetryRecordIn.
    # Included here too so a session with no telemetry events yet (e.g. the
    # watcher's very first heartbeat) can still be attributed to a
    # repo/branch as soon as one is known.
    project_name: Optional[str] = None
    repo_name: Optional[str] = None
    organization: Optional[str] = None
    branch: Optional[str] = None
    scm_type: Optional[str] = None
    vcs_type: Optional[str] = None
    remote_url: Optional[str] = None

    class Config:
        # Forward-compatible with the same policy as TelemetryRecordIn.
        extra = "allow"


class HealthIngestResponse(BaseModel):
    """What POST /api/health replies with on success."""

    status: str = "ok"


class HealthRecordOut(BaseModel):
    """One entry in the GET /api/health response: the latest known
    status of a single (session_id, component) pair.
    """

    session_id: Optional[str]
    component: str
    machine_name: str
    user_name: str
    pid: Optional[int]
    last_heartbeat_at: str
    seconds_since_last_heartbeat: float
    is_alive: bool
    project_name: Optional[str] = None
    repo_name: Optional[str] = None
    organization: Optional[str] = None
    branch: Optional[str] = None
    scm_type: Optional[str] = None
    vcs_type: Optional[str] = None
    remote_url: Optional[str] = None


class TelemetryListResponse(BaseModel):
    """What GET /api/telemetry replies with: a page of records plus
    simple pagination info, so the dashboard can show "N records,
    showing latest 100" style summaries.
    """

    total_count: int
    records: List[TelemetryRecordOut]


class HealthListResponse(BaseModel):
    """What GET /api/health replies with: the latest heartbeat status
    for every (session, component) pair the server has ever seen.
    """

    total_count: int
    records: List[HealthRecordOut]


class HealthClearResponse(BaseModel):
    """What DELETE /api/health replies with: how many (session,
    component) heartbeat entries were removed.
    """

    status: str = "ok"
    cleared_count: int


class TelemetryClearResponse(BaseModel):
    """What DELETE /api/telemetry replies with: how many telemetry
    records were removed.
    """

    status: str = "ok"
    cleared_count: int


class AnalyticsBucket(BaseModel):
    """One row of the aggregated (grouped) analytics view: a single
    group-by combination (e.g. one repo+branch+feature+user+session) plus
    its computed totals for the selected date range.
    """

    group_key: str = Field(
        description="Stable identifier for this group, built from the "
        "dimension values below; used as a React-style list key on the "
        "frontend and for chart series labels."
    )
    user_name: Optional[str] = None
    session_id: Optional[str] = None
    organization: Optional[str] = None
    repo_name: Optional[str] = None
    project_name: Optional[str] = None
    branch: Optional[str] = None
    scm_type: Optional[str] = None
    vcs_type: Optional[str] = None
    remote_url: Optional[str] = None

    turn_count: int = Field(description="Number of turn_tokens events in this group")
    total_prompt_tokens: int
    total_response_tokens: int
    total_tokens: int
    total_credits_used: float
    first_seen_at: Optional[str] = Field(
        default=None, description="Earliest event timestamp contributing to this group"
    )
    last_seen_at: Optional[str] = Field(
        default=None, description="Latest event timestamp contributing to this group"
    )


class AnalyticsDailyPoint(BaseModel):
    """One point on the time-series used by the "Charts" tab: totals for
    a single calendar day (UTC) within the selected date range, after all
    other filters have been applied.
    """

    date: str = Field(description="Calendar day in YYYY-MM-DD form (UTC)")
    turn_count: int
    total_tokens: int
    total_credits_used: float


class AnalyticsFilterOptions(BaseModel):
    """The distinct values currently available for each filterable
    dimension, given the data on hand -- used to populate the dashboard's
    filter dropdowns without the frontend needing its own copy of "every
    value we've ever seen."
    """

    user_names: List[str]
    session_ids: List[str]
    organizations: List[str]
    repo_names: List[str]
    project_names: List[str]
    branches: List[str]
    scm_types: List[str]
    vcs_types: List[str]


class TelemetryAnalyticsResponse(BaseModel):
    """What GET /api/telemetry/analytics replies with: both the
    aggregated (grouped) view and the detailed (record-level) view for
    the same filter/date-range selection, plus a daily time series for
    charting and the set of distinct filter values seen, so a single
    request can drive the whole dashboard (summary tiles, detail table,
    and charts) without redundant round trips.
    """

    total_matching_records: int = Field(
        description="Total number of raw telemetry records that matched the filters "
        "(before the `limit` on `records` below was applied)"
    )
    aggregate: List[AnalyticsBucket]
    daily_series: List[AnalyticsDailyPoint]
    records: List[TelemetryRecordOut]
    filter_options: AnalyticsFilterOptions
