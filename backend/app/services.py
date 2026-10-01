from __future__ import annotations

import math
from typing import Any

import numpy as np
from fastapi import HTTPException
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from . import imaging
from .database import password_hash
from .models import (
    AuditLog,
    Base,
    ControlSample,
    Image,
    ImagePermission,
    JobGeneration,
    MatrixVersion,
    ResultVersion,
    TileTask,
    UnmixJob,
    User,
    utcnow,
)
from .storage import ObjectStore, canonical_json, sha256_bytes

ENGINE_ROLES = {"engineer", "admin"}
PUBLISH_ROLES = {"publisher", "admin"}


class ServiceError(RuntimeError):
    def __init__(self, http_status: int, code: str, detail: str):
        super().__init__(detail)
        self.http_status = http_status
        self.code = code
        self.detail = detail


def http_error(exc: ServiceError) -> HTTPException:
    return HTTPException(exc.http_status, {"code": exc.code, "detail": exc.detail})


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def audit(
    db: Session,
    *,
    actor_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        details=details or {},
    )
    db.add(row)
    return row


def seed_demo(db: Session) -> dict[str, User]:
    users = {
        "viewer@example.com": ("Microscopy Viewer", "viewer", "viewer-password"),
        "engineer@example.com": ("Matrix Engineer", "engineer", "engineer-password"),
        "publisher@example.com": ("Result Publisher", "publisher", "publisher-password"),
        "admin@example.com": ("Platform Admin", "admin", "admin-password"),
    }
    result = {}
    for email, (name, role, password) in users.items():
        user = db.query(User).filter(User.email == email).one_or_none()
        if not user:
            user = User(
                email=email,
                display_name=name,
                role=role,
                password_hash=password_hash(password),
            )
            db.add(user)
        result[role] = user
    db.commit()
    return result


def create_matrix_version(
    db: Session,
    store: ObjectStore,
    *,
    actor: User,
    name: str,
    channel_names: list[str],
    component_names: list[str],
    coefficients: list[list[float]],
    control_sample_ids: list[str] | None = None,
    matrix_key: str | None = None,
    publish: bool = False,
    allow_duplicate_version: bool = False,
) -> MatrixVersion:
    if actor.role not in ENGINE_ROLES:
        raise ServiceError(403, "forbidden", "matrix publication requires engineer or admin")

    coefficients_np = np.asarray(coefficients, dtype=np.float64)
    if coefficients_np.shape != (len(channel_names), len(component_names)):
        raise ServiceError(
            422,
            "invalid_matrix",
            f"expected {len(channel_names)} x {len(component_names)} coefficients",
        )
    if np.any(~np.isfinite(coefficients_np)) or np.any(coefficients_np < 0):
        raise ServiceError(422, "invalid_matrix", "coefficients must be finite and non-negative")
    if np.all(coefficients_np == 0, axis=0).any() or np.all(coefficients_np == 0, axis=1).any():
        raise ServiceError(422, "invalid_matrix", "matrix contains an all-zero row or column")
    rank = int(np.linalg.matrix_rank(coefficients_np, tol=1e-10))
    condition = None if rank < min(coefficients_np.shape) else float(np.linalg.cond(coefficients_np))
    if condition is None or not math.isfinite(condition) or condition > 1e8:
        raise ServiceError(
            422,
            "ill_conditioned_matrix",
            f"matrix condition number {condition} exceeds limit 100000000.0",
        )

    previous = None
    if matrix_key:
        previous = (
            db.query(MatrixVersion)
            .filter(MatrixVersion.matrix_key == matrix_key)
            .order_by(MatrixVersion.version_no.desc())
            .first()
        )
        if not previous:
            raise ServiceError(404, "matrix_not_found", matrix_key)
    matrix_key = matrix_key or sha256_bytes(canonical_json({"name": name}))[:16]
    version_no = (previous.version_no + 1) if previous else 1

    payload = {
        "name": name,
        "channel_names": channel_names,
        "component_names": component_names,
        "coefficients": coefficients,
        "control_sample_ids": control_sample_ids or [],
    }
    object_key, digest = store.put_json("matrices", payload)
    duplicate = db.query(MatrixVersion).filter(MatrixVersion.coefficient_digest == digest).one_or_none()
    if duplicate and not allow_duplicate_version:
        return duplicate

    row = MatrixVersion(
        matrix_key=matrix_key,
        version_no=version_no,
        name=name,
        status="published" if publish else "draft",
        channel_names=channel_names,
        component_names=component_names,
        coefficients=coefficients,
        coefficient_object_key=object_key,
        coefficient_digest=digest,
        condition_number=condition if condition is not None and math.isfinite(condition) else None,
        control_sample_ids=control_sample_ids or [],
        created_by=actor.id,
        published_by=actor.id if publish else None,
        published_at=utcnow() if publish else None,
    )
    db.add(row)
    db.flush()
    if previous and publish:
        previous.status = "superseded"
        previous.superseded_by_id = row.id
        previous.published_at = previous.published_at or utcnow()
    audit(
        db,
        actor_id=actor.id,
        action="matrix.version_create",
        resource_type="matrix_version",
        resource_id=row.id,
        outcome="success",
        details={"matrix_key": matrix_key, "version_no": version_no, "published": publish},
    )
    db.commit()
    db.refresh(row)
    return row


