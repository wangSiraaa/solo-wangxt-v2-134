from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ControlSampleCreate(BaseModel):
    name: str
    component_name: str
    image_id: str | None = None
    description: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class MatrixCreate(BaseModel):
    name: str
    channel_names: list[str]
    component_names: list[str]
    coefficients: list[list[float]]
    control_sample_ids: list[str] = Field(default_factory=list)
    matrix_key: str | None = None
    publish: bool = False
    allow_duplicate_version: bool = False


class JobCreate(BaseModel):
    image_id: str
    matrix_version_id: str
    algorithm: Literal["nnls", "fista_nnls"] = "fista_nnls"
    algorithm_params: dict[str, Any] = Field(default_factory=dict)


class PermissionGrant(BaseModel):
    user_id: str
    permission: Literal["view_source", "view_derived", "publish_result"]


class PublishRequest(BaseModel):
    force: bool = False
