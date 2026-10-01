from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import pipeline
from ..database import get_db
from ..models import Generation, Image, Job, MatrixVersion, TileRecord
from ..schemas import JobIn
from ..security import (
    Principal,
    audit,
    can_view_image,
    require_scope,
    resolve_principal,
)
from ..tasks import get_runner

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_brief(job: Job) -> dict:
    return {
        "id": job.id, "image_id": job.image_id,
        "matrix_version_id": job.matrix_version_id,
        "algorithm": job.algorithm,
        "image_digest": job.image_digest,
        "matrix_digest": job.matrix_digest,
        "status": job.status,
        "params_hash": job.params_hash,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "successor_job_id": job.successor_job_id,
    }


@router.post("")
def create_job(body: JobIn, db: Session = Depends(get_db),
               p: Principal = Depends(require_scope("job:create"))):
    image = db.get(Image, body.image_id)
    if image is None:
        raise HTTPException(404, "image not found")
    if not can_view_image(db, p, image):
        audit(db, p, "job.create", "denied", resource=f"image:{image.id}",
              detail="no view permission")
        db.commit()
        raise HTTPException(403, "no view permission on this image")
    if body.matrix_version_id is None:
        matrix = db.scalars(
            select(MatrixVersion).where(MatrixVersion.name == "default")
            .order_by(MatrixVersion.version.desc())
        ).first()
    else:
        matrix = db.get(MatrixVersion, body.matrix_version_id)
    if matrix is None:
        raise HTTPException(404, "matrix version not found")

    # PIN BOTH DIGESTS AT CREATION (the requirement: 任务在启动时固定二者摘要)
    job = Job(
        image_id=image.id,
        matrix_version_id=matrix.id,
        algorithm=body.algorithm,
        algorithm_params={},
        image_digest=image.data_digest,
        matrix_digest=matrix.digest,
        status="pending",
        created_by=p.id,
    )
    db.add(job)
    db.flush()
    audit(db, p, "job.create", "allowed", resource=f"job:{job.id}",
          detail=f"image={image.data_digest[:12]} matrix={matrix.name}"
                 f"v{matrix.version} {matrix.digest[:12]} alg={body.algorithm}")
    db.commit()
    get_runner().submit_freeze(job.id)
    return _job_brief(job)


@router.get("")
def list_jobs(db: Session = Depends(get_db),
              p: Principal = Depends(resolve_principal)):
    jobs = db.scalars(select(Job).order_by(Job.id)).all()
    out = []
    for j in jobs:
        image = db.get(Image, j.image_id)
        if can_view_image(db, p, image):
            out.append(_job_brief(j))
    return out


@router.get("/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db),
            p: Principal = Depends(resolve_principal)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    image = db.get(Image, job.image_id)
    if not can_view_image(db, p, image):
        raise HTTPException(403, "no view permission on this image")
    brief = _job_brief(job)
    gens = []
    for g in sorted(job.generations, key=lambda g: g.gen_no):
        tiles = db.scalars(select(TileRecord).where(TileRecord.generation_id == g.id)).all()
        counts: dict[str, int] = {}
        for t in tiles:
            counts[t.state] = counts.get(t.state, 0) + 1
        gens.append({
            "id": g.id, "gen_no": g.gen_no, "state": g.state,
            "is_current": g.is_current, "tiles": counts,
            "total_tiles": len(tiles),
            "recovery_rmse": g.recovery_rmse,
            "reconstruction_rmse": g.reconstruction_rmse,
            "metrics": g.metrics,
        })
    brief["generations"] = gens
    brief["frozen"] = job.frozen_params
    return brief


@router.get("/{job_id}/generations/{gen_id}")
def get_generation(job_id: int, gen_id: int,
                   db: Session = Depends(get_db),
                   p: Principal = Depends(resolve_principal)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if not can_view_image(db, p, db.get(Image, job.image_id)):
        raise HTTPException(403, "no view permission on this image")
    gen = db.get(Generation, gen_id)
    if gen is None or gen.job_id != job_id:
        raise HTTPException(404, "generation not found")
    tiles = db.scalars(
        select(TileRecord).where(TileRecord.generation_id == gen.id)
        .order_by(TileRecord.level, TileRecord.tile_y, TileRecord.tile_x)
    ).all()
    return {
        "id": gen.id, "gen_no": gen.gen_no, "state": gen.state,
        "is_current": gen.is_current,
        "recovery_rmse": gen.recovery_rmse,
        "reconstruction_rmse": gen.reconstruction_rmse,
        "metrics": gen.metrics,
        "tiles": [{
            "level": t.level, "x": t.tile_x, "y": t.tile_y,
            "state": t.state, "attempts": t.attempts,
            "error_code": t.error_code,
            "keys": {
                "raw": _safe_json(t.raw_key),
                "components": _safe_json(t.comp_key),
                "residual": _safe_json(t.resid_key),
            },
        } for t in tiles],
    }


def _safe_json(value):
    import json

    try:
        return json.loads(value) if value else None
    except Exception:
        return None


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db),
               p: Principal = Depends(require_scope("job:cancel"))):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if not can_view_image(db, p, db.get(Image, job.image_id)):
        raise HTTPException(403, "no view permission on this image")
    pipeline.cancel_job(db, job, reason=f"canceled by {p.name}")
    audit(db, p, "job.cancel", "allowed", resource=f"job:{job_id}")
    db.commit()
    return _job_brief(db.get(Job, job_id))


@router.post("/{job_id}/retry")
def retry_job(job_id: int, db: Session = Depends(get_db),
              p: Principal = Depends(require_scope("job:create"))):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if not can_view_image(db, p, db.get(Image, job.image_id)):
        raise HTTPException(403, "no view permission on this image")
    try:
        gen = pipeline.retry_job(db, job, get_runner())
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit(db, p, "job.retry", "allowed", resource=f"job:{job_id}",
          detail=f"new generation {gen.id} (digests & frozen params retained)")
    return _job_brief(job) | {"new_generation_id": gen.id}
