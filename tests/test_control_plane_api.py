import os

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.app.main import create_app
from control_plane.app.database import get_engine
from control_plane.app.models import Base
from control_plane.app.config import get_settings


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def configure_env():
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
    os.environ["AGENT_API_TOKENS"] = "agent-token"
    os.environ["DASHBOARD_API_TOKENS"] = "dashboard-token"
    get_settings.cache_clear()
    yield
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("AGENT_API_TOKENS", None)
    os.environ.pop("DASHBOARD_API_TOKENS", None)
    get_settings.cache_clear()


@pytest.fixture(scope="module", autouse=True)
async def setup_database():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app = create_app()
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.mark.anyio
async def test_create_site_and_snapshot(client: AsyncClient):
    response = await client.post(
        "/api/v1/sites",
        json={"name": "Test", "region": "EMEA"},
        headers={"Authorization": "Bearer dashboard-token"},
    )
    assert response.status_code == 201
    site_id = response.json()["id"]

    snapshot_payload = {
        "component": "get_zones",
        "hash": "abc",
        "count": 1,
        "metrics": {},
        "flags": {},
    }
    response = await client.post(
        f"/api/v1/sites/{site_id}/snapshots",
        json=snapshot_payload,
        headers={"Authorization": "Bearer agent-token"},
    )
    assert response.status_code == 202

    status_response = await client.get(
        "/api/v1/sites/status",
        headers={"Authorization": "Bearer dashboard-token"},
    )
    assert status_response.status_code == 200
    assert status_response.json()[0]["site_id"] == site_id


