"""
Runners execute pipeline stages. Two implementations:

* EagerRunner: bounded thread pool inside the API process — used in tests and
  single-node demos. Stages are the exact same functions Celery calls.
* CeleryRunner: Redis-backed distributed workers.

Faults in one tile never block the pool: each tile attempt is one job and the
retry/finalize submissions are scheduled as follow-up jobs.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import pipeline
from .config import get_settings


class Runner:
    session_factory = None

    def submit_freeze(self, job_id: int) -> None: ...
    def submit_tile(self, job_id: int, generation_id: int, tile_id: int,
                    delay: float = 0.0) -> None: ...
    def submit_finalize(self, job_id: int, generation_id: int) -> None: ...

    # pipeline entry points (shared)
    run_freeze = staticmethod(pipeline.freeze_and_dispatch)
    run_tile = staticmethod(pipeline.process_tile)
    run_finalize = staticmethod(pipeline.finalize_generation)


class EagerRunner(Runner):
    def __init__(self, session_factory, max_workers: int = 4) -> None:
        self.session_factory = session_factory
        self.pool = ThreadPoolExecutor(max_workers=max_workers,
                                       thread_name_prefix="unmix")
        self._lock = threading.Lock()
        self._inflight = 0
        self._idle = threading.Condition()
        self._shutdown = False

    def shutdown(self, wait: bool = True, **kwargs) -> None:
        self._shutdown = True
        self.pool.shutdown(wait=wait, **kwargs)

    def _track(self, fn, *args) -> None:
        with self._idle:
            self._inflight += 1

        def wrapped():
            if self._shutdown:
                with self._idle:
                    self._inflight -= 1
                    if self._inflight == 0:
                        self._idle.notify_all()
                return
            try:
                fn(*args, self)
            except Exception:  # stage failures are recorded on the job/tiles
                import logging

                logging.getLogger("unmix.tasks").exception("stage failure")
            finally:
                with self._idle:
                    self._inflight -= 1
                    if self._inflight == 0:
                        self._idle.notify_all()

        self.pool.submit(wrapped)

    def submit_freeze(self, job_id: int) -> None:
        if self._shutdown:
            return
        self._track(self.run_freeze, job_id)

    def submit_tile(self, job_id: int, generation_id: int, tile_id: int,
                    delay: float = 0.0) -> None:
        if self._shutdown:
            return
        if delay:
            def delayed():
                # the backoff wait itself counts as in-flight so callers
                # waiting for quiescence cannot observe a false idle gap
                time.sleep(delay)
                self._track(self.run_tile, job_id, generation_id, tile_id)

            with self._idle:
                self._inflight += 1

            def tracked_delayed():
                try:
                    time.sleep(delay)
                    if not self._shutdown:
                        self._track(self.run_tile, job_id, generation_id, tile_id)
                finally:
                    with self._idle:
                        self._inflight -= 1
                        if self._inflight == 0:
                            self._idle.notify_all()

            self.pool.submit(tracked_delayed)
        else:
            self._track(self.run_tile, job_id, generation_id, tile_id)

    def submit_finalize(self, job_id: int, generation_id: int) -> None:
        if self._shutdown:
            return
        self._track(self.run_finalize, job_id, generation_id)

    def wait_idle(self, timeout: float = 120.0) -> bool:
        with self._idle:
            if self._inflight == 0:
                return True
            return self._idle.wait_for(lambda: self._inflight == 0, timeout)


_celery_app = None
_runner: Runner | None = None


def build_celery():
    from celery import Celery

    s = get_settings()
    app = Celery("unmix", broker=s.celery_broker_url,
                 backend=s.celery_result_backend or s.celery_broker_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
    )

    @app.task(name="unmix.freeze", acks_late=True)
    def freeze_task(job_id: int):
        pipeline.freeze_and_dispatch(job_id, get_runner())

    @app.task(name="unmix.tile", acks_late=True, max_retries=None)
    def tile_task(job_id: int, generation_id: int, tile_id: int):
        pipeline.process_tile(job_id, generation_id, tile_id, get_runner())

    @app.task(name="unmix.finalize", acks_late=True)
    def finalize_task(job_id: int, generation_id: int):
        pipeline.finalize_generation(job_id, generation_id, get_runner())

    return app, {
        "freeze": freeze_task,
        "tile": tile_task,
        "finalize": finalize_task,
    }


class CeleryRunner(Runner):
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        global _celery_app
        _celery_app, self.tasks = build_celery()

    def submit_freeze(self, job_id: int) -> None:
        self.tasks["freeze"].apply_async(args=[job_id])

    def submit_tile(self, job_id: int, generation_id: int, tile_id: int,
                    delay: float = 0.0) -> None:
        self.tasks["tile"].apply_async(
            args=[job_id, generation_id, tile_id], countdown=delay
        )

    def submit_finalize(self, job_id: int, generation_id: int) -> None:
        self.tasks["finalize"].apply_async(args=[job_id, generation_id])


def get_runner() -> Runner:
    global _runner
    if _runner is None:
        from .models import SessionLocal

        s = get_settings()
        if s.celery_broker_url:
            _runner = CeleryRunner(SessionLocal)
        else:
            _runner = EagerRunner(SessionLocal)
    return _runner


def set_runner(runner: Runner) -> None:
    """Tests inject a runner (e.g. a fresh EagerRunner per test session)."""
    global _runner
    _runner = runner
