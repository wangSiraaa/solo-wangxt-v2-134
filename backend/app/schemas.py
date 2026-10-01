from pydantic import BaseModel, Field


class GrantIn(BaseModel):
    user_id: int
    can_view: bool = False
    can_publish: bool = False


class SyntheticImageIn(BaseModel):
    name: str = "synthetic"
    height: int = Field(default=1024, ge=64, le=24000)
    width: int = Field(default=1024, ge=64, le=24000)
    channels: int = Field(default=4, ge=1, le=16)
    components: int = Field(default=3, ge=1, le=8)
    seed: int = 7


class ControlSampleIn(BaseModel):
    name: str
    fluorophore: str
    spectrum: list[float]


class MatrixVersionIn(BaseModel):
    name: str
    matrix: list[list[float]] | None = None
    note: str | None = None


class MatrixFromControlsIn(BaseModel):
    name: str
    channels: int
    note: str | None = None


class JobIn(BaseModel):
    image_id: int
    matrix_version_id: int | None = None
    algorithm: str = "nnls"


class ReleaseIn(BaseModel):
    job_id: int
