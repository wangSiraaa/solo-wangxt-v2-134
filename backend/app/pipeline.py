"""
Job orchestration: freeze -> fan-out per tile -> finalize.

Guarantees enforced in this module
-----------------------------------
1. Job creation PINS image and matrix content digests; the pipeline always
   loads the immutable objects by digest, never "the current matrix".
2. Whole-image statistics are estimated exactly once (freeze stage) and
   content-hashed. Tiles verify the hash and solve per-pixel only.
3. Every matrix revision or retry starts a new GENERATION. Late results of
   non-current generations are marked abandoned and cannot count toward
   completion or be released.
4. Tiles may fail independently and are retried a bounded number of times;
   a generation can complete PARTIAL (explicitly flagged) and partial
   generations can never be published.
5. Original images and matrix objects are content-addressed and immutable;
   reports are likewise content-addressed and the 'current' pointer moves in
   one DB transaction, so failure can never overwrite an image or an old
   report.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import imaging, unmix
from .config import get_settings
from .models import Generation, Image, Job, MatrixVersion, TileRecord, utcnow
from .object_store import (
    ObjectStore,
    get_store,
    matrix_object_key,
    put_idempotent,
    raw_image_key,
    raw_pyramid_key,
    sha256_hex,
    tile_cache_key,
)
from .unmix import UnmixingError

# ---------------------------------------------------------------------------
# Test fault-injection hooks (never used by production code paths)
# ---------------------------------------------------------------------------


@dataclass
class FaultHooks:
    # called once before each tile attempt with (job_id, gen_id, level, x, y, attempt)
    tile_fail: object = None  # callable returning error_code str or None
    # delay tile start to widen cancellation/supersede windows in tests
    tile_delay: float = 0.0
    # fail the freeze stage with this error code, once
    freeze_fail_once: str | None = None


_HOOKS = FaultHooks()
_HOOKS_LOCK = threading.Lock()


def set_fault_hooks(**kw) -> None:
    global _HOOKS
    with _HOOKS_LOCK:
        _HOOKS = FaultHooks(**{**_HOOKS.__dict__, **kw})


def reset_fault_hooks() -> None:
    global _HOOKS
    with _HOOKS_LOCK:
        _HOOKS = FaultHooks()


def _hook_failure(job_id, gen_id, level, x, y, attempt) -> str | None:
    fn = _HOOKS.tile_fail
    if fn is None:
        return None
    return fn(job_id, gen_id, level, x, y, attempt)


# ---------------------------------------------------------------------------
# Immutable object loading + pyramid cache
# ---------------------------------------------------------------------------

_pyramid_cache: dict[str, list[np.ndarray]] = {}
_pyramid_lock = threading.Lock()


def load_image_array(image: Image, store: ObjectStore) -> np.ndarray:
    raw = store.get(raw_image_key(image.data_digest))
    arr = imaging.load_npy(raw)
    # pinning guard: bytes fetched under digest must actually hash to it
    if sha256_hex(raw) != image.data_digest:
        raise UnmixingError(
            "digest_mismatch", "stored original image bytes do not match pinned digest"
        )
    return arr


def load_matrix(matrix: MatrixVersion, store: ObjectStore) -> np.ndarray:
    key = matrix_object_key(matrix.digest)
    if not store.exists(key):
        # self-heal: object store missing (fresh volume) but immutable DB row
        # present — re-materialize the exact content-addressed bytes
        M0 = np.asarray(matrix.matrix, dtype=np.float64)
        store.put(key, imaging.save_npz(matrix=M0, digest=np.asarray(matrix.digest)),
                  content_type="application/octet-stream")
    raw = store.get(key)
    payload = imaging.load_npz(raw)
    M = payload["matrix"]
    if str(payload["digest"].item()) != matrix.digest:
        raise UnmixingError(
            "digest_mismatch", "stored matrix bytes do not match pinned digest"
        )
    return M


def get_pyramid(image: Image, store: ObjectStore) -> list[np.ndarray]:
    """Content-addressed pyramid; computed once per image digest per worker."""
    with _pyramid_lock:
        cached = _pyramid_cache.get(image.data_digest)
    if cached is not None:
        return cached

    key = raw_pyramid_key(image.data_digest)
    if store.exists(key):
        levels = load_pyramid_npz(store.get(key))
    else:
        base = load_image_array(image, store)
        levels = [base]
        while levels[-1].shape[0] > 256 or levels[-1].shape[1] > 256:
            levels.append(imaging.downsample2(levels[-1]))
        # store (immutable; key is derived from the image content digest)
        store.put(key, build_pyramid_npz(levels))
    with _pyramid_lock:
        _pyramid_cache[image.data_digest] = levels
    return levels


def build_pyramid_npz(levels: list[np.ndarray]) -> bytes:
    return imaging.save_npz(**{f"lvl{i}": lv for i, lv in enumerate(levels)})


def load_pyramid_npz(data: bytes) -> list[np.ndarray]:
    z = imaging.load_npz(data)
    keys = sorted(z.keys(), key=lambda k: int(k[3:]))
    return [z[k] for k in keys]


# ---------------------------------------------------------------------------
# Rendering of the three coordinated views
# ---------------------------------------------------------------------------


def _gray_pngs(arr2d: np.ndarray, scale: float) -> bytes:
    view = imaging.normalize_for_view(arr2d, 255.0 / scale)
    return imaging.encode_gray_png(view)


def render_tile_views(raw_tile: np.ndarray, result: dict, frozen: dict) -> dict:
    """raw: per-channel PNGs; components: per-component PNGs; residual: |r| +
    per-channel signed residual PNGs."""
    comp = result["components"]
    resid = result["residual"]
    d = frozen["display"]
    raw_png = [_gray_pngs(raw_tile[..., c], d["raw_scale"][c])
               for c in range(raw_tile.shape[2])]
    comp_png = [_gray_pngs(comp[..., k], d["comp_scale"][k])
                for k in range(comp.shape[2])]
    resid_abs = np.sqrt(np.mean(resid.astype(np.float32) ** 2, axis=-1))
    resid_png = [_gray_pngs(resid_abs, d["resid_scale"])]
    # signed per-channel residuals: offset to 128 with the same global scale
    for c in range(resid.shape[2]):
        r = resid[..., c].astype(np.float32)
        view = np.clip(128.0 + r * (127.0 / d["resid_scale"]), 0, 255).astype(np.uint8)
        resid_png.append(imaging.encode_gray_png(view))
    return {"raw": raw_png, "components": comp_png, "residual": resid_png}


# ---------------------------------------------------------------------------
# Freeze stage
# ---------------------------------------------------------------------------


def freeze_and_dispatch(job_id: int, runner) -> None:
    """
    Stage 1: load pinned objects, validate, freeze whole-image parameters,
    create the current generation + tile records, then fan tile tasks out.
    Idempotent: safe to retry (resumes the existing current generation).
    """
    settings = get_settings()
    store = get_store()
    db = runner.session_factory()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        if job.status in {"canceled", "superseded", "completed", "partial", "failed"}:
            return

        # Resume existing current generation if freeze already succeeded.
        # NOTE: on resume we do NOT re-fan pending tiles — a tile in pending
        # is already owned by a scheduled attempt (initial dispatch or a
        # bounded retry). Re-dispatching would run the same tile twice.
        gen = db.scalar(
            select(Generation).where(
                Generation.job_id == job.id, Generation.is_current.is_(True)
            )
        )
        if gen is not None:
            return

        if _HOOKS.freeze_fail_once:
            code = _HOOKS.freeze_fail_once
            job.status = "failed"
            job.error_code = code
            job.error_detail = "injected freeze failure"
            db.commit()
            set_fault_hooks(freeze_fail_once=None)
            return
        image = db.get(Image, job.image_id)
        matrix = db.get(MatrixVersion, job.matrix_version_id)
        arr = load_image_array(image, store)
        M = load_matrix(matrix, store)
        # pinned-digest guard (the loaders already verify stored bytes;
        # this makes the pinning contract explicit at freeze time)
        if image.data_digest != job.image_digest or matrix.digest != job.matrix_digest:
            job.status = "failed"
            job.error_code = "digest_drift"
            job.error_detail = "pinned digest no longer resolves to the same object"
            db.commit()
            return
        try:
            frozen = unmix.estimate_frozen_params(arr, M, job.algorithm)
        except UnmixingError as exc:
            job.status = "failed"
            job.error_code = exc.code
            job.error_detail = exc.detail
            db.commit()
            return

        # Second fence: cancel/supersede may have landed WHILE the whole-image
        # statistics were being estimated (this stage can be slow on a 20k px
        # image). Never create a fresh running generation on a dead job.
        db.refresh(job)
        if job.status in {"canceled", "superseded", "completed", "partial",
                          "failed"}:
            return

        frozen["matrix_issues"] = unmix.validate_matrix(M, arr.shape[2])
        frozen["hash"] = unmix.params_hash(frozen)
        job.frozen_params = frozen
        job.params_hash = frozen["hash"]
        job.status = "running"

        gen = Generation(job_id=job.id, gen_no=1, state="running")
        db.add(gen)
        db.flush()

        tile_size = settings.tile_size
        n_levels = imaging.num_levels(arr.shape[0], arr.shape[1], tile_size)
        records = []
        for level in range(n_levels):
            lh, lw = imaging.level_dimensions(arr.shape[0], arr.shape[1], level)
            nx, ny = imaging.grid_for_level(lh, lw, tile_size)
            for ty in range(ny):
                for tx in range(nx):
                    records.append(
                        TileRecord(
                            generation_id=gen.id, level=level,
                            tile_x=tx, tile_y=ty, state="pending",
                        )
                    )
        db.add_all(records)
        db.commit()

        # single dispatch wave for the freshly created generation
        for tr in records:
            runner.submit_tile(job.id, gen.id, tr.id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tile stage
# ---------------------------------------------------------------------------

TERMINAL_TILE = {"done", "failed", "abandoned"}


def process_tile(job_id: int, generation_id: int, tile_id: int, runner) -> None:
    settings = get_settings()
    store = get_store()
    db = runner.session_factory()
    try:
        tile = db.get(TileRecord, tile_id)
        if tile is None or tile.state in TERMINAL_TILE:
            return
        gen = db.get(Generation, generation_id)
        job = db.get(Job, job_id)
        # Generational fence: anything arriving for a generation that is no
        # longer current is abandoned on the spot (late result -> dead gen).
        if gen is None or not gen.is_current or job is None or job.status in {
            "canceled", "superseded",
        }:
            _abandon_tile(db, tile, "late result for abandoned generation")
            db.commit()
            return

        # Atomic claim: exactly one attempt may move a tile pending->running.
        # A duplicate delivery (broker redelivery, overlapping retry) loses
        # the claim and returns instead of recomputing the same block.
        claimed = db.execute(
            TileRecord.__table__.update()
            .where(TileRecord.id == tile_id, TileRecord.state == "pending")
            .values(state="running", attempts=TileRecord.attempts + 1)
        ).rowcount
        db.commit()
        if not claimed:
            return
        db.expire_all()  # bypass stale identity map (expire_on_commit=False)
        tile = db.get(TileRecord, tile_id)
        attempt = tile.attempts

        if _HOOKS.tile_delay:
            time.sleep(_HOOKS.tile_delay)
            # re-check the fence after the wait
            tile = db.get(TileRecord, tile_id)
            gen = db.get(Generation, generation_id)
            job = db.get(Job, job_id)
            if tile.state in TERMINAL_TILE:
                return
            if not gen.is_current or job.status in {"canceled", "superseded"}:
                _abandon_tile(db, tile, "abandoned while waiting")
                db.commit()
                return

        try:
            injected = _hook_failure(job_id, generation_id, tile.level,
                                     tile.tile_x, tile.tile_y, attempt)
            if injected:
                raise UnmixingError(injected, "injected tile fault")
            _compute_and_store(db, store, job, gen, tile, runner)
            return
        except UnmixingError as exc:
            code, detail = exc.code, exc.detail
        except Exception as exc:  # one corrupt block never kills the job
            code, detail = "tile_corrupt", f"{type(exc).__name__}: {exc}"

        # failure bookkeeping: bounded retries, then a permanent per-tile fail
        tile = db.get(TileRecord, tile_id)
        tile.error_code = code
        tile.last_error = detail[:500]
        if attempt >= settings.max_tile_attempts:
            tile.state = "failed"
            db.commit()
            runner.submit_finalize(job_id, generation_id)
            return
        tile.state = "pending"
        db.commit()
        runner.submit_tile(job_id, generation_id, tile_id, delay=min(2 ** attempt, 10))
    finally:
        db.close()


def _abandon_tile(db: Session, tile: TileRecord, reason: str) -> None:
    tile.state = "abandoned"
    tile.last_error = reason[:500]
    tile.error_code = "generation_abandoned"


def _compute_and_store(db: Session, store: ObjectStore, job: Job,
                       gen: Generation, tile: TileRecord, runner) -> None:
    # Final fence immediately before producing bytes: a cancel/revision that
    # landed during the per-tile solve must not receive stored output.
    db.refresh(job)
    db.refresh(gen)
    if not gen.is_current or job.status in {"canceled", "superseded"}:
        _abandon_tile(db, tile, "abandoned immediately before store")
        db.commit()
        return
    image = db.get(Image, job.image_id)
    matrix = db.get(MatrixVersion, job.matrix_version_id)
    frozen = job.frozen_params
    if not frozen or job.params_hash != frozen.get("hash"):
        raise UnmixingError("frozen_params_tampered",
                            "job is missing verified whole-image parameters")
    M = load_matrix(matrix, store)
    levels = get_pyramid(image, store)
    if tile.level >= len(levels):
        raise UnmixingError("tile_corrupt",
                            f"level {tile.level} missing from pyramid")
    level_arr = levels[tile.level]
    ts = get_settings().tile_size
    raw_tile = imaging.crop_tile(None, level_arr, tile.tile_x, tile.tile_y, ts)

    # Unmix at EVERY level against the SAME frozen global model (background,
    # noise, scales come from the freeze stage — never re-estimated here).
    result = unmix.unmix_tile(raw_tile, M, frozen)
    views = render_tile_views(raw_tile, result, frozen)

    keys = {"raw": [], "components": [], "residual": []}
    for kind, pngs in (("raw", views["raw"]),
                       ("components", views["components"]),
                       ("residual", views["residual"])):
        for idx, png in enumerate(pngs):
            view = f"{kind}{idx}"
            key = tile_cache_key(
                job.image_digest, job.matrix_digest, job.algorithm,
                gen.id, tile.level, tile.tile_x, tile.tile_y, view,
            )
            # idempotent: a retry after a lost DB commit re-encounters the
            # same bytes and proceeds; different bytes under the key fails.
            put_idempotent(store, key, png, content_type="image/png")
            keys[kind].append(key)

    tile.raw_key = json.dumps(keys["raw"])
    tile.comp_key = json.dumps(keys["components"])
    tile.resid_key = json.dumps(keys["residual"])
    tile.tile_stats = result["stats"]
    tile.last_error = None
    tile.state = "done"
    db.commit()
    # trigger a finalize attempt (cheap; only acts when all tiles settled)
    runner.submit_finalize(job.id, gen.id)


# ---------------------------------------------------------------------------
# Finalize stage
# ---------------------------------------------------------------------------


def finalize_generation(job_id: int, generation_id: int, runner) -> None:
    db = runner.session_factory()
    try:
        gen = db.get(Generation, generation_id)
        job = db.get(Job, job_id)
        if gen is None or job is None or not gen.is_current:
            return
        tiles = db.scalars(
            select(TileRecord).where(TileRecord.generation_id == gen.id)
        ).all()
        outstanding = [t for t in tiles if t.state in {"pending", "running"}]
        if outstanding:
            return  # still in flight

        failed = [t for t in tiles if t.state == "failed"]
        metrics = unmix.aggregate_generation_metrics(
            [t.tile_stats for t in tiles if t.state == "done" and t.tile_stats]
        )

        # End-to-end recovery check for known-composition (synthetic) images
        image = db.get(Image, job.image_id)
        if image.synthetic_ground_truth:
            gt_info = image.synthetic_ground_truth
            try:
                gt = imaging.load_npz(get_store().get(gt_info["key"]))
                M = load_matrix(db.get(MatrixVersion, job.matrix_version_id), get_store())
                recovered = _coarse_component_map(image, job, M)
                truth = gt["truth"].astype(np.float64)
                # Downsample the truth to exactly the coarsest level used.
                while truth.shape[0] > recovered.shape[0] or truth.shape[1] > recovered.shape[1]:
                    truth = imaging.downsample2(truth.astype(np.float32)).astype(np.float64)
                truth = _fit_to(truth, recovered.shape[:2])
                rec = unmix.recovery_error_vs_truth(recovered, truth)
                metrics.update(rec)
                gen.recovery_rmse = rec["recovery_rmse"]
            except Exception as exc:  # never let metrics destroy tile work
                metrics["recovery_error_failed"] = f"{type(exc).__name__}: {exc}"

        gen.reconstruction_rmse = metrics["reconstruction_rmse"]
        gen.metrics = metrics
        gen.state = "completed" if not failed else "partial"
        gen.finalized_at = utcnow()
        job.status = "completed" if not failed else "partial"
        job.error_code = None if not failed else "tiles_failed"
        if failed:
            job.error_detail = f"{len(failed)}/{len(tiles)} tiles permanently failed"
        db.commit()
    finally:
        db.close()


def store_get(digest: str, key: str) -> bytes:
    return get_store().get(key)


def _fit_to(arr: np.ndarray, shape_hw: tuple[int, int]) -> np.ndarray:
    """Center-crop/pad an (H,W,...) array to an exact (H',W') — used only to
    align the ground truth with the ceil-rounded coarsest level."""
    h, w = arr.shape[:2]
    th, tw = shape_hw
    out = np.zeros((th, tw) + arr.shape[2:], dtype=arr.dtype)
    sh, sw = min(h, th), min(w, tw)
    y0a, x0a = (h - sh) // 2, (w - sw) // 2
    y0b, x0b = (th - sh) // 2, (tw - sw) // 2
    out[y0b:y0b + sh, x0b:x0b + sw] = arr[y0a:y0a + sh, x0a:x0a + sw]
    return out


def _coarse_component_map(image: Image, job: Job, M: np.ndarray) -> np.ndarray:
    """Re-run the global model on the coarsest level for recovery RMSE."""
    levels = get_pyramid(image, get_store())
    coarse = levels[-1].astype(np.float64)
    bg = np.asarray(job.frozen_params["background"])
    Y = np.clip(coarse - bg, 0.0, None)
    X, _ = unmix.nnls_pixels(Y.reshape(-1, Y.shape[-1]), M)
    return X.reshape(coarse.shape[0], coarse.shape[1], -1).astype(np.float64)


# ---------------------------------------------------------------------------
# Cancellation / retry / supersession (new generations)
# ---------------------------------------------------------------------------


def cancel_job(db: Session, job: Job, reason: str = "user requested") -> None:
    if job.status in {"completed", "partial", "failed", "canceled", "superseded"}:
        return
    job.status = "canceled"
    job.error_detail = reason
    for gen in job.generations:
        if gen.is_current:
            gen.is_current = False
            gen.state = "canceled"
            for t in gen.tiles:
                if t.state in {"pending", "running"}:
                    t.state = "abandoned"
                    t.error_code = "job_canceled"
                    t.last_error = reason[:500]
    db.commit()


def retry_job(db: Session, job: Job, runner) -> Generation:
    """
    Retry after cancellation/failure: the pinned image/matrix digests and the
    frozen whole-image parameters are REUSED (same model), but a fresh
    generation is opened so none of the old tiles can masquerade as new work.
    """
    if job.status not in {"canceled", "partial", "failed", "completed"}:
        raise ValueError("job is not in a retryable state")
    for gen in job.generations:
        gen.is_current = False
        if gen.state in {"dispatching", "running"}:
            gen.state = "abandoned"
    next_no = (max(g.gen_no for g in job.generations) + 1) if job.generations else 1
    gen = Generation(job_id=job.id, gen_no=next_no, state="running", is_current=True)
    db.add(gen)
    job.status = "running"
    job.error_code = None
    job.error_detail = None
    db.flush()

    image = db.get(Image, job.image_id)
    ts = get_settings().tile_size
    n_levels = imaging.num_levels(image.height, image.width, ts)
    records = []
    for level in range(n_levels):
        lh, lw = imaging.level_dimensions(image.height, image.width, level)
        nx, ny = imaging.grid_for_level(lh, lw, ts)
        for ty in range(ny):
            for tx in range(nx):
                records.append(TileRecord(generation_id=gen.id, level=level,
                                          tile_x=tx, tile_y=ty, state="pending"))
    db.add_all(records)
    db.commit()

    for tr in records:
        runner.submit_tile(job.id, gen.id, tr.id)
    return gen


def supersede_for_matrix(db: Session, new_matrix_id: int, runner,
                         creator_id: int) -> list[int]:
    """
    A matrix revision happened. Every still-active (pending/running) job using
    a previous version of that matrix name is superseded immediately: its
    in-flight generation is fenced off, and successor jobs pinned to the new
    digest are created and dispatched.

    Terminal jobs are left untouched: a completed job keeps its immutable
    report, and a partial job stays an explicit old-matrix partial preview.
    """
    new_matrix = db.get(MatrixVersion, new_matrix_id)
    active = db.scalars(
        select(Job).where(
            Job.matrix_version_id != new_matrix_id,
            Job.status.in_(["pending", "running"]),
        )).all()
    old_matrix_ids = {
        m.id for m in db.scalars(select(MatrixVersion).where(
            MatrixVersion.name == new_matrix.name)).all()
    }
    successors = []
    for job in active:
        if job.matrix_version_id not in old_matrix_ids:
            continue
        job.status = "superseded"
        job.error_code = "matrix_revision"
        job.error_detail = f"matrix {new_matrix.name!r} revised mid-compute"
        for gen in job.generations:
            if gen.is_current:
                gen.is_current = False
                gen.state = "superseded"
                for t in gen.tiles:
                    if t.state in {"pending", "running"}:
                        t.state = "abandoned"
                        t.error_code = "matrix_revision"
        succ = Job(
            image_id=job.image_id,
            matrix_version_id=new_matrix_id,
            algorithm=job.algorithm,
            algorithm_params=dict(job.algorithm_params or {}),
            image_digest=job.image_digest,
            matrix_digest=new_matrix.digest,
            status="pending",
            created_by=creator_id,
        )
        db.add(succ)
        db.flush()
        job.successor_job_id = succ.id
        successors.append(succ.id)
    db.commit()
    for sid in successors:
        runner.submit_freeze(sid)
    return successors
