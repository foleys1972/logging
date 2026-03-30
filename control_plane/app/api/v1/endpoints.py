"""API endpoints skeleton."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from ...database import session_scope
from ...models import RAGResult, Site, Snapshot
from ...schemas import (
    ComponentStatus,
    SiteCreate,
    SiteDetail,
    SiteOut,
    SiteStatus,
    SnapshotIn,
    SnapshotIngestResponse,
    SnapshotOut,
)
from ...services.rag import evaluate_snapshot
from ...security import require_agent_token, require_dashboard_token


router = APIRouter()


async def get_session() -> AsyncSession:
    async with session_scope() as session:
        yield session


@router.post("/sites", response_model=SiteOut, status_code=status.HTTP_201_CREATED)
async def create_site(
    payload: SiteCreate,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_dashboard_token),
) -> Site:
    existing = await session.scalar(select(Site).where(Site.name == payload.name))
    if existing:
        raise HTTPException(status_code=400, detail="Site already exists")

    site = Site(name=payload.name, region=payload.region)
    session.add(site)
    await session.flush()
    return site


@router.get("/sites", response_model=list[SiteOut])
async def list_sites(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_dashboard_token),
) -> list[Site]:
    result = await session.execute(select(Site))
    return list(result.scalars().all())


@router.get("/sites/status", response_model=list[SiteStatus])
async def site_status(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_dashboard_token),
) -> list[SiteStatus]:
    stmt = (
        select(Site)
        .options(joinedload(Site.snapshots), joinedload(Site.snapshots).joinedload(Snapshot.rag_results))
    )
    result = await session.execute(stmt)
    sites = result.scalars().unique().all()

    statuses: list[SiteStatus] = []
    for site in sites:
        latest_snapshot = max(site.snapshots, key=lambda snap: snap.created_at, default=None)
        latest_rag = None
        if latest_snapshot:
            latest_rag = max(latest_snapshot.rag_results, key=lambda r: r.created_at, default=None)

        statuses.append(
            SiteStatus(
                site_id=site.id,
                name=site.name,
                region=site.region,
                status=latest_rag.status if latest_rag else "AMBER",
                summary=latest_rag.summary if latest_rag else "Baseline pending",
                last_snapshot_at=latest_snapshot.created_at if latest_snapshot else None,
            )
        )

    return statuses


@router.get("/sites/{site_id}/detail", response_model=SiteDetail)
async def site_detail(
    site_id: int,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_dashboard_token),
) -> SiteDetail:
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    rag_stmt = (
        select(RAGResult)
        .where(RAGResult.site_id == site_id)
        .order_by(RAGResult.created_at.desc())
    )
    rag_result_records = (await session.execute(rag_stmt)).scalars().all()

    latest_by_component: dict[str, RAGResult] = {}
    for record in rag_result_records:
        if record.component not in latest_by_component:
            latest_by_component[record.component] = record

    components: list[ComponentStatus] = []
    for comp, record in latest_by_component.items():
        components.append(
            ComponentStatus(
                component=comp,
                status=record.status,
                summary=record.summary,
                last_updated=record.created_at,
                reasons=record.reasons or [],
            )
        )

    last_snapshot_at = max((c.last_updated for c in components), default=None)
    overall_status = _aggregate_overall_status([c.status for c in components])

    return SiteDetail(
        site_id=site.id,
        name=site.name,
        region=site.region,
        overall_status=overall_status,
        components=components,
        last_snapshot_at=last_snapshot_at,
    )


def _aggregate_overall_status(statuses: list[str]) -> str:
    if not statuses:
        return "AMBER"
    order = {"GREEN": 0, "AMBER": 1, "RED": 2}
    worst = max(statuses, key=lambda s: order.get(s, 1))
    return worst


@router.post(
    "/sites/{site_id}/snapshots",
    response_model=SnapshotIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_snapshot(
    site_id: int,
    payload: SnapshotIn,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_agent_token),
) -> SnapshotIngestResponse:
    site = await session.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    snapshot = Snapshot(
        site_id=site_id,
        component=payload.component,
        hash=payload.hash,
        count=payload.count,
        metrics=payload.metrics,
        flags=payload.flags,
        payload=payload.payload,
    )
    session.add(snapshot)
    await session.flush()

    rag_result, _ = await evaluate_snapshot(session, snapshot)
    await session.flush()

    return SnapshotIngestResponse(snapshot=snapshot, rag_result=rag_result)


