from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from .database import decode_token, get_db
from .models import Image, ImagePermission, User


def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    # OpenSeadragon's image loader cannot set Authorization headers; image
    # tile routes therefore accept the same short-lived bearer token in query.
    elif request.query_params.get("access_token"):
        token = request.query_params["access_token"]
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None
    user = db.get(User, payload["sub"])
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return user


def require_role(*roles: str):
    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {user.role} may not perform this action")
        return user

    return checker


def has_image_permission(db: Session, image_id: str, user: User, permission: str) -> bool:
    if user.role == "admin":
        return True
    row = (
        db.query(ImagePermission)
        .filter(
            ImagePermission.image_id == image_id,
            ImagePermission.user_id == user.id,
            ImagePermission.permission == permission,
        )
        .one_or_none()
    )
    return row is not None


def require_image_permission(permission: str):
    def checker(image_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        if not db.get(Image, image_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "image not found")
        if not has_image_permission(db, image_id, user, permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing {permission}")
        return user

    return checker
