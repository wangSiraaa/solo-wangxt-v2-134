"""Celery worker entrypoint: `celery -A app.worker worker -l info`."""

from .database import bootstrap
from .tasks import build_celery, get_runner  # noqa: F401

bootstrap()
celery_app, _tasks = build_celery()
