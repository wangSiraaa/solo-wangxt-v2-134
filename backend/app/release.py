"""
Result release.

The release gate (requirement: "正式发布必须确认所有必需切片来自同一代次"):

* the job's CURRENT generation must be in state `completed` — every required
  tile of every pyramid level is `done`, none failed or abandoned;
* every tile object listed in the manifest is verified to exist under keys
  pinned to the SAME image digest, matrix digest, algorithm and generation;
* the report itself is a content-addressed object — a failed/aborted release
  therefore cannot overwrite any prior report (the current pointer moves only
  in the final commit);
* two people racing to release the same job are serialized by a row lock; the
  loser receives 409 and the attempt is audited.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    Generation,
    Image,
    Job,
    MatrixVersion,
    ResultVersion,
    TileRecord,
)
from .object_store import get_store, report_key, sha256_hex
from .security import Principal, audit


class ReleaseError(Exception):
    def __init__(self, code: str, detail: str, status: int = 409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def _verify_manifest(store, job: Job, gen: Generation,
                     tiles: list[TileRecord]) -> dict:
    expected_counts: dict[int, int] = {}
    manifest_tiles = []
    for t in tiles:
        if t.state != "done":
            raise ReleaseError(
                "generation_not_complete",
                f"tile L{t.level}/({t.tile_x},{t.tile_y}) is {t.state}; "
                "all required tiles of one generation must be done",
            )
        for field in ("raw_key", "comp_key", "resid_key"):
            keys = json.loads(getattr(t, field))
            for k in keys:
                # every object must live in this exact cache namespace
                expected = (
                    f"/img-{job.image_digest[:16]}/mat-{job.matrix_digest[:16]}"
                    f"/{job.algorithm}/gen{gen.id:06d}/L{t.level}/"
                )
                if expected not in f"/{k}":
                    raise ReleaseError(
                        "cross_generation_tile",
                        f"tile object {k} is not from generation {gen.id}",
                    )
                if not store.exists(k):
                    raise ReleaseError(
                        "tile_object_missing",
                        f"declared tile object missing from store: {k}",
                    )
        expected_counts[t.level] = expected_counts.get(t.level, 0) + 1
        manifest_tiles.append({
            "level": t.level, "x": t.tile_x, "y": t.tile_y,
            "raw": json.loads(t.raw_key),
            "components": json.loads(t.comp_key),
            "residual": json.loads(t.resid_key),
            "stats": t.tile_stats,
        })

    return {"levels": expected_counts, "tiles": manifest_tiles}


def release_result(db: Session, principal: Principal, job_id: int) -> ResultVersion:
    store = get_store()
    # Serialize concurrent releases of the same job.
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise ReleaseError("job_not_found", f"job {job_id} does not exist", 404)

    image = db.get(Image, job.image_id)
    matrix = db.get(MatrixVersion, job.matrix_version_id)

    existing = db.scalar(select(ResultVersion).where(ResultVersion.job_id == job.id))
    if existing is not None:
        audit(db, principal, "result.release", "denied",
              resource=f"job:{job.id}", detail="already released")
        raise ReleaseError("already_released",
                           f"job {job.id} already has release {existing.id}")

    if job.status == "superseded":
        audit(db, principal, "result.release", "denied",
              resource=f"job:{job.id}", detail="job superseded by matrix revision")
        raise ReleaseError("job_superseded",
                           "matrix changed during/after compute; only a successor job "
                           "pinned to the new matrix can be released")

    gen = db.scalar(
        select(Generation).where(
            Generation.job_id == job.id, Generation.is_current.is_(True)
        ).with_for_update()
    )
    if gen is None:
        raise ReleaseError("no_generation", "job has no current generation")
    if gen.state != "completed":
        audit(db, principal, "result.release", "denied",
              resource=f"job:{job.id}",
              detail=f"generation state={gen.state}, partial preview only")
        raise ReleaseError(
            "generation_not_complete",
            f"current generation is {gen.state}; a partial result may be previewed "
            "but not released",
        )

    tiles = list(db.scalars(
        select(TileRecord).where(TileRecord.generation_id == gen.id)
    ).all())

    # dimensional completeness check
    from . import imaging
    from .config import get_settings

    ts = get_settings().tile_size
    n_levels = imaging.num_levels(image.height, image.width, ts)
    counts: dict[int, int] = {}
    for t in tiles:
        if t.state != "done":
            raise ReleaseError("generation_not_complete",
                               f"tile L{t.level} is {t.state}")
        counts[t.level] = counts.get(t.level, 0) + 1
    if set(counts) != set(range(n_levels)):
        raise ReleaseError("missing_pyramid_levels",
                           f"have levels {sorted(counts)}, need 0..{n_levels - 1}")
    for level in range(n_levels):
        lh, lw = imaging.level_dimensions(image.height, image.width, level)
        nx, ny = imaging.grid_for_level(lh, lw, ts)
        if counts[level] != nx * ny:
            raise ReleaseError(
                "incomplete_tile_set",
                f"level {level}: {counts[level]}/{nx * ny} tiles present",
            )

    manifest = _verify_manifest(store, job, gen, tiles)
    manifest["n_levels"] = n_levels
    manifest["tile_size"] = ts
    manifest["image"] = {
        "id": image.id, "digest": job.image_digest,
        "width": image.width, "height": image.height,
        "channels": image.channels,
    }
    manifest["matrix"] = {
        "id": matrix.id, "name": matrix.name, "version": matrix.version,
        "digest": job.matrix_digest,
    }
    manifest["algorithm"] = job.algorithm
    manifest["frozen_params_hash"] = job.params_hash
    manifest["metrics"] = gen.metrics or {}
    manifest["generation_id"] = gen.id

    report_body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    report_digest = sha256_hex(report_body)
    rkey = report_key(job.image_digest, job.matrix_digest, job.algorithm, report_digest)
    # Content-addressed: same report -> idempotent, different -> rejected if key
    # ever collided. No old report object is ever overwritten.
    store.put(rkey, report_body, content_type="application/json")

    result = ResultVersion(
        job_id=job.id,
        generation_id=gen.id,
        is_current=True,
        manifest=manifest,
        recovery_rmse=gen.recovery_rmse,
        reconstruction_rmse=gen.reconstruction_rmse,
        report_object_key=rkey,
        report_digest=report_digest,
        created_by=principal.id,
    )
    db.add(result)
    audit(db, principal, "result.release", "allowed",
          resource=f"job:{job.id}",
          detail=f"release gen={gen.id} tiles={len(tiles)} "
                 f"recovery_rmse={gen.recovery_rmse} recon_rmse={gen.reconstruction_rmse}")
    try:
        db.commit()
    except IntegrityError:
        # Losing the concurrent-publish race on the one-release-per-job key.
        db.rollback()
        audit(db, principal, "result.release", "denied",
              resource=f"job:{job.id}", detail="lost concurrent publish race")
        db.commit()
        raise ReleaseError("already_released",
                           "another release for this job committed first")
    return result
