from __future__ import annotations

import hashlib
import json

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import imaging, pipeline
from ..database import get_db
from ..models import ControlSample, MatrixVersion
from ..object_store import get_store, matrix_object_key
from ..schemas import ControlSampleIn, MatrixFromControlsIn, MatrixVersionIn
from ..security import Principal, audit, require_scope, resolve_principal
from ..tasks import get_runner

router = APIRouter(tags=["matrices"])


def matrix_digest(matrix: np.ndarray) -> str:
    # digest over the exact JSON-typed payload persisted in the DB row
    return hashlib.sha256(
        json.dumps(np.asarray(matrix, dtype=np.float64).tolist(),
                   sort_keys=True).encode()
    ).hexdigest()


def _store_matrix_object(matrix: np.ndarray, digest: str) -> None:
    """The matrix is an immutable object under a content key. A revision
    creates a NEW object; old bytes stay addressable by pinned jobs."""
    get_store().put(
        matrix_object_key(digest),
        imaging.save_npz(matrix=matrix.astype(np.float64),
                         digest=np.asarray(digest)),
        content_type="application/octet-stream",
    )


@router.post("/control-samples")
def add_control(body: ControlSampleIn, db: Session = Depends(get_db),
                p: Principal = Depends(require_scope("matrix:publish"))):
    if not body.spectrum or any(not np.isfinite(body.spectrum)):
        raise HTTPException(400, "spectrum must be a finite non-empty list")
    cs = ControlSample(name=body.name, fluorophore=body.fluorophore,
                       spectrum=[float(x) for x in body.spectrum],
                       created_by=p.id)
    db.add(cs)
    db.flush()
    audit(db, p, "control.create", "allowed",
          resource=f"control:{cs.id}", detail=body.fluorophore)
    db.commit()
    return {"id": cs.id}


@router.get("/control-samples")
def list_controls(db: Session = Depends(get_db),
                  p: Principal = Depends(resolve_principal)):
    rows = db.scalars(select(ControlSample).order_by(ControlSample.id)).all()
    return [{"id": c.id, "name": c.name, "fluorophore": c.fluorophore,
             "spectrum": c.spectrum} for c in rows]


@router.get("/matrices")
def list_matrices(db: Session = Depends(get_db),
                  p: Principal = Depends(resolve_principal)):
    rows = db.scalars(
        select(MatrixVersion).order_by(MatrixVersion.name, MatrixVersion.version)
    ).all()
    return [{
        "id": m.id, "name": m.name, "version": m.version,
        "digest": m.digest, "superseded": m.superseded,
        "note": m.note,
        "shape": [len(m.matrix), len(m.matrix[0])],
    } for m in rows]


@router.get("/matrices/{matrix_id}")
def get_matrix(matrix_id: int, db: Session = Depends(get_db),
               p: Principal = Depends(resolve_principal)):
    m = db.get(MatrixVersion, matrix_id)
    if m is None:
        raise HTTPException(404, "matrix not found")
    return {"id": m.id, "name": m.name, "version": m.version,
            "digest": m.digest, "matrix": m.matrix,
            "superseded": m.superseded, "note": m.note}


def _publish_matrix(db: Session, p: Principal, name: str,
                    matrix: np.ndarray, note: str | None) -> MatrixVersion:
    if matrix.ndim != 2:
        raise HTTPException(400, "matrix must be 2-D [channels, components]")
    matrix = matrix.astype(np.float64)
    if not np.isfinite(matrix).all():
        raise HTTPException(400, "matrix contains NaN/Inf")
    if (matrix < 0).any():
        raise HTTPException(400, "matrix must be non-negative")
    digest = matrix_digest(matrix)

    # no-op republish of the identical current version
    latest = db.scalars(
        select(MatrixVersion).where(MatrixVersion.name == name)
        .order_by(MatrixVersion.version.desc())
    ).first()
    if latest is not None and latest.digest == digest:
        return latest

    version_no = (latest.version + 1) if latest else 1
    _store_matrix_object(matrix, digest)
    mv = MatrixVersion(name=name, version=version_no, matrix=matrix.tolist(),
                       digest=digest, note=note, created_by=p.id)
    db.add(mv)
    if latest is not None:
        latest.superseded = True
    db.flush()

    # The critical invalidation step: fence off running jobs of the OLD
    # generation and launch successors pinned to the new digest.
    successor_ids = []
    try:
        successor_ids = pipeline.supersede_for_matrix(db, mv.id, get_runner(), p.id)
    except Exception as exc:  # pragma: no cover - defensive
        audit(db, p, "matrix.publish", "error", resource=f"matrix:{name}",
              detail=f"supersede failed: {exc}")
        raise

    audit(db, p, "matrix.publish", "allowed", resource=f"matrix:{name}",
          detail=f"v{version_no} digest={digest[:16]} successors={successor_ids}")
    db.commit()
    return mv


@router.post("/matrices")
def publish_matrix(body: MatrixVersionIn, db: Session = Depends(get_db),
                   p: Principal = Depends(require_scope("matrix:publish"))):
    if not body.matrix:
        raise HTTPException(400, "matrix rows required")
    mv = _publish_matrix(db, p, body.name, np.asarray(body.matrix, dtype=np.float64),
                         body.note)
    return {"id": mv.id, "name": mv.name, "version": mv.version,
            "digest": mv.digest}


@router.post("/matrices/from-controls")
def publish_matrix_from_controls(body: MatrixFromControlsIn,
                                 db: Session = Depends(get_db),
                                 p: Principal = Depends(require_scope("matrix:publish"))):
    samples = db.scalars(select(ControlSample).order_by(ControlSample.id)).all()
    if not samples:
        raise HTTPException(400, "no control samples registered")
    cols = []
    names = []
    for s in samples:
        if len(s.spectrum) != body.channels:
            raise HTTPException(
                400,
                f"control {s.fluorophore!r} has {len(s.spectrum)} channels, "
                f"expected {body.channels}",
            )
        cols.append(s.spectrum)
        names.append(s.fluorophore)
    matrix = np.asarray(cols, dtype=np.float64).T  # (C, K)
    mv = _publish_matrix(db, p, body.name, matrix,
                         body.note or f"assembled from {len(cols)} controls")
    return {"id": mv.id, "name": mv.name, "version": mv.version,
            "digest": mv.digest, "fluorophores": names}
