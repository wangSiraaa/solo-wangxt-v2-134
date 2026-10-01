"""Acceptance: cancel a running job, then retry — new generation, same model."""

import time


def test_cancel_then_retry_uses_new_generation_same_digests(client, auth, helpers):
    img = helpers.synthetic(h=1024, w=1024, name="cancel-retry")
    job = helpers.job(img["id"])

    client.post("/test/faults/delay", headers=auth("admin"), json={"seconds": 1.0})
    time.sleep(0.8)  # freeze done, tiles in flight
    r = client.post(f"/jobs/{job['id']}/cancel", headers=auth("imager"))
    assert r.status_code == 200
    assert r.json()["status"] == "canceled"

    helpers.wait(job["id"])  # drain the in-flight (abandoned) attempts
    canceled = client.get(f"/jobs/{job['id']}", headers=auth("imager")).json()
    g1 = canceled["generations"][0]
    assert g1["state"] == "canceled"
    assert g1["is_current"] is False
    # no tile from the dead generation can be marked done after cancellation
    assert g1["tiles"].get("done", 0) == 0
    assert g1["tiles"].get("abandoned", 0) >= 1

    # retry: same pinned image + matrix, same frozen hash, NEW generation
    r = client.post(f"/jobs/{job['id']}/retry", headers=auth("imager"))
    assert r.status_code == 200, r.text
    new_gid = r.json()["new_generation_id"]
    final = helpers.wait(job["id"])
    assert final["status"] == "completed"
    assert final["image_digest"] == canceled["image_digest"]
    assert final["matrix_digest"] == canceled["matrix_digest"]
    assert final["params_hash"] is not None
    g2 = [g for g in final["generations"] if g["id"] == new_gid][0]
    assert g2["gen_no"] == g1["gen_no"] + 1
    assert g2["is_current"] is True
    assert g2["tiles"]["done"] == g2["total_tiles"]


def test_late_tile_after_cancel_lands_abandoned(client, auth, helpers, runner):
    img = helpers.synthetic(name="late-cancel")
    job = helpers.job(img["id"])
    client.post("/test/faults/delay", headers=auth("admin"), json={"seconds": 1.5})
    time.sleep(0.7)
    client.post(f"/jobs/{job['id']}/cancel", headers=auth("imager"))
    helpers.wait(job["id"])
    j = client.get(f"/jobs/{job['id']}", headers=auth("imager")).json()
    for g in j["generations"]:
        if g["state"] == "canceled":
            # every tile is pending-less: done or abandoned, nothing hanging
            assert g["tiles"].get("running", 0) == 0
            assert g["tiles"].get("pending", 0) == 0
