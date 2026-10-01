import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import bootstrap
from .routers import audit, images, jobs, matrices, results, test_faults, viewer
from .tasks import get_runner

app = FastAPI(title="Fluorescence Unmixing Platform", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audit.router)
app.include_router(images.router)
app.include_router(matrices.router)
app.include_router(jobs.router)
app.include_router(viewer.router)
app.include_router(results.router)
if os.environ.get("UNMIX_ENABLE_FAULTS", "1") == "1":
    app.include_router(test_faults.router)


@app.on_event("startup")
def _startup() -> None:
    bootstrap()
    # initialize runner eagerly so first request doesn't race
    get_runner()


@app.get("/health")
def health() -> dict:
    from .config import get_settings

    s = get_settings()
    return {"status": "ok", "store": s.object_store,
            "broker": s.celery_broker_url or "eager"}
