"""
routers/telemetry.py
=====================

Implements the two telemetry endpoints:

  POST /api/telemetry  (API #1) - the handler/watcher push a log entry here
  GET  /api/telemetry  (API #3) - the dashboard reads log entries from here

Both are protected by the X-API-Key header (see app/security.py).

How records are stored in Redis (see app/redis_client.py for the full
data-model explanation):

  - Each record gets a unique id (a UUID) and is stored as JSON text in
    the Redis key  telemetry:record:<id>  with a 7-day expiry (TTL).
  - The id is also added to a sorted set  telemetry:index , scored by the
    time it was received, so we can fetch "most recent N records" (or
    records for one session) quickly, sorted, without loading everything.
"""

import json
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config import settings
from app.models import (
    AnalyticsBucket,
    AnalyticsDailyPoint,
    AnalyticsFilterOptions,
    TelemetryAnalyticsResponse,
    TelemetryClearResponse,
    TelemetryIngestResponse,
    TelemetryListResponse,
    TelemetryRecordIn,
    TelemetryRecordOut,
)
from app.redis_client import redis_client
from app.security import require_api_key

logger = logging.getLogger("kiro_trace.telemetry")

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])

# Redis key names used by this router. Centralized here so there's exactly
# one place to look if you ever need to change the naming scheme.
INDEX_KEY = "telemetry:index"

# Health data lives under this prefix (see app/routers/health.py). Telemetry
# records themselves don't carry machine_name/user_name (see
# TelemetryRecordIn in app/models.py) -- only session_id -- so filtering
# telemetry by machine or user requires cross-referencing the health index
# to find which session_id(s) belong to that machine/user, same as the
# dashboard already does client-side in dashboard.js's buildUsageTree().
HEALTH_INDEX_KEY = "health:index"


def _record_key(record_id: str) -> str:
    return f"telemetry:record:{record_id}"


def _session_ids_for(machine_name: Optional[str] = None, user_name: Optional[str] = None) -> Set[str]:
    """Looks up every session_id that has ever reported a heartbeat from
    the given machine_name and/or user_name, by scanning the health index.

    Used by DELETE /api/telemetry to support clearing telemetry data by
    machine or user, since telemetry records themselves only carry a
    session_id, not machine/user info directly.
    """
    matching_session_ids: Set[str] = set()
    keys = redis_client.smembers(HEALTH_INDEX_KEY)
    for key in keys:
        raw = redis_client.get(key)
        if raw is None:
            continue
        data = json.loads(raw)
        if machine_name is not None and data.get("machine_name") != machine_name:
            continue
        if user_name is not None and data.get("user_name") != user_name:
            continue
        session_id = data.get("session_id")
        if session_id:
            matching_session_ids.add(session_id)
    return matching_session_ids


@router.post("", response_model=TelemetryIngestResponse, dependencies=[Depends(require_api_key)])
async def ingest_telemetry(record: TelemetryRecordIn) -> TelemetryIngestResponse:
    """API #1: receives one telemetry log entry from telemetry-handler.js
    or telemetry-watcher.js and stores it in Redis.

    The record's own JSON shape is preserved exactly as sent (this is the
    same structure already written to the local telemetry.log file) --
    we only add a server-generated `record_id` and `received_at` on top,
    we never modify the fields the client sent.
    """
    record_id = str(uuid.uuid4())
    received_at = datetime.now(timezone.utc).isoformat()

    stored = record.dict()
    stored["record_id"] = record_id
    stored["received_at"] = received_at

    # Log the exact payload as received (before we add record_id/received_at)
    # so you can see precisely what the hook sent -- including whether
    # source-control fields like organization/repo_name/project_name/
    # branch/scm_type/vcs_type/remote_url were present at all. This is the
    # quickest way to tell "the hook isn't sending it" apart from "the
    # server/dashboard isn't showing it". Remove or lower to DEBUG once
    # you've confirmed the fields are arriving as expected.
    logger.info(
        "POST /api/telemetry payload received (record_id=%s): %s",
        record_id,
        json.dumps(record.dict(), sort_keys=True),
    )

    # Store the full record as a single JSON string. `redis_client.set`
    # with `ex=...` sets the value AND its time-to-live in one call: the
    # key disappears automatically after TELEMETRY_TTL_SECONDS (7 days by
    # default), so old telemetry doesn't accumulate in Redis forever.
    redis_client.set(
        _record_key(record_id),
        json.dumps(stored),
        ex=settings.TELEMETRY_TTL_SECONDS,
    )

    # Add this record's id to the sorted set, scored by arrival time (as a
    # Unix timestamp), so GET /api/telemetry can return the newest records
    # first without scanning every key. We use a Redis "sorted set" (ZADD)
    # here because it keeps the index automatically ordered.
    redis_client.zadd(INDEX_KEY, {record_id: time.time()})

    return TelemetryIngestResponse(record_id=record_id)


