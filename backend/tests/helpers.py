import numpy as np


def create_dataset(env, *, size=160, corrupt=False):
    services = env["services"]
    imaging = env["imaging"]
    ingest = env["ingest"]
    db = env["db_factory"]()
    store = env["store"]
    source, truth = ingest.synthetic_demo_image(size, size)

    placeholder = services.register_image(
        db,
        actor=env["admin"],
        name=f"synthetic-{size}-{'corrupt' if corrupt else 'ok'}",
        width=size,
        height=size,
        channel_names=["DAPI", "FITC", "TRITC", "Cy5"],
        source_object_key="",
        manifest_object_key="",
        source_digest="placeholder",
    )
    manifest = ingest.create_pyramid_manifest(
        store, source, image_id=placeholder.id, tile_size=env["settings"].tile_size
    )
    if corrupt:
        target = next(tile for tile in manifest["tiles"] if tile["level"] == 1)
        bad = source.copy()
        bad[:, 0, 0] = np.nan
        data = imaging.encode_source_tile(bad)
        key, digest = store.put_content_addressed(
            f"corrupt/{placeholder.id}/level/0", data, ".npz"
        )
        target["object_key"] = key
        target["digest"] = digest
        manifest["source_digest"] = imaging.sha256_bytes(
            imaging.canonical_json([t["digest"] for t in manifest["tiles"]])
        )
    manifest_key, digest = store.put_json(f"manifests/{placeholder.id}", manifest)
    placeholder.manifest_object_key = manifest_key
    placeholder.source_object_key = manifest_key
    placeholder.source_digest = digest

    services.grant_image_permission(
        db,
        actor=env["admin"],
        image_id=placeholder.id,
        user_id=env["publisher"].id,
        permission="view_derived",
    )
    services.grant_image_permission(
        db,
        actor=env["admin"],
        image_id=placeholder.id,
        user_id=env["publisher"].id,
        permission="publish_result",
    )
    matrix = services.create_matrix_version(
        db,
        store,
        actor=env["engineer"],
        name="calibration",
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
    db.commit()
    db.refresh(placeholder)
    db.refresh(matrix)
    return db, placeholder, matrix, source, truth, manifest


def start_and_process_all(env, job_id):
    services = env["services"]
    worker = env["worker"]
    db = env["db_factory"]()
    job = services.start_job(db, env["store"], job_id)
    gen = job.generations[-1]
    tasks = services.pending_tasks(db, gen.id, limit=10_000)
    processed = []
    for task in tasks:
        processed.append(worker.process_tile(db, env["store"], task.id, env["settings"]))
    job = services.finish_if_complete(db, job.id)
    return db, job, processed