def publish_matrix(db: Session, matrix_id: str, actor: User) -> MatrixVersion:
    if actor.role not in ENGINE_ROLES:
        raise ServiceError(403, "forbidden", "matrix publication requires engineer or admin")
    row = db.get(MatrixVersion, matrix_id)
    if not row:
        raise ServiceError(404, "matrix_not_found", matrix_id)
    if row.status in {"published", "superseded"}:
        return row
    if row.condition_number is None or not math.isfinite(float(row.condition_number)):
        raise ServiceError(422, "ill_conditioned_matrix", "rank-deficient matrix cannot be published")
    if float(row.condition_number) > 1e8:
        raise ServiceError(422, "ill_conditioned_matrix", "matrix condition number exceeds limit")
    row.status = "published"
    row.published_by = actor.id
    row.published_at = utcnow()
    audit(
        db,
        actor_id=actor.id,
        action="matrix.publish",
        resource_type="matrix_version",
        resource_id=row.id,
        outcome="success",
        details={"matrix_key": row.matrix_key, "version_no": row.version_no},
    )
    db.commit()
    db.refresh(row)
    return row


def register_image(
    db: Session,
    *,
    actor: User,
    name: str,
    width: int,
    height: int,
    channel_names: list[str],
    source_object_key: str,
    manifest_object_key: str,
    source_digest: str,
    image_id: str | None = None,
) -> Image:
    row = Image(
        id=image_id,
        name=name,
        width=width,
        height=height,
        channel_names=channel_names,
        source_object_key=source_object_key,
        manifest_object_key=manifest_object_key,
        source_digest=source_digest,
        uploader_id=actor.id,
    )
    db.add(row)
    db.flush()
    db.add(ImagePermission(image_id=row.id, user_id=actor.id, permission="view_source"))
    db.add(ImagePermission(image_id=row.id, user_id=actor.id, permission="view_derived"))
    audit(
        db,
        actor_id=actor.id,
        action="image.register",
        resource_type="image",
        resource_id=row.id,
        outcome="success",
        details={"name": name, "digest": source_digest},
    )
    db.commit()
    db.refresh(row)
    return row


def grant_image_permission(
    db: Session, *, actor: User, image_id: str, user_id: str, permission: str
) -> ImagePermission:
    if permission not in {"view_source", "view_derived", "publish_result"}:
        raise ServiceError(422, "invalid_permission", permission)
    if not db.get(Image, image_id) or not db.get(User, user_id):
        raise ServiceError(404, "not_found", "image or user not found")
    row = (
        db.query(ImagePermission)
        .filter(
            ImagePermission.image_id == image_id,
            ImagePermission.user_id == user_id,
            ImagePermission.permission == permission,
        )
        .one_or_none()
    )
    if not row:
        row = ImagePermission(image_id=image_id, user_id=user_id, permission=permission)
        db.add(row)
    audit(
        db,
        actor_id=actor.id,
        action="image.grant",
        resource_type="image",
        resource_id=image_id,
        outcome="success",
        details={"grantee": user_id, "permission": permission},
    )
    db.commit()
    db.refresh(row)
    return row


