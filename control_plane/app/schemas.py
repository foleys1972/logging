"""Pydantic schemas for API responses and requests."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class BaselineReason(BaseModel):
    code: str
    message: str
    details: dict


class SnapshotIn(BaseModel):
    component: str
    hash: str
    count: int
    metrics: dict
    flags: dict = {}
    payload: Optional[dict] = None


class SnapshotOut(SnapshotIn):
    id: int
    site_id: int
    created_at: datetime

    class Config:
        orm_mode = True


class RAGResultOut(BaseModel):
    id: int
    site_id: int
    snapshot_id: int
    component: str
    status: str
    reasons: List[dict]
    summary: Optional[str]
    created_at: datetime

    class Config:
        orm_mode = True


class SnapshotIngestResponse(BaseModel):
    snapshot: SnapshotOut
    rag_result: RAGResultOut

    class Config:
        orm_mode = True


class SiteCreate(BaseModel):
    name: str
    region: Optional[str] = None


class SiteOut(BaseModel):
    id: int
    name: str
    region: Optional[str]
    created_at: datetime
    updated_at: datetime
    is_active: bool

    class Config:
        orm_mode = True


class SiteStatus(BaseModel):
    site_id: int
    name: str
    region: Optional[str]
    status: str
    summary: Optional[str]
    last_snapshot_at: Optional[datetime]


class ComponentStatus(BaseModel):
    component: str
    status: str
    summary: Optional[str]
    last_updated: datetime
    reasons: List[dict]


class SiteDetail(BaseModel):
    site_id: int
    name: str
    region: Optional[str]
    overall_status: str
    components: List[ComponentStatus]
    last_snapshot_at: Optional[datetime]


