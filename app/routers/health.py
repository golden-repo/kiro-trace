"""
routers/health.py
===================

Implements the two health endpoints:

  POST /api/health  (API #2) - the handler/watcher push a heartbeat here
  GET  /api/health  (API #4) - the dashboard reads liveness status from here

A "heartbeat" is a small message that says "component X, for session Y,
running as user Z on machine M, is still alive as of time T". The
telemetry-watcher.js script sends one of these roughly every 5 minutes
for as long as it runs; telemetry-handler.js sends one every time it
fires (once per completed prompt turn).

How liveness is determined
----------------------------
This API does NOT actively reach out and check whether a process is
still running (it has no way to do that across machines). Instead, it
uses the simple, standard heartbeat pattern: if we received a heartbeat
"recently enough" (see HEALTH_STALE_AFTER_SECONDS below), we consider
that component alive; otherwise we mark it as not alive (stale).
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.models import (
    HealthClearResponse,
    HealthHeartbeatIn,
    HealthIngestResponse,
    HealthListResponse,
    HealthRecordOut,
)
from app.redis_client import redis_client
from app.security import require_api_key

logger = logging.getLogger("kiro_trace.health")

router = APIRouter(prefix="/api/health", tags=["health"])

INDEX_KEY = "health:index"

# If a component's most recent heartbeat is older than this, the
# dashboard/API reports it as not-alive ("stale"). The watcher's own
# heartbeat interval defaults to 5 minutes (300s), so we allow a bit more
# than double that before calling it stale, to tolerate one missed beat
# without a false "dead" reading.
HEALTH_STALE_AFTER_SECONDS = 12 * 60  # 12 minutes


def _health_key(session_id: str, component: str) -> str:
    # session_id may be the literal string "none" when a watcher sends a
    # heartbeat before observing any session activity yet (see
    # telemetry-watcher.js, which uses `null` -> represented as "none"
    # here since Redis hash keys must be plain strings).
    safe_session_id = session_id or "none"
    return f"health:{safe_session_id}:{component}"


@router.post("", response_model=HealthIngestResponse, dependencies=[Depends(require_api_key)])
async def ingest_heartbeat(heartbeat: HealthHeartbeatIn) -> HealthIngestResponse:
    """API #2: receives one heartbeat from telemetry-handler.js or
    telemetry-watcher.js and records it as the LATEST known status for
    that (session_id, component) pair, overwriting any previous one.

    We deliberately do not keep heartbeat history -- only "when did we
    last hear from this component" -- since that's all that's needed to
    answer "is it currently alive".
    """
    key = _health_key(heartbeat.session_id, heartbeat.component)
    received_at = datetime.now(timezone.utc).isoformat()

    payload = heartbeat.dict()
    payload["received_at"] = received_at

    # Log the exact payload as received so you can see precisely what the
    # handler/watcher sent -- including whether source-control fields
    # (organization/repo_name/project_name/branch/scm_type/vcs_type/
    # remote_url) were present at all. Remove or lower to DEBUG once
    # confirmed working.
    logger.info(
        "POST /api/health payload received (component=%s, session_id=%s): %s",
        heartbeat.component,
        heartbeat.session_id,
        json.dumps(heartbeat.dict(), sort_keys=True),
    )

    redis_client.set(key, json.dumps(payload))
    redis_client.sadd(INDEX_KEY, key)

    return HealthIngestResponse()


@router.get("", response_model=HealthListResponse, dependencies=[Depends(require_api_key)])
async def list_health() -> HealthListResponse:
    """API #4: returns the latest known heartbeat for every
    (session_id, component) pair this server has ever seen, along with
    a computed `is_alive` flag, for the dashboard to render.

    Because multiple Kiro CLI sessions (possibly on different machines)
    may be running at once, this always returns ALL of them -- the
    dashboard is responsible for grouping/displaying them per session.
    """
    keys = redis_client.smembers(INDEX_KEY)
    now = datetime.now(timezone.utc)

    records: List[HealthRecordOut] = []
    for key in keys:
        raw = redis_client.get(key)
        if raw is None:
            # Shouldn't normally happen (health keys have no TTL), but
            # guard against it and clean up the stale index entry.
            redis_client.srem(INDEX_KEY, key)
            continue

        data = json.loads(raw)
        last_heartbeat_at = data.get("received_at") or data.get("timestamp")

        try:
            last_seen = datetime.fromisoformat(last_heartbeat_at)
            # fromisoformat gives a naive or aware datetime depending on
            # the input string; normalize to UTC-aware for subtraction.
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            seconds_since = (now - last_seen).total_seconds()
        except (TypeError, ValueError):
            # If we can't parse the timestamp for any reason, treat the
            # entry as maximally stale rather than crashing the endpoint.
            seconds_since = float("inf")

        records.append(
            HealthRecordOut(
                session_id=data.get("session_id"),
                component=data.get("component", "unknown"),
                machine_name=data.get("machine_name", "unknown"),
                user_name=data.get("user_name", "unknown"),
                pid=data.get("pid"),
                last_heartbeat_at=last_heartbeat_at or "unknown",
                seconds_since_last_heartbeat=seconds_since,
                is_alive=seconds_since <= HEALTH_STALE_AFTER_SECONDS,
                project_name=data.get("project_name"),
                repo_name=data.get("repo_name"),
                organization=data.get("organization"),
                branch=data.get("branch"),
                scm_type=data.get("scm_type"),
                vcs_type=data.get("vcs_type"),
                remote_url=data.get("remote_url"),
            )
        )

    # Show the most recently active components first.
    records.sort(key=lambda r: r.seconds_since_last_heartbeat)

    return HealthListResponse(total_count=len(records), records=records)


@router.delete("", response_model=HealthClearResponse, dependencies=[Depends(require_api_key)])
async def clear_health(
    session_id: Optional[str] = Query(
        default=None,
        description=(
            "Clear heartbeat data for this Kiro session id. Pass the "
            "literal string 'none' to clear heartbeats recorded with no "
            "session id (a watcher heartbeat sent before any session "
            "activity was observed)."
        ),
    ),
    user_name: Optional[str] = Query(
        default=None,
        description="Clear heartbeat data for every session/component reported by this user.",
    ),
    component: Optional[str] = Query(
        default=None,
        description='Restrict the clear to one component ("handler" or "watcher"). '
        "If omitted, all components matching the other filter(s) are cleared.",
    ),
) -> HealthClearResponse:
    """Clears heartbeat ("still alive") data.

    Supports two mutually exclusive filters (used by the dashboard's
    "Clear" button):
      - session_id: remove heartbeat entries for one specific session
      - user_name:  remove heartbeat entries for every session belonging
                     to one user (useful for cleaning up after a user
                     whose sessions have all ended)

    Exactly one of session_id / user_name must be provided, to avoid an
    accidental "clear everything" call with no filter at all. `component`
    can be combined with either filter to only clear one row (e.g. just
    the "watcher" entry) instead of both handler+watcher for that
    session/user.
    """
    if not session_id and not user_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide session_id or user_name to select which heartbeat data to clear.",
        )
    if session_id and user_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide only one of session_id or user_name, not both.",
        )

    keys = redis_client.smembers(INDEX_KEY)
    keys_to_clear = []

    for key in keys:
        if component and not key.endswith(f":{component}"):
            continue

        if session_id:
            safe_session_id = session_id or "none"
            if key.startswith(f"health:{safe_session_id}:"):
                keys_to_clear.append(key)
            continue

        # user_name filter requires reading the record to check its
        # user_name field, since it isn't part of the Redis key itself.
        raw = redis_client.get(key)
        if raw is None:
            continue
        data = json.loads(raw)
        if data.get("user_name") == user_name:
            keys_to_clear.append(key)

    cleared_count = 0
    for key in keys_to_clear:
        if redis_client.delete(key):
            cleared_count += 1
        redis_client.srem(INDEX_KEY, key)

    return HealthClearResponse(cleared_count=cleared_count)
