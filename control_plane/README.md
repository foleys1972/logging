# Control Plane Skeleton

FastAPI-based service acting as the central hub for the global WBA monitoring platform.

## Components

- `app/config.py` – settings management via Pydantic `BaseSettings`.
- `app/database.py` – SQLAlchemy async engine/session helpers.
- `app/models.py` – ORM models for sites, baselines, snapshots, RAG results.
- `app/schemas.py` – Pydantic schemas mirroring the API contracts.
- `app/api/v1/endpoints.py` – initial ingest and query endpoints skeleton.
- `app/main.py` – FastAPI app factory wiring routers and startup/shutdown handlers.

## Local Development

```bash
python -m venv .venv
source .venv/Scripts/activate  # Windows
pip install fastapi[all] sqlalchemy sqlalchemy[asyncio] asyncpg pydantic-settings
uvicorn app.main:app --reload
```

Database URL defaults to `postgresql+asyncpg://wba:wba@localhost:5432/wba_monitor` and can be overridden via environment variables.