def create_job(
    db: Session,
    *,
    actor: User,
    image_id: str,
    matrix_version_id: str,
    algorithm: str = "nnls",
    algorithm_params: dict[str, Any] | None = None,
) -> UnmixJob:
    from .security import has_image_permission

    image = db.get(Image, image_id)
    matrix = db.get(MatrixVersion, matrix_version_id)
    if not image:
        raise ServiceError(404, "image_not_found", image_id)
    if not matrix:
        raise ServiceError(404, "matrix_not_found", matrix_version_id)
    if matrix.status not in {"published", "superseded"}:
        raise ServiceError(422, "matrix_not_published", "publish the matrix before starting a job")
    if not has_image_permission(db, image_id, actor, "view_derived"):
        raise ServiceError(403, "forbidden", "view_derived permission is required to compute")
    # Validate eagerly, but start_job revalidates after locking the job row.
    imaging.validate_for_image(
        {"coefficients": matrix.coefficients, "channel_names": matrix.channel_names},
        {"channel_names": image.channel_names},
    )
    row = UnmixJob(
        image_id=image.id,
        matrix_version_id=matrix.id,
        algorithm=algorithm,
        algorithm_params=algorithm_params or {},
        image_digest=image.source_digest,
        matrix_digest=matrix.coefficient_digest,
        source_object_key=image.source_object_key,
        matrix_object_key=matrix.coefficient_object_key,
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        actor_id=actor.id,
        action="job.create",
        resource_type="unmix_job",
        resource_id=row.id,
        outcome="success",
        details={"image_id": image.id, "matrix_version_id": matrix.id, "algorithm": algorithm},
    )
    db.commit()
    db.refresh(row)
    return row


def _new_generation(db: Session, job: UnmixJob, reason: str) -> JobGeneration:
    current = (
        db.query(JobGeneration)
        .filter(JobGeneration.job_id == job.id)
        .order_by(JobGeneration.generation_no.desc())
        .first()
    )
    if current and current.status == "active":
        current.status = "deprecated"
        current.deprecated_at = utcnow()
    no = (current.generation_no + 1 if current else 1)
    gen = JobGeneration(job_id=job.id, generation_no=no, reason=reason)
    db.add(gen)
    job.current_generation_no = no
    db.flush()
    return gen


def start_job(db: Session, store: ObjectStore, job_id: str, *, reason: str = "initial") -> UnmixJob:
    job = db.get(UnmixJob, job_id, with_for_update=True)
    if not job:
        raise ServiceError(404, "job_not_found", job_id)
    if job.cancel_requested and reason == "initial":
        raise ServiceError(409, "job_cancelled", "create a retry generation explicitly")
    if job.status in {"running", "succeeded"}:
        return job

    image = db.get(Image, job.image_id)
    matrix = db.get(MatrixVersion, job.matrix_version_id)
    if image.source_digest != job.image_digest or image.source_object_key != job.source_object_key:
        raise ServiceError(409, "source_digest_changed", "immutable source binding no longer matches image")
    if matrix.coefficient_digest != job.matrix_digest or matrix.coefficient_object_key != job.matrix_object_key:
        raise ServiceError(409, "matrix_digest_changed", "immutable matrix binding no longer matches job")
    try:
        matrix_a, condition = imaging.validate_for_image(
            {"coefficients": matrix.coefficients, "channel_names": matrix.channel_names},
            {"channel_names": image.channel_names},
        )

        manifest = store.get_json(image.manifest_object_key)
        level_zero = [
            imaging.decode_source_tile(store.get(t["object_key"]))
            for t in manifest["tiles"]
            if t["level"] == 0
        ]
        frozen_params = imaging.freeze_global_statistics(level_zero)
    except imaging.UnmixValidationError as exc:
        raise ServiceError(422, exc.code, exc.detail) from exc

    frozen_params["matrix_digest"] = job.matrix_digest
    frozen_params["image_digest"] = job.image_digest
    frozen_params["condition_number"] = condition
    frozen_digest = sha256_bytes(canonical_json(frozen_params))

    gen = _new_generation(db, job, reason)
    job.status = "running"
    job.started_at = job.started_at or utcnow()
    job.finished_at = None
    job.error_code = None
    job.error_detail = None
    job.frozen_params = frozen_params
    job.frozen_digest = frozen_digest
    job.level_count = len(manifest["levels"])
    required = 0
    for level_info in manifest["levels"]:
        level = level_info["level"]
        for x in range(level_info["x_tiles"]):
            for y in range(level_info["y_tiles"]):
                for kind in ("component", "residual"):
                    key = imaging.tile_cache_key(
                        image_digest=job.image_digest,
                        matrix_digest=job.matrix_digest,
                        algorithm=job.algorithm,
                        algorithm_params=job.algorithm_params,
                        frozen_digest=frozen_digest,
                        level=level,
                        x=x,
                        y=y,
                        kind=kind,
                    )
                    db.add(TileTask(
                        job_id=job.id,
                        generation_id=gen.id,
                        level=level,
                        x=x,
                        y=y,
                        kind=kind,
                        required=True,
                        cache_key=key,
                    ))
                    required += 1
    job.required_tile_count = required
    audit(
        db,
        actor_id=job.created_by,
        action="job.start",
        resource_type="unmix_job",
        resource_id=job.id,
        outcome="success",
        details={"generation_no": gen.generation_no, "required_tiles": required, "reason": reason},
    )
    db.commit()
    db.refresh(job)
    return job


