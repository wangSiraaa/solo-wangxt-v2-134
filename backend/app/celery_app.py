from __future__ import annotations

from celery import Celery

from .config import get_settings
from .database import SessionLocal
from .models import JobGeneration
from .services import pending_tasks, start_job
from .storage import ObjectStore
from .worker import TaskRejected, process_tile

settings = get_settings()
celery_app = Celery("fluorescence_unmix", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=5,
)


def get_store() -> ObjectStore:
    return ObjectStore(
        local_dir=settings.local_object_dir,
        endpoint=None if settings.local_object_dir else settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )


def dispatch_generation(db, job) -> int:
    gen = (
        db.query(JobGeneration)
        .filter(JobGeneration.job_id == job.id, JobGeneration.generation_no == job.current_generation_no)
        .one()
    )
    tasks = pending_tasks(db, gen.id, limit=100_000)
    for task in tasks:
        process_tile_task.delay(task.id)
    return len(tasks)


@celery_app.task(name="job.start", bind=True)
def start_job_task(self, job_id: str, reason: str = "initial") -> None:
    db = SessionLocal()
    try:
        job = start_job(db, get_store(), job_id, reason=reason)
        dispatch_generation(db, job)
    finally:
        db.close()


@celery_app.task(name="tile.process", bind=True, max_retries=4, retry_backoff=True)
def process_tile_task(self, task_id: str) -> None:
    db = SessionLocal()
    try:
        task = process_tile(db, get_store(), task_id, settings)
        if task.status == "pending":
            raise self.retry(exc=RuntimeError(task.error_detail or "retryable tile failure"))
    except TaskRejected:
        return
    finally:
        db.close()
