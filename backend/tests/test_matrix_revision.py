"""Acceptance: matrix is revised WHILE computation is in flight."""

import time


REVISED_MATRIX = [
    [0.88, 0.20, 0.02],
    [0.22, 0.82, 0.05],
    [0.02, 0.22, 0.88],
    [0.01, 0.02, 0.30],
]


def test_matrix_revision_fences_old_generation_and_starts_successor(
    client, auth, helpers, runner
):
    img = helpers.synthetic(h=1024, w=1024, name="rev-during")
    job = helpers.job(img["id"])

    # widen the compute window, then revise the matrix while tiles run
    r = client.post("/test/faults/delay", headers=auth("admin"),
                    json={"seconds": 0.35})
    assert r.status_code == 200
    time.sleep(0.45)  # let freeze finish and tiles start

    r = client.post("/matrices", headers=auth("matrixer"),
                    json={"name": "default", "matrix": REVISED_MATRIX,
                          "note": "revised mid-compute"})
    assert r.status_code == 200, r.text
    new_matrix = r.json()
    assert new_matrix["version"] == 2

    runner.wait_idle(180)
    pipeline = __import__("app.pipeline", fromlist=["x"])
    pipeline.reset_fault_hooks()

    old = client.get(f"/jobs/{job['id']}", headers=auth("imager")).json()
    assert old["status"] == "superseded", old
    assert old["matrix_digest"] != new_matrix["digest"]
    assert old["successor_job_id"], "successor job must be auto-created"

    old_gen = old["generations"][-1]
    assert old_gen["state"] == "superseded"
    assert old_gen["is_current"] is False
    # in-flight tiles that reported late must land in the abandoned generation,
    # never silently mixed into any completed result
    assert old_gen["tiles"].get("done", 0) + old_gen["tiles"].get("abandoned", 0) \
        == old_gen["total_tiles"]

    # the OLD job cannot be released even though some tiles may be done
    helpers.grant(img["id"], "publisher", view=True, publish=True)
    r = helpers.release(job["id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "job_superseded"

    # successor is pinned to the NEW digest and completes independently
    succ_id = old["successor_job_id"]
    succ = helpers.wait(succ_id)
    assert succ["status"] == "completed", succ
    assert succ["matrix_version_id"] == new_matrix["id"]
    assert succ["matrix_digest"] == new_matrix["digest"]
    assert succ["image_digest"] == old["image_digest"]  # original image untouched
    assert len(succ["generations"]) == 1
    assert succ["generations"][0]["tiles"]["done"] == \
        succ["generations"][0]["total_tiles"]


def test_cached_tiles_of_revised_matrix_are_not_reused(client, auth, helpers):
    img = helpers.synthetic(name="cache-namespace")
    j1 = helpers.job(img["id"])
    f1 = helpers.wait(j1["id"])
    old_gid = f1["generations"][0]["id"]
    old_ns = f"/mat-{j1['matrix_digest'][:16]}/nnls/gen{old_gid:06d}/"

    client.post("/matrices", headers=auth("matrixer"),
                json={"name": "default", "matrix": REVISED_MATRIX})
    new_matrix = helpers.default_matrix()
    # the completed old job stays valid (no auto-successor); a fresh job
    # pinned to the new matrix is what must use a disjoint tile namespace
    j2 = helpers.job(img["id"], matrix_version_id=new_matrix["id"])
    f2 = helpers.wait(j2["id"])
    assert f2["matrix_digest"] == new_matrix["digest"]
    new_gid = f2["generations"][0]["id"]
    new_ns = f"/mat-{f2['matrix_digest'][:16]}/nnls/gen{new_gid:06d}/"
    assert new_ns != old_ns

    # the new generation's tile objects do not share any key with the old
    detail = client.get(
        f"/jobs/{j1['id']}/generations/{old_gid}", headers=auth("imager")
    ).json()
    old_keys = set()
    for t in detail["tiles"]:
        for layer in t["keys"].values():
            old_keys.update(layer)
    detail2 = client.get(
        f"/jobs/{f2['id']}/generations/{new_gid}", headers=auth("imager")
    ).json()
    for t in detail2["tiles"]:
        for layer in t["keys"].values():
            assert not (set(layer) & old_keys), "new gen reused old coefficient tile"
