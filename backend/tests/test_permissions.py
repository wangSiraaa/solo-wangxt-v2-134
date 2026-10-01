"""Acceptance: viewing an image does NOT authorize publishing; releases are
audited; two publishers racing lose one with 409; matrix publish is a
separate capability."""

import threading

import pytest

from app import pipeline
from app.tasks import get_runner


def test_analyst_cannot_publish_results(client, auth, helpers):
    img = helpers.synthetic(role="imager", name="perm-1")
    helpers.grant(img["id"], "analyst", view=True, publish=False)
    job = helpers.job(img["id"], role="analyst")
    helpers.wait(job["id"])

    # analyst can see everything but has no result:publish scope
    r = helpers.release(job["id"], role="analyst")
    assert r.status_code == 403
    assert "result:publish" in r.json()["detail"]


def test_view_grant_without_publish_grant_is_denied(client, auth, helpers):
    img = helpers.synthetic(role="imager", name="perm-2")
    helpers.grant(img["id"], "publisher", view=True, publish=False)
    job = helpers.job(img["id"], role="imager")
    helpers.wait(job["id"])
    r = helpers.release(job["id"])
    assert r.status_code == 403
    assert "viewing" in r.json()["detail"].lower()


def test_matrixer_cannot_publish_results(client, auth, helpers):
    img = helpers.synthetic(role="imager", name="perm-3")
    helpers.grant(img["id"], "matrixer", view=True, publish=False)
    job = helpers.job(img["id"], role="imager")
    helpers.wait(job["id"])
    # matrixer can publish matrices but not result reports
    r = client.post("/results/release", headers=auth("matrixer"),
                    json={"job_id": job["id"]})
    assert r.status_code == 403


def test_publisher_with_grant_succeeds_and_is_audited(client, auth, helpers):
    img = helpers.synthetic(role="imager", name="perm-4")
    helpers.grant(img["id"], "publisher", view=True, publish=True)
    job = helpers.job(img["id"], role="imager")
    helpers.wait(job["id"])
    r = helpers.release(job["id"])
    assert r.status_code == 200, r.text

    rows = client.get("/audit?action=result.release", headers=auth("admin")).json()
    allowed = [a for a in rows if a["resource"] == f"job:{job['id']}"]
    assert any(a["outcome"] == "allowed" for a in allowed)


def test_unauthenticated_and_invalid_key_rejected(client):
    r = client.get("/images")
    assert r.status_code == 401
    r = client.get("/images", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_denied_publish_is_audited(client, auth, helpers):
    img = helpers.synthetic(role="imager", name="perm-5")
    helpers.grant(img["id"], "analyst", view=True)
    job = helpers.job(img["id"], role="analyst")
    helpers.wait(job["id"])
    helpers.release(job["id"], role="analyst")
    rows = client.get("/audit?action=result.release&outcome=denied",
                      headers=auth("admin")).json()
    assert rows, "denied release must appear in the audit log"


def test_concurrent_publish_race_single_winner(client, auth, helpers, runner):
    img = helpers.synthetic(role="imager", name="race-1")
    helpers.grant(img["id"], "publisher", view=True, publish=True)
    job = helpers.job(img["id"], role="imager")
    helpers.wait(job["id"])

    results: list = []
    barrier = threading.Barrier(2)

    from app.models import SessionLocal
    from app.release import release_result, ReleaseError
    from app.security import Principal
    from app.models import User
    from sqlalchemy import select

    def attempt():
        barrier.wait()
        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.name == "publisher"))
            p = Principal(user)
            release_result(db, p, job["id"])
            results.append(200)
        except ReleaseError as exc:
            results.append(exc.status)
        finally:
            db.close()

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 409], results

    # exactly one release row exists
    rows = client.get("/results", headers=auth("publisher")).json()
    mine = [r for r in rows if r["job_id"] == job["id"]]
    assert len(mine) == 1