@router.get("", response_model=TelemetryListResponse, dependencies=[Depends(require_api_key)])
async def list_telemetry(
    session_id: Optional[str] = Query(
        default=None, description="Only return records for this Kiro session id"
    ),
    project_name: Optional[str] = Query(
        default=None, description="Only return records from this project name"
    ),
    repo_name: Optional[str] = Query(
        default=None, description="Only return records from this repository name"
    ),
    branch: Optional[str] = Query(
        default=None, description="Only return records recorded on this branch"
    ),
    organization: Optional[str] = Query(
        default=None, description="Only return records from this organization/account"
    ),
    limit: int = Query(
        default=200, ge=1, le=2000, description="Maximum number of records to return"
    ),
) -> TelemetryListResponse:
    """API #3: returns the most recent telemetry records, optionally
    filtered to a single session_id and/or source-control dimension, for
    the dashboard's raw log view.

    Since there can be multiple active Kiro sessions at once (possibly on
    different machines, all pushing to this same service), the dashboard
    calls this endpoint WITHOUT a session_id filter to get an overview of
    everything, or WITH a session_id to drill into one session. For
    richer filtering (by user, date range, or multiple source-control
    dimensions at once) plus aggregated totals, see
    GET /api/telemetry/analytics instead.
    """
    has_extra_filter = any([project_name, repo_name, branch, organization])
    # ZREVRANGE returns ids from the sorted set, newest first (highest
    # score first), which is exactly the order a dashboard wants to show.
    # We fetch more than `limit` when filtering, since some of the newest
    # records may not match and get filtered out below; fetching a
    # generous batch keeps this simple without needing a secondary Redis
    # index per filterable field for this proof of concept.
    fetch_count = limit if not (session_id or has_extra_filter) else min(2000, limit * 10)
    record_ids = redis_client.zrevrange(INDEX_KEY, 0, fetch_count - 1)

    records: List[TelemetryRecordOut] = []
    for record_id in record_ids:
        raw = redis_client.get(_record_key(record_id))
        if raw is None:
            # The record's TTL expired (or it was removed) after the id
            # was added to the index; skip it. We also opportunistically
            # remove the stale id from the index so it stops being
            # re-fetched on future requests.
            redis_client.zrem(INDEX_KEY, record_id)
            continue

        data = json.loads(raw)
        if session_id is not None and data.get("session_id") != session_id:
            continue
        if project_name is not None and data.get("project_name") != project_name:
            continue
        if repo_name is not None and data.get("repo_name") != repo_name:
            continue
        if branch is not None and data.get("branch") != branch:
            continue
        if organization is not None and data.get("organization") != organization:
            continue

        records.append(TelemetryRecordOut(**data))
        if len(records) >= limit:
            break

    return TelemetryListResponse(total_count=len(records), records=records)


# ---------------------------------------------------------------------
# Analytics: aggregated + detailed usage, filterable by user, session,
# source-control dimensions (organization, repo/project name, branch,
# SCM host type, VCS type), and a date range. Backs the dashboard's
# "Overview", "Detailed Records", and "Charts" tabs from a single call.
# ---------------------------------------------------------------------

# Safety cap on how many raw records this endpoint will ever scan per
# request. There is no secondary index on any of the new dimensions (see
# redis_client.py's data model notes), so every request walks the full
# telemetry:index once; this cap keeps a single request bounded even if
# TELEMETRY_TTL_SECONDS is raised well beyond the default 7 days.
MAX_ANALYTICS_SCAN = 20000


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _matches_analytics_filters(
    data: dict,
    user_name: Optional[str],
    session_id: Optional[str],
    organization: Optional[str],
    repo_name: Optional[str],
    project_name: Optional[str],
    branch: Optional[str],
    scm_type: Optional[str],
    vcs_type: Optional[str],
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    session_id_to_user: Dict[str, str],
) -> bool:
    if session_id is not None and data.get("session_id") != session_id:
        return False
    if user_name is not None:
        resolved_user = session_id_to_user.get(data.get("session_id") or "")
        if resolved_user != user_name:
            return False
    if organization is not None and data.get("organization") != organization:
        return False
    if repo_name is not None and data.get("repo_name") != repo_name:
        return False
    if project_name is not None and data.get("project_name") != project_name:
        return False
    if branch is not None and data.get("branch") != branch:
        return False
    if scm_type is not None and data.get("scm_type") != scm_type:
        return False
    if vcs_type is not None and data.get("vcs_type") != vcs_type:
        return False

    if start_dt is not None or end_dt is not None:
        event_dt = _parse_iso(data.get("timestamp"))
        if event_dt is None:
            return False
        if start_dt is not None and event_dt < start_dt:
            return False
        if end_dt is not None and event_dt > end_dt:
            return False

    return True


