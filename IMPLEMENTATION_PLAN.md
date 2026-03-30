## Implementation Plan & Backlog

### Phase 0 – Foundations
- **Agree on Tech Stack & Hosting**: Confirm language/runtime choices (Python agents, FastAPI backend, React front end), cloud region, and deployment model (Kubernetes vs managed services).
- **Define Data Contracts**: Draft JSON schemas for `SiteSnapshot`, `Baseline`, `RAGResult`, and agent registration payloads. Publish via OpenAPI.
- **Security Blueprint**: Decide on authn/authz flows (agent service tokens, OIDC for UI) and certificate handling.

### Phase 1 – Collector Agent Extraction
- [ ] Create new agent package (`wba_agent`) extracting `SiteConnection` logic sans Tk dependencies.
- [ ] Implement async-safe event bus (queue + callbacks) for logging, status, and telemetry emission.
- [ ] Add configuration loader (YAML/JSON) supporting per-site commands, intervals, baseline resets, debug flags.
- [ ] Provide local logging (structured JSON) and metrics endpoints (Prometheus scrape or statsd).
- [ ] Build packaging scripts (Dockerfile + systemd/Windows service templates).

### Phase 2 – Control Plane API
- [ ] Scaffold FastAPI project with core models and persistence layer (SQLAlchemy + Postgres/Timescale).
- [ ] Implement endpoints:
  - Agent registration & auth handshake.
  - Baseline submission & retrieval.
  - Snapshot ingestion (current state + diffs).
  - RAG status query (per site and global).
  - Baseline reset requests & command configuration updates.
- [ ] Add background worker (Celery/RQ) for RAG scoring, alert generation, and baseline versioning.
- [ ] Integrate object storage for large payload archival.
- [ ] Provide WebSocket/SSE for dashboard subscriptions.
- [ ] Instrument API for metrics/logging/tracing.

### Phase 3 – Global Dashboard UI
- [ ] Scaffold SPA (React + TypeScript) consuming the OpenAPI spec.
- [ ] Implement views:
  - Global grid/map showing per-site RAG and last update.
  - Site detail with component-level diff cards, command history, alerts.
  - Baseline management (view, approve reset, diff comparisons).
- [ ] Add real-time updates via WebSocket/SSE client.
- [ ] Integrate authentication (OIDC) and RBAC (global vs regional roles).
- [ ] Provide alert/notification center with filtering.

### Phase 4 – Ops, Observability, & QA
- [ ] Set up CI/CD pipelines (lint/test/build/deploy) for agent, API, and UI repos.
- [ ] Define infrastructure-as-code (Terraform/Helm) for control plane deployment.
- [ ] Implement centralized logging, metrics dashboards, and alerting thresholds.
- [ ] Create synthetic monitoring to ensure agent/API connectivity.
- [ ] Develop automated and manual test plans (unit, integration, load, failover drills).

### Phase 5 – Rollout & Iteration
- [ ] Pilot with 1–2 sites; validate baseline capture, RAG accuracy, dashboard UX.
- [ ] Gather feedback; refine diff rules, thresholds, UI ergonomics.
- [ ] Onboard remaining sites; monitor scaling behavior.
- [ ] Plan enhancements: SLA reporting, historical analytics, automation hooks (ticketing/ChatOps).

### Workstream Ownership (Suggested)
- **Platform Backend**: API + persistence + workers.
- **Agent Engineering**: Collector extraction, packaging, deployment tooling.
- **Frontend/UI**: Dashboard SPA, design system integration.
- **Infra/SRE**: CI/CD, hosting, observability, security posture.

### Milestones & Deliverables
1. **M1 – Agent MVP**: Headless agent runs against mock control plane, sends telemetry, supports baseline capture.
2. **M2 – API Core Ready**: Control plane ingests snapshots, stores baselines, exposes RAG endpoints.
3. **M3 – Dashboard Alpha**: Global view + site detail with live updates; integrates with API.
4. **M4 – End-to-End Beta**: Pilot sites live, authentication in place, observability wired, failover tested.
5. **M5 – Production Launch**: All 15 sites onboarded, SOPs written, support model defined.


