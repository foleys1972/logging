## WBA Logger Code Audit

### Candidate Components for Reuse
- **`SiteConnection`**
  - Mature async workflow for websocket auth, subscription management, command execution, multi-batch assembly, baseline comparison, and retry logic.
  - Baseline helpers (`_compare_zones`, `_compare_tpos`) provide initial delta detection rules to adapt for the central baseline engine.
  - Requires decoupling from Tkinter callbacks (`_log_to_gui`, `update_site_status`) and replacing with event hooks/queues suitable for the headless collector agent.

- **`LogRotator`**
  - Provides rotation/retention logic that can be repurposed for agent-side local logging until centralized shipping is in place.
  - Consider exposing metrics about rollover events to central monitoring.

- **Command/Subscription Definitions**
  - `AVAILABLE_COMMANDS`, `DEFAULT_COMMANDS`, and per-site configuration patterns form the foundation for agent config schemas.
  - Baseline establishment logic (first successful `get_zones`/`get_tpos`) aligns with the planned baseline workflow.

### Elements to Refactor or Replace
- **Tkinter Application (`WBALoggerApp`, `SiteDialog`)**
  - GUI orchestration is being retired in favor of the web control plane; repurpose only the configuration concepts (intervals, debug mode, auto-start, per-site commands).

- **Thread-GUI Coupling**
  - Current design mutates Tk widgets from the async event loop thread, which is unsafe. During the agent extraction, replace GUI calls with async-safe logging and outbound event publishing.

- **Local-only Persistence**
  - JSON config + file-based logs suffice for the desktop app but fall short for distributed monitoring. Introduce robust config management (central registry, secrets) and structured telemetry payloads destined for the control plane.

### Gaps to Address for the New Platform
- Central API contracts for baseline snapshots, RAG scoring, and historical storage.
- Secure agent registration, token provisioning, and certificate handling.
- Observability hooks (metrics, health endpoints) to integrate with the broader platform.
- Packaging/deployment scripts to distribute the agent as a service (Docker/systemd/Windows service).


