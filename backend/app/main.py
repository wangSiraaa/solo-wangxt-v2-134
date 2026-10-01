from __future__ import annotations

import io
import uuid
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from . import imaging, ingest, services
from .celery_app import start_job_task
from .config import get_settings
from .database import create_token, get_db, init_db, engine
from .models import (
    AuditLog,
    ControlSample,
    Image,
    ImagePermission,
    JobGeneration,
    MatrixVersion,
    ResultVersion,
    TileTask,
    UnmixJob,
    User,
)
from .schemas import ControlSampleCreate, JobCreate, LoginRequest, MatrixCreate, PermissionGrant, PublishRequest, TokenResponse
from .security import current_user, has_image_permission, require_role
from .worker import source_tile_for
from .storage import ObjectStore

settings = get_settings()
app = FastAPI(title="Fluorescence Channel Unmixing Platform", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def store() -> ObjectStore:
    return ObjectStore(
        local_dir=settings.local_object_dir,
        endpoint=None if settings.local_object_dir else settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )


@app.on_event("startup")
def startup() -> None:
    init_db(engine)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    from .database import verify_password

    user = db.query(User).filter(User.email == payload.email).one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return TokenResponse(access_token=create_token(user.id, user.role), role=user.role)


@app.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "role": user.role}


@app.post("/demo/bootstrap")
def bootstrap_demo(db: Session = Depends(get_db)):
    """Create deterministic demo users, image and published matrix."""
    users = services.seed_demo(db)
    existing = db.query(Image).filter(Image.name == "20k synthetic four-channel").one_or_none()
    if existing:
        return {"image_id": existing.id, "users": {r: u.email for r, u in users.items()}}

    source, truth = ingest.synthetic_demo_image(2048, 2048)
    object_store = store()
    image_id = str(uuid.uuid4())
    manifest = ingest.create_pyramid_manifest(
        object_store, source, image_id=image_id, tile_size=settings.tile_size
    )
    image = services.register_image(
        db,
        actor=users["admin"],
        name="20k synthetic four-channel",
        width=manifest["width"],
        height=manifest["height"],
        channel_names=["DAPI", "FITC", "TRITC", "Cy5"],
        source_object_key="",
        manifest_object_key="",
        source_digest=manifest["source_digest"],
        image_id=image_id,
    )
    manifest_key, _ = object_store.put_json(f"manifests/{image.id}", manifest)
    image.manifest_object_key = manifest_key
    image.source_object_key = manifest_key

    matrix = services.create_matrix_version(
        db,
        object_store,
        actor=users["engineer"],
        name="four-channel calibration v1",
        channel_names=["DAPI", "FITC", "TRITC", "Cy5"],
        component_names=["nuclei", "membrane", "beads"],
        coefficients=[
            [0.95, 0.08, 0.02],
            [0.08, 0.88, 0.05],
            [0.03, 0.10, 0.90],
            [0.02, 0.04, 0.18],
        ],
        publish=True,
    )
    for role, perms in {
        "viewer": ["view_source"],
        "engineer": ["view_source", "view_derived"],
        "publisher": ["view_source", "view_derived", "publish_result"],
    }.items():
        for perm in perms:
            db.add(ImagePermission(
                image_id=image.id, user_id=users[role].id, permission=perm
            ))
    db.commit()
    return {"image_id": image.id, "matrix_version_id": matrix.id, "users": {r: u.email for r, u in users.items()}}


