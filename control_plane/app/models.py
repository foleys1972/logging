"""SQLAlchemy models for the control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    baselines: Mapped[list[Baseline]] = relationship("Baseline", back_populates="site")
    snapshots: Mapped[list[Snapshot]] = relationship("Snapshot", back_populates="site")


class Baseline(Base):
    __tablename__ = "baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    component: Mapped[str] = mapped_column(String(50), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    hash: Mapped[str] = mapped_column(String(255))
    count: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(100))

    site: Mapped[Site] = relationship("Site", back_populates="baselines")


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    component: Mapped[str] = mapped_column(String(50), index=True)
    hash: Mapped[str] = mapped_column(String(255))
    count: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    flags: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    site: Mapped[Site] = relationship("Site", back_populates="snapshots")
    rag_results: Mapped[list[RAGResult]] = relationship("RAGResult", back_populates="snapshot")


class RAGResult(Base):
    __tablename__ = "rag_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    component: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(10))
    reasons: Mapped[dict] = mapped_column(JSON, default=list)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    snapshot: Mapped[Snapshot] = relationship("Snapshot", back_populates="rag_results")


