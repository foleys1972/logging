"""Baseline comparison and RAG evaluation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class RAGStatus(str, Enum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


@dataclass
class ComponentSnapshot:
    component: str
    hash: str
    count: int
    metrics: Dict[str, int]
    flags: Dict[str, bool]


@dataclass
class Baseline:
    component: str
    hash: str
    count: int
    metrics: Dict[str, int]
    version: int


@dataclass
class DiffReason:
    code: str
    message: str
    details: Dict[str, object]


@dataclass
class DiffResult:
    component: str
    status: RAGStatus
    reasons: List[DiffReason]


class RAGEvaluator:
    """Evaluate component snapshots against baselines using rule sets."""

    def __init__(self, tolerances: Optional[Dict[str, Dict[str, object]]] = None) -> None:
        self.tolerances = tolerances or {}

    def evaluate(self, snapshot: ComponentSnapshot, baseline: Optional[Baseline]) -> DiffResult:
        if baseline is None:
            return DiffResult(
                component=snapshot.component,
                status=RAGStatus.AMBER,
                reasons=[DiffReason(code="baseline.missing", message="Baseline not established", details={})],
            )

        reasons: List[DiffReason] = []
        status = RAGStatus.GREEN

        if snapshot.hash != baseline.hash:
            reasons.append(
                DiffReason(
                    code="hash.mismatch",
                    message="Snapshot hash differs from baseline",
                    details={"baseline_hash": baseline.hash, "current_hash": snapshot.hash},
                )
            )
            status = max(status, RAGStatus.AMBER, key=_rag_priority)

        if snapshot.count != baseline.count:
            delta = snapshot.count - baseline.count
            severity = self._count_severity(snapshot.component, baseline.count, snapshot.count)
            reasons.append(
                DiffReason(
                    code="count.delta",
                    message=f"Count changed by {delta}",
                    details={"baseline": baseline.count, "current": snapshot.count},
                )
            )
            status = max(status, severity, key=_rag_priority)

        for flag, value in snapshot.flags.items():
            if value:
                reasons.append(
                    DiffReason(
                        code=f"flag.{flag}",
                        message=f"Flag '{flag}' set true",
                        details={},
                    )
                )
                status = max(status, RAGStatus.AMBER, key=_rag_priority)

        if not reasons:
            reasons.append(DiffReason(code="baseline.match", message="Snapshot matches baseline", details={}))

        return DiffResult(component=snapshot.component, status=status, reasons=reasons)

    def _count_severity(self, component: str, baseline_count: int, current_count: int) -> RAGStatus:
        rules = self.tolerances.get(component, {})
        amber_pct = float(rules.get("amber_pct", 10.0))
        red_pct = float(rules.get("red_pct", 25.0))

        if baseline_count == 0:
            return RAGStatus.GREEN

        delta_pct = abs(current_count - baseline_count) / baseline_count * 100
        if delta_pct >= red_pct:
            return RAGStatus.RED
        if delta_pct >= amber_pct:
            return RAGStatus.AMBER
        return RAGStatus.GREEN


def _rag_priority(status: RAGStatus) -> int:
    order = {RAGStatus.GREEN: 0, RAGStatus.AMBER: 1, RAGStatus.RED: 2}
    return order[status]


