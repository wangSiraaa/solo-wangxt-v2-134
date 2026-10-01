from __future__ import annotations

from typing import Any
from sqlalchemy.orm import Session

from . import imaging
from .config import Settings, get_settings
from .models import Image, JobGeneration, MatrixVersion, TileTask, UnmixJob, utcnow
from .services import finish_if_complete
from .storage import ObjectStore


class TaskRejected(Exception):
    """The task belongs to a stale/cancelled generation and must not compute."""


def load_manifest(store: ObjectStore, image: Image) -> dict[str, Any]:
    return store.get_json(image.manifest_object_key)


def source_tile_for(manifest: dict[str, Any], level: int, x: int, y: int) -> dict[str, Any]:
    for tile in manifest["tiles"]:
        if tile["level"] == level and tile["x"] == x and tile["y"] == y:
            return tile
    raise imaging.UnmixValidationError(
        "corrupt_tile", f"manifest has no source tile l={level},x={x},y={y}"
    )


def process_tile(
    db: Session,
    store: ObjectStore,
    task_id: str,
    settings: Settings | None = None,
) -> TileTask:
    settings = settings or get_settings()
    task = db.get(TileTask, task_id, with_for_update=True)
    if not task:
        raise TaskRejected("task not found")
    job = db.get(UnmixJob, task.job_id, with_for_update=True)
    gen = db.get(JobGeneration, task.generation_id, with_for_update=True)

    # A late worker must never write output into a newer generation. Its row
    # is preserved as an audit trail but excluded from active completeness.
    if gen.generation_no != job.current_generation_no or gen.status != "active":
        task.status = "stale"
        task.finished_at = utcnow()
        db.commit()
        db.refresh(task)
        return task
    if job.cancel_requested:
        task.status = "cancelled"
        task.finished_at = utcnow()
        db.commit()
        db.refresh(task)
        return task
    if task.status == "succeeded":
        return task

    task.attempts += 1
    task.status = "running"
    task.started_at = utcnow()
    db.flush()

    try:
        image = db.get(Image, job.image_id)
        matrix = db.get(MatrixVersion, job.matrix_version_id)
        if image.source_digest != job.image_digest or matrix.coefficient_digest != job.matrix_digest:
            raise imaging.UnmixValidationError("digest_mismatch", "job inputs changed after start")
        a, _ = imaging.validate_for_image(
            {"coefficients": matrix.coefficients, "channel_names": matrix.channel_names},
            {"channel_names": image.channel_names},
        )
        manifest = load_manifest(store, image)
        source_ref = source_tile_for(manifest, task.level, task.x, task.y)
        source = imaging.decode_source_tile(store.get(source_ref["object_key"]))
        result = imaging.unmix_tile(
            source,
            a,
            job.frozen_params,
            algorithm=job.algorithm,
            ridge=float(job.algorithm_params.get("ridge", 0.0)),
        )

        if task.kind == "component":
            data = _npz_parts(components=result.components, reconstruction=result.reconstruction)
            prefix = "derived/components"
        elif task.kind == "residual":
            data = _npz_parts(residual=result.residual, reconstruction=result.reconstruction)
            prefix = "derived/residual"
        else:
            raise imaging.UnmixValidationError("unknown_tile_kind", task.kind)

        object_key, digest = store.put_content_addressed(prefix, data, ".npz")
        task.status = "succeeded"
        task.object_key = object_key
        task.object_digest = digest
        task.error_code = None
        task.error_detail = None
        task.quality_flags = {
            **result.quality_flags,
            "tile_rmse": result.stats["rmse"],
            "tile_max_abs_residual": result.stats["max_abs_residual"],
            "component_mass": result.stats["component_mass"],
        }
        task.finished_at = utcnow()
    except imaging.UnmixValidationError as exc:
        task.error_code = exc.code
        task.error_detail = exc.detail
        task.finished_at = utcnow()
        if exc.code in {"corrupt_tile", "missing_channel"} or task.attempts >= settings.max_tile_attempts:
            task.status = "failed"
        else:
            task.status = "pending"
    except Exception as exc:  # storage/network failures are retryable then failed
        task.error_code = "tile_processing_error"
        task.error_detail = str(exc)
        task.finished_at = utcnow()
        task.status = "failed" if task.attempts >= settings.max_tile_attempts else "pending"

    db.commit()
    db.refresh(task)
    finish_if_complete(db, job.id)
    return task


def _npz_parts(**arrays) -> bytes:
    import io
    import numpy as np

    buf = io.BytesIO()
    np.savez_compressed(buf, **{k: v.astype(np.float32) for k, v in arrays.items()})
    return buf.getvalue()
