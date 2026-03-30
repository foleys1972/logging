## Global WBA Monitoring Platform Architecture

### Overview
- Provide a single global dashboard that shows the health of ~15 WBA sites using Red/Amber/Green (RAG) status.
- Collect site telemetry via lightweight agents that run close to each WBA deployment.
- Centralize processing, persistence, and visualization in a highly available control plane.

### Core Components
- **Collector Agent**
  - Maintains websocket sessions to the local WBA endpoint, reusing logic from the existing `SiteConnection` class.
  - Executes configured command cycles, manages baseline establishment, aggregates metrics, and evaluates per-component deltas.
  - Streams summarized health payloads (plus optional verbose logs) to the control plane over HTTPS/WebSocket.
  - Runs as a service/daemon on-site; packaged via Docker or system service with site-specific config and credentials.

- **Control Plane API**
  - REST + WebSocket endpoints (FastAPI or equivalent) to ingest agent payloads, manage baselines, expose site status, and handle dashboard subscriptions.
  - Orchestrates baseline persistence, diff evaluation, RAG scoring, and command/notification preferences per site.
  - Provides secure authentication (service tokens for agents, SSO/OIDC for operators).

- **Persistence Layer**
  - **PostgreSQL/TimescaleDB** for site metadata, baselines, snapshots, and RAG history.
  - **Object Storage (S3/Azure Blob, etc.)** for large command responses or archived logs beyond database-friendly size.
  - Optional cache (Redis) for quick lookup of latest site status and subscription sessions.

- **Global Dashboard UI**
  - SPA (React/Vue/Svelte) consuming the Control Plane API.
  - Landing page: map/grid display of all sites with RAG states, last update timestamp, and key metrics.
  - Detail views: component-level baselines vs current state, command histories, alerts, and controls for baseline resets or debug toggles.
  - Real-time updates via WebSocket/SSE.

- **Background Workers**
  - Celery/RQ workers handle intensive diff processing, alert enrichment, baseline re-computation, and scheduled report generation.

### Data Flow (Happy Path)
1. Agent connects to WBA endpoint, authenticates, establishes baseline on first successful command run.
2. Agent posts a `SiteSnapshot` payload to the Control Plane (summary metrics, component hashes/checksums, diff indicators, raw delta metadata).
3. Control Plane stores snapshot, runs RAG evaluation against stored baseline, persists result, and emits update events to connected dashboards.
4. Dashboard updates site tile color and detail panes in real time; operators can drill down or trigger actions (e.g., reset baseline).
5. Historical snapshots support trend analysis and reporting.

### Baseline & RAG Evaluation
- **Baseline Establishment**: first successful full command cycle; stored per component (zones, TPOs, users, etc.) with metadata (hash, counts, sample).
- **Delta Assessment**: compare incoming component snapshots to baseline using rule sets:
  - No change → `GREEN`.
  - Tolerable drift (counts within tolerance, non-critical metadata) → `AMBER` with notes.
  - Critical differences (missing zones, TPO state changes, auth failures) → `RED`.
- **Reset Workflow**: operators can request a baseline reset; Control Plane queues task for agent to capture new baseline in next cycle.

### Security & Operations
- Mutual TLS or signed tokens for agent-to-control plane communication.
- SSO/OIDC for dashboard login with role-based permissions (global admin, regional operator, read-only).
- Observability: metrics (Prometheus), logs (centralized logging), alerts for missed heartbeats or data ingestion failures.
- Configuration management: store site definitions and credentials securely (vault/secret manager); provide admin UI for updates.

### Deployment & Scaling
- Host Control Plane (API, DB, workers, dashboard) in a primary cloud region with multi-AZ redundancy.
- Containerize services; orchestrate with Kubernetes or managed container service.
- Agents run on-site infrastructure (VM, container, or bare metal) with automated updates.
- Designed for current 15 sites with room to scale to 30+ by horizontal scaling of API pods and provisioning larger DB tiers.


