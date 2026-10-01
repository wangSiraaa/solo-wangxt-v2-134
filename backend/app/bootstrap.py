"""Idempotent seed data: capability-bearing service users + a healthy matrix."""

from __future__ import annotations

import hashlib
import json

import numpy as np
from sqlalchemy import select

from . import imaging
from .config import get_settings
from .models import MatrixVersion, SessionLocal, User
from .object_store import get_store, matrix_object_key

SCOPE_SETS = {
    "admin": ["*"],
    "imager": [
        "image:upload",
        "image:view-own",
        "matrix:view",
        "job:create",
        "job:view",
        "job:cancel",
        "result:view",
        "grant:manage",
    ],
    "analyst": [
        "image:view-granted",
        "matrix:view",
        "job:create",
        "job:view",
        "job:cancel",
        "result:view",
    ],
    "matrixer": [
        "image:view-any",
        "matrix:view",
        "matrix:publish",
        "job:view",
    ],
    "publisher": [
        "image:view-granted",
        "matrix:view",
        "job:view",
        "result:view",
        "result:publish",
    ],
}

KEYS = ("seed_admin_key", "seed_imager_key", "seed_analyst_key",
        "seed_matrixer_key", "seed_publisher_key")


def seed_data() -> None:
    s = get_settings()
    db = SessionLocal()
    try:
        for (name, scopes), key_attr in zip(SCOPE_SETS.items(), KEYS):
            key = getattr(s, key_attr)
            existing = db.scalar(select(User).where(User.name == name))
            if existing is None:
                db.add(User(name=name, api_key=key, scopes=scopes))
            elif list(existing.scopes or []) != scopes:
                # reconcile seeded capability sets on redeploy
                existing.scopes = scopes
                existing.api_key = key
        db.flush()

        if db.scalar(select(MatrixVersion).where(MatrixVersion.name == "default")) is None:
            M = np.array(
                [
                    [0.95, 0.12, 0.04],
                    [0.10, 0.90, 0.08],
                    [0.03, 0.15, 0.92],
                    [0.02, 0.05, 0.20],
                ],
                dtype=np.float64,
            )
            digest = hashlib.sha256(
                json.dumps(M.tolist(), sort_keys=True).encode()
            ).hexdigest()
            admin = db.scalar(select(User).where(User.name == "admin"))
            get_store().put(
                matrix_object_key(digest),
                imaging.save_npz(matrix=M, digest=np.asarray(digest)),
            )
            db.add(
                MatrixVersion(
                    name="default",
                    version=1,
                    matrix=M.tolist(),
                    digest=digest,
                    created_by=admin.id,
                    note="seeded healthy 4x3 bleed matrix",
                )
            )
        db.commit()
    finally:
        db.close()
