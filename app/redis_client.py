"""
redis_client.py
================

Sets up a single, shared connection to Redis that the rest of the app
reuses. Redis is where all telemetry and health data actually lives --
this app itself does not use a traditional SQL database.

Data model (how we use Redis)
------------------------------
Redis is a key-value store with a few useful data structures built in.
We use three of them here:

1. Sorted Set "telemetry:index"
   Holds the ID of every telemetry record ever ingested, scored by the
   timestamp it arrived (as a Unix epoch number). A sorted set lets us
   efficiently ask "give me the most recent N records" or "give me
   records between time A and time B" without scanning everything.

2. Hash "telemetry:record:<id>"
   Holds the actual telemetry record (the same JSON structure the
   telemetry-handler.js / telemetry-watcher.js scripts already write to
   their local telemetry.log file) as a single JSON string, stored under
   the field "data". One hash per record, so we can attach a TTL
   (auto-expiry) to each one individually.

3. Hash "health:<session_id>:<component>"
   Holds the latest heartbeat for one (session, component) pair, e.g.
   ("sess_abc123", "watcher"). Each new heartbeat OVERWRITES the previous
   one for that same session+component -- we only care about the most
   recent "is it alive" signal, not a full history of heartbeats.

4. Set "health:index"
   Holds the key name of every health hash we've ever seen, so the GET
   /api/health endpoint can enumerate "every session/component we know
   about" without using Redis's KEYS command (which is discouraged in
   production Redis because it can be slow on large databases).

Why not just use Redis lists for everything?
---------------------------------------------
Lists don't support efficient time-range queries or per-item TTLs, and
Redis's SCAN/KEYS commands are unsuited for interactive dashboard reads.
Sorted sets + per-record hashes are the standard idiomatic pattern for
this feed/index kind of data in Redis.
"""

import redis

from app.config import settings


def create_redis_connection() -> redis.Redis:
    """Creates and returns a Redis client using values from Settings.

    `decode_responses=True` makes the client return normal Python `str`
    objects instead of raw `bytes` -- much easier to work with, since we
    are only ever storing/reading JSON text and simple strings.
    """
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        username=settings.REDIS_USERNAME or None,
        password=settings.REDIS_PASSWORD or None,
        db=settings.REDIS_DB,
        ssl=settings.REDIS_USE_TLS,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


# A single shared Redis client, created once when this module is first
# imported, and reused by every request the API handles. The redis-py
# library manages an internal connection pool, so sharing one client
# object across requests is the normal, recommended usage pattern.
redis_client = create_redis_connection()
