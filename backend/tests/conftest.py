import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_ROOT = Path(tempfile.mkdtemp(prefix="unmix-tests-"))
os.environ["UNMIX_DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'test.db'}"
os.environ["UNMIX_LOCAL_OBJECT_DIR"] = str(TEST_ROOT / "objects")
os.environ["UNMIX_TILE_SIZE"] = "64"

from app import imaging, ingest, services, worker  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Base, User  # noqa: E402
from app.storage import ObjectStore  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    Base.metadata.create_all(get_settings() and __import__("app.database", fromlist=["engine"]).engine)
    yield
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


@pytest.fixture()
def env(prepared_database):
    settings = get_settings()
    store = ObjectStore(local_dir=settings.local_object_dir)
    db = SessionLocal()
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.commit()
    admin = User(email="admin@example.com", display_name="Admin", role="admin")
    engineer = User(email="engineer@example.com", display_name="Engineer", role="engineer")
    publisher = User(email="publisher@example.com", display_name="Publisher", role="publisher")
    viewer = User(email="viewer@example.com", display_name="Viewer", role="viewer")
    db.add_all([admin, engineer, publisher, viewer])
    db.commit()
    yield {
        "settings": settings,
        "db_factory": SessionLocal,
        "store": store,
        "services": services,
        "imaging": imaging,
        "ingest": ingest,
        "worker": worker,
        "admin": admin,
        "engineer": engineer,
        "publisher": publisher,
        "viewer": viewer,
    }
    db.close()
