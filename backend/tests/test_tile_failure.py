"""Acceptance: one slice fails repeatedly; others still complete, and the
result is an explicitly flagged PARTIAL preview that can never be released."""


def test_repeated_tile_failure_yields_partial_blocking_release(
    client, auth, helpers, runner
):
    img = helpers.synthetic(name="flaky-tile")
    # arm the fault BEFORE dispatch so the fast 512px tile set cannot finish
    client.post("/test/faults/tile", headers=auth("admin"), json={
        "level": 0, "x": 1, "y": 0,
        "error": "tile_corrupt", "times": 100, "every_attempt": True,
    })
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])

    assert final["status"] == "partial"
    g = final["generations"][0]
    assert g["state"] == "partial"
    assert g["tiles"]["failed"] == 1
    assert g["tiles"]["done"] == g["total_tiles"] - 1

    # failed tile record carries the dedicated error code and attempt count
    detail = client.get(
        f"/jobs/{job['id']}/generations/{g['id']}", headers=auth("imager")
    ).json()
    failed = [t for t in detail["tiles"] if t["state"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == "tile_corrupt"
    assert failed[0]["attempts"] == 3

    # preview serves an explicit placeholder for the broken block
    r = client.get(
        f"/viewer/jobs/{job['id']}/generations/{g['id']}/raw_files/1/1_0.png",
        headers=auth("imager"),
    )
    assert r.status_code == 200
    assert r.headers["X-Unmix-Tile-State"] == "failed"
    assert "no-store" in r.headers["Cache-Control"]

    # and the partial generation can never be formally released
    helpers.grant(img["id"], "publisher", view=True, publish=True)
    r = helpers.release(job["id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "generation_not_complete"


def test_successful_tiles_around_failure_are_served_normally(client, auth, helpers):
    img = helpers.synthetic(name="flaky-tile-2")
    client.post("/test/faults/tile", headers=auth("admin"), json={
        "level": 0, "x": 1, "y": 0, "times": 100, "every_attempt": True,
    })
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    g = final["generations"][0]
    # neighbor tiles (0,0) and the deeper level render as real data
    for x, y in [(0, 0)]:
        r = client.get(
            f"/viewer/jobs/{job['id']}/generations/{g['id']}"
            f"/components_files/1/{x}_{y}.png",
            headers=auth("imager"),
        )
        assert r.status_code == 200
        assert r.headers["X-Unmix-Tile-State"] == "done"
    r = client.get(
        f"/viewer/jobs/{job['id']}/generations/{g['id']}/raw_files/0/0_0.png",
        headers=auth("imager"),
    )
    assert r.status_code == 200
    assert r.headers["X-Unmix-Tile-State"] == "done"