@router.get(
    "/analytics",
    response_model=TelemetryAnalyticsResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_telemetry_analytics(
    user_name: Optional[str] = Query(
        default=None,
        description="Only include records belonging to sessions reported by this user "
        "(resolved via heartbeat data from /api/health, same as the existing "
        "machine/user telemetry-clear filters).",
    ),
    session_id: Optional[str] = Query(
        default=None, description="Only include records for this Kiro session id."
    ),
    organization: Optional[str] = Query(
        default=None,
        description="Only include records from this organization/account, e.g. \"ericsson\".",
    ),
    repo_name: Optional[str] = Query(
        default=None, description="Only include records from this repository name."
    ),
    project_name: Optional[str] = Query(
        default=None, description="Only include records from this project name."
    ),
    branch: Optional[str] = Query(
        default=None, description="Only include records recorded on this branch."
    ),
    scm_type: Optional[str] = Query(
        default=None,
        description='Only include records hosted on this SCM platform, e.g. "Azure DevOps", "GitHub".',
    ),
    vcs_type: Optional[str] = Query(
        default=None, description='Only include records from this version-control system, e.g. "git".'
    ),
    start_date: Optional[str] = Query(
        default=None,
        description="Only include records with an event timestamp on or after this ISO-8601 "
        "date/time (e.g. 2026-08-01 or 2026-08-01T00:00:00Z).",
    ),
    end_date: Optional[str] = Query(
        default=None,
        description="Only include records with an event timestamp on or before this ISO-8601 "
        "date/time (e.g. 2026-08-24 or 2026-08-24T23:59:59Z).",
    ),
    group_by: str = Query(
        default="organization,repo_name,branch,user_name,session_id",
        description="Comma-separated list of dimensions to group the aggregate view by. "
        "Valid values: user_name, session_id, organization, repo_name, project_name, "
        "branch, scm_type, vcs_type, remote_url.",
    ),
    record_limit: int = Query(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of individual records to return in the detailed view "
        "(the aggregate view and daily chart series always reflect ALL matching "
        "records, regardless of this limit).",
    ),
) -> TelemetryAnalyticsResponse:
    """Returns both an aggregated (grouped) view and a detailed
    (record-level) view of telemetry usage for the same filter selection,
    plus a UTC-daily time series and the set of distinct filter values
    currently available -- everything the dashboard's Overview, Detailed
    Records, and Charts tabs need from a single request.
    """
    valid_group_fields = {
        "user_name",
        "session_id",
        "organization",
        "repo_name",
        "project_name",
        "branch",
        "scm_type",
        "vcs_type",
        "remote_url",
    }
    group_fields = [f.strip() for f in group_by.split(",") if f.strip()]
    for field in group_fields:
        if field not in valid_group_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown group_by field: {field!r}. Valid values: {sorted(valid_group_fields)}",
            )
    if not group_fields:
        group_fields = ["session_id"]

    start_dt = _parse_iso(start_date)
    end_dt = _parse_iso(end_date)
    if start_date and start_dt is None:
        raise HTTPException(status_code=400, detail=f"Could not parse start_date: {start_date!r}")
    if end_date and end_dt is None:
        raise HTTPException(status_code=400, detail=f"Could not parse end_date: {end_date!r}")

    # session_id -> user_name, resolved from heartbeat data, same pattern
    # already used by _session_ids_for() for the existing machine/user
    # telemetry-clear filters. Needed here because telemetry records only
    # carry a session_id, not the user/machine that produced them.
    session_id_to_user: Dict[str, str] = {}
    for key in redis_client.smembers(HEALTH_INDEX_KEY):
        raw = redis_client.get(key)
        if raw is None:
            continue
        data = json.loads(raw)
        sid = data.get("session_id")
        if sid:
            session_id_to_user[sid] = data.get("user_name", "unknown")

    all_record_ids = redis_client.zrevrange(INDEX_KEY, 0, MAX_ANALYTICS_SCAN - 1)

    total_matching = 0
    detail_records: List[TelemetryRecordOut] = []

    # group_key -> running totals
    buckets: Dict[Tuple, dict] = {}
    # date (YYYY-MM-DD) -> running totals, for the chart series
    daily: Dict[str, dict] = defaultdict(lambda: {"turn_count": 0, "total_tokens": 0, "total_credits_used": 0.0})

    # Distinct values seen across ALL matching records, for populating
    # filter dropdowns on the frontend.
    seen_users: Set[str] = set()
    seen_sessions: Set[str] = set()
    seen_organizations: Set[str] = set()
    seen_repo_names: Set[str] = set()
    seen_project_names: Set[str] = set()
    seen_branches: Set[str] = set()
    seen_scm_types: Set[str] = set()
    seen_vcs_types: Set[str] = set()

    for record_id in all_record_ids:
        raw = redis_client.get(_record_key(record_id))
        if raw is None:
            redis_client.zrem(INDEX_KEY, record_id)
            continue

        data = json.loads(raw)

        if not _matches_analytics_filters(
            data,
            user_name,
            session_id,
            organization,
            repo_name,
            project_name,
            branch,
            scm_type,
            vcs_type,
            start_dt,
            end_dt,
            session_id_to_user,
        ):
            continue

        total_matching += 1
        resolved_user = session_id_to_user.get(data.get("session_id") or "", "unknown")

        if resolved_user and resolved_user != "unknown":
            seen_users.add(resolved_user)
        if data.get("session_id"):
            seen_sessions.add(data["session_id"])
        if data.get("organization"):
            seen_organizations.add(data["organization"])
        if data.get("repo_name"):
            seen_repo_names.add(data["repo_name"])
        if data.get("project_name"):
            seen_project_names.add(data["project_name"])
        if data.get("branch"):
            seen_branches.add(data["branch"])
        if data.get("scm_type"):
            seen_scm_types.add(data["scm_type"])
        if data.get("vcs_type"):
            seen_vcs_types.add(data["vcs_type"])

        dimension_values = {
            "user_name": resolved_user,
            "session_id": data.get("session_id"),
            "organization": data.get("organization"),
            "repo_name": data.get("repo_name"),
            "project_name": data.get("project_name"),
            "branch": data.get("branch"),
            "scm_type": data.get("scm_type"),
            "vcs_type": data.get("vcs_type"),
            "remote_url": data.get("remote_url"),
        }
        key_tuple = tuple(dimension_values[f] for f in group_fields)

        bucket = buckets.get(key_tuple)
        if bucket is None:
            bucket = {
                "dims": {f: dimension_values[f] for f in group_fields},
                "turn_count": 0,
                "total_prompt_tokens": 0,
                "total_response_tokens": 0,
                "total_credits_used": 0.0,
                "first_seen_at": None,
                "last_seen_at": None,
            }
            buckets[key_tuple] = bucket

        prompt_tokens = data.get("prompt_tokens_estimated") or 0
        response_tokens = data.get("response_tokens_estimated") or 0
        credits_used = data.get("credits_used") or 0.0

        if data.get("type") == "turn_tokens":
            bucket["turn_count"] += 1
        bucket["total_prompt_tokens"] += prompt_tokens
        bucket["total_response_tokens"] += response_tokens
        bucket["total_credits_used"] += credits_used

        timestamp = data.get("timestamp")
        if timestamp:
            if bucket["first_seen_at"] is None or timestamp < bucket["first_seen_at"]:
                bucket["first_seen_at"] = timestamp
            if bucket["last_seen_at"] is None or timestamp > bucket["last_seen_at"]:
                bucket["last_seen_at"] = timestamp

            day = timestamp[:10]  # "YYYY-MM-DD" prefix of an ISO-8601 timestamp
            day_bucket = daily[day]
            if data.get("type") == "turn_tokens":
                day_bucket["turn_count"] += 1
            day_bucket["total_tokens"] += prompt_tokens + response_tokens
            day_bucket["total_credits_used"] += credits_used

        if len(detail_records) < record_limit:
            detail_records.append(TelemetryRecordOut(**data))

    aggregate: List[AnalyticsBucket] = []
    for key_tuple, bucket in buckets.items():
        group_key = "|".join(str(v) if v is not None else "—" for v in key_tuple)
        aggregate.append(
            AnalyticsBucket(
                group_key=group_key,
                user_name=bucket["dims"].get("user_name"),
                session_id=bucket["dims"].get("session_id"),
                organization=bucket["dims"].get("organization"),
                repo_name=bucket["dims"].get("repo_name"),
                project_name=bucket["dims"].get("project_name"),
                branch=bucket["dims"].get("branch"),
                scm_type=bucket["dims"].get("scm_type"),
                vcs_type=bucket["dims"].get("vcs_type"),
                remote_url=bucket["dims"].get("remote_url"),
                turn_count=bucket["turn_count"],
                total_prompt_tokens=bucket["total_prompt_tokens"],
                total_response_tokens=bucket["total_response_tokens"],
                total_tokens=bucket["total_prompt_tokens"] + bucket["total_response_tokens"],
                total_credits_used=bucket["total_credits_used"],
                first_seen_at=bucket["first_seen_at"],
                last_seen_at=bucket["last_seen_at"],
            )
        )
    # Largest credit consumers first -- the most actionable ordering for a
    # cost/usage review.
    aggregate.sort(key=lambda b: b.total_credits_used, reverse=True)

    daily_series = [
        AnalyticsDailyPoint(
            date=day,
            turn_count=totals["turn_count"],
            total_tokens=totals["total_tokens"],
            total_credits_used=totals["total_credits_used"],
        )
        for day, totals in sorted(daily.items())
    ]

    return TelemetryAnalyticsResponse(
        total_matching_records=total_matching,
        aggregate=aggregate,
        daily_series=daily_series,
        records=detail_records,
        filter_options=AnalyticsFilterOptions(
            user_names=sorted(seen_users),
            session_ids=sorted(seen_sessions),
            organizations=sorted(seen_organizations),
            repo_names=sorted(seen_repo_names),
            project_names=sorted(seen_project_names),
            branches=sorted(seen_branches),
            scm_types=sorted(seen_scm_types),
            vcs_types=sorted(seen_vcs_types),
        ),
    )


