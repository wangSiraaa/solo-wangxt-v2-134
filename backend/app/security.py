"""Authentication and capability checks.

Viewing an original image grants NOTHING else: publishing matrices or
results requires explicit scopes (and, for results, a per-image grant).
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, Image, ImageGrant, User


class Principal:
    def __init__(self, user: User):
        self.user = user
        self.id = user.id
        self.name = user.name
        self.scopes = set(user.scopes or [])

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes


def audit(db: Session, principal: Principal | None, action: str, outcome: str,
          resource: str | None = None, detail: str | None = None) -> None:
    db.add(
        AuditLog(
            actor_id=principal.id if principal else None,
            actor_name=principal.name if principal else None,
            action=action,
            outcome=outcome,
            resource=resource,
            detail=(detail or "")[:2000],
        )
    )


def get_principal(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> Principal:
    # Raising here (no DB) keeps dependency wiring simple; DB session is added
    # by resolve_principal below.
    key = x_api_key
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization.split(" ", 1)[1].strip()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")
    return key  # type: ignore[return-value]


def resolve_principal(api_key: str = Depends(get_principal)) -> Principal:
    from .database import SessionLocal  # local import avoids cycles

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.api_key == api_key))
        if not user:
            audit(db, None, "authenticate", "denied", detail="unknown api key")
            db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
        detached = User(
            id=user.id, name=user.name, api_key=user.api_key, scopes=list(user.scopes or [])
        )
        return Principal(detached)


def require_scope(scope: str):
    def dep(p: Principal = Depends(resolve_principal)) -> Principal:
        if not p.has(scope):
            # deny is audited; needs a session
            from .database import SessionLocal

            with SessionLocal() as db:
                audit(db, p, f"require_{scope}", "denied",
                      detail=f"principal lacks scope {scope}")
                db.commit()
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"missing required capability: {scope}",
            )
        return p

    return dep


def can_view_image(db: Session, p: Principal, image: Image) -> bool:
    # admins and image uploaders can always view; otherwise need explicit grant
    if p.has("image:view-any") or image.uploaded_by == p.id:
        return True
    grant = db.scalar(
        select(ImageGrant).where(
            ImageGrant.image_id == image.id, ImageGrant.user_id == p.id
        )
    )
    return bool(grant and grant.can_view)


def can_publish_image(db: Session, p: Principal, image: Image) -> bool:
    if p.has("result:publish-any"):
        return True
    grant = db.scalar(
        select(ImageGrant).where(
            ImageGrant.image_id == image.id, ImageGrant.user_id == p.id
        )
    )
    return bool(grant and grant.can_publish)
