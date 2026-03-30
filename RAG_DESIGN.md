## RAG Evaluation & Baseline Diff Strategy

### Goals
- Provide deterministic, explainable Red/Amber/Green scoring per site and component.
- Share logic between collector agents (local pre-checks) and the control plane (authoritative scoring & persistence).
- Support baseline establishment, change detection, tolerances, and operator overrides.

### Key Concepts
- **Component Snapshot**: Structured payload summarizing the most recent command result (counts, hashes, critical flags).
- **Baseline**: Stored snapshot marked as the reference standard. Versioned per component with metadata (timestamp, operator, hash).
- **Diff Result**: Comparison output capturing added/removed/changed entities along with severity hints.
- **RAG Status**:
  - `GREEN`: No significant deviations from baseline.
  - `AMBER`: Non-critical changes, tolerable drift, or partial data (e.g., truncated response).
  - `RED`: Critical deviations (missing zones, TPO state changes, auth failures, command errors).

### Component Coverage
- `zones`: Compare IDs and critical attributes. Missing/additional zones → `RED`. Metadata-only changes (label, description) → `AMBER` unless flagged critical.
- `tpos`: Evaluate alive/state flags, missing TPOs, or newly added TPOs. State change → `RED`. Count drift with all alive → `AMBER`.
- `turrets`, `users`, `lines`, `shared_profiles`: Focus on counts and critical attribute hashes. Large count delta (> configurable %) → `AMBER`/`RED` depending on severity.
- `calls`, `events`: Historical/batched data primarily for insights; RAG typically unaffected unless ingestion fails repeatedly.
- `health`, `version`: Hash comparison for version drift; major version mismatch → `AMBER`, known incompatible versions → `RED`.

### Evaluation Flow
1. **Baseline Establishment**
   - First successful snapshot sets baseline for all components.
   - Store component hash (e.g., SHA256 of normalized data) plus key metrics (counts, alive states).
   - Mark baseline version; allow manual resets triggered via control plane.

2. **Snapshot Normalization**
   - Agents normalize raw command responses into component snapshots (counts, sets, state summaries, hashes).
   - Include metadata: timestamp, command duration, truncated flag, partial data indicator.

3. **Diff & Scoring**
   - Control plane receives snapshot, loads baseline, runs diff per component.
   - Diff outputs severity (GREEN/AMBER/RED) plus structured reasons (list of `DiffItem`).
   - Aggregate severity: highest component severity defines site RAG. Also compute component-level scores for drilldown.

4. **Persistence & Events**
   - Store snapshot, diff, and final RAG result.
   - Emit events to dashboard subscribers with summary (site, overall RAG, top reasons).

### Thresholds & Rules
- Maintain configuration table (per component) containing:
  - Critical fields list (changes imply RED).
  - Tolerances (% count delta for AMBER vs RED).
  - Known benign versions/metadata fields to ignore.
  - Optional per-site overrides.
- Support dynamic adjustments without redeploying agents (control plane dictates latest rules).

### Agent vs Control Plane Responsibilities
- **Agent**
  - Perform lightweight diff to catch obvious RED conditions early (e.g., auth failure, command error) and include metadata in telemetry.
  - Do not persist baselines locally; rely on control plane for authoritative versioning.
  - Include raw data digests (hashes, sample IDs) so control plane can run the full diff without needing entire payload if unchanged.

- **Control Plane**
  - Maintain baseline history, manage resets, and execute complete diff logic.
  - Serve rule set to agents (e.g., via configuration endpoint) so both sides share thresholds.
  - Surface diff context and remediation hints on the dashboard.

### Override & Reset Workflow
- Operators can acknowledge AMBER/RED findings and optionally promote the current snapshot to a new baseline.
- Retain previous baseline for audit with reason codes.
- Provide REST endpoints to request resets; agents receive instructions on next poll or via WebSocket command.

### Error Handling
- If a command fails or snapshot is incomplete → mark component RED with reason (e.g., timeout, partial data).
- If baseline missing (new site or component) → site `AMBER` with message "Baseline pending" until established.
- Stale data (no snapshot > threshold) → `RED` for site-level health.

### Data Structures (Shared Schema Sketch)
- `ComponentSnapshot`
  - `component`: string
  - `hash`: str (normalized data hash)
  - `count`: int
  - `metrics`: dict (component-specific metrics)
  - `flags`: dict (e.g., `partial`, `truncated`)

- `Baseline`
  - `component`: string
  - `hash`: str
  - `count`: int
  - `metrics`: dict
  - `version`: int
  - `created_at`: timestamp

- `DiffResult`
  - `component`: string
  - `status`: `GREEN|AMBER|RED`
  - `reasons`: list of structured items (code, message, metadata)

### Implementation Notes
- Build reusable diff helpers per component (compare sets, alive/status fields, metadata hash).
- Use deterministic ordering and normalization before hashing (sort lists, remove non-critical fields).
- Provide library (`rag.py`) with evaluation functions usable by both agent and control plane.
- Extensive unit tests with fixture baselines to avoid regressions.


