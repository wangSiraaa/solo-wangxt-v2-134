from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    # Roles are deliberately coarse; action-level checks stay in services.
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    password_hash: Mapped[str] = mapped_column(String(255), default="!")


class ControlSample(Base):
    __tablename__ = "control_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    image_id: Mapped[str | None] = mapped_column(ForeignKey("images.id"), nullable=True)
    component_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatrixVersion(Base):
    __tablename__ = "matrix_versions"
    __table_args__ = (
        UniqueConstraint("matrix_key", "version_no", name="uq_matrix_version"),
        Index("ix_matrix_digest", "coefficient_digest"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    matrix_key: Mapped[str] = mapped_column(String(64), index=True, default=lambda: new_id())
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    channel_names: Mapped[list[str]] = mapped_column(JSON)
    component_names: Mapped[list[str]] = mapped_column(JSON)
    coefficients: Mapped[list[list[float]]] = mapped_column(JSON)
    coefficient_object_key: Mapped[str] = mapped_column(Text)
    coefficient_digest: Mapped[str] = mapped_column(String(64), index=True)
    condition_number: Mapped[float | None] = mapped_column(Numeric(30, 12), nullable=True)
    control_sample_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    published_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("matrix_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Image(Base):
    __tablename__ = "images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    channel_names: Mapped[list[str]] = mapped_column(JSON)
    source_object_key: Mapped[str] = mapped_column(Text)
    manifest_object_key: Mapped[str] = mapped_column(Text)
    source_digest: Mapped[str] = mapped_column(String(64), index=True)
    uploader_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    permissions: Mapped[list[ImagePermission]] = relationship(back_populates="image")


class ImagePermission(Base):
    __tablename__ = "image_permissions"
    __table_args__ = (
        UniqueConstraint("image_id", "user_id", "permission", name="uq_image_permission"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # view_source, view_derived, publish_result are intentionally separate.
    permission: Mapped[str] = mapped_column(String(32))
    image: Mapped[Image] = relationship(back_populates="permissions")


class UnmixJob(Base):
    __tablename__ = "unmix_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), index=True)
    matrix_version_id: Mapped[str] = mapped_column(ForeignKey("matrix_versions.id"))
    algorithm: Mapped[str] = mapped_column(String(64), default="nnls")
    algorithm_params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_generation_no: Mapped[int] = mapped_column(Integer, default=0)
    # Captured at job creation/start and never replaced.
    image_digest: Mapped[str] = mapped_column(String(64))
    matrix_digest: Mapped[str] = mapped_column(String(64))
    source_object_key: Mapped[str] = mapped_column(Text)
    matrix_object_key: Mapped[str] = mapped_column(Text)
    level_count: Mapped[int] = mapped_column(Integer, default=1)
    required_tile_count: Mapped[int] = mapped_column(Integer, default=0)
    frozen_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    frozen_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_flags: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    generations: Mapped[list[JobGeneration]] = relationship(back_populates="job")


class JobGeneration(Base):
    __tablename__ = "job_generations"
    __table_args__ = (
        UniqueConstraint("job_id", "generation_no", name="uq_job_generation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("unmix_jobs.id"), index=True)
    generation_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    reason: Mapped[str] = mapped_column(String(255), default="initial")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job: Mapped[UnmixJob] = relationship(back_populates="generations")


class TileTask(Base):
    __tablename__ = "tile_tasks"
    __table_args__ = (
        UniqueConstraint(
            "generation_id", "level", "x", "y", "kind", name="uq_generation_tile_kind"
        ),
        Index("ix_tile_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("unmix_jobs.id"), index=True)
    generation_id: Mapped[str] = mapped_column(ForeignKey("job_generations.id"), index=True)
    level: Mapped[int] = mapped_column(Integer)
    x: Mapped[int] = mapped_column(Integer)
    y: Mapped[int] = mapped_column(Integer)
    # component / residual / quality are different views of one coordinate.
    kind: Mapped[str] = mapped_column(String(32), default="component")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cache_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_flags: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResultVersion(Base):
    __tablename__ = "result_versions"
    __table_args__ = (
        UniqueConstraint("image_id", "version_no", name="uq_image_result_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("unmix_jobs.id"))
    generation_id: Mapped[str] = mapped_column(ForeignKey("job_generations.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="not_published", index=True)
    cache_namespace: Mapped[str] = mapped_column(Text)
    report_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    published_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