@router.delete("", response_model=TelemetryClearResponse, dependencies=[Depends(require_api_key)])
async def clear_telemetry(
    session_id: Optional[str] = Query(
        default=None,
        description="Clear telemetry records for this Kiro session id.",
    ),
    user_name: Optional[str] = Query(
        default=None,
        description=(
            "Clear telemetry records for every session reported by this "
            "user (resolved via heartbeat data -- see GET /api/health)."
        ),
    ),
    machine_name: Optional[str] = Query(
        default=None,
        description=(
            "Clear telemetry records for every session reported from this "
            "machine (resolved via heartbeat data -- see GET /api/health)."
        ),
    ),
) -> TelemetryClearResponse:
    """Clears telemetry (usage log) data -- token/credit history -- by
    session, user, or machine.

    Exactly one of session_id / user_name / machine_name must be
    provided, to avoid an accidental "clear everything" call with no
    filter at all. This mirrors DELETE /api/health, but removes usage
    log entries (POST /api/telemetry records) instead of heartbeat/
    liveness data; it does not touch anything in the health index.

    Since telemetry records only carry a session_id (not machine_name/
    user_name directly -- see TelemetryRecordIn in app/models.py),
    user_name and machine_name filters are resolved to a set of
    session_id(s) via the heartbeat data already reported to
    POST /api/health, then every telemetry record belonging to any of
    those sessions is removed.
    """
    provided = [f for f in (session_id, user_name, machine_name) if f]
    if len(provided) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide session_id, user_name, or machine_name to select which telemetry data to clear.",
        )
    if len(provided) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide only one of session_id, user_name, or machine_name, not more than one.",
        )

    if session_id:
        target_session_ids = {session_id}
    else:
        target_session_ids = _session_ids_for(machine_name=machine_name, user_name=user_name)
        if not target_session_ids:
            # No heartbeat has ever reported this machine/user, so there is
            # nothing to resolve to -- nothing to clear.
            return TelemetryClearResponse(cleared_count=0)

    # Walk the full telemetry index once, deleting any record whose
    # session_id is in the target set. There is no per-session secondary
    # index for telemetry records (see redis_client.py's data model
    # notes), so this is an O(n) scan over all currently-indexed record
    # ids -- acceptable for this proof-of-concept's expected volume.
    all_record_ids = redis_client.zrange(INDEX_KEY, 0, -1)
    cleared_count = 0

    for record_id in all_record_ids:
        raw = redis_client.get(_record_key(record_id))
        if raw is None:
            # Already expired (TTL) or missing; drop the dangling index entry.
            redis_client.zrem(INDEX_KEY, record_id)
            continue

        data = json.loads(raw)
        if data.get("session_id") not in target_session_ids:
            continue

        redis_client.delete(_record_key(record_id))
        redis_client.zrem(INDEX_KEY, record_id)
        cleared_count += 1

    return TelemetryClearResponse(cleared_count=cleared_count)
