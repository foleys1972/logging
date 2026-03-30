"""RAG evaluation pipeline for snapshots."""

from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from wba_agent.rag import Baseline as BaselineData
from wba_agent.rag import ComponentSnapshot, DiffResult, RAGEvaluator

from ..models import Baseline, RAGResult, Snapshot


async def evaluate_snapshot(
    session: AsyncSession, snapshot: Snapshot, evaluator: Optional[RAGEvaluator] = None
) -> Tuple[RAGResult, DiffResult]:
    """Evaluate snapshot against baseline and persist RAG result."""

    evaluator = evaluator or RAGEvaluator()

    baseline_model = await _get_latest_baseline(session, snapshot.site_id, snapshot.component)

    baseline_data: Optional[BaselineData]
    if baseline_model:
        baseline_data = BaselineData(
            component=baseline_model.component,
            hash=baseline_model.hash,
            count=baseline_model.count,
            metrics=baseline_model.metrics or {},
            version=baseline_model.version,
        )
    else:
        baseline_data = None

    component_snapshot = ComponentSnapshot(
        component=snapshot.component,
        hash=snapshot.hash,
        count=snapshot.count,
        metrics=snapshot.metrics or {},
        flags=snapshot.flags or {},
    )

    diff = evaluator.evaluate(component_snapshot, baseline_data)

    reasons_payload = [
        {"code": reason.code, "message": reason.message, "details": reason.details}
        for reason in diff.reasons
    ]

    summary = next((r["message"] for r in reasons_payload if r["code"] != "baseline.match"), reasons_payload[0]["message"])

    rag_result = RAGResult(
        site_id=snapshot.site_id,
        snapshot_id=snapshot.id,
        component=snapshot.component,
        status=diff.status.value,
        reasons=reasons_payload,
        summary=summary,
    )

    session.add(rag_result)

    if baseline_model is None:
        new_baseline = Baseline(
            site_id=snapshot.site_id,
            component=snapshot.component,
            version=1,
            hash=snapshot.hash,
            count=snapshot.count,
            metrics=snapshot.metrics or {},
        )
        session.add(new_baseline)

    return rag_result, diff


async def _get_latest_baseline(session: AsyncSession, site_id: int, component: str) -> Optional[Baseline]:
    stmt: Select[Baseline] = (
        select(Baseline)
        .where(Baseline.site_id == site_id, Baseline.component == component)
        .order_by(Baseline.version.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


