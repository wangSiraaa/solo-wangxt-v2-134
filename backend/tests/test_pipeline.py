import numpy as np
import pytest

from app.models import JobGeneration, TileTask
from helpers import create_dataset, start_and_process_all


def test_pipeline_frozen_params_and_publish(env):
    services = env["services"]
    db, image, matrix, source, truth, manifest = create_dataset(env)
    job = services.create_job(
        db,
        actor=env["publisher"],
        image_id=image.id,
        matrix_version_id=matrix.id,
        algorithm="nnls",
    )
    db, job, tasks = start_and_process_all(env, job.id)
    assert job.status == "succeeded"
    assert job.frozen_params["source_tile_count"] == len([t for t in manifest["tiles"] if t["level"] == 0])
    assert len({t.generation_id for t in tasks if t.status == "succeeded"}) == 1
    assert all(t.object_digest for t in tasks if t.status == "succeeded")

    # Recovery check on the full-resolution first tile. Residuals are also
    # persisted and must not silently exceed the synthetic noise scale.
    component_tile = db.query(__import__("app.models", fromlist=["TileTask"]).TileTask).filter_by(
        job_id=job.id, level=0, x=0, y=0, kind="component", status="succeeded"
    ).one()
    blob = env["store"].get(component_tile.object_key)
    loaded = np.load(__import__("io").BytesIO(blob), allow_pickle=False)
    estimated = loaded["components"]
    tile_size = env["settings"].tile_size
    expected = truth[:, :tile_size, :tile_size]
    assert np.sqrt(np.mean((estimated - expected) ** 2)) < 0.08
    assert component_tile.quality_flags["tile_rmse"] < 0.1

    result = services.publish_result(db, env["store"], job_id=job.id, actor=env["publisher"])
    report = env["store"].get_json(result.report_object_key)
    assert report["matrix_digest"] == job.matrix_digest
    assert report["image_digest"] == job.image_digest
    assert report["generation_no"] == job.current_generation_no
    assert len(report["tiles"]) == job.required_tile_count


def test_matrix_mid_flight_keeps_job_on_old_digest(env):
    services = env["services"]
    db, image, matrix, _, _, _ = create_dataset(env)
    job = services.create_job(
        db, actor=env["publisher"], image_id=image.id, matrix_version_id=matrix.id
    )
    job = services.start_job(db, env["store"], job.id)
    assert job.status == "running"
    old_digest = job.matrix_digest

    new_matrix = services.create_matrix_version(
        db,
        env["store"],
        actor=env["engineer"],
        name="calibration",
        matrix_key=matrix.matrix_key,
        channel_names=matrix.channel_names,
        component_names=matrix.component_names,
        coefficients=(np.asarray(matrix.coefficients) * 1.03).tolist(),
        publish=True,
    )
    db.refresh(matrix)
    assert new_matrix.coefficient_digest != old_digest
    assert matrix.status == "superseded"

    db, job, _ = start_and_process_all(env, job.id)
    assert job.matrix_digest == old_digest
    assert job.matrix_version_id == matrix.id
    active_gen = next(g for g in job.generations if g.generation_no == job.current_generation_no)
    tasks = db.query(__import__("app.models", fromlist=["TileTask"]).TileTask).filter_by(generation_id=active_gen.id).all()
    assert all(f"mat={old_digest[:12]}" in t.cache_key for t in tasks)


def test_corrupt_single_tile_allows_preview_but_blocks_publish(env):
    services = env["services"]
    db, image, matrix, _, _, _ = create_dataset(env, corrupt=True)
    job = services.create_job(
        db, actor=env["publisher"], image_id=image.id, matrix_version_id=matrix.id
    )
    db, job, tasks = start_and_process_all(env, job.id)
    assert job.status == "partial"
    failed = [t for t in tasks if t.status == "failed"]
    assert failed
    assert {t.error_code for t in failed} <= {"corrupt_tile"}
    # The coordinate's other required view may succeed; either way publication
    # requires every required tile of the same generation.
    with pytest.raises(services.ServiceError) as exc:
        services.publish_result(db, env["store"], job_id=job.id, actor=env["publisher"])
    assert exc.value.code == "incomplete_generation"


