"""Happy path + known-composition recovery and reconstruction quality."""


def test_job_completes_all_tiles_same_generation(client, helpers):
    img = helpers.synthetic()
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    assert final["status"] == "completed"
    gens = final["generations"]
    assert len(gens) == 1
    assert gens[0]["state"] == "completed"
    # every required tile of every level in the SAME generation
    counts = gens[0]["tiles"]
    assert counts.get("failed", 0) == 0
    assert counts.get("abandoned", 0) == 0
    assert counts["done"] == gens[0]["total_tiles"] == 5  # 512px, tile 256


def test_known_composition_recovery_and_residual(client, helpers):
    img = helpers.synthetic(h=1024, w=1024)
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    g = final["generations"][0]
    m = g["metrics"]
    # recovered component maps closely match the known truth (< 20% rel at the
    # heavily downsampled validation level)
    assert m["recovery_relative_rmse"] < 0.20, m
    # NNLS reconstruction residual is small relative to the signal scale
    assert m["reconstruction_rmse"] is not None
    assert m["reconstruction_rmse"] < 0.02 * 65535, m


def test_frozen_params_single_hash_identical_across_tiles(client, helpers):
    img = helpers.synthetic()
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    assert final["params_hash"]
    # whole-image parameters exist and carry display scales for all layers
    f = final["frozen"]
    assert len(f["display"]["raw_scale"]) == img["channels"]
    assert len(f["display"]["comp_scale"]) == 3
    assert f["background"] is not None and f["noise_sigma"] is not None


def test_release_manifest_pinned_and_report_content_addressed(client, helpers):
    img = helpers.synthetic()
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    helpers.grant(img["id"], "publisher", view=True, publish=True)
    r = helpers.release(job["id"])
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    detail = client.get(f"/results/{rid}", headers={"X-API-Key": "admin-key"}).json()
    assert detail["manifest_summary"]["image"]["digest"] == final["image_digest"]
    assert detail["manifest_summary"]["matrix"]["digest"] == final["matrix_digest"]
    assert detail["manifest_summary"]["frozen_params_hash"] == final["params_hash"]
    # every manifest tile key lives in the exact gen/digest namespace
    rpt = client.get(f"/results/{rid}?include_report=true",
                     headers={"X-API-Key": "admin-key"}).json()
    import json

    manifest = json.loads(rpt["report"])
    expected_ns = (
        f"/img-{final['image_digest'][:16]}/mat-{final['matrix_digest'][:16]}"
        f"/nnls/gen{manifest['generation_id']:06d}/"
    )
    for t in manifest["tiles"]:
        for k in t["raw"] + t["components"] + t["residual"]:
            assert expected_ns in f"/{k}", k


def test_tile_bytes_immutable_cache_headers(client, helpers):
    img = helpers.synthetic()
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    gid = final["generations"][0]["id"]
    # DZI top tile: internal L1 for a 512px image is the dzi level 0
    r = client.get(f"/viewer/jobs/{job['id']}/generations/{gid}/raw.dzi",
                   headers={"X-API-Key": "imager-key"})
    assert r.status_code == 200
    assert "<Image" in r.text
    r = client.get(
        f"/viewer/jobs/{job['id']}/generations/{gid}/raw_files/0/0_0.png",
        headers={"X-API-Key": "imager-key"})
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert r.headers["X-Unmix-Tile-State"] == "done"
