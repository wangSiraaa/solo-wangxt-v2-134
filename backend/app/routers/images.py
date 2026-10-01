from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import imaging
from ..database import get_db
from ..models import Image, ImageGrant, User
from ..object_store import get_store, ground_truth_key, raw_image_key, sha256_hex
from ..schemas import GrantIn, SyntheticImageIn
from ..security import (
    Principal,
    audit,
    can_view_image,
    require_scope,
    resolve_principal,
)

router = APIRouter(prefix="/images", tags=["images"])


def _store_image(db: Session, p: Principal, arr: np.ndarray, name: str,
                 ground_truth: dict | None = None) -> Image:
    if arr.ndim != 3 or not np.issubdtype(arr.dtype, np.integer):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "image must be a 3-D HWC integer array")
    blob = imaging.save_npy(arr)
    digest = sha256_hex(blob)
    # content-addressed => identical re-upload returns the same image row;
    # different bytes can never land under an existing key
    existing = db.scalar(select(Image).where(Image.data_digest == digest))
    if existing is not None:
        return existing
    key = raw_image_key(digest)
    get_store().put(key, blob, content_type="application/octet-stream")
    gt_payload = None
    if ground_truth is not None:
        gtkey = ground_truth_key(digest)
        get_store().put(gtkey, imaging.save_npz(**ground_truth),
                        content_type="application/octet-stream")
        gt_payload = {"key": gtkey, "components": int(ground_truth["truth"].shape[-1])}
    image = Image(
        name=name, width=arr.shape[1], height=arr.shape[0],
        channels=arr.shape[2], dtype=str(arr.dtype),
        data_digest=digest, object_key=key, uploaded_by=p.id,
        synthetic_ground_truth=gt_payload,
    )
    db.add(image)
    db.flush()
    audit(db, p, "image.upload", "allowed", resource=f"image:{image.id}",
          detail=f"digest={digest[:16]} shape={arr.shape}")
    db.commit()
    db.refresh(image)
    return image


@router.post("/synthetic")
def create_synthetic(body: SyntheticImageIn,
                     db: Session = Depends(get_db),
                     p: Principal = Depends(require_scope("image:upload"))):
    if body.components > body.channels:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "components must not exceed channels")
    observed, truth, M, _masks = imaging.make_synthetic_image(
        h=body.height, w=body.width, channels=body.channels,
        components=body.components, seed=body.seed,
    )
    image = _store_image(db, p, observed, body.name, ground_truth={"truth": truth})
    return {
        "id": image.id, "digest": image.data_digest,
        "width": image.width, "height": image.height,
        "channels": image.channels,
        "ground_truth_matrix": M.tolist(),
        "note": "known-composition validation image; truth retained in store",
    }


@router.post("/upload")
async def upload_image(name: str = "upload",
                       file: UploadFile = File(...),
                       db: Session = Depends(get_db),
                       p: Principal = Depends(require_scope("image:upload"))):
    data = await file.read()
    try:
        arr = imaging.load_npy(data)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"not a NumPy .npy array: {exc}")
    image = _store_image(db, p, arr, name)
    return {"id": image.id, "digest": image.data_digest,
            "width": image.width, "height": image.height,
            "channels": image.channels}


@router.get("")
def list_images(db: Session = Depends(get_db),
                p: Principal = Depends(resolve_principal)):
    images = db.scalars(select(Image).order_by(Image.id)).all()
    out = []
    for im in images:
        grant = db.scalar(select(ImageGrant).where(
            ImageGrant.image_id == im.id, ImageGrant.user_id == p.id))
        visible = can_view_image(db, p, im)
        out.append({
            "id": im.id, "name": im.name, "width": im.width,
            "height": im.height, "channels": im.channels,
            "digest": im.data_digest,
            "has_ground_truth": im.synthetic_ground_truth is not None,
            "can_view": visible,
            "can_publish": bool(grant and grant.can_publish),
        })
    return out


@router.get("/{image_id}")
def get_image(image_id: int, db: Session = Depends(get_db),
              p: Principal = Depends(resolve_principal)):
    im = db.get(Image, image_id)
    if im is None:
        raise HTTPException(404, "image not found")
    if not can_view_image(db, p, im):
        audit(db, p, "image.view", "denied", resource=f"image:{image_id}")
        db.commit()
        raise HTTPException(403, "no view permission on this image")
    return {
        "id": im.id, "name": im.name, "width": im.width, "height": im.height,
        "channels": im.channels, "digest": im.data_digest,
        "synthetic": im.synthetic_ground_truth is not None,
    }


@router.put("/{image_id}/grants", status_code=200)
def put_grant(image_id: int, body: GrantIn, db: Session = Depends(get_db),
              p: Principal = Depends(require_scope("grant:manage"))):
    im = db.get(Image, image_id)
    if im is None:
        raise HTTPException(404, "image not found")
    if db.get(User, body.user_id) is None:
        raise HTTPException(404, "user not found")
    grant = db.scalar(select(ImageGrant).where(
        ImageGrant.image_id == image_id, ImageGrant.user_id == body.user_id))
    if grant is None:
        grant = ImageGrant(image_id=image_id, user_id=body.user_id,
                           can_view=body.can_view, can_publish=body.can_publish)
        db.add(grant)
    else:
        grant.can_view = body.can_view
        grant.can_publish = body.can_publish
    audit(db, p, "image.grant", "allowed", resource=f"image:{image_id}",
          detail=f"user={body.user_id} view={body.can_view} publish={body.can_publish}")
    db.commit()
    return {"ok": True}
