"""
security.py
============

Implements the simple API-key authentication used to protect every
/api/* endpoint in this project.

How it works
------------
Every request must include an HTTP header:

    X-API-Key: <the secret value configured via the API_KEY env var>

FastAPI lets us express "this endpoint requires X" as a *dependency*: a
small function that runs before the endpoint's own code, and can reject
the request early. `require_api_key` below is that dependency. Any route
that should be protected simply adds:

    @router.get("/something", dependencies=[Depends(require_api_key)])

...and FastAPI takes care of calling it automatically on every request
to that route.

Why not a more complex auth scheme (OAuth, JWT, etc.)?
--------------------------------------------------------
This is a small, single-purpose internal telemetry service, not a
public-facing product. A shared API key is a lightweight, well-understood
way to make sure random/unauthenticated clients on the network can't read
or write telemetry data (which may include captured prompt/response text
if the user has enabled that). It is NOT a substitute for network-level
protections (e.g. running this behind a private VPC / cluster-internal
Service, or a proper API gateway) if you deploy this somewhere with
broader network access -- treat API_KEY as a baseline, not a complete
security solution.
"""

from fastapi import Header, HTTPException, status

from app.config import settings


async def require_api_key(x_api_key: str = Header(default=None)) -> None:
    """FastAPI dependency that enforces the X-API-Key header.

    - If the server itself has no API_KEY configured (empty string), we
      refuse ALL requests with a clear error, rather than silently
      running unauthenticated. This is intentional: an empty/default
      secret is a common source of accidental exposure.
    - If the client didn't send the header, or sent the wrong value,
      we reject with HTTP 401 Unauthorized.
    """
    if not settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Server misconfiguration: API_KEY environment variable is "
                "not set. Refusing to serve requests until an API key is "
                "configured. See .env.example."
            ),
        )

    if not x_api_key or x_api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header.",
        )
