"""
config.py
=========

Central place where ALL configuration values for this application are
read from environment variables.

Why environment variables instead of a settings file?
-------------------------------------------------------
This app is designed to run the same way whether you start it directly
with `uvicorn`, inside a plain `docker run` container, or as a Kubernetes
Pod. In all three cases, the standard/portable way to pass configuration
in is environment variables:

  - Local dev:  export REDIS_HOST=localhost  (or a `.env` file, see below)
  - Docker:     docker run -e REDIS_HOST=redis ...
  - Kubernetes: env / envFrom in the Pod spec, backed by a ConfigMap/Secret

Nothing in this file hardcodes a real secret. Every secret-like value
(REDIS_PASSWORD, API_KEY) has an empty-string default and the app will
refuse to use an empty API key (see security.py) -- you MUST set these
yourself via the environment. See `.env.example` in the project root for
a documented list of every variable this app understands.

If you are not familiar with Python: the `os.environ.get(NAME, default)`
calls below simply read an environment variable named NAME, and use
`default` if that variable was never set. `int(...)` / `.lower() == "true"`
convert the text value (env vars are always strings) into the actual
Python type we want to use (a number, a boolean, etc).

Loading values from a .env file
--------------------------------
Real environment variables (e.g. set via `export`, `docker run -e`, or a
Kubernetes Pod's `env:`) always take priority. On top of that, this app
also loads a `.env`-style file using `python-dotenv`, which is convenient
so you don't have to `export` every variable by hand during local
development.

Which file gets loaded is controlled by the `ENVIRONMENT` variable:

  - ENVIRONMENT=development (or unset, the default) -> loads `.env.development`
  - ENVIRONMENT=production                           -> loads `.env`

`load_dotenv()` only *fills in* variables that are not already present in
the process environment -- it never overrides a real `export`ed value or
one passed via `docker run -e` / Kubernetes `env:`. This means the same
image/code works unmodified in every environment: locally it picks up
`.env.development`, and in Docker/Kubernetes the real environment
variables you pass in take precedence and the .env files (if not even
present in the image) are simply skipped.
"""

import os

from dotenv import load_dotenv

# Decide which .env file to load based on ENVIRONMENT (defaults to
# "development" so `uvicorn app.main:app --reload` "just works" without
# any extra flags during local development).
_ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").strip().lower()
_ENV_FILE = ".env" if _ENVIRONMENT == "production" else ".env.development"

# override=False (the default) means real environment variables already
# set in the process always win over values from the file.
load_dotenv(dotenv_path=_ENV_FILE, override=False)


def _get_bool(name: str, default: bool) -> bool:
    """Reads an environment variable and interprets it as True/False.

    Environment variables are always strings, so "true", "True", "1" all
    need to be treated as boolean True. Anything else is treated as False,
    unless the variable was not set at all, in which case `default` is used.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """Groups every configurable value the app needs in one object.

    An instance of this class is created once, below (see `settings = Settings()`),
    and imported by every other module that needs configuration
    (e.g. `from app.config import settings`).
    """

    # ------------------------------------------------------------------
    # Redis connection settings
    # ------------------------------------------------------------------
    # These match the Redis instance described by the user: it runs in a
    # "redis" Kubernetes namespace and is reachable at localhost:6379 from
    # the user's machine. When this app itself runs inside a container/Pod,
    # REDIS_HOST should instead be set to whatever hostname/service reaches
    # that Redis from inside the cluster (e.g. "redis.redis.svc.cluster.local").
    REDIS_HOST: str = os.environ.get("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.environ.get("REDIS_PORT", "6379"))
    REDIS_USERNAME: str = os.environ.get("REDIS_USERNAME", "default")
    REDIS_PASSWORD: str = os.environ.get("REDIS_PASSWORD", "")
    REDIS_DB: int = int(os.environ.get("REDIS_DB", "0"))

    # Use TLS to connect to Redis (rediss://). Off by default since the
    # described setup is a plain local/in-cluster Redis without TLS.
    REDIS_USE_TLS: bool = _get_bool("REDIS_USE_TLS", False)

    # How long (in seconds) telemetry entries are kept in Redis before
    # they automatically expire. 7 days = 7 * 24 * 60 * 60 seconds.
    TELEMETRY_TTL_SECONDS: int = int(os.environ.get("TELEMETRY_TTL_SECONDS", str(7 * 24 * 60 * 60)))

    # ------------------------------------------------------------------
    # API security
    # ------------------------------------------------------------------
    # Every request to any /api/* endpoint must include a header:
    #   X-API-Key: <this value>
    # Requests without a matching key are rejected with HTTP 401.
    # There is NO default here on purpose -- if you forget to set this,
    # the app will refuse to start serving requests (see security.py),
    # rather than silently running unauthenticated.
    API_KEY: str = os.environ.get("API_KEY", "")

    # ------------------------------------------------------------------
    # Web server settings
    # ------------------------------------------------------------------
    HOST: str = os.environ.get("APP_HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("APP_PORT", "8000"))

    # Set to true for verbose logging while developing locally.
    DEBUG: bool = _get_bool("DEBUG", False)

    # Which environment this process considers itself to be running in
    # ("development" or "production"). Also determines which .env file
    # was loaded above (.env.development vs .env).
    ENVIRONMENT: str = _ENVIRONMENT


# A single shared Settings instance used everywhere else in the app.
settings = Settings()
