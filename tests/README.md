# Testing Strategy

- **Agent**: use pytest + pytest-asyncio to validate config loader defaults, event bus dispatch, and `SiteConnection` behaviors (mocking websockets with `pytest-mock`/`asynctest`).
- **Control Plane**: use pytest + httpx `AsyncClient` against FastAPI app factory, with an in-memory SQLite database for migrations and API contract tests.
- **Dashboard**: use Vitest + React Testing Library for component rendering, query states, and interaction flows (tile selection, detail panel).

Run all suites with a top-level `pytest` + `npm run test` pipeline and integrate into CI once coverage foundations are in place.


