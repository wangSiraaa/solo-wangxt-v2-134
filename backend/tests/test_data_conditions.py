"""Acceptance: saturation, missing channel, ill-conditioned matrix, and
immutability of the original image / old reports on failure."""

import numpy as np

from app import imaging, unmix
from app.object_store import get_store, raw_image_key
from app.unmix import UnmixingError


def test_saturation_recorded_in_frozen_params(client, auth, helpers):
    img = helpers.synthetic(name="saturated")
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    sat = final["frozen"]["saturation_fraction"]
    assert len(sat) == img["channels"]
    # synthetic hot cores intentionally saturate at least one channel
    assert max(sat) > 0.0


def test_missing_channel_is_hard_error_at_freeze(client, auth, helpers):
    # matrix whose first detection channel records nothing
    M = [[0.0, 0.0, 0.0],
         [0.1, 0.9, 0.1],
         [0.0, 0.1, 0.9],
         [0.0, 0.0, 0.2]]
    r = client.post("/matrices", headers=auth("matrixer"),
                    json={"name": "dead-channel", "matrix": M})
    assert r.status_code == 200
    img = helpers.synthetic(name="dead-channel-img")
    # job freezes onto that matrix and must fail at freeze, not tile-by-tile
    payload = {"image_id": img["id"], "matrix_version_id": r.json()["id"]}
    job = client.post("/jobs", headers=auth("imager"), json=payload).json()
    final = helpers.wait(job["id"])
    assert final["status"] == "failed"
    assert final["error_code"] == "missing_channel"


def test_ill_conditioned_matrix_warns_but_runs(client, auth, helpers):
    # two nearly identical components => high condition number
    eps = 1e-3
    M = [[1.0, 1.0 + eps, 0.0],
         [0.0, eps, 1.0],
         [0.0, 0.0, 0.1],
         [0.0, 0.0, 0.0]]
    M = np.array(M)
    M[:, 2] += 1e-2
    M = M.tolist()
    r = client.post("/matrices", headers=auth("matrixer"),
                    json={"name": "illcond", "matrix": M})
    assert r.status_code == 200
    issues = unmix.validate_matrix(np.array(M, dtype=float), 4)
    assert any(i["code"] == "matrix_ill_conditioned" for i in issues)


def test_original_image_bytes_never_overwritten(client, auth, helpers):
    img = helpers.synthetic(name="immutable-img")
    key = raw_image_key(img["digest"])
    before = get_store().get(key)
    # a failed job must not touch the original object
    client.post("/test/faults/tile", headers=auth("admin"), json={
        "level": 0, "x": 1, "y": 0, "times": 100, "every_attempt": True})
    job = helpers.job(img["id"])
    final = helpers.wait(job["id"])
    assert final["status"] == "partial"
    after = get_store().get(key)
    assert after == before


def test_release_failure_keeps_old_report_intact(client, auth, helpers):
    img = helpers.synthetic(name="keep-report")
    helpers.grant(img["id"], "publisher", view=True, publish=True)
    job = helpers.job(img["id"])
    helpers.wait(job["id"])
    r = helpers.release(job["id"])
    assert r.status_code == 200
    first = r.json()

    # attempting to release the same job again must not alter the first report
    r2 = helpers.release(job["id"])
    assert r2.status_code == 409
    rows = client.get("/results", headers=auth("publisher")).json()
    mine = [x for x in rows if x["job_id"] == job["id"]]
    assert len(mine) == 1
    assert mine[0]["report_digest"] == first["report_digest"]


def test_negative_or_nonfinite_matrix_rejected(client, auth):
    r = client.post("/matrices", headers=auth("matrixer"),
                    json={"name": "neg", "matrix": [[-1.0, 0.0], [0.0, 1.0]]})
    assert r.status_code == 400
    # non-finite JSON tokens are never accepted (FastAPI rejects the payload;
    # the exact status is 4xx) and never reach the unmixing pipeline
    r = client.post("/matrices", headers={**auth("matrixer"),
                                           "Content-Type": "application/json"},
                    content='{"name":"nan","matrix":[[NaN,0.0]]}')
    assert 400 <= r.status_code < 500
    r = client.post("/matrices", headers={**auth("matrixer"),
                                           "Content-Type": "application/json"},
                    content='{"name":"inf","matrix":[[Infinity,0.0]]}')
    assert 400 <= r.status_code < 500
