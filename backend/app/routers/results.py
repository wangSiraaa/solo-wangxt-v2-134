from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Image, Job, ResultVersion
from ..object_store import get_store
from ..release import ReleaseError, release_result
from ..schemas import ReleaseIn
from ..security import (
    Principal,
    audit,
    can_publish_image,
    can_view_image,
    resolve_principal,
)

router = APIRouter(prefix="/results", tags=["results"])


def _brief(r: ResultVersion) -> dict:
    return {
        "id": r.id, "job_id": r.job_id, "generation_id": r.generation_id,
        "is_current": r.is_current, "report_digest": r.report_digest,
        "report_object_key": r.report_object_key,
        "recovery_rmse": r.recovery_rmse,
        "reconstruction_rmse": r.reconstruction_rmse,
    }


@router.post("/release")
def release(body: ReleaseIn, db: Session = Depends(get_db),
            p: Principal = Depends(resolve_principal)):
    """
    Publishing requires BOTH the result:publish capability AND a per-image
    publish grant. Image-view rights are deliberately insufficient.
    """
    job = db.get(Job, body.job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    image = db.get(Image, job.image_id)

    if not can_view_image(db, p, image):
        audit(db, p, "result.release", "denied", resource=f"job:{job.id}",
              detail="no image access")
        db.commit()
        raise HTTPException(403, "no access to this image")
    if "result:publish" not in p.scopes and "*" not in p.scopes:
        audit(db, p, "result.release", "denied", resource=f"job:{job.id}",
              detail=f"principal {p.name} lacks result:publish")
        db.commit()
        raise HTTPException(403, "missing capability: result:publish")
    if not can_publish_image(db, p, image):
        audit(db, p, "result.release", "denied", resource=f"job:{job.id}",
              detail=f"principal {p.name} not publish-granted on image {image.id}")
        db.commit()
        raise HTTPException(
            403, "viewing this image does not authorize publishing its results",
        )
    try:
        result = release_result(db, p, job.id)
    except ReleaseError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "detail": exc.detail})
    return _brief(result) | {"tiles": len(result.manifest["tiles"])}


@router.get("")
def list_results(db: Session = Depends(get_db),
                 p: Principal = Depends(resolve_principal)):
    rows = db.scalars(select(ResultVersion).order_by(ResultVersion.id)).all()
    out = []
    for r in rows:
        image = db.get(Image, db.get(Job, r.job_id).image_id)
        if can_view_image(db, p, image):
            out.append(_brief(r))
    return out


@router.get("/{result_id}")
def get_result(result_id: int, include_report: bool = False,
               db: Session = Depends(get_db),
               p: Principal = Depends(resolve_principal)):
    r = db.get(ResultVersion, result_id)
    if r is None:
        raise HTTPException(404, "result not found")
    job = db.get(Job, r.job_id)
    image = db.get(Image, job.image_id)
    if not can_view_image(db, p, image):
        raise HTTPException(403, "no access to this image")
    body = _brief(r)
    body["manifest_summary"] = {
        "image": r.manifest["image"], "matrix": r.manifest["matrix"],
        "algorithm": r.manifest["algorithm"],
        "frozen_params_hash": r.manifest["frozen_params_hash"],
        "metrics": r.manifest["metrics"],
        "n_levels": r.manifest["n_levels"],
        "tile_counts_per_level": r.manifest["levels"],
        "tile_count": len(r.manifest["tiles"]),
    }
    if include_report:
        body["report"] = get_store().get(r.report_object_key).decode()
    return body
