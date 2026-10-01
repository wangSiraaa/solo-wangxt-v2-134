import os
import tempfile

import pytest

# isolate every test RUN in a fresh tmp dir BEFORE app imports read settings
_TMP = tempfile.mkdtemp(prefix=f"unmix-test-{os.getpid()}-")
os.environ["UNMIX_DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["UNMIX_LOCAL_STORE_DIR"] = os.path.join(_TMP, "store")
os.environ["UNMIX_TILE_SIZE"] = "256"
os.environ["UNMIX_MAX_TILE_ATTEMPTS"] = "3"
os.environ["UNMIX_ENABLE_FAULTS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import pipeline  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import bootstrap  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SessionLocal  # noqa: E402
from app.tasks import EagerRunner, set_runner  # noqa: E402

get_settings.cache_clear()


@pytest.fixture()
def runner():
    rv = EagerRunner(SessionLocal, max_workers=8)
    set_runner(rv)
    pipeline.reset_fault_hooks()
    yield rv
    pipeline.reset_fault_hooks()
    # _shutdown flips before pool shutdown: sleeping backoff tasks wake up and
    # no-op WITHOUT touching the DB, so they cannot leak into the next test
    rv.shutdown(wait=False, cancel_futures=True)


@pytest.fixture()
def client(runner):
    bootstrap()
    with TestClient(app) as c:
        yield c


KEYS = {
    "admin": "admin-key",
    "imager": "imager-key",
    "analyst": "analyst-key",
    "matrixer": "matrixer-key",
    "publisher": "publisher-key",
}


@pytest.fixture()
def auth():
    def _h(role):
        return {"X-API-Key": KEYS[role]}

    return _h


@pytest.fixture()
def helpers(client, auth, runner):
    class H:
        @staticmethod
        def synthetic(role="imager", h=512, w=512, channels=4, components=3,
                      name="syn", seed=7):
            r = client.post(
                "/images/synthetic",
                headers=auth(role),
                json={"name": name, "height": h, "width": w,
                      "channels": channels, "components": components, "seed": seed},
            )
            assert r.status_code == 200, r.text
            return r.json()

        @staticmethod
        def job(image_id, role="imager", matrix_version_id=None):
            payload = {"image_id": image_id}
            if matrix_version_id:
                payload["matrix_version_id"] = matrix_version_id
            r = client.post("/jobs", headers=auth(role), json=payload)
            assert r.status_code == 200, r.text
            return r.json()

        @staticmethod
        def wait(job_id, timeout=180):
            assert runner.wait_idle(timeout), "tasks did not settle"
            return client.get(f"/jobs/{job_id}", headers=auth("imager")).json()

        @staticmethod
        def default_matrix():
            rows = client.get("/matrices", headers=auth("imager")).json()
            current = [m for m in rows if m["name"] == "default" and not m["superseded"]]
            return current[-1]

        @staticmethod
        def user_id(name):
            for u in client.get("/users", headers=auth("admin")).json():
                if u["name"] == name:
                    return u["id"]
            raise KeyError(name)

        @staticmethod
        def grant(image_id, user, view=False, publish=False):
            uid = H.user_id(user)
            r = client.put(f"/images/{image_id}/grants",
                           headers=auth("imager"),
                           json={"user_id": uid, "can_view": view,
                                 "can_publish": publish})
            assert r.status_code == 200, r.text

        @staticmethod
        def release(job_id, role="publisher"):
            return client.post("/results/release", headers=auth(role),
                               json={"job_id": job_id})

    return H
