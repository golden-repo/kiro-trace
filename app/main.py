"""
main.py
=======

Entry point of the FastAPI application. This is the file `uvicorn` runs.

What this file does:
  1. Creates the FastAPI app object.
  2. Registers the telemetry and health routers (the actual /api/* logic
     lives in app/routers/telemetry.py and app/routers/health.py).
  3. Serves the static dashboard (an HTML/CSS/JS page) at "/".
  4. Adds a simple, UNauthenticated "/healthz" endpoint for container /
     Kubernetes liveness and readiness probes (separate from the
     authenticated /api/health API described in the project requirements
     -- this one is just "is the web server itself up", used by
     Docker/Kubernetes, not by the Kiro CLI handler/watcher scripts).

How to run this locally without Docker (for reference; see README.md for
the full walkthrough):

    pip install -r requirements.txt
    cp .env.development .env.development.local   # optional, or edit directly
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Configuration is loaded from a .env file by app/config.py (via
python-dotenv): .env.development is used by default, .env is used when
ENVIRONMENT=production. See app/config.py and README.md for details.
"""

from pathlib import Path
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.routers import health, telemetry

# Make sure our own loggers (app.routers.telemetry / app.routers.health)
# actually reach stdout, at INFO level, so the payload-received log lines
# added there (used to verify exactly what the Kiro CLI handler/watcher
# hooks are sending -- e.g. whether repo_remote_url/branch_name/
# repo_project_name/feature_name/scm_provider are present) are visible in
# `docker logs` / `kubectl logs` / your terminal, without needing extra
# uvicorn flags. This only sets a level/handler if the root logger has
# none configured yet, so it won't fight with any logging setup you add
# later (e.g. via uvicorn's --log-config).
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

app = FastAPI(
    title="kiro-trace",
    description=(
        "Central traceability & health dashboard for Kiro CLI telemetry "
        "hooks (telemetry-handler.js / telemetry-watcher.js)."
    ),
    version="1.0.0",
)

# Mount the two routers. Each router file defines its own URL prefix
# (/api/telemetry and /api/health respectively), so we don't repeat it here.
app.include_router(telemetry.router)
app.include_router(health.router)

# The dashboard's HTML/CSS/JS files live in the top-level "static" folder
# (one level above this "app" package). We serve them under /static/...
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def dashboard_root() -> FileResponse:
    """Serves the dashboard's main HTML page at the site root ("/"),
    so visiting http://<host>:8000/ opens the dashboard directly instead
    of requiring visitors to know the /static/dashboard.html path.
    """
    return FileResponse(str(STATIC_DIR / "dashboard.html"))


@app.get("/healthz", include_in_schema=False)
async def container_health_check() -> dict:
    """A minimal, unauthenticated endpoint for Docker/Kubernetes health
    probes to hit, to check "is this web server process up and
    responding". This is intentionally separate from the authenticated
    GET /api/health endpoint, which reports on the *Kiro CLI* handler and
    watcher scripts' liveness, not on this web server itself.
    """
    return {"status": "ok"}