@app.get("/images")
def list_images(user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = db.query(Image)
    if user.role != "admin":
        query = query.join(Image.permissions).filter(ImagePermission.user_id == user.id)
    return [
        {"id": i.id, "name": i.name, "width": i.width, "height": i.height, "channels": i.channel_names}
        for i in query.order_by(Image.created_at.desc())
    ]


@app.get("/control-samples")
def list_control_samples(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [
        {
            "id": s.id,
            "name": s.name,
            "component_name": s.component_name,
            "image_id": s.image_id,
            "description": s.description,
        }
        for s in db.query(ControlSample).order_by(ControlSample.created_at.desc())
    ]


@app.post("/control-samples")
def create_control_sample(
    payload: ControlSampleCreate,
    user: User = Depends(require_role("engineer", "admin")),
    db: Session = Depends(get_db),
):
    row = ControlSample(
        name=payload.name,
        component_name=payload.component_name,
        image_id=payload.image_id,
        description=payload.description,
    )
    db.add(row)
    db.flush()
    services.audit(
        db,
        actor_id=user.id,
        action="control_sample.create",
        resource_type="control_sample",
        resource_id=row.id,
        outcome="success",
        details={"name": payload.name, "component": payload.component_name},
    )
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "component_name": row.component_name}


@app.post("/images/{image_id}/permissions")
def grant_permission(
    image_id: str,
    payload: PermissionGrant,
    user: User = Depends(require_role("engineer", "admin")),
    db: Session = Depends(get_db),
):
    try:
        row = services.grant_image_permission(
            db, actor=user, image_id=image_id, user_id=payload.user_id, permission=payload.permission
        )
    except services.ServiceError as exc:
        raise services.http_error(exc)
    return {"id": row.id, "permission": row.permission}


@app.get("/matrices")
def list_matrices(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.query(MatrixVersion).order_by(
        MatrixVersion.matrix_key, MatrixVersion.version_no.desc()
    )
    return [_matrix_dict(m) for m in rows]


@app.post("/matrices")
def create_matrix(
    payload: MatrixCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        matrix = services.create_matrix_version(
            db,
            store(),
            actor=user,
            name=payload.name,
            channel_names=payload.channel_names,
            component_names=payload.component_names,
            coefficients=payload.coefficients,
            control_sample_ids=payload.control_sample_ids,
            matrix_key=payload.matrix_key,
            publish=payload.publish,
            allow_duplicate_version=payload.allow_duplicate_version,
        )
    except services.ServiceError as exc:
        raise services.http_error(exc)
    return _matrix_dict(matrix)


@app.post("/matrices/{matrix_id}/publish")
def publish_matrix(
    matrix_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        matrix = services.publish_matrix(db, matrix_id, user)
    except services.ServiceError as exc:
        raise services.http_error(exc)
    return _matrix_dict(matrix)


def _matrix_dict(m: MatrixVersion):
    return {
        "id": m.id,
        "matrix_key": m.matrix_key,
        "version_no": m.version_no,
        "name": m.name,
        "status": m.status,
        "channel_names": m.channel_names,
        "component_names": m.component_names,
        "coefficients": m.coefficients,
        "coefficient_digest": m.coefficient_digest,
        "condition_number": float(m.condition_number) if m.condition_number is not None else None,
        "published_at": m.published_at,
    }


@app.post("/jobs")
def create_job(
    payload: JobCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        job = services.create_job(
            db,
            actor=user,
            image_id=payload.image_id,
            matrix_version_id=payload.matrix_version_id,
            algorithm=payload.algorithm,
            algorithm_params=payload.algorithm_params,
        )
    except imaging.UnmixValidationError as exc:
        raise HTTPException(422, {"code": exc.code, "detail": exc.detail})
    except services.ServiceError as exc:
        raise services.http_error(exc)
    start_job_task.delay(job.id)
    return _job_dict(job)


@app.post("/jobs/{job_id}/start")
def start_existing_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        job = services.start_job(db, store(), job_id)
    except services.ServiceError as exc:
        raise services.http_error(exc)
    return _job_dict(job)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        job = services.request_cancel(db, job_id, user)
    except services.ServiceError as exc:
        raise services.http_error(exc)
    return _job_dict(job)


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        job = services.retry_job(db, store(), job_id, user)
    except services.ServiceError as exc:
        raise services.http_error(exc)
    start_job_task.delay(job.id, "manual_retry")
    return _job_dict(job)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        snap = services.job_snapshot(db, job_id)
    except services.ServiceError as exc:
        raise services.http_error(exc)
    return snap


def _job_dict(job: UnmixJob):
    return {
        "id": job.id,
        "image_id": job.image_id,
        "matrix_version_id": job.matrix_version_id,
        "status": job.status,
        "generation_no": job.current_generation_no,
        "algorithm": job.algorithm,
        "image_digest": job.image_digest,
        "matrix_digest": job.matrix_digest,
        "frozen_digest": job.frozen_digest,
    }


@app.get("/jobs/{job_id}/tiles")
def list_job_tiles(
    job_id: str,
    generation: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    job = db.get(UnmixJob, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not has_image_permission(db, job.image_id, user, "view_derived"):
        raise HTTPException(403, "missing view_derived")
    q = db.query(TileTask).join(JobGeneration, TileTask.generation_id == JobGeneration.id)
    q = q.filter(TileTask.job_id == job_id)
    if generation is not None:
        q = q.filter(JobGeneration.generation_no == generation)
    else:
        q = q.filter(JobGeneration.generation_no == job.current_generation_no)
    return [
        {
            "id": t.id,
            "generation_no": generation if generation is not None else job.current_generation_no,
            "level": t.level,
            "x": t.x,
            "y": t.y,
            "kind": t.kind,
            "status": t.status,
            "attempts": t.attempts,
            "error_code": t.error_code,
            "cache_key": t.cache_key,
            "quality_flags": t.quality_flags,
        }
        for t in q.order_by(TileTask.level, TileTask.y, TileTask.x, TileTask.kind)
    ]


@app.get("/jobs/{job_id}/dzi/{kind}.json")
def job_dzi(job_id: str, kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if kind not in {"component", "residual"}:
        raise HTTPException(404, "unknown viewer kind")
    job = db.get(UnmixJob, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not has_image_permission(db, job.image_id, user, "view_derived"):
        raise HTTPException(403, "missing view_derived")
    image = db.get(Image, job.image_id)
    return {
        "Image": {
            "xmlns": "http://schemas.microsoft.com/deepzoom/2008",
            "TileSize": settings.tile_size,
            "Overlap": 0,
            "Format": "png",
            "Size": {"Width": image.width, "Height": image.height},
        },
        "tileUrl": f"/jobs/{job_id}/tiles/{kind}/files",
        "generation": job.current_generation_no,
        "frozenDigest": job.frozen_digest,
        "matrixDigest": job.matrix_digest,
    }


@app.get("/jobs/{job_id}/tiles/{kind}/files/{osd_level}/{x}_{y}.png")
def derived_tile_png(
    job_id: str,
    kind: str,
    osd_level: int,
    x: int,
    y: int,
    channel: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    job = db.get(UnmixJob, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not has_image_permission(db, job.image_id, user, "view_derived"):
        raise HTTPException(403, "missing view_derived")
    level = imaging.osd_to_storage_level(osd_level, job.level_count - 1)
    task = (
        db.query(TileTask)
        .join(JobGeneration, TileTask.generation_id == JobGeneration.id)
        .filter(
            TileTask.job_id == job_id,
            JobGeneration.generation_no == job.current_generation_no,
            TileTask.kind == kind,
            TileTask.level == level,
            TileTask.x == x,
            TileTask.y == y,
        )
        .one_or_none()
    )
    if not task or task.status != "succeeded":
        # Transparent 64x64 placeholder plus X in the header gives the viewer a
        # clear partial-result marker instead of stale pixels.
        raise HTTPException(409, "tile unavailable in current generation")
    blob = store().get(task.object_key)
    loaded = np.load(io.BytesIO(blob), allow_pickle=False)
    array = loaded["components"] if kind == "component" else loaded["residual"]
    if channel >= array.shape[0]:
        raise HTTPException(422, "channel out of range")
    color = (80, 220, 255) if kind == "component" else (255, 180, 80)
    png = imaging.encode_png_channel(array[channel], color=color)
    return Response(content=png, media_type="image/png", headers={
        "X-Cache-Key": task.cache_key or "",
        "X-Generation-No": str(job.current_generation_no),
        "X-Unmixed": "partial" if job.status == "partial" else "current",
        "Cache-Control": "no-store",
    })


@app.get("/images/{image_id}/dzi/source.json")
def source_dzi(image_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(404, "image not found")
    if not has_image_permission(db, image_id, user, "view_source"):
        raise HTTPException(403, "missing view_source")
    return {
        "Image": {
            "xmlns": "http://schemas.microsoft.com/deepzoom/2008",
            "TileSize": settings.tile_size,
            "Overlap": 0,
            "Format": "png",
            "Size": {"Width": image.width, "Height": image.height},
        },
        "tileUrl": f"/images/{image_id}/tiles/source/files",
        "imageDigest": image.source_digest,
    }


@app.get("/images/{image_id}/tiles/source/files/{osd_level}/{x}_{y}.png")
def source_tile_png(
    image_id: str,
    osd_level: int,
    x: int,
    y: int,
    channel: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(404, "image not found")
    if not has_image_permission(db, image_id, user, "view_source"):
        raise HTTPException(403, "missing view_source")
    manifest = store().get_json(image.manifest_object_key)
    level = imaging.osd_to_storage_level(osd_level, len(manifest["levels"]) - 1)
    ref = source_tile_for(manifest, level, x, y)
    tile = imaging.decode_source_tile(store().get(ref["object_key"]))
    if channel >= tile.shape[0]:
        raise HTTPException(422, "channel out of range")
    png = imaging.encode_png_channel(tile[channel], color=(170, 255, 170))
    return Response(content=png, media_type="image/png", headers={
        "X-Image-Digest": image.source_digest,
        "Cache-Control": "no-store",
    })


@app.get("/audit-logs")
def audit_logs(
    resource_type: str | None = None,
    resource_id: str | None = None,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    query = db.query(AuditLog)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if resource_id:
        query = query.filter(AuditLog.resource_id == resource_id)
    return [
        {
            "id": row.id,
            "actor_id": row.actor_id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "outcome": row.outcome,
            "details": row.details,
            "created_at": row.created_at,
        }
        for row in query.order_by(AuditLog.created_at.desc()).limit(500)
    ]


@app.get("/results")
def list_results(user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = db.query(ResultVersion)
    if user.role != "admin":
        query = query.join(Image, ResultVersion.image_id == Image.id).join(
            ImagePermission, ImagePermission.image_id == Image.id
        ).filter(ImagePermission.user_id == user.id)
    return [
        {
            "id": r.id,
            "image_id": r.image_id,
            "job_id": r.job_id,
            "version_no": r.version_no,
            "status": r.status,
            "report_digest": r.report_digest,
            "published_at": r.published_at,
        }
        for r in db.query(ResultVersion).order_by(ResultVersion.created_at.desc())
    ]


@app.post("/jobs/{job_id}/publish")
def publish_job_result(
    job_id: str,
    payload: PublishRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        result = services.publish_result(db, store(), job_id=job_id, actor=user, force=payload.force)
    except services.ServiceError as exc:
        raise services.http_error(exc)
    return {
        "id": result.id,
        "version_no": result.version_no,
        "status": result.status,
        "report_object_key": result.report_object_key,
        "report_digest": result.report_digest,
    }
