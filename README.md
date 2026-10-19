# kiro-trace — AI-Powered Token & Cost Analytics for Kiro CLI

> **Built by Himadri Majumdar** · [himadri.majumdar@ericsson.com](mailto:himadri.majumdar@ericsson.com)
> Contributor in the AI Engineering space. Excited to explore the advancement happening in this area.

---

## What is kiro-trace?

**kiro-trace** is a production-quality, self-hosted observability service that captures real-time **token consumption** and **credit usage** from the [Kiro CLI](https://kiro.dev) AI coding assistant — and turns that raw telemetry into actionable fleet-wide analytics on a live web dashboard.

Think of it as a lightweight **FinOps control plane for AI-assisted development**: every prompt turn generates token estimates and real credit costs; kiro-trace collects all of it, stores it in Redis, and lets engineering managers and platform teams slice it by **user, session, repository, branch, organization, and SCM platform** — answering questions like:

- _"Which team / repository is consuming the most AI credits this week?"_
- _"Is my Kiro CLI handler still alive on my build server right now?"_
- _"How do token costs trend by day across our Azure DevOps monorepo?"_

The project is fully containerised (Docker + Kubernetes manifests included), requires zero external dependencies beyond Redis, and works entirely **offline** inside a locked-down enterprise network.

---

## The Business Problem: Tokenomics in the FinOps Space

As organisations adopt AI coding assistants at scale, **token consumption becomes a direct cost line** — yet most teams have near-zero visibility into _who is spending what, on which project, and when_.

This is the **Tokenomics gap in FinOps**:

| Traditional Cloud FinOps                               | AI/LLM Tokenomics (the gap)                                  |
| ------------------------------------------------------ | ------------------------------------------------------------ |
| VM/container cost attributed to team via resource tags | Token cost attributed to... the whole organisation's API key |
| Cost anomalies detected per workload                   | Token spikes invisible until the monthly invoice             |
| Show-back / charge-back per business unit              | No attribution below the account level                       |
| Idle resource detection                                | No signal on whether a paid AI session is even still running |

**kiro-trace closes this gap** for Kiro CLI users by:

1. **Attributing every AI turn to its source-control context** — organization → repository → branch — so token cost is traceable back to the exact piece of work that generated it.
2. **Providing liveness monitoring** ("heartbeats") so platform teams know whether a billed CLI session is actually active or silently dead.
3. **Surfacing daily trend charts and per-repository credit breakdowns** so FinOps and engineering leads can spot anomalies, enforce budgets, and do show-back reporting without waiting for a vendor invoice.
4. **Running entirely on-premises** — sensitive prompt data never leaves the corporate network.

This directly maps to the emerging **LLMOps / AI FinOps** discipline: treating LLM token budgets with the same rigour as cloud compute budgets.

---

## How I Built It

### Approach

I built kiro-trace as a **vertical slice from data ingestion to live dashboard**, keeping each layer thin and composable. The guiding principle was: _ship something a real engineering manager can open in a browser on day one, with zero frontend build tooling required_.

### Technology choices

| Layer      | Technology                                 | Why                                                                                                                     |
| ---------- | ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| API server | **FastAPI** (Python)                       | Auto-generates OpenAPI docs; async-native; Pydantic models give free validation                                         |
| Storage    | **Redis**                                  | Sub-millisecond reads for dashboard refresh; built-in TTL for automatic data expiry; trivially deployable in Kubernetes |
| Frontend   | **Vanilla HTML / CSS / JS** (no framework) | Works fully offline; zero npm install; zero CDN dependency — enterprise-network friendly                                |
| Charts     | **Inline SVG** (hand-drawn via JS)         | No Chart.js / D3 CDN calls; dashboard stays functional in air-gapped environments                                       |
| Container  | **Docker + Kubernetes manifests**          | One-command deploy; secrets via K8s `Secret`; ConfigMap for tunable settings                                            |
| Auth       | **X-API-Key header**                       | Simple, stateless; browser stores it in `localStorage` and attaches on every fetch                                      |

### The AI-assisted development angle

I used **Kiro CLI with the Kiro Coding Agent harness** throughout the build. This was intentional — kiro-trace _tracks the very tool that built it_, which validated the end-to-end telemetry pipeline in real time as I worked. Concretely:

- The Kiro Coding Agent helped scaffold the FastAPI router structure, Pydantic models, and Redis data model.
- I used it to iterate quickly on the dashboard JS (filter logic, SVG chart generation, collapsible UI panels) while keeping manual control over the architecture decisions.
- Every agent turn was captured by kiro-trace itself, giving me live feedback on token costs and session liveness as the project grew.

This is a practical demonstration of **AI-augmented engineering**: using an AI coding agent to build a system that observes AI coding agents — closing the loop between tooling and observability.

---

## Project Structure & File Purposes

### Source files

| Path                       | Type       | Purpose                                                                                                                                      |
| -------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/main.py`              | Python     | FastAPI application entry point; wires routers, static files, and the `/healthz` probe                                                       |
| `app/config.py`            | Python     | Reads all runtime settings from environment variables (with dotenv fallback); single source of truth for configuration                       |
| `app/security.py`          | Python     | `X-API-Key` header dependency injected into every protected endpoint                                                                         |
| `app/redis_client.py`      | Python     | Redis connection factory; documents the data model (key naming, TTL strategy, index sets)                                                    |
| `app/models.py`            | Python     | All Pydantic request/response schemas — `TelemetryRecordIn`, `HealthHeartbeatIn`, analytics response shapes, etc.                            |
| `app/routers/telemetry.py` | Python     | `POST /api/telemetry`, `GET /api/telemetry`, `GET /api/telemetry/analytics`, `DELETE /api/telemetry`                                         |
| `app/routers/health.py`    | Python     | `POST /api/health`, `GET /api/health`, `DELETE /api/health`; heartbeat ingestion, liveness calculation, selective clear                      |
| `static/dashboard.html`    | HTML       | Single-page dashboard: auth card, fleet summary strip, Usage Analytics section (collapsible filter bar + tabs), Fleet Health section, modals |
| `static/dashboard.css`     | CSS        | Full dark-theme design system; responsive grid; no external stylesheet dependency                                                            |
| `static/dashboard.js`      | JavaScript | All dashboard logic: API calls, filter state, aggregate table, SVG chart rendering, fleet health tree, auto-refresh, modals                  |
| `requirements.txt`         | Config     | Pinned Python dependency versions (`fastapi`, `uvicorn`, `redis`, `pydantic`, `python-dotenv`, `tiktoken`)                                   |
| `Dockerfile`               | Config     | Production container image; copies only app source; `ENVIRONMENT=production` at runtime                                                      |
| `.env.example`             | Config     | Documents every environment variable the app understands; safe to commit (no real secrets)                                                   |
| `Developer.README.md`      | Docs       | Full technical reference: local setup, Docker, Kubernetes manifests, API details, configuration guide                                        |

### REST API surface

| #   | Method   | Path                       | Purpose                                                                             |
| --- | -------- | -------------------------- | ----------------------------------------------------------------------------------- |
| 1   | `POST`   | `/api/telemetry`           | Ingest one token/credit usage event from the Kiro CLI handler or watcher            |
| 2   | `POST`   | `/api/health`              | Ingest a "still alive" heartbeat from the handler or watcher                        |
| 3   | `GET`    | `/api/telemetry`           | Read recent telemetry records (optionally filtered)                                 |
| 4   | `GET`    | `/api/telemetry/analytics` | Aggregated totals + daily series + filter options for the Usage Analytics dashboard |
| 5   | `GET`    | `/api/health`              | Read liveness status of every known handler/watcher session                         |
| 6   | `DELETE` | `/api/health`              | Clear heartbeat data by session or user                                             |
| 7   | `DELETE` | `/api/telemetry`           | Clear usage records by session, user, or machine                                    |

---

## Key Engineering Highlights

- **Source-control attribution** — every telemetry event and heartbeat carries `organization`, `repo_name`, `branch`, `scm_type`, `vcs_type`, and `remote_url`, parsed from the Git remote in the working directory at the time of the event. This makes per-repository cost attribution possible without any manual tagging.
- **Backward-compatible schema** — Pydantic's `extra = "allow"` policy means older handler/watcher versions that don't send the new SCM fields keep working with zero changes.
- **Analytics in one call** — `GET /api/telemetry/analytics` returns computed aggregates, individual records, a daily time series, _and_ the set of valid filter values for the dropdowns, all in a single HTTP round trip.
- **Collapsible filter UI** — the dashboard's filter bar collapses by default and expands on demand, keeping the screen clean for managers who just want the top-level numbers.
- **No build step** — the entire frontend is plain HTML/CSS/JS served directly by FastAPI's `StaticFiles` mount; there is no webpack, vite, or npm involved. This means the dashboard works instantly in any browser, including in air-gapped enterprise environments.
- **Auto-expiring data** — Redis TTL (`TELEMETRY_TTL_SECONDS`, default 7 days) means the store self-cleans without any cron job or manual maintenance.

---

## Quick Start (Local)

```bash
# 1. Clone and enter the directory
cd kiro-trace

# 2. Create a virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS

pip install -r requirements.txt

# 3. Configure (edit .env.development with your Redis details and a chosen API key)

# 4. Run
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000/** → paste your API key → data appears as soon as the Kiro CLI handler/watcher starts pushing events.

> Full deployment instructions (Docker, Kubernetes, connecting the Kiro CLI scripts, all configuration options) are in [Developer.README.md](./Developer.README.md).

---

## About Me

I'm an Enterprise & AI Solution Architect with hands-on experience in **platform engineering, cloud-native architecture, AI tooling, and developer experience**. kiro-trace is a concrete example of how I approach problems: identify a real operational gap (AI cost visibility), design a minimal but production-quality solution, ship it end-to-end, and validate it with the tools that built it.

📧 [majumdar.himadri@gmail.com](mailto:majumdar.himadri@gmail.com)
🔗 [LinkedIn](https://www.linkedin.com/in/himadri-majumdar)