def request_cancel(db: Session, job_id: str, actor: User) -> UnmixJob:
    job = db.get(UnmixJob, job_id)
    if not job:
        raise ServiceError(404, "job_not_found", job_id)
    if not actor.role == "admin" and job.created_by != actor.id:
        raise ServiceError(403, "forbidden", "only owner or admin can cancel")
    job.cancel_requested = True
    job.status = "cancelled"
    job.finished_at = utcnow()
    gen = (
        db.query(JobGeneration)
        .filter(and_(JobGeneration.job_id == job.id, JobGeneration.generation_no == job.current_generation_no))
        .one_or_none()
    )
    if gen and gen.status == "active":
        gen.status = "cancelled"
        gen.deprecated_at = utcnow()
    audit(db, actor_id=actor.id, action="job.cancel", resource_type="unmix_job", resource_id=job.id, outcome="success")
    db.commit()
    db.refresh(job)
    return job


def retry_job(db: Session, store: ObjectStore, job_id: str, actor: User) -> UnmixJob:
    job = db.get(UnmixJob, job_id)
    if not job:
        raise ServiceError(404, "job_not_found", job_id)
    if not actor.role == "admin" and job.created_by != actor.id:
        raise ServiceError(403, "forbidden", "only owner or admin can retry")
    if not job.cancel_requested and job.status not in {"failed", "partial"}:
        raise ServiceError(409, "retry_not_allowed", "only cancelled, failed or partial jobs can retry")
    job.cancel_requested = False
    return start_job(db, store, job_id, reason="manual_retry")


def pending_tasks(db: Session, generation_id: str, limit: int = 100) -> list[TileTask]:
    return (
        db.query(TileTask)
        .filter(TileTask.generation_id == generation_id, TileTask.status == "pending")
        .order_by(TileTask.level, TileTask.y, TileTask.x, TileTask.kind)
        .limit(limit)
        .all()
    )


def job_snapshot(db: Session, job_id: str) -> dict[str, Any]:
    job = db.get(UnmixJob, job_id)
    if not job:
        raise ServiceError(404, "job_not_found", job_id)
    rows = (
        db.query(TileTask.status, TileTask.kind, func.count())
        .filter(TileTask.job_id == job_id, TileTask.generation_id == _active_generation_id(db, job))
        .group_by(TileTask.status, TileTask.kind)
        .all()
    )
    counts: dict[str, int] = {}
    for state, _kind, count in rows:
        counts[state] = counts.get(state, 0) + int(count)
    return {
        "job_id": job.id,
        "status": job.status,
        "generation_no": job.current_generation_no,
        "required_tile_count": job.required_tile_count,
        "counts": counts,
        "quality_flags": job.quality_flags,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "frozen_digest": job.frozen_digest,
    }


def _active_generation_id(db: Session, job: UnmixJob) -> str:
    gen = (
        db.query(JobGeneration)
        .filter(JobGeneration.job_id == job.id, JobGeneration.generation_no == job.current_generation_no)
        .one()
    )
    return gen.id


def finish_if_complete(db: Session, job_id: str) -> UnmixJob:
    job = db.get(UnmixJob, job_id, with_for_update=True)
    gen_id = _active_generation_id(db, job)
    total = db.query(func.count(TileTask.id)).filter(TileTask.generation_id == gen_id).scalar()
    succeeded = (
        db.query(func.count(TileTask.id))
        .filter(TileTask.generation_id == gen_id, TileTask.status == "succeeded")
        .scalar()
    )
    permanently_failed = (
        db.query(func.count(TileTask.id))
        .filter(
            TileTask.generation_id == gen_id,
            TileTask.status.in_(["failed", "cancelled", "stale"]),
        )
        .scalar()
    )
    pending = total - succeeded - permanently_failed
    if permanently_failed and pending == 0:
        job.status = "partial"
        job.finished_at = utcnow()
    elif succeeded == total:
        job.status = "succeeded"
        job.finished_at = utcnow()
    if job.status in {"partial", "succeeded"}:
        gen_tasks = db.query(TileTask).filter(TileTask.generation_id == gen_id).all()
        saturated = sum(
            1
            for task in gen_tasks
            if task.status == "succeeded" and task.quality_flags.get("saturated")
        )
        job.quality_flags = {
            "saturated_tiles": saturated,
            "failed_tiles": permanently_failed,
        }
    db.commit()
    db.refresh(job)
    return job


