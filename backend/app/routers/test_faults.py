"""
Test-only fault injection surface. In a real deployment do not mount this
router (main mounts it when UNMIX_ENABLE_FAULTS=1).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import pipeline
from ..security import Principal, require_scope

router = APIRouter(prefix="/test", tags=["test-faults"])


class TileFaultIn(BaseModel):
    # match spec like "L0:1,2" (level, x, y) or "*" with a times cap
    level: int | None = None
    x: int | None = None
    y: int | None = None
    error: str = "tile_corrupt"
    times: int = 1
    every_attempt: bool = False


class DelayIn(BaseModel):
    seconds: float = 0.0


@router.post("/faults/tile")
def set_tile_fault(body: TileFaultIn,
                   p: Principal = Depends(require_scope("*"))):
    state = {"remaining": body.times}

    def fn(job_id, gen_id, level, x, y, attempt):
        if body.level is not None and level != body.level:
            return None
        if body.x is not None and x != body.x:
            return None
        if body.y is not None and y != body.y:
            return None
        if not body.every_attempt and attempt != 1:
            return None
        if state["remaining"] <= 0:
            return None
        state["remaining"] -= 1
        return body.error

    pipeline.set_fault_hooks(tile_fail=fn)
    return {"ok": True}


@router.post("/faults/delay")
def set_delay(body: DelayIn, p: Principal = Depends(require_scope("*"))):
    pipeline.set_fault_hooks(tile_delay=body.seconds)
    return {"ok": True}


@router.post("/faults/reset")
def reset(p: Principal = Depends(require_scope("*"))):
    pipeline.reset_fault_hooks()
    return {"ok": True}
