from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AuditLog, User
from ..security import Principal, require_scope, resolve_principal

router = APIRouter(tags=["audit"])


@router.get("/me")
def me(p: Principal = Depends(resolve_principal)):
    return {"id": p.id, "name": p.name, "scopes": sorted(p.scopes)}


@router.get("/users")
def list_users(db: Session = Depends(get_db),
               p: Principal = Depends(resolve_principal)):
    rows = db.scalars(select(User).order_by(User.id)).all()
    return [{"id": u.id, "name": u.name, "scopes": u.scopes} for u in rows]


@router.get("/audit")
def list_audit(limit: int = 200, action: str | None = None,
               outcome: str | None = None,
               db: Session = Depends(get_db),
               p: Principal = Depends(require_scope("audit:read"))):
    q = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        q = q.where(AuditLog.action == action)
    if outcome:
        q = q.where(AuditLog.outcome == outcome)
    rows = db.scalars(q.limit(limit)).all()
    return [{
        "id": a.id, "ts": a.ts.isoformat() if a.ts else None,
        "actor": a.actor_name, "action": a.action, "outcome": a.outcome,
        "resource": a.resource, "detail": a.detail,
    } for a in rows]