def publish_result(
    db: Session,
    store: ObjectStore,
    *,
    job_id: str,
    actor: User,
    force: bool = False,
) -> ResultVersion:
    from .security import has_image_permission

    job = db.get(UnmixJob, job_id, with_for_update=True)
    if not job:
        raise ServiceError(404, "job_not_found", job_id)
    if not has_image_permission(db, job.image_id, actor, "publish_result"):
        audit(
            db,
            actor_id=actor.id,
            action="result.publish_denied",
            resource_type="unmix_job",
            resource_id=job.id,
            outcome="denied",
        )
        db.commit()
        raise ServiceError(403, "forbidden", "source viewing does not grant result publication")

    gen = (
        db.query(JobGeneration)
        .filter(JobGeneration.job_id == job.id, JobGeneration.generation_no == job.current_generation_no)
        .with_for_update()
        .one()
    )
    if gen.status != "active":
        raise ServiceError(409, "generation_deprecated", "only the active generation can be published")

    missing = (
        db.query(TileTask)
        .filter(
            TileTask.generation_id == gen.id,
            TileTask.required.is_(True),
            TileTask.status != "succeeded",
        )
        .count()
    )
    if missing:
        raise ServiceError(409, "incomplete_generation", f"{missing} required tiles are missing")

    # Serialize competing publishers for this exact image. The second request
    # sees the newly published result and receives a conflict instead of
    # silently creating a second current report.
    image = db.get(Image, job.image_id, with_for_update=True)
    latest_no = (
        db.query(func.coalesce(func.max(ResultVersion.version_no), 0))
        .filter(ResultVersion.image_id == image.id)
        .scalar()
    )
    active_published = (
        db.query(ResultVersion)
        .filter(ResultVersion.image_id == image.id, ResultVersion.status == "published")
        .one_or_none()
    )
    if active_published and not force:
        raise ServiceError(409, "concurrent_publication", "another report is already published")

    tasks = db.query(TileTask).filter(TileTask.generation_id == gen.id).all()
    by_coordinate: dict[tuple[int, int, int], dict[str, TileTask]] = {}
    for t in tasks:
        by_coordinate.setdefault((t.level, t.x, t.y), {})[t.kind] = t
    bad_coordinates = [
        key for key, views in by_coordinate.items()
        if not {"component", "residual"} <= set(views)
        or any(v.generation_id != gen.id for v in views.values())
    ]
    if bad_coordinates:
        raise ServiceError(409, "mixed_generation", "required views do not all come from one generation")

    succeeded_tasks = [t for t in tasks if t.status == "succeeded"]
    rmse_values = [float(t.quality_flags.get("tile_rmse", 0.0)) for t in succeeded_tasks]
    aggregate = {"rmse_mean": float(np.mean(rmse_values)) if rmse_values else None}
    quality_summary = job.quality_flags or {}
    report = {
        "image_id": image.id,
        "image_digest": job.image_digest,
        "matrix_digest": job.matrix_digest,
        "matrix_version_id": job.matrix_version_id,
        "job_id": job.id,
        "generation_id": gen.id,
        "generation_no": gen.generation_no,
        "algorithm": job.algorithm,
        "algorithm_params": job.algorithm_params,
        "frozen_digest": job.frozen_digest,
        "tile_count": len(tasks),
        "tiles": [
            {
                "level": t.level, "x": t.x, "y": t.y, "kind": t.kind,
                "cache_key": t.cache_key, "object_digest": t.object_digest,
            }
            for t in sorted(tasks, key=lambda z: (z.level, z.y, z.x, z.kind))
        ],
        "quality_summary": quality_summary,
        "aggregate": aggregate,
    }
    report_key, report_digest = store.put_json("reports", report)
    row = ResultVersion(
        image_id=image.id,
        job_id=job.id,
        generation_id=gen.id,
        version_no=int(latest_no) + 1,
        status="published",
        cache_namespace=(
            f"img={job.image_digest}/mat={job.matrix_digest}/alg={job.algorithm}/"
            f"frozen={job.frozen_digest}/gen={gen.generation_no}"
        ),
        report_object_key=report_key,
        report_digest=report_digest,
        quality_summary=quality_summary,
        created_by=actor.id,
        published_by=actor.id,
        published_at=utcnow(),
    )
    db.add(row)
    db.flush()
    if active_published:
        active_published.status = "superseded"
    audit(
        db,
        actor_id=actor.id,
        action="result.publish",
        resource_type="result_version",
        resource_id=row.id,
        outcome="success",
        details={
            "image_id": image.id,
            "job_id": job.id,
            "generation_no": gen.generation_no,
            "report_digest": report_digest,
            "superseded_result_id": active_published.id if active_published else None,
        },
    )
    db.commit()
    db.refresh(row)
    return row