def test_cancel_then_retry_creates_deprecated_generation(env):
    services = env["services"]
    db, image, matrix, _, _, _ = create_dataset(env)
    job = services.create_job(
        db, actor=env["publisher"], image_id=image.id, matrix_version_id=matrix.id
    )
    job = services.start_job(db, env["store"], job.id)
    first_gen_id = job.generations[-1].id
    services.request_cancel(db, job.id, env["publisher"])
    stale_task = db.query(TileTask).filter(TileTask.generation_id == first_gen_id).first()
    late_task = env["worker"].process_tile(db, env["store"], stale_task.id, env["settings"])
    assert late_task.status in {"cancelled", "stale"}
    db.refresh(job)
    assert job.status == "cancelled"
    assert db.get(__import__("app.models", fromlist=["JobGeneration"]).JobGeneration, first_gen_id).status == "cancelled"

    retried = services.retry_job(db, env["store"], job.id, env["publisher"])
    assert retried.current_generation_no == 2
    assert retried.status == "running"
    gens = db.query(JobGeneration).filter_by(job_id=job.id).all()
    assert [g.status for g in gens] == ["cancelled", "active"]
    old_task = db.query(TileTask).filter(TileTask.generation_id == first_gen_id).first()
    late_after_retry = env["worker"].process_tile(db, env["store"], old_task.id, env["settings"])
    assert late_after_retry.status in {"cancelled", "stale"}
    assert late_after_retry.generation_id == first_gen_id


def test_temporary_tile_failure_retries_until_attempt_limit(env, monkeypatch):
    services = env["services"]
    db, image, matrix, _, _, _ = create_dataset(env)
    job = services.create_job(
        db, actor=env["publisher"], image_id=image.id, matrix_version_id=matrix.id
    )
    job = services.start_job(db, env["store"], job.id)
    task = db.query(TileTask).filter_by(level=0, x=0, y=0, kind="component").one()
    original_get = env["store"].get

    def failing_get(key: str):
        if key.endswith(".npz") and "sources" in key:
            raise RuntimeError("temporary slice storage failure")
        return original_get(key)

    monkeypatch.setattr(env["store"], "get", failing_get)
    processed = task
    for expected_attempt in range(1, env["settings"].max_tile_attempts + 1):
        processed = env["worker"].process_tile(db, env["store"], task.id, env["settings"])
        assert processed.attempts == expected_attempt
        assert processed.error_code == "tile_processing_error"
    assert processed.status == "failed"


def test_viewer_cannot_publish_and_competing_publish_wins_once(env):
    services = env["services"]
    db, image, matrix, _, _, _ = create_dataset(env)
    job = services.create_job(
        db, actor=env["publisher"], image_id=image.id, matrix_version_id=matrix.id
    )
    db, job, _ = start_and_process_all(env, job.id)

    # Viewer can see source via demo-like explicit grant but has no publish.
    services.grant_image_permission(
        db, actor=env["admin"], image_id=image.id, user_id=env["viewer"].id, permission="view_source"
    )
    with pytest.raises(services.ServiceError) as exc:
        services.publish_result(db, env["store"], job_id=job.id, actor=env["viewer"])
    assert exc.value.code == "forbidden"

    first = services.publish_result(db, env["store"], job_id=job.id, actor=env["publisher"])
    assert first.status == "published"
    with pytest.raises(services.ServiceError) as conflict:
        services.publish_result(db, env["store"], job_id=job.id, actor=env["publisher"])
    assert conflict.value.code == "concurrent_publication"


def test_failed_result_does_not_overwrite_report_or_source(env):
    services = env["services"]
    initial_db, created_image, matrix, _, _, _ = create_dataset(env, corrupt=True)
    image_id = created_image.id
    original_source_key = created_image.source_object_key
    original_source_digest = created_image.source_digest
    job = services.create_job(
        initial_db, actor=env["publisher"], image_id=image_id, matrix_version_id=matrix.id
    )
    db, job, _ = start_and_process_all(env, job.id)
    with pytest.raises(services.ServiceError):
        services.publish_result(db, env["store"], job_id=job.id, actor=env["publisher"])
    image = db.get(__import__("app.models", fromlist=["Image"]).Image, image_id)
    assert image.source_object_key == original_source_key
    assert image.source_digest == original_source_digest
    assert db.query(__import__("app.models", fromlist=["ResultVersion"]).ResultVersion).count() == 0
